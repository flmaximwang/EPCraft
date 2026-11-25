"""
test_quantify_acceptance — acceptance suite for ep-srz series concentration quantification.

Covers:
  * Parsers: `parse_sample` (`NAME=LANE[:BAND][@RATIO],...`, mixed-ratio & duplicate-lane refusals),
    `parse_sample_band` (`LO,HI` kDa window), `parse_drop_sample`.
  * Direct concentration math: `conc_i = C_equiv_i / ratio_i`, `conc_sd_i = equiv_sd_i / ratio_i`.
  * Series merge modes:
      - `single` (n_used == 1): point estimate only (`conc_sd = None`, `sem = None`), plus `common_pct`.
      - `mean`   (n_used >= 2, same ratio): normalized inverse-variance weighted mean, SD, SEM.
      - `ratio`  (n_used >= 2, >= 2 distinct ratios): through-origin WLS slope + `k >= 3` intercept
        linearity check, with fallback to `mean` when intercept is significantly non-zero.
  * Refusals & guards: out-of-range / non-positive active members withhold merge until `--drop-sample`,
    standard lanes cannot be declared as samples, unknown `--drop-sample` refused, missing scatter
    stats emit `NO_SCATTER_NOTE`, `--load-cv` folds into `common_pct` in quadrature.
  * Output formatters & CLI: `format_quant`, `format_quant_report` (`ep-craft-densitometry-report`),
    `epcraft dens quantify` (`--sample`, `--sample-band`, `--drop-sample`, `--load-cv`, `--report`),
    and `plotting.plot_curve` compatibility with `quant["samples"]`.
"""

import io
import json
import math
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from epcraft import cli as gel_photo
from epcraft import densitometry, plotting


def _band(index, y_cm, mw_kda, area_flank, area_global, *, strongest=True):
    return {
        "index": index,
        "y_cm": y_cm,
        "mw_kda": mw_kda,
        "area_flank": area_flank,
        "area_global": area_global,
        "strongest": strongest,
    }


def _dens():
    """Synthetic dens.json: lanes 1-4 are standards, 5-15 are samples."""
    return {
        "kind": densitometry.KIND,
        "version": densitometry.VERSION,
        "y_cm": [0.0, 1.0, 2.0, 3.0],
        "lanes": [
            # Standards: 0.25, 0.50, 1.00, 2.00 mg/mL -> areas ~10, 20, 40, 80 (C ~ 0.025 * area)
            {"lane": 1, "label": "S1", "bands": [_band(1, 1.5, 66.0, 10.2, 9.8)]},
            {"lane": 2, "label": "S2", "bands": [_band(1, 1.5, 66.0, 19.8, 19.2)]},
            {"lane": 3, "label": "S3", "bands": [_band(1, 1.5, 66.0, 40.3, 39.0)]},
            {"lane": 4, "label": "S4", "bands": [_band(1, 1.5, 66.0, 79.7, 76.0)]},
            # Single sample (lane 5)
            {"lane": 5, "label": "Single", "bands": [_band(1, 1.5, 66.0, 30.0, 28.5)]},
            # Replicate pair (lanes 6, 7, default ratio=1)
            {"lane": 6, "label": "RepA1", "bands": [_band(1, 1.5, 66.0, 38.0, 36.0)]},
            {"lane": 7, "label": "RepA2", "bands": [_band(1, 1.5, 66.0, 42.0, 40.0)]},
            # Linear dilution series (lanes 8, 9, 10 at ratios 1, 2, 3 -> equiv ~ 0.6, 1.2, 1.8 -> conc ~ 0.6)
            {"lane": 8, "label": "Dil1", "bands": [_band(1, 1.5, 66.0, 24.0, 23.0)]},
            {"lane": 9, "label": "Dil2", "bands": [_band(1, 1.5, 66.0, 48.0, 46.0)]},
            {"lane": 10, "label": "Dil3", "bands": [_band(1, 1.5, 66.0, 72.0, 69.0)]},
            # Multi-band lane (lane 11): band 1 at 120 kDa, band 2 at 66.5 kDa
            {"lane": 11, "label": "Multi", "bands": [
                _band(1, 0.8, 120.0, 50.0, 48.0, strongest=True),
                _band(2, 1.5, 66.5, 32.0, 30.0, strongest=False),
            ]},
            # Out-of-range lane (lane 12: area 120 > vmax 79.7)
            {"lane": 12, "label": "OOR", "bands": [_band(1, 1.5, 66.0, 120.0, 115.0)]},
            # Non-linear dilution series (lanes 13, 14, 15 at ratios 1, 2, 4 -> equiv ~ 0.6, 0.8, 1.0 -> big intercept)
            {"lane": 13, "label": "NL1", "bands": [_band(1, 1.5, 66.0, 24.0, 23.0)]},
            {"lane": 14, "label": "NL2", "bands": [_band(1, 1.5, 66.0, 32.0, 30.5)]},
            {"lane": 15, "label": "NL4", "bands": [_band(1, 1.5, 66.0, 40.0, 38.0)]},
        ],
    }


def _curve(dens, *, fit="linear", baseline="flank"):
    std = densitometry.parse_std("1=0.25,2=0.5,3=1.0,4=2.0")
    return densitometry.build_curve(dens, std, unit="mg/mL", baseline=baseline, fit=fit)


def _assert_raises_msg(fn, needle, desc):
    try:
        fn()
    except (RuntimeError, ValueError) as exc:
        if needle and needle not in str(exc):
            raise AssertionError("%s: expected %r in %r" % (desc, needle, str(exc)))
        return
    raise AssertionError("%s: expected error containing %r, got no error" % (desc, needle))


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = gel_photo.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def check_parsers():
    s = densitometry.parse_sample("SampleA=8:2@1, 9:2@2, 10:2@3")
    assert s["name"] == "SampleA"
    assert s["members"] == [
        {"lane": 8, "band": 2, "ratio": 1.0},
        {"lane": 9, "band": 2, "ratio": 2.0},
        {"lane": 10, "band": 2, "ratio": 3.0},
    ]

    s2 = densitometry.parse_sample("Rep=6,7")
    assert s2["members"] == [
        {"lane": 6, "band": None, "ratio": 1.0},
        {"lane": 7, "band": None, "ratio": 1.0},
    ]

    _assert_raises_msg(lambda: densitometry.parse_sample("bad"), "=", "missing =")
    _assert_raises_msg(lambda: densitometry.parse_sample("=6,7"), "缺少样品名", "empty name")
    _assert_raises_msg(lambda: densitometry.parse_sample("A="), "没有成员泳道", "empty body")
    _assert_raises_msg(lambda: densitometry.parse_sample("A=6,6"), "出现了两次", "dup lane")
    _assert_raises_msg(lambda: densitometry.parse_sample("A=6@1,7"), "混用", "mixed ratio")
    _assert_raises_msg(lambda: densitometry.parse_sample("A=6@0"), "正有限数", "zero ratio")
    _assert_raises_msg(lambda: densitometry.parse_sample("A=6@-2"), "正有限数", "neg ratio")

    assert densitometry.parse_sample_band("55,80") == (55.0, 80.0)
    assert densitometry.parse_sample_band(None) is None
    _assert_raises_msg(lambda: densitometry.parse_sample_band("80,55"), "范围无效", "inverted band")
    _assert_raises_msg(lambda: densitometry.parse_sample_band("0,55"), "范围无效", "zero lo")

    assert densitometry.parse_drop_sample(["12", "13, 14"]) == {12, 13, 14}
    _assert_raises_msg(lambda: densitometry.parse_drop_sample(["0"]), "正整数", "zero drop")
    return 1


def check_single_and_mean_and_ratio():
    dens = _dens()
    curve = _curve(dens)
    samples = [
        densitometry.parse_sample("Single=5"),
        densitometry.parse_sample("Rep=6,7"),
        densitometry.parse_sample("Dil=8@1,9@2,10@3"),
    ]
    q = densitometry.quantify(dens, curve, samples, load_cv=5.0)
    assert q["kind"] == densitometry.QUANT_KIND
    assert q["unit"] == "mg/mL"
    assert q["load_cv_pct"] == 5.0

    by_name = {s["name"]: s for s in q["samples"]}

    # 1. Single lane: no ± (conc_sd/sem are None), common_pct present
    s_est = by_name["Single"]["est"]
    assert s_est["method"] == "single"
    assert s_est["conc_sd"] is None and s_est["sem"] is None
    assert s_est["conc"] is not None and 0.70 < s_est["conc"] < 0.80
    assert s_est["common_pct"] is not None and s_est["common_pct"] >= 5.0

    # 2. Replicate pair: method == "mean", conc ~ 1.0, sem > 0
    r_est = by_name["Rep"]["est"]
    assert r_est["method"] == "mean"
    assert r_est["n"] == 2 and r_est["n_used"] == 2
    assert 0.95 < r_est["conc"] < 1.05
    assert r_est["sem"] > 0 and r_est["conc_sd"] == r_est["sem"]
    assert r_est["ratio"] is None

    # 3. Linear dilution series: method == "ratio", conc ~ 0.60, r2 > 0.999
    d_est = by_name["Dil"]["est"]
    assert d_est["method"] == "ratio"
    assert d_est["n"] == 3 and d_est["n_used"] == 3
    assert 0.58 < d_est["conc"] < 0.62
    assert d_est["ratio"] is not None
    assert d_est["ratio"]["r2"] > 0.999
    assert d_est["ratio"]["nonlinear"] is False
    assert d_est["ratio"]["intercept"] is not None
    assert d_est["ratio"]["intercept_sd"] is not None
    return 1


def check_nonlinear_ratio_fallback():
    dens = _dens()
    curve = _curve(dens)
    samples = [densitometry.parse_sample("NL=13@1,14@2,15@4")]
    q = densitometry.quantify(dens, curve, samples)
    est = q["samples"][0]["est"]
    assert est["ratio"] is not None
    assert est["ratio"]["nonlinear"] is True, "expected non-linear intercept flag: %r" % (est,)
    assert est["method"] == "mean", "expected fallback to mean when non-linear"
    assert any("不线性" in n for n in est["notes"])
    return 1


def check_sample_band_and_drop_sample():
    dens = _dens()
    curve = _curve(dens)

    # Multi-band lane 11: select 66.5 kDa band via --sample-band 55,80
    q = densitometry.quantify(
        dens, curve,
        [densitometry.parse_sample("Target=11")],
        sample_band=(55.0, 80.0),
    )
    m = q["samples"][0]["members"][0]
    assert m["band"] == 2 and abs(m["area"] - 32.0) < 1e-6

    # Window matching 0 bands refuses
    _assert_raises_msg(
        lambda: densitometry.quantify(dens, curve, [densitometry.parse_sample("Target=11")], sample_band=(10.0, 20.0)),
        "没有匹配条带", "no band in kDa window"
    )
    # Window matching 2 bands refuses
    _assert_raises_msg(
        lambda: densitometry.quantify(dens, curve, [densitometry.parse_sample("Target=11")], sample_band=(50.0, 150.0)),
        "有 2 条带", "multiple bands in kDa window"
    )

    # OOR lane 12 withholds merge unless dropped
    q_oor = densitometry.quantify(dens, curve, [densitometry.parse_sample("WithOOR=6,7,12")])
    assert q_oor["samples"][0]["est"]["conc"] is None
    assert q_oor["samples"][0]["est"]["method"] is None

    q_rescued = densitometry.quantify(
        dens, curve,
        [densitometry.parse_sample("WithOOR=6,7,12")],
        drop_sample={12},
    )
    est = q_rescued["samples"][0]["est"]
    assert est["method"] == "mean"
    assert est["n"] == 3 and est["n_used"] == 2
    assert est["conc"] is not None

    # Refusals: standard lane as sample, unknown drop_sample
    _assert_raises_msg(
        lambda: densitometry.quantify(dens, curve, [densitometry.parse_sample("Bad=1")]),
        "标曲泳道", "standard lane in --sample"
    )
    _assert_raises_msg(
        lambda: densitometry.quantify(dens, curve, [densitometry.parse_sample("Rep=6,7")], drop_sample={99}),
        "不在任何 --sample 中", "unknown --drop-sample"
    )
    return 1


def check_no_scatter_and_reports_and_cli():
    dens = _dens()
    curve = _curve(dens)
    bare_curve = dict(curve)
    bare_curve["fit_params"] = {
        k: v for k, v in curve["fit_params"].items()
        if k not in ("residual_sd", "sxx", "mean_x", "slope_sd", "intercept_sd", "cov_slope_intercept")
    }
    q_bare = densitometry.quantify(dens, bare_curve, [densitometry.parse_sample("S=5")])
    assert densitometry.NO_SCATTER_NOTE in q_bare["notes"]
    assert q_bare["samples"][0]["members"][0]["conc_sd"] is None

    # CLI end-to-end with --report and plot_curve compatibility
    with tempfile.TemporaryDirectory() as td:
        dens_path = os.path.join(td, "fig.dens.json")
        curve_path = os.path.join(td, "fig.curve.json")
        densitometry.write_json(dens, dens_path)
        densitometry.write_json(curve, curve_path)

        rc, out, err = _run_cli([
            "dens", "quantify",
            "--dens", dens_path,
            "--curve", curve_path,
            "--sample", "Single=5",
            "--sample", "Dil=8@1,9@2,10@3",
            "--load-cv", "4.0",
            "--report",
        ])
        assert rc == 0, "CLI failed rc=%r stderr=%r" % (rc, err)
        quant_path = os.path.join(td, "fig.quant.json")
        report_path = os.path.join(td, "fig.report.md")
        assert os.path.isfile(quant_path), "missing default quant.json"
        assert os.path.isfile(report_path), "missing default report.md"

        with open(quant_path, encoding="utf-8") as fh:
            q_disk = json.load(fh)
        with open(report_path, encoding="utf-8") as fh:
            md_disk = fh.read()
        assert densitometry.REPORT_KIND in md_disk
        assert "Single" in md_disk and "Dil" in md_disk
        assert "ratio" in out

        # Verify plot_curve accepts the new quant.json format
        import importlib.util
        if importlib.util.find_spec("matplotlib") is not None:
            png_path = os.path.join(td, "fig.curve.png")
            w, h = plotting.plot_curve(curve, png_path, quant=q_disk)
            assert w == 1020 and h > 0 and os.path.isfile(png_path)
    return 1


def run():
    checks = [
        check_parsers,
        check_single_and_mean_and_ratio,
        check_nonlinear_ratio_fallback,
        check_sample_band_and_drop_sample,
        check_no_scatter_and_reports_and_cli,
    ]
    passed = 0
    for fn in checks:
        passed += fn()
    print("test_quantify_acceptance %d/%d 通过" % (passed, len(checks)))
    return passed


if __name__ == "__main__":
    run()
