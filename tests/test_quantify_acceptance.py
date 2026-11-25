#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_quantify_acceptance — independent acceptance suite for ep-bqv.5 (`quantify`).

Ticket: ep-bqv.5 "quantify: area to protein mass with out-of-range refusal".
Contract: docs/specs/ep-craft-densitometry.md §2.7, §2.8, §3.3, §4.6, §5, §7 items 9-11, plus the
ticket's acceptance criteria.  This suite is deliberately SEPARATE from the implementation
change's own selftests: it uses only the public API (`densitometry.quantify`,
`densitometry.format_quant`, `densitometry.write_json`, `densitometry.load_json`), the public CLI
(`epcraft densitometry quantify` / `dens quantify`), and synthetic fixtures built in memory.  It
never reads a user file and never calls ImageMagick.

What it covers
--------------
* JSON shape (§4.6): exact top-level / lane / band key sets, version / kind / unit / baseline /
  fit copied from the curve, `source` recording the paths exactly as typed (ruling R6), and the
  rounding rules of ruling R2 (areas / masses / conc 3 dp, percentages 2 dp, integers stay int).
* Standard lanes are left out of the table — including a standard lane dropped with
  `used: false` and one selected with the `:*` whole-lane selector — and the note counts them.
* In-range masses: an analytically simple IDENTITY fit (linear `slope = 1, intercept = 0`; power
  `scale = 1, exponent = 1`) makes `mass == area`, which keeps the shape / percentage / boundary
  checks easy to read; the end-to-end chain `measure → build_curve → quantify` uses the same
  identity on a real synthetic report, where each standard's mass is its own measured area.
* The direction the fit is evaluated in (lead ruling — see below): standards built so the truth
  is `mass = 2 + 8·area` (linear) or `mass = 2·area^1.5` (power) must be recovered within 1 %,
  on the `flank` column and, for the linear truth, on a `baseline = global` curve as well.
* An area of 0 under a power fit is out of range (`range.volume_min > 0`), gets `mass: null`
  and takes its lane's total down with it — no `0^exponent` crash and no invented number.
* Inclusive boundaries: an area exactly on `range.volume_min` / `range.volume_max` is in range;
  areas a hair outside are out of range.
* Out of range (§2.8): `mass: null` + `out_of_range: true` for the band, `mass_total: null`,
  `conc: null`, `frac_pct_among_lanes: null`, `rel_pct: null`, the band's 1-based index in
  `out_of_range_bands`, one note per band naming the band with the "超出标曲范围" sentence, and
  a terminal line showing `—`, the range comparison and no total.  A lane with one out-of-range
  band keeps the in-range band's mass but has a null total.
* Totals / percentages / loads: `mass_total` = Σ band masses, `rel_pct` per lane sums to 100,
  `frac_pct_among_lanes` sums to 100 over the known totals (within 0.11 once each share is
  rounded to 2 dp, which is the rounding the JSON is required to carry), `overall_mass` = Σ
  known totals, `conc` = `mass_total / load_ul`, null without a load.
* Per-baseline area: `area` comes from the curve's own `baseline` column (`area_flank` vs
  `area_global`).
* Non-positive mass (lead ruling): a fit that crosses zero inside its own range and yields a
  `mass <= 0` for an in-range band gets `mass: null` + `rel_pct: null` but `out_of_range: false`;
  the lane's `mass_total` / `conc` / `frac_pct_among_lanes` go null, `overall_mass` leaves it
  out, exactly one note names the lane and the band, and no JSON key is invented for the state.
* Terminal precision (lead ruling): `mass` and `conc` print with up to 3 decimals, trailing zeros
  stripped, at least one decimal kept — 0.522 and 0.075 stay distinguishable, 5.0 keeps its dot.
* The out-of-range lane line (lead ruling) prints the area and the violated bound with 3
  significant digits, so area 4.495 against a maximum of 4.484 cannot collapse into `4.5 > 4.5`.
* CLI surface (§3.3): `quantify` is listed in `dens --help` and takes `--dens` / `--curve` /
  `--out` / `--load`; the table has exactly the sample lanes (`泳道` lines, standards absent),
  the header names baseline / fit / unit / range / point count / r² / skipped count, and two
  identical runs write byte-identical JSON.
* JSON idempotence (§7.10-11): `quantify` twice is identical, `write_json` twice is
  byte-identical, the file is UTF-8 without `\\u` escapes, `load_json` round-trips every field.
* Default output path (ruling R12) through the CLI: `fig.curve.json` → `fig.quant.json` (never
  `fig.curve.quant.json`), the fallback `c.json` → `c.quant.json`, the case-insensitive mirror of
  `cmd_curve`; every refusal is one `[!] …` stderr line with exit 2 and nothing on stdout.
* The `[定量]` header (§5) — every part that `quant` can supply is required; the parts that only
  the curve knows are required whenever `format_quant` can be handed the curve (see the
  format-contract note below).

FORMAT-CONTRACT NOTE — WHERE THE HEADER'S NUMBERS COME FROM
-----------------------------------------------------------
§5's sample header reads `[定量] 基线 flank · linear · ug（标曲 5–20 ug，4 点，r² 0.9985）；
跳过 4 条标准道` — it asks for the standard mass range, the point count and r².  §4.6 lists the
quant.json keys exhaustively and none of them holds those three numbers, so they are not
recoverable from `quant` alone: an implementation must either take the curve as display metadata
(`format_quant(quant, *, curve=None)`) or leave them out.  `_formatted` therefore passes `curve=`
only when the signature accepts it, and reports whether it did; the baseline / fit / unit /
skipped-count parts are required unconditionally (they are all in `quant`), while the range /
count / r² parts are required only when the curve was available — and always required through
the CLI, which has both objects in hand.  This is about the printed header, not the JSON: the
JSON key list stays exactly §4.6's.

THE DIRECTION OF THE FIT — LEAD RULING (SETTLED)
-----------------------------------------------
§2.6, §4.5 and §7 items 8-9 define the model as `mass = f(area)` — the band area (the "volume"
column) is the predictor and `mass` the response: `mass = intercept + slope·volume`,
`mass = scale·volume^exponent`, stored as such in `curve.json`'s `fit_params`.  §4.6's phrase
"`mass` = inverse of the fit" and the ticket's `mass = (area − intercept)/slope` formula read
it the other way.  The lead ruled FORWARD: `quantify` evaluates the stored fit at the band's
area — `mass = intercept + slope·area`, `mass = scale·area^exponent` — the only reading that
makes §7 item 9's "within 1 % of the generated truth" reachable at all.  This suite asserts the
forward reading (`check_recovery_linear_nonidentity` / `check_recovery_power_nonidentity`, both
in `CHECKS`); the identity-fit checks are direction-neutral and were valid before the ruling too.

HOW THIS SUITE WAS VALIDATED — THREE IMPLEMENTATION STATES

1. No implementation (this branch before ep-bqv.5 lands): `run()` raises one line,
   `[!] densitometry.quantify 不存在——ep-bqv.5（quantify）还没落到这个分支上`, exit 1.  A
   placeholder that raises NotImplementedError for every in-range band is reported the same
   way, so a green run cannot come from a stub.
2. Commit ef6dc7a (the forward evaluation, CLI registered, before the three rulings):
   20 of the 23 checks pass; `check_nonpositive_mass_null`, `check_terminal_precision` and
   `check_out_of_range_three_sig` fail — exactly the behaviours the rulings require and that
   commit does not have yet (`mass: -84.0` for the crossing fit, `0.5 ug` for a 0.522 band,
   `面积 4.5 > 4.5`).
3. A /tmp scratch copy of ef6dc7a with the three rulings applied by hand: 23/23 pass.  Earlier,
   while the direction question was still open, the same kind of scratch copy (that draft plus a
   forward-reading `_mass_from_area`) was used to validate the suite before any of this landed.

A mutation run against the ruling-applied copy (20 one-line regressions aimed at the guarded
behaviours: the non-positive check, its effect on the total and on the note, the out-of-range
flag; mass printed at 1 dp and without a decimal; area and bound printed at 1 dp or collapsed
into the same number; exclusive range bounds; the baseline column ignored; the skip note dropped;
`conc` ignoring the load; forward→inverse for both fits; `ensure_ascii=True`; a header without
the range; the default output keeping `.curve`; a missing overwrite guard; `source` basenamed
instead of as-typed) catches all 20.  Nothing in this file was tuned to either implementation:
every assertion cites the spec or the ruling, and every expected number is computed from the
fixture.

Run: `PYTHONPATH=src python3 -m tests.test_quantify_acceptance`
(`run()` prints `test_quantify_acceptance N/N 通过` and returns the number of checks).
"""
import contextlib
import inspect
import io
import json
import math
import os
import re
import tempfile

from epcraft import densitometry
from epcraft import gel_photo
from . import test_densitometry

QUANT_KIND = "ep-craft-densitometry-quant"

# ep-c1i §4: quant.json carries the propagated uncertainty next to every point estimate.
TOP_KEYS = {"version", "kind", "unit", "baseline", "fit", "source", "lanes",
            "overall_mass", "notes", "uncertainty"}
SOURCE_KEYS = {"dens", "curve", "load"}
LANE_KEYS = {"lane", "label", "load_ul", "bands", "mass_total", "conc", "out_of_range",
             "out_of_range_bands", "frac_pct_among_lanes", "mass_total_sd", "conc_sd"}
BAND_KEYS = {"index", "y_cm", "mw_kda", "area", "mass", "out_of_range", "rel_pct",
             "mass_sd"}
UNCERTAINTY_KEYS = {"basis", "k", "unit"}
UNCERTAINTY_BASIS = "ols_prediction"

# ep-c1i §3, restated here so this suite can disagree with the densitometry suite on purpose.
LINEAR_FIT_KEYS = {"slope", "intercept", "r2", "n", "space",
                   "slope_sd", "intercept_sd", "cov_slope_intercept", "residual_sd",
                   "sxx", "mean_x"}
POWER_FIT_KEYS = {"exponent", "scale", "r2", "n", "space",
                  "exponent_sd", "scale_sd", "residual_sd", "sxx", "mean_x"}

# Half a unit in the third decimal: the tolerance a 3-dp stored number may differ by (ruling R2).
_TOL_3DP = 5.1e-4

# lanes 1-4 are the standards (4 is dropped), 6..13 are the samples; 13 has no bands at all
STANDARD_LANES = (1, 2, 3, 4)
SAMPLE_LANES = (6, 7, 9, 10, 11, 12, 13)
OOR_BELOW = (10, 1)                 # lane 10's band 1 is 20 % below volume_min (2.0)
OOR_ABOVE = (11, 1)                 # lane 11's band 1 is 20 % above volume_max (8.0)
MIXED_LANE = 12                     # band 1 out of range (1.6), band 2 in range (5.0)


# ---------------------------------------------------------------- fixtures

def _number(value):
    """True for a JSON number (an int or a float, never a bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _band(index, y_cm, mw_kda, area_flank, area_global=None, strongest=False):
    """One dens.json band object (spec §4.4) with both area columns."""
    if area_global is None:
        area_global = area_flank
    return {"index": index, "y_cm": y_cm, "mw_kda": mw_kda, "strongest": strongest,
            "area_flank": area_flank, "area_global": area_global}


def _lane(no, label, bands, marker=False, trimmed=False):
    """One dens.json lane object (spec §4.4) with consistent lane totals."""
    return {"lane": no, "label": label, "marker": marker, "trimmed": trimmed,
            "bands": list(bands),
            "area_flank": round(sum(b["area_flank"] for b in bands), 3),
            "area_global": round(sum(b["area_global"] for b in bands), 3)}


def _dens(lanes):
    """A dens.json dict (spec §4.4) for `quantify`; synthetic, built in memory."""
    return {
        "version": 1, "kind": "ep-craft-densitometry",
        "units": {"y": "cm from gel-face top", "gray": "0-255 (lower = darker)",
                  "ink": "baseline - gray (higher = more stain)", "area": "gray·cm"},
        "source": {"report": "", "gel": {}, "window": {}, "marker_lane": None},
        "params": {"flank_cm": 0.15, "min_ink": 0.5, "smooth_cm": 0.03,
                   "window": "walk outward while smoothed ink > min_ink",
                   "lanes": "all sample lanes, trimmed and marker excluded"},
        "lanes": list(lanes), "notes": [],
    }


def _identity_curve(dens, std, fit="linear", baseline="flank", drop=(), dens_path="",
                    std_text="", unit="ug"):
    """`build_curve` on an identity truth: every standard's mass equals its own area.

    The identity is what makes this suite neutral about the §4.6-vs-§2.6 direction conflict
    (linear: mass = area; power: 1.0·area^1.0 = area).  A non-identity fit here would be a
    fixture bug, so it is asserted loudly.
    """
    curve = densitometry.build_curve(dens, std, unit=unit, fit=fit, baseline=baseline,
                                     drop=drop, dens_path=dens_path, std_text=std_text)
    params = curve["fit_params"]
    if fit == "power":
        assert params["exponent"] == 1.0 and params["scale"] == 1.0, \
            "fixture: the power identity fit did not come back as 1/1: %r" % (params,)
        assert params["space"] == "log10", params
    else:
        assert params["slope"] == 1.0 and params["intercept"] == 0.0, \
            "fixture: the linear identity fit did not come back as slope 1 / intercept 0: %r" % (
                params,)
    assert params["r2"] == 1.0, params
    return curve


def _fixture(global_factor=1.0, baseline="flank", fit="linear"):
    """The standard synthetic dens + identity curve pair used by most checks.

    Standards 1/2/3 carry areas 2/4/8 and are fitted with mass = their own area, so the identity
    fit spans volume 2.0 … 8.0.  Standard 4 (area 3.0) is selected with `:*` and dropped, which
    keeps it a standard for the exclusion rule while taking it out of the range.  Sample lanes:
    6 = 2 in-range bands (5.0 + 2.5 → 7.5), 7 = lower boundary (2.0), 9 = upper boundary (8.0),
    10 = 20 % below (1.6), 11 = 20 % above (9.6), 12 = one out-of-range + one in-range band,
    13 = no bands.
    """
    lanes = []
    for no, area in ((1, 2.0), (2, 4.0), (3, 8.0), (4, 3.0)):
        lanes.append(_lane(no, "S%d" % no,
                           [_band(1, 2.0, 45.0, area, area * global_factor, strongest=True)]))
    lanes.append(_lane(6, "A", [_band(1, 2.0, 45.0, 5.0, 5.0 * global_factor),
                                _band(2, 4.6, None, 2.5, 2.5 * global_factor)]))
    lanes.append(_lane(7, "B", [_band(1, 2.0, 45.0, 2.0, 2.0 * global_factor, strongest=True)]))
    lanes.append(_lane(9, "C", [_band(1, 2.0, 45.0, 8.0, 8.0 * global_factor, strongest=True)]))
    lanes.append(_lane(10, "D", [_band(1, 2.0, 45.0, 1.6, 1.6 * global_factor,
                                      strongest=True)]))
    lanes.append(_lane(11, "E", [_band(1, 2.0, 45.0, 9.6, 9.6 * global_factor,
                                      strongest=True)]))
    lanes.append(_lane(12, "F", [_band(1, 2.0, 45.0, 1.6, 1.6 * global_factor),
                                 _band(2, 4.6, None, 5.0, 5.0 * global_factor, strongest=True)]))
    lanes.append(_lane(13, None, []))
    dens = _dens(lanes)
    factor = 1.0 if baseline == "flank" else float(global_factor)
    std = [(1, None, 2.0 * factor), (2, None, 4.0 * factor),
           (3, None, 8.0 * factor), (4, "*", 3.0 * factor)]
    curve = _identity_curve(dens, std, fit=fit, baseline=baseline, drop=(4,),
                            dens_path="fig.dens.json",
                            std_text="1=%g,2=%g,3=%g,4:*=%g"
                                     % (2.0 * factor, 4.0 * factor, 8.0 * factor,
                                        3.0 * factor))
    return dens, curve


def _lane_of(quant, no):
    for lane in quant["lanes"]:
        if lane["lane"] == no:
            return lane
    raise AssertionError("quant 里没有泳道 %d（实际 %r）"
                         % (no, [ln["lane"] for ln in quant["lanes"]]))


def _band_of(lane, index):
    for band in lane["bands"]:
        if band["index"] == index:
            return band
    raise AssertionError("泳道 %d 里没有带 %d" % (lane["lane"], index))


def _known(lane):
    """A lane whose total is a number (its mass shares are defined)."""
    return lane["mass_total"] is not None


def _has_num(text, value, nd=1):
    """True when `text` shows `value` either fixed (`%.nd f`) or trailing-zero trimmed."""
    fixed = "%.*f" % (nd, value)
    forms = [fixed]
    if "." in fixed:
        forms.append(fixed.rstrip("0").rstrip("."))
    for form in forms:
        if re.search(r"(?<![0-9.])%s(?![0-9])" % re.escape(form), text):
            return True
    return False


def _has_pct(text, value, max_nd=2):
    """True when `text` shows `value` as a percentage rounded to 0 … `max_nd` decimals.

    The JSON keeps percentages at 2 dp (ruling R2); the terminal table is only required to print
    the share, so every precision the implementation may choose for display is accepted.
    """
    for nd in range(max_nd + 1):
        fixed = "%.*f%%" % (nd, value)
        if re.search(r"(?<![0-9.])%s" % re.escape(fixed), text):
            return True
        trimmed = fixed[:-1]
        if "." in trimmed:
            trimmed = trimmed.rstrip("0").rstrip(".")
        if re.search(r"(?<![0-9.])%s%%" % re.escape(trimmed), text):
            return True
    return False


def _decimals(value):
    """Decimal places the stored float shows in its JSON repr (scientific notation is an error)."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        raise AssertionError("quant 里出现了科学计数法 %r（精度规则要求定点小数）" % (value,))
    return len(text.split(".")[1]) if "." in text else 0


def _formatted(quant, curve=None):
    """`format_quant(quant)` (spec §4.1), plus the curve as display metadata when the signature
    accepts it.  §5 asks the header for the standard range / point count / r², none of which
    §4.6's fixed quant.json key list can hold, so an implementation may take the curve as an
    extra keyword; both shapes are accepted and the check states which one ran."""
    try:
        params = inspect.signature(densitometry.format_quant).parameters
    except (TypeError, ValueError):
        accepts = False
    else:
        accepts = "curve" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
    if curve is not None and accepts:
        return densitometry.format_quant(quant, curve=curve), True
    return densitometry.format_quant(quant), False


def _single_line(msg, what):
    assert msg and "\n" not in msg, "%s 的错误必须是一行（收到 %r）" % (what, msg)


def _refuses(fn, what, *words):
    """`fn()` must raise one single-line RuntimeError naming each of `words` (if any)."""
    try:
        fn()
    except RuntimeError as e:
        msg = str(e)
        _single_line(msg, what)
        for word in words:
            assert word in msg, "%s 的错误该提到 %r：%r" % (what, word, msg)
        return msg
    raise AssertionError("%s 该抛出 RuntimeError" % what)


def _run(argv, canvas=True):
    """Run `gel_photo.main` with redirected stdio → (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = gel_photo.main(argv, canvas=canvas)
    return rc, out.getvalue(), err.getvalue()


def _help(argv):
    """The help text `gel_photo.main(argv)` prints for an `--help` invocation.

    argparse prints the help and then exits through SystemExit(0); the text is on stdout, so it
    is captured here instead of being allowed to escape into the test runner.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            gel_photo.main(list(argv), canvas=True)
        except SystemExit as exc:
            assert exc.code in (0, None), "--help 该以 0 退出（%r）：%r" % (exc.code, err.getvalue())
        else:
            raise AssertionError("%r 没走 --help 的 SystemExit：stdout=%r" % (argv, out.getvalue()))
    return out.getvalue()


def _cli_refusal(argv, what, *words):
    """One CLI refusal: exit 2, empty stdout, one `[!] …` stderr line naming each word."""
    rc, out, err = _run(argv)
    assert rc == 2, "%s：退出码该是 2（收到 %r，stdout=%r）" % (what, rc, out)
    assert out == "", "%s：拒绝路径不该往 stdout 写东西（收到 %r）" % (what, out)
    assert err.startswith("[!] ") and err.endswith("\n"), "%s：stderr 该是一行 [!] …（%r）" % (
        what, err)
    assert err.rstrip("\n").count("\n") == 0, "%s：stderr 该只有一行（%r）" % (what, err)
    assert "Traceback" not in err, "%s：不该吐 traceback（%r）" % (what, err)
    for word in words:
        assert word in err, "%s：错误该提到 %r（%r）" % (what, word, err)
    return err


def _write(dens, curve, td):
    """Write the fixture's dens.json + curve.json into `td`; returns their paths."""
    dens_path = os.path.join(td, "fig.dens.json")
    curve_path = os.path.join(td, "fig.curve.json")
    densitometry.write_json(dens, dens_path)
    densitometry.write_json(curve, curve_path)
    return dens_path, curve_path


def _copy(src, dst):
    """Copy bytes: read first, then write, so a case-insensitive name collision cannot truncate."""
    with open(src, "rb") as f1:
        blob = f1.read()
    with open(dst, "wb") as f2:
        f2.write(blob)


# ---------------------------------------------------------------- 1) JSON shape and totals

def check_schema_and_totals():
    """§4.6 shape: exact key sets, copied unit/baseline/fit/source, dens lane order, integer
    lanes/indices, and ruling R2's precision (3 dp areas/masses/totals, 2 dp percentages)."""
    dens, curve = _fixture()
    quant = densitometry.quantify(dens, curve, dens_path="fig.dens.json",
                                  curve_path="fig.curve.json")
    assert set(quant) == TOP_KEYS, sorted(quant)
    assert (quant["version"], quant["kind"]) == (1, QUANT_KIND), quant
    assert quant["unit"] == "ug" and quant["baseline"] == "flank" and quant["fit"] == "linear"
    assert set(quant["source"]) == SOURCE_KEYS, sorted(quant["source"])
    assert quant["source"] == {"dens": "fig.dens.json", "curve": "fig.curve.json",
                               "load": None}, quant["source"]
    assert _number(quant["overall_mass"]) and _decimals(quant["overall_mass"]) <= 3
    assert isinstance(quant["notes"], list)

    # lane order = dens order (standards removed), lanes stay integers
    assert [ln["lane"] for ln in quant["lanes"]] == list(SAMPLE_LANES), \
        [ln["lane"] for ln in quant["lanes"]]
    for lane in quant["lanes"]:
        assert set(lane) == LANE_KEYS, sorted(lane)
        assert isinstance(lane["lane"], int) and isinstance(lane["bands"], list)
        assert lane["out_of_range_bands"] == [
            b["index"] for b in lane["bands"] if b["out_of_range"]], lane
        assert lane["load_ul"] is None, "不给 load 时 load_ul 该是 None"
        for band in lane["bands"]:
            assert set(band) == BAND_KEYS, sorted(band)
            assert isinstance(band["index"], int) and isinstance(band["out_of_range"], bool)
            assert _number(band["area"]) and _decimals(band["area"]) <= 3, band
            if band["mass"] is not None:
                assert _number(band["mass"]) and _decimals(band["mass"]) <= 3, band
            if band["rel_pct"] is not None:
                assert _number(band["rel_pct"]) and _decimals(band["rel_pct"]) <= 2, band
        for key in ("mass_total", "conc"):
            if lane[key] is not None:
                assert _number(lane[key]) and _decimals(lane[key]) <= 3, lane
        if lane["frac_pct_among_lanes"] is not None:
            assert (_number(lane["frac_pct_among_lanes"])
                    and _decimals(lane["frac_pct_among_lanes"]) <= 2), lane

    # y_cm / mw_kda / label copied through; band order is dens order
    two_band = _lane_of(quant, 6)
    assert [b["index"] for b in two_band["bands"]] == [1, 2]
    assert two_band["label"] == "A"
    assert two_band["bands"][0]["y_cm"] == 2.0 and two_band["bands"][1]["y_cm"] == 4.6
    assert two_band["bands"][0]["mw_kda"] == 45.0
    assert two_band["bands"][1]["mw_kda"] is None, "mw_kda 该原样保留 None"
    assert _lane_of(quant, 13)["label"] is None, "label 不该被改写"
    assert _lane_of(quant, 13)["bands"] == []

    # totals: mass_total = Σ band masses, overall_mass = Σ known totals
    for lane in quant["lanes"]:
        if lane["bands"] and all(b["mass"] is not None for b in lane["bands"]):
            assert abs(lane["mass_total"] - sum(b["mass"] for b in lane["bands"])) <= 0.0005, lane
    known = [ln["mass_total"] for ln in quant["lanes"] if _known(ln)]
    assert abs(quant["overall_mass"] - sum(known)) <= 0.001, (quant["overall_mass"], known)
    # the identity fit puts the three clean sample lanes at 7.5 / 2.0 / 8.0
    assert abs(_lane_of(quant, 6)["mass_total"] - 7.5) <= 0.0005
    assert abs(_lane_of(quant, 7)["mass_total"] - 2.0) <= 0.0005
    assert abs(_lane_of(quant, 9)["mass_total"] - 8.0) <= 0.0005
    assert abs(quant["overall_mass"] - 17.5) <= 0.001, quant["overall_mass"]

    # the curve's unit is copied verbatim (§4.6) instead of a hard-coded "ug", and it is what the
    # terminal header and the per-band lines print
    dens_ng, _unused_curve = _fixture()
    curve_ng = _identity_curve(dens_ng, [(1, None, 2.0), (2, None, 4.0), (3, None, 8.0),
                                         (4, "*", 3.0)], drop=(4,), unit="ng",
                               dens_path="fig.dens.json", std_text="1=2,2=4,3=8,4:*=3")
    assert curve_ng["unit"] == "ng", curve_ng["unit"]
    quant_ng = densitometry.quantify(dens_ng, curve_ng)
    assert quant_ng["unit"] == "ng", quant_ng["unit"]
    head = _formatted(quant_ng, curve_ng)[0].splitlines()[0]
    assert "ng" in head and "ug" not in head.replace("ng", ""), \
        "单位该原样跟着 curve（ng）：%r" % (head,)
    return True


# ---------------------------------------------------------------- 2) standard lanes are skipped

def check_standard_lanes_skipped():
    """Every lane in ANY curve point entry — a dropped one and a `:*` one included — is absent
    from the table, and the note says how many were skipped (§4.6)."""
    dens, curve = _fixture()
    curve_lanes = sorted({int(entry[0]) for point in curve["points"]
                          for entry in point["entries"]})
    assert curve_lanes == list(STANDARD_LANES), curve_lanes
    dropped = next(p for p in curve["points"] if not p["used"])
    assert dropped["entries"] == [[4, "*"]], dropped

    quant = densitometry.quantify(dens, curve)
    listed = [ln["lane"] for ln in quant["lanes"]]
    for no in curve_lanes:
        assert no not in listed, "标准道 %d 不该出现在定量表里（%r）" % (no, listed)
    assert listed == list(SAMPLE_LANES), listed

    skips = [note for note in quant["notes"] if "跳过" in note]
    assert len(skips) == 1, quant["notes"]
    skip = skips[0]
    _single_line(skip, "跳过标准道的注")
    assert "4 条标准道" in skip, skip
    if "1-4" not in skip:                       # a compact run is the ticket's preference
        for no in STANDARD_LANES:
            assert re.search(r"(?<![0-9])%d(?![0-9])" % no, skip), \
                "跳过注该列出泳道 %d（或写成 1-4）：%r" % (no, skip)
    return True


# ---------------------------------------------------------------- 3) in-range masses (neutral)

def check_in_range_masses_identity():
    """In-range bands (identity fit) recover `mass == area`; `rel_pct` agrees with the lane total
    and sums to 100; a lane with no out-of-range band has `out_of_range: false` (§4.6)."""
    dens, curve = _fixture()
    quant = densitometry.quantify(dens, curve)
    for lane in quant["lanes"]:
        for band in lane["bands"]:
            if band["out_of_range"]:
                continue
            assert abs(band["mass"] - band["area"]) <= 0.0005, \
                "identity fit 下在范围内的质量该等于面积：%r" % (band,)
        if not lane["bands"]:
            continue
        shares = [b["rel_pct"] for b in lane["bands"]]
        if all(share is not None for share in shares):
            assert abs(sum(shares) - 100.0) <= 0.05, (lane["lane"], shares)
            for band in lane["bands"]:
                assert abs(band["rel_pct"]
                           - band["mass"] / lane["mass_total"] * 100.0) <= 0.02, (lane, band)
    for no in (6, 7, 9):
        lane = _lane_of(quant, no)
        assert lane["out_of_range"] is False and lane["out_of_range_bands"] == [], lane
    # lane 7 sits exactly on volume_min and lane 9 exactly on volume_max: both are IN range
    assert _lane_of(quant, 7)["bands"][0]["mass"] == 2.0
    assert _lane_of(quant, 9)["bands"][0]["mass"] == 8.0
    # several bands in one lane are joined by the lane total: 5.0 + 2.5 = 7.5
    two_band = _lane_of(quant, 6)
    assert abs(two_band["mass_total"] - 7.5) <= 0.0005, two_band
    assert [round(b["rel_pct"], 2) for b in two_band["bands"]] == [66.67, 33.33], two_band
    return True


# ---------------------------------------------------------------- 4) out of range

def check_out_of_range_nulls():
    """§2.8: an out-of-range band gets `mass: None` + `out_of_range: true` and takes its lane's
    `mass_total` / `conc` / `frac_pct_among_lanes` / `rel_pct` down with it; the band index is
    in `out_of_range_bands`; the note names the band; the terminal line shows `—`, the range
    comparison and no total."""
    dens, curve = _fixture()
    quant = densitometry.quantify(dens, curve)

    below = _lane_of(quant, OOR_BELOW[0])
    band = _band_of(below, OOR_BELOW[1])
    assert band["area"] == 1.6, band
    assert band["mass"] is None and band["out_of_range"] is True and band["rel_pct"] is None, band
    assert below["mass_total"] is None and below["conc"] is None, below
    assert below["frac_pct_among_lanes"] is None, below
    assert below["out_of_range"] is True and below["out_of_range_bands"] == [1], below

    above = _lane_of(quant, OOR_ABOVE[0])
    band = _band_of(above, OOR_ABOVE[1])
    assert band["area"] == 9.6, band
    assert band["mass"] is None and band["out_of_range"] is True and band["rel_pct"] is None, band
    assert above["mass_total"] is None and above["conc"] is None, above
    assert above["out_of_range_bands"] == [1], above

    # one out-of-range band and one in-range band: the in-range band keeps its mass, the lane
    # total is still None
    mixed = _lane_of(quant, MIXED_LANE)
    out_band = _band_of(mixed, 1)
    in_band = _band_of(mixed, 2)
    assert out_band["mass"] is None and out_band["out_of_range"] is True, out_band
    assert in_band["mass"] == 5.0 and in_band["out_of_range"] is False, in_band
    assert in_band["rel_pct"] is None, "合计为 None 时 rel_pct 也该是 None"
    assert mixed["mass_total"] is None and mixed["frac_pct_among_lanes"] is None
    assert mixed["out_of_range_bands"] == [1], mixed

    # one note per out-of-range band, naming lane + band, with the §2.8 sentence; the advice
    # differs by direction (below vs above)
    oor_notes = [note for note in quant["notes"] if "超出标曲范围" in note]
    assert len(oor_notes) == 3, quant["notes"]
    for note in oor_notes:
        _single_line(note, "超出标曲范围的注")
        assert "不给数" in note, "超范围注该说明合计与浓度不给数：%r" % (note,)
    by_lane = {}
    for note in oor_notes:
        found = re.search(r"泳道\s*(\d+)", note)
        assert found, note
        by_lane[int(found.group(1))] = note
    assert sorted(by_lane) == [10, 11, 12], sorted(by_lane)
    for no in (10, 11, 12):
        assert re.search(r"带\s*1", by_lane[no]), by_lane[no]
    # the advice is direction-specific and the ticket prescribes its wording: too little stain →
    # load more; too much → dilute or load less
    assert "多加点" in by_lane[10], \
        "低于范围该建议多加点 / 浓缩（%r）：%r" % ("多加点或浓缩后再跑", by_lane[10])
    assert "稀释" in by_lane[11], \
        "高于范围该建议稀释 / 少点一点（%r）：%r" % ("稀释或少点一点再跑", by_lane[11])

    # terminal format: no number for the out-of-range lane, only the comparison + advice
    text, has_curve = _formatted(quant, curve)
    lines = text.splitlines()
    assert "None" not in text, "终端输出里不该出现 None"
    for lane_no, sign in ((10, "<"), (11, ">")):
        line = next(ln for ln in lines if re.search(r"泳道\s*%d(?!\d)" % lane_no, ln))
        assert "—" in line, line
        assert "超出标曲范围" in line, line
        if has_curve:      # the bounds live only in the curve (see the format-contract note)
            assert re.search(r"面积\s+[0-9.]+\s*%s\s*[0-9.]+" % sign, line), line
        assert "合计" not in line, "超范围泳道不给合计：%r" % (line,)
    below_line = next(ln for ln in lines if re.search(r"泳道\s*10(?!\d)", ln))
    above_line = next(ln for ln in lines if re.search(r"泳道\s*11(?!\d)", ln))
    assert "多加点" in below_line, "低于范围的终端行该带着加样建议：%r" % (below_line,)
    assert "稀释" in above_line, "高于范围的终端行该带着稀释建议：%r" % (above_line,)
    mixed_line = next(ln for ln in lines if re.search(r"泳道\s*12(?!\d)", ln))
    assert "—" in mixed_line and "超出标曲范围" in mixed_line, mixed_line
    assert "合计" not in mixed_line, mixed_line
    return True


# ---------------------------------------------------------------- 5) boundaries

def check_boundaries():
    """Inclusive range: an area exactly on `volume_min` / `volume_max` is measured; a hair
    outside is out of range (§2.8 + the ticket's `volume_min ≤ area ≤ volume_max`)."""
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 8.0, strongest=True)]),
        _lane(3, "LOWER", [_band(1, 2.0, 45.0, 1.999, strongest=True)]),
        _lane(4, "UPPER", [_band(1, 2.0, 45.0, 8.001, strongest=True)]),
    ])
    curve = _identity_curve(dens, [(1, None, 2.0), (2, None, 8.0)],
                            dens_path="fig.dens.json", std_text="1=2,2=8")
    assert curve["range"] == {"volume_min": 2.0, "volume_max": 8.0,
                              "mass_min": 2.0, "mass_max": 8.0}, curve["range"]
    quant = densitometry.quantify(dens, curve)
    assert [ln["lane"] for ln in quant["lanes"]] == [3, 4]
    for no, area in ((3, 1.999), (4, 8.001)):
        lane = _lane_of(quant, no)
        band = lane["bands"][0]
        assert band["area"] == area, band
        assert band["out_of_range"] is True, (no, band)
        assert band["mass"] is None, (no, band)
        assert lane["mass_total"] is None and lane["out_of_range_bands"] == [1], lane
    return True


# ---------------------------------------------------------------- 6) per-baseline area

def check_baseline_column():
    """`area` is the curve's own baseline column: a global-baseline curve measures
    `area_global`, a flank curve `area_flank` (§4.6), and the identity mass follows the area."""
    dens_flank, curve_flank = _fixture()
    dens_global, curve_global = _fixture(global_factor=2.0, baseline="global")
    qf = densitometry.quantify(dens_flank, curve_flank)
    qg = densitometry.quantify(dens_global, curve_global)
    assert qf["baseline"] == "flank" and qg["baseline"] == "global"
    flank_band = _band_of(_lane_of(qf, 6), 1)
    global_band = _band_of(_lane_of(qg, 6), 1)
    assert flank_band["area"] == 5.0 and global_band["area"] == 10.0, (flank_band, global_band)
    assert abs(global_band["mass"] - 10.0) <= 0.0005, global_band
    assert abs(flank_band["mass"] - 5.0) <= 0.0005, flank_band
    # the range follows the chosen column as well (global: 4 … 16)
    assert curve_global["range"]["volume_min"] == 4.0, curve_global["range"]
    assert curve_global["range"]["volume_max"] == 16.0, curve_global["range"]
    assert _lane_of(qg, 12)["out_of_range_bands"] == [1], _lane_of(qg, 12)
    assert abs(_lane_of(qg, 9)["mass_total"] - 16.0) <= 0.0005
    return True


# ---------------------------------------------------------------- 7) power identity

def check_power_identity():
    """The power fit's identity (`scale = 1, exponent = 1`) recovers `mass == area` too, with
    the curve's `fit: "power"` and log-space fit params (§4.5 / §4.6)."""
    dens, curve = _fixture(fit="power")
    params = curve["fit_params"]
    assert set(params) == POWER_FIT_KEYS, sorted(params)
    assert params["exponent"] == 1.0 and params["scale"] == 1.0, params
    assert params["space"] == "log10" and params["r2"] == 1.0
    quant = densitometry.quantify(dens, curve)
    assert quant["fit"] == "power" and quant["baseline"] == "flank"
    assert abs(_lane_of(quant, 6)["bands"][0]["mass"] - 5.0) <= 0.0005
    assert abs(_lane_of(quant, 6)["bands"][1]["mass"] - 2.5) <= 0.0005
    assert _lane_of(quant, 7)["bands"][0]["mass"] == 2.0
    assert _lane_of(quant, 9)["bands"][0]["mass"] == 8.0
    assert _lane_of(quant, 10)["bands"][0]["mass"] is None
    assert _lane_of(quant, 11)["bands"][0]["mass"] is None
    return True


# ---------------------------------------------------------------- 8) loads / concentration

def check_load_and_concentration():
    """`load_ul` + `conc = mass_total / load_ul` (3 dp, unit/µL), `None` without a load and
    `None` when the lane total is `None`; `source.load` keeps the text as typed (§4.6, R6)."""
    dens, curve = _fixture()
    load = {6: 2.0, 7: 4.0, 10: 2.0}
    quant = densitometry.quantify(dens, curve, load=load, load_text="6=2,7=4,10=2",
                                  dens_path="fig.dens.json", curve_path="fig.curve.json")
    assert quant["source"]["load"] == "6=2,7=4,10=2", quant["source"]
    lane6 = _lane_of(quant, 6)
    lane7 = _lane_of(quant, 7)
    lane9 = _lane_of(quant, 9)
    assert lane6["load_ul"] == 2.0 and abs(lane6["conc"] - 3.75) <= 0.0005, lane6
    assert lane7["load_ul"] == 4.0 and abs(lane7["conc"] - 0.5) <= 0.0005, lane7
    assert lane9["load_ul"] is None and lane9["conc"] is None, lane9
    oor = _lane_of(quant, 10)
    assert oor["load_ul"] == 2.0 and oor["conc"] is None, "超范围泳道不给浓度：%r" % (oor,)
    for lane in (lane6, lane7):
        assert abs(lane["conc"] - lane["mass_total"] / lane["load_ul"]) <= 0.001, lane

    no_load = densitometry.quantify(dens, curve, dens_path="fig.dens.json",
                                    curve_path="fig.curve.json")
    assert no_load["source"]["load"] is None
    for lane in no_load["lanes"]:
        assert lane["load_ul"] is None and lane["conc"] is None, lane

    with_load = _formatted(quant, curve)[0].splitlines()
    lane6_line = next(ln for ln in with_load if re.search(r"泳道\s*6(?!\d)", ln))
    assert "浓度" in lane6_line and "ug/uL" in lane6_line, lane6_line
    without = _formatted(no_load, curve)[0]
    assert "ug/uL" not in without and "None" not in without, without
    return True


# ---------------------------------------------------------------- 9) lane shares / overall

def check_lane_shares_and_overall():
    """`frac_pct_among_lanes` is 2 dp, agrees with `mass_total / Σ known totals`, sums to 100
    (within 0.11 — the accumulated 2-dp rounding), is `None` for the unknown totals, and
    `overall_mass` is their sum (§4.6)."""
    dens, curve = _fixture()
    quant = densitometry.quantify(dens, curve)
    with_bands = [lane for lane in quant["lanes"] if lane["bands"]]
    known = [lane for lane in with_bands if _known(lane)]
    unknown = [lane for lane in with_bands if not _known(lane)]
    assert [lane["lane"] for lane in unknown] == [10, 11, 12], \
        [lane["lane"] for lane in unknown]
    total_sum = sum(lane["mass_total"] for lane in known)
    assert abs(total_sum - 17.5) <= 0.001, total_sum
    assert abs(quant["overall_mass"] - total_sum) <= 0.001, quant["overall_mass"]
    shares = []
    for lane in known:
        share = lane["frac_pct_among_lanes"]
        assert share is not None, lane
        assert abs(share - lane["mass_total"] / total_sum * 100.0) <= 0.02, lane
        shares.append(share)
    assert abs(sum(shares) - 100.0) <= 0.11, shares
    assert [round(s, 2) for s in shares] == [42.86, 11.43, 45.71], shares
    # the same 0.11 bound holds when every share is independently rounded: three lanes, each
    # rounding error ≤ 0.005, so a 2-dp sum may legitimately land at 100.00 ± 0.015
    assert abs(sum(round(s, 2) for s in shares) - 100.0) <= 0.11, shares
    for lane in unknown:
        assert lane["frac_pct_among_lanes"] is None, lane
    return True


# ---------------------------------------------------------------- 10) precision

def check_precision_rounding():
    """Ruling R2 observed on a 4-dp area and a 4-dp y_cm: the built dict rounds areas / masses
    to 3 dp and keeps y_cm at 4 dp; integers stay integers."""
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 8.0, strongest=True)]),
        _lane(3, "A", [_band(1, 2.4321, 45.0, 5.00051),
                       _band(2, 4.6, None, 3.0, strongest=True)]),
    ])
    curve = _identity_curve(dens, [(1, None, 2.0), (2, None, 8.0)],
                            dens_path="fig.dens.json", std_text="1=2,2=8")
    quant = densitometry.quantify(dens, curve)
    lane = _lane_of(quant, 3)
    band = _band_of(lane, 1)
    assert band["area"] == 5.001, band
    assert band["mass"] == 5.001, band
    assert band["y_cm"] == 2.4321, band
    assert abs(lane["mass_total"] - 8.001) <= 0.0005, lane
    assert isinstance(band["rel_pct"], float) or isinstance(band["rel_pct"], int)
    assert isinstance(band["index"], int) and isinstance(lane["lane"], int)
    return True


# ---------------------------------------------------------------- 11) purity / idempotence

def check_purity_and_json_round_trip():
    """`quantify` is pure (§2.11) and idempotent (§7.11): inputs untouched, two calls identical,
    `write_json` twice byte-identical, UTF-8 without `\\u` escapes, `load_json` round-trips
    every field (§7.10)."""
    dens, curve = _fixture()
    before = json.dumps([dens, curve], sort_keys=True)
    first = densitometry.quantify(dens, curve, dens_path="fig.dens.json",
                                  curve_path="fig.curve.json")
    second = densitometry.quantify(dens, curve, dens_path="fig.dens.json",
                                   curve_path="fig.curve.json")
    assert json.dumps([dens, curve], sort_keys=True) == before, "quantify 改了输入"
    assert first == second, "quantify 两次结果不一致"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    with tempfile.TemporaryDirectory() as td:
        path_a = os.path.join(td, "a.quant.json")
        path_b = os.path.join(td, "b.quant.json")
        densitometry.write_json(first, path_a)
        densitometry.write_json(second, path_b)
        with open(path_a, "rb") as f1, open(path_b, "rb") as f2:
            raw_a, raw_b = f1.read(), f2.read()
        assert raw_a == raw_b, "两次写出的 quant.json 字节不一致"
        assert b"\\u" not in raw_a, "UTF-8 文件里不该出现 \\u 转义（ensure_ascii=False）"
        # the Chinese notes are stored as real UTF-8, and the round trip keeps every field
        for note in first["notes"]:
            assert note.encode("utf-8") in raw_a, note
        assert raw_a.endswith(b"\n"), "write_json 该以换行结尾"
        loaded = densitometry.load_json(path_a, QUANT_KIND)
        assert loaded == first, "load_json 往返丢了字段"
        assert loaded["kind"] == QUANT_KIND and loaded["version"] == 1
        assert "timestamp" not in json.dumps(loaded), "quant.json 不该带时间戳"
    return True


# ---------------------------------------------------------------- 12) terminal format

def check_format_quant():
    """§5 `format_quant`: the `[定量]` header (baseline / fit / unit / standard range / point
    count / r² / skipped count), one line per sample lane with the `│ 合计 … 占全胶 …` block,
    `注：` lines for the notes, no `None` anywhere (also for a `None` label)."""
    dens, curve = _fixture()
    quant = densitometry.quantify(dens, curve, dens_path="fig.dens.json",
                                  curve_path="fig.curve.json")
    text, has_curve = _formatted(quant, curve)
    assert isinstance(text, str)
    assert not text.endswith("\n"), "format_quant 的文本由调用方按行打印，不该自带结尾换行"
    assert "None" not in text, "终端输出里不该出现 None（label 为 None 的泳道也在内）"
    lines = text.splitlines()

    head = lines[0]
    assert head.startswith("[定量]"), head
    for token in ("基线", "flank", "linear", "ug", "跳过 4 条标准道"):
        assert token in head, "表头缺 %r：%r" % (token, head)
    if has_curve:
        # §5 also wants the standard range, the point count and r²; §4.6's fixed key list has none
        # of them, so they can only come from display metadata (see the format-contract note)
        assert "标曲" in head, head
        assert re.search(r"r²\s*1\.0000", head), head
        assert "3 点" in head, head
        assert _has_num(head, 2.0) and _has_num(head, 8.0), \
            "表头该给出标曲的质量范围 2–8 ug：%r" % (head,)

    # one lane line per sample lane, in dens order, with the band / area / mass / share block
    lane_lines = [line for line in lines[1:] if line.startswith("  泳道 ")]
    assert len(lane_lines) == len(quant["lanes"]), (lane_lines, quant["lanes"])
    for line, lane in zip(lane_lines, quant["lanes"]):
        assert re.match(r"  泳道\s*%d(?!\d)" % lane["lane"], line), (line, lane["lane"])
    lane6_line = lane_lines[0]
    assert "A" in lane6_line and "带1" in lane6_line and "带2" in lane6_line, lane6_line
    assert "kDa" in lane6_line and "面积" in lane6_line, lane6_line
    assert "ug" in lane6_line and _has_pct(lane6_line, 66.67), lane6_line
    assert "│ 合计" in lane6_line and "占全胶" in lane6_line, lane6_line
    assert _has_num(lane6_line, 45.0) and _has_num(lane6_line, 5.0, 3), lane6_line
    # the band whose mw_kda is None prints a dash, never "None"
    assert "带2" in lane6_line and "—" in lane6_line, lane6_line
    lane13_line = lane_lines[-1]
    assert "泳道 13" in lane13_line and "None" not in lane13_line, lane13_line

    # notes are printed as `  注：…` lines; every out-of-range note must be there
    note_lines = [line for line in lines[1:] if line.startswith("  注：")]
    assert note_lines, "格式里该把 notes 印成 注： 行"
    assert len(note_lines) <= len(quant["notes"]), (note_lines, quant["notes"])
    for note in quant["notes"]:
        if "超出标曲范围" in note:
            assert any(note in line for line in note_lines), (note, note_lines)
    for line in note_lines:
        assert line.strip() != "注：", "注： 行不该为空"
    return True


# ---------------------------------------------------------------- 13) API refusals

def check_api_refusals():
    """The pure function's user errors are single-line RuntimeErrors: an unrecognised
    `baseline` / `fit`, curve keys it needs, and a load naming a lane that is not in dens."""
    dens, curve = _fixture()
    bad = dict(curve)
    bad["baseline"] = "local"
    _refuses(lambda: densitometry.quantify(dens, bad), "curve baseline=local")
    bad = dict(curve)
    bad["fit"] = "cubic"
    _refuses(lambda: densitometry.quantify(dens, bad), "curve fit=cubic")
    bad = dict(curve)
    bad.pop("range")
    _refuses(lambda: densitometry.quantify(dens, bad), "curve without range")
    bad = dict(curve)
    bad.pop("fit_params")
    _refuses(lambda: densitometry.quantify(dens, bad), "curve without fit_params")
    _refuses(lambda: densitometry.quantify(dens, curve, load={99: 2.0}), "load lane 99", "99")
    return True


# ---------------------------------------------------------------- 14) CLI parser + default path

def check_cli_default_path():
    """CLI as contract (§3.3, R12): `quantify` is advertised by `dens --help` and its own help,
    parser shape/defaults, both group names, default `fig.curve.json → fig.quant.json` (never
    `fig.curve.quant.json`), the fallback names, the §5 terminal block (header + one line per
    sample lane, no standard lane), and two identical CLI runs writing byte-identical JSON."""
    from epcraft import densitometry_cli  # noqa: F401  (the group must be registered by canvas_cli)

    # the sub-command must be discoverable, and its help must name every option of §3.3
    group_help = _help(["dens", "--help"])
    assert "quantify" in group_help, "dens --help 里该出现 quantify：%r" % (group_help,)
    assert re.search(r"quantify", group_help), group_help
    quant_help = _help(["dens", "quantify", "--help"])
    for token in ("--dens", "--curve", "--out", "--load"):
        assert token in quant_help, "dens quantify --help 里该有 %r：%r" % (token, quant_help)
    assert "quantify" in quant_help and "usage:" in quant_help, quant_help

    ap = gel_photo.build_parser(canvas=True)
    a = ap.parse_args(["dens", "quantify", "--dens", "d.json", "--curve", "c.json"])
    assert a.cmd == "dens" and a.action == "quantify", (a.cmd, a.action)
    assert a.func.__name__ == "cmd_quantify", a.func
    assert a.out is None and a.load is None, (a.out, a.load)
    assert a.dens == "d.json" and a.curve == "c.json", (a.dens, a.curve)
    b = ap.parse_args(["densitometry", "quantify", "--dens", "d.json", "--curve", "c.json",
                       "--load", "6=2", "--out", "q.json"])
    assert (b.load, b.out) == ("6=2", "q.json") and b.func is a.func, (b.load, b.out)
    for argv, flag in ((("dens", "quantify", "--curve", "c.json"), "--dens"),
                       (("dens", "quantify", "--dens", "d.json"), "--curve")):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                ap.parse_args(list(argv))
            except SystemExit as e:
                assert e.code == 2, (argv, e.code)
            else:
                raise AssertionError("%r 该被 argparse 拒绝（缺 %s）" % (argv, flag))
        assert flag in err.getvalue(), (argv, err.getvalue())

    dens, curve = _fixture()
    with tempfile.TemporaryDirectory() as td:
        dens_path, curve_path = _write(dens, curve, td)
        target = os.path.join(td, "fig.quant.json")
        rc, out, err = _run(["dens", "quantify", "--dens", dens_path, "--curve", curve_path])
        assert rc == 0 and err == "", "成功路径不该写 stderr：%r" % (err,)
        lines = out.splitlines()
        head = lines[0]
        assert head.startswith("[定量]"), head
        # §5's header: baseline, fit, unit, standard mass range, point count, r² and the number
        # of skipped standard lanes.  The CLI has both objects in hand, so all of it is required
        # (see the format-contract note in the module docstring).
        for token in ("基线", "flank", "linear", "ug", "标曲", "跳过 4 条标准道"):
            assert token in head, "§5 的定量表头缺 %r：%r" % (token, head)
        assert re.search(r"r²\s*1\.0000", head), head
        assert "3 点" in head, "表头该给出标曲的有效点数 3（第 4 点被 drop）：%r" % (head,)
        assert _has_num(head, 2.0) and _has_num(head, 8.0), \
            "表头该给出标曲的质量范围 2–8 ug：%r" % (head,)
        # one line per sample lane, in dens order, and no standard lane in the table
        lane_lines = [line for line in lines if re.match(r"\s*泳道\s*\d+", line)]
        lane_nos = [int(re.match(r"\s*泳道\s*(\d+)", line).group(1)) for line in lane_lines]
        assert lane_nos == list(SAMPLE_LANES), lane_nos
        # the standards are absent from the table itself (the skip NOTE does name them, so only
        # the lane lines are searched here)
        for no in STANDARD_LANES:
            assert no not in lane_nos, "标准道 %d 不该出现在终端表里：%r" % (no, lane_nos)
        assert lines[-1] == "[i] 写出 %s" % target, lines[-1]
        assert os.path.isfile(target), target
        assert not os.path.exists(os.path.join(td, "fig.curve.quant.json")), \
            "默认名不该把 .curve 留在文件名里"
        quant = densitometry.load_json(target, QUANT_KIND)
        assert quant["kind"] == QUANT_KIND and quant["version"] == 1
        assert quant["source"] == {"dens": dens_path, "curve": curve_path, "load": None}
        assert [ln["lane"] for ln in quant["lanes"]] == list(SAMPLE_LANES)
        with open(target, "rb") as f:
            raw_first = f.read()
        rc, out2, err2 = _run(["dens", "quantify", "--dens", dens_path, "--curve", curve_path])
        assert rc == 0 and err2 == "", err2
        with open(target, "rb") as f:
            assert f.read() == raw_first, "CLI 两次写出的 quant.json 字节不一致"

        # fallback: a curve that is not *.curve.json drops its final extension
        flat = os.path.join(td, "c.json")
        _copy(curve_path, flat)
        rc, out3, err3 = _run(["dens", "quantify", "--dens", dens_path, "--curve", flat])
        assert rc == 0 and err3 == "", err3
        fallback = os.path.join(td, "c.quant.json")
        assert out3.splitlines()[-1] == "[i] 写出 %s" % fallback, out3.splitlines()[-1]
        assert os.path.isfile(fallback), fallback
        assert not os.path.exists(os.path.join(td, "c.curve.quant.json"))
        # the chain suffix is matched case-insensitively, mirroring cmd_curve (distinct stem, so a
        # case-insensitive filesystem cannot alias the source and the target)
        upper = os.path.join(td, "spec.CURVE.JSON")
        _copy(curve_path, upper)
        rc, out4, err4 = _run(["dens", "quantify", "--dens", dens_path, "--curve", upper])
        assert rc == 0 and err4 == "", err4
        upper_target = os.path.join(td, "spec.quant.json")
        assert out4.splitlines()[-1] == "[i] 写出 %s" % upper_target, out4.splitlines()[-1]
        assert os.path.isfile(upper_target), upper_target
    return True


# ---------------------------------------------------------------- 15) CLI load / source

def check_cli_load():
    """`--load "12=2,13=2.5"` end to end through the CLI: the grammar parses, `source.load` is the
    text as typed (R6), `load_ul` and `conc = mass_total / load_ul` land in the JSON, and the
    terminal block carries the concentration; both group names agree."""
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 4.0, strongest=True)]),
        _lane(3, "S3", [_band(1, 2.0, 45.0, 8.0, strongest=True)]),
        _lane(12, "F", [_band(1, 2.0, 45.0, 5.0, strongest=True)]),
        _lane(13, "G", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
    ])
    curve = _identity_curve(dens, [(1, None, 2.0), (2, None, 4.0), (3, None, 8.0)],
                            dens_path="fig.dens.json", std_text="1=2,2=4,3=8")
    with tempfile.TemporaryDirectory() as td:
        dens_path, curve_path = _write(dens, curve, td)
        target = os.path.join(td, "load.quant.json")
        rc, out, err = _run(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                             "--load", "12=2,13=2.5", "--out", target])
        assert rc == 0 and err == "", err
        quant = densitometry.load_json(target, QUANT_KIND)
        assert quant["source"] == {"dens": dens_path, "curve": curve_path, "load": "12=2,13=2.5"}, \
            quant["source"]
        lane12 = _lane_of(quant, 12)
        lane13 = _lane_of(quant, 13)
        # identity fit, in range: lane 12 = 5.0 ug over 2 uL, lane 13 = 2.0 ug over 2.5 uL
        assert lane12["load_ul"] == 2.0 and abs(lane12["conc"] - 5.0 / 2.0) <= 0.0005, lane12
        assert lane13["load_ul"] == 2.5 and abs(lane13["conc"] - 2.0 / 2.5) <= 0.0005, lane13
        for lane in (lane12, lane13):
            assert abs(lane["conc"] - lane["mass_total"] / lane["load_ul"]) <= 0.0005, lane
        for line in out.splitlines():
            found = re.match(r"\s*泳道\s*(\d+)", line)
            if not found:
                continue
            no = int(found.group(1))
            if no in (12, 13):
                assert "浓度" in line and "ug/uL" in line, \
                    "带 load 的泳道该印浓度：%r" % (line,)
        # the same numbers through the long group name
        alias_target = os.path.join(td, "alias.quant.json")
        rc, out2, err2 = _run(["densitometry", "quantify", "--dens", dens_path,
                               "--curve", curve_path, "--load", "12=2,13=2.5",
                               "--out", alias_target])
        assert rc == 0 and err2 == "", err2
        alias_quant = densitometry.load_json(alias_target, QUANT_KIND)
        assert alias_quant["lanes"] == quant["lanes"], "两个命令名的结果该一致"
        # a lane that is left out of --load keeps `load_ul: null` and `conc: null`
        unloaded_target = os.path.join(td, "partial.quant.json")
        rc, out3, err3 = _run(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                               "--load", "12=2", "--out", unloaded_target])
        assert rc == 0 and err3 == "", err3
        partial = densitometry.load_json(unloaded_target, QUANT_KIND)
        assert _lane_of(partial, 12)["load_ul"] == 2.0
        assert _lane_of(partial, 13)["load_ul"] is None and _lane_of(partial, 13)["conc"] is None, \
            "没给 --load 的泳道不给浓度：%r" % (_lane_of(partial, 13),)
    return True


# ---------------------------------------------------------------- 16) CLI refusals

def check_cli_refusals():
    """Every refusal through one `[!] …` stderr line with exit 2, empty stdout and no output file
    written (§3.3, R5, R12): missing / wrong-kind inputs, the whole `--load` grammar (empty
    segment, missing `=`, non-integer lane, zero, negative or non-numeric volume, duplicate lane,
    unknown lane) and `--out` pointing at an input, a directory or a missing directory."""
    dens, curve = _fixture()
    with tempfile.TemporaryDirectory() as td:
        dens_path, curve_path = _write(dens, curve, td)
        other_dens = os.path.join(td, "curve-as-dens.json")   # right JSON, wrong kind
        other_curve = os.path.join(td, "dens-as-curve.json")
        _copy(curve_path, other_dens)
        _copy(dens_path, other_curve)

        _cli_refusal(["dens", "quantify", "--dens", os.path.join(td, "nope.dens.json"),
                      "--curve", curve_path], "dens 不存在", "nope.dens.json")
        _cli_refusal(["dens", "quantify", "--dens", dens_path,
                      "--curve", os.path.join(td, "nope.curve.json")],
                     "curve 不存在", "nope.curve.json")
        _cli_refusal(["dens", "quantify", "--dens", other_dens, "--curve", curve_path],
                     "dens kind 不对", "kind")
        _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", other_curve],
                     "curve kind 不对", "kind")
        # every grammar error is owned by parse_load: one line, naming --load, never a traceback.
        # `--out` names a fresh path, so "nothing was written" is checkable rather than implied.
        for i, bad_load in enumerate(("6", "62", "6.5=2", "x=2", "6=0", "6=-2", "6=abc", "6=2u",
                                      "=2", ",6=2", "6=2,", "6=2,,7=4", "6=2,6=2.5", "6=2;7=3")):
            target = os.path.join(td, "refused-%d.quant.json" % i)
            _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                          "--load", bad_load, "--out", target],
                         "--load %r" % (bad_load,), "--load")
            assert not os.path.exists(target), \
                "被拒绝的 --load %r 不该留下输出文件：%s" % (bad_load, target)
        # a lane that is not in dens is the pure function's error; it names the lane
        unknown_target = os.path.join(td, "unknown.quant.json")
        _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                      "--load", "99=2", "--out", unknown_target], "--load 未知泳道", "99")
        assert not os.path.exists(unknown_target), unknown_target
        _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                      "--out", dens_path], "--out=dens", "--out", "输入文件")
        _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                      "--out", curve_path], "--out=curve", "--out", "输入文件")
        _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                      "--out", td], "--out=dir", "目录")
        _cli_refusal(["dens", "quantify", "--dens", dens_path, "--curve", curve_path,
                      "--out", os.path.join(td, "no", "q.json")], "--out 缺目录", "输出目录")
        # the inputs survive every refusal above unchanged
        assert densitometry.load_json(dens_path, "ep-craft-densitometry") == dens
        assert densitometry.load_json(curve_path, "ep-craft-densitometry-curve") == curve
    return True


# ---------------------------------------------------------------- 17) chain end to end

def check_chain_identity_end_to_end():
    """`measure → build_curve → quantify` on a real synthetic report (§7 items 8-9 in their
    direction-neutral form): every standard's mass is its own measured area (identity fit), so
    each in-range sample band must come back equal to its measured area; the out-of-range lane
    gets no numbers; a band-free lane does not break the run."""
    report = test_densitometry._synthetic_report([
        {"label": "M", "marker": True, "bands": [(1.0, 20.0, 0.03)]},
        {"label": "S1", "bands": [(2.0, 40.0, 0.04)]},
        {"label": "S2", "bands": [(2.0, 80.0, 0.04)]},
        {"label": "S3", "bands": [(2.0, 160.0, 0.04)]},
        {"label": "A", "bands": [(2.0, 60.0, 0.04), (4.6, 40.0, 0.04)]},
        {"label": "B", "bands": [(2.0, 200.0, 0.20)]},
        {"label": None, "bands": []},
    ])
    dens = densitometry.measure(report)
    by_lane = {lane["lane"]: lane for lane in dens["lanes"]}
    assert sorted(by_lane) == [2, 3, 4, 5, 6, 7], sorted(by_lane)  # the marker lane is skipped
    std = [(no, None, by_lane[no]["bands"][0]["area_flank"]) for no in (2, 3, 4)]
    curve = _identity_curve(dens, std, dens_path="fig.dens.json",
                            std_text=", ".join("%d=%s" % (no, by_lane[no]["bands"][0]["area_flank"])
                                               for no in (2, 3, 4)))
    quant = densitometry.quantify(dens, curve, load={5: 2.0},
                                  dens_path="fig.dens.json", curve_path="fig.curve.json",
                                  load_text="5=2")
    listed = [lane["lane"] for lane in quant["lanes"]]
    assert listed == [5, 6, 7], listed
    assert quant["source"] == {"dens": "fig.dens.json", "curve": "fig.curve.json",
                               "load": "5=2"}
    # in-range bands are recovered as their own area (the identity fit is exact at 3 dp)
    for lane in quant["lanes"]:
        for band in lane["bands"]:
            truth = by_lane[lane["lane"]]["bands"][band["index"] - 1]["area_flank"]
            if band["out_of_range"]:
                assert band["mass"] is None, (lane["lane"], band)
                continue
            assert band["mass"] == truth, (lane["lane"], band, truth)
    # lane 6's broad band is above volume_max: no mass, no total, no concentration
    big = _lane_of(quant, 6)
    assert big["out_of_range"] is True and big["out_of_range_bands"] == [1], big
    assert big["mass_total"] is None and big["conc"] is None, big
    assert any("超出标曲范围" in note for note in quant["notes"])
    # lane 5: two in-range bands, a load, a concentration and canonical shares
    lane5 = _lane_of(quant, 5)
    assert lane5["load_ul"] == 2.0 and abs(lane5["conc"] - lane5["mass_total"] / 2.0) <= 0.001
    assert abs(sum(b["rel_pct"] for b in lane5["bands"]) - 100.0) <= 0.05, lane5
    assert abs(sum(lane5["bands"][i]["rel_pct"] for i in (0, 1)) - 100.0) <= 0.05
    # the band-free lane is reported without breaking the table or the terminal output
    band_free = _lane_of(quant, 7)
    assert band_free["bands"] == [] and band_free["mass_total"] in (0.0, None), band_free
    text = _formatted(quant, curve)[0]
    assert "None" not in text and text.startswith("[定量]"), text[:80]
    assert any(re.search(r"泳道\s*7(?!\d)", line) for line in text.splitlines())
    # overall_mass is the sum of the known totals (a band-free lane contributes nothing)
    known = [ln["mass_total"] for ln in quant["lanes"] if _known(ln)]
    assert abs(quant["overall_mass"] - sum(known)) <= 0.001, (quant["overall_mass"], known)
    return True



# ---------------------------------------------------------------- real-fit recovery
#
# The §7 items 8-9 / ticket selftest 1 recovery assertions on a NON-identity fit: the standards
# are built so the truth is `mass = 2 + 8·area` (linear) or `mass = 2·area^1.5` (power), and a
# sample band must come back within 1 %.  This asserts the lead's FORWARD ruling (§2.6 / §4.5 /
# §7.8-9): `quantify` evaluates the stored fit at the band's area.  The literal "inverse" reading
# (§4.6, and the formula the ticket originally carried: `(area - intercept)/slope`) would return
# something else entirely, so a failure here means the implementation read the fit backwards.

def check_recovery_linear_nonidentity():
    """Linear truth `mass = 2 + 8·area` recovered within 1 % on both baseline columns."""
    areas = (2.0, 4.0, 8.0)
    truth_area = 5.0
    for baseline, factor in (("flank", 1.0), ("global", 2.0)):
        masses = tuple(2.0 + 8.0 * (a * factor) for a in areas)
        lanes = [_lane(no, "S%d" % no,
                       [_band(1, 2.0, 45.0, a, a * factor, strongest=True)])
                 for no, a in zip((1, 2, 3), areas)]
        lanes.append(_lane(4, "A", [_band(1, 2.0, 45.0, truth_area, truth_area * factor,
                                    strongest=True)]))
        dens = _dens(lanes)
        curve = densitometry.build_curve(
            dens, [(no, None, m) for no, m in zip((1, 2, 3), masses)],
            baseline=baseline, dens_path="fig.dens.json",
            std_text="1=%g,2=%g,3=%g" % masses)
        params = curve["fit_params"]
        assert params["slope"] == 8.0 and params["intercept"] == 2.0, \
            "fixture (%s): the linear truth 2 + 8·area was not recovered: %r" % (baseline, params)
        quant = densitometry.quantify(dens, curve)
        band = _lane_of(quant, 4)["bands"][0]
        assert band["area"] == truth_area * factor, (baseline, band)
        truth = 2.0 + 8.0 * (truth_area * factor)
        assert abs(band["mass"] - truth) <= truth * 0.01, \
            "在范围内的质量该是 %.3f（真值 2 + 8·面积，%s 列），收到 %r" % (
                truth, baseline, band["mass"])
    return True


def check_recovery_power_nonidentity():
    """Power truth `mass = 2·area^1.5` recovered within 1 % (fitted in log10, evaluated forwards)."""
    areas = (2.0, 4.0, 8.0)
    masses = tuple(2.0 * a ** 1.5 for a in areas)
    lanes = [_lane(no, "S%d" % no, [_band(1, 2.0, 45.0, a, strongest=True)])
             for no, a in zip((1, 2, 3), areas)]
    lanes.append(_lane(4, "A", [_band(1, 2.0, 45.0, 5.0, strongest=True)]))
    dens = _dens(lanes)
    curve = densitometry.build_curve(
        dens, [(no, None, m) for no, m in zip((1, 2, 3), masses)],
        fit="power", dens_path="fig.dens.json",
        std_text="1=%r,2=%r,3=%r" % masses)
    params = curve["fit_params"]
    assert abs(params["exponent"] - 1.5) <= 0.0005 and abs(params["scale"] - 2.0) <= 0.0005, \
        "fixture: the power truth 2·area^1.5 was not recovered: %r" % (params,)
    assert params["space"] == "log10" and params["n"] == 3, params
    quant = densitometry.quantify(dens, curve)
    band = _lane_of(quant, 4)["bands"][0]
    truth = 2.0 * 5.0 ** 1.5
    assert abs(band["mass"] - truth) <= truth * 0.01, \
        "在范围内的质量该是 %.3f（真值 2·面积^1.5），收到 %r" % (truth, band["mass"])
    return True


# ---------------------------------------------------------------- 18) zero area under a power fit

def check_power_zero_area_out_of_range():
    """An area of 0 under a power fit is out of range (`range.volume_min` > 0): `mass: null`, the
    band in `out_of_range_bands`, the lane's total and concentration null, and no `0^exponent`
    crash or invented number."""
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 4.0, strongest=True)]),
        _lane(3, "S3", [_band(1, 2.0, 45.0, 8.0, strongest=True)]),
        _lane(4, "ZERO", [_band(1, 2.0, 45.0, 0.0, strongest=True)]),
    ])
    curve = _identity_curve(dens, [(1, None, 2.0), (2, None, 4.0), (3, None, 8.0)],
                            fit="power", dens_path="fig.dens.json", std_text="1=2,2=4,3=8")
    assert curve["range"] == {"volume_min": 2.0, "volume_max": 8.0,
                              "mass_min": 2.0, "mass_max": 8.0}, curve["range"]
    quant = densitometry.quantify(dens, curve)
    lane = _lane_of(quant, 4)
    band = lane["bands"][0]
    assert band["area"] == 0.0, band
    assert band["out_of_range"] is True, band
    assert band["mass"] is None, ("0 ^ exponent 不是质量问题，而是超范围：%r" % (band,))
    assert band["rel_pct"] is None, band
    assert lane["mass_total"] is None and lane["conc"] is None, lane
    assert lane["out_of_range"] is True and lane["out_of_range_bands"] == [1], lane
    notes = [note for note in quant["notes"] if "超出标曲范围" in note]
    assert len(notes) == 1 and "泳道 4" in notes[0], quant["notes"]
    assert "多加点" in notes[0], notes[0]
    text = _formatted(quant, curve)[0]
    assert text.startswith("[定量]") and "None" not in text, text[:80]
    line = next(ln for ln in text.splitlines() if re.search(r"泳道\s*4(?!\d)", ln))
    assert "—" in line and "超出标曲范围" in line, line
    return True


# ---------------------------------------------------------------- hand-written curve helpers
#
# Two of the later rulings describe states `build_curve` cannot produce: a stored fit that crosses
# zero inside its own standard range, and a standard range whose top is 4.484.  Those fixtures are
# therefore written out by hand in the §4.5 shape (the shape `build_curve` emits, not a new one);
# only the fields `quantify` reads (`points[*].entries`, `range`, `fit_params`) carry meaning.

def _curve_doc(entries, fit_params, volume_min, volume_max, mass_min, mass_max,
               fit="linear", baseline="flank", unit="ug"):
    """A hand-written curve.json (spec §4.5): one used point per `(lane, volume, mass)`."""
    points = []
    for lane, volume, mass in entries:
        points.append({"mass": mass, "volume": volume, "n": 1, "sd": 0.0, "cv_pct": 0.0,
                       "lanes": [lane], "entries": [[lane, None]], "used": True})
    return {"version": 1, "kind": "ep-craft-densitometry-curve", "unit": unit,
            "baseline": baseline, "fit": fit,
            "source": {"dens": "hand.dens.json", "std": "", "drop": []},
            "points": points, "fit_params": dict(fit_params),
            "range": {"volume_min": volume_min, "volume_max": volume_max,
                      "mass_min": mass_min, "mass_max": mass_max},
            "residuals": [], "notes": []}


def _sig_digits(text):
    """Significant digits of a decimal number as printed (leading zeros are not significant)."""
    digits = text.replace(".", "").lstrip("0")
    return len(digits)


# ---------------------------------------------------------------- 21) non-positive mass

def check_nonpositive_mass_null():
    """A ruling on a fit that crosses zero inside its own range: an in-range band whose stored fit
    yields a mass <= 0 gets `mass: null` + `rel_pct: null` but `out_of_range: false`; its lane's
    `mass_total` / `conc` / `frac_pct_among_lanes` go null; `overall_mass` leaves that lane out;
    exactly one note names the lane and the band; no JSON key is added anywhere."""
    # hand-written curve: mass = -100 + 8·area, so the fit crosses zero at area 12.5 — INSIDE the
    # standard range 1 .. 20.  Lanes 3 (area 15) and 4 (area 13) land above the crossing (20 and 4
    # ug), lane 2 (area 2) lands below it (-84 ug).
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 1.0, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 20.0, strongest=True)]),
        _lane(3, "NEG", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
        _lane(4, "POS1", [_band(1, 2.0, 45.0, 15.0, strongest=True)]),
        _lane(5, "POS2", [_band(1, 2.0, 45.0, 13.0, strongest=True)]),
    ])
    # both recorded standards sit on the positive side (4 ug at 13, 60 ug at 20); the range is
    # hand-widened down to 1 so that the recorded line crosses zero at area 12.5, inside it
    curve = _curve_doc([(1, 13.0, 4.0), (2, 20.0, 60.0)],
                       {"slope": 8.0, "intercept": -100.0, "r2": 1.0, "n": 2,
                        "space": "linear"},
                       1.0, 20.0, -92.0, 60.0)
    # the fixture's own arithmetic: the stored fit at area 2 is -100 + 8·2 = -84 < 0
    assert curve["fit_params"]["intercept"] + curve["fit_params"]["slope"] * 2.0 == -84.0
    quant = densitometry.quantify(dens, curve, load={3: 2.0, 4: 2.0, 5: 2.0},
                                  load_text="3=2,4=2,5=2")

    # no new JSON keys anywhere: the null state is carried by the existing fields only
    assert set(quant) == TOP_KEYS, sorted(quant)
    for lane in quant["lanes"]:
        assert set(lane) == LANE_KEYS, sorted(lane)
        for band in lane["bands"]:
            assert set(band) == BAND_KEYS, sorted(band)

    neg = _lane_of(quant, 3)
    band = _band_of(neg, 1)
    assert band["area"] == 2.0, band
    assert band["out_of_range"] is False, "质量非正不是超范围：%r" % (band,)
    assert band["mass"] is None, "拟合给出 -84 ug，该是 null：%r" % (band,)
    assert band["rel_pct"] is None, band
    assert neg["out_of_range"] is False and neg["out_of_range_bands"] == [], neg
    assert neg["mass_total"] is None, neg
    assert neg["conc"] is None, "合计为 null 时浓度也该是 null：%r" % (neg,)
    assert neg["frac_pct_among_lanes"] is None, neg

    # the positive lanes are untouched and carry the whole overall mass (24 = 20 + 4)
    pos1 = _lane_of(quant, 4)
    pos2 = _lane_of(quant, 5)
    assert abs(pos1["bands"][0]["mass"] - 20.0) <= 0.0005, pos1
    assert abs(pos1["mass_total"] - 20.0) <= 0.0005, pos1
    assert abs(pos1["conc"] - 10.0) <= 0.0005, pos1
    assert abs(pos2["bands"][0]["mass"] - 4.0) <= 0.0005, pos2
    assert abs(pos2["mass_total"] - 4.0) <= 0.0005, pos2
    assert abs(quant["overall_mass"] - 24.0) <= 0.001, quant["overall_mass"]
    assert abs(pos1["frac_pct_among_lanes"] - 83.33) <= 0.011, pos1
    assert abs(pos2["frac_pct_among_lanes"] - 16.67) <= 0.011, pos2
    assert abs(pos1["frac_pct_among_lanes"] + pos2["frac_pct_among_lanes"] - 100.0) <= 0.11
    for lane in quant["lanes"]:
        for band in lane["bands"]:
            assert band["mass"] is None or band["mass"] > 0.0, \
                "非正的质量不该作为数出现在 quant 里：%r" % (band,)

    # exactly one note about it (plus the skip note), naming the lane and the band.  ep-c1i §4 adds
    # a second unrelated note here — this curve is hand-written with n == 2, so it carries no
    # residual scatter and says so once; that note is about the calibration, not about lane 3.
    skip = [note for note in quant["notes"] if "跳过" in note]
    scatter = [note for note in quant["notes"] if _is_no_scatter_note(note)]
    others = [note for note in quant["notes"]
              if "跳过" not in note and not _is_no_scatter_note(note)]
    assert len(skip) == 1, quant["notes"]
    assert len(scatter) == 1, quant["notes"]
    assert len(others) == 1, quant["notes"]
    _single_line(others[0], "质量非正的注")
    assert re.search(r"泳道\s*3(?!\d)", others[0]), others[0]
    assert re.search(r"带\s*1(?!\d)", others[0]), others[0]

    # the terminal block: no invented number, no negative one, no None
    text = _formatted(quant, curve)[0]
    assert "None" not in text, text
    assert "-84" not in text and "-" + "84" + "0" not in text, text
    return True


# ---------------------------------------------------------------- 22) terminal precision

def check_terminal_precision():
    """A ruling on the printed masses: `mass` and `conc` show up to 3 decimals with trailing zeros
    stripped and at least one decimal kept, so 0.522 / 0.075 / 5.0 stay distinguishable."""
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 0.075, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 5.0, strongest=True)]),
        _lane(3, "P1", [_band(1, 2.0, 45.0, 0.522, strongest=True)]),
        _lane(4, "P2", [_band(1, 2.0, 45.0, 0.075, strongest=True)]),
        _lane(5, "P3", [_band(1, 2.0, 45.0, 5.0, strongest=True)]),
    ])
    curve = _identity_curve(dens, [(1, None, 0.075), (2, None, 5.0)],
                            dens_path="fig.dens.json", std_text="1=0.075,2=5")
    assert curve["range"]["volume_min"] == 0.075 and curve["range"]["volume_max"] == 5.0
    quant = densitometry.quantify(dens, curve, load={3: 1.0, 4: 1.0, 5: 1.0},
                                  load_text="3=1,4=1,5=1")
    for no, mass in ((3, 0.522), (4, 0.075), (5, 5.0)):
        band = _lane_of(quant, no)["bands"][0]
        assert abs(band["mass"] - mass) <= 0.0005, (no, band)
        assert abs(_lane_of(quant, no)["conc"] - mass) <= 0.0005, (no, _lane_of(quant, no))

    text = _formatted(quant, curve)[0]
    lines = {}
    for line in text.splitlines():
        found = re.match(r"\s*泳道\s*(\d+)", line)
        if found:
            lines[int(found.group(1))] = line
    # 0.522 and 0.075 must print differently: at 1 dp both would collapse to 0.5 / 0.1
    assert re.search(r"0\.522\s+ug", lines[3]), lines[3]
    assert re.search(r"0\.075\s+ug", lines[4]), lines[4]
    assert not re.search(r"(?<![0-9.])0\.5\s+ug", lines[3]), \
        "0.522 不该被印成 0.5：%r" % (lines[3],)
    assert not re.search(r"(?<![0-9.])0\.1\s+ug", lines[4]), \
        "0.075 不该被印成 0.1：%r" % (lines[4],)
    # … and 5.0 keeps one decimal instead of losing the dot altogether
    assert re.search(r"(?<![0-9.])5\.0\s+ug", lines[5]), lines[5]
    assert not re.search(r"(?<![0-9.])5\s+ug", lines[5]), "5.0 该印成 5.0 而不是 5：%r" % (lines[5],)
    # no trailing-zero noise and no 4th decimal anywhere on those lines
    for no in (3, 4, 5):
        assert not re.search(r"\.[0-9]{4}", lines[no]), lines[no]
    # the totals and the concentrations follow the same rule
    for no, token in ((3, "0.522"), (4, "0.075"), (5, "5.0")):
        assert re.search(r"合计\s*%s\s+ug" % re.escape(token), lines[no]), lines[no]
        assert re.search(r"浓度\s*%s\s*ug/uL" % re.escape(token), lines[no]), lines[no]
    assert "None" not in text, text
    return True


# ---------------------------------------------------------------- 23) out-of-range line: 3 sig digits

def check_out_of_range_three_sig():
    """A ruling on the out-of-range lane line: the area and the violated bound print with 3
    significant digits, so area 4.495 against a maximum of 4.484 cannot collapse into
    `4.5 > 4.5`."""
    dens = _dens([
        _lane(1, "S1", [_band(1, 2.0, 45.0, 1.0, strongest=True)]),
        _lane(2, "S2", [_band(1, 2.0, 45.0, 4.484, strongest=True)]),
        _lane(3, "HI", [_band(1, 2.0, 45.0, 4.495, strongest=True)]),
    ])
    curve = _curve_doc([(1, 1.0, 1.0), (2, 4.484, 4.484)],
                       {"slope": 1.0, "intercept": 0.0, "r2": 1.0, "n": 2, "space": "linear"},
                       1.0, 4.484, 1.0, 4.484)
    quant = densitometry.quantify(dens, curve)
    lane = _lane_of(quant, 3)
    assert lane["out_of_range"] is True and lane["out_of_range_bands"] == [1], lane
    assert _band_of(lane, 1)["mass"] is None, lane

    text, has_curve = _formatted(quant, curve)
    assert has_curve, "这条裁决要求终端行印出标曲上界，format_quant 必须能拿到 curve"
    line = next(ln for ln in text.splitlines() if re.search(r"泳道\s*3(?!\d)", ln))
    assert "超出标曲范围" in line, line
    assert "4.5 > 4.5" not in line, "面积 4.495 与上界 4.484 不该都印成 4.5：%r" % (line,)
    found = re.search(r"面积\s*([0-9]+(?:\.[0-9]+)?)\s*([<>])\s*([0-9]+(?:\.[0-9]+)?)", line)
    assert found, "超范围的终端行该带着 面积 X <|> Y 的比较：%r" % (line,)
    area_text, sign, bound_text = found.group(1), found.group(2), found.group(3)
    assert sign == ">", line
    # the whole point of the ruling: the two numbers must stay distinguishable at the printed
    # precision (the failure this replaces printed `4.5 > 4.5`)
    assert area_text != bound_text, "面积与上界印成了同一个数：%r" % (line,)
    # three significant digits = half a unit in the third digit: at this magnitude an error of at
    # most 0.005.  An implementation may strip the trailing zero of 4.50 into `4.5` (the printed
    # value is still that close and the comparison still reads correctly), so only ONE of the two
    # numbers has to show three digits; both would have to be printed more coarsely than that
    # before the `4.5 > 4.5` failure could come back.
    assert max(_sig_digits(area_text), _sig_digits(bound_text)) >= 3, \
        "面积与上界该按 3 位有效数字印出：%r" % (line,)
    assert abs(float(area_text) - 4.495) <= 0.005 + 1e-9, (area_text, line)
    assert abs(float(bound_text) - 4.484) <= 0.005 + 1e-9, (bound_text, line)
    assert float(area_text) > float(bound_text), (area_text, bound_text, line)
    return True


# ---------------------------------------------------------------- ep-c1i §4: fit statistics helpers
#
# These helpers compute the expected OLS statistics HERE, with plain arithmetic, so this suite can
# disagree with `densitometry._linfit`.  Nothing below imports a production formula helper.

def _ols(xs, ys):
    """Plain-arithmetic OLS with the stats ep-c1i §2 defines (full precision, no production code).

    `slope_sd`, `intercept_sd` and `cov` are `None` when n <= 2, exactly like the stored ones; the
    caller decides whether that absence is acceptable at its precision (4 dp for curve.json).
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
        slope_sd, intercept_sd = sd / math.sqrt(sxx), sd * math.sqrt(1.0 / n + mx * mx / sxx)
        cov = -sd * sd * mx / sxx
    else:
        sd = slope_sd = intercept_sd = cov = None
    return {"n": n, "mean_x": mx, "sxx": sxx, "slope": slope, "intercept": intercept,
            "r2": r2, "residual_sd": sd, "slope_sd": slope_sd,
            "intercept_sd": intercept_sd, "cov": cov}


def _is_no_scatter_note(note):
    """True for the ONE note ep-c1i §4 writes when the calibration has no scatter to propagate."""
    text = str(note)
    return "残差散布" in text or "不确定度不给数" in text or "没有可用的残差" in text


def _stats_from_params(params):
    """The stored (rounded) statistics as the numbers the propagation formula must use."""
    return {"n": params["n"], "mx": float(params["mean_x"]),
            "s": float(params["residual_sd"]), "sxx": float(params["sxx"])}


def _linear_prediction_sd(params, area):
    """`s · sqrt(1 + 1/n + (x − x̄)²/sxx)` from the STORED (4 dp) linear fit_params."""
    stats = _stats_from_params(params)
    return stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"]
                                  + (float(area) - stats["mx"]) ** 2 / stats["sxx"])


def _power_prediction_sd(params, area, mass):
    """`mass · ln(10) · s · sqrt(1 + 1/n + (u − ū)²/sxx)` from the stored log-space fit_params."""
    stats = _stats_from_params(params)
    u = math.log10(float(area))
    sd_log = stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"]
                                    + (u - stats["mx"]) ** 2 / stats["sxx"])
    return float(mass) * math.log(10.0) * sd_log


def _assert_3dp(actual, expected, what):
    """One propagated number: stored at 3 dp, so half a unit in the third decimal is allowed."""
    assert _number(actual) and _decimals(actual) <= 3, \
        "%s 该是 3 位小数的数（收到 %r）" % (what, actual)
    assert abs(actual - float(expected)) <= _TOL_3DP, \
        "%s = %r，独立手算为 %r（差 %.6g）" % (what, actual, float(expected),
                                              abs(actual - float(expected)))


def _propagation_fixture(fit="linear"):
    """Standards + samples for the ep-c1i §4 checks: a real `build_curve` fit with residual scatter.

    The statistics are whatever `build_curve` stores (the digits come from the curve, not from
    this file); every expected number below is then re-computed from THOSE stored values by `_ols`,
    so the check is about propagation, not about the fit.
    """
    rows = ((1, 1.0, 3.0), (2, 2.0, 5.0), (3, 4.0, 11.0), (4, 8.0, 19.0), (5, 12.0, 31.0)) \
        if fit == "linear" else \
        ((1, 1.0, 2.0), (2, 2.0, 5.0), (3, 4.0, 9.0), (4, 8.0, 21.0), (5, 16.0, 38.0))
    far = 6.5 if fit == "linear" else 6.0
    mid = 10.0 if fit == "linear" else 13.0
    edge = 1.0 if fit == "linear" else 0.5
    std_lanes = [_lane(no, "S%d" % no, [_band(1, 2.0, 45.0, area, strongest=True)])
                 for no, area, _mass in rows]
    samples = [_lane(6, "A", [_band(1, 2.0, 45.0, 3.0), _band(2, 4.6, None, far)]),
               _lane(7, "B", [_band(1, 2.0, 45.0, mid, strongest=True)]),
               _lane(8, "C", [_band(1, 2.0, 45.0, edge), _band(2, 4.6, None, 12.0)])]
    dens = _dens(std_lanes + samples)
    curve = densitometry.build_curve(dens, [(no, None, mass) for no, _area, mass in rows],
                                     fit=fit, baseline="flank", dens_path="fig.dens.json",
                                     std_text=", ".join("%d=%g" % (no, mass)
                                                        for no, _area, mass in rows))
    return dens, curve


# ---------------------------------------------------------------- ep-c1i §4: propagation

def check_uncertainty_block():
    """ep-c1i §4: quant.json says what its own numbers are — `{"basis": "ols_prediction",
    "k": 1, "unit": <curve unit>}`, with exactly those three keys and the curve's unit copied."""
    for fit in ("linear", "power"):
        dens, curve = _propagation_fixture(fit)
        quant = densitometry.quantify(dens, curve)
        assert "uncertainty" in quant, sorted(quant)
        unc = quant["uncertainty"]
        assert isinstance(unc, dict), unc
        assert set(unc) == UNCERTAINTY_KEYS, sorted(unc)
        assert unc["basis"] == UNCERTAINTY_BASIS, unc
        assert unc["k"] == 1 and not isinstance(unc["k"], bool), unc
        assert unc["unit"] == curve["unit"], (unc, curve["unit"])
        # no NaN / Infinity token: a JSON reader must be able to load this file
        raw = json.dumps(quant)
        for token in ("NaN", "Infinity"):
            assert token not in raw, "quant.json 里出现了 %r" % (token,)
    # the unit follows the curve, not a hard-coded "ug"
    dens, curve = _propagation_fixture()
    curve_ng = dict(curve, unit="ng")
    quant = densitometry.quantify(dens, curve_ng)
    assert quant["uncertainty"]["unit"] == "ng", quant["uncertainty"]
    return True


def check_mass_sd_linear():
    """ep-c1i §4: a linear in-range band's `mass_sd` is the OLS PREDICTION SD
    `s · sqrt(1 + 1/n + (x − x̄)²/sxx)`, re-computed here from the stored fit_params.

    The leading `1` is the whole difference between a prediction interval and a confidence band: a
    suite that only checked "some positive number" would pass a fit-mean interval that shrinks to
    zero at x̄.  Both formulas are therefore asserted apart, and the prediction SD must stay above
    the fit-mean SD everywhere.
    """
    dens, curve = _propagation_fixture("linear")
    params = curve["fit_params"]
    reference = _ols([1.0, 2.0, 4.0, 8.0, 12.0], [3.0, 5.0, 11.0, 19.0, 31.0])
    # the fixture is only meaningful when the stored statistics are the ones we hand-computed
    for key, expected in (("mean_x", reference["mean_x"]), ("sxx", reference["sxx"]),
                          ("residual_sd", reference["residual_sd"]),
                          ("slope", reference["slope"]), ("intercept", reference["intercept"])):
        assert abs(params[key] - expected) <= 5.1e-5, (key, params[key], expected)
    quant = densitometry.quantify(dens, curve, load={6: 2.0, 7: 2.0}, load_text="6=2,7=2")
    assert set(quant["uncertainty"]) == UNCERTAINTY_KEYS, quant["uncertainty"]

    for lane in quant["lanes"]:
        for band in lane["bands"]:
            if band["mass"] is None:
                assert band["mass_sd"] is None, band
                continue
            expected = _linear_prediction_sd(params, band["area"])
            _assert_3dp(band["mass_sd"], expected, "泳道 %d 带 %d 的 mass_sd"
                        % (lane["lane"], band["index"]))
            # the prediction SD can never be smaller than the fit-mean SD at the same x: a
            # implementation that dropped the leading `1` would land on the smaller one
            fit_mean = float(params["residual_sd"]) * math.sqrt(
                (float(band["area"]) - float(params["mean_x"])) ** 2 / float(params["sxx"]))
            assert band["mass_sd"] > fit_mean, (band["area"], band["mass_sd"], fit_mean)

    # the SD is smallest near x̄ and grows towards both ends of the range — the shape a pure
    # slope/intercept error would NOT have (it is largest at the ends and never has a `1`)
    lane6 = _lane_of(quant, 6)
    near = _band_of(lane6, 2)["mass_sd"]
    far = _band_of(lane6, 1)["mass_sd"]
    assert far != near, (lane6,)
    centre = _band_of(_lane_of(quant, 7), 1)["mass_sd"]
    assert centre > 0.0 and near > 0.0, (centre, near)
    # the highest x in the range (area 12) carries the largest SD of the evaluated samples
    widest = _band_of(_lane_of(quant, 8), 2)["mass_sd"]
    assert widest > centre, (widest, centre)
    return True


def check_mass_sd_power():
    """ep-c1i §4: a power band's `mass_sd` is
    `mass · ln(10) · s · sqrt(1 + 1/n + (u − ū)²/sxx)` with `u = log10(area)`, i.e. the log-space
    prediction SD mapped onto the mass scale by the same delta method `scale_sd` uses."""
    dens, curve = _propagation_fixture("power")
    params = curve["fit_params"]
    reference = _ols([math.log10(v) for v in (1.0, 2.0, 4.0, 8.0, 16.0)],
                     [math.log10(m) for m in (2.0, 5.0, 9.0, 21.0, 38.0)])
    assert params["space"] == "log10", params
    for key, expected in (("mean_x", reference["mean_x"]), ("sxx", reference["sxx"]),
                          ("residual_sd", reference["residual_sd"]),
                          ("exponent", reference["slope"]), ("scale", 10 ** reference["intercept"])):
        assert abs(params[key] - expected) <= 5.1e-5, (key, params[key], expected)
    quant = densitometry.quantify(dens, curve, load={6: 2.0, 7: 3.0}, load_text="6=2,7=3")

    for lane in quant["lanes"]:
        for band in lane["bands"]:
            if band["mass"] is None:
                assert band["mass_sd"] is None, band
                continue
            expected = _power_prediction_sd(params, band["area"], band["mass"])
            _assert_3dp(band["mass_sd"], expected, "泳道 %d 带 %d 的 mass_sd"
                        % (lane["lane"], band["index"]))
            # without the ln(10) the number would be ~2.3× too small; require the factor
            stats = _stats_from_params(params)
            u = math.log10(float(band["area"]))
            sd_log = stats["s"] * math.sqrt(1.0 + 1.0 / stats["n"]
                                            + (u - stats["mx"]) ** 2 / stats["sxx"])
            assert abs(band["mass_sd"] - float(band["mass"]) * sd_log) > 1e-6, \
                "power 的 mass_sd 少了 delta 方法的 ln(10) 因子：%r" % (band,)

    # the log-space SD is nearly uniform, so the mass SD scales with the mass itself (delta
    # method): between the smallest and the largest in-range area the SD ratio must track the mass
    # ratio, not stay constant (a log-space number leaked into the mass column would not)
    small = _band_of(_lane_of(quant, 6), 1)
    big = _band_of(_lane_of(quant, 7), 1)
    assert small["mass_sd"] is not None and big["mass_sd"] is not None, (small, big)
    ratio_mass = big["mass"] / small["mass"]
    ratio_sd = big["mass_sd"] / small["mass_sd"]
    assert 0.6 <= ratio_sd / ratio_mass <= 1.6, \
        "power 的 mass_sd 该随质量一起放大（delta 方法）：mass ×%.2f 而 SD ×%.2f" % (
            ratio_mass, ratio_sd)
    return True


def check_lane_uncertainty_sums():
    """ep-c1i §4: `mass_total_sd` is the ROOT SUM OF SQUARES of the band SDs (independent errors),
    `conc_sd` is that total SD over the load, and both are None exactly where their point
    estimate is None."""
    dens, curve = _propagation_fixture("linear")
    quant = densitometry.quantify(dens, curve, load={6: 2.0}, load_text="6=2")
    for lane in quant["lanes"]:
        sds = [b["mass_sd"] for b in lane["bands"] if b["mass"] is not None]
        if lane["mass_total"] is None:
            assert lane["mass_total_sd"] is None, lane
            assert lane["conc_sd"] is None, lane
            continue
        assert len(sds) == len([b for b in lane["bands"] if b["mass"] is not None]), lane
        expected = math.sqrt(sum(sd * sd for sd in sds))
        _assert_3dp(lane["mass_total_sd"], expected, "泳道 %d 的 mass_total_sd" % lane["lane"])
        # not the sum, and not the maximum: either mistake is a plausible implementation bug
        assert abs(lane["mass_total_sd"] - sum(sds)) > _TOL_3DP or len(sds) == 1, lane
        if len(sds) > 1:
            assert lane["mass_total_sd"] < sum(sds), lane
            assert lane["mass_total_sd"] > max(sds), lane
        if lane["load_ul"] is None or lane["conc"] is None:
            assert lane["conc_sd"] is None, lane
        else:
            _assert_3dp(lane["conc_sd"], lane["mass_total_sd"] / lane["load_ul"],
                        "泳道 %d 的 conc_sd" % lane["lane"])
            # the concentration SD is the mass SD divided by the load, so it is always SMALLER
            # than the mass total SD for a load above 1 uL and equal only at exactly 1 uL
            if lane["load_ul"] > 1.0:
                assert lane["conc_sd"] < lane["mass_total_sd"], lane
    # lane 8 has two bands and no load: total SD yes, concentration SD no
    lane8 = _lane_of(quant, 8)
    assert lane8["mass_total"] is not None and lane8["mass_total_sd"] is not None, lane8
    assert lane8["load_ul"] is None and lane8["conc"] is None and lane8["conc_sd"] is None, lane8
    # loading lane 8 makes conc_sd appear, still the total SD over the load
    loaded = densitometry.quantify(dens, curve, load={6: 2.0, 8: 4.0}, load_text="6=2,8=4")
    lane8b = _lane_of(loaded, 8)
    assert lane8b["conc"] is not None and lane8b["conc_sd"] is not None, lane8b
    _assert_3dp(lane8b["conc_sd"], lane8b["mass_total_sd"] / 4.0, "泳道 8 加载后的 conc_sd")
    return True


def check_out_of_range_and_nonpositive_have_no_sd():
    """ep-c1i §4: a band that got no mass (out of range, or a non-positive fitted value) has
    `mass_sd: null`, and its lane's total SD / concentration SD follow the total's null."""
    dens, curve = _propagation_fixture("linear")
    # lane 9 is 20 % below volume_min, lane 10 is 20 % above volume_max
    dens["lanes"].append(_lane(9, "LOW", [_band(1, 2.0, 45.0, 0.5, strongest=True)]))
    dens["lanes"].append(_lane(10, "HIGH", [_band(1, 2.0, 45.0, 15.0, strongest=True)]))
    # lane 11 pairs one out-of-range band with one in-range one: the total cannot be trusted, so
    # neither the total SD nor the concentration SD may appear
    dens["lanes"].append(_lane(11, "MIX", [_band(1, 2.0, 45.0, 0.5),
                                           _band(2, 4.6, None, 3.0)]))
    quant = densitometry.quantify(dens, curve, load={9: 2.0, 10: 2.0, 11: 2.0},
                                  load_text="9=2,10=2,11=2")
    for no in (9, 10):
        lane = _lane_of(quant, no)
        assert lane["out_of_range"] is True and lane["mass_total"] is None, lane
        assert lane["mass_total_sd"] is None and lane["conc_sd"] is None, lane
        for band in lane["bands"]:
            assert band["out_of_range"] is True and band["mass"] is None, band
            assert band["mass_sd"] is None, band
    mixed = _lane_of(quant, 11)
    assert mixed["mass_total"] is None and mixed["mass_total_sd"] is None, mixed
    assert mixed["conc"] is None and mixed["conc_sd"] is None, mixed
    # the in-range half of the mixed lane still carries its own SD (per band, not per lane)
    in_range = _band_of(mixed, 2)
    assert in_range["mass"] is not None and in_range["mass_sd"] is not None, in_range
    _assert_3dp(in_range["mass_sd"], _linear_prediction_sd(curve["fit_params"],
                                                           in_range["area"]),
                "混合泳道里在范围内那一条带的 mass_sd")

    # a NON-POSITIVE fitted value is the third no-mass case: with usable statistics present the
    # band still gets no SD (there is no mass to attach it to), and its lane's total SD is null
    # even though a sibling band in the same lane is perfectly in range.
    crossing = {1: (13.0, 4.0), 2: (20.0, 60.0)}
    neg = _dens([_lane(1, "S1", [_band(1, 2.0, 45.0, 13.0, strongest=True)]),
                 _lane(2, "S2", [_band(1, 2.0, 45.0, 20.0, strongest=True)]),
                 _lane(3, "NEG", [_band(1, 2.0, 45.0, 2.0, strongest=True)]),
                 _lane(4, "POS", [_band(1, 2.0, 45.0, 15.0, strongest=True)])])
    # mass = -100 + 8·area crosses zero at 12.5, inside the hand-widened range 1 .. 20
    crossing_curve = _curve_doc([(1, crossing[1][0], crossing[1][1]),
                                 (2, crossing[2][0], crossing[2][1])],
                                {"slope": 8.0, "intercept": -100.0, "r2": 1.0, "n": 5,
                                 "space": "linear", "residual_sd": 0.5, "sxx": 30.0,
                                 "mean_x": 16.0},
                                1.0, 20.0, -92.0, 60.0)
    neg_quant = densitometry.quantify(neg, crossing_curve, load={3: 2.0, 4: 2.0},
                                      load_text="3=2,4=2")
    neg_lane = _lane_of(neg_quant, 3)
    assert neg_lane["bands"][0]["mass"] is None and neg_lane["bands"][0]["mass_sd"] is None, \
        neg_lane
    assert neg_lane["mass_total"] is None and neg_lane["mass_total_sd"] is None, neg_lane
    assert neg_lane["conc"] is None and neg_lane["conc_sd"] is None, neg_lane
    # the positive lane in the same run keeps its SD, at the hand-computed prediction value
    pos_lane = _lane_of(neg_quant, 4)
    assert pos_lane["mass_total_sd"] is not None, pos_lane
    _assert_3dp(pos_lane["bands"][0]["mass_sd"],
                _linear_prediction_sd(crossing_curve["fit_params"],
                                      pos_lane["bands"][0]["area"]),
                "跨零拟合里仍在正侧那条带的 mass_sd")
    return True


def check_no_residual_sd_withholds_everything():
    """ep-c1i §4: a hand-written curve.json without `residual_sd` (and an n == 2 curve from
    `build_curve`) propagates NOTHING: every `mass_sd` / `mass_total_sd` / `conc_sd` is None and
    exactly ONE note explains why — no number is invented, and no `± 0` pretends to a perfect fit."""
    dens, curve = _propagation_fixture("linear")
    bare = _curve_doc([(1, 1.0, 3.0), (2, 2.0, 5.0), (3, 4.0, 11.0), (4, 8.0, 19.0),
                       (5, 12.0, 31.0)],
                      {"slope": curve["fit_params"]["slope"],
                       "intercept": curve["fit_params"]["intercept"],
                       "r2": curve["fit_params"]["r2"], "n": 5, "space": "linear"},
                      1.0, 12.0, 3.0, 31.0)
    quant = densitometry.quantify(dens, bare, load={6: 2.0}, load_text="6=2")
    for lane in quant["lanes"]:
        for band in lane["bands"]:
            assert band["mass_sd"] is None, band
        assert lane["mass_total_sd"] is None, lane
        assert lane["conc_sd"] is None, lane
    # the point estimates are untouched: only the uncertainty is withheld
    assert abs(_lane_of(quant, 6)["mass_total"] - 24.344) <= 0.0005, _lane_of(quant, 6)
    notes = [note for note in quant["notes"] if _is_no_scatter_note(note)]
    assert len(notes) == 1, quant["notes"]
    _single_line(notes[0], "无残差散布的注")
    assert "残差散布" in notes[0] and "不给数" in notes[0], notes[0]
    skip = [note for note in quant["notes"] if "跳过" in note]
    assert len(skip) == 1, quant["notes"]
    assert len(quant["notes"]) == 2, quant["notes"]
    # the terminal output prints no "±" next to any value in this case (an unknown must not look
    # like a measured zero); the header's basis clause is the only place a ± appears
    text = _formatted(quant, bare)[0]
    lines = text.splitlines()
    assert "±" in lines[0] and "SD" in lines[0], lines[0]
    for line in lines[1:]:
        assert "±" not in line, line
    assert "None" not in text, text

    # the same rule for a genuine n == 2 curve built by `build_curve`
    two = _curve_doc([(1, 2.5, 22.0), (2, 5.0, 42.0)],
                     {"slope": 8.0, "intercept": 2.0, "r2": 1.0, "n": 2, "space": "linear",
                      "residual_sd": None, "sxx": 3.125, "mean_x": 3.75},
                     2.5, 5.0, 22.0, 42.0)
    two_quant = densitometry.quantify(dens, two)
    for lane in two_quant["lanes"]:
        for band in lane["bands"]:
            assert band["mass_sd"] is None, band
        assert lane["mass_total_sd"] is None, lane
    assert len([n for n in two_quant["notes"] if _is_no_scatter_note(n)]) == 1, two_quant["notes"]
    return True


def check_uncertainty_terminal():
    """ep-c1i §4 terminal: every known value prints its `±` SD at 3 significant digits, an unknown
    SD omits the `±` entirely, and the header gains the basis clause."""
    dens, curve = _propagation_fixture("linear")
    quant = densitometry.quantify(dens, curve, load={6: 2.0, 7: 2.0}, load_text="6=2,7=2")
    text = _formatted(quant, curve)[0]
    assert "±" in text, text
    head = text.splitlines()[0]
    # the header names the basis: one short tail clause, and the JSON block is where it comes from
    assert "±1 SD" in head, head
    assert "拟合" in head, head
    assert "None" not in text, text

    lane6 = [ln for ln in text.splitlines() if ln.startswith("  泳道  6")][0]
    band1 = _band_of(_lane_of(quant, 6), 1)
    # `7.788 ± 1.09 ug`: value first, then ±, then the SD at three significant digits, then unit
    found = re.search(r"7\.788\s*±\s*([0-9]+(?:\.[0-9]+)?)\s*ug", lane6)
    assert found, "带值的质量该印成 `值 ± SD 单位`：%r" % (lane6,)
    sd_text = found.group(1)
    assert abs(float(sd_text) - band1["mass_sd"]) <= 0.005 + 1e-9, (sd_text, band1["mass_sd"])
    # three significant digits, and never the full stored number (1.088 must not print as 1.088)
    assert float(sd_text) != band1["mass_sd"], (sd_text, band1["mass_sd"])
    assert len(sd_text.replace(".", "").lstrip("0")) <= 3, sd_text
    assert abs(float(sd_text) - band1["mass_sd"]) <= 0.005 + 1e-9, (sd_text, band1["mass_sd"])
    # the lane total and the concentration carry the same `±` shape
    assert re.search(r"合计\s*24\.344\s*±\s*[0-9.]+\s*ug", lane6), lane6
    assert re.search(r"浓度\s*12\.172\s*±\s*[0-9.]+\s*ug/uL", lane6), lane6
    # the unit follows the SD, not the value: `± 1.09 ug`, never `± 1.09` with the unit dangling.
    # (the header's `±1 SD` basis clause is not a value, so only the lane lines are scanned)
    for line in text.splitlines()[1:]:
        if not line.startswith("  泳道 "):
            continue
        for token in re.findall(r"±\s*[0-9.]+\s*(?:ug(?:/uL)?)?", line):
            assert "ug" in token, "± 后面该跟着单位：%r" % (token,)

    # the values and the SDs stay distinguishable at the printed precision
    band2 = _band_of(_lane_of(quant, 6), 2)
    found2 = re.search(r"16\.555\s*±\s*([0-9]+(?:\.[0-9]+)?)\s*ug", lane6)
    assert found2, "第二条带也带着自己的 ± 值：%r" % (lane6,)
    assert abs(float(found2.group(1)) - band2["mass_sd"]) <= 0.005 + 1e-9, (found2.group(1),
                                                                         band2["mass_sd"])
    assert found2.group(1) != sd_text, "两条带的 SD 不同，不该印成同一个数"
    # a lane without a load prints no concentration and therefore no concentration SD
    lane8 = [ln for ln in text.splitlines() if ln.startswith("  泳道  8")][0]
    assert "浓度" not in lane8, lane8
    assert "ug/uL" not in lane8, lane8
    assert re.search(r"合计\s*33\.11\s*±\s*[0-9.]+\s*ug", lane8), lane8

    # ep-c1i must not disturb the existing column alignment: the `±` sits INSIDE the last slot of
    # a line, never in the label column, so every lane's body starts at the same display width
    # (the labels here are A / B / C, i.e. already equal — the two-band and one-band lanes would
    # drift apart if the uncertainty were appended before the padding).
    widths = []
    for line in text.splitlines()[1:]:
        if not line.startswith("  泳道 "):
            continue
        found = re.search(r"带[0-9]|—|（无带）", line)
        assert found, "泳道行该有带/无带的正文块：%r" % (line,)
        widths.append(densitometry._dwidth(line[:found.start()]))
    assert len(set(widths)) == 1, "加入 ± 后各泳道行的正文起始列不再对齐：%r" % (widths,)
    return True


def check_uncertainty_json_round_trip():
    """ep-c1i §4 precision: the propagated numbers are stored at 3 dp, the block survives a
    `write_json` / `load_json` round trip, and nothing non-finite reaches the file."""
    dens, curve = _propagation_fixture("power")
    quant = densitometry.quantify(dens, curve, load={6: 2.0}, load_text="6=2")
    for lane in quant["lanes"]:
        if lane["mass_total_sd"] is not None:
            assert _decimals(lane["mass_total_sd"]) <= 3, lane
        if lane["conc_sd"] is not None:
            assert _decimals(lane["conc_sd"]) <= 3, lane
        for band in lane["bands"]:
            if band["mass_sd"] is not None:
                assert _decimals(band["mass_sd"]) <= 3, band
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "u.quant.json")
        densitometry.write_json(quant, path)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        for token in ("NaN", "Infinity", "-Infinity"):
            assert token not in raw, "quant.json 里出现了 %r" % (token,)
        back = densitometry.load_json(path, QUANT_KIND)
        assert back == json.loads(raw), "round trip 丢了传播字段"
        assert back["uncertainty"] == quant["uncertainty"], back["uncertainty"]
        assert back == quant, "load_json 往返与内存里的 quant 不一致"
        # a None SD is a JSON null, never a 0 and never the string "null"
        assert '"mass_sd": null' in raw, raw[:400]
    return True


# ---------------------------------------------------------------- runner

CHECKS = [
    check_schema_and_totals,
    check_standard_lanes_skipped,
    check_in_range_masses_identity,
    check_out_of_range_nulls,
    check_boundaries,
    check_baseline_column,
    check_power_identity,
    check_load_and_concentration,
    check_lane_shares_and_overall,
    check_precision_rounding,
    check_purity_and_json_round_trip,
    check_format_quant,
    check_api_refusals,
    check_cli_default_path,
    check_cli_load,
    check_cli_refusals,
    check_chain_identity_end_to_end,
    check_recovery_linear_nonidentity,
    check_recovery_power_nonidentity,
    check_power_zero_area_out_of_range,
    check_nonpositive_mass_null,
    check_terminal_precision,
    check_out_of_range_three_sig,
    check_uncertainty_block,
    check_mass_sd_linear,
    check_mass_sd_power,
    check_lane_uncertainty_sums,
    check_out_of_range_and_nonpositive_have_no_sd,
    check_no_residual_sd_withholds_everything,
    check_uncertainty_terminal,
    check_uncertainty_json_round_trip,
]


def run():
    """Run the acceptance suite (ep-bqv.5) and return the number of passed checks."""
    for name in ("quantify", "format_quant"):
        if not callable(getattr(densitometry, name, None)):
            raise AssertionError(
                "densitometry.%s 不存在——ep-bqv.5（quantify）还没落到这个分支上" % name)
    # a placeholder that raises NotImplementedError for every in-range band is NOT a passing
    # implementation: say so once, instead of failing somewhere deep inside the first check
    dens, curve = _fixture()
    try:
        densitometry.quantify(dens, curve)
    except NotImplementedError as e:
        raise AssertionError(
            "quantify 目前仍是占位实现（在范围内的质量没有算出来）：%s" % (str(e) or "NotImplementedError"))
    for check in CHECKS:
        check()
    print("test_quantify_acceptance %d/%d 通过" % (len(CHECKS), len(CHECKS)))
    return len(CHECKS)


# kept as a thin alias so an old invocation keeps working; it runs the same checks as `run()`
run_pending = run


if __name__ == "__main__":
    # A failed acceptance run must not look like a crash: one line, non-zero exit, no traceback
    # (this module is run directly on a branch where ep-bqv.5 may not have landed yet).
    import sys
    try:
        run()
    except (AssertionError, NotImplementedError) as exc:
        sys.stderr.write("[!] %s\n" % exc)
        raise SystemExit(1)
