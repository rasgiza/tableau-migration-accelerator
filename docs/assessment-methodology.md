# Assessment Methodology — Sizing a Tableau Estate

How to turn "150+ workbooks, thousands of calculations" into a defensible scope and effort
estimate **before** committing to a migration. The output is a ranked backlog and an effort
model, produced by the accelerator's inventory/scan pass (`--scan`) plus the scoring rubric
below.

---

## 1. Inventory (what to collect)

Run the estate scan to produce a raw inventory. For each **workbook** and each
**datasource**, capture:

| Signal | Why it matters |
|---|---|
| Datasource count & sharing | Shared datasources are the reuse unit — migrate once, rebind many. |
| Connection type (live/extract, connector class, custom SQL) | Custom SQL and unusual connectors raise effort and need rebind. |
| Calculation count | Raw volume of logic to translate. |
| Calc types (aggregation / row-level / **LOD** / **table calc** / parameter) | Drives the complexity score — the tail dominates effort. |
| Parameters & actions | Map to field/what-if parameters; some are manual. |
| Worksheets, dashboards, viz types | Report-rebuild surface. |
| Certification & last-access / staleness | Retire, don't migrate, dead content. |

> The engine's `scan.json` gives you the machine-readable version of most of this. Enrich
> with usage/staleness from Tableau Server admin views (or the Tableau MCP inventory).

---

## 2. Complexity scoring

Score each **datasource** (the migration unit) 1–5. Workbook difficulty is derived from the
datasources it uses plus its viz/parameter surface.

### Calculation complexity (per datasource)

| Points | Calc profile |
|---|---|
| 1 | Only simple aggregations / arithmetic (`SUM`, `AVG`, ratios). Fully deterministic. |
| 2 | + row-level calculated fields, basic `IF`/`CASE`. |
| 3 | + a few **FIXED LOD** or straightforward parameters. |
| 4 | + INCLUDE/EXCLUDE LOD, multiple table calcs (`RUNNING_*`, `WINDOW_*`, `RANK`). |
| 5 | Heavy nested LOD + table calcs + custom SQL. Mostly manual DAX. |

### Structural modifiers (add to the base)

| +Points | Condition |
|---|---|
| +1 | Custom SQL / non-standard connector (rebind + M/SQL work). |
| +1 | Many-table model with non-obvious / missing join keys. |
| +1 | Parameters driving calc logic (not just display). |
| −1 | Datasource shared by ≥5 workbooks (amortize the cost). |

**Bucket:** score ≤2 → **Simple**, 3 → **Moderate**, ≥4 → **Complex**.

---

## 3. Effort model

Effort is driven by **distinct complex calcs**, not workbook count. Per-datasource estimate:

```
effort_hours ≈ base(bucket)
             + manual_calc_hours × distinct_complex_calcs
             + rebind_hours (if custom SQL / non-standard connector)
             + report_rebuild_hours × dashboards_needing_manual_layout
```

Suggested starting coefficients (calibrate after the pilot):

| Component | Simple | Moderate | Complex |
|---|---|---|---|
| `base(bucket)` | 2 h | 6 h | 16 h |
| `manual_calc_hours` (per distinct complex calc) | — | 0.5 h | 1.5 h |
| `rebind_hours` (if applicable) | 2 h | 4 h | 8 h |
| `report_rebuild_hours` (per dashboard needing manual layout) | 1 h | 2 h | 4 h |

What the **automation removes** from these numbers (proven in `../output/`): schema/type
rebuild, safe-subset calc→DAX, TMDL authoring, and PBIP scaffolding — i.e. the base is small
precisely because the mechanical work is generated.

### Worked example (illustrative, for the 150-workbook estate)

Assume the scan buckets the estate as:

| Bucket | Datasources | Avg distinct complex calcs | Avg dashboards |
|---|---|---|---|
| Simple | 30 | 0 | 1 |
| Moderate | 15 | 6 | 3 |
| Complex | 10 | 20 | 4 |

```
Simple  : 30 × (2 + 0        + 0 + 1×1)          = 30 × 3   = 90 h
Moderate: 15 × (6 + 0.5×6    + 4 + 2×3)          = 15 × 19  = 285 h
Complex : 10 × (16 + 1.5×20  + 8 + 4×4)          = 10 × 70  = 700 h
                                          Total  ≈ 1,075 h
```

Because ~55 datasources back 150 workbooks (heavy sharing), **workbook count overstates the
work** — the migrated semantic models are reused and reports are rebound. Report this ratio
explicitly; it is the strongest argument for a domain-by-domain approach.

> These coefficients are **placeholders to be recalibrated after the Revenue-Cycle pilot.**
> The pilot's actual auto-translation rate (our sample proof hit 2/3 measures) and the actual
> manual-calc hours become the real numbers for the full-estate estimate.

---

## 4. Prioritization

Rank datasources/domains for the backlog by:

1. **Business value** (Revenue Cycle first — the customer's chosen pilot).
2. **Reuse leverage** (shared datasources deliver the most workbooks per migration).
3. **Complexity** (start Simple/Moderate within the pilot to build momentum and calibrate).
4. **Staleness** (retire unused content — the cheapest "migration" is deletion).

---

## 5. Outputs of the assessment

- **Estate inventory** (`scan.json` + enrichment): counts, connection types, calc profiles.
- **Complexity-scored backlog**: every datasource bucketed Simple/Moderate/Complex.
- **Effort estimate**: hours by bucket, with the workbook-to-datasource reuse ratio called out.
- **Pilot scope**: the Revenue-Cycle datasources to migrate first, and the manual-calc list
  to resolve with Copilot.
- **Risk flags**: custom SQL, missing join keys, table-calc-heavy datasources,
  storage-mode decisions.

This assessment is the gate before any migration commitment — scope and cost are an *output*
of the data, not a guess.
