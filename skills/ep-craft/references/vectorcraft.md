# VectorCraft 0.14.0: file format and control port (V0 measurements)

Measured on 2026-10-22 against `/Applications/VectorCraft.app` 0.14.0 (macOS, `--control 7797`). Everything here was run, not read from docs. The skill targets 0.14.x only. On another version, re-run the checks in §6 before trusting this page.

## 1. File format (`.vectorcraft`, format version 4)

Three encodings exist. The app **opens all three** and the reader must accept all three:

| Encoding | Written by | Layout |
|---|---|---|
| inline | `document.save` (default, and `compress:false`) | one JSON object; `images[key] = {mime, data: <base64>}` |
| blobs | older saves (e.g. `~/Downloads/DGC.vectorcraft`) | `<JSON>` `\n` `\0VCBLOBS\0` `<bytes>`; `images[key] = {mime, blob: [offset, length]}`, offset from the first byte after the marker |
| gzip | `compress:true` / the `useCompression` preference | gzip of either of the above (magic `1f 8b`) |

Our writer emits **inline** (one JSON file; no binary container to get wrong).

**The loader is strict.** A missing field fails the whole open with e.g. `not a VectorCraft file: missing field 'color' at line 137 column 5`. So the writer **never builds object JSON from scratch**. It deep-copies each object kind from `templates/kinds.vectorcraft` (app-saved, one object per kind) and changes only the fields below. The document-level fields (swatches, graphic styles, grid, …) are copied from the same template.

**Unknown keys are dropped on save.** A top-level `ep_extra` and a `document.metadata.ep_labeling` both vanished after one save. Never store our data in extra keys.

Object kinds we use (`document.layers[].kind.children[]`):

| Kind | Fields we set | Notes |
|---|---|---|
| layer | `id`, `name`, `kind.children`, `kind.printable`, `kind.color.Preset`; `visible:false` / `locked:true` on the node itself (absent = true/false) | `printable` lives in `kind`, `visible`/`locked` on the node |
| line | `id`, `name`, `kind.path.subpaths[0].anchors[i].p`, `kind.live.a/b`, `appearance.items[1]` (stroke colour, `width`) | keep `path` and `live` in agreement |
| point text | `id`, `name`, `kind.xf` (translation = anchor point; `para.justify:"Center"` centres on x), `kind.runs[0].text`, `runs[0].style.{font_family,size,fill}` | y is the **baseline** (`charAlign` romanBaseline). Fonts are not embedded; the default family when none is given is "Source Sans 3", so always set `Arial` |
| image | `id`, `name`, `kind.key`, `kind.width/height` (pixels), `kind.xf = [s,0,0,s,x,y]` (pt per pixel, top-left) | no `link` = embedded. One `images` entry can back several image objects (Workbench + every Figure segment). `mime` `image/png` or `image/jpeg` (the `Crop` preview) |
| rectangle | cloned from the template clip path: closed 4-anchor path with `live: {shape:"rectangle", w, h, xf, radii}`; we set anchors, `live.w/h/xf`, stroke | anchors TL TR BR BL; see the V10 notes below |
| clip group | `kind.type:"group"`, `kind.clip:true`, children `[path with "clipping":true, image]` | the clip path's own paint is none. Measured: a 100 pt rect over a 200 pt image shows only the rect |
| artboard | `id`, `name`, `rect{x0,y0,x1,y1}` | coordinates in pt, y down, artboards placed side by side |

`next_id` must exceed every id used. Units are `"Points"`, and 1 cm = 72/2.54 pt.

### Point-text rotation (V9 measurements)

The point-text `xf` is a plain affine in the text's own frame: `u` = advance direction, `v` = up from the baseline;
`x' = a·u + c·v + e`, `y' = b·u + d·v + f`. The translation part is the anchor, and rotation happens **about the anchor**:

| `xf` linear part | looks like |
|---|---|
| `[1, 0, 0, 1]` | upright |
| `[0, −1, 1, 0]` | 90° counter-clockwise: reads bottom-to-top; for `justify:Center` the glyph column sits **left** of the anchor |
| `[0, 1, −1, 0]` | 90° clockwise: reads top-to-bottom; column **right** of the anchor |

Measured: a file written with `[0,−1,1,0,40,240]` opened with `warnings: []`, `document.json` returned the matrix
unchanged, and the exported PNG put the ink exactly left of the anchor and upward from it. Angle convention used by the
skill: **degrees, counter-clockwise**, `θ = atan2(−b, a)`, normalized to (−180, 180] (`vc_file.rot_xf` /
`vc_file.angle_of_xf`). `object.rotate {angle}` uses the same direction.

For layout the skill treats a label as an ink box: `u ∈ [0, w]` (shifted by `justify`), `v ∈ [−0.716·size, 0]`
(cap height, no descender), rotated by the same matrix. The box is anchored (4 pt from the gel) and the page margins are
measured from it, so any angle — not just multiples of 90° — lands correctly.

### Rectangles and images (V10 measurements, 2026-10-23, scratch doc)

- **`bbox_angle` (measured 0.19.0, 2026-10-31).** A live rectangle's rotation for the inspector and the selection box is a **node-level field**, not part of `live` and not read from `xf`: `{"id":…, "name":…, "appearance":…, "kind":{…}, "bbox_angle":3.147}`. Value = `−atan2(b, a)` = `vc_file.angle_of_xf(xf)` (i.e. after `shape.rectangle {rotation:r}` the node has `bbox_angle:r` and `xf` angle `−r`; `object.rotate {angle:r}` lands on the same node). The app **omits it when the angle is 0** — a rectangle with only `xf` rotated is drawn right but the inspector says 0° and the selection box is drawn as the upright bounding box, which looks like a second, straight rectangle around the tilted one. Writing `bbox_angle` by hand works: the app reads it (`document.inspect` reports it as `rotation`), and it survives save + reopen (drift 0). Check: with the angle set, `object.rotate {absolute:true, angle:0}` turns the frame exactly axis-aligned; with the wrong sign it lands at twice the angle.
- **Point text carries the same `bbox_angle`** (ep-vz7, measured 2026-10-31): `text.create` + `object.rotate {angle:90}` saved a node with `bbox_angle:90.0` next to an `xf` at 90°, and `document.inspect` reported `rotation:90`. Text written with only a rotated `xf` reads as 0° in the inspector. Same rule: `bbox_angle = angle_of_xf(xf)`, omitted at 0°.
- `shape.rectangle {x, y, width, height, rotation?}` → a path with `live: {shape:"rectangle", w, h, xf, radii}` and four anchors TL TR BR BL. `object.rotate {angle:10}` (CCW) changed `live.xf` to `[cos, −sin, sin, cos, …]` and rotated the anchors; `object.scale {sx:120}` scaled `live.w/h`. **Anchor order is kept** through both, so anchors 0→1 stay the same edge at any angle (the skill's `crop` frame: the wells edge).
- `file.place {name, dataBase64}` embeds PNG and JPEG (`images[key].mime = image/jpeg`) and returns `{ids: [id]}`. The image is centred in the view: move it afterwards (`object.transform`).
- `links.relink {ids, path, preserve:"transforms"}` then `links.embed {ids}` replace an embedded image's pixels: `xf` and `id` stay, the `key` changes (content-addressed). Works on an image **inside a clip group in a locked layer**. Inside `edit.undoGroup` the whole swap is one undo step (`edit.undo` restored the old key). Re-read the key after the embed before creating more objects from it.
- `file.close` takes only `{index}`.

## 2. Control port

Protocol: TCP, one JSON line per request `{"id":N,"method":M,"params":P}` → `{"ok":true,"result":…}` / `{"ok":false,"error":"…"}`. Top-level methods we use: `ui.inspect`, `engine.execute {command, params}`, `engine.commands` (1090 commands with parameter docs; grep it before guessing).

A running instance **without** `--control` can't be reached. Launch with `VectorCraft --control 7797 <file>`.

### Reading

| Need | Command | Measured result |
|---|---|---|
| full model | `document.json {}` | same shape as the file's `document` (exact line endpoints, text runs with font/size/fill, `xf`, layers with flags) |
| tree with bounds | `document.inspect {depth}` | layers **top-first** (reverse of the file order); per object `id,name,kind,bounds,locked,visible`; per layer also `printable`; plus `history`, `dirty` |
| by name | `document.find {name}` | substring match |

`sync` reads `document.json`. Order inside a layer is not meaningful to us: lanes are paired by x.

### Writing (all verified on a live document)

| Need | Command | Notes |
|---|---|---|
| move by id | `object.move {ids, dx, dy}` | text: adds to `xf` translation exactly |
| set a line exactly | `path.setAnchors {id, subpaths:[{anchors:[{x,y},{x,y}], closed:false}]}` | works on objects inside a **locked** layer (the clip rects in `gel`) |
| text content | `text.setText {id, text}` | keeps the first run's style; works in a locked, hidden layer |
| name / lock / hide | `object.setProps {id, name?, locked?, visible?}`; `layer.setProps {id, name?, locked?, visible?, printable?}` | `text.create` / `shape.line` take no name: create, then `object.setProps` |
| create | `layer.setCurrent {id}` → `select.set {ids:[]}` → `shape.line {x1,y1,x2,y2}` / `text.create {x,y,text,size,font:"Arial",justify:"center"}` | returns `{id}` |
| move between layers | `layer.move {ids, target, place:"inside"}` | **fails into a locked layer** ("the layer or group is locked") |
| delete | `edit.clear {ids}` | |
| artboard | `artboard.setProps {index, width?, height?, x?, y?, name?}`, `artboard.new {x,y,width,height,name}` | |
| rectangle | `shape.rectangle {x, y, width, height, rotation?}` | live rectangle, anchors TL TR BR BL (§1 V10). **`rotation` is clockwise** (measured: `rotation:10` moved TR down — the opposite of `object.rotate`), so pass `−angle` for a CCW angle. `path.setAnchors` on a live rectangle drops `live` (it becomes a plain path); use it only as a fallback |
| place image | `file.place {name, dataBase64}` | PNG or JPEG, embedded, centred in the view; returns `{ids}` |
| swap pixels | `links.relink {ids, path, preserve:"transforms"}` → `links.embed {ids}` | keeps `xf`/`id`, new `key`; fine in a locked clip group; one undo step inside a group |
| close | `file.close {index}` | no other params |
| clip | `object.clippingMask.make {ids:[clipPath, image]}` | returns the group id |
| file info | `file.info {description?, keywords?}` | **copied into exported PNGs** (tEXt), see §4. |
| rotate | `object.rotate {ids, angle, origin}` | `angle` = degrees, counter-clockwise, **relative**; `origin` = the anchor keeps the anchor fixed. Measured: `angle:-90` turned a 90° label back to `[1,0,0,1]`; `absolute:true` with `angle:0` was a **no-op** on that same label — never use `absolute` to set 0. |

**Atomic sync:** `command.batch {label, commands:[{command, params}…]}` is all-or-nothing (a failing step left no change) and lands as **one** undo step. `edit.undoGroup.begin {label}` … `edit.undoGroup.end {cancel?}` does the same for steps that need results from earlier steps (e.g. a create, then its id); `cancel:true` rolls back. `sync` uses one group labelled `ep-craft sync`, so ⌘Z undoes the whole sync.

### Pitfalls (measured)

1. **New objects ignore a hidden current layer.** After `layer.setCurrent` on a hidden layer, `text.create` put the object in the previously current visible layer. Create in a visible layer, then `layer.move`, or (better) let the file writer put it there at `render` time.
2. **Hidden ≠ not exported?** Non-printing layers **are** drawn by `document.export`: the Workbench export showed the lane lines. Keep only printable art on the Figure artboard. Don't rely on `printable:false` to keep things out of `fig.png`.
3. **Save can be read half-written.** `document.save` returned, and one read straight after it hit a truncated file (`Expecting ',' … char 30695`), though another returned complete in 10 ms. Poll until the file is newer than the request **and** parses.
4. **Default font** is "Source Sans 3". Always pass `font:"Arial"` (and set `font_family` in the writer).

### More pitfalls (measured in V7 with the live executor, `scripts/sync_exec.py`)

5. **Locked layers block creation only.** Objects in a locked layer still accept `edit.clear`, `object.move` (including an image inside a clip group in the locked `gel`), `object.setProps`, `paint.setStroke`, `path.setAnchors` and `text.setText`. Creation is what fails: with a locked current layer, `text.create` silently put the object in the previously current layer, the same as with a hidden one. `sync_exec` therefore unlocks/shows only the target layers of `create` ops, relocks them at the end (also on error), and then checks each new object's layer once (with a `layer.move` fallback).
6. **Painting by id also changes the defaults for new art.** `paint.setStroke` / `paint.setFill` / `stroke.set` with `ids` also change the default paint, and an undo-group cancel doesn't restore it. Calling the same commands with nothing selected restores the defaults without adding history. `document.inspect` reports `paint` and `currentLayer`, which is enough to restore both.
7. **`layer.setCurrent` is not undoable.** `layer.setProps {locked}` inside an undo group *is* part of the group.
8. **`object.transform {ids, matrix, copy:true}` premultiplies** (new xf = matrix ∘ old xf). It works on a locked layer's image and returns the copy's id; the copy shares the same `images` key. This is how a Figure segment gets its own instance of the gel image.
9. **`object.clippingMask.make {ids:[rect, img]}` returns the group id.** The clip path's paint becomes none, matching the template's clip structure.
10. **Colours and point text.** `color` accepts `[r,g,b]` (0–1) or `"#rrggbb"`. `text.create {justify:"center"}` puts the `xf` translation exactly at (x, y).
11. **Bad ids fail cleanly.** A non-existent id fails with `no such object id N`, which aborts the undo group cleanly.
12. **Speed.** A sync of about 260 ops (mostly rebuilding the Profiles and analysis layers) takes 6–9 s, at one connection per call.
13. **`text.create` has no rotation.** Its params are `x, y, text, vertical, size, font, style, color, keepModel, area, placeholder, justify, leadingModel, charAlign` — no angle, no `xf`. A rotated label is created upright, then turned with `object.rotate {ids, angle, origin:[x, y]}` (relative, CCW). `absolute:true` with `angle:0` did **nothing** to an already rotated label; to set it back to horizontal rotate −90.

## 3. Export

```json
{"command":"document.export","params":{"path":".../fig.png","format":"png","artboard":0,"ppi":300,"background":"white"}}
→ {"bytes":7416,"format":"png","path":".../fig.png","warnings":[]}
```

- `artboard` exports **that artboard only** (no whole-page crop needed).
- `ppi` sets the pixel size **and** writes pHYs: a 260 × 200 pt artboard at `ppi:300` gave 1083 × 833 px, density 118.11 px/cm (= 300 dpi). No post-processing needed.
- The `file.info` description and keywords appear in the PNG's tEXt chunks.

## 4. Decisions settled by these measurements

- **V-D2, run metadata:** a text object `ep-meta` in a hidden, locked, non-printing layer `ep-meta`. It is written by the file writer (no hidden-layer create problem) and updated with `text.setText` (works while locked and hidden). Not `file.info description`, because that leaks into `fig.png`. Not custom JSON keys, because those are dropped on save.
- **V-D1, trim marking:** a short line in the `trimmed` layer whose midpoint lies inside a lane drops that lane from the Figure (lane lines are shared separators, so they can't be moved out pair by pair). Agent recipe: `layer.setCurrent` on `trimmed`, then `shape.line` across the lane above the gel. Read back from `document.json` like any layer. `trimmed` must stay **unlocked**. Sync re-strokes everything in `trimmed` grey.
- **Writer:** inline encoding, objects cloned from `templates/kinds.vectorcraft`, all text `Arial`.
- **PNG:** `document.export` with `artboard:0` (Figure is artboard 1) and `ppi:<dpi>`.

## 5. Templates

`templates/kinds.vectorcraft`: saved by VectorCraft 0.14.0 through the control port, uncompressed. One layer `layer` holding `clip` (clip group: rectangle + embedded 2 × 2 px image `image`), `line` (shape line) and `text` (Arial 8 pt point text, centred). The writer copies these. Regenerate it with the same app version if the loader starts rejecting files.

## 6. Re-check on a new VectorCraft version

1. A hand-written file built from the template opens with `warnings: []` and no `ui.dialog`.
2. `document.json` still returns line anchors and text runs as in §1.
3. `command.batch` is still atomic.
4. `document.export {artboard, ppi}` still writes one artboard with pHYs.
5. A live rectangle still carries `bbox_angle` and the app still reports it as `rotation` (§1): on a rotated `crop` frame, `document.inspect` shows the angle and the selection box hugs the tilted shape. If it reads 0 again, the writer in `vc_file.rect()` must be updated to the new field.
