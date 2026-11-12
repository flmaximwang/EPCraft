# EPCraft

An agent skill for **electrophoresis gel figures** (SDS-PAGE, agarose): turn a phone photo of a gel into a labeled, true-size figure, built on [VectorCraft](https://github.com/storytold/vectorcraft).

> **EP** = electrophoresis, **Craft** = [VectorCraft](https://github.com/storytold/vectorcraft). EPCraft is not part of VectorCraft and is not endorsed by its authors.

## What it does

The skill is `ep-craft`. Detection makes a first guess; each detected ladder band becomes a `band <MW>` line with its molecular weight already attached, so there is no pairing step. Correcting it by dragging lines in VectorCraft is optional, and the agent re-reads your lines and updates everything in place.

1. **Render**: crop, straighten and rescale the gel photo to its real physical size. Seed lanes from the wells and hang the marker's MWs on the ladder bands it detects (in order, at the detected positions). If the ladder lane does not look like a ladder, the figure and MWs are still produced, marked **low confidence** (`marker_confidence` in the document, `low_confidence` in `fig.mw.json`). Write an editable VectorCraft document.
2. **Edit by hand** on the *Workbench* artboard: drag lane separators, band lines and the top/bottom window. Rename or restyle labels on the *Figure*.
3. **Sync**: the agent reads the live document, recomputes profiles, bands and molecular weights, updates the Figure and re-exports. Running it twice changes nothing.

Output (three files, same stem):

| File | Content |
|---|---|
| `fig.vectorcraft` | Editable document: artboards `Figure` (exported), `Workbench` (edit here), `Profiles` (curves) |
| `fig.png` | The Figure artboard, exported by VectorCraft at the chosen dpi |
| `fig.mw.json` | Calibration points, raw per-lane intensity profiles, called bands with MW (two-point interpolation between neighbouring marker bands) |

Built-in data: three epizyme (雅酶) pre-stained marker presets (WJ101, WJ103 in Tris-Glycine and Tris-Tricine) and a `mini-gel` size preset. See [`skills/ep-craft/SKILL.md`](skills/ep-craft/SKILL.md) for the full workflow, checkpoints and flags.

## Commands

The public entry point is the executable `ep-craft`. Commands are grouped by the VectorCraft canvas they work on; every sync refreshes the document and all dependent canvases in one undo step.

| Command | Does |
|---|---|
| `ep-craft launch [DOC] [--port 7797]` | Attach to (or launch) VectorCraft with `--control`; optionally open `DOC`. |
| `ep-craft figure render IMAGE --out DOC ...` | Detect the gel, seed lanes and marker bands, write `DOC`, export the Figure PNG, write the MW report. |
| `ep-craft figure edit DOC ...` | Apply lane inclusion, lane names or marker-lane changes; then sync (alias of `workbench edit`). |
| `ep-craft figure sync DOC ...` | Re-read the live document and refresh the document, PNG and MW report. |
| `ep-craft figure export DOC [--out PATH] [--dpi N] [--background C] [--port 7797]` | Export the `Figure` artboard to PNG (default `<DOC stem>.png`; `--out` must end in `.png`; refuses to overwrite the document; checks run before VectorCraft is contacted). |
| `ep-craft crop probe IMAGE ...` | Probe the photo without writing files or launching the app. |
| `ep-craft crop sync DOC ...` | Re-crop the gel face from a moved Crop frame, then refresh everything. |
| `ep-craft workbench edit DOC ...` | Same as `figure edit`. |
| `ep-craft workbench sync DOC ...` | Same as `figure sync`. |
| `ep-craft workbench export DOC [--out PATH]` | Dump lane geometry, window, marker bands, trim and lane names as JSON (stdout when `--out` is omitted). |
| `ep-craft workbench import DOC --input PATH [--no-sync]` | Validate and apply an exported lane-data JSON to the live document; sync unless `--no-sync`. |
| `ep-craft profile sync DOC ...` | Recompute the Profiles canvas (shared sync handler). |
| `ep-craft doctor` / `ep-craft selftest` | Environment check / offline self-test. |

`sync` accepts `--port 7797`, `--dpi 300`, `--dry-run` and `--reset-peaks [N ...]`; `render` keeps all its detection and layout flags (see `SKILL.md`). Exit code `0` = success, `2` = expected user/environment error (message on stderr, no traceback).

## Install

Requirements: macOS, `python3` (stdlib only), [ImageMagick 7](https://imagemagick.org) (`brew install imagemagick`), and [VectorCraft](https://github.com/storytold/vectorcraft) 0.14.x (the skill launches it with `--control`).

Clone the repository and symlink the skill into your agent's skills directory:

```bash
git clone git@github.com:flmaximwang/EPCraft.git
ln -s "$PWD/EPCraft/skills/ep-craft" ~/.claude/skills/ep-craft
```

The skill directory now holds documentation only. The CLI lives in the repository-root package `src/ep_craft/` and is always run from an installed tool or through the repo's `src/`.

Install the CLI as a [uv tool](https://docs.astral.sh/uv/) (the package is not on PyPI yet, so install from the checkout or the git URL):

```bash
uv tool install "git+https://github.com/flmaximwang/EPCraft.git"  # add @main or a release tag to pin
uv tool install /path/to/EPCraft                                  # or from the local checkout root
ep-craft doctor
```

For development, install the checkout editable so edits under `src/ep_craft/` take effect without reinstalling:

```bash
uv tool install -e /path/to/EPCraft
```

To run without installing, use the package from the repo root:

```bash
PYTHONPATH=src python3 -m ep_craft doctor
```

`uv tool install` puts one executable on your PATH (usually `~/.local/bin`; run `uv tool update-shell` if it is missing). The package is stdlib-only; Python 3.9+ is required, and ImageMagick 7 and VectorCraft remain external system dependencies. Runtime modules live in the `ep_craft` package; presets and templates ship as package data, so locate them with `$(uv tool dir)/ep-craft/bin/python -c "import ep_craft; print(ep_craft.data_dir())"` (a checkout's `src/ep_craft/data/` remains the simplest source). Remove the tool with `uv tool uninstall ep-craft`.

## Repository layout

| Path | Content |
|---|---|
| [`skills/ep-craft/`](skills/ep-craft/) | The skill documentation: `SKILL.md`, `references/` (no runnable scripts — the CLI is installed from `src/`) |
| [`src/ep_craft/`](src/ep_craft/) | Installable package: runtime modules plus bundled `data/`, `templates/`, `fixtures/` |
| [`pyproject.toml`](pyproject.toml) | Packaging: console script `ep-craft`, package data, setuptools build |
| [`specs/`](specs/) | Design specs (VectorCraft output, editable output, gel prior) |
| [`tickets/`](tickets/) | Work breakdown for the VectorCraft output |

## Acknowledgements and citing

EPCraft would not exist without **VectorCraft**. It provides the document format, the editing GUI, the rendering and the control port that this skill drives.

- **VectorCraft**, the VectorCraft authors, <https://github.com/storytold/vectorcraft>. Dual-licensed MIT OR Apache-2.0. EPCraft does not bundle VectorCraft; it launches the installed app and talks to it through its `--control` TCP port. The port protocol is documented upstream in [`docs/control-protocol.md`](https://github.com/storytold/vectorcraft/blob/main/docs/control-protocol.md), and the file format and commands this skill relies on are summarized in [`references/vectorcraft.md`](skills/ep-craft/references/vectorcraft.md).

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
