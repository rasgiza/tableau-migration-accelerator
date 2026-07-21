# Semantic Model Best Practices for a Tableau → Power BI / Fabric Migration

**The question this answers:** *When you migrate 150 Tableau workbooks whose source is
Snowflake or Azure SQL, and the customer wants DirectLake + AI/Copilot — do they need to
build a new semantic model? Since the accelerator isn't 100%, what's left for the human?*

**Short answer:** **Yes — the customer builds *new*, consolidated semantic models (ideally
star schemas). A migration is deliberately *not* a 1:1 copy, and Microsoft explicitly
recommends against making it one.** The accelerator automates the mechanical ~80% (parse,
type, calc→DAX, PBIP, deploy). The semantic-model *design* — consolidation, star-schema
modeling, role-playing dates, Q&A tuning — is the human 20%, and it's where the DirectLake +
AI value actually comes from.

This guide is **honest about what the engine does and does not do** (every claim traces to
code in `engine/skills/tableau-migration/scripts/`), so it can be handed to a customer
without overcommitting.

---

## 1. What Microsoft actually says

### Don't replicate — modernize and consolidate

Microsoft's official [Power BI migration guidance][mig] is explicit:

> *"Rather than strictly attempting to always migrate reports precisely as they appear in the
> legacy BI platform, focus on the business question the report is trying to answer."*
>
> *"Be willing to rethink how things have always been done… consolidating multiple reports
> into one, or eliminating legacy items that haven't been used for some time."*

So **150 workbooks ≠ 150 semantic models.** The target is a small set of shared, governed
models + many thin reports. The number of models equals the number of **distinct
datasources**, not the number of workbooks — in a real estate, 150 workbooks usually connect
to 20–40 published datasources.

### The model should be a star schema

Microsoft's [star schema guidance][star] states this is *the* design for performance,
usability, **and Q&A/Copilot**:

> *"Dimension tables enable filtering and grouping. Fact tables enable summarization."*
>
> *"When you source data from an export file or data extract, it's likely that it represents
> a denormalized set of data… transform and shape the source data into multiple normalized
> tables."*

That last line **is the Tableau situation**: Tableau workbooks are almost always built on
wide, denormalized extracts (a flat `Orders` with customer, product, and geography embedded
as columns). Migrating that 1:1 gives a poor Power BI model. A star schema is a **rebuild**,
not a copy.

### DirectLake *forces* the modeling upstream

From the [DirectLake overview][dl]:

> *"Direct Lake depends on data preparation being done in the data lake… using Spark jobs,
> T-SQL, dataflows, pipelines… to maximize reusability."*

DirectLake also has real modeling constraints (no calculated columns today, no complex
column types, relationships need matching data types and unique one-side keys). Translation:
**you can't paper over a bad model with report-level hacks the way Import/Tableau can — the
fact/dimension structure has to be correct in the Delta (gold) layer.** DirectLake is
designed to sit over the *gold layer of a medallion architecture* — i.e., a properly modeled
star.

### Why AI/Copilot makes this non-negotiable

Copilot and Power BI Q&A reason over **dimensions (to filter/group)** and **measures (to
summarize)**, using field names, descriptions, and synonyms. A flat 150-column mirror of a
Tableau extract gives Copilot noise; a clean star (`DimCustomer` / `DimProduct` / `DimDate` +
well-named measures + synonyms) is what makes *"show me profit by segment last quarter"*
work. The star schema isn't polish for an AI initiative — it's the substrate.

---

## 2. The 80 / 20 split — automate the mechanical, surface the judgement

| Layer | Who does it | Value |
|---|---|---|
| Parse `.twb/.tds`, type columns, calc→DAX, viz scaffold, openable PBIP | **Accelerator (automated)** | ~80% of the grind removed |
| Detect published-vs-embedded datasource; rebind a workbook to its shared model | **Accelerator (automated)** | Enables consolidation |
| Consolidate a multi-datasource workbook into one model | **Accelerator (automated)** | Fewer models |
| **Decide** which datasources become the shared governed models (estate dedup) | **Human** (engine flags the signal) | Governance |
| **Design the star schema** (fact/dims, role-playing dates, bridges) | **Human + data engineering** (Spark/T-SQL in the lake) | The real value |
| Land Delta in OneLake (mirror / notebook / pipeline) | **Fabric** | Feeds DirectLake |
| Add descriptions + synonyms for Q&A/Copilot | **Human** (engine *scores* readiness) | AI-readiness |

The accelerator is **faithful-or-stub**: it never emits a subtly-wrong model. Anything
outside the safe subset stays an inert `= 0` stub with the original Tableau formula preserved
as an annotation, and is flagged for a human. That flagged 20% *is* the semantic-model work.

---

## 3. Capability matrix — implemented vs. human (honest, code-traceable)

### ✅ Implemented in the engine today

| Capability | Where |
|---|---|
| Parse `.tds/.tdsx/.twb/.twbx`; type columns; safe-subset **calc→DAX** (formula preserved) | `assemble_model.py`, `calc_to_dax.py` |
| **Storage-mode auto-selection** (Import / DirectQuery), honest *needs-decision* fallback | `storage_mode.py` |
| **DirectLake TMDL** typed from the **actual landed Delta schema** (opt-in, never auto) | `tmdl_generate.py`, `assemble_directlake_model` in `assemble_model.py` |
| **Published-datasource detection** (`connection_class == 'sqlproxy'`) — the consolidation signal | `is_published_ds_workbook()` in `migrate_estate.py` |
| **Rebind a workbook to its already-migrated published/shared model** (unions both sides' calcs) | `_rebuild_from_published_match()` in `migrate_estate.py` |
| **Consolidate a multi-datasource workbook** into one model; resolve colliding field captions | `resolve_consolidated_column()` in `assemble_model.py` |
| **Copilot / Q&A readiness scorecard** (coverage, inert-stub measures, descriptions, synonyms) | `copilot_readiness.py`, `linguistic.py` |
| **Offline migration report** (coverage, calc lineage, follow-ups) — no server/JS | `migration_report_html.py` |
| **Deploy** model + report to Fabric over REST (LRO, rebind, refresh) | `deploy_to_fabric.py` |

### ❌ NOT implemented — the deliberate human 20%

| Not automated | Why it's human |
|---|---|
| **Star-schema decomposition** (normalize a flat `Orders` into `FactSales` + dimensions) | Requires modeling judgement + ETL; the engine types what's *landed*, it doesn't re-shape source data |
| **Estate-wide consolidation *decisions*** ("these 50 workbooks → 3 shared models") | Engine *detects and rebinds*; it records the signal but doesn't auto-pick the governed set |
| **Role-playing dimension scaffolding** (duplicate Date for Order/Ship) | A design choice per model |
| **Building the star in the lake** (Spark/T-SQL/dataflow to produce the gold Delta tables) | Data-engineering step upstream of the model, by design out of the stdlib engine |
| **Authoring descriptions/synonyms** | Engine *scores* readiness; a human writes the business language |

> **Bottom line:** the engine *enables* the star-schema + DirectLake + AI end-state (detects
> published datasources, rebinds/consolidates, types DirectLake from landed Delta, scores
> Copilot-readiness) — but the **star-schema modeling itself and the data landing are the
> human + Fabric steps.** The accelerator never claims 100%, and that's the honest, correct
> framing for the customer.

---

## 4. What this means at 150-workbook scale

```
150 Tableau workbooks  →  ~150 thin Power BI reports
                          +  ~25 shared star-schema semantic models
   (one report each)         (one per DISTINCT datasource, deduped, modeled as a star)
                          →  landed as Delta in OneLake
                          →  bound in DirectLake
                          →  which powers both the reports AND Copilot/Q&A
```

Motion:

1. **Inventory + dedup** the datasources (engine flags published vs embedded).
2. **Model a governed star** per distinct datasource (human + data engineering).
3. **Land it as Delta** in OneLake (mirror / notebook / pipeline — see
   [directlake-mirroring-flow.md](directlake-mirroring-flow.md)).
4. **Generate the DirectLake model** typed from the landed schema (accelerator, opt-in).
5. **Deploy** reports + models (accelerator).
6. **Tune for AI** — descriptions + synonyms (human; engine scores it).

---

## 5. Worked example — Superstore

Superstore ships as a **denormalized flat extract** (a single wide `Orders` table). No public
Superstore dashboard is a star schema, because the data isn't modeled that way. To demonstrate
best practice you **decompose** it:

| Star table | Built from `Orders` columns | Role |
|---|---|---|
| **FactSales** | Sales, Quantity, Discount, Profit + FKs | Fact |
| **DimCustomer** | Customer ID / Name, Segment | Dimension |
| **DimProduct** | Product ID, Category, Sub-Category, Name | Dimension |
| **DimGeography** | Country, Region, State, City, Postal Code | Dimension |
| **DimDate** | Order Date + Ship Date | Role-playing dim |
| **DimShipMode** | Ship Mode | Dimension |
| **DimRegionManager** | the **People** table | Dimension on Region |
| **FactReturns** | the **Returns** table | Fact / bridge |

This is the demo's whole point: *your Tableau workbooks sit on flat extracts; we auto-convert
the mechanical parts with the accelerator, model a governed star in the lake, land it as
Delta, bind a DirectLake model, and that model powers both your reports and Copilot.* It
**demonstrates** the best practice instead of asserting it.

---

## References

- [Understand star schema and the importance for Power BI][star] — Microsoft Learn
- [Direct Lake overview][dl] — Microsoft Fabric
- [Power BI migration overview][mig] — Microsoft Learn
- [directlake-mirroring-flow.md](directlake-mirroring-flow.md) — how the data lands
- [architecture.md](architecture.md) — the two migration motions and phased plan
- [customer-response.md](customer-response.md) — GA product vs. field accelerator vs. manual

[star]: https://learn.microsoft.com/en-us/power-bi/guidance/star-schema
[dl]: https://learn.microsoft.com/en-us/fabric/fundamentals/direct-lake-overview
[mig]: https://learn.microsoft.com/en-us/power-bi/guidance/powerbi-migration-overview
