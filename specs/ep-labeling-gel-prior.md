# Spec: ep-labeling — gel preset as a mandatory prior

Status: draft · Scope: `skills/ep-labeling` · Written 2026-10-20

## 1. Problem

Today the detected box *is* the crop; the gel preset only gives scale (`width_cm`) and a warning-only height check. Lane finding and band finding are entangled: lane spacing, lane-centre snapping and lane acceptance all use band rows. Consequences:

- A halo or the wells inside the detected box silently shifts both scale and crop (preset height vs detected height was already ~3 % off on the reference photo).
- Lane count and positions come from heuristics that were patched one bug at a time (9 of 15 lanes, ghost lanes, squeezed lanes).
- Band detection depends on lane positions that were themselves derived from bands (circular).

## 2. Decisions

1. **The gel preset is mandatory.** The user must provide it; there is no "no prior" mode and no default preset. `render` and `probe` fail with a clear message if none is given. (`--gel-cm WxH` stops being a substitute; it may remain only as an override of width/height inside a given preset.)
2. **The preset defines the gel area.** Detection locates the gel; the preset fixes its shape.
3. **Pipeline is strictly sequential with frozen outputs:** gel area → lanes → bands. Each stage reads only the frozen output of the previous one.

## 3. Preset schema (`data/gel_size.json`, extended)

Required: `name`, `width_cm`, `height_cm`, `lane_count`, `lane_pitch_cm`.
Optional: `trim_top_cm`, `trim_bottom_cm`, `anchor` (`bottom` | `top` | `center`, default `bottom`), `_source`.

`lane_count` = lanes on the whole gel. `lane_pitch_cm` = centre-to-centre distance. Invariant checked on load: `lane_count × lane_pitch_cm ≈ width_cm` within 10 %, else error.
Cropped photos: the user passes `--gel-lanes N` (visible lanes); width and expected lane count are then derived from `N × lane_pitch_cm`.

## 4. Stage 1 — gel area

Input: photo, preset. Output (frozen): `gel_box` = rotated rectangle in photo pixels, `angle`, `px_per_cm`, `source` of each number.

1. Detect box and tilt as today (`detect_gel`, `--rect` still skips detection).
2. Scale: `px_per_cm = detected_width_px / width_cm` (or, for cropped photos, from lane pitch).
3. Shape: height = `(height_cm − trims) × px_per_cm`, anchored on `anchor` edge of the detected box (default bottom, because the top carries the wells).
4. Cross-check and report: detected vs preset height and, when ≥ 2 lanes are later found, scale from lane pitch vs scale from width. Disagreement > 10 % → warning with both numbers. No silent correction beyond step 3.

## 5. Stage 2 — lanes (prior-driven, no band input)

Input: frozen `gel_box`, `lane_count`, `lane_pitch_cm × px_per_cm`. Output (frozen): per lane `{index, x_centre, width, measured|estimated, evidence}`.

1. Expected centres: `x_i = gel_left + (i − 0.5) × pitch_px` (after re-centering the lattice, see 2).
2. Column darkness profile (loose threshold, as today) inside the box.
3. Fit the lattice: choose offset (and a small pitch tolerance, ±5 %) maximising profile evidence at the expected centres. Per lane: refine within ±0.4 pitch to the local profile peak, else keep the lattice position and mark `estimated`.
4. Output exactly `lane_count` lanes. Counts never come from heuristics; faint or empty lanes are `estimated` lanes with their evidence value, not dropped.
5. Removed from lane stage: pitch from densest band row, snap-to-anchors, band-evidence acceptance, `fill_gaps`, `split_wide`/`merge_near` (kept only if tests show they still help the refinement).

## 6. Stage 3 — bands

Input: frozen lanes. For each lane, band rows from the lane-averaged row profile (top-hat peaks) at fine raster, as today. Output: `[(y, width, depth)]` per lane. Nothing flows back to stage 2.

## 7. Detection record (in-context JSON)

One JSON object, produced after stage 3, printed by `probe` and optionally written by `render`/`probe` via `--record PATH`; also passed to the MW report instead of the raw `lb` dict.

Open question (needs an owner decision before implementation): coordinate system. Proposed: `gel_box` and lanes in original-photo pixels; bands in output-PNG pixels; include `angle`, `px_per_cm`, `analysis_scale` so any number can be converted.

Fields: `preset`, `gel_box`, `angle`, `px_per_cm`, `checks`, `lanes[{index, x, width, estimated, evidence}]`, `bands[lane][{y, width, depth}]`.

## 8. Acceptance

- No preset → both commands exit non-zero with the message `gel preset required (--gel-size FILE)`.
- Reference photo `IMG_1464 (1).jpeg` with the 15-lane preset: 15 lanes, none dropped, centres within 3 analysis px of today's measured ones; MW report unchanged within 1 kDa.
- Synthetic gels in `selftest`: existing 5-lane case (faint lane 3, vertical streak) passes with a 5-lane preset; new cases: empty lane, off-centre photo, preset height 8 % larger than the detected box (area follows the preset, a warning is printed).
- A stage never imports from a later stage (checked by a test that greps imports).
- Docs (`SKILL.md`, conventions reference, `test-prompts.json`) state the preset is mandatory and ask for it in Workflow step 1.

## 9. Out of scope

Perspective correction; auto-selecting a preset; changes to MW calibration math.
