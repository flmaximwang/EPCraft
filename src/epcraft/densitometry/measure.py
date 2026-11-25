"""
measure — §4.2-§4.4: report v2 (fig.mw.json) → dens.json.

One measured lane carries the smoothed ink, the profile it came from, both baselines' areas and
windows, and the flags (§4.3); `measure` only selects the lanes and builds the file around them.
"""

from .contracts import FLANK_CM, KIND, MIN_INK, REPORT_PATH_KEY, SMOOTH_CM, UNITS, VERSION, WINDOW_TEXT
from .values import _dy, _nearest, _num, _rows_in, _runs
from .bands import _flank_levels, _peak_in, _ramp_at, _ramp_dict, _walk, area_of, smooth

def _select_lanes(lanes_all, by_no, lanes, include_marker, include_trimmed):
    """Lane numbers to measure (ruling R8): --lanes replaces the default, the flags extend it."""
    if lanes is not None:
        wanted = sorted({int(v) for v in lanes})
        if not wanted:
            raise RuntimeError("measure：lanes 列表是空的（省略它 = 用默认选择）")
        missing = [v for v in wanted if v not in by_no]
        if missing:
            raise RuntimeError("measure：报告里没有泳道 %s（报告有 1–%d）" % (_runs(missing), max(by_no)))
        return wanted
    default = [int(ln["lane"]) for ln in lanes_all if not ln.get("marker") and not ln.get("trimmed")]
    extra = []
    if include_marker:
        extra += [int(ln["lane"]) for ln in lanes_all if ln.get("marker")]
    if include_trimmed:
        extra += [int(ln["lane"]) for ln in lanes_all if ln.get("trimmed")]
    selected = sorted(set(default) | set(extra))
    if not selected:
        raise RuntimeError("measure：没有可测的泳道（默认跳过 marker 与 trimmed）；"
                           "用 --lanes 指定，或加 --include-marker / --include-trimmed")
    return selected


def _lanes_text(selected, lanes_all):
    """Derive params.lanes from what was measured vs the report — it cannot disagree with the array."""
    all_nos = sorted(int(ln["lane"]) for ln in lanes_all)
    sample = sorted(int(ln["lane"]) for ln in lanes_all if not ln.get("marker") and not ln.get("trimmed"))
    if selected == all_nos:
        return "all lanes"
    if selected == sample:
        return "all sample lanes, trimmed and marker excluded"
    return "lanes " + _runs(selected)


def _invisible_note(no, index, inv, min_ink):
    """One Chinese sentence per invisible case (R7); the ASCII tag stays in band.notes."""
    both = len(inv) == 2
    where = "两种基线" if both else ("global 基线" if inv[0] == "global" else "flank 基线")
    pad = "" if both else " "                                  # "在两种基线上" / "在 global 基线上"
    return ("泳道 %d 的带 %d 的平滑墨量在%s%s上 ≤ min_ink（%g 灰度级），窗口按带宽取"
            % (no, index, pad, where, min_ink))


def _measure_lane(lane, flank_cm, min_ink, smooth_cm, notes):
    """One report lane → one dens lane (bands integrated on both baselines); appends top-level notes."""
    no = int(lane["lane"])
    prof = lane.get("profile")
    if not isinstance(prof, dict):
        raise RuntimeError("泳道 %d 没有 profile" % no)
    y = [float(v) for v in (prof.get("y_cm") or [])]
    gray = [float(v) for v in (prof.get("gray") or [])]
    base = [float(v) for v in (prof.get("baseline") or [])]
    if not y or len(y) != len(gray) or len(y) != len(base):
        raise RuntimeError("泳道 %d 的 profile 数组为空或长度不一致（y %d / gray %d / baseline %d）"
                           % (no, len(y), len(gray), len(base)))
    try:
        dy = _dy(y)
    except RuntimeError as e:
        raise RuntimeError("泳道 %d：%s" % (no, e))
    half = max(1, int(round(smooth_cm / dy))) if dy > 0 else 1
    ink_g = [base[k] - gray[k] for k in range(len(y))]
    sm_g = smooth(ink_g, half)

    raw_bands = lane.get("bands") or []
    if not isinstance(raw_bands, list):
        raise RuntimeError("泳道 %d 的 bands 不是列表" % no)
    bands = []
    for b in raw_bands:
        if not isinstance(b, dict) or "y_cm" not in b:
            raise RuntimeError("泳道 %d 里有带缺少 y_cm" % no)
        bands.append(b)
    bands.sort(key=lambda b: float(b["y_cm"]))
    band_ys = [float(b["y_cm"]) for b in bands]

    out_bands = []
    total_g = 0.0
    total_f = 0.0
    for index, b in enumerate(bands, 1):
        y_band = band_ys[index - 1]
        y_up = (band_ys[index - 2] + y_band) / 2.0 if index >= 2 else y[0]
        y_dn = (y_band + band_ys[index]) / 2.0 if index < len(bands) else y[-1]
        i = _nearest(y, y_band)
        width = max(float(b.get("width_cm") or 0.0), 2.0 * dy)

        # (1) global baseline: walk on the report's own ink, then integrate (§4.2, R7 order)
        inv = []
        if sm_g[i] <= min_ink:
            y0g, y1g = y_band - width / 2.0, y_band + width / 2.0
            up_g = dn_g = None
            inv.append("global")
        else:
            i0g, i1g, up_g, dn_g = _walk(y, sm_g, i, y_up, y_dn, min_ink)
            y0g, y1g = y[i0g], y[i1g]
        area_g = area_of(y, ink_g, y0g, y1g)
        peak_g = _peak_in(y, ink_g, y0g, y1g)

        # (2) flank ramp from the global window, then its own walk on ink_flank (§4.3)
        left, right = _flank_levels(y, gray, y0g, y1g, y_up, y_dn, flank_cm, base)
        ramp = _ramp_dict(left, right)
        ink_f = [_ramp_at(ramp, value) - gray[k] for k, value in enumerate(y)]
        sm_f = smooth(ink_f, half)
        if sm_f[i] <= min_ink:
            y0f, y1f = y_band - width / 2.0, y_band + width / 2.0
            up_f = dn_f = None
            inv.append("flank")
        else:
            i0f, i1f, up_f, dn_f = _walk(y, sm_f, i, y_up, y_dn, min_ink)
            y0f, y1f = y[i0f], y[i1f]
        area_f = area_of(y, ink_f, y0f, y1f)
        peak_f = _peak_in(y, ink_f, y0f, y1f)

        # (3) tags (fixed ASCII vocabulary, R7) and their Chinese sentences
        tags = []
        if inv:
            tags.append("invisible")
            notes.append(_invisible_note(no, index, inv, min_ink))
        up_stop = "end" if (up_g == "end" or up_f == "end") else ("mid" if (up_g == "mid" or up_f == "mid") else None)
        dn_stop = "end" if (dn_g == "end" or dn_f == "end") else ("mid" if (dn_g == "mid" or dn_f == "mid") else None)
        if up_stop == "end":
            tags.append("clamped_top")
            notes.append("泳道 %d 的带 %d 窗口贴到剖面上端" % (no, index))
        elif up_stop == "mid":
            tags.append("clamped_mid_up")
            notes.append("泳道 %d 的带 %d 窗口贴到上方相邻带的中点" % (no, index))
        if dn_stop == "end":
            tags.append("clamped_bottom")
            notes.append("泳道 %d 的带 %d 窗口贴到剖面下端" % (no, index))
        elif dn_stop == "mid":
            tags.append("clamped_mid_down")
            notes.append("泳道 %d 的带 %d 窗口贴到下方相邻带的中点" % (no, index))
        if not left[2]:
            tags.append("flank_fallback_left")
            notes.append("泳道 %d 的带 %d 左侧 flank 不足 2 行，取 global 基线" % (no, index))
        if not right[2]:
            tags.append("flank_fallback_right")
            notes.append("泳道 %d 的带 %d 右侧 flank 不足 2 行，取 global 基线" % (no, index))

        # (4) saturation flags: notes, never errors (§4.4)
        a_win, b_win = _rows_in(y, y0f, y1f)
        rows = gray[a_win:b_win + 1] if b_win >= a_win else []
        clipped = any(v <= 8.0 for v in rows)
        flat_top = len(rows) >= 3 and sum(1 for v in rows if v <= min(rows) + 1.0) >= 3

        out_bands.append({
            "index": index,
            "y_cm": _num(y_band, 4),
            "mw_kda": b.get("mw_kda"),
            "strongest": bool(b.get("strongest", False)),
            "peak_ink_global": _num(peak_g, 2),
            "peak_ink_flank": _num(peak_f, 2),
            "window_global": [_num(y0g, 4), _num(y1g, 4)],
            "window_flank": [_num(y0f, 4), _num(y1f, 4)],
            "area_global": _num(area_g, 3),
            "area_flank": _num(area_f, 3),
            "ramp": {"left": [_num(left[0], 4), _num(left[1], 2)],
                     "right": [_num(right[0], 4), _num(right[1], 2)],
                     "source": ramp["source"]},
            "clipped": clipped,
            "flat_top": flat_top,
            "notes": tags,
        })
        total_g += area_g
        total_f += area_f

    label = lane.get("label")
    return {
        "lane": no,
        "label": label,
        "marker": bool(lane.get("marker")),
        "trimmed": bool(lane.get("trimmed")),
        "noise": lane.get("noise"),
        "profile": {"y_cm": list(prof.get("y_cm") or []),
                    "gray": list(prof.get("gray") or []),
                    "baseline_global": list(prof.get("baseline") or [])},
        "bands": out_bands,
        "area_global": _num(total_g, 3),
        "area_flank": _num(total_f, 3),
    }


def measure(report, lanes=None, flank_cm=FLANK_CM, min_ink=MIN_INK, smooth_cm=SMOOTH_CM,
            *, include_marker=False, include_trimmed=False):
    """Report v2 dict → dens.json dict (pure: no I/O; spec §4.4).

    `lanes=None` takes the default selection of ruling R8 (sample lanes: marker and trimmed
    excluded); an explicit list replaces it (those lanes are measured whatever their flags, and a
    lane that is not in the report is a RuntimeError).  `include_marker` / `include_trimmed` extend
    the **default** only — they are keyword-only extensions of the §4.1 signature, needed so the
    CLI flags cannot disagree with `params.lanes`.

    Bad parameters raise ValueError (flank_cm <= 0, min_ink < 0, smooth_cm <= 0).  source.report is
    the path `load_report` recorded (R6); a report dict built in memory carries "".
    """
    if float(flank_cm) <= 0.0:
        raise ValueError("flank_cm 必须是正数（收到 %r）" % (flank_cm,))
    if float(min_ink) < 0.0:
        raise ValueError("min_ink 不能是负数（收到 %r）" % (min_ink,))
    if float(smooth_cm) <= 0.0:
        raise ValueError("smooth_cm 必须是正数（收到 %r）" % (smooth_cm,))
    if not isinstance(report, dict):
        raise RuntimeError("measure：报告不是 dict（收到 %s）" % type(report).__name__)
    lanes_all = report.get("lanes")
    if not isinstance(lanes_all, list) or not lanes_all:
        raise RuntimeError("measure：报告里没有 lanes 列表")
    by_no = {}
    for ln in lanes_all:
        if not isinstance(ln, dict):
            raise RuntimeError("measure：lanes 里有不是对象的条目")
        try:
            no = int(ln.get("lane"))
        except (TypeError, ValueError):
            raise RuntimeError("measure：泳道条目缺少整数 lane（收到 %r）" % (ln.get("lane"),))
        by_no[no] = ln

    selected = _select_lanes(lanes_all, by_no, lanes, include_marker, include_trimmed)
    notes = []
    out_lanes = [_measure_lane(by_no[no], flank_cm, min_ink, smooth_cm, notes) for no in selected]

    calibration = report.get("calibration")
    source_report = report.get(REPORT_PATH_KEY)
    dens = {
        "version": VERSION,
        "kind": KIND,
        "units": dict(UNITS),
        "source": {"report": source_report if isinstance(source_report, str) else "",
                   "gel": dict(report.get("gel") or {}),
                   "window": dict(report.get("window") or {}),
                   "marker_lane": calibration.get("marker_lane") if isinstance(calibration, dict) else None},
        "params": {"flank_cm": _num(flank_cm, 4), "min_ink": _num(min_ink, 2),
                   "smooth_cm": _num(smooth_cm, 4), "window": WINDOW_TEXT,
                   "lanes": _lanes_text(selected, lanes_all)},
        "lanes": out_lanes,
        "notes": notes,
    }
    return dens
