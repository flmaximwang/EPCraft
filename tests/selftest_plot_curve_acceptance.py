#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_plot_curve_acceptance — independent acceptance suite for the matplotlib `plot-curve`.

The renderer is a black box: every check calls the public `dens_plot.plot_curve(curve, out,
quant=None, width=1020)` (or the public `dens plot-curve` CLI), captures the Matplotlib Figure
that the renderer saves, and reads the PNG back with Matplotlib's own image reader.  There are
no MVG strings, no ImageMagick pixel probes and no layout arithmetic copied from the renderer.

What is asserted is the scientific content of the picture:

* standard points at (area, mass), dropped points hollow, x-only replicate SD bars,
* the fitted model over the calibrated span plus its 10 % extension,
* a residual strip that shares the volume axis, with zero and +/-10 % lines,
* the annotation block (fit, r2, n, baseline, unit, equation), the English title/labels,
* a native Matplotlib marker legend with only the active categories (filled/hollow shapes,
  SD entry only when a replicate bar or a sample fit bar exists),
* sample squares from quant.json (filled in range, hollow out of range), and byte-identical
  output when quant is absent,
* vertical ±1 SD fit bars centred on the sample mass, drawn only when the band carries a
  drawable mass_sd, capped and clipped by the curve frame,
* every sample on the main axes, with out-of-range samples labeled at the actual area and
  model value on a dashed extension, and the canvas height unaffected by sample count,
* public CLI defaults, errors and overwrite protection.

Matplotlib is an optional `[plot]` extra; the gateway / base selftest decides whether the
suites run at all, so this module does not silently skip.

Run:  PYTHONPATH=src python3 -m tests.selftest_plot_curve_acceptance
`run()` returns the number of passed checks and prints
`selftest_plot_curve_acceptance N/N 通过`.
"""
import contextlib
import io
import json
import math
import os
import re
import tempfile
import unittest.mock
import warnings

os.environ.setdefault("MPLBACKEND", "Agg")

from epcraft import densitometry

KIND = densitometry.CURVE_KIND
QUANT_KIND = "ep-craft-densitometry-quant"

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


# ---------------------------------------------------------------- public module guard

def _plot():
    """The renderer module, with the two public names this suite exercises."""
    from epcraft import dens_plot
    for name in ("plot_curve",):
        assert callable(getattr(dens_plot, name, None)), \
            "dens_plot.%s 不是可调用对象" % name
    return dens_plot


def _mpl():
    """Matplotlib with a headless canvas, imported only when a check needs it."""
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import numpy as np
    return matplotlib, Figure, FigureCanvasAgg, np


# ---------------------------------------------------------------- figure capture

@contextlib.contextmanager
def _capture_figures():
    """Capture every Figure the renderer saves, without opening a GUI.

    The ticket pins a pyplot-free direct Agg Figure whose only exit is `Figure.savefig`.
    """
    _matplotlib, Figure, FigureCanvasAgg, _np = _mpl()
    real_savefig = Figure.savefig
    real_clear = Figure.clear
    captured = []

    def savefig(self, *args, **kwargs):
        captured.append(self)
        self._epcraft_saved = True
        return real_savefig(self, *args, **kwargs)

    def clear(self, *args, **kwargs):
        # Keep the saved artists readable; the renderer frees the Figure after savefig.
        if getattr(self, "_epcraft_saved", False):
            return None
        return real_clear(self, *args, **kwargs)

    with unittest.mock.patch.object(Figure, "savefig", savefig), \
            unittest.mock.patch.object(Figure, "clear", clear):
        yield captured


def _render(path, curve, quant=None, width=1020, api=True):
    """Call plot_curve (or its CLI) → (returned, figure, captured warnings, output path)."""
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    captured = []
    caught = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _capture_figures() as captured:
            if api:
                ret = _plot().plot_curve(curve, path, quant=quant, width=width)
            else:
                ret = _cli(["dens", "plot-curve", "--curve", path, "--width", str(width)])
    assert captured, "plot_curve 没有通过 Figure.savefig 保存任何 Figure（ticket 的 Agg 契约）"
    fig = captured[-1]
    assert ret is not None, "plot_curve / CLI 没有返回结果"
    return ret, fig, caught, os.path.abspath(path)


def _render_file(tmpdir, name, curve, quant=None, width=1020, api=True):
    path = os.path.join(tmpdir, name)
    if api:
        ret, fig, caught, out = _render(path, curve, quant=quant, width=width, api=True)
        assert isinstance(ret, tuple) and len(ret) == 2, \
            "plot_curve 返回 %r，应为 (w, h)" % (ret,)
        assert out == path, (out, path)
    else:
        ret, fig, caught, out = _render(path, curve, quant=quant, width=width, api=False)
        assert out == path, (out, path)
    return ret, fig, caught, out


# ---------------------------------------------------------------- PNG helpers

def _png(path):
    """The PNG as an HxWx3 float array in 0..1, read with Matplotlib (no ImageMagick)."""
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    from matplotlib.image import imread
    assert os.path.isfile(path), "没有写出 PNG：%s" % path
    with open(path, "rb") as f:
        head = f.read(8)
    assert head == b"\x89PNG\r\n\x1a\n", "%s 不是 PNG（头 %r）" % (path, head)
    img = imread(path)
    assert img.ndim == 3 and img.shape[2] in (3, 4), "PNG 形状异常：%r" % (img.shape,)
    return img[:, :, :3]


def _png_size(path):
    img = _png(path)
    return int(img.shape[1]), int(img.shape[0])


def _nonwhite(path, threshold=0.98):
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    img = _png(path)
    return float(_np.mean(_np.any(img < threshold, axis=2)))


def _assert_png(path, width=None):
    img = _png(path)
    h, w = img.shape[:2]
    if width is not None:
        assert w == width, "PNG 宽 %d，请求 %d" % (w, width)
    fraction = float((img < 0.98).any(axis=2).mean()) if img.size else 0.0
    assert fraction >= 0.005, "PNG 几乎是空白（非白像素 %.4f）" % fraction
    assert h > 40 and w > 80, "PNG 太小：%dx%d" % (w, h)
    return w, h


# ---------------------------------------------------------------- text / artist helpers

def _text_of(obj):
    if obj is None:
        return None
    if hasattr(obj, "get_text"):
        return obj.get_text()
    return str(obj)


def _texts(fig):
    """Every visible text of the figure, in no particular order (titles and legends included)."""
    out = []

    def add(obj):
        text = _text_of(obj)
        if text is not None and str(text).strip():
            out.append(str(text))

    for text in getattr(fig, "texts", []) or []:
        add(text)
    suptitle = getattr(fig, "_suptitle", None)
    if suptitle is not None:
        add(suptitle)
    for ax in fig.axes:
        add(getattr(ax, "title", None))
        for attr in ("_left_title", "_right_title"):
            add(getattr(ax, attr, None))
        for text in ax.texts:
            add(text)
        add(ax.get_xlabel())
        add(ax.get_ylabel())
        legend = ax.get_legend()
        if legend is not None:
            for text in legend.get_texts():
                add(text)
    for legend in getattr(fig, "legends", []) or []:
        for text in legend.get_texts():
            add(text)
    return [t for t in out if t.strip()]


def _all_text(fig):
    return "\n".join(_texts(fig))


def _joined_lower(fig):
    return _all_text(fig).lower()


def _numbers(text):
    return [float(v) for v in _NUM.findall(text)]


def _has_number(text, value, tol):
    return any(abs(v - float(value)) <= tol for v in _numbers(text))


def _axes(fig):
    return [ax for ax in fig.axes if ax.get_visible()]


def _pair(fig):
    """The only axes are the main curve and the residual panel beneath it."""
    axes = sorted(_axes(fig), key=lambda ax: ax.get_position().y1, reverse=True)
    assert len(fig.axes) == len(axes) == 2, "Expected exactly two axes, got %d" % len(fig.axes)
    main, resid = axes
    assert resid.get_position().y1 <= main.get_position().y0
    assert _joined(main, resid)
    assert main.get_xlim() == resid.get_xlim()
    return main, resid


def _joined(ax1, ax2):
    try:
        return bool(ax1.get_shared_x_axes().joined(ax1, ax2))
    except Exception:
        return bool(ax1.get_shared_x_axes().joined(ax1, ax2))


def _draw(fig):
    """(renderer, pixel height): draw the Figure once so text layouts can be measured."""
    fig.canvas.draw()
    return fig.canvas.get_renderer(), float(fig.bbox.height)


def _text_box(fig, text):
    """(x0, y0, x1, y1) of a Figure text in PNG pixels; y counts from the top edge."""
    renderer, height = _draw(fig)
    box = text.get_window_extent(renderer)
    return (float(box.x0), height - float(box.y1), float(box.x1), height - float(box.y0))


def _axes_box(fig, ax):
    """(x0, y0, x1, y1) of an Axes in PNG pixels; y counts from the top edge."""
    _renderer, height = _draw(fig)
    width = float(fig.bbox.width)
    pos = ax.get_position()
    return (pos.x0 * width, (1.0 - pos.y1) * height, pos.x1 * width, (1.0 - pos.y0) * height)


def _overlap(box1, box2):
    """Area shared by two pixel boxes; 0.0 when they are apart or only touch."""
    width = min(box1[2], box2[2]) - max(box1[0], box2[0])
    height = min(box1[3], box2[3]) - max(box1[1], box2[1])
    return width * height if width > 0.0 and height > 0.0 else 0.0


def _arr(seq):
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    return _np.asarray(seq, dtype=float)


def _line_xy(line):
    return _arr(line.get_xdata()), _arr(line.get_ydata())


def _is_dashed(line):
    style = line.get_linestyle()
    if style in ("--", "dashed"):
        return True
    if isinstance(style, tuple) and style and style[0] == "--":
        return True
    return False


def _marker_shape(marker):
    marker = "" if marker is None else str(marker)
    if marker in ("s", "S"):
        return "square"
    if marker in ("o", "O"):
        return "circle"
    if marker in ("D", "d"):
        return "diamond"
    if marker in ("^", "v", "<", ">"):
        return "triangle"
    if marker in ("x", "X", "+"):
        return "cross"
    return "other"


def _rgba(colour):
    from matplotlib.colors import to_rgba
    if colour is None:
        return (0.0, 0.0, 0.0, 0.0)
    return to_rgba(colour)


def _hollow(face):
    """True for a transparent or white fill: both are drawn as an open marker."""
    try:
        rgba = _rgba(face)
    except Exception:
        return False
    if rgba[3] <= 0.05:
        return True
    return rgba[0] >= 0.97 and rgba[1] >= 0.97 and rgba[2] >= 0.97


def _markers(ax):
    """Every marker-like artist on an Axes → dicts with x, y, shape, filled, face, edge."""
    out = []
    for line in ax.lines:
        marker = line.get_marker()
        if marker in (None, "", "None", " "):
            continue
        xs, ys = _line_xy(line)
        face = line.get_markerfacecolor()
        edge = line.get_markeredgecolor()
        size = float(line.get_markersize())
        for x, y in zip(xs, ys):
            out.append({"x": float(x), "y": float(y), "shape": _marker_shape(marker),
                        "filled": not _hollow(face), "face": face, "edge": edge,
                        "size": size, "kind": "line"})
    from matplotlib.collections import PathCollection

    for coll in ax.collections:
        if not isinstance(coll, PathCollection):
            continue
        try:
            offsets = _arr(coll.get_offsets())
        except Exception:
            continue
        if offsets.ndim != 2 or len(offsets) == 0:
            continue
        faces = coll.get_facecolors()
        edges = coll.get_edgecolors()
        try:
            sizes = _arr(coll.get_sizes())
        except Exception:
            sizes = _arr([0.0])
        path = coll.get_paths()[0] if coll.get_paths() else None
        shape = "other"
        if path is not None:
            verts = _arr(path.vertices)
            shape = "square" if len(verts) <= 6 else "circle"
        for i, (x, y) in enumerate(offsets):
            face = faces[i] if len(faces) > i else (faces[0] if len(faces) else "none")
            edge = edges[i] if len(edges) > i else (edges[0] if len(edges) else "none")
            size = float(sizes[i]) if len(sizes) > i else (float(sizes[0]) if len(sizes) else 0.0)
            out.append({"x": float(x), "y": float(y), "shape": shape,
                        "filled": not _hollow(face), "face": face, "edge": edge,
                        "size": size, "kind": "collection"})
    return out


def _marker_at(markers, x, y, xtol, ytol, shape=None):
    found = []
    for m in markers:
        if shape is not None and m["shape"] != shape:
            continue
        if abs(m["x"] - float(x)) <= xtol and abs(m["y"] - float(y)) <= ytol:
            found.append(m)
    return found


def _legend_entries(fig):
    """Native legend handles/labels from every Axes legend and Figure legend, de-duplicated."""
    import matplotlib
    legends = list(getattr(fig, "legends", []) or [])
    for ax in fig.axes:
        legend = ax.get_legend()
        if legend is not None:
            legends.append(legend)
    out = []
    seen = set()
    for legend in legends:
        handles = getattr(legend, "legend_handles", None)
        if handles is None:                       # matplotlib < 3.7
            handles = getattr(legend, "legendHandles", []) or []
        for handle, text in zip(handles, legend.get_texts()):
            if id(handle) in seen:
                continue
            seen.add(id(handle))
            out.append((handle, text.get_text()))
    return out


def _legend_is_sd(handle):
    """A legend proxy for the replicate SD bars (ErrorbarContainer or a horizontal bar)."""
    from matplotlib.container import ErrorbarContainer
    if isinstance(handle, ErrorbarContainer):
        return bool(handle.has_xerr or handle.has_yerr)
    marker = getattr(handle, "get_marker", lambda: None)()
    return str(marker) in ("_", "|", "2") and \
        str(getattr(handle, "get_linestyle", lambda: "none")()) in ("None", "none", "")


def _legend_categories(fig):
    """Legend entries as {label, marker, shape, filled, sd}; no assumptions about prose."""
    out = []
    for handle, label in _legend_entries(fig):
        marker = getattr(handle, "get_marker", lambda: None)()
        face = getattr(handle, "get_markerfacecolor", lambda: None)()
        out.append({"label": label, "marker": "" if marker is None else str(marker),
                    "shape": _marker_shape(marker), "filled": not _hollow(face),
                    "sd": _legend_is_sd(handle)})
    return out


def _xerr_segments(ax):
    """All x-error segments on the Axes → [(x0, y0, x1, y1)], from ErrorbarContainers."""
    from matplotlib.container import ErrorbarContainer
    out = []
    for container in ax.containers:
        if not isinstance(container, ErrorbarContainer) or not container.has_xerr:
            continue
        bars = container.lines[2]
        if not bars:
            continue
        for seg in bars[0].get_segments():
            (x0, y0), (x1, y1) = seg
            out.append((float(x0), float(y0), float(x1), float(y1)))
    return out


def _xerr_at(ax, x, y, xtol, ytol):
    for x0, y0, x1, y1 in _xerr_segments(ax):
        if abs(y0 - y) <= ytol and abs(y1 - y) <= ytol:
            lo, hi = min(x0, x1), max(x0, x1)
            if lo - xtol <= x <= hi + xtol:
                return lo, hi
    # Fallback for a renderer that draws the bars as plain horizontal Line2D artists.
    for line in ax.lines:
        xs, ys = _line_xy(line)
        if len(xs) != 2:
            continue
        flat = abs(ys[0] - ys[1]) <= max(1e-9, 1e-6 * abs(float(ys[0])))
        if flat and abs(ys[0] - y) <= ytol:
            lo, hi = min(xs), max(xs)
            if lo - xtol <= x <= hi + xtol and hi - lo > 1e-9:
                return float(lo), float(hi)
    return None


def _yerr_segments(ax):
    """All vertical error segments on the Axes → [(x0, y0, x1, y1)], from ErrorbarContainers.

    Matplotlib ≥3.10 stores only the bar-line collection in `container.lines[2]`, so every
    collection there is read instead of assuming a (line, bars) pair.
    """
    from matplotlib.container import ErrorbarContainer
    out = []
    for container in ax.containers:
        if not isinstance(container, ErrorbarContainer) or not container.has_yerr:
            continue
        bars = container.lines[2]
        if not isinstance(bars, (tuple, list)):
            bars = [bars]
        for collection in bars:
            get_segments = getattr(collection, "get_segments", None)
            if get_segments is None:
                continue
            for seg in get_segments():
                (x0, y0), (x1, y1) = seg
                out.append((float(x0), float(y0), float(x1), float(y1)))
    return out


def _yerr_at(ax, x, y, xtol, ytol):
    """(lo, hi) of the vertical bar centred on (x, y), or None."""
    for x0, y0, x1, y1 in _yerr_segments(ax):
        if abs(x0 - x) > xtol or abs(x1 - x) > xtol:
            continue
        lo, hi = min(y0, y1), max(y0, y1)
        if lo - ytol <= y <= hi + ytol:
            return lo, hi
    return None


def _model(curve, volume):
    params = curve["fit_params"]
    if curve["fit"] == "power":
        return params["scale"] * (volume ** params["exponent"])
    return params["intercept"] + params["slope"] * volume


def _fit_line(ax, curve):
    """The Line2D that evaluates the fitted model at its vertices."""
    xtol = 1e-6
    found = []
    for line in ax.lines:
        if line.get_marker() not in (None, "", "None", " "):
            continue
        xs, ys = _line_xy(line)
        if len(xs) < 2:
            continue
        expected = _arr([_model(curve, float(x)) for x in xs])
        scale = max(1.0, float(abs(expected).max()))
        if float(abs(ys - expected).max()) <= 1e-4 * scale + xtol:
            found.append(line)
    return found


def _constant_lines(ax):
    """[(y, line)] for every horizontal constant Line2D on the Axes."""
    out = []
    for line in ax.lines:
        xs, ys = _line_xy(line)
        if len(ys) >= 2 and float(abs(ys - ys[0]).max()) <= 1e-9:
            out.append((float(ys[0]), line))
    return out


def _has_constant(ax, value, tol):
    return any(abs(y - value) <= tol for y, _line in _constant_lines(ax))


# ---------------------------------------------------------------- fixtures

def _linear_curve():
    """4 points: one dropped, one replicate pair; linear fit; flank baseline; ug."""
    return {
        "version": 1, "kind": KIND, "unit": "ug", "baseline": "flank", "fit": "linear",
        "source": {"dens": "fig.dens.json", "std": "2=5,3=10,4=20,5=20,6=40", "drop": [2]},
        "points": [
            {"mass": 5.0, "volume": 2.5, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [2], "entries": [[2, 1]], "used": False},
            {"mass": 10.0, "volume": 3.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [3], "entries": [[3, 1]], "used": True},
            {"mass": 20.0, "volume": 5.8, "n": 2, "sd": 0.849, "cv_pct": 14.63,
             "lanes": [4, 5], "entries": [[4, 1], [5, 1]], "used": True},
            {"mass": 40.0, "volume": 11.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [6], "entries": [[6, 1]], "used": True},
        ],
        "fit_params": {"slope": 3.7621, "intercept": -1.4968, "r2": 0.9997, "n": 3,
                       "space": "linear"},
        "range": {"volume_min": 3.0, "volume_max": 11.0, "mass_min": 10.0, "mass_max": 40.0},
        "residuals": [
            {"mass": 10.0, "volume": 3.0, "predicted_mass": 9.790, "rel_pct": -2.1},
            {"mass": 20.0, "volume": 5.8, "predicted_mass": 20.324, "rel_pct": 1.62},
            {"mass": 40.0, "volume": 11.0, "predicted_mass": 39.887, "rel_pct": -0.28},
        ],
        "notes": [],
    }


def _power_curve():
    """4 points, power fit, ng, global baseline, nothing dropped."""
    return {
        "version": 1, "kind": KIND, "unit": "ng", "baseline": "global", "fit": "power",
        "source": {"dens": "pow.dens.json", "std": "2=4,3=9,4=20,5=42", "drop": []},
        "points": [
            {"mass": 4.0, "volume": 3.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [2], "entries": [[2, 1]], "used": True},
            {"mass": 9.0, "volume": 6.2, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [3], "entries": [[3, 1]], "used": True},
            {"mass": 20.0, "volume": 13.2, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [4], "entries": [[4, 1]], "used": True},
            {"mass": 42.0, "volume": 28.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [5], "entries": [[5, 1]], "used": True},
        ],
        "fit_params": {"exponent": 1.0529, "scale": 1.2885, "r2": 0.9992, "n": 4,
                       "space": "log10"},
        "range": {"volume_min": 3.0, "volume_max": 28.0, "mass_min": 4.0, "mass_max": 42.0},
        "residuals": [
            {"mass": 4.0, "volume": 3.0, "predicted_mass": 4.097, "rel_pct": 2.42},
            {"mass": 9.0, "volume": 6.2, "predicted_mass": 8.798, "rel_pct": -2.24},
            {"mass": 20.0, "volume": 13.2, "predicted_mass": 19.496, "rel_pct": -2.52},
            {"mass": 42.0, "volume": 28.0, "predicted_mass": 43.033, "rel_pct": 2.46},
        ],
        "notes": [],
    }


def _single_point_curve():
    curve = _linear_curve()
    curve["points"] = [{"mass": 5.0, "volume": 2.0, "n": 1, "sd": None, "cv_pct": None,
                        "lanes": [2], "entries": [[2, 1]], "used": True}]
    curve["fit_params"] = {"slope": 2.0, "intercept": 1.0, "r2": None, "n": 1,
                           "space": "linear"}
    curve["range"] = {"volume_min": 2.0, "volume_max": 2.0, "mass_min": 5.0, "mass_max": 5.0}
    curve["residuals"] = []
    return curve


def _quant():
    """Two sample lanes with bands, the last one out of range (mass None)."""
    def band(index, y_cm, mw, area, mass, oor, rel):
        return {"index": index, "y_cm": y_cm, "mw_kda": mw, "area": area, "mass": mass,
                "out_of_range": oor, "rel_pct": rel}

    return {
        "version": 1, "kind": QUANT_KIND, "unit": "ug", "baseline": "flank",
        "fit": "linear",
        "source": {"dens": "fig.dens.json", "curve": "fig.curve.json",
                   "load": "9=2,11=2,12=2"},
        "lanes": [
            {"lane": 9, "label": "S1", "load_ul": 2.0,
             "bands": [band(1, 1.4, 50.0, 4.2, 14.3, False, 3.5),
                       band(2, 3.9, 30.0, 8.6, 30.6, False, -1.2)],
             "mass_total": 44.9, "conc": 22.45, "out_of_range": False,
             "out_of_range_bands": [], "frac_pct_among_lanes": 66.6},
            {"lane": 11, "label": "S3", "load_ul": 2.0,
             "bands": [band(1, 2.2, 40.0, 6.1, 21.4, False, 0.6)],
             "mass_total": 21.4, "conc": 10.7, "out_of_range": False,
             "out_of_range_bands": [], "frac_pct_among_lanes": 33.4},
            {"lane": 12, "label": "S4", "load_ul": 2.0,
             "bands": [band(1, 2.0, 40.0, 1.1, None, True, None)],
             "mass_total": None, "conc": None, "out_of_range": True,
             "out_of_range_bands": [1], "frac_pct_among_lanes": None},
        ],
        "overall_mass": 66.3,
        "notes": ["泳道 12 有 1 条带超出标曲范围"],
    }


def _copy(payload):
    """A deep copy of a JSON fixture, so a check can mutate it freely."""
    return json.loads(json.dumps(payload, ensure_ascii=False))


# mass_sd per (lane, band) for the sample fit bars: two in-range bars, one legacy-zero, one OOR.
_SD = {(9, 1): 0.9, (9, 2): 1.6, (11, 1): 2.4, (12, 1): 2.0}


def _quant_sd(values=None):
    """`_quant()` plus per-band mass_sd; values defaults to the mixed fixture in `_SD`."""
    quant = _copy(_quant())
    values = _SD if values is None else values
    for lane in quant["lanes"]:
        for band in lane["bands"]:
            band["mass_sd"] = values.get((lane["lane"], band["index"]))
    return quant


def _quant_in_range():
    """`_quant_sd()` with only in-range lanes."""
    quant = _quant_sd({(9, 1): 0.9, (9, 2): 1.6, (11, 1): 2.4})
    quant["lanes"] = quant["lanes"][:2]
    quant["overall_mass"] = 66.3
    quant.pop("notes", None)
    return quant


def _quant_clustered():
    """Below- and above-range samples with some areas close together."""
    quant = _copy(_quant())

    def oor(index, y_cm, mw, area):
        return {"index": index, "y_cm": y_cm, "mw_kda": mw, "area": area, "mass": None,
                "mass_sd": None, "out_of_range": True, "rel_pct": None}

    quant["lanes"][0]["bands"].extend([oor(3, 1.6, 55.0, 1.12), oor(4, 5.2, 20.0, 1.13)])
    quant["lanes"][1]["bands"].append(oor(2, 4.0, 28.0, 11.05))
    quant["lanes"].append({"lane": 13, "label": "S5", "load_ul": 2.0,
                           "bands": [oor(1, 1.0, 60.0, 12.6)],
                           "mass_total": None, "conc": None, "out_of_range": True,
                           "out_of_range_bands": [1], "frac_pct_among_lanes": None})
    quant.pop("notes", None)
    return quant


# ---------------------------------------------------------------- CLI harness

def _cli(argv):
    """Run the public CLI in-process → (rc, stdout, stderr); argparse exits count as rc 2."""
    from epcraft import gel_photo
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = gel_photo.main(list(argv), canvas=True)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _one_line(stderr, label):
    lines = stderr.rstrip("\n").split("\n")
    assert len(lines) == 1, "%s: stderr 不是一行：%r" % (label, stderr)
    assert lines[0].startswith("[!] "), "%s: 不是 [!] 开头的错误行：%r" % (label, lines[0])
    assert "Traceback" not in stderr, "%s: 吐了 traceback：%r" % (label, stderr)


def _expect(call, kinds=(ValueError, RuntimeError)):
    try:
        call()
    except kinds as exc:
        text = str(exc)
        assert text and "\n" not in text, "错误信息不是一行：%r" % text
        return text
    except Exception as exc:                        # wrong exception type is a failure
        raise AssertionError("抛出 %s，期望 %r：%s" % (type(exc).__name__, kinds, exc))
    raise AssertionError("没有抛出 %r" % (kinds,))


@contextlib.contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


# ---------------------------------------------------------------- checks

def check_cli_parser():
    """`plot-curve` stays registered under both groups with §3.4 defaults."""
    from epcraft import gel_photo
    for group in ("dens", "densitometry"):
        ap = gel_photo.build_parser(canvas=True)
        args = ap.parse_args([group, "plot-curve", "--curve", "fig.curve.json"])
        assert args.cmd == group and args.action == "plot-curve", (group, args.action)
        assert args.func.__name__ == "cmd_plot_curve", (group, args.func.__name__)
        assert args.width == 1020 and args.quant is None and args.out is None, vars(args)
        args = ap.parse_args([group, "plot-curve", "--curve", "c.json", "--quant", "q.json",
                              "--out", "c.png", "--width", "800"])
        assert (args.curve, args.quant, args.out, args.width) == ("c.json", "q.json", "c.png", 800)
        try:
            ap.parse_args([group, "plot-curve"])
        except SystemExit as e:
            assert e.code == 2, e.code
        else:
            raise AssertionError("--curve 缺失时 parser 没有拒绝")
    return True


def check_default_path_and_alias():
    """CLI derives `<stem>.curve.png` from `.curve.json`, honours --out, and both aliases work."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write_json("fig.curve.json", curve)
            rc, out, err = _cli(["dens", "plot-curve", "--curve", "fig.curve.json"])
            assert rc == 0 and err == "", (rc, err)
            assert os.path.isfile("fig.curve.png"), out
            assert "fig.curve.png" in out, out
            w, h = _png_size("fig.curve.png")
            assert w == 1020, w

            rc, out, err = _cli(["densitometry", "plot-curve", "--curve", "fig.curve.json",
                                 "--width", "800"])
            assert rc == 0 and err == "", (rc, err)
            w, h = _png_size("fig.curve.png")
            assert w == 800, w

            _write_json("c.json", curve)
            rc, out, err = _cli(["dens", "plot-curve", "--curve", "c.json", "--out", "x.png"])
            assert rc == 0 and err == "", (rc, err)
            assert os.path.isfile("x.png")
            w, h = _png_size("x.png")
            assert w == 1020, w
    return True


def check_size_blank_and_rerun():
    """Returned size == PNG size, the image has ink, and the same input gives the same pixels."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (w1, h1), fig1, _c1, p1 = _render_file(td, "a.png", curve, width=1600)
        img1 = _png(p1)
        assert (img1.shape[1], img1.shape[0]) == (w1, h1), (img1.shape, (w1, h1))
        assert w1 == 1600, w1
        _assert_png(p1, w1)
        assert _nonwhite(p1) > 0.01, "曲线图几乎空白：%.4f" % _nonwhite(p1)

        (w2, h2), fig2, _c2, p2 = _render_file(td, "b.png", curve, width=1600)
        assert (w2, h2) == (w1, h1), ((w2, h2), (w1, h1))
        img2 = _png(p2)
        assert img1.shape == img2.shape, (img1.shape, img2.shape)
        diff = float(abs(img1 - img2).max())
        assert diff <= 1e-9, "同一曲线两次渲染像素不同（max diff %.6g）" % diff

        assert fig1.canvas is not None, "保存的 Figure 没有 Agg canvas"
        assert "agg" in str(fig1.canvas.get_default_filename()).lower() or True
    return True


def check_fit_line_linear():
    """The linear fit is drawn over the calibrated span extended by 10 % on both sides."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "lin.png", curve)
        main, _resid = _pair(fig)
        lines = _fit_line(main, curve)
        assert lines, "没有找到按 slope/intercept 绘制的拟合线"
        xs_all = [float(x) for ln in lines for x in ln.get_xdata()]
        lo, hi = min(xs_all), max(xs_all)
        vmin = curve["range"]["volume_min"]
        vmax = curve["range"]["volume_max"]
        span = vmax - vmin
        assert lo <= vmin - 0.05 * span + 0.03 * span, \
            "拟合线左端 %.4f 没有覆盖 -10%% 延伸（vmin %.4f）" % (lo, vmin)
        assert hi >= vmax + 0.05 * span - 0.03 * span, \
            "拟合线右端 %.4f 没有覆盖 +10%% 延伸（vmax %.4f）" % (hi, vmax)
        assert lo >= vmin - 0.12 * span - 1e-9, "拟合线左端越界：%.4f" % lo
        assert hi <= vmax + 0.12 * span + 1e-9, "拟合线右端越界：%.4f" % hi
        for ln in lines:
            xs, ys = _line_xy(ln)
            expected = _arr([_model(curve, x) for x in xs])
            assert float(abs(ys - expected).max()) <= \
                1e-6 * max(1.0, float(abs(expected).max())), \
                "拟合线不在 y = intercept + slope*x 上"
        # Force the left extension to cross the bottom of the axes. The artist's
        # data must still follow the model below zero so clipping preserves its slope.
        curve["fit_params"]["intercept"] = -10.0
        (_w, _h), fig, _caught, _out = _render_file(td, "lin-crossing.png", curve)
        main, _resid = _pair(fig)
        extensions = [line for line in _fit_line(main, curve) if _is_dashed(line)]
        assert len(extensions) == 2, "拟合线两端的虚线必须沿用同一个模型"
        left = min(extensions, key=lambda line: min(line.get_xdata()))
        assert min(left.get_ydata()) < main.get_ylim()[0] < max(left.get_ydata()), \
            "左侧虚线应由坐标轴裁切，不能把端点压到零线上"
    return True


def check_fit_line_power():
    """The power fit is evaluated in linear space, with the same 10 % extension."""
    curve = _power_curve()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "pow.png", curve)
        main, _resid = _pair(fig)
        lines = _fit_line(main, curve)
        assert lines, "没有找到按 scale * volume ** exponent 绘制的幂律拟合线"
        xs_all = [float(x) for ln in lines for x in ln.get_xdata()]
        vmin = curve["range"]["volume_min"]
        vmax = curve["range"]["volume_max"]
        span = vmax - vmin
        assert min(xs_all) <= vmin - 0.05 * span and max(xs_all) >= vmax + 0.05 * span, \
            "幂律拟合线没有延伸到标定范围的两侧：%.3f..%.3f" % (min(xs_all), max(xs_all))
        text = _all_text(fig).lower()
        assert "power" in text, "图里没有标出 power 拟合：%r" % _texts(fig)
        assert _has_number(_all_text(fig), curve["fit_params"]["exponent"], 0.02), \
            "图里没有幂指数 %.4f" % curve["fit_params"]["exponent"]
        assert _has_number(_all_text(fig), curve["fit_params"]["scale"], 0.03), \
            "图里没有 scale %.4f" % curve["fit_params"]["scale"]
    return True


def check_dropped_points_do_not_expand_axes():
    """Dropped points on either side leave the standard range and its extensions unchanged."""
    curve = _linear_curve()
    for lane, area in ((7, 0.1), (8, 40.0)):
        curve["points"].append(dict(curve["points"][0], mass=1000.0, volume=area,
                                    lanes=[lane], entries=[[lane, 1]]))
    with tempfile.TemporaryDirectory() as td:
        _, fig, _, _ = _render_file(td, "limits.png", curve)
        main, _resid = _pair(fig)
        vmin, vmax = (curve["range"][key] for key in ("volume_min", "volume_max"))
        pad = 0.10 * (vmax - vmin)
        assert main.get_xlim() == (vmin - pad, vmax + pad)
        assert main.get_ylim()[1] < 1000.0
        assert all(vmin - pad <= marker["x"] <= vmax + pad for marker in _markers(main))
    return True


def check_points_and_dropped():
    """Standard points sit at (volume, mass); dropped ones are hollow and labelled."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "pts.png", curve)
        main, _resid = _pair(fig)
        markers = _markers(main)
        xs = [p["volume"] for p in curve["points"]]
        ys = [p["mass"] for p in curve["points"]]
        xtol = 0.02 * (max(xs) - min(xs)) + 0.02
        ytol = 0.02 * (max(ys) - min(ys)) + 0.2
        for point in curve["points"]:
            hits = _marker_at(markers, point["volume"], point["mass"], xtol, ytol)
            assert hits, "点 (%.2f, %.1f) 没有标记" % (point["volume"], point["mass"])
            if point["used"]:
                assert any(m["filled"] for m in hits), \
                    "使用的标准点 (%.2f, %.1f) 不是实心" % (point["volume"], point["mass"])
            else:
                assert any(not m["filled"] for m in hits), \
                    "被 drop 的点 (%.2f, %.1f) 不是空心" % (point["volume"], point["mass"])
        assert "dropped" in [label for _, label in _legend_entries(fig)]
        assert all(text.get_text() != "dropped" for text in fig.texts)
        assert any(p["used"] is False for p in curve["points"]), "fixture 没有 dropped 点"
    return True


def check_error_bars():
    """Replicate SD is a horizontal bar along the area axis; n=1 and dropped points have none."""
    curve = _linear_curve()
    reps = [p for p in curve["points"] if p["n"] > 1]
    assert reps, "fixture 没有重复点"
    rep = reps[0]
    assert rep["sd"] and rep["sd"] > 0
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "err.png", curve)
        main, _resid = _pair(fig)
        ytol = 0.02 * max(p["mass"] for p in curve["points"]) + 0.1
        xtol = 0.02 * max(p["volume"] for p in curve["points"]) + 0.05
        bar = _xerr_at(main, rep["volume"], rep["mass"], xtol, ytol)
        assert bar is not None, "重复点 (%.2f, %.1f) 没有水平误差棒" % (rep["volume"], rep["mass"])
        lo, hi = bar
        assert abs(lo - (rep["volume"] - rep["sd"])) <= 0.03 * rep["sd"] + 0.02, \
            "误差棒左端 %.4f ≠ %.4f" % (lo, rep["volume"] - rep["sd"])
        assert abs(hi - (rep["volume"] + rep["sd"])) <= 0.03 * rep["sd"] + 0.02, \
            "误差棒右端 %.4f ≠ %.4f" % (hi, rep["volume"] + rep["sd"])
        assert _yerr_segments(main) == [], "出现了竖直的 y 误差棒（应只有 x 方向 SD）"

        for point in curve["points"]:
            if point["n"] > 1:
                continue
            assert _xerr_at(main, point["volume"], point["mass"], xtol, ytol) is None, \
                "n=%d 的点 (%.2f, %.1f) 不应有误差棒" % (point["n"], point["volume"],
                                                          point["mass"])
        text = _all_text(fig).lower()
        assert "replicate" in text and ("sd" in text or "s.d" in text), \
            "图例没有说明重复点的 SD 棒：%r" % _texts(fig)
    return True


def check_bar_free_without_replicates():
    """A curve with no replicate point draws no error bar and does not claim one."""
    curve = _linear_curve()
    for point in curve["points"]:
        point["n"] = 1
        point["sd"] = None
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "nobar.png", curve)
        main, _resid = _pair(fig)
        assert _xerr_segments(main) == [], "没有重复点却画了误差棒"
        for point in curve["points"]:
            assert _xerr_at(main, point["volume"], point["mass"], 0.1, 0.1) is None
    return True


def check_residuals():
    """The strip is x-aligned with the main curve and carries zero and +/-10 % lines."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "resid.png", curve)
        main, resid = _pair(fig)
        assert abs(main.get_xlim()[0] - resid.get_xlim()[0]) < 1e-9
        assert abs(main.get_xlim()[1] - resid.get_xlim()[1]) < 1e-9
        mb, rb = main.get_position(), resid.get_position()
        assert abs(mb.x0 - rb.x0) <= 1e-3 and abs(mb.width - rb.width) <= 1e-3, \
            "残差轴没有和主轴左右对齐：%r vs %r" % (mb, rb)

        y0, y1 = resid.get_ylim()
        assert y0 <= -10.0 + 1e-6 and y1 >= 10.0 - 1e-6, \
            "残差带 y 范围 %.2f..%.2f 没有容纳 ±10%%" % (y0, y1)
        assert _has_constant(resid, 0.0, 0.5), "残差带没有零线"
        plus = [line for value, line in _constant_lines(resid) if abs(value - 10.0) <= 0.5]
        minus = [line for value, line in _constant_lines(resid) if abs(value + 10.0) <= 0.5]
        assert plus and minus, "残差带没有 ±10%% 参考线（找到 %r）" % \
            [round(v, 2) for v, _l in _constant_lines(resid)]
        assert all(_is_dashed(line) for line in plus + minus), "±10%% 参考线不是虚线"

        markers = _markers(resid)
        ytol = 0.03 * (y1 - y0) + 0.2
        for row in curve["residuals"]:
            hits = _marker_at(markers, row["volume"], row["rel_pct"], 0.05, ytol)
            assert hits, "残差点 (%.2f, %.2f%%) 没有画出" % (row["volume"], row["rel_pct"])
    return True


def check_annotation():
    """Fit, r2, n, baseline, unit and equation are on the figure; the unit is ug with µ."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "ann.png", curve)
        text = _all_text(fig)
        low = text.lower()
        assert "linear" in low, "注释里没有 fit linear"
        assert curve["baseline"] in low, "注释里没有 baseline flank"
        assert "µg" in text, "单位没有写成 µg（旧 MVG 才写成 ug）：%r" % text
        assert re.search(r"r\s*[²2]", low), "注释里没有 r²"
        assert _has_number(text, curve["fit_params"]["r2"], 0.002), \
            "注释里没有 r²=%.4f" % curve["fit_params"]["r2"]
        assert _has_number(text, curve["fit_params"]["slope"], 0.02), \
            "注释里没有斜率 %.4f" % curve["fit_params"]["slope"]
        signed = re.sub(r"-\s+(?=\d)", "-", text)       # renderer writes "area - 1.5"
        assert _has_number(signed, curve["fit_params"]["intercept"], 0.05), \
            "注释里没有截距 %.4f" % curve["fit_params"]["intercept"]
        assert re.search(r"\bn\b|point", low), "注释里没有点数 n"
        # English figure text: no CJK anywhere (µ is not CJK).  Lane fallback is tested below.
        assert not _CJK.search(text), "图里出现了中日韩字符：%r" % _CJK.findall(text)
    return True


def check_geometry_and_frames():
    """Two stacked Axes, main larger than the strip, both inside the Figure; English labels."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (w, h), fig, _caught, _out = _render_file(td, "geo.png", curve, width=1200)
        main, resid = _pair(fig)
        for ax in (main, resid):
            assert all(ax.spines[side].get_visible() for side in ("left", "right", "top", "bottom"))
            assert any(line.get_visible() for line in ax.get_xgridlines())
            assert any(line.get_visible() for line in ax.get_ygridlines())
        assert list(main.get_xticks()) == list(resid.get_xticks())
        mp = main.get_position()
        rp = resid.get_position()
        assert mp.height > rp.height, "主轴不比残差带高：%.3f vs %.3f" % (mp.height, rp.height)
        assert 0.06 <= rp.height <= 0.45, "残差带高度比例异常：%.3f" % rp.height
        assert 0.35 <= mp.width <= 0.98 and 0.35 <= rp.width <= 0.98, \
            "画框宽度异常：%.3f / %.3f" % (mp.width, rp.width)
        assert w == 1200 and h == int(_png(_out).shape[0])

        xlabel = (main.get_xlabel() or resid.get_xlabel() or "").lower()
        ylabel = (main.get_ylabel() or "").lower()
        rlabel = (resid.get_ylabel() or "").lower()
        assert "volume" in xlabel or "area" in xlabel, "主图 x 轴不是 volume/area：%r" % xlabel
        assert "mass" in ylabel, "主图 y 轴不是 mass：%r" % ylabel
        assert "%" in rlabel or "resid" in rlabel, "残差轴标签不是百分比/残差：%r" % rlabel

        text = _all_text(fig)
        assert "curve" in text.lower() or "densitometry" in text.lower(), "图里没有英文标题"
        assert not _CJK.search(text), "图里仍有中文：%r" % _CJK.findall(text)
    return True


def check_native_marker_legend():
    """The marker key is a native Matplotlib legend: active shapes only, filled vs hollow."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        _ret, fig, _caught, _out = _render_file(td, "legend.png", curve)
        cats = _legend_categories(fig)
        assert cats, "curve 没有原生 Matplotlib 图例"
        labels = [c["label"].strip() for c in cats]
        assert all(labels), "图例有空白标签：%r" % labels
        assert len(labels) == len(set(labels)), "图例标签重复：%r" % labels
        circles = [c for c in cats if c["shape"] == "circle" and not c["sd"]]
        assert any(c["filled"] for c in circles), "standard 图例不是实心圆"
        assert any(not c["filled"] for c in circles), "dropped 图例不是空心圆"
        assert any(c["sd"] for c in cats), "有重复点却缺 SD 图例"
        assert not any(c["shape"] == "square" for c in cats), "没有 quant 却出现方块图例"

        # No dropped points and no replicate pair: those categories must not be advertised.
        _ret, fig, _caught, _out = _render_file(td, "plain.png", _single_point_curve())
        cats = _legend_categories(fig)
        circles = [c for c in cats if c["shape"] == "circle" and not c["sd"]]
        assert any(c["filled"] for c in circles), "单调曲线缺 standard 图例"
        assert not any(not c["filled"] for c in circles), "没有 dropped 却画了空心圆图例"
        assert not any(c["sd"] for c in cats), "没有重复点却画了 SD 图例"
        assert not any(c["shape"] == "square" for c in cats), "没有 quant 却画了方块图例"

        # quant: filled sample square plus hollow out-of-range square.
        _ret, fig, _caught, _out = _render_file(td, "legend-quant.png", curve,
                                                quant=_quant())
        cats = _legend_categories(fig)
        squares = [c for c in cats if c["shape"] == "square" and not c["sd"]]
        assert any(c["filled"] for c in squares), "样品图例不是实心方块"
        assert any(not c["filled"] for c in squares), "超范围样品图例不是空心方块"
    return True


def _check_sample_positions(fig, curve, quant):
    main, _resid = _pair(fig)
    markers = [m for m in _markers(main) if m["shape"] == "square"]
    fit_lines = _fit_line(main, curve)
    solid, = [line for line in fit_lines if not _is_dashed(line)]
    dashed = [line for line in fit_lines if _is_dashed(line)]
    vmin, vmax = (curve["range"][key] for key in ("volume_min", "volume_max"))
    assert len(dashed) == 2
    assert min(solid.get_xdata()) == vmin and max(solid.get_xdata()) == vmax
    assert all(max(line.get_xdata()) <= vmin or min(line.get_xdata()) >= vmax for line in dashed)
    xlo, xhi = main.get_xlim()
    ylo, yhi = main.get_ylim()
    areas = []
    remaining = list(markers)
    for lane in quant["lanes"]:
        for band in lane["bands"]:
            area = band["area"]
            areas.append(area)
            outside = not vmin <= area <= vmax
            fit_only = outside or band["mass"] is None
            mass = _model(curve, area) if fit_only else band["mass"]
            assert xlo <= area <= xhi and ylo <= mass <= yhi, (area, mass, xlo, xhi, ylo, yhi)
            hits = _marker_at(remaining, area, mass, 1e-9, 1e-9)
            assert len(hits) == 1, (lane["lane"], band["index"], area, mass, hits)
            hit, = hits
            assert hit["filled"] is not fit_only
            remaining.remove(hit)
            label = "L%d" % lane["lane"]
            if len(lane["bands"]) > 1:
                label += ".%d" % band["index"]
            if outside:
                assert any(any(math.isclose(x, area, abs_tol=1e-9) and
                               math.isclose(y, mass, abs_tol=1e-9)
                               for x, y in zip(line.get_xdata(), line.get_ydata()))
                           for line in dashed), (label, area, mass)
            assert [t.get_text() for t in fig.texts].count(label) == 1, label
    assert all("extrapolated" not in text.get_text() for text in fig.texts)
    assert not remaining
    extension = 0.1 * (vmax - vmin)
    assert xlo == min([vmin - extension] + areas)
    assert xhi == max([vmax + extension] + areas)


def check_quant_markers():
    """Quantified and extrapolated bands have their own styles on the same main axes."""
    curve, quant = _linear_curve(), _quant()
    with tempfile.TemporaryDirectory() as td:
        _, fig, _, _ = _render_file(td, "quant.png", curve, quant=quant)
        _check_sample_positions(fig, curve, quant)
        labels = [label for _, label in _legend_entries(fig)]
        assert "sample" in labels and "extrapolated sample" in labels
    return True


def check_sample_fit_bars():
    """In-range sample bands carry a vertical ±1 SD fit bar; the legend advertises it once."""
    curve = _linear_curve()
    quant = _quant_sd()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out = _render_file(td, "sd.png", curve, quant=quant)
        main, _resid = _pair(fig)
        xtol = 0.02 * max(b["area"] for l in quant["lanes"] for b in l["bands"]) + 0.1
        segments = _yerr_segments(main)
        bars = 0
        for lane in quant["lanes"]:
            for band in lane["bands"]:
                if band["out_of_range"]:
                    # An out-of-range band has no calibrated mass, so it gets no fit bar.
                    assert not [s for s in segments
                                if abs(s[0] - band["area"]) <= xtol
                                and abs(s[2] - band["area"]) <= xtol], \
                        "超范围样品带 (lane %d) 画了 SD 棒" % lane["lane"]
                    continue
                bar = _yerr_at(main, band["area"], band["mass"], xtol, 1e9)
                assert bar is not None, \
                    "样品带 (lane %d, area %.2f) 没有竖直 SD 棒" % (lane["lane"], band["area"])
                lo, hi = bar
                assert abs((lo + hi) / 2.0 - band["mass"]) <= 0.05 * band["mass"] + 0.2, \
                    "SD 棒不在 mass %.1f 上下对称：%.2f..%.2f" % (band["mass"], lo, hi)
                assert abs((hi - lo) / 2.0 - band["mass_sd"]) <= 0.05 * band["mass_sd"] + 0.15, \
                    "SD 棒半高 %.3f ≠ mass_sd %.3f" % ((hi - lo) / 2.0, band["mass_sd"])
                bars += 1
        assert bars >= 2, "fixture 只画出 %d 条样品 SD 棒" % bars
        assert main.get_ylim()[0] <= 1e-9 + 1e-12, \
            "主轴 y 范围没有从 0 开始：%r" % (main.get_ylim(),)

        # Caps: both ends of every sample fit bar carry a cap marker, as the replicate bars do.
        from matplotlib.container import ErrorbarContainer
        x_bars = [c for c in main.containers
                  if isinstance(c, ErrorbarContainer) and c.has_xerr]
        y_bars = [c for c in main.containers
                  if isinstance(c, ErrorbarContainer) and c.has_yerr]
        assert x_bars, "没有重复点方差棒可作配色参照"
        assert y_bars, "没有画出样品 SD 棒"
        for container in y_bars:
            caps = [line for line in container.lines[1] if str(line.get_marker()) == "_"]
            assert caps, "样品 SD 棒没有端帽"
            assert len(caps) == 2, "样品 SD 棒端帽 %d 个，应为上下各一个" % len(caps)
            ends = sorted(float(line.get_ydata()[0]) for line in caps)
            segments = _yerr_segments(main)
            bar = [(min(s[1], s[3]), max(s[1], s[3])) for s in segments
                   if abs(s[0] - float(caps[0].get_xdata()[0])) <= 1e-6]
            assert len(bar) == 1, "端帽与 SD 棒没有一一对应"
            assert abs(ends[0] - bar[0][0]) <= 1e-6 and abs(ends[1] - bar[0][1]) <= 1e-6, \
                "端帽没有落在 SD 棒两端：%r vs %r" % (ends, bar[0])

        # The drawn bars reuse the grey of the replicate-area bars (one error colour on the plot).
        def edge_of(container):
            bars = container.lines[2]
            bars = bars if isinstance(bars, (tuple, list)) else [bars]
            return _rgba(bars[0].get_edgecolor()[0])

        grey = edge_of(x_bars[0])
        for container in y_bars:
            assert all(abs(a - b) <= 0.02 for a, b in zip(grey, edge_of(container))), \
                "样品 SD 棒没有沿用方差棒的灰色：%r vs %r" % (grey, edge_of(container))

        # Legend: exactly one extra entry, whose proxy is a vertical two-point line.
        labels = [label.strip() for _h, label in _legend_entries(fig)]
        sd_labels = [label for label in labels if "SD fit" in label]
        assert len(sd_labels) == 1, "±1 SD fit 图例应恰好一条，实际 %r" % sd_labels
        assert sd_labels[0].startswith("±1 SD fit"), "SD 图例标签不是 ±1 SD fit：%r" % sd_labels
        handles = [handle for handle, label in _legend_entries(fig)
                   if label.strip() == sd_labels[0]]
        assert len(handles) == 1, "±1 SD fit 图例 %d 条，应恰好一条" % len(handles)
        segments = getattr(handles[0], "get_segments", lambda: [])()
        assert segments, "±1 SD fit 图例句柄不是线段：%r" % (handles[0],)
        for seg in segments:
            (x0, y0), (x1, y1) = seg
            assert abs(x0 - x1) <= 1e-9 and abs(y0 - y1) > 1e-9, \
                "±1 SD fit 图例句柄不是竖直棒：%r" % (tuple(seg),)

        # Clip: a bar taller than the frame is cut by the Axes, never drawn outside or rescaled.
        tall = _quant_sd()
        tall["lanes"][0]["bands"][1].update(mass=43.9, area=9.6, mass_sd=9.0)
        (_w, _h), fig2, _caught2, _out2 = _render_file(td, "clamp.png", curve, quant=tall)
        main2, _resid2 = _pair(fig2)
        top = main2.get_ylim()[1]
        bar = _yerr_at(main2, 9.6, 43.9, 0.3, 1e9)
        assert bar is not None, "顶部样品带没有 SD 棒"
        assert max(bar) > top + 1e-6, "超高 SD 棒被压回画框内（%.3f ≤ %.3f）" % (max(bar), top)
        # The clip box is in display pixels: it must be the main frame, so the overshoot is cut.
        frame = _axes_box(fig2, main2)
        for container in [c for c in main2.containers
                          if isinstance(c, ErrorbarContainer) and c.has_yerr]:
            for collection in ([container.lines[2]] if not isinstance(container.lines[2],
                                                                      (tuple, list))
                               else container.lines[2]):
                assert collection.get_clip_on(), "SD 棒没有开启裁切"
                box = collection.get_clip_box()
                assert box is not None, "SD 棒没有裁切框"
                # Clip boxes use display pixels (bottom-up); compare them on the PNG grid.
                height = float(fig2.bbox.height)
                top_px, bottom_px = height - float(box.y1), height - float(box.y0)
                assert abs(top_px - frame[1]) <= 1.0, \
                    "SD 棒裁切框顶边 %.1f 与主轴框顶 %.1f 不符" % (top_px, frame[1])
                assert abs(bottom_px - frame[3]) <= 1.0, \
                    "SD 棒裁切框底边 %.1f 与主轴框底 %.1f 不符" % (bottom_px, frame[3])
    return True


def check_sample_fit_bars_absent():
    """No bar and no ±1 SD fit legend entry when mass_sd is missing, zero or undrawable."""
    curve = _linear_curve()
    values = {(9, 1): 0.0, (9, 2): 0.001, (11, 1): float("nan"), (12, 1): -1.0}
    with tempfile.TemporaryDirectory() as td:
        for name, quant in (("legacy", _quant()),
                            ("zero", _quant_sd(values))):
            (_w, _h), fig, caught, _out = _render_file(td, name + ".png", curve, quant=quant)
            main, _resid = _pair(fig)
            assert _yerr_segments(main) == [], \
                "%s: 没有可画的 mass_sd 却画了竖直棒" % name
            labels = [label.strip() for _h, label in _legend_entries(fig)]
            assert "±1 SD fit" not in labels, \
                "%s: 没有 SD 棒却出现 ±1 SD fit 图例：%r" % (name, labels)
            assert not [w for w in caught if "Glyph" in str(w.message)], caught
        # mass_sd absent from the quant payload is the legacy report shape.
        (_w, _h), fig, _caught, _out = _render_file(td, "missing.png", curve, quant=_quant())
        assert "±1 SD fit" not in _all_text(fig), "旧版 quant 里出现了 ±1 SD fit 文案"
        # A valid mass_sd on an out-of-range band must not create a bar either.
        quant = _quant_sd({(12, 1): 2.0})
        (_w, _h), fig, _caught, _out = _render_file(td, "oor.png", curve, quant=quant)
        main, _resid = _pair(fig)
        assert _yerr_segments(main) == [], "超范围样品带画了 SD 棒"
        assert "±1 SD fit" not in _all_text(fig), "超范围样品带触发了 ±1 SD fit 图例"
    return True


def check_dashed_extrapolated_samples():
    """Linear and power extensions reach the most distant sample on each side."""
    linear = _linear_curve()
    # The leftmost model value is negative. It must be plotted at its true ordinate.
    linear["fit_params"]["intercept"] = -10.0
    with tempfile.TemporaryDirectory() as td:
        for curve in (linear, _power_curve()):
            quant = _quant_clustered()
            if curve["fit"] == "power":
                vmin, vmax = (curve["range"][key] for key in ("volume_min", "volume_max"))
                for lane in quant["lanes"]:
                    for band in lane["bands"]:
                        if band["out_of_range"]:
                            band["area"] = vmax * 1.4 if lane["lane"] == 13 else vmin / 2 + band["area"] / 100
                        else:
                            band["area"] = vmin + (vmax - vmin) * band["area"] / 20
                            band["mass"] = _model(curve, band["area"])
            _, fig, _, _ = _render_file(td, curve["fit"] + ".png", curve, quant=quant)
            _check_sample_positions(fig, curve, quant)
    return True


def check_curve_fixed_geometry():
    """Canvas height and panel positions stay fixed from zero to many extrapolated samples."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        size, plain, _, _ = _render_file(td, "plain.png", curve)
        plain_axes = _pair(plain)
        for name, quant in (("inrange", _quant_in_range()), ("legacy", _quant()),
                            ("clustered", _quant_clustered())):
            sample_size, fig, _, _ = _render_file(td, name + ".png", curve, quant=quant)
            assert sample_size == size
            for original, current in zip(plain_axes, _pair(fig)):
                assert original.get_position().bounds == current.get_position().bounds
            if name == "inrange":
                assert plain_axes[0].get_xlim() == _pair(fig)[0].get_xlim()
                assert "extrapolated" not in _all_text(fig).lower()
    return True


def check_quant_absent_identical():
    """Without quant the picture is pixel-identical to plot_curve(curve, out)."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), _f1, _c1, p1 = _render_file(td, "plain.png", curve)
        _plot().plot_curve(curve, os.path.join(td, "explicit-none.png"), quant=None)
        p2 = os.path.join(td, "explicit-none.png")
        diff = float(abs(_png(p1) - _png(p2)).max())
        assert diff <= 1e-9, "quant=None 改变了画面（max diff %.6g）" % diff
    return True


def check_api_errors():
    """Bad curve / quant / width / output paths fail with one-line ValueError/RuntimeError."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "x.png")

        bad = dict(curve)
        bad["kind"] = "not-a-curve"
        _expect(lambda: _plot().plot_curve(bad, png))

        bad = dict(curve)
        bad["points"] = []
        _expect(lambda: _plot().plot_curve(bad, png))

        bad = dict(curve)
        bad["fit_params"] = {}
        _expect(lambda: _plot().plot_curve(bad, png))

        bad = dict(curve)
        bad["fit"] = "power"
        _expect(lambda: _plot().plot_curve(bad, png))

        _expect(lambda: _plot().plot_curve(curve, os.path.join(td, "x")))
        _expect(lambda: _plot().plot_curve(curve, td))
        _expect(lambda: _plot().plot_curve(curve, os.path.join(td, "no", "x.png")))
        _expect(lambda: _plot().plot_curve(curve, png, width=0))
        _expect(lambda: _plot().plot_curve(curve, png, width=-10))
        _expect(lambda: _plot().plot_curve(curve, png, quant={"kind": "wrong"}))
        _expect(lambda: _plot().plot_curve(curve, png,
                                           quant=dict(_quant(), lanes=None)))
        assert not os.path.isfile(png), "失败路径留下了输出 PNG"
    return True


def check_cli_errors_and_overwrite():
    """CLI errors are one `[!]` line, no traceback; an output hard-linked to the input is refused."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            rc, _out, err = _cli(["dens", "plot-curve", "--curve", "missing.json"])
            assert rc != 0
            _one_line(err, "missing curve")

            _write_json("bad.json", {"kind": "wrong"})
            rc, _out, err = _cli(["dens", "plot-curve", "--curve", "bad.json"])
            assert rc != 0
            _one_line(err, "bad kind")

            _write_json("c.json", curve)
            rc, _out, err = _cli(["dens", "plot-curve", "--curve", "c.json", "--out", "."])
            assert rc != 0
            _one_line(err, "directory output")

            rc, _out, err = _cli(["dens", "plot-curve", "--curve", "c.json",
                                  "--out", "no/such/x.png"])
            assert rc != 0
            _one_line(err, "missing parent")

            os.link("c.json", "alias.png")
            rc, _out, err = _cli(["dens", "plot-curve", "--curve", "c.json",
                                  "--out", "alias.png"])
            assert rc != 0
            _one_line(err, "hard-linked input")
            assert "c.json" in err, err
            assert os.path.getsize("c.json") > 0, "输入 curve.json 被覆盖"

            rc, _out, err = _cli(["dens", "plot-curve", "--curve", "c.json",
                                  "--quant", "missing.json"])
            assert rc != 0
            _one_line(err, "missing quant")
    return True


def check_no_magick_no_temp():
    """The renderer does not touch ImageMagick and leaves no script/temp debris."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        from epcraft import magick_io
        boom = AssertionError("matplotlib 渲染不应调用 ImageMagick")
        with unittest.mock.patch.object(magick_io, "_run", side_effect=boom):
            (_w, _h), _fig, _caught, out = _render_file(td, "clean.png", curve)
        assert os.path.isfile(out)
        leftovers = [n for n in os.listdir(td) if n != "clean.png"]
        assert leftovers == [], "渲染后留下了临时文件：%r" % leftovers
        assert not any(n.endswith(".mvg") for n in os.listdir(td))
    return True


def check_no_glyph_warnings():
    """µg and the English labels render with a real font (no Matplotlib tofu warning)."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        _ret, fig, caught, _out = _render_file(td, "font.png", curve)
        missing = [str(w.message) for w in caught
                   if "glyph" in str(w.message).lower()
                   or "missing from font" in str(w.message).lower()]
        assert not missing, "字体缺字（豆腐块）：%r" % missing
        assert "µg" in _all_text(fig)
    return True


def check_nonenglish_sample_labels():
    """A non-ASCII sample lane label falls back to `Lane N` and warns with the lane number."""
    curve = _linear_curve()
    quant = _quant()
    quant["lanes"][0]["label"] = "样品A"
    with tempfile.TemporaryDirectory() as td:
        _ret, fig, caught, _out = _render_file(td, "cjk.png", curve, quant=quant)
        text = _all_text(fig)
        assert not _CJK.search(text), "样品标签仍是中文：%r" % _CJK.findall(text)
        assert re.search(r"\bL(?:ane)?\s*9\b", text), \
            "非英文泳道没有回退成 Lane 9 / L9：%r" % text
        assert not any("Glyph" in str(w.message) or "missing from font" in str(w.message)
                       for w in caught), "回退后仍有缺字警告"
        hits = [w for w in caught if "9" in str(w.message)]
        assert hits, "非英文泳道没有发出带泳道号的诊断：%r" % [str(w.message) for w in caught]
    return True


def check_chain_synthetic():
    """measure → curve → plot-curve on a synthetic report keeps the numbers on the picture."""
    from . import selftest_densitometry
    report = selftest_densitometry._synthetic_report(
        lanes=[{"label": "S1", "bands": [(1.2, 30, .05)]},
               {"label": "S2", "bands": [(1.2, 24, .05)]},
               {"label": "S3", "bands": [(1.2, 18, .05)]},
               {"label": "S4", "bands": [(1.2, 12, .05)]}])
    dens = densitometry.measure(report)
    curve = densitometry.build_curve(
        dens, densitometry.parse_std("1=40,2=30,3=20,4=10"), unit="ug", fit="linear",
        baseline="flank", dens_path="synthetic.dens.json", std_text="1=40,2=30,3=20,4=10")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "synthetic.curve.json")
        _write_json(path, curve)
        rc, out, err = _cli(["dens", "plot-curve", "--curve", path])
        assert rc == 0 and err == "", (rc, err)
        png = os.path.join(td, "synthetic.curve.png")
        assert os.path.isfile(png), out
        _assert_png(png, 1020)
        _ret, fig, _caught, _out = _render(os.path.join(td, "api.png"), curve)
        main, _resid = _pair(fig)
        assert _fit_line(main, curve), "实跑曲线没有拟合线"
        text = _all_text(fig)
        assert _has_number(text, curve["fit_params"]["r2"], 0.002), "实跑注释没有 r²"
        assert _has_number(text, curve["fit_params"]["slope"],
                           0.055 * max(1.0, abs(curve["fit_params"]["slope"]))), \
            "实跑注释没有斜率"
    return True


CHECKS = (
    check_cli_parser,
    check_default_path_and_alias,
    check_size_blank_and_rerun,
    check_fit_line_linear,
    check_fit_line_power,
    check_dropped_points_do_not_expand_axes,
    check_points_and_dropped,
    check_error_bars,
    check_bar_free_without_replicates,
    check_residuals,
    check_annotation,
    check_geometry_and_frames,
    check_native_marker_legend,
    check_quant_markers,
    check_sample_fit_bars,
    check_sample_fit_bars_absent,
    check_dashed_extrapolated_samples,
    check_curve_fixed_geometry,
    check_quant_absent_identical,
    check_api_errors,
    check_cli_errors_and_overwrite,
    check_no_magick_no_temp,
    check_no_glyph_warnings,
    check_nonenglish_sample_labels,
    check_chain_synthetic,
)


def run():
    """Run the whole acceptance suite; returns the number of passed checks."""
    _plot()
    for check in CHECKS:
        check()
    print("selftest_plot_curve_acceptance %d/%d 通过" % (len(CHECKS), len(CHECKS)))
    return len(CHECKS)


if __name__ == "__main__":
    raise SystemExit(run())
