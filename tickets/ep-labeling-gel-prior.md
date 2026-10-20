# Tickets: ep-labeling gel-preset prior

Spec: `specs/ep-labeling-gel-prior.md`. Order = dependency order. Each ticket is shippable on its own and must keep `selftest` green.

## T1 — Preset schema and mandatory gate
- Extend `data/gel_size.json` with `lane_count`, `lane_pitch_cm` (keep `width_cm`, `height_cm`, trims), optional `anchor`.
- `config.load_gel_size`: validate required keys and the `lane_count × lane_pitch_cm ≈ width_cm` invariant.
- `probe` and `render`: require `--gel-size`; drop the `--gel-cm`-only path (keep as override of width/height).
- Done when: no preset → exit ≠ 0 with the spec message; bad invariant → clear error; `selftest` has a test for both.

## T2 — Gel area from the preset (stage 1)
- Depends on T1.
- New `gel_area.py`: takes detection + preset, returns frozen `gel_box`, `angle`, `px_per_cm`, with sources.
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
- Depends on T4. Needs the coordinate-system decision in spec §7 first (blocking question to the owner).
- New `record.py` builds the record; `probe` prints it, `--record PATH` writes it; `mw_report` reads the record instead of raw `lb`.
- Done when: record round-trips (write, read, MW report identical), fields exactly as spec §7, documented in the conventions reference.

## T6 — Docs and tests
- Depends on T1–T5 (can start with T1).
- `SKILL.md`: Workflow step 1 asks for the gel preset (mandatory); Scope states it; checkpoints for missing preset and for the cross-check warning.
- Conventions reference: new stage description (replaces sections 2a–2c where obsolete); `special-cases-and-pitfalls.md`: mark the lane-heuristic pitfalls as historical.
- `test-prompts.json`: add cases (no preset given → ask; preset/box disagreement; cropped photo with `--gel-lanes`).
- Done when: no doc claims a "no preset" path; `selftest` count updated everywhere.
