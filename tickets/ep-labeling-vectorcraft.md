# Tickets: ep-labeling VectorCraft output

Spec: `specs/ep-labeling-vectorcraft.md`. Order = dependency order. Each ticket must keep `selftest` green. Tests that need the app are a manual checklist in the ticket close-out; `selftest` never needs VectorCraft.

```
V0 ─┬─ V1 ── V3 ─┬─ V4 ─┬─ V5 ── V6 ── V7 ── V9
    └─ V2 ───────┼──────┘          ↑
                 └─ V8 (parallel) ─┘
```

## V0 — Spike: VectorCraft 0.14 facts (gate)
- Against `/Applications/VectorCraft.app` 0.14.0 with `--control`, find out and write down in `references/vectorcraft.md`:
  - the file format (§5): does a minimal hand-written file (one artboard, one embedded PNG, one line, one text) open without dialogs? Which document fields are required? Save it from the app and diff.
  - are object `name`s kept across a save? Can a name be set when creating a line or text? Does `document.inspect` return names, layer membership, line endpoints, text runs/style and `xf`?
  - commands to: move, delete or create a path/text by id; set text position; set an artboard rect; clip an image (clipping mask or crop); lock/hide/set a layer non-printing; create an artboard.
  - export: can one artboard be exported alone? What does `scale` mean? Is pHYs written?
  - does an unknown key in `document.metadata` survive a save (decides V-D2)?
  - what is easiest for trimming (decides V-D1)?
- Commit `templates/blank.vectorcraft` (minimal app-saved document) as the writer's base.
- Done when: every question has a measured answer with the exact JSON request/response, and the spec §5/§6 is corrected where reality differs.

## V1 — `.vectorcraft` writer and reader
- Depends on V0.
- New `vc_file.py`: `write(doc_model, blobs, path)` and `read(path) -> (json, blobs)`; blob packing (`\n\0VCBLOBS\0`, offsets); id allocation and `next_id`; helpers for artboard, layer, line, point text, embedded image with `xf`, clipped image.
- Done when: `selftest` round-trips a synthetic document (write → read → same names/geometry/blob bytes); the written file opens in VectorCraft without dialogs (manual) and looks as built.

## V2 — Control-port client
- Depends on V0.
- New `vc_control.py`: attach or launch (remember ownership), dialog clearing, `inspect()`, `execute(cmd, params)`, `save()`, and `export_png(path, artboard, dpi)` with mtime polling, the crop fallback and the pHYs post-process. Timeouts and clear errors ("instance without --control — save, quit, re-run").
- `doctor` checks that VectorCraft.app is present and its version (warn ≠ 0.14.x).
- Done when: unit tests run against a fake line-JSON TCP server in `selftest`; the manual check exports a PNG of the right physical size with pHYs = dpi.

## V3 — Workbench model (build + read back)
- Depends on V1.
- `workbench.py`: a plain data model (gel image, lane pairs, window, marker bands with MW, trimmed pairs, metadata) ↔ document objects per spec §4.2.
  - `from_detection(stage outputs, lane names, marker preset, trim)` → model.
  - `to_objects(model)` for the writer; `from_inspect(inspect_json)` and `from_file(read())` → model.
  - Validation per spec §4.3, with messages that say what to fix in the app.
  - Label matching on lane add/delete (nearest previous centre, spec §4.4).
- Done when: `selftest` covers pairing by x order, odd/overlapping lines, missing window, band names, the marker lane found from the bands, and matching after an insert or delete.

## V4 — Analysis from the Workbench + `fig.mw.json` v2
- Depends on V3 (model only).
- `profile.py`: per-lane mean grey over [L, R] × [top, bottom] on the embedded gel image; baseline → `ink`; peaks with MAD-relative prominence (no absolute ink floor); log-linear calibration from `band <MW>` lines; MW per band.
- `mw_report.py` writes v2 (spec §7) and keeps the terminal table.
- Done when: `selftest` checks `gray` against an independent mean on a synthetic gel; a faint lane (3 grey levels deep) still yields its peak; deleting a band changes the fit; extrapolation flags as before.

## V5 — Figure and Profiles artboards
- Depends on V3, V4.
- Figure: kept segments from the lane lines (splice at mid-gap, as today), clipped gel instances, splice lines, page rule margins, `label lane n` / `label band <MW>` at their anchors, nominal font sizes.
- Profiles: per-lane `ink` curves, MW axis from the calibration, called bands marked. Show the user both layouts (V-D3) on one DGC photo before settling.
- `plan_sync(old_doc_model, new_model) -> ops`: the minimal list of create/move/delete/replace operations, respecting the ownership table (spec §4.4). It is used by both `render` (from empty) and `sync`.
- Done when: `selftest` asserts the ops for the spec §11 scripted round trip, and that no op ever touches a label's text or style.

## V6 — `render` end to end; remove old outputs
- Depends on V2, V5.
- `render` → model → write `fig.vectorcraft` → open in the app → export `fig.png` → `fig.mw.json`. `--trim-lanes` is added; `--keep-lanes`, `--svg`, `--no-editor`, `--debug`, `--record` and the `export` subcommand exit with "replaced by …".
- Delete `editor.py`, `templates/editor.html`, `scene_svg.py`, `scene_png.py`, `debug_overlay.py`, `record.py` and their tests; fold or keep `scene.py` per spec §10.
- Overwrite guard per V-D4.
- Done when: the reference photo and the five DGC photos each give exactly three files; the documents open cleanly with the layers in §4.2; `fig.png` has the right size and dpi; the `selftest` count is updated.

## V7 — `sync`
- Depends on V6.
- `sync fig.vectorcraft`: attach → `inspect` → model (validate; on error, change nothing and print the fixes) → analysis → `plan_sync` → execute ops → save → export PNG → rewrite `fig.mw.json` → print the diff summary. A lane label created for a new lane says `?` and the agent asks for its name.
- Done when: the live checklist in spec §11 passes and is recorded; a second `sync` is a no-op; the validation errors are reproduced live once each.

## V8 — Better seed: wells-anchored lane phase + DGC fixtures
- Depends on V3 (independent of V4–V7).
- Stage 2: detect the comb gaps at the gel top and use them to fix the grid phase; the column-ink fit becomes a tie-breaker. Log which source won.
- Fixtures: IMG_1496–1500 in a local fixture folder (not committed if large; record their hashes), with the user-confirmed layouts. IMG_1496 has its marker in well 1. **Ask the user to confirm the other four**, and whether IMG_1497 really has two ladder-like lanes (M and sample 1) before using it as truth.
- Done when: on all five photos the marker lane is seeded in the confirmed well, and the reference photo is unchanged.

## V9 — Docs, workflow, test prompts
- Depends on V6, V7.
- `SKILL.md`: the deliverables are `fig.vectorcraft` + `fig.png` (+ `fig.mw.json`); the user's loop is "open in VectorCraft, fix the lines on the Workbench, tell the agent to sync"; text-only changes go through the control port, then `sync`; never re-render to fix lanes or bands. Keep the user's pending wording change on the ask-up-front step.
- References: replace the scene/editor/SVG sections with the document structure (spec §4), ownership, sync errors and `fig.mw.json` v2; link `references/vectorcraft.md`.
- `test-prompts.json`: "lane 3 is off, I moved it — update" → `sync`; "rename lane 2 to WT" → control port + `sync`; "drop lanes 1–2" → move to `trimmed` or `--trim-lanes`; "the 35 kD band isn't there" → delete `band 35` + `sync`.
- Mark `specs/ep-labeling-editable-output.md` as superseded.
- Done when: no doc mentions `fig.html`, `export`, `--svg`, `--debug PNG` or `--record` except as removed; the `selftest` count is consistent everywhere.
