# Tickets: ep-labeling gel-preset prior

Spec: `specs/ep-labeling-gel-prior.md`. Order = dependency order. Each ticket is shippable on its own and must keep `selftest` green.

## T1 — Preset schema and mandatory gate
- Extend `data/gel_size.json` with `lane_count`, `lane_pitch_cm` (keep `width_cm`, `height_cm`, trims), optional `anchor`.
- `config.load_gel_size`: validate required keys and the `lane_count × lane_pitch_cm ≈ width_cm` invariant.
- `probe` and `render`: require `--gel-size`; drop the `--gel-cm`-only path (keep as override of width/height).
- Done when: no preset → exit ≠ 0 with the spec message; bad invariant → clear error; `selftest` has a test for both.

## T2 — Gel area from the preset (stage 1)
- Depends on T1.
- New `gel_area.py`: takes detection + preset, returns frozen `gel_box`, `angle`, `px_per_cm`, with sources (internal to the run; not stored in the record).
- Height from preset, anchored per `anchor`; the width/height cross-check warning at 10 %.
- `render` uses this box for crop and scale (replaces the "detected box is the crop" logic and the height-only warning).
- Done when: synthetic gel with preset height +8 % crops to preset height and prints a warning; reference photo output differs from today only in crop height by ≤ the trim/anchor effect (documented).

## T3 — Prior-driven lanes (stage 2)
- Depends on T2.
- New lattice-fit lane finder in `lane_detect.py` (or `lane_fit.py`), no band input. Returns exactly `lane_count` lanes with `estimated` flag and evidence.
- `--gel-lanes N` for cropped photos: width, count from `N × lane_pitch_cm`.
- Remove lane-stage dependence on band rows (pitch-from-row, snap anchors, evidence by bands); keep helpers only if used.
- Done when: reference photo gives 15 lanes within 3 analysis px of current; synthetic cases (5-lane faint+streak, empty lane, off-centre) pass; old lane-specific selftests are replaced or deleted with reasons.

## T4 — Bands on frozen lanes (stage 3)
- Depends on T3.
- `lanes_and_bands` split into `find_lanes` and `find_bands(frozen_lanes)`; `probe`/`render` call them in sequence; no back-references.
- Import-direction test (lane stage never imports band code).
- Done when: band rows and MW report on the reference photo match today's within 1 kDa / 0.5 analysis px; all callers (`debug_overlay`, `mw_report`, `render`) use the new return shapes.

## T5 — Detection record JSON
- Depends on T4. Format fixed by spec §7: cropped-gel coordinates in cm, origin top-left of the cropped face, no photo coordinates, no gel box.
- New `record.py` converts stage outputs (photo px) to the record at the end; `probe` prints it, `--record PATH` writes it; `mw_report` reads the record instead of raw `lb` (y in cm instead of analysis px; calibration math unchanged since the map is affine).
- Done when: record round-trips (write, read, MW report identical to the pre-record result within 0.1 kDa); a test asserts the key set and that no photo-space key is present; fields documented in the conventions reference.

## T6 — Lane selection: crop and stitch (stage 4)
- Depends on T4; the `crop` part of the record needs T5 (build the crop operation first, add it to the record when T5 lands).
- New `lane_select.py`: parse `--keep-lanes` (commas, ranges, validate against `lane_count`), compute strips from frozen lane centres (midpoint rule, gel edges at the ends), return the `crop` structure.
- `render`: build the output canvas from strips with a white splice gap and a thin centred line between non-adjacent strips (`--splice-gap-cm`, default 0.15); lane and marker label x positions mapped through the strips; prints the splice disclosure reminder.
- Marker handling: if no marker lane is kept and `--marker-policy` is absent, exit non-zero naming the cropped-out marker lanes; `calibrate-only` and `keep` implemented per spec §6b.
- MW report: sample lanes limited to kept lanes; calibration still from all marker lanes.
- Done when: the acceptance cases in spec §8 for lane selection pass (single run, two strips with gap, missing-marker failure, MW equal to full-gel values for the same lanes); strip widths match `n × pitch`; `selftest` covers the strip math (midpoints, edges, adjacent vs non-adjacent).

## T7 — Docs and tests
- Depends on T1–T6 (can start with T1).
- `SKILL.md`: Workflow step 1 asks for the gel preset (mandatory) and which lanes the user needs; a checkpoint for the marker-lane-cropped-out question and the splice disclosure; Scope states it; checkpoints for missing preset and for the cross-check warning.
- Conventions reference: new stage description (replaces sections 2a–2c where obsolete); `special-cases-and-pitfalls.md`: mark the lane-heuristic pitfalls as historical.
- `test-prompts.json`: add cases (no preset given → ask; preset/box disagreement; cropped photo with `--gel-lanes`; user asks for lanes 1,2,8-9 → ask which lanes up front, splice disclosure; marker lane cropped out → ask the policy).
- Done when: no doc claims a "no preset" path; `selftest` count updated everywhere.
