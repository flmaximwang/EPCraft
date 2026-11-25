"""
quantify — §2.8, §3.3, §4.6: dens.json + curve.json (+ the volume loaded per lane) → quant.json.

The area column is the one the curve was fitted on (`area_<baseline>`), and a band whose area lies
outside the calibrated `range.volume_min … range.volume_max` gets NO number: mass null, the lane
total and the concentration null, the band listed in `out_of_range_bands`, and a note naming it
(§2.8 — the failure this chain exists to prevent is a plausible-looking extrapolation).

A band whose area IS in range but whose fitted mass is not positive is rejected the same way (lead
ruling): mass null, `out_of_range` stays false (the area really is inside the range), rel_pct null,
the lane's total / concentration / share null, and one note naming the lane, the band and its area.
A line that crosses zero inside its own standard range says nothing at the low end; a negative mass
must never reach a total, and it is not written anywhere — not into the JSON, not into a note, not
into the terminal block, not even as a diagnostic (lead ruling).

The mass comes from ONE internal function, `_mass_from_area`, which evaluates the STORED fit
parameters forward (volume → mass), the direction §4.5 `fit_params` and §4.7's printed equation
define and the one §7.8/§7.9's recovery checks are calibrated against (lead ruling for ep-bqv.5).
"""

import math
import re

from .contracts import BASELINES, FITS, FUZZ, NO_SCATTER_NOTE, QUANT_KIND, SKIP_NOTE, VERSION, _SKIP_NOTE_RE
from .values import _area_of, _finite_number, _num, _runs
from .layout import _dwidth, _fmt_fixed, _fmt_sig, _fmt_trim, _fmt_value, _pad_left, _pad_right

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


def _quant_stats(fit_params):
    """The stored OLS statistics the sample uncertainty needs → `{s, n, mx, sxx}`, or `None`.

    ep-c1i §4: `quantify` never refits, so the whole input is what `build_curve` already wrote into
    `fit_params` — the residual SD (`residual_sd`), the x mean (`mean_x`) and the x sum of squares
    (`sxx`), all in the space `space` names (`linear` = the area column, `log10` = log10(area)).
    A hand-written curve.json and a two-point fit carry none of them (`build_curve` stores `None`,
    see §3), and a curve is also refused when the numbers are present but unusable (a negative SD,
    a zero or negative `sxx`).  `None` therefore means "the calibration has no residual scatter to
    propagate", never "assume zero" — a fake 0 would print `± 0` and claim a perfect fit.
    """
    n = fit_params.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 2:
        return None
    values = (fit_params.get("residual_sd"), fit_params.get("mean_x"),
              fit_params.get("sxx"))
    if not all(_finite_number(value) for value in values):
        return None
    s, mx, sxx = (float(value) for value in values)
    if s < 0.0 or sxx <= 0.0:
        return None
    return {"s": s, "n": n, "mx": mx, "sxx": sxx}

def _prediction_sd(area, mass, fit, stats):
    """The OLS PREDICTION standard deviation of the mass at `area`, in the curve's unit (ep-c1i §4).

        linear  sd = s · sqrt(1 + 1/n + (x − x̄)² / sxx)                    x = area
        power   sd = mass · ln(10) · s · sqrt(1 + 1/n + (u − ū)² / sxx)     u = log10(area)

    The prediction interval, not the confidence band of the line: a sample is a NEW observation, so
    its own measurement scatter adds the leading `1` to the variance of the fitted mean at that x.
    Without it the SD would describe how well the LINE is pinned down and would go to zero at x̄ —
    exactly where a real sample's uncertainty is largest in the mass it was converted from.

    The power fit's `s`, `mean_x`, `sxx` and therefore `sd_log` live in log10 space (like its `r2`);
    the response scale is reached by the delta method, `d(scale·x^e)/d(log10 x) = mass·ln(10)`, the
    same transformation `scale_sd` uses in §3.  `_quant_stats` has already guaranteed `sxx > 0` and
    `n > 2`, and a positive `mass` guarantees `area > 0` for the power fit (scale > 0), so the
    logarithm is defined.  A result that is not a finite non-negative number is not propagated.
    """
    x = float(area)
    if fit == "power":
        if x <= 0.0:
            return None
        u = math.log10(x)
        sd_log = stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"]
                                        + (u - stats["mx"]) ** 2 / stats["sxx"])
        sd = float(mass) * math.log(10.0) * sd_log
    else:
        sd = stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"]
                                    + (x - stats["mx"]) ** 2 / stats["sxx"])
    if not _finite_number(sd) or sd < 0.0:
        return None
    return float(sd)   # a SD is never negative; `_num` also folds a -0.0 into 0.0 when stored

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

    UNCERTAINTY (ep-c1i §4): every band that got a positive mass also gets `mass_sd`, its OLS
    prediction SD (`_prediction_sd`), and every lane whose total is known gets `mass_total_sd`
    (`sqrt(Σ mass_sd²)` over the bands carrying a mass — the band errors are combined as
    independent) plus `conc_sd` (`mass_total_sd / load_ul`) when a load is known.  The file also
    names the convention once in a top-level `uncertainty` block.  Nothing is invented when the
    stored fit has no residual degrees of freedom: `_quant_stats` returns `None`, every propagated
    value stays `None`, and ONE note says the calibration carries no residual scatter (`n == 2`, a
    hand-written curve.json, or an unusable statistic).  Totals, `rel_pct`,
    `frac_pct_among_lanes` and every refusal are unchanged — they keep using the point estimates,
    because the error belongs next to the value, not inside the arithmetic that defines it.

    `load` is the dict from `parse_load` (`{lane: ul}`); a lane that is not in dens is refused.
    `dens_path` / `curve_path` / `load_text` are recorded verbatim in `source` (ruling R6) and feed
    nothing else.  Bad `baseline` / `fit` / `fit_params`, a missing key the function needs, or a
    load lane that dens does not have raise one Chinese RuntimeError; none of the return values is
    ever a NaN, and no SD is ever negative (`_num` folds a -0.0 into 0.0).
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

    # The fit's scatter is a property of the stored curve, not of any one lane: read it once.  When
    # it is absent every mass_sd / mass_total_sd / conc_sd below stays None and one note explains
    # why — the flag `withheld` records whether any band actually had a mass to attach an error to,
    # so the note is only written when there was something to withhold.
    stats = _quant_stats(fit_params)
    withheld = False

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
                mass, mass_sd = None, None
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
                    mass, mass_sd = None, None
                elif stats is not None:
                    mass_sd = _prediction_sd(area, mass, fit, stats)
                    withheld = withheld or mass_sd is None
                else:
                    mass_sd = None
                    withheld = True
            raw_masses.append((mass, mass_sd))
            bands.append({"index": index, "y_cm": _num(y_cm, 4), "mw_kda": band.get("mw_kda"),
                          "area": _num(area, 3), "mass": None if mass is None else _num(mass, 3),
                          "mass_sd": None if mass_sd is None else _num(mass_sd, 3),
                          "out_of_range": bool(below or above), "rel_pct": None})

        total = None if (out_bands or nonpos) else sum(mass for mass, _sd in raw_masses)
        for index, area, below in pending:
            notes.append(_out_of_range_note(no, index, area, vmin, vmax, below, total is None))
        for index, area in nonpos:
            notes.append(_non_positive_note(no, index, area))
        if total is not None and total != 0.0:
            for band, (mass, _sd) in zip(bands, raw_masses):
                if mass is not None:
                    band["rel_pct"] = _num(mass / total * 100.0, 2)

        # Quadrature approximates lane uncertainty without the shared-fit covariance (ep-c1i §4).
        # `mass_total_sd` exists only where `mass_total` does — a lane whose total is withheld has
        # no error to propagate either — and `conc_sd` divides it by the very load `conc` used.
        # When any contributing band has no SD (the fit carries no residual scatter) the sum is
        # not defined and stays None rather than being quietly computed from a subset.
        band_masses = [mass for mass, _sd in raw_masses if mass is not None]
        band_sds = [sd for mass, sd in raw_masses if mass is not None and sd is not None]
        total_sd = None
        if total is not None and band_masses and len(band_sds) == len(band_masses):
            total_sd = math.sqrt(sum(sd * sd for sd in band_sds))
        conc = None if (lane_load is None or total is None) else _num(total / lane_load, 3)
        conc_sd = (None if (lane_load is None or total_sd is None)
                   else _num(total_sd / float(lane_load), 3))

        raw_totals.append(total)
        rows.append({"lane": no, "label": lane.get("label"),
                     "load_ul": None if lane_load is None else float(lane_load),
                     "bands": bands, "mass_total": None if total is None else _num(total, 3),
                     "mass_total_sd": None if total_sd is None else _num(total_sd, 3),
                     "conc": conc, "conc_sd": conc_sd, "out_of_range": bool(out_bands),
                     "out_of_range_bands": out_bands, "frac_pct_among_lanes": None})

    known = [total for total in raw_totals if total is not None]
    overall = sum(known)
    for row, total in zip(rows, raw_totals):
        if total is None or overall <= 0.0:
            continue
        row["frac_pct_among_lanes"] = _num(total / overall * 100.0, 2)

    if withheld:
        notes.append(NO_SCATTER_NOTE)

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
        # the file states its own convention, so a reader of quant.json needs no doc (ep-c1i §4):
        # 1 SD of the OLS prediction, one sigma, in the curve's unit
        "uncertainty": {"basis": "ols_prediction", "k": 1, "unit": unit},
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

    ep-c1i §4 adds one tail clause naming the uncertainty basis (`±1 SD 来自拟合`), read back from
    the file's own `uncertainty` block so a hand-written quant.json without one simply prints no
    clause.  It is a fixed Chinese phrase rather than a translation of `basis` / `k`: the JSON
    carries the machine-readable name and this line is for the person reading the table.
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
    unc = quant.get("uncertainty")
    k = unc.get("k") if isinstance(unc, dict) else None
    basis = unc.get("basis") if isinstance(unc, dict) else None
    if isinstance(k, int) and not isinstance(k, bool) and basis:
        head += "；±%d SD 来自拟合" % k
    return head


def _with_sd(value_text, sd, unit):
    """`0.522` + `0.031` + `ug` → `0.522 ± 0.031 ug`, and just `0.522 ug` when the SD is unknown.

    ep-c1i §4: the uncertainty sits next to the value it qualifies, at three significant digits
    (`_fmt_sig`, the same formatter the out-of-range note uses), with the unit after both numbers so
    the line still reads as one quantity.  A value without an SD prints no `±` at all rather than
    `± —`: a calibration with no residual scatter has nothing to add, and an unknown must not look
    like a measured zero.
    """
    if not _finite_number(sd):
        return "%s %s" % (value_text, unit)
    return "%s ± %s %s" % (value_text, _fmt_sig(sd), unit)


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
    when it is really 4.495 against 4.484.  The mass also carries its `±mass_sd` when the curve
    supports one (ep-c1i §4); a rejected band has no mass, hence no error to print either.
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
    text += "  " + _with_sd(_fmt_value(band["mass"]), band.get("mass_sd"), unit)
    if _finite_number(band.get("rel_pct")):
        text += " (%.0f%%)" % band["rel_pct"]
    return text


def format_quant(quant, *, curve=None):
    """Terminal table (§5): the header, one line per sample lane, then `  注：` lines.

    `curve` is optional display metadata: it supplies the standard range / point count / r² and the
    out-of-range bounds, none of which §4.6 lets quant.json carry.  `None` never appears in the
    output: a value that is absent (no load, an unknown total, an unknown share) is simply omitted,
    and an out-of-range band prints `—` with the range comparison and the advice instead of a mass.

    ep-c1i §4: the totals and concentrations print their uncertainty the same way a band does
    (`合计 7.5 ± 0.12 ug`, `浓度 3.75 ± 0.06 ug/uL`), and the header names the basis.  The `±` is
    only ever appended INSIDE the last slot of a line, never in the label column, so the table's
    width padding (`_dwidth`, `_pad_right`) is unaffected.
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
            tail.append("合计 " + _with_sd(_fmt_value(lane["mass_total"]),
                                          lane.get("mass_total_sd"), unit))
            if _finite_number(lane.get("conc")):
                tail.append("浓度 " + _with_sd(_fmt_value(lane["conc"]),
                                              lane.get("conc_sd"), unit + "/uL"))
            if _finite_number(lane.get("frac_pct_among_lanes")):
                tail.append("占全胶 %.0f%%" % lane["frac_pct_among_lanes"])
        if tail:
            body += " │ " + "  ".join(tail)
        lines.append(body)
    for note in quant.get("notes") or []:
        lines.append("  注：%s" % note)
    return "\n".join(lines)
