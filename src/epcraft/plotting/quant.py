"""
quant — plot-quant: sample series quantification & ratio-regression plot.

``plot_quant(quant, out, width=1020, cols=2)``

Draws one panel per sample series in ``quant.json``:
- For multi-ratio dilution series (``est.method == "ratio"``): ``C_equiv`` vs
  ``sample_standard_ratio`` with per-member ``±equiv_sd`` error bars, hollow orange
  markers for dropped lanes, the through-origin regression line ``C_equiv = conc * ratio``,
  and the fitted formula + final concentration with ``±`` errors.
- For equal-volume replicates or single-lane series (``"mean"`` / ``"single"``):
  per-lane converted concentration with ``±conc_sd`` error bars, merged weighted-mean line
  and ``±SEM`` band (for ``n_used >= 2``), and the formula + final concentration with ``±``.
"""

import math
import warnings

from .. import densitometry
from .curve import _add_curve_legend, _bar_handle, _place_label, _style_curve_axes, _tick_digits
from .render import (
    ANN_FONT,
    ANN_STEP,
    AXIS_FONT,
    CURVE_LEGEND_FONT,
    CURVE_RIGHT_PAD,
    CURVE_SUB_FONT,
    CURVE_TICK_FONT,
    CURVE_TITLE_FONT,
    DPI,
    DROP_RGB,
    ERR_RGB,
    GUIDE_RGB,
    LEFT_PAD,
    LINE_RGB,
    MARK_FONT,
    MUTED_RGB,
    Mapping,
    OOR_RGB,
    SAMPLE_HALF,
    SAMPLE_RGB,
    SAMPLE_STROKE_RGB,
    TEXT_RGB,
    TOP_PAD,
    ZERO_RGB,
    LaneLabelWarning,
    _check_out,
    _eqnum,
    _fig_rect,
    _fig_text,
    _font_prop,
    _is_english_label,
    _load_mpl,
    _mcolor,
    _nice_step,
    _safe_ascii,
    _save_png,
    _text_width,
    _ticks,
    _trim_num,
    _unit_text,
)

QUANT_KIND = getattr(densitometry, "QUANT_KIND", "ep-craft-densitometry-quant")


def _fmt_val(v, digits=3):
    """Format a positive or signed finite number compactly."""
    if v is None or not math.isfinite(float(v)):
        return "—"
    v = float(v)
    if v == 0.0:
        return "0"
    if abs(v) >= 100.0:
        return _trim_num(f"{v:.1f}")
    if abs(v) >= 1.0:
        return _trim_num(f"{v:.{digits}f}")
    return _trim_num(f"{v:.{max(digits, 3)}f}")


def _fmt_sd(v):
    """Format an uncertainty / SD / SEM with at least 3 decimals when small."""
    if v is None or not math.isfinite(float(v)):
        return None
    v = float(v)
    if v == 0.0:
        return "0"
    if abs(v) >= 1.0:
        return _trim_num(f"{v:.3f}")
    if abs(v) >= 0.01:
        return _trim_num(f"{v:.3f}")
    return _trim_num(f"{v:.4f}")


def _member_label(member, multi_band_lanes):
    no = int(member["lane"])
    band = member.get("band")
    if no in multi_band_lanes and band is not None:
        return f"L{no}.{band}"
    return f"L{no}"


def _extract_series(quant):
    """Normalise quant['samples'] (or legacy quant['lanes']) into a list of panel dicts."""
    if not isinstance(quant, dict) or quant.get("kind") != QUANT_KIND:
        raise RuntimeError(
            "不是 %s 文件（kind=%r）"
            % (QUANT_KIND, quant.get("kind") if isinstance(quant, dict) else None)
        )

    raw_samples = quant.get("samples")
    if isinstance(raw_samples, list) and raw_samples:
        out = []
        for idx, s in enumerate(raw_samples):
            if not isinstance(s, dict):
                raise RuntimeError("quant 的 samples 里有不是对象的条目")
            raw_name = s.get("name") or f"Sample {idx + 1}"
            members = s.get("members") or []
            if not isinstance(members, list):
                raise RuntimeError("quant 的 sample %r 缺少 members 列表" % (raw_name,))
            out.append({
                "raw_name": str(raw_name),
                "est": s.get("est") if isinstance(s.get("est"), dict) else None,
                "members": [dict(m) for m in members if isinstance(m, dict)],
            })
        return out

    raw_lanes = quant.get("lanes")
    if isinstance(raw_lanes, list) and raw_lanes:
        out = []
        for lane in raw_lanes:
            if not isinstance(lane, dict):
                continue
            no = int(lane["lane"])
            raw_name = lane.get("label") or f"Lane {no}"
            bands = [b for b in (lane.get("bands") or []) if isinstance(b, dict)]
            if not bands:
                continue
            members = []
            for b in bands:
                mass = b.get("mass")
                mass_sd = b.get("mass_sd")
                oor = bool(b.get("out_of_range")) or mass is None
                members.append({
                    "lane": no,
                    "band": b.get("index"),
                    "ratio": 1.0,
                    "area": b.get("area"),
                    "equiv": mass,
                    "equiv_sd": mass_sd,
                    "conc": mass,
                    "conc_sd": mass_sd,
                    "out_of_range": oor,
                    "used": not oor,
                })
            active = [m for m in members if m["used"] and m["conc"] is not None]
            est = None
            if len(active) == 1:
                est = {
                    "method": "single",
                    "conc": active[0]["conc"],
                    "conc_sd": None,
                    "sem": None,
                    "common_pct": None,
                    "n": len(members),
                    "n_used": 1,
                }
            elif len(active) >= 2:
                vals = [float(m["conc"]) for m in active]
                mean_c = sum(vals) / len(vals)
                var = sum((v - mean_c) ** 2 for v in vals) / (len(vals) - 1)
                sd = math.sqrt(max(0.0, var))
                sem = sd / math.sqrt(len(vals))
                est = {
                    "method": "mean",
                    "conc": mean_c,
                    "conc_sd": sem,
                    "sem": sem,
                    "common_pct": None,
                    "n": len(members),
                    "n_used": len(active),
                    "mean": {"conc": mean_c, "sd": sd, "sem": sem},
                }
            out.append({
                "raw_name": str(raw_name),
                "est": est,
                "members": members,
            })
        if out:
            return out

    raise RuntimeError("quant 里没有可绘制的样品（samples / lanes 为空）")


def _panel_summary_lines(panel, unit_str):
    """Build the two footer lines (formula with ±, and final concentration with ±)."""
    est = panel["est"]
    unit_sfx = f" {unit_str}" if unit_str else ""
    if est is None:
        return [
            "Formula: no valid estimate (out of range or all lanes dropped)",
            "Conc = —",
        ]

    method = est.get("method")
    conc = est.get("conc")
    sem = est.get("sem")
    common_pct = est.get("common_pct")
    n_used = est.get("n_used", 0)
    n_tot = est.get("n", n_used)
    common_str = (
        f" · common ±{_trim_num(f'{float(common_pct):.2f}')}%"
        if common_pct is not None and math.isfinite(float(common_pct))
        else ""
    )

    if method == "ratio":
        rinfo = est.get("ratio") or {}
        slope_sd = rinfo.get("slope_sd") if rinfo.get("slope_sd") is not None else est.get("conc_sd")
        r2 = rinfo.get("r2")
        r2_str = f" · r² = {float(r2):.4f}" if r2 is not None and math.isfinite(float(r2)) else ""
        nl_str = " · nonlinear warning" if rinfo.get("nonlinear") else ""
        if slope_sd is not None and math.isfinite(float(slope_sd)):
            formula = f"C_equiv = ({_fmt_val(conc)} ± {_fmt_sd(slope_sd)}) · ratio{r2_str}"
        else:
            formula = f"C_equiv = {_fmt_val(conc)} · ratio{r2_str}"
        if sem is not None and math.isfinite(float(sem)):
            result = (
                f"Conc = {_fmt_val(conc)} ± {_fmt_sd(sem)}{unit_sfx} "
                f"(ratio WLS, n={n_used}/{n_tot}){common_str}{nl_str}"
            )
        else:
            result = f"Conc = {_fmt_val(conc)}{unit_sfx} (ratio WLS, n={n_used}/{n_tot}){common_str}{nl_str}"
        return [formula, result]

    if method == "mean":
        minfo = est.get("mean") or {}
        sd = minfo.get("sd")
        sd_str = f", SD = ±{_fmt_sd(sd)}{unit_sfx}" if sd is not None and math.isfinite(float(sd)) else ""
        if sem is not None and math.isfinite(float(sem)):
            formula = f"Weighted mean: Conc = {_fmt_val(conc)} ± {_fmt_sd(sem)}{unit_sfx}{sd_str}"
            result = (
                f"Conc = {_fmt_val(conc)} ± {_fmt_sd(sem)}{unit_sfx} "
                f"(SEM, n={n_used}/{n_tot}){common_str}"
            )
        else:
            formula = f"Weighted mean: Conc = {_fmt_val(conc)}{unit_sfx}{sd_str}"
            result = f"Conc = {_fmt_val(conc)}{unit_sfx} (n={n_used}/{n_tot}){common_str}"
        return [formula, result]

    # single lane
    active = [m for m in panel["members"] if m.get("used", True) is not False and m.get("conc") is not None]
    m_sd = active[0].get("conc_sd") if active else None
    if m_sd is not None and math.isfinite(float(m_sd)):
        formula = f"Single lane: Conc = C_equiv / ratio = {_fmt_val(conc)} ± {_fmt_sd(m_sd)}{unit_sfx} (curve ±1 SD)"
    else:
        formula = f"Single lane: Conc = C_equiv / ratio = {_fmt_val(conc)}{unit_sfx}"
    result = f"Conc = {_fmt_val(conc)}{unit_sfx} (single, n={n_used}/{n_tot}){common_str}"
    return [formula, result]


def quant_layout(quant, width=1020, cols=2):
    """Compute geometry and panel data for `plot_quant`."""
    if not isinstance(width, int) or isinstance(width, bool) or width < 400:
        raise ValueError("width 必须是 >= 400 的整数（收到 %r）" % (width,))
    if not isinstance(cols, int) or isinstance(cols, bool) or cols < 1:
        raise ValueError("cols 必须是 >= 1 的整数（收到 %r）" % (cols,))

    series_list = _extract_series(quant)
    n_panels = len(series_list)
    eff_cols = min(cols, n_panels)
    rows = math.ceil(n_panels / eff_cols)

    unit_str = _unit_text(quant.get("unit") or "")
    baseline = quant.get("baseline") or "flank"
    fit = quant.get("fit") or "linear"
    load_cv = quant.get("load_cv_pct")
    sub_parts = [
        f"{str(baseline).capitalize()} baseline",
        f"{str(fit).capitalize()} standard curve",
    ]
    if unit_str:
        sub_parts.append(f"Unit: {unit_str}")
    if load_cv is not None and math.isfinite(float(load_cv)) and float(load_cv) > 0:
        sub_parts.append(f"Load CV: ±{_trim_num(f'{float(load_cv):.2f}')}%")
    subtitle = " · ".join(sub_parts)

    scale = 1.0 if eff_cols == 1 else max(0.76, min(0.92, width / (eff_cols * 540.0)))
    title_font = int(round(CURVE_TITLE_FONT * (1.0 if eff_cols == 1 else 0.86)))
    sub_font = int(round(CURVE_SUB_FONT * scale))
    header_font = int(round(24 * scale))
    axis_font = int(round(AXIS_FONT * scale))
    tick_font = int(round(CURVE_TICK_FONT * scale))
    ann_font = int(round(ANN_FONT * scale))
    mark_font = int(round(MARK_FONT * scale))
    legend_font = int(round(CURVE_LEGEND_FONT * scale))
    ann_step = int(round(ANN_STEP * scale))

    header_y = TOP_PAD
    sub_y = header_y + title_font + 8
    grid_top = sub_y + sub_font + 22

    left_pad = LEFT_PAD if eff_cols == 1 else int(round(92 * scale + 16))
    right_pad = CURVE_RIGHT_PAD if eff_cols == 1 else 28
    col_gap = 64 if eff_cols > 1 else 0
    panel_w = (width - left_pad - right_pad - col_gap * (eff_cols - 1)) / float(eff_cols)
    plot_h = int(round(min(440.0, max(260.0, panel_w * 0.50))))
    panel_title_h = header_font + 10
    xlabel_h = axis_font + 28
    footer_h = ann_step * 2 + 12
    row_gap = 28
    row_h = panel_title_h + plot_h + xlabel_h + footer_h
    height = int(round(grid_top + rows * row_h + max(0, rows - 1) * row_gap + 16))

    panels = []
    for idx, s in enumerate(series_list):
        r = idx // eff_cols
        c = idx % eff_cols
        px0 = left_pad + c * (panel_w + col_gap)
        px1 = px0 + panel_w
        py_top = grid_top + r * (row_h + row_gap)
        box = (px0, py_top + panel_title_h, px1, py_top + panel_title_h + plot_h)
        footer_y = box[3] + xlabel_h + 6

        raw_name = s["raw_name"]
        safe_name = _safe_ascii(raw_name, 36) if not _is_english_label(raw_name) else raw_name.strip()
        lane_counts = {}
        for m in s["members"]:
            no = int(m["lane"])
            lane_counts[no] = lane_counts.get(no, 0) + 1
        multi_band_lanes = {no for no, cnt in lane_counts.items() if cnt > 1}

        est = s["est"]
        method = est.get("method") if est else None
        mode = "ratio" if method == "ratio" else "replicate"

        plotted_members = []
        for m in s["members"]:
            no = int(m["lane"])
            ratio = float(m.get("ratio", 1.0))
            equiv = m.get("equiv")
            equiv_sd = m.get("equiv_sd")
            conc = m.get("conc")
            conc_sd = m.get("conc_sd")
            oor = bool(m.get("out_of_range")) or (equiv is None if mode == "ratio" else conc is None)
            dropped = m.get("used") is False
            plotted_members.append({
                "lane": no,
                "band": m.get("band"),
                "label": _member_label(m, multi_band_lanes),
                "ratio": ratio,
                "equiv": None if equiv is None else float(equiv),
                "equiv_sd": None if equiv_sd is None else float(equiv_sd),
                "conc": None if conc is None else float(conc),
                "conc_sd": None if conc_sd is None else float(conc_sd),
                "out": oor,
                "dropped": dropped,
            })

        if mode == "ratio":
            ratios = [m["ratio"] for m in plotted_members]
            max_r = max(ratios) if ratios else 1.0
            x_pad = max(0.15, 0.12 * max_r)
            x_range = (-0.06 * max_r, max_r + x_pad)
            y_vals = [0.0]
            slope = float(est["conc"]) if est and est.get("conc") is not None else None
            slope_sd = None
            if est:
                rinfo = est.get("ratio") or {}
                slope_sd = rinfo.get("slope_sd") if rinfo.get("slope_sd") is not None else est.get("conc_sd")
            if slope is not None:
                y_vals.append(slope * (max_r + 0.08 * max_r))
            for m in plotted_members:
                if m["equiv"] is not None:
                    sd = m["equiv_sd"] or 0.0
                    y_vals.extend([m["equiv"] - sd, m["equiv"] + sd])
            y_lo, y_hi = min(y_vals), max(y_vals)
            span = (y_hi - y_lo) or 1.0
            y_range = (min(0.0, y_lo - 0.08 * span), y_hi + 0.14 * span)
            x_label = "Sample / standard ratio"
            y_label = f"Curve equivalent ({unit_str})" if unit_str else "Curve equivalent"
        else:
            plottable = [m for m in plotted_members if m["conc"] is not None]
            n_m = max(1, len(plottable))
            x_range = (-0.6, (n_m - 1) + 0.6)
            y_vals = []
            for m in plottable:
                sd = m["conc_sd"] or 0.0
                y_vals.extend([m["conc"] - sd, m["conc"] + sd])
            if est and est.get("conc") is not None:
                c_est = float(est["conc"])
                sem_est = float(est["sem"]) if est.get("sem") is not None else 0.0
                y_vals.extend([c_est - sem_est, c_est + sem_est])
            if not y_vals:
                y_vals = [0.0, 1.0]
            y_lo, y_hi = min(y_vals), max(y_vals)
            span = (y_hi - y_lo) or max(abs(y_hi), 1.0)
            y_range = (min(0.0, y_lo - 0.10 * span) if y_lo >= 0 else y_lo - 0.12 * span,
                       y_hi + 0.16 * span)
            x_label = "Lane"
            y_label = f"Concentration ({unit_str})" if unit_str else "Concentration"

        panels.append({
            "raw_name": raw_name,
            "name": safe_name,
            "mode": mode,
            "method": method,
            "est": est,
            "members": plotted_members,
            "box": box,
            "title_pos": (px0, py_top),
            "footer_y": footer_y,
            "x_range": x_range,
            "y_range": y_range,
            "x_label": x_label,
            "y_label": y_label,
            "lines": _panel_summary_lines(s, unit_str),
            "axes": Mapping(box, x_range, y_range, y_down=False),
        })

    return {
        "width": width,
        "height": height,
        "title": "Sample quantification",
        "subtitle": subtitle,
        "unit": unit_str,
        "panels": panels,
        "title_font": title_font,
        "sub_font": sub_font,
        "header_font": header_font,
        "axis_font": axis_font,
        "tick_font": tick_font,
        "ann_font": ann_font,
        "mark_font": mark_font,
        "legend_font": legend_font,
        "ann_step": ann_step,
        "header_y": header_y,
        "sub_y": sub_y,
        "left_pad": left_pad,
        "right_pad": right_pad,
        "font": None,
    }


def _draw_quant_panel(fig, geo, panel):
    width, height = geo["width"], geo["height"]
    font = geo["font"]
    box = panel["box"]
    ax = fig.add_axes(_fig_rect(box, width, height))
    ax.set_xlim(panel["x_range"])
    ax.set_ylim(panel["y_range"])
    ax.set_autoscale_on(False)
    _style_curve_axes(ax, font)

    # Panel title above the axes box
    tx0, ty0 = panel["title_pos"]
    _fig_text(fig, tx0, ty0, panel["name"], geo["header_font"], TEXT_RGB,
              va="top", font=font, clip_width=box[2] - box[0])

    # Y ticks
    ylo, yhi = panel["y_range"]
    ystep = _nice_step(yhi - ylo, 5)
    ydigits = _tick_digits(ystep)
    yticks = [v for v in _ticks(ylo, yhi, 5) if ylo - 1e-9 <= v <= yhi + 1e-9]
    ax.set_yticks(yticks)
    ax.set_yticklabels([_eqnum(v, ydigits) for v in yticks])
    for lbl in ax.get_yticklabels():
        lbl.set_fontproperties(_font_prop(geo["tick_font"], font))
        lbl.set_color(_mcolor(MUTED_RGB))

    ax.set_xlabel(panel["x_label"], color=_mcolor(TEXT_RGB),
                  fontproperties=_font_prop(geo["axis_font"], font), labelpad=5)
    ax.set_ylabel(panel["y_label"], color=_mcolor(TEXT_RGB),
                  fontproperties=_font_prop(geo["axis_font"], font), labelpad=6)

    mapping = panel["axes"]
    est = panel["est"]
    handles, labels = [], []
    boxes = []
    sample_handle = None
    dropped_handle = None
    fit_handle = None
    has_err = False

    if panel["mode"] == "ratio":
        xlo, xhi = panel["x_range"]
        xstep = _nice_step(xhi - xlo, 5)
        xdigits = _tick_digits(xstep)
        xticks = [v for v in _ticks(max(0.0, xlo), xhi, 5) if xlo - 1e-9 <= v <= xhi + 1e-9]
        ax.set_xticks(xticks)
        ax.set_xticklabels([_eqnum(v, xdigits) for v in xticks])
        for lbl in ax.get_xticklabels():
            lbl.set_fontproperties(_font_prop(geo["tick_font"], font))
            lbl.set_color(_mcolor(MUTED_RGB))

        ax.axhline(0.0, color=_mcolor(GUIDE_RGB), linewidth=0.8, linestyle=(0, (4, 4)), zorder=1)
        ax.axvline(0.0, color=_mcolor(GUIDE_RGB), linewidth=0.8, linestyle=(0, (4, 4)), zorder=1)

        if est and est.get("conc") is not None:
            slope = float(est["conc"])
            rinfo = est.get("ratio") or {}
            slope_sd = rinfo.get("slope_sd") if rinfo.get("slope_sd") is not None else est.get("conc_sd")
            max_x = xhi * 0.98
            xs = [0.0, max_x]
            ys = [0.0, slope * max_x]
            if slope_sd is not None and math.isfinite(float(slope_sd)) and float(slope_sd) > 0:
                sd = float(slope_sd)
                ax.fill_between(xs, [0.0, (slope - sd) * max_x], [0.0, (slope + sd) * max_x],
                                color=_mcolor(SAMPLE_RGB), alpha=0.10, linewidth=0, zorder=1)
            fit_handle, = ax.plot(xs, ys, color=_mcolor(LINE_RGB), linewidth=1.8,
                                  solid_capstyle="round", zorder=3)

        for m in panel["members"]:
            if m["equiv"] is None:
                continue
            x_val, y_val = m["ratio"], m["equiv"]
            sd = m["equiv_sd"]
            if sd is not None and math.isfinite(sd) and sd > 0.0:
                ax.errorbar([x_val], [y_val], yerr=[[sd]], fmt="none",
                            ecolor=_mcolor(ERR_RGB), elinewidth=1.4,
                            capsize=3.6, capthick=1.4, zorder=4)
                has_err = True
            if m["dropped"]:
                h, = ax.plot([x_val], [y_val], marker="s",
                             markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                             markerfacecolor="white", markeredgecolor=_mcolor(DROP_RGB),
                             markeredgewidth=1.8, zorder=5)
                dropped_handle = dropped_handle or h
                lrgb = DROP_RGB
            else:
                h, = ax.plot([x_val], [y_val], marker="s",
                             markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                             markerfacecolor=_mcolor(SAMPLE_RGB),
                             markeredgecolor=_mcolor(SAMPLE_STROKE_RGB),
                             markeredgewidth=0.9, zorder=5)
                sample_handle = sample_handle or h
                lrgb = SAMPLE_RGB
            px, py = mapping.x(x_val), mapping.y(y_val)
            err_top = mapping.y(y_val + sd) if (sd is not None and math.isfinite(sd) and sd > 0.0) else py
            err_bot = mapping.y(y_val - sd) if (sd is not None and math.isfinite(sd) and sd > 0.0) else py
            own = (px - SAMPLE_HALF - 4, min(py - SAMPLE_HALF - 2, err_top - 4),
                   px + SAMPLE_HALF + 4, max(py + SAMPLE_HALF + 2, err_bot + 4))
            ltx, lty, lalign, lbox = _place_label(m["label"], geo["mark_font"], px, py,
                                                  boxes, font, box, skip=own)
            _fig_text(fig, ltx, lty, m["label"], geo["mark_font"], lrgb, ha=lalign, font=font)
            boxes.append(lbox)
    else:
        plottable = [m for m in panel["members"] if m["conc"] is not None]
        xs = list(range(len(plottable)))
        ax.set_xticks(xs)
        ax.set_xticklabels([m["label"] for m in plottable])
        for lbl in ax.get_xticklabels():
            lbl.set_fontproperties(_font_prop(geo["tick_font"], font))
            lbl.set_color(_mcolor(MUTED_RGB))

        if est and est.get("conc") is not None and panel["method"] == "mean":
            c_est = float(est["conc"])
            sem = est.get("sem")
            if sem is not None and math.isfinite(float(sem)) and float(sem) > 0:
                ax.axhspan(c_est - float(sem), c_est + float(sem),
                           color=_mcolor(SAMPLE_RGB), alpha=0.12, linewidth=0, zorder=1)
            fit_handle = ax.axhline(c_est, color=_mcolor(LINE_RGB), linewidth=1.8,
                                    linestyle=(0, (6, 4)), zorder=3)

        for i, m in enumerate(plottable):
            y_val = m["conc"]
            sd = m["conc_sd"]
            if sd is not None and math.isfinite(sd) and sd > 0.0:
                ax.errorbar([i], [y_val], yerr=[[sd]], fmt="none",
                            ecolor=_mcolor(ERR_RGB), elinewidth=1.4,
                            capsize=3.6, capthick=1.4, zorder=4)
                has_err = True
            if m["dropped"]:
                h, = ax.plot([i], [y_val], marker="s",
                             markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                             markerfacecolor="white", markeredgecolor=_mcolor(DROP_RGB),
                             markeredgewidth=1.8, zorder=5)
                dropped_handle = dropped_handle or h
                lrgb = DROP_RGB
            else:
                h, = ax.plot([i], [y_val], marker="s",
                             markersize=SAMPLE_HALF * 2 * 72 / DPI, linestyle="none",
                             markerfacecolor=_mcolor(SAMPLE_RGB),
                             markeredgecolor=_mcolor(SAMPLE_STROKE_RGB),
                             markeredgewidth=0.9, zorder=5)
                sample_handle = sample_handle or h
                lrgb = SAMPLE_RGB
            px, py = mapping.x(i), mapping.y(y_val)
            err_top = mapping.y(y_val + sd) if (sd is not None and math.isfinite(sd) and sd > 0.0) else py
            err_bot = mapping.y(y_val - sd) if (sd is not None and math.isfinite(sd) and sd > 0.0) else py
            own = (px - SAMPLE_HALF - 4, min(py - SAMPLE_HALF - 2, err_top - 4),
                   px + SAMPLE_HALF + 4, max(py + SAMPLE_HALF + 2, err_bot + 4))
            val_txt = _fmt_val(y_val)
            ltx, lty, lalign, lbox = _place_label(val_txt, geo["mark_font"], px, py,
                                                  boxes, font, box, skip=own)
            _fig_text(fig, ltx, lty, val_txt, geo["mark_font"], lrgb, ha=lalign, font=font)
            boxes.append(lbox)

    if sample_handle is not None:
        handles.append(sample_handle)
        labels.append("sample")
    if dropped_handle is not None:
        handles.append(dropped_handle)
        labels.append("dropped sample")
    if fit_handle is not None:
        handles.append(fit_handle)
        labels.append("through-origin fit" if panel["mode"] == "ratio" else "weighted mean")
    if has_err:
        handles.append(_bar_handle())
        labels.append("±1 SD")
    if handles:
        leg_geo = {
            "width": width,
            "height": height,
            "main_box": box,
            "font": font,
        }
        _add_curve_legend(fig, ax, leg_geo, handles, labels, boxes)
    ax.set_xlim(panel["x_range"])
    ax.set_ylim(panel["y_range"])

    # Footer lines (formula with ± and final conc with ±)
    room = box[2] - box[0]
    for i, line in enumerate(panel["lines"]):
        color = TEXT_RGB if i == 0 else MUTED_RGB
        size = geo["ann_font"]
        if i == 0:
            tw = _text_width(font, size, line)
            if tw > room and tw > 0:
                size *= room / tw
        _fig_text(fig, box[0], panel["footer_y"] + i * geo["ann_step"], line, size, color,
                  va="top", font=font, clip_width=room)


def plot_quant(quant, out, width=1020, cols=2):
    """Render quant.json's sample series quantification and ratio fits as a PNG."""
    Figure, FigureCanvasAgg = _load_mpl()
    target = _check_out(out)
    geo = quant_layout(quant, width=width, cols=cols)

    for panel in geo["panels"]:
        if not _is_english_label(panel["raw_name"]):
            warnings.warn_explicit(
                "sample label %r is not English; drawn as %r" % (panel["raw_name"], panel["name"]),
                LaneLabelWarning, __file__, 0, registry=None,
            )

    w, h = geo["width"], geo["height"]
    fig = Figure(figsize=(w / float(DPI), h / float(DPI)), dpi=DPI, facecolor="white")
    FigureCanvasAgg(fig)
    room = w - geo["left_pad"] - geo["right_pad"]
    _fig_text(fig, geo["left_pad"], geo["header_y"], geo["title"], geo["title_font"],
              TEXT_RGB, va="top", font=geo["font"], clip_width=room)
    _fig_text(fig, geo["left_pad"], geo["sub_y"], geo["subtitle"], geo["sub_font"],
              MUTED_RGB, va="top", font=geo["font"], clip_width=room)

    for panel in geo["panels"]:
        _draw_quant_panel(fig, geo, panel)

    return _save_png(fig, w, h, target)
