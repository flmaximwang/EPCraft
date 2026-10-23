# Spec: ep-labeling — Crop artboard (adjust the crop frame in VectorCraft)

Status: agreed · Scope: `skills/ep-labeling` · Written 2026-10-23 · Builds on `specs/ep-labeling-vectorcraft.md`

## 1. Problem

Stage 1 (gel face: rectangle + rotation, `gel_area.compute` → `gel_photo.gel_face`) is fixed at `render` time. When detection is off (frame a few px too low, angle 0.5° off, wrong edge picked), the only fix is a re-render with `--rect` / `--angle`, which starts a fresh document and **loses the user's Workbench and label edits**. The user also has no way to *see* the crop against the photo.

## 2. Decisions (grilled 2026-10-23)

1. **New artboard `Crop`** in `fig.vectorcraft`: the original photo with the crop frame drawn on it. The user drags/rotates/scales the frame, then says "I edited it"; `sync` re-crops in place.
2. **Frame = one rectangle that keeps the preset aspect.** Move, rotate and *uniform* scale are allowed. Width sets the scale (px/cm); the aspect is fixed by the preset (`width_cm` : `height_cm − trims`). A frame stretched out of aspect is an error (nothing changes), like every other Workbench error.
3. **Workbench lines stay put in gel-face cm.** A re-crop changes the pixels under the lines, not the lines. Gel-face size in cm is unchanged (preset), so no line can fall off the face.
4. **The photo is embedded as a preview** (JPEG, long side ≤ 2000 px). The re-crop itself always reads the **full-resolution photo** at `ep-meta.photo`.
5. **Old documents get the artboard on their first sync** when the photo is still there and the current crop can be recovered (§7); otherwise one note, and sync carries on as before.

Measured on VectorCraft 0.14.0 (2026-10-23, scratch doc), see §9 for the records to add to `references/vectorcraft.md`.

## 3. Document additions

| Artboard # | Name | Content | Export |
|---|---|---|---|
| 3 (appended; Figure stays 0) | `Crop` | photo preview + crop frame | never |

Placed **left of the Workbench**: `x = −(crop_w + BOARD_GAP)`, `y = 0` (Workbench and Profiles never move it; Figure is below the Workbench).

| Layer | Artboard | Lock / print | Object | User may |
|---|---|---|---|---|
| `photo` | Crop | locked, non-printing | image `photo` (embedded JPEG preview) | no |
| `crop` | Crop | unlocked, non-printing | rectangle `crop` (closed 4-anchor path with `live.shape:"rectangle"`), green `(0, 0.75, 0.25)`, 0.75 pt, no fill | **move, rotate, scale with Shift** |

Both layers are appended after the existing ones in `workbench.LAYERS` (before `ep-meta` is fine too; order has no meaning across artboards).

**Display scale.** pt per *original* photo px `s = width_cm·PT / frame_width_px`, so the frame is drawn exactly as wide as the gel face on the Workbench. The preview image's `xf = [s·k, 0, 0, s·k, X, Y]` with `k = W0 / Wp` (original px per preview px). Artboard = the preview's bounds + 0.3 cm margin on each side.

**Frame anchors.** Anchor order is TL, TR, BR, BL **of the gel face** (TL–TR = the wells edge). VectorCraft keeps anchor order through rotate/scale (measured), so the top edge is always anchors 0→1 whatever the angle.

### `ep-meta` key `crop`

```json
"crop": {"photo_size": [4032, 3024],                 // auto-oriented original px
         "photo_sha1": "3f2a…",                       // first 12 hex of sha1 of the photo file
         "frame_px": [[x,y],[x,y],[x,y],[x,y]],       // TL TR BR BL, original px (auto-oriented)
         "stage1": {"cue": "dark", "rect": null, "angle": null, "no_rotate": false,
                    "longest": 700, "ink_thr": null, "gel_lanes": null,
                    "trim_top_cm": 0.0, "trim_bottom_cm": 0.0}}  // the render args, informational
```

## 4. Geometry (`scripts/crop.py`, new, pure stdlib + magick through `magick_io`)

Conventions: original px are **after `-auto-orient`**; `angle` is magick `-rotate` (positive = clockwise), the same as `gel_area.compute` / `geometry.rotate_pt`; the rotated canvas is magick's expanded canvas `(W1, H1)` from `geometry.load_dims_after`.

| Function | Contract |
|---|---|
| `frame_from_stage1(gel_box, angle, size0, size1)` | Rotated-canvas box → 4 corners (TL TR BR BL) in original px: inverse of `geometry.rotated_rect_bbox`'s map (rotate by `−angle` about the canvas centre, shift back by the canvas offset). |
| `stage1_from_frame(frame_px, size0, size1_fn, face_aspect)` | 4 corners → `(angle, gel_box)`. `angle = atan2(−(y1−y0), x1−x0)` in degrees as a **visual CCW** angle of the top edge; that equals the magick angle that levels it (rotating a CCW-tilted frame clockwise by the same amount). `size1_fn(angle)` → `(W1, H1)` (production: `load_dims_after`; tests: closed form). Centre → rotated canvas; `w = |TR − TL|`, `h = w · face_aspect` (**preset aspect, not the drawn height**); `gel_box = (cx−w/2, cy−h/2, cx+w/2, cy+h/2)`. Must round-trip `frame_from_stage1` to ≤ 0.01 px for angles 0, ±0.4, ±3, ±30, 90, 180. |
| `crop_face(image, angle, gel_box, width_cm, height_cm, dpi)` | → gel-face PNG bytes: `-rotate`, **`+repage`**, clamp to canvas, `-crop`, `+repage`, `-resize WxH!`, `+profile "*"`, `-depth 8`, `-density dpi`. `gel_face` calls it. The first `+repage` is the page-offset fix (§4a); unrotated renders are byte-identical to before, rotated ones are not. |
| `legacy_page_shift(angle, size0, size1)` | → `((W1−W0)/2, (H1−H0)/2)` (0 for multiples of 90°): where the pre-fix render actually cropped relative to `gel_box`. Used only by the old-document backfill (§7). |
| `preview(image, long_side=2000)` | → `(jpeg_bytes, (Wp, Hp), k)`; `-auto-orient`, `+profile "*"`, `-quality 85`, never upscales (`k ≥ 1`). |
| `layout(frame_px, size0, preview_size, width_cm, x_right)` | → `{"artboard": (x, y, w, h), "image_xf": [...], "frame_pt": [4 corners], "s": pt per original px}` with the artboard's right edge at `x_right` (= `−BOARD_GAP`). |
| `read_frame(document, meta, face_aspect)` | Live/saved document → `(frame_px or None, errors[])`. `None` = no `Crop` artboard (old document). Corners come from the path's anchors (always present), mapped pt → preview px through the `photo` image `xf` → original px by `photo_size / (width, height)`. |
| `frame_delta(old_px, new_px)` | → `None` if every corner moved ≤ 0.5 original px; else `{"angle_deg", "dx_cm", "dy_cm", "scale"}` for the summary (cm = gel-face cm at the old scale). |
| `photo_sha1(path)` | first 12 hex of the file's sha1. |

### 4a. Page-offset fix (2026-10-23)

Found while building `crop.py`: render ran `-rotate A -crop …` with no `+repage` in between. IM 7 leaves a virtual-canvas offset of `(−(W1−W0)/2, −(H1−H0)/2)` after `-rotate` (0 for multiples of 90°), and `-crop` is measured from it, so the pixels cropped were shifted right/down by `((W1−W0)/2, (H1−H0)/2)` from `gel_box`. That is about 0.25 mm on IMG_1499 (0.214°) and about 1 mm at 1° on a 4032×3024 photo; it may be part of the "frame a bit low" symptom in §1. Fixed by `+repage` after `-rotate`. Consequences: new renders of rotated photos crop ~0.25–1 mm up-left of before (that is, where the frame says); documents rendered before the fix keep their embedded image until their frame is moved, and the backfill (§7) draws their frame where the old crop really was.

### Read-back errors (all listed at once; nothing changes)

| Error (gist) | Fix |
|---|---|
| `crop` layer must hold exactly one closed 4-corner shape (found N) | delete the extra / undo the deletion |
| frame is not a rectangle (corner off by > 0.5°, or opposite sides differ > 0.5 %) | undo the distort; only move / rotate / scale |
| frame aspect `h/w` differs from the preset by > 1 % (`… vs …`) | scale with Shift (uniform) |
| `photo` image missing, or scaled non-uniformly | re-render |

A frame partly outside the photo is **not** an error: `crop_face` clamps exactly as render does and prints the same `[!] clipped` warning (so a render whose detected box already touched the canvas edge still syncs to 0 changes).

## 5. Render

`cmd_render` → after `gel_face` (which now returns the stage-1 values needed): compute `frame_px = frame_from_stage1(...)`, `preview(...)`, store `ep-meta.crop` (§3), and pass a crop spec to `figure.build_document(..., crop=…)`, which adds layers `photo` + `crop`, the JPEG image, the rectangle and the `Crop` artboard as artboard index 3. `vc_file` must accept `image/jpeg` (size from the SOF marker) and clone the rectangle from the template's clip path (`live.shape:"rectangle"`).

Done = a fresh render opens with `warnings: []`, the Crop artboard shows the frame on the gel edges, and an immediate `sync` is **0 ops** (idempotence holds with the Crop artboard present).

## 6. Sync

Order inside `cmd_sync` (new steps in bold):

1. connect, activate, `document.json` → `workbench.from_document`.
2. **`crop.read_frame`** → errors join the Workbench error list (one message, nothing changed; header stays `Workbench 有 N 处要改（什么都没动）`, Crop items prefixed `Crop：`).
3. **If the frame changed** (`frame_delta` not `None`): `ep-meta.photo` must exist and its `photo_sha1` must match, else error `照片 <path> 找不到 / 换过了 —— 把原照片放回去，或重新 render` (nothing changed). Re-crop with `crop_face` at the meta dpi → the new gel PNG is the one analysed (instead of the embedded one). Same pixel size as before (width_cm, height_cm, dpi unchanged).
4. analyse, `plan_sync` as today.
5. **Prepend op `{"op": "relink", "ids": [Workbench gel image + every Figure gel-seg image], "png": <bytes>}`**. `sync_exec` writes the PNG to a temp file, runs `links.relink {ids, path, preserve:"transforms"}` then `links.embed {ids}` inside the same `ep-labeling sync` undo group, deletes the temp file after the group. Any later op that creates a gel segment must use the **new** image key (re-read it after the embed, or create segments by `object.transform copy:true` of the Workbench image as today — verify which `plan_sync` does).
6. `ep-meta.crop.frame_px` ← the new frame (written by the existing ep-meta update).
7. Summary line: `裁切框：转 +0.40°，移 +0.05/−0.10 cm，宽 ×1.020（重新裁了胶面图）`. Without a change, no line.

Idempotence: after a re-crop sync, a second sync is 0 ops (frame equals meta).

## 7. Old documents (no `Crop` artboard)

On sync, when `read_frame` returns `None`:

1. `ep-meta.photo` missing on disk → note `[i] 旧文档没有 Crop 画板（照片 <path> 不在了）；要调裁切就重新 render`, continue as today.
2. Else re-run stage 1 with **default args** (meta has none), shift the box by `legacy_page_shift` (the embedded image was cut by the pre-fix render, §4a), `crop_face` at the meta dpi, and compare with the embedded gel PNG: same pixel size and mean |Δgray| ≤ 2.0 levels → the current crop is recovered. Not equal → the same note with `（当时的裁切参数没记下来）`, continue as today.
3. Recovered → append live ops (same undo group): `artboard.new` (Crop), create layers `photo` / `crop` with their flags, place the preview (`file.place {name, dataBase64}` then `object.transform` into place — mind pitfalls 1 and 5: create in a visible, unlocked current layer, then lock), `shape.rectangle {x, y, width, height, rotation}` + name + stroke, and write `ep-meta.crop`. Summary line `已补上 Crop 画板`. A second sync is 0 ops.

## 8. Not covered

Brightness/contrast, perspective (keystone) correction, a frame with a non-preset aspect, changing `--gel-lanes` or the preset (still a re-render).

## 9. Measured facts (add to `references/vectorcraft.md`)

- `shape.rectangle {x, y, width, height, rotation?}` → a path with `live: {shape:"rectangle", w, h, xf, radii}` and four anchors TL TR BR BL. `object.rotate {angle:10}` (CCW) changed `live.xf` to `[cos, −sin, sin, cos, …]` and rotated the anchors; `object.scale {sx:120}` scaled `live.w/h`. **Anchor order is kept** through both.
- `file.place {name, dataBase64}` embeds PNG and JPEG (`images[key].mime = image/jpeg`), returns `{ids: [id]}`; it is centred in the view, so move it afterwards.
- `links.relink {ids, path, preserve:"transforms"}` + `links.embed {ids}` replace an embedded image's pixels; `xf` and `id` stay, the `key` changes (content-addressed). Works on an image **inside a clip group in a locked layer**; inside `edit.undoGroup` the whole swap is one undo step (`edit.undo` restored the old key).
- `file.close` takes only `{index}`.

## 10. Tests

`selftest` (no app): geometry round trip; `read_frame` on synthetic documents (moved, rotated, scaled, distorted, two rects, missing photo); `crop_face` byte-equal to the pre-refactor `gel_face` path; JPEG `add_image` + rectangle clone; `build_document` with a crop spec writes artboard 3 + layers; `plan_sync`/`sync_exec` with the fake port: re-crop emits relink → embed first in the group, unchanged frame emits nothing, old-doc backfill emits the creation sequence. Manual (app): §5 done-when; rotate the frame 1°, sync → gel straightened, one undo step, second sync 0 ops; an old document gains the artboard.
