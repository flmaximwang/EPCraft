---
name: epcraft-quantify
description: >-
  Use when quantifying protein bands on an SDS-PAGE gel that already has
  loaded standards: turns the `fig.mw.json` v2 report written by the
  `epcraft-label` skill into band areas on two baselines, a standard curve and
  per-band masses (fig.dens.json, fig.curve.json, fig.quant.json, fig.bands.png
  and fig.curve.png). The chain is offline and reads files only — it never
  opens VectorCraft and never edits the labeling document or its PNG/report.
  Agarose gels have no loaded standards and cannot be mass-quantified.
compatibility: >-
  Python 3.9+; the optional Matplotlib extra (`epcraft[plot]`) renders the two PNGs.
  Uses the shared `epcraft` distribution, `epcraft` import package and
  `epcraft` CLI installed once for both skills; there is no separate install.
---

# Band Quantification (SDS-PAGE densitometry)

Take the MW report that `epcraft-label` already wrote and answer "how much protein is in this band / this lane / this gel". The chain is a sequence of small offline commands: each one reads the previous file, writes a sibling file, and can be inspected before the next step. Nothing here launches VectorCraft or rewrites the labeling outputs.

## Scope

- In: an existing `fig.mw.json` v2 (or a `fig.vectorcraft` whose sibling report is current) from `epcraft-label`, the **standard lanes and the mass loaded in each**, optionally the mass unit, the baseline, the fit model, replicate drops, the load volume per lane, and the lane selection when the default is wrong.
- Out (five sibling files, same stem; the three labeling files are untouched):

| File | Content |
|---|---|
| `fig.dens.json` | Every measured band's area on **both** baselines, the window and the flags (`kind: "ep-craft-densitometry"`). |
| `fig.bands.png` | Local peak traces and band areas. Single-lane panels shade the integrated signal. Overlay panels mark window endpoints and can color peaks by concentration. |
| `fig.curve.json` | The standard curve: collapsed standard points, the chosen fit model (`linear` or `power`) with its stored coefficients, `range`, residuals and notes (`kind: "ep-craft-densitometry-curve"`). |
| `fig.quant.json` | Every sample band's area → mass through the stored fit, lane totals, concentration and shares (`kind: "ep-craft-densitometry-quant"`). |
| `fig.curve.png` | The standard curve, its residuals, and (with `--quant`) the sample points. |

- Not covered: labeling, cropping, lane/band correction, MW calibration, re-rendering, or any pixel work on the gel. **SDS-PAGE only**: every mass comes from `--std`; an agarose gel can be labeled and sized but has no loaded standards, so there is nothing to calibrate mass against.

## Input contract: `fig.mw.json` v2

`fig.mw.json` is the only handoff from `epcraft-label`. The chain validates it: `version` must be `2`, `lanes` must be a non-empty list, each lane needs a `profile` with equal-length `y_cm` / `gray` / `baseline` arrays and strictly increasing `y_cm`, and `gel` / `window` must be objects. It uses the gel size, the integration window, the marker lane, each lane's `marker` / `trimmed` flags, the raw profile and each band's `y_cm` (plus `mw_kda` for reading). Full key list: `references/densitometry.md` §1.

- A `.vectorcraft` argument resolves to its sibling `<stem>.mw.json`. If that report is older than the document, `measure` refuses with `先 sync`: the document is the truth, and `sync` (in `epcraft-label`) rewrites the report. Never hand-edit either file.
- No report at all means this is labeling work first, not quantification: hand back to `epcraft-label` (render or sync), then quantify.
- The staged workflow commands also save the document without writing a report; only `render` / `sync` produce it.

## Files and CLI

Both skills share the `epcraft` executable. From the checkout root, use `uv tool install --force '.[plot]'` for a tool installation that can render PNGs. Use `uv tool install .` for JSON-only quantification, or run `PYTHONPATH=src python3 -m epcraft …` with a Python environment that already has the dependencies you need. Run from the directory holding the existing `fig.mw.json`, or pass explicit paths.

| Command | Use |
|---|---|
| `epcraft densitometry measure REPORT [--out DENS] [--flank-cm 0.15] [--min-ink 0.5] [--lanes 2-15] [--include-marker] [--include-trimmed]` | Areas on both baselines from `fig.mw.json` → `fig.dens.json` (default: report stem with `.mw` dropped). |
| `epcraft densitometry plot-bands --dens DENS [--out PNG] [--baseline flank\|global\|both] [--lanes …] [--group NAME=LANE[:BAND][@CONC],…] [--cols 2] [--width 1440]` | Local peaks, optionally overlaid by group → `fig.bands.png`. |
| `epcraft densitometry curve --dens DENS --std "2=20,3=10,4=5" [--out CURVE] [--baseline flank\|global] [--fit linear\|power] [--unit ug] [--drop 4]` | Fit the standards → `fig.curve.json`. |
| `epcraft densitometry plot-curve --curve CURVE [--quant QUANT] [--out PNG] [--width 1020]` | The curve and residuals, optionally the samples → `fig.curve.png`. |
| `epcraft densitometry quantify --dens DENS --curve CURVE [--out QUANT] [--load "9=2,10=2"]` | Area → mass, lane totals, shares → `fig.quant.json`. |

Short alias: `epcraft dens`. `measure` skips marker lanes and trimmed lanes by default; `--lanes` **replaces** that selection, while `--include-marker` / `--include-trimmed` extend it. `--std` grammar: `lane=mass` uses that lane's strongest band, `lane:2=mass` the second band from the top, `lane:*=mass` the lane total; masses are in `--unit` (default `ug`). Exit `0` = success, `2` = expected user/environment error. For the peak plot, repeat `--group` to overlay the standards and each sample dilution series in separate panels. Append `@concentration` to every member of a group to use a continuous concentration scale. For example, `--group 'Standards (mg/mL)=2@0.25,3@0.5,4@1' --group 'Sample A (relative)=5:1@1,6:2@0.5,7:1@0.25'`. `--group` takes the place of `--lanes` for that plot. Each peak is drawn at its true migration position, with its own baseline subtracted and its signal amplitude retained.

## Workflow

Run the steps in order and stop wherever an artifact looks wrong — the chain is meant to be inspected between steps, not run blind end to end.

1. **Confirm the input.** The report exists and is v2; the user has named the standard lanes and the mass in each (if not, ask once — never guess which lane is which). SDS-PAGE with loaded standards; otherwise stop and explain that an agarose gel has no mass calibration.
2. **Measure**: `epcraft densitometry measure fig.mw.json`. It prints which lanes it measured and how many bands per lane. Check the lane selection: second ladders, blank lanes and pooled lanes are samples to `measure` unless excluded. `--lanes` **replaces** the default selection, so it must list **the standard lanes together with the sample lanes** (for example `--lanes 2-8,10-12` where 2–4 are standards and 5–8, 10–12 are samples); list only the wells that take part in the fit or the report.
3. **Read `fig.bands.png`** (`plot-bands`, default `both`). Check the integration endpoints and compare the peaks and their tabulated areas in the Flank and Global panels. Both panels use a zero line for the selected baseline. Use explicit band numbers when the expected peak is weaker than another band in its lane.
4. **Fit**: `epcraft densitometry curve --dens fig.dens.json --std "…"` (add `--baseline global`, `--fit power` or `--drop N` only for a stated reason). Read the printed range, point count, `r²` and `notes`.
5. **Read `fig.curve.png`**: points against the line, the residual strip, replicate bars, and whether `notes` flags too few points or a large CV. A bent calibration is visible here before any sample is quantified.
6. **Quantify**: `epcraft densitometry quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]` (`--load` affects `conc` only). Then optionally re-run `plot-curve --quant` to place the samples on the curve.
7. **Report once**: the fit (baseline, model, `r²`, standard range and points), each lane's band masses or the explicit refusal, the lane totals and shares the user asked for, and anything unverified (which standard lanes were assumed, the unit, replicate spread). Quote numbers from the JSON, not from the terminal table's rounded percentages.

## Hard rules

- **Never extrapolate.** A band outside `range.volume_min … volume_max` gets `mass: null` + `out_of_range: true`; the lane total and `conc` go `null` too. Report the refusal and the direction of the fix (`多加点或浓缩后再跑` below the range, `稀释或少点一点再跑` above it), or add a bracketing standard and refit.
- **Never fabricate a standard point, a lane, or a fit.** `curve` uses `--std` only; a refused fit (`标准点不单调：…`) means the measurement and the mapping disagree — go back to `fig.bands.png`, check lane order, band index, windows and flags, then fix the mapping or drop a lane. Trying the other fit is legitimate; inventing a positive number is not.
- **Never refit in `quantify`.** `quantify` evaluates the stored `fit_params`; the same numbers must be reproducible from `curve.json` by hand.
- **Never hide a non-positive fitted mass.** An in-range band whose stored fit evaluates to ≤ 0 gets `mass: null`, `out_of_range: false`, one note, and no printed value anywhere. The fix is on the curve side (`--drop` the lowest standard or `--fit power`), not on the sample.
- **Keep the three percentages apart.** `rel_pct` = band share of its own lane; `frac_pct_among_lanes` = lane share of the gel; `conc` = `mass_total / load_ul`. They answer different questions; never multiply or average them into one "purity" number. Report the field the user asked for.
- **Do not touch the labeling outputs.** `fig.vectorcraft`, `fig.png` and `fig.mw.json` are read-only here; the chain writes only its own sibling files. Do not re-render or sync to "fix" a quantification result unless the report itself is stale — and then hand that step back to `epcraft-label`.
- **Read before you quote.** `fig.bands.png` and `fig.curve.png` are the audit trail; a mass quoted without looking at them is a number without evidence. Terminal percentages are rounded — percentages to quote come from the JSON.

## Checkpoints

| When | Do |
|---|---|
| `measure` prints `先 sync` (report older than the document) | The document was edited after the report. Hand to `epcraft-label` for one `sync`, then re-run `measure`. Do not use the stale report. |
| No `fig.mw.json` / not report v2 | This is labeling work: `epcraft-label` renders or syncs first. Do not reconstruct a report from a PNG or from memory. |
| `densitometry quantify` prints `超出标曲范围` (`out_of_range: true`, `mass: null`) | The band's area is below the smallest or above the largest standard area, so there is no mass. Never extrapolate. Relay the lane, the band and the direction: below → load more or concentrate; above → dilute or load less; or add a bracketing standard and refit. Look at `fig.bands.png` first if the *area* looks wrong. |
| In-range band with `out_of_range: false` but `mass: null` | The stored fit evaluated to zero or less at the low-area end. No number is printed anywhere — do not reconstruct or estimate one. Say the low-area end of the curve is not trustworthy and fix the curve (`--drop` the lowest standard, or `--fit power`, then re-run `curve` and `quantify`). |
| `curve` refuses `标准点不单调：…` | With the measured areas and the `--std` mapping, mass does not grow with area. Read `fig.bands.png` and `fig.dens.json` first: are the `--std` lanes the titration lanes, is the band index right, do the windows run into a neighbour or stop short, do flags say `clipped` / `flat top` / `invisible`? Then fix the mapping or `--drop` a lane and refit. Never fake a standard point. |
| Fewer than 3 standard points, or `cv_pct > 15` in `notes` | The fit is thin or the replicates are loose. Report it with the numbers; the curve still exists, but do not present the masses as precise. A replicate bar on `fig.curve.png` shows the area spread. |
| A second ladder, blank lane or pooled lane appears in `quant.json` | `measure` decides "marker" from the report's `marker` flag, not the label. Re-run `measure --lanes …` listing the standard lanes **and** the real sample lanes, leaving the unrelated wells out (for example `--lanes 2-8,10-12`, not `--lanes 9-12`, which would drop the standards the curve needs); then `curve` / `quantify` again. Otherwise those extra lanes inflate `overall_mass` and shrink every real sample's `frac_pct_among_lanes`. |
| User asks for mass on an agarose gel | Stop: there are no loaded protein standards, so no mass calibration. Labeling and MW sizing still belong to `epcraft-label`. |
| User asks to change a lane name, trim a lane, move a band or fix the crop | That is labeling work (`epcraft-label` + `sync`); quantification only consumes the resulting report. |

## Done when

- `fig.dens.json`, `fig.curve.json`, `fig.quant.json`, `fig.bands.png` and `fig.curve.png` exist next to `fig.mw.json`; the labeling outputs are unchanged.
- The reply quotes the fit (baseline, model, `r²`, standard range/points, replicates and drops), each requested lane's masses or the explicit refusal with its reason, and the specific share the user asked for (`rel_pct` vs `frac_pct_among_lanes` vs `conc`).
- Anything unverified is stated: which standard lanes were assumed and in what unit, any too-few-points / high-CV note, and any band or lane that got no number.
- References opened only as needed: `references/densitometry.md` (full contract, JSON keys, known accuracy limits) and the shared `specs/ep-craft-densitometry.md` design record.
