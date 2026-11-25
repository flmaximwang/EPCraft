# EPCraft Documentation & Long-Term Specs

- **Long-term design specs** live in [`docs/specs/`](specs/) (ordered below by pipeline / dependency order).
- **Superseded historical specs** live in [`docs/specs/archive/`](specs/archive/).
- **Task tickets and temporary implementation specs** are tracked in **beads** (`bd list`, `bd show <id>`) rather than in repository markdown files.

## Active Specs

| Spec | Scope | Status | Role |
|---|---|---|---|
| [`gel-prior.md`](specs/gel-prior.md) | `src/epcraft` + `skills/epcraft-label` | Implemented | Mandatory gel preset prior & three-stage frozen pipeline |
| [`vectorcraft.md`](specs/vectorcraft.md) | `src/epcraft` + `skills/epcraft-label` | Implemented | VectorCraft editing surface, `.vectorcraft` document & `sync` |
| [`crop-artboard.md`](specs/crop-artboard.md) | `src/epcraft` + `skills/epcraft-label` | Implemented | `Crop` artboard & in-place re-crop |
| [`crop-frame-rotation.md`](specs/crop-frame-rotation.md) | `src/epcraft` + `skills/epcraft-label` | Implemented | `bbox_angle` representation for rotated crop frames |
| [`densitometry.md`](specs/densitometry.md) | `src/epcraft` + `skills/epcraft-quantify` | Implemented | Offline SDS-PAGE densitometry & quantification chain |

## Archive

| Spec | Status | Notes |
|---|---|---|
| [`archive/editable-output.md`](specs/archive/editable-output.md) | Superseded (2026-10-22) | Pre-V6 scene/HTML/SVG design; replaced by [`vectorcraft.md`](specs/vectorcraft.md) |

## Naming & Persisted Contracts

The package, CLI, and skills were renamed from `ep-craft` / `ep_craft` to `epcraft` (`epcraft-label`, `epcraft-quantify`). Spec filenames, headers, and command/module references in `docs/specs/` reflect current `epcraft` conventions, while **persisted on-disk identifiers** (`kind: "ep-craft-densitometry*"`, `schema: "ep-craft-workbench-lanes"`, `ep-meta.skill: "ep-craft"`, and the `"ep-craft sync"` undo group label) are intentionally preserved for backward compatibility.
