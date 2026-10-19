---
name: ep-labeling
description: Use when labeling an electrophoresis gel photo (SDS-PAGE, agarose). Crops, straightens and rescales the gel to real size, writes marker sizes and lane names, and exports a labeled PNG.
compatibility: python3 (stdlib only), ImageMagick 7 (`magick`)
---

# Electrophoresis Labeling

Turn a phone photo of a gel (gel in the middle, background around it) into a labeled PNG at true physical size, ready for a report or paper.

## Scope

- In: one gel photo, marker type, gel type, a name for each lane.
- Out: one PNG (white canvas, labels outside the gel, dpi written into the file).
- Not covered: perspective correction (shoot straight, or pass `--angle`), band quantification.

## Files

| Path | Use |
|---|---|
| `scripts/gel_photo.py` | Entry point: `probe`, `render`, `doctor`, `selftest`. Implementation is split into sibling modules (see `references/photo-labeling-conventions.md` §5). It has no built-in data paths; pass the files below explicitly. |
| `data/markers.json` | Library of marker presets. Pass it with `--markers` and choose one with `--preset <number or keyword>` (an ambiguous choice lists all presets). |
| `data/gel_size.json` | Physical gel size (`width_cm`, `height_cm`, `trim_top_cm`, `lane_pitch_cm`). Pass with `--gel-size`; `--gel-cm WxH` overrides. |
| `templates/mini-gel-15lanes.{lanes,markers}.json` | Finished configs for a 15-lane mini gel; copy and edit. |
| `references/photo-labeling-conventions.md` | JSON field tables, detection mechanism, sizing rules. Read when writing or debugging a config. |
| `references/special-cases-and-pitfalls.md` | Close band pairs (`rows: 2`), cropped photos (`--gel-lanes`), manual calibration (`y_px`), pitfalls, measured baseline. Read when a checkpoint below fires. |

## Workflow

1. **Ask** the user: which marker, which gel type, what sample is in each lane (left to right). Offer the presets in `data/markers.json` as options.
2. **Check the environment**: `python3 scripts/gel_photo.py doctor`. If `magick` is missing, stop and install ImageMagick 7 (`brew install imagemagick`, `winget install ImageMagick.ImageMagick`, `apt install imagemagick`).
3. **Probe first**: `python3 scripts/gel_photo.py probe <photo> --debug debug.png`. Check that the red box frames the gel, the lane lines sit on lanes, and the band lines match the marker. Fix the cause before rendering (`--cue chroma`, `--rect x0,y0,x1,y1`, `--ink-thr`, `--longest`, `--angle`).
4. **Write two configs**: `markers.json` (labels top to bottom; or use `--markers data/markers.json --preset N`) and `lanes.json` (labels left to right), starting from a template.
5. **Render**:
   ```bash
   python3 scripts/gel_photo.py render <photo> --out out.png \
       --markers markers.json --lanes lanes.json \
       --gel-size data/gel_size.json --dpi 300 --debug debug.png
   ```
   Sizes (`size`, `gap`) are in pt; dpi changes pixel count only, not physical size.
6. **Read the PNG back** (do not trust the log alone): each marker label sits on one band; lane labels are on white margin, with the right count and positions; `magick identify -format "%x" out.png` shows the dpi.

## Checkpoints

| When | Do |
|---|---|
| Band count found ≠ marker label count | Script refuses to render. Do not use `--pair-force`. Calibrate with `y_px` (see special cases §2) or pick the right preset. |
| Two bands too close for separate labels (e.g. 21/20 kD) | Merge into one label with `"rows": 2` (§1). |
| User says the photo is cropped / not all lanes | Pass `--gel-lanes N` (§1); otherwise the size is wrong. |
| `lanes.labels` count > detected lanes | Script errors. Count lanes on the debug overlay; give missing ones via `lanes.x_frac`. |
| Orange lanes on the overlay | They are estimated, not seen. Give `x_frac` if exact positions matter. |
| Dark and chroma gel boxes disagree | Look at the overlay; use `--cue chroma` or `--rect`. |
| Dense ladder cannot be fully labeled | Not a bug: raise dpi, shrink `size`, or label only key bands. |
| Output file already exists | The script overwrites silently; name outputs deliberately. |

## Done when

- `out.png` exists, passes the read-back in step 6, and has the intended dpi.
- The reply states: the config files used, any warnings from the script, and anything not verified (e.g. estimated lanes).
