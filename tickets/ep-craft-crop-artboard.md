# Tickets: ep-craft Crop artboard

Spec: `specs/ep-craft-crop-artboard.md`. Each ticket keeps `python3 scripts/gel_photo.py selftest` green. Agents share one checkout and own disjoint files (below); nobody commits.

```
wave 1:  C1 crop.py ─┐   C2 vc_file ─┐   C5 docs (from spec)
wave 2:              ├─ C3 render ───┤
                     └─ C4 sync ─────┘
```

| Ticket | Owns (may edit) | Must not touch |
|---|---|---|
| C1 | `scripts/crop.py` (new), `scripts/selftest_crop.py` (new), `gel_photo.gel_face` (refactor only) | everything else |
| C2 | `scripts/vc_file.py`, `selftest.py` (vc_file tests only) | crop.py, figure.py, gel_photo.py |
| C3 | `figure.build_document`, `workbench.LAYERS`, `gel_photo.cmd_render`, `selftest.py` (render tests) | `cmd_sync`, sync_exec, plan_sync |
| C4 | `gel_photo.cmd_sync` / `_summary`, `sync_exec.py`, `figure.plan_sync` (only if needed), `selftest_sync.py` | `cmd_render`, build_document |
| C5 | `SKILL.md`, `references/*.md` | code |

## C1 — `crop.py`: geometry, read-back, re-crop
- Implement spec §4 exactly (signatures and error list). Refactor `gel_photo.gel_face` to call `crop.crop_face` and also return what C3 needs: `(png, ga, stage1)` where `stage1 = {"angle", "gel_box", "size0", "size1"}` — update the two callers (`cmd_render`, `cmd_probe`) only as far as the new return value.
- Tests in `selftest_crop.py` exposing `run()` like `selftest_sync.py`: round trip (§4 angles), `read_frame` cases, `frame_delta` threshold, `crop_face` byte-equal to the old inline magick call on a synthetic photo.
- Done when the tests pass and a real render's `fig.png` is unchanged by the refactor (byte compare against a pre-refactor render of the same photo, if one is at hand; otherwise compare gel PNG bytes in a test).

## C2 — `vc_file`: JPEG images and the rectangle object
- `add_image(doc, data, mime)` accepts `image/jpeg` (size from SOF0/SOF2); `read` keeps the mime.
- `rect(doc, name, corners_pt, rgb, width)` (or similar): clone the template clip path (`live.shape:"rectangle"`), set `live.w/h/xf` and the four anchors from TL TR BR BL corners (rotation allowed), stroke as given, no fill, not a clipping path.
- Tests: JPEG round trip; rect written at 0° and 17° reads back the same corners; written file still passes the template field-completeness check.

## C3 — render: Crop artboard + `ep-meta.crop` (after C1, C2)
- `cmd_render`: `frame_from_stage1`, `preview`, `photo_sha1`, `ep-meta.crop` (spec §3) with the stage-1 args.
- `build_document(..., crop=None)`: layers `photo` / `crop` (append to `workbench.LAYERS`), JPEG image `photo` with `layout()`'s xf, rect `crop`, artboard `Crop` appended (index 3). `crop=None` = old behaviour.
- Manual: render a real photo, open → `warnings: []`, the frame sits on the gel; immediate `sync --dry-run` = 0 ops.

## C4 — sync: re-crop, relink, old-document backfill (after C1, C2)
- Spec §6 and §7. New `relink` op in `sync_exec` (temp PNG, `links.relink preserve:transforms`, `links.embed`, all in the undo group; temp file deleted after). Check how gel segments are created and make sure they end up on the new key.
- Backfill ops for old docs (§7), executed in the same group; verify needed commands exist with `engine.commands` (layer creation, `artboard.new`, `file.place`, `shape.rectangle`).
- Tests in `selftest_sync.py` with the fake port: re-crop order, no-change = no op, backfill sequence, error on missing/changed photo.
- Manual on a copy of a real document: rotate frame 1° → sync → straightened, one undo step, second sync 0 ops; an old document gains the artboard.

## C5 — docs
- `SKILL.md`: Crop artboard in Out / step 7 (what the user can do on it) / step 8 (sync re-crops) / "Re-render only for…" (crop fixes no longer need one) / Not covered (brightness only) / checkpoints (`Crop：` errors, photo missing/changed, old document note).
- `references/photo-labeling-conventions.md`: §4 tables (artboard 3, layers, ownership, ep-meta `crop`), §5 Crop read-back errors, §9 sync steps and summary line.
- `references/vectorcraft.md`: spec §9 measurements.

## C6 — page-offset fix — DONE 2026-10-23
- Found in C1: render cropped `((W1−W0)/2, (H1−H0)/2)` px right/down of `gel_box` on rotated photos (no `+repage` after `-rotate`). Spec §4a.
- `crop_face` now does `-rotate A +repage -crop …`; the frame geometry has no shift; `crop.legacy_page_shift` is used only by the old-document backfill.
- Tests: `selftest_crop` 2 adds a dot that must land in the centre of a 3°-rotated frame's crop (fails with the old command); 3 keeps byte equality for unrotated crops and checks legacy-shifted crops reproduce old ones (mean |Δgray| ≤ 2); `selftest_sync` 14 builds the old document with the genuine pre-fix crop.
