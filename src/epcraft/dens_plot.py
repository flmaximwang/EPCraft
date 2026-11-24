#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dens_plot — matplotlib renderers for the densitometry chain (spec §4.7).

Two public pictures, both drawn directly with matplotlib's Agg backend (no pyplot, no GUI,
no ImageMagick/MVG):

``plot_bands(dens, out, baseline="both", lanes=None, cols=2, width=1440, *, groups=None)``
    Local band-peak traces, one tile per lane (or per named group).  The x axis is the true
    migration distance from the dens.json profile (``y_cm``), the y axis is the active
    baseline minus the raw gray value (a signed ink trace).  All traces in the figure share a
    zero line and one y scale, so a standard peak and a sample peak can be compared directly.
    The stored integration window is shaded where the trace is positive; a short context
    around the window is drawn but not shaded.  With ``groups`` a tile overlays the selected
    ``(lane, band)`` members (``band=None`` picks the strongest band for the active baseline);
    a member may carry a concentration as a third tuple item, and the group is then coloured
    by a per-group sequential scale.  Without groups each lane gets its own tile and every
    band in that lane is shown in the stable lane palette.

``plot_curve(curve, out, quant=None, width=1020)``
    The standard curve (and optional sample markers) with a residual strip that shares the
    area axis.  Replicate bars are horizontal on the area axis because ``curve.points[].sd``
    is the sample SD of replicate areas, not of masses.

Only ``matplotlib`` is optional.  It is imported lazily inside the two plot functions, so
``import epcraft.dens_plot`` and the non-plot CLI commands keep working without the extra;
a plot call without it raises one RuntimeError whose message contains ``epcraft[plot]``.
The module also exposes ``LaneLabelWarning``.  Every render that has to replace a non-English
lane label with its numeric fallback (``Lane N`` for bands, ``L<n>`` for quant samples) emits
exactly one warning per affected lane through ``warnings.warn_explicit`` (the normal Python
warning channel, visible on stderr by default), so a CLI user can suppress it with
``warnings.simplefilter`` and a test can record it with ``warnings.catch_warnings(record=True)``.

All figure prose is English.  Standard unit symbols that Arial covers (µ, ·, ±, ², →) are
used; user lane labels are only drawn when they are printable ASCII, otherwise the stable
English fallback is used and the warning above is emitted.
"""
import math
import numbers
import os
import re
import struct
import tempfile
import warnings

from . import densitometry
from . import text_layout

BASELINES = ("flank", "global", "both")
KIND = densitometry.KIND

# ---------------------------------------------------------------- colours and type sizes

CURVE_RGB = (32, 32, 32)           # the old gray-profile colour, kept for compatibility
GLOBAL_RGB = (30, 90, 200)         # the global baseline: blue
FLANK_RGB = (235, 130, 15)         # the flank ramp: orange
GLOBAL_FILL = (30, 90, 200)
FLANK_FILL = (240, 140, 20)
FILL_ALPHA = 0.35
GRID_RGB = (150, 150, 150)
TEXT_RGB = (20, 20, 20)
MUTED_RGB = (95, 95, 95)

TITLE_FONT = 26                    # retained for callers; the new layout has no page header
PARAMS_FONT = 17
LEGEND_FONT = 16
HEADER_FONT = 23                   # group heading / `Lane n: label`
LABEL_FONT = 16                    # everything else (ticket: >= 16 px for labels)
TRACE_TICK_FONT = 16
TRACE_AXIS_FONT = 17
TRACE_PANEL_FONT = 17

# plot-curve's palette, retained as module constants: the acceptance selftest samples them.
POINT_RGB = (37, 78, 150)
POINT_STROKE_RGB = (17, 45, 104)
DROP_RGB = (196, 88, 42)
LINE_RGB = (38, 38, 38)
SAMPLE_RGB = (12, 132, 72)
SAMPLE_STROKE_RGB = (7, 82, 44)
OOR_RGB = (148, 58, 168)
ZERO_RGB = (60, 60, 60)
GUIDE_RGB = (150, 150, 150)
ERR_RGB = (105, 105, 105)
CURVE_FRAME_RGB = GRID_RGB

CURVE_TITLE_FONT = 38
CURVE_SUB_FONT = 22
ANN_FONT = 23
AXIS_FONT = 25
CURVE_TICK_FONT = 20
CURVE_LEGEND_FONT = 20
MARK_FONT = 20

POINT_R = 4.0
SAMPLE_HALF = 4.5
RESID_R = 3.5
ERR_BAR_MIN_PX = 1.0

ANN_STEP = 30
TOP_PAD = 20
LEFT_PAD = 110
CURVE_RIGHT_PAD = 48
GUIDE_LABEL_MIN = 15.0

ERROR_BAR_AXIS = "x"

# plot-bands' geometry.  These are pixels at DPI=100; the figure is created at that exact
# size, so a layout box and a rendered pixel agree.
DPI = 100
MARGIN = 24
TITLE_H = 0                        # no filename/params/legend block above the tiles
GAP_ROWS = 26
COL_GAP = 20
GUTTER = 70
RIGHT_PAD = 12
AXIS_H = 42
TILE_HEADER_H = 48
SUB_GAP = 10
LEGEND_TOP = 7
LEGEND_LINE_H = 22
LEGEND_PAD = 8
LEGEND_HEADER_H = 20
XLABEL_H = 46
NARROW_PANEL = 260
BAND_CONTEXT_MIN = 0.04            # cm of context drawn on each side of a window
BAND_CONTEXT_MAX = 0.20
TRACE_MIN_WIDTH = 160

LANE_PALETTE = (
    (31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40),
    (148, 103, 189), (140, 86, 75), (227, 119, 194), (127, 127, 127),
    (188, 189, 34), (23, 190, 207), (174, 199, 232), (255, 187, 120),
)

# Matplotlib's actual coolwarm samples at 0, 1/16, ..., 1, kept as a fallback for the
# geometry-only `band_layout` path (and for installs where the optional plot extra is not
# importable).  `_sequential_color` prefers `matplotlib.colormaps["coolwarm"]` at render
# time, so the plotted colours are the real colormap, not a lookalike.
SEQUENTIAL_RGB = (
    (59, 76, 192), (78, 104, 216), (98, 130, 234), (119, 154, 247),
    (141, 176, 254), (163, 194, 254), (185, 208, 249), (204, 217, 237),
    (221, 220, 220), (236, 211, 197), (245, 196, 172), (247, 176, 147),
    (244, 152, 122), (235, 125, 98), (221, 95, 75), (202, 59, 55),
    (180, 4, 38),
)


class LaneLabelWarning(UserWarning):
    """A non-English lane label was replaced by the English fallback ``Lane N``.

    Public because it is the warning channel promised to the caller: one warning per
    affected lane per render, emitted with ``warnings.warn_explicit``.  Filter it like any
    other Python warning.
    """


# ---------------------------------------------------------------- optional matplotlib

def _load_mpl():
    """Import the Agg Figure pieces, or raise one actionable RuntimeError."""
    try:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except Exception as exc:                       # ImportError, broken install, missing lib
        raise RuntimeError(
            "绘图需要 matplotlib（可选依赖）。安装："
            "uv tool install --force '/path/to/EPCraft[plot]'"
            "（或 pip install 'epcraft[plot]'）；开发环境可用 uv sync --extra plot"
        ) from exc
    return Figure, FigureCanvasAgg


def _font_path():
    """The repo's preferred font file, or None (matplotlib's default sans then)."""
    try:
        path = text_layout.pick_font()
    except Exception:
        path = None
    return path or None


def _font():
    """Compatibility helper: the font file, or the old ``"Arial"`` family fallback."""
    return _font_path() or "Arial"


def _font_prop(size_px=LABEL_FONT, font=None):
    """A FontProperties at ``size_px`` pixels (matplotlib sizes are points at DPI)."""
    from matplotlib.font_manager import FontProperties
    size_pt = float(size_px) * 72.0 / DPI
    candidate = font if font is not None else _font_path()
    if isinstance(candidate, str) and os.path.exists(candidate):
        return FontProperties(fname=candidate, size=size_pt)
    if isinstance(candidate, str) and candidate and candidate != "Arial":
        return FontProperties(family=candidate, size=size_pt)
    return FontProperties(family="DejaVu Sans", size=size_pt)


_WIDTH_CACHE = {}


def _text_width(font, size, text):
    """Rendered width of `text` in pixels (measured with matplotlib, estimated as fallback)."""
    key = (font, int(size), str(text))
    if key not in _WIDTH_CACHE:
        try:
            from matplotlib.textpath import TextPath
            prop = _font_prop(size, font)
            width_pt = TextPath((0.0, 0.0), str(text), prop=prop,
                                size=float(size) * 72.0 / DPI).get_extents().width
            _WIDTH_CACHE[key] = int(math.ceil(max(0.0, width_pt) * DPI / 72.0))
        except Exception:
            _WIDTH_CACHE[key] = int(0.55 * float(size) * len(str(text)))
    return _WIDTH_CACHE[key]


def _estimate_width(size, text):
    """Cheap width estimate for packing decisions (no font machinery)."""
    return int(0.55 * float(size) * len(str(text)))


def _clip_text(text, size, max_px, font=None):
    """Truncate text to `max_px`, with a trailing ``...`` when it does not fit."""
    text = str(text)
    if max_px <= 0:
        return ""
    if _text_width(font, size, text) <= max_px:
        return text
    keep = len(text)
    while keep > 0:
        keep -= 1
        probe = text[:keep].rstrip() + "..."
        if _text_width(font, size, probe) <= max_px:
            return probe
    return "..."


def _safe_ascii(text, limit=80):
    """English-figure-safe data text: printable ASCII, whitespace collapsed, truncated."""
    out = []
    for ch in str(text):
        code = ord(ch)
        if ch in "\r\n\t":
            out.append(" ")
        elif 32 <= code < 127:
            out.append(ch)
        else:
            out.append("?")
    value = " ".join("".join(out).split())
    if limit and len(value) > limit:
        value = value[:limit - 3].rstrip() + "..."
    return value


def _english_filename(path):
    """A report basename safe for an English figure title."""
    if not isinstance(path, str) or not path:
        return ""
    return _safe_ascii(os.path.basename(path), 80)


def _unit_text(unit):
    """Display text for a unit: ``ug`` → ``µg`` while preserving a standard µ spelling."""
    value = str(unit).strip() if unit is not None else ""
    known = {
        "ug": "µg", "uG": "µg", "UG": "µg", "µg": "µg",
        "ul": "µL", "uL": "µL", "uL": "µL", "µL": "µL",
        "um": "µm", "uM": "µM", "µM": "µM",
        "ug/mL": "µg/mL", "ug/uL": "µg/µL", "ug/ul": "µg/µL",
    }
    if value in known:
        return known[value]
    value = re.sub(r"^u(?=[gGlLmM])", "µ", value)
    if all(32 <= ord(ch) < 127 for ch in value) or all(
            ch in "µ·±²→" or 32 <= ord(ch) < 127 for ch in value):
        return value
    return _safe_ascii(value, 18)


def _is_english_label(label):
    """True only when a lane label is non-empty printable ASCII (no tofu, no control)."""
    if not isinstance(label, str):
        return False
    text = label.strip()
    return bool(text) and all(32 <= ord(ch) < 127 for ch in text)


def _display_label(label, limit=28):
    """The text part after ``Lane n:``; None when there is nothing safe to draw."""
    if not _is_english_label(label):
        return None
    value = str(label).strip()
    if len(value) > limit:
        value = value[:limit - 3].rstrip() + "..."
    return value


def _check_out(out):
    """Validate a plot output path (ruling R12) → absolute path to write.

    ValueError (single line, Chinese) when the path does not end in `.png` (any case), when it
    is an existing directory, or when its parent directory is missing — output directories are
    never created implicitly (`plot-bands` writes next to the dens, it never invents a tree).
    """
    text = str(out)
    if not text.lower().endswith(".png"):
        raise ValueError("输出文件必须以 .png 结尾（收到 %r）" % text)
    target = os.path.abspath(text)
    if os.path.isdir(target):
        raise ValueError("输出路径 %s 是个目录（要写文件路径）" % target)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("输出目录 %s 不存在（不自动创建，先建好它）" % parent)
    return target


def _png_size(path):
    """(width, height) read from a PNG's IHDR, so the return value is what was written."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("渲染失败（%s）：写出的不是 PNG" % path)
    return struct.unpack(">II", head[16:24])


def _save_png(fig, width, height, target):
    """Atomically save `fig` as an 8-bit PNG and return its actual size."""
    fd, tmp = tempfile.mkstemp(prefix=".dens-plot-", suffix=".png",
                                dir=os.path.dirname(target) or ".")
    os.close(fd)
    try:
        try:
            fig.savefig(tmp, format="png", dpi=DPI, facecolor="white", edgecolor="none",
                        metadata={"Software": None})
        except TypeError:
            fig.savefig(tmp, format="png", dpi=DPI, facecolor="white", edgecolor="none")
        os.replace(tmp, target)
    except Exception as exc:
        cause = " ".join(str(exc).splitlines()[:1]).strip() or "matplotlib 保存失败"
        raise RuntimeError("渲染失败（%s）：%s" % (target, cause)) from exc
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return _png_size(target)


def _mcolor(rgb):
    """RGB 0..255 tuple → matplotlib 0..1 tuple."""
    return (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def _fig_rect(box, width, height):
    """Top-left pixel box → matplotlib figure rect (left, bottom, width, height)."""
    x0, y0, x1, y1 = [float(v) for v in box]
    return (x0 / width, 1.0 - y1 / height, (x1 - x0) / width, (y1 - y0) / height)


def _fig_text(fig, x, y, text, size, color=TEXT_RGB, ha="left", va="baseline",
              font=None, zorder=10, clip_width=None):
    """Absolute-pixel text; `clip_width` truncates with the same measured-width helper."""
    if clip_width is not None:
        text = _clip_text(text, size, clip_width, font)
    fig.text(x / fig.bbox.width, 1.0 - y / fig.bbox.height, str(text),
             ha=ha, va=va, color=_mcolor(color),
             fontproperties=_font_prop(size, font), zorder=zorder)


# ---------------------------------------------------------------- value → pixel mapping

class Mapping:
    """A value rectangle mapped into a pixel box: x to the right, y down (or up)."""
    def __init__(self, box, x_range, y_range, y_down=True):
        self.x0, self.y0, self.x1, self.y1 = [float(v) for v in box]
        self.x_min, self.x_max = [float(v) for v in x_range]
        self.y_min, self.y_max = [float(v) for v in y_range]
        if self.x_max <= self.x_min:
            self.x_max = self.x_min + 1.0
        if self.y_max <= self.y_min:
            self.y_max = self.y_min + 1.0
        self.y_down = bool(y_down)

    def x(self, value):
        t = (float(value) - self.x_min) / (self.x_max - self.x_min)
        return self.x0 + t * (self.x1 - self.x0)

    def y(self, value):
        t = (float(value) - self.y_min) / (self.y_max - self.y_min)
        if self.y_down:
            return self.y0 + t * (self.y1 - self.y0)
        return self.y1 - t * (self.y1 - self.y0)

    def x_value(self, px):
        if self.x1 == self.x0:
            return self.x_min
        t = (float(px) - self.x0) / (self.x1 - self.x0)
        return self.x_min + t * (self.x_max - self.x_min)

    def y_value(self, py):
        if self.y1 == self.y0:
            return self.y_min
        t = (float(py) - self.y0) / (self.y1 - self.y0)
        if self.y_down:
            return self.y_min + t * (self.y_max - self.y_min)
        return self.y_max - t * (self.y_max - self.y_min)


class TraceAxes:
    """plot-bands' mapping: ``.x(y_cm)``, ``.y(ink)``, plus the pixel box and ranges."""
    def __init__(self, box, x_range, y_range):
        self.box = tuple(float(v) for v in box)
        self.mapping = Mapping(box, x_range, y_range, y_down=False)
        self.x_range = tuple(float(v) for v in x_range)
        self.y_range = tuple(float(v) for v in y_range)

    def x(self, y_cm):
        return self.mapping.x(y_cm)

    def y(self, ink):
        return self.mapping.y(ink)

    def x_value(self, px):
        return self.mapping.x_value(px)

    def y_value(self, py):
        return self.mapping.y_value(py)


class CurveAxes:
    """plot-curve's value → pixel mapping: `.x(volume)`, `.y(mass)`, `.yr(rel_pct)`."""
    def __init__(self, main_box, resid_box, x_range, y_range, rel_range):
        self.main = Mapping(main_box, x_range, y_range, y_down=False)
        self.resid = Mapping(resid_box, x_range, rel_range, y_down=False)
        self.box = main_box
        self.resid_box = resid_box
        self.x_range = tuple(float(v) for v in x_range)
        self.y_range = tuple(float(v) for v in y_range)
        self.rel_range = tuple(float(v) for v in rel_range)

    def x(self, volume):
        return self.main.x(volume)

    def y(self, mass):
        return self.main.y(mass)

    def yr(self, rel_pct):
        return self.resid.y(rel_pct)

    def x_value(self, px):
        return self.main.x_value(px)

    def y_value(self, py):
        return self.main.y_value(py)


# ---------------------------------------------------------------- dens validation / lanes

def _lane_numbers(dens):
    """[(lane number, lane dict)] in dens order, validating the parts the plot needs."""
    if not isinstance(dens, dict) or dens.get("kind") != KIND:
        raise RuntimeError("不是 %s 文件（kind=%r）"
                           % (KIND, dens.get("kind") if isinstance(dens, dict) else None))
    lanes = dens.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise RuntimeError("dens 里没有泳道（先跑 measure）")
    out = []
    for ln in lanes:
        if not isinstance(ln, dict):
            raise RuntimeError("dens 的 lanes 里有不是对象的条目")
        try:
            no = int(ln.get("lane"))
        except (TypeError, ValueError):
            raise RuntimeError("dens 的泳道条目缺少整数 lane（收到 %r）" % (ln.get("lane"),))
        prof = ln.get("profile")
        if not isinstance(prof, dict):
            raise RuntimeError("dens 的泳道 %d 没有 profile" % no)
        arrays = []
        for key in ("y_cm", "gray", "baseline_global"):
            vals = prof.get(key)
            if not isinstance(vals, list) or not vals:
                raise RuntimeError("dens 的泳道 %d 的 profile 缺少 %s 数组" % (no, key))
            try:
                [float(v) for v in vals]
            except (TypeError, ValueError):
                raise RuntimeError("dens 的泳道 %d 的 profile.%s 不是数字数组" % (no, key))
            arrays.append(vals)
        if len(arrays[0]) != len(arrays[1]) or len(arrays[0]) != len(arrays[2]):
            raise RuntimeError("dens 的泳道 %d 的 profile 三条数组长度不一致（%d / %d / %d）"
                               % (no, len(arrays[0]), len(arrays[1]), len(arrays[2])))
        out.append((no, ln))
    return out


def _select(dens, lanes):
    """Lane numbers to plot: None = every lane in dens order, else those lanes in dens order."""
    pairs = _lane_numbers(dens)
    available = [no for no, _ln in pairs]
    if lanes is None:
        return pairs
    try:
        wanted = [int(v) for v in lanes]
    except (TypeError, ValueError):
        raise RuntimeError("lanes 必须是泳道号列表（收到 %r）" % (lanes,))
    missing = [v for v in wanted if v not in available]
    if missing:
        raise RuntimeError("dens 里没有泳道 %s（dens 有 %s）"
                           % (", ".join(str(v) for v in missing),
                              ", ".join(str(v) for v in available)))
    if not wanted:
        raise RuntimeError("lanes 是空的，没有可画的泳道（省略它 = 画全部）")
    return [(no, ln) for no, ln in pairs if no in set(wanted)]


def _check_options(baseline, cols, width):
    """plot-bands' option contract (ValueError, per §4.7)."""
    if baseline not in BASELINES:
        raise ValueError("baseline 只能是 %s（收到 %r）"
                         % ("/".join(BASELINES), baseline))
    try:
        c = int(cols)
        w = int(width)
    except (TypeError, ValueError):
        raise ValueError("cols / width 必须是整数（收到 %r / %r）" % (cols, width))
    if c < 1:
        raise ValueError("cols 必须 ≥ 1（收到 %d）" % c)
    if w < 600:
        raise ValueError("width 必须 ≥ 600 像素（收到 %d）" % w)
    return c, w


def _lane_header(lane, display=None):
    """`Lane 2: S1`; a missing or non-English label prints just the lane number."""
    no = int(lane.get("lane"))
    text = "Lane %d" % no
    if display:
        text += ": " + display
    return text


def _band_index(band):
    """The integer band index, or None (a malformed dens is a RuntimeError)."""
    try:
        idx = band.get("index")
        if isinstance(idx, bool) or idx is None:
            raise TypeError
        return int(idx)
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("dens 的带缺少整数 index（收到 %r）"
                           % (band.get("index") if isinstance(band, dict) else None,))


def _bands(lane):
    """The lane's bands as dicts, with validated unique integer indices."""
    bands = lane.get("bands")
    if bands is None:
        bands = []
    if not isinstance(bands, list):
        raise RuntimeError("dens 的泳道 %r 的 bands 不是列表" % lane.get("lane"))
    out = []
    seen = set()
    for band in bands:
        if not isinstance(band, dict):
            raise RuntimeError("dens 的泳道 %r 的 bands 里有不是对象的条目" % lane.get("lane"))
        idx = _band_index(band)
        if idx in seen:
            raise RuntimeError("dens 的泳道 %r 的带 index %d 重复" % (lane.get("lane"), idx))
        seen.add(idx)
        out.append((idx, band))
    return out


def _window(band, baseline, lane_no):
    """A validated (y0, y1) integration window for one baseline."""
    key = "window_flank" if baseline == "flank" else "window_global"
    win = band.get(key)
    if (not isinstance(win, (list, tuple)) or len(win) != 2):
        raise RuntimeError("dens 的泳道 %d 的带 %r 缺少 %s"
                           % (lane_no, band.get("index"), key))
    try:
        a, b = float(win[0]), float(win[1])
    except (TypeError, ValueError):
        raise RuntimeError("dens 的泳道 %d 的带 %r 的 %s 不是两个数字"
                           % (lane_no, band.get("index"), key))
    if b < a:
        a, b = b, a
    if b - a <= 1e-12:
        b = a + 1e-9
    return a, b


def _area(band, baseline, lane_no):
    """The stored area for one baseline, as a float."""
    key = "area_flank" if baseline == "flank" else "area_global"
    try:
        return float(band[key])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("dens 的泳道 %d 的带 %r 缺少数字 %s"
                           % (lane_no, band.get("index"), key))


def _row_at(lane, y_cm):
    """Profile row nearest to a band's y_cm."""
    y = lane["profile"]["y_cm"]
    return min(range(len(y)), key=lambda k: abs(float(y[k]) - float(y_cm)))


def _ramp_at(band, y_cm):
    """The flank ramp value at a depth (linear between endpoints, clamped outside)."""
    ramp = band.get("ramp")
    if not isinstance(ramp, dict):
        raise RuntimeError("dens 的带 %r 缺少 ramp" % (band.get("index"),))
    try:
        (yl, gl), (yr, gr) = ramp["left"], ramp["right"]
        yl, gl, yr, gr = float(yl), float(gl), float(yr), float(gr)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("dens 的带 %r 的 ramp 端点不完整" % (band.get("index"),))
    if yr <= yl:
        return gl
    t = (float(y_cm) - yl) / (yr - yl)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return gl + (gr - gl) * t


def _rows(lane, window):
    """Row indices of the profile inside a closed window."""
    y = lane["profile"]["y_cm"]
    a, b = float(window[0]) - 1e-9, float(window[1]) + 1e-9
    return [k for k, v in enumerate(y) if a <= float(v) <= b]


# ---------------------------------------------------------------- group normalisation

def _pick_band(lane, lane_no, band_index, baseline, group_name):
    """Resolve ``None`` to the strongest band for `baseline`; validate an explicit index."""
    bands = _bands(lane)
    if not bands:
        raise ValueError("group %r 的泳道 %d 没有带，不能画局部峰" % (group_name, lane_no))
    if band_index is None:
        key = "area_flank" if baseline in ("flank", "both") else "area_global"
        best = None
        for idx, band in bands:
            try:
                area = float(band.get(key))
            except (TypeError, ValueError):
                raise RuntimeError("dens 的泳道 %d 的带 %r 缺少数字 %s"
                                   % (lane_no, idx, key))
            # max area; a tie goes to the lower band index
            rank = (area, -idx)
            if best is None or rank > best[0]:
                best = (rank, idx, band)
        return best[1], best[2]
    if isinstance(band_index, bool):
        raise ValueError("group %r 的带号必须是整数（收到 %r）" % (group_name, band_index))
    try:
        want = int(band_index)
    except (TypeError, ValueError):
        raise ValueError("group %r 的带号必须是整数（收到 %r）" % (group_name, band_index))
    for idx, band in bands:
        if idx == want:
            return idx, band
    raise ValueError("group %r 的泳道 %d 没有带 %d（有 %s）"
                     % (group_name, lane_no, want,
                        ", ".join(str(i) for i, _b in bands) or "无"))


def _normalize_groups(dens, groups, baseline, lanes):
    """Validate `groups` / `lanes` into ordered tile specifications.

    Each group is ``(english_name, [(lane_no, band_index_or_None), ...])``.  Members are
    resolved and validated against the report; duplicate members inside one group are rejected.
    """
    if groups is not None and lanes is not None:
        raise ValueError("lanes 与 groups 不能同时使用（groups 自己指定泳道和带）")
    by_no = {no: lane for no, lane in _lane_numbers(dens)}
    if groups is None:
        selected = _select(dens, lanes)
        tiles = []
        for no, lane in selected:
            bands = _bands(lane)
            members = [{"lane": no, "lane_dict": lane, "band": band, "index": idx}
                       for idx, band in bands]
            tiles.append({"kind": "lane", "name": None, "lane": no,
                          "lane_dict": lane, "members": members})
        return tiles

    if not isinstance(groups, (list, tuple)) or not groups:
        raise ValueError("groups 必须是非空序列（省略它 = 按泳道画）")
    tiles = []
    for entry in groups:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError("每个 group 必须是 (name, members) 两元组（收到 %r）" % (entry,))
        raw_name, raw_members = entry
        if not isinstance(raw_name, str):
            raise ValueError("group 名称必须是字符串（收到 %r）" % (raw_name,))
        name = raw_name.strip()
        if not name or any(not (32 <= ord(ch) < 127) for ch in name):
            raise ValueError("group 名称必须是可打印 ASCII 英文（收到 %r）" % (raw_name,))
        if not isinstance(raw_members, (list, tuple)) or not raw_members:
            raise ValueError("group %r 的成员不能为空" % name)
        members = []
        seen = set()
        for token in raw_members:
            if not isinstance(token, (list, tuple)) or len(token) not in (2, 3):
                raise ValueError("group %r 的成员必须是 (lane, band) 或 "
                                 "(lane, band, concentration)（收到 %r）" % (name, token))
            raw_lane, raw_band = token[0], token[1]
            raw_conc = token[2] if len(token) == 3 else None
            if isinstance(raw_lane, bool):
                raise ValueError("group %r 的泳道号必须是整数（收到 %r）" % (name, raw_lane))
            try:
                lane_no = int(raw_lane)
            except (TypeError, ValueError):
                raise ValueError("group %r 的泳道号必须是整数（收到 %r）" % (name, raw_lane))
            if len(token) == 3:
                if isinstance(raw_conc, bool) or not isinstance(raw_conc, numbers.Real):
                    raise ValueError("group %r 的浓度必须是 0 或正有限数（收到 %r）"
                                     % (name, raw_conc))
                conc = float(raw_conc)
                if not math.isfinite(conc) or conc < 0.0:
                    raise ValueError("group %r 的浓度必须是 0 或正有限数（收到 %r）"
                                     % (name, raw_conc))
            else:
                conc = None
            lane = by_no.get(lane_no)
            if lane is None:
                raise ValueError("group %r 要求泳道 %d，但 dens 只有 %s"
                                 % (name, lane_no, ", ".join(str(n) for n in by_no) or "无"))
            idx, band = _pick_band(lane, lane_no, raw_band, baseline, name)
            key = (lane_no, idx)
            if key in seen:
                raise ValueError("group %r 重复选择了泳道 %d 的带 %d" % (name, lane_no, idx))
            seen.add(key)
            members.append({"lane": lane_no, "lane_dict": lane, "band": band,
                            "index": idx, "concentration": conc})
        with_conc = sum(1 for m in members if m["concentration"] is not None)
        if with_conc and with_conc != len(members):
            raise ValueError("group %r 的浓度必须全部给出或全部省略（%d/%d 个成员有值）"
                             % (name, with_conc, len(members)))
        tiles.append({"kind": "group", "name": name, "lane": None,
                      "members": members})
    return tiles


# ---------------------------------------------------------------- plot-bands: traces / layout

def _context_span(band, lane_no, baseline):
    """One band's stored window plus the short local context used by traces and axes."""
    w0, w1 = _window(band, baseline, lane_no)
    span = max(w1 - w0, 1e-6)
    context = max(BAND_CONTEXT_MIN, min(BAND_CONTEXT_MAX, 0.25 * span))
    return (w0 - context, w1 + context)


def _trace_for(lane, lane_no, band, idx, baseline, x_min, x_max, domain=None):
    """Extract one local ink trace (true migration on x, active baseline minus gray on y).

    `domain` is the shared sampling range for this band.  A grouped tile passes the tile's
    full local x range so the Flank and Global traces of one member have identical extents;
    a single-lane tile passes the union of that member's own window contexts only, so a
    trace neither stops at its own narrower window nor runs through another band.
    """
    prof = lane["profile"]
    ys = [float(v) for v in prof["y_cm"]]
    gray = [float(v) for v in prof["gray"]]
    if baseline == "flank":
        base = [_ramp_at(band, y) for y in ys]
    else:
        base = [float(v) for v in prof["baseline_global"]]
    ink = [b - g for b, g in zip(base, gray)]
    w0, w1 = _window(band, baseline, lane_no)
    a0, a1 = domain if domain is not None else (w0, w1)
    a, b = max(x_min, a0), min(x_max, a1)
    rows = [k for k, y in enumerate(ys) if a - 1e-9 <= y <= b + 1e-9]
    if len(rows) < 2:                              # a window outside the profile: nearest rows
        near = sorted(range(len(ys)), key=lambda k: abs(ys[k] - 0.5 * (w0 + w1)))[:2]
        rows = sorted(near)
    rows = sorted(rows, key=lambda k: ys[k])
    xs = [ys[k] for k in rows]
    vals = [ink[k] for k in rows]
    inside = [k for k in rows if w0 - 1e-9 <= ys[k] <= w1 + 1e-9]
    peak_k = max(inside, key=lambda k: ink[k]) if inside else max(rows, key=lambda k: ink[k])
    areas = {}
    for key, name in (("area_flank", "flank"), ("area_global", "global")):
        try:
            areas[name] = float(band[key])
        except (KeyError, TypeError, ValueError):
            areas[name] = None
    mw = band.get("mw_kda")
    try:
        mw = float(mw) if mw is not None else None
    except (TypeError, ValueError):
        mw = None
    notes = [_safe_ascii(n, 48)
             for n in (band.get("notes") or []) if isinstance(n, str) and n.strip()]
    y_cm = band.get("y_cm")
    try:
        y_cm = float(y_cm)
    except (TypeError, ValueError):
        y_cm = 0.5 * (w0 + w1)
    return {
        "lane": lane_no, "band": idx, "y_cm": y_cm,
        "window": (w0, w1), "x": xs, "ink": vals,
        "peak": (ys[peak_k], ink[peak_k]),
        "area": _area(band, baseline, lane_no),
        "areas": areas, "mw": mw,
        "clipped": bool(band.get("clipped")),
        "flat_top": bool(band.get("flat_top")),
        "notes": notes, "baseline": baseline,
    }


def _lane_color_map(lane_numbers):
    """Stable lane → colour mapping, in numeric lane order."""
    numbers = sorted(set(int(n) for n in lane_numbers))
    return {no: LANE_PALETTE[k % len(LANE_PALETTE)] for k, no in enumerate(numbers)}


def _sequential_color(t):
    """RGB from Matplotlib's actual coolwarm colormap at 0 ≤ t ≤ 1.

    The embedded table is only a fallback for the pure-geometry path when the optional
    matplotlib extra cannot be imported; normal renders ask the real colormap.
    """
    t = max(0.0, min(1.0, float(t)))
    try:
        from matplotlib import colormaps
        r, g, b, _a = colormaps["coolwarm"](t)
        return (int(round(r * 255.0)), int(round(g * 255.0)), int(round(b * 255.0)))
    except Exception:
        pass
    pos = t * (len(SEQUENTIAL_RGB) - 1)
    i = int(min(len(SEQUENTIAL_RGB) - 2, math.floor(pos)))
    f = pos - i
    a, b = SEQUENTIAL_RGB[i], SEQUENTIAL_RGB[i + 1]
    return tuple(int(round(a[k] + (b[k] - a[k]) * f)) for k in range(3))


def _assign_group_colors(tile):
    """Color a group tile: numeric concentration if present, else member order.

    A group has either a concentration on every member or on none (validated in
    ``_normalize_groups``).  Numeric values are normalized to this group only; equal values
    all get the middle stop rather than implying a range.  Without values, colors follow the
    member order and no numerical scale is claimed.
    """
    if tile.get("kind") != "group":
        return
    members = tile["members"]
    values = [m.get("concentration") for m in members]
    numeric = bool(values) and all(v is not None for v in values)
    if numeric:
        lo, hi = min(values), max(values)
        if hi == lo:
            stops = [0.5] * len(members)
        else:
            stops = [(float(v) - lo) / (hi - lo) for v in values]
    else:
        stops = [0.5 if len(members) == 1 else i / float(len(members) - 1)
                 for i in range(len(members))]
    for member, t in zip(members, stops):
        member["color_t"] = t
        member["color"] = _sequential_color(t)


def _fmt_conc(value):
    """Compact concentration text for the table (4 significant digits)."""
    if value is None:
        return ""
    text = "%.4g" % float(value)
    if "e" in text or "E" in text:
        return text
    return _trim_num(text)


def _band_legend_row(member, display, baseline):
    """One compact legend-table row: lane.band, label, MW, flank/global area, flags."""
    lane_no = int(member["lane"])
    index = member.get("index")
    lane_band = str(lane_no) if index is None else "%d.%d" % (lane_no, int(index))
    label = display if display and display != "Lane %d" % lane_no else "Lane %d" % lane_no
    band = member.get("band") or {}
    mw = band.get("mw_kda")
    mw_text = "-" if mw is None else "%.1f" % float(mw)
    if baseline == "both":
        f, g = band.get("area_flank"), band.get("area_global")
        area_text = "%s / %s" % ("-" if f is None else "%.1f" % float(f),
                                 "-" if g is None else "%.1f" % float(g))
    else:
        value = band.get("area_" + baseline)
        area_text = "-" if value is None else "%.1f" % float(value)
    flags = []
    if band.get("flat_top"):
        flags.append("flat")
    if band.get("clipped"):
        flags.append("clip")
    conc_text = _fmt_conc(member.get("concentration"))
    text = "%s  %s  %s  %s  %s  %s" % (lane_band, label, conc_text, mw_text,
                                      area_text, ",".join(flags))
    return {"lane_band": lane_band, "label": label, "conc": conc_text,
            "mw": mw_text, "area": area_text, "flags": ",".join(flags), "text": text,
            "color": member.get("color"), "linestyle": member.get("linestyle")}


def _legend_text(member, display, baseline):
    """One legend line: lane, band, English label, MW, area and notable flags."""
    trace = member["traces"]["flank" if baseline in ("flank", "both") else "global"]
    text = "Lane %d b%d" % (member["lane"], member["index"])
    if display:
        text += " (%s)" % display
    if trace.get("mw") is not None:
        text += " · %.1f kDa" % trace["mw"]
    areas = trace.get("areas") or {}
    if baseline == "both":
        f, g = areas.get("flank"), areas.get("global")
        text += " · flank %s / global %s gray·cm" % (
            "-" if f is None else "%.1f" % f, "-" if g is None else "%.1f" % g)
    else:
        value = trace.get("area")
        text += " · %s %s gray·cm" % (baseline, "-" if value is None else "%.1f" % value)
    flags = []
    if trace.get("clipped"):
        flags.append("!clipped")
    if trace.get("flat_top"):
        flags.append("flat top")
    if flags:
        text += " · " + ", ".join(flags)
    if trace.get("notes"):
        text += " · " + trace["notes"][0]
    return text


def _tile_subtitle(tile, baseline):
    if tile["kind"] == "group":
        lanes = len({m["lane"] for m in tile["members"]})
        text = "%d trace%s · %d lane%s" % (
            len(tile["members"]), "" if len(tile["members"]) == 1 else "s",
            lanes, "" if lanes == 1 else "s")
    else:
        count = len(tile["members"])
        text = "%d band%s" % (count, "" if count == 1 else "s") if count else "no bands"
    text += " · flank + global" if baseline == "both" else " · baseline " + baseline
    return text


def band_layout(dens, baseline="both", lanes=None, cols=2, width=1440, groups=None):
    """Geometry and trace data for plot-bands.

    Returns a dict with the canvas size, one entry per *tile* (a lane or a named group), the
    pixel boxes of each baseline sub-panel and the extracted local traces.  A tile contains
    one plot for ``baseline='flank'/'global'`` and two paired plots for ``baseline='both'``;
    ``--cols`` counts tiles, so a pair never splits across columns.
    """
    cols, width = _check_options(baseline, cols, width)
    tiles = _normalize_groups(dens, groups, baseline, lanes)
    n_tiles = len(tiles)
    rows = (n_tiles + cols - 1) // cols

    tile_w = (width - 2.0 * MARGIN - (cols - 1) * COL_GAP) / float(cols)
    axes_w = tile_w - GUTTER - RIGHT_PAD
    if axes_w < TRACE_MIN_WIDTH:
        raise ValueError("width %d 下无法绘制 %d 列（每列只剩 %.0f px，请增大 --width "
                         "或减少 --cols）" % (width, cols, axes_w))

    # one y scale for every trace: comparing standards with samples only means something when
    # the zero line and the vertical scale are shared.
    all_lane_numbers = []
    for tile in tiles:
        for m in tile["members"]:
            if m["lane"] not in all_lane_numbers:
                all_lane_numbers.append(m["lane"])
    colors = _lane_color_map(all_lane_numbers)

    def _tile_x_range(tile):
        """Local x limits: union of this tile's band contexts, pair included, no full profile."""
        xs = []
        for member in tile["members"]:
            for which in (("flank", "global") if baseline == "both" else (baseline,)):
                xs.extend(_context_span(member["band"], member["lane"], which))
        if not xs:
            xs = [float(v) for v in tile["lane_dict"]["profile"]["y_cm"]]
        if not xs:
            return (0.0, 1.0)
        lo, hi = min(xs), max(xs)
        if hi <= lo:
            hi = lo + 0.01
        pad = max(0.005, 0.02 * (hi - lo))
        return (max(0.0, lo - pad), hi + pad)

    for tile in tiles:
        tile["x_range"] = _tile_x_range(tile)
    x_range = (min(t["x_range"][0] for t in tiles), max(t["x_range"][1] for t in tiles))

    # resolve members and traces before fixing the y range
    loose = []
    for tile in tiles:
        _assign_group_colors(tile)
        lane_seen = {}
        for m in tile["members"]:
            lane_no = m["lane"]
            lane = m["lane_dict"]
            display = _display_label(lane.get("label"))
            m["display"] = display
            if not m.get("color"):
                m["color"] = colors.get(lane_no, LANE_PALETTE[0])
            busy = lane_seen.get(lane_no, 0)
            lane_seen[lane_no] = busy + 1
            m["linestyle"] = ("-", "--", "-.", ":")[busy % 4]
            m["traces"] = {}
            if tile.get("kind") == "group":
                # Every baseline of every member shares the tile-local x domain, so a
                # narrower Global window cannot visibly cut its trace short.
                trace_domain = tile["x_range"]
            else:
                # Per member, not per band window: Flank and Global align, but the trace
                # does not wander through this lane's other bands.
                spans = [_context_span(m["band"], lane_no, which)
                         for which in (("flank", "global") if baseline == "both" else (baseline,))]
                trace_domain = (min(a for a, _b in spans), max(b for _a, b in spans))
            for which in (("flank", "global") if baseline == "both" else (baseline,)):
                tr = _trace_for(lane, lane_no, m["band"], m["index"], which,
                                tile["x_range"][0], tile["x_range"][1], domain=trace_domain)
                tr["color"] = m["color"]
                tr["linestyle"] = m["linestyle"]
                m["traces"][which] = tr
                loose.append(tr)
    values = [v for tr in loose for v in tr["ink"]] or [0.0, 1.0]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    span = hi - lo
    y_lo, y_hi = lo - 0.06 * span, hi + 0.06 * span
    if y_lo > 0.0:
        y_lo = 0.0
    if y_hi < 0.0:
        y_hi = 0.0
    if y_hi - y_lo < 1e-9:
        y_hi = y_lo + 1.0
    y_range = (y_lo, y_hi)

    n_plots = 2 if baseline == "both" else 1
    sub_h = 200.0
    max_members = max([len(t["members"]) for t in tiles] + [1])
    legend_h = (LEGEND_PAD + LEGEND_HEADER_H + max_members * LEGEND_LINE_H + 6)
    tile_h = (TILE_HEADER_H + n_plots * sub_h + (n_plots - 1) * SUB_GAP
              + XLABEL_H + legend_h)
    height = int(round(2 * MARGIN + rows * tile_h + (rows - 1) * GAP_ROWS))

    plot_x0 = MARGIN + GUTTER
    plot_x1 = MARGIN + tile_w - RIGHT_PAD
    for k, tile in enumerate(tiles):
        col, row = k % cols, k // cols
        cx = MARGIN + col * (tile_w + COL_GAP)
        cy = MARGIN + row * (tile_h + GAP_ROWS)
        tile["cell"] = (cx, cy, tile_w, tile_h)
        tile["title"] = (tile["name"] if tile["kind"] == "group"
                         else _lane_header(tile["lane_dict"], _display_label(
                             tile["lane_dict"].get("label"))))
        tile["subtitle"] = ""
        tile["legend_box"] = (cx + 4.0,
                              cy + TILE_HEADER_H + n_plots * sub_h
                              + (n_plots - 1) * SUB_GAP + XLABEL_H,
                              cx + tile_w - 4.0, cy + tile_h - 2.0)
        tile["plots"] = []
        for p, which in enumerate(("flank", "global") if baseline == "both" else (baseline,)):
            top = cy + TILE_HEADER_H + p * (sub_h + SUB_GAP)
            box = (plot_x0 + col * (tile_w + COL_GAP), top,
                   plot_x1 + col * (tile_w + COL_GAP), top + sub_h)
            axes = TraceAxes(box, tile["x_range"], y_range)
            plot = {"baseline": which, "box": box, "axes": axes,
                    "title": which, "x_range": tile["x_range"],
                    "traces": [m["traces"][which] for m in tile["members"]]}
            tile["plots"].append(plot)
        tile["legend_rows"] = [_band_legend_row(m, m["display"], baseline)
                               for m in tile["members"]]
        # Both names carry the SAME dict rows: _draw_band_legend reads row["color"] /
        # row["linestyle"]; `legend_text` is the flat-string view for callers that want text.
        tile["legend_lines"] = list(tile["legend_rows"])
        tile["legend_text"] = [row["text"] for row in tile["legend_rows"]]
        tile["traces"] = [m["traces"] for m in tile["members"]]

    title = ""
    params_line = ""
    legend = ""
    panels = {}
    axes_list = []
    for t_idx, tile in enumerate(tiles):
        for p_idx, plot in enumerate(tile["plots"]):
            panels[(t_idx, plot["baseline"])] = plot["axes"]
            axes_list.append(plot["axes"])
    return {
        "width": width, "height": height, "cols": cols, "rows": rows,
        "tile_w": tile_w, "tile_h": tile_h, "sub_h": sub_h,
        "margin": MARGIN, "title_h": TITLE_H, "baseline": baseline,
        "tiles": tiles, "panels": panels, "axes": axes_list,
        "x_range": x_range, "y_range": y_range,
        "lane_order": [m["lane"] for tile in tiles for m in tile["members"]],
        "title": title, "params_line": params_line, "legend": legend,
        "font": _font(),
    }


def _warn_lane_labels(tiles):
    """One LaneLabelWarning per affected lane in this render."""
    seen = set()
    for tile in tiles:
        for m in tile["members"]:
            lane_no = m["lane"]
            if lane_no in seen:
                continue
            seen.add(lane_no)
            label = m["lane_dict"].get("label")
            if isinstance(label, str) and label.strip() and not _is_english_label(label):
                msg = ("lane %d label %r is not English; drawn as 'Lane %d'"
                       % (lane_no, label, lane_no))
                warnings.warn_explicit(msg, LaneLabelWarning, __file__, 0, registry=None)


# ---------------------------------------------------------------- plot-bands: drawing

def _style_trace_axes(ax, geo, show_y_label, show_x_label):
    """Common frame/ticks/grid styling for one local trace panel (readable at 1440 px)."""
    font = geo["font"]
    for spine in ax.spines.values():
        spine.set_color(_mcolor(GRID_RGB))
        spine.set_linewidth(0.8)
    ax.set_facecolor("white")
    ax.grid(True, axis="both", color=_mcolor(GRID_RGB), alpha=0.18,
            linewidth=0.5, linestyle=":")
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", colors=_mcolor(MUTED_RGB), width=0.7,
                   length=3.0, labelsize=10, pad=3.0)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(_font_prop(TRACE_TICK_FONT, font))
        label.set_color(_mcolor(MUTED_RGB))
    if show_y_label:
        ax.set_ylabel("Baseline - gray (gray units)", color=_mcolor(MUTED_RGB),
                      fontproperties=_font_prop(TRACE_AXIS_FONT, font), labelpad=6)
    if show_x_label:
        ax.set_xlabel("Migration (cm)", color=_mcolor(MUTED_RGB),
                      fontproperties=_font_prop(TRACE_AXIS_FONT, font), labelpad=5)


def _draw_band_plot(fig, geo, tile, plot, show_y_label, show_x_label):
    """Draw one baseline sub-panel.

    Individual lane tiles keep the signed window fill; grouped overlays deliberately drop
    fills so several concentration traces stay readable, but keep the lines, peak dots,
    same-colour integration endpoint ticks and zero line.  Coolwarm's midpoint is nearly
    white, so grouped lines/ticks get a thin dark backing stroke; the actual colormap colour
    stays on top and the table swatch uses the same colour.
    """
    ax = fig.add_axes(_fig_rect(plot["box"], geo["width"], geo["height"]))
    ax.set_xlim(plot["x_range"])
    ax.set_ylim(geo["y_range"])
    ax.set_autoscale_on(False)
    _style_trace_axes(ax, geo, show_y_label, show_x_label)
    panel_name = "Global" if plot["baseline"] == "global" else "Flank"
    ax.text(0.010, 0.96, panel_name, transform=ax.transAxes, ha="left", va="top",
            color=_mcolor(GLOBAL_RGB if plot["baseline"] == "global" else FLANK_RGB),
            fontproperties=_font_prop(TRACE_PANEL_FONT, geo["font"]), zorder=6,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.68, "pad": 1.0})
    ax.axhline(0.0, color=_mcolor(ZERO_RGB), linewidth=1.0, zorder=1.4)
    grouped = tile.get("kind") == "group"
    for tr in plot["traces"]:
        color = _mcolor(tr["color"])
        w0, w1 = tr["window"]
        xs, ys = tr["x"], tr["ink"]
        if not grouped:
            inside = [(w0 - 1e-9 <= x <= w1 + 1e-9) for x in xs]
            ax.fill_between(xs, 0.0, ys,
                            where=[inside[k] and v >= 0.0 for k, v in enumerate(ys)],
                            interpolate=True, color=color, alpha=0.24, linewidth=0, zorder=1.8)
            ax.fill_between(xs, 0.0, ys,
                            where=[inside[k] and v < 0.0 for k, v in enumerate(ys)],
                            interpolate=True, color=color, alpha=0.12, linewidth=0, zorder=1.7)
        if grouped:
            # A coolwarm midpoint is ~RGB(221,220,220): nearly invisible on white.  The
            # dark backing is only a visibility aid; the visible stroke remains the true
            # colormap colour.
            ax.plot(xs, ys, color=_mcolor((38, 38, 38)), linewidth=3.5,
                    alpha=0.42, linestyle=tr["linestyle"], solid_capstyle="round",
                    zorder=2.9)
        ax.plot(xs, ys, color=color, linewidth=1.7, linestyle=tr["linestyle"],
                solid_capstyle="round", zorder=3)
        tick_h = max(0.35, 0.035 * (geo["y_range"][1] - geo["y_range"][0]))
        if grouped:
            ax.vlines([w0, w1], -tick_h, tick_h, color=_mcolor((38, 38, 38)),
                      linewidth=2.4, linestyles=(0, (3, 2)), alpha=0.38, zorder=2.4)
        ax.vlines([w0, w1], -tick_h, tick_h, color=color, linewidth=1.0,
                  linestyles=(0, (3, 2)), alpha=0.85, zorder=2.5)
        px, py = tr["peak"]
        ax.plot([px], [py], marker="o", markersize=4.5, color=color,
                markeredgecolor=_mcolor((42, 42, 42)) if grouped else "white",
                markeredgewidth=0.9 if grouped else 0.7, zorder=4)
    # tick sets: include zero; the top panel of a pair hides its x numbers so the pair reads
    # as one shared migration axis.
    yticks = sorted(set([0.0] + [v for v in _ticks(geo["y_range"][0], geo["y_range"][1], 4)]))
    yticks = [v for v in yticks if geo["y_range"][0] - 1e-9 <= v <= geo["y_range"][1] + 1e-9]
    ax.set_yticks(yticks)
    ax.set_yticklabels([_eqnum(v, 0 if abs(v) >= 10 else 1) for v in yticks])
    xticks = _ticks(plot["x_range"][0], plot["x_range"][1], 5)
    ax.set_xticks(xticks)
    if show_x_label:
        ax.set_xticklabels([_eqnum(v, 2 if (plot["x_range"][1] - plot["x_range"][0]) < 10 else 1)
                            for v in xticks])
    else:
        ax.tick_params(axis="x", labelbottom=False)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(_font_prop(TRACE_TICK_FONT, geo["font"]))
        label.set_color(_mcolor(MUTED_RGB))
    # Keep the published pixel layout exact even if a tick sits just outside the range.
    ax.set_xlim(plot["x_range"])


def _draw_band_header(fig, geo, tile):
    """One group/lane heading above a tile; no filename or explanatory page header."""
    cx, cy, tw, _th = tile["cell"]
    _fig_text(fig, cx + 4.0, cy + 24.0, tile["title"], HEADER_FONT, TEXT_RGB,
              va="center", font=geo["font"], clip_width=tw - 10.0)


def _draw_band_legend(fig, geo, tile, baseline="both"):
    """Compact table under a tile: lane.band, label, MW, flank/global area, flags."""
    lx0, ly0, lx1, _ly1 = tile["legend_box"]
    from matplotlib.lines import Line2D
    rule = Line2D([lx0 / geo["width"], lx1 / geo["width"]],
                  [1.0 - ly0 / geo["height"], 1.0 - ly0 / geo["height"]],
                  transform=fig.transFigure, color=_mcolor(GRID_RGB),
                  linewidth=0.7, zorder=5)
    fig.add_artist(rule)
    rows = tile.get("legend_rows") or tile.get("legend_lines") or []
    if not rows:
        _fig_text(fig, lx0 + 4.0, ly0 + LEGEND_PAD + 10.0, "no bands measured",
                  TRACE_TICK_FONT, MUTED_RGB, va="center", font=geo["font"])
        return
    width = max(1.0, lx1 - lx0)
    has_conc = all(bool(row.get("conc")) for row in rows)
    x_lane = lx0 + 18.0
    x_label = lx0 + (58.0 if has_conc else 72.0)
    x_conc = lx0 + 0.40 * width
    x_mw = lx0 + (0.51 if has_conc else 0.50) * width
    x_area = lx0 + 0.69 * width
    x_flags = lx0 + 0.92 * width
    hy = ly0 + LEGEND_PAD + 10.0
    header_size = 16
    area_header = ("F/G area (gray·cm)" if baseline == "both"
                   else "area (gray·cm)")
    if _text_width(geo["font"], header_size, area_header) > x_flags - x_area - 4.0:
        area_header = "F/G area" if baseline == "both" else "area"
    header_cells = [("lane", x_lane, 38.0),
                    ("label", x_label, (x_conc if has_conc else x_mw) - x_label - 6.0)]
    if has_conc:
        header_cells.append(("conc", x_conc, x_mw - x_conc - 4.0))
    header_cells.extend([
        ("MW (kDa)", x_mw, x_area - x_mw - 4.0),
        (area_header, x_area, x_flags - x_area - 4.0),
        ("flags", x_flags, lx1 - x_flags)])
    for text, x, clip in header_cells:
        _fig_text(fig, x, hy, text, header_size, MUTED_RGB, va="center",
                  font=geo["font"], clip_width=max(0.0, clip))
    first = ly0 + LEGEND_PAD + LEGEND_HEADER_H + LEGEND_LINE_H / 2.0
    grouped = tile.get("kind") == "group"
    for i, row in enumerate(rows):
        cy = first + i * LEGEND_LINE_H
        if grouped:
            backing = Line2D([lx0 / geo["width"], (lx0 + 13.0) / geo["width"]],
                             [1.0 - cy / geo["height"], 1.0 - cy / geo["height"]],
                             transform=fig.transFigure, color=_mcolor((38, 38, 38)),
                             linewidth=3.6, alpha=0.42, linestyle=row["linestyle"],
                             zorder=5.5)
            fig.add_artist(backing)
        swatch = Line2D([lx0 / geo["width"], (lx0 + 13.0) / geo["width"]],
                        [1.0 - cy / geo["height"], 1.0 - cy / geo["height"]],
                        transform=fig.transFigure, color=_mcolor(row["color"]),
                        linewidth=2.2, linestyle=row["linestyle"], zorder=6)
        fig.add_artist(swatch)
        _fig_text(fig, x_lane, cy, row["lane_band"], TRACE_TICK_FONT, TEXT_RGB,
                  va="center", font=geo["font"])
        _fig_text(fig, x_label, cy, row["label"], TRACE_TICK_FONT, TEXT_RGB,
                  va="center", font=geo["font"],
                  clip_width=max(0.0, (x_conc if has_conc else x_mw) - x_label - 8.0))
        if has_conc:
            _fig_text(fig, x_conc, cy, row["conc"], TRACE_TICK_FONT, TEXT_RGB,
                      va="center", font=geo["font"],
                      clip_width=max(0.0, x_mw - x_conc - 4.0))
        _fig_text(fig, x_mw, cy, row["mw"], TRACE_TICK_FONT, TEXT_RGB,
                  va="center", font=geo["font"], clip_width=max(0.0, x_area - x_mw - 6.0))
        _fig_text(fig, x_area, cy, row["area"], TRACE_TICK_FONT, TEXT_RGB,
                  va="center", font=geo["font"], clip_width=max(0.0, x_flags - x_area - 6.0))
        _fig_text(fig, x_flags, cy, row["flags"], TRACE_TICK_FONT, MUTED_RGB,
                  va="center", font=geo["font"], clip_width=max(0.0, lx1 - x_flags))


def _draw_bands_title(fig, geo):
    """Title, run parameters and the general legend in the top margin."""
    _fig_text(fig, MARGIN, MARGIN + 2.0, geo["title"], TITLE_FONT, TEXT_RGB,
              va="top", font=geo["font"], clip_width=geo["width"] - 2 * MARGIN)
    _fig_text(fig, MARGIN, MARGIN + 34.0, geo["params_line"], PARAMS_FONT, MUTED_RGB,
              va="top", font=geo["font"], clip_width=geo["width"] - 2 * MARGIN)
    _fig_text(fig, MARGIN, MARGIN + 60.0, geo["legend"], LEGEND_FONT, MUTED_RGB,
              va="top", font=geo["font"], clip_width=geo["width"] - 2 * MARGIN)


def plot_bands(dens, out, baseline="both", lanes=None, cols=2, width=1440, *, groups=None):
    """Render dens.json as local band-peak traces → (width, height).

    ``groups=None`` preserves the lane view: one tile per selected lane, all of that lane's
    bands.  ``groups`` is an ordered sequence of ``(english_name, members)`` and overlays the
    selected bands in one tile per group.  A member is ``(lane, band_or_None)`` or
    ``(lane, band_or_None, concentration)``; concentrations must be nonnegative finite
    numbers given for every member of a group or for none of them.  Numeric concentrations
    use Matplotlib's actual ``coolwarm`` colour map normalized within that group and shown
    in the compact table's ``conc`` column; groups without values use member order and make
    no numerical scale claim.  Group overlays keep the traces, peak dots, same-colour
    integration endpoint ticks and AUC table, but no area fill; individual lane tiles keep
    the signed area shading.  ``baseline='both'`` renders a paired flank/global tile;
    ``--cols`` counts tiles, not sub-panels.

    The returned tuple is the actual PNG size.  Output validation and the non-English lane
    warning follow the module docstring; no report file is read or written.
    """
    Figure, FigureCanvasAgg = _load_mpl()
    target = _check_out(out)
    geo = band_layout(dens, baseline=baseline, lanes=lanes, cols=cols, width=width,
                      groups=groups)
    _warn_lane_labels(geo["tiles"])
    fig = Figure(figsize=(geo["width"] / float(DPI), geo["height"] / float(DPI)), dpi=DPI)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor("white")
    for tile in geo["tiles"]:
        _draw_band_header(fig, geo, tile)
        for p_idx, plot in enumerate(tile["plots"]):
            _draw_band_plot(fig, geo, tile, plot,
                            show_y_label=(p_idx == 0),
                            show_x_label=(p_idx == len(tile["plots"]) - 1))
        _draw_band_legend(fig, geo, tile, baseline)
    w, h = _save_png(fig, geo["width"], geo["height"], target)
    fig.clear()
    return w, h


# ---------------------------------------------------------------- plot-curve (spec §4.5)

CURVE_KIND = getattr(densitometry, "CURVE_KIND", "ep-craft-densitometry-curve")
QUANT_KIND = getattr(densitometry, "QUANT_KIND", "ep-craft-densitometry-quant")
CURVE_SUFFIX = getattr(densitometry, "CURVE_SUFFIX", ".curve.json")


def _trim_num(text):
    """`0.400` → `0.4`, `6.00` → `6`, `-0.0` → `0`."""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _eqnum(value, digits=1):
    """A value with `digits` decimals, trailing zeros trimmed."""
    return _trim_num(("%." + str(int(digits)) + "f") % float(value))


def _tick_digits(step):
    """Decimals a tick label needs for `step`."""
    if step <= 0:
        return 0
    return max(0, min(6, 2 - int(math.floor(math.log10(step)))))


def _check_curve(curve):
    """Validate what the plot reads of a curve.json (§4.5) → (fit, fn, equation, r2)."""
    if not isinstance(curve, dict) or curve.get("kind") != CURVE_KIND:
        raise RuntimeError("不是 %s 文件（kind=%r）"
                           % (CURVE_KIND, curve.get("kind") if isinstance(curve, dict) else None))
    fit = curve.get("fit")
    if fit not in ("linear", "power"):
        raise RuntimeError("curve 的 fit 只能是 linear 或 power（收到 %r）" % (fit,))
    fp = curve.get("fit_params")
    if not isinstance(fp, dict):
        raise RuntimeError("curve 缺少 fit_params（不是 curve.json？）")
    try:
        if fit == "linear":
            slope = float(fp["slope"])
            intercept = float(fp["intercept"])

            def fn(volume):
                return intercept + slope * float(volume)

            if intercept < 0.0:
                eqn = "mass = %s · area - %s" % (_trim_num("%.2f" % slope),
                                                _trim_num("%.2f" % -intercept))
            else:
                eqn = "mass = %s + %s · area" % (_trim_num("%.2f" % intercept),
                                                _trim_num("%.2f" % slope))
        else:
            scale = float(fp["scale"])
            exponent = float(fp["exponent"])

            def fn(volume):
                volume = float(volume)
                return scale * (volume ** exponent) if volume > 0.0 else 0.0

            eqn = "mass = %s · area^%s" % (_trim_num("%.2f" % scale),
                                           _trim_num("%.2f" % exponent))
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("curve 的 fit_params 与 fit=%r 不匹配（%r）" % (fit, fp))
    try:
        r2 = None if fp.get("r2") is None else float(fp["r2"])
    except (TypeError, ValueError):
        raise RuntimeError("curve 的 fit_params.r2 不是数字（收到 %r）" % (fp.get("r2"),))
    points = curve.get("points")
    if not isinstance(points, list):
        raise RuntimeError("curve 里没有 points 列表（不是 curve.json？）")
    if not points:
        raise RuntimeError("curve 里没有标准点（先跑 curve）")
    for point in points:
        if not isinstance(point, dict):
            raise RuntimeError("curve 的 points 里有不是对象的条目")
        for key in ("mass", "volume"):
            try:
                float(point[key])
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("curve 的标准点缺少数字 %s（收到 %r）"
                                   % (key, point.get(key) if isinstance(point, dict) else None))
        sd = point.get("sd")
        if sd is not None:
            try:
                float(sd)
            except (TypeError, ValueError):
                raise RuntimeError("curve 的标准点的 sd 不是数字（收到 %r）" % (sd,))
    return fit, fn, eqn, r2


def _quant_lanes(quant):
    """Validate a quant.json (§4.6) for the sample markers → [(lane dict, [band dict])]."""
    if not isinstance(quant, dict) or quant.get("kind") != QUANT_KIND:
        raise RuntimeError("不是 %s 文件（kind=%r）"
                           % (QUANT_KIND, quant.get("kind") if isinstance(quant, dict) else None))
    lanes = quant.get("lanes")
    if not isinstance(lanes, list):
        raise RuntimeError("quant 里没有 lanes 列表（不是 quant.json？）")
    out = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise RuntimeError("quant 的 lanes 里有不是对象的条目")
        try:
            no = int(lane["lane"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("quant 的泳道条目缺少整数 lane（收到 %r）" % (lane.get("lane"),))
        bands = []
        for band in lane.get("bands") or []:
            if not isinstance(band, dict):
                raise RuntimeError("quant 的泳道 %d 的 bands 里有不是对象的条目" % no)
            try:
                area = float(band["area"])
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("quant 的泳道 %d 有一条带缺少数字 area（收到 %r）"
                                   % (no, band.get("area")))
            mass = band.get("mass")
            if mass is not None:
                try:
                    mass = float(mass)
                except (TypeError, ValueError):
                    raise RuntimeError("quant 的泳道 %d 有一条带的 mass 不是数字（收到 %r）"
                                       % (no, mass))
            out_band = bool(band.get("out_of_range")) or mass is None
            bands.append({"index": band.get("index"), "area": area, "mass": mass,
                          "out": out_band})
        out.append((lane, bands))
    return out


def _used_range(curve, rng):
    """(volume_min, volume_max) of the used standard points: the curve's range, else derived."""
    try:
        vmin = float(rng["volume_min"])
        vmax = float(rng["volume_max"])
        if vmax > vmin:
            return vmin, vmax
    except (KeyError, TypeError, ValueError):
        pass
    used = [float(p["volume"]) for p in curve["points"] if p.get("used", True) is not False]
    if not used:
        raise RuntimeError("curve 里没有可用的标准点（--drop 是不是把所有点都弃用了？）")
    lo, hi = min(used), max(used)
    if hi <= lo:
        return lo - 0.5, lo + 0.5
    return lo, hi


def _nice_step(span, target):
    """A 1 / 2 / 2.5 / 5 / 10 step giving about `target` ticks over `span`."""
    if span <= 0:
        return 0.0
    raw = float(span) / max(1, int(target))
    mag = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if mult * mag >= raw * 0.999:
            return mult * mag
    return 10.0 * mag


def _ticks(lo, hi, target=6):
    """Tick values from `lo` to `hi` with a nice step."""
    step = _nice_step(hi - lo, target)
    if step <= 0:
        return [float(lo), float(hi)]
    first = math.ceil(lo / step - 1e-9) * step
    values, value = [], first
    while value <= hi + step * 0.5 and len(values) < 40:
        values.append(round(value, 6))
        value += step
    return values


def _boxes_touch(a, b, pad=3.0):
    """Do two label boxes overlap (with a little breathing room)?"""
    return (a[2] + pad > b[0] and b[2] + pad > a[0]
            and a[3] + pad > b[1] and b[3] + pad > a[1])


def _place_label(text, size, ax, ay, boxes, font, plot_box, order=(0, 1, 2, 3, 4, 5),
                 skip=None):
    """First free slot for a label around (ax, ay) → (x, y, align, box)."""
    width = _text_width(font, int(size), text)
    height = int(size)
    x0, y0, x1, y1 = plot_box
    slots = ((ax + POINT_R + 4.0, ay - 9.0, "left"),
             (ax - POINT_R - 4.0, ay - 9.0, "right"),
             (ax, ay - height - 11.0, "center"),
             (ax, ay + height + 7.0, "center"),
             (ax + POINT_R + 4.0, ay + height + 7.0, "left"),
             (ax - POINT_R - 4.0, ay + height + 7.0, "right"))
    first, fallback = None, None
    for k in order:
        tx, ty, align = slots[k]
        left = tx if align == "left" else (tx - width if align == "right" else tx - width / 2.0)
        left = min(max(left, x0 + 2.0), max(x0 + 2.0, x1 - 2.0 - width))
        box = (left, ty - height * 0.8, left + width, ty + height * 0.25)
        if box[1] < y0 + 2.0 or box[3] > y1 - 2.0:
            continue
        if fallback is None:
            fallback = (left + (width / 2.0 if align == "center" else
                                (width if align == "right" else 0.0)), ty, align, box)
        if skip is not None and _boxes_touch(box, skip):
            continue
        if not any(_boxes_touch(box, other) for other in boxes):
            return (left + (width / 2.0 if align == "center" else
                            (width if align == "right" else 0.0)), ty, align, box)
        if first is None:
            first = (left + (width / 2.0 if align == "center" else
                             (width if align == "right" else 0.0)), ty, align, box)
    if first is not None:
        return first
    if fallback is not None:
        return fallback
    left = min(max(ax + POINT_R + 4.0, x0 + 2.0), max(x0 + 2.0, x1 - 2.0 - width))
    return (left, ay - 9.0, "left", (left, ay - height * 0.8, left + width, ay + height * 0.25))


def _bar_extent(center, half_px, box, horizontal):
    """The pixel span a replicate bar would cover, or None when it is too short to draw."""
    if half_px <= ERR_BAR_MIN_PX:
        return None
    lo_box, hi_box = ((box[0] + 2.0, box[2] - 2.0) if horizontal
                      else (box[1] + 2.0, box[3] - 2.0))
    lo, hi = max(center - half_px, lo_box), min(center + half_px, hi_box)
    if hi - lo <= 1.0:
        return None
    return lo, hi


def _bar_half(axes, sd):
    """Pixel half-length of a replicate bar for `sd`, on the axis ERROR_BAR_AXIS names."""
    sd = abs(float(sd))
    if ERROR_BAR_AXIS == "x":
        return abs(axes.x(sd) - axes.x(0.0))
    return abs(axes.y(sd) - axes.y(0.0))


def _has_error_bar(axes, point, box):
    """True when a replicate bar would actually be drawn for this point."""
    if not point["used"] or point.get("n") in (None, 1) or point.get("sd") is None:
        return False
    horizontal = ERROR_BAR_AXIS == "x"
    half = _bar_half(axes, point["sd"])
    return _bar_extent(point["x"] if horizontal else point["y"], half, box, horizontal) is not None


def curve_layout(curve, quant=None, width=1020):
    """Geometry of the plot-curve canvas — shared by `plot_curve` and its selftests."""
    fit, fn, eqn, r2 = _check_curve(curve)
    try:
        w = int(width)
    except (TypeError, ValueError):
        raise ValueError("width 必须是整数（收到 %r）" % (width,))
    if w < 600:
        raise ValueError("width 必须 ≥ 600 像素（收到 %d）" % w)
    quants = _quant_lanes(quant) if quant is not None else []
    rng = curve.get("range") if isinstance(curve.get("range"), dict) else {}
    vmin, vmax = _used_range(curve, rng)
    span = vmax - vmin
    ext_lo = vmin - 0.10 * span
    ext_hi = vmax + 0.10 * span

    points = []
    for point in curve["points"]:
        used = point.get("used", True) is not False
        points.append({"point": point, "used": used,
                       "volume": float(point["volume"]), "mass": float(point["mass"]),
                       "n": point.get("n"),
                       "sd": None if point.get("sd") is None else float(point["sd"])})
    dropped = [p for p in points if not p["used"]]

    residuals = []
    raw = curve.get("residuals")
    if isinstance(raw, list):
        for res in raw:
            if not isinstance(res, dict):
                raise RuntimeError("curve 的 residuals 里有不是对象的条目")
            try:
                residuals.append({"volume": float(res["volume"]),
                                  "rel_pct": float(res["rel_pct"])})
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("curve 的残差缺少数字 volume / rel_pct（收到 %r）" % (res,))

    volumes = [p["volume"] for p in points] + [b["area"] for _ln, bands in quants
                                               for b in bands]
    masses = [p["mass"] for p in points]
    try:
        y_hi = float(rng["mass_max"])
    except (KeyError, TypeError, ValueError):
        y_hi = max(masses) if masses else 0.0
    x_max = max(max([vmax] + volumes) * 1.08, ext_hi)
    y_max = max([y_hi] + masses + [0.0]) * 1.12
    rel_hi = max([15.0, 1.2 * max([abs(r["rel_pct"]) for r in residuals] + [0.0])])
    if x_max <= 0.0:
        x_max = 1.0
    if y_max <= 0.0:
        y_max = 1.0

    height = max(round(896 + (w - 1020) * 0.56), 670)
    unit = curve.get("unit") if isinstance(curve.get("unit"), str) and curve.get("unit") else "ug"
    baseline = curve.get("baseline") if isinstance(curve.get("baseline"), str) else "flank"
    n_used = sum(1 for p in points if p["used"])
    n_drop = len(dropped)
    font = _font()
    unit_txt = _unit_text(unit)
    base_txt = _safe_ascii(baseline, 18)
    concentration = unit_txt.lower().endswith("/ml")
    quantity = "concentration" if concentration else "mass"
    eqn = re.sub(r"^mass\s*=\s*", quantity + " = ", eqn)
    details = ["%s fit" % fit.capitalize(),
               "r² = %s" % ("–" if r2 is None else "%.4f" % r2),
               "%d %s%s" % (n_used, "point" if n_used == 1 else "points",
                             (" (%d dropped)" % n_drop) if n_drop else ""),
               "%s baseline" % base_txt.capitalize()]
    try:
        m_lo = _trim_num("%.3f" % float(rng["mass_min"]))
        m_hi = _trim_num("%.3f" % float(rng["mass_max"]))
        span_txt = m_lo if m_lo == m_hi else "%s–%s" % (m_lo, m_hi)
        details.append("Standards %s %s" % (span_txt, unit_txt))
    except (KeyError, TypeError, ValueError):
        details.append(unit_txt)

    lines = [eqn]
    available = w - LEFT_PAD - CURVE_RIGHT_PAD
    row = details[0]
    for detail in details[1:]:
        candidate = row + "  ·  " + detail
        if _text_width(font, ANN_FONT, candidate) <= available:
            row = candidate
        else:
            lines.append(row)
            row = detail
    lines.append(row)

    top_h = TOP_PAD + CURVE_TITLE_FONT + 22
    footer_y = height - 22 - ANN_STEP * len(lines)
    bottom_h = height - footer_y + 76
    gap = 30
    resid_h = max(96, int(round(0.16 * height)))
    main_h = height - top_h - bottom_h - gap - resid_h
    if main_h < 80:
        raise ValueError("width %d 太小，画不下主体坐标系（试 ≥ 800）" % w)

    main_box = (float(LEFT_PAD), float(top_h), float(w - CURVE_RIGHT_PAD), float(top_h + main_h))
    resid_box = (float(LEFT_PAD), float(top_h + main_h + gap),
                 float(w - CURVE_RIGHT_PAD), float(top_h + main_h + gap + resid_h))
    axes = CurveAxes(main_box, resid_box, (0.0, float(x_max)), (0.0, float(y_max)),
                     (-float(rel_hi), float(rel_hi)))

    out_points = [dict(p, x=axes.x(p["volume"]), y=axes.y(p["mass"])) for p in points]
    out_res = [dict(r, x=axes.x(r["volume"]), y=axes.yr(r["rel_pct"])) for r in residuals]

    guide_gap = abs(axes.yr(0.0) - axes.yr(10.0))
    guide_note = None
    guide_note_at = None
    if guide_gap < GUIDE_LABEL_MIN:
        guide_note = "±10% guides (dashed)"
        note_x = resid_box[2] - 6.0
        # Top-anchored in the reserved gutter, just below the main axes.
        note_y = main_box[3] + 4.0
        guide_note_at = (note_x, note_y, "right")

    mx0, my0, mx1, my1 = main_box
    samples = []
    for lane, bands in quants:
        no = int(lane["lane"])
        many = len(bands) > 1
        for band in bands:
            x = min(max(axes.x(band["area"]), mx0 + SAMPLE_HALF + 4.0), mx1 - SAMPLE_HALF - 4.0)
            idx = band["index"]
            if band["out"]:
                y = my1 - SAMPLE_HALF - 1.0
                label = ("L%d.%s out" % (no, idx) if idx is not None else "L%d out" % no)
            else:
                y = min(max(axes.y(band["mass"]), my0 + SAMPLE_HALF + 4.0), my1 - SAMPLE_HALF - 4.0)
                label = ("L%d.%s" % (no, idx) if many and idx is not None else "L%d" % no)
            samples.append({"lane": no, "band": idx, "area": band["area"],
                            "mass": band["mass"], "out": band["out"], "label": label,
                            "x": x, "y": y})

    title = "Standard curve"
    bars = any(_has_error_bar(axes, p, main_box) for p in out_points)
    # Plain metadata key for callers/tests: the visible key is the native graphical legend
    # built from the real artists in _draw_curve_main (never this prose line).
    legend_parts = ["standard"]
    if dropped:
        legend_parts.append("dropped")
    if any(not s["out"] for s in samples):
        legend_parts.append("sample")
    if any(s["out"] for s in samples):
        legend_parts.append("out of range")
    if bars:
        legend_parts.append("replicate area SD")
    legend = " · ".join(legend_parts)
    return {"width": w, "height": height, "axes": axes, "main_box": main_box,
            "resid_box": resid_box, "x_range": (0.0, float(x_max)),
            "y_range": (0.0, float(y_max)), "rel_range": (-float(rel_hi), float(rel_hi)),
            "fitted": {"volume_min": vmin, "volume_max": vmax,
                       "extension_min": ext_lo, "extension_max": ext_hi,
                       "drawn_min": max(ext_lo, 0.0), "drawn_max": ext_hi},
            "fit": fit, "eval": fn, "equation": eqn,
            "points": out_points, "dropped": [p for p in out_points if not p["used"]],
            "residuals": out_res, "samples": samples,
            "gap": float(gap), "footer_y": footer_y,
            "guide_note": guide_note, "guide_note_at": guide_note_at,
            "annotation": "\n".join(lines), "annotation_lines": lines,
            "title": title, "legend": legend,
            "x_label": "Area (gray·cm)",
            "y_label": "%s (%s)" % ("Concentration" if concentration else "Mass", unit_txt),
            "resid_label": "Residual (%)",
            "font": font}


# ---------------------------------------------------------------- plot-curve: drawing

def _style_curve_axes(ax, font):
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color(_mcolor(CURVE_FRAME_RGB))
        spine.set_linewidth(0.8)
    ax.set_facecolor("white")
    ax.grid(True, axis="both", color="#dce5e9", linewidth=0.65)
    ax.set_axisbelow(True)
    ax.tick_params(colors=_mcolor(MUTED_RGB), width=0.7, length=3.0,
                   labelsize=10.0, pad=3.0)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(_font_prop(CURVE_TICK_FONT, font))
        label.set_color(_mcolor(MUTED_RGB))


def _box_overlap(a, b):
    """Intersection area of two top-left-origin pixel boxes."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0.0 and h > 0.0 else 0.0


def _add_curve_legend(fig, ax, geo, handles, labels, boxes):
    """Add the native marker legend, choosing the corner with the least ink collision.

    Handles are the real plotted artists (or a horizontal bar proxy for the SD bars), so the
    key cannot drift from the picture.  The old text line ``filled = ...`` is not drawn.
    """
    width, height = geo["width"], geo["height"]
    leg = ax.legend(handles, labels, loc="upper left",
                    bbox_to_anchor=(0.0, 1.0), bbox_transform=fig.transFigure,
                    borderaxespad=0.0, borderpad=0.34, labelspacing=0.22,
                    handlelength=1.25, handletextpad=0.55, markerscale=1.0,
                    frameon=True, facecolor="white", edgecolor=_mcolor((165, 165, 165)),
                    framealpha=0.95,
                    prop=_font_prop(CURVE_LEGEND_FONT, geo["font"]))
    leg.set_zorder(20)
    leg.set_clip_on(False)
    for text in leg.get_texts():
        text.set_color(_mcolor(TEXT_RGB))
    frame = leg.get_frame()
    frame.set_linewidth(0.7)
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    except Exception:
        renderer = None
    if renderer is None:
        return leg
    bb = leg.get_window_extent(renderer)
    lw, lh = float(bb.width), float(bb.height)
    # Display pixels and the layout's pixels are both at DPI, so the measured key size can
    # be placed directly in main_box coordinates.  Four corners plus the two side centres.
    main = geo["main_box"]
    pad = 6.0
    candidates = [
        ("upper left", main[0] + pad, main[1] + pad),
        ("upper right", main[2] - pad, main[1] + pad),
        ("lower left", main[0] + pad, main[3] - pad),
        ("lower right", main[2] - pad, main[3] - pad),
        ("center left", main[0] + pad, (main[1] + main[3]) / 2.0),
        ("center right", main[2] - pad, (main[1] + main[3]) / 2.0),
    ]
    best = None
    for loc, anchor_x, anchor_y in candidates:
        if loc.startswith("upper"):
            top = anchor_y
        elif loc.startswith("lower"):
            top = anchor_y - lh
        else:
            top = anchor_y - lh / 2.0
        if loc.endswith("left"):
            left = anchor_x
        else:
            left = anchor_x - lw
        rect = (left, top, left + lw, top + lh)
        score = 0.0
        for box in boxes:
            score += _box_overlap(rect, box)
        # Keep the key inside the plotting rectangle; a little outside is worse than a
        # collision with a label, but still preferable to clipping it at the canvas edge.
        inside_w = max(0.0, min(main[2], rect[2]) - max(main[0], rect[0]))
        inside_h = max(0.0, min(main[3], rect[3]) - max(main[1], rect[1]))
        score += 8.0 * (lw * lh - inside_w * inside_h)
        order = ["upper left", "upper right", "lower left", "lower right",
                 "center left", "center right"].index(loc)
        score += order * 0.01
        if best is None or score < best[0]:
            best = (score, loc, anchor_x, anchor_y)
    _score, loc, anchor_x, anchor_y = best
    leg.set_loc(loc)
    leg.set_bbox_to_anchor((anchor_x / float(width), 1.0 - anchor_y / float(height)),
                           transform=fig.transFigure)
    return leg


def _warn_quant_lane_labels(quants):
    """One LaneLabelWarning per non-English quant lane label in this render."""
    seen = set()
    for lane, bands in quants:
        if not bands:
            continue
        no = int(lane.get("lane"))
        if no in seen:
            continue
        seen.add(no)
        label = lane.get("label")
        if isinstance(label, str) and label.strip() and not _is_english_label(label):
            warnings.warn_explicit(
                "lane %d label %r is not English; drawn as 'L%d'" % (no, label, no),
                LaneLabelWarning, __file__, 0, registry=None)


def _draw_curve_text(fig, geo):
    """Set the title above the data and the calibration details below the shared axis."""
    font = geo["font"]
    room = geo["width"] - LEFT_PAD - CURVE_RIGHT_PAD
    _fig_text(fig, LEFT_PAD, TOP_PAD, geo["title"], CURVE_TITLE_FONT, TEXT_RGB,
              va="top", font=font, clip_width=room)
    for i, line in enumerate(geo["annotation_lines"]):
        color = TEXT_RGB if i == 0 else MUTED_RGB
        _fig_text(fig, LEFT_PAD, geo["footer_y"] + i * ANN_STEP, line, ANN_FONT, color,
                  va="top", font=font, clip_width=room)


def _draw_curve_fit(ax, geo):
    """The fitted line: solid over the standard range, dashed ±10% extensions."""
    drawn = geo["fitted"]
    segments = (
        (drawn["volume_min"], drawn["volume_max"], "-", 1.65),
        (drawn["drawn_min"], drawn["volume_min"], "--", 1.2),
        (drawn["volume_max"], drawn["drawn_max"], "--", 1.2),
    )
    steps = 2 if geo["fit"] == "linear" else 40
    for start, stop, style, width in segments:
        if stop <= start:
            continue
        xs = [start + (stop - start) * k / (steps - 1) for k in range(steps)]
        # Let the axes clip the path. Clamping its vertices would distort the fit.
        ax.plot(xs, [geo["eval"](x) for x in xs], color="#11776f",
                linestyle=style, linewidth=width, solid_capstyle="round", zorder=3)


def _draw_curve_main(fig, geo):
    """Main axes: frame, ticks, fitted line, replicate bars, points, samples and labels."""
    width, height = geo["width"], geo["height"]
    font = geo["font"]
    ax = fig.add_axes(_fig_rect(geo["main_box"], width, height))
    ax.set_xlim(geo["x_range"])
    ax.set_ylim(geo["y_range"])
    ax.set_autoscale_on(False)
    _style_curve_axes(ax, font)
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    step = _nice_step(geo["y_range"][1], 6)
    digits = _tick_digits(step)
    yticks = sorted(set([0.0] + [v for v in _ticks(0.0, geo["y_range"][1], 6)
                                 if 0.0 <= v <= geo["y_range"][1]]))
    ax.set_yticks(yticks)
    ax.set_yticklabels([_eqnum(v, digits) for v in yticks])
    for label in ax.get_yticklabels():
        label.set_fontproperties(_font_prop(CURVE_TICK_FONT, font))
        label.set_color(_mcolor(MUTED_RGB))
    ax.set_ylabel(geo["y_label"], color=_mcolor(TEXT_RGB),
                  fontproperties=_font_prop(AXIS_FONT, font), labelpad=8)
    _draw_curve_fit(ax, geo)

    # replicate area bars: horizontal, clipped to the axes
    has_bars = False
    for p in geo["points"]:
        if not _has_error_bar(geo["axes"], p, geo["main_box"]):
            continue
        span = _bar_extent(p["x"], _bar_half(geo["axes"], p["sd"]), geo["main_box"], True)
        if span is None:
            continue
        lo = max(0.0, p["volume"] - geo["axes"].x_value(span[0]))
        hi = max(0.0, geo["axes"].x_value(span[1]) - p["volume"])
        ax.errorbar([p["volume"]], [p["mass"]], xerr=[[lo], [hi]], fmt="none",
                    ecolor=_mcolor(ERR_RGB), elinewidth=1.4, capsize=3.6,
                    capthick=1.4, zorder=2)
        has_bars = True

    # Keep one real artist per marker category for the native legend.  The points are drawn
    # marker-only (no connecting line), so their legend handles match the picture exactly.
    used_handle = None
    dropped_handle = None
    for p in geo["points"]:
        if p["used"]:
            handle, = ax.plot([p["volume"]], [p["mass"]], marker="o",
                              markersize=POINT_R * 2 * 72 / DPI, linestyle="none",
                              color=_mcolor(POINT_RGB),
                              markeredgecolor=_mcolor(POINT_STROKE_RGB),
                              markeredgewidth=0.9, zorder=5)
            used_handle = used_handle or handle
        else:
            handle, = ax.plot([p["volume"]], [p["mass"]], marker="o",
                              markersize=POINT_R * 2 * 72 / DPI, linestyle="none",
                              markerfacecolor="white", markeredgecolor=_mcolor(DROP_RGB),
                              markeredgewidth=1.8, zorder=5)
            dropped_handle = dropped_handle or handle

    sample_handle = None
    oor_handle = None
    for s in geo["samples"]:
        sx = geo["axes"].x_value(s["x"])
        sy = geo["axes"].y_value(s["y"])
        if s["out"]:
            handle, = ax.plot([sx], [sy], marker="s",
                              markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                              markerfacecolor="white", markeredgecolor=_mcolor(OOR_RGB),
                              markeredgewidth=1.8, zorder=5)
            oor_handle = oor_handle or handle
        else:
            handle, = ax.plot([sx], [sy], marker="s",
                              markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                              color=_mcolor(SAMPLE_RGB),
                              markeredgecolor=_mcolor(SAMPLE_STROKE_RGB),
                              markeredgewidth=0.8, zorder=5)
            sample_handle = sample_handle or handle

    # label placement stays in pixel space, exactly as the layout computed it
    point_boxes = [(p["x"] - POINT_R - 1.0, p["y"] - POINT_R - 1.0,
                    p["x"] + POINT_R + 1.0, p["y"] + POINT_R + 1.0) for p in geo["points"]]
    sample_boxes = [(s["x"] - SAMPLE_HALF - 1.0, s["y"] - SAMPLE_HALF - 1.0,
                     s["x"] + SAMPLE_HALF + 1.0, s["y"] + SAMPLE_HALF + 1.0)
                    for s in geo["samples"]]
    line_boxes = []
    span_min, span_max = geo["fitted"]["drawn_min"], geo["fitted"]["drawn_max"]
    for k in range(61):
        v = span_min + (span_max - span_min) * (k / 60.0)
        lx = min(max(geo["axes"].x(v), geo["main_box"][0] + 1.0), geo["main_box"][2] - 1.0)
        ly = min(max(geo["axes"].y(geo["eval"](v)), geo["main_box"][1] + 1.0),
                 geo["main_box"][3] - 1.0)
        line_boxes.append((lx - 2.5, ly - 2.5, lx + 2.5, ly + 2.5))
    boxes = list(point_boxes) + list(sample_boxes) + line_boxes
    if geo["guide_note_at"] is not None:
        nx, ny, _na = geo["guide_note_at"]
        nw = _text_width(font, CURVE_TICK_FONT, geo["guide_note"])
        boxes.append((nx - nw, ny, nx, ny + CURVE_TICK_FONT))

    for p, own in zip(geo["points"], point_boxes):
        if p["used"]:
            continue
        tx, ty, align, box = _place_label("dropped", MARK_FONT, p["x"], p["y"], boxes,
                                          font, geo["main_box"], skip=own)
        _fig_text(fig, tx, ty, "dropped", MARK_FONT, DROP_RGB, ha=align, font=font)
        boxes.append(box)
    for s, own in zip(geo["samples"], sample_boxes):
        colour = OOR_RGB if s["out"] else SAMPLE_RGB
        order = (3, 2, 0, 1, 5, 4) if s["out"] else (0, 1, 2, 3, 4, 5)
        plot_box = geo["main_box"]
        if s["out"]:
            _x0, _y0, _x1, y1 = geo["main_box"]
            plot_box = (geo["main_box"][0], geo["main_box"][1], geo["main_box"][2],
                        y1 + geo["gap"])
        tx, ty, align, box = _place_label(s["label"], MARK_FONT, s["x"], s["y"], boxes,
                                          font, plot_box, order=order, skip=own)
        _fig_text(fig, tx, ty, s["label"], MARK_FONT, colour, ha=align, font=font)
        boxes.append(box)

    # Native graphical key: real marker artists (or a horizontal bar glyph for SD bars),
    # placed after every label so collision checking sees the finished plot.
    handles, labels = [], []
    for handle, label in ((used_handle, "standard"), (dropped_handle, "dropped"),
                          (sample_handle, "sample"), (oor_handle, "out of range")):
        if handle is not None:
            handles.append(handle)
            labels.append(label)
    if has_bars:
        from matplotlib.lines import Line2D
        handles.append(Line2D([], [], color=_mcolor(ERR_RGB), marker="_", linestyle="none",
                              markersize=12.0, markeredgewidth=1.7))
        labels.append("replicate area SD")
    if handles:
        _add_curve_legend(fig, ax, geo, handles, labels, boxes)

    # Fix the shared area axis after every artist: set_xticks()/autoscale can otherwise
    # expand one view beyond geo["x_range"], leaving the main plot and the residual strip
    # with visibly different spans (acceptance: 0..11.88 vs 0..12).
    ax.set_xlim(geo["x_range"])
    return ax


def _draw_curve_resid(fig, geo, main_ax):
    """Residual strip: frame, zero line, ±10 % guides and one marker per residual."""
    width, height = geo["width"], geo["height"]
    font = geo["font"]
    ax = fig.add_axes(_fig_rect(geo["resid_box"], width, height), sharex=main_ax)
    ax.set_xlim(geo["x_range"])
    ax.set_ylim(geo["rel_range"])
    ax.set_autoscale_on(False)
    _style_curve_axes(ax, font)
    x0, y0, x1, y1 = geo["resid_box"]
    ax.axhspan(-10.0, 10.0, color=_mcolor(GUIDE_RGB), alpha=0.07, linewidth=0, zorder=0)
    ax.axhline(0.0, color=_mcolor(ZERO_RGB), linewidth=1.6, zorder=2)
    for value in (-10.0, 10.0):
        ax.axhline(value, color=_mcolor(GUIDE_RGB), linewidth=0.8,
                   linestyle=(0, (5, 5)), zorder=1)
    if geo["guide_note"] is None:
        ax.set_yticks([-10.0, 0.0, 10.0])
        ax.set_yticklabels(["-10%", "0", "+10%"])
    else:
        ax.set_yticks([0.0])
        ax.set_yticklabels(["0"])
        note_x, note_y, note_ha = geo["guide_note_at"]
        _fig_text(fig, note_x, note_y, geo["guide_note"], CURVE_TICK_FONT, MUTED_RGB,
                  ha=note_ha, va="top", font=font)
    for label in ax.get_yticklabels():
        label.set_fontproperties(_font_prop(CURVE_TICK_FONT, font))
        label.set_color(_mcolor(MUTED_RGB))
    xticks = [v for v in _ticks(geo["x_range"][0], geo["x_range"][1], 6)
              if geo["x_range"][0] - 1e-9 <= v <= geo["x_range"][1] + 1e-9]
    ax.set_xticks(xticks)
    step = _nice_step(geo["x_range"][1], 6)
    digits = _tick_digits(step)
    ax.set_xticklabels([_eqnum(v, digits) for v in xticks])
    for label in ax.get_xticklabels():
        label.set_fontproperties(_font_prop(CURVE_TICK_FONT, font))
        label.set_color(_mcolor(MUTED_RGB))
    ax.set_xlabel(geo["x_label"], color=_mcolor(TEXT_RGB),
                  fontproperties=_font_prop(AXIS_FONT, font), labelpad=6)
    ax.set_ylabel(geo["resid_label"], color=_mcolor(TEXT_RGB),
                  fontproperties=_font_prop(CURVE_SUB_FONT, font), labelpad=6)
    zero = 0.0
    for res in geo["residuals"]:
        x = geo["axes"].x_value(res["x"])
        y = geo["axes"].resid.y_value(res["y"])
        ax.vlines([x], zero, y, color=_mcolor(ERR_RGB), linewidth=0.9, zorder=2)
        ax.plot([x], [y], marker="o", markersize=RESID_R * 2 * 72 / DPI,
                color=_mcolor(LINE_RGB), markeredgecolor="none", zorder=3)
    # Same final x limits as the main axes, after the residual artists and tick labels.
    ax.set_xlim(geo["x_range"])


def plot_curve(curve, out, quant=None, width=1020):
    """Render curve.json — and quant.json's sample markers when given — as a PNG (§4.7).

    Returns the actual pixel size written.  Missing matplotlib becomes one RuntimeError with
    the ``epcraft[plot]`` install hint, and no output directory is ever created implicitly.
    """
    Figure, FigureCanvasAgg = _load_mpl()
    target = _check_out(out)
    geo = curve_layout(curve, quant=quant, width=width)
    if quant is not None:
        _warn_quant_lane_labels(_quant_lanes(quant))
    fig = Figure(figsize=(geo["width"] / float(DPI), geo["height"] / float(DPI)), dpi=DPI)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor("white")
    _draw_curve_text(fig, geo)
    main_ax = _draw_curve_main(fig, geo)
    _draw_curve_resid(fig, geo, main_ax)
    # Both axes share the same left edge, so their vertical labels share a column too.
    label_x = (30 - LEFT_PAD) / (geo["width"] - LEFT_PAD - CURVE_RIGHT_PAD)
    for ax in fig.axes:
        ax.yaxis.set_label_coords(label_x, 0.5)
    w, h = _save_png(fig, geo["width"], geo["height"], target)
    fig.clear()
    return w, h
