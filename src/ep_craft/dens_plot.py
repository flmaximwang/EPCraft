#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dens_plot — MVG renderers for the densitometry chain (spec §4.7; ticket ep-bqv.2).

`plot_bands(dens, out, ...)` turns a dens.json dict into a PNG grid of one panel per lane:
gray runs left (dark, gray 0) to right (light, gray 255), `y_cm` runs downward (gel order),
the global baseline is a blue dashed line, each band's flank ramp an orange dashed segment,
and the window of the active baseline(s) is a translucent polygon between the baseline and the
gray curve — the shaded region IS the integral that `measure` reported, so a reviewer can
check the number by looking.

No math here beyond `ink = baseline - gray` and coordinate mapping: every number is read from
the dens dict (`densitometry.measure` owns the arithmetic).  The picture is drawn with
ImageMagick only (MVG script → `magick -draw @file`), so `plot-*` stays offline.

`plot_curve(curve, out, quant=None, width=1600)` draws curve.json (spec §4.5) on log-free
linear axes — standard points with their replicate bars, dropped points hollow, the fitted
model over the calibrated span plus its ±10 % dashed extension, and a residual strip under the
main axes that shares the volume axis.  With a quant dict the sample bands appear as squares,
so one picture shows what the curve predicts against what the samples actually measured.

Shared plumbing (ticket ep-bqv.4, plot-curve, builds on the same pieces):
  `_check_out(out)`            — validate an output PNG path (ruling R12) → absolute path
  `_font()`                    — text_layout.pick_font() or "Arial"
  `_render(script, w, h, out)` — write the MVG script, run magick, always delete the script
                                 (`strip=True` drops the PNG date/text chunks: reproducible bytes)
  `Script`                     — tiny MVG builder (polyline / polygon / rule / text / circle…)
  `Mapping`                    — value rectangle → pixel box (x right, y down or up)
  `Panel`                      — one lane panel: `x(gray)`, `y(y_cm)`, `.box`
  `band_layout(...)`           — the panel grid (boxes, row/column count, image size) that the
                                 renderer and the pixel selftest share, so a test computes
                                 pixel positions without duplicating layout arithmetic.
  `curve_layout(...)`          — the same contract for plot-curve: `CurveAxes` with
                                 `x(volume)` / `y(mass)` / `yr(rel_pct)`, both pixel boxes,
                                 the value ranges, the fitted span and the annotation lines.

All plot text is English ASCII (ruling R1: `ug`, `gray*cm`); user-supplied lane labels are
sanitised first, because an apostrophe or a non-ASCII character would break the MVG text
primitive or the ASCII rule.
"""
import math
import os
import tempfile

from . import densitometry
from . import magick_io
from . import text_layout

BASELINES = ("flank", "global", "both")
KIND = densitometry.KIND

# ---------------------------------------------------------------- colours and type sizes

CURVE_RGB = (32, 32, 32)           # the gray profile: a solid dark polyline
GLOBAL_RGB = (30, 90, 200)         # the global baseline: blue dashed
FLANK_RGB = (235, 130, 15)         # the flank ramp: orange dashed
GLOBAL_FILL = (30, 90, 200)        # translucent shading (spec §4.7 / §2.2)
FLANK_FILL = (240, 140, 20)
FILL_ALPHA = 0.35
GRID_RGB = (150, 150, 150)
TEXT_RGB = (20, 20, 20)
MUTED_RGB = (95, 95, 95)

TITLE_FONT = 26                    # the one-off header block in the top margin
PARAMS_FONT = 17
LEGEND_FONT = 16
HEADER_FONT = 20                   # `LANE n: label`
LABEL_FONT = 16                    # everything else (ticket: >= 16 px for labels)


# ---------------------------------------------------------------- shared plumbing (§4.7)

def _font():
    """The font file to draw with: the repo's picker, or the ImageMagick default "Arial"."""
    return text_layout.pick_font() or "Arial"


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


def _render(script, w, h, out, strip=False):
    """Draw one MVG script into `out` (a PNG path already through `_check_out`).

    `strip=True` drops the PNG metadata chunks so two renders of the same inputs are
    byte-identical (plot-curve's determinism check; plot-bands keeps the default).

    The script is written beside the output as a `*.mvg` temp file and removed in a `finally`
    block, on success and on failure alike, so no plot debris is ever left next to the PNG.
    A magick failure (bad primitive, patched binary, non-zero exit) becomes ONE RuntimeError
    line: the first line of the cause plus the output path, because the CLI must print one
    `[!] …` line and `magick_io._run`'s own message embeds multi-line stderr.
    """
    font = _font()
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix=".dens-plot-", suffix=".mvg",
                                    dir=os.path.dirname(out) or ".")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        args = [magick_io.MAGICK, "-size", "%dx%d" % (w, h), "xc:white",
                "-font", font, "-draw", "@" + path, "-depth", "8"]
        if strip:
            # `-strip` drops the date / text PNG chunks, so the same inputs render to the
            # exact same bytes (plot-curve's selftest compares two renders).
            args.append("-strip")
        args.append(out)
        magick_io._run(args)
    except RuntimeError as e:
        cause = " ".join(str(e).splitlines()[:1]).strip() or "magick 失败"
        raise RuntimeError("渲染失败（%s）：%s" % (out, cause))
    finally:
        if path is not None and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------- value → pixel mapping

class Mapping:
    """A value rectangle mapped into a pixel box: x to the right, y down (or up).

    `Mapping(box=(x0, y0, x1, y1), x_range=(x_min, x_max), y_range=(y_min, y_max), y_down=True)`
    maps a value to a pixel coordinate inside the box; y_down=True puts y_min at the top (the
    gel orientation of plot-bands), y_down=False puts y_min at the bottom (plot-curve's mass
    axis).  The box is inclusive in intent: `x(x_min)` is x0 and `x(x_max)` is x1.
    """
    def __init__(self, box, x_range, y_range, y_down=True):
        self.box = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        self.x_min, self.x_max = float(x_range[0]), float(x_range[1])
        self.y_min, self.y_max = float(y_range[0]), float(y_range[1])
        self.y_down = bool(y_down)

    def _frac(self, value, lo, hi):
        if hi == lo:
            return 0.5
        return (float(value) - lo) / (hi - lo)

    def x(self, value):
        """Value → pixel x (x_min at the left edge, x_max at the right edge)."""
        x0, _y0, x1, _y1 = self.box
        return x0 + self._frac(value, self.x_min, self.x_max) * (x1 - x0)

    def y(self, value):
        """Value → pixel y (the top edge when y_down and the value is y_min)."""
        _x0, y0, _x1, y1 = self.box
        f = self._frac(value, self.y_min, self.y_max)
        return y0 + (f if self.y_down else 1.0 - f) * (y1 - y0)


class Panel:
    """One lane panel: x(gray) 0 (dark) at the left … 255 at the right, y(y_cm) downward.

    The pixel box is where the profile is drawn (inside the cell, below the header and above
    the gray axis).  The renderer and `selftest_dens_plot` both use this object, so a test's
    pixel positions are the renderer's positions — no layout arithmetic is duplicated.
    """
    def __init__(self, box, y_min, y_max):
        self.mapping = Mapping(box, (0.0, 255.0), (y_min, y_max), y_down=True)

    @property
    def box(self):
        """(x0, y0, x1, y1) of the plot area in image pixels."""
        return self.mapping.box

    def x(self, gray):
        """Gray level (0 = dark) → pixel x."""
        return self.mapping.x(gray)

    def y(self, y_cm):
        """Depth in cm (0 = gel-face top) → pixel y."""
        return self.mapping.y(y_cm)


# ---------------------------------------------------------------- MVG script builder

def _rgb(color):
    return "rgb(%d,%d,%d)" % (int(color[0]), int(color[1]), int(color[2]))


def _rgba(color, alpha):
    return "rgba(%d,%d,%d,%.2f)" % (int(color[0]), int(color[1]), int(color[2]), alpha)


def _fill(gray_or_rgba):
    """`None` → none, an RGB triple → opaque, and `(r, g, b, alpha)` → translucent."""
    if not gray_or_rgba:
        return "none"
    if len(gray_or_rgba) == 4:
        return _rgba(gray_or_rgba[:3], gray_or_rgba[3])
    return _rgb(gray_or_rgba)


def _ascii(text, limit=64):
    """User text → printable ASCII that is safe inside an MVG single-quoted string.

    ``'`` becomes ``"`` (a single quote would end the primitive even when escaped carelessly),
    ``\\`` becomes ``/`` (backslash starts an escape), everything outside 32..126 becomes ``?``
    (ruling R1 keeps plot text ASCII), and an over-long label is truncated with `...` so it
    cannot run over the neighbouring panel.
    """
    out = []
    for ch in str(text):
        code = ord(ch)
        if ch == "'":
            out.append('"')
        elif ch == "\\":
            out.append("/")
        elif 32 <= code < 127:
            out.append(ch)
        else:
            out.append("?")
    s = "".join(out).strip()
    if len(s) > limit:
        s = s[:limit - 3].rstrip() + "..."
    return s


_WIDTH_CACHE = {}


def _text_width(font, size, text):
    """Rendered width of `text` in pixels (measured once per (font, size, text))."""
    key = (font, size, text)
    if key not in _WIDTH_CACHE:
        try:
            _WIDTH_CACHE[key] = int(text_layout.text_extent(font, size, text)[0])
        except Exception:
            _WIDTH_CACHE[key] = int(0.47 * size * len(text))
    return _WIDTH_CACHE[key]


def _estimate_width(size, text):
    """Cheap width estimate for packing decisions (no magick call)."""
    return int(0.47 * size * len(text))


class Script:
    """Tiny MVG builder: every primitive sets the fill and stroke state it needs.

    State is never left behind deliberately (`_style` always writes `fill` and `stroke`,
    and every dashed primitive resets `stroke-dasharray none`), so reordering a drawing call
    cannot silently dash the next line.  Coordinates are formatted with one decimal.
    """
    def __init__(self):
        self._lines = []

    # -- primitive state
    def _style(self, fill=None, stroke=None, width=1, dash=None):
        self._lines.append("fill %s" % _fill(fill))
        self._lines.append("stroke %s" % (_rgb(stroke) if stroke else "none"))
        if stroke is not None:
            self._lines.append("stroke-width %.1f" % float(width))
            self._lines.append("stroke-dasharray %s" % (dash or "none"))

    @staticmethod
    def _points(points):
        return " ".join("%.1f,%.1f" % (float(x), float(y)) for x, y in points)

    # -- drawing primitives
    def polyline(self, points, color, width=2, dash=None):
        """Solid (dash=None) or dashed polyline through `points`."""
        self._style(fill=None, stroke=color, width=width, dash=dash)
        self._lines.append("polyline " + self._points(points))

    def polygon(self, points, fill, alpha=FILL_ALPHA):
        """Translucent filled polygon (overlapping fills blend, as spec §4.7 requires)."""
        self._style(fill=(fill[0], fill[1], fill[2], alpha))
        self._lines.append("polygon " + self._points(points))

    def rule(self, x0, y0, x1, y1, color, width=1, dash=None):
        """One straight line (solid by default); a window edge or an axis."""
        self._style(fill=None, stroke=color, width=width, dash=dash)
        self._lines.append("line %.1f,%.1f %.1f,%.1f" % (x0, y0, x1, y1))

    def rect(self, x0, y0, x1, y1, fill=None, stroke=None, width=1):
        """Filled and/or outlined rectangle (plot-curve's legend chips / markers)."""
        self._style(fill=fill, stroke=stroke, width=width)
        self._lines.append("rectangle %.1f,%.1f %.1f,%.1f" % (x0, y0, x1, y1))

    def circle(self, cx, cy, r, fill=None, stroke=None, width=1):
        """Circle marker (plot-curve's points)."""
        self._style(fill=fill, stroke=stroke, width=width)
        self._lines.append("circle %.1f,%.1f %.1f,%.1f" % (cx, cy, cx + r, cy))

    def text(self, x, y, text, size, color=TEXT_RGB, align="left", font=None):
        """Text at baseline (x, y); align="left" / "center" / "right" moves the anchor.

        Right/centre alignment uses the measured width (`_text_width`, cached) so the label
        ends where the caller wants it; left alignment is exact without measuring.
        """
        s = str(text)
        ax = x
        if align != "left":
            w = _text_width(font or _font(), int(size), s)
            ax = x - w if align == "right" else x - w / 2.0
        self._style(fill=color, stroke=None)
        self._lines.append("font-size %d" % int(size))
        self._lines.append("text %.1f,%.1f '%s'" % (ax, y, s.replace("'", '"')))

    def translate(self, x, y):
        """Move the drawing origin — a rotated axis label is anchored this way."""
        self._lines.append("translate %.1f,%.1f" % (float(x), float(y)))

    def rotate(self, degrees):
        """Rotate the drawing frame by `degrees` (a y axis label needs -90)."""
        self._lines.append("rotate %.1f" % float(degrees))

    def render(self):
        """The script text for `magick -draw @file`."""
        return "\n".join(self._lines) + "\n"


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
            arrays.append(vals)
        if len(arrays[0]) != len(arrays[1]) or len(arrays[0]) != len(arrays[2]):
            raise RuntimeError("dens 的泳道 %d 的 profile 三条数组长度不一致（%d / %d / %d）"
                               % (no, len(arrays[0]), len(arrays[1]), len(arrays[2])))
        out.append((no, ln))
    return out


def _select(dens, lanes):
    """Lane numbers to plot (§4.7): None = every lane in dens order, else those lanes in dens order.

    A requested lane that the dens does not hold is a RuntimeError listing the lanes it has
    (`plot_bands` stays strict; the CLI filters `--lanes` down to what the file holds).
    """
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


# ---------------------------------------------------------------- per-panel texts

def _lane_header(lane):
    """`LANE 2: S1`; a None label prints as the lane number only (ruling R6)."""
    no = int(lane.get("lane"))
    label = lane.get("label")
    text = "LANE %d" % no
    if isinstance(label, str) and label.strip():
        text += ": " + _ascii(label, 28)
    return text


def _lane_total(lane, baseline):
    """The lane total for the active baseline: `flank 23.4 | global 22.3 gray*cm`."""
    if baseline == "both":
        return "flank %.1f | global %.1f gray*cm" % (lane["area_flank"], lane["area_global"])
    key = "area_flank" if baseline == "flank" else "area_global"
    return "%s %.1f gray*cm" % (baseline, lane[key])


def _band_label(band, baseline, compact=False):
    """The per-band label: `b1 11.7 / 5.8 45.0 kDa` (flank, then global; unit in the legend).

    The unit (`gray*cm`, ASCII per ruling R1) is stated once in the legend instead of on every
    label, and the `clipped` / `flat top` flags and note tags are per-lane header lines
    (`_header_lines`), so a label stays short enough to fit beside a dark band on a 3-column
    grid.  `compact` drops the MW for a panel too narrow to host the full label.
    """
    if baseline == "both":
        area = "%.1f / %.1f" % (band["area_flank"], band["area_global"])
    else:
        area = "%.1f" % band["area_flank" if baseline == "flank" else "area_global"]
    text = "b%d %s" % (int(band["index"]), area)
    mw = band.get("mw_kda")
    if (not compact) and isinstance(mw, (int, float)) and not isinstance(mw, bool):
        text += " %.1f kDa" % float(mw)
    return text


def _flag_line(lane):
    """The lane's quality flags on ONE line: `b1 flat top | b2 ! clipped, flat top`.

    Flags are per band but cheap to pack, and a single line leaves the header room for the
    note tags, which are the more interesting detail (a clamped window, a ramp fallback).
    """
    parts = []
    for band in lane.get("bands") or []:
        flags = []
        if band.get("clipped"):
            flags.append("! clipped")
        if band.get("flat_top"):
            flags.append("flat top")
        if flags:
            parts.append("b%d %s" % (int(band["index"]), ", ".join(flags)))
    return [" | ".join(parts)] if parts else []


def _tag_lines(lane):
    """Per-band measurement notes as header lines: `b2 clamped_mid_down, flank_fallback_right`."""
    lines = []
    for band in lane.get("bands") or []:
        tags = [t for t in (band.get("notes") or []) if isinstance(t, str) and t]
        if tags:
            lines.append("b%d %s" % (int(band["index"]), ", ".join(tags)))
    return lines


def _header_lines(lane):
    """The per-lane header detail lines: quality flags first, then the measurement notes."""
    return _flag_line(lane) + _tag_lines(lane)


def _clip_text(text, size, max_px, font=None):
    """Truncate text to the available width, with a trailing `...` (ASCII stays ASCII).

    Uses the measured width when a `font` is passed and the cheap estimate otherwise; the
    estimate is only for packing decisions and never for the truncation a human reads.
    """
    if max_px <= 0:
        return ""
    if _estimate_width(size, text) <= max_px:
        return text
    if font is not None:
        if _text_width(font, size, text) <= max_px:
            return text
        keep = len(text)
        while keep > 0:
            keep -= 1
            probe = text[:keep].rstrip() + "..."
            if _text_width(font, size, probe) <= max_px:
                return probe
        return "..."
    keep = max(3, int(max_px / (0.47 * size)) - 3)
    return text[:keep].rstrip() + "..."


# ---------------------------------------------------------------- layout (§4.7)

MARGIN = 28              # around the whole grid
TITLE_H = 92             # the one-off header block in the top margin
GAP_ROWS = 24
GUTTER = 48              # y-axis labels sit left of the first column's plot area
RIGHT_PAD = 12
NARROW_PANEL = 260       # panels narrower than this reserve one header line and drop MW
AXIS_H = 42              # gray axis under each panel
NOTE_STEP = 19
MAX_NOTE_LINES = 3


def band_layout(dens, baseline="both", lanes=None, cols=3, width=2400):
    """Geometry of the plot-bands grid — shared by `plot_bands` and the pixel selftest.

    Returns a dict: `width`, `height`, `cols`, `rows`, `panel_w`, `panel_h`, `lane_order`,
    `panels` (lane no → `Panel`), `cells` (lane no → (cell x, cell y, cell w, cell h)),
    `header_h`, and the `title` / `params_line` / `legend` strings.  This is the ONLY place
    that computes the grid, so a test reads `panels[lane].x(gray)` / `.y(y_cm)` and samples
    exactly where the renderer drew.
    """
    cols, width = _check_options(baseline, cols, width)
    selected = _select(dens, lanes)
    n = len(selected)
    rows = (n + cols - 1) // cols
    panel_w = (width - 2.0 * MARGIN) / cols
    if panel_w <= GUTTER + RIGHT_PAD:
        raise ValueError("width %d 下无法绘制 %d 列（请增大 --width 或减少 --cols）" % (width, cols))
    panel_h = max(round(panel_w * 3.0 / 4.0), 160)

    note_counts = []
    for _no, lane in selected:
        note_counts.append(min(MAX_NOTE_LINES, len(_header_lines(lane))))
    lines = max(note_counts) if note_counts else 0
    header_h = 52 + NOTE_STEP * lines
    if panel_w < NARROW_PANEL:              # very narrow cells: one note line at most
        header_h = 52 + NOTE_STEP * min(lines, 1)
        lines = min(lines, 1)

    plot_h = panel_h - header_h - AXIS_H
    if plot_h < 80:                        # keep the profile legible; the cell grows instead
        plot_h = 80
        panel_h = header_h + AXIS_H + plot_h
    height = int(2 * MARGIN + TITLE_H + rows * panel_h + (rows - 1) * GAP_ROWS)

    panels = {}
    cells = {}
    lane_order = []
    for k, (no, lane) in enumerate(selected):
        col, row = k % cols, k // cols
        cx = MARGIN + col * panel_w
        cy = MARGIN + TITLE_H + row * (panel_h + GAP_ROWS)
        perf = lane["profile"]
        y_min = min(float(v) for v in perf["y_cm"])
        y_max = max(float(v) for v in perf["y_cm"])
        if y_max <= y_min:
            y_max = y_min + 0.01
        box = (cx + GUTTER, cy + header_h, cx + panel_w - RIGHT_PAD, cy + header_h + plot_h)
        panels[no] = Panel(box, y_min, y_max)
        cells[no] = (cx, cy, panel_w, panel_h)
        lane_order.append(no)

    params = dens.get("params") if isinstance(dens.get("params"), dict) else {}
    report = (dens.get("source") or {}).get("report") if isinstance(dens.get("source"), dict) else ""
    title = "ep-craft densitometry plot-bands"
    if isinstance(report, str) and report:
        title += "  -  " + _ascii(os.path.basename(report), 80)
    params_line = "baseline %s  |  flank_cm %g  |  min_ink %g  |  %d lane%s" % (
        baseline, float(params.get("flank_cm", 0.0)), float(params.get("min_ink", 0.0)),
        n, "" if n == 1 else "s")
    legend = ("solid dark = gray profile  |  dashed blue = global baseline  |  "
              "dashed orange = flank ramp  |  shaded = area (gray*cm)")
    return {"width": width, "height": height, "cols": cols, "rows": rows,
            "panel_w": panel_w, "panel_h": panel_h, "header_h": header_h,
            "margin": MARGIN, "title_h": TITLE_H, "lane_order": lane_order,
            "panels": panels, "cells": cells,
            "title": title, "params_line": params_line, "legend": legend}


# ---------------------------------------------------------------- band anchors / clipping

def _row_at(lane, y_cm):
    """Profile row nearest to a band's y_cm (the peak row the label and the check use)."""
    y = lane["profile"]["y_cm"]
    return min(range(len(y)), key=lambda k: abs(float(y[k]) - float(y_cm)))


def _ramp_at(band, y_cm):
    """The flank ramp value at a depth (linear between its endpoints, clamped outside)."""
    (yl, gl), (yr, gr) = band["ramp"]["left"], band["ramp"]["right"]
    if yr <= yl:
        return float(gl)
    t = (float(y_cm) - float(yl)) / (float(yr) - float(yl))
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return float(gl) + (float(gr) - float(gl)) * t


def _clip_gray_segment(yl, gl, yr, gr):
    """Clip a ramp segment to gray 0..255 → (yl, gl, yr, gr) or None (§4.7: no outside values)."""
    lo, hi = 0.0, 255.0
    dy, dg = float(yr) - float(yl), float(gr) - float(gl)
    t0, t1 = 0.0, 1.0
    for p, q in ((-dg, float(gl) - lo), (dg, hi - float(gl))):
        if p == 0.0:
            if q < 0.0:
                return None
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return None
                if r < t1:
                    t1 = r
    if t0 >= t1:
        return None
    return (float(yl) + dy * t0, float(gl) + dg * t0,
            float(yl) + dy * t1, float(gl) + dg * t1)


def _rows(lane, window):
    """Row indices of the profile inside a closed window (for the shaded polygons)."""
    y = lane["profile"]["y_cm"]
    a, b = float(window[0]) - 1e-9, float(window[1]) + 1e-9
    return [k for k, v in enumerate(y) if a <= float(v) <= b]


# ---------------------------------------------------------------- the plot-bands renderer

def _shade(sh, lane, band, panel, baseline):
    """Shade one band's window(s) — the integrated area — and draw its flank ramp (§4.7).

    The window polygon follows the gray curve on one side and the baseline on the other over
    the profile rows whose y_cm falls in the closed window [y0, y1]; that is exactly the
    region `measure` summed, so the picture can be checked against the number.  The flank
    ramp is drawn over the band only, clipped to gray 0..255 (`_clip_gray_segment`).
    """
    if baseline in ("both", "flank"):
        rows = _rows(lane, band["window_flank"])
        if rows:
            y = lane["profile"]["y_cm"]
            gray = lane["profile"]["gray"]
            top = [(panel.x(float(gray[k])), panel.y(float(y[k]))) for k in rows]
            low = [(panel.x(_ramp_at(band, float(y[k]))), panel.y(float(y[k])))
                   for k in reversed(rows)]
            sh.polygon(top + low, FLANK_FILL)
        clipped = _clip_gray_segment(*band["ramp"]["left"], *band["ramp"]["right"])
        if clipped is not None:
            yl, gl, yr, gr = clipped
            sh.polyline([(panel.x(gl), panel.y(yl)), (panel.x(gr), panel.y(yr))],
                        FLANK_RGB, width=2, dash="6 4")

    if baseline in ("both", "global"):
        rows = _rows(lane, band["window_global"])
        if rows:
            y = lane["profile"]["y_cm"]
            gray = lane["profile"]["gray"]
            base = lane["profile"]["baseline_global"]
            top = [(panel.x(float(gray[k])), panel.y(float(y[k]))) for k in rows]
            low = [(panel.x(float(base[k])), panel.y(float(y[k]))) for k in reversed(rows)]
            sh.polygon(top + low, GLOBAL_FILL)


def _label_slot(text, xc, xb, x0, x1, font):
    """(text, anchor x, align, whole) — the two gaps beside a band's curve at one row.

    Preferred slot: right-aligned 8 px left of the curve (the dark side of the peak).  Second:
    left-aligned just past the baseline (the light side, outside the shading).  When neither
    gap holds the whole text, the wider gap gets the truncated label and `whole` is False.
    Anchors stay inside [x0, x1], so a label can never spill into the neighbouring panel.
    """
    full = _text_width(font, LABEL_FONT, text)
    if xc - 8.0 - full >= x0 + 2.0:
        return text, xc - 8.0, "right", True
    if xb + 8.0 + full <= x1 - 2.0:
        return text, xb + 8.0, "left", True
    gaps = [(xc - 8.0, "right", xc - 8.0 - x0), (xb + 8.0, "left", x1 - 2.0 - (xb + 8.0))]
    anchor, align, room = max(gaps, key=lambda g: g[2])
    return _clip_text(text, LABEL_FONT, int(room) - 2, font), anchor, align, False


def _label_box(text, anchor, align, y, font):
    """The pixel rectangle a placed label occupies (left, top, right, bottom)."""
    w = _text_width(font, LABEL_FONT, text)
    left = anchor - w if align == "right" else anchor
    return (left, y - 13.0, left + w, y + 4.0)


def _occupied(lane, panel, baseline):
    """[(y pixel, x left, x right)] of everything the label must not cover in one panel.

    Per profile row that is the curve (the polyline is 2 px wide, padded to 3) and the
    global baseline; rows inside an active window also carry the shading, which spans
    horizontally from the curve to the baseline (flank ramp when the flank baseline is
    active).  A label rectangle clear of these segments covers neither a curve nor its fill,
    which is what §4.7 asks for.
    """
    y_cm = lane["profile"]["y_cm"]
    gray = lane["profile"]["gray"]
    base = lane["profile"]["baseline_global"]
    rows_spans = []
    for k in range(len(y_cm)):
        y = panel.y(float(y_cm[k]))
        xg = panel.x(float(gray[k]))
        xb = panel.x(float(base[k]))
        rows_spans.append((y, min(xg, xb) - 3.0, max(xg, xb) + 3.0))
    for band in lane.get("bands") or []:
        windows = []
        if baseline in ("both", "flank"):
            windows.append((band["window_flank"], True))
        if baseline in ("both", "global"):
            windows.append((band["window_global"], False))
        for window, is_flank in windows:
            for k in _rows(lane, window):
                y = panel.y(float(y_cm[k]))
                xg = panel.x(float(gray[k]))
                xo = (panel.x(_ramp_at(band, float(y_cm[k]))) if is_flank
                      else panel.x(float(base[k])))
                lo, hi = (xg, xo) if xg <= xo else (xo, xg)
                old = rows_spans[k]
                rows_spans[k] = (y, min(old[1], lo - 1.0), max(old[2], hi + 1.0))
    return rows_spans


def _rect_hits(rect, spans, pad=3.0):
    """True when a label rectangle touches one of the reserved spans (curve / fill)."""
    top, bottom = rect[1] - pad, rect[3] + pad
    left, right = rect[0] - pad, rect[2] + pad
    for y, lo, hi in spans:
        if top <= y <= bottom and left <= hi and lo <= right:
            return True
    return False


def _boxes_apart(a, b, pad=4.0):
    """True when two label rectangles keep at least `pad` px apart."""
    return (a[2] + pad <= b[0] or b[2] + pad <= a[0]
            or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _label_rows(lane, panel, band, limit=60):
    """Candidate rows for a band's label, closest to the band first, all within reach.

    The band's own peak row comes first; the rest are rows whose pixel depth lies inside the
    band's window plus one label height above and below, so the label can move into the white
    gap between two neighbouring bands when the curve's own spike leaves no room.
    """
    y_px = [panel.y(float(v)) for v in lane["profile"]["y_cm"]]
    k0 = _row_at(lane, band["y_cm"])
    window = band.get("window_flank") or band.get("window_global")
    rows = _rows(lane, window) if window else [k0]
    top = min(y_px[j] for j in rows) - 2.0 * NOTE_STEP
    bottom = max(y_px[j] for j in rows) + 2.0 * NOTE_STEP
    reach = [j for j, v in enumerate(y_px) if top <= v <= bottom]
    if k0 not in reach:
        reach.append(k0)
    reach.sort(key=lambda j: abs(y_px[j] - y_px[k0]))
    return reach[:limit]


def _label_layout(lane, panel, baseline, x0, y0, x1, y1):
    """[(text, anchor x, align, y)] for every band — fitted, clear of ink, de-collided.

    A band label must (a) hold the whole text if any slot can, (b) not cover the curve,
    baseline or shading of the panel (`_occupied` / `_rect_hits`), (c) not touch another
    band's label, and (d) stay inside the plot box.  Rows are tried from the band's own
    peak outward, so the label ends up next to the band it describes; when no row can host
    the whole text the closest row gets a truncated label rather than nothing.
    """
    y_cm = lane["profile"]["y_cm"]
    gray = lane["profile"]["gray"]
    base = lane["profile"]["baseline_global"]
    font = _font()
    spans = _occupied(lane, panel, baseline)
    compact = (x1 - x0) < 420.0          # a narrow panel: keep the labels short, not absent
    labels = []
    boxes = []
    for band in lane.get("bands") or []:
        full = _band_label(band, baseline, compact)
        fallback = None
        chosen = None
        for k in _label_rows(lane, panel, band):
            xc = panel.x(float(gray[k]))
            xb = panel.x(float(base[k]))
            if baseline in ("both", "flank"):
                xb = max(xb, panel.x(_ramp_at(band, float(y_cm[k]))))
            text, anchor, align, whole = _label_slot(full, xc, xb, x0, x1, font)
            y = panel.y(float(y_cm[k])) + 6.0
            if not (y0 + 14.0 <= y <= y1 - 4.0):
                continue
            box = _label_box(text, anchor, align, y, font)
            if _rect_hits(box, spans):
                if fallback is None:
                    fallback = (text, anchor, align, y)
                continue
            if not all(_boxes_apart(box, other) for other in boxes):
                if fallback is None:
                    fallback = (text, anchor, align, y)
                continue
            chosen = (text, anchor, align, y)
            if whole:
                break
        if chosen is None and fallback is not None:
            chosen = fallback
        if chosen is None:
            k = _row_at(lane, band["y_cm"])
            xc = panel.x(float(gray[k]))
            xb = panel.x(float(base[k]))
            text, anchor, align, _whole = _label_slot(full, xc, xb, x0, x1, font)
            chosen = (text, anchor, align,
                      min(max(panel.y(float(y_cm[k])) + 6.0, y0 + 14.0), y1 - 4.0))
        text, anchor, align, y = chosen
        labels.append(chosen)
        boxes.append(_label_box(text, anchor, align, y, font))
    edges = []
    for band in lane.get("bands") or []:
        if baseline in ("both", "flank"):
            edges.extend(panel.y(float(v)) for v in band["window_flank"])
        if baseline in ("both", "global"):
            edges.extend(panel.y(float(v)) for v in band["window_global"])
    return _settle(labels, spans, edges, y0, y1, font)


# label offsets tried by `_settle`, nearest first: a label box is 17 px tall, so moving it
# half a text line is often enough to step off a window rule or the curve's shoulder.
_LABEL_OFFSETS = (0, -4, 4, -8, 8, -12, 12, -16, 16, -20, 20, -24, 24, -28, 28,
                  -32, 32, -36, 36, -40, 40, -48, 48, -56, 56, -64, 64)


def _ink_overlap(box, spans):
    """Pixels of reserved ink (curve / baseline / shading) a label rectangle would cover.

    Sums the horizontal overlap with every reserved span whose depth the box reaches; the
    result is 0.0 only when the text sits entirely on white, which is what the placement
    scorer maximises.
    """
    left, top, right, bottom = box[0] - 1.0, box[1] - 1.0, box[2] + 1.0, box[3] + 1.0
    total = 0.0
    for y, lo, hi in spans:
        if top <= y <= bottom and left <= hi and lo <= right:
            total += min(right, hi) - max(left, lo)
    return total


def _settle(labels, spans, edges, y0, y1, font):
    """Move each label to its best free slot: clear of ink, of other labels, of window rules.

    Two bands can be closer together than one text line (the synthetic lane 1 has two bands
    0.15 cm apart), so their natural slots collide; a band's peak row can cross the curve's
    shoulder even when its horizontal slot is white; and every band's window contributes two
    dashed rules at its own depth, which a label sitting on the band would cross.  Candidates
    are the natural depth plus offsets of up to 64 px either way (nearest first), scored by
    (no ink under the box, no window rule through the box, least ink coverage, nearest to the
    band).  A candidate that still covers ink is only used when nothing clean exists, and only
    if it covers less than the natural slot would.
    """
    settled = []
    out = []
    for text, anchor, align, y in labels:
        best = None
        for delta in _LABEL_OFFSETS:
            ty = min(max(y + delta, y0 + 14.0), y1 - 4.0)
            box = _label_box(text, anchor, align, ty, font)
            if not all(_boxes_apart(box, other) for other in settled):
                continue
            overlap = _ink_overlap(box, spans)
            cut = any(box[1] - 3.0 <= ey <= box[3] + 3.0 for ey in edges)
            # clean ink first, then a box no window rule crosses, then the least overlap,
            # then the shortest move; tuple comparison keeps the order deterministic.
            rank = (1 if overlap <= 0.0 else 0, 0 if cut else 1, -overlap, -abs(ty - y))
            if best is None or rank > best[0]:
                best = (rank, (text, anchor, align, ty), box)
        if best is None:                      # nothing free: keep the natural slot
            ty = min(max(y, y0 + 14.0), y1 - 4.0)
            box = _label_box(text, anchor, align, ty, font)
            best = ((0, 0, -1e9, 0), (text, anchor, align, ty), box)
        out.append(best[1])
        settled.append(best[2])
    return out


def _rule_segments(x0, x1, y, boxes, pad=1.0):
    """Horizontal segments of a window rule that no label box covers.

    The window edges run across the whole plot area (§4.7), and in a dense lane several
    windows can sit within one text line, so a label often has to share its row with an edge
    rule.  The rule is dashed anyway, so it is drawn in segments that stop at each label box:
    the boundary stays visible everywhere it carries meaning and the text stays legible.
    """
    cuts = sorted((max(x0, box[0] - pad), min(x1, box[2] + pad))
                  for box in boxes if box[1] - pad <= y <= box[3] + pad)
    segs = []
    cursor = x0
    for lo, hi in cuts:
        if lo > cursor:
            segs.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < x1:
        segs.append((cursor, x1))
    return [(a, b) for a, b in segs if b - a >= 2.0]


def _draw_panel(sh, lane, no, geo, baseline):
    """One lane panel: header, shaded bands, curves, axes, window edges and band labels."""
    panel = geo["panels"][no]
    cx, cy, pw, _ph = geo["cells"][no]
    x0, y0, x1, y1 = panel.box
    y = lane["profile"]["y_cm"]
    gray = lane["profile"]["gray"]
    base = lane["profile"]["baseline_global"]

    # labels are placed before anything is drawn: their boxes decide where the window rules
    # have to leave a gap, so text never sits under a dashed edge
    font = _font()
    labels = _label_layout(lane, panel, baseline, x0, y0, x1, y1)
    label_boxes = [_label_box(text, ax, align, ly, font) for text, ax, align, ly in labels]

    # header: lane, total for the active baseline, and the band tag lines — every line is
    # clipped to the cell, so a narrow panel can never spill text into its neighbour
    room = int(pw - 12)
    sh.text(cx + 4, cy + 24, _clip_text(_lane_header(lane), HEADER_FONT, room, font),
            HEADER_FONT, TEXT_RGB)
    sh.text(cx + 4, cy + 46, _clip_text(_lane_total(lane, baseline), LABEL_FONT, room, font),
            LABEL_FONT, MUTED_RGB)
    # the layout reserved `header_h` for this header; drawing more lines than that fit would
    # push text into the plot area, so the count follows the reserved height (narrow cells
    # reserve one note line, wide ones up to MAX_NOTE_LINES)
    fits = max(0, int((geo["header_h"] - 52) // NOTE_STEP))
    for i, line in enumerate(_header_lines(lane)[:fits]):
        sh.text(cx + 4, cy + 66 + NOTE_STEP * i,
                _clip_text(line, LABEL_FONT, room, font), LABEL_FONT, MUTED_RGB)

    # translucent shading first, so the curves stay crisp on top of it
    for band in lane.get("bands") or []:
        _shade(sh, lane, band, panel, baseline)

    # window edges of the active baseline(s): thin dashed rules across the plot area
    for band in lane.get("bands") or []:
        edges = []
        if baseline in ("both", "global"):
            edges.append((band["window_global"], GLOBAL_RGB))
        if baseline in ("both", "flank"):
            edges.append((band["window_flank"], FLANK_RGB))
        for win, color in edges:
            for edge in win:
                ey = panel.y(edge)
                if y0 < ey < y1:
                    for a, b in _rule_segments(x0, x1, ey, label_boxes):
                        sh.rule(a, ey, b, ey, color, width=1, dash="2 2")

    # the gray profile, then the global baseline (both drawn for every baseline choice: the
    # shading is what `baseline` selects, the curves are context — spec §4.7)
    sh.polyline([(panel.x(float(g)), panel.y(float(v))) for g, v in zip(gray, y)],
                CURVE_RGB, width=2)
    sh.polyline([(panel.x(float(b)), panel.y(float(v))) for b, v in zip(base, y)],
                GLOBAL_RGB, width=2, dash="8 5")

    # y axis: 1 cm ticks with labels, first (left-most) column only
    sh.rule(x0, y0, x0, y1, GRID_RGB, width=1)
    col = (cx - geo["margin"]) / float(geo["panel_w"])
    if abs(col) < 0.5:
        start = max(1, int(math.ceil(panel.mapping.y_min)))
        for cm in range(start, int(panel.mapping.y_max) + 1):
            ty = panel.y(float(cm))
            if y0 < ty < y1:
                sh.rule(x0 - 4, ty, x0, ty, GRID_RGB, width=1)
                sh.text(x0 - 7, ty + 5, "%d" % cm, LABEL_FONT, MUTED_RGB, align="right")
        sh.text(x0 - 7, y0 + 8, "cm", LABEL_FONT, MUTED_RGB, align="right")

    # gray axis under every panel: 0 (dark) at the left … 255 at the right
    ay = y1 + 12
    sh.rule(x0, ay, x1, ay, GRID_RGB, width=1)
    for g in (0, 64, 128, 192, 255):
        tx = panel.x(float(g))
        sh.rule(tx, ay, tx, ay + 5, GRID_RGB, width=1)
    sh.text(x0, y1 + 34, "0 (dark)", LABEL_FONT, TEXT_RGB)
    sh.text(x1, y1 + 34, "255", LABEL_FONT, TEXT_RGB, align="right")
    if x1 - x0 > 300:
        sh.text((x0 + x1) / 2.0, y1 + 34, "gray", LABEL_FONT, MUTED_RGB, align="center")

    # band labels: area (.1f), MW and the ASCII flags, placed clear of curve and shading
    for label, ax, align, ly in labels:
        sh.text(ax, ly, label, LABEL_FONT, TEXT_RGB, align=align)


def _draw_title(sh, geo, width):
    """The one-off margin block: title, run parameters and the ASCII legend."""
    sh.text(MARGIN, MARGIN + 26, _clip_text(geo["title"], TITLE_FONT, width - 2 * MARGIN),
            TITLE_FONT, TEXT_RGB)
    sh.text(MARGIN, MARGIN + 54, _clip_text(geo["params_line"], PARAMS_FONT, width - 2 * MARGIN),
            PARAMS_FONT, MUTED_RGB)
    sh.text(MARGIN, MARGIN + 78, _clip_text(geo["legend"], LEGEND_FONT, width - 2 * MARGIN),
            LEGEND_FONT, MUTED_RGB)


def plot_bands(dens, out, baseline="both", lanes=None, cols=3, width=2400):
    """Render dens.json as a PNG grid of per-lane panels (§4.7) → (width, height).

    `out` must be a `.png` path whose directory exists (ruling R12); `baseline` picks which
    baseline(s) are shaded and totalled; `lanes=None` plots every lane in dens order.  The
    return value is the pixel size that was written, which is also `band_layout`'s `width` /
    `height`.
    """
    target = _check_out(out)
    geo = band_layout(dens, baseline=baseline, lanes=lanes, cols=cols, width=width)
    sh = Script()
    _draw_title(sh, geo, geo["width"])
    lanes_by_no = {no: lane for no, lane in _lane_numbers(dens)}
    for no in geo["lane_order"]:
        _draw_panel(sh, lanes_by_no[no], no, geo, baseline)
    _render(sh.render(), geo["width"], geo["height"], target)
    return geo["width"], geo["height"]


# ---------------------------------------------------------------- plot-curve (ticket ep-bqv.4)
#
# `plot_curve(curve, out, quant=None, width=1600)` draws curve.json (spec §4.5) on log-free
# linear axes: the standard points as filled circles with their replicate bars, dropped points
# hollow, the fitted model across the calibrated range and its ±10 % extension, and a residual
# strip under the main axes that shares the volume axis.  A curve that looks fine but has one
# point 12 % off is exactly what a terminal table hides, so the picture — not the r² — is what
# tells a human whether the fit can be trusted.
#
# With a quant dict (spec §4.6) the sample measurements appear as squares on the same axes;
# without `quant` nothing about the picture changes.  No math lives here beyond evaluating the
# fit at the line's ends and scaling values into pixels: every number comes from the curve /
# quant dicts.
#
# The kind strings are resolved through `getattr` so this renderer works both next to a
# `densitometry.py` that predates the curve builder (ticket .3) and after it merges in.

CURVE_KIND = getattr(densitometry, "CURVE_KIND", "ep-craft-densitometry-curve")
QUANT_KIND = getattr(densitometry, "QUANT_KIND", "ep-craft-densitometry-quant")
CURVE_SUFFIX = getattr(densitometry, "CURVE_SUFFIX", ".curve.json")

POINT_RGB = (37, 78, 150)          # filled standard point
POINT_STROKE_RGB = (17, 45, 104)
DROP_RGB = (196, 88, 42)           # dropped standard: hollow ring, white centre, `dropped`
LINE_RGB = (38, 38, 38)            # fitted line and residual markers
SAMPLE_RGB = (12, 132, 72)         # filled sample square (--quant)
SAMPLE_STROKE_RGB = (7, 82, 44)
OOR_RGB = (148, 58, 168)           # out-of-range sample: hollow square on the x axis
ZERO_RGB = (60, 60, 60)            # residual strip zero line
GUIDE_RGB = (150, 150, 150)        # residual strip ±10 % guides (dashed)
ERR_RGB = (105, 105, 105)          # replicate bars / residual stems
CURVE_FRAME_RGB = GRID_RGB

CURVE_TITLE_FONT = 24
CURVE_SUB_FONT = 16
ANN_FONT = 16
AXIS_FONT = 18
CURVE_TICK_FONT = 15
MARK_FONT = 15

POINT_R = 6.0                      # standard point radius
SAMPLE_HALF = 5.0                  # sample square half side
RESID_R = 4.0                      # residual marker radius

ANN_STEP = 20
TOP_PAD = 16
LEFT_PAD = 104                     # y tick labels + the rotated y axis label
CURVE_RIGHT_PAD = 42
RESID_MIN = 80
GUIDE_LABEL_MIN = 15.0             # px between the ±10 % guides and the zero line for per-line
                                   # labels; closer than that and one band note is used instead
ANN_GAP = 20

# SPEC CONFLICT (units), flagged to the lead: `curve.points[].sd` is the sample SD of the
# replicate AREAS (`volume`, gray*cm) — `build_curve` collapses replicates by mass and computes
# the SD over `volume`.  §4.7 and this ticket ask for VERTICAL y-error bars, i.e. the same
# number drawn as a mass uncertainty; the units do not match, and the vertical form must not be
# read as a mass uncertainty.  The literal vertical drawing is the default because the plotting
# contract asks for it; the axis treatment is isolated here on purpose so review can flip it:
#   "y" -> vertical bars on the mass axis (literal §4.7 / ticket wording; current behaviour)
#   "x" -> horizontal bars on the volume axis, the unit `sd` actually has
ERROR_BAR_AXIS = "y"


# ---------------------------------------------------------------- plot-curve: validation

def _trim_num(text):
    """`0.400` → `0.4`, `6.00` → `6`, `-0.0` → `0` (trailing zeros trimmed, never `-0`)."""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _eqnum(value, digits=1):
    """A value with `digits` decimals, trailing zeros trimmed (`2.50` → `2.5`, `3.00` → `3`)."""
    return _trim_num(("%." + str(int(digits)) + "f") % float(value))


def _tick_digits(step):
    """Decimals a tick label needs for `step` (`0.02` → 4, `100` → 0)."""
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
                eqn = "mass = %s*area - %s" % (_trim_num("%.2f" % slope),
                                               _trim_num("%.2f" % -intercept))
            else:
                eqn = "mass = %s + %s*area" % (_trim_num("%.2f" % intercept),
                                               _trim_num("%.2f" % slope))
        else:
            scale = float(fp["scale"])
            exponent = float(fp["exponent"])

            def fn(volume):
                volume = float(volume)
                return scale * (volume ** exponent) if volume > 0.0 else 0.0

            eqn = "mass = %s*area^%s" % (_trim_num("%.2f" % scale),
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
                                       % (no, band.get("mass")))
            out_band = bool(band.get("out_of_range")) or mass is None
            bands.append({"index": band.get("index"), "area": area, "mass": mass,
                          "out": out_band})
        out.append((lane, bands))
    return out


def _used_range(curve, rng):
    """(volume_min, volume_max) of the used standard points: the curve's `range`, else derived."""
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
    if hi <= lo:                              # a single standard volume: give the line a width
        return lo - 0.5, lo + 0.5
    return lo, hi


# ---------------------------------------------------------------- plot-curve: layout helpers

def _nice_step(span, target):
    """A 1 / 2 / 2.5 / 5 / 10 step giving about `target` ticks over `span` (0 when empty)."""
    if span <= 0:
        return 0.0
    raw = float(span) / max(1, int(target))
    mag = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if mult * mag >= raw * 0.999:
            return mult * mag
    return 10.0 * mag


def _ticks(lo, hi, target=6):
    """Tick values from `lo` to `hi` with a nice step (never more than 40 values)."""
    step = _nice_step(hi - lo, target)
    if step <= 0:
        return [float(lo), float(hi)]
    first = math.ceil(lo / step - 1e-9) * step
    values, value = [], first
    while value <= hi + step * 0.5 and len(values) < 40:
        values.append(round(value, 6))
        value += step
    return values


def _snap(value, width):
    """Snap a rule coordinate to whole pixels (odd stroke centered on it, even between two).

    Without this a 1 px line lands between two pixel rows and antialiases into two grey rows,
    so a test looking for the exact guide/zero colour would not find it.
    """
    base = float(round(value))
    return base if int(width) % 2 else base + 0.5


def _hrule(sh, x0, x1, y, color, width=1, dash=None):
    """Horizontal rule snapped to a pixel row so its exact colour survives."""
    sh.rule(x0, _snap(y, width), x1, _snap(y, width), color, width=width, dash=dash)


def _vrule(sh, x, y0, y1, color, width=1, dash=None):
    """Vertical rule snapped to a pixel column (same reason as `_hrule`)."""
    x = _snap(x, width)
    sh.rule(x, y0, x, y1, color, width=width, dash=dash)


class CurveAxes:
    """plot-curve's value → pixel mapping: `.x(volume)`, `.y(mass)`, `.yr(rel_pct)`.

    `main` maps the main axes (x = volume to the right, y = mass upward, 0 at the bottom-left);
    `resid` maps the residual strip under it (same x range, y = percent deviation with 0 in the
    middle).  `.box` / `.resid_box` are the pixel rectangles, so a pixel test reads positions
    from this object instead of re-deriving the layout.
    """
    def __init__(self, main_box, resid_box, x_range, y_range, rel_range):
        self.main = Mapping(main_box, x_range, y_range, y_down=False)
        self.resid = Mapping(resid_box, x_range, rel_range, y_down=False)
        self.box = main_box
        self.resid_box = resid_box

    def x(self, volume):
        """Area (volume) → pixel x (shared by the main axes and the residual strip)."""
        return self.main.x(volume)

    def y(self, mass):
        """Mass → pixel y on the main axes."""
        return self.main.y(mass)

    def yr(self, rel_pct):
        """Percent deviation → pixel y on the residual strip."""
        return self.resid.y(rel_pct)


def _fit_segment(geo, v0, v1):
    """Pixel polyline of the fitted model between two volumes, clamped inside the axes box."""
    x0, y0, x1, y1 = geo["main_box"]
    steps = 2 if geo["fit"] == "linear" else 40
    pts = []
    for k in range(steps):
        v = float(v0) + (float(v1) - float(v0)) * (k / float(steps - 1))
        px = min(max(geo["axes"].x(v), x0 + 1.0), x1 - 1.0)
        py = min(max(geo["axes"].y(geo["eval"](v)), y0 + 1.0), y1 - 1.0)
        pts.append((px, py))
    return pts


def _dash_runs(points, on=7.0, off=5.0):
    """Split a polyline into the solid runs of a dash pattern, anchored at its LAST point.

    MVG's `stroke-dasharray` starts its phase at the first vertex, so the pattern can end on a
    gap — the fit line would then stop a few pixels short of the frame edge, and no pixel test
    can sample "the line reaches the edge".  These runs are derived backwards from the end
    instead: the final run always ends exactly on the polyline's last point.
    """
    if len(points) < 2:
        return [list(points)] if points else []
    spans, total = [], 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        d = math.hypot(x1 - x0, y1 - y0)
        spans.append((total, total + d, (x0, y0), (x1, y1)))
        total += d
    if total <= 0.0:
        return [list(points)]

    def at(s):
        s = min(max(s, 0.0), total)
        for start, end, a, b in spans:
            if end >= s - 1e-9:
                frac = 0.0 if end <= start else (s - start) / (end - start)
                return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)
        return points[-1]

    runs, end = [], total
    while end > 0.0:
        start = max(0.0, end - on)
        inner = [pts for lo, hi, pts, _p in spans if lo >= start and hi < end]
        runs.append([at(start)] + inner + [at(end)])
        end = start - off
    return list(reversed(runs))


def _place_label(text, size, ax, ay, boxes, font, plot_box, order=(0, 1, 2, 3, 4, 5),
                 skip=None):
    """First free slot for a label around (ax, ay) → (x, y, align, box).

    Slots are tried in `order` (right, left, above, below, right-below, left-below by default);
    a slot that would leave `plot_box` is clamped back inside it horizontally, and the first
    candidate that touches no box already placed (its own marker's box is `skip`) wins.  When
    every slot collides, the first allowed one is used anyway: a slightly crowded label beats
    a missing one.
    """
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


def _boxes_touch(a, b, pad=3.0):
    """Do two label boxes overlap (with a little breathing room)?"""
    return (a[2] + pad > b[0] and b[2] + pad > a[0]
            and a[3] + pad > b[1] and b[3] + pad > a[1])


def _error_bar(sh, cx, cy, half_px, box, horizontal=False):
    """Replicate bar through a standard point, with small caps (see ERROR_BAR_AXIS).

    The bar is clipped to the axes `box`: a bar longer than the axis must not run out of the
    plot and across the header (with a single low-mass standard point, `sd` can cover the whole
    y range).
    """
    if half_px <= 1.0:
        return
    cap = 5.0
    bx0, by0, bx1, by1 = box
    if horizontal:
        x0, x1 = max(cx - half_px, bx0 + 2.0), min(cx + half_px, bx1 - 2.0)
        if x1 - x0 <= 1.0:
            return
        _hrule(sh, x0, x1, cy, ERR_RGB, width=2)
        _vrule(sh, x0, cy - cap, cy + cap, ERR_RGB, width=2)
        _vrule(sh, x1, cy - cap, cy + cap, ERR_RGB, width=2)
    else:
        y0, y1 = max(cy - half_px, by0 + 2.0), min(cy + half_px, by1 - 2.0)
        if y1 - y0 <= 1.0:
            return
        _vrule(sh, cx, y0, y1, ERR_RGB, width=2)
        _hrule(sh, cx - cap, cx + cap, y0, ERR_RGB, width=2)
        _hrule(sh, cx - cap, cx + cap, y1, ERR_RGB, width=2)


def curve_layout(curve, quant=None, width=1600):
    """Geometry of the plot-curve canvas — shared by `plot_curve` and the pixel selftests.

    Returns a dict with `width`, `height`, `axes` (`CurveAxes`: `.x(volume)` / `.y(mass)` /
    `.yr(rel_pct)` / `.box` / `.resid_box`), `main_box`, `resid_box`, `x_range`, `y_range`,
    `rel_range`, `fitted` (`volume_min` / `volume_max` / the ±10 % `extension_min` /
    `extension_max`), `points` (every standard point with its pixel `x` / `y` and `used`),
    `dropped`, `residuals` (the ones actually drawn, each with `x` / `y` / `rel_pct`),
    `samples` (only with quant), `annotation` (newline-joined block) / `annotation_lines` /
    `annotation_box`, `title`, `legend`, `x_label`, `y_label`, `resid_label` and `font`.

    x range: 0 → the largest of `range.volume_max`, every point's volume and every quant band's
    area, × 1.08, but at least the fitted line's +10 % end (so the dashed tail always reaches
    the right edge; flagged to the lead as a deviation).  y range: 0 → the largest of
    `range.mass_max` and every point's mass, × 1.12.  The main axes take about 70 % of the
    height at the default width; the residual strip covers at least ±15 % and at least
    ±1.2 × the largest |rel_pct|.
    """
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
    # x range per the ticket: the largest of range.volume_max, every point's volume and every
    # quant band's area, × 1.08 — but never narrower than the fitted line's +10 % end, so the
    # drawn line always reaches the right edge of the frame instead of stopping short of it
    # (deviation flagged to the lead; the ticket's formula is otherwise applied unchanged).
    x_max = max(max([vmax] + volumes) * 1.08, ext_hi)
    y_max = max([y_hi] + masses + [0.0]) * 1.12
    rel_hi = max([15.0, 1.2 * max([abs(r["rel_pct"]) for r in residuals] + [0.0])])
    if x_max <= 0.0:
        x_max = 1.0
    if y_max <= 0.0:
        y_max = 1.0

    height = int(round(w * 0.75))
    unit = curve.get("unit") if isinstance(curve.get("unit"), str) and curve.get("unit") else "ug"
    baseline = curve.get("baseline") if isinstance(curve.get("baseline"), str) else "flank"
    n_used = sum(1 for p in points if p["used"])
    n_drop = len(dropped)
    font = _font()
    # every annotation field is user-supplied text reachable by `_ascii`: an apostrophe or a
    # non-ASCII byte in a unit would break the MVG text primitive (ruling R1)
    unit_txt = _ascii(unit, 18)
    base_txt = _ascii(baseline, 18)
    lines = ["fit: %s   r2: %s" % (fit, "-" if r2 is None else "%.4f" % r2),
             "n: %d %s%s   baseline: %s   unit: %s"
             % (n_used, "point" if n_used == 1 else "points",
                (" (%d dropped)" % n_drop) if n_drop else "", base_txt, unit_txt),
             eqn]
    try:
        m_lo = _trim_num("%.3f" % float(rng["mass_min"]))
        m_hi = _trim_num("%.3f" % float(rng["mass_max"]))
        span = m_lo if m_lo == m_hi else "%s - %s" % (m_lo, m_hi)
        lines.append("standard range: %s %s" % (span, unit_txt))
    except (KeyError, TypeError, ValueError):
        pass

    # vertical margins: the annotation block on top, the ticks / labels and the residual strip
    # below.  The label bands need a FIXED number of pixels (text does not scale), so at the
    # default width the main axes take about 70 % of the height and on a small canvas they take
    # proportionally less — shrinking the text would make the picture unreadable instead.
    bottom_h = max(68, int(round(0.062 * height)))       # x tick labels + the x axis label
    gap = max(22, int(round(0.028 * height)))            # room below the x axis frame
    resid_h = max(RESID_MIN, int(round(0.100 * height)))
    ann_w = max(_text_width(font, ANN_FONT, line) for line in lines) + 22
    ann_h = ANN_STEP * len(lines) + 12
    header_h = max(ann_h, 58)
    # the annotation block sits at the top right, beside the title/legend; on a canvas too
    # narrow for both it moves under the title rather than squeezing the title into an ellipsis
    beside = (w - CURVE_RIGHT_PAD - ann_w - MARGIN - 120) >= 300
    if beside:
        ann_box = (float(w - CURVE_RIGHT_PAD - ann_w), float(TOP_PAD),
                   float(w - CURVE_RIGHT_PAD), float(TOP_PAD + header_h))
        title_right = ann_box[0] - 20.0
        top_h = TOP_PAD + header_h + ANN_GAP
    else:
        # under the title AND the legend (both are drawn at fixed offsets from the top edge)
        ann_w = min(ann_w, w - 2 * MARGIN)
        ann_y = float(TOP_PAD + 74)
        ann_box = (float(MARGIN), ann_y, float(MARGIN + ann_w), ann_y + ann_h)
        title_right = float(w - CURVE_RIGHT_PAD)
        top_h = int(ann_y) + ann_h + ANN_GAP
    main_h = height - top_h - bottom_h - gap - resid_h
    if main_h < 0.55 * height:                # a small canvas: take the space back from the
        bottom_h = max(66, int(round(0.062 * height)))         # thin margins, keep the text
        gap = max(18, int(round(0.024 * height)))
        resid_h = max(60, int(round(0.10 * height)))
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

    # A residual range much wider than ±10 % (a bad standard curve: residuals of ±180 %) puts
    # the two guides a few pixels from the zero line, so one label per guide would print them
    # on top of each other.  The rules stay exactly where they belong; only the labelling
    # changes: `+/-10% band (dashed)` in the gap above the strip describes both at once.
    guide_gap = abs(axes.yr(0.0) - axes.yr(10.0))
    guide_note = None
    guide_note_at = None
    if guide_gap < GUIDE_LABEL_MIN:
        guide_note = "+/-10% band (dashed)"
        note_x = resid_box[2] - 8.0
        note_y = resid_box[1] - 8.0
        guide_note_at = (note_x, note_y, "right")

    mx0, my0, mx1, my1 = main_box
    samples = []
    for lane, bands in quants:
        no = int(lane["lane"])
        many = len(bands) > 1
        for band in bands:
            x = min(max(axes.x(band["area"]), mx0 + SAMPLE_HALF + 4.0), mx1 - SAMPLE_HALF - 4.0)
            idx = band["index"]
            if band["out"]:                   # no mass: the marker sits ON the x axis
                y = my1 - SAMPLE_HALF - 1.0
                label = ("L%d.%s out" % (no, idx) if idx is not None else "L%d out" % no)
            else:
                y = min(max(axes.y(band["mass"]), my0 + SAMPLE_HALF + 4.0), my1 - SAMPLE_HALF - 4.0)
                label = ("L%d.%s" % (no, idx) if many and idx is not None else "L%d" % no)
            samples.append({"lane": no, "band": idx, "area": band["area"],
                            "mass": band["mass"], "out": band["out"], "label": label,
                            "x": x, "y": y})

    source = curve.get("source") if isinstance(curve.get("source"), dict) else {}
    report = source.get("dens") if isinstance(source.get("dens"), str) else ""
    title = "ep-craft densitometry plot-curve"
    if report:
        title += "  -  " + _ascii(os.path.basename(report), 80)
    legend = "filled circle = standard (bar = replicate SD)  |  hollow circle = dropped"
    if samples:
        legend += "  |  square = sample  |  hollow square = out of range"
    return {"width": w, "height": height, "axes": axes, "main_box": main_box,
            "resid_box": resid_box, "x_range": (0.0, float(x_max)),
            "y_range": (0.0, float(y_max)), "rel_range": (-float(rel_hi), float(rel_hi)),
            "fitted": {"volume_min": vmin, "volume_max": vmax,
                       "extension_min": ext_lo, "extension_max": ext_hi,
                       # the drawn tails: the ±10 % ends, kept inside the frame (when the
                       # frame is wider than the extension — a far-out dropped point or a
                       # sample band — the line stops at its extension, exactly as §4.7 asks)
                       "drawn_min": max(ext_lo, 0.0),
                       "drawn_max": ext_hi},
            "fit": fit,
            "eval": fn,
            "equation": eqn,
            "points": out_points,
            "dropped": [p for p in out_points if not p["used"]],
            "residuals": out_res, "samples": samples,
            "gap": float(gap), "title_right": float(title_right),
            "guide_note": guide_note, "guide_note_at": guide_note_at,
            "annotation": "\n".join(lines), "annotation_lines": lines,
            "annotation_box": ann_box,
            "title": title, "legend": legend,
            "x_label": "area (gray*cm, %s)" % base_txt,
            "y_label": "mass (%s)" % unit_txt,
            "resid_label": "residual (%)",
            "font": font}


# ---------------------------------------------------------------- plot-curve: drawing

def _draw_curve_header(sh, geo):
    """Title and legend at the left, the framed annotation block at the top right."""
    font = geo["font"]
    ai_x0, ai_y0, ai_x1, ai_y1 = geo["annotation_box"]
    sh.rect(ai_x0, ai_y0, ai_x1, ai_y1, fill=(255, 255, 255),
            stroke=(170, 170, 170), width=1)
    room = int(geo["title_right"] - MARGIN)
    sh.text(MARGIN, MARGIN + 26,
            _clip_text(geo["title"], CURVE_TITLE_FONT, room, font),
            CURVE_TITLE_FONT, TEXT_RGB)
    sh.text(MARGIN, MARGIN + 52,
            _clip_text(geo["legend"], CURVE_SUB_FONT, room, font),
            CURVE_SUB_FONT, MUTED_RGB)
    for i, line in enumerate(geo["annotation_lines"]):
        text = _clip_text(line, ANN_FONT, int(ai_x1 - ai_x0 - 20.0), font)
        sh.text(ai_x1 - 10.0, ai_y0 + 19.0 + i * ANN_STEP, text, ANN_FONT, TEXT_RGB,
                align="right", font=font)


def _draw_curve_main(sh, geo):
    """Main axes: frame, ticks, the fitted line, replicate bars, points and their labels."""
    ax = geo["axes"]
    font = geo["font"]
    x0, y0, x1, y1 = geo["main_box"]
    sh.rect(x0, y0, x1, y1, fill=None, stroke=CURVE_FRAME_RGB, width=1)

    step = _nice_step(geo["y_range"][1], 6)
    digits = _tick_digits(step)
    sh.text(x0 - 10.0, y1 + 6.0, "0", CURVE_TICK_FONT, MUTED_RGB, align="right", font=font)
    for value in _ticks(0.0, geo["y_range"][1], 6):
        ty = ax.y(value)
        if y0 + 4.0 < ty < y1 - 2.0:
            _hrule(sh, x0 - 6.0, x0, ty, CURVE_FRAME_RGB, width=1)
            sh.text(x0 - 10.0, ty + 5.0, _eqnum(value, digits), CURVE_TICK_FONT, MUTED_RGB,
                    align="right", font=font)

    # the rotated y axis label lives in the left margin, clear of the tick labels
    label_x, label_y = MARGIN + 10.0, 0.5 * (y0 + y1)
    sh.translate(label_x, label_y)
    sh.rotate(-90)
    sh.text(0.0, 0.0, geo["y_label"], AXIS_FONT, TEXT_RGB, align="center", font=font)
    sh.rotate(90)
    sh.translate(-label_x, -label_y)

    # fitted line: dashed outside the standard range, solid and thicker inside it
    # ... and the ±10 % extensions dashed, with the dashes anchored so the tail really reaches
    # the right edge of the frame (a dash phase starting at the vertex may end on a gap)
    drawn = geo["fitted"]
    sh.polyline(_fit_segment(geo, drawn["volume_min"], drawn["volume_max"]), LINE_RGB, width=3)
    for v0, v1 in ((drawn["drawn_min"], drawn["volume_min"]),
                   (drawn["volume_max"], drawn["drawn_max"])):
        for run in _dash_runs(_fit_segment(geo, v0, v1)):
            if len(run) >= 2:
                sh.polyline(run, LINE_RGB, width=2)
    # the line itself is an obstacle for every marker label: a `dropped` tag printed across the
    # fitted line reads as a typo, and the marker labels are the only text that may move
    line_boxes = []
    span_min, span_max = drawn["drawn_min"], drawn["drawn_max"]
    for k in range(61):
        v = span_min + (span_max - span_min) * (k / 60.0)
        lx = min(max(ax.x(v), x0 + 1.0), x1 - 1.0)
        ly = min(max(ax.y(geo["eval"](v)), y0 + 1.0), y1 - 1.0)
        line_boxes.append((lx - 2.5, ly - 2.5, lx + 2.5, ly + 2.5))

    # replicate bars first, so the point's own circle sits on top of its bar
    for p in geo["points"]:
        sd = p.get("sd")
        if not p["used"] or p.get("n") in (None, 1) or sd is None:
            continue
        if ERROR_BAR_AXIS == "y":                      # literal §4.7 vertical form
            half = abs(ax.y(abs(sd)) - ax.y(0.0))
        else:                                          # the axis sd really belongs to
            half = abs(ax.x(abs(sd)) - ax.x(0.0))
        _error_bar(sh, p["x"], p["y"], half, geo["main_box"],
                   horizontal=(ERROR_BAR_AXIS == "x"))

    for p in geo["points"]:
        if p["used"]:
            sh.circle(p["x"], p["y"], POINT_R, fill=POINT_RGB, stroke=POINT_STROKE_RGB,
                      width=1)
        else:
            sh.circle(p["x"], p["y"], POINT_R, fill=(255, 255, 255), stroke=DROP_RGB,
                      width=2)

    # sample squares (with quant), then the labels around them and the dropped points
    for s in geo["samples"]:
        if s["out"]:
            sh.rect(s["x"] - SAMPLE_HALF, s["y"] - SAMPLE_HALF, s["x"] + SAMPLE_HALF,
                    s["y"] + SAMPLE_HALF, fill=(255, 255, 255), stroke=OOR_RGB, width=2)
        else:
            sh.rect(s["x"] - SAMPLE_HALF, s["y"] - SAMPLE_HALF, s["x"] + SAMPLE_HALF,
                    s["y"] + SAMPLE_HALF, fill=SAMPLE_RGB, stroke=SAMPLE_STROKE_RGB, width=1)

    point_boxes = [(p["x"] - POINT_R - 1.0, p["y"] - POINT_R - 1.0,
                    p["x"] + POINT_R + 1.0, p["y"] + POINT_R + 1.0) for p in geo["points"]]
    sample_boxes = [(s["x"] - SAMPLE_HALF - 1.0, s["y"] - SAMPLE_HALF - 1.0,
                     s["x"] + SAMPLE_HALF + 1.0, s["y"] + SAMPLE_HALF + 1.0)
                    for s in geo["samples"]]
    boxes = list(point_boxes) + list(sample_boxes) + line_boxes
    if geo["guide_note_at"] is not None:
        nx, ny, _na = geo["guide_note_at"]
        nw = _text_width(font, CURVE_TICK_FONT, geo["guide_note"])
        boxes.append((nx - nw, ny - CURVE_TICK_FONT * 0.8, nx, ny + CURVE_TICK_FONT * 0.25))
    for p, own in zip(geo["points"], point_boxes):
        if p["used"]:
            continue
        tx, ty, align, box = _place_label("dropped", MARK_FONT, p["x"], p["y"], boxes,
                                          font, geo["main_box"], skip=own)
        sh.text(tx, ty, "dropped", MARK_FONT, DROP_RGB, align=align, font=font)
        boxes.append(box)
    for s, own in zip(geo["samples"], sample_boxes):
        colour = OOR_RGB if s["out"] else SAMPLE_RGB
        # an out-of-range marker already sits on the axis: its label goes below the frame
        # first (the gap above the residual strip), then above it, then beside it
        order = (3, 2, 0, 1, 5, 4) if s["out"] else (0, 1, 2, 3, 4, 5)
        plot_box = geo["main_box"]
        if s["out"]:
            _x0, _y0, _x1, y1 = geo["main_box"]
            plot_box = (geo["main_box"][0], geo["main_box"][1], geo["main_box"][2],
                        y1 + geo["gap"])
        tx, ty, align, box = _place_label(s["label"], MARK_FONT, s["x"], s["y"], boxes,
                                          font, plot_box, order=order, skip=own)
        sh.text(tx, ty, s["label"], MARK_FONT, colour, align=align, font=font)
        boxes.append(box)


def _draw_curve_resid(sh, geo):
    """Residual strip: frame, zero line, ±10 % guides and one marker per used standard point."""
    ax = geo["axes"]
    font = geo["font"]
    x0, y0, x1, y1 = geo["resid_box"]
    sh.rect(x0, y0, x1, y1, fill=None, stroke=CURVE_FRAME_RGB, width=1)
    zero = ax.yr(0.0)
    _hrule(sh, x0 + 1.0, x1 - 1.0, zero, ZERO_RGB, width=2)
    for value in (-10.0, 10.0):
        gy = ax.yr(value)
        _hrule(sh, x0 + 1.0, x1 - 1.0, gy, GUIDE_RGB, width=1, dash="5 5")
        if geo["guide_note"] is None:
            sh.text(x0 - 10.0, gy + 5.0, "%+d%%" % int(value), CURVE_TICK_FONT, MUTED_RGB,
                    align="right", font=font)
    sh.text(x0 - 10.0, zero + 5.0, "0", CURVE_TICK_FONT, MUTED_RGB, align="right", font=font)
    if geo["guide_note_at"] is not None:
        # the two guides are too close to carry their own labels: name them once, in the gap
        # between the main axes and the strip (an empty band, reserved by the marker labels)
        nx, ny, na = geo["guide_note_at"]
        sh.text(nx, ny, geo["guide_note"], CURVE_TICK_FONT, MUTED_RGB, align=na, font=font)

    label_x, label_y = MARGIN + 10.0, 0.5 * (y0 + y1)
    sh.translate(label_x, label_y)
    sh.rotate(-90)
    sh.text(0.0, 0.0, geo["resid_label"], CURVE_TICK_FONT, TEXT_RGB, align="center", font=font)
    sh.rotate(90)
    sh.translate(-label_x, -label_y)

    for res in geo["residuals"]:
        _vrule(sh, res["x"], min(zero, res["y"]), max(zero, res["y"]), ERR_RGB, width=1)
        sh.circle(res["x"], res["y"], RESID_R, fill=LINE_RGB, stroke=None)


def _draw_curve_xaxis(sh, geo):
    """The shared x axis under the residual strip: ticks, numeric labels and the axis label."""
    ax = geo["axes"]
    font = geo["font"]
    x0, _y0, x1, y1 = geo["resid_box"]
    step = _nice_step(geo["x_range"][1], 6)
    digits = _tick_digits(step)
    for value in _ticks(0.0, geo["x_range"][1], 6):
        tx = ax.x(value)
        if x0 + 2.0 < tx < x1 - 2.0:                 # a tick mark stays inside the frame
            _vrule(sh, tx, y1 + 1.0, y1 + 7.0, CURVE_FRAME_RGB, width=1)
        if x0 - 4.0 <= tx <= x1 + 4.0:               # its number may sit at the corner
            sh.text(tx, y1 + 27.0, _eqnum(value, digits), CURVE_TICK_FONT, MUTED_RGB,
                    align="center", font=font)
    sh.text(0.5 * (x0 + x1), y1 + 58.0, geo["x_label"], AXIS_FONT, TEXT_RGB,
            align="center", font=font)


def plot_curve(curve, out, quant=None, width=1600):
    """Render curve.json — and quant.json's sample markers when given — as a PNG (§4.7).

    `out` must be a `.png` path whose directory exists (ruling R12); `quant=None` draws the
    standard curve alone.  Returns the pixel size that was written, which is also
    `curve_layout`'s `width` / `height`.  The PNG is written with `-strip` so two renders of
    the same inputs are byte-identical (the determinism check in the selftests needs that).
    """
    target = _check_out(out)
    geo = curve_layout(curve, quant=quant, width=width)
    sh = Script()
    _draw_curve_header(sh, geo)
    _draw_curve_main(sh, geo)
    _draw_curve_resid(sh, geo)
    _draw_curve_xaxis(sh, geo)
    _render(sh.render(), geo["width"], geo["height"], target, strip=True)
    return geo["width"], geo["height"]
