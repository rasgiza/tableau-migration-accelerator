# Customer Response — Tableau → Power BI / Fabric Migration

**Context:** 150+ Tableau workbooks; thousands of calculations, LOD expressions,
parameters, and custom SQL; Snowflake remains the source of truth; Fabric F64 already
deployed; pilot domain is Revenue Cycle.

**Purpose:** Answer the customer's five questions accurately, without overcommitting on
automation that does not exist today. Every "automated" claim below traces to a capability
we have actually run (see `../output/` and `../README.md`), or is explicitly flagged as a
manual / human-in-the-loop step.

---

## The one-paragraph answer

Microsoft does **not** ship a GA, first-party, one-click tool that ingests Tableau
`TWB/TWBX` files and emits finished Power BI/Fabric semantic models. What Microsoft *does*
provide is a strong set of **first-party building blocks** (open PBIP/TMDL project format
with Git, DirectLake on Fabric, Fabric REST deployment APIs, Copilot for DAX authoring)
plus a proven **field/community accelerator** (`tableau-fabric-skills`) that automates the
mechanical majority of a migration — parsing the Tableau model, typing columns from the
Tableau schema, translating the *safe subset* of calculations to DAX, and generating an
openable PBIP — while **deliberately surfacing** the judgement-heavy parts (complex LOD and
table calcs, ambiguous relationships, storage-mode choice, and native-source rebind) for
human decision. We have run this end-to-end offline against a sample workbook; the evidence
is in this repo.

---

## Q1 — Does Microsoft have an officially recommended approach for automating Tableau-to-Power BI/Fabric migrations?

**Not as a single GA product.** There is no Microsoft-branded "Tableau Migration Tool" that
parses TWBX and outputs a finished semantic model.

What *is* officially recommended and first-party:

- **PBIP + TMDL** — Power BI's open, text-based project format. Semantic models and reports
  are plain files (TMDL / PBIR JSON) that live in Git and can be **generated
  programmatically**. This is the officially supported foundation that makes automated
  migration *possible* at all.
- **DirectLake on Fabric** — the recommended storage mode to put a migrated model directly
  over OneLake Delta tables at F64 scale.
- **Fabric REST APIs** — the supported way to deploy generated semantic models into a
  workspace without manual clicks.
- **Copilot in Power BI** — Microsoft's recommended assist for *authoring/repairing DAX*,
  which is where the human 20% of a Tableau migration lands.

So the honest framing to the customer: *Microsoft provides the recommended target platform
and the automation-enabling primitives; the "parse Tableau and pre-build the model" layer is
delivered by an accelerator on top of those primitives, not by a boxed product.*

## Q2 — Are there Microsoft-supported tools, GitHub/Copilot workflows, sample projects, or accelerators that analyze Tableau metadata, calculations, lineage, or workbook dependencies?

Yes — with a clear provenance line:

| Asset | What it does | Provenance |
|---|---|---|
| **`tableau-fabric-skills`** (the engine in this repo) | Parses `.tds/.tdsx/.twb/.twbx`, rebuilds the model as typed **TMDL**, translates the **safe subset** of calcs to **DAX** (originals preserved), auto-selects storage mode, and emits an openable **PBIP**; can deploy to Fabric via REST. | Community/field (MIT). Runs as a GitHub Copilot / VS Code skill. |
| **`Tableau-Fabric-AI-Bridge`** | The same capabilities as do-it-yourself Fabric notebooks ("Plays"): metadata inventory, VizQL data landing, semantic-model generation, and an **estate migration-assessment** dashboard. | Community/field. |
| **Tableau MCP server** (`mcp.tableau.com`) | Official Tableau (Salesforce) MCP server for **live** metadata + natural-language query over a published datasource — useful for lineage/inventory and for an "AI-on-top" motion. | Official Tableau, Apache-2.0. |
| **Power BI / Fabric primitives** | PBIP, TMDL, DirectLake, Fabric REST, Copilot for DAX. | First-party Microsoft (GA). |

**Copilot workflow:** the migration engine is packaged as an agent **skill**, so an SE runs
it inside GitHub Copilot / VS Code — point it at exported workbooks, it produces the PBIP
bundle plus a migration report listing exactly which calcs translated and which need review.

**What we proved (see `../output/`):** an offline run over a Superstore datasource +
workbook produced a typed TMDL model, auto-translated 2 of 3 measures to DAX
(including choosing `DIVIDE` for safe division), preserved the untranslatable table calc as
an annotated stub, and emitted an openable `.pbip`.

**On the visuals (not just the model):** the engine also reads each dashboard's viz grammar
and rebuilds **native, live Power BI visuals** — bar/column, line, area, combo, table, matrix,
pie, scatter, maps, cards, and slicers — bound to the migrated model, with canvas size and zone
layout mapped. It does *not* image-convert or screenshot a dashboard. Exotic marks (treemap,
packed bubbles, polygons), exact formatting (fonts/colors/tooltips), and filter-scope semantics
are deferred to a structured warning, and a separately-authored `fidelity_oracle` scores every
visual 0..1 so reviewers get a punch-list, never a silent guess. The data model is high-fidelity;
the report layer is a faithful structural rebuild that a designer refines.

## Q3 — Has anyone supported a large Tableau migration (100+ workbooks), and what are the lessons learned?

Patterns that hold for a 150-workbook estate (and are baked into this accelerator):

1. **Assess before you move.** Run an estate inventory first (datasource/workbook counts,
   connection diversity, calc complexity, certification/staleness). Migration scope and cost
   are an output of the assessment, not a guess. See `assessment-methodology.md`.
2. **Migrate by domain, not big-bang.** The pilot is Revenue Cycle — do one business domain,
   validate with users, then expand. The engine supports filtering/batching to scope a subset.
3. **Datasources are the reuse unit.** Many workbooks share a handful of datasources.
   Migrate the shared **semantic models** once; rebind reports to them. This is where the
   150→small-number leverage comes from.
4. **The calc long-tail is the real work.** Simple aggregations and ratios translate
   deterministically; **LOD expressions, table calcs, and `RUNNING_*`/`WINDOW_*` functions**
   are the manual tail. Budget human DAX time proportional to *distinct* complex calcs, not
   workbook count.
5. **Snowflake stays the source of truth.** Land/serve via DirectLake over
   Mirroring/Shortcuts to Snowflake; don't fork the data. The generated model binds by
   table name + schema so the ingestion path can be swapped without breaking the model.
6. **Fidelity is validated, not assumed.** Keep the original Tableau formula on every measure
   as an annotation so reviewers can diff intent vs. translation.

## Q4 — Are there Fabric, Power BI, CAT, or GitHub resources that could advise on migration automation strategy for a pilot?

- **First-party platform docs & GA features:** PBIP/TMDL, DirectLake, Fabric REST,
  Copilot for DAX — the supported target and automation surface.
- **The accelerator repos** (`tableau-fabric-skills`, `Tableau-Fabric-AI-Bridge`) — runnable
  starting points; this folder is a working instance of them.
- **Official Tableau MCP** — for live lineage/inventory and the AI-on-top motion.
- **Engagement path for a pilot:** scope with the estate assessment → migrate the Revenue
  Cycle domain's shared datasources → validate in Power BI Desktop against the source
  Tableau views → expand. Bring in Fabric CAT / account-aligned specialists for the
  DirectLake-over-Snowflake and F64 capacity-sizing decisions.

*Note: treat the community accelerators as field IP, not a Microsoft SLA-backed product.
Position them as "we can stand this up and prove it in your tenant," not "Microsoft ships
and supports this as a product."*

## Q5 — Is there Microsoft guidance on converting Tableau business logic (LOD, calculations, parameters) into Power BI semantic models at scale?

There is no official 1:1 "Tableau function → DAX function" conversion product. In practice:

| Tableau construct | Converts how | Automated today? |
|---|---|---|
| Simple aggregations (`SUM`, `AVG`, `MIN/MAX`) | Direct DAX aggregation | ✅ Deterministic |
| Ratios / arithmetic on aggregates | DAX with **safe division** (`DIVIDE`) | ✅ Deterministic (proven) |
| Row-level calculated fields | DAX calculated column / measure | ✅ Common subset |
| **FIXED LOD** | `CALCULATE` + `ALLEXCEPT` / grouping patterns | ⚠️ Partly — pattern-based, **review each** |
| **INCLUDE / EXCLUDE LOD** | Context-transition DAX patterns | ⚠️ Manual / assisted |
| **Table calcs** (`RUNNING_SUM`, `WINDOW_*`, `RANK`) | DAX time-intelligence / window patterns | ⚠️ Manual (stubbed + preserved) |
| **Parameters** | Field parameters / what-if parameters / slicers | ⚠️ Partly — mapped where shape allows |
| **Custom SQL** | M query / Fabric SQL / view over Snowflake | ⚠️ Case-by-case |

**How the accelerator handles the long tail honestly:** the deterministic pass translates
the safe subset and **stubs everything it can't prove**, preserving the original Tableau
formula as a `TableauFormula` annotation (so nothing is silently wrong). An **opt-in,
LLM-assisted "second compiler"** pass can then propose DAX for the stubs — but only on
explicit approval, and each candidate is gate-checked. Complex LOD/table-calc logic is where
human DAX expertise (assisted by Copilot) is still required, and effort should be estimated
against the count of **distinct** complex calcs across the estate.

---

## Bottom line for the customer

- **Don't promise** a Microsoft one-click TWBX→PBIP product — it doesn't exist.
- **Do offer** a proven, repeatable accelerator that automates the mechanical majority,
  is fully auditable (original formulas preserved), deploys to their F64 via Fabric REST,
  and keeps **Snowflake as the source of truth** via DirectLake.
- **Scope honestly:** the automation removes weeks of schema/model rebuild; the remaining
  effort is concentrated in the *distinct* complex calculations and relationship/storage
  decisions — which we quantify up front with an estate assessment before any commitment.
