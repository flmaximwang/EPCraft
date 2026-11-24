"""
render — shared rendering primitives for the densitometry plots (spec §4.7).

The two renderers (``epcraft.plotting.bands`` and ``epcraft.plotting.curve``) draw with
matplotlib's Agg backend directly: no pyplot, no GUI, no ImageMagick/MVG.  This module owns
everything both pictures need to agree on:

* the palette and type-size table, verbatim from the original ``dens_plot`` module;
* the value → pixel mappings ``Mapping`` / ``TraceAxes`` / ``CurveAxes``;
* the number and tick helpers ``_trim_num`` / ``_eqnum`` / ``_nice_step`` / ``_ticks``;
* text measurement and clipping, the English-fallback label helpers and the RGB → 0..1
  colour conversion;
* the atomic PNG write and its ``.png`` / existing-directory output validation;
* ``LaneLabelWarning``, the public warning channel (see below).

Only ``matplotlib`` is optional.  It is imported lazily inside the helpers and inside the
public plot functions, so ``import epcraft.dens_plot`` and the non-plot CLI commands keep
working without the extra; a plot call without it raises one RuntimeError whose message
contains ``epcraft[plot]``.

``LaneLabelWarning`` is emitted exactly once per affected lane per render through
``warnings.warn_explicit`` (the normal Python warning channel, visible on stderr by default),
so a CLI user can suppress it with ``warnings.simplefilter`` and a test can record it with
``warnings.catch_warnings(record=True)``.

All figure prose is English.  Standard unit symbols that Arial covers (µ, ·, ±, ², →) are
used; user lane labels are only drawn when they are printable ASCII, otherwise the stable
English fallback is used and the warning above is emitted.

"""

import math
import os
import re
import struct
import tempfile

from .. import text_layout

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

# Out-of-range status strip (ep-c1i).  The strip is built only when a sample cannot be
# placed on the mass axis, so a curve without one keeps the old two-axes geometry exactly.
STATUS_ROW_H = 36                  # vertical pitch of one marker/tag row inside the strip
STATUS_PAD = 6                     # top/bottom padding of the strip
STATUS_GAP = 20                    # room between the main axes and the strip
STATUS_MARK_Y = 27                 # marker centre inside a row, measured from the row top

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
    """plot-curve's value → pixel mapping: `.x(volume)`, `.y(mass)`, `.yr(rel_pct)`.

    ``status_box`` is the optional out-of-range strip between the main axes and the
    residual strip; it is None when no sample left the mass axis.
    """
    def __init__(self, main_box, resid_box, x_range, y_range, rel_range, status_box=None):
        self.main = Mapping(main_box, x_range, y_range, y_down=False)
        self.resid = Mapping(resid_box, x_range, rel_range, y_down=False)
        self.box = main_box
        self.resid_box = resid_box
        self.status_box = None if status_box is None else tuple(float(v) for v in status_box)
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


# ------------------------------------------------- shared number and tick helpers


def _trim_num(text):
    """`0.400` → `0.4`, `6.00` → `6`, `-0.0` → `0`."""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _eqnum(value, digits=1):
    """A value with `digits` decimals, trailing zeros trimmed."""
    return _trim_num(("%." + str(int(digits)) + "f") % float(value))


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
