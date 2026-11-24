#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_densitometry — analytic checks for the densitometry math (spec §7 items 1-4, 6-8, 10, 11).

Every report used here is built in memory by `_synthetic_report` (exported: selftest_cli reuses it
for the end-to-end CLI run), so no user file is read and no ImageMagick is called.  The gray /
baseline arrays follow the real pipeline: `profiles.ink_trace` builds the top-hat baseline, so the
global-baseline numbers below are what a real report would give (the top-hat dips under a band).

`run()` returns the number of passed checks and prints one `selftest_densitometry N/N 通过` line.
"""
import ast
import json
import math
import os
import tempfile

from epcraft import densitometry
from epcraft import profiles


# ---------------------------------------------------------------- synthetic report v2

def _synthetic_report(lanes=None, slope=0.0, height_cm=6.0, dy_cm=0.01, width_cm=8.6,
                      texture=0.0, version=2):
    """Build a report v2 dict in memory (spec §7).

    `lanes` = list of lane specs; None → one sample lane "S1" without bands.  A spec is a dict:
      label (str|None), marker (bool), trimmed (bool), noise (float, default 0.4),
      bands — list of (y_cm, amplitude, sigma_cm) tuples (Gaussians subtracted from the
              background; the report band dicts are built from them), or a list of band dicts
              already in report form (then nothing is subtracted),
      gray  — optional callable `y_cm -> gray` or a full gray list overriding the background
              (used by the flat-top / saturation cases).

    Background gray = 200 + slope·y − Σ A_k·exp(−½((y−c_k)/σ_k)²) (+ a deterministic texture),
    rounded to 2 dp like the real report; the baseline is the real top-hat of
    `profiles.ink_trace`, so the analytic comparisons measure the algorithm, not a mock.
    """
    if lanes is None:
        lanes = [{"label": "S1", "bands": []}]
    y = [round(k * dy_cm, 4) for k in range(int(round(height_cm / dy_cm)))]
    px_per_cm = 1.0 / dy_cm

    out_lanes = []
    for no, spec in enumerate(lanes, 1):
        spec = dict(spec or {})
        raw_bands = spec.get("bands") or []
        tuples = [tuple(b) for b in raw_bands if not isinstance(b, dict)]
        given = [dict(b) for b in raw_bands if isinstance(b, dict)]

        def background(yy):
            g = 200.0 + slope * yy
            for c, amp, sig in tuples:
                g -= amp * math.exp(-0.5 * ((yy - c) / sig) ** 2)
            if texture:
                g += texture * math.sin(37.0 * yy) * math.sin(11.0 * yy)
            return g

        override = spec.get("gray")
        if override is None:
            gray = [background(yy) for yy in y]
        elif callable(override):
            gray = [override(yy) for yy in y]
        else:
            gray = [float(v) for v in override]
            assert len(gray) == len(y), "gray override 长度 %d ≠ 剖面 %d 行" % (len(gray), len(y))
        base, ink, _sm = profiles.ink_trace(gray, px_per_cm)

        band_dicts = given
        for c, amp, sig in tuples:
            band_dicts.append({"y_cm": round(float(c), 4), "prominence": round(float(amp), 2),
                               "snr": 4.0, "width_cm": round(2.3548 * float(sig), 4),
                               "mw_kda": 45.0, "extrapolated": False, "manual": False})
        band_dicts.sort(key=lambda b: float(b["y_cm"]))
        for b in band_dicts:
            b.setdefault("strongest", False)
        if band_dicts and not any(b.get("strongest") for b in band_dicts):
            max(band_dicts, key=lambda b: float(b.get("prominence") or 0.0))["strongest"] = True

        out_lanes.append({
            "lane": no, "label": spec.get("label"), "marker": bool(spec.get("marker")),
            "trimmed": bool(spec.get("trimmed")),
            "band_source": "lines" if spec.get("marker") else "called",
            "left_cm": 0.0, "right_cm": width_cm,
            "noise": float(spec.get("noise", 0.4)),
            "profile": {"y_cm": y,
                        "gray": [round(v, 2) for v in gray],
                        "baseline": [round(v, 2) for v in base],
                        "ink": [round(v, 2) for v in ink]},
            "bands": band_dicts,
        })

    marker_lane = next((ln["lane"] for ln in out_lanes if ln["marker"]), 0)
    return {
        "version": version,
        "units": {"x": "cm from gel-face left", "y": "cm from gel-face top",
                  "gray": "0-255 (lower = darker)", "ink": "baseline - gray (higher = more stain)"},
        "gel": {"width_cm": width_cm, "height_cm": height_cm, "px_per_cm": round(px_per_cm, 3)},
        "window": {"top_cm": 0.0, "bottom_cm": round(height_cm, 4)},
        "calibration": {"method": "interp", "marker_lane": marker_lane, "points": []},
        "params": {"base_half_cm": profiles.BASE_HALF_CM, "smooth_cm": profiles.SMOOTH_CM},
        "lanes": out_lanes, "notes": [],
    }


def _truth(a, sigma):
    """Analytic area of a Gaussian: A·σ·√(2π)."""
    return a * sigma * math.sqrt(2.0 * math.pi)


def _rel(value, truth):
    return value / truth - 1.0


def _band_of(dens, lane_no=1, index=1):
    for lane in dens["lanes"]:
        if lane["lane"] == lane_no:
            for band in lane["bands"]:
                if band["index"] == index:
                    return band
    raise AssertionError("dens 里没有泳道 %d 的带 %d" % (lane_no, index))


def _finite(obj, path="dens"):
    """Every float in the built dict is finite (no NaN / inf anywhere, spec §7.4)."""
    if isinstance(obj, float):
        assert math.isfinite(obj), "%s 不是有限数：%r" % (path, obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _finite(v, "%s.%s" % (path, k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _finite(v, "%s[%d]" % (path, i))


# ---------------------------------------------------------------- 1) single Gaussian, flat background

def check_single_gaussian():
    """σ = 0.03 recovers the analytic area on the flank baseline; σ = 0.06 (the spec's sizing)
    is −3.1 % on flank and −42.4 % on global — the numbers the spec was calibrated with (R9)."""
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.03)]}])
    band = _band_of(densitometry.measure(rep))
    truth = _truth(30.0, 0.03)
    # narrow band: the flank ramp recovers the analytic area within 2 %
    assert abs(_rel(band["area_flank"], truth)) <= 0.02, ("σ=0.03", band["area_flank"], truth)
    # the global top-hat dips under the band: ≥ 10 % short (the reason the flank baseline exists)
    assert _rel(band["area_global"], truth) <= -0.10, ("σ=0.03 global", band["area_global"], truth)

    # the spec's own sizing: σ = 0.06, A = 30 → flank −3.1 %, global −42.4 % (R9, measured)
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.06)]}])
    band = _band_of(densitometry.measure(rep))
    truth = _truth(30.0, 0.06)
    assert abs(_rel(band["area_flank"], truth)) <= 0.05, ("σ=0.06", band["area_flank"], truth)
    assert abs(_rel(band["area_flank"], truth) + 0.031) <= 0.010, \
        ("σ=0.06 flank 应约 −3.1 %", band["area_flank"] / truth)
    assert _rel(band["area_global"], truth) <= -0.25, ("σ=0.06 global", band["area_global"], truth)
    assert abs(_rel(band["area_global"], truth) + 0.424) <= 0.015, \
        ("global 应约 −42.4 %", band["area_global"] / truth)
    return True


# ---------------------------------------------------------------- 2) sloping background

def check_sloping_background():
    """40 gray levels over the profile: the flank ramp absorbs the slant, the global one does not."""
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.03)]}], slope=40.0 / 5.0)
    band = _band_of(densitometry.measure(rep))
    truth = _truth(30.0, 0.03)
    assert abs(_rel(band["area_flank"], truth)) <= 0.02, ("slope flank", band["area_flank"], truth)
    assert _rel(band["area_global"], truth) <= -0.10, ("slope global", band["area_global"], truth)
    assert band["ramp"]["source"] == "flank", band["ramp"]
    # the ramp follows the background: its two levels differ by the slope over their y span
    (yl, gl), (yr, gr) = band["ramp"]["left"], band["ramp"]["right"]
    want = (40.0 / 5.0) * (yr - yl)
    got = gr - gl
    assert got * want > 0.0 and abs(got - want) <= 0.5 * abs(want), (gl, gr, want)
    return True


# ---------------------------------------------------------------- 3) two Gaussians

def check_two_gaussians():
    """0.06 cm apart at σ = 0.06 (the spec's sizing): the two windows meet at the midpoint without
    overlapping, each is clamped there, and each flank area keeps ≈ ⅔ — the missing third is the
    neighbour's tail inside the flank, which also forces the flank fallback (R9 sizing)."""
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.06), (3.06, 30.0, 0.06)]}])
    dens = densitometry.measure(rep)
    b1, b2 = _band_of(dens, 1, 1), _band_of(dens, 1, 2)
    mid = (3.0 + 3.06) / 2.0
    truth = _truth(30.0, 0.06)
    for b in (b1, b2):
        assert abs(_rel(b["area_flank"], truth) + 0.333) <= 0.02, \
            ("close pair 每侧 flank 约 −33 %", b["area_flank"] / truth)
    assert b1["window_global"][1] <= mid + densitometry.FUZZ, b1["window_global"]
    assert b2["window_global"][0] >= mid - densitometry.FUZZ, b2["window_global"]
    assert b1["window_flank"][1] <= mid + densitometry.FUZZ, b1["window_flank"]
    assert b2["window_flank"][0] >= mid - densitometry.FUZZ, b2["window_flank"]
    # the windows meet at the midpoint but never cross it (no double counting of a band)
    assert b1["window_global"][1] <= b2["window_global"][0] + densitometry.FUZZ, (b1, b2)
    assert "clamped_mid_down" in b1["notes"], b1["notes"]
    assert "clamped_mid_up" in b2["notes"], b2["notes"]
    assert any("的中点" in n for n in dens["notes"]), dens["notes"]

    # well separated: each flank area stays within 5 % of its own analytic value
    rep = _synthetic_report([{"label": "S1", "bands": [(2.6, 30.0, 0.03), (3.0, 30.0, 0.03)]}])
    dens = densitometry.measure(rep)
    truth = _truth(30.0, 0.03)
    for index in (1, 2):
        band = _band_of(dens, 1, index)
        assert abs(_rel(band["area_flank"], truth)) <= 0.05, (index, band["area_flank"], truth)
    return True


# ---------------------------------------------------------------- 4) band-free lane, invisible band

def check_band_free_lane():
    """No bands → no band objects, zero totals, and no NaN anywhere (spec §7.4)."""
    dens = densitometry.measure(_synthetic_report([{"label": "S1", "bands": []}]))
    lane = dens["lanes"][0]
    assert lane["bands"] == [], lane["bands"]
    assert lane["area_global"] == 0.0 and lane["area_flank"] == 0.0, (lane["area_global"], lane["area_flank"])
    _finite(dens)

    # an invisible band (ink never above min_ink) still gets a band object: the fallback window
    # spans the band width, nothing is integrated, and the reason is in the top-level notes.
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 0.3, 0.03)]}])
    dens = densitometry.measure(rep)
    band = _band_of(dens)
    assert band["notes"] == ["invisible"], band["notes"]
    assert abs(band["window_global"][1] - band["window_global"][0] - 2.3548 * 0.03) <= 0.011, band["window_global"]
    assert any("min_ink" in n for n in dens["notes"]), dens["notes"]
    _finite(dens)
    return True


# ---------------------------------------------------------------- 5) saturated / flat-topped bands

def check_saturation_flags():
    """`clipped` = a row in window_flank at gray ≤ 8; `flat_top` = ≥ 3 rows within 1 level of the
    window minimum; both false on a plain Gaussian (they are notes, never errors)."""
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.03)]}])
    plain = _band_of(densitometry.measure(rep))
    assert plain["clipped"] is False and plain["flat_top"] is False, plain

    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 200.0, 0.03)]}])
    dark = _band_of(densitometry.measure(rep))
    gray = rep["lanes"][0]["profile"]["gray"]
    assert dark["clipped"] is True, dark
    assert min(gray) <= 8.0, min(gray)

    def flat(yy):
        if 2.90 <= yy <= 3.10:
            return 180.0
        if 2.80 <= yy < 2.90:
            return 200.0 - 20.0 * (yy - 2.80) / 0.10
        if 3.10 < yy <= 3.20:
            return 200.0 - 20.0 * (3.20 - yy) / 0.10
        return 200.0

    rep = _synthetic_report([{"label": "S1", "gray": flat, "bands": [
        {"y_cm": 3.0, "prominence": 20.0, "snr": 6.0, "width_cm": 0.2, "mw_kda": 45.0,
         "extrapolated": False, "manual": False}]}])
    flat_band = _band_of(densitometry.measure(rep))
    assert flat_band["flat_top"] is True, flat_band
    assert flat_band["clipped"] is False, flat_band
    return True


# ---------------------------------------------------------------- 6) profile ends

def check_profile_ends():
    """A band at the first / last profile row clamps its window to that row (never a negative
    index) and says so with the ASCII tag and the Chinese note."""
    for centre, tag, note in (("top", "clamped_top", "剖面上端"), ("bottom", "clamped_bottom", "剖面下端")):
        y0 = 0.0 if centre == "top" else 5.99
        rep = _synthetic_report([{"label": "S1", "bands": [(y0, 30.0, 0.03)]}])
        dens = densitometry.measure(rep)
        band = _band_of(dens)
        edges = (rep["lanes"][0]["profile"]["y_cm"][0], rep["lanes"][0]["profile"]["y_cm"][-1])
        assert band["window_global"][0] >= edges[0] - densitometry.FUZZ, band["window_global"]
        assert band["window_global"][1] <= edges[1] + densitometry.FUZZ, band["window_global"]
        assert band["window_flank"][0] >= edges[0] - densitometry.FUZZ, band["window_flank"]
        assert band["window_flank"][1] <= edges[1] + densitometry.FUZZ, band["window_flank"]
        assert tag in band["notes"], (centre, band["notes"])
        if centre == "top":
            assert abs(band["window_global"][0] - edges[0]) <= densitometry.FUZZ, band["window_global"]
        else:
            assert abs(band["window_global"][1] - edges[1]) <= densitometry.FUZZ, band["window_global"]
        assert any(note in n for n in dens["notes"]), (centre, dens["notes"])
    # and walk_window itself returns the profile rows, never an index below zero
    y = [0.01 * k for k in range(40)]
    ink = [1.0] * 40
    assert densitometry.walk_window(y, ink, 0.0, 0.0, y[-1], 0.5) == (y[0], y[-1])
    return True


# ---------------------------------------------------------------- 7) --lanes grammar

def check_parse_lanes():
    """`1,2,8-10` → [1, 2, 8, 9, 10]; None / "" → every lane; bad input → RuntimeError naming --lanes."""
    assert densitometry.parse_lanes("1,2,8-10", 12) == [1, 2, 8, 9, 10]
    assert densitometry.parse_lanes(" 2 , 4-5 ", 6) == [2, 4, 5]
    assert densitometry.parse_lanes("3,3,2", 5) == [2, 3]          # 去重、升序
    assert densitometry.parse_lanes(None, 5) == [1, 2, 3, 4, 5]
    assert densitometry.parse_lanes("", 5) == [1, 2, 3, 4, 5]
    for bad in ("0", "13", "5-3", "x", "1,,3", "1-", "-2"):
        try:
            densitometry.parse_lanes(bad, 12)
        except RuntimeError as e:
            assert "--lanes" in str(e) and "\n" not in str(e), (bad, str(e))
        else:
            raise AssertionError("--lanes %r 该报错" % (bad,))
    return True


# ---------------------------------------------------------------- 8) lane selection + report loading

def check_lane_selection():
    """The four cases of ruling R8, `params.lanes` agreeing with the measured lanes, a missing lane
    as an error, and the `.vectorcraft` → report resolution (missing / stale → 先 sync)."""
    lanes = []
    for no in range(1, 13):
        spec = {"label": "L%d" % no, "bands": [(3.0, 20.0, 0.03)]}
        if no == 1:
            spec["marker"] = True
        if no == 3:
            spec["trimmed"] = True
        lanes.append(spec)
    rep = _synthetic_report(lanes)

    dens = densitometry.measure(rep)
    assert [ln["lane"] for ln in dens["lanes"]] == [2, 4, 5, 6, 7, 8, 9, 10, 11, 12], dens["lanes"]
    assert dens["params"]["lanes"] == "all sample lanes, trimmed and marker excluded", dens["params"]["lanes"]

    dens = densitometry.measure(rep, include_marker=True)
    assert [ln["lane"] for ln in dens["lanes"]][0] == 1 and 3 not in [ln["lane"] for ln in dens["lanes"]]
    assert dens["params"]["lanes"] == "lanes 1-2,4-12", dens["params"]["lanes"]

    dens = densitometry.measure(rep, include_trimmed=True)
    assert [ln["lane"] for ln in dens["lanes"]] == list(range(2, 13)), dens["lanes"]
    assert dens["params"]["lanes"] == "lanes 2-12", dens["params"]["lanes"]

    dens = densitometry.measure(rep, include_marker=True, include_trimmed=True)
    assert [ln["lane"] for ln in dens["lanes"]] == list(range(1, 13)), dens["lanes"]
    assert dens["params"]["lanes"] == "all lanes", dens["params"]["lanes"]

    # --lanes replaces the default: a trimmed lane is measured whatever its flags
    dens = densitometry.measure(rep, lanes=[3, 1])
    assert [ln["lane"] for ln in dens["lanes"]] == [1, 3], dens["lanes"]
    assert dens["params"]["lanes"] == "lanes 1,3", dens["params"]["lanes"]

    small = _synthetic_report([{"label": "a"}, {"label": "b"}, {"label": "c"}])
    for bad in ([7], [], [0]):
        try:
            densitometry.measure(small, lanes=bad)
        except RuntimeError as e:
            assert "\n" not in str(e), str(e)
        else:
            raise AssertionError("--lanes %r 该报错" % (bad,))

    with tempfile.TemporaryDirectory() as td:
        sample = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.03)]}])
        report = os.path.join(td, "fig.mw.json")
        densitometry.write_json(sample, report)
        doc = os.path.join(td, "fig.vectorcraft")
        with open(doc, "w", encoding="utf-8") as f:
            f.write("document")
        os.utime(doc, (2000, 2000))
        os.utime(report, (1000, 1000))
        try:
            densitometry.load_report(doc)
        except RuntimeError as e:
            assert "先 sync" in str(e) and "\n" not in str(e), str(e)
        else:
            raise AssertionError("旧报告该被拒绝")
        os.utime(report, (3000, 3000))
        loaded = densitometry.load_report(doc)
        assert loaded[densitometry.REPORT_PATH_KEY] == doc, loaded[densitometry.REPORT_PATH_KEY]
        assert densitometry.measure(loaded)["source"]["report"] == doc, "source.report 应保留用户写的路径"
        assert len(densitometry.measure(loaded)["lanes"]) == 1
        os.remove(report)
        try:
            densitometry.load_report(doc)
        except RuntimeError as e:
            assert "先 sync" in str(e), str(e)
        else:
            raise AssertionError("缺报告该被拒绝")

    # not v2 / no profile / non-uniform y are RuntimeErrors
    for bad in (dict(_synthetic_report([{"label": "S1"}]), version=1),
                {"version": 2, "lanes": [{"lane": 1, "bands": []}], "gel": {}, "window": {}}):
        try:
            densitometry.load_report(_write_tmp(bad))
        except RuntimeError as e:
            assert "\n" not in str(e), str(e)
        else:
            raise AssertionError("坏报告该被拒绝：%r" % (bad.get("version"),))

    # bad parameters are ValueError (§4.1), the report dict is not modified
    rep_ok = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.03)]}])
    before = json.dumps(rep_ok, sort_keys=True)
    for kwargs in ({"flank_cm": 0.0}, {"flank_cm": -0.1}, {"min_ink": -1.0}, {"smooth_cm": 0.0}):
        try:
            densitometry.measure(rep_ok, **kwargs)
        except ValueError as e:
            assert "\n" not in str(e), str(e)
        else:
            raise AssertionError("参数 %r 该被拒绝" % (kwargs,))
    assert json.dumps(rep_ok, sort_keys=True) == before, "measure 改了传进来的报告"
    rep_ok = _synthetic_report([{"label": "S1"}])
    rep_ok["lanes"][0]["profile"]["y_cm"][20] += 0.005               # 剖面 y 不均匀（步长跳一半）
    try:
        densitometry.load_report(_write_tmp(rep_ok))
    except RuntimeError as e:
        assert "均匀" in str(e), str(e)
    else:
        raise AssertionError("不均匀的 y 该被拒绝")
    return True


def _write_tmp(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    densitometry.write_json(obj, path)
    return path


# ---------------------------------------------------------------- 9) dens.json shape (§4.4)

def check_schema():
    """The built dict has exactly the §4.4 keys, the fixed ASCII tag vocabulary, `strongest` as a
    bool on every band (ruling R3), and `load_json`'s four RuntimeError cases (ruling R4)."""
    rep = _synthetic_report([{"label": "S1", "bands": [(3.0, 30.0, 0.03)]},
                             {"label": "S2", "bands": [{"y_cm": 4.0, "prominence": 5.0, "snr": 2.0,
                                                       "width_cm": 0.1, "mw_kda": None}]}])
    dens = densitometry.measure(rep)
    assert set(dens) == {"version", "kind", "units", "source", "params", "lanes", "notes"}, sorted(dens)
    assert set(dens["units"]) == {"y", "gray", "ink", "area"}, dens["units"]
    assert set(dens["source"]) == {"report", "gel", "window", "marker_lane"}, dens["source"]
    assert set(dens["params"]) == {"flank_cm", "min_ink", "smooth_cm", "window", "lanes"}, dens["params"]
    assert dens["source"]["report"] == "", "内存里的报告没有路径，source.report 应是空串"
    lane_keys = {"lane", "label", "marker", "trimmed", "noise", "profile", "bands",
                 "area_global", "area_flank"}
    band_keys = {"index", "y_cm", "mw_kda", "strongest", "peak_ink_global", "peak_ink_flank",
                 "window_global", "window_flank", "area_global", "area_flank", "ramp", "clipped",
                 "flat_top", "notes"}
    tags = {"invisible", "clamped_top", "clamped_bottom", "clamped_mid_up", "clamped_mid_down",
            "flank_fallback_left", "flank_fallback_right"}
    for lane in dens["lanes"]:
        assert set(lane) == lane_keys, sorted(lane)
        assert set(lane["profile"]) == {"y_cm", "gray", "baseline_global"}, sorted(lane["profile"])
        assert "ink" not in lane["profile"], "ink 不该被存进 dens.json（可推导）"
        assert isinstance(lane["area_global"], float) and isinstance(lane["area_flank"], float)
        for band in lane["bands"]:
            assert set(band) == band_keys, sorted(band)
            assert set(band["ramp"]) == {"left", "right", "source"}, sorted(band["ramp"])
            assert isinstance(band["strongest"], bool), band["strongest"]
            assert isinstance(band["clipped"], bool) and isinstance(band["flat_top"], bool)
            assert len(band["window_global"]) == 2 and len(band["window_flank"]) == 2
            assert band["window_global"][0] <= band["window_global"][1], band["window_global"]
            assert band["window_flank"][0] <= band["window_flank"][1], band["window_flank"]
            for tag in band["notes"]:
                assert tag in tags, "band.notes 只放固定的 ASCII 词：%r" % (tag,)
    # 报告里 strongest=true 的带原样带过来
    rep2 = _synthetic_report([{"label": "S1", "bands": [
        {"y_cm": 3.0, "prominence": 20.0, "strongest": True},
        {"y_cm": 4.0, "prominence": 20.0, "strongest": False}]}])
    dens2 = densitometry.measure(rep2)
    assert [b["strongest"] for b in dens2["lanes"][0]["bands"]] == [True, False], dens2["lanes"][0]["bands"]

    # load_json 的四种拒绝：缺文件 / 不是 JSON / 不是对象 / kind 不符（都一行 RuntimeError）
    with tempfile.TemporaryDirectory() as td:
        for bad, want in (("missing", "找不到"), ("notjson", "JSON"), ("array", "对象"), ("kind", "kind")):
            path = os.path.join(td, "x.json")
            if bad == "notjson":
                with open(path, "w", encoding="utf-8") as f:
                    f.write("{not json")
            elif bad == "array":
                with open(path, "w", encoding="utf-8") as f:
                    f.write("[1, 2]")
            elif bad == "kind":
                densitometry.write_json({"version": 1, "kind": "other"}, path)
            try:
                if bad == "missing":
                    densitometry.load_json(os.path.join(td, "gone.json"))
                else:
                    densitometry.load_json(path, "ep-craft-densitometry")
            except RuntimeError as e:
                assert want in str(e) and "\n" not in str(e), (bad, str(e))
            else:
                raise AssertionError("load_json 该拒绝 %s" % bad)
    # kind=None 时不校验 kind（dens.json 由别的命令读时才有意义）
    assert densitometry.load_json(_write_tmp({"any": 1})) == {"any": 1}
    return True


# ---------------------------------------------------------------- 10) JSON round trip + idempotence

def check_json_io():
    """write_json → load_json keeps every field; UTF-8 with no `\\u` escapes; measure twice →
    byte-identical JSON (no timestamps, spec §7.10-§7.11)."""
    rep = _synthetic_report([{"label": "S1", "bands": [(0.0, 30.0, 0.03), (3.0, 30.0, 0.03)]},
                             {"label": None, "marker": True, "bands": []}])
    dens = densitometry.measure(rep, include_marker=True)
    assert dens["notes"], "该报告应有贴边提示：%r" % (dens["notes"],)
    with tempfile.TemporaryDirectory() as td:
        p1, p2 = os.path.join(td, "a.dens.json"), os.path.join(td, "b.dens.json")
        densitometry.write_json(dens, p1)
        back = densitometry.load_json(p1, "ep-craft-densitometry")
        assert back == dens, "round trip 丢了字段"
        assert back["kind"] == "ep-craft-densitometry" and back["version"] == 1
        with open(p1, encoding="utf-8") as f:
            raw = f.read()
        assert raw.endswith("\n")
        assert "\\u" not in raw, "ensure_ascii=False 失效"
        assert dens["notes"][0] in raw, "中文 notes 没有原样写盘"
        densitometry.write_json(densitometry.measure(rep, include_marker=True), p2)
        with open(p1, "rb") as f1, open(p2, "rb") as f2:
            assert f1.read() == f2.read(), "measure 两次的结果不一致（有不确定的内容）"
    # a directory that does not exist must fail loudly, not silently create it
    with tempfile.TemporaryDirectory() as td:
        try:
            densitometry.write_json(dens, os.path.join(td, "nope", "x.json"))
        except OSError:
            pass
        else:
            raise AssertionError("写进不存在的目录却成功了")
    return True


# ---------------------------------------------------------------- 11) module purity

def check_purity():
    """`densitometry.py` is math + JSON only: no subprocess, no ImageMagick, no gel_photo (§2.11)."""
    path = os.path.join(os.path.dirname(os.path.abspath(densitometry.__file__)), "densitometry.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    for forbidden in ("subprocess", "magick_io", "gel_photo", "dens_plot", "profiles"):
        assert forbidden not in imported, "densitometry.py 不该 import %s" % forbidden
    stdlib = getattr(__import__("sys"), "stdlib_module_names", set())
    assert imported <= set(stdlib), "densitometry.py 导入了非 stdlib 模块：%s" % (imported - set(stdlib),)
    return True


# ---------------------------------------------------------------- 12) curve: parse_std (§2.9)

def _curve_dens(bands_by_lane, marker_lanes=()):
    """A dens-like dict for `build_curve`: only the keys the curve reads (spec §4.4 shape).

    `bands_by_lane` = [[2.5], [5.0, 3.0], …]: one inner list of band areas per lane.  Every band
    becomes {index, strongest, area_global, area_flank} and the lane totals are the sums, so a
    `:i` entry and a `:*` entry can both be checked.  `marker_lanes` marks 1-based lanes.
    """
    lanes = []
    for no, areas in enumerate(bands_by_lane, 1):
        values = [float(a) for a in areas]
        best = max(values) if values else None
        bands = [{"index": index, "strongest": area == best, "area_global": area,
                  "area_flank": area}
                 for index, area in enumerate(values, 1)]
        lanes.append({"lane": no, "label": "S%d" % no, "marker": no in marker_lanes,
                      "trimmed": False, "bands": bands,
                      "area_global": float(sum(values)), "area_flank": float(sum(values))})
    return {"version": 1, "kind": "ep-craft-densitometry", "lanes": lanes}


def _curve(areas, masses, **kwargs):
    """A linear-case curve: one strong band per lane, masses in lane order (§4.5)."""
    dens = _curve_dens([[a] for a in areas])
    std = [(no, None, mass) for no, mass in enumerate(masses, 1)]
    return densitometry.build_curve(dens, std, **kwargs)


# ---------------------------------------------------------------- 12b) fit statistics (ep-c1i §2/§3)

# The exact fit_params key sets the ep-c1i contract fixes: the old point estimates plus the
# statistics, and nothing else (`_quant_fit` tolerates the extra keys, so they must not grow).
_LINEAR_STAT_KEYS = {"slope", "intercept", "r2", "n", "space",
                     "slope_sd", "intercept_sd", "cov_slope_intercept", "residual_sd",
                     "sxx", "mean_x"}
_POWER_STAT_KEYS = {"exponent", "scale", "r2", "n", "space",
                    "exponent_sd", "scale_sd", "residual_sd", "sxx", "mean_x"}

# The four-dp rounding of ruling R2: a stored number may differ from the true value by at most
# half a unit in the fourth decimal.  5.1e-5 leaves room for the binary repr of that rounding.
_STAT_TOL = 5.1e-5


def _fit_stat_values(params):
    """The ep-c1i statistics of one `fit_params` dict, as `(name, stored)` pairs in a fixed order."""
    return [(key, params.get(key)) for key in
            ("slope_sd", "intercept_sd", "cov_slope_intercept", "residual_sd",
             "exponent_sd", "scale_sd", "sxx", "mean_x") if key in params]


def _assert_stat(params, key, expected, what):
    """One statistic: a number (never None/NaN at n > 2) within the 4-dp rounding tolerance."""
    value = params.get(key)
    assert isinstance(value, float) and math.isfinite(value), \
        "%s: %s 该是有限数（收到 %r）：%r" % (what, key, value, params)
    assert abs(value - float(expected)) <= _STAT_TOL + abs(float(expected)) * 1e-12, \
        "%s: %s = %r，独立手算为 %r（差 %.6g）：%r" % (
            what, key, value, float(expected), abs(value - float(expected)), params)
    assert _decimals(value) <= 4, "%s: %s 超过 4 位小数：%r" % (what, key, value)


def check_fit_statistics_linear():
    """ep-c1i §2/§3: the linear fit stores slope_sd, intercept_sd, cov_slope_intercept,
    residual_sd, sxx and mean_x, each reproducing an independent hand computation.

    The expected numbers are computed HERE with plain arithmetic from the same standard points
    (no production helper is imported), so a formula bug inside `_linfit` cannot hide behind
    itself.  Two curves are used: a noisy one whose literals are written out below, and a
    second one checked only through the re-computed arithmetic.
    """
    # noisy fixture: areas 1, 2, 4, 8 ↔ masses 3, 5, 11, 19.  By hand:
    #   x̄ = 3.75, Σ(x−x̄)² = 28.75, slope = 2.313043…, intercept = 0.826087…,
    #   SSres = 1.774194…, s = sqrt(SSres/2) = 0.768963…,
    #   slope_sd = s/sqrt(sxx) = 0.143412…, intercept_sd = s·sqrt(1/4 + x̄²/sxx) = 0.661098…,
    #   cov = −s²·x̄/sxx = −0.077127…, r² = 1 − SSres/SStot = 0.992370…
    curve = _curve([1.0, 2.0, 4.0, 8.0], [3.0, 5.0, 11.0, 19.0])
    params = curve["fit_params"]
    assert set(params) == _LINEAR_STAT_KEYS, sorted(params)
    assert params["n"] == 4 and params["space"] == "linear", params
    assert abs(params["slope"] - 2.3130) <= _STAT_TOL, params
    assert abs(params["intercept"] - 0.8261) <= _STAT_TOL, params
    assert abs(params["r2"] - 0.9924) <= _STAT_TOL, params
    assert abs(params["mean_x"] - 3.75) <= _STAT_TOL, params
    assert abs(params["sxx"] - 28.75) <= _STAT_TOL, params
    assert abs(params["residual_sd"] - 0.7690) <= _STAT_TOL, params
    assert abs(params["slope_sd"] - 0.1434) <= _STAT_TOL, params
    assert abs(params["intercept_sd"] - 0.6611) <= _STAT_TOL, params
    assert abs(params["cov_slope_intercept"] - (-0.0771)) <= _STAT_TOL, params

    # the same formulas re-derived from the points, so the fixture literals above cannot be a
    # self-fulfilling copy of whatever the implementation printed
    reference = _ols_reference([1.0, 2.0, 4.0, 8.0], [3.0, 5.0, 11.0, 19.0])
    for key, expected in (("slope_sd", reference["slope_sd"]),
                          ("intercept_sd", reference["intercept_sd"]),
                          ("cov_slope_intercept", reference["cov"]),
                          ("residual_sd", reference["residual_sd"]),
                          ("sxx", reference["sxx"]),
                          ("mean_x", reference["mean_x"])):
        _assert_stat(params, key, expected, "noisy linear fit")

    # a second curve, only through the arithmetic: areas 2, 3, 5, 9 ↔ masses 4, 7, 13, 21
    second = _curve([2.0, 3.0, 5.0, 9.0], [4.0, 7.0, 13.0, 21.0])
    reference = _ols_reference([2.0, 3.0, 5.0, 9.0], [4.0, 7.0, 13.0, 21.0])
    assert set(second["fit_params"]) == _LINEAR_STAT_KEYS, sorted(second["fit_params"])
    for key, expected in (("slope_sd", reference["slope_sd"]),
                          ("intercept_sd", reference["intercept_sd"]),
                          ("cov_slope_intercept", reference["cov"]),
                          ("residual_sd", reference["residual_sd"]),
                          ("sxx", reference["sxx"]),
                          ("mean_x", reference["mean_x"])):
        _assert_stat(second["fit_params"], key, expected, "second linear fit")
    # sxx / mean_x are stored in the fit's own space: linear → the areas themselves
    assert abs(second["fit_params"]["mean_x"] - reference["mean_x"]) <= _STAT_TOL
    assert abs(second["fit_params"]["sxx"] - reference["sxx"]) <= _STAT_TOL
    # the covariance is negative whenever the points sit on the positive side (the line pivots
    # about x̄): a sign flip here is the classic slope/intercept covariance bug
    assert params["cov_slope_intercept"] <= 0.0, params
    return True


def check_fit_statistics_power():
    """ep-c1i §2/§3: the power fit's statistics are computed in log10 space exactly like its r²,
    and `scale_sd` applies the delta method `scale · ln(10) · se(log_c)`."""
    # areas 1, 2, 4, 8 ↔ masses 2, 5, 9, 21 (deliberately not an exact power law)
    curve = _curve([1.0, 2.0, 4.0, 8.0], [2.0, 5.0, 9.0, 21.0], fit="power")
    params = curve["fit_params"]
    assert set(params) == _POWER_STAT_KEYS, sorted(params)
    assert params["n"] == 4 and params["space"] == "log10", params
    # by hand, over (u, v) = (log10 area, log10 mass):
    #   ū = 0.451545…, Σ(u−ū)² = 0.453095…, exponent = 1.102495…, log_c = 0.321289…,
    #   s = 0.041744…, exponent_sd = s/sqrt(sxx) = 0.062016…,
    #   scale = 10^log_c = 2.095508…, scale_sd = scale·ln(10)·s·sqrt(1/4 + ū²/sxx) = 0.168520…
    assert abs(params["exponent"] - 1.1025) <= _STAT_TOL, params
    assert abs(params["scale"] - 2.0955) <= _STAT_TOL, params
    assert abs(params["r2"] - 0.9937) <= _STAT_TOL, params
    assert abs(params["mean_x"] - 0.4515) <= _STAT_TOL, params
    assert abs(params["sxx"] - 0.4531) <= _STAT_TOL, params
    assert abs(params["residual_sd"] - 0.0417) <= _STAT_TOL, params
    assert abs(params["exponent_sd"] - 0.0620) <= _STAT_TOL, params
    assert abs(params["scale_sd"] - 0.1685) <= _STAT_TOL, params

    areas = [1.0, 2.0, 4.0, 8.0]
    masses = [2.0, 5.0, 9.0, 21.0]
    reference = _ols_reference([math.log10(a) for a in areas], [math.log10(m) for m in masses])
    for key, expected in (("exponent_sd", reference["slope_sd"]),
                          ("residual_sd", reference["residual_sd"]),
                          ("sxx", reference["sxx"]),
                          ("mean_x", reference["mean_x"])):
        _assert_stat(params, key, expected, "power fit, log10 space")
    # the delta method is checked against its own formula, not against the stored scale: a
    # implementation that forgot the ln(10) factor would be off by ~2.3×
    scale = 10.0 ** reference["intercept"]
    expected_scale_sd = scale * math.log(10.0) * reference["intercept_sd"]
    _assert_stat(params, "scale_sd", expected_scale_sd, "power fit, delta method")
    assert abs(params["scale_sd"] - (scale * reference["intercept_sd"])) > 0.05, \
        "scale_sd 少了 delta 方法的 ln(10) 因子：%r" % (params["scale_sd"],)
    # the log-space sxx must not be the linear-space one: on this fixture they differ a lot
    linear_sxx = _ols_reference(areas, masses)["sxx"]
    assert abs(params["sxx"] - linear_sxx) > 0.05, \
        "power 的 sxx 不是对数空间的量：%r vs 线性空间 %r" % (params["sxx"], linear_sxx)
    return True


def check_fit_statistics_n2():
    """ep-c1i §3: with n <= 2 there is no residual degree of freedom, so EVERY new value is None —
    never a NaN, never a fake zero — and `curve` still succeeds.  The existing keys keep their
    values and the statistics rollout adds no note."""
    for fit in ("linear", "power"):
        if fit == "linear":
            curve = _curve([2.5, 5.0], [22.0, 42.0])
            new_keys = ("slope_sd", "intercept_sd", "cov_slope_intercept", "residual_sd",
                        "sxx", "mean_x")
            expected_keys = _LINEAR_STAT_KEYS
        else:
            curve = _curve([2.0, 4.0], [4.0, 16.0], fit="power")
            new_keys = ("exponent_sd", "scale_sd", "residual_sd", "sxx", "mean_x")
            expected_keys = _POWER_STAT_KEYS
        params = curve["fit_params"]
        assert set(params) == expected_keys, sorted(params)
        assert params["n"] == 2, params
        for key in new_keys:
            assert params[key] is None, \
                "%s: n=2 时 %s 该是 None（收到 %r），不能造出零或 NaN" % (fit, key, params[key])
        # the point estimates are untouched by the statistics rollout
        if fit == "linear":
            assert params["slope"] == 8.0 and params["intercept"] == 2.0, params
        else:
            assert params["exponent"] == 2.0 and params["scale"] == 1.0, params
        assert params["r2"] == 1.0 and params["space"] == ("linear" if fit == "linear"
                                                           else "log10"), params
        # no NaN / Infinity anywhere: the JSON must be able to hold nulls instead
        raw = json.dumps(curve)
        for token in ("NaN", "Infinity", "nan", "inf"):
            assert token not in raw, "curve 里出现了 %r：%r" % (token, raw[:200])
        # 3 points is the first n with one residual degree of freedom: numbers appear again
        if fit == "linear":
            three = _curve([1.0, 2.0, 4.0], [3.0, 5.0, 11.0])
        else:
            three = _curve([1.0, 2.0, 4.0], [2.0, 5.0, 9.0], fit="power")
        for key in new_keys:
            assert isinstance(three["fit_params"][key], float), \
                "%s: n=3 时 %s 该是实数（收到 %r）" % (fit, key, three["fit_params"][key])
    # the n=2 curve keeps the note it already had, and gains none about the missing statistics
    two = _curve([2.5, 5.0], [22.0, 42.0])
    assert two["notes"] == ["只有 2 个标准点，r² 不足以说明形状"], two["notes"]
    return True


def check_fit_statistics_json():
    """ep-c1i §3 precision: every statistic is 4 dp through `_num`, written as a JSON number (null
    when None), and a `write_json` / `load_json` round trip keeps it exactly."""
    curve = _curve([1.0, 2.0, 4.0, 8.0], [3.0, 5.0, 11.0, 19.0])
    for key in ("slope_sd", "intercept_sd", "cov_slope_intercept", "residual_sd",
                "sxx", "mean_x"):
        value = curve["fit_params"][key]
        assert isinstance(value, float) and math.isfinite(value), (key, value)
        assert _decimals(value) <= 4, "fit_params[%r] 超过 4 位小数：%r" % (key, value)
    power = _curve([1.0, 2.0, 4.0, 8.0], [2.0, 5.0, 9.0, 21.0], fit="power")
    for key in ("exponent_sd", "scale_sd", "residual_sd", "sxx", "mean_x"):
        value = power["fit_params"][key]
        assert isinstance(value, float) and math.isfinite(value), (key, value)
        assert _decimals(value) <= 4, "power fit_params[%r] 超过 4 位小数：%r" % (key, value)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "stats.curve.json")
        densitometry.write_json(curve, path)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        back = densitometry.load_json(path, "ep-craft-densitometry-curve")
        assert back == json.loads(raw), "round trip 丢了新增的统计字段"
        assert back["fit_params"] == curve["fit_params"], (back["fit_params"],
                                                            curve["fit_params"])
        # the statistics are real JSON numbers, and the n=2 case is a JSON null (not "NaN")
        for key in ("slope_sd", "intercept_sd", "cov_slope_intercept", "residual_sd",
                    "sxx", "mean_x"):
            assert isinstance(back["fit_params"][key], float), (key, back["fit_params"][key])
        two = _curve([2.5, 5.0], [22.0, 42.0])
        densitometry.write_json(two, path)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        assert raw.count("null") >= 6, "n=2 的统计该以 null 落盘：%r" % (raw,)
        back = densitometry.load_json(path, "ep-craft-densitometry-curve")
        assert back["fit_params"]["residual_sd"] is None, back["fit_params"]
    return True


def _ols_reference(xs, ys):
    """Plain-arithmetic OLS with the ep-c1i §2 statistics; computed in the test on purpose.

    Nothing here calls `densitometry._linfit`: the acceptance suite must be able to disagree with
    the production formula.  Returns full-precision values (the caller compares against the
    4-dp stored ones within `_STAT_TOL`).
    """
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ssres = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    sstot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ssres / sstot if sstot > 0.0 else 1.0
    if n > 2:
        sd = math.sqrt(ssres / (n - 2.0))
        slope_sd = sd / math.sqrt(sxx)
        intercept_sd = sd * math.sqrt(1.0 / n + mx * mx / sxx)
        cov = -sd * sd * mx / sxx
    else:
        sd = slope_sd = intercept_sd = cov = None
    return {"n": n, "mean_x": mx, "sxx": sxx, "slope": slope, "intercept": intercept,
            "r2": r2, "residual_sd": sd, "slope_sd": slope_sd,
            "intercept_sd": intercept_sd, "cov": cov}



def check_curve_parse_std():
    """`parse_std` accepts the LANE[[:INDEX]|[:*]]=MASS grammar and refuses everything else with
    one RuntimeError per case (§2.9)."""
    std = densitometry.parse_std("2=20,3:2=10,4:*=5")
    assert std == [(2, None, 20.0), (3, 2, 10.0), (4, "*", 5.0)], std
    assert densitometry.parse_std(" 2 = 1e1 , 3:1=2.5 ") == [(2, None, 10.0), (3, 1, 2.5)]
    assert densitometry.parse_std("5:*=0.5") == [(5, "*", 0.5)]
    # a lane may not be named twice: its mass would be ambiguous
    for bad, want in (
            ("", "空"),
            (" , ", "空"),
            ("2", "没有 ="),
            ("2=20,", "空的"),
            ("a=20", "正整数"),
            ("0=20", "≥ 1"),
            ("2:0=20", "≥ 1"),
            ("2:x=20", "1、2"),
            ("2:=20", "冒号后面是空的"),
            ("2=abc", "不是数字"),
            ("2=0", "正数"),
            ("2=-5", "正数"),
            ("2=nan", "正数"),
            ("2=20,2=20", "出现了两次")):
        try:
            densitometry.parse_std(bad)
        except RuntimeError as e:
            msg = str(e)
            assert want in msg and "\n" not in msg, (bad, msg)
            assert "--std" in msg, (bad, msg)
        else:
            raise AssertionError("--std %r 该被拒绝" % (bad,))
    return True


# ---------------------------------------------------------------- 13) curve: points + fits (§4.5)

def _decimals(value):
    """How many decimal places a stored float shows (`repr`, which is what lands in the JSON)."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        raise AssertionError("curve 里出现了科学计数法 %r（精度规则要求定点小数）" % (value,))
    return len(text.split(".")[1]) if "." in text else 0


def check_curve_schema_and_fit():
    """§4.5 shape + the exact linear fixture: slope 8 / intercept 2 / r² 1, replicates collapsed
    into one point (n, sd, cv_pct, lanes, entries), residuals mass-ascending with
    rel_pct = (predicted − mass)/mass·100, and rounding only in the built dict (ruling R2)."""
    curve = _curve([2.5, 5.0, 9.8, 10.2, 15.0, 30.0], [22, 42, 82, 82, 122, 242],
                   dens_path="/tmp/fig.dens.json", std_text="2=22,3=42,4=82,5=82,6=122,7=242")
    assert set(curve) == {"version", "kind", "unit", "baseline", "fit", "source", "points",
                          "fit_params", "range", "residuals", "notes"}, sorted(curve)
    assert curve["version"] == 1 and curve["kind"] == "ep-craft-densitometry-curve"
    assert curve["unit"] == "ug" and curve["baseline"] == "flank" and curve["fit"] == "linear"
    assert curve["source"] == {"dens": "/tmp/fig.dens.json",
                               "std": "2=22,3=42,4=82,5=82,6=122,7=242", "drop": []}
    # ep-c1i §3: the fit carries its own statistics next to the point estimates.  This fixture
    # is exactly linear, so every statistic is a true zero (n = 5 > 2, hence numbers, not None).
    assert set(curve["fit_params"]) == _LINEAR_STAT_KEYS, sorted(curve["fit_params"])
    assert curve["fit_params"] == {"slope": 8.0, "intercept": 2.0, "r2": 1.0, "n": 5,
                                   "space": "linear",
                                   "slope_sd": 0.0, "intercept_sd": 0.0,
                                   "cov_slope_intercept": 0.0, "residual_sd": 0.0,
                                   "sxx": 475.0, "mean_x": 12.5}, curve["fit_params"]
    point_keys = {"mass", "volume", "n", "sd", "cv_pct", "lanes", "entries", "used"}
    for point in curve["points"]:
        assert set(point) == point_keys, sorted(point)
        assert isinstance(point["lanes"], list) and point["lanes"] == sorted(point["lanes"])
        for lane, band in point["entries"]:
            assert isinstance(lane, int) and (band is None or band is True or band is False
                                              or isinstance(band, (int, str)))
        assert _decimals(point["volume"]) <= 3, point
        if point["sd"] is not None:
            assert _decimals(point["sd"]) <= 3 and _decimals(point["cv_pct"]) <= 2, point
    # 82 appears twice in --std: the two lanes collapse into ONE point with n=2 (§2.7)
    assert [p["mass"] for p in curve["points"]] == [22.0, 42.0, 82.0, 122.0, 242.0]
    assert [p["lanes"] for p in curve["points"]] == [[1], [2], [3, 4], [5], [6]]
    replicate = curve["points"][2]
    assert replicate["n"] == 2 and replicate["lanes"] == [3, 4]
    assert replicate["entries"] == [[3, None], [4, None]], replicate["entries"]
    assert abs(replicate["sd"] - 0.2828427) < 1e-3, replicate["sd"]
    assert abs(replicate["cv_pct"] - 2.83) < 0.01, replicate["cv_pct"]
    assert all(p["used"] for p in curve["points"])
    assert curve["range"] == {"volume_min": 2.5, "volume_max": 30.0,
                              "mass_min": 22.0, "mass_max": 242.0}, curve["range"]
    assert [r["mass"] for r in curve["residuals"]] == [22.0, 42.0, 82.0, 122.0, 242.0]
    for residual in curve["residuals"]:
        assert set(residual) == {"mass", "volume", "predicted_mass", "rel_pct"}, sorted(residual)
        assert abs(residual["predicted_mass"] - (2.0 + 8.0 * residual["volume"])) < 1e-3
        assert residual["rel_pct"] == 0.0, residual
    assert curve["notes"] == []
    # precision: fit_params 4 dp, points / residuals 3 dp (percentages 2 dp)
    assert _decimals(curve["fit_params"]["slope"]) <= 4 and _decimals(curve["fit_params"]["r2"]) <= 4
    for residual in curve["residuals"]:
        assert _decimals(residual["predicted_mass"]) <= 3
        assert _decimals(residual["rel_pct"]) <= 2
    # the power fit: mass = scale·volume^exponent, r² in log space (§4.5)
    power = _curve([2.0, 4.0, 6.0, 8.0], [0.2 * v ** 1.1 for v in (2.0, 4.0, 6.0, 8.0)], fit="power")
    assert set(power["fit_params"]) == _POWER_STAT_KEYS, sorted(power["fit_params"])
    assert power["fit_params"]["space"] == "log10"
    assert abs(power["fit_params"]["exponent"] - 1.1) < 1e-3, power["fit_params"]
    assert abs(power["fit_params"]["scale"] - 0.2) < 1e-3, power["fit_params"]
    assert power["fit_params"]["r2"] == 1.0 and power["fit_params"]["n"] == 4
    # §3: the power statistics live in log10 space — sxx and mean_x are the log sums
    assert abs(power["fit_params"]["mean_x"] - 0.6461) <= 5e-5, power["fit_params"]["mean_x"]
    assert abs(power["fit_params"]["sxx"] - 0.2045) <= 5e-5, power["fit_params"]["sxx"]
    assert abs(power["fit_params"]["exponent_sd"]) <= 1e-3, power["fit_params"]
    for residual in power["residuals"]:
        assert abs(residual["predicted_mass"] - residual["mass"]) < 1e-2, residual
        assert residual["rel_pct"] == 0.0, residual
    # --baseline global reads the other column, --unit is recorded, source keeps the typed paths
    dens = _curve_dens([[2.5], [5.0], [10.0]])
    for lane in dens["lanes"]:
        lane["area_global"] = lane["area_flank"] * 2.0
        for band in lane["bands"]:
            band["area_global"] = band["area_flank"] * 2.0
    global_curve = densitometry.build_curve(dens, [(1, None, 22), (2, None, 42), (3, None, 82)],
                                            baseline="global", unit="ng", fit="linear",
                                            dens_path="fig.dens.json", std_text="1=22,2=42,3=82")
    assert global_curve["baseline"] == "global" and global_curve["unit"] == "ng"
    assert global_curve["fit_params"]["slope"] == 4.0, global_curve["fit_params"]
    # the function is pure: neither input is modified, and two calls agree byte for byte
    dens = _curve_dens([[2.5], [5.0], [10.0], [15.0]])
    std = [(1, None, 22), (2, None, 42), (3, None, 82), (4, None, 122)]
    before = json.dumps([dens, std], sort_keys=True)
    first = densitometry.build_curve(dens, std)
    second = densitometry.build_curve(dens, std)
    assert json.dumps([dens, std], sort_keys=True) == before, "build_curve 改了输入"
    assert first == second, "build_curve 两次结果不一致"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    return True


def check_curve_bands():
    """`--std` band selection: no suffix = the strongest band, `:i` = the i-th, `:*` = the lane
    total; `entries` keep the band exactly as typed (§4.5)."""
    # no suffix: lane 1's strongest band is the second one (9.0), not the first (3.0)
    strongest = densitometry.build_curve(_curve_dens([[3.0, 9.0], [2.0]]),
                                        [(1, None, 40), (2, None, 20)])
    assert [p["entries"] for p in strongest["points"]] == [[[2, None]], [[1, None]]],         strongest["points"]
    assert abs(strongest["points"][0]["volume"] - 2.0) < 1e-9, strongest["points"][0]
    assert abs(strongest["points"][1]["volume"] - 9.0) < 1e-9, strongest["points"][1]
    # :1 / :2 pick the top / bottom band of one lane
    top = densitometry.build_curve(_curve_dens([[3.0, 9.0], [12.0]]),
                                   [(1, 1, 20), (2, None, 40)])
    assert top["points"][0]["entries"] == [[1, 1]], top["points"][0]["entries"]
    assert abs(top["points"][0]["volume"] - 3.0) < 1e-9, top["points"][0]
    bottom = densitometry.build_curve(_curve_dens([[1.0, 9.0], [12.0]]),
                                     [(1, 2, 20), (2, None, 40)])
    assert bottom["points"][0]["entries"] == [[1, 2]], bottom["points"][0]["entries"]
    assert abs(bottom["points"][0]["volume"] - 9.0) < 1e-9, bottom["points"][0]
    # :* reads the whole-lane total (5 + 7 = 12), not one band
    whole = densitometry.build_curve(_curve_dens([[5.0, 7.0], [4.0]]),
                                     [(1, "*", 40), (2, "*", 20)])
    assert [p["entries"] for p in whole["points"]] == [[[2, "*"]], [[1, "*"]]], whole["points"]
    assert abs(whole["points"][0]["volume"] - 4.0) < 1e-9, whole["points"][0]
    assert abs(whole["points"][1]["volume"] - 12.0) < 1e-9, whole["points"][1]
    # a band-free lane has no strongest band; `:*` still reads its (zero) total
    band_free = _curve_dens([[2.0], []])
    try:
        densitometry.build_curve(band_free, [(1, None, 40), (2, None, 20)])
    except RuntimeError as e:
        assert "没有带" in str(e) and "*=质量" in str(e) and "\n" not in str(e), str(e)
    else:
        raise AssertionError("没有带的泳道不该给出 strongest")
    zero = densitometry.build_curve(band_free, [(1, None, 40), (2, "*", 20)])
    assert abs(zero["points"][0]["volume"] - 0.0) < 1e-9, zero["points"][0]
    try:
        densitometry.build_curve(band_free, [(1, None, 40), (2, "*", 20)], fit="power")
    except RuntimeError as e:
        assert "power 拟合要求面积 > 0" in str(e) and "\n" not in str(e), str(e)
    else:
        raise AssertionError("power 在面积 0 处该被拒绝")
    return True


def check_curve_drop():
    """`--drop` keeps a point with `used: false` and out of the fit / range / residuals / notes
    (§4.5); a replicate point drops as a whole when any of its lanes is dropped."""
    sixteen = _curve([2.5, 5.0, 9.8, 10.2, 15.0, 30.0], [22, 42, 82, 82, 122, 242], drop=[6])
    assert sixteen["source"]["drop"] == [6], sixteen["source"]
    assert sixteen["fit_params"]["n"] == 4 and sixteen["fit_params"]["slope"] == 8.0
    assert sixteen["points"][-1]["used"] is False and sixteen["points"][-1]["n"] == 1
    assert [p["used"] for p in sixteen["points"]] == [True, True, True, True, False], sixteen["points"]
    assert sixteen["range"] == {"volume_min": 2.5, "volume_max": 15.0,
                                "mass_min": 22.0, "mass_max": 122.0}, sixteen["range"]
    assert [r["mass"] for r in sixteen["residuals"]] == [22.0, 42.0, 82.0, 122.0]
    assert sixteen["notes"] == [], "4 个使用点是够的，不该有成点数提示"
    # dropping 242 out of a 3-point curve leaves 2 points → the shape note appears
    small = _curve([2.5, 5.0, 10.0], [22, 42, 122], drop=[3])
    assert small["fit_params"]["n"] == 2 and small["notes"] == ["只有 2 个标准点，r² 不足以说明形状"]
    # one lane of a replicate pair drops the whole point (its mean area is no longer usable)
    pair = _curve([2.0, 2.5, 5.0, 8.0], [20, 20, 40, 60], drop=[2])
    assert pair["points"][0]["used"] is False and pair["points"][0]["n"] == 2, pair["points"][0]
    assert pair["points"][0]["lanes"] == [1, 2] and pair["fit_params"]["n"] == 2
    # duplicate --drop lanes collapse; the source list is sorted and 1-based
    twice = _curve([2.5, 5.0, 10.0, 15.0], [22, 42, 82, 122], drop=[1, 4, 1])
    assert twice["source"]["drop"] == [1, 4], twice["source"]
    assert twice["fit_params"]["n"] == 2, twice["fit_params"]
    # a drop lane that is not a standard lane is an error, as is dropping every point
    for bad, want in (([4], "不在 --std 里"), ([1, 2, 3], "所有标准点"),
                      ([0], "≥ 1"), (["x"], "不是泳道号")):
        try:
            _curve([2.5, 5.0, 10.0], [22, 42, 82], drop=bad)
        except RuntimeError as e:
            assert want in str(e) and "\n" not in str(e), (bad, str(e))
        else:
            raise AssertionError("--drop %r 该被拒绝" % (bad,))
    return True


def check_curve_notes():
    """The three §4.5 notes, in order, each on its own fixture: <3 used points, the saturated top
    standard (linear only, rel_pct < −10), and a replicate CV > 15%."""
    assert _curve([2.5, 5.0, 10.0, 15.0], [22, 42, 82, 122])["notes"] == []
    saturated = _curve([2.0, 4.0, 6.0, 8.0], [30, 40, 50, 95])
    assert saturated["notes"] == ["最高标准点比线性低 11%，可能已饱和；试 --fit power 或 --drop 它"], \
        saturated["notes"]
    assert saturated["residuals"][-1]["rel_pct"] < -10.0, saturated["residuals"][-1]
    # the same fixture under the power fit has no saturation note (it is a linear-only hint)
    assert "饱和" not in "".join(_curve([2.0, 4.0, 6.0, 8.0], [30, 40, 50, 95], fit="power")["notes"])
    # a top point right at the edge (rel_pct == −10) does not trigger the note
    edge = _curve([2.0, 4.0, 6.0, 8.0], [20, 40, 60, 80])
    assert edge["notes"] == [], edge["notes"]
    noisy = _curve([2.0, 2.5, 5.0, 8.0], [20, 20, 40, 60])
    assert noisy["notes"] == ["泳道 1/2 的重复 CV 16% > 15%"], noisy["notes"]
    assert noisy["points"][0]["cv_pct"] == 15.71, noisy["points"][0]
    # <3 used points comes first when both hints apply
    combined = _curve([2.0, 2.5, 5.0], [20, 20, 30])
    assert combined["notes"] == ["只有 2 个标准点，r² 不足以说明形状",
                                 "泳道 1/2 的重复 CV 16% > 15%"], combined["notes"]
    # a dropped noisy point says nothing: the note is about used points only
    silent = _curve([2.0, 2.5, 4.0, 6.0, 8.0], [10, 10, 20, 30, 40], drop=[1])
    assert silent["points"][0]["used"] is False and silent["points"][0]["n"] == 2
    assert abs(silent["points"][0]["cv_pct"] - 15.71) < 0.01, silent["points"][0]
    assert silent["points"][0]["cv_pct"] > 15.0, "这组重复该有 CV 提示，但它是弃用的"
    assert silent["notes"] == [], silent["notes"]
    return True


# ---------------------------------------------------------------- 14) curve: refusals (§4.5)

def check_curve_refusals():
    """Every §4.5 refusal is one Chinese RuntimeError line; bad parameters are ValueError and
    non-monotonic fits start with 标准点不单调 plus the plot-bands hint."""
    dens = _curve_dens([[2.5], [5.0], [10.0], [15.0]], marker_lanes=(4,))
    std = [(1, None, 22), (2, None, 42), (3, None, 82)]
    # ValueError: the enum-like arguments (§4.1 style)
    for kwargs, want in (({"fit": "cubic"}, "--fit"), ({"baseline": "local"}, "--baseline"),
                         ({"unit": " "}, "--unit")):
        try:
            densitometry.build_curve(dens, std, **kwargs)
        except ValueError as e:
            assert want in str(e) and "\n" not in str(e), (kwargs, str(e))
        else:
            raise AssertionError("%r 该被拒绝" % (kwargs,))
    cases = (
        (dens, [(4, None, 22), (1, None, 42)], "marker 泳道"),
        (dens, [(1, None, 22), (9, None, 42)], "不在 dens 里"),
        (dens, [(1, 2, 22), (2, None, 42)], "只有 1 个带"),
        (dens, [(1, None, 22), (2, None, 42), (3, None, 82), (3, None, 82)], "出现了两次"),
        (dens, [(1, None, 0), (2, None, 42)], "正数"),
        (dens, [(1, None, 22)], "至少需要 2 个"),
        (_curve_dens([[2.0], [2.0], [2.0]]), [(1, None, 22), (2, None, 42), (3, None, 82)],
         "面积不同"),
        (_curve_dens([[2.0], [4.0], [6.0]]), [(1, None, 30), (2, None, 20), (3, None, 10)],
         "标准点不单调"),
    )
    for bad_dens, bad_std, want in cases:
        try:
            densitometry.build_curve(bad_dens, bad_std)
        except RuntimeError as e:
            msg = str(e)
            assert want in msg and "\n" not in msg, (want, msg)
        else:
            raise AssertionError("%r 该被拒绝" % (want,))
    # the monotonic messages name the fit, the offending parameter and the plot-bands hint
    for fit, what in (("linear", "斜率"), ("power", "指数")):
        try:
            densitometry.build_curve(_curve_dens([[2.0], [4.0], [6.0]]),
                                     [(1, None, 30), (2, None, 20), (3, None, 10)], fit=fit)
        except RuntimeError as e:
            msg = str(e)
            assert msg.startswith("标准点不单调") and "plot-bands" in msg and what in msg, msg
            assert "\n" not in msg, msg
        else:
            raise AssertionError("递减的标准点（%s）该被拒绝" % fit)
    # a std entry that is not (lane, band, mass), or no std at all
    for bad, want in (([], "空的"), ("2=20", "空的"), ([(1, 20)], "不是"), ([("x", None, 20)], "不是整数"),
                      ([(0, None, 20)], "≥ 1")):
        try:
            densitometry.build_curve(dens, bad)
        except RuntimeError as e:
            assert want in str(e) and "\n" not in str(e), (bad, str(e))
        else:
            raise AssertionError("std %r 该被拒绝" % (bad,))
    # a dens dict without the baseline column (or with a non-finite one) is refused, not guessed
    thin = _curve_dens([[2.5], [5.0]])
    del thin["lanes"][0]["bands"][0]["area_flank"]
    try:
        densitometry.build_curve(thin, [(1, None, 22), (2, None, 42)])
    except RuntimeError as e:
        assert "area_flank" in str(e) and "\n" not in str(e), str(e)
    else:
        raise AssertionError("缺 area_flank 的带该被拒绝")
    broken = _curve_dens([[2.5], [5.0]])
    broken["lanes"][1]["bands"][0]["area_flank"] = float("inf")
    try:
        densitometry.build_curve(broken, [(1, None, 22), (2, None, 42)])
    except RuntimeError as e:
        assert "有限数" in str(e) and "\n" not in str(e), str(e)
    else:
        raise AssertionError("inf 面积该被拒绝")
    for bad_dens, want in (({}, "lanes"), ({"lanes": []}, "lanes"),
                           ({"lanes": [{"lane": 1}, {"lane": 1}]}, "两次"),
                           ({"lanes": [{"label": "x"}]}, "整数 lane")):
        try:
            densitometry.build_curve(bad_dens, [(1, None, 22), (2, None, 42)])
        except RuntimeError as e:
            assert want in str(e) and "\n" not in str(e), (bad_dens, str(e))
        else:
            raise AssertionError("dens %r 该被拒绝" % (bad_dens,))
    return True


# ---------------------------------------------------------------- 15) curve JSON round trip

def check_curve_json_io():
    """`curve` twice → byte-identical JSON (no timestamps, §7.11) and a `load_json` round trip that
    keeps every field of the §4.5 document (§7.10)."""
    dens = _curve_dens([[2.5], [5.0], [9.8], [10.2], [15.0]])
    std = [(1, None, 22), (2, None, 42), (3, None, 82), (4, None, 82), (5, None, 122)]
    with tempfile.TemporaryDirectory() as td:
        first = densitometry.build_curve(dens, std, drop=[5], dens_path="fig.dens.json",
                                         std_text="1=22,2=42,3=82,4=82,5=122")
        path1 = os.path.join(td, "a.curve.json")
        densitometry.write_json(first, path1)
        second = densitometry.build_curve(dens, std, drop=[5], dens_path="fig.dens.json",
                                          std_text="1=22,2=42,3=82,4=82,5=122")
        path2 = os.path.join(td, "b.curve.json")
        densitometry.write_json(second, path2)
        with open(path1, "rb") as f1, open(path2, "rb") as f2:
            assert f1.read() == f2.read(), "curve 两次的 JSON 不一致"
        with open(path1, encoding="utf-8") as f:
            raw = f.read()
        assert raw.endswith("\n") and "\\u" not in raw, "write_json 的 UTF-8 / ensure_ascii 变了"
        back = densitometry.load_json(path1, "ep-craft-densitometry-curve")
        assert back == json.loads(raw), "round trip 丢了字段"
        assert back["kind"] == "ep-craft-densitometry-curve" and back["version"] == 1
        # 数字仍是数字，None 仍是 null（不是字符串），盘点过的点保留 used: false
        assert isinstance(back["fit_params"]["slope"], float)
        assert back["points"][0]["sd"] is None and back["points"][0]["cv_pct"] is None
        assert back["points"][-1]["used"] is False and back["source"]["drop"] == [5]
        # kind 不符的 curve.json 被 load_json 拒绝（写错了文件的常见情形）
        densitometry.write_json(dict(first, kind="ep-craft-densitometry"), path2)
        try:
            densitometry.load_json(path2, "ep-craft-densitometry-curve")
        except RuntimeError as e:
            assert "kind" in str(e) and "\n" not in str(e), str(e)
        else:
            raise AssertionError("kind 不符的 curve.json 该被拒绝")
    return True


# ---------------------------------------------------------------- 15) curve table (§5)

def check_format_curve():
    """The §5 table: header, aligned rows (heaviest first), `（n=1）` without CV, `（已弃用）`
    instead of a residual, power equations, notes as `  注：`, and never `None` / `µg`."""
    curve = _curve([2.5, 5.0, 9.8, 10.2, 15.0, 30.0], [22, 42, 82, 82, 122, 242])
    text = densitometry.format_curve(curve)
    lines = text.split("\n")
    assert lines[0] == ("[标曲] 基线 flank，拟合 linear（ug）：mass = 2 + 8·area，r² 1.0000，5 点"
                        "（1 个重复）"), lines[0]
    assert len(lines) == 1 + len(curve["points"]), lines
    wanted = ["  242 ug   面积  30（n=1）           残差 0.0%",
              "   82 ug   面积  10（n=2，CV 2.8%）  残差 0.0%"]
    for line, expect in zip([lines[1], lines[3]], wanted):
        assert line == expect, (line, expect)
    assert "None" not in text and "µ" not in text and "nan" not in text
    # one line per point, columns aligned (the residual/已弃用 column starts at a fixed width)
    heads, cols = [], []
    for line in lines[1:]:
        if "（已弃用）" in line:
            cols.append(densitometry._dwidth(line[:line.index("（已弃用）")]))
        else:
            cols.append(densitometry._dwidth(line[:line.index("残差")]))
        heads.append(densitometry._dwidth(line[:line.index("面积")]))
    assert len(set(cols)) == 1, "残差列没有对齐：%r" % (cols,)
    assert len(set(heads)) == 1, "面积列没有对齐：%r" % (heads,)
    # dropped point: no residual, `（已弃用）` in its place, and the header counts used points
    dropped = densitometry.format_curve(_curve([2.5, 5.0, 9.8, 10.2, 15.0, 30.0],
                                               [22, 42, 82, 82, 122, 242], drop=[6]))
    dlines = dropped.split("\n")
    assert "4 点" in dlines[0] and "5 点" not in dlines[0], dlines[0]
    assert "（已弃用）" in dlines[1] and "残差" not in dlines[1], dlines[1]
    assert "242 ug" in dlines[1]
    # power: both parameters, 2 dp like the §5 sample; the unit comes from the curve
    power = _curve([2.0, 4.0, 6.0, 8.0], [0.2 * v ** 1.1 for v in (2.0, 4.0, 6.0, 8.0)], fit="power")
    ptext = densitometry.format_curve(power)
    assert ptext.split("\n")[0].startswith(
        "[标曲] 基线 flank，拟合 power（ug）：mass = 0.2·area^1.1，r² 1.0000，4 点"), ptext.split("\n")[0]
    # notes are printed with the 注 prefix, one line each, in curve order
    noted = _curve([2.0, 2.5, 5.0], [20, 20, 30])
    ntext = densitometry.format_curve(noted)
    assert "  注：只有 2 个标准点，r² 不足以说明形状" in ntext
    assert "  注：泳道 1/2 的重复 CV 16% > 15%" in ntext
    assert ntext.split("\n")[-2].startswith("  注：") and ntext.split("\n")[-1].startswith("  注：")
    # a hand-built curve with a 4-dp param still prints without trailing zeros
    tidy = dict(curve, fit_params={"slope": 6.0200, "intercept": 0.4000, "r2": 0.9985, "n": 5,
                                   "space": "linear"})
    assert densitometry.format_curve(tidy).split("\n")[0].endswith(
        "mass = 0.4 + 6.02·area，r² 0.9985，5 点（1 个重复）"), densitometry.format_curve(tidy)
    return True


# ---------------------------------------------------------------- entry point

def run():
    checks = [check_single_gaussian, check_sloping_background, check_two_gaussians,
              check_band_free_lane, check_saturation_flags, check_profile_ends, check_parse_lanes,
              check_lane_selection, check_schema, check_json_io, check_purity,
              check_curve_parse_std, check_curve_schema_and_fit, check_fit_statistics_linear,
              check_fit_statistics_power, check_fit_statistics_n2, check_fit_statistics_json,
              check_curve_bands,
              check_curve_drop, check_curve_notes, check_curve_refusals, check_curve_json_io,
              check_format_curve]
    for check in checks:
        check()
    print("selftest_densitometry %d/%d 通过" % (len(checks), len(checks)))
    return len(checks)


if __name__ == "__main__":
    run()
