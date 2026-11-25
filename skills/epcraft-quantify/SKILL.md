---
name: epcraft-quantify
description: >-
  Use when quantifying protein bands on an SDS-PAGE gel that already has
  loaded standards: turns the `fig.mw.json` v2 report written by the
  `epcraft-label` skill into band areas on two baselines, a standard curve and
  per-band masses or equal-volume concentrations (fig.dens.json, fig.curve.json, fig.quant.json, fig.bands.png
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

- In: an existing `fig.mw.json` v2 (or a `fig.vectorcraft` whose sibling report is current) from `epcraft-label`, the **standard lanes and their loaded masses or known concentrations with equal loading volumes**, optionally the calibration unit, the baseline, the fit model, replicate drops, the load volume per lane, and the lane selection when the default is wrong.
- Out (five sibling files, same stem; the three labeling files are untouched):

| File | Content |
|---|---|
| `fig.dens.json` | Every measured band's area on **both** baselines, the window and the flags (`kind: "ep-craft-densitometry"`). |
| `fig.bands.png` | Local peak traces and band areas. Single-lane panels shade the integrated signal. Overlay panels mark window endpoints and can color peaks by concentration. |
| `fig.curve.json` | The standard curve: collapsed standard points, the chosen fit model (`linear` or `power`) with its stored coefficients **and their standard errors** (`slope_sd` / `intercept_sd` / `cov_slope_intercept` / `residual_sd` / `sxx` / `mean_x`; `exponent_sd` / `scale_sd` for power), `range`, residuals and notes (`kind: "ep-craft-densitometry-curve"`). |
| `fig.quant.json` | Every sample band's area → mass through the stored fit with its `mass_sd`, lane totals and concentrations with their SDs, shares and the `uncertainty` block (`kind: "ep-craft-densitometry-quant"`). |
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
| `epcraft densitometry curve --dens DENS --std "2=20,3=10,4=5" [--out CURVE] [--baseline flank\|global] [--fit linear\|power] [--unit ug] [--drop 4]` | Fit the standards → `fig.curve.json`. |
| `epcraft densitometry plot --dens DENS --curve CURVE [--quant QUANT] [--baseline flank\|global\|both] [--group NAME=LANE[:BAND][@CONC],…] [--cols 2] [--width 1440] [--curve-width 1020] [--bands-out PNG] [--curve-out PNG]` | Both pictures in one run → `fig.bands.png` and `fig.curve.png`; the standards are grouped and labelled from the fit itself. |
| `epcraft densitometry plot-bands --dens DENS [--out PNG] [--baseline flank\|global\|both] [--lanes …] [--group NAME=LANE[:BAND][@CONC],…] [--cols 2] [--width 1440]` | Standalone band overlay (same picture as in `plot`, without the curve) → `fig.bands.png`. |
| `epcraft densitometry plot-curve --curve CURVE [--quant QUANT] [--out PNG] [--width 1020]` | Standalone curve and residuals (same picture as in `plot`) → `fig.curve.png`. |
| `epcraft densitometry quantify --dens DENS --curve CURVE [--out QUANT] [--load "9=2,10=2"]` | Area → mass, lane totals, shares → `fig.quant.json`. |

Short alias: `epcraft dens`. `measure` skips marker lanes and trimmed lanes by default; `--lanes` **replaces** that selection, while `--include-marker` / `--include-trimmed` extend it. `--std` grammar: `lane=mass` uses that lane's strongest band, `lane:2=mass` the second band from the top, `lane:*=mass` the lane total, `lane=0` a blank standard lane (below); masses are in `--unit` (default `ug`). Exit `0` = success, `2` = expected user/environment error.

`plot` is the command to run: it renders the band overlay and the curve together, so the two pictures cannot drift apart. It always draws an automatic `Standards` group built from the points the fit used (dropped points stay out), then one tile per `--group`. The curve picks up `--quant` sample markers, limited to the bands the groups drew. `plot-bands` and `plot-curve` remain for standalone renders and for troubleshooting one picture at a time; they take the same `--group` / `--quant` grammar. For the peak plot, repeat `--group` to overlay the standards and each sample dilution series in separate panels. Append `@concentration` to every member of a group to use a continuous concentration scale. For example, `--group 'Standards (mg/mL)=2@0.25,3@0.5,4@1' --group 'Sample A (relative)=5:1@1,6:2@0.5,7:1@0.25'`. `--group` takes the place of `--lanes` for `plot-bands`. Each peak is drawn at its true migration position, with its own baseline subtracted and its signal amplitude retained.

**Blank standard lanes.** A standard lane with no detected bands (an empty well or a lane the detector found nothing in) is a legitimate `--std` member with mass `0`: write `--std "2=0,3=0.125,4=0.25"`. Such a lane has no band to integrate, so `curve` lends it the window of the measured single-band standards in the same `--std` list and integrates it on its own profile — twice, once per baseline. It contributes the zero point of the fit, gets no relative residual (`残差 —`), and `--fit power` refuses it (the log of zero is undefined): blank standards mean `--fit linear`. In `plot` the lane appears in the `Standards` group as `2 (blank)` with its measured area, so the overlay still shows every point the fit used. A blank lane needs at least one measured single-band standard in the same `--std` list to supply the window; `:*` (lane total) entries do not supply one, and a lone blank lane is refused with that reason. Mass `0` is reserved for a lane with no detected bands: naming a lane that *does* show bands with mass `0` is refused, since a real peak weighing nothing would bend the line towards the origin.

## Workflow

Run the steps in order and stop wherever an artifact looks wrong — the chain is meant to be inspected between steps, not run blind end to end.

1. **Confirm the input.** The report exists and is v2; the user has identified the standard lanes and supplied their loaded masses or their concentrations with equal loading volumes (if missing, ask once; use the confirmed lane mapping). SDS-PAGE with loaded standards; otherwise stop and explain that an agarose gel has no mass calibration.
2. **Measure**: `epcraft densitometry measure fig.mw.json`. It prints which lanes it measured and how many bands per lane. Check the lane selection: second ladders, blank lanes and pooled lanes are samples to `measure` unless excluded. `--lanes` **replaces** the default selection, so it must list **the standard lanes together with the sample lanes** (for example `--lanes 2-8,10-12` where 2–4 are standards and 5–8, 10–12 are samples); list only the wells that take part in the fit or the report.
3. **Inspect the peaks before fitting.** With no curve yet, the overlay is the only picture: `epcraft dens plot-bands --dens fig.dens.json --baseline both`, with one `--group` per sample dilution series (the standards get their own group later, from the fit). For each sample dilution series, deliver one overlay of the requested target band, or the main peak by default. In `--group`, a bare `LANE` selects one band by maximum integrated area (Flank for `both`); use `LANE:BAND` when the known target differs or when corresponding bands have different indices across the series. Add a separate overlay for a secondary band only if the user requests that band or asks to compare multiple bands. Append `@` values to every member for concentration coloring when concentrations or relative dilution concentrations are known. Use ungrouped `plot-bands` for individual-lane diagnostics or when the user requests a lane overview. Deliver the CLI-generated grouped PNG for standard and dilution comparisons. Check both panels' integration endpoints and tabulated areas before fitting. A blank standard lane (no detected band) is written `--std …=0` and has no peak to select here — read its area from `fig.dens.json` or from the `(blank)` row of the `Standards` overlay that step 5 renders.
4. **Fit**: `epcraft densitometry curve --dens fig.dens.json --std "…"` (add `--baseline global`, `--fit power` or `--drop N` only for a stated reason; blank standard lanes force `--fit linear`). Use the calibration units described below. Read the printed range, point count, `r²` and `notes`.
5. **Render both pictures and read the curve.** `epcraft dens plot --dens fig.dens.json --curve fig.curve.json` writes `fig.bands.png` and `fig.curve.png` in one run: it groups and labels the standards from the fit itself (dropped points stay out, a blank standard lane reads `2 (blank)`), and each `--group` you pass adds a tile next to them. In `fig.curve.png` check the points against the line, the residual strip (a blank standard has no residual and no dot), replicate bars, and whether `notes` flags too few points or a large CV. A bent calibration is visible here before any sample is quantified.
6. **Quantify**: `epcraft densitometry quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]` (`--load` affects `conc` only). Then re-run the same `epcraft dens plot … --quant fig.quant.json` to place the samples on the curve; with groups given, the curve marks only the bands those groups drew.
7. **Report once**: lead with the requested quantity and its unit, then give the fit (baseline, model, `r²`, standard range and points), any explicit refusal, and anything unverified (lane mapping, units, replicate spread). Quote numbers from the JSON. Report only lane totals or shares that the user requested and that make sense for the selected targets.

### Equal-volume concentration calibration

When the user requests concentration and confirms identical loading volumes for every standard and sample lane, use the standards' concentrations directly. For example, `epcraft dens curve --dens fig.dens.json --std '5:1=0.125,6:3=0.25,7:3=0.5' --unit mg/mL`. Use the actual target band indices and standard concentrations. In this mode `mg/mL` is the unit of the fitted response. Run `quantify` with `--load` omitted, and read each requested band's concentration from `bands[].mass` in that unit. The existing JSON key names are unchanged. The lane-level `conc` field is for mass calibration followed by division by a supplied load volume. Report the fitted concentration of the loaded dilution first. If a known dilution factor is available and the user wants the original sample concentration, multiply by that factor and label the result separately. If load volumes differ, fit loaded masses and divide each requested band's fitted mass by its own loading volume.

### How far to trust a mass: the ± from the fit

Every mass now carries the calibration's own scatter, not just a point estimate. `quantify` evaluates the **OLS prediction** standard deviation of the response at the band's area — the fit residual (`residual_sd`) plus the uncertainty of slope and intercept at that x — and stores it next to the value:

- `bands[].mass_sd` — the band's `±`, in the calibration unit, 3 decimals;
- `lanes[].mass_total_sd` — `sqrt(Σ mass_sd²)` over the lane's quantified bands, an approximation that omits covariance between bands sharing the same fit. It is `null` whenever `mass_total` is `null`.
- `lanes[].conc_sd` — `mass_total_sd / load_ul`, `null` unless both are known;
- top-level `"uncertainty": {"basis": "ols_prediction", "k": 1, "unit": "<curve unit>"}`.

The terminal block prints them: `0.522 ± 0.031 ug`, `0.052 ± 0.0031 ug/uL`, and the header names the convention (`±1 SD 来自拟合`). The `±` is one standard deviation (`k = 1`), not a confidence interval, and it describes **reproducing the fit on the standards**, not pipetting, staining or gel-to-gel variation — quote it as "from the standard curve" and never as a full error budget. Shares (`rel_pct`, `frac_pct_among_lanes`) stay point estimates and get no `±`: their errors are not independent of the masses in their own denominator.

A curve built from fewer than three standards, or one with a hand-written `fit_params` that has no `residual_sd`, cannot support the propagation. Then every propagated value is `null`, one `notes` sentence says the calibration carries no residual scatter, and the terminal block prints no `±`. That is not a bug to work around: report the masses, say the uncertainty is unavailable, and say why. Never compute a `±` by hand from the residuals and present it as if `quant.json` had stored it.

## Hard rules

- **Never extrapolate.** A band outside `range.volume_min … volume_max` gets `mass: null` + `out_of_range: true`; the lane total and `conc` go `null` too. Report the refusal and the direction of the fix (`多加点或浓缩后再跑` below the range, `稀释或少点一点再跑` above it), or add a bracketing standard and refit.
- **Never fabricate a standard point, a lane, or a fit.** `curve` uses `--std` only; a refused fit (`标准点不单调：…`) means the measurement and the mapping disagree — go back to `fig.bands.png`, check lane order, band index, windows and flags, then fix the mapping or drop a lane. Trying the other fit is legitimate; inventing a positive number is not.
- **Never refit in `quantify`.** `quantify` evaluates the stored `fit_params`; the same numbers must be reproducible from `curve.json` by hand.
- **Never invent an uncertainty.** `mass_sd` / `mass_total_sd` / `conc_sd` come from the stored `residual_sd`, `sxx` and `mean_x` by the formulas above. When the curve has none of them the JSON says `null` and the block prints no `±`; do not derive a number from the residuals, spread the `r²`, or borrow a figure from a previous run. A missing `±` is information — report it with its reason.
- **Never hide a non-positive fitted mass.** An in-range band whose stored fit evaluates to ≤ 0 gets `mass: null`, `out_of_range: false`, one note, and no printed value anywhere. The fix is on the curve side (`--drop` the lowest standard or `--fit power`), not on the sample.
- **Keep the three percentages apart.** `rel_pct` = band share of its own lane; `frac_pct_among_lanes` = lane share of the gel; `conc` = `mass_total / load_ul`. They answer different questions; never multiply or average them into one "purity" number. Report the field the user asked for.
- **Do not touch the labeling outputs.** `fig.vectorcraft`, `fig.png` and `fig.mw.json` are read-only here; the chain writes only its own sibling files. Do not re-render or sync to "fix" a quantification result unless the report itself is stale — and then hand that step back to `epcraft-label`.
- **Use the built-in plots for delivery.** Generate the figures with `epcraft dens plot`, which renders the standard overlay and the curve in one run; `plot-bands` and `plot-curve` re-render one of them on its own when that is all you need. Use the existing CLI options for plot changes and use a custom plotting script only if the user asks for one.
- **Read before you quote.** `fig.bands.png` and `fig.curve.png` are the audit trail; a mass quoted without looking at them is a number without evidence. Terminal percentages are rounded — percentages to quote come from the JSON.

## Checkpoints

| When | Do |
|---|---|
| `measure` prints `先 sync` (report older than the document) | The document was edited after the report. Hand to `epcraft-label` for one `sync`, then re-run `measure`. Do not use the stale report. |
| No `fig.mw.json` / not report v2 | This is labeling work: `epcraft-label` renders or syncs first. Do not reconstruct a report from a PNG or from memory. |
| `densitometry quantify` prints `超出标曲范围` (`out_of_range: true`, `mass: null`) | The band's area is below the smallest or above the largest standard area, so there is no mass. Never extrapolate. Relay the lane, the band and the direction: below → load more or concentrate; above → dilute or load less; or add a bracketing standard and refit. Look at `fig.bands.png` first if the *area* looks wrong. |
| In-range band with `out_of_range: false` but `mass: null` | The stored fit evaluated to zero or less at the low-area end. No number is printed anywhere — do not reconstruct or estimate one. Say the low-area end of the curve is not trustworthy and fix the curve (`--drop` the lowest standard, or `--fit power`, then re-run `curve` and `quantify`). |
| `curve` refuses `标准点不单调：…` | With the measured areas and the `--std` mapping, mass does not grow with area. Read `fig.bands.png` and `fig.dens.json` first: are the `--std` lanes the titration lanes, is the band index right, do the windows run into a neighbour or stop short, do flags say `clipped` / `flat top` / `invisible`? Then fix the mapping or `--drop` a lane and refit. Never fake a standard point. |
| Fewer than 3 standard points, or `cv_pct > 15` in `notes` | The fit is thin or the replicates are loose. Report it with the numbers; the curve still exists, but do not present the masses as precise. A replicate bar on `fig.curve.png` shows the area spread. With fewer than 3 points every `mass_sd` / `mass_total_sd` / `conc_sd` is `null` (no residual degrees of freedom): say the uncertainty is unavailable rather than quoting the point estimates as if they carried none. |
| `mass_sd` / `conc_sd` is `null` on a curve with ≥ 3 points, and `notes` says 标曲没有可用的残差散布 | The stored `curve.json` has no `residual_sd` / `sxx` / `mean_x` — it was hand-written, or produced before this ticket. Re-run `curve` from the dens and re-run `quantify`; do not patch numbers into the JSON by hand. |
| A second ladder, blank lane or pooled lane appears in `quant.json` | `measure` decides "marker" from the report's `marker` flag, not the label. Re-run `measure --lanes …` listing the standard lanes **and** the real sample lanes, leaving the unrelated wells out (for example `--lanes 2-8,10-12`, not `--lanes 9-12`, which would drop the standards the curve needs); then `curve` / `quantify` again. Otherwise those extra lanes inflate `overall_mass` and shrink every real sample's `frac_pct_among_lanes`. |
| User asks for mass on an agarose gel | Stop: there are no loaded protein standards, so no mass calibration. Labeling and MW sizing still belong to `epcraft-label`. |
| User asks to change a lane name, trim a lane, move a band or fix the crop | That is labeling work (`epcraft-label` + `sync`); quantification only consumes the resulting report. |

## Done when

- `fig.dens.json`, `fig.curve.json`, `fig.quant.json`, `fig.bands.png` and `fig.curve.png` exist next to `fig.mw.json`; the labeling outputs are unchanged.
- The reply leads with each requested sample band's concentration or mass in the calibration unit — with its `±` (one SD from the fit) and the unit — or the explicit refusal with its reason. Include the fit (baseline, model, `r²`, standard range/points, replicates and drops) and any requested shares. For dilution series, label loaded-dilution and original-sample concentrations explicitly.
- Anything unverified is stated: which standard lanes were assumed and in what unit, any too-few-points / high-CV note, any band or lane that got no number, and any missing `±` with its reason.
- References opened only as needed: `references/densitometry.md` (full contract, JSON keys, known accuracy limits) and the shared `docs/specs/ep-craft-densitometry.md` design record.
