---
name: tableau-migration
description: >
  Port Tableau workbooks and data sources to Power BI semantic models and reports on
  Microsoft Fabric. Converts .tds / .twb / .twbx / .tdsx into a PBIP project (TMDL semantic
  model + PBIR report), deterministically translating Tableau calculations to DAX and
  connection metadata to Power Query M. Provides an exhaustive construct map: arithmetic /
  logical / string / date / aggregation calcs to DAX, LOD FIXED to calculated columns or
  measures, parameters to what-if tables and native field parameters, and foldable custom SQL
  to native queries. Covers storage-mode selection (Import, DirectQuery, DirectLake) so the
  customer chooses whether data lands in OneLake, and emits a migration report that lists every
  construct it could NOT translate as a labeled, per-item worklist (warn, never wrong). Use when
  the user wants to: (1) migrate Tableau workbooks or extracts to Power BI / Fabric, (2) convert
  a .tds / .twb / .twbx / .tdsx to a semantic model, (3) translate Tableau calculations or LOD
  expressions to DAX, (4) plan a large Tableau estate migration.
  Triggers: "migrate from tableau", "tableau to power bi", "tableau to fabric",
  "tds to semantic model", "twbx to power bi", "tableau extract to import", "tableau calc to dax",
  "lod expression to dax", "tableau custom sql to power query", "tableau estate migration",
  "convert tableau workbook", "tableau directlake".
---

> **Update Check — ONCE PER SESSION (mandatory)**
> The first time this skill is used in a session, run the **check-updates** skill before proceeding.
> - **GitHub Copilot CLI / VS Code**: invoke the `check-updates` skill.
> - **Claude Code / Cowork / Cursor / Windsurf / Codex**: compare local vs remote package.json version.
> - Skip if the check was already performed earlier in this session.

> **CRITICAL NOTES**
> 1. **Warn, never wrong.** Every construct that cannot be translated deterministically is emitted
>    as an inert, labeled stub with the original Tableau formula attached — never a guessed value that
>    ships. A red definition-of-done gate is the tool being honest, not broken.
> 2. **Storage mode is the customer's choice, never guessed.** `Import` and `DirectQuery` keep the
>    model bound to the source with **no data moved to OneLake**; `DirectLake` is opt-in. Do not
>    auto-land customer data in OneLake.
> 3. **The migration report is the deliverable, not just the model.** It lists every calc / LOD /
>    custom-SQL / visual that needs a human, with the reason. Always surface it to the user.
> 4. Read the source as **XML** (`.tds` / `.twb`) or the packaged **zip** (`.tdsx` / `.twbx`). The
>    XML schema is stable across most Tableau versions; parse defensively (check known attribute
>    spellings newest-first) rather than pinning to one Tableau version.
> 5. Reading packaged extract data (`.hyper` inside `.twbx` / `.tdsx`) needs the optional
>    `tableauhyperapi` native dependency, which is **x64-only** — run large estates on an x64 box or
>    CI runner, otherwise packaged-data workbooks warn-and-skip.
> 6. **Never read credentials** from a `.tds`. Connection descriptors are serialized into the
>    migration report; usernames / passwords / tokens must never be lifted or emitted.

# Tableau → Microsoft Fabric / Power BI Migration

Convert Tableau content into a Power BI **PBIP** project — a TMDL **semantic model** plus a PBIR
**report** — that opens in Power BI Desktop and publishes into Fabric. The engine does the mechanical
majority deterministically and hands back a precise worklist for the tail it refuses to guess.

## Table of Contents

| Topic | Reference |
|---|---|
| Migration Workload Map | [§ Migration Workload Map](#migration-workload-map) |
| Calculation & LOD → DAX | [construct-mapping.md](resources/construct-mapping.md) |
| Storage modes (Import / DirectQuery / DirectLake) | [§ Storage Modes](#storage-modes--who-holds-the-data) |
| The migration report (worklist) | [§ The Migration Report](#the-migration-report--what-it-couldnt-do) |
| Must / Prefer / Avoid | [§ Must / Prefer / Avoid](#must--prefer--avoid) |
| Authentication & Token Acquisition | [COMMON-CORE.md § Authentication](../../common/COMMON-CORE.md#authentication--token-acquisition) |
| Semantic model authoring | [semantic-model-authoring](../semantic-model-authoring/SKILL.md) |
| Power BI report authoring (PBIR/PBIP) | [powerbi-report-authoring](../powerbi-report-authoring/SKILL.md) |

---

## Migration Workload Map

| Tableau Component | Fabric / Power BI Target | Notes |
|---|---|---|
| **Workbook** (`.twb` / `.twbx`) | **PBIP project** (semantic model + report) | Opens in Power BI Desktop; publishes as Fabric items |
| **Data source** (`.tds` / `.tdsx`) | **Semantic model** (TMDL) with M partitions | Connection metadata → Power Query M |
| **Extract** (`.hyper`) | **Import** mode table (VertiPaq) | Data cached in the model; source hit only at refresh |
| **Live relational connection** (SQL Server, Snowflake, Postgres, Databricks, …) | **DirectQuery** or **DirectLake** | Customer chooses; see [§ Storage Modes](#storage-modes--who-holds-the-data) |
| **Calculations** (arithmetic, logical, string, date, standard aggregations) | **DAX** measures/columns | Deterministic; original formula kept as a `TableauFormula` annotation; safe division → `DIVIDE()` |
| **Table calcs** (`RUNNING_SUM`, `WINDOW_*`, rank, nested/argmax) | **Flagged stub** | Inert, labeled TODO with the original formula — never a wrong number |
| **LOD — `FIXED`** over a real/derived grain | **DAX** (calculated column, tractable cases) | Detected and bound to a real column |
| **LOD — `INCLUDE` / `EXCLUDE` / nested** | **Flagged handoff** | No clean 1:1 DAX; handed off, not force-fit |
| **Parameters — value / what-if** | **What-if table** + `[<Param> Value]` measure | Rebuilt natively |
| **Parameters — field / measure swap** | **Power BI field parameters** | Rebuilt natively |
| **Parameters — plain filter** | **Flagged for review** | A Tableau filter card ≠ a Power BI slicer |
| **Custom SQL** (foldable) | **Native query** in M | De-escaped; parameter references extracted |
| **Custom SQL** (unfoldable cross-engine joins/unions, unknown connector) | **Flagged for review** | Reported, not dropped, so a human rebinds it |
| **Worksheets** | **Report visuals** | Rebuilt as native, live Power BI visuals |
| **Dashboards** | **Report pages** | Layout approximated; complex vizzes want a visual-QA pass |

---

## Storage Modes — who holds the data

The customer picks the storage mode; the tool **never guesses it and never auto-lands data in
OneLake**. Present these three explicitly:

| Mode | Data location | Serving compute | Source compute | Moves data to OneLake? |
|---|---|---|---|---|
| **Import** | Cached in the model (OneLake-backed) | Fabric capacity | Source only at refresh | No (staged in the model, not a lake copy) |
| **DirectQuery** | Stays in the source | Mostly the source (per interaction) | Every query is live | No |
| **DirectLake** | Delta in OneLake | Fabric capacity | Landing / mirror to OneLake | Yes (opt-in) |

- **Default** derives a source-bound mode automatically: an extract → **Import**; a live relational
  source → **DirectQuery**. Flat files and driver-only ODBC → **Import**.
- **DirectLake is never auto-selected** — it is an explicit opt-in that lands data as Delta in OneLake.
- If the customer requests a mode that is infeasible for the source (e.g. DirectQuery on a flat file
  or offline extract), keep the safe derived mode and **flag it loudly** — never emit a broken model.

See [construct-mapping.md § Storage modes](resources/construct-mapping.md#storage-modes) for the
connector-by-connector detail.

---

## The Migration Report — what it *couldn't* do

Every run emits a migration report (HTML, offline-openable, plus machine-readable JSON) whose job is to
hand back the tail the engine refuses to guess as a **precise, labeled worklist**:

- **Coverage scoreboard** — per workbook: *"X of Y calcs translated · N% coverage · M need review."*
- **"Needs review" worklist** — every un-translatable calc with its **name, original Tableau formula,
  category** (LOD / table calc / unsupported function) and the **concrete reason**.
- **Manual follow-ups** — per data source: configure credentials in Fabric, set up a gateway for an
  on-prem source, review preserved custom SQL before refresh, flat-file source binding, storage-mode
  confirmations.
- **Visual fidelity punch-list** — which visuals matched cleanly and which want hand-finishing.
- **Honest status stamp** — each workbook is `migrated` or `migrated_with_followups`; the run cannot
  report a clean migration when follow-ups exist.

At estate scale, *"here is exactly what a human still needs to finish, and why"* is worth more than the
raw conversion percentage. Always show the user this worklist — it is the plan for the remaining work.

---

## Must / Prefer / Avoid

### MUST DO
- **Parse the source defensively.** Read `.tds` / `.twb` as XML and `.tdsx` / `.twbx` as a zip;
  check known attribute spellings newest-first so a real file resolves regardless of Tableau version.
- **Preserve every original Tableau formula** as a `TableauFormula` annotation on the emitted DAX, so a
  reviewer can always see the source of truth.
- **Emit an inert, labeled stub** for any construct that cannot be translated deterministically
  (table calcs, INCLUDE/EXCLUDE/nested LODs, unfoldable custom SQL) — never a guessed value.
- **Let the customer choose the storage mode** (Import / DirectQuery / DirectLake); default to a
  source-bound derivation and treat DirectLake as an explicit opt-in.
- **Produce and surface the migration report** — the flagged worklist is the deliverable, not an
  afterthought.
- **Handle secrets strictly** — never read usernames / passwords / tokens from a `.tds`; scrub
  credential-bearing keys before emitting any connection descriptor.

### PREFER
- **Import** for extracts and small/medium data that can be cached — source is hit only at refresh.
- **DirectQuery** when the customer must keep data in place and query it live at the source.
- **DirectLake** as the Fabric-native end-state when the customer opts to land data as Delta in OneLake.
- **Native Power BI field parameters** for Tableau field/measure-swap parameters, and **what-if tables**
  for value parameters.
- **`DIVIDE()`** over a naive `/` when translating ratios, to preserve Tableau's safe-division behavior.
- **An x64 runner** for large estates, so packaged `.twbx` / `.tdsx` extract data reads successfully.

### AVOID
- **Do not auto-land customer data in OneLake** — DirectLake is opt-in; Import/DirectQuery move no data.
- **Do not guess a measure** when no clean DAX equivalent exists — flag it for review instead.
- **Do not pin parsing to a single Tableau version** — parse the stable XML schema with attribute
  fallbacks; exotic version-specific constructs should flag for review, not break the run.
- **Do not read or emit credentials** from a `.tds` connection.
- **Do not report a clean migration** when manual follow-ups remain — stamp `migrated_with_followups`.
- **Do not treat a red definition-of-done gate as a failure** — it means the tool refused to ship an
  unproven binding.

---

## Examples

**Extract-backed workbook → Import semantic model**

```
# A .twbx whose data source is a .hyper extract becomes an Import-mode model:
#   source hit only at refresh; interactive serving runs on Fabric capacity.
```

**Live Snowflake source → customer picks the mode**

```
# Live Snowflake connection. Default derives DirectQuery (query stays on Snowflake).
# The customer can opt into DirectLake to land the data as Delta in OneLake instead —
# an explicit choice, never automatic.
```

**Tableau calc → DAX (auto), original preserved**

```
// Tableau:  [Profit] / [Sales]
// DAX (safe division, formula preserved as annotation):
Profit Ratio = DIVIDE ( SUM ( 'Orders'[Profit] ), SUM ( 'Orders'[Sales] ) )
```

**Table calc → flagged stub (not guessed)**

```
// Tableau:  RUNNING_SUM(SUM([Sales]))
// Emitted as an inert, labeled TODO with the original formula attached — appears in the
// migration report's "needs review" worklist for a human to finish.
```
