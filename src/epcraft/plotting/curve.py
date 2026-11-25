"""
curve — plot-curve: the standard curve, residuals and samples (spec §4.5/§4.7).

``plot_curve(curve, out, quant=None, width=1020)``
    The standard curve (and optional sample markers) with a residual strip that shares the
    area axis.  Replicate bars are horizontal on the area axis because ``curve.points[].sd``
    is the sample SD of replicate areas. Out-of-range samples are marked on the dashed
    extrapolation at their measured areas.

The renderer is importable without matplotlib: the Figure classes are loaded lazily by
``plot_curve`` itself, and ``curve_layout`` only computes geometry.  Output validation and
the one-per-lane ``LaneLabelWarning`` for ``quant`` samples are the contracts described in
:mod:`epcraft.plotting.render`.

"""

import math
import re
import warnings

from .. import densitometry

from .render import (ANN_FONT, ANN_STEP, AXIS_FONT, CURVE_FRAME_RGB,
                    CURVE_LEGEND_FONT, CURVE_RIGHT_PAD, CURVE_SUB_FONT,
                    CURVE_TICK_FONT, CURVE_TITLE_FONT, CurveAxes, DPI, DROP_RGB,
                    ERROR_BAR_AXIS, ERR_BAR_MIN_PX, ERR_RGB, GUIDE_LABEL_MIN,
                    GUIDE_RGB, LEFT_PAD, LINE_RGB, LaneLabelWarning, MARK_FONT,
                    MUTED_RGB, OOR_RGB, POINT_R, POINT_RGB, POINT_STROKE_RGB,
                    RESID_R, SAMPLE_HALF, SAMPLE_RGB, SAMPLE_STROKE_RGB,
                    TEXT_RGB, TOP_PAD,
                    ZERO_RGB, _check_out, _eqnum, _fig_rect, _fig_text, _font,
                    _font_prop, _is_english_label, _load_mpl, _mcolor, _nice_step,
                    _safe_ascii, _save_png, _text_width, _ticks, _trim_num,
                    _unit_text)

# ------------------------------------------------------------- plot-curve (spec §4.5)


CURVE_KIND = getattr(densitometry, "CURVE_KIND", "ep-craft-densitometry-curve")
QUANT_KIND = getattr(densitometry, "QUANT_KIND", "ep-craft-densitometry-quant")
CURVE_SUFFIX = getattr(densitometry, "CURVE_SUFFIX", ".curve.json")


# Same tolerance the quantifier uses to decide below/above, so the picture cannot disagree
# with the numbers in quant.json.
FUZZ = getattr(densitometry, "FUZZ", 1e-9)


def _tick_digits(step):
    """Decimals a tick label needs for `step`."""
    if step <= 0:
        return 0
    return max(0, min(6, 2 - int(math.floor(math.log10(step)))))


def _coefficient_text(value, standard_error):
    """Format a fitted coefficient and its optional standard error."""
    if isinstance(standard_error, (int, float)) and not isinstance(standard_error, bool):
        if math.isfinite(standard_error) and standard_error >= 0.0:
            return f"({value:.4g} ± {standard_error:.4g})"
    return _trim_num(f"{value:.2f}")


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

            slope_term = _coefficient_text(slope, fp.get("slope_sd"))
            intercept_term = _coefficient_text(abs(intercept), fp.get("intercept_sd"))
            sign = "-" if intercept < 0.0 else "+"
            eqn = f"mass = {slope_term} · area {sign} {intercept_term}"
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
            if not math.isfinite(area):
                raise RuntimeError("quant 的泳道 %d 有一条带的 area 不是有限数" % no)
            mass = band.get("mass")
            if mass is not None:
                try:
                    mass = float(mass)
                except (TypeError, ValueError):
                    raise RuntimeError("quant 的泳道 %d 有一条带的 mass 不是数字（收到 %r）"
                                       % (no, mass))
            # `mass_sd` is the OLS prediction SD from quantify (ep-c1i).  It is read here as a
            # raw value: absence is normal (older quant.json has no uncertainty) and the one
            # predicate `_drawable_sd` decides whether a drawable bar can come from it.
            mass_sd = band.get("mass_sd")
            oor = bool(band.get("out_of_range"))
            out_band = oor or mass is None
            bands.append({"index": band.get("index"), "area": area, "mass": mass,
                          "mass_sd": mass_sd, "out": out_band})
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


def _drawable_sd(value):
    """The sample uncertainty a bar can be drawn from, or None.

    THE one predicate for sample y error bars: the renderer, the native legend entry and the
    layout bookkeeping all call it, so a legend entry can never advertise a bar that is not
    on the picture.  A raw ``mass_sd`` may be absent (older quant.json), null (no mass, no
    residual scatter) or zero; none of those is an error, and none of them yields a bar.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        sd = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sd) or sd <= 0.0:
        return None
    return sd


def _sample_bar(axes, sample, box):
    """(centre mass, sd) of a drawable ±SD y bar for one sample; None when none is drawn.

    THE shared predicate for sample uncertainty bars: the renderer and the native legend
    entry both call it, so a key line can never advertise a bar that is not on the picture.
    The bar stays centred on its marker and is clipped by the axes.
    """
    if sample["out"]:
        return None
    sd = _drawable_sd(sample.get("mass_sd"))
    if sd is None:
        return None
    centre_px = float(sample["y"])
    centre_mass = sample["mass"]
    # A linear mass axis maps a ±sd bar to ±|y(sd) - y(0)| pixels around the marker.
    half = abs(axes.y(centre_mass + sd) - centre_px)
    if _bar_extent(centre_px, half, box, horizontal=False) is None:
        return None
    return centre_mass, sd


def curve_layout(curve, quant=None, width=1020):
    """Geometry of the plot-curve canvas — shared by `plot_curve` and its tests."""
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

    samples = []
    for lane, bands in quants:
        no = int(lane["lane"])
        for band in bands:
            area, idx = band["area"], band["index"]
            outside = area < vmin - FUZZ or area > vmax + FUZZ
            fit_only = outside or band["out"]
            mass = fn(area) if fit_only else band["mass"]
            if not math.isfinite(mass):
                raise RuntimeError("quant 的泳道 %d 有一条带的绘图质量不是有限数" % no)
            label = "L%d.%s" % (no, idx) if len(bands) > 1 and idx is not None else "L%d" % no
            samples.append({"lane": no, "band": idx, "area": area, "mass": mass,
                            "mass_sd": band["mass_sd"], "out": fit_only,
                            "outside": outside, "label": label})

    areas = [p["volume"] for p in points] + [s["area"] for s in samples]
    area_min, area_max = min(areas), max(areas)
    x_pad = 0.10 * ((area_max - area_min) or (vmax - vmin))
    x_range = (area_min - x_pad, area_max + x_pad)
    drawn_lo, drawn_hi = x_range
    if fit == "power":
        drawn_lo = max(0.0, drawn_lo)
    masses = [p["mass"] for p in points] + [s["mass"] for s in samples]
    fit_ends = [fn(drawn_lo), fn(drawn_hi)]
    y_lo, y_hi = min(masses + fit_ends), max(masses + fit_ends)
    y_pad = 0.10 * (y_hi - y_lo or max(abs(y_lo), 1.0))
    y_range = (y_lo - y_pad, y_hi + y_pad)
    rel_hi = max([15.0, 1.2 * max([abs(r["rel_pct"]) for r in residuals] + [0.0])])
    rel_range = (-rel_hi, rel_hi)

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
    if "±" in eqn:
        details.append("Coefficients ± SE")
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

    # Canvas height depends only on the requested width.
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
    axes = CurveAxes(main_box, resid_box, x_range, y_range, rel_range)
    for sample in samples:
        sample["x"] = axes.x(sample["area"])
        sample["y"] = axes.y(sample["mass"])

    out_points = [dict(p, x=axes.x(p["volume"]), y=axes.y(p["mass"])) for p in points]
    out_res = [dict(r, x=axes.x(r["volume"]), y=axes.yr(r["rel_pct"])) for r in residuals]

    guide_gap = abs(axes.yr(0.0) - axes.yr(10.0))
    guide_note = None
    guide_note_at = None
    if guide_gap < GUIDE_LABEL_MIN:
        guide_note = "±10% guides (dashed)"
        note_x = resid_box[2] - 6.0
        # Top-anchored in the reserved gutter above the residual strip.
        note_y = resid_box[1] - gap + 4.0
        guide_note_at = (note_x, note_y, "right")

    title = "Standard curve"
    bars = any(_has_error_bar(axes, p, main_box) for p in out_points)
    # Plain metadata key for callers/tests: the visible key is the native graphical legend
    # built from the real artists in _draw_curve_main (never this prose line).
    legend_parts = ["standard"]
    if dropped:
        legend_parts.append("dropped")
    if any(not s["out"] for s in samples):
        legend_parts.append("sample")
    if any(s["outside"] for s in samples):
        legend_parts.append("extrapolated sample")
    if any(s["out"] and not s["outside"] for s in samples):
        legend_parts.append("unquantified sample")
    if bars:
        legend_parts.append("replicate area SD")
    legend = " · ".join(legend_parts)
    return {"width": w, "height": height, "axes": axes, "main_box": main_box,
            "resid_box": resid_box, "x_range": x_range, "y_range": y_range, "rel_range": rel_range,
            "fitted": {"volume_min": vmin, "volume_max": vmax,
                       "extension_min": drawn_lo, "extension_max": drawn_hi,
                       "drawn_min": drawn_lo, "drawn_max": drawn_hi},
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


# --------------------------------------------------------------- plot-curve: drawing


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


def _bar_handle():
    """A legend proxy for the replicate SD bars: a short horizontal grey bar glyph."""
    from matplotlib.lines import Line2D
    return Line2D([], [], color=_mcolor(ERR_RGB), marker="_", linestyle="none",
                  markersize=12.0, markeredgewidth=1.7)


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
        size = ANN_FONT
        if i == 0:
            size *= min(1.0, room / _text_width(font, ANN_FONT, line))
        _fig_text(fig, LEFT_PAD, geo["footer_y"] + i * ANN_STEP, line, size, color,
                  va="top", font=font, clip_width=room)


def _draw_curve_fit(ax, geo):
    """The fitted line: solid over the standard range, dashed through extrapolated samples."""
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
        if style == "--":
            # Include the marked areas as vertices so a power fit crosses each marker exactly.
            xs = sorted(set(xs + [s["area"] for s in geo["samples"]
                                  if s["outside"] and start <= s["area"] <= stop]))
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
    lo, hi = geo["y_range"]
    digits = _tick_digits(_nice_step(hi - lo, 6))
    yticks = [v for v in _ticks(lo, hi, 6) if lo <= v <= hi]
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

    # Sample uncertainty: a vertical ±mass_sd bar per in-range sample, with caps and clipped
    # to the main axes.  _sample_bar is THE shared predicate — the native key entry below is
    # appended only when this loop kept one real bar artist, so the key cannot claim a bar
    # that is not drawn.
    sd_handle = None
    for s in geo["samples"]:
        bar = _sample_bar(geo["axes"], s, geo["main_box"])
        if bar is None:
            continue
        centre_mass, sd = bar
        try:
            container = ax.errorbar([s["area"]], [centre_mass], yerr=[[sd]],
                                    fmt="none", ecolor=_mcolor(ERR_RGB), elinewidth=1.4,
                                    capsize=3.6, capthick=1.4, zorder=4)
        except (TypeError, ValueError):
            continue
        sd_handle = sd_handle or container

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

    sample_handles = {}
    for s in geo["samples"]:
        kind = ("extrapolated sample" if s["outside"] else
                "unquantified sample" if s["out"] else "sample")
        handle, = ax.plot([s["area"]], [s["mass"]], marker="s",
                          markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                          markerfacecolor="white" if s["out"] else _mcolor(SAMPLE_RGB),
                          markeredgecolor=_mcolor(OOR_RGB if s["out"] else SAMPLE_STROKE_RGB),
                          markeredgewidth=1.8 if s["out"] else 0.8, zorder=5,
                          clip_on=False)
        sample_handles.setdefault(kind, handle)

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

    for s, own in zip(geo["samples"], sample_boxes):
        tx, ty, align, box = _place_label(s["label"], MARK_FONT, s["x"], s["y"], boxes,
                                          font, geo["main_box"], skip=own)
        _fig_text(fig, tx, ty, s["label"], MARK_FONT, OOR_RGB if s["out"] else SAMPLE_RGB,
                  ha=align, font=font)
        boxes.append(box)

    # Native graphical key: real marker artists (or a bar glyph for the SD bars), placed
    # after every label so collision checking sees the finished plot.  Every entry is
    # appended only when the real artist it stands for was actually drawn.
    handles, labels = [], []
    for handle, label in ((used_handle, "standard"), (dropped_handle, "dropped")):
        if handle is not None:
            handles.append(handle)
            labels.append(label)
    for label, handle in sample_handles.items():
        handles.append(handle)
        labels.append(label)
    if has_bars:
        handles.append(_bar_handle())
        labels.append("replicate area SD")
    if sd_handle is not None:
        handles.append(sd_handle)
        labels.append("±1 SD fit")
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
    step = _nice_step(geo["x_range"][1] - geo["x_range"][0], 6)
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
