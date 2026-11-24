"""
bands — plot-bands: local band-peak traces of a dens.json (spec §4.7).

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

The renderer is importable without matplotlib: the Figure classes are loaded lazily by
``plot_bands`` itself, and ``band_layout`` only computes geometry.  Output validation (a
``.png`` path in an existing directory) and the one-per-lane ``LaneLabelWarning`` are the
contracts described in :mod:`epcraft.plotting.render`.

"""

import math
import numbers
import warnings

from .. import densitometry

from .render import (BAND_CONTEXT_MAX, BAND_CONTEXT_MIN, COL_GAP, DPI, FLANK_RGB,
                    GAP_ROWS, GLOBAL_RGB, GRID_RGB, GUTTER, HEADER_FONT,
                    LANE_PALETTE, LEGEND_FONT, LEGEND_HEADER_H, LEGEND_LINE_H,
                    LEGEND_PAD, LaneLabelWarning, MARGIN, MUTED_RGB, PARAMS_FONT,
                    RIGHT_PAD, SEQUENTIAL_RGB, SUB_GAP, TEXT_RGB, TILE_HEADER_H,
                    TITLE_FONT, TITLE_H, TRACE_AXIS_FONT, TRACE_MIN_WIDTH,
                    TRACE_PANEL_FONT, TRACE_TICK_FONT, TraceAxes, XLABEL_H,
                    ZERO_RGB, _check_out, _display_label, _eqnum, _fig_rect,
                    _fig_text, _font, _font_prop, _is_english_label, _load_mpl,
                    _mcolor, _safe_ascii, _save_png, _text_width, _ticks, _trim_num)

BASELINES = ("flank", "global", "both")
KIND = densitometry.KIND


# ------------------------------------------------------------- dens validation / lanes


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


# ---------------------------------------------------------- plot-bands: traces / layout


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


# --------------------------------------------------------------- plot-bands: drawing


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
