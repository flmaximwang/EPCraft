# Spec: ep-labeling — VectorCraft as the editing surface

Status: draft, 2026-10-22. Supersedes `specs/ep-labeling-editable-output.md` (the HTML editor, SVG writer and `export` go away). Builds on `specs/ep-labeling-gel-prior.md` (gel preset, frozen stages, record coordinates).

## 0. Why

The 2026-10-22 failure report on five SDS-PAGE photos (`~/Downloads/DGC/ANALYSIS-REPORT.md`, IMG_1496–1500, 15-well mini-gel, WJ103 marker) blamed `pitch_from_row` / `split_wide` / `fill_gaps` / `snap_to_anchors`. **Those were removed in T3** (`d8761f2`). The report ran against an outdated copy of the skill. With the current code the lane count is always the preset's 15, and a batch on the same photos rendered all five figures.

What is still wrong:

- **The lane grid can be one lane off.** On IMG_1496 the marker is in well 1 (confirmed by the user), but the grid puts the ladder in lane 2, and the batch labelled it that way. The column-profile phase fit has too little ink to lock onto when most lanes are faint or empty.
- **No way to correct it.** When detection is wrong, the only fix is flags and a re-run. The HTML editor only edits text, so it can't move a lane, fix a band or change the MW report.
- **Faint sample lanes are invisible in the outputs.** On these gels the sample bands are 3–4 grey levels deep and the ladder about 24. The MW report lists called bands only, with no curve to check them against.
- **Manual workarounds in the batch.** For IMG_1497 the old detection found only 10 ladder bands, so the batch rewrote the marker labels by hand, dropping 35 kD. (V4/V8 later showed all 11 bands are there; lane 1 sits on a damaged gel edge.)

Automatic detection won't be perfect on phone photos. So the skill should **make a good first guess, then let the user correct it by hand, and follow the correction**: the user moves lane lines and band lines in VectorCraft, and the agent re-reads them and updates the labels, curves and MW report.

## 1. Goal

- `render` writes **`fig.vectorcraft`** (the editable document), **`fig.png`** (the figure, exported by VectorCraft from that document) and **`fig.mw.json`** (MW report with the raw per-lane grey-scale profiles).
- The detected lane boundaries, the shared integration window and the marker band rows are **editable objects** in the document.
- A new **`sync`** command reads the user's edits from the live VectorCraft document. It recomputes profiles, bands and MW, updates the labels, curves and figure in place, saves, and re-exports `fig.png` and `fig.mw.json`.

## 2. Decisions

1. **Three outputs only:** `fig.vectorcraft`, `fig.png`, `fig.mw.json`. Debug and lane-configuration information lives in the document as layers, never as separate pictures. These are removed: `fig.gel.png`, `fig.scene.json`, `fig.html`, `fig.svg`, `--debug PNG` and `--record` JSON.
2. **Python writes the `.vectorcraft` file directly** (§5). The app is not needed to create it.
3. **The PNG always comes from VectorCraft's own export**, through the control port (§6). Then `fig.png` always matches what the user sees, both after `render` and after every `sync`. VectorCraft is a **hard requirement**: `doctor` checks for it, and `render` fails without it. It never writes a `.vectorcraft` without a PNG.
4. **Sync is live.** The user keeps the document open in a VectorCraft instance started with `--control`. `sync` reads the live document, applies updates in place, saves and exports. The user never closes or reloads anything.
5. **Lane model = lines, not rectangles.** All lanes share **one** integration window: one `top` and one `bottom` horizontal line. Each lane is a **pair of vertical lines** (left and right). Lanes are paired by x order, left to right.
6. **Marker band lines carry their MW in their name** (e.g. `band 35`). Deleting a band line drops that calibration point, and renaming one changes its value. This replaces hand-rewritten marker label lists (the IMG_1497 case).
7. **The marker lane is found from the geometry:** it is the lane whose [L, R] contains the marker band lines. It is not a stored index, so it survives added or deleted lanes.
8. **Ownership on sync** (§4.4): the agent owns geometry (label x follows the lane centre, marker label y follows its band line, curves, figure crop). The user owns label **text, font, size and colour**, which are never overwritten.
9. **Lanes are kept by default.** Trimming happens only for lanes the user marks (§4.5). No automatic blank-lane trimming.
10. **Three artboards:** `Workbench` (edit here), `Figure` (derived, exported as `fig.png`) and `Profiles` (curves). The Figure is rebuilt from the Workbench on every sync, so splicing never complicates what the user edits.

## 3. Outputs

| File | Written by | Content |
|---|---|---|
| `fig.vectorcraft` | `render` (new); `sync` (saved in place via the app) | The document in §4. The gel image is embedded at analysis resolution (not linked, not a proxy). |
| `fig.png` | VectorCraft `document.export {artboard: 0, ppi: dpi}` | Figure artboard only, at `--dpi`; the app writes pHYs itself (measured in V0). |
| `fig.mw.json` | `render`, `sync` | §7: calibration, per-lane called bands with MW, and the **raw profile** per lane. `--no-mw` drops it. |

The terminal MW table stays.

## 4. Document structure

Units: VectorCraft points. Origin top-left, y down (verified in 0.14). Non-printing layers are still drawn by the export (V0), so the Figure artboard holds only printable art. 1 cm = 72/2.54 pt. Physical size is kept: the gel is `width_cm` wide on every artboard (the preset still sets the scale).

### 4.1 Artboards

| # | Name | Content | Exported |
|---|---|---|---|
| 1 | `Figure` | Gel (cropped/spliced) + lane labels + marker labels, page margins per today's page rule | → `fig.png` |
| 2 | `Workbench` | Whole straightened gel face, all lane lines, the window, marker band lines, detected-band ticks, lane number tags | no |
| 3 | `Profiles` | One curve per lane, stacked, with a shared y axis in cm and MW ticks | no (the user can export it by hand) |

Artboard 1 is `Figure` so that a plain "export" in the app also gives the figure.

### 4.2 Layers and objects

Object names are the binding between the document and the analysis. The agent only touches objects in the layers below, and finds them by name.

| Layer | Artboard | Lock / print | Objects (name) | Edited by the user |
|---|---|---|---|---|
| `gel` | all | locked, printable | Workbench: one image `gel`. Figure: one clipped instance per kept segment `gel seg k`, splice lines `splice k` | no |
| `lanes` | Workbench | unlocked, non-printing | vertical lines named `lane` (pairs by x order); full window height | **move, add, delete** |
| `window` | Workbench | unlocked, non-printing | two horizontal lines `top`, `bottom`, full gel width | **move** |
| `marker bands` | Workbench | unlocked, non-printing | horizontal lines `band <MW>` over the marker lane | **move, delete, rename** |
| `trimmed` | Workbench | unlocked, non-printing | lane line pairs the user moved here (§4.5) | **move pairs in/out** |
| `analysis` | Workbench | locked, non-printing, agent-owned | lane number tags `tag n`, ticks for detected sample bands `peak n.k` | no (rebuilt every sync) |
| `labels` | Figure | unlocked, printable | lane labels `label lane n`, marker labels `label band <MW>`, user-added free text (any other name, never touched) | **text and style**; position is agent-owned |
| `profiles` | Profiles | locked, printable | `curve n`, axis, band markers, lane captions | no (rebuilt every sync) |
| `ep-meta` | Workbench, off-page | hidden, locked, non-printing | one text object `ep-meta` holding the run metadata JSON (§4.6) | no |

Line style: lanes cyan, window orange, marker bands magenta, 0.5 pt, so they are easy to grab and tell apart (it mirrors today's debug overlay).

### 4.3 Reading the Workbench (validation)

`sync` refuses to change anything and prints what to fix when:
- the number of `lane` lines (outside `trimmed`) is odd, or two pairs overlap;
- `top` or `bottom` is missing, or `top` is below `bottom`;
- no lane contains all the `band <MW>` lines, or they span more than one lane;
- a band name doesn't parse as a number, fewer than 3 bands remain, or MW is not monotonic with y.

Lines that are not quite vertical or horizontal (because of a user's drag) are read by their midpoint x or y. They are not rejected.

### 4.4 Ownership on sync

| Object | Agent writes | Agent never changes |
|---|---|---|
| `label lane n` | x = lane centre, y = gap above the Figure gel top; created if the lane is new (text `?`, and the agent asks for the name); deleted if the lane is gone | text, font, size, colour, anchor |
| `label band <MW>` | y = its band line (mapped into the Figure); created/deleted with the band line | text, font, size, colour |
| free text in `labels` | — | everything |
| `gel seg k`, `splice k`, Figure artboard size | everything (crop from lane lines + kept lanes) | — |
| `analysis`, `profiles` layers | everything | — |

Lane numbering `n` is left-to-right among kept + trimmed lanes. When lanes are added or deleted, existing labels are matched by **nearest previous centre** (stored in `ep-meta`), so a label's custom text follows its lane.

### 4.5 Trimming

Every lane is kept by default. To drop a lane from the Figure, the user moves its two lines into the `trimmed` layer (one action in the Layers panel), or passes `--trim-lanes 1,2` to `render`, which puts them there. Trimmed lanes still get a profile and appear in `fig.mw.json` (`"trimmed": true`). Non-contiguous kept lanes are spliced exactly as today: segment boundaries sit at the midpoint between neighbouring lanes, and a splice line is drawn. `--keep-lanes` is replaced by `--trim-lanes`.

### 4.6 Run metadata

What `sync` needs and can't read from geometry: the skill version, the photo path, the gel preset, the marker preset, px/cm of the embedded gel, dpi, the lane names given at render time, and the previous lane centres (for label matching). Stored in the `ep-meta` text object (§4.2), written by the file writer and updated with `text.setText`. V0 ruled out the alternatives: custom keys are dropped on save, and the File Info description is copied into the exported PNG.

## 5. `.vectorcraft` file (format version 4, VectorCraft 0.14.0)

Measured in V0; full details in `references/vectorcraft.md`. The writer emits the **inline** encoding (one JSON object, images as base64 `data`). The reader also accepts the `\0VCBLOBS\0` blob container below and gzip. The app's loader is strict about missing fields, so every object is **deep-copied from `templates/kinds.vectorcraft`** (app-saved), never built from scratch. Unknown keys are dropped on save.

```
<UTF-8 JSON object> "\n" "\0VCBLOBS\0" <blob bytes concatenated>
```

- Top level: `format: "vectorcraft"`, `version: 4`, `generator`, `document`, `images`.
- `images[key] = {mime, blob: [offset, length]}`. Offsets are relative to the first byte after `\0VCBLOBS\0`. Image objects reference `key`, carry pixel `width`/`height`, and place the image with the affine `xf = [a, b, c, d, tx, ty]`. Our gel images are **embedded** (no `link`, no `proxy`).
- `document`: `units: "Points"`, `artboards[] {id, name, rect{x0,y0,x1,y1}}`, `layers[]` (each `kind.type: "layer"` with `children`), `next_id` > every used id, plus the bookkeeping fields copied from the app-saved template `templates/kinds.vectorcraft` (swatches, graphic styles, grid, …).
- Lines: `kind.type: "path"`, a 2-anchor open subpath, `live: {shape: "line", a, b}`, with a stroke in `appearance`. Text: `kind.type: "text"`, point text, `xf` translation, `runs[{text, style}]`, `para.justify`.
- The writer emits only what it needs. The reader reads only what it needs: names, layer membership, line endpoints, text content/position/style, and the gel blob. Any field it doesn't know is preserved when the agent edits through the app. The agent never rewrites the file directly after `render`.

## 6. Control port

- Attach to the instance on `--port` (default 7797). If none is listening, launch `VectorCraft --control <port> fig.vectorcraft` and remember that we started it. A running instance without `--control` can't be reached: tell the user to save, quit and let the skill relaunch it.
- Call `ui.inspect` after opening and cancel any dialog (missing links, …).
- `sync` reads with `document.json` (exact geometry, names, text style). It writes with `object.move`, `path.setAnchors`, `text.setText`, `object.setProps`, `edit.clear`, `artboard.setProps`, `layer.move` and creates (then names) new objects. Everything runs inside one `edit.undoGroup` labelled `ep-labeling sync`, so one ⌘Z undoes a sync, and a failure is rolled back with `cancel`. The command list and the pitfalls are in `references/vectorcraft.md`.
- Save with `document.save`, then poll until the file is newer than the request **and** parses (V0 caught one half-written read); time out after 60 s. Export with `document.export {artboard: 0, ppi: dpi}`: one artboard, with pHYs written by the app.
- Fonts: text items are created at their nominal size, never scaled (a scaled text box renders at the scaled size while the panel still shows the nominal one). The default font is Arial.

## 7. Analysis from the Workbench

The same code runs in `render` (on detected geometry) and in `sync` (on the user's geometry):

1. **Profile per lane:** for every row y between `top` and `bottom`, the mean grey over x ∈ [L, R] of the embedded gel image (the image used for the figure, not the photo). Also an `ink = baseline − gray` curve, where `baseline` is the stage-3 top-hat (a sliding mean, half-window 0.30 cm, expressed in cm so it doesn't depend on resolution). Store `gray`, `baseline` and `ink` raw. V4 tried a morphological closing instead and rejected it: it turned smooth texture into a sawtooth and called bands on flat lanes.
2. **Bands per lane:** local darkenings of the lightly smoothed **raw grey** profile. **Local prominence** is how much darker the band is than the lighter of its two flanks within 0.15 cm. Not measured on `ink`: the sliding-mean baseline sags at strong bands, which leaves negative lobes beside them and turns the flat background between two strong bands into a ghost peak (a synthetic 4-band ladder gave 7 calls on `ink` and exactly 4 on raw grey). Not height above zero either: a faint band beside a strong one is negative on `ink`. A band is called when that prominence is ≥ 4 × the lane's **background** noise (MAD of the smoothed ink, iteratively excluding band rows and lobes). There is no absolute ink floor like `min_loose_ink=6`, so faint lanes still get their peaks. Each band reports prominence, SNR and width; the curve is the user's check. On IMG_1496 this calls 11/11 marker bands, finds the faint 3–4-grey-level sample bands, and calls nothing on empty lanes.
3. **Calibration:** two-point interpolation only — inside the span between two neighbouring `band <MW>` lines, `log10(MW)` is linearly interpolated **between those two points**. A global fit (`log10(MW) = a + b·y` least squares) is forbidden: on Tris-Tricine gels it is systematically off (it pushes 250 kD down ~40 % and 10 kD up ~30 %) and it does not even pass through the marker values. The calibration stores the points only — no fit coefficients, no R².
4. **MW per band**; a band whose y falls outside the marker span gets no number (`mw_kda: null`, `extrapolated: true`) — never extrapolate.

`fig.mw.json` (v2):

```json
{"version": 2, "units": {"x": "cm from gel-face left", "y": "cm from gel-face top", "gray": "0-255 (lower = darker)", "ink": "baseline - gray"},
 "window": {"top_cm": 0.0, "bottom_cm": 5.9},
 "calibration": {"method": "interp", "marker_lane": 1, "points": [[0.42, 250], ...]},
 "lanes": [{"lane": 1, "label": "M", "trimmed": false, "left_cm": 0.31, "right_cm": 0.74,
            "profile": {"y_cm": [...], "gray": [...], "ink": [...]},
            "bands": [{"y_cm": 2.1, "mw_kda": 23.5, "prominence": 4.1, "extrapolated": false}]}]}
```

Coordinates are in cm on the gel face, as in the record (gel-prior §7). There are no photo pixels.

**Profiles artboard:** one `ink` curve per lane, drawn horizontally (the y in the gel becomes x on the plot, with MW ticks from the calibration on top) or vertically beside its lane. V5 chooses the layout and shows the user both options before fixing it. Called bands are marked; the marker lane is drawn with its calibration points.

## 8. Initial detection (render)

Only stage 1 (the gel face) still comes from the old pipeline. The Workbench is then seeded from the gel-face image alone (`workbench.from_image`, V8):
- **Lanes: an evenly spaced comb anchored on the wells** (`well_grid.py`). Each well shows as a dark U in the top 0.12 cm of the gel face, whether or not it was loaded. The comb `centre_k = c0 + k·pitch` is fitted over pitch (preset ±20%) and offset, maximising the well depth inside the 0.75·pitch windows minus outside, including the well-free side margins. Lane width is 0.7·pitch.
  - Measured on the five DGC photos: pitch 0.510–0.522 cm, well depth 41–87 grey levels (wells absent: 0.9–9.9).
  - Against the user's hand-placed IMG_1499 lanes: 0.015 cm mean, 0.042 cm max.
  - With no visible wells (depth < 20), it falls back to an even comb at the preset pitch, centred, and says so.
  - The old stage-2 column-ink fit is no longer used for seeding.
- **Marker lines: the marker lane's called profile peaks** (§7), not the old stage-3 rows, which sat 0.13–0.5 mm low on all five photos. If the count doesn't match the labels, it refuses and lists the y values it called.
- The preset's `lane_pitch_cm` is now 0.52; the old 0.565 assumed the 15 lanes fill the whole gel width.
- Regression fixtures: the five DGC photos, with the user's hand-placed IMG_1499 lanes as ground truth.

## 9. Commands

```bash
python3 scripts/gel_photo.py render IMG.jpeg --gel-size data/gel_size.json --markers data/markers.json --preset N \
    --lanes lanes.json [--trim-lanes 1,2] [--dpi 300] [--port 7797] --out fig.vectorcraft
python3 scripts/gel_photo.py sync fig.vectorcraft [--port 7797] [--dpi 300]
python3 scripts/gel_photo.py probe IMG.jpeg ...    # prints the detection; no files
python3 scripts/gel_photo.py doctor                # also checks VectorCraft.app and its version
```

- `render` writes the document, opens it in VectorCraft, exports `fig.png` and writes `fig.mw.json`. It prints which instance and port the user should keep open.
- `sync` changes nothing if the Workbench is unchanged and the labels are already in place (idempotent). It prints a diff summary, e.g. `lane 3 moved +0.12 cm; band 35 deleted; 2 labels re-centred; MW refit r² 0.97 → 0.99`.
- Text-only requests from the user ("rename lane 3 to WT") are done by the agent through the control port on `label lane 3`, then `sync` for the export. A re-render is never needed.
- `export`, `--svg`, `--no-editor`, `--debug`, `--record` and `--keep-lanes` are removed. Each one fails with a message naming its replacement.

## 10. Removed code

`editor.py`, `templates/editor.html`, `scene_svg.py`, `scene_png.py` (labels are now drawn by VectorCraft), `debug_overlay.py` (now the `analysis` layer), `record.py` (now the Workbench geometry + `ep-meta`). `scene.py` stays only if the Figure layout code (page rule, label anchors) still fits there; otherwise it's folded into the Figure builder. ImageMagick stays for reading, rotating and cropping the photo.

## 11. Acceptance

- On the reference photo and the five DGC photos, `render` writes exactly three files. The document opens in VectorCraft 0.14 without dialogs and shows three artboards and the layers in §4.2.
- `fig.png` has the Figure artboard's physical size within 0.01 cm, and pHYs equals `--dpi`.
- **Round trip, scripted (selftest, no app):** build a Workbench model → move lane 3's lines by +0.2 cm, delete `band 35`, add one lane pair, move one pair to `trimmed` → `plan_sync()` gives the expected ops (label moves/creates/deletes, figure segments, MW refit), and leaves user text and style alone.
- **Round trip, live (manual checklist, recorded in the ticket):** in VectorCraft, drag a lane line, rename a lane label and change its colour, delete a band line → `sync` → the label stays renamed and coloured but re-centres; the MW report and curves change; `fig.png` matches the window.
- `sync` run twice in a row changes nothing the second time.
- IMG_1496 seeds the marker in lane 1 (V8).
- `fig.mw.json` profiles have one sample per gel-image row within the window; `gray` matches an independent mean over the same pixels.

## 12. Out of scope

- Editing the gel crop, angle or brightness in VectorCraft (that needs a re-render; the `gel` layer is locked).
- Carrying edits across a re-`render` (a re-render starts a fresh document; the agent warns before overwriting an edited one).
- Perspective correction, band quantification (the profiles are raw data, not densitometry).
- Supporting VectorCraft versions other than 0.14.x; `doctor` warns on any other version.

## 13. Open decisions

- ~~V-D1 — trim marking~~ **Settled (V0):** move the pair into the unlocked `trimmed` layer; sync re-strokes trimmed lines grey.
- ~~V-D2 — run metadata location~~ **Settled (V0):** the `ep-meta` text object (§4.6).
- **V-D3 — Profiles layout:** horizontal stack with an MW axis, or vertical strips aligned beside each lane (V5 shows both).
- **V-D4 — overwrite guard:** does `render` onto an existing `fig.vectorcraft` refuse, back up (`fig.vectorcraft.bak`), or ask?
