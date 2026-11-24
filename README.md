# EPCraft

Two agent skills for **electrophoresis gel figures** (SDS-PAGE, agarose), built on [VectorCraft](https://github.com/storytold/vectorcraft): `epcraft-label` turns a phone photo of a gel into a labeled, true-size figure; `epcraft-quantify` turns the report from a gel with loaded protein standards into band areas, a standard curve and per-band masses.

> **EP** = electrophoresis, **Craft** = [VectorCraft](https://github.com/storytold/vectorcraft). EPCraft is not part of VectorCraft and is not endorsed by its authors.

## What it does

The skills are `epcraft-label` and `epcraft-quantify`. They share one distribution, one `epcraft` executable and one `fig.mw.json` v2 handoff: labeling writes the report, quantification reads it. Neither skill replaces the other, and quantification never opens VectorCraft.

Labeling: detection makes a first guess; each detected ladder band becomes a `band <MW>` line with its molecular weight already attached, so there is no pairing step. Correcting it by dragging lines in VectorCraft is optional, and the agent re-reads your lines and updates everything in place.

1. **Render**: crop, straighten and rescale the gel photo to its real physical size. Seed lanes from the wells and hang the marker's MWs on the ladder bands it detects (in order, at the detected positions). If the ladder lane does not look like a ladder, the figure and MWs are still produced, marked **low confidence** (`marker_confidence` in the document, `low_confidence` in `fig.mw.json`). Write an editable VectorCraft document.
2. **Edit by hand** on the *Workbench* artboard: drag lane separators, band lines and the top/bottom window. Rename or restyle labels on the *Figure*.
3. **Sync**: the agent reads the live document, recomputes profiles, bands and molecular weights, updates the Figure and re-exports. Running it twice changes nothing.

Output (three files, same stem):

| File | Content |
|---|---|
| `fig.vectorcraft` | Editable document: artboards `Figure` (exported), `Workbench` (edit here), `Profiles` (curves), `Crop` (photo + crop frame) |
| `fig.png` | The Figure artboard, exported by VectorCraft at the chosen dpi |
| `fig.mw.json` | Calibration points, raw per-lane intensity profiles, called bands with MW (two-point interpolation between neighbouring marker bands) |

Built-in data: three epizyme (雅酶) pre-stained marker presets (WJ101, WJ103 in Tris-Glycine and Tris-Tricine) and a `mini-gel` size preset. See [`skills/epcraft-label/SKILL.md`](skills/epcraft-label/SKILL.md) for the full workflow, checkpoints and flags.

**Quantification is a second skill, not a mode of the first.** For SDS-PAGE gels with loaded protein standards, `epcraft-quantify` reads the `fig.mw.json` v2 report and writes five sibling files — `fig.dens.json`, `fig.curve.json`, `fig.quant.json`, `fig.bands.png`, `fig.curve.png`. The chain is offline, opt-in per gel, and leaves the three labeling outputs read-only. See [`skills/epcraft-quantify/SKILL.md`](skills/epcraft-quantify/SKILL.md) and the `densitometry` commands below.

## Commands

The public entry point is the single executable `epcraft`, shared by both skills. Commands are grouped by the VectorCraft canvas they work on; every sync refreshes the document and all dependent canvases in one undo step.

| Command | Does |
|---|---|
| `epcraft launch [DOC] [--port 7797]` | Attach to (or launch) VectorCraft with `--control`; optionally open `DOC`. |
| `epcraft figure render IMAGE --out DOC ...` | Detect the gel, seed lanes and marker bands, write `DOC`, export the Figure PNG, write the MW report. |
| `epcraft figure edit DOC ...` | Apply lane inclusion, lane names or marker-lane changes; then sync (alias of `workbench edit`). |
| `epcraft figure sync DOC ...` | Re-read the live document and refresh the document, PNG and MW report. |
| `epcraft figure export DOC [--out PATH] [--dpi N] [--background C] [--port 7797]` | Export the `Figure` artboard to PNG (default `<DOC stem>.png`; `--out` must end in `.png`; refuses to overwrite the document; checks run before VectorCraft is contacted). |
| `epcraft crop probe IMAGE ...` | Probe the photo without writing files or launching the app. |
| `epcraft crop sync DOC ...` | Re-crop the gel face from a moved Crop frame, then refresh everything. |
| `epcraft workbench edit DOC ...` | Same as `figure edit`. |
| `epcraft workbench sync DOC ...` | Same as `figure sync`. |
| `epcraft workbench export DOC [--out PATH]` | Dump lane geometry, window, marker bands, trim and lane names as JSON (stdout when `--out` is omitted). |
| `epcraft workbench import DOC --input PATH [--no-sync]` | Validate and apply an exported lane-data JSON to the live document; sync unless `--no-sync`. |
| `epcraft profile sync DOC ...` | Recompute the Profiles canvas (shared sync handler). |
| `epcraft densitometry measure REPORT [--out DENS] ...` | Measure every band's area on two baselines from `fig.mw.json`; writes `fig.dens.json` next to it. |
| `epcraft densitometry curve --dens DENS --std ...` | Fit the standard curve from the standard lanes; writes `fig.curve.json`. |
| `epcraft densitometry quantify --dens DENS --curve CURVE ...` | Map each sample band's area to mass; writes `fig.quant.json`. |
| `epcraft densitometry plot-bands --dens DENS ...` | Plot local peaks on a shared zero baseline. Single-lane panels shade the integration windows. Repeat `--group 'Standards (mg/mL)=2@0.25,3:1@0.5'` to overlay peaks with window endpoints and `coolwarm` concentration colors. Writes `fig.bands.png`. |
| `epcraft densitometry plot-curve --curve CURVE ...` | Render the standard curve, residuals and (with `--quant`) the sample points; writes `fig.curve.png`. |
| `epcraft doctor` / `epcraft selftest` | Environment check / offline smoke check for the installed package. |

`sync` accepts `--port 7797`, `--dpi 300`, `--dry-run` and `--reset-peaks [N ...]`; `render` keeps all its detection and layout flags (see the [label manual](skills/epcraft-label/SKILL.md)). Exit code `0` = success, `2` = expected user/environment error (message on stderr, no traceback).

**Densitometry is the whole tool surface of `epcraft-quantify` and touches no other file.** Its five `epcraft densitometry` commands (short alias `epcraft dens`: `measure`, `curve`, `quantify`, `plot-bands`, `plot-curve`) read the `fig.mw.json` that `render` / `sync` already wrote and write **sibling** JSON/PNG files next to it — `fig.dens.json`, `fig.curve.json`, `fig.quant.json`, `fig.bands.png`, `fig.curve.png`. They never launch VectorCraft, never modify `fig.vectorcraft`, and never rewrite `fig.png` or `fig.mw.json`: the three-file render output is exactly what it was. The chain is meant to be inspected between the steps, so each command reads the previous file and a bad window or a bent fit is caught before the next one runs. `--std` names the standard lanes (`2=20` = that lane's strongest band, `3:2=10` = the second band from the top, `4:*=5` = the lane total). A band whose area falls outside the standard range gets no mass (`mass: null`, `out_of_range: true`) — below the range that means loading more or concentrating, above it loading less — and the chain never extrapolates the curve.

### Staged workflow

`epcraft workflow` exposes each canvas transition separately. Start a document, import the photo, adjust its Crop frame in VectorCraft, then seed Workbench. After editing lanes or bands, refresh Profiles and Figure independently and export the Figure canvas as PNG:

```bash
epcraft workflow init-canvas fig.vectorcraft
epcraft workflow img-to-crop photo.jpg --doc fig.vectorcraft \
    --gel-size src/epcraft/data/gel_size.json
epcraft workflow crop-to-workbench fig.vectorcraft \
    --markers src/epcraft/data/markers.json --preset 1 --lanes lanes.json
epcraft workflow workbench-to-profile fig.vectorcraft
epcraft workflow workbench-to-figure fig.vectorcraft
epcraft workflow export fig.vectorcraft --out fig.png --dpi 300
```

`init-canvas` creates empty `Figure`, `Workbench`, `Profiles` and `Crop` canvases. `img-to-crop` takes the same detection flags as `crop probe`, including the required `--gel-size`. `crop-to-workbench` takes marker and lane configuration, `--trim-lanes`, label angles and `--profiles stack|strips`. It seeds the Workbench from the edited crop; running it again reseeds that canvas. Each derived-canvas command reads the current Workbench and updates its target. PNG export runs only with `workflow export`, which uses the same Figure selection and output protection as `figure export`. All stages accept `--port`; the derived-canvas commands also accept `--dry-run` and `--reset-peaks [N ...]`.

## Install

Both skills require Python 3.9+. **Labeling** also needs [ImageMagick 7](https://imagemagick.org) (`brew install imagemagick`) and [VectorCraft](https://github.com/storytold/vectorcraft) 0.14.x. **Quantification** uses the standard library for `measure`, `curve` and `quantify`. Its two PNG commands use headless Matplotlib, installed through the optional `[plot]` extra.

Clone the repository and symlink **both** skills into your agent's skills directory — they share one CLI, so install once and pick the skill per task:

```bash
git clone git@github.com:flmaximwang/EPCraft.git
ln -s "$PWD/EPCraft/skills/epcraft-label" ~/.claude/skills/epcraft-label
ln -s "$PWD/EPCraft/skills/epcraft-quantify" ~/.claude/skills/epcraft-quantify
```

The skill directories hold documentation only. The CLI lives in the repository-root package `src/epcraft/` and is always run from an installed tool or through the repo's `src/`.

Install the CLI as a [uv tool](https://docs.astral.sh/uv/) from a checkout that contains both skills, such as `main` after the split or a later release tag. Install from the checkout root because the package is not on PyPI.

```bash
uv tool install /path/to/EPCraft   # the checkout root, at that revision
epcraft doctor
```

For densitometry PNGs, install the extra from the same checkout. Use `--force` to add it to an existing uv tool installation.

```bash
uv tool install --force '/path/to/EPCraft[plot]'
```

**Upgrading from the combined skill.** The old install used one `ep-craft` skill and one `ep-craft` tool; neither is updated in place. If you installed it, remove the old tool and old skill link, then install the new build and symlink both skills as above:

```bash
uv tool uninstall ep-craft        # if the old tool is still installed
rm ~/.claude/skills/ep-craft      # the old skill symlink (or directory)
uv tool install /path/to/EPCraft
ln -s "$PWD/EPCraft/skills/epcraft-label" ~/.claude/skills/epcraft-label
ln -s "$PWD/EPCraft/skills/epcraft-quantify" ~/.claude/skills/epcraft-quantify
```

For development, install the checkout editable so edits under `src/epcraft/` take effect without reinstalling:

```bash
uv tool install -e /path/to/EPCraft
```

To run without installing, use the package from the repo root:

```bash
PYTHONPATH=src python3 -m epcraft doctor
```

`uv tool install` puts one executable on your PATH (usually `~/.local/bin`; run `uv tool update-shell` if it is missing). The base package uses the standard library. `epcraft doctor` reports whether Matplotlib is available. `epcraft selftest` checks the bundled gel preset, document template and a densitometry calculation offline. Runtime modules live in the `epcraft` package; presets and templates ship as package data, so locate them with `$(uv tool dir)/epcraft/bin/python -c "import epcraft; print(epcraft.data_dir())"` (a checkout's `src/epcraft/data/` remains the simplest source). Remove the tool with `uv tool uninstall epcraft`.

## Running the regression suite

From the repository root, run `PYTHONPATH=src python3 -m tests` with ImageMagick on `PATH`. Use a Python environment with `epcraft[plot]` installed to include the headless Matplotlib checks. The suite prints an explicit skip for those checks in a base environment.

## Repository layout

| Path | Content |
|---|---|
| [`skills/epcraft-label/`](skills/epcraft-label/) | Labeling skill: `SKILL.md`, `references/` (conventions, pitfalls, VectorCraft) — no runnable scripts |
| [`skills/epcraft-quantify/`](skills/epcraft-quantify/) | Quantification skill: `SKILL.md`, `references/densitometry.md` — consumes the labeling report |
| `src/epcraft/` | Installable package shared by both skills, with runtime modules, bundled `data/` and `templates/` |
| [`tests/`](tests/) | Repository regression suites and fixtures |
| [`pyproject.toml`](pyproject.toml) | Packaging: console script `epcraft`, package data, setuptools build |
| [`specs/`](specs/) | Design specs (VectorCraft output, editable output, gel prior, skill split) |
| [`tickets/`](tickets/) | Work breakdown for the VectorCraft output |

## Acknowledgements and citing

EPCraft would not exist without **VectorCraft**. It provides the document format, the editing GUI, the rendering and the control port that this skill drives.

- **VectorCraft**, the VectorCraft authors, <https://github.com/storytold/vectorcraft>. Dual-licensed MIT OR Apache-2.0. EPCraft does not bundle VectorCraft; it launches the installed app and talks to it through its `--control` TCP port. The port protocol is documented upstream in [`docs/control-protocol.md`](https://github.com/storytold/vectorcraft/blob/main/docs/control-protocol.md), and the file format and commands this skill relies on are summarized in [`references/vectorcraft.md`](skills/epcraft-label/references/vectorcraft.md).

If you use EPCraft for a figure in a publication, please state that the figure was prepared with VectorCraft, and cite EPCraft:

```bibtex
@software{epcraft,
  author  = {Wang, Fanlin},
  title   = {EPCraft: electrophoresis gel figure labeling on VectorCraft},
  year    = {2026},
  url     = {https://github.com/flmaximwang/EPCraft}
}
```

### Other things used or read

| Project | License | How it is used |
|---|---|---|
| [ImageMagick](https://github.com/ImageMagick/ImageMagick) | ImageMagick License (Apache-2.0 compatible) | **Runtime dependency**: all pixel work (rotate, crop, scale, read). Not bundled. |
| [GelGenie](https://github.com/mattaq31/GelGenie) (Aquilina et al., *Nat Commun* **16**, 4087, 2025) | Apache-2.0 | **Read only**: confirmed that gel AI models segment *bands*, while lanes are derived by rules. Hence EPCraft uses a rule chain instead of a model. No code copied. |
| [weenke/gel-analysis](https://github.com/weenke/gel-analysis) | MIT | **Read only**: lane grouping and smiling correction, and its known pitfalls. No code copied. |
| epizyme (雅酶) WJ101 / WJ103 marker band tables | Vendor product page and manual | **Data**: copied into the built-in presets; source and retrieval date are in each preset's `_source` field. |

## License

[MIT](LICENSE) © 2026 flmaximwang.
