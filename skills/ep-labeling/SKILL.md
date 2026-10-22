---
name: ep-labeling
description: Use when labeling an electrophoresis gel photo (SDS-PAGE, agarose). Crops, straightens and rescales the gel to real size, seeds lanes from the wells and marker lines from the ladder, and writes an editable VectorCraft document (fig.vectorcraft), the figure PNG exported from it, and a MW report with raw per-lane profiles. The user corrects lanes and bands by hand in VectorCraft; `sync` follows the correction.
compatibility: macOS; python3 (stdlib only), ImageMagick 7 (`magick`), VectorCraft 0.14.x (driven through its `--control` port; the skill launches it)
---

# Electrophoresis Labeling

Turn a phone photo of a gel into a labeled figure at true physical size. Detection makes a first guess; the user fixes it by dragging lines in VectorCraft; the agent re-reads the lines and updates labels, curves, the figure and the MW report in place.

## Scope

- In: one gel photo, a marker preset, **a gel preset (`--gel-size`, mandatory; no default)**, a name for each lane, which wells hold ladders, lanes to leave out of the figure (optional).
- Out (exactly three files, same stem):

| File | Content |
|---|---|
| `fig.vectorcraft` | The editable document: artboards `Figure` (exported), `Workbench` (edit here), `Profiles` (curves). Structure: `references/photo-labeling-conventions.md` §4. |
| `fig.png` | The Figure artboard, **exported by VectorCraft** at `--dpi` (pHYs set). Always matches what the user sees. |
| `fig.mw.json` | Report v2: calibration, every lane's raw `gray`/`baseline`/`ink` profile, called bands with MW (`--no-mw` drops it). |

- Not covered: perspective correction (shoot straight, or `--angle`), band quantification (profiles are raw data, not densitometry), identifying what a band is, editing the gel crop/angle/brightness in VectorCraft (that is a re-render).

## Files

| Path | Use |
|---|---|
| `scripts/gel_photo.py` | Entry point: `doctor`, `probe`, `render`, `sync`, `selftest`. No built-in data paths; pass the files below explicitly. |
| `data/markers.json` | Marker preset library: `--markers data/markers.json --preset <number or keyword>` (an ambiguous choice lists all presets). |
| `data/gel_size.json` | Gel preset (`--gel-size`). `lane_count × lane_pitch_cm ≈ width_cm` (±10 %) is checked on load. |
| `templates/mini-gel-15lanes.{lanes,markers}.json` | Example configs; copy and edit. |
| `references/photo-labeling-conventions.md` | Config fields, document structure and ownership, Workbench reading rules, seeding and band calling, `fig.mw.json`, Figure/Profiles layout, `sync` semantics, the label-text recipe. |
| `references/special-cases-and-pitfalls.md` | Close band pairs, cropped photos, damaged gel edge, wells cut off, history. Read when a checkpoint fires. |
| `references/vectorcraft.md` | VectorCraft 0.14 file format and control-port commands (measured). Read before touching the document through the port. |

## Workflow

1. **Ask once, up front** (all together):
   - **Marker preset** from `data/markers.json`; offer the presets as options.
   - **Gel preset**: a `name` in `data/gel_size.json`, or the user's width × height and lane count. No preset = no run.
   - **Which lanes to trim** from the figure (`--trim-lanes`, 1-based, e.g. `1,2,14-15`; omitted = keep all). Trimmed lanes are still analysed.
   - **Which wells hold ladders** (→ `lanes.json` `marker_lanes`, in order of preference; list every ladder well, so a damaged one can be skipped).
   Then ask what sample is in each lane (left to right). **DO NOT** research on your own, **DO NOT** research on your own, **DO NOT** research on your own, **ASK** immediately.
2. **Check the environment**: `python3 scripts/gel_photo.py doctor`. Missing `magick` → install ImageMagick 7 (`brew install imagemagick`). Missing VectorCraft → stop; it is required (it exports `fig.png`). Port 7797 "VectorCraft running without --control" → ask the user to save and quit VectorCraft first.
3. **Write `lanes.json`** (`labels` left to right, strings, `""` = blank lane without a label; `marker_lanes`) from the template. Use a library marker preset, or a copied `markers.json` (`labels` top to bottom, `rows: 2` for a close pair).
4. **Optional probe** (no files, no app): `python3 scripts/gel_photo.py probe <photo> --gel-size data/gel_size.json` prints the gel face, the well seed (pitch, first lane, well depth) and every lane's called band y values. Use it to pick a marker lane or debug stage 1 (`--cue chroma`, `--rect x0,y0,x1,y1`, `--angle`).
5. **Render**:
   ```bash
   python3 scripts/gel_photo.py render <photo> --out fig.vectorcraft \
       --markers data/markers.json --preset <N> --lanes lanes.json \
       --gel-size data/gel_size.json [--trim-lanes 1,2] [--dpi 300] [--profiles stack|strips]
   ```
   It launches VectorCraft with `--control 7797` if needed, opens the document, exports `fig.png`, writes `fig.mw.json` and prints the MW table. An existing `fig.vectorcraft` is backed up to `fig.vectorcraft.bak`.
6. **Read `fig.png` back** (do not trust the log): each marker label sits on its band, lane labels sit over their lanes, `magick identify -format "%x" fig.png` shows the dpi.
7. **Hand over** `fig.png` and say the document is open in VectorCraft. On the **Workbench** artboard the user can:
   - drag the cyan **lane** lines (each lane = a left and a right line, paired by x order); add or delete a pair;
   - drag the orange **top** / **bottom** window lines (shared by all lanes);
   - drag, delete or rename the magenta **`band <MW>`** lines (the name is the calibration value);
   - move a lane's **two** lines into the `trimmed` layer to drop it from the Figure (and back to restore);
   - on the **Figure**, freely change label text, font, size and colour, and add free text in `labels`.
   Then tell the agent "I edited it".
8. **Sync** when the user says they edited: `python3 scripts/gel_photo.py sync fig.vectorcraft` (`--dry-run` to preview). It reads the live document (no need to save first), recomputes profiles, bands and MW, updates the Figure, labels and curves in **one undo step** (`ep-labeling sync`), saves, re-exports `fig.png` and `fig.mw.json`, and prints what changed. Running it twice changes nothing. **Never re-render to fix lanes or bands**; a re-render starts a fresh document and loses the user's edits. Re-render only for a new photo, a new crop/stage-1 fix, or a different gel or marker preset.
9. **Text-only requests** ("rename lane 3 to WT"): set the label text through the control port (recipe: conventions §9c), or ask the user to type it on the Figure; then `sync` to re-export. Labels keep their user text, font, size and colour; only their position is the agent's.
10. **New lanes come back as "?"**: `sync` prints `[?] 泳道 [n] 还没有名字`. Ask the user for the names, set them (step 9), `sync` again.

## Checkpoints

| When | Do |
|---|---|
| `gel preset required (--gel-size FILE)` | Ask for the gel preset. `--gel-cm` only overrides width/height inside a preset. |
| `[!] scale: 加样孔孔距 … 预设 lane_pitch_cm …` (>10 %) | Width scale or preset is off, or the photo is cropped (`--gel-lanes N`). Ask; don't guess. |
| `看不清孔，预设间距居中` (no visible wells) | Lanes are an even comb at the preset pitch, not anchored. Tell the user to check the lane lines on the Workbench (special cases §3). |
| `marker 泳道 n 配不上` / render refuses with the called y values | The ladder lane called a different number of bands than the labels need. Render already tried every listed marker lane. Read the listed y values: pick another ladder well (`marker_lanes`), fix the labels (`rows: 2`, drop a band the ladder lacks), or check the lane on `probe`. Never force a pairing. |
| `VectorCraft 在运行，但端口 7797 上没有控制口` | VectorCraft was opened normally. Ask the user to save and quit it, then re-run; the skill relaunches it with `--control`. |
| render: `开着 fig.vectorcraft 而且有没存的改动` | The document is open with unsaved edits. Ask the user: save it (then `sync` is probably what they want) or pass another `--out`. |
| sync: `Workbench 有 N 处要改（什么都没动）` | Nothing was changed. Relay every listed fix (odd line count in `lanes`/`trimmed`, overlapping pairs, window not exactly two horizontal lines or top below bottom, a band line not named `band <MW>`, band lines outside or across lanes, fewer than 3 bands, MW not decreasing downwards). Meanings: conventions §5. After the user fixes them, `sync` again. |
| Figure is spliced (non-adjacent kept lanes) | The figure legend **must** state that non-adjacent lanes were spliced from one gel; labels keep the original lane numbers. |
| Marker lane is trimmed | Calibration still uses it; no marker labels are drawn on the Figure. Tell the user. |
| Calibration R² low (< ~0.98) or `*` extrapolated values | Report them; suggest checking the `band` lines against the ladder on the Workbench. Don't hide extrapolation. |
| Two bands too close for separate labels (21/20 kD) | One label with `"rows": 2` (two band lines, one label). |
| User says the photo is cropped | `--gel-lanes N` (N visible lanes); otherwise the scale is wrong. |
| A removed flag (`--keep-lanes`, `--svg`, `--debug`, `--record`, `export`, …) | The script names the replacement; follow it (conventions §11). |

## Done when

- `fig.vectorcraft`, `fig.png` and `fig.mw.json` exist; `fig.png` passed the read-back and has the intended dpi; the document is open in VectorCraft on the control port.
- The reply includes the MW table (strongest band per lane, R², any extrapolation), the configs used, the script's warnings, which lanes are unnamed, and anything not verified. If lanes were spliced, it states the splice.
- After every `sync`, the reply relays the printed change summary.
