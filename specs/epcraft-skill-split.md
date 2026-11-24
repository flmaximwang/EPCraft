# Spec: epcraft — split the combined skill into labeling and quantification

Status: implemented, 2026-11-24 (ticket `ep-yl2.2`; packaging/rename in `ep-yl2.1`; integrated acceptance in `ep-yl2.3`). Supersedes the single-skill arrangement of `skills/ep-craft/` and the optional densitometry section of that skill. Historical design records (`specs/ep-craft-densitometry.md` and the other `ep-craft-*` specs) are unchanged.

## 0. Why

The combined skill did two jobs behind one description: it labeled a gel photo (crop, lanes, bands, VectorCraft document, MW report) and it could optionally quantify protein bands (areas, standard curve, masses). The two jobs have different inputs, different failure modes and different knowledge:

- Labeling needs the photo, VectorCraft and the detection/layout rules; its report `fig.mw.json` is the deliverable.
- Quantification needs **only** an existing report plus the masses loaded in the standard lanes; it never opens VectorCraft and can refuse for reasons (out of range, non-monotonic standards) that mean nothing while labeling.

A combined description makes routing noisy: "how much is in this band" competes with "label this gel", and a user who only wants masses has to read about artboards and crops. Splitting the manual also stops the densitometry rules (which assume an already-finished report) from crowding the labeling checkpoints. The split is a documentation and routing change; it alters no pipeline step and no data contract. The one runtime change around it is the companion package/entry-point rename in `ep-yl2.1` (same code, new `epcraft` import and executable), which this ticket only documents.

## 1. Goal

- Two skills with distinct frontmatter names and descriptions:
  - `skills/epcraft-label/` — label an electrophoresis gel photo and write `fig.vectorcraft`, `fig.png`, `fig.mw.json`.
  - `skills/epcraft-quantify/` — read a labeling report (`fig.mw.json` v2, or a `.vectorcraft` path that resolves to its sibling) and write `fig.dens.json`, `fig.curve.json`, `fig.quant.json`, `fig.bands.png`, `fig.curve.png`.
- Both use the **same** `epcraft` distribution, the `epcraft` import package and the single `epcraft` executable (`python -m epcraft` when not installed). Neither skill ships code, its own entry point or a second executable.
- The inter-skill handoff is the existing **`fig.mw.json` v2** report. `measure` may also be given the `.vectorcraft` document and resolves its sibling report; after that the chain consumes its own `fig.dens.json` / `fig.curve.json` artifacts. No other initial input is accepted.
- The old combined skill is retired: no active `skills/ep-craft/` directory and no `ep-craft` skill routing remain; the README migration note may still name it historically.
- Existing outputs, JSON kinds and schemas, document metadata and historical specs remain valid.

## 2. Decisions

1. **Two skills, one per job.** `epcraft-label` owns crop → lanes → bands → document/PNG/report. `epcraft-quantify` owns measure → curve → quantify → plots. A task that needs both is a labeling task followed by a quantification task, in that order.
2. **One runtime, installed once.** The executable/import rename to `epcraft` belongs to `ep-yl2.1`; the skills only document it. `uv tool install .` (or `PYTHONPATH=src python3 -m epcraft`) provides every command. Quantification does not require running the labeling skill, only a report file that already exists.
3. **`fig.mw.json` v2 is the inter-skill contract, unchanged.** Quantification validates `version: 2`, a non-empty `lanes` list, equal-length `profile.y_cm` / `gray` / `baseline` arrays, strictly increasing `y_cm`, and `gel` / `window` objects. A `.vectorcraft` argument resolves to its sibling `<stem>.mw.json`; a report older than the document is refused (`先 sync`) because the document is the truth. No new fields, no version bump. The report is the only thing labeling hands over: later steps read the chain's own `fig.dens.json` / `fig.curve.json`, which are derived artifacts rather than a second input format.
4. **Reference split follows the job.**
   - Label keeps `photo-labeling-conventions.md` §1–§13 (markers, gel preset, stages, document layout, Workbench, lane seeding, profiles/bands/calibration, layout, sync, commands, marker confidence, module map, Workbench JSON), plus `special-cases-and-pitfalls.md` and `vectorcraft.md`.
   - Quantify gets `references/densitometry.md`, a standalone manual: input contract, baselines, windows/units/limits, plots, fits/`--std`/replicates/drops, out-of-range and non-positive masses, reading `quant.json` (three distinct percentages), exact JSON keys, decisions/limits and modules.
   - The historical design rationale stays in `specs/ep-craft-densitometry.md`; the new reference is the operating manual, not a second design record.
5. **Routing probes are per skill and include cross-negatives.** Each `test-prompts.json` keeps its existing shape (`skill`, `version`, `test_cases` with `id` / `type` / `prompt` / `expected_behavior` / `notes`). Label has positive labeling cases plus a quantification-only negative; quantify has positive quantification cases plus labeling and protein-ID negatives, and edge cases for agarose, PNG-only input, stale reports and extra wells. Label keeps its own quantification cross-negative (case `should-not-trigger-35`); quantify's moved "quantify from standard lanes" case is a separate positive (`should-trigger-01`), the same scenario seen from the quantification side, so the two probes test different skills rather than duplicating one expectation.
6. **Naming follows the runtime for prose and data names for compatibility.** Skill docs use `epcraft` for the executable, import package, paths (`src/epcraft/…`) and console invocations. Persisted identifiers keep their historical spelling so old files keep working:
   - JSON kinds `ep-craft-densitometry`, `ep-craft-densitometry-curve`, `ep-craft-densitometry-quant`;
   - the Workbench exchange schema `ep-craft-workbench-lanes`;
   - historical spec/ticket filenames.
   Runtime-visible undo labels (`epcraft sync`, `epcraft edit`, `epcraft workbench import`) follow the shared runtime's `epcraft` spelling (`ep-yl2.1`); they are labels, not persisted data, so documents written before the split are unaffected. Persisted metadata keeps its literal too: `ep-meta.skill` stays `"ep-craft"` (it names the data producer, not the executable, and is never migrated); the other `ep-meta` keys are unchanged.
7. **Retire, do not alias.** `skills/ep-craft/` is removed with `git mv` (history preserved). There is no compatibility `ep-craft` skill and no third "combined" skill: a ghost skill that still matches old routing would defeat the split; nor is there a second executable, `ep-craft` script or `python -m ep_craft` entry point — `epcraft` is the only one. Users symlink the two new directories; the runtime preserves the subcommand names (`figure`, `crop`, `workbench`, `profile`, `workflow`, `densitometry`, `doctor`, `selftest`) under that executable.
8. **Label outputs are read-only to quantification.** Labeling owns and writes its three files (`fig.vectorcraft`, `fig.png`, `fig.mw.json`), including on `render` / `sync`; quantification writes only its five siblings and refuses to rewrite/refit what labeling produced. A stale report is fixed by `epcraft-label`'s `sync`, not by hand-editing JSON.

## 3. Resulting layout

```text
skills/
  epcraft-label/
    SKILL.md
    test-prompts.json
    references/
      photo-labeling-conventions.md
      special-cases-and-pitfalls.md
      vectorcraft.md
  epcraft-quantify/
    SKILL.md
    test-prompts.json
    references/
      densitometry.md
specs/epcraft-skill-split.md
```

`README.md` describes the two skills, the shared `epcraft` CLI, installs both symlinks, and links both `SKILL.md` files and the label references. `src/epcraft/` and `pyproject.toml` are the single distribution; the package rename is tracked by `ep-yl2.1`, and no source file is touched by this ticket.

## 4. Compatibility

- **Outputs:** `fig.mw.json` stays v2 with the same keys; `fig.dens.json` / `fig.curve.json` / `fig.quant.json` keep kinds and schema version 1. Existing quantification outputs remain readable by the same commands.
- **Document metadata:** artboards, layers, lane/marker objects and `ep-meta` keys are untouched; the persisted `ep-meta.skill` literal remains `"ep-craft"`, so `sync` continues to read documents written before the split.
- **Historical specs:** all `specs/ep-craft-*.md` files keep their names and contents; this spec is additive. Tickets keep their `ep-craft` titles.
- **Commands:** the runtime keeps the commands the old skill documented (`figure`, `crop`, `workbench`, `profile`, `workflow`, `densitometry`, `doctor`, `selftest`); only the invocation name changes to `epcraft` per `ep-yl2.1`.

## 5. Verification

Performed in the `refactor/epcraft-skills` worktree:

- Frontmatter parsed with a real YAML parser (Ruby Psych 3.1.0): both names and folded descriptions load; the old colon-in-plain-scalar problem is gone.
- Both `test-prompts.json` files parse; label has no densitometry positive, quantify's moved case and cross-negatives are present.
- Every relative link in both `SKILL.md` files, their references and `README.md` resolves inside the worktree; the `src/epcraft/` layout entry is plain text because the package rename lands in `ep-yl2.1`.
- `grep` finds no active reference to the old `skills/ep-craft/` directory or the `ep-craft` skill name outside the README migration note and historical specs/tickets; persisted identifiers (`ep-craft-densitometry*`, `ep-craft-workbench-lanes`, `ep-meta.skill`) and historical spec names are intentionally unchanged.

The integrated acceptance (clean-env install, single `epcraft` entry point, full `selftest` + `doctor`, both frontmatter names, positive/negative routing probes, local links, retired old skill, `fig.mw.json` v2 handoff, historical spec contents, JSON kinds/schemas and `ep-meta` compatibility, main still `a6c230a`) is `ep-yl2.3`'s job; this ticket stops at the documentation and probes.
