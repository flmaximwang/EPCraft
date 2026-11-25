"""
bands — the §4.1 band math: smoothing, the ink walk, integration windows, the flank ramp.

All functions are pure over lists from a report v2 profile: `smooth` is the centred moving mean
the walk runs on, `walk_window` is the monotonic outward walk, `area_of` integrates one window,
and `flank_ramp` / `_flank_levels` build the straight ramp through the median gray of the rows
beside a band (§2.3).  Full precision — rounding belongs to the caller that builds the dict
(ruling R2).
"""

import bisect

from .contracts import FUZZ, SMOOTH_CM
from .values import _dy, _median, _nearest, _rows_in

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
