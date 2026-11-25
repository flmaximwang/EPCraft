"""
curve — §2.6, §2.7, §2.9, §4.5: dens.json + the `--std` standard lanes → curve.json.

Two fits, both cheap (§2.6): linear `mass = intercept + slope·volume` and power
`mass = scale · volume^exponent` (least squares in log10 for both axes, r2 in log space).  Nothing
is extrapolated: the fit is fitted, printed with its residuals and refused when it is not
monotonic — the plot is the judge (§2.6, and the `plot-bands` hint in the §4.5 error messages).

Standard entries: `parse_std` owns the `LANE[[:INDEX]|[:*]]=MASS` grammar, `build_curve` owns the
data.  Replicates (entries with the same mass) collapse into one point (n / sd / cv_pct / lanes /
entries, §2.7), `drop` keeps a point with `used: false` so the plot can show it.  The stored
fit_params are rounded to 4 dp (ruling R2), so `quantify` evaluates the STORED numbers (forward),
never a refit.
"""

import math
import re

from .bands import _flank_levels, _ramp_at, _ramp_dict, area_of
from .contracts import BASELINES, CURVE_KIND, FITS, FLANK_CM, FUZZ, VERSION
from .values import _area_of, _num, _num_or_none, _runs
from .layout import _dwidth, _fmt_eq, _fmt_fixed, _fmt_signed, _fmt_trim, _pad_left, _pad_right

def parse_std(text):
    """'2=20,3:2=10,4:*=5' → [(2, None, 20.0), (3, 2, 10.0), (4, '*', 5.0)] (§2.9).

    LANE[[:INDEX]|[:*]]=MASS, comma separated: no suffix = the lane's strongest band (`None`),
    `:i` = the i-th band from the top (1-based `index`), `:*` = the whole-lane total.  MASS is 0
    (a blank standard lane, §4.5) or a positive number (decimal or scientific) and whitespace
    around a token is tolerated.  Every bad input is one RuntimeError naming `--std` and the
    offending token: empty text, a token without `=`, a lane / index that is not a positive
    integer, a negative or non-finite mass, and the same lane named twice.
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
        if not math.isfinite(mass) or mass < 0.0:
            raise RuntimeError("--std 里 %r 的质量必须是 0（空白标准道）或正数（收到 %r）"
                               % (token, mass_text.strip()))
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

def _std_band(lane, lane_no, band):
    """One `--std` band selector → (1-based index, band dict): None = the lane's strongest band.

    A lane with no detected bands has no band to name.  `--std` either takes the whole lane with
    `:*` or marks the lane blank with mass 0 and no band selector (§4.5); an explicit band index
    on such a lane is refused with a message that says so.
    """
    bands = lane.get("bands") or []
    if not isinstance(bands, list):
        raise RuntimeError("泳道 %d 的 bands 不是列表" % lane_no)
    if not bands:
        if band is None:
            raise RuntimeError("标准道泳道 %d 没有带（--std 写成 %d:*=质量 取整条泳道）"
                               % (lane_no, lane_no))
        raise RuntimeError("标准道泳道 %d 没有带，不能选第 %s 个带（--std 写成 %d=0 当空白标准道，"
                           "或 %d:*=质量 取整条泳道）"
                           % (lane_no, band, lane_no, lane_no))
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
    return index, band_dict


def _std_area(lane, lane_no, band, baseline):
    """One `--std` entry → its area on the chosen baseline (§4.5 `entries` keeps `band` as typed).

    `band` is None (the lane's strongest band, resolved here), "*" (the whole-lane total) or a
    1-based index; the returned area is always a single band's `area_<baseline>` or, for "*", the
    lane's `area_<baseline>` — never a sum computed here.  A blank standard lane (no detected
    bands, mass 0) is integrated by `_blank_area` before this is called.
    """
    bands = lane.get("bands") or []
    if not isinstance(bands, list):
        raise RuntimeError("泳道 %d 的 bands 不是列表" % lane_no)
    if band == "*":
        return _area_of(lane, lane_no, "整条泳道", baseline)
    index, band_dict = _std_band(lane, lane_no, band)
    return _area_of(band_dict, lane_no, "第 %d 个带" % index, baseline)


def _profile_arrays(lane, lane_no):
    """(y_cm, gray, baseline_global) of one dens lane as float lists (the §4.1 profile)."""
    prof = lane.get("profile")
    if not isinstance(prof, dict):
        raise RuntimeError("泳道 %d 没有 profile（dens.json 由 measure 生成）" % lane_no)
    try:
        y = [float(v) for v in prof["y_cm"]]
        gray = [float(v) for v in prof["gray"]]
        base = [float(v) for v in prof["baseline_global"]]
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("泳道 %d 的 profile 缺少数字 y_cm / gray / baseline_global" % lane_no)
    if not y or len(y) != len(gray) or len(y) != len(base):
        raise RuntimeError("泳道 %d 的 profile 三条数组长度不一致（%d / %d / %d）"
                           % (lane_no, len(y), len(gray), len(base)))
    return y, gray, base


def _blank_window(windows, y):
    """The window a blank standard lane inherits, clamped to its own profile (§4.5).

    `windows` are the `window_<baseline>` pairs of the measured single-band standards: the blank
    lane has no peak to walk, so it is integrated where those standards' bands sit — at their
    average centre and average width.  Returns (y0, y1) inside [y[0], y[-1]].
    """
    if not windows:
        raise RuntimeError("curve：空白标准道需要至少一个非空白的单带标准道来提供窗口")
    mean_c = sum(0.5 * (a + b) for a, b in windows) / len(windows)
    mean_w = sum(abs(b - a) for a, b in windows) / len(windows)
    lo, hi = float(y[0]), float(y[-1])
    y0 = min(hi, max(lo, mean_c - 0.5 * mean_w))
    y1 = min(hi, max(lo, mean_c + 0.5 * mean_w))
    if y1 < y0:
        y0, y1 = y1, y0
    return y0, y1


def _blank_area(dens, lane, lane_no, windows, baseline):
    """A blank standard lane's area: its own profile integrated inside the inherited window.

    The lane has no detected bands, so `measure` wrote neither a window nor an area for it.  The
    ink is built on the blank lane's own profile with the same flank / global definition `measure`
    uses (`_flank_levels` + `_ramp_dict` for flank, the profile's `baseline_global` for global), so
    the number is comparable with the real standard bands and the blank point can anchor the curve
    at mass 0 (§4.5).
    """
    y, gray, base = _profile_arrays(lane, lane_no)
    y0, y1 = _blank_window(windows, y)
    if baseline == "flank":
        flank_cm = float((dens.get("params") or {}).get("flank_cm") or FLANK_CM)
        left, right = _flank_levels(y, gray, y0, y1, y[0], y[-1], flank_cm, base)
        ramp = _ramp_dict(left, right)
        ink = [_ramp_at(ramp, value) - g for value, g in zip(y, gray)]
    else:
        ink = [b - g for b, g in zip(base, gray)]
    return area_of(y, ink, y0, y1)


def _linfit(xs, ys):
    """Ordinary least squares y = a + b·x → a dict of the fit AND its sampling statistics (§4.5).

    Returns `n`, `mx`, `sxx` (Σ(x − x̄)²), `b`, `a`, `r2` plus the error numbers ep-c1i needs:
    `s` (residual standard deviation `sqrt(SSres / (n − 2))`), `se_b` (`s / sqrt(sxx)`), `se_a`
    (`s · sqrt(1/n + x̄²/sxx)`) and `cov_ab` (`−s² · x̄ / sxx`).  A dict, not the old `(b, a, r2)`
    tuple, because the callers no longer want only the line: `curve` stores the slope / intercept
    standard errors and `sxx` / `mx`, and `quantify` turns those into the per-sample prediction SD
    without ever refitting (ep-c1i §2-§4).

    `s`, `se_b`, `se_a` and `cov_ab` are `None` when `n <= 2`: with no residual degrees of
    freedom the residual scatter is not estimable, and a `0.0` there would claim a perfect fit —
    every propagated uncertainty downstream stays `None` instead (ep-c1i §3).  `r2` keeps its old
    behaviour exactly (`1.0` when SStot is 0), because a degenerate fit is still defined.

    Callers guarantee `n >= 2` with at least two distinct x (`_require_two_points`), so `sxx > 0`
    here; nothing in this module calls it on a degenerate set.
    """
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
    s = se_b = se_a = cov_ab = None
    if n > 2:
        s = math.sqrt(ssres / (n - 2))
        se_b = s / math.sqrt(sxx)
        se_a = s * math.sqrt(1.0 / n + mx ** 2 / sxx)
        cov_ab = -s ** 2 * mx / sxx
    return {"n": n, "mx": mx, "sxx": sxx, "b": b, "a": a, "r2": r2,
            "s": s, "se_b": se_b, "se_a": se_a, "cov_ab": cov_ab}


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
    entries, std_lanes, specs = [], set(), []
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
        if not math.isfinite(mass) or mass < 0.0:
            raise RuntimeError("curve：泳道 %d 的质量必须是 0（空白标准道）或正数（收到 %r）"
                               % (no, mass_value))
        # A blank standard lane (§4.5): mass 0 on a lane with no detected bands.  `measure` stored
        # neither a window nor an area for it, so it is integrated below on its own profile inside
        # the window its measured peers provide.  A lane that does show bands is not blank — a
        # band there would be a real peak carrying mass 0, which bends the line towards the origin.
        lane_bands = lane.get("bands")
        if mass == 0.0 and lane_bands:
            if not isinstance(lane_bands, list):
                raise RuntimeError("泳道 %d 的 bands 不是列表" % no)
            raise RuntimeError("curve：泳道 %d 的质量是 0，但这条泳道检出了 %d 条带；质量 0 只能给"
                               "没有带的标准道（空白标准道），这条泳道要按普通标准道给质量"
                               % (no, len(lane_bands)))
        blank = mass == 0.0 and band is None and not lane_bands
        specs.append({"lane": no, "band": band, "mass": mass,
                      "lane_dict": lane, "blank": blank})
        std_lanes.add(no)

    # The blank lanes inherit the average window of the measured single-band standards, on the
    # same baseline the curve is fitted on (their own peaks do not exist to walk).
    windows = []
    blank_nos = [s["lane"] for s in specs if s["blank"]]
    if blank_nos:
        for spec in specs:
            if spec["blank"] or spec["band"] == "*":
                continue
            index, src_band = _std_band(spec["lane_dict"], spec["lane"], spec["band"])
            win = src_band.get("window_" + baseline)
            try:
                windows.append((float(win[0]), float(win[1])))
            except (TypeError, ValueError, IndexError):
                raise RuntimeError("curve：标准道泳道 %d 的带 %d 没有 window_%s（dens.json 由 measure "
                                   "生成）" % (spec["lane"], index, baseline))
        if not windows:
            raise RuntimeError("curve：空白标准道（泳道 %s）需要至少一个非空白的单带标准道提供窗口；"
                               "--std 里只有空白道或整道点" % _runs(blank_nos))

    for spec in specs:
        if spec["blank"]:
            area = _blank_area(dens, spec["lane_dict"], spec["lane"], windows, baseline)
        else:
            area = _std_area(spec["lane_dict"], spec["lane"], spec["band"], baseline)
        entries.append({"lane": spec["lane"], "band": spec["band"],
                        "mass": spec["mass"], "volume": area})

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
    # A blank standard point (mass 0) is legal and anchors the line at zero; the relative
    # residual is undefined for it, so the residual loop below writes `rel_pct: null`.  Only a
    # power fit needs strictly positive masses, checked in its own branch.

    if fit == "linear":
        line = _linfit([p["volume"] for p in used], [p["mass"] for p in used])
        slope, intercept, r2 = line["b"], line["a"], line["r2"]
        if slope <= 0.0:
            raise _not_monotonic(fit, "斜率", slope)
        # ep-c1i §3: the OLS statistics `quantify` needs to propagate the fit error, stored next
        # to the point estimates (4 dp, ruling R2) in the SAME space `space` names — here the area
        # column itself.  When n <= 2 there is no residual degree of freedom: `_linfit` reports
        # `s` / `se_b` / `se_a` / `cov_ab` as None and `sxx` / `mean_x` are withheld too (a stored
        # real sum from which no error could be derived would invite a fake zero later).
        stats_ok = line["s"] is not None
        fit_params = {"slope": _num(slope, 4), "intercept": _num(intercept, 4),
                      "r2": _num(r2, 4), "n": len(used), "space": "linear",
                      "slope_sd": _num_or_none(line["se_b"], 4),
                      "intercept_sd": _num_or_none(line["se_a"], 4),
                      "cov_slope_intercept": _num_or_none(line["cov_ab"], 4),
                      "residual_sd": _num_or_none(line["s"], 4),
                      "sxx": _num_or_none(line["sxx"], 4) if stats_ok else None,
                      "mean_x": _num_or_none(line["mx"], 4) if stats_ok else None}
    else:
        for point in used:
            if point["mass"] <= 0.0:
                raise RuntimeError("curve：power 拟合要求标准点质量 > 0（泳道 %s 的质量是 %g）；"
                                   "质量 0 的空白标准道只能用 --fit linear"
                                   % ("/".join(str(v) for v in point["lanes"]), point["mass"]))
        for point in used:
            if point["volume"] <= 0.0:
                raise RuntimeError("curve：power 拟合要求面积 > 0（泳道 %s 的面积是 %.3f）"
                                   % ("/".join(str(v) for v in point["lanes"]), point["volume"]))
        line = _linfit([math.log10(p["volume"]) for p in used],
                       [math.log10(p["mass"]) for p in used])
        exponent, log_c, r2 = line["b"], line["a"], line["r2"]
        if exponent <= 0.0:
            raise _not_monotonic(fit, "指数", exponent)
        scale = 10.0 ** log_c
        # Same statistics in log10 space (like `r2`): `sxx` / `mean_x` are log-space sums and
        # `residual_sd` is a log-space SD.  `scale = 10^log_c`, so its SD follows from log_c's by
        # the delta method, d(10^c)/dc = 10^c · ln(10) (ep-c1i §3).
        stats_ok = line["s"] is not None
        fit_params = {"exponent": _num(exponent, 4), "scale": _num(scale, 4),
                      "r2": _num(r2, 4), "n": len(used), "space": "log10",
                      "exponent_sd": _num_or_none(line["se_b"], 4),
                      "scale_sd": _num_or_none(None if line["se_a"] is None
                                               else scale * math.log(10.0) * line["se_a"], 4),
                      "residual_sd": _num_or_none(line["s"], 4),
                      "sxx": _num_or_none(line["sxx"], 4) if stats_ok else None,
                      "mean_x": _num_or_none(line["mx"], 4) if stats_ok else None}

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
        if point["mass"] == 0.0:
            # A blank standard has no relative residual: `rel_pct` divides by its mass (§4.5).
            raw_rel.append(None)
            residuals.append({"mass": shown["mass"], "volume": shown["volume"],
                              "predicted_mass": _num(predicted, 3), "rel_pct": None})
            continue
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
    if fit == "linear" and raw_rel and raw_rel[-1] is not None and raw_rel[-1] < -10.0:
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
            if not residual:
                column = "残差 ?"
            elif residual.get("rel_pct") is None:  # a blank standard (mass 0) has no residual
                column = "残差 —"
            else:
                column = "残差 %s%%" % _fmt_signed(residual.get("rel_pct"), 1)
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
