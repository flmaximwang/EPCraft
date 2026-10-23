# EPCraft

An agent skill for **electrophoresis gel figures** (SDS-PAGE, agarose): turn a phone photo of a gel into a labeled, true-size figure, built on [VectorCraft](https://github.com/storytold/vectorcraft).

> **EP** = electrophoresis, **Craft** = [VectorCraft](https://github.com/storytold/vectorcraft). EPCraft is not part of VectorCraft and is not endorsed by its authors.

## What it does

The skill is `ep-craft`. Detection makes a first guess, you correct it by dragging lines in VectorCraft, and the agent re-reads your lines and updates everything in place.

1. **Render**: crop, straighten and rescale the gel photo to its real physical size. Seed lanes from the wells and marker lines from the ladder. Write an editable VectorCraft document.
2. **Edit by hand** on the *Workbench* artboard: drag lane separators, band lines and the top/bottom window. Rename or restyle labels on the *Figure*.
3. **Sync**: the agent reads the live document, recomputes profiles, bands and molecular weights, updates the Figure and re-exports. Running it twice changes nothing.

Output (three files, same stem):

| File | Content |
|---|---|
| `fig.vectorcraft` | Editable document: artboards `Figure` (exported), `Workbench` (edit here), `Profiles` (curves) |
| `fig.png` | The Figure artboard, exported by VectorCraft at the chosen dpi |
| `fig.mw.json` | Calibration points, raw per-lane intensity profiles, called bands with MW (two-point interpolation between neighbouring marker bands) |

Built-in data: three epizyme (雅酶) pre-stained marker presets (WJ101, WJ103 in Tris-Glycine and Tris-Tricine) and a `mini-gel` size preset. See [`skills/ep-craft/SKILL.md`](skills/ep-craft/SKILL.md) for the full workflow, checkpoints and flags.

## Install

Requirements: macOS, `python3` (stdlib only), [ImageMagick 7](https://imagemagick.org) (`brew install imagemagick`), and [VectorCraft](https://github.com/storytold/vectorcraft) 0.14.x (the skill launches it with `--control`).

Copy or symlink the skill into your agent's skills directory:

```bash
git clone git@github.com:flmaximwang/EPCraft.git
ln -s "$PWD/EPCraft/skills/ep-craft" ~/.claude/skills/ep-craft
```

Then check the environment:

```bash
python3 skills/ep-craft/scripts/gel_photo.py doctor
```

## Repository layout

| Path | Content |
|---|---|
| [`skills/ep-craft/`](skills/ep-craft/) | The skill: `SKILL.md`, `scripts/`, `data/` (marker and gel presets), `templates/`, `references/` |
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
