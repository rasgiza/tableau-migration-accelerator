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

Requirements: **Python 3.11+** (the script auto-detects `py -3.11` / `python`).
No live Tableau, no Tableau Desktop, no internet.

### Will this run on my teammates' machines?

Yes. The engine is **pure Python 3.11 standard library — zero `pip install`** for the
core migration (parse `.twb`/`.tds`, translate calcs → DAX, build the TMDL semantic
model + `.pbip` report + native visuals). That core runs identically on **Windows
(x64 and ARM64), macOS (Intel and Apple Silicon), and Linux**. The PowerShell wrapper
is Windows-only, but the underlying `migrate_estate.py` runs anywhere Python does.

There is exactly **one optional native dependency**, `tableauhyperapi`, used *only* to
read the data baked inside packaged `.twbx` files (the `.hyper` extract). It is not
required for the deliverable and the engine **warns-and-skips** when it is absent — it
never crashes. Install it only if you need to crack open packaged extracts:

```powershell
py -3.11 -m pip install tableauhyperapi
```

Availability: x64 Windows / x64 Linux / macOS ✅. **Windows-on-ARM is the one gap**
(Salesforce ships no ARM wheel) — on those machines packaged `.twbx` extracts warn and
skip; run just those on any x64 box or CI runner, or bind to the live warehouse instead
(the usual real-project path, where `.hyper` reading is never needed).

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
| `engine/skills/tableau-migration/resources/viz-rebuild.md` | The visual layer: which Tableau chart types rebuild into which Power BI visuals, and what is deferred to a warning. |

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

## What happens to my dashboards & visuals?

The tool does **not** screenshot or image-convert a dashboard. It reads the dashboard's
underlying **viz grammar** (the workbook XML — marks, shelves, encodings, filters, and zone
layout) and rebuilds **native, live Power BI visuals** bound to the migrated model. You get an
interactive `.pbip` report, not a flat picture. Fidelity splits into two layers:

| Layer | What it covers | Fidelity |
|---|---|---|
| **Semantic model** (data + calcs) | Types, tables, relationships, safe calc→DAX. **The deliverable.** | High — typed TMDL, deterministic |
| **Report / visuals** | Chart types, field bindings, dashboard layout | Structural — faithful for the supported set; polish expected |

**Chart types rebuilt faithfully** (see [viz-rebuild.md](engine/skills/tableau-migration/resources/viz-rebuild.md) for the full mapping table): bar/column (incl. stacked), line, area, dual-axis combo, table, matrix/highlight table, pie, scatter, filled/point maps, cards, and slicers. Dashboard canvas size and zone positions are mapped, and axis sorts are preserved when the sort measure is bound.

**Deferred to a structured warning (never guessed wrong):** exotic marks (treemap, packed bubbles, polygons, Gantt), exact formatting (fonts, colors, tooltips, conditional formatting), filter-scope semantics (a Tableau filter card ≠ a Power BI slicer), reference lines, annotations, and dashboard actions. These are surfaced for a human to finish.

**Every visual is scored.** The `fidelity_oracle` is a separately-authored second opinion that re-reads both sides from disk and reports a per-visual 0..1 agreement across four components — chart-type family, field bindings, role split (axis vs. value), and dashboard layout position — so you get a punch-list of exactly which visuals matched and which need hand-finishing, rather than a guess.

Bottom line: it removes the mechanical rebuild (recreating dozens of charts from scratch and
rebinding every field) and hands a designer a **live, openable report to refine** — not a blank
canvas and not a static image.

## How does it handle my calculations, LOD expressions, parameters & custom SQL?

This is the question most estates actually care about. A large customer told us they had
*"150+ Tableau workbooks; thousands of calculations, LOD expressions, parameters, and custom
SQL."* Here is exactly what the engine does with each — automated where it can be **proven**,
flagged (never silently guessed) where it can't:

| Tableau construct | What the engine does | Confidence |
|---|---|---|
| **Calculations** (arithmetic, logical, string, date, standard aggregations) | Deterministically translated to **DAX**, original formula preserved as a `TableauFormula` annotation. Safe division becomes `DIVIDE()`, not a naive `/`. | High — auto |
| **Calculations** (table calcs, `RUNNING_SUM`, `WINDOW_*`, rank, nested/argmax) | Emitted as an **inert, labeled stub** with the original formula attached, so it's a visible TODO — never a wrong number that ships. | Flagged for review |
| **LOD — `FIXED`** over a real/derived grain (e.g. `{FIXED [Order Date (Months)] : SUM([Sales])}`) | Detected, bound to a real calculated column, and translated. | High — auto (tractable cases) |
| **LOD — `INCLUDE` / `EXCLUDE`, nested LODs, argmax** | Detected and **handed off** rather than force-fit into wrong DAX (no clean 1:1 exists). | Flagged for review |
| **Parameters — value / what-if** | Rebuilt as a disconnected what-if table + a `[<Param> Value]` measure. | High — auto |
| **Parameters — field/measure swap** | Rebuilt as native **Power BI field parameters**. | High — auto |
| **Parameters — plain filter** | Surfaced for review (a Tableau filter card ≠ a Power BI slicer). | Flagged for review |
| **Custom SQL** (foldable) | Flows through as a native query with correct de-escaping and parameter-reference extraction. | High — auto |
| **Custom SQL** (unfoldable cross-engine joins/unions, unknown connector) | **Reported**, not dropped, so a human rebinds it. | Flagged for review |

**What to expect at estate scale.** On a calc-heavy stress-test estate, a single run auto-translated
~28% of workbook calcs and flagged the rest — that number is *deliberately conservative* because the
engine refuses to guess. Real production estates skew far higher, because most calcs are simple
arithmetic/logical expressions. The value is not "100% automatic": it is that the tool does the
mechanical majority and hands your team a **precise, per-construct worklist** of exactly what needs a
human — instead of forcing them to hunt for what silently broke.

**Guiding principle — *warn, never wrong*.** Every run ends with a definition-of-done gate that fails
*loud* when it cannot prove a binding (e.g. it will not auto-pick Import vs. DirectLake storage mode).
A red gate is the tool being honest, not broken — it converts what it can prove and refuses to guess
the rest.

## Planning a large estate (e.g. 150+ workbooks) — what to expect

If you are sizing a real migration, tell the customer these four things up front. It is an
**accelerator, not a zero-touch converter** — and that distinction is the whole value.

1. **Plan for a review pass.** Expect a meaningful flagged/stubbed list on any large estate. The
   value is not "100% automatic" — the tool does the mechanical majority, tells you *exactly* which
   calcs/LODs/custom-SQL need a human, and **never ships a wrong measure** (see the construct table
   above).
2. **Storage mode is deliberately not guessed.** It will not auto-pick DirectLake vs. Import; it
   binds what it can prove and flags the rest for you to point at the live warehouse. A red
   definition-of-done gate here is expected, not a failure.
3. **Packaged `.twbx` extract reading needs x64.** The one optional native dependency
   (`tableauhyperapi`) has no Windows-on-ARM wheel. For a 150-workbook batch, run the estate on an
   **x64 box or CI runner** — otherwise packaged-data workbooks warn-and-skip (see
   [Will this run on my teammates' machines?](#will-this-run-on-my-teammates-machines) above).
4. **Visual fidelity is "close, needs eyeballing."** Charts rebuild as native, live Power BI
   visuals, but complex vizzes want a visual QA pass — the `fidelity_oracle` hands you a per-visual
   punch-list of exactly which ones matched and which need hand-finishing (see
   [What happens to my dashboards & visuals?](#what-happens-to-my-dashboards--visuals) above).

**Bottom line for the customer conversation:** it collapses the mechanical majority of a
150-workbook migration into a deterministic, repeatable, **offline** batch, and — crucially — hands
the team a **precise, labeled worklist** for the LOD / custom-SQL / calc tail instead of forcing them
to hunt for what silently broke. At that scale, "here is exactly what needs a human" is worth more
than the raw conversion percentage.

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
