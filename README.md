# Tableau → Power BI / Microsoft Fabric Migration Accelerator

A proof-backed accelerator for migrating a Tableau estate to Power BI / Microsoft
Fabric semantic models. Built to answer a customer's core question honestly:

> *"Does Microsoft have a native strategy, accelerator, or recommended approach to
> parse Tableau TWB/TWBX files, extract calculations and lineage, and accelerate
> creation of Power BI / Fabric semantic models or PBIP projects?"*

Short answer: **there is no GA first-party one-click Tableau→Power BI converter.**
What exists is a repeatable, **evidence-backed accelerator** that automates the
mechanical 80% (schema, data types, safe-subset calc→DAX, TMDL, an openable PBIP)
and clearly flags the 20% that stays a human decision (complex LOD/table calcs,
ambiguous relationships, storage-mode choice, native-source rebind). This folder
proves that with a real offline run.

## Convert a Tableau report to a Power BI semantic model (one command)

This is the shareable tool. Point it at any Tableau file — get a Power BI/Fabric
semantic model + an openable PBIP back.

```powershell
# from the tableau-accelerator folder
.\scripts\Convert-TableauToPowerBI.ps1 -Source .\sample\Superstore.twb
```

Or against your own workbook / a whole folder of exports:

```powershell
.\scripts\Convert-TableauToPowerBI.ps1 -Source C:\exports\MyDashboard.twbx
.\scripts\Convert-TableauToPowerBI.ps1 -Source C:\exports\revenue-cycle -Output C:\out\rc
```

What it does, in order:
1. Stages the Tableau file(s) — a single `.twb` also pulls in its sibling `.tds`
   datasource so calculations resolve.
2. **Scans** datasource bindings and flags any *published* datasource that must be
   fetched first (won't silently produce a partial model).
3. **Builds** the typed **TMDL** semantic model, translates the safe subset of
   calculations to **DAX** (originals preserved), and emits an openable **`.pbip`**.
4. Copies the bundle to `-Output` (default `.\output`) and prints a summary +
   the exact `.pbip` path to open in Power BI Desktop.

Requirements: Windows PowerShell + **Python 3.11+** (the script auto-detects
`py -3.11` / `python`). No live Tableau, no Tableau Desktop, no internet.

## What's here


| Path | What it is |
|---|---|
| `engine/` | Cloned [`tableau-fabric-skills`](https://github.com/Yarbrdab000/tableau-fabric-skills) — the community/field migration engine (the `tableau-migration` skill is the workhorse). |
| `sample/` | `Superstore.tds` + `Superstore.twb` — a real-shaped sample datasource + workbook (offline; no live Tableau needed). |
| `scripts/Convert-TableauToPowerBI.ps1` | **The shareable tool** — one-command wrapper over the engine. |
| `output/` | **The proof.** The actual generated bundle from a run: TMDL semantic model, calc→DAX measures, and an openable `.pbip`. |
| `docs/customer-response.md` | Honest answers to the customer's 5 questions. |
| `docs/architecture.md` | Reference architecture + the two migration motions + phased Revenue-Cycle-first plan. |
| `docs/assessment-methodology.md` | How to size a 150-workbook estate and estimate effort. |

## The offline proof (what actually ran)

The engine parsed a Superstore datasource + workbook **entirely offline** and produced:

- **1 semantic model** (`output/semantic_models/Superstore.SemanticModel`) as typed TMDL
  — column types taken from the Tableau schema, never inferred.
- **2 of 3 calculations auto-translated to DAX**, deterministically:
  - `Total Sales`: `SUM([Sales])` → `SUM('Orders'[Sales_Amount])`
  - `Profit Ratio`: `SUM([Profit]) / SUM([Sales])` → `DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales_Amount]))`
    *(note the engine chose `DIVIDE` for safe division — not a naive `/`)*
  - `Running Sales`: `RUNNING_SUM(SUM([Sales]))` → **left as an inert stub**, original
    formula preserved as a `TableauFormula` annotation (table calcs are a manual step).
- **An openable `.pbip`** project (`output/pbip/Superstore/Superstore.pbip`).
- **A definition-of-done gate that failed loud** on the workbook report binding because
  the engine **refuses to auto-pick** a storage mode (Import vs. DirectLake) — exactly the
  kind of honest, human-in-the-loop behavior you want when telling a customer what is and
  isn't automated.

This is the honest headline: **the boring, error-prone 80% is automated and auditable;
the judgement 20% is surfaced, never silently guessed.**

## Reproduce the run

Just run the tool (see the one-command section above):

```powershell
.\scripts\Convert-TableauToPowerBI.ps1 -Source .\sample\Superstore.twb
```

Open `output\pbip\Superstore\Superstore.pbip` in Power BI Desktop to validate visually.

<details>
<summary>Advanced: call the engine directly</summary>

```powershell
$SKILL = "$PWD\engine\skills\tableau-migration"
$RUN   = (py -3.11 "$SKILL\scripts\new_run.py" --root C:\tfmig)   # mints a clean run folder
Copy-Item .\sample\Superstore.tds, .\sample\Superstore.twb (Join-Path $RUN 'in') -Force
py -3.11 "$SKILL\scripts\migrate_estate.py" -i (Join-Path $RUN 'in') -o (Join-Path $RUN 'out') --scan   # gate
py -3.11 "$SKILL\scripts\migrate_estate.py" -i (Join-Path $RUN 'in') -o (Join-Path $RUN 'out')          # build
```
</details>

## Recreate the sample (optional)

The sample is materialized from the engine's own synthetic fixtures (a real-shaped
Superstore datasource + workbook), so no Tableau Desktop or Tableau Public download is
required:

```powershell
$fix = "$PWD\engine\skills\tableau-migration\tests\integration"
py -3.11 -c "import sys; sys.path.insert(0, r'$fix'); import fixtures; fixtures.materialize_superstore(r'$PWD\sample')"
```

To run against a **real** workbook instead, drop any `.twb`/`.twbx` (or `.tds`/`.tdsx`)
into `sample\` and re-run — the engine ingests packaged files directly.

## Provenance & honesty note

- The `engine/` is a **community/field** project (MIT), not a shipping Microsoft product.
  It wraps deterministic parsing + the official Tableau MCP server where live access is used.
- Power BI **TMDL**, **PBIP + Git**, **DirectLake**, **Fabric REST**, and **Copilot for DAX**
  are the first-party Microsoft building blocks this accelerator stands on.
- See `docs/customer-response.md` for the precise line between "GA product,"
  "field accelerator," and "manual effort."
