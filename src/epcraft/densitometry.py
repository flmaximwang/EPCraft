#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""densitometry — the offline math of the densitometry chain.

    report v2 (fig.mw.json) ──► measure() ──► dens.json ──► build_curve() ──► curve.json

Everything is a pure function over dicts (spec `specs/ep-craft-densitometry.md` §2.1-§2.9, §4.1-4.5):
no VectorCraft, no ImageMagick, no I/O beyond json.load / json.dump.  The CLI owns argparse and
paths (`densitometry_cli.py`); the plots own ImageMagick (`dens_plot.py`, ticket .2).

Two baselines, always both, per band (decision §2.2):
  * global — the report's top-hat `baseline` array (half window 0.30 cm).  It dips under a dense
    band, so `ink` from it under-reports the dark end of a titration.
  * flank — a straight ramp in y through the median gray of the rows beside the band (§2.3).
Both are integrated over their own window: walk outward on ink smoothed over a SMOOTH_CM half
window while it stays above min_ink, clamped to the midpoints of the neighbouring bands in the
same lane (§2.4, ruling R7).

Engineering notes are English, user-facing strings (JSON `notes`, error messages) are Chinese,
like the rest of the package (ruling R1).  Rounding happens only when the returned dict is built
(ruling R2); `walk_window` / `area_of` / `flank_ramp` / `smooth` stay full precision.
"""
import bisect
import json
import math
import os
import re
import unicodedata

VERSION = 1
KIND = "ep-craft-densitometry"
CURVE_KIND = "ep-craft-densitometry-curve"
MIN_INK = 0.5              # gray levels; the walking threshold
FLANK_CM = 0.15            # rows taken beside a band for the flank level
SMOOTH_CM = 0.03           # half window for the edge walk (= profiles.SMOOTH_CM, mirrored here
                           #   so this module never pulls in magick_io through profiles)
FUZZ = 1e-9

MAX_DY_SPREAD = 0.10       # report y_cm is rounded to 4 dp: accept diffs within 10 % of the median
DOC_SUFFIX = ".vectorcraft"
REPORT_SUFFIX = ".mw.json"
REPORT_PATH_KEY = "_report_path"   # load_report() records the path it was handed (R6: source.report)

UNITS = {"y": "cm from gel-face top", "gray": "0-255 (lower = darker)", "ink": "baseline - gray",
         "area": "gray·cm (ink integrated over the band window)"}
WINDOW_TEXT = ("walk outward on smoothed ink while > min_ink, clamped to the midpoints of the "
               "neighbouring bands")


# ---------------------------------------------------------------- small shared helpers

def _one_line(text):
    """Any exception text → one line (CLI errors must never carry a newline)."""
    return " ".join(str(text).split())


def _median(values):
    """Median of a non-empty sequence (sorted, even count → mean of the two middle values)."""
    s = sorted(values)
    n = len(s)
    if not n:
        raise ValueError("median of an empty sequence")
    h = n // 2
    return s[h] if n % 2 else (s[h - 1] + s[h]) / 2.0


def _num(value, nd):
    """Round for the built dict, never producing -0.0 (R2: round only when building)."""
    r = round(float(value), nd)
    return 0.0 if r == 0 else float(r)


def _nearest(y, value):
    """Index of the row whose y is closest to value (y is ascending)."""
    return min(range(len(y)), key=lambda k: abs(y[k] - value))


def _runs(numbers):
    """[1, 2, 3, 9] → '1-3,9' (compact lane list for messages and params.lanes)."""
    parts, start, prev = [], None, None
    for k in sorted(numbers):
        if start is None:
            start = prev = k
        elif k == prev + 1:
            prev = k
        else:
            parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
            start = prev = k
    if start is not None:
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ",".join(parts)


def _dy(y):
    """Median row spacing of a profile, validating the uniform-y contract (§4.2, ruling R7).

    The report rounds y_cm to 4 dp, so equality is impossible; accept when every diff stays
    within MAX_DY_SPREAD of the median (real reports: dy ≈ 0.008-0.012 cm, jitter ≈ 1 %).
    A one-row profile has no spacing and returns 0.0 (the callers then use one-row windows).
    """
    n = len(y)
    if n < 2:
        return 0.0
    diffs = [y[k + 1] - y[k] for k in range(n - 1)]
    med = _median(diffs)
    if med <= 0.0 or any(v <= 0.0 for v in diffs):
        raise RuntimeError("剖面的 y_cm 必须单调递增（出现 %.6f cm 的步长）" % min(diffs))
    spread = max(abs(v - med) for v in diffs)
    if spread > MAX_DY_SPREAD * med:
        raise RuntimeError("剖面的 y_cm 不均匀（步长中位 %.6f cm，最大偏差 %.6f cm）" % (med, spread))
    return med


def _rows_in(y, y0, y1):
    """First / last row index whose y lies inside the closed window [y0, y1]; (0, -1) when empty."""
    a = bisect.bisect_left(y, y0 - FUZZ)
    b = bisect.bisect_right(y, y1 + FUZZ) - 1
    return (a, b) if b >= a else (0, -1)


# ---------------------------------------------------------------- §4.1 helpers

def smooth(values, half_rows):
    """Centred moving mean over 2·half_rows + 1 rows, edges shortened (§4.2 step 1).

    Same arithmetic as `profiles.ink_trace`'s smoothing, so the walk sees exactly the trace the
    report's own band caller saw.
    """
    n = len(values)
    if n == 0:
        return []
    h = max(1, int(half_rows))
    pre = [0.0]
    for v in values:
        pre.append(pre[-1] + v)
    return [(pre[min(n, i + h + 1)] - pre[max(0, i - h)]) / (min(n, i + h + 1) - max(0, i - h))
            for i in range(n)]


def _walk(y, sm, i, y_up, y_dn, min_ink):
    """Walk outward from row i while the smoothed ink stays above min_ink (§4.2 step 3).

    → (i0, i1, up_stop, dn_stop): the LAST ROW THAT PASSED on each side (the stopped row is
    excluded, ruling R7); stop ∈ {"end", "mid", None} — "end" = the walk ran out of profile rows,
    "mid" = the row just outside is still above min_ink but lies past the neighbour limit,
    None = the ink fell below min_ink.
    """
    n = len(sm)
    a = i
    while a > 0 and y[a - 1] >= y_up - FUZZ and sm[a - 1] > min_ink:
        a -= 1
    b = i
    while b + 1 < n and y[b + 1] <= y_dn + FUZZ and sm[b + 1] > min_ink:
        b += 1
    if a == 0:
        up = "end"
    elif sm[a - 1] > min_ink:
        up = "mid"
    else:
        up = None
    if b == n - 1:
        dn = "end"
    elif sm[b + 1] > min_ink:
        dn = "mid"
    else:
        dn = None
    return a, b, up, dn


def walk_window(y, ink, y_band, y_up, y_dn, min_ink, smooth_cm=SMOOTH_CM):
    """Window of one band on one ink trace (§4.2) → (y0, y1) or (None, None) when invisible.

    `y0` / `y1` are the last rows that passed on each side (the row that stopped the walk is
    excluded).  When the smoothed ink at the band row is ≤ min_ink there is no walk at all: the
    band is invisible and `measure()` builds the fallback window
    `y_band ± max(width_cm, 2·dy)/2` and says so in the notes (the §4.2 step-2 window needs the
    band's width_cm, which this signature does not carry).
    """
    n = len(y)
    if n == 0:
        return None, None
    dy = _dy(y)
    half = max(1, int(round(smooth_cm / dy))) if dy > 0 else 1
    sm = smooth(ink, half)
    i = _nearest(y, y_band)
    if sm[i] <= min_ink:
        return None, None
    i0, i1, _up, _dn = _walk(y, sm, i, y_up, y_dn, min_ink)
    return y[i0], y[i1]


def area_of(y, ink, y0, y1):
    """Trapezoid integral of ink over the rows inside the closed window [y0, y1], gray·cm (§4.2).

    Only the rows inside the window contribute (no partial trapezoids at the edges, ruling R7).
    """
    a, b = _rows_in(y, y0, y1)
    total = 0.0
    for k in range(a, b):
        total += 0.5 * (ink[k] + ink[k + 1]) * (y[k + 1] - y[k])
    return total


def _flank_levels(y, gray, y0, y1, y_up, y_dn, flank_cm, baseline):
    """The two ramp endpoints (§4.3) → ((y_l, g_l, ok_l), (y_r, g_r, ok_r)), full precision.

    Left flank = rows in [max(y_up, y0 − fw·dy), y0); right flank = rows in (y1, min(y_dn, y1 +
    fw·dy)]; fw = max(2, round(flank_cm / dy)).  Level = median gray of those rows, placed at the
    flank's y centre.  A flank with fewer than 2 rows takes the **global** baseline at the window
    edge row itself instead (y0 / y1, the last row inside the window on that side — which is where
    a missing flank sits; still a ramp endpoint) and reports ok=False — `measure` turns that into
    the `flank_fallback_left/right` tag and a note.

    `measure` needs the per-side fallback flags for the notes, so it calls this directly and
    builds the same dict through `_ramp_dict` that `flank_ramp` returns.
    """
    dy = _dy(y)
    fw = max(2, int(round(flank_cm / dy))) if dy > 0 else 2

    def point(a, b, edge):
        if b - a + 1 >= 2:
            return (y[a] + y[b]) / 2.0, _median(gray[a:b + 1]), True
        k = _nearest(y, edge)
        return y[k], baseline[k], False

    lo_l = max(y_up, y0 - fw * dy)
    a_l = bisect.bisect_left(y, lo_l - FUZZ)
    b_l = bisect.bisect_left(y, y0 - FUZZ) - 1
    left = point(a_l, b_l, y0)

    hi_r = min(y_dn, y1 + fw * dy)
    a_r = bisect.bisect_right(y, y1 + FUZZ)
    b_r = bisect.bisect_right(y, hi_r + FUZZ) - 1
    right = point(a_r, b_r, y1)
    return left, right


def _ramp_dict(left, right):
    """Ramp endpoints → the public §4.3 dict (source = "flank" only when both sides are real)."""
    return {"left": [left[0], left[1]], "right": [right[0], right[1]],
            "source": "flank" if (left[2] and right[2]) else "global"}


def flank_ramp(y, gray, y0, y1, y_up, y_dn, flank_cm, baseline):
    """Flank ramp (§4.3) → {"left": [y_c, g], "right": [y_c, g], "source": "flank"|"global"}.

    The level at y is the straight line between the two endpoints, clamped outside them; use the
    returned points with `_ramp_at` (or interpolate the same way) to build `ink_flank`.
    """
    left, right = _flank_levels(y, gray, y0, y1, y_up, y_dn, flank_cm, baseline)
    return _ramp_dict(left, right)


def _ramp_at(ramp, value):
    """Ramp value at y: linear between the endpoints, clamped to the end values outside them."""
    (yl, gl), (yr, gr) = ramp["left"], ramp["right"]
    if yr <= yl:
        return gl
    t = (value - yl) / (yr - yl)
    if t <= 0.0:
        return gl
    if t >= 1.0:
        return gr
    return gl + (gr - gl) * t


def _peak_in(y, ink, y0, y1):
    """Maximum of the ink trace inside the window (0.0 when the window holds no row)."""
    a, b = _rows_in(y, y0, y1)
    if b < a:
        return 0.0
    return max(ink[a:b + 1])


# ---------------------------------------------------------------- lane grammar (§4.1, R8)

def parse_lanes(text, n):
    """'1,2,8-10' → [1, 2, 8, 9, 10] (1-based, de-duplicated, ascending).

    Grammar of `lane_select.parse_keep`, but the messages name `--lanes`: None or "" selects every
    lane.  Out of range / empty segment / reversed range / garbage raise RuntimeError, one line.
    """
    count = int(n)
    if count < 1:
        raise RuntimeError("--lanes：报告里的泳道数必须 ≥1（收到 %r）" % (n,))
    if text is None:
        return list(range(1, count + 1))
    s = str(text).strip()
    if not s:
        return list(range(1, count + 1))
    out = set()
    for raw in s.split(","):
        tok = raw.strip()
        if not tok:
            raise RuntimeError("--lanes 里有一段是空的：%r（逗号之间不能没有泳道号）" % s)
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", tok)
        if not m:
            raise RuntimeError("--lanes 里有一段写法不对：%r（只接受 3 或 8-10 这种，1 起）" % tok)
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        if b < a:
            raise RuntimeError("--lanes 里区间反了：%r（左号必须 ≤ 右号）" % tok)
        for k in (a, b):
            if k < 1 or k > count:
                raise RuntimeError("--lanes 里第 %d 条越界：报告里只有 1–%d 条泳道（那段是 %r）"
                                   % (k, count, tok))
        out.update(range(a, b + 1))
    return sorted(out)


# ---------------------------------------------------------------- JSON I/O

def report_path(path):
    """A `.vectorcraft` document path → its report sibling `<stem>.mw.json`; anything else is
    already the report path (decision §2.1).  Pure string work — no existence check."""
    text = str(path)
    if text.lower().endswith(DOC_SUFFIX):
        return text[:-len(DOC_SUFFIX)] + REPORT_SUFFIX
    return text


def load_json(path, kind=None):
    """Read a JSON object written by this chain (ruling R4).

    RuntimeError (single line, Chinese) when the file is missing, is not JSON, is not an object,
    or its `kind` differs from `kind`.  `load_report` is built on this.
    """
    text = str(path)
    if not os.path.exists(text):
        raise RuntimeError("找不到 %s" % text)
    try:
        with open(text, encoding="utf-8") as f:
            obj = json.load(f)
    except ValueError as e:
        raise RuntimeError("%s 不是合法的 JSON：%s" % (text, _one_line(e)))
    if not isinstance(obj, dict):
        raise RuntimeError("%s 不是 JSON 对象（顶层是 %s）" % (text, type(obj).__name__))
    if kind is not None and obj.get("kind") != kind:
        raise RuntimeError("%s 不是 %s 文件（kind=%r）" % (text, kind, obj.get("kind")))
    return obj


def _validate_report(rep, where):
    """Validate report v2: version, lanes, per-lane profile arrays (length, non-empty, uniform y)."""
    if rep.get("version") != 2:
        raise RuntimeError("%s 不是报告 v2（version=%r）" % (where, rep.get("version")))
    lanes = rep.get("lanes")
    if not isinstance(lanes, list):
        raise RuntimeError("%s 里没有 lanes 列表（不是报告 v2？）" % where)
    if not lanes:
        raise RuntimeError("%s 里没有泳道" % where)
    for ln in lanes:
        if not isinstance(ln, dict):
            raise RuntimeError("%s 的 lanes 里有不是对象的条目" % where)
        no = ln.get("lane")
        prof = ln.get("profile")
        if not isinstance(prof, dict):
            raise RuntimeError("%s 的泳道 %r 没有 profile" % (where, no))
        arrays = []
        for key in ("y_cm", "gray", "baseline"):
            vals = prof.get(key)
            if not isinstance(vals, list) or not vals:
                raise RuntimeError("%s 的泳道 %r 的 profile 缺少 %s 数组" % (where, no, key))
            arrays.append(vals)
        if len(arrays[0]) != len(arrays[1]) or len(arrays[0]) != len(arrays[2]):
            raise RuntimeError("%s 的泳道 %r 的 profile 三条数组长度不一致（%d / %d / %d）"
                               % (where, no, len(arrays[0]), len(arrays[1]), len(arrays[2])))
        try:
            _dy([float(v) for v in arrays[0]])
        except RuntimeError as e:
            raise RuntimeError("%s 的泳道 %r：%s" % (where, no, e))
    for key in ("gel", "window"):
        if not isinstance(rep.get(key), dict):
            raise RuntimeError("%s 缺少 %s（不是报告 v2？）" % (where, key))


def load_report(path):
    """Report v2 at path → dict (a `.vectorcraft` path resolves to its `<stem>.mw.json` first).

    A missing / stale report for a document raises RuntimeError naming `先 sync（它会重写这份报告）`
    (decision §2.1: sync rewrites the report, so the document is the truth).  The returned dict is
    a shallow copy carrying REPORT_PATH_KEY = the path as the user typed it (source.report, ruling
    R6) — use `report_path()` on it when the file that was actually read is needed.
    """
    typed = str(path)
    rp = report_path(typed)
    if rp != typed and not os.path.exists(typed):
        raise RuntimeError("找不到 %s" % typed)
    if not os.path.exists(rp):
        if rp != typed:
            raise RuntimeError("%s 不存在；先 sync（它会重写这份报告）" % rp)
        raise RuntimeError("找不到 %s（报告要由 render / sync 生成）" % rp)
    if rp != typed:
        try:
            stale = os.path.getmtime(rp) < os.path.getmtime(typed)
        except OSError as e:
            raise RuntimeError("读不了 %s 的时间戳：%s" % (rp, _one_line(e)))
        if stale:
            raise RuntimeError("%s 比文档 %s 旧；先 sync（它会重写这份报告）" % (rp, typed))
    rep = load_json(rp)
    _validate_report(rep, rp)
    return dict(rep, **{REPORT_PATH_KEY: typed})


def write_json(obj, path):
    """UTF-8, ensure_ascii=False, indent=1, trailing newline (§4.1, §4.4, ruling R2)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


# ---------------------------------------------------------------- measure (§4.2-§4.4)

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


# ---------------------------------------------------------------- curve (§2.6, §2.7, §2.9; §4.5)
#
# dens.json + the standard lanes given on the command line ──► curve.json.  Two fits, both cheap
# (§2.6): linear `mass = intercept + slope·volume` and power `mass = scale · volume^exponent`
# (least squares in log10 for both axes, r2 in log space).  Nothing is extrapolated: the fit is
# fitted, printed with its residuals and refused when it is not monotonic — the plot is the judge
# (§2.6, and the `plot-bands` hint in the §4.5 error messages).
#
# Standard entries: `parse_std` owns the `LANE[[:INDEX]|[:*]]=MASS` grammar, `build_curve` owns the
# data.  Replicates (entries with the same mass) collapse into one point (n / sd / cv_pct / lanes /
# entries, §2.7), `drop` keeps a point with `used: false` so the plot can show it.  The stored
# fit_params are rounded to 4 dp (ruling R2), so `quantify` evaluates the STORED numbers (forward),
# never a refit.

CURVE_SUFFIX = ".curve.json"      # default: <document stem>.curve.json, i.e. fig.dens.json → fig.curve.json
FITS = ("linear", "power")
BASELINES = ("flank", "global")


def parse_std(text):
    """'2=20,3:2=10,4:*=5' → [(2, None, 20.0), (3, 2, 10.0), (4, '*', 5.0)] (§2.9).

    LANE[[:INDEX]|[:*]]=MASS, comma separated: no suffix = the lane's strongest band (`None`),
    `:i` = the i-th band from the top (1-based `index`), `:*` = the whole-lane total.  MASS is a
    positive number (decimal or scientific) and whitespace around a token is tolerated.  Every bad
    input is one RuntimeError naming `--std` and the offending token: empty text, a token without
    `=`, a lane / index that is not a positive integer, a mass that is not a positive number, and
    the same lane named twice.
    """
    raw_text = "" if text is None else str(text)
    if not raw_text.strip():
        raise RuntimeError("--std 是空的（写成 2=20,3=10,4=5 这种：泳道[:带]=质量）")
    out, seen = [], set()
    for raw in raw_text.split(","):
        token = raw.strip()
        if not token:
            raise RuntimeError("--std 里有一段是空的：%r（逗号之间不能没有标准道）" % raw_text)
        left, eq, mass_text = token.partition("=")
        if not eq:
            raise RuntimeError("--std 里的一段没有 =：%r（写成 泳道[:带]=质量）" % token)
        lane_text, colon, band_text = left.strip().partition(":")
        if not re.fullmatch(r"[0-9]+", lane_text):
            raise RuntimeError("--std 里的泳道号不是正整数：%r（那段是 %r）" % (lane_text, token))
        lane = int(lane_text)
        if lane < 1:
            raise RuntimeError("--std 里的泳道号必须 ≥ 1：%r（那段是 %r）" % (lane_text, token))
        if colon:
            band_token = band_text.strip()
            if not band_token:
                raise RuntimeError("--std 里 %r 的冒号后面是空的（写带序号或 *）" % token)
            if band_token == "*":
                band = "*"
            elif re.fullmatch(r"[0-9]+", band_token):
                band = int(band_token)
                if band < 1:
                    raise RuntimeError("--std 里带序号必须 ≥ 1：%r（那段是 %r）" % (band_token, token))
            else:
                raise RuntimeError("--std 里带序号只能是 1、2 … 或 *：%r（那段是 %r）"
                                   % (band_token, token))
        else:
            band = None
        try:
            mass = float(mass_text.strip())
        except (TypeError, ValueError):
            raise RuntimeError("--std 里 %r 的质量不是数字：%r" % (token, mass_text.strip()))
        if not math.isfinite(mass) or mass <= 0.0:
            raise RuntimeError("--std 里 %r 的质量必须是正数" % token)
        if lane in seen:
            raise RuntimeError("--std 里泳道 %d 出现了两次（一个泳道只能给一个质量）" % lane)
        seen.add(lane)
        out.append((lane, band, mass))
    return out


def _curve_lanes(dens):
    """The dens lanes by number, refusing a dict `build_curve` cannot resolve."""
    if not isinstance(dens, dict):
        raise RuntimeError("curve：dens 不是 dict（收到 %s）" % type(dens).__name__)
    lanes = dens.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise RuntimeError("curve：dens 里没有 lanes（先用 measure 生成 dens.json）")
    by_no = {}
    for lane in lanes:
        if not isinstance(lane, dict):
            raise RuntimeError("curve：dens 的 lanes 里有不是对象的条目")
        try:
            no = int(lane.get("lane"))
        except (TypeError, ValueError):
            raise RuntimeError("curve：dens 的泳道条目缺少整数 lane（收到 %r）" % (lane.get("lane"),))
        if no in by_no:
            raise RuntimeError("curve：dens 里泳道 %d 出现了两次" % no)
        by_no[no] = lane
    return by_no


def _area_of(obj, lane_no, what, baseline):
    """The chosen baseline's area of a band / a lane → float; a missing or bad column is refused."""
    key = "area_" + baseline
    value = obj.get(key)
    if value is None:
        raise RuntimeError("泳道 %d 的%s没有 %s（dens.json 由 measure 生成时两种基线都有；重跑 measure）"
                           % (lane_no, what, key))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("泳道 %d 的%s的 %s 不是数字（收到 %r）" % (lane_no, what, key, value))
    value = float(value)
    if not math.isfinite(value):
        raise RuntimeError("泳道 %d 的%s的 %s 不是有限数（收到 %r）" % (lane_no, what, key, value))
    return value


def _std_area(lane, lane_no, band, baseline):
    """One `--std` entry → its area on the chosen baseline (§4.5 `entries` keeps `band` as typed).

    `band` is None (the lane's strongest band, resolved here), "*" (the whole-lane total) or a
    1-based index; the returned area is always a single band's `area_<baseline>` or, for "*", the
    lane's `area_<baseline>` — never a sum computed here.
    """
    bands = lane.get("bands") or []
    if not isinstance(bands, list):
        raise RuntimeError("泳道 %d 的 bands 不是列表" % lane_no)
    if band == "*":
        return _area_of(lane, lane_no, "整条泳道", baseline)
    if not bands:
        raise RuntimeError("标准道泳道 %d 没有带（--std 写成 %d:*=质量 取整条泳道）"
                           % (lane_no, lane_no))
    if band is None:
        index = next((i for i, b in enumerate(bands, 1)
                      if isinstance(b, dict) and b.get("strongest")), None)
        if index is None:
            raise RuntimeError("标准道泳道 %d 没有 strongest 带（--std 写成 %d:1=质量 指定第几个带）"
                               % (lane_no, lane_no))
    else:
        index = int(band)
        if index < 1:
            raise RuntimeError("标准道泳道 %d 的带序号必须 ≥ 1（收到 %d）" % (lane_no, index))
        if index > len(bands):
            raise RuntimeError("标准道泳道 %d 只有 %d 个带，--std 要的是第 %d 个"
                               % (lane_no, len(bands), index))
    band_dict = bands[index - 1]
    if not isinstance(band_dict, dict):
        raise RuntimeError("泳道 %d 的第 %d 个带不是对象" % (lane_no, index))
    return _area_of(band_dict, lane_no, "第 %d 个带" % index, baseline)


def _linfit(xs, ys):
    """Least squares y = a + b·x → (b, a, r2) with r2 = 1 − SSres / SStot (§4.5)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ssres = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    sstot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ssres / sstot if sstot > 0.0 else 1.0
    return b, a, r2


def _distinct_count(values):
    """How many values differ by more than FUZZ (the fit needs ≥ 2, else its slope is 0/0)."""
    ordered = sorted(values)
    count = 1 if ordered else 0
    for a, b in zip(ordered, ordered[1:]):
        if abs(b - a) > FUZZ:
            count += 1
    return count


def _require_two_points(used, fit):
    """Refuse a degenerate fit: ≥ 2 used points with at least 2 distinct volumes (§4.5)."""
    if len(used) < 2:
        raise RuntimeError("curve：%s 拟合至少需要 2 个标准点（现在 %d 个使用点）" % (fit, len(used)))
    distinct = _distinct_count(p["volume"] for p in used)
    if distinct < 2:
        raise RuntimeError("curve：%s 拟合需要至少 2 个面积不同的标准点（现在 %d 个使用点、%d 个不同面积）"
                           % (fit, len(used), distinct))


def _not_monotonic(fit, what, value):
    """The §4.5 monotonicity refusal: one line, starts with the fixed phrase, names plot-bands."""
    return RuntimeError("标准点不单调：%s 拟合的%s是 %.4f ≤ 0（质量没有随面积一起增大？先跑 plot-bands "
                        "看面积和标准点顺序）" % (fit, what, value))


def build_curve(dens, std, unit="ug", fit="linear", baseline="flank", drop=(),
                *, dens_path="", std_text=""):
    """dens.json + standard lanes → curve.json (§4.5).  Pure: no I/O, no rounding before the math.

    `std` = the list from `parse_std`; `drop` = lanes to keep with `used: false` (point level:
    every point holding an entry from a dropped lane keeps its volume / n / sd / cv_pct but is out
    of the fit, the range, the residuals and the notes).  `dens_path` / `std_text` are recorded
    verbatim in `source` (ruling R6, passed by the CLI as the user typed them; a dens dict built in
    memory leaves them empty).

    ValueError for a bad `fit` / `baseline` / `unit`; every data problem raises one Chinese
    RuntimeError (marker lane, unknown lane, missing band, non-monotonic fit, …).
    """
    if baseline not in BASELINES:
        raise ValueError("--baseline 只能是 flank 或 global（收到 %r）" % (baseline,))
    if fit not in FITS:
        raise ValueError("--fit 只能是 linear 或 power（收到 %r）" % (fit,))
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("--unit 不能是空的（默认 ug）")

    dropped = []
    for value in (drop or ()):
        try:
            no = int(value)
        except (TypeError, ValueError):
            raise RuntimeError("curve：--drop 的 %r 不是泳道号" % (value,))
        if no < 1:
            raise RuntimeError("curve：--drop 的泳道号必须 ≥ 1（收到 %r）" % (value,))
        dropped.append(no)
    dropped = sorted(set(dropped))

    if not isinstance(std, (list, tuple)) or not std:
        raise RuntimeError("curve：--std 是空的（没有标准道就没有曲线）")
    by_no = _curve_lanes(dens)
    entries, std_lanes = [], set()
    for item in std:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise RuntimeError("curve：标准道条目不是 (lane, band, mass)：%r" % (item,))
        lane_value, band, mass_value = item
        try:
            no = int(lane_value)
        except (TypeError, ValueError):
            raise RuntimeError("curve：标准道的泳道号不是整数（收到 %r）" % (lane_value,))
        if no < 1:
            raise RuntimeError("curve：标准道的泳道号必须 ≥ 1（收到 %r）" % (lane_value,))
        if no in std_lanes:
            raise RuntimeError("curve：--std 里泳道 %d 出现了两次（一个泳道只能给一个质量）" % no)
        if no not in by_no:
            raise RuntimeError("curve：--std 的泳道 %d 不在 dens 里（dens 有 %s）"
                               % (no, _runs(sorted(by_no))))
        lane = by_no[no]
        if lane.get("marker"):
            raise RuntimeError("curve：--std 的泳道 %d 是 marker 泳道（分子量梯不是标准道）" % no)
        try:
            mass = float(mass_value)
        except (TypeError, ValueError):
            raise RuntimeError("curve：泳道 %d 的质量不是数字（收到 %r）" % (no, mass_value))
        if not math.isfinite(mass) or mass <= 0.0:
            raise RuntimeError("curve：泳道 %d 的质量必须是正数（收到 %r）" % (no, mass_value))
        area = _std_area(lane, no, band, baseline)
        std_lanes.add(no)
        entries.append({"lane": no, "band": band, "mass": mass, "volume": area})

    unknown = [no for no in dropped if no not in std_lanes]
    if unknown:
        raise RuntimeError("curve：--drop 的泳道 %s 不在 --std 里（标准道是 %s）"
                           % (_runs(unknown), _runs(sorted(std_lanes))))

    # replicates collapse by mass (relative tolerance 1e-9, §2.7); points sort by mass ascending
    groups = []
    for entry in sorted(entries, key=lambda e: (e["mass"], e["lane"])):
        if groups and math.isclose(entry["mass"], groups[-1][0]["mass"], rel_tol=1e-9, abs_tol=0.0):
            groups[-1].append(entry)
        else:
            groups.append([entry])
    raw = []
    for group in groups:
        volumes = [e["volume"] for e in group]
        n = len(volumes)
        mass = sum(e["mass"] for e in group) / n
        volume = sum(volumes) / n
        sd = cv = None
        if n > 1:
            sd = math.sqrt(sum((v - volume) ** 2 for v in volumes) / (n - 1))
            cv = (sd / volume * 100.0) if volume > 0.0 else None
        raw.append({"mass": mass, "volume": volume, "n": n, "sd": sd, "cv_pct": cv,
                    "lanes": sorted(e["lane"] for e in group),
                    "entries": [[e["lane"], e["band"]] for e in sorted(group, key=lambda e: e["lane"])],
                    "used": not any(e["lane"] in dropped for e in group)})

    used = [p for p in raw if p["used"]]
    if not used:
        raise RuntimeError("curve：--drop 把所有标准点都弃用了（至少留 2 个能拟合的点）")
    _require_two_points(used, fit)
    for point in used:
        if point["mass"] <= 0.0:                     # rel_pct divides by it; parse_std refuses it
            raise RuntimeError("curve：标准点的质量必须 > 0（收到 %r）" % (point["mass"],))

    if fit == "linear":
        slope, intercept, r2 = _linfit([p["volume"] for p in used], [p["mass"] for p in used])
        if slope <= 0.0:
            raise _not_monotonic(fit, "斜率", slope)
        fit_params = {"slope": _num(slope, 4), "intercept": _num(intercept, 4),
                      "r2": _num(r2, 4), "n": len(used), "space": "linear"}
    else:
        for point in used:
            if point["volume"] <= 0.0:
                raise RuntimeError("curve：power 拟合要求面积 > 0（泳道 %s 的面积是 %.3f）"
                                   % ("/".join(str(v) for v in point["lanes"]), point["volume"]))
        exponent, log_c, r2 = _linfit([math.log10(p["volume"]) for p in used],
                                     [math.log10(p["mass"]) for p in used])
        if exponent <= 0.0:
            raise _not_monotonic(fit, "指数", exponent)
        fit_params = {"exponent": _num(exponent, 4), "scale": _num(10.0 ** log_c, 4),
                      "r2": _num(r2, 4), "n": len(used), "space": "log10"}

    points = [{"mass": _num(p["mass"], 3), "volume": _num(p["volume"], 3), "n": p["n"],
               "sd": None if p["sd"] is None else _num(p["sd"], 3),
               "cv_pct": None if p["cv_pct"] is None else _num(p["cv_pct"], 2),
               "lanes": p["lanes"], "entries": p["entries"], "used": p["used"]}
              for p in raw]

    residuals, raw_rel = [], []
    for point, shown in zip(raw, points):
        if not point["used"]:
            continue
        if fit == "linear":
            predicted = intercept + slope * point["volume"]
        else:
            predicted = (10.0 ** log_c) * point["volume"] ** exponent
        rel = (predicted - point["mass"]) / point["mass"] * 100.0     # note thresholds use this
        raw_rel.append(rel)
        residuals.append({"mass": shown["mass"], "volume": shown["volume"],
                          "predicted_mass": _num(predicted, 3), "rel_pct": _num(rel, 2)})

    shown_used = [point for point in points if point["used"]]
    rng = {"volume_min": min(p["volume"] for p in shown_used),
           "volume_max": max(p["volume"] for p in shown_used),
           "mass_min": min(p["mass"] for p in shown_used),
           "mass_max": max(p["mass"] for p in shown_used)}

    notes = []
    if len(shown_used) < 3:
        notes.append("只有 %d 个标准点，r² 不足以说明形状" % len(shown_used))
    if fit == "linear" and raw_rel and raw_rel[-1] < -10.0:
        notes.append("最高标准点比线性低 %.0f%%，可能已饱和；试 --fit power 或 --drop 它"
                     % (-raw_rel[-1]))
    for point, shown in zip(raw, points):
        if point["used"] and point["n"] > 1 and point["cv_pct"] is not None \
                and point["cv_pct"] > 15.0:
            notes.append("泳道 %s 的重复 CV %.0f%% > 15%%"
                         % ("/".join(str(v) for v in shown["lanes"]), point["cv_pct"]))

    return {
        "version": VERSION,
        "kind": CURVE_KIND,
        "unit": unit,
        "baseline": baseline,
        "fit": fit,
        "source": {"dens": str(dens_path), "std": str(std_text), "drop": dropped},
        "points": points,
        "fit_params": fit_params,
        "range": rng,
        "residuals": residuals,
        "notes": notes,
    }


def _dwidth(text):
    """Display width of a string: East Asian wide / fullwidth characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad_right(text, width):
    return text + " " * max(0, width - _dwidth(text))


def _pad_left(text, width):
    return " " * max(0, width - _dwidth(text)) + text


def _fmt_trim(value, nd):
    """2.0 → '2', 6.02 → '6.02': round to nd dp, drop trailing zeros, never print -0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    text = "%.*f" % (nd, number)
    if float(text) == 0.0:
        text = "%.*f" % (nd, 0.0)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _fmt_eq(value):
    """An equation parameter as the §5 sample prints it: 2 dp, trailing zeros dropped (`0.4`,
    `6.02`, `1.05`).  A nonzero parameter too small for 2 dp keeps two significant digits instead,
    so the printed equation can never claim a zero coefficient that the fit did not find."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number == 0.0 or not math.isfinite(number):
        return "0"
    if abs(number) < 0.005:
        digits = 1 - int(math.floor(math.log10(abs(number))))
        return _fmt_trim(number, min(8, digits))
    return _fmt_trim(number, 2)


def _fmt_fixed(value, nd):
    """r² always shows its 4 dp (a perfect fit reads 1.0000, not 1) — ruling R2's precision."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if round(number, nd) == 0.0:
        number = 0.0
    return "%.*f" % (nd, number)


def _fmt_value(value):
    """A measured mass / concentration as the terminal table prints it: up to 3 dp, trailing zeros
    dropped, at least one decimal kept (`0.522`, `0.052`, `5.0`).

    §5's sample (`3.1 ug`, `2.5 ug/uL`, `100%`) is unchanged by this rule, while real lanes whose
    concentrations differ in the third decimal no longer collapse to the same `0.1 ug/uL` (lead
    ruling on ep-bqv.5).  The JSON keeps ruling R2's fixed 3 dp — this is display only.  A value
    that rounds to zero keeps no sign, the convention `_fmt_signed` already documents.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number) or round(number, 3) == 0.0:
        number = 0.0
    text = "%.3f" % number
    text = text.rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def _fmt_sig(value):
    """Three significant digits, no scientific notation: 4.495 → '4.5', 12.0 → '12', 0.522 → '0.522'.

    The out-of-range note and the lane line share this formatter, so a measurement can never print
    the contradictory `面积 4.5 > 4.5` that separate precisions produced (area 4.495 against a
    maximum of 4.484, both rounded to 1 dp).  It matches `%.3g` wherever the note used to be
    readable; above 1000 `%.3g` switches to `1.2e+03`, which no note or table should show, so the
    fixed form is kept with three significant digits instead.  A whole number keeps no trailing
    decimal point (`12`).
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number == 0.0 or not math.isfinite(number):
        return "0"
    digits = 2 - int(math.floor(math.log10(abs(number))))
    if digits < 0:                       # ≥ 1000: round to three significant digits, no exponent
        number, digits = round(number, digits), 0
    # the cap keeps a tiny area from collapsing to a bare "0" in a note; 9 dp still shows three
    # significant digits down to 1e-7, and no realistic area ever needs more
    return _fmt_trim(number, min(9, digits))


def _fmt_signed(value, nd):
    """+0.8 / -1.5: a signed percentage (a value that rounds to zero keeps no sign)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if round(number, nd) == 0.0:
        number = 0.0
    return ("%+.*f" % (nd, number)) if number else ("%.*f" % (nd, 0.0))


def format_curve(curve):
    """Terminal table (§5): the equation line, one line per point, then `  注：` lines.

    Heaviest mass first, like the §5 sample (the JSON keeps the points mass-ascending).  A dropped
    point keeps its `（n=…）` block and shows `（已弃用）` where the residual would be; `None` never
    appears (a `n == 1` point has no CV, a dropped one no residual).
    """
    unit = str(curve.get("unit") or "ug")
    fit = curve.get("fit") or "linear"
    baseline = curve.get("baseline") or "flank"
    params = curve.get("fit_params") or {}
    if fit == "power":
        equation = "mass = %s·area^%s" % (_fmt_eq(params.get("scale")),
                                          _fmt_eq(params.get("exponent")))
    else:
        equation = "mass = %s + %s·area" % (_fmt_eq(params.get("intercept")),
                                            _fmt_eq(params.get("slope")))
    points = sorted(curve.get("points") or [], key=lambda p: float(p.get("mass") or 0.0),
                    reverse=True)
    used = [p for p in points if p.get("used")]
    repeats = sum(1 for p in used if int(p.get("n") or 0) > 1)
    head = ("[标曲] 基线 %s，拟合 %s（%s）：%s，r² %s，%d 点"
            % (baseline, fit, unit, equation, _fmt_fixed(params.get("r2"), 4), len(used)))
    if repeats:
        head += "（%d 个重复）" % repeats

    # residuals are listed for the used points in mass-ascending order (§4.5), so hand them out
    # point by point (a lookup by mass would silently break on an equal-mass pair)
    residual_list = list(curve.get("residuals") or [])
    residual_at = {}
    for point in sorted(points, key=lambda p: float(p.get("mass") or 0.0)):
        if point.get("used") and residual_list:
            residual_at[id(point)] = residual_list.pop(0)
    rows = []
    for point in points:
        n = int(point.get("n") or 0)
        cv = point.get("cv_pct")
        paren = "（n=%d，CV %s%%）" % (n, _fmt_trim(cv, 1)) if (n > 1 and cv is not None) \
            else "（n=%d）" % n
        if point.get("used"):
            residual = residual_at.get(id(point))
            column = ("残差 %s%%" % _fmt_signed(residual.get("rel_pct"), 1)
                      if residual else "残差 ?")
        else:
            column = "（已弃用）"
        rows.append(("%s %s" % (_fmt_trim(point.get("mass"), 3), unit),
                     _fmt_trim(point.get("volume"), 1), paren, column))
    width_mass = max((_dwidth(r[0]) for r in rows), default=0)
    width_volume = max((_dwidth(r[1]) for r in rows), default=0)
    width_paren = max((_dwidth(r[2]) for r in rows), default=0)
    body_width = _dwidth("面积 ") + width_volume + width_paren
    lines = [head]
    for mass_text, volume_text, paren, column in rows:
        body = "面积 " + _pad_left(volume_text, width_volume) + paren
        lines.append("  " + _pad_left(mass_text, width_mass) + "   " +
                     _pad_right(body, body_width) + "  " + column)
    for note in curve.get("notes") or []:
        lines.append("  注：%s" % note)
    return "\n".join(lines)


# ---------------------------------------------------------------- quantify (§2.8, §3.3; §4.6)
#
# dens.json + curve.json (+ the volume loaded per lane) ──► quant.json.  The area column is the one
# the curve was fitted on (`area_<baseline>`), and a band whose area lies outside the calibrated
# `range.volume_min … range.volume_max` gets NO number: mass null, the lane total and the
# concentration null, the band listed in `out_of_range_bands`, and a note naming it (§2.8 — the
# failure this chain exists to prevent is a plausible-looking extrapolation).
#
# A band whose area IS in range but whose fitted mass is not positive is rejected the same way (lead
# ruling): mass null, `out_of_range` stays false (the area really is inside the range), rel_pct null,
# the lane's total / concentration / share null, and one note naming the lane, the band and its area.
# A line that crosses zero inside its own standard range says nothing at the low end; a negative mass
# must never reach a total, and it is not written anywhere — not into the JSON, not into a note, not
# into the terminal block, not even as a diagnostic (lead ruling).
#
# The mass comes from ONE internal function, `_mass_from_area`, which evaluates the STORED fit
# parameters forward (volume → mass), the direction §4.5 `fit_params` and §4.7's printed equation
# define and the one §7.8/§7.9's recovery checks are calibrated against (lead ruling for ep-bqv.5).

QUANT_KIND = "ep-craft-densitometry-quant"
QUANT_SUFFIX = ".quant.json"       # default: <document stem>.quant.json, i.e. fig.curve.json → fig.quant.json

# ONE template for the "skipped standard lanes" note: `quantify` writes it, `format_quant` reads the
# count back out of it (`_SKIP_NOTE_RE` is compiled from this very string, never hand-written), because
# §4.6's exhaustive key list has no field for it and the note is the only record.
SKIP_NOTE = "已跳过 %d 条标准道（泳道 %s）"
_SKIP_NOTE_RE = re.compile(re.escape(SKIP_NOTE).replace("%d", "(\\d+)").replace("%s", "(.+)"))


def _finite_number(value):
    """A real, finite number (bool excluded) — the numeric contract of every chain file."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def parse_load(text):
    """'9=2,10=2.5' → {9: 2.0, 10: 2.5}: microlitres loaded per lane (the spec lists no parser).

    Comma separated `LANE=UL`; LANE a positive integer, UL a positive number, whitespace tolerated.
    None or "" → {}.  Every bad input is one RuntimeError naming `--load` and the offending token:
    an empty segment, a token without `=`, a lane that is not a positive integer, a volume that is
    not a positive number, and the same lane named twice.
    """
    raw_text = "" if text is None else str(text)
    if not raw_text.strip():
        return {}
    out, seen = {}, set()
    for raw in raw_text.split(","):
        token = raw.strip()
        if not token:
            raise RuntimeError("--load 里有一段是空的：%r（逗号之间不能没有 泳道=体积）" % raw_text)
        left, eq, ul_text = token.partition("=")
        if not eq:
            raise RuntimeError("--load 里的一段没有 =：%r（写成 泳道=体积）" % token)
        lane_text = left.strip()
        if not re.fullmatch(r"[0-9]+", lane_text):
            raise RuntimeError("--load 里的泳道号不是正整数：%r（那段是 %r）" % (lane_text, token))
        lane = int(lane_text)
        if lane < 1:
            raise RuntimeError("--load 里的泳道号必须 ≥ 1：%r（那段是 %r）" % (lane_text, token))
        try:
            volume = float(ul_text.strip())
        except (TypeError, ValueError):
            raise RuntimeError("--load 里 %r 的体积不是数字：%r" % (token, ul_text.strip()))
        if not math.isfinite(volume) or volume <= 0.0:
            raise RuntimeError("--load 里 %r 的体积必须是正数" % token)
        if lane in seen:
            raise RuntimeError("--load 里泳道 %d 出现了两次（一个泳道只能给一个体积）" % lane)
        seen.add(lane)
        out[lane] = volume
    return out


def _quant_range(curve):
    """`range.volume_min … range.volume_max` of a curve (full precision, as build_curve stored it)."""
    rng = curve.get("range")
    if not isinstance(rng, dict):
        raise RuntimeError("quantify：curve 里没有 range（先用 curve 生成 curve.json）")
    bounds = []
    for key in ("volume_min", "volume_max"):
        value = rng.get(key)
        if not _finite_number(value):
            raise RuntimeError("quantify：curve 的 range 缺少数值 %s（重跑 curve）" % key)
        bounds.append(float(value))
    if bounds[1] < bounds[0]:
        raise RuntimeError("quantify：curve 的 range 反了（volume_min %.3f > volume_max %.3f）"
                           % (bounds[0], bounds[1]))
    return bounds[0], bounds[1]


def _quant_fit(curve):
    """Validate `fit` and the STORED `fit_params` → `(params, fit)`, refusing what cannot be used.

    Only the two models `curve` fits exist.  A hand-edited curve.json could carry a slope / scale
    that is zero or negative (which would make the mapping meaningless), so that is refused too.
    """
    fit = curve.get("fit")
    if fit not in FITS:
        raise RuntimeError("quantify：curve 的 fit 不是 linear 或 power（收到 %r）" % (fit,))
    params = curve.get("fit_params")
    if not isinstance(params, dict):
        raise RuntimeError("quantify：curve 里没有 fit_params（先用 curve 生成 curve.json）")
    if fit == "linear":
        keys, positive = ("intercept", "slope"), "slope"
    else:
        keys, positive = ("scale", "exponent"), "scale"
    for key in keys:
        if not _finite_number(params.get(key)):
            raise RuntimeError("quantify：curve 的 fit_params 缺少数值 %s（重跑 curve）" % key)
    if float(params[positive]) <= 0.0:
        raise RuntimeError("quantify：curve 的 fit_params.%s 不是正数（收到 %r；重跑 curve）"
                           % (positive, params[positive]))
    return params, fit


def _quant_std_lanes(curve):
    """Standard lane numbers: every lane in ANY `points[*].entries`, used or dropped (§4.6).

    `entries` is the only trail back from a fitted point to the lanes it came from, so a curve
    without it cannot say which dens lanes are standards — one line, refuse.
    """
    points = curve.get("points")
    if not isinstance(points, list):
        raise RuntimeError("quantify：curve 里没有 points 列表（先用 curve 生成 curve.json）")
    lanes = set()
    for pos, point in enumerate(points, 1):
        if not isinstance(point, dict):
            raise RuntimeError("quantify：curve 的 points 里有不是对象的条目")
        entries = point.get("entries")
        if not isinstance(entries, list):
            raise RuntimeError("quantify：curve 的 points 第 %d 个点缺少 entries（重跑 curve）" % pos)
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise RuntimeError("quantify：curve 的 points 第 %d 个点的 entries 条目不是 [泳道, 带]：%r"
                                   % (pos, entry))
            lane = entry[0]
            if isinstance(lane, bool) or not isinstance(lane, int) or lane < 1:
                raise RuntimeError("quantify：curve 的 entries 里的泳道号不是正整数：%r" % (lane,))
            lanes.add(lane)
    return lanes


def _quant_dens_lanes(dens):
    """The dens lanes in report order, refusing a dict `quantify` cannot read (lane / bands shape)."""
    lanes = dens.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise RuntimeError("quantify：dens 里没有 lanes（先用 measure 生成 dens.json）")
    ordered, seen = [], set()
    for lane in lanes:
        if not isinstance(lane, dict):
            raise RuntimeError("quantify：dens 的 lanes 里有不是对象的条目")
        no = lane.get("lane")
        if isinstance(no, bool) or not isinstance(no, int) or no < 1:
            raise RuntimeError("quantify：dens 的泳道条目缺少正整数 lane（收到 %r）" % (no,))
        if no in seen:
            raise RuntimeError("quantify：dens 里泳道 %d 出现了两次" % no)
        if not isinstance(lane.get("bands"), list):
            raise RuntimeError("quantify：dens 的泳道 %d 没有 bands 列表（重跑 measure）" % no)
        seen.add(no)
        ordered.append(lane)
    return ordered


def _quant_load(load, lanes):
    """Validate the `{lane: ul}` dict (from `parse_load`) against the lanes dens actually has."""
    if load is None:
        return {}
    if not isinstance(load, dict):
        raise RuntimeError("quantify：load 不是 dict（收到 %s）" % type(load).__name__)
    out = {}
    for key, value in load.items():
        if isinstance(key, bool) or not isinstance(key, int) or key < 1:
            raise RuntimeError("quantify：load 的键不是泳道号：%r（写成 {9: 2.0}）" % (key,))
        if not _finite_number(value) or float(value) <= 0.0:
            raise RuntimeError("quantify：load 里泳道 %d 的体积必须是正数（收到 %r）" % (key, value))
        if key not in lanes:
            raise RuntimeError("quantify：--load 里的泳道 %d 不在 dens 里（dens 有 %s）"
                               % (key, _runs(sorted(lanes))))
        out[key] = float(value)
    return out


def _range_advice(below):
    """The direction-specific advice of §2.8 / the ticket: too little stain vs too much."""
    return "多加点或浓缩后再跑" if below else "稀释或少点一点再跑"


def _out_of_range_note(lane_no, index, area, vmin, vmax, below, total_is_none):
    """One Chinese sentence per out-of-range band (§4.6): what was measured, the range, the advice."""
    text = ("泳道 %d 的带 %d 面积 %s 超出标曲范围（%s–%s）：%s"
            % (lane_no, index, _fmt_sig(area), _fmt_sig(vmin), _fmt_sig(vmax),
               _range_advice(below)))
    if total_is_none:
        text += "；该道的合计与浓度不给数"
    return text


def _non_positive_note(lane_no, index, area):
    """One Chinese sentence for an in-range band whose fitted mass is not positive (lead ruling).

    The area is inside `range.volume_min … range.volume_max`, so this is not `out_of_range`; what
    failed is the fit itself at the low end (the line crosses zero inside its own standard range).
    The area uses `_fmt_sig`, the same formatter the out-of-range note uses.

    The value the fit produced is NOT included, not even as a diagnostic: a mass <= 0 must never
    appear in quant.json or in the terminal block, and a negative number leaked through a note
    still counts as leaking it (lead ruling).  The advice names the two fixes instead.
    """
    return ("泳道 %d 的带 %d 面积 %s 在标曲范围内，但标曲给出的质量不为正："
            "标曲低面积端不可信，试 --drop 最低标准点或 --fit power 后重跑 curve"
            % (lane_no, index, _fmt_sig(area)))


def _non_positive_reason():
    """What a lane line prints where the mass would be: the fit failed, and how to fix the curve.

    One sentence, same advice as the note (§2.8's rule that a number the fit cannot support is
    never printed); the band's own index / MW / area stay in front of it, so the slot still says
    which band it is.
    """
    return "— 标曲给出的质量不为正：试 --drop 最低标准点或 --fit power"


def _mass_from_area(area, curve, fit_params=None):
    """area (the curve's baseline column) → protein mass in `curve["unit"]`, full precision.

    Evaluates the STORED fit FORWARD (volume → mass), the direction §4.5 `fit_params` and §4.7's
    printed equation define, and the one §7.8/§7.9's recovery checks are calibrated against
    (lead ruling for ep-bqv.5; §4.6's "inverse of the fit" is the same equation read for its
    response, not its predictor).  Nothing is refitted, so a reader of curve.json and one of
    quant.json see the same numbers:

        linear  mass = intercept + slope · area
        power   mass = scale · area ** exponent        (scale = 10^c, as build_curve stores it)

    `area` has already been checked to lie inside `range.volume_min … range.volume_max` within
    FUZZ, so both models are evaluated inside the calibrated band only.  `fit_params` is the
    already-validated dict when `quantify` has one, otherwise it is read from `curve` (and refused
    the same way).  A result that is not a finite number raises — it never becomes a NaN in the
    JSON (a curve fitted with a negative intercept and an area below mass 0 would do that).
    Rounding happens only when `quantify` builds the returned dict (ruling R2).
    """
    if fit_params is None:
        fit_params, _unused = _quant_fit(curve)
    fit = curve.get("fit")
    if fit == "linear":
        intercept, slope = fit_params["intercept"], fit_params["slope"]
        mass = float(intercept) + float(slope) * float(area)
    else:
        scale, exponent = fit_params["scale"], fit_params["exponent"]
        if area < 0.0 or (area == 0.0 and float(exponent) < 0.0):
            raise RuntimeError("quantify：幂律标曲不能算负面积的 %r 次方（面积 %r）"
                               % (exponent, area))
        mass = float(scale) * float(area) ** float(exponent)
    if not _finite_number(mass):
        raise RuntimeError("quantify：面积 %r 按标曲算出的质量不是有限数（%r；标曲与轴可能反了）"
                           % (area, mass))
    return mass   # callers must refuse a non-positive value; it is never a reportable mass


def quantify(dens, curve, load=None, *, dens_path="", curve_path="", load_text=""):
    """dens.json + curve.json (+ loaded volumes) → quant.json (§4.6).  Pure: no I/O, no rounding
    before the maths.

    Lane order is the dens order; the standard lanes (any lane named in `curve["points"][*]
    ["entries"]`, used or dropped) are left out of the table and counted in one note.  Every area
    is read from the column the curve was fitted on (`area_<baseline>`), and a band outside
    `range.volume_min … range.volume_max` gets `mass: None` + `out_of_range: True` + a note; that
    lane's `mass_total` / `conc` / `frac_pct_among_lanes` are then `None` too (§2.8).  In-range
    bands are converted by `_mass_from_area`, which evaluates the stored fit forward (see its
    docstring); when that value is not positive the band also gets `mass: None` + `rel_pct: None`
    with `out_of_range: False` (the area really is inside the range) and one note naming the lane,
    the band and its area, and the lane's total / concentration / share drop out exactly as above
    (lead ruling: a line crossing zero inside its own standard range must not reach a total).  The
    non-positive value itself is not written anywhere — not into the JSON, not into the note, not
    into the terminal line.
    No JSON key changes; only `bands[*].mass` and the lane totals become `None`.

    `load` is the dict from `parse_load` (`{lane: ul}`); a lane that is not in dens is refused.
    `dens_path` / `curve_path` / `load_text` are recorded verbatim in `source` (ruling R6) and feed
    nothing else.  Bad `baseline` / `fit` / `fit_params`, a missing key the function needs, or a
    load lane that dens does not have raise one Chinese RuntimeError; none of the return values is
    ever a NaN.
    """
    if not isinstance(dens, dict):
        raise RuntimeError("quantify：dens 不是 dict（收到 %s）" % type(dens).__name__)
    if not isinstance(curve, dict):
        raise RuntimeError("quantify：curve 不是 dict（收到 %s）" % type(curve).__name__)
    baseline = curve.get("baseline")
    if baseline not in BASELINES:
        raise RuntimeError("quantify：curve 的 baseline 不是 flank 或 global（收到 %r）" % (baseline,))
    fit_params, fit = _quant_fit(curve)
    unit = curve.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise RuntimeError("quantify：curve 里没有 unit（先用 curve 生成 curve.json）")
    vmin, vmax = _quant_range(curve)
    std_lanes = _quant_std_lanes(curve)

    ordered = _quant_dens_lanes(dens)
    lane_numbers = {lane["lane"] for lane in ordered}
    loads = _quant_load(load, lane_numbers)

    skipped_nos = [lane["lane"] for lane in ordered if lane["lane"] in std_lanes]
    notes = []
    if skipped_nos:
        notes.append(SKIP_NOTE % (len(skipped_nos), _runs(skipped_nos)))

    rows, raw_totals = [], []
    for lane in ordered:
        no = lane["lane"]
        if no in std_lanes:
            continue
        lane_load = loads.get(no)
        bands, raw_masses, out_bands, pending, nonpos = [], [], [], [], []
        for pos, band in enumerate(lane["bands"], 1):
            if not isinstance(band, dict):
                raise RuntimeError("quantify：泳道 %d 的第 %d 个带不是对象" % (no, pos))
            index = band.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 1:
                raise RuntimeError("quantify：泳道 %d 的第 %d 个带的 index 不是正整数（收到 %r；重跑 measure）"
                                   % (no, pos, index))
            y_cm = band.get("y_cm")
            if not _finite_number(y_cm):
                raise RuntimeError("quantify：泳道 %d 的第 %d 个带缺少数值 y_cm（重跑 measure）"
                                   % (no, index))
            area = _area_of(band, no, "第 %d 个带" % index, baseline)
            below = area < vmin - FUZZ
            above = area > vmax + FUZZ
            if below or above:
                out_bands.append(index)
                pending.append((index, area, below))
                mass = None
            else:
                mass = _mass_from_area(area, curve, fit_params)
                if not _finite_number(mass):
                    raise RuntimeError("quantify：泳道 %d 的第 %d 个带算出的质量不是有限数（%r）"
                                       % (no, index, mass))
                mass = float(mass)
                if mass <= 0.0:
                    # the value itself is deliberately not kept: it must not reach a note, a lane
                    # line or the JSON, so only the band it belongs to is recorded
                    nonpos.append((index, area))
                    mass = None
            raw_masses.append(mass)
            bands.append({"index": index, "y_cm": _num(y_cm, 4), "mw_kda": band.get("mw_kda"),
                          "area": _num(area, 3), "mass": None if mass is None else _num(mass, 3),
                          "out_of_range": bool(below or above), "rel_pct": None})

        total = None if (out_bands or nonpos) else sum(raw_masses)
        for index, area, below in pending:
            notes.append(_out_of_range_note(no, index, area, vmin, vmax, below, total is None))
        for index, area in nonpos:
            notes.append(_non_positive_note(no, index, area))
        if total is not None and total != 0.0:
            for band, mass in zip(bands, raw_masses):
                if mass is not None:
                    band["rel_pct"] = _num(mass / total * 100.0, 2)

        conc = None if (lane_load is None or total is None) else _num(total / lane_load, 3)
        raw_totals.append(total)
        rows.append({"lane": no, "label": lane.get("label"),
                     "load_ul": None if lane_load is None else float(lane_load),
                     "bands": bands, "mass_total": None if total is None else _num(total, 3),
                     "conc": conc, "out_of_range": bool(out_bands),
                     "out_of_range_bands": out_bands, "frac_pct_among_lanes": None})

    known = [total for total in raw_totals if total is not None]
    overall = sum(known)
    for row, total in zip(rows, raw_totals):
        if total is None or overall <= 0.0:
            continue
        row["frac_pct_among_lanes"] = _num(total / overall * 100.0, 2)

    load_source = "" if load_text is None else str(load_text)
    return {
        "version": VERSION,
        "kind": QUANT_KIND,
        "unit": unit,
        "baseline": baseline,
        "fit": fit,
        "source": {"dens": str(dens_path), "curve": str(curve_path),
                   "load": load_source if load_source.strip() else None},
        "lanes": rows,
        "overall_mass": _num(overall, 3),
        "notes": notes,
    }


def _skipped_count(quant):
    """How many standard lanes the table skipped, read back from the one note that states it.

    §4.6's key list has no dedicated field, so the note is the only record: the pattern is compiled
    from `SKIP_NOTE`, the very template `quantify` writes, so writer and reader cannot drift.  When
    the note is absent (nothing was skipped) the header simply omits the clause.
    """
    for note in quant.get("notes") or []:
        match = _SKIP_NOTE_RE.match(str(note))
        if match:
            return int(match.group(1))
    return 0


def _quant_header(quant, curve=None):
    """§5 header: baseline · fit · unit, the standard range / point count / r², skipped standards.

    The range and the fit parameters are NOT in quant.json (§4.6's key list is fixed), so the
    CLI passes the curve it just read (display metadata only, never persisted); without it the
    header keeps every part it can still derive from the quant dict.
    """
    baseline = quant.get("baseline") or "flank"
    fit = quant.get("fit") or "linear"
    unit = str(quant.get("unit") or "ug")
    head = "[定量] 基线 %s · %s · %s" % (baseline, fit, unit)
    bits = []
    rng = curve.get("range") if isinstance(curve, dict) else None
    params = curve.get("fit_params") if isinstance(curve, dict) else None
    if isinstance(rng, dict) and _finite_number(rng.get("mass_min")) \
            and _finite_number(rng.get("mass_max")):
        bits.append("标曲 %s–%s %s" % (_fmt_trim(rng["mass_min"], 3),
                                       _fmt_trim(rng["mass_max"], 3), unit))
    if isinstance(params, dict):
        n = params.get("n")
        if isinstance(n, int) and not isinstance(n, bool):
            bits.append("%d 点" % n)
        if _finite_number(params.get("r2")):
            bits.append("r² %s" % _fmt_fixed(params["r2"], 4))
    if bits:
        head += "（%s）" % "，".join(bits)
    skipped = _skipped_count(quant)
    if skipped:
        head += "；跳过 %d 条标准道" % skipped
    return head


def _band_text(band, unit, curve=None):
    """One band of a lane line (§5): mass + share, or the rejected band's reason in one slot.

    Two rejections print no number: the area is outside the standard range (`out_of_range`, whose
    comparison needs `range.volume_min … range.volume_max` — a value §4.6 does not let quant.json
    keep, hence the optional `curve`), or the area is in range but the fit gives a non-positive
    mass (`mass is None` while `out_of_range` is false, lead ruling).

    Precision: the measured mass and the lane total use `_fmt_value` (up to 3 dp, trailing zeros
    trimmed) so two real concentrations no longer collapse into one printed value; the in-range
    area keeps §5's single decimal, and the out-of-range comparison uses `_fmt_sig` — the same
    three significant digits the note prints — so a measurement can never read `面积 4.5 > 4.5`
    when it is really 4.495 against 4.484.
    """
    mw = band.get("mw_kda")
    mw_text = ("%.1f kDa" % float(mw)) if _finite_number(mw) else "—"
    area = band.get("area")
    if band.get("out_of_range"):
        area_text = _fmt_sig(area) if _finite_number(area) else "?"
        rng = curve.get("range") if isinstance(curve, dict) else None
        if isinstance(rng, dict) and _finite_number(rng.get("volume_min")) \
                and _finite_number(rng.get("volume_max")) and _finite_number(area):
            vmin, vmax = float(rng["volume_min"]), float(rng["volume_max"])
            below = float(area) < vmin - FUZZ
            bound = vmin if below else vmax
            return "—  超出标曲范围（面积 %s %s %s）：%s" % (
                area_text, "<" if below else ">", _fmt_sig(bound), _range_advice(below))
        return "—  超出标曲范围（面积 %s）" % area_text
    text = "带%d %s  面积 %s" % (band.get("index") or 0, mw_text,
                                 _fmt_fixed(area, 1) if _finite_number(area) else "?")
    if band.get("mass") is None:
        return text + "  " + _non_positive_reason()
    text += "  %s %s" % (_fmt_value(band["mass"]), unit)
    if _finite_number(band.get("rel_pct")):
        text += " (%.0f%%)" % band["rel_pct"]
    return text


def format_quant(quant, *, curve=None):
    """Terminal table (§5): the header, one line per sample lane, then `  注：` lines.

    `curve` is optional display metadata: it supplies the standard range / point count / r² and the
    out-of-range bounds, none of which §4.6 lets quant.json carry.  `None` never appears in the
    output: a value that is absent (no load, an unknown total, an unknown share) is simply omitted,
    and an out-of-range band prints `—` with the range comparison and the advice instead of a mass.
    """
    unit = str(quant.get("unit") or "ug")
    lanes = [lane for lane in (quant.get("lanes") or []) if isinstance(lane, dict)]
    label_w = max((_dwidth(str(lane.get("label"))) for lane in lanes if lane.get("label")),
                  default=0)
    lines = [_quant_header(quant, curve)]
    for lane in lanes:
        body = "  泳道 " + _pad_left("%d" % int(lane.get("lane") or 0), 2)
        if label_w:
            body += " " + _pad_right(str(lane.get("label") or ""), label_w)
        parts = [_band_text(band, unit, curve)
                 for band in (lane.get("bands") or []) if isinstance(band, dict)]
        body += "  " + (" · ".join(parts) if parts else "（无带）")
        tail = []
        if _finite_number(lane.get("mass_total")):
            tail.append("合计 %s %s" % (_fmt_value(lane["mass_total"]), unit))
            if _finite_number(lane.get("conc")):
                tail.append("浓度 %s %s/uL" % (_fmt_value(lane["conc"]), unit))
            if _finite_number(lane.get("frac_pct_among_lanes")):
                tail.append("占全胶 %.0f%%" % lane["frac_pct_among_lanes"])
        if tail:
            body += " │ " + "  ".join(tail)
        lines.append(body)
    for note in quant.get("notes") or []:
        lines.append("  注：%s" % note)
    return "\n".join(lines)
