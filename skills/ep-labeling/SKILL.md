---
name: ep-labeling
description: Use when labeling an electrophoresis gel photo (SDS-PAGE, agarose). Crops, straightens and rescales the gel to real size, writes marker sizes and lane names, and exports a labeled PNG plus a self-contained HTML editor for the text layer.
compatibility: python3 (stdlib only), ImageMagick 7 (`magick`)
---

# Electrophoresis Labeling

Turn a phone photo of a gel (gel in the middle, background around it) into a labeled PNG at true physical size, ready for a report or paper. The same run writes a one-file HTML editor (`fig.html`) so the user can change lane names, marker labels, sizes, colours and positions themselves in a browser, offline.

## Scope

- In: one gel photo, a marker preset, **a gel preset (`--gel-size`, mandatory)**, the lanes to keep (optional), a name for each lane.
- The gel preset is not optional: there is no "no prior" mode and no default. Without `--gel-size` both `probe` and `render` exit with `gel preset required (--gel-size FILE)`. The preset fixes the gel's shape; detection only locates the gel.
- Outputs: `fig.png` (white canvas, labels outside the gel, dpi written into the file), `fig.gel.png` (the gel face only, no text), `fig.scene.json` (the layout data every writer draws from) and `fig.html` (the editor, with the gel and the scene embedded). `fig.svg` is written only with `--svg`; `--no-editor` suppresses `fig.html` for batch runs. The molecular-weight report (terminal table and `<out>.mw.json`) is unchanged.
- Not covered: perspective correction (shoot straight, or pass `--angle`), band intensity quantification, identifying what a band is.

## Files

| Path | Use |
|---|---|
| `scripts/gel_photo.py` | Entry point: `probe`, `render`, `export`, `doctor`, `selftest`. Implementation is split into sibling modules (see `references/photo-labeling-conventions.md` §5). It has no built-in data paths; pass the files below explicitly. |
| `data/markers.json` | Library of marker presets. Pass it with `--markers` and choose one with `--preset <number or keyword>` (an ambiguous choice lists all presets). |
| `data/gel_size.json` | Gel preset — **mandatory** (`--gel-size`). `width_cm` sets the scale, `height_cm` sets the gel-face height (anchored per `anchor`, minus the trims), and `lane_count` × `lane_pitch_cm` must match `width_cm` (±10 %). `--gel-cm WxH` only overrides width/height inside a preset; it is not a substitute. |
| `templates/mini-gel-15lanes.{lanes,markers}.json` | Finished configs for a 15-lane mini gel; copy and edit. |
| `references/photo-labeling-conventions.md` | JSON field tables, detection mechanism, sizing rules. Read when writing or debugging a config. |
| `references/special-cases-and-pitfalls.md` | Close band pairs (`rows: 2`), cropped photos (`--gel-lanes`), manual calibration (`y_px`), pitfalls, measured baseline. Read when a checkpoint below fires. |

## Outputs

`render --out fig.png` writes:

| File | When | Content |
|---|---|---|
| `fig.png` | always | The figure: gel and labels, `--dpi` pixels, dpi in pHYs. Show this one. |
| `fig.gel.png` | always | The gel face only (crop, straighten, splice line), no text; it is the image the scene references. |
| `fig.scene.json` | always | The layout data (page, gel, one item per label). See `references/photo-labeling-conventions.md` §7. |
| `fig.html` | always, unless `--no-editor` | The editor: one offline file with the gel and the scene embedded. |
| `fig.svg` | with `--svg` | Vector version of the same scene. |
| `fig.mw.json` | as today (`--no-mw` turns it off) | MW report; unchanged. |
| `fig.record.json` | with `--record PATH` | Detection record at the path you give; unchanged. |

## Workflow

1. **Ask once, up front** — this is the only place all three are asked together:
   - **Marker preset**: from `data/markers.json`; offer the presets as options.
   - **Gel preset**: the `name` inside `data/gel_size.json` (pass it with `--gel-size`), or the width × height and lane count the user gives. No preset = no run.
   - **Which lanes to keep** in this batch (`--keep-lanes`, 1-based, e.g. `1,2,8-10`; omitted = all lanes).
   Then ask what sample is in each lane (left to right).
2. **Check the environment**: `python3 scripts/gel_photo.py doctor`. If `magick` is missing, stop and install ImageMagick 7 (`brew install imagemagick`, `winget install ImageMagick.ImageMagick`, `apt install imagemagick`).
3. **Probe first**: `python3 scripts/gel_photo.py probe <photo> --gel-size data/gel_size.json --debug debug.png`. Check that the red box is the preset-shaped gel face, the lane lines sit on lanes, and the band lines match the marker. Fix the cause before rendering (`--cue chroma`, `--rect x0,y0,x1,y1`, `--ink-thr`, `--longest`, `--angle`).
4. **Write two configs**: `markers.json` (labels top to bottom; or use `--markers data/markers.json --preset N`) and `lanes.json` (labels left to right; `""` = blank lane; `marker_lanes` = 1-based lanes holding the marker), starting from a template.
5. **Render**:
   ```bash
   python3 scripts/gel_photo.py render <photo> --out out.png \
       --markers markers.json --lanes lanes.json \
       --gel-size data/gel_size.json --dpi 300 --debug debug.png
   ```
   Sizes (`size`, `gap`) are in pt; dpi changes pixel count only, not physical size. This writes `out.png`, `out.gel.png`, `out.scene.json` and `out.html` (add `--svg` for `out.svg`; `--no-editor` skips the HTML).
6. **Check the MW report** printed after rendering: calibration R² per marker lane, `[主]` marks each lane's strongest band, `*` marks values extrapolated beyond the marker range (indicative only). Marker lanes and blank lanes are skipped. With `--keep-lanes`, only the kept sample lanes are reported; calibration still uses every marker lane. `--mw-method logfit` (default, straight line of log10 MW vs position) or `interp` (between marker bands, no extrapolation); `--no-mw` turns it off.
7. **Read the PNG back** (do not trust the log alone): each marker label sits on one band; lane labels are on white margin, with the right count and positions; `magick identify -format "%x" out.png` shows the dpi. Also check `out.gel.png` has no text and `out.scene.json` has one item per drawn label (ids such as `lane-3` / `marker-2-1`).
8. **Hand over** `out.png` **and** `out.html` together. Say in one line that lane names, marker labels, sizes, colours and positions can be edited in `out.html` in a browser, offline, with undo and a PNG/SVG export.
9. **Text-only changes never re-run `render`.** Rename a lane, remove a label, move the marker column, change a size or colour → edit `fig.scene.json` (or take the user's `fig.html` / edited scene) and run:
   ```bash
   python3 scripts/gel_photo.py export fig.scene.json --png out.png [--dpi 300] [--svg out.svg]
   python3 scripts/gel_photo.py export fig.html --png out.png
   ```
   It takes about a second, touches no analysis and does not re-detect anything.
10. **Analysis changes still need `render`**: crop, `--keep-lanes`, gel preset, marker preset, manual `y_px` calibration, band pairing. A re-run produces a fresh scene; carrying the user's text edits across a re-run is out of scope (the scene's `bind` field exists for a later version).

## Checkpoints

| When | Do |
|---|---|
| No `--gel-size` given | Script exits with `gel preset required (--gel-size FILE)`. Ask the user for the gel preset and re-run. Do not fall back to `--gel-cm` — it only overrides width/height inside a preset. |
| Detected box vs preset size differ by >10 % (script prints `[!]`) | Read the two numbers on that line; ask whether to follow the preset (default: the image is cropped to the preset shape) or to fix the detection with `--rect` first. |
| No marker lane among the kept lanes | Script stops and lists the marker lanes that would be cropped out. Ask the user: `--marker-policy calibrate-only` (calibration/MW only, no marker labels drawn) or `keep` (add the nearest marker lane). Ask every time; never pick for them. |
| Output is a splice (two or more kept strips) | Terminal prints a one-line reminder. **Must** declare in the figure legend that non-adjacent lanes were spliced from one gel, and keep the original lane numbers. |
| Band count found ≠ marker label count | Script refuses to render. Do not use `--pair-force`. Calibrate with `y_px` (see special cases §2) or pick the right preset. |
| Two bands too close for separate labels (e.g. 21/20 kD) | Merge into one label with `"rows": 2` (§1). |
| User says the photo is cropped / not all lanes | Pass `--gel-lanes N` (§1); otherwise the size is wrong. |
| `lanes.labels` count > detected lanes | Script errors. Count lanes on the debug overlay; give missing ones via `lanes.x_frac`. |
| Orange lanes on the overlay | They are estimated, not seen. Give `x_frac` if exact positions matter. |
| Dark and chroma gel boxes disagree | Look at the overlay; use `--cue chroma` or `--rect`. |
| Dense ladder cannot be fully labeled | Not a bug: raise dpi, shrink `size`, or label only key bands. |
| MW report says lane not in `marker_lanes` / extra marker lanes reported as samples | Add `marker_lanes` to `lanes.json`. Without it only the lane named in `markers.json` is treated as marker. |
| Calibration R² low (< ~0.98) or a band shows "out of range" | Report it; try `--mw-method interp`; do not hide extrapolated values. |
| Text-only change: rename a lane, remove a label, move the marker column, change a size or colour | **Do not re-run `render`.** Edit `fig.scene.json` (or use the user's `fig.html` / edited scene) and `python3 scripts/gel_photo.py export fig.scene.json --png out.png [--dpi 300] [--svg out.svg]`. About a second; no detection, no change to lanes or MW. |
| Analysis change: crop, `--keep-lanes`, gel preset, marker preset, `y_px` calibration, band pairing | Re-run `render`. It produces a fresh scene; the user's text edits are not carried across (out of scope — `bind` is for a later version). |
| Batch run used `--no-editor`, user later wants to edit | There is no `fig.html`; re-run without `--no-editor` (or `export fig.scene.json --html out.html`) and hand it over. |
| User edited `fig.html` and wants the PNG updated | `python3 scripts/gel_photo.py export fig.html --png out.png`, which reads the scene from the HTML. No re-detection. |
| Output file already exists | The script overwrites silently; name outputs deliberately. |

## Done when

- `out.png` exists, passes the read-back in step 7, and has the intended dpi; `out.html` was handed to the user with it (or `--no-editor` was deliberate for a batch).
- The reply includes the MW table (strongest band per lane, any extrapolation) and states: the config files used, any warnings from the script, and anything not verified (e.g. estimated lanes). If lanes were spliced, the reply states the splice explicitly (the legend wording above).
