"""
values — the scalar and row helpers every densitometry stage shares.

They are deliberately tiny and side-effect free: rounding for the built dicts (ruling R2: round
only when the returned dict is built), the median, the compact lane list of the messages, the
uniform-y contract of a report profile, and the numeric contract of every chain file.
"""

import bisect
import math

from .contracts import FUZZ, MAX_DY_SPREAD

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


def _num_or_none(value, nd):
    """`_num` for a statistic that may be UNDEFINED: `None` in, `None` out (never NaN, never 0).

    `_linfit` leaves its standard errors undefined when the fit has no residual degrees of freedom
    (n <= 2); writing `0.0` there would claim a perfect fit, and the JSON must not carry NaN
    either, so an absent statistic stays absent all the way into `curve.json` (ep-c1i §3).
    """
    return None if value is None else _num(value, nd)


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

def _finite_number(value):
    """A real, finite number (bool excluded) — the numeric contract of every chain file."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
