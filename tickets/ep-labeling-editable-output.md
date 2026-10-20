# Tickets: ep-labeling editable output

Spec: `specs/ep-labeling-editable-output.md`. Order = dependency order. Each ticket is shippable on its own and must keep `selftest` green.

## E1 — Split layout from drawing; write the scene
- New `scene.py`: build the scene (spec §4) from what `cmd_render` computes today (label items, margins, gel face size). Also `scene_load`/`scene_write`, schema validation, and the page rule (`page_size_cm(scene)`).
- `cmd_render`: label placement code moves out of the drawing code. Lane labels and marker labels become scene items with `id`, `role`, `bind` and `anchor`. Margins come from the page rule in cm, not from `margin_frac` in canvas px. `margin_frac` → `margin_min_cm` happens once, at scene build.
- `render` writes `fig.scene.json` and `fig.gel.png` (gel face, no text, splice line included). It still draws `fig.png` with the existing code path in this ticket.
- Done when: the reference photo's `fig.png` is pixel-identical to before (full gel and `--keep-lanes 1,2,8-9`); the scene has one item per drawn label; `fig.gel.png` equals the gel region of `fig.png`; `selftest` checks the schema, the page rule, and the no-photo-keys rule.

## E2 — PNG writer from the scene, and `export`
- Depends on E1.
- New `writers/png.py` (or `scene_png.py`): scene + dpi → PNG via ImageMagick. Gel composited at its place, labels via `annotate_cmd` from the anchors, pHYs dpi.
- `cmd_render` draws `fig.png` through this writer. The old drawing code is deleted.
- New `export` subcommand (spec §7), scene input only for now (HTML input comes in E4).
- Done when: `render`'s `fig.png` is still pixel-identical to the pre-E1 output; `export fig.scene.json --png` reproduces it exactly; editing one label's text in the scene changes only that label's area and the margin (spec §9 round trip); `selftest` covers scene→PNG on a synthetic gel.

## E3 — SVG writer
- Depends on E1 (independent of E2 apart from the shared page rule).
- New SVG writer per spec §5: size and viewBox in cm, embedded gel, one `<text>` per item with `id`/`data-role`/`data-bind`, outline as an underlying stroked copy, baseline offset from font metrics.
- `render --svg` and `export --svg`.
- Done when: the SVG opens at the scene's page size; item count and ids match the scene; label centres are within the spec §8 tolerance of the PNG (checked by rasterising the SVG with `magick` in `selftest`, or manually in Inkscape if the rasteriser's font handling makes the automatic check unreliable — say which in the ticket close-out); opened once in Illustrator or Inkscape and once via PowerPoint's Insert → Picture, with notes on what each did.

## E4 — HTML editor
- Depends on E1. Uses E3's SVG logic for "Download SVG" (port the mapping to JS; keep one description of it in the spec, not two diverging implementations — note any difference in the template's header comment).
- `templates/editor.html`: all HTML/CSS/JS, two placeholders (gel data URI, scene JSON). No external resources.
- Editing per spec §6: select, edit text in place and in the panel, delete, add `free` label, drag, arrow-nudge, multi-select, bulk size/colour by role, undo/redo, gel locked, live page rule.
- Output per spec §6: PNG at the chosen dpi with a pHYs chunk, SVG, Save (new HTML with the current scene), scene JSON.
- `render` writes `fig.html` (unless `--no-editor`); `export` accepts `fig.html` as input (read `#scene` and the embedded gel).
- Done when: the editor checklist in spec §9 passes in a browser (Chrome and Safari); the downloaded PNG has the right dpi (`magick identify -format "%x"`) and meets spec §8 against `export` for the same scene; `selftest` extracts the scene and image from a generated HTML; the pHYs function is tested under `node` when available (skip with a notice otherwise).

## E5 — Docs, skill workflow, tests
- Depends on E1–E4 (can start with E1).
- `SKILL.md`: deliverables are `fig.png` plus `fig.html`; tell the user they can edit labels themselves in `fig.html`; for text-only requests the agent edits the scene and runs `export`, never `render`; a checkpoint that analysis changes (lanes, marker preset, crop) still need `render`.
- Conventions reference: the scene schema (spec §4), the page rule, the file list (spec §3), `export`.
- `test-prompts.json`: "rename lane 3 to WT" → `export` path, no re-detection; "remove the 15 kDa label" → same; "keep only lanes 1–5" → `render` path.
- Done when: no doc describes labels as only burnt into the PNG; the `selftest` count is updated everywhere.

## Open decisions to settle before E1/E4
- E-D1 default outputs, E-D2 editor dpi default, E-D3 MW labels in the editor (spec §11).
