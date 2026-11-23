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

Shared plumbing (ticket ep-bqv.4, plot-curve, builds on the same pieces):
  `_check_out(out)`            — validate an output PNG path (ruling R12) → absolute path
  `_font()`                    — text_layout.pick_font() or "Arial"
  `_render(script, w, h, out)` — write the MVG script, run magick, always delete the script
  `Script`                     — tiny MVG builder (polyline / polygon / rule / text / circle…)
  `Mapping`                    — value rectangle → pixel box (x right, y down or up)
  `Panel`                      — one lane panel: `x(gray)`, `y(y_cm)`, `.box`
  `band_layout(...)`           — the panel grid (boxes, row/column count, image size) that the
                                 renderer and the pixel selftest share, so a test computes
                                 pixel positions without duplicating layout arithmetic.

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


def _render(script, w, h, out):
    """Draw one MVG script into `out` (a PNG path already through `_check_out`).

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
        magick_io._run([magick_io.MAGICK, "-size", "%dx%d" % (w, h), "xc:white",
                        "-font", font, "-draw", "@" + path, "-depth", "8", out])
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
