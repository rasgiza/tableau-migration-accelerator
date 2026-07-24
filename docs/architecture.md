# Reference Architecture — Tableau → Power BI / Fabric

This document describes the target architecture, the **two migration motions**, and a
**phased, domain-first** rollout plan. It is **source-agnostic**: your system of record can be
**Snowflake, Azure SQL, Databricks, Fabric SQL, or any warehouse** — the semantic model binds
by table name, so the ingestion path can change without rewriting the model. Where a concrete
example helps, this doc uses a **150+ workbook estate on a warehouse, landing on Fabric F64,
Revenue-Cycle domain first**; treat those specifics as *illustrative*, not required.

---

## Big picture at a glance

Keep the data in place; migrate the **intelligence** as reviewable code; serve it on Fabric via
**DirectLake**. Source-agnostic — the semantic model binds by table name, so the ingestion path
can change without rewriting the model.

```mermaid
flowchart LR
    subgraph SRC["1 - SOURCE (stays put)"]
        TAB[(Tableau Server / Cloud<br/>or Desktop)]
        WH[(Your warehouse<br/>Snowflake / Azure SQL / Databricks)]
    end
    subgraph ACCEL["2 - ACCELERATOR (offline, deterministic)"]
        FILES[.twb / .twbx / .tds / .tdsx] --> PARSE[Parse + inventory]
        PARSE --> MODEL[Typed TMDL model]
        PARSE --> CALC[Calc -> DAX<br/>safe subset + preserved stubs]
        PARSE --> VIZ[Report pages / visuals]
        MODEL --> PBIP[.pbip project in Git]
        CALC --> PBIP
        VIZ --> PBIP
    end
    subgraph GATES["Human gates (never guessed)"]
        LOD[Complex LOD / table calcs]
        REL[Relationship review]
        STORE[Storage mode: Import vs DirectLake]
    end
    subgraph FABRIC["3 - FABRIC target (F-SKU workspace)"]
        OL[(OneLake Delta<br/>Mirroring / Shortcut)]
        SM[Semantic model<br/>DirectLake]
        RPT[Power BI reports]
        COP["Copilot / Q&A"]
        USERS[Business users]
    end
    TAB --> FILES
    WH -. Mirror/Shortcut .-> OL
    PBIP -->|Fabric REST CI/CD| SM
    OL -->|bind by table name| SM
    SM --> RPT --> USERS
    SM --> COP
    CALC -.review.-> LOD
    MODEL -.review.-> REL
    PBIP -.decide.-> STORE
```

The rest of this doc unpacks each layer: the **design principles**, the **two migration
motions**, the **target-state** detail, and the **phased rollout**.

---

## Design principles

1. **Your warehouse stays the source of truth.** No data fork. Fabric reads it via
   **Mirroring or Shortcuts** (Snowflake, Azure SQL, Databricks, Fabric SQL, ...), and
   semantic models serve via **DirectLake**.
2. **Automate the mechanical, surface the judgement.** Schema, types, and safe-subset
   calc→DAX are deterministic and auditable. Complex calcs, relationships, and storage-mode
   decisions are explicit human gates — never silently guessed.
3. **Everything-as-code.** Semantic models and reports are **PBIP/TMDL** in Git; deployment
   to F64 is via **Fabric REST**. Migrations are reproducible and reviewable in PRs.
4. **Fidelity is provable.** Every migrated measure keeps its original Tableau formula as an
   annotation so reviewers can diff intent vs. translation.
5. **Modernize, don't replicate.** A migration is *not* a 1:1 copy. Workbooks consolidate into
   a small set of shared **star-schema** semantic models; the model design is the human 20%.
   See [semantic-model-best-practices.md](semantic-model-best-practices.md) for the
   Microsoft-grounded rationale and an honest implemented-vs-human capability matrix.

---

## Two migration motions

You do not have to choose one. They are complementary and the pilot can start with either.

### Motion A — "AI on top" (keep Tableau, add Fabric intelligence)

Use the **official Tableau MCP server** for live natural-language query, lineage, and
inventory over published datasources. Value is fast (days), non-destructive, and de-risks the
estate by producing an accurate, live inventory that *feeds* Motion B.

```mermaid
flowchart LR
    U[Analyst / Copilot] -->|NL query| MCP[Tableau MCP server]
    MCP --> TS[(Tableau Server<br/>published datasources)]
    MCP --> INV[Estate inventory<br/>+ lineage]
    INV --> ASSESS[Migration assessment]
```

### Motion B — Migration accelerator (rebuild on Fabric)

The `tableau-migration` engine parses workbooks/datasources **offline** and generates the
Fabric-native model + report as code, then deploys to F64.

```mermaid
flowchart LR
    subgraph Source
      TWBX[.twb / .twbx / .tds / .tdsx]
      SNOW[(Warehouse - source of truth<br/>Snowflake / Azure SQL / Databricks)]
    end
    TWBX --> PARSE[Deterministic parser]
    PARSE --> TMDL[Typed TMDL<br/>semantic model]
    PARSE --> CALC[Calc → DAX<br/>safe subset + stubs]
    CALC --> TMDL
    PARSE --> PBIR[PBIR report pages]
    TMDL --> PBIP[.pbip project in Git]
    PBIR --> PBIP
    PBIP -->|Fabric REST| WS[Fabric F64 workspace]
    SNOW -->|Mirroring / Shortcut| OL[(OneLake Delta)]
    OL -->|DirectLake| WS
    subgraph Manual gates
      LOD[Complex LOD / table calcs]
      REL[Relationship review]
      STORE[Storage-mode decision]
    end
    CALC -.review.-> LOD
    TMDL -.review.-> REL
    PBIP -.decision.-> STORE
```

---

## Target-state architecture

```mermaid
flowchart TB
    SNOW[(Warehouse - system of record<br/>Snowflake / Azure SQL / Databricks)]
    SNOW -->|Mirroring / Shortcut| OL[(OneLake<br/>Delta tables)]
    OL --> SM[Power BI semantic models<br/>DirectLake · TMDL in Git]
    SM --> RPT[Power BI reports<br/>PBIR]
    SM --> COPILOT[Copilot for DAX<br/>author / repair]
    RPT --> USERS[Business users<br/>Revenue Cycle first]
    GIT[(Git repo<br/>PBIP projects)] -->|Fabric REST CI/CD| WS[Fabric F64 workspace]
    SM --- GIT
    RPT --- GIT
```

**Why DirectLake over your warehouse:** keeps the warehouse authoritative, avoids import refresh
windows at F-SKU scale, and lets the semantic model bind by table/schema name so the ingestion
path (Mirroring vs. Shortcut vs. staged Delta) can change without rewriting the model.

**Native-source rebind:** the engine emits the model with the source connection abstracted.
The live-pipeline step points those tables at the OneLake Delta landing of your warehouse. This is
a deliberate manual/config step (the offline demo proves model+calc generation; the rebind is
where real data lands).

> **Worked example (real backend):** [real-source-binding-runbook.md](real-source-binding-runbook.md)
> executes this native-source rebind end-to-end against a real **Azure SQL** database
> (Snowflake/Fabric-SQL stand-in) — provisioning, Entra-only auth, giving each datasource a
> resolvable physical descriptor, and re-running so two previously-unbound workbooks flip to
> model-bound `.pbip`. It also validates the motion against enterprise best practice.

---

## Phased plan — domain-first (Revenue-Cycle example)

| Phase | Goal | Key activities | Exit criteria |
|---|---|---|---|
| **0. Assess** | Know the estate | Inventory workbooks/datasources; score calc complexity; identify shared datasources; size effort (`assessment-methodology.md`). | Ranked backlog + effort estimate for Revenue Cycle. |
| **1. Foundation** | Data landed on Fabric | Mirror/Shortcut Snowflake Revenue-Cycle schemas into OneLake; validate DirectLake. | Delta tables queryable at F64. |
| **2. Pilot migrate** | One domain proven | Run the accelerator on Revenue-Cycle datasources; auto-translate safe calcs; manually resolve complex LOD/table calcs with Copilot; rebind to DirectLake; deploy via REST. | Reports validated against source Tableau views by users. |
| **3. Harden** | Repeatable | CI/CD for PBIP via Git + Fabric REST; RLS; certification; performance tuning. | Green PR-to-workspace pipeline. |
| **4. Scale out** | Expand domains | Repeat by domain, reusing shared migrated semantic models; batch workbooks. | Estate coverage against the Phase-0 backlog. |

---

## What is automated vs. manual (architecture view)

| Layer | Automated | Manual / assisted |
|---|---|---|
| Schema & column types | ✅ From Tableau schema | — |
| Simple calcs & ratios | ✅ Deterministic DAX (`DIVIDE` etc.) | — |
| LOD / table calcs | ⚠️ Stubbed + preserved | ✅ Human + Copilot |
| Relationships | Inferred where join keys present | ✅ Review (esp. from `.twbx` fidelity) |
| Storage mode | Proposed | ✅ **Explicit decision** (Import vs. DirectLake) |
| Data landing / rebind | Model bound by name | ✅ Mirroring/Shortcut config |
| Deployment | ✅ Fabric REST | — |

> **Fidelity caveat:** a `.twbx`/`.twb` file gives model + calc + layout fidelity but not row
> data and may omit hidden join keys. The offline demo proves calc→DAX / TMDL / PBIP
> generation; **relationship inference and data landing are the live-pipeline (Phase 1–2)
> step**, best done against published datasources (via Tableau MCP / VDS) + your warehouse.
