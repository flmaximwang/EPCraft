#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_dens_plot — independent acceptance suite for the matplotlib `plot-bands`.

The renderer is a black box: every check calls the public
`dens_plot.plot_bands(dens, out, baseline="both", lanes=None, cols=2, width=1440, *, groups=None)`
(or the public `dens plot-bands` CLI), captures the Matplotlib Figure that the renderer saves
and reads the PNG back with Matplotlib's own image reader.  There are no MVG scripts, no
ImageMagick calls and no pixel arithmetic borrowed from the renderer.

What is asserted is the scientific content of the picture:

* one Axes per (lane or explicit group, baseline); a lane with N bands gets N local traces at
  their true migration positions — never the whole 0..height profile;
* x is true migration (cm), y is the lane's own baseline minus raw gray, signed; the stored
  band window is shaded to zero on both sides of the signed trace; there is a shared zero line;
* the axes zoom to the span of the local traces rather than the full lane;
* the panel text identifies lane/group, band number, the original ASCII label, dilution names,
  stored area, MW, index and flags; figures are English with fallback `Lane N` for non-ASCII
  lane labels (plus a LaneLabelWarning carrying the lane number);
* explicit `groups` overlay the selected bands in comparison panels with a lane+band legend;
  overlays use lines/dots/window ticks and no area fill, while a lane view keeps its signed fill;
  each member's Flank/Global traces share one x sample grid, while its window markers and
  areas stay member-specific; numeric concentrations map through Matplotlib `coolwarm`,
  endpoints verified by hue;
* public errors, CLI defaults, overwrite protection and the atomic PNG write are all checked.

Matplotlib is an optional `[plot]` extra; the gateway / base selftest decides whether the
suites run at all, so this module does not silently skip.

Run:  PYTHONPATH=src python3 -m epcraft.selftest_dens_plot
`run()` returns the number of passed checks and prints `selftest_dens_plot N/N 通过`.
"""
import contextlib
import inspect
import io
import json
import os
import re
import tempfile
import unittest.mock
import warnings

os.environ.setdefault("MPLBACKEND", "Agg")

from . import densitometry

KIND = densitometry.KIND
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


# ---------------------------------------------------------------- public module guard

def _plot():
    """The renderer module, with the names this suite exercises."""
    from . import dens_plot
    for name in ("plot_bands", "LaneLabelWarning"):
        assert hasattr(dens_plot, name), "dens_plot 缺少 %s" % name
    assert callable(dens_plot.plot_bands), "dens_plot.plot_bands 不是可调用对象"
    assert issubclass(dens_plot.LaneLabelWarning, Warning), \
        "dens_plot.LaneLabelWarning 不是 Warning 子类"
    return dens_plot


def _mpl():
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import numpy as np
    return matplotlib, Figure, FigureCanvasAgg, np


# ---------------------------------------------------------------- figure capture

@contextlib.contextmanager
def _capture_figures():
    """Capture every Figure and every save path without opening a GUI."""
    _matplotlib, Figure, FigureCanvasAgg, _np = _mpl()
    real_savefig = Figure.savefig
    real_clear = Figure.clear
    figures, paths = [], []

    def savefig(self, fname, *args, **kwargs):
        figures.append(self)
        self._epcraft_saved = True
        name = getattr(fname, "name", fname)
        try:
            paths.append(os.fspath(name))
        except TypeError:
            paths.append(None)
        return real_savefig(self, fname, *args, **kwargs)

    def clear(self, *args, **kwargs):
        # Keep the saved artists readable; the renderer frees the Figure after savefig.
        if getattr(self, "_epcraft_saved", False):
            return None
        return real_clear(self, *args, **kwargs)

    with unittest.mock.patch.object(Figure, "savefig", savefig), \
            unittest.mock.patch.object(Figure, "clear", clear):
        yield figures, paths


def _render(path, dens, baseline="both", lanes=None, cols=2, width=1440, groups=None,
            api=True):
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    figures, paths, caught = [], [], []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _capture_figures() as (figures, paths):
            if api:
                ret = _plot().plot_bands(dens, path, baseline=baseline, lanes=lanes,
                                         cols=cols, width=width, groups=groups)
            else:
                ret = _cli(["dens", "plot-bands", "--dens", path, "--out", path,
                            "--baseline", baseline, "--cols", str(cols),
                            "--width", str(width)])
    assert figures, "plot_bands 没有通过 Figure.savefig 保存任何 Figure（ticket 的 Agg 契约）"
    assert paths, "plot_bands 没有把 Figure 保存到文件"
    assert ret is not None, "plot_bands / CLI 没有返回结果"
    return ret, figures[-1], caught, os.path.abspath(path), paths


def _render_file(tmpdir, name, dens, **kwargs):
    path = os.path.join(tmpdir, name)
    ret, fig, caught, out, paths = _render(path, dens, **kwargs)
    if kwargs.get("api", True):
        assert isinstance(ret, tuple) and len(ret) == 2, \
            "plot_bands 返回 %r，应为 (w, h)" % (ret,)
    assert out == path, (out, path)
    return ret, fig, caught, out, paths


# ---------------------------------------------------------------- PNG helpers

def _png(path):
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


def _assert_png(path, width=None):
    img = _png(path)
    h, w = img.shape[:2]
    if width is not None:
        assert abs(w - width) <= 2, "PNG 宽 %d，请求 %d" % (w, width)
    fraction = float((img < 0.98).any(axis=2).mean()) if img.size else 0.0
    assert fraction >= 0.005, "PNG 几乎是空白（非白像素 %.4f）" % fraction
    assert h > 120 and w > 200, "PNG 太小：%dx%d" % (w, h)
    return w, h


# ---------------------------------------------------------------- text / artist helpers

def _text_of(obj):
    if obj is None:
        return None
    if hasattr(obj, "get_text"):
        return obj.get_text()
    return str(obj)


def _texts(fig):
    out = []

    def add(obj):
        text = _text_of(obj)
        if text is not None and str(text).strip():
            out.append(str(text))

    for text in getattr(fig, "texts", []) or []:
        add(text)
    add(getattr(fig, "_suptitle", None))
    for ax in fig.axes:
        add(getattr(ax, "title", None))
        add(getattr(ax, "_left_title", None))
        add(getattr(ax, "_right_title", None))
        add(ax.get_xlabel())
        add(ax.get_ylabel())
        for text in ax.texts:
            add(text)
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


def _ax_text(ax):
    out = [_text_of(getattr(ax, "title", None)),
           _text_of(getattr(ax, "_left_title", None)),
           _text_of(getattr(ax, "_right_title", None)),
           ax.get_xlabel(), ax.get_ylabel()]
    out.extend(_text_of(t) for t in ax.texts)
    legend = ax.get_legend()
    if legend is not None:
        out.extend(_text_of(t) for t in legend.get_texts())
    return "\n".join(str(t) for t in out if t)


def _legend_texts(ax):
    legend = ax.get_legend()
    if legend is None:
        return []
    return [str(t.get_text()) for t in legend.get_texts() if str(t.get_text()).strip()]


def _numbers(text):
    return [float(v) for v in _NUM.findall(text)]


def _has_number(text, value, tol):
    return any(abs(v - float(value)) <= tol for v in _numbers(text))


def _mentions_band(text, index):
    low = text.lower()
    return bool(re.search(r"(?:\bband\b|#|\bb)\s*#?\s*%d\b" % int(index), low))


def _shows_band(text, band, baseline):
    """The selected band is identifiable from its index, position, window or area."""
    if _mentions_band(text, band["index"]):
        return True
    for key in ("y_cm", "area_" + baseline):
        if _has_number(text, band[key], 0.12):
            return True
    w0, w1 = _window(band, baseline)
    return _has_number(text, w0, 0.05) or _has_number(text, w1, 0.05)


def _axes(fig):
    return [ax for ax in fig.axes if ax.get_visible()]


def _assert_axes_count(fig, expected):
    axes = _axes(fig)
    assert len(axes) == expected, \
        "应有 %d 个 Axes（每 lane/group × baseline 一个），实际 %d 个" % (expected, len(axes))
    return axes


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


def _constant_lines(ax):
    out = []
    for line in ax.lines:
        xs, ys = _line_xy(line)
        if len(ys) >= 2 and float(abs(ys - ys[0]).max()) <= 1e-9:
            out.append((float(ys[0]), line))
    return out


def _has_constant(ax, value, tol):
    return any(abs(y - value) <= tol for y, _line in _constant_lines(ax))


def _has_zero_line(ax):
    if _has_constant(ax, 0.0, 0.5):
        return True
    for spine in ax.spines.values():
        try:
            position = spine.get_position()
        except Exception:
            continue
        if position == ("data", 0):
            return True
    return False


def _same_trace(a, b):
    """Two Line2D artists carry the same data (group overlays draw a dark halo underneath)."""
    xa, ya = _line_xy(a)
    xb, yb = _line_xy(b)
    if len(xa) != len(xb) or len(ya) != len(yb):
        return False
    if len(xa) == 0:
        return False
    return (float(abs(xa - xb).max()) <= 1e-9
            and float(abs(ya - yb).max()) <= 1e-9)


def _dedupe_traces(lines):
    """Collapse halo/underlay copies of one data trace, keeping the thinner visible line."""
    out = []
    for line in lines:
        for i, kept in enumerate(out):
            if _same_trace(line, kept):
                try:
                    thinner = float(line.get_linewidth()) < float(kept.get_linewidth())
                except (TypeError, ValueError):
                    thinner = False
                if thinner:
                    out[i] = line
                break
        else:
            out.append(line)
    return out


def _traces(ax):
    """Distinct non-constant Line2D artists with at least four samples (local band traces)."""
    out = []
    for line in ax.lines:
        xs, ys = _line_xy(line)
        if len(xs) < 4 or not _np_finite(xs) or not _np_finite(ys):
            continue
        if float(xs.max() - xs.min()) <= 1e-9 or float(ys.max() - ys.min()) <= 1e-9:
            continue
        out.append(line)
    return _dedupe_traces(out)


def _np_finite(values):
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    return bool(_np.isfinite(values).all())


def _local_traces(ax, profile_span):
    out = []
    for line in _traces(ax):
        xs, _ys = _line_xy(line)
        if float(xs.max() - xs.min()) < 0.80 * profile_span:
            out.append(line)
    return out


def _band_trace(ax, band, profile_span):
    y = float(band["y_cm"])
    candidates = []
    for line in _local_traces(ax, profile_span):
        xs, _ys = _line_xy(line)
        if float(xs.min()) - 0.08 <= y <= float(xs.max()) + 0.08:
            candidates.append((abs((float(xs.min()) + float(xs.max())) / 2.0 - y), line))
    assert candidates, "band y=%.2f cm 没有局部 trace" % y
    return min(candidates, key=lambda item: item[0])[1]


def _profile_span(lane):
    y = _arr(lane["profile"]["y_cm"])
    return float(y.max() - y.min())


def _window(band, baseline):
    key = "window_%s" % baseline
    win = band.get(key)
    assert isinstance(win, (list, tuple)) and len(win) == 2, "band 缺 %s：%r" % (key, win)
    return float(win[0]), float(win[1])


def _ramp_at(band, x):
    (yl, gl), (yr, gr) = band["ramp"]["left"], band["ramp"]["right"]
    yl, gl, yr, gr = float(yl), float(gl), float(yr), float(gr)
    if yr < yl:
        yl, gl, yr, gr = yr, gr, yl, gl
    if x <= yl:
        return gl
    if x >= yr:
        return gr
    return gl + (gr - gl) * (x - yl) / (yr - yl)


def _expected_trace(lane, band, xs, baseline):
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    prof = lane["profile"]
    xp = _arr(prof["y_cm"])
    gray = _np.interp(xs, xp, _arr(prof["gray"]))
    if baseline == "global":
        base = _np.interp(xs, xp, _arr(prof["baseline_global"]))
    else:
        base = _arr([_ramp_at(band, float(x)) for x in xs])
    return base - gray


def _axis_baseline(ax):
    low = _ax_text(ax).lower()
    has_f = bool(re.search(r"\bflank\b", low))
    has_g = bool(re.search(r"\bglobal\b", low))
    assert has_f != has_g, "Axes 没有把 flank / global 分开（flank=%r global=%r）：%r" % (
        has_f, has_g, _ax_text(ax))
    return "flank" if has_f else "global"


def _figure_texts(fig):
    """Every figure-level text line, in artist order (tile headers and comparison legends)."""
    return [str(t.get_text()).strip() for t in getattr(fig, "texts", []) or []
            if str(t.get_text()).strip()]


def _axes_with_traces(axes, profile_span, count):
    return [ax for ax in axes if len(_local_traces(ax, profile_span)) == count]


def _window_fill(ax, band, baseline, sign=1):
    """A PolyCollection shading one signed side of the stored band window."""
    w0, w1 = _window(band, baseline)
    width = max(w1 - w0, 1e-9)
    for coll in ax.collections:
        for path in coll.get_paths():
            vertices = _arr(path.vertices)
            if vertices.ndim != 2 or len(vertices) < 3:
                continue
            vertices = vertices[_np_finite_2d(vertices)]
            if len(vertices) < 3:
                continue
            if sign >= 0:
                chosen = vertices[vertices[:, 1] > 0.01]
            else:
                chosen = vertices[vertices[:, 1] < -0.01]
            if len(chosen) < 2:
                continue
            x0, x1 = float(chosen[:, 0].min()), float(chosen[:, 0].max())
            overlap = min(x1, w1) - max(x0, w0)
            if (overlap >= 0.05 * width and x0 >= w0 - 0.30
                    and x1 <= w1 + 0.30):
                return coll, chosen
    return None, None


def _area_fills(ax):
    """Polygon collections that shade area to the zero line (fill_between, not tick lines)."""
    out = []
    for coll in ax.collections:
        try:
            paths = coll.get_paths()
        except Exception:
            continue
        for path in paths:
            vertices = _arr(path.vertices)
            if vertices.ndim != 2 or len(vertices) < 4:
                continue
            vertices = vertices[_np_finite_2d(vertices)]
            if len(vertices) < 4:
                continue
            width = float(vertices[:, 0].max() - vertices[:, 0].min())
            height = float(abs(vertices[:, 1]).max())
            if width > 1e-6 and height > 0.05:
                out.append(coll)
                break
    return out


def _hue(rgb):
    import colorsys
    return colorsys.rgb_to_hsv(float(rgb[0]), float(rgb[1]), float(rgb[2]))[0]


def _hue_gap(a, b):
    gap = abs(float(a) - float(b))
    return min(gap, 1.0 - gap)


def _np_finite_2d(values):
    _matplotlib, _Figure, _Canvas, _np = _mpl()
    return _np.isfinite(values).all(axis=1)


# ---------------------------------------------------------------- CLI harness

def _cli(argv):
    from . import gel_photo
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


def _stderr_ok(err):
    """Success runs may warn once about a non-English lane label, but never print an error."""
    if not err.strip():
        return True
    lines = [line for line in err.strip().splitlines() if line.strip()]
    if any(line.startswith("[!]") or "Traceback" in line for line in lines):
        return False
    return all("LaneLabelWarning" in line and "is not English" in line for line in lines)


def _expect(call, kinds=(ValueError, RuntimeError)):
    try:
        call()
    except kinds as exc:
        text = str(exc)
        assert text and "\n" not in text, "错误信息不是一行：%r" % text
        return text
    except Exception as exc:
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


# ---------------------------------------------------------------- fixture

_LANE_SPECS = [
    {"label": "Std 1:2", "bands": [(1.2, 30, .05), (3.8, 20, .06)]},
    {"label": "Std 1:4", "bands": [(1.2, 15, .05), (3.8, 10, .06)]},
    {"label": "Std 1:8", "bands": [(1.2, 8, .05)]},
    {"label": "样品A", "bands": [(2.5, 12, .05)]},
    {"label": "Std 1:16", "bands": [(1.2, 4, .05)]},
]


def _dens():
    """Synthetic measured dens with known bands, areas, flags and one non-ASCII label."""
    from . import selftest_densitometry
    report = selftest_densitometry._synthetic_report(lanes=_LANE_SPECS)
    dens = densitometry.measure(report)
    for lane in dens["lanes"]:
        if lane["lane"] == 1:
            lane["bands"][0]["flat_top"] = True
        if lane["lane"] == 2:
            lane["bands"][0]["flat_top"] = True
            lane["bands"][1]["mw_kda"] = 66.4
            lane["bands"][1]["clipped"] = True
            lane["bands"][1]["notes"] = ["window clamped to neighbour"]
    return dens


def _lane(dens, no):
    for lane in dens["lanes"]:
        if int(lane["lane"]) == int(no):
            return lane
    raise AssertionError("fixture 没有 lane %d" % no)


def _band_by_index(lane, index):
    """Resolve a 1-based band index without assuming it is a list position."""
    for band in lane["bands"]:
        try:
            if int(band["index"]) == int(index):
                return band
        except (KeyError, TypeError, ValueError):
            continue
    raise AssertionError("lane %r 没有 band index %r" % (lane.get("lane"), index))


def _largest_band(lane, baseline):
    key = "area_%s" % baseline
    return max(lane["bands"], key=lambda b: float(b[key]))


def _window_tick_positions(ax):
    """x positions of vertical window-edge segments (two-point LineCollection paths)."""
    out = []
    for coll in ax.collections:
        try:
            paths = coll.get_paths()
        except Exception:
            continue
        for path in paths:
            vertices = _arr(path.vertices)
            if vertices.ndim != 2 or len(vertices) != 2:
                continue
            if abs(float(vertices[0, 0]) - float(vertices[1, 0])) > 1e-9:
                continue
            out.append(float(vertices[0, 0]))
    return out


# ---------------------------------------------------------------- checks

def check_api_signature():
    """Public signature keeps the old arguments and adds keyword-only `groups`."""
    from . import dens_plot
    sig = inspect.signature(dens_plot.plot_bands)
    names = list(sig.parameters)
    assert names[:6] == ["dens", "out", "baseline", "lanes", "cols", "width"], names
    assert "groups" in sig.parameters, "plot_bands 没有 groups 参数：%r" % names
    group = sig.parameters["groups"]
    assert group.default is None, group.default
    assert group.kind is inspect.Parameter.KEYWORD_ONLY, \
        "groups 必须是 keyword-only（收到 %r）" % group.kind
    # Defaults may shrink with the user-facing redesign; only require sane, public values.
    assert isinstance(sig.parameters["cols"].default, int) and sig.parameters["cols"].default >= 1
    assert isinstance(sig.parameters["width"].default, int) and sig.parameters["width"].default >= 600
    assert sig.parameters["baseline"].default == "both"
    return True


def check_cli_parser():
    """`plot-bands` keeps §3.4 defaults and accepts repeatable --group."""
    from . import gel_photo
    for group in ("dens", "densitometry"):
        ap = gel_photo.build_parser(canvas=True)
        args = ap.parse_args([group, "plot-bands", "--dens", "d.json"])
        assert args.cmd == group and args.action == "plot-bands", (group, args.action)
        assert args.func.__name__ == "cmd_plot_bands", (group, args.func.__name__)
        assert args.baseline in ("flank", "global", "both"), vars(args)
        assert isinstance(args.cols, int) and args.cols >= 1, vars(args)
        assert isinstance(args.width, int) and args.width >= 600, vars(args)
        assert args.lanes is None and args.out is None, vars(args)

        key = next((k for k in vars(args) if "group" in k.lower()), None)
        assert key is not None, "plot-bands 没有 --group 选项"
        args = ap.parse_args([group, "plot-bands", "--dens", "d.json",
                              "--group", "Standards=2,3,4"])
        assert len(getattr(args, key) or []) == 1, getattr(args, key)
        args = ap.parse_args([group, "plot-bands", "--dens", "d.json",
                              "--group", "Standards=2,3,4",
                              "--group", "Samples=4"])
        assert len(getattr(args, key) or []) == 2, getattr(args, key)

        try:
            ap.parse_args([group, "plot-bands"])
        except SystemExit as e:
            assert e.code == 2, e.code
        else:
            raise AssertionError("--dens 缺失时 parser 没有拒绝")
    return True


def check_default_path_and_alias():
    """CLI derives `<stem>.bands.png`, honours --out, and both aliases work."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write_json("fig.dens.json", dens)
            rc, out, err = _cli(["dens", "plot-bands", "--dens", "fig.dens.json"])
            assert rc == 0 and _stderr_ok(err), (rc, err)
            assert os.path.isfile("fig.bands.png"), out
            assert "fig.bands.png" in out, out
            w, h = _png_size("fig.bands.png")
            from . import gel_photo
            default_width = gel_photo.build_parser(canvas=True).parse_args(
                ["dens", "plot-bands", "--dens", "fig.dens.json"]).width
            assert abs(w - default_width) <= 2, (w, default_width)

            rc, out, err = _cli(["densitometry", "plot-bands", "--dens", "fig.dens.json",
                                 "--width", "800"])
            assert rc == 0 and _stderr_ok(err), (rc, err)
            w, h = _png_size("fig.bands.png")
            assert abs(w - 800) <= 2, w

            _write_json("mydens.json", dens)
            rc, out, err = _cli(["dens", "plot-bands", "--dens", "mydens.json"])
            assert rc == 0 and _stderr_ok(err), (rc, err)
            assert os.path.isfile("mydens.bands.png"), out

            rc, out, err = _cli(["dens", "plot-bands", "--dens", "mydens.json",
                                 "--out", "x.png"])
            assert rc == 0 and _stderr_ok(err), (rc, err)
            assert os.path.isfile("x.png")
    return True


def check_size_and_rerun():
    """Returned size == PNG size, width is honoured, and the same input gives the same pixels."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (w1, h1), fig1, _c1, p1, _paths = _render_file(td, "a.png", dens, width=1200,
                                                       lanes=[2])
        img1 = _png(p1)
        assert (img1.shape[1], img1.shape[0]) == (w1, h1), (img1.shape, (w1, h1))
        assert abs(w1 - 1200) <= 2, w1
        _assert_png(p1, 1200)
        assert (_png(p1) < 0.98).any(axis=2).mean() > 0.01

        (w2, h2), fig2, _c2, p2, _paths = _render_file(td, "b.png", dens, width=1200,
                                                       lanes=[2])
        assert (w2, h2) == (w1, h1), ((w2, h2), (w1, h1))
        diff = float(abs(_png(p1) - _png(p2)).max())
        assert diff <= 1e-9, "同输入两次渲染像素不同（max diff %.6g）" % diff
        assert fig1.canvas is not None, "保存的 Figure 没有 Agg canvas"
    return True


def check_lane_panels_and_local_traces():
    """Implicit lanes get one Axes per baseline; each band is one local trace, not a profile."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "lanes.png", dens, baseline="both", lanes=[2, 3])
        axes = _assert_axes_count(fig, 4)
        text = _all_text(fig)
        assert "Lane 2" in text and "Lane 3" in text, text
        assert "Std 1:4" in text and "Std 1:8" in text, text

        lane2 = _lane(dens, 2)
        span = _profile_span(lane2)
        two_trace_axes = _axes_with_traces(axes, span, 2)
        one_trace_axes = _axes_with_traces(axes, span, 1)
        assert len(two_trace_axes) == 2, \
            "lane 2 有 2 条带，flank/global 各应有 2 条局部 trace，实际 %d" % len(two_trace_axes)
        assert len(one_trace_axes) == 2, \
            "lane 3 只有 1 条带，flank/global 各应有 1 条局部 trace，实际 %d" % len(one_trace_axes)
        for ax in two_trace_axes:
            for band in lane2["bands"]:
                _band_trace(ax, band, span)
        for ax in axes:
            # Never the whole 0..height profile (that was the old MVG layout).
            for line in _traces(ax):
                xs, _ys = _line_xy(line)
                assert float(xs.max() - xs.min()) < 0.85 * span, \
                    "一条 trace 覆盖了整条泳道（span %.2f / profile %.2f）" % (
                        float(xs.max() - xs.min()), span)
    return True


def check_trace_geometry_and_baseline():
    """x is true migration cm; y is the lane's own baseline minus raw gray (signed)."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "geom.png", dens, baseline="both", lanes=[2])
        axes = _assert_axes_count(fig, 2)
        lane = _lane(dens, 2)
        span = _profile_span(lane)
        for ax in axes:
            baseline = _axis_baseline(ax)
            for band in lane["bands"]:
                line = _band_trace(ax, band, span)
                xs, ys = _line_xy(line)
                assert float(xs.min()) <= band["y_cm"] <= float(xs.max()), \
                    "band y=%.2f 不在 trace 的 x 范围 %.2f..%.2f 内" % (
                        band["y_cm"], float(xs.min()), float(xs.max()))
                center = (float(xs.min()) + float(xs.max())) / 2.0
                assert abs(center - float(band["y_cm"])) <= 0.6, \
                    "trace 被移位/居中：中心 %.2f，band %.2f" % (center, band["y_cm"])
                expected = _expected_trace(lane, band, xs, baseline)
                diff = float(abs(ys - expected).max())
                assert diff <= 0.15, \
                    "%s 基线 trace 的 y 不是 baseline-gray（max diff %.3f）" % (baseline, diff)
                assert float(ys.max()) > 1.0, "trace 被归一化/压扁了：max %.3f" % float(ys.max())
    return True


def check_window_shading():
    """The stored window is shaded to zero, including negative parts of signed ink."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "shade.png", dens, baseline="both", lanes=[2])
        lane = _lane(dens, 2)
        for ax in axes_for(fig):
            baseline = _axis_baseline(ax)
            for band in lane["bands"]:
                fill, chosen = _window_fill(ax, band, baseline, +1)
                assert fill is not None, \
                    "band y=%.2f 的 %s 窗口没有正向阴影" % (band["y_cm"], baseline)
                w0, w1 = _window(band, baseline)
                assert float(chosen[:, 0].min()) >= w0 - 0.25, chosen[:, 0].min()
                assert float(chosen[:, 0].max()) <= w1 + 0.25, chosen[:, 0].max()

        # A window whose signed trace dips well below zero must still be shaded to the zero
        # line: area_of integrates signed ink, so hiding the negative lobe would misreport it.
        signed = _dens()
        lane = _lane(signed, 2)
        band = lane["bands"][0]
        band["window_global"] = [1.0, 1.4]
        with tempfile.TemporaryDirectory() as td:
            (_w, _h), fig, _caught, _out, _paths = _render_file(
                td, "signed.png", signed, baseline="global", lanes=[2])
            ax = _assert_axes_count(fig, 1)[0]
            pos, pos_v = _window_fill(ax, band, "global", +1)
            neg, neg_v = _window_fill(ax, band, "global", -1)
            assert pos is not None, "签名窗口没有正向阴影"
            assert neg is not None, "签名窗口的负向 ink 没有画到零线"
            assert float(pos_v[:, 1].max()) > 5.0, float(pos_v[:, 1].max())
            assert float(neg_v[:, 1].min()) <= -1.0, float(neg_v[:, 1].min())
    return True


def axes_for(fig):
    return _axes(fig)


def check_zero_line_and_zoom():
    """Every panel shares a zero line and zooms to the local traces, not the full lane."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "zoom.png", dens, baseline="both", lanes=[2, 3])
        lane2 = _lane(dens, 2)
        span2 = _profile_span(lane2)
        for ax in _axes(fig):
            assert _has_zero_line(ax), "Axes 没有 y=0 的共享零线：%r" % _ax_text(ax)
            low, high = ax.get_xlim()
            width = high - low
            assert width < 0.90 * span2, \
                "Axes 没有放大到局部 trace（xlim %.2f..%.2f，整条泳道 %.2f）" % (
                    low, high, span2)
            trace_x = []
            for line in _local_traces(ax, span2):
                xs, _ys = _line_xy(line)
                trace_x.extend([float(xs.min()), float(xs.max())])
                assert low - 0.05 <= float(xs.min()) and float(xs.max()) <= high + 0.05, \
                    "trace 超出 xlim：%.2f..%.2f vs %.2f..%.2f" % (
                        float(xs.min()), float(xs.max()), low, high)
            if trace_x:
                span = max(trace_x) - min(trace_x)
                assert width <= span + 1.5, \
                    "xlim 没有贴合局部 trace 的跨度（xlim %.2f，traces %.2f）" % (width, span)
    return True


def check_group_shared_limits():
    """A group's flank and global panels share x and y limits so they compare directly."""
    dens = _dens()
    groups = [("Standards", [(2, 1), (3, 1), (5, 1)])]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "limits.png", dens, baseline="both", groups=groups)
        axes = _assert_axes_count(fig, 2)
        assert {_axis_baseline(ax) for ax in axes} == {"flank", "global"}
        xlims = [ax.get_xlim() for ax in axes]
        ylims = [ax.get_ylim() for ax in axes]
        assert abs(xlims[0][0] - xlims[1][0]) <= 1e-6, xlims
        assert abs(xlims[0][1] - xlims[1][1]) <= 1e-6, xlims
        assert abs(ylims[0][0] - ylims[1][0]) <= 1e-6, ylims
        assert abs(ylims[0][1] - ylims[1][1]) <= 1e-6, ylims
    return True


def check_group_x_bounds():
    """Each group zooms to its own selected peaks; groups do not share one giant x range."""
    dens = _dens()
    groups = [("Standards", [(2, 1), (3, 1), (5, 1)]),
              ("Samples", [(4, 1)])]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "bounds.png", dens, baseline="flank", groups=groups)
        axes = _assert_axes_count(fig, 2)
        centers = [(lo + hi) / 2.0 for lo, hi in (ax.get_xlim() for ax in axes)]
        widths = [hi - lo for lo, hi in (ax.get_xlim() for ax in axes)]
        expected = [float(_lane(dens, 2)["bands"][0]["y_cm"]),
                    float(_lane(dens, 4)["bands"][0]["y_cm"])]
        pairs = sorted(zip(centers, widths, axes), key=lambda item: item[0])
        assert abs(pairs[0][0] - min(expected)) <= 0.45, pairs
        assert abs(pairs[1][0] - max(expected)) <= 0.45, pairs
        assert abs(pairs[0][0] - pairs[1][0]) >= 0.8, \
            "两个 group 共用了同一个 x 范围：%r" % (centers,)
        assert all(width < 1.5 for width in widths), \
            "group 没有紧凑地放大到自己的峰：%r" % (widths,)
    return True


def check_lane_selection():
    """`lanes=` keeps exactly the selected lanes in both baseline panels."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "select.png", dens, baseline="both", lanes=[2, 5])
        axes = _assert_axes_count(fig, 4)
        span = _profile_span(_lane(dens, 2))
        assert len(_axes_with_traces(axes, span, 2)) == 2, \
            "lane 2 的两个基线面板应各有 2 条局部 trace"
        assert len(_axes_with_traces(axes, span, 1)) == 2, \
            "lane 5 的两个基线面板应各有 1 条局部 trace"
        text = _all_text(fig)
        assert "Std 1:4" in text and "Std 1:16" in text, text
        assert "Std 1:2" not in text, "未选中的 lane 1 出现在图里"
        assert "Std 1:8" not in text, "未选中的 lane 3 出现在图里"
        assert "Lane 4" not in text, "未选中的 lane 4 出现在图里"
    return True


def check_baseline_single_and_both():
    """Single baselines draw one Axes per lane; `both` draws two separate panels."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        _ret, fig, _caught, _out, _paths = _render_file(
            td, "flank.png", dens, baseline="flank", lanes=[2, 3])
        axes = _assert_axes_count(fig, 2)
        assert all(_axis_baseline(ax) == "flank" for ax in axes)
        assert "global" not in _all_text(fig).lower(), "baseline=flank 仍画了 global"

        _ret, fig, _caught, _out, _paths = _render_file(
            td, "global.png", dens, baseline="global", lanes=[2, 3])
        axes = _assert_axes_count(fig, 2)
        assert all(_axis_baseline(ax) == "global" for ax in axes)
        assert "flank" not in _all_text(fig).lower(), "baseline=global 仍画了 flank"

        _ret, fig, _caught, _out, _paths = _render_file(
            td, "both.png", dens, baseline="both", lanes=[2, 3])
        axes = _assert_axes_count(fig, 4)
        for ax in axes:
            low = _ax_text(ax).lower()
            assert ("flank" in low) != ("global" in low), \
                "baseline=both 的同一个 Axes 混了两种基线：%r" % _ax_text(ax)
        span = _profile_span(_lane(dens, 2))
        for baseline in ("flank", "global"):
            selected = [ax for ax in axes if _axis_baseline(ax) == baseline]
            assert len(selected) == 2, (baseline, len(selected))
            assert len(_axes_with_traces(selected, span, 2)) == 1, baseline
            assert len(_axes_with_traces(selected, span, 1)) == 1, baseline
        _expect(lambda: _plot().plot_bands(dens, os.path.join(td, "bad.png"),
                                           baseline="bogus"))
    return True


def check_groups_overlay():
    """An explicit group overlays its selected bands in one comparison panel per baseline."""
    dens = _dens()
    groups = [("Standards", [(2, None), (3, None), (5, None)])]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "group.png", dens, baseline="both", groups=groups)
        axes = _assert_axes_count(fig, 2)
        assert {_axis_baseline(ax) for ax in axes} == {"flank", "global"}
        text = _all_text(fig)
        assert "Standards" in text, "图里没有组名 Standards：%r" % text
        for ax in axes:
            baseline = _axis_baseline(ax)
            traces = _local_traces(ax, _profile_span(_lane(dens, 2)))
            assert len(traces) == 3, "组里 3 个选择应对应 3 条 trace，实际 %d" % len(traces)
            for no in (2, 3, 5):
                lane = _lane(dens, no)
                selected = _largest_band(lane, baseline)
                _band_trace(ax, selected, _profile_span(lane))
        assert "Std 1:4" in text and "Std 1:8" in text and "Std 1:16" in text, text
        assert "1:4" in text and "1:8" in text and "1:16" in text, "稀释度名字丢了"
    return True


def check_group_band_selection_none():
    """`None` selects the largest stored area on the panel's baseline; explicit index wins."""
    dens = _dens()
    lane = _lane(dens, 2)
    with tempfile.TemporaryDirectory() as td:
        for baseline in ("flank", "global"):
            selected = _largest_band(lane, baseline)
            groups = [("Largest", [(2, None)])]
            (_w, _h), fig, _caught, _out, _paths = _render_file(
                td, "largest-%s.png" % baseline, dens, baseline=baseline, groups=groups)
            ax = _assert_axes_count(fig, 1)[0]
            assert _band_trace(ax, selected, _profile_span(lane)) is not None, \
                "%s 下 None 没有选中最大面积 band %d" % (baseline, selected["index"])
            text = _all_text(fig)
            assert _shows_band(text, selected, baseline), \
                "%s 下可见文字没有标出选中 band（%r）：%r" % (
                    baseline, selected, text)

        # Explicit band 2 (the smaller one) must be respected, not silently replaced by max.
        smaller = lane["bands"][1]
        groups = [("Second", [(2, smaller["index"])])]
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "second.png", dens, baseline="flank", groups=groups)
        ax = _assert_axes_count(fig, 1)[0]
        assert _band_trace(ax, smaller, _profile_span(lane)) is not None, smaller
        assert _shows_band(_all_text(fig), smaller, "flank"), _all_text(fig)
    return True


def check_group_legend_and_labels():
    """Comparison panels keep the group name and the original lane/dilution names."""
    dens = _dens()
    groups = [("Standards", [(2, None), (3, 1), (5, 1)])]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "legend.png", dens, baseline="flank", groups=groups)
        _assert_axes_count(fig, 1)
        text = _all_text(fig)
        assert "Standards" in text, "图里没有组名：%r" % text
        for no, band_index in groups[0][1]:
            lane = _lane(dens, no)
            label = lane.get("label") or ""
            wanted = band_index
            if wanted is None:
                wanted = _largest_band(lane, "flank")["index"]
            assert label in text, "图里没有 lane %d 的原标签 %r：%r" % (no, label, text)
            assert re.search(r"\blane\s*%d\b" % no, text, re.I) or label in text, no
            band = next(b for b in lane["bands"] if int(b["index"]) == int(wanted))
            assert _shows_band(text, band, "flank"), \
                "图里没有选中 band %d 的数据：%r" % (wanted, text)
        # Dilution factors are part of the names, so they must survive.
        assert "1:4" in text and "1:8" in text and "1:16" in text, text
    return True


def check_groups_two_and_order():
    """Two groups are both drawn, in the given order, and keep their own comparison panels."""
    dens = _dens()
    groups = [("Standards", [(2, None), (3, None)]),
              ("Samples", [(4, None)])]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "two.png", dens, baseline="both", groups=groups)
        axes = _assert_axes_count(fig, 4)
        text = _all_text(fig)
        assert "Standards" in text and "Samples" in text, text
        span = _profile_span(_lane(dens, 2))
        for baseline in ("flank", "global"):
            selected = [ax for ax in axes if _axis_baseline(ax) == baseline]
            assert len(selected) == 2, (baseline, len(selected))
            assert len(_axes_with_traces(selected, span, 2)) == 1, baseline
            assert len(_axes_with_traces(selected, span, 1)) == 1, baseline
        # The non-ASCII sample label in lane 4 must fall back, not become tofu.
        assert "Lane 4" in text, text
        assert not _CJK.search(text)
    return True


def check_group_validation():
    """Groups are validated: ASCII printable names, known lanes/bands, no lanes+groups mix."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "g.png")

        def call(groups, lanes=None):
            return lambda: _plot().plot_bands(dens, out, baseline="flank", lanes=lanes,
                                              groups=groups)

        _expect(call([("标准", [(2, None)])]), (ValueError,))
        _expect(call([("Bad\nName", [(2, None)])]), (ValueError,))
        _expect(call([("Bad\tName", [(2, None)])]), (ValueError,))
        _expect(call([("", [(2, None)])]), (ValueError,))
        _expect(call([("Good", [])]), (ValueError,))
        _expect(call([("Good", [(99, None)])]))
        _expect(call([("Good", [(2, 99)])]))
        _expect(call([("Good", [(2, 0)])]))
        _expect(call([("Good", [(True, None)])]), (ValueError,))
        _expect(call([("Good", [(2, True)])]), (ValueError,))
        _expect(call([("Good", [(2, None), (2, None)])]), (ValueError,))
        _expect(call([("Good", [(2,)])]), (ValueError,))
        _expect(call([("Good", [(2, None)], "extra")]), (ValueError,))
        _expect(call([("Good", [(2, None)])], lanes=[2]), (ValueError,))
        _expect(call([]), (ValueError,))
        _expect(call("not-a-list"), (ValueError,))
        assert not os.path.isfile(out), "验证失败后仍写出了 PNG"
    return True


def check_nonenglish_lane_fallback():
    """A non-ASCII lane label becomes `Lane N` and warns with the lane number, once."""
    dens = _dens()
    lane = _lane(dens, 4)
    lane["label"] = "样品A"
    with tempfile.TemporaryDirectory() as td:
        _ret, fig, caught, _out, _paths = _render_file(
            td, "cjk.png", dens, baseline="flank", lanes=[4])
        text = _all_text(fig)
        assert "Lane 4" in text, "非英文泳道没有回退成 Lane 4：%r" % text
        assert not _CJK.search(text), "图里仍有中日韩字符：%r" % _CJK.findall(text)
        from . import dens_plot
        lane_warnings = [w for w in caught if issubclass(w.category, dens_plot.LaneLabelWarning)]
        assert lane_warnings, "非英文泳道没有发出 LaneLabelWarning：%r" % [
            (w.category.__name__, str(w.message)) for w in caught]
        assert any("4" in str(w.message) for w in lane_warnings), \
            "LaneLabelWarning 没有带泳道号：%r" % [str(w.message) for w in lane_warnings]

        # An ASCII lane label is used as-is and does not warn.
        _ret, fig, caught, _out, _paths = _render_file(
            td, "ascii.png", dens, baseline="flank", lanes=[2])
        assert "Std 1:4" in _all_text(fig)
        assert not [w for w in caught if issubclass(w.category, dens_plot.LaneLabelWarning)], \
            "ASCII 泳道也发了 LaneLabelWarning"
    return True


def check_english_and_unicode():
    """Figures are English; lane labels keep their dilution names; µ is allowed where used."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "english.png", dens, baseline="both", lanes=[2])
        text = _all_text(fig)
        assert not _CJK.search(text), "图里出现中日韩字符（应回退 / 英文）：%r" % _CJK.findall(text)
        low = text.lower()
        assert "flank" in low and "global" in low, "缺 baseline 说明"
        assert "lane 2" in low, "缺 lane 2 标识"
        assert "std 1:4" in low, "缺原标签 Std 1:4"
        assert re.search(r"\bcm\b", low), "缺 migration 单位 cm：%r" % text
        assert "gray" in low, "缺 gray 单位：%r" % text
    return True


def check_api_errors():
    """Bad dens/options/output paths fail with one-line ValueError/RuntimeError; no output."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "x.png")
        _expect(lambda: _plot().plot_bands(dens, os.path.join(td, "x.jpg")))
        _expect(lambda: _plot().plot_bands(dens, td))
        _expect(lambda: _plot().plot_bands(dens, os.path.join(td, "no", "x.png")))
        _expect(lambda: _plot().plot_bands(dens, png, baseline="bogus"), (ValueError,))
        _expect(lambda: _plot().plot_bands(dens, png, cols=0), (ValueError,))
        _expect(lambda: _plot().plot_bands(dens, png, width=100), (ValueError,))

        bad = dict(dens)
        bad["kind"] = "wrong"
        _expect(lambda: _plot().plot_bands(bad, png))

        _expect(lambda: _plot().plot_bands(dens, png, lanes=[99]))
        _expect(lambda: _plot().plot_bands(dens, png, lanes=[]))
        _expect(lambda: _plot().plot_bands(dens, png, lanes="two"))
        _expect(lambda: _plot().plot_bands(dens, png, lanes=2))
        assert not os.path.isfile(png), "失败路径留下了输出 PNG"

        # Empty lanes list in the dens is a public error, not a blank PNG.
        empty = dict(dens)
        empty["lanes"] = []
        _expect(lambda: _plot().plot_bands(empty, png))
    return True


def check_cli_errors_and_overwrite():
    """CLI errors are one `[!]` line; an output hard-linked to the input is refused."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "missing.json"])
            assert rc != 0
            _one_line(err, "missing dens")

            _write_json("bad.json", {"kind": "wrong"})
            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "bad.json"])
            assert rc != 0
            _one_line(err, "bad kind")

            _write_json("d.dens.json", dens)
            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                  "--lanes", "99"])
            assert rc != 0
            _one_line(err, "unknown lane")

            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                  "--out", "."])
            assert rc != 0
            _one_line(err, "directory output")

            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                  "--out", "no/such/x.png"])
            assert rc != 0
            _one_line(err, "missing parent")

            os.link("d.dens.json", "alias.png")
            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                  "--out", "alias.png"])
            assert rc != 0
            _one_line(err, "hard-linked input")
            assert "d.dens.json" in err, err
            assert os.path.getsize("d.dens.json") > 0, "输入 dens.json 被覆盖"

            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                  "--group", "标准=2"])
            assert rc != 0
            _one_line(err, "non-ASCII group")

            for bad_group in ("Standards", "=2", "Standards=abc", "Standards=2,99"):
                rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                      "--out", "bad-group.png", "--group", bad_group])
                assert rc != 0, ("非法 --group 被接受", bad_group)
                _one_line(err, "bad group %r" % bad_group)
                assert not os.path.exists("bad-group.png"), \
                    "非法 --group 仍写出了 PNG：%r" % bad_group

            rc, _out, err = _cli(["dens", "plot-bands", "--dens", "d.dens.json",
                                  "--lanes", "2", "--group", "Standards=2"])
            assert rc == 2, "与 --lanes 互斥时应是 argparse 错误（rc 2），收到 %r" % rc
    return True


def check_no_magick_no_temp():
    """The renderer does not touch ImageMagick and leaves no MVG / temp debris."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        from . import magick_io
        boom = AssertionError("matplotlib 渲染不应调用 ImageMagick")
        with unittest.mock.patch.object(magick_io, "_run", side_effect=boom):
            (_w, _h), _fig, _caught, out, _paths = _render_file(
                td, "clean.png", dens, lanes=[2])
        assert os.path.isfile(out)
        leftovers = [n for n in os.listdir(td) if n != "clean.png"]
        assert leftovers == [], "渲染后留下了临时文件：%r" % leftovers
        assert not any(n.endswith(".mvg") for n in os.listdir(td))
    return True


def check_atomic_save():
    """The PNG is written to a temp path and renamed atomically; no direct write to `out`."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "atomic.png")
        ret, fig, _caught, final, paths = _render(out, dens, lanes=[2])
        assert final == out
        assert os.path.isfile(out), "原子写后没有最终 PNG"
        saved = [os.path.abspath(p) for p in paths if p]
        assert saved, "没有捕获到 savefig 路径"
        assert all(p != os.path.abspath(out) for p in saved), \
            "直接 savefig 到最终路径，不是 atomic temp + rename：%r" % saved
        for path in saved:
            assert not os.path.exists(path), "临时 PNG 没有清理：%s" % path
        leftovers = [n for n in os.listdir(td) if n != "atomic.png"]
        assert leftovers == [], "输出目录有临时残留：%r" % leftovers
    return True


def check_cli_groups():
    """CLI --group is repeatable and yields the same comparison panels as the API."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        dens_path = os.path.join(td, "d.dens.json")
        _write_json(dens_path, dens)
        out = os.path.join(td, "cli.png")
        with _capture_figures() as (figures, _paths):
            rc, stdout, stderr = _cli(["dens", "plot-bands", "--dens", dens_path,
                                       "--out", out, "--group", "Standards=2,3,5"])
        assert rc == 0 and _stderr_ok(stderr), (rc, stderr)
        assert figures, "CLI --group 没有保存 Figure"
        fig = figures[-1]
        axes = _assert_axes_count(fig, 2)
        assert {_axis_baseline(ax) for ax in axes} == {"flank", "global"}
        assert "Standards" in _all_text(fig)
        assert "Std 1:4" in _all_text(fig) and "Std 1:16" in _all_text(fig)

        out2 = os.path.join(td, "cli2.png")
        with _capture_figures() as (figures, _paths):
            rc, stdout, stderr = _cli(["dens", "plot-bands", "--dens", dens_path,
                                       "--out", out2,
                                       "--group", "Standards=2,3",
                                       "--group", "Samples=4"])
        assert rc == 0 and _stderr_ok(stderr), (rc, stderr)
        fig = figures[-1]
        axes = _assert_axes_count(fig, 4)
        text = _all_text(fig)
        assert "Standards" in text and "Samples" in text, text
        span = _profile_span(_lane(dens, 2))
        for baseline in ("flank", "global"):
            selected = [ax for ax in axes if _axis_baseline(ax) == baseline]
            assert len(selected) == 2, (baseline, len(selected))
            assert len(_axes_with_traces(selected, span, 2)) == 1, baseline
            assert len(_axes_with_traces(selected, span, 1)) == 1, baseline
    return True


def _rgba(line):
    from matplotlib.colors import to_rgba
    return tuple(round(float(v), 6) for v in to_rgba(line.get_color()))


def _colors_by_baseline(fig, dens):
    """Trace colours in drawing order for each baseline panel of a group."""
    span = max(_profile_span(lane) for lane in dens["lanes"])
    out = {}
    for ax in _axes(fig):
        baseline = _axis_baseline(ax)
        if baseline in ("flank", "global"):
            out[baseline] = [_rgba(line) for line in _local_traces(ax, span)]
    return out


def _rgb_dist(a, b):
    return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5


def check_group_concentration_colors():
    """A group with concentrations colours members continuously, equally in both baselines."""
    dens = _dens()
    members = [(1, 1, 0.0), (2, 2, 0.5), (4, 1, 1.0)]
    equal = [(1, 1, 0.5), (2, 2, 0.5), (4, 1, 0.5)]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "conc.png", dens, baseline="both", groups=[("Series", members)])
        axes = _assert_axes_count(fig, 2)
        assert {_axis_baseline(ax) for ax in axes} == {"flank", "global"}
        colors = _colors_by_baseline(fig, dens)
        flank, glob = colors["flank"], colors["global"]
        assert len(flank) == len(members), "3 个成员应有 3 条 trace，实际 %d" % len(flank)
        assert len(glob) == len(members)
        assert len(set(flank)) == len(members),             "不同浓度没有映射到不同颜色：%r" % (flank,)
        assert flank == glob, "同一个成员的 Flank / Global 颜色不一致：%r vs %r" % (flank, glob)

        # Equal concentrations cannot be ordered and therefore share one colour.
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "conc-equal.png", dens, baseline="both", groups=[("Equal", equal)])
        colors = _colors_by_baseline(fig, dens)
        assert len(set(colors["flank"])) == 1,             "相同浓度应使用同一颜色：%r" % (colors["flank"],)
        assert colors["flank"] == colors["global"]
        assert "Equal" in _all_text(fig)
    return True


def check_group_concentration_scale():
    """Colours follow numeric concentration (not member rank); without values, member order."""
    dens = _dens()
    members = [(1, 1), (2, 2), (4, 1)]
    with tempfile.TemporaryDirectory() as td:
        groups_a = [("Series", [(no, band, value) for (no, band), value in
                                zip(members, (1.0, 2.0, 3.0))])]
        groups_b = [("Series", [(no, band, value) for (no, band), value in
                                zip(members, (1.0, 2.0, 100.0))])]
        (_w, _h), fig_a, _ca, _oa, _pa = _render_file(
            td, "scale-a.png", dens, baseline="flank", groups=groups_a)
        (_w, _h), fig_b, _cb, _ob, _pb = _render_file(
            td, "scale-b.png", dens, baseline="flank", groups=groups_b)
        col_a = _colors_by_baseline(fig_a, dens)["flank"]
        col_b = _colors_by_baseline(fig_b, dens)["flank"]
        assert len(col_a) == len(col_b) == 3
        assert len(set(col_a)) == 3 and len(set(col_b)) == 3
        # In 1..100 the middle value (2) sits near the low end; a rank palette would not move.
        assert _rgb_dist(col_b[1], col_b[0]) < 0.5 * _rgb_dist(col_b[1], col_b[2]),             "颜色没有按浓度数值缩放：%r" % (col_b,)
        assert col_a[1] != col_b[1], "同一成员的中间浓度在不同量程下颜色相同（像按序号着色）"

        # Without concentrations, sequential colours follow the member order inside a group.
        order_a = [("Order", [(5, None), (1, None), (2, None)])]
        order_b = [("Order", [(1, None), (2, None), (5, None)])]
        (_w, _h), fig_a, _ca, _oa, _pa = _render_file(
            td, "order-a.png", dens, baseline="flank", groups=order_a)
        (_w, _h), fig_b, _cb, _ob, _pb = _render_file(
            td, "order-b.png", dens, baseline="flank", groups=order_b)
        ca = _colors_by_baseline(fig_a, dens)["flank"]
        cb = _colors_by_baseline(fig_b, dens)["flank"]
        by_a = dict(zip((5, 1, 2), ca))
        by_b = dict(zip((1, 2, 5), cb))
        assert len(set(ca)) == 3
        assert any(by_a[no] != by_b[no] for no in (1, 2, 5)),             "无浓度时颜色没有按成员顺序分配（仍按泳道号排序）：%r / %r" % (by_a, by_b)
    return True


def check_group_no_area_fill():
    """Group overlays are lines/dots/window ticks only; an individual lane keeps signed fill."""
    dens = _dens()
    members = [(1, 1), (2, 2), (4, 1)]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "group-nofill.png", dens, baseline="both",
            groups=[("Overlay", members)])
        axes = _assert_axes_count(fig, 2)
        for ax in axes:
            assert not _area_fills(ax), \
                "分组叠加不应有面积填充（%s）" % _axis_baseline(ax)
        # The lane view keeps the signed integration context.
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "lane-fill.png", dens, baseline="both", lanes=[1])
        axes = _assert_axes_count(fig, 2)
        for ax in axes:
            assert _area_fills(ax), \
                "单泳道视图丢了面积填充（%s）" % _axis_baseline(ax)
    return True


def check_paired_trace_sample_grid():
    """A group member's Flank/Global traces share one x sample grid even with unlike windows.

    The two members deliberately have far-apart windows and different areas.  Pairing is by
    group order (the renderer draws one data line per member in each baseline panel), so the
    test also works when a grouped tile spans several peaks.
    """
    dens = _dens()
    members = [(1, 1), (2, 2)]
    span = max(_profile_span(lane) for lane in dens["lanes"])
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "paired.png", dens, baseline="both", groups=[("Paired", members)])
        axes = _assert_axes_count(fig, 2)
        by_baseline = {_axis_baseline(ax): ax for ax in axes}
        flank = _local_traces(by_baseline["flank"], span)
        glob = _local_traces(by_baseline["global"], span)
        assert len(flank) == len(members), (len(flank), members)
        assert len(glob) == len(members), (len(glob), members)

        bands = [_band_by_index(_lane(dens, lane_no), idx) for lane_no, idx in members]
        for i, band in enumerate(bands):
            xf, yf = _line_xy(flank[i])
            xg, yg = _line_xy(glob[i])
            assert len(xf) == len(xg),                 "member %d 的 Flank/Global 采样数不同：%d vs %d" % (i, len(xf), len(xg))
            assert float(abs(xf - xg).max()) <= 1e-9,                 "member %d 的 Flank/Global x 采样网格没有对齐：%r vs %r" % (i, xf, xg)
            assert float(abs(yf - yg).max()) > 1e-6,                 "member %d 的 Flank/Global 数据被混用了" % i
            for baseline, ax in by_baseline.items():
                w0, w1 = _window(band, baseline)
                ticks = _window_tick_positions(ax)
                for edge in (w0, w1):
                    assert any(abs(pos - edge) <= 0.02 for pos in ticks),                         "member %d 的 %s 窗口刻度缺失 x=%.3f" % (i, baseline, edge)

        # The aligned grids must not collapse the two members' windows or areas.
        for baseline in ("flank", "global"):
            wa = _window(bands[0], baseline)
            wb = _window(bands[1], baseline)
            assert max(abs(wa[0] - wb[0]), abs(wa[1] - wb[1])) > 0.2, (baseline, wa, wb)
        text = _all_text(fig)
        for band in bands:
            for baseline in ("flank", "global"):
                assert _has_number(text, band["area_%s" % baseline], 0.12),                     "band index %s 的 %s 面积没有出现在图中" % (band["index"], baseline)
        assert abs(float(bands[0]["area_flank"]) - float(bands[1]["area_flank"])) > 0.2
        assert abs(float(bands[0]["area_global"]) - float(bands[1]["area_global"])) > 0.2
    return True


def check_group_coolwarm_colors():
    """Numeric group colours follow Matplotlib coolwarm endpoints (hue, not exact RGB)."""
    dens = _dens()
    members = [(1, 1, 0.0), (2, 2, 1.0)]
    with tempfile.TemporaryDirectory() as td:
        (_w, _h), fig, _caught, _out, _paths = _render_file(
            td, "coolwarm.png", dens, baseline="both",
            groups=[("Ramp", members)])
        colors = _colors_by_baseline(fig, dens)
        flank, glob = colors["flank"], colors["global"]
        assert len(flank) == 2 and flank == glob
        assert flank[0] != flank[1], "两个端点浓度用了同一种颜色"
        from matplotlib import colormaps
        cmap = colormaps["coolwarm"]
        lo = _hue(cmap(0.0))
        hi = _hue(cmap(1.0))
        hues = [_hue(c) for c in flank]
        assert any(_hue_gap(h, lo) <= 0.08 for h in hues), \
            "低浓度端点不是 coolwarm 的蓝色端：%r（期望 hue %.3f）" % (hues, lo)
        assert any(_hue_gap(h, hi) <= 0.08 for h in hues), \
            "高浓度端点不是 coolwarm 的红色端：%r（期望 hue %.3f）" % (hues, hi)
    return True


def check_group_concentration_validation():
    """Concentration triples must be all-or-none, numeric, finite and nonnegative."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "bad.png")

        def call(members):
            return lambda: _plot().plot_bands(
                dens, out, baseline="flank", groups=[("Series", members)])

        _expect(call([(1, 1, 0.1), (2, 2)]))
        _expect(call([(1, 1, -0.1)]))
        _expect(call([(1, 1, float("nan"))]))
        _expect(call([(1, 1, float("inf"))]))
        _expect(call([(1, 1, True)]))
        _expect(call([(1, 1, "0.1")]))
        _expect(call([(1, 1, 0.1, 2)]))
        _expect(call([(1, 1, 0.1), (1, 1, 0.2)]))
        # Zero is a valid concentration.
        _plot().plot_bands(dens, out, baseline="flank",
                           groups=[("Series", [(1, 1, 0.0), (2, 2, 0.0)])])
        assert os.path.isfile(out)
    return True


def check_cli_group_concentrations():
    """CLI `lane[:band]@value` accepts complete groups and rejects partial/negative/NaN values."""
    dens = _dens()
    with tempfile.TemporaryDirectory() as td:
        dens_path = os.path.join(td, "d.dens.json")
        _write_json(dens_path, dens)
        out = os.path.join(td, "conc.png")
        with _capture_figures() as (figures, _paths):
            rc, _stdout, stderr = _cli([
                "dens", "plot-bands", "--dens", dens_path, "--out", out,
                "--group", "Series=1:1@0.0,2:2@1,4:1@100"])
        assert rc == 0 and _stderr_ok(stderr), (rc, stderr)
        assert figures, "CLI concentration group 没有保存 Figure"
        fig = figures[-1]
        colors = _colors_by_baseline(fig, dens)
        assert len(set(colors["flank"])) == 3
        assert colors["flank"] == colors["global"]
        assert os.path.isfile(out)

        for k, spec in enumerate(["Series=2@0.1,3",
                                  "Series=2@-0.1",
                                  "Series=2@nan",
                                  "Series=2@inf",
                                  "Series=2@abc",
                                  "Series=2@"]):
            bad = os.path.join(td, "bad-%d.png" % k)
            rc, _stdout, stderr = _cli([
                "dens", "plot-bands", "--dens", dens_path, "--out", bad, "--group", spec])
            assert rc != 0, ("非法浓度被接受", spec)
            _one_line(stderr, "bad concentration %r" % spec)
            assert not os.path.exists(bad), "非法浓度仍写出了 PNG：%r" % spec
    return True


CHECKS = (
    check_api_signature,
    check_cli_parser,
    check_default_path_and_alias,
    check_size_and_rerun,
    check_lane_panels_and_local_traces,
    check_trace_geometry_and_baseline,
    check_window_shading,
    check_zero_line_and_zoom,
    check_lane_selection,
    check_baseline_single_and_both,
    check_group_shared_limits,
    check_group_x_bounds,
    check_groups_overlay,
    check_group_band_selection_none,
    check_group_legend_and_labels,
    check_group_concentration_colors,
    check_group_concentration_scale,
    check_group_no_area_fill,
    check_paired_trace_sample_grid,
    check_group_coolwarm_colors,
    check_group_concentration_validation,
    check_cli_group_concentrations,
    check_groups_two_and_order,
    check_group_validation,
    check_nonenglish_lane_fallback,
    check_english_and_unicode,
    check_api_errors,
    check_cli_errors_and_overwrite,
    check_no_magick_no_temp,
    check_atomic_save,
    check_cli_groups,
)


def run():
    """Run the whole acceptance suite; returns the number of passed checks."""
    _plot()
    for check in CHECKS:
        check()
    print("selftest_dens_plot %d/%d 通过" % (len(CHECKS), len(CHECKS)))
    return len(CHECKS)


if __name__ == "__main__":
    raise SystemExit(run())
