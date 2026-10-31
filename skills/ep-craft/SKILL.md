---
name: ep-craft
description: Use when labeling an electrophoresis gel photo (SDS-PAGE, agarose). Crops, straightens and rescales the gel to real size, seeds lanes from the wells and marker lines from the ladder, and writes an editable VectorCraft document (fig.vectorcraft), the figure PNG exported from it, and a MW report with raw per-lane profiles. The user corrects lanes and bands by hand in VectorCraft; `sync` follows the correction.
compatibility: macOS; python3 (stdlib only), ImageMagick 7 (`magick`), VectorCraft 0.14.x (driven through its `--control` port; the skill launches it)
---

# Electrophoresis Labeling

Turn a phone photo of a gel into a labeled figure at true physical size. Detection makes a first guess; the user fixes it by dragging lines in VectorCraft; the agent re-reads the lines and updates labels, curves, the figure and the MW report in place.

## Scope

- In: one gel photo, a marker preset, **a gel preset (`--gel-size`, mandatory; no default)**, a name for each lane, which wells hold ladders, lanes to leave out of the figure (optional), a label angle (optional: `--label-angle` for lane names, `--marker-label-angle` for marker labels, or `angle` on a single `lanes.json` / `markers.json` entry; degrees, counter-clockwise, 90 = reads bottom-to-top).
- Out (exactly three files, same stem):

| File | Content |
|---|---|
| `fig.vectorcraft` | The editable document: artboards `Figure` (exported), `Workbench` (edit here), `Profiles` (curves), `Crop` (photo preview + crop frame, left of the Workbench). Structure: `references/photo-labeling-conventions.md` §4. |
| `fig.png` | The Figure artboard, **exported by VectorCraft** at `--dpi` (pHYs set). Always matches what the user sees. |
| `fig.mw.json` | Report v2: calibration points, every lane's raw `gray`/`baseline`/`ink` profile, called bands with MW (`--no-mw` drops it). MW comes from two-point interpolation between neighbouring marker bands — no global fit. |

- Not covered: perspective correction (shoot straight, or `--angle`), band quantification (profiles are raw data, not densitometry), identifying what a band is, brightness/contrast edits, a crop frame with a non-preset aspect (crop position/angle/scale **are** editable on `Crop`).

## Files

| Path | Use |
|---|---|
| `scripts/gel_photo.py` | Entry point: `doctor`, `probe`, `render`, `sync`, `edit`, `selftest`. No built-in data paths; pass the files below explicitly. |
| `data/markers.json` | Marker preset library: `--markers data/markers.json --preset <number or keyword>` (an ambiguous choice lists all presets). |
| `data/gel_size.json` | Gel preset (`--gel-size`). `lane_count × lane_pitch_cm ≈ width_cm` (±10 %) is checked on load. |
| `templates/mini-gel-15lanes.{lanes,markers}.json` | Example configs; copy and edit. |
| `references/photo-labeling-conventions.md` | Config fields, document structure and ownership, Workbench reading rules, seeding and band calling, `fig.mw.json`, Figure/Profiles layout, `sync` semantics, the label-text recipe. |
| `references/special-cases-and-pitfalls.md` | Close band pairs, cropped photos, damaged gel edge, wells cut off, history. Read when a checkpoint fires. |
| `references/vectorcraft.md` | VectorCraft 0.14 file format and control-port commands (measured). Read only when an edit through the port isn't covered by conventions §9c. |

## Hard rules: do the work, don't audit it

Detection is a **first guess the user corrects by hand** in VectorCraft. The agent's job is to get a document open fast, not to make detection perfect. A full job (3 photos, 5 figures) is about 10 `render` calls of 10–20 s each: **minutes, not tens of minutes**.

- **The commands in this file are the whole toolbox.** Never write diagnostic scripts, never import the skill's modules from ad-hoc Python to scan, sweep or shift crops, never read the scripts' source. Why: in one session the agent wrote 10 diagnostic scripts over 13 minutes before the first `render`; the user then said "just run everything, I'll fix the lanes myself".
- **Never look at the gel photo to guess geometry** (lanes, ladder, crop). Lane lines, band lines and the crop frame are the user's to fix on the Workbench / Crop artboards.
- **Start from this file and the user's message.** Do not read `references/` up front (open one only when a checkpoint names it), do not dump `data/*.json` beyond listing preset names, do not run `--help`, do not search the disk or memory for earlier outputs.
- **Run each command once.** No `--dry-run` before a real `sync`; no re-reading the document through the control port or re-parsing `fig.mw.json` to double-check what the command already printed. The printed summary is the verification.
- **Act, then report once.** When the user's instruction is clear, do it; don't offer "A or B?" and don't confirm before and again after. Ask only at a checkpoint below that says *ask*. Why: the user's complaint was "my time all went into your useless confirmations".
- **Several photos or several figures from one photo:** write all configs, then run every `render` in one shell loop; report once at the end.
- **Stop rule:** two attempts at the same problem without a new output file → stop, report what the script printed, and hand over (or ask if a checkpoint says so). Don't open a third line of investigation.

## Workflow

1. **Ask once, only for what's missing.** If the user's message already gives an item, don't ask for it. Further questions are prohibited except checkpoints or `?` by `sync`:
   - **Marker preset** from `data/markers.json`; offer the presets as options.
   - **Gel preset**: a `name` in `data/gel_size.json`, or the user's width × height and lane count. No preset = no run.
   - **Which lanes to trim** from the figure (`--trim-lanes`, 1-based, e.g. `1,2,14-15`; omitted = keep all). Trimmed lanes are still analysed.
   - **Which wells hold ladders** (→ `lanes.json` `marker_lanes`, in order of preference; list every ladder well, so a damaged one can be skipped).
   - **What sample is in each lane** (left to right). Never research this; ask.
2. **Check the environment**: `python3 scripts/gel_photo.py doctor`. Missing `magick` → install ImageMagick 7 (`brew install imagemagick`). Missing VectorCraft → stop; it is required (it exports `fig.png`). Port 7797 "VectorCraft running without --control" → ask the user to save and quit VectorCraft first.
3. **Write `lanes.json`** (`labels` left to right, strings, `""` = blank lane without a label; `marker_lanes`) from the template. Use a library marker preset, or a copied `markers.json` (`labels` top to bottom, `rows: 2` for a close pair).
   - **One photo cut into several figures** ("2–7 one figure, 8–15 another"): one `render` per figure with its own `--out` (`IMG_x_A.vectorcraft`, `IMG_x_B.vectorcraft`) and `--trim-lanes` = every lane *outside* that figure. Same `labels`; in `marker_lanes` put the ladder well **inside** the kept range first, then the other ladder wells as fallbacks.
4. **Skip `probe`.** Go straight to `render`. Run `probe` (no files, no app; prints the gel face, well seed and every lane's called band y values) only once, after `render` refused, to read which lanes call how many bands.
5. **Render**:
   ```bash
   python3 scripts/gel_photo.py render <photo> --out fig.vectorcraft \
       --markers data/markers.json --preset <N> --lanes lanes.json \
       --gel-size data/gel_size.json [--trim-lanes 1,2] [--dpi 300] [--profiles stack|strips] \
       [--label-angle 90] [--marker-label-angle 0]
   ```
   It launches VectorCraft with `--control 7797` if needed, opens the document, exports `fig.png`, writes `fig.mw.json` and prints the MW table. If no ladder lane calls exactly as many bands as the labels need, it still writes the document with a **draft calibration** (`[!] 草稿标定`): band lines at guessed positions, MW not trustworthy (checkpoint below). `--no-draft` refuses instead. An existing `fig.vectorcraft` is backed up to `fig.vectorcraft.bak`.
6. **Check the dpi only**: `magick identify -format "%x %wx%h\n" fig.png` (one call for all outputs). The visual check is the user's: the document is open in VectorCraft.
7. **Hand over** `fig.png` and say the document is open in VectorCraft. On the **Workbench** artboard the user can:
   - drag the cyan **lane** lines: they are separators, so neighbouring lanes share one line (n lanes = n+1 lines, every gap between two neighbouring lines is a lane); add one line to split a lane, delete one to merge two;
   - drag the orange **top** / **bottom** window lines (shared by all lanes);
   - drag, delete or rename the magenta **`band <MW>`** lines (the name is the calibration value);
   - draw a short line in the `trimmed` layer inside a lane to drop that lane from the Figure (delete the mark to restore; the lane lines stay in `lanes`);
   - on the **Figure**, freely change label text, font, size, colour and angle (rotate a label), and add free text in `labels`;
   - on **Crop**, move / rotate / Shift-scale the green `crop` frame over the photo to fix the gel-face crop (aspect stays the preset's; Workbench lines keep their gel-face cm).
   Then tell the agent "I edited it".
8. **Sync** when the user says they edited: `python3 scripts/gel_photo.py sync fig.vectorcraft` (several documents: one shell loop; no `--dry-run` first, sync is one undo step). It reads the live document (no need to save first), re-crops the gel face from the full-resolution photo at `ep-meta.photo` if the `crop` frame moved (pixels swapped in place), recomputes profiles, bands and MW, updates the Figure, labels and curves in **one undo step** (`ep-craft sync`), saves, re-exports `fig.png` and `fig.mw.json`, and prints what changed. Running it twice changes nothing. **Never re-render to fix lanes or bands**; a re-render starts a fresh document and loses the user's edits. Re-render only for a new photo, a different gel or marker preset (crop/angle fixes go through `Crop` + `sync`).
9. **Edits the user asks the agent to make** go through `edit`, never a hand-written control-port script. It applies the change as one undo step (`ep-craft edit`) and then runs `sync`:
   ```bash
   python3 scripts/gel_photo.py edit fig.vectorcraft --keep 2-7          # which lanes go into the Figure (or --trim 1,8-15)
   python3 scripts/gel_photo.py edit fig.vectorcraft --marker-lane 2     # calibrate on another ladder lane (band lines move, y kept)
   python3 scripts/gel_photo.py edit fig.vectorcraft --shift-labels 1    # every lane name one lane to the right
   python3 scripts/gel_photo.py edit fig.vectorcraft --label 9=M --label 3=WT   # or --labels "",M,Total,Sup (from lane 1)
   ```
   Options combine in one call (one call per document). Lane numbers are the Workbench's current lanes. A name set on a lane without a label creates the label at the document's lane-label angle. Rotating a label is the only port recipe left (conventions §9c); or ask the user to do it on the Figure; then `sync`. Labels keep their user text, font, size, colour and **angle**; only their position (the anchor) is the agent's — `sync` reads the angle back and re-anchors, it never rewrites it.
10. **New lanes come back as "?"**: `sync` prints `[?] 泳道 [n] 还没有名字`. Ask the user for the names, set them with `edit --label n=…` (step 9).

## Checkpoints

| When | Do |
|---|---|
| `gel preset required (--gel-size FILE)` | Ask for the gel preset. `--gel-cm` only overrides width/height inside a preset. |
| `[!] scale: 加样孔孔距 … 预设 lane_pitch_cm …` (>10 %) | Width scale or preset is off, or the photo is cropped (`--gel-lanes N`). Ask; don't guess. |
| `看不清孔，预设间距居中` (no visible wells) | Lanes are an even comb at the preset pitch, not anchored. Tell the user to check the lane lines on the Workbench (special cases §3). |
| `marker 泳道 n 配不上` then `[!] 草稿标定` | No listed ladder lane called the right number of bands, so render wrote a draft calibration from the closest lane. **Don't retry or investigate**: hand over and tell the user, plainly, that the MW values are not valid yet and that they should drag each `band <MW>` line onto its ladder band on the Workbench (delete ones the ladder lacks), then say when. `sync` drops the draft flag as soon as any band line changed; until then `fig.mw.json` has `calibration.draft: true` and the report's first note says so. Never quote draft MW values as results. Never sweep `--rect` / crop shifts or write scripts to hunt for a window that "fits". |
| `VectorCraft 在运行，但端口 7797 上没有控制口` | VectorCraft was opened normally. Ask the user to save and quit it, then re-run; the skill relaunches it with `--control`. |
| render: `开着 fig.vectorcraft 而且有没存的改动` | The document is open with unsaved edits. Ask the user: save it (then `sync` is probably what they want) or pass another `--out`. |
| sync: `Workbench 有 N 处要改（什么都没动）` | Nothing was changed. Relay every listed fix (fewer than 2 lane lines, a vertical line or a stray mark in `trimmed`, two lane lines on top of each other, window not exactly two horizontal lines or top below bottom, a band line not named `band <MW>`, band lines outside or across lanes, fewer than 3 bands, MW not decreasing downwards). Meanings: conventions §5. After the user fixes them, `sync` again. |
| sync: items prefixed `Crop：` (in the same list) | The frame was distorted, stretched out of aspect, duplicated/deleted, or `photo` was touched. Relay the fix (undo the distort; scale with Shift only); conventions §5. |
| sync: `照片 … 找不到 / 换过了` | The frame moved but the original photo is gone or changed (sha1). Ask the user to put it back at that path, or re-render. Nothing was changed. |
| sync: `裁切框：… 移 …` with a large move/rotate/scale, and the MW of the main bands jumps | `band` lines keep their gel-face cm, so after a big crop change they no longer sit on the ladder. Tell the user to drag the `band` lines onto the ladder on the Workbench and say when; then `sync`. Don't re-render (it drops their lane edits) unless they ask. |
| sync: `[i] 旧文档没有 Crop 画板…` | Old document; sync went on as before without the artboard. To adjust the crop, re-render. (`已补上 Crop 画板` = it was added; nothing to do.) |
| Figure is spliced (non-adjacent kept lanes) | The figure legend **must** state that non-adjacent lanes were spliced from one gel; labels keep the original lane numbers. |
| Marker lane is trimmed | Calibration still uses it; no marker labels are drawn on the Figure. Tell the user. If they want marker labels on that figure and another ladder lane is kept, `edit --marker-lane <kept ladder lane>`. |
| A band prints `超出 marker 范围` | Its y lies outside the ladder's span, so two-point interpolation gives it no number (never extrapolate). If a real band of interest fell outside, check the `band` lines on the Workbench — a misplaced line shrinks the calibrated span. |
| Two bands too close for separate labels (21/20 kD) | One label with `"rows": 2` (two band lines, one label). |
| User says the photo is cropped | `--gel-lanes N` (N visible lanes); otherwise the scale is wrong. |
| A removed flag (`--keep-lanes`, `--svg`, `--debug`, `--record`, `export`, …) | The script names the replacement; follow it (conventions §11). |
| Unsure about lane alignment, `看不清孔`, or a lane seed that looks off | Render anyway and hand over: the user drags lane lines on the Workbench. Don't investigate. Only a cropped photo changes the command (`--gel-lanes N`). |

## Done when

- `fig.vectorcraft`, `fig.png` and `fig.mw.json` exist; `fig.png` has the intended dpi; the document is open in VectorCraft on the control port.
- The reply includes the MW table (strongest band per lane; bands outside the ladder span are reported as having no number), the configs used, the script's warnings, which lanes are unnamed, and anything not verified. If lanes were spliced, it states the splice.
- After every `sync`, the reply relays the printed change summary.
