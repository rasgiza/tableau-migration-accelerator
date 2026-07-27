# Add `tableau-migration` skill (Tableau → Power BI / Fabric)

Resolves the proposal in #67.

## What this adds

A new **`skills/tableau-migration/`** skill, alongside the existing `databricks-migration`,
`synapse-migration`, `hdinsight-migration`, and `pipeline-migration` skills. It provides guidance for
converting Tableau content into a Power BI **PBIP** project — a TMDL **semantic model** plus a PBIR
**report** — that opens in Power BI Desktop and publishes into Fabric.

```
skills/tableau-migration/
  SKILL.md
  resources/construct-mapping.md
```

## Coverage

- **Sources:** `.tds` / `.twb` / `.twbx` / `.tdsx` (XML datasources/workbooks and packaged zips);
  version-tolerant parsing (known attribute spellings checked newest-first).
- **Calculations → DAX:** arithmetic / logical / string / date / aggregation translated
  deterministically, original Tableau formula preserved as an annotation; `RUNNING_SUM` / `WINDOW_*` /
  nested table calcs flagged for review rather than guessed.
- **LOD expressions:** `FIXED` over a real grain translated; `INCLUDE` / `EXCLUDE` / nested handed off.
- **Parameters:** value/what-if → what-if table + measure; field/measure swap → native Power BI field
  parameters; plain filters flagged.
- **Custom SQL:** foldable → native query; unfoldable → flagged for review.
- **Storage modes:** Import / DirectQuery / DirectLake — the customer chooses whether data lands in
  OneLake (DirectLake opt-in; Import/DirectQuery move no data).
- **Migration report:** the un-translatable tail is emitted as a labeled, per-item worklist
  ("warn, never wrong").

## Conventions followed

- Modeled on `databricks-migration`: frontmatter (`name` + `description` with triggers), the mandatory
  update-check block, critical notes, workload-map tables, MUST/PREFER/AVOID, and examples.
- References sibling skills (`semantic-model-authoring`, `powerbi-report-authoring`) and
  `common/COMMON-CORE.md`.
- **Guidance-only** — links a working MIT reference implementation rather than vendoring any engine
  code into this repo.

## Notes

- No runtime code, dependencies, or workflows changed — this is a self-contained skill folder.
- Happy to adjust naming, scope, or structure to match maintainer preferences.
