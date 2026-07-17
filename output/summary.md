# Tableau -> Fabric Estate Migration Report

_Generated 2026-07-17T19:11:50Z by `migrate_estate` from LocalFilesSource._

## ⛔ DEFINITION OF DONE: FAILED

1 of 1 workbook input(s) produced no openable, model-bound Power BI report. A Tableau workbook migration is not complete until its dashboards are rebuilt and bound into a `.pbip` (see the Workbooks table below).

- **Superstore** -- embedded datasource 'Superstore' needs a storage decision (Connector class 'unknown' is not mapped for direct M; storage decision required -- default to a direct-to-source Import rebuild, or opt in to land-to-Delta + DirectLake (never auto-selected).) -- workbook .pbip skipped (model lands separately)

## Summary

- **Datasources:** 1 total -> 1 migrated (0 need manual follow-ups), 0 fallback, 0 error
- **Tables:** 2 | **Columns:** 5
- **Measures:** 3 total -> 2 translated, 1 stubbed
- **Calc columns:** 0 total -> 0 translated, 0 stubbed
- **Storage modes:** Import 0, DirectQuery 1, fallback 0
- **Connectors seen:** sqlserver
- **Workbooks:** 1 total -> 1 viz built, 0 warned, 0 error (viz stage available)

## Datasources

| Datasource | Status | Mode | Tables | Columns | Measures (tr/stub) | Output |
|---|---|---|---|---|---|---|
| Superstore | migrated | DirectQuery | 2 | 5 | 2/1 | semantic_models/Superstore.SemanticModel |

> **Open locally:** each migrated datasource also has an openable Power BI project at `pbip/<Name>/<Name>.pbip` — double-click to explore and test it in Power BI Desktop.

## Next step — second compiler (optional; offer to run)

1 calculation(s) fell back to inert stubs (the original Tableau formula is preserved). The second compiler is an **opt-in** stage: offer it to the user, then run it only on an explicit GO. If they decline, this deterministic result ships as-is. Once authorized: for each calc author a candidate DAX, validate it with `check_candidate_dax` (and the reconciliation oracle when data is landed), then land every validated candidate via `approved_calc_dax` and redeploy. Anything with no faithful DAX form stays an inert stub. See [second-compiler.md](resources/second-compiler.md).

| Datasource | Calculation | Role | Category | Fallback reason | Suggestion ready |
|---|---|---|---|---|---|
| Superstore | Running Sales | measure | missing_addressing_intent | unsupported function RUNNING_SUM | no |

## Workbooks

| Workbook | Viz | Visuals (rebuilt/warned) | Project (.pbip) | Bound model | Note |
|---|---|---|---|---|---|
| Superstore | built | 3/0 | - | - |  |

## Audit guarantees

- Column types come from the Tableau source schema, never inferred.
- Every calculated field's original formula is preserved as a `TableauFormula` annotation; translated measures carry `TranslatedBy`, stubs stay inert `= 0`.
- Fallback datasources are listed with a reason; nothing is emitted wrong silently.
- No credentials are read, stored, or written anywhere in this bundle.
