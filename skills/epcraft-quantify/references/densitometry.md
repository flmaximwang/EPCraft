# Band densitometry: report to mass

This reference is the contract of the offline densitometry chain — the commands, the files, the JSON keys and the refusals. It is the quantification half that used to be §14 of the labeling conventions; the labeling half lives in the sibling `epcraft-label` skill.

**SDS-PAGE only.** The chain assumes the standard lanes hold **protein standards of known mass**: every mass value comes from `--std`, and the areas are integrations of raw gray on the same gel. An agarose gel can still be sized (MW), but it has no such loaded standards, so there is nothing to calibrate mass against — label it with `epcraft-label`, do not quantify it.

The whole chain `measure → curve → quantify` reads files only and never touches VectorCraft: the input is the `fig.mw.json` that `epcraft-label`'s `render` / `sync` already wrote (report v2, §1), and the outputs are sibling files with the same stem — `fig.vectorcraft`, `fig.png` and `fig.mw.json` are not changed by a single byte.

```
fig.mw.json ──measure──► fig.dens.json ──curve──► fig.curve.json ──quantify──► fig.quant.json
                              │                        │
                              └──► fig.bands.png       └──► fig.curve.png (sample points added with --quant)
```

Every step reads the previous step's file, so the chain can be inspected one step at a time and stopped wherever something looks wrong. Commands (short alias `epcraft dens`):

```bash
epcraft densitometry measure fig.mw.json                   # areas, both baselines
epcraft densitometry curve --dens fig.dens.json --std "2=20,3=10,4=5"
epcraft densitometry quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]
epcraft densitometry plot-bands --dens fig.dens.json [--baseline flank|global|both]
epcraft densitometry plot-curve --curve fig.curve.json [--quant fig.quant.json]
```

Every command accepts `--out`; without one the output path follows the chain suffix: `fig.mw.json` → `fig.dens.json` → `fig.curve.json` → `fig.quant.json`, and the plots are `fig.bands.png` / `fig.curve.png`. An `--out` that names a directory, sits in a directory that does not exist, or points at an input this command reads is refused with an error and a non-zero exit (directories are never created, inputs are never overwritten).

## 1. Input: `fig.mw.json` v2

This is the only interface to `epcraft-label`; the chain never re-renders, re-crops or re-calibrates. `measure` validates the report before using it:

- `version` must be `2`;
- `lanes` must be a non-empty list, each lane carrying a `profile` with `y_cm`, `gray` and `baseline` arrays of the same non-zero length, with `y_cm` strictly increasing;
- `gel` and `window` must be objects.

The keys the chain reads:

| Key | Used for |
|---|---|
| `gel.width_cm` / `height_cm` / `px_per_cm` | copied into `dens.json` for traceability, and the cm units of the window walk |
| `window.top_cm` / `bottom_cm` | the gel-face integration window |
| `calibration.marker_lane` | copied into `dens.json`; the marker lane is skipped by `measure` |
| `lanes[].lane` / `label` / `marker` / `trimmed` | lane selection (`marker` decides the skip — never the label), traceability |
| `lanes[].profile.y_cm` / `gray` / `baseline` | the raw profile: `baseline` is the report's global (top-hat, half-window 0.30 cm) baseline; `ink` is not stored in `dens.json`, recompute it for plots |
| `lanes[].bands[]` | `y_cm` anchors each integration window; `mw_kda` is copied into the outputs for reading, never used in a mass calculation |
| `notes` | not read by the chain |

A `.vectorcraft` argument resolves to its sibling `<stem>.mw.json`. If that report is **older than the document**, `measure` refuses with `先 sync` — the document is the truth and `sync` rewrites the report; do not hand-edit either file.

## 2. Two baselines: why flank is the default

`measure` computes two areas for **every band**:

- **global**: the top-hat baseline carried in report v2 (`profile.baseline`), half-window 0.30 cm. It treats the whole lane as background, so it dips underneath a dense band — which is exactly why `profiles.call_bands` looks for peaks on raw gray.
- **flank**: a baseline estimated from the rows immediately **either side** of the band, `--flank-cm 0.15` cm per side by default, median per side, with the two side points joined by a straight **ramp** (not a constant). Each end is clamped inside the midpoint to the neighbouring band; when a side has fewer than 2 usable rows, that side takes the global baseline value at the window edge and the band is tagged `flank_fallback_left` / `flank_fallback_right`.

The two baselines are **ordered**: the window is walked on the global baseline first, the flank ramp's two ends are taken **outside** the edges of that global window, and only then is the window walked again on the flank ramp (§3). A global window that creeps into a neighbouring band or stops halfway across its own band therefore shifts where the flank rows are taken — which is why both shaded sets in `fig.bands.png` are worth a look: when the two areas differ a lot, the usual cause is not which baseline is right, but that the global window itself is wrong.

`curve --baseline flank` (the default) fits on the flank areas; `--baseline global` fits on the global ones. Flank is the default for a practical reason: gels often carry a gradient background and smile, and a dense band is exactly what the top-hat dips under most; together they make the global baseline underestimate the dense end of a titration — and standard lanes usually sit at that dense end. Both areas are written to `dens.json` (`area_global` / `area_flank`, at band level and at lane level), so switching baselines means re-running `curve`, not `measure`.

**Choosing a baseline from `fig.bands.png`** (§4): the shading is the rows that were integrated and the dashed lines are the two baselines. If both dashed lines hug the background just outside the band and the shading does not creep into a neighbour, flank is sound; if the background is visibly sloped while the orange line is flat, the flank rows landed on the gradient — switch baselines or change `--flank-cm`.

## 3. Windows, units, flags

**The window rule is walked once per band and per baseline:**

1. start at the band's `y_cm` and walk outward on `smooth(ink, 0.03 cm)` until the smoothed ink drops below `--min-ink` (default 0.5 gray levels);
2. the walk never crosses the **midpoint to the neighbouring band** (valley-to-valley), so two adjacent bands do not count each other twice;
3. the walk also stops at the first/last row of the profile (`clamped_top` / `clamped_bottom`), and at the neighbour midpoint it records `clamped_mid_up` / `clamped_mid_down`;
4. when the band is invisible (smoothed ink at the start row is already ≤ `--min-ink`) nothing is walked: a fixed window of `max(width_cm, 2·dy)` is opened and `invisible` is recorded.

The area is the **trapezoid integral** of ink over the window, with no half-cell edge approximation. The unit is `gray·cm`: `ink = baseline − gray`, `gray` runs 0–255 with darker = lower, so the area is "how much darker than the baseline, × how long". **It is a gray integration, not optical density** — there is no illumination reference, this tool does no OD, exposure or gamma conversion, so the premise that area is proportional to stain load is not corrected for; the standard curve and its residuals are what expose the deviation (§5).

`clipped` and `flat_top` are **observations, not diagnoses**: the former means some row in the flank window has `gray ≤ 8` (near black), the latter that at least 3 rows sit within 1 gray level of the window minimum (a flat top). They say only what the pixel values in those rows are; whether the cause is too much load, overexposure or the image itself is something the standard curve cannot tell — check the photo before writing a conclusion. Likewise `peak_ink_flank` / `peak_ink_global` are the maximum ink inside their own window and are not `prominence`.

`measure` skips marker lanes and trimmed lanes by default (the ladder is not a sample, and trimmed lanes were deliberately left out of the Figure); `--lanes 2-15` replaces the whole selection and `--include-marker` / `--include-trimmed` extend the default one. Which lanes were actually measured is written into the text of `params.lanes` — `all lanes`, `all sample lanes, trimmed and marker excluded`, or a run list such as `lanes 1-5` / `lanes 5` / `lanes 2-4,9` — so the JSON says so itself.

**Known limits of the specified algorithm.** These numbers are measured on synthetic profiles and are the reason `fig.bands.png` has to be read before any mass is quoted:

- one Gaussian band (σ = 0.06 cm, amplitude 30) — the flank area lands about 3 % under the analytic area, the global area about 42 % under, because the top-hat baseline dips underneath a band of that width;
- two Gaussian bands 0.06 cm apart (both σ = 0.06 cm) — the midpoint split truncates each window, and each band keeps about two thirds of its analytic area (`clamped_mid_down` / `clamped_mid_up`).

Both are limits of the algorithm as specified, not bugs to work around: a window that is too tight loses area, and a dipping background takes more of it than a flank ramp does. What to look for in `fig.bands.png`: how far the shading runs past the band on each side, whether it stops at a neighbour midpoint, and how far the dashed baseline sits below the flat ends of the profile.

## 4. Reading `fig.bands.png` / `fig.curve.png`

Both are standalone PNGs drawn by ImageMagick (MVG, ASCII text only, `ug` rather than `µg`), not VectorCraft artboards; `plot-*` is offline, pixel-testable and never modifies the document.

`plot-bands`: one panel per lane, `--cols` defaults to 3 and `--width` to 2400. A margin block on top carries the title (`epcraft densitometry plot-bands  -  <report basename>`), a run-parameter line `baseline both  |  flank_cm 0.15  |  min_ink 0.5  |  6 lanes` and the legend `solid dark = gray profile  |  dashed blue = global baseline  |  dashed orange = flank ramp  |  shaded = area (gray*cm)`. In a panel the x axis is gray (`0 (dark) … 255`) and the y axis is `y_cm` (top = gel top). The dark solid line is `profile.gray`; the blue dashed line is the global baseline; the orange dashed lines are each band's flank ramp; the shading is the integration window under the active baseline, and **the area of the shading is the number**. A panel header reads `LANE n: label` (`LANE n` alone when the lane has no name), with lane totals `flank 23.4 | global 22.3 gray*cm` under `both` and `flank 23.4 gray*cm` under a single baseline; each band is labelled `b1 11.7 / 5.8 45.0 kDa` (flank / global area + MW; under a single baseline just that one area, so `b1 11.7 45.0 kDa`), a flag line reads `b1 flat top | b2 ! clipped, flat top`, and a tag line reads `b2 clamped_mid_down, flank_fallback_right`. `--baseline flank|global|both` (default `both`) decides which baseline the shading and totals use; with `both` the blue and orange windows are both drawn translucent, so a mismatch is visible at a glance.

Reading order: **windows first** — does the shading creep into a neighbour, stop halfway across the band, run to the first/last row of the profile; **baselines second** — do the dashed lines hug the background outside the band, does the orange line follow a sloped background; **numbers last** — two areas for the same band that differ a lot mean the baseline choice matters, so do not just pick the prettier one. `measure` turns these situations into sentences in its `notes` (e.g. `泳道 4 的带 1 窗口贴到相邻带中点`), matching the tag lines on the panels.

`plot-curve`: x = `volume` (area), y = `mass`. Standard points are filled circles (points removed by `--drop` are drawn hollow and labelled `dropped`), the fitted line runs solid across `range.volume_min … volume_max` and dashed through the 10 % extension on either side (it stops at the x axis rather than drawing a negative mass); underneath is a residual strip (`rel_pct` with a zero line and two dashed ±10 % guides). The strip's own scale widens when a residual is far outside ±10 % (`rel_hi`), and when the guides then sit too close together their two labels are replaced by a single `+/-10% band (dashed)` note above the strip — the rules never move, only the labelling. Top right is an annotation block with `fit` / `r2` / `n` (and how many points were dropped) / `baseline` / `unit`, the equation, and the `standard range`. The legend explains the markers in one line (filled circle for a standard point, hollow circle for a dropped point, and with `--quant` the sample markers — filled for in range, hollow for out of range), and it explains the bar once there instead of on every point. With `--quant` the samples are drawn as filled squares labelled `L<lane>` (`L5`), or `L<lane>.<band>` (`L8.2`) when that lane has several bands; an out-of-range sample is a hollow square sitting on the x axis, labelled `L<lane>.<band> out` (`L8.1 out`, or `L<lane> out` when there is no band index). When reading it, check how far each point sits from the line, whether a single point sits high or low in the residual strip, and whether `notes` mentions too few points or a large replicate CV — the fit is a black box that returns parameters, and the plot is the only place to audit it.

**Replicate bars.** A standard point that was measured more than once (`n > 1`) carries a bar drawn **along the area axis**, through the point, with small caps at both ends. Its half-length is the point's `sd` — the spread of the replicate **areas** in `gray·cm` (§5), the same number `curve.json` stores — so the bar is read on the x axis (gray*cm) and never on the mass axis: it says how far apart the replicate measurements landed, not ±mass. A point with a single measurement (`n = 1`, `sd` `null`) gets no bar. The plot is the quick check that replicates exist and roughly how tight they are; for the exact numbers read `sd` / `cv_pct` in `curve.json` (§5).

## 5. Two fits, standard-lane grammar, replicates and drops

Standard lanes are given on the command line only (`--std`), never in a config file: `2=20,3=10,4=5` is `lane=mass`. Without an index the lane's **strongest band** (`strongest`) is used, `3:2=10` is the second band from the top (1-based), and `4:*=5` is the whole lane's total area. The mass unit is `--unit` (default `ug`); it is written to `curve.json`'s `unit` and does not change any number. The lanes named in `--std` must exist in the dens and must not be marker lanes; a lane given twice, a band index out of range and a non-positive mass each produce a one-line error and exit 2.

The two fits (`--fit`):

| `--fit` | Model | Parameters | Notes |
|---|---|---|---|
| `linear` (default) | a least-squares straight line between area and mass | `slope` / `intercept` / `r2` / `n` / `space: "linear"` | the usual choice; a bent calibration shows up in the residuals |
| `power` | one least-squares line on log10 of both area and mass (a power law) | `exponent` / `scale` / `r2` / `n` / `space: "log10"` | `r2` is in log space; requires every area and mass > 0 |

Parameters are stored to 4 decimals. The curve reads **area → mass**: linear `mass = intercept + slope · area`, power `mass = scale · area^exponent` (`scale = 10^c`). That is the direction `format_curve` prints, the direction `fit_params` is defined in, and the direction `quantify` evaluates (§8); `points[].volume` is the area and `points[].mass` the mass throughout. `quantify` converts areas to masses with the **stored** parameters and never refits, so recomputing the same equation by hand reproduces what is in `quant.json`.

**Replicates**: standard lanes with the same mass collapse into one point (keeping `n`, `sd`, `cv_pct`, `lanes`, `entries`), and the fit works on points, not lanes. `sd` is the standard deviation of the **area** (the spread of areas between replicate lanes), not of mass; `cv_pct = sd / mean area × 100`. A `cv_pct > 15` goes into `notes`.

**`--drop`**: removes a standard lane from the fit (`--drop 4`, repeatable); its point stays in `points` with `used: false`, so both the plot and the JSON show it as dropped. `range`, `residuals` and `notes` use `used: true` points only. A lane named in `--drop` must be in `--std`.

**When the fit is refused**: `curve` requires ≥ 2 `used` points with at least two distinct areas; a linear slope or a power exponent ≤ 0 is refused outright with `标准点不单调：…` and a pointer to run `plot-bands` first. That error says "with the areas as measured and the mapping `--std` gives, mass does not grow with area" — it is a self-check of data against mapping and **not** a verdict about saturation or about how the gel was made; the next move is to go back to `fig.bands.png` and check windows, standard-lane order and flags, then either fix the mapping or drop a lane, instead of forcing a positive number.

## 6. Out of range: no number

`quantify` compares every sample band's area against the curve's `range.volume_min … volume_max`:

- inside the interval: `mass` is computed from the stored `fit_params` (no refit) as `mass = intercept + slope · area` (linear) or `mass = scale · area^exponent` (power), `out_of_range: false`;
- outside it: `mass: null` + `out_of_range: true`. The band stays in the JSON with its `area` and other keys, but `mass` and `rel_pct` are `null`; the **whole lane's** `mass_total` / `conc` become `null` as well (so the other bands in that lane also get `rel_pct: null`), and `out_of_range_bands` lists which bands they were.

This follows the same rule as MW (`mw_kda: null`, no extrapolation) — a plausible-looking number outside the curve is exactly the failure mode to avoid. The remedy depends on which side the area fell, and the terminal message and `notes` say so: below the range `多加点或浓缩后再跑` (load more or concentrate), above it `稀释或少点一点再跑` (dilute or load less) — bring the load inside the standard range in the right direction, or add a standard lane that brackets it and re-run `curve`. A band outside the range is never extrapolated either way.

**An in-range area can still produce no mass.** If the stored fit evaluates to zero or less for a band whose area does lie inside `range.volume_min … volume_max`, that band also gets `mass: null`, with `out_of_range: false`; the whole lane's `mass_total` / `conc` / `frac_pct_among_lanes` go `null` and one `note` names the lane, the band and the area and says the low-area end of the curve is not trustworthy. **The value the fit produced is never printed anywhere** — not in `quant.json`, not in the terminal block, not in the note as a diagnostic — so there is no zero or negative number to read, quote or mistake for a measurement. The fix is on the curve side: drop the lowest standard (`--drop`), or try `--fit power`, then re-run `curve`.

## 7. Reading `quant.json`

`quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]`: `--load` gives how many µL were loaded per lane (`lane=µL`) and affects `conc` only. Standard lanes (those appearing in `curve.points[].entries`) are skipped automatically and `notes` says how many were skipped. Lane order follows the dens.

The terminal block is a reading aid, not the data: a header line `[定量] 基线 flank · linear · ug（标曲 6–20 ug，3 点，r² 1.0000）；跳过 3 条标准道`, then one line per sample lane — `泳道  5 X  带1 45.0 kDa  面积 2.1  14.03 ug (100%) │ 合计 14.03 ug  浓度 7.015 ug/uL  占全胶 64%` — then the `注：` lines. A lane with several bands joins its band texts with ` · ` (`… 面积 4.5  14.869 ug · —  超出标曲范围（面积 1.75 < 1.78）：多加点或浓缩后再跑`). A band that gets no number either keeps its `带N … 面积 …` head and then prints `— 标曲给出的质量不为正：试 --drop 最低标准点或 --fit power`, or — when its area is outside the range — prints only `—  超出标曲范围（面积 0.003 < 0.871）：多加点或浓缩后再跑` with no band number and no MW (the `注：` line that follows names the lane and band); the comparison uses 3 significant digits, the same as the note. A rejected band also removes the lane's `│` tail, since `mass_total` is `null`. The header's standard range, point count and `r²` come from `curve.json` for display only — `quant.json` does not store them.

Precision: `quant.json` stores `mass` / `mass_total` / `conc` / `area` to 3 decimals and `rel_pct` / `frac_pct_among_lanes` to 2. The terminal table prints masses and concentrations with up to 3 decimals (trailing zeros dropped, so `0.522 ug` and `0.052 ug/uL` stay distinct) and prints shares as whole percent, so a percentage to quote should come from the JSON rather than from the table.

Every reported `mass` is the curve's stored fit evaluated on the band's area — `mass = intercept + slope · area`, or `mass = scale · area^exponent` for the power fit — read off the `area_<baseline>` column the curve was fitted on, so a reader can reproduce the number from `curve.json` alone.

Keep the three percentages apart:

- `rel_pct` (band level) = this band's mass ÷ **its own lane's** `mass_total` × 100 — "how much of this lane is this band";
- `frac_pct_among_lanes` (lane level) = this lane's `mass_total` ÷ the sum of known `mass_total`s × 100 — "how much of the gel is this lane";
- `conc` = `mass_total / load_ul`, numerically mg/mL (`ug` / µL).

They are three different quantities and must not be merged into one "purity": for "is this lane pure" read `rel_pct`, for "what fraction of the expression is the product" read `frac_pct_among_lanes`, for concentration read `conc`. Lanes whose `mass_total` is `null` do not enter the denominator of `frac_pct_among_lanes` (the denominator holds known totals only).

**Every measured lane that is not a standard lane counts as a sample.** `quantify` has no lane filter of its own: it takes whatever `measure` wrote, minus the standard lanes named in the curve, and puts all of it into `overall_mass` and the `frac_pct_among_lanes` denominator. `measure` in turn decides what is a marker by the report's `marker` flag, **not by the label** — a real report can carry a second ladder labelled `Marker` that is not the calibration lane, and it gets measured and quantified as if it were a sample. Before quantifying, leave out second ladders, blank lanes and anything else that is not a sample with an explicit `measure --lanes` that still lists the **standard lanes** together with the real sample lanes (for example `--lanes 2-8,10-12` where 2–4 are standards and 5–8, 10–12 are samples; `--lanes` replaces the default selection, so a bare `--lanes 9-12` would drop the standards the curve needs); otherwise those lanes inflate the gel total and shrink every real sample's lane share. `measure` records what it actually measured in `params.lanes`.

## 8. JSON keys

**`fig.dens.json`** (`kind: "ep-craft-densitometry"`, `version: 1`):

| Key | Meaning |
|---|---|
| `version` / `kind` | fixed `1` / `"ep-craft-densitometry"` (checked on load by `curve` / `quantify`) |
| `units` | prose for `{y, gray, ink, area}`: `cm from gel-face top`, `0-255 (lower = darker)`, `baseline - gray`, `gray·cm (ink integrated over the band window)` |
| `source.report` | the path **as typed** on the command line (verbatim: a `fig.vectorcraft` argument is recorded as `.vectorcraft`, a `fig.mw.json` one as `.mw.json`); `""` for a report built in memory |
| `source.gel` / `source.window` / `source.marker_lane` | gel-face size, integration window and marker lane copied from the report, for traceability |
| `params.flank_cm` / `min_ink` / `smooth_cm` | the three values actually used (not the defaults) |
| `params.window` | fixed prose describing the window rule |
| `params.lanes` | which lanes were measured (`all lanes` / `all sample lanes, trimmed and marker excluded` / a run list such as `lanes 1-5`, `lanes 5`, `lanes 2-4,9`) |
| `lanes[]` | per lane: `lane`, `label`, `marker`, `trimmed`, `noise`; `profile` (`y_cm` / `gray` / `baseline_global`, value-for-value the report's, with no stored `ink` — recompute it for plots); `bands[]`; `area_global` / `area_flank` (sum of that lane's band areas) |
| `lanes[].bands[]` | `index` (1-based, top to bottom), `y_cm`, `mw_kda`, `strongest`, `peak_ink_global` / `peak_ink_flank`, `window_global` / `window_flank` (first and last `y_cm` of the closed interval), `area_global` / `area_flank`, `ramp`, `clipped`, `flat_top`, `notes` |
| `ramp` | `left` / `right` (each `[y_cm, gray]`), `source` (`flank` only when both sides are real flank rows, else `global`) |
| `notes` | top-level remarks (window clamped, flank fallback, invisible band, …), one sentence each (the messages are Chinese, like every CLI message) |

`bands[].notes` holds fixed ASCII tags, **never Chinese**: `invisible`, `clamped_top`, `clamped_bottom`, `clamped_mid_up`, `clamped_mid_down`, `flank_fallback_left`, `flank_fallback_right`; the Chinese sentences live in the top-level `notes`.

**`fig.curve.json`** (`kind: "ep-craft-densitometry-curve"`, `version: 1`):

| Key | Meaning |
|---|---|
| `unit` / `baseline` / `fit` | mass unit, the baseline that drove the curve, the fit model |
| `source.dens` / `source.std` / `source.drop` | input dens path, `--std` verbatim, list of dropped lane numbers |
| `points[]` | `mass`, `volume` (area), `n`, `sd` (standard deviation of the area, `null` when `n=1`), `cv_pct`, `lanes`, `entries`, `used` |
| `entries` | `[lane, band]` per replicate; `band` is a 1-based index, `"*"` the lane total, `null` the strongest band — traceable back to the exact lane and band |
| `fit_params` | `linear`: `slope` / `intercept` / `r2` / `n` / `space: "linear"`; `power`: `exponent` / `scale` / `r2` / `n` / `space: "log10"`; all to 4 decimals |
| `range` | `volume_min` / `volume_max` / `mass_min` / `mass_max`, over `used: true` points only |
| `residuals[]` | per used point: `mass` / `volume` / `predicted_mass` / `rel_pct` (`(predicted − actual) / actual × 100`) |
| `notes` | too few points (< 3), top-point residual < −10 %, replicate CV > 15 %, … |

**`fig.quant.json`** (`kind: "ep-craft-densitometry-quant"`, `version: 1`):

| Key | Meaning |
|---|---|
| `unit` / `baseline` / `fit` | copied from the curve, naming which parameter set was used |
| `source.dens` / `source.curve` / `source.load` | both input paths plus `--load` verbatim |
| `lanes[]` | `lane`, `label`, `load_ul` (`null` without `--load`), `bands[]`, `mass_total`, `conc`, `out_of_range`, `out_of_range_bands`, `frac_pct_among_lanes` |
| `lanes[].bands[]` | `index`, `y_cm`, `mw_kda`, `area` (the curve baseline's column), `mass`, `out_of_range`, `rel_pct` |
| `mass_total` / `conc` | the lane's known masses summed (`null` as soon as one band in the lane is out of range **or** its fitted mass is not positive, §6) / `mass_total ÷ load_ul` (`null` without `--load` or when `mass_total` is `null`) |
| `out_of_range` / `out_of_range_bands` | whether any band in the lane is **out of range**, and the `index` of those bands; an empty list means all areas are inside (a band inside the range whose fitted mass came out ≤ 0 is not listed here — it has `mass: null` with `out_of_range: false`) |
| `overall_mass` | sum of every known `mass_total` |
| `notes` | the `已跳过 N 条标准道（泳道 …）` sentence, then one sentence per band that got no mass — `… 超出标曲范围（min–max）：多加点或浓缩后再跑；该道的合计与浓度不给数` or the non-positive-mass sentence (§6) |

## 9. Decisions and known limits

Three points call for an explicit record: one where behaviour follows a decision rather than the literal wording of the spec, one where that wording was corrected outright, and one where the specified algorithm has known accuracy limits.

1. **Direction of the fit.** The curve is **area → mass**: linear `mass = intercept + slope · area`, power `mass = scale · area^exponent` with `scale = 10^c`. `quantify` evaluates the stored `fit_params` in that direction (§5, §6). The spec's §4.6 wording ("inverse of the fit") does not line up with this, but the stored parameters, the equation `format_curve` prints and the acceptance item that recovers known masses within 1 % are all defined this way; the implementation follows them.
2. **Replicate error bars (resolved, ticket ep-bqv.7).** A standard point with replicates is drawn with a bar along the **area** axis, because the point's `sd` is the spread of replicate **areas** in `gray·cm` (§5); nothing is drawn on the mass axis. The earlier spec §4.7 wording ("y-error bars") did not match what the stored number means; the spec has been corrected and now asks for the area-axis bar, so the plot, the plot legend and `curve.json` all say the same thing.
3. **Accuracy limits of the algorithm.** On synthetic profiles the flank area comes out about 3 % low and the global area about 42 % low for a single σ = 0.06 cm band; two bands 0.06 cm apart each keep about two thirds of their analytic area because the midpoint split truncates both windows (§3). These are limits of the specified windowing and baselines, and the reason `fig.bands.png` is read before any mass is quoted.

## 10. Modules (shared `epcraft` package)

The chain lives in the same installed package as labeling; these are its modules (the labeling modules are listed in the `epcraft-label` conventions §12):

| Module | Role |
|---|---|
| `src/epcraft/densitometry.py` | densitometry math: `measure` (areas on both baselines), `parse_std` / `parse_lanes` / `parse_load`, `build_curve` (both fits), `format_curve`, `quantify` (area → mass through the stored fit), `format_quant`, JSON writing |
| `src/epcraft/dens_plot.py` | the two PNGs: `plot_bands` / `plot_curve`. MVG layout only, no math |
| `src/epcraft/densitometry_cli.py` | argparse and handlers for `densitometry` (alias `dens`): flags, path guards, error boundary |
| `src/epcraft/selftest_densitometry.py`, `selftest_dens_plot.py`, `selftest_plot_curve_acceptance.py`, `selftest_quantify_acceptance.py` | the four offline suites under `epcraft selftest`: analytic truth of areas/fits/quantification, pixel checks of both PNGs, plot-curve acceptance (horizontal area bars, no vertical error-bar ink), and quantify acceptance (stored-fit masses, both refusals, `--load`, lane shares, terminal block, CLI) |

Design contract: `specs/ep-craft-densitometry.md` (historical file name and contents are preserved; the CLI name in it belongs to the pre-split runtime).
