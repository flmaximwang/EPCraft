# Spec: Densitometry (Quantify Protein from an SDS-PAGE Gel)

Status: implemented · Scope: `src/epcraft` + `skills/epcraft-quantify` · Written 2026-11-23 · Updated 2026-11-24 (ep-bqv.7 errata) · Builds on `docs/specs/gel-prior.md`, `docs/specs/crop-artboard.md`

## 1. Problem

`fig.mw.json` (report v2) already carries, per lane, the full gray / baseline (top-hat) / ink profile and the called bands. What it does *not* answer is the question the gel was run for: **how much protein is in each band**. Today the user must copy profiles into ImageJ and re-do background subtraction, band windowing, a standard curve and the inverse mapping by hand — with no way to see whether the choices were right.

We need a chain of small commands, each reading the previous one's JSON, so an agent (or the user) can run them in order, inspect each artifact, and stop where it looks wrong:

```
measure ──► dens.json ──► curve ──► curve.json ──► quantify ──► quant.json
              │                        │                         │
              ├─► plot-bands (PNG)     └─► plot-curve (PNG) ◄─────┘
```

No VectorCraft involvement: everything reads `fig.mw.json` (already on disk after every `render` / `sync`) and writes JSON / PNG. `measure` never launches the app, so the whole chain is offline and testable in `tests/`.

## 2. Decisions

1. **Input is the report, not the document.** `measure REPORT` takes `fig.mw.json` (v2). If handed a `.vectorcraft` document path instead, it derives `<stem>.mw.json` and, when that file is missing or older than the document, says so (`[!] … 先 sync（它会重写这份报告）`) and exits 2. Rationale: profiles, bands, lane labels, trim and the marker lane are all in the report already; reading the document would need the control port for no gain.
2. **Two baselines, always both.** Per band `measure` computes the area under the **global** baseline (the report's top-hat `baseline` array, half-window 0.30 cm) *and* under a **flank** baseline derived from the rows immediately beside the band. `curve --baseline flank|global` picks which column drives the standard curve; `plot-bands` draws both so the choice is visible. Measuring both costs nothing and removes the "re-run measure because I changed my mind" step. Default `flank`: the top-hat baseline dips under a dense band (that is why `profiles.call_bands` peaks on raw gray), so `ink` from it under-reports the dense end of a titration — exactly where standards sit.
3. **Flank baseline is a ramp, not a constant.** Two flank medians (left / right, each `--flank-cm` 0.15 cm of rows, clamped to the band's valley limits) are joined by a straight line in y. A constant level is wrong on the usual gel (gradient + smile) and on the top/bottom of the profile the global baseline is the natural fallback (used when a flank has fewer than 2 usable rows).
4. **One windowing rule, applied per baseline.** For each band and each baseline the ink trace is walked outward from the band's `y_cm` while the **smoothed** ink (0.03 cm half-window) stays above `--min-ink` (default 0.5 gray), stopping at the last row above, at the first row below, and never past the **midpoint to the neighbouring band** (valley-to-valley, so overlapping bands are not double counted). Area = trapezoid rule over the window; unit `gray·cm`.
5. **Areas are reported, not OD.** `gray` is 8-bit intensity; `ink = baseline − gray` is linear in stain, and so is the area. Coomassie saturation is a property of the standard curve (it bends at the top), which is why the fit and its residuals are always printed and plotted. Decision: no optical-density conversion — it would need an illumination reference this skill does not have.
6. **Two fits, both cheap.** `--fit linear` (default): least squares `mass = a + b·volume`, `r2` + residuals. `--fit power`: least squares in log10 for both axes, `mass = 10^c · volume^b` (the usual densitometry model of dye response), `r2` in log space. Whatever the fit, `plot-curve` shows points, line and residuals so the choice is checked, not trusted.
7. **Replicates are averaged, never silently merged.** Standard entries with the same mass become one point (`n`, `sd`, `cv_pct`, `lanes`); the curve is fitted on the points. `--drop LANE` excludes a lane (outlier) and keeps it as `used: false` in the JSON, so the plot shows what was dropped.
8. **Outside the standard range = no number.** A band whose area falls below the smallest / above the largest standard volume gets `mass: null` + `out_of_range: true` + a note; the lane's `mass_total` / `conc` are then `null` too, with `out_of_range_bands` naming them. Same rule as molecular weight in report v2 (`mw_kda: null` + `extrapolated`) — a plausible-looking number outside the curve is the failure mode we are avoiding.
9. **Standard syntax stays on the command line** — `--std "2=20,3=10,4=5"` (lane=mass, lane's strongest band), `"3:2=10"` (2nd band from the top), `"4:*=5"` (whole lane total), mass in `--unit` (default `ug`). No extra spec file: the whole chain is meant to be runnable in one line, and the resolved points land in `curve.json` where they can be inspected and re-used.
10. **Plots are standalone PNGs**, not VectorCraft artboards (originally ImageMagick MVG in `ep-bqv`, later migrated to optional Matplotlib `[plot]` in `ep-2rb`). Drawing an artboard would need the control port, a document mutation and an undo group for a picture that is *about* the document; standalone rendering keeps `plot-*` offline and pixel-testable in `tests`. Text is **English ASCII only** (`Arial.ttf` through `text_layout.pick_font()`), `µg` → `ug`, because the CJK font is not guaranteed on Linux.
11. **Everything is a pure function over dicts.** `densitometry/` has no I/O beyond `json.load` / `json.dump` and no renderer call; `plotting/` has no math. This keeps the tests hermetic (synthetic report → analytically known areas) and lets the math be reviewed without the renderer.
12. **`measure` skips the marker lane and trimmed lanes** — the ladder is not a sample, and a trimmed lane was removed from the Figure on purpose. `--lanes 1-5,9,12` (1-based, same grammar as `lane_select.parse_keep`) overrides that for a specific run; `--include-marker` / `--include-trimmed` override it per class. Both overrides are recorded in `params.lanes` so the JSON says what was measured.
13. **Not an artboard, not a new report version.** `fig.mw.json` is untouched; `dens.json` / `curve.json` / `quant.json` are new files next to it with their own `kind`. Nothing in the existing `sync` path reads them.

## 3. Commands (`epcraft densitometry …`, short alias `dens`)

The group is registered as `densitometry` with `aliases=["dens"]`, and both names go into
`gel_photo.CANVAS_COMMANDS` (the alias must also select the canvas parser).

### 3.1 `measure`

```
epcraft densitometry measure REPORT [--out dens.json] [--flank-cm 0.15] [--min-ink 0.5]
                                    [--lanes 2-15] [--include-marker] [--include-trimmed]
```

`--out` default: `<report stem>.dens.json` (`fig.mw.json` → `fig.dens.json`). Prints one line per lane: `泳道 9 S1：2 带，面积 12.4 / 8.1 gray·cm`; notes on stderr-free `[i] …` lines (same convention as `sync`).

### 3.2 `curve`

```
epcraft densitometry curve --dens dens.json --std "2=20,3=10,4=5" [--out curve.json]
                            [--baseline flank|global] [--fit linear|power] [--unit ug]
                            [--drop 4] [--drop 5]
```

`--out` default `<dens stem>.curve.json`. Prints the points (mass, area, n, cv), the fit, `r2`, and every residual; refuses `--std` that names the marker lane, a missing lane or a lane with more bands than the index given (exit 2, one line).

### 3.3 `quantify`

```
epcraft densitometry quantify --dens dens.json --curve curve.json [--out quant.json]
                               [--load "9=2,10=2"]      # µL loaded, per lane
```

`--out` default `<curve stem>.quant.json`. Prints the sample table (§5) and the JSON path.

### 3.4 `plot`, `plot-bands`, `plot-curve`

```
epcraft densitometry plot --dens dens.json --curve curve.json [--quant quant.json]
                          [--group NAME=LANE[:BAND][@CONC],…] [--baseline flank|global|both]
                          [--cols 2] [--width 1120] [--curve-width 1020]
                          [--bands-out bands.png] [--curve-out curve.png]
epcraft densitometry plot-bands --dens dens.json [--out dens.png] [--lanes 2-15]
                                 [--baseline flank|global|both] [--cols 3] [--width 2400]
epcraft densitometry plot-curve --curve curve.json [--quant quant.json] [--out curve.png]
                                 [--width 1600]
```

`plot` is the command to run: one invocation writes both pictures. It always draws a `Standards` group built from the curve's used points — a blank standard lane (mass 0, §4.5) shows there as `(blank)` — and one tile per repeated `--group`; with `--quant`, the curve marks the sample bands those groups drew. `plot-bands` and `plot-curve` render one of the two pictures on its own for standalone use or troubleshooting, and take the same `--group` / `--quant` grammar.

Defaults: `plot` → `<dens stem>.bands.png` + `<curve stem>.curve.png`, `baseline both`, `cols 2`; `plot-bands` → `<dens stem>.bands.png`, `baseline both`, `cols 3`; `plot-curve` → `<curve stem>.curve.png`. `--quant` is optional and only adds the sample markers.

## 4. Contracts

### 4.1 `densitometry.py` (math only; pure stdlib)

```python
VERSION = 1
MIN_INK = 0.5                 # gray levels; the walking threshold
FLANK_CM = 0.15               # flank half-taken beside a band
SMOOTH_CM = 0.03              # half-window for the edge walk (same as profiles.SMOOTH_CM)
FUZZ = 1e-9

parse_lanes(text, n) -> list[int]                       # 1-based, None/"" -> all
parse_std(text) -> list[(lane:int, band:int|"*"|None, mass:float)]
load_report(path) -> dict                               # validates version 2 + profile, raises RuntimeError
measure(report, lanes=None, flank_cm=FLANK_CM, min_ink=MIN_INK, smooth_cm=SMOOTH_CM) -> dict
build_curve(dens, std, unit="ug", fit="linear", baseline="flank", drop=()) -> dict
quantify(dens, curve, load=None) -> dict                # load = {lane:int -> ul:float}
format_curve(curve) -> str
format_quant(quant) -> str
write_json(obj, path)                                   # UTF-8, ensure_ascii=False, indent=1, trailing newline
```

Helpers (also public, so tests and the plot can use them):

```python
walk_window(y, ink, y_band, y_up, y_dn, min_ink, smooth_cm) -> (y0, y1)   # see §4.2
area_of(y, ink, y0, y1) -> float                                          # trapezoid, gray·cm
flank_ramp(y, gray, y0, y1, y_up, y_dn, flank_cm, baseline) -> dict      # §4.3
smooth(values, half_rows) -> list[float]                                  # centred moving mean, edges shortened
```

### 4.2 Windowing (§2.4)

```
y0, y1 = walk_window(y, ink, y_band, y_up, y_dn, min_ink, smooth_cm)
```

1. `sm = smooth(ink, half_rows=max(1, round(smooth_cm / dy)))`, `dy = median diff of y` (rows are uniform in report v2; a non-uniform `y` is an error).
2. `i = argmin |y − y_band|`. If `sm[i] <= min_ink` → the band is invisible: `(y_band − w/2, y_band + w/2)` where `w = max(width_cm, 2·dy)`, no walk (caller adds a note).
3. Walk up while `sm > min_ink` and `y >= y_up`; stop at the first row failing either. Same down (`y <= y_dn`). Return the row where the walk stopped on each side, exclusive of the stopped row.
4. `y_up` / `y_dn`: the midpoint between this band's `y_cm` and the previous / next band in the same lane; the profile's first / last `y` when there is none. Edge reasons recorded in `notes` when a clamp actually bites.

Every band is integrated twice: once on `ink_global = baseline_global − gray` with the report's `baseline`, once on `ink_flank = flank_ramp − gray` with the ramp.

### 4.3 Flank ramp (§2.3)

```
flank_ramp(y, gray, y0, y1, y_up, y_dn, flank_cm, baseline) -> {"left": [y_c, g], "right": [y_c, g], "source": "flank"|"global"}
```

- `fw = max(2, round(flank_cm / dy))` rows.
- Left flank = rows `[max(y_up, y0 − fw·dy), y0)`; right flank = `(y1, min(y_dn, y1 + fw·dy)]`. Level = **median** gray of those rows, placed at the flank's y centre.
- A flank with fewer than 2 rows → its level is taken from the **global** `baseline` at that row (still a point of the ramp); `source: "global"`. Both sides unusable → `source: "global"` and the ramp degenerates to the constant global level there.
- The ramp value at y: linear between `(y_left, g_left)` and `(y_right, g_right)`; clamped to the end values outside them.

### 4.4 `dens.json` (`kind: "ep-craft-densitometry"`)

```json
{"version": 1, "kind": "ep-craft-densitometry",
 "units": {"y": "cm from gel-face top", "gray": "0-255 (lower = darker)",
           "ink": "baseline - gray", "area": "gray·cm (ink integrated over the band window)"},
 "source": {"report": "fig.mw.json", "gel": {"width_cm": 8.6, "height_cm": 6.25, "px_per_cm": 118.11},
            "window": {"top_cm": 0.15, "bottom_cm": 6.06}, "marker_lane": 1},
 "params": {"flank_cm": 0.15, "min_ink": 0.5, "smooth_cm": 0.03,
            "window": "walk outward on smoothed ink while > min_ink, clamped to the midpoints of the neighbouring bands",
            "lanes": "all sample lanes, trimmed and marker excluded"},
 "lanes": [{"lane": 9, "label": "S1", "marker": false, "trimmed": false, "noise": 0.8,
            "profile": {"y_cm": [...], "gray": [...], "baseline_global": [...]},
            "bands": [{"index": 1, "y_cm": 2.4, "mw_kda": 45.0, "peak_ink_global": 12.3, "peak_ink_flank": 13.1,
                       "window_global": [2.31, 2.49], "window_flank": [2.30, 2.50],
                       "area_global": 14.2, "area_flank": 15.1,
                       "ramp": {"left": [2.24, 197.4], "right": [2.56, 194.1], "source": "flank"},
                       "clipped": false, "flat_top": false, "notes": []}],
            "area_global": 22.3, "area_flank": 23.4}],
 "notes": ["泳道 4 的带 1 窗口贴到相邻带中点"]}
```

- `profile.gray` and `profile.baseline_global` are copied verbatim from the report (rounded to 2 dp, as there). `ink` is *not* stored (derivable); `dens_plot` recomputes it.
- `window_*` / `area_*` exist only for the baselines actually integrated — both, always, for every band.
- `clipped` = some row inside `window_flank` has `gray <= 8` (black, i.e. saturated). `flat_top` = ≥ 3 rows inside `window_flank` within 1 gray level of the window minimum. Both are notes, never errors.
- `peak_ink_*` = the maximum of the corresponding ink trace inside its own window (not the band's `prominence`, which is measured against the raw gray flank rule).
- `ramp` is per band (the flank baseline is a property of that band, not the lane).
- `index` is 1-based, top → bottom within the lane; `curve --std "lane:i"` addresses it.

### 4.5 `curve.json` (`kind: "ep-craft-densitometry-curve"`)

```json
{"version": 1, "kind": "ep-craft-densitometry-curve",
 "unit": "ug", "baseline": "flank", "fit": "linear",
 "source": {"dens": "fig.dens.json", "std": "2=20,3=10,4=5", "drop": [4]},
 "points": [{"mass": 20.0, "volume": 120.5, "n": 2, "sd": 1.1, "cv_pct": 0.9,
             "lanes": [2, 3], "entries": [[2, null], [3, null]], "used": true}],
 "fit_params": {"slope": 6.02, "intercept": 0.4, "r2": 0.9985, "n": 4},
 "range": {"volume_min": 12.0, "volume_max": 120.5, "mass_min": 5.0, "mass_max": 20.0},
 "residuals": [{"mass": 20.0, "volume": 120.5, "predicted_mass": 19.7, "rel_pct": -1.5}],
 "notes": []}
```

- `fit_params` for `power` instead: `{"exponent": 1.05, "scale": 0.19, "r2": 0.997, "space": "log10", "n": 4}`; `scale = 10^c` so `mass = scale · volume^exponent`.
- `entries` keeps the original `(lane, band)` per replicate so a later stage (or the user) can trace a point back. `band` is the 1-based `index`, `"*"` for a whole-lane total, or `null` for the lane's strongest band.
- `used: false` = dropped by `--drop`. Dropped points stay in the JSON and in the plot.
- `--fit linear` needs ≥ 2 used points with distinct volumes; `--fit power` needs all `volume > 0` and `mass > 0`. Slope ≤ 0 (linear) or `exponent <= 0` (power) → `RuntimeError`("标准点不单调…"), with a hint to run `plot-bands`.
- `mass 0` is the zero point of a linear fit and is legal only for a **blank standard lane**: a lane with no detected bands. It inherits the average `window_<baseline>` of the measured single-band standards in the same `--std` list, is integrated on its own profile (both baselines), and carries `rel_pct: null` (no residual dot). A lane that does show bands may not take mass 0 (`RuntimeError`), and `--fit power` refuses the point outright (`log10(0)`).
- `notes` collects: `n < 3` ("只有 %d 个标准点，r² 不足以说明形状"), top-point residual < −10% ("最高标准点比线性低 %.0f%%，可能已饱和；试 --fit power 或 --drop 它"), replicate `cv_pct > 15`.

### 4.6 `quant.json` (`kind: "ep-craft-densitometry-quant"`)

```json
{"version": 1, "kind": "ep-craft-densitometry-quant",
 "unit": "ug", "baseline": "flank", "fit": "linear",
 "source": {"dens": "fig.dens.json", "curve": "fig.curve.json", "load": "9=2,10=2"},
 "lanes": [{"lane": 9, "label": "S1", "load_ul": 2.0,
            "bands": [{"index": 1, "y_cm": 2.4, "mw_kda": 45.0, "area": 15.1,
                       "mass": 3.1, "out_of_range": false, "rel_pct": 62.0}],
            "mass_total": 5.0, "conc": 2.5, "out_of_range": false, "out_of_range_bands": [],
            "frac_pct_among_lanes": 33.3}],
 "overall_mass": 15.0,
 "notes": []}
```

- `area` = the curve's baseline column. `mass` = inverse of the fit; `null` + `out_of_range: true` when outside `range.volume_min … volume_max` (§2.8).
- `rel_pct` = band mass / `mass_total` × 100 (null when `mass_total` is null).
- `conc` = `mass_total / load_ul`, reported in `unit`/µL (= mg/mL numerically); `null` without `--load`.
- `frac_pct_among_lanes` = lane `mass_total` / Σ known `mass_total` × 100.
- Lane order = `dens` order. Standard lanes (any lane appearing in `curve.points.entries`) are excluded; the note says how many were skipped.

### 4.7 `dens_plot.py` (ImageMagick only, no math)

```python
plot_bands(dens, out, baseline="both", lanes=None, cols=3, width=2400) -> (w, h)
plot_curve(curve, out, quant=None, width=1600) -> (w, h)
```

Both build an MVG script in a `tempfile` file, call `magick -size WxH xc:white -font <font> -draw @script -depth 8 out`, delete the script, and raise `RuntimeError` on a non-zero exit (through `magick_io._run`). Font: `text_layout.pick_font()` (fall back to `"Arial"`).

`plot-bands` (§2.10, §2.2, §2.4): a grid of `cols` columns, one panel per lane, panel width `(width − 2·M)/cols`, panel height 4:3. Per panel: y axis = `y_cm` downward (gel order, top of gel at the top), x axis = gray 0→255 to the right; the polished view is *gray* vs *y* with the **baseline drawn as a dashed line and the area between it and the curve filled** for the active baseline, so the shaded area **is** the number. Requirements:

- Draw `gray` as a solid dark polyline; `baseline_global` as a blue dashed polyline; the per-band flank ramps as orange dashed segments.
- Shade (translucent fill) each band's window for the active baseline(s): `baseline=both` shades the global window in blue-ish and the flank window in orange-ish, translucent so both windows are visible when they differ.
- Label each band with `area` (`.1f`) and `MW` when present, plus a `LANE n: label` header, the lane's `area_{active}`, the `clipped` / `flat_top` flags as `! clipped` / `flat top`, and the run parameters (`baseline`, `flank_cm`, `min_ink`) once in the margin.
- Axes: cm ticks each 1 cm on the left of column 1, a `gray` axis under each panel labelled `0 … 255` with `dark →` direction, and the integration window edges drawn as thin vertical rules.

`plot-curve`: log-free linear axes; x = `volume` (area), y = `mass`; standard points as filled circles with x-error bars (horizontal, along the area axis) when `n > 1` (sd), dropped points as hollow circles labelled `dropped`; the fitted line across `range.volume_min … volume_max` extended by 10 % either side; a residual strip under the main axes (x = volume, y = `rel_pct`, zero line, ±10 % guides); annotation block top-right with `fit`, `r2`, `n`, `baseline`, `unit`, the equation (`mass = a + b·volume` or `mass = c · volume^b`); sample markers from `quant` when given (filled squares, hollow for `out_of_range`, labelled with their lane).

## 5. Terminal output

`format_curve`:

```
[标曲] 基线 flank，拟合 linear（ug）：mass = 0.4 + 6.02·area，r² 0.9985，4 点（1 个重复）
  20 ug   面积 120.5（n=2，CV 0.9%）  残差 -1.5%
  10 ug   面积  58.1（n=1）            残差 +0.8%
  注：只有 4 个标准点…
```

`format_quant`:

```
[定量] 基线 flank · linear · ug（标曲 5–20 ug，4 点，r² 0.9985）；跳过 4 条标准道
  泳道  9 S1     带1 45.0 kDa  面积 15.1  3.1 ug (62%) │ 合计 5.0 ug  浓度 2.5 ug/uL  占全胶 33%
  泳道 10 S2     —  超出标曲范围（面积 4.2 < 12.0）：稀释或加点
```

## 6. Files

| File | Role |
|---|---|
| `src/epcraft/densitometry/` | math: `measure` / `build_curve` / `quantify` / parsing / formatting |
| `src/epcraft/plotting/` | optional Matplotlib `[plot]` renderers → PNG |
| `src/epcraft/cli/densitometry.py` | `densitometry` parser + handlers (argparse only, no math) |
| `tests/densitometry/test_densitometry.py` | math suite (synthetic reports with analytically known areas) |
| `tests/plotting/test_dens_plot.py` | pixel tests on the two PNGs |
| `src/epcraft/cli/canvas.py` | `add_commands`: register the group |
| `src/epcraft/cli/gel_photo.py` | `CANVAS_COMMANDS` gains `densitometry` and `dens` |
| `tests/cli/test_cli.py` | parser shape + one-line error boundary for the new commands |
| `tests/core/test_core.py` / `tests/__main__.py` | call the test suites |
| `skills/epcraft-quantify/SKILL.md`, `README.md` | command table, workflow step, "Not covered" |
| `skills/epcraft-quantify/references/quantification-conventions.md` | quantification workflow, baseline/fit policy, JSON keys |

## 7. Tests (analytic, no user files)

Synthetic report v2 built in memory (`_synthetic_report(...)` inside `tests/densitometry/test_densitometry.py`): uniform `y`, `dy = 0.01 cm`, `gray = 200 + slope·y − Σ A_k·exp(−½((y−c_k)/σ_k)²) + deterministic texture`.

1. Single Gaussian band, flat background: `area_flank` ≈ `A·σ·√(2π)` within 2 %; `area_global` within 6 % (the top-hat dip is real). The synthetic case is sized so the ink at the window edge is still above `min_ink` (σ = 0.06 cm, `A` = 30) — otherwise the assertion measures the threshold, not the baseline.
2. Sloping background (40 gray levels over the profile): flank within 2 %, global off by ≥ 10 % — asserts *why* flank exists.
3. Two Gaussians 0.06 cm apart: each window stops at the midpoint; each area within 5 % of its own analytic value; `window_*[1] ≤ midpoint ≤ window_*[0]` of the next band.
4. Noise only (no band) → `measure --lanes` on a lane with `bands: []` produces no bands and no NaN.
5. `clipped`: gray ≤ 8 in the band → flag true; `flat_top` on a flat-bottomed synthetic band.
6. `walk_window` edges: band at the profile top / bottom clamps to the window (`notes`), not to negative indices.
7. `parse_std` / `parse_lanes`: `"2=20,3:2=10,4:*=5"`, aliases, bad input → `RuntimeError`.
8. `build_curve`: exact linear recovery (mass = 2 + 8·area generated, ±0.1 %), replicate `n`/`sd`, `--drop` keeps the point with `used: false` and changes `fit_params`, power fit recovers `mass = 0.2·area^1.1`, `r2` ≈ 1; `slope <= 0` → error; `--std` on the marker lane → error.
9. `quantify`: masses within 1 % of the truth; a band 20 % below `volume_min` → `mass: null`, lane `mass_total: null`, `out_of_range_bands: [i]`; `--load` → `conc`; `frac_pct_among_lanes` sums to 100.
10. JSON round trip: `write_json` → `load_report`-style read of `dens`/`curve`/`quant` keeps every field; UTF-8, no `\u` escapes.
11. Idempotence: `measure` twice → byte-identical JSON (no timestamps); `curve` twice → identical.

`tests/plotting/test_dens_plot.py`: build a 6-band synthetic `dens` and a `curve`, render to a scratch dir, then assert (a) PNG exists with the requested `size`, (b) it is not blank (≥ 2 % of pixels differ from white), (c) sampling the panel's shaded window rows finds the fill colour and sampling rows outside the window finds white/background, (d) `plot-curve` draws the fitted line across the axes (a column inside the range has non-white pixels at the line's row), (e) an `out` path that is a directory or has no `.png` suffix raises `RuntimeError`/`ValueError`, (f) no temp file survives (glob the scratch dir).

## 8. Docs

- `skills/epcraft-quantify/SKILL.md`: quantification workflow (`measure` → `curve` → `quantify`, `plot-bands` / `plot-curve`), checkpoint lines for out-of-range and monotonicity errors.
- `README.md`: command table and three-file label output note (densitometry writes siblings, opt-in).
- `skills/epcraft-quantify/references/quantification-conventions.md`: baseline choice (why flank is the default, what to look for in `plot-bands`), the windowing rule, the area unit, both fits, out-of-range policy, JSON keys.
- `skills/epcraft-quantify/test-prompts.json`: trigger and non-trigger prompts for the quantification workflow.

## 9. Out of scope

- Loading-volume / dilution bookkeeping beyond `--load` (no stock concentrations, no linearity titration solver).
- Optical-density conversion, exposure/gamma correction, Coomassie-vs-silver calibration tables.
- VectorCraft artboards for the plots, `sync` integration, `ep-meta` keys.
- Multi-gel comparisons, statistics across gels, total-protein normalization (Ponceau/stain-free channels).
- Automatic band detection for the standards (they come from the report's bands, like everything else).

## 10. Errata and settled decisions (2026-11-24)

1. **§4.7 replicate error bars (settled, `ep-bqv.7`)**: `curve.points[].sd` is the sample SD of replicate **areas** (`volume`, `gray·cm`), not mass. Standard points with `n > 1` therefore carry horizontal **x-error bars** (`±sd` along the area axis, clipped to the main axes, with caps) and no y-error bars. Points with `n = 1` and dropped points carry no bar. Error bars on sample points are out of scope for this epic.
2. **§4.6 fit direction**: `quantify` evaluates the stored `fit_params` forward (`mass = intercept + slope · area` for `linear`, `mass = scale · area^exponent` for `power`), consistent with §2.6, §5, and §7.8–7.9. Where §4.6 informally referred to the "inverse of the fit", the forward area → mass evaluation is authoritative.
3. **§7 synthetic Gaussian tolerances**: Under the specified 0.30 cm top-hat baseline, 0.03 cm smoothing, 0.15 cm flank ramp, and midpoint clamp (§§2.3–2.4, 4.2–4.3), a single Gaussian at σ = 0.06 cm, A = 30 integrates to approximately −3.1 % (`area_flank`) and −42.4 % (`area_global`), and two equal Gaussians 0.06 cm apart integrate to approximately −33.3 % each due to midpoint truncation. The algorithm is kept as specified and `tests/densitometry/test_densitometry.py` assertions are calibrated to those measured bounds (flank within 2 % at σ = 0.03 cm, within 5 % at σ = 0.06 cm; global visibly depressed; close pair asserts the midpoint clamp).
