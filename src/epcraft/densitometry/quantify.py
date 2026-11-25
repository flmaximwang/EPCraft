"""
quantify — series concentration quantification with sample_standard_ratio (ep-srz).

Evaluates the stored standard curve forward (area → equivalent concentration C_equiv in
`curve["unit"]`), divides each member by its `sample_standard_ratio` (default 1.0), and merges
active (`used: true`) members per declared sample series:

    single (n_used == 1):  point estimate only (`conc_sd = None`, `sem = None`), plus `common_pct`
    mean   (n_used >= 2):  normalized inverse-variance weighted mean, sample SD, and SEM
    ratio  (n_used >= 2, >= 2 distinct ratios): through-origin WLS slope C_equiv = conc * ratio,
                           with free-intercept linearity check when n_used >= 3

Common-mode relative uncertainty (`common_pct`) combines the shared calibration-line uncertainty
(which does NOT shrink with replicate count) and optional `--load-cv` in quadrature.
"""

import math
import re

from .contracts import (
    BASELINES,
    FITS,
    FUZZ,
    NO_SCATTER_NOTE,
    QUANT_KIND,
    REPORT_KIND,
    SKIP_NOTE,
    VERSION,
    _SKIP_NOTE_RE,
)
from .layout import _fmt_eq, _fmt_fixed, _fmt_sig, _fmt_trim, _fmt_value
from .values import _area_of, _finite_number, _num, _runs


# ------------------------------------------------------------------ parsers

_MEMBER_RE = re.compile(r"^\s*([0-9]+)(?::([0-9]+))?(?:@([^\s@:]+))?\s*$")


def parse_sample(text):
    """Parse `--sample NAME=LANE[:BAND][@RATIO],...` into `{"name": str, "members": [...]}`.

    Within a series, `@RATIO` (`sample_standard_ratio > 0`) must be either omitted for every
    member (defaulting to `1.0`) or given for every member; mixing is refused.  Duplicate lanes
    in the same series are refused.
    """
    raw = "" if text is None else str(text).strip()
    if not raw or "=" not in raw:
        raise RuntimeError("--sample 格式应为 NAME=LANE[:BAND][@RATIO],...（收到 %r）" % (text,))
    name, _, body = raw.partition("=")
    name = name.strip()
    if not name:
        raise RuntimeError("--sample 缺少样品名：%r" % (text,))
    if not body.strip():
        raise RuntimeError("--sample %s 没有成员泳道" % name)

    members = []
    seen_lanes = set()
    has_ratio = []
    for raw_tok in body.split(","):
        tok = raw_tok.strip()
        if not tok:
            raise RuntimeError("--sample %s 里有空成员：%r" % (name, text))
        m = _MEMBER_RE.match(tok)
        if not m:
            raise RuntimeError("--sample %s 的成员格式不对：%r（应为 LANE[:BAND][@RATIO]）" % (name, tok))
        lane = int(m.group(1))
        if lane < 1:
            raise RuntimeError("--sample %s 的泳道号必须 ≥ 1：%r" % (name, tok))
        if lane in seen_lanes:
            raise RuntimeError("--sample %s 里泳道 %d 出现了两次" % (name, lane))
        seen_lanes.add(lane)

        band = None
        if m.group(2) is not None:
            band = int(m.group(2))
            if band < 1:
                raise RuntimeError("--sample %s 的带号必须 ≥ 1：%r" % (name, tok))

        ratio = None
        if m.group(3) is not None:
            try:
                ratio = float(m.group(3))
            except ValueError:
                raise RuntimeError("--sample %s 里 %r 的 ratio 不是数字" % (name, tok))
            if not math.isfinite(ratio) or ratio <= 0.0:
                raise RuntimeError("--sample %s 里 %r 的 ratio 必须是正有限数" % (name, tok))
            has_ratio.append(True)
        else:
            has_ratio.append(False)

        members.append({"lane": lane, "band": band, "ratio": ratio})

    if any(has_ratio) and not all(has_ratio):
        raise RuntimeError("--sample %s 不能混用省略与显式 @RATIO（要么全写，要么全省略）" % name)
    for member in members:
        if member["ratio"] is None:
            member["ratio"] = 1.0
    return {"name": name, "members": members}


def parse_sample_band(text):
    """Parse `--sample-band LO,HI` (kDa window) → `(lo, hi)` or `None`."""
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        raise RuntimeError("--sample-band 格式应为 LO,HI（kDa 窗口，收到 %r）" % (text,))
    try:
        lo, hi = float(parts[0]), float(parts[1])
    except ValueError:
        raise RuntimeError("--sample-band 需要两个数字（收到 %r）" % (text,))
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo <= 0.0 or hi < lo:
        raise RuntimeError("--sample-band 范围无效（需要 0 < LO <= HI，收到 %r）" % (text,))
    return (lo, hi)


def parse_drop_sample(values):
    """Parse repeatable `--drop-sample` (single int or comma-separated ints) → `set[int]`."""
    out = set()
    if not values:
        return out
    items = [values] if isinstance(values, (str, int)) else list(values)
    for item in items:
        for tok in str(item).split(","):
            s = tok.strip()
            if not s:
                continue
            if not re.fullmatch(r"[0-9]+", s) or int(s) < 1:
                raise RuntimeError("--drop-sample 需要正整数泳道号（收到 %r）" % (tok,))
            out.add(int(s))
    return out


# ------------------------------------------------------------------ curve & dens checks

def _quant_range(curve):
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
    fit = curve.get("fit")
    if fit not in FITS:
        raise RuntimeError("quantify：curve 的 fit 不是 linear 或 power（收到 %r）" % (fit,))
    params = curve.get("fit_params")
    if not isinstance(params, dict):
        raise RuntimeError("quantify：curve 缺少 fit_params（重跑 curve）")
    required = ("intercept", "slope") if fit == "linear" else ("scale", "exponent")
    for key in required:
        if not _finite_number(params.get(key)):
            raise RuntimeError("quantify：curve 的 fit_params 缺少数值 %s（重跑 curve）" % key)
    return params, fit


def _quant_std_lanes(curve):
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
    lanes = dens.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise RuntimeError("quantify：dens 里没有 lanes（先用 measure 生成 dens.json）")
    by_no = {}
    for lane in lanes:
        if not isinstance(lane, dict):
            raise RuntimeError("quantify：dens 的 lanes 里有不是对象的条目")
        no = lane.get("lane")
        if isinstance(no, bool) or not isinstance(no, int) or no < 1:
            raise RuntimeError("quantify：dens 的泳道条目缺少正整数 lane（收到 %r）" % (no,))
        if no in by_no:
            raise RuntimeError("quantify：dens 里泳道 %d 出现了两次" % no)
        if not isinstance(lane.get("bands"), list):
            raise RuntimeError("quantify：dens 的泳道 %d 没有 bands 列表（重跑 measure）" % no)
        by_no[no] = lane
    return by_no


def _equiv_from_area(area, curve, fit_params):
    """Evaluate the stored curve forward: area → equivalent concentration in `curve['unit']`."""
    fit = curve.get("fit")
    if fit == "linear":
        val = float(fit_params["intercept"]) + float(fit_params["slope"]) * float(area)
    else:
        exponent = float(fit_params["exponent"])
        if area < 0.0 or (area == 0.0 and exponent < 0.0):
            raise RuntimeError("quantify：幂律标曲不能算负面积的 %r 次方（面积 %r）" % (exponent, area))
        val = float(fit_params["scale"]) * (float(area) ** exponent)
    if not _finite_number(val):
        raise RuntimeError("quantify：面积 %r 按标曲算出的浓度不是有限数（%r）" % (area, val))
    return float(val)


def _quant_stats(fit_params):
    """Stored OLS statistics `{s, n, mx, sxx}` or `None` when residual scatter is unavailable."""
    n = fit_params.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 2:
        return None
    values = (fit_params.get("residual_sd"), fit_params.get("mean_x"), fit_params.get("sxx"))
    if not all(_finite_number(v) for v in values):
        return None
    s, mx, sxx = (float(v) for v in values)
    if s < 0.0 or sxx <= 0.0:
        return None
    return {"s": s, "n": n, "mx": mx, "sxx": sxx}


def _prediction_sd(area, equiv, fit, stats):
    """1-sigma OLS prediction SD of a single sample measurement at `area`."""
    x = float(area)
    if fit == "power":
        if x <= 0.0:
            return None
        u = math.log10(x)
        sd_log = stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"] + (u - stats["mx"]) ** 2 / stats["sxx"])
        sd = float(equiv) * math.log(10.0) * sd_log
    else:
        sd = stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"] + (x - stats["mx"]) ** 2 / stats["sxx"])
    return float(sd) if (_finite_number(sd) and sd >= 0.0) else None


def _line_sd(area, equiv, fit, stats):
    """1-sigma shared calibration-line SD at `area` (no leading `1`, does NOT shrink with sample n)."""
    x = float(area)
    if fit == "power":
        if x <= 0.0:
            return None
        u = math.log10(x)
        sd_log = stats["s"] * math.sqrt(1.0 / stats["n"] + (u - stats["mx"]) ** 2 / stats["sxx"])
        sd = float(equiv) * math.log(10.0) * sd_log
    else:
        sd = stats["s"] * math.sqrt(1.0 / stats["n"] + (x - stats["mx"]) ** 2 / stats["sxx"])
    return float(sd) if (_finite_number(sd) and sd >= 0.0) else None


# ------------------------------------------------------------------ band selection

def _select_band(lane, band_index, sample_band, baseline):
    """Pick the single target band dict in `lane` by explicit index, MW window, or max area."""
    no = lane["lane"]
    bands = [b for b in lane["bands"] if isinstance(b, dict)]
    if not bands:
        raise RuntimeError("quantify：泳道 %d 没有检测到任何条带" % no)

    if band_index is not None:
        for b in bands:
            if b.get("index") == band_index:
                return b
        raise RuntimeError("quantify：泳道 %d 没有第 %d 号带" % (no, band_index))

    if sample_band is not None:
        lo, hi = sample_band
        hits = []
        for b in bands:
            mw = b.get("mw_kda")
            if _finite_number(mw) and lo <= float(mw) <= hi:
                hits.append(b)
        if not hits:
            raise RuntimeError("quantify：泳道 %d 在 %.1f–%.1f kDa 窗口内没有匹配条带" % (no, lo, hi))
        if len(hits) > 1:
            raise RuntimeError("quantify：泳道 %d 在 %.1f–%.1f kDa 窗口内有 %d 条带（请用 LANE:BAND 指定）"
                               % (no, lo, hi, len(hits)))
        return hits[0]

    area_key = "area_flank" if baseline == "flank" else "area_global"
    return max(bands, key=lambda b: (float(b.get(area_key, 0.0)), -int(b.get("index", 0))))


# ------------------------------------------------------------------ series estimators

def _weighted_mean(active):
    """Normalized inverse-variance weighted mean, weighted sample SD, and SEM."""
    k = len(active)
    concs = [m["_conc"] for m in active]
    sds = [m["_conc_sd"] for m in active]
    if all(sd is not None and sd > 0.0 for sd in sds):
        raw_w = [1.0 / (sd * sd) for sd in sds]
        w_sum = sum(raw_w)
        weights = [w / w_sum for w in raw_w]
    else:
        weights = [1.0 / k] * k

    mean_c = sum(w * c for w, c in zip(weights, concs))
    var = (k / (k - 1.0)) * sum(w * (c - mean_c) ** 2 for w, c in zip(weights, concs))
    sd = math.sqrt(max(0.0, var))
    sem = sd / math.sqrt(k)
    return {
        "conc": _num(mean_c, 3),
        "sd": _num(sd, 3),
        "sem": _num(sem, 3),
        "_conc": mean_c,
        "_sem": sem,
    }


def _ratio_regression(active):
    """Through-origin WLS C_equiv = conc * ratio, plus free-intercept check when n >= 3."""
    ratios = [m["_ratio"] for m in active]
    if len({round(r, 9) for r in ratios}) < 2:
        return None
    equivs = [m["_equiv"] for m in active]
    sds = [m["_equiv_sd"] for m in active]
    has_sd_weights = all(sd is not None and sd > 0.0 for sd in sds)
    if has_sd_weights:
        weights = [1.0 / (sd * sd) for sd in sds]
    else:
        weights = [1.0] * len(active)

    k = len(active)
    s_xx = sum(w * r * r for w, r in zip(weights, ratios))
    s_xy = sum(w * r * y for w, r, y in zip(weights, ratios, equivs))
    s_yy = sum(w * y * y for w, y in zip(weights, equivs))
    if s_xx <= 0.0:
        return None

    slope = s_xy / s_xx
    res = [y - slope * r for r, y in zip(ratios, equivs)]
    rss = sum(w * e * e for w, e in zip(weights, res))
    s2 = rss / (k - 1.0)
    slope_sd = math.sqrt(max(0.0, s2 / s_xx))
    r2 = 1.0 - (rss / s_yy) if s_yy > 0.0 else 1.0

    intercept = None
    intercept_sd = None
    nonlinear = False
    if k >= 3:
        w_sum = sum(weights)
        r_bar = sum(w * r for w, r in zip(weights, ratios)) / w_sum
        y_bar = sum(w * y for w, y in zip(weights, equivs)) / w_sum
        d_rr = sum(w * (r - r_bar) ** 2 for w, r in zip(weights, ratios))
        d_ry = sum(w * (r - r_bar) * (y - y_bar) for w, r, y in zip(weights, ratios, equivs))
        if d_rr > 0.0:
            b1 = d_ry / d_rr
            b0 = y_bar - b1 * r_bar
            rss_free = sum(w * (y - (b0 + b1 * r)) ** 2 for w, r, y in zip(weights, ratios, equivs))
            s2_free = rss_free / (k - 2.0)
            scale_var = max(1.0, s2_free) if has_sd_weights else s2_free
            se_b0 = math.sqrt(max(0.0, scale_var * (1.0 / w_sum + (r_bar * r_bar) / d_rr)))
            intercept = _num(b0, 3)
            intercept_sd = _num(se_b0, 3)
            max_y = max(abs(y) for y in equivs)
            if se_b0 > 0.0 and abs(b0) > 2.5 * se_b0 and abs(b0) > 0.05 * max_y:
                nonlinear = True

    return {
        "conc": _num(slope, 3),
        "slope_sd": _num(slope_sd, 3),
        "r2": _num(r2, 4),
        "intercept": intercept,
        "intercept_sd": intercept_sd,
        "nonlinear": nonlinear,
        "_conc": slope,
        "_slope_sd": slope_sd,
    }


def _common_pct(active, fit, stats, load_cv):
    """Common-mode relative uncertainty (%) that does NOT shrink with replicate count."""
    curve_pct = None
    if stats is not None and active:
        rel_list = []
        for m in active:
            lsd = _line_sd(m["_area"], m["_equiv"], fit, stats)
            if lsd is not None and m["_equiv"] > 0.0:
                rel_list.append(100.0 * lsd / m["_equiv"])
        if len(rel_list) == len(active):
            curve_pct = sum(rel_list) / len(rel_list)

    if curve_pct is None and load_cv is None:
        return None
    total_sq = (curve_pct or 0.0) ** 2 + (load_cv or 0.0) ** 2
    return _num(math.sqrt(total_sq), 2)


# ------------------------------------------------------------------ main quantify

def quantify(dens, curve, samples, *, drop_sample=(), sample_band=None, load_cv=None,
             dens_path="", curve_path=""):
    """dens.json + curve.json + sample series → quant.json (ep-srz)."""
    if not isinstance(dens, dict):
        raise RuntimeError("quantify：dens 不是 dict（收到 %s）" % type(dens).__name__)
    if not isinstance(curve, dict):
        raise RuntimeError("quantify：curve 不是 dict（收到 %s）" % type(curve).__name__)
    if not samples:
        raise RuntimeError("quantify：至少需要一个 --sample 系列")
    if load_cv is not None:
        if not _finite_number(load_cv) or float(load_cv) < 0.0:
            raise RuntimeError("quantify：--load-cv 必须是非负数字（收到 %r）" % (load_cv,))
        load_cv = float(load_cv)

    baseline = curve.get("baseline")
    if baseline not in BASELINES:
        raise RuntimeError("quantify：curve 的 baseline 不是 flank 或 global（收到 %r）" % (baseline,))
    fit_params, fit = _quant_fit(curve)
    unit = curve.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise RuntimeError("quantify：curve 里没有 unit（先用 curve 生成 curve.json）")
    vmin, vmax = _quant_range(curve)
    std_lanes = _quant_std_lanes(curve)
    by_lane = _quant_dens_lanes(dens)
    stats = _quant_stats(fit_params)

    drop_set = parse_drop_sample(drop_sample)
    declared_lanes = set()
    for series in samples:
        for member in series["members"]:
            no = member["lane"]
            if no not in by_lane:
                raise RuntimeError("quantify：样品 %s 引用的泳道 %d 不在 dens 里（dens 有 %s）"
                                   % (series["name"], no, _runs(sorted(by_lane))))
            if no in std_lanes:
                raise RuntimeError("quantify：泳道 %d 是标曲泳道，不能作为样品（样品 %s）"
                                   % (no, series["name"]))
            declared_lanes.add(no)
    unknown_drops = sorted(drop_set - declared_lanes)
    if unknown_drops:
        raise RuntimeError("quantify：--drop-sample 指定的泳道不在任何 --sample 中：%s"
                           % _runs(unknown_drops))

    notes = []
    skipped_nos = sorted(no for no in by_lane if no in std_lanes)
    if skipped_nos:
        notes.append(SKIP_NOTE % (len(skipped_nos), _runs(skipped_nos)))
    if drop_set:
        notes.append("已剔除样品泳道 %s（原因：待补充）" % _runs(sorted(drop_set)))

    out_samples = []
    any_withheld = False

    for series in samples:
        sname = series["name"]
        members_out = []
        internal = []
        for spec in series["members"]:
            no = spec["lane"]
            ratio = float(spec["ratio"])
            lane = by_lane[no]
            band = _select_band(lane, spec["band"], sample_band, baseline)
            index = band.get("index")
            y_cm = band.get("y_cm")
            area = _area_of(band, no, "第 %s 个带" % index, baseline)
            below = area < vmin - FUZZ
            above = area > vmax + FUZZ
            oor = bool(below or above)
            used = no not in drop_set

            equiv, equiv_sd, conc, conc_sd = None, None, None, None
            nonpos = False
            if not oor:
                val = _equiv_from_area(area, curve, fit_params)
                if val <= 0.0:
                    nonpos = True
                else:
                    equiv = val
                    if stats is not None:
                        equiv_sd = _prediction_sd(area, equiv, fit, stats)
                    else:
                        any_withheld = True
                    conc = equiv / ratio
                    conc_sd = None if equiv_sd is None else (equiv_sd / ratio)

            members_out.append({
                "lane": no,
                "band": index,
                "y_cm": None if y_cm is None else _num(y_cm, 4),
                "mw_kda": band.get("mw_kda"),
                "ratio": _num(ratio, 4),
                "area": _num(area, 3),
                "equiv": None if equiv is None else _num(equiv, 3),
                "equiv_sd": None if equiv_sd is None else _num(equiv_sd, 3),
                "conc": None if conc is None else _num(conc, 3),
                "conc_sd": None if conc_sd is None else _num(conc_sd, 3),
                "out_of_range": oor,
                "used": used,
            })
            internal.append({
                "lane": no,
                "band": index,
                "ratio": ratio,
                "_ratio": ratio,
                "_area": area,
                "_equiv": equiv,
                "_equiv_sd": equiv_sd,
                "_conc": conc,
                "_conc_sd": conc_sd,
                "oor": oor,
                "nonpos": nonpos,
                "used": used,
            })

        active = [m for m in internal if m["used"]]
        series_notes = []
        n_total = len(internal)
        n_used = len(active)

        bad_oor = [m["lane"] for m in active if m["oor"]]
        bad_nonpos = [m["lane"] for m in active if m["nonpos"]]

        if n_used == 0:
            est = {
                "method": None, "conc": None, "conc_sd": None, "sem": None,
                "common_pct": None, "n": n_total, "n_used": 0,
                "mean": None, "ratio": None, "notes": ["所有成员均已剔除，无法合并"],
            }
            notes.append("样品 %s：所有成员均已剔除，不给合并浓度" % sname)
        elif bad_oor or bad_nonpos:
            reasons = []
            if bad_oor:
                reasons.append("泳道 %s 超出标曲范围（%s–%s）" % (_runs(bad_oor), _fmt_sig(vmin), _fmt_sig(vmax)))
            if bad_nonpos:
                reasons.append("泳道 %s 拟合值不为正" % _runs(bad_nonpos))
            msg = "；".join(reasons) + "，不给合并浓度（可 --drop-sample 剔除后重算）"
            series_notes.append(msg)
            notes.append("样品 %s：%s" % (sname, msg))
            est = {
                "method": None, "conc": None, "conc_sd": None, "sem": None,
                "common_pct": None, "n": n_total, "n_used": n_used,
                "mean": None, "ratio": None, "notes": series_notes,
            }
        elif n_used == 1:
            m0 = active[0]
            cpct = _common_pct(active, fit, stats, load_cv)
            est = {
                "method": "single",
                "conc": _num(m0["_conc"], 3),
                "conc_sd": None,
                "sem": None,
                "common_pct": cpct,
                "n": n_total,
                "n_used": 1,
                "mean": {"conc": _num(m0["_conc"], 3), "sd": None, "sem": None},
                "ratio": None,
                "notes": [],
            }
        else:
            mean_est = _weighted_mean(active)
            ratio_est = _ratio_regression(active)
            if ratio_est is not None and ratio_est["nonlinear"]:
                warn = "该系列上样比例不线性（截距显著偏离原点），ratio 合并不可信"
                series_notes.append(warn)
                notes.append("样品 %s：%s" % (sname, warn))
            use_ratio = ratio_est is not None and not ratio_est["nonlinear"]
            method = "ratio" if use_ratio else "mean"
            conc_val = ratio_est["conc"] if use_ratio else mean_est["conc"]
            conc_sd_val = ratio_est["slope_sd"] if use_ratio else mean_est["sem"]
            cpct = _common_pct(active, fit, stats, load_cv)
            est = {
                "method": method,
                "conc": conc_val,
                "conc_sd": conc_sd_val,
                "sem": mean_est["sem"],
                "common_pct": cpct,
                "n": n_total,
                "n_used": n_used,
                "mean": {k: v for k, v in mean_est.items() if not k.startswith("_")},
                "ratio": None if ratio_est is None else {k: v for k, v in ratio_est.items() if not k.startswith("_")},
                "notes": series_notes,
            }

        out_samples.append({
            "name": sname,
            "band_mw": list(sample_band) if sample_band is not None else None,
            "members": members_out,
            "est": est,
        })

    if stats is None and any_withheld:
        notes.append(NO_SCATTER_NOTE)

    return {
        "kind": QUANT_KIND,
        "version": VERSION,
        "baseline": baseline,
        "fit": fit,
        "unit": unit,
        "load_cv_pct": None if load_cv is None else _num(load_cv, 2),
        "source": {
            "dens": dens_path,
            "curve": curve_path,
            "samples": [s["name"] for s in samples],
            "drop_sample": sorted(drop_set),
            "sample_band": list(sample_band) if sample_band is not None else None,
        },
        "samples": out_samples,
        "uncertainty": {"basis": "ols_prediction_and_series", "k": 1, "unit": unit},
        "notes": notes,
    }


# ------------------------------------------------------------------ formatters

def _skipped_count(quant):
    for note in quant.get("notes") or []:
        match = _SKIP_NOTE_RE.match(str(note))
        if match:
            return int(match.group(1))
    return 0


def _curve_summary_bits(curve):
    if not isinstance(curve, dict):
        return []
    bits = []
    rng = curve.get("range")
    params = curve.get("fit_params")
    unit = curve.get("unit") or ""
    if isinstance(rng, dict) and _finite_number(rng.get("mass_min")) and _finite_number(rng.get("mass_max")):
        bits.append("标曲 %s–%s %s" % (_fmt_trim(rng["mass_min"], 3), _fmt_trim(rng["mass_max"], 3), unit))
    if isinstance(params, dict):
        n = params.get("n")
        if isinstance(n, int) and not isinstance(n, bool):
            bits.append("%d 点" % n)
        if _finite_number(params.get("r2")):
            bits.append("r² %s" % _fmt_fixed(params["r2"], 4))
    return bits


def format_quant(quant, curve=None):
    """Terminal summary table for series-based quantification."""
    baseline = quant.get("baseline") or "flank"
    fit = quant.get("fit") or "linear"
    unit = str(quant.get("unit") or "ug")
    head = "[定量] 基线 %s · %s · %s" % (baseline, fit, unit)
    bits = _curve_summary_bits(curve)
    if bits:
        head += "（%s）" % "，".join(bits)
    skipped = _skipped_count(quant)
    if skipped:
        head += "；跳过 %d 条标准道" % skipped

    lines = [head]
    for s in quant.get("samples") or []:
        est = s.get("est") or {}
        method = est.get("method")
        n_used = est.get("n_used", 0)
        n_tot = est.get("n", 0)
        if est.get("conc") is None:
            summary = "—（无法合并）"
        elif method == "single":
            summary = "%s %s（single, n=1）" % (_fmt_value(est["conc"]), unit)
        else:
            sd = est.get("conc_sd")
            sd_txt = (" ± %s" % _fmt_sig(sd)) if _finite_number(sd) else ""
            summary = "%s%s %s（%s, n=%d/%d）" % (_fmt_value(est["conc"]), sd_txt, unit, method, n_used, n_tot)
            ratio_info = est.get("ratio")
            if isinstance(ratio_info, dict) and _finite_number(ratio_info.get("r2")):
                summary += "，ratio r²=%s" % _fmt_fixed(ratio_info["r2"], 4)
        cpct = est.get("common_pct")
        if _finite_number(cpct):
            summary += "，共同模 ±%s%%" % _fmt_trim(cpct, 2)
        lines.append("样品 %s：%s" % (s["name"], summary))

        for m in s.get("members") or []:
            tag = "" if m.get("used", True) else " [已剔除]"
            if m.get("out_of_range"):
                val_txt = "超标曲范围"
            elif m.get("conc") is None:
                val_txt = "拟合值非正"
            else:
                sd_txt = (" ± %s" % _fmt_sig(m["conc_sd"])) if _finite_number(m.get("conc_sd")) else ""
                val_txt = "%s%s %s" % (_fmt_value(m["conc"]), sd_txt, unit)
            mw_txt = ("%.1f kDa" % m["mw_kda"]) if _finite_number(m.get("mw_kda")) else "— kDa"
            lines.append("  泳道 %d.%s（%s，ratio=%s，面积 %.1f）：%s%s"
                         % (m["lane"], m.get("band") or "?", mw_txt, _fmt_trim(m["ratio"], 3),
                            m["area"], val_txt, tag))

    for note in quant.get("notes") or []:
        lines.append("[i] %s" % note)
    return "\n".join(lines)


def _curve_equation_md(curve):
    if not isinstance(curve, dict):
        return "（未提供 curve.json）"
    fit = curve.get("fit")
    fp = curve.get("fit_params") or {}
    unit = curve.get("unit") or ""
    if fit == "linear":
        slope = fp.get("slope", 0.0)
        intercept = fp.get("intercept", 0.0)
        sign = "-" if intercept < 0 else "+"
        eqn = "`C_equiv = %s · area %s %s` (%s)" % (_fmt_eq(slope), sign, _fmt_eq(abs(intercept)), unit)
    else:
        scale = fp.get("scale", 0.0)
        exp = fp.get("exponent", 0.0)
        eqn = "`C_equiv = %s · area^%s` (%s)" % (_fmt_eq(scale), _fmt_eq(exp), unit)
    r2 = fp.get("r2")
    rsd = fp.get("residual_sd")
    parts = [eqn]
    if _finite_number(r2):
        parts.append("r² = %s" % _fmt_fixed(r2, 4))
    if _finite_number(rsd):
        parts.append("residual SD = %s" % _fmt_sig(rsd))
    return "，".join(parts)


def format_quant_report(quant, curve=None):
    """Standalone Markdown report (kind: ep-craft-densitometry-report) for `quantify --report`."""
    unit = str(quant.get("unit") or "ug")
    baseline = quant.get("baseline") or "flank"
    fit = quant.get("fit") or "linear"

    md = [
        "<!-- kind: %s -->" % REPORT_KIND,
        "# SDS-PAGE 定量报告",
        "",
        "## 1. 标准曲线与口径",
        "",
        "- **基线 / 拟合**：`%s` / `%s`（单位：`%s`）" % (baseline, fit, unit),
        "- **标曲方程**：%s" % _curve_equation_md(curve),
        "- **换算口径**：`conc_i = C_equiv_i / sample_standard_ratio_i`（当量浓度直算，跳过质量中间步；默认 ratio=1 为等体积当量）",
        "",
        "## 2. 样品系列汇总",
        "",
        "| 样品 | 有效/总道数 | 口径 | 合并浓度 (%s) | 随机误差 (SEM) | 共同模误差 (%%) | ratio r² |" % unit,
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for s in quant.get("samples") or []:
        est = s.get("est") or {}
        method = est.get("method") or "—"
        conc_txt = _fmt_value(est["conc"]) if _finite_number(est.get("conc")) else "—"
        sem_txt = ("±%s" % _fmt_sig(est["sem"])) if _finite_number(est.get("sem")) else "—"
        cpct_txt = ("±%s%%" % _fmt_trim(est["common_pct"], 2)) if _finite_number(est.get("common_pct")) else "—"
        rinfo = est.get("ratio")
        r2_txt = _fmt_fixed(rinfo["r2"], 4) if isinstance(rinfo, dict) and _finite_number(rinfo.get("r2")) else "—"
        md.append("| %s | %d / %d | %s | %s | %s | %s | %s |"
                  % (s["name"], est.get("n_used", 0), est.get("n", 0), method,
                     conc_txt, sem_txt, cpct_txt, r2_txt))

    md.extend([
        "",
        "## 3. 各系列成员明细",
        "",
    ])
    for s in quant.get("samples") or []:
        md.append("### %s" % s["name"])
        md.append("")
        md.append("| 泳道 | 带 | MW (kDa) | ratio | 面积 (gray·cm) | 标曲当量 (%s) | 换算浓度 (%s) | 状态 |" % (unit, unit))
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for m in s.get("members") or []:
            mw_txt = ("%.1f" % m["mw_kda"]) if _finite_number(m.get("mw_kda")) else "—"
            eq_txt = _fmt_value(m["equiv"]) if _finite_number(m.get("equiv")) else "—"
            c_txt = _fmt_value(m["conc"]) if _finite_number(m.get("conc")) else "—"
            if _finite_number(m.get("conc_sd")) and c_txt != "—":
                c_txt = "%s ± %s" % (c_txt, _fmt_sig(m["conc_sd"]))
            status = "剔除" if not m.get("used", True) else ("超范围" if m.get("out_of_range") else ("非正" if m.get("conc") is None else "有效"))
            md.append("| %d | %s | %s | %s | %.3f | %s | %s | %s |"
                      % (m["lane"], m.get("band") or "—", mw_txt, _fmt_trim(m["ratio"], 3),
                         m["area"], eq_txt, c_txt, status))
        md.append("")

    md.extend([
        "## 4. 误差来源说明",
        "",
        "- **随机误差（SEM）**：来自同一样品多泳道之间的离散度，随有效泳道数 `n_used` 增大而缩小（`SD / √n_used`）；单道（`n_used = 1`）不报随机误差。",
        "- **共同模误差（common_pct）**：来自标曲回归线本身的共享不确定度与 `--load-cv` 加样误差的方和根，**不随样本重复次数 `n` 缩小**。",
    ])

    notes = quant.get("notes") or []
    if notes:
        md.extend(["", "## 5. 备注", ""])
        for note in notes:
            md.append("- %s" % note)
    md.append("")
    return "\n".join(md)
