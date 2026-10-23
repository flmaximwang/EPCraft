> **Superseded by `specs/ep-craft-vectorcraft.md` (2026-10-22).** The scene file, SVG writer, HTML editor and `export` described here were removed in V6; kept for history only.

# Spec: ep-craft — editable output (scene file, SVG, HTML editor)

Status: draft · Scope: `skills/ep-craft` · Written 2026-10-20 · Builds on `specs/ep-craft-gel-prior.md`

## 1. Problem

`render` works out where each label goes and burns it straight into the PNG with ImageMagick (`gel_photo.cmd_render`, ~380 lines, layout and drawing mixed). After that the labels are pixels. Every "rename lane 3", "remove that label" or "move the marker labels a bit left" is a request to the agent, and the agent re-runs the whole pipeline (detection, lanes, bands, drawing; ~10 s) to change one string.

Users should be able to make text changes themselves, without the agent, a terminal or special software. Changes to the analysis (crop, lanes, calibration) stay with the skill.

## 2. Decisions

1. **Layout is data.** `render` stops just before drawing and writes a **scene file**: the finished figure described as JSON (page, gel image, one item per label). Every output is drawn from the scene, never from layout code directly.
2. **The gel image carries no text.** It is written once as its own PNG (cropped, straightened, stitched, with the splice line if any). Nothing a user does to the text can change the gel pixels, the lane positions or the MW numbers.
3. **Three writers, one scene:** PNG (ImageMagick, as today), SVG (vector editors), HTML editor (browser). Each reads only the scene.
4. **The HTML editor is the default editing surface.** It is one self-contained file that works offline. It embeds the gel image, the scene and a small script. The analysis code is not embedded.
5. **Only the text layer is editable.** In the editor, the gel image is locked. The splice line is part of the gel image, so it cannot be removed (spec gel-prior §6b: disclosure is mandatory).
6. **Analysis changes still go through the skill.** Keeping different lanes, using another marker preset or fixing the crop means a re-run, and the re-run produces a fresh scene. Carrying a user's text edits over to a re-run is out of scope here (§10).

## 3. Files written by `render`

For `--out fig.png`:

| File | Always | Content |
|---|---|---|
| `fig.png` | ✓ | As today: gel and labels, `--dpi` pixels, dpi in pHYs. |
| `fig.gel.png` | ✓ | The gel face only, no text, at the output dpi. This is the image the scene references. |
| `fig.scene.json` | ✓ | The scene (§4). |
| `fig.html` | ✓ | The editor (§6), with `fig.gel.png` and the scene embedded. |
| `fig.svg` | with `--svg` | The SVG (§5), with the gel embedded. |
| `fig.mw.json`, `--record` | as today | Unchanged. |

`--no-editor` suppresses `fig.html` (for batch runs).

## 4. Scene file

One JSON object. All lengths are in **cm** and all font sizes in **pt**, so the scene does not depend on dpi. The origin is the **top-left of the gel face**, the same as the record (gel-prior §7). x runs right and y runs down. Labels to the left of or above the gel have negative coordinates.

```json
{
  "version": 1,
  "gel": {"image": "fig.gel.png", "width_cm": 8.60, "height_cm": 6.24},
  "page": {"margin_min_cm": 0.43, "pad_cm": 0.17, "background": "#ffffff"},
  "output": {"dpi": 300},
  "font": {"family": "Arial", "file": "/System/Library/Fonts/Supplemental/Arial.ttf"},
  "outline": {"color": "#ffffff", "width_frac": 0.07},
  "items": [
    {"id": "lane-3", "role": "lane", "bind": {"lane": 3},
     "text": "3", "x_cm": 1.53, "y_cm": -0.14, "anchor": "bottom-center",
     "size_pt": 8, "color": "#000000"},
    {"id": "marker-2-1", "role": "marker", "bind": {"marker_lane": 2, "label_index": 1, "band_y_cm": 0.98},
     "text": "250 kDa", "x_cm": 0.31, "y_cm": 0.98, "anchor": "right-middle",
     "size_pt": 8, "color": "#000000"}
  ],
  "source": {"record": "fig.record.json", "written": "2026-10-20", "skill": "ep-craft"}
}
```

- `items[].anchor` names the point of the text box that sits at `(x_cm, y_cm)`: `{top|middle|bottom}-{left|center|right}`. Lane labels use `bottom-center` (box bottom sits `gap` above the gel top). Marker labels use `right-middle` or `left-middle` (box vertical centre on the band row). The anchor is stored, not re-derived, so a writer never needs the layout rules.
- The text box is the font's **ink box**, the same measure `text_layout.text_extent` uses today. Writers that can't measure ink (SVG, browser) use the anchor with standard baseline offsets. §8 sets the allowed difference.
- `role` is `lane`, `marker` or `free` (added by the user). `bind` records what the label refers to. Writers ignore it. It exists so a later version can carry edits across re-runs (§10).
- `page` is **derived**, not stored as a size. Page = gel plus, on each side, `max(margin_min_cm, text overhang + pad_cm)`. That is the rule `render` uses today (`margin_frac`, `pad`), restated in cm. Every writer recomputes it, so a longer label grows the page instead of being cut off.
- `font.file` is used by the Python writers. `font.family` is used by SVG and the browser.
- `id` is unique and stable within one scene.
- `source.record` is present only when `render` was given `--record`.
- No photo coordinates, analysis-scale numbers, angles or boxes. Same rule as the record.

## 5. SVG writer

- `width`/`height` in cm (page size from §4), `viewBox` in cm, so editors open it at true physical size.
- The gel is a `<image>` with the PNG as a base64 data URI (the SVG stands alone), locked in a `<g id="gel">`.
- Each item is a `<text>` in `<g id="labels">`, with `id`, `data-role` and `data-bind` attributes. Anchor mapping: `text-anchor` start/middle/end, and the baseline offset computed from font metrics in the writer, not from `dominant-baseline` (which Illustrator and PowerPoint handle inconsistently).
- The outline is drawn as a second `<text>` underneath with `stroke` (same as the two-pass approach in `annotate_cmd`), not `paint-order`, for the same reason.
- An SVG edited in another tool is the user's final copy. The skill never reads it back.

## 6. HTML editor

One file, no network access, no external scripts or fonts. It is generated from `templates/editor.html` by filling in two placeholders: the base64 gel image and the scene JSON (`<script type="application/json" id="scene">`). The template holds all HTML, CSS and JS once.

**Editing**
- Click a label to select it. Type to edit its text in place, or use a side panel for text, size (pt), colour and anchor.
- Delete or Backspace removes the selected label. "Add label" places a `free` item at the click point.
- Drag to move, and arrow keys nudge by 0.01 cm (0.1 cm with Shift). Multi-select with Shift-click, so a whole marker column can be moved together.
- Bulk actions: set the size or colour of all lane labels, or all marker labels.
- Undo and redo for every change.
- The gel image cannot be selected, moved or hidden.
- The page grows and shrinks with the labels using the §4 rule, live.

**Output**
- **Download PNG:** draws the page on a canvas at the chosen dpi (defaults to `output.dpi`), white background, gel at true size, labels with outline. It writes a `pHYs` chunk with the dpi into the PNG before download. The canvas API doesn't do this itself, and without it Word and PowerPoint open the PNG at the wrong physical size.
- **Download SVG:** the same SVG as §5, from the current state.
- **Save:** downloads a new `fig.html` with the current scene embedded. A browser can't overwrite the file it opened, so this is the way to keep edits. The page title and the save filename say it is the edited version.
- **Download scene JSON:** for the `export` command (§7) or for the agent.

**Look:** the page is shown at screen scale with a zoom control, rulers in cm, and a light/dark UI theme. The figure itself is always white-background.

## 7. `export` command

```bash
python3 scripts/gel_photo.py export fig.scene.json --png out.png [--dpi 300] [--svg out.svg] [--html out.html]
python3 scripts/gel_photo.py export fig.html --png out.png
```

- Input is a scene file or an editor HTML file (the scene is read from its `#scene` block, and the gel from the embedded data URI).
- Uses the PNG, SVG and HTML writers only. No detection, no ImageMagick reading of the photo. It takes about a second.
- The agent uses this for text-only requests: edit the scene, then `export`. It never re-runs `render` for a text change.

## 8. Fidelity between writers

Same scene, same dpi:
- **Gel placement** is pixel-identical between the Python PNG and the editor PNG (same left/top offset and size). Both are computed from the §4 page rule in cm and rounded the same way.
- **Text position:** each label's ink-box centre is within 1 px at 96 dpi, or 0.03 cm at any dpi, between the Python PNG and the editor PNG, when the browser has the same font family. Glyph shapes may differ (different rasterisers). That is accepted.
- **Physical size:** all three outputs report the same page size in cm, within 0.01 cm.

## 9. Acceptance

- `render` with no flags writes `fig.png`, `fig.gel.png`, `fig.scene.json` and `fig.html`. `fig.png` is pixel-identical to today's output for the reference photo (full gel and `--keep-lanes 1,2,8-9`). The refactor changes no pixels.
- `export fig.scene.json --png` reproduces `fig.png` pixel for pixel.
- Scene round trip: `render` → scene → edit one label's text in the JSON → `export` gives a PNG that differs from `fig.png` only inside that label's box and wherever the page margin changed.
- `fig.gel.png` contains no text. It is pixel-identical to the gel region of `fig.png` with the labels masked out.
- The scene contains no key named like `rect`, `bbox`, `angle` or `scale` (same test as the record).
- The SVG opens in a browser at the scene's page size in cm, and every item is a `<text>` element with its `id`.
- The editor, checked in a browser: edit, delete, add, drag, undo/redo and Save → reopen keeps the edits. The PNG download carries the chosen dpi in pHYs (checked with `magick identify -format "%x"`). It also meets §8 against `export` for the same scene.
- `selftest` covers: the scene schema and the page rule, scene→PNG equal to the legacy draw on a synthetic gel, the SVG item count and size, extracting the scene and image from a generated HTML, and a pHYs writer test. If the pHYs code lives only in JS, the test runs the JS function under `node` when it's available and is skipped with a notice otherwise.
- Docs: `SKILL.md` tells the agent to send the user `fig.html` with the PNG, and to use `export` (never `render`) for text-only changes. The conventions reference documents the scene schema.

## 10. Out of scope

- Carrying user edits across a re-run (matching by `bind`). The `bind` field is written now so this can be added later.
- Reading back an SVG edited in Illustrator or Inkscape.
- Editing anything other than text in the editor: crop, lanes, bands, MW, gel brightness or contrast.
- A .pptx writer (it can be added later as another scene writer).
- Embedding font files in the HTML or SVG (licence of system fonts such as Arial). Writers name the family, and §8 tolerates glyph differences.
- Hosting or sharing the editor. It is a local file.

## 11. Open decisions

- **E-D1 — default outputs.** This spec writes the editor on every `render` (`--no-editor` to skip) and the SVG only with `--svg`. The alternative is to write all of them every time, or only the PNG unless asked.
- **E-D2 — where the editor's PNG dpi comes from.** It defaults to `output.dpi` from the render. The alternative is a fixed 300 dpi default for journals.
- **E-D3 — MW values as labels.** Today the MW report is printed, not drawn. Should the editor offer "add MW label for this band" (a `free` item pre-filled from `fig.mw.json`)? That would mean embedding the MW report too.
