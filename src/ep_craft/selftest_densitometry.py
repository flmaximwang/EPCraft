#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_densitometry — analytic checks for the densitometry math (spec §7 items 1-4, 6, 7, 10, 11).

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

from . import densitometry
from . import profiles


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


# ---------------------------------------------------------------- entry point

def run():
    checks = [check_single_gaussian, check_sloping_background, check_two_gaussians,
              check_band_free_lane, check_saturation_flags, check_profile_ends, check_parse_lanes,
              check_lane_selection, check_schema, check_json_io, check_purity]
    for check in checks:
        check()
    print("selftest_densitometry %d/%d 通过" % (len(checks), len(checks)))
    return len(checks)


if __name__ == "__main__":
    run()
