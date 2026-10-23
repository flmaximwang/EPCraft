# Spec: ep-craft — gel preset as a mandatory prior

Status: draft · Scope: `skills/ep-craft` · Written 2026-10-20

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
`height_cm` is the **slab height**: from the bottom of the wells to the lower edge. It excludes the teeth of gel left standing between the wells (amended 2026-10-20, see §4).
Optional: `trim_top_cm`, `trim_bottom_cm`, `anchor` (`bottom` | `top` | `center`, default `bottom`), `_source`.

`lane_count` = lanes on the whole gel. `lane_pitch_cm` = centre-to-centre distance. Invariant checked on load: `lane_count × lane_pitch_cm ≈ width_cm` within 10 %, else error.
Cropped photos: the user passes `--gel-lanes N` (visible lanes); width and expected lane count are then derived from `N × lane_pitch_cm`.

## 4. Stage 1 — gel area

Input: photo, preset. Output (frozen): `gel_box` = rotated rectangle in photo pixels, `angle`, `px_per_cm`, `source` of each number.

1. Detect box and tilt as today (`detect_gel`, `--rect` still skips detection). The box's top edge is the first row covering ≥ 90 % of the widest row, which is the slab top (well bottoms). Left, right and bottom keep the 50 % coverage rule. Reason: above the slab stands a row of irregular, often bent teeth, and a 50 % line falls somewhere inside them with no physical meaning. On the reference photo it produced 6.70 cm, while the slab measures 6.25–6.27 cm (amended 2026-10-20).
2. Scale: `px_per_cm = detected_width_px / width_cm` (or, for cropped photos, from lane pitch).
3. Shape: height = `(height_cm − trims) × px_per_cm`, anchored on `anchor` edge of the detected box (default bottom, the straightest and cleanest edge).
4. Cross-check and report: detected vs preset height and, when ≥ 2 lanes are later found, scale from lane pitch vs scale from width. Height disagreement > 5 % → warning with both numbers. The detected top is now a physical edge, and the reference photo agrees within 1 %. Scale disagreement > 10 % → warning with both numbers. No silent correction beyond step 3.

## 5. Stage 2 — lanes (prior-driven, no band input)

Input: frozen `gel_box`, `lane_count`, `lane_pitch_cm × px_per_cm`. Output (frozen): per lane `{index, x_centre, width, measured|estimated, evidence}`.

1. Expected centres: `x_i = gel_left + (i − 0.5) × pitch_px` (after re-centering the lattice, see 2).
2. Column darkness profile (loose threshold, as today) inside the box.
3. Fit the lattice: choose offset (and a small pitch tolerance, ±5 %) maximising profile evidence at the expected centres. Per lane: refine within ±0.4 pitch to the local profile peak, else keep the lattice position and mark `estimated`. A peak narrower than 0.25 pitch at half prominence is a speck, not a lane, and counts as no peak (amended 2026-10-20: on the reference photo, a 2 px speck pulled lane 10 by 6 px).
4. Output exactly `lane_count` lanes. Counts never come from heuristics; faint or empty lanes are `estimated` lanes with their evidence value, not dropped.
5. Removed from lane stage: pitch from densest band row, snap-to-anchors, band-evidence acceptance, `fill_gaps`, `split_wide`/`merge_near` (kept only if tests show they still help the refinement).

## 6. Stage 3 — bands

Input: frozen lanes. For each lane, band rows from the lane-averaged row profile (top-hat peaks) at fine raster, as today. Output: `[(y, width, depth)]` per lane. Nothing flows back to stage 2.

## 6b. Stage 4 — lane selection (crop and stitch)

The user may keep only some lanes. Detection, calibration and the MW report always run on the **whole** gel (stages 1–3); selection happens afterwards, on frozen lanes, before text is placed.

- **Input:** `--keep-lanes SPEC`, original 1-based lane numbers, commas and ranges (`1,2,8-10`). Omitted = keep all. The skill asks the user which lanes they need (Workflow step 1) and passes the answer. Order is always left to right as on the gel; no reordering.
- **Strips:** each maximal run of consecutive kept lanes is one strip. A strip spans from the midpoint between its first lane and the lane before it (gel edge for lane 1) to the midpoint between its last lane and the lane after it (gel edge for the last lane). Midpoints use the frozen lane centres.
- **Stitching:** strips are joined left to right with a visible break: a white gap of `--splice-gap-cm` (default 0.15 cm) with a thin vertical line centred in it. A single contiguous run produces no gap and no mark. Stitching is the only image operation beyond crop and scale; no strip is stretched, shifted vertically or colour-adjusted.
- **Labels:** original lane labels and numbers are kept (a kept lane 8 still says 8). Lane labels sit above their lane in the stitched layout; marker labels sit beside the marker lane as today.
- **Marker lane not kept:** the script does not decide. It stops and tells the skill which marker lanes would be cropped out; the skill asks the user each time and re-runs with one of: (a) `--marker-policy calibrate-only` — marker lanes are used for calibration/MW only, no marker labels are drawn; (b) `--marker-policy keep` — the marker lane nearest the kept lanes is added to the selection. Without a policy and with no kept marker lane, the run fails with that message.
- **MW report:** reports kept sample lanes only; calibration uses all marker lanes regardless of selection.
- **Record:** a `crop` section is added to the record (§7), so the crop operation is reproducible and is the only place the original lane numbers are mapped to the stitched layout.
- **Disclosure:** non-adjacent lanes stitched from one gel must be disclosed in a figure legend. The terminal output prints a one-line reminder whenever a splice gap is produced, and `SKILL.md` repeats it.

## 7. Detection record (in-context JSON)

One JSON object, produced after stage 3, printed by `probe` and optionally written by `render`/`probe` via `--record PATH`; also passed to the MW report instead of the raw `lb` dict.

**Cropped-gel coordinates only.** The original photo is discarded after cropping, so the record stores no photo coordinates, no `gel_box`, no tilt angle and no analysis-scale numbers. Origin = top-left corner of the cropped gel face (after rotation, crop and trims), x to the right, y downward. Every position is given in **cm** (physical, dpi-independent); the output PNG's pixel position is `cm / 2.54 × dpi` and needs nothing else from the record. Stages 1–3 still work in photo pixels internally and convert at the end.

Fields:
- `preset`: name plus the numbers used (`width_cm`, `height_cm`, `lane_count`, `lane_pitch_cm`, trims).
- `gel`: `{width_cm, height_cm}` of the output face as actually produced (after lane selection, if any).
- `crop`: present only when lanes were selected: `{keep_lanes: [original numbers], marker_policy, splice_gap_cm, strips: [{lanes: [original numbers], src_x0_cm, src_x1_cm, dst_x0_cm}]}`. `src_*` are cm on the **uncropped gel face** (origin top-left of that face), not on the photo; `dst_x0_cm` is the strip's left edge in the output.
- `checks`: results of the cross-checks in §4 (numbers and pass/warn).
- `lanes[]`: `{index, x_cm, width_cm, estimated, evidence}` for the kept lanes only; `index` is the original lane number and `x_cm` is the position in the output layout.
- `bands[lane][]`: `{y_cm, width_cm, depth}`.

Not stored: anything needed to map back to the photo.

## 8. Acceptance

- No preset → both commands exit non-zero with the message `gel preset required (--gel-size FILE)`.
- Reference photo `IMG_1464 (1).jpeg` with the 15-lane preset: 15 lanes, none dropped, centres within 3 analysis px of today's measured ones; MW report unchanged within 1 kDa.
- Synthetic gels in `selftest`: existing 5-lane case (faint lane 3, vertical streak) passes with a 5-lane preset; new cases: empty lane, off-centre photo, preset height 8 % larger than the detected box (area follows the preset, a warning is printed; 3 % stays silent); a slab with irregular teeth standing above it (the box top lands on the slab, not inside the teeth).
- A stage never imports from a later stage (checked by a test that greps imports).
- The record contains no key named like `rect`, `bbox`, `angle` or `scale` and no number needing the original photo; a test checks the key set.
- Lane selection: `--keep-lanes 3-5` on the reference photo gives one strip with width `3 × pitch` within 1 %, no gap; `--keep-lanes 1,2,8-9` gives two strips, a gap of `splice_gap_cm`, original labels 1,2,8,9, and `crop.strips` matching; a selection without a marker lane and no policy exits non-zero with the message; the MW result for kept lanes equals the full-gel result for the same lanes.
- Docs (`SKILL.md`, conventions reference, `test-prompts.json`) state the preset is mandatory and ask for it in Workflow step 1.

## 9. Out of scope

Perspective correction; auto-selecting a preset; changes to MW calibration math; reordering lanes; stretching or shifting strips; hiding the splice (the break mark is mandatory).
