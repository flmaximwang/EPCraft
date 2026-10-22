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
| image | `id`, `name`, `kind.key`, `kind.width/height` (pixels), `kind.xf = [s,0,0,s,x,y]` (pt per pixel, top-left) | no `link` = embedded. One `images` entry can back several image objects (Workbench + every Figure segment) |
| clip group | `kind.type:"group"`, `kind.clip:true`, children `[path with "clipping":true, image]` | the clip path's own paint is none. Measured: a 100 pt rect over a 200 pt image shows only the rect |
| artboard | `id`, `name`, `rect{x0,y0,x1,y1}` | coordinates in pt, y down, artboards placed side by side |

`next_id` must exceed every id used. Units are `"Points"`, and 1 cm = 72/2.54 pt.

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
| clip | `object.clippingMask.make {ids:[clipPath, image]}` | returns the group id |
| file info | `file.info {description?, keywords?}` | **copied into exported PNGs** (tEXt), see §4 |

**Atomic sync:** `command.batch {label, commands:[{command, params}…]}` is all-or-nothing (a failing step left no change) and lands as **one** undo step. `edit.undoGroup.begin {label}` … `edit.undoGroup.end {cancel?}` does the same for steps that need results from earlier steps (e.g. a create, then its id); `cancel:true` rolls back. `sync` uses one group labelled `ep-labeling sync`, so ⌘Z undoes the whole sync.

### Pitfalls (measured)

1. **New objects ignore a hidden current layer.** After `layer.setCurrent` on a hidden layer, `text.create` put the object in the previously current visible layer. Create in a visible layer, then `layer.move`, or (better) let the file writer put it there at `render` time.
2. **Hidden ≠ not exported?** Non-printing layers **are** drawn by `document.export`: the Workbench export showed the lane lines. Keep only printable art on the Figure artboard. Don't rely on `printable:false` to keep things out of `fig.png`.
3. **Save can be read half-written.** `document.save` returned, and one read straight after it hit a truncated file (`Expecting ',' … char 30695`), though another returned complete in 10 ms. Poll until the file is newer than the request **and** parses.
4. **Default font** is "Source Sans 3". Always pass `font:"Arial"` (and set `font_family` in the writer).

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
- **V-D1, trim marking:** moving the pair into the `trimmed` layer works through `layer.move` and the Layers panel, and is read back from `document.json` like any layer. `trimmed` must stay **unlocked**, otherwise lines can't be moved in. Sync re-strokes trimmed lines grey so they read as "off".
- **Writer:** inline encoding, objects cloned from `templates/kinds.vectorcraft`, all text `Arial`.
- **PNG:** `document.export` with `artboard:0` (Figure is artboard 1) and `ppi:<dpi>`.

## 5. Templates

`templates/kinds.vectorcraft`: saved by VectorCraft 0.14.0 through the control port, uncompressed. One layer `layer` holding `clip` (clip group: rectangle + embedded 2 × 2 px image `image`), `line` (shape line) and `text` (Arial 8 pt point text, centred). The writer copies these. Regenerate it with the same app version if the loader starts rejecting files.

## 6. Re-check on a new VectorCraft version

1. A hand-written file built from the template opens with `warnings: []` and no `ui.dialog`.
2. `document.json` still returns line anchors and text runs as in §1.
3. `command.batch` is still atomic.
4. `document.export {artboard, ppi}` still writes one artboard with pHYs.
