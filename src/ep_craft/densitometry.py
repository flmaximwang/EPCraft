#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""densitometry — band areas from a report v2 profile; the offline first stage of the chain.

    report v2 (fig.mw.json) ──► measure() ──► dens.json

Everything is a pure function over dicts (spec `specs/ep-craft-densitometry.md` §2.1-§2.4, §4.1-4.4):
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
import os
import re

VERSION = 1
KIND = "ep-craft-densitometry"
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
