# Tickets: ep-labeling VectorCraft output

Spec: `specs/ep-labeling-vectorcraft.md`. Order = dependency order. Each ticket must keep `selftest` green. Tests that need the app are a manual checklist in the ticket close-out; `selftest` never needs VectorCraft.

```
V0 ─┬─ V1 ── V3 ─┬─ V4 ─┬─ V5 ── V6 ── V7 ── V9
    └─ V2 ───────┼──────┘          ↑
                 └─ V8 (parallel) ─┘
```

## V0 — Spike: VectorCraft 0.14 facts (gate) — DONE 2026-10-22, see `references/vectorcraft.md`
- Against `/Applications/VectorCraft.app` 0.14.0 with `--control`, find out and write down in `references/vectorcraft.md`:
  - the file format (§5): does a minimal hand-written file (one artboard, one embedded PNG, one line, one text) open without dialogs? Which document fields are required? Save it from the app and diff.
  - are object `name`s kept across a save? Can a name be set when creating a line or text? Does `document.inspect` return names, layer membership, line endpoints, text runs/style and `xf`?
  - commands to: move, delete or create a path/text by id; set text position; set an artboard rect; clip an image (clipping mask or crop); lock/hide/set a layer non-printing; create an artboard.
  - export: can one artboard be exported alone? What does `scale` mean? Is pHYs written?
  - does an unknown key in `document.metadata` survive a save (decides V-D2)?
  - what is easiest for trimming (decides V-D1)?
- Commit `templates/kinds.vectorcraft` (app-saved, one object per kind) as the writer's base.
- Done when: every question has a measured answer with the exact JSON request/response, and the spec §5/§6 is corrected where reality differs.

## V1 — `.vectorcraft` writer and reader — DONE 2026-10-22
- Close-out: `scripts/vc_file.py`; selftest 51–54 (round trip, three encodings, field completeness against the template, write gates). Manual: a `vc_file`-written three-artboard document opened in VectorCraft 0.14.0 with `warnings: []` and no dialog, with layer flags as written. Figure export was 1083 × 833 px at 300 dpi. An app-saved copy read back with identical names, lines, label style and image bytes.
- Depends on V0.
- New `vc_file.py`: `write(doc_model, images, path)` (inline base64, objects deep-copied from `templates/kinds.vectorcraft`) and `read(path)` accepting inline, `\0VCBLOBS\0` and gzip; id allocation and `next_id`; helpers for artboard, layer, line, point text, embedded image with `xf`, clipped image.
- Done when: `selftest` round-trips a synthetic document (write → read → same names/geometry/blob bytes); the written file opens in VectorCraft without dialogs (manual) and looks as built.

## V2 — Control-port client — DONE 2026-10-22
- Close-out: `scripts/vc_control.py` (`connect`, `Client.call/execute/open/clear_dialogs/save/export_png/batch/undo_group/quit`, `png_info`), and `doctor` reports the app, its version and the port state. selftest 55–59 run against a fake line-JSON server (protocol and split replies, dialogs, save polling past a half-written file, export dpi/size checks, undo group, batch, connect and launch). Mutation-checked: a save that doesn't wait for a parseable file and an undo group without rollback both fail selftest.
- Manual, VectorCraft 0.14.0:
  - `connect` launched the app with `--control` in 0.5 s.
  - Figure export at 300 dpi was 1083 × 833 px with pHYs 300; Workbench at 150 dpi was 625 × 417.
  - `save` waited for a complete, parseable file.
  - An undo group rolled back on error; a successful group was one history step (`ep-labeling sync`) undone by one `edit.undo`.
  - A batch with a failing step changed nothing.
  - With the app opened normally (no `--control`), `connect` and `doctor` both said to save and quit.
- Depends on V0.
- New `vc_control.py`: attach or launch (remember ownership), dialog clearing, `inspect()`, `execute(cmd, params)`, `save()` (poll until newer **and** parses), `export_png(path, artboard, dpi)` via `document.export {artboard, ppi}` (pHYs comes from the app), and an undo-group context manager (`ep-labeling sync`, cancel on error). Timeouts and clear errors ("instance without --control — save, quit, re-run").
- `doctor` checks that VectorCraft.app is present and its version (warn ≠ 0.14.x).
- Done when: unit tests run against a fake line-JSON TCP server in `selftest`; the manual check exports a PNG of the right physical size with pHYs = dpi.

## V3 — Workbench model (build + read back) — DONE 2026-10-22
- Close-out: `scripts/workbench.py`.
  - **Build:** `from_record` builds the model from the record, which is already in gel-face cm, so no stage code is touched. It keeps the old "labels vs band count" check, and a `rows: 2` pair becomes two band lines.
  - **Write:** `make_layers` creates the spec §4.2 layers; `add_workbench` draws the Workbench.
  - **Read back:** `from_document` binds by **layer**, so an unnamed new line counts. Positions come from the Workbench gel image's own `xf`. All problems are reported in one message.
  - **Validate:** `validate` checks the invariants.
  - **Lane matching:** `match_lanes` and `lane_names_after` move lane names with their lanes.
- Tests: selftest 60–64 cover the build, the round trip, reading back edits (moved and tilted lines, a deleted band, a new unnamed pair, a pair moved to `trimmed`, a moved window), every validation error, and lane matching.
- Manual, IMG_1496 in VectorCraft 0.14.0: the document opened cleanly and the Workbench rendered as intended. A live read-back equalled the model with 0 cm difference. Simulated drags read back exactly: lane 2 moved +5 pt = 0.1764 cm, `band 250` moved +1.5 pt, and deleting `band 35` removed that band.
- Seen on the real gel (for V8): lane 1 is seeded at 0.15 cm, hugging the left edge, so the ladder lands in lane 2. Several band lines also sit about 0.5 mm off their bands, the known bias in the automatic rows. Both are now fixable by hand on the Workbench.
- Default window: the whole gel face, with the bottom 3% excluded (the bottom-edge gradient). Band lines reach 0.15 cm past their lane, and window lines past the gel, so they are easy to grab.
- Depends on V1.
- `workbench.py`: a plain data model (gel image, lane pairs, window, marker bands with MW, trimmed pairs, metadata) ↔ document objects per spec §4.2.
  - `from_detection(stage outputs, lane names, marker preset, trim)` → model.
  - `to_objects(model)` for the writer; `from_inspect(inspect_json)` and `from_file(read())` → model.
  - Validation per spec §4.3, with messages that say what to fix in the app.
  - Label matching on lane add/delete (nearest previous centre, spec §4.4).
- Done when: `selftest` covers pairing by x order, odd/overlapping lines, missing window, band names, the marker lane found from the bands, and matching after an insert or delete.

## V4 — Analysis from the Workbench + `fig.mw.json` v2 — DONE 2026-10-22
- Close-out: `scripts/profiles.py` (`gel_gray`, `lane_profile`, `ink_trace`, `noise_of`, `call_bands`, `analyze`), and `mw_report.format_report_v2` / `write_v2`. The report is in gel-face cm and carries the raw `gray`/`baseline`/`ink` per lane (about 300 KB for 15 lanes). selftest 65–68 cover pixel arithmetic, faint/flat/ramp lanes, a dense ladder built from IMG_1496's measured bands (with the counter-check that a one-shot MAD fails), and v2 end to end against an independent mean.
- Two corrections to the stage-3 recipe, both measured on IMG_1496:
  1. **Noise is the background MAD with band rows clipped.** Differencing sat on the floor and called texture; a one-shot MAD called only 8 of 11 ladder bands.
  2. **Prominence is local.** A faint band in a strong neighbour's negative lobe has a negative absolute height.
- A morphological closing baseline was tried and rejected: it called bands on flat lanes.
- **Five DGC photos:**
  - Calibration R² is 0.949–0.964, and every marker lane calls all its bands. Faint sample bands are found (e.g. 1496 lanes 5, 6, 13, 14 at SNR 5–11), and most empty lanes call nothing.
  - **For V6:** the seeded `band <MW>` lines sit **0.13–0.50 mm below** the profile peaks on all five photos, always in the same direction (the old stage-3 rows). V6 should snap seeded band lines to the marker lane's called peaks (nearest, within 0.1 cm) before writing the document.
  - **For V8, ask the user:** lanes labelled "1" (next to the marker) show full 11-band ladders in 1497–1500. Lanes labelled empty also show 11-band patterns: 1499 lanes 2–4, 1500 lanes 2–3. Either the layouts given are off, or the grid is shifted; the user needs to look.
- Depends on V3 (model only).
- `profile.py`: per-lane mean grey over [L, R] × [top, bottom] on the embedded gel image; baseline → `ink`; peaks with MAD-relative prominence (no absolute ink floor); log-linear calibration from `band <MW>` lines; MW per band.
- `mw_report.py` writes v2 (spec §7) and keeps the terminal table.
- Done when: `selftest` checks `gray` against an independent mean on a synthetic gel; a faint lane (3 grey levels deep) still yields its peak; deleting a band changes the fit; extrapolation flags as before.

## V5 — Figure and Profiles artboards — DONE 2026-10-22 (V-D3 awaiting the user)
- Close-out: `scripts/figure.py`.
  - **One description:** `targets()` lists every agent-owned object once. `build_document()` writes them for render; `plan_sync()` diffs them against the live `document.json` into ops (artboard, create, delete, move, rename, set_text, stroke, reshape).
  - **`apply_ops_json()`** applies ops to a file's JSON, so the round trip is tested without the app; V7's control executor mirrors it.
  - Also: `vc_file.polyline`, and an app-made `polyline` added to `templates/kinds.vectorcraft`.
- Layout and analysis decisions:
  - The Figure goes **below** the Workbench, so resizing never overlaps it; Profiles go to the right. Artboard order is Figure, Workbench, Profiles.
  - Splices are a 0.15 cm white gap, as before (no extra line object).
  - Marker labels are spread with a minimal-displacement 1-D layout (`spread`), keeping order, so close bands (250/150/100, 40/35) no longer collide.
  - The default window top is 0.15 cm: the wells' U outlines put a spike at the start of every curve.
  - Each Profiles curve is scaled to at least 1/4 of the strongest sample lane, so empty lanes don't look like bands.
- Ownership is enforced by plan_sync: labels only ever get move, rename, create or delete. Test 73 checks that a user's text and colour survive a sync. A "?" placeholder counts as unnamed.
- Tests: selftest 71–73 cover `spread`; render structure and idempotence in both layouts; and the spec §11 scripted round trip (moved lane, deleted band, new unnamed pair, interior lane trimmed, label renamed and recoloured), followed by a second sync that yields 0 ops.
- **V-D3:** both layouts are open for the user (`~/Downloads/DGC/vc-demo/IMG_1496.v5-{stack,strips}.vectorcraft`); the default is `stack`.
- Depends on V3, V4.
- Figure: kept segments from the lane lines (splice at mid-gap, as today), clipped gel instances, splice lines, page rule margins, `label lane n` / `label band <MW>` at their anchors, nominal font sizes.
- Profiles: per-lane `ink` curves, MW axis from the calibration, called bands marked. Show the user both layouts (V-D3) on one DGC photo before settling.
- `plan_sync(old_doc_model, new_model) -> ops`: the minimal list of create/move/delete/replace operations, respecting the ownership table (spec §4.4). It is used by both `render` (from empty) and `sync`.
- Done when: `selftest` asserts the ops for the spec §11 scripted round trip, and that no op ever touches a label's text or style.

## V6 — `render` end to end; remove old outputs — DONE 2026-10-22
- Close-out: `gel_photo.py` is rewritten around the new pipeline:
  1. Stage 1 (find, straighten and crop the gel face) → gel-face PNG at `--dpi` (default now 300).
  2. `workbench.from_image`: lanes from the wells, marker lines from profile peaks.
  3. `profiles.analyze` → `figure.build_document` → `fig.vectorcraft`.
  4. VectorCraft (`--port`, launched if needed) exports `fig.png` from artboard 0, and `fig.mw.json` v2 is written with the terminal table.
- Also in `render`:
  - **Marker lanes:** it tries each listed marker lane (`lanes.json` `marker_lanes`, then the marker preset's) until one pairs. IMG_1497 falls back from its damaged lane 1 to lane 2.
  - **Scale check:** it now compares the measured well pitch with the preset (it was the old stage-2 cheap estimate, which gave false 62–107% warnings).
  - **New flags:** `--trim-lanes`, `--profiles stack|strips`, `--port`.
  - **V-D4 (overwrite guard):** an existing output is copied to `.vectorcraft.bak`. If the document is open in VectorCraft with unsaved changes, render refuses.
  - **Removed flags:** `--keep-lanes`, `--svg`, `--no-editor`, `--debug`, `--record`, `--pair-force`, `--marker-policy`, `--margin-frac`, `--outline`, `--no-outline`, `--font`, `--mw-method`, `--splice-gap-cm` and the `export` subcommand each fail with "removed: use …".
- `probe` prints stage 1, the well seed and every lane's called bands, without writing files. `sync` is wired here (executor: V7).
- **Deleted:** `editor.py`, `templates/editor.html`, `scene.py`, `scene_png.py`, `scene_svg.py`, `debug_overlay.py`, `record.py`, `lane_fit.py` and `band_fit.py`, plus the dead functions left in `geometry`, `text_layout`, `lane_select` and `mw_report`.
- **selftest:** the 32 tests of removed code are gone. The import-direction test now encodes the new layering. Test 74 is new (removed flags and pitch check). Total 42/42.
- **Live, five DGC photos** (`~/Downloads/DGC/v6/`): each gives exactly `IMG_x.vectorcraft`, `IMG_x.png` (1213 × 859 px, pHYs 300 dpi) and `IMG_x.mw.json`. Every seed came from the wells, and every marker pairs 11 bands. Lane names there are placeholders (ladder wells "M", the rest numbered); only IMG_1496's layout is user-confirmed.
- Depends on V2, V5.
- `render` → model → write `fig.vectorcraft` → open in the app → export `fig.png` → `fig.mw.json`. `--trim-lanes` is added; `--keep-lanes`, `--svg`, `--no-editor`, `--debug`, `--record` and the `export` subcommand exit with "replaced by …".
- Delete `editor.py`, `templates/editor.html`, `scene_svg.py`, `scene_png.py`, `debug_overlay.py`, `record.py` and their tests; fold or keep `scene.py` per spec §10.
- Overwrite guard per V-D4.
- Done when: the reference photo and the five DGC photos each give exactly three files; the documents open cleanly with the layers in §4.2; `fig.png` has the right size and dpi; the `selftest` count is updated.

## V7 — `sync` — DONE 2026-10-22
- **Executor (subagent, branch `refs/heads/worktree-agent-a402e487170abb8cb`, merged):** `scripts/sync_exec.py` `apply_ops_live` runs every `plan_sync` op kind through the control port inside one `ep-labeling sync` undo group. `scripts/selftest_sync.py` runs 7 fake-port checks, hooked into the main selftest. Its live check (its own instance on port 7798) covered:
  - the round trip, followed by a second plan with 0 ops;
  - an injected failure, after which the document was byte-identical and the history had no step;
  - one ⌘Z undoing the sync exactly, and redo restoring it.
- **Command:** `gel_photo.py sync fig.vectorcraft [--port] [--dry-run]` runs these steps:
  1. Attach to the instance (launching it if needed) and activate or open the document.
  2. Read `document.json` → `workbench.from_document`. If the Workbench can't be read, it prints every fix needed and changes nothing.
  3. Run the analysis on the gel image read from the saved file, then `plan_sync` → `apply_ops_live`.
  4. Save, export `fig.png` at the dpi from ep-meta, and rewrite `fig.mw.json` with the current label texts as lane names.
  5. Print what changed (moved, new, deleted and trimmed lanes, band lines moved or deleted, R² before → after) and ask for names of new "?" lanes.
- **Live acceptance (spec §11),** in the user's instance on port 7797, with `~/Downloads/DGC/v6/IMG_1496.vectorcraft`. Edits: lane 3 dragged +0.15 cm, lane 14 moved to `trimmed`, `band 35` deleted, lane-2 label renamed "WT" and coloured red.
  - The sync summary reported exactly those edits, and R² went 0.9470 → 0.9483.
  - The update was one undo step (artboard 1, delete 136, create 133, move 3, set_text 1, stroke 2, reshape 1), in about 9 s.
  - "WT" kept its text and red (#d01010). Label "3" re-centred on the moved lane. Lane 14's label and the 35 kD label were removed, and the Figure split into `gel seg 1` and `gel seg 2` with a splice gap.
  - A second `sync` reported 0 changes.
- Fixed during the live check: the report notes listed every lane as unnamed (they came from the pre-relabel analysis).
- Depends on V6.
- `sync fig.vectorcraft`: attach → `inspect` → model (validate; on error, change nothing and print the fixes) → analysis → `plan_sync` → execute ops → save → export PNG → rewrite `fig.mw.json` → print the diff summary. A lane label created for a new lane says `?` and the agent asks for its name.
- Done when: the live checklist in spec §11 passes and is recorded; a second `sync` is a no-op; the validation errors are reproduced live once each.

## V8 — Better seed: wells-anchored lane phase + DGC fixtures — DONE 2026-10-22
- Close-out: `scripts/well_grid.py` fits an even comb anchored on the wells. `workbench.from_image` seeds the Workbench from the gel-face image only: lanes from the comb, marker lines from the marker lane's profile peaks. The preset `lane_pitch_cm` changed from 0.565 to 0.52. selftest 69–70 cover synthetic wells (pitch, per-lane centres, a 9%-wrong prior, the no-wells fallback) and `from_image` (marker lines from peaks, refusal listing the called y values).
- **Ground truth:** the user hand-placed IMG_1499's 15 lanes in VectorCraft (`~/Downloads/DGC/vc-demo/IMG_1499.vectorcraft`, saved 2026-10-22). The seed matches them to **0.015 cm mean, 0.042 cm max**, below the ±0.03 cm jitter of the hand placement. All five photos seed from the wells with pitch 0.510–0.522 cm.
- **The layouts, as the wells show them:** IMG_1496 has its ladder in well 1 (as the user said). There are ladders in wells 1–3 (IMG_1497), 1–2 (IMG_1498), 1–5 (IMG_1499) and 1–4 (IMG_1500). The "blank"/"1" lanes in the original batch were ladders.
- Fixed along the way:
  - the comb window default was bound at definition, so tuning `FRAC` did nothing;
  - band prominence is now measured on raw grey, which removes the ghost peaks between strong bands.
- **Known limit:** IMG_1497 lane 1 sits on the gel's damaged left edge. Its lane-wide noise (1.88) hides the faint 250 kD band, so it calls 10/11 and seeding refuses. Lanes 2–3 pair cleanly (choose another marker lane).
- Depends on V3 (independent of V4–V7).
- Stage 2: detect the comb gaps at the gel top and use them to fix the grid phase; the column-ink fit becomes a tie-breaker. Log which source won.
- Fixtures: IMG_1496–1500 in a local fixture folder (not committed if large; record their hashes), with the user-confirmed layouts. IMG_1496 has its marker in well 1. **Ask the user to confirm the other four**, and whether IMG_1497 really has two ladder-like lanes (M and sample 1) before using it as truth.
- Done when: on all five photos the marker lane is seeded in the confirmed well, and the reference photo is unchanged.

## V9 — Docs, workflow, test prompts — DONE 2026-10-22
- Close-out: docs only, no code touched.
  - `SKILL.md` rewritten around the VectorCraft loop: ask up front (marker preset, gel preset, lanes to trim, ladder wells, then lane names; the user's "DO NOT research … ASK" wording kept verbatim) → `doctor` → optional `probe` → `render` → user edits the Workbench → `sync`. Text-only changes go through the control port (or the user types them), then `sync`; re-render only for a new photo, crop or preset. Checkpoints cover Workbench validation errors, an instance without `--control`, an unsaved document blocking render, and a marker lane that can't pair.
  - `references/photo-labeling-conventions.md` rewritten: live config fields only (`marker_lanes` = candidate ladder wells), stage 1, document structure and ownership (spec §4), Workbench reading rules and every validation message, the well-anchored seed (V8 numbers), band calling and calibration (V4/V8), `fig.mw.json` v2, Figure/Profiles layout, `sync` semantics and a label-text recipe through the port, and a removed → replacement table.
  - `references/special-cases-and-pitfalls.md`: kept `rows: 2`, `--gel-lanes` and the stage-1/IM7 pitfalls; added the damaged gel edge, wells cut off, the 0.565 → 0.52 pitch history and the stale-installed-copy lesson; dropped the pitfalls of removed features.
  - `test-prompts.json` 0.5.0: 31 cases. Old export/scene routes are replaced, and new cases cover move-a-lane → `sync`, rename → label text + `sync`, drop lanes → `trimmed`/`--trim-lanes`, a missing 35 kD band → delete `band 35` + `sync`, an added lane → `sync` and ask for the `?` name, plus no `--control`, re-render overwrite and a stale-copy report.
  - `specs/ep-labeling-editable-output.md` is marked superseded.
- `selftest` is 49/49. Docs don't hard-code the count. `env_check.py`'s doctor line still says "16 项" (stale, code).
- Depends on V6, V7.
- `SKILL.md`: the deliverables are `fig.vectorcraft` + `fig.png` (+ `fig.mw.json`); the user's loop is "open in VectorCraft, fix the lines on the Workbench, tell the agent to sync"; text-only changes go through the control port, then `sync`; never re-render to fix lanes or bands. Keep the user's pending wording change on the ask-up-front step.
- References: replace the scene/editor/SVG sections with the document structure (spec §4), ownership, sync errors and `fig.mw.json` v2; link `references/vectorcraft.md`.
- `test-prompts.json`: "lane 3 is off, I moved it — update" → `sync`; "rename lane 2 to WT" → control port + `sync`; "drop lanes 1–2" → move to `trimmed` or `--trim-lanes`; "the 35 kD band isn't there" → delete `band 35` + `sync`.
- Mark `specs/ep-labeling-editable-output.md` as superseded.
- Done when: no doc mentions `fig.html`, `export`, `--svg`, `--debug PNG` or `--record` except as removed; the `selftest` count is consistent everywhere.
