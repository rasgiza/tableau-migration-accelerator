# Competitive Analysis — Honest Positioning

How this accelerator compares to the public Tableau→Power BI / Fabric migration guides, commercial
accelerators, and open-source engines; what it genuinely does well, where others are ahead, and the
short list of ideas worth stealing.

Sources reviewed (Nov 2026):

- **Power BI Consulting** — [Tableau to Power BI Migration: Step-by-Step (2026)](https://powerbiconsulting.com/blog/tableau-to-power-bi-migration-guide-2026) — phased methodology, complexity scoring, wave planning, change management.
- **Emerline** — [Migrating from Tableau to Power BI and Microsoft Fabric](https://emerline.com/blog/migrating-from-tableau-to-power-bi-and-microsoft-fabric) — OneLake/DirectLake target architecture, anti-lift-and-shift, RLS→Entra ID, IT checklist.
- **BIChart** — [Tableau Prep to Microsoft Fabric: A Pattern-by-Pattern Migration Guide](https://bichart.ai/blog/tableau-prep-to-microsoft-fabric-a-pattern-by-pattern-migration-guide) — 10-pattern classification of Tableau Prep flows.
- **Evoke Technologies** — [Tableau to Power BI Migration Accelerator](https://www.evoketechnologies.com/services/data-ai/tableau-to-power-bi-migration-services/) — Asset Discovery, Metadata Extraction, Dataset/Visual Conversion, Validation & Reconciliation, Migration Monitoring.
- **Pulse Convert** — Azure Marketplace SaaS accelerator (overview page; direct competitor).
- **cyphou/Tableau-To-PowerBI** — [open-source MIT engine on GitHub](https://github.com/cyphou/Tableau-To-PowerBI) — the closest *code-level* competitor; see [§ 1a](#1a-open-source-engines--the-closest-competitor).

> **Honesty note.** This is an *accelerator*, not a zero-touch converter. Every public guide
> agrees no tool converts `.twb/.twbx` to production-quality Power BI unattended
> (Power BI Consulting: third-party parsers "handle only 40–60% and require significant manual
> refinement"). Our differentiator is not a bigger automation claim — it's that we
> **automate the mechanical and *prove* what we translated, warn-never-wrong on the rest.**

---

## 1. Capability coverage vs. the guides & commercial accelerators

Grounded in the current engine (`engine/skills/tableau-migration/scripts/`), verified in code and
tests. This section compares against the **consulting methodologies and commercial accelerators**
listed above; for the open-source engine comparison see [§ 1a](#1a-open-source-engines--the-closest-competitor).

| Capability competitors advertise | What the accelerator already does | Evidence |
|---|---|---|
| Asset Discovery / inventory | `scan_estate` + parsers enumerate workbooks, datasources, connections, calcs, params, viz. | `migrate_estate.py::scan_estate`, `scan.json` |
| Metadata extraction | Calc graph, table-calc usage, parameter classification, custom-SQL detection. | `calc_graph.py`, `workbook_table_calcs.py`, `parameters.py` |
| Dataset conversion | `.tds/.tdsx` → **TMDL semantic model** with types, relationships, display folders, hierarchies. | `tmdl_generate.py`, `assemble_model.py` |
| Visualization conversion | Tableau viz → **native Power BI visuals** in a `.pbip` report. | `twb_to_pbir.py`, `visual_calc_emitter.py` |
| Validation & reconciliation | **Empirical numeric oracle** parses BOTH the Tableau formula and candidate DAX into one shared AST, evaluates over landed rows, returns PASS/FAIL/INCONCLUSIVE. Faithful-or-stub. | `reconciliation_oracle.py` |
| Visual/structural parity | Structural fidelity score + image oracle for layout/format parity. | `fidelity_oracle.py`, `image_oracle.py` |
| RLS / entitlement preservation | Tableau user filters → Power BI `role` blocks with `tablePermission` DAX; `USERNAME()`→`USERPRINCIPALNAME()`; **untranslatable filters fail closed to `FALSE()`** (deny-by-default). Original preserved as `annotation TableauUserFilter`. | `tmdl_generate.py`, `test_model_enrichment.py` |
| Complexity scoring / effort model | 1–5 per-datasource rubric + effort coefficients (calibrate after pilot), **now auto-produced per workbook** as a `report.json` `assessment` block: surface area, complexity bucket, transparent 0–100 difficulty score, unused-field + orphaned-worksheet detection, per-calc impact. | [assessment-methodology.md](assessment-methodology.md), `workbook_assessment.py` |
| Everything-as-code / governance | PBIP/TMDL in Git; original Tableau formula kept as annotation for intent-vs-translation diff. | [architecture.md](architecture.md) |
| Reproducibility / trust | 65+ test files, CI on every push (byte-compile + `pytest`), two-copy mirror-parity guard, version-bump check. | `.github/workflows/tests.yml` |
| Migration monitoring | Self-contained offline `migration-report.html` (estate **assessment** sizing, coverage KPIs, sign-off table, lineage, follow-ups) rendered from `report.json` every run. | `migration_report_html.py`, `workbook_assessment.py` |

**Net:** the six "Core Capabilities" Evoke sells (Asset Discovery, Metadata Extraction, Dataset
Conversion, Visualization Conversion, Validation & Reconciliation, Migration Monitoring) map 1:1
to shipping code here — **including Migration Monitoring** (`migration-report.html`, added as steal
#1 below).

---

## 1a. Open-source engines — the closest competitor

Most of the sources above are consulting **methodologies**, not automation. There is, however, at
least one serious open-source **engine** in this space, and it is only fair to say so plainly.

**[cyphou/Tableau-To-PowerBI](https://github.com/cyphou/Tableau-To-PowerBI)** (MIT, Python) is broad
and actively developed. On raw feature surface area it is **ahead of this project**: a VS Code
extension, a plugin SDK and pattern marketplace, shared/merged semantic models with thin reports,
Tableau Prep (`.tfl`) flow migration, Tableau Server/Cloud ingestion, a Fabric-native output chain
(Lakehouse + Dataflow Gen2 + PySpark notebook + DirectLake model + pipeline), a DAX optimizer, and a
QA auto-fix suite — with a much larger advertised connector / visual / function count.

The two projects optimize for **opposite** things, and that — not feature count — is the real
distinction:

| | This accelerator | cyphou/Tableau-To-PowerBI |
|---|---|---|
| Optimizes for | **Correctness** — prove it or flag it | **Coverage** — convert everything, auto-fix after |
| Untranslatable construct | Inert labeled stub + original formula; surfaced in the worklist | Converted aggressively; a QA pass auto-patches known "leak patterns" |
| Numeric validation | `reconciliation_oracle.py` — Tableau formula **and** candidate DAX parsed into one shared AST, evaluated over landed rows; `pass` only on proven agreement, else the stub stays | `--validate-data` post-migration query equivalence |
| Data in OneLake | Customer's choice — Import/DirectQuery move no data; DirectLake is opt-in | Fabric-native path lands data as Delta |
| Breadth | Deliberately narrower; deep on LOD / table calcs / custom SQL | Very broad |

**Honest framing to use.** Do not claim to be "the best" or the only option. The defensible claim is
narrower and stronger: *this project is built so that a wrong number is never shipped silently — it
proves a translation against real rows or leaves a labeled stub.* Both projects publish their own
test counts; neither number is a head-to-head correctness measurement. A public benchmark comparing
both tools' output against Tableau's actual computed values is the only thing that would settle it,
and until that exists this document should not imply the question is settled.

---

## 2. Ideas worth stealing (ranked)

| # | Idea (source) | Status in repo | Value | Recommended action |
|---|---|---|---|---|
| 1 | **Migration Monitoring dashboard** — leadership-visible progress/coverage/validation (Evoke); "data lineage mapping" (Emerline checklist) | ✅ **Shipped** — `migration_report_html.py` emits a self-contained offline `migration-report.html` from `report.json` on every run | High | Done: coverage KPIs, definition-of-done banner, workbook sign-off table, per-datasource calc lineage, and de-duplicated manual follow-ups — no web server, no JS, all HTML-escaped. |
| 2 | **Emit the complexity score + wave plan** (Power BI Consulting) | ✅ **Shipped** — `workbook_assessment.py` computes a per-workbook complexity bucket + transparent 0–100 difficulty score (formula printed, no opaque "AI"), surface-area band, unused-field + orphaned-worksheet detection, and per-calc Low/Med/High impact; rolled up in `report.json` `assessment` and rendered in `migration-report.html` | High | Done from data we already parse. Difficulty is defensible arithmetic over the workbook's own grammar, not a guess. |
| 3 | **Batch resumability / idempotency** | ❌ Gap — monolithic pass; `--force` clobbers hand-fixes | High | Add per-workbook checkpoint + `--resume` so a mid-estate failure doesn't force re-run-all or overwrite finished work. |
| 4 | **Fabric deploy last-mile** — publish → bind creds → gateway → refresh (Emerline checklist) | 🟡 `deploy_to_fabric.py` exists, wrapper doesn't orchestrate it | Med | Wire the publish→bind→refresh sequence into the one-command wrapper behind an explicit `-Deploy` flag. |
| 5 | **Anti-"lift-and-shift" star-schema guidance** (Emerline) | 🟡 We emit flat models + flag; guidance is implicit | Med | Add a "modeling debt" note to `report.json` manual-followups when a flat, wide extract is detected. |
| 6 | **Incremental refresh policy** (implied by Import-mode guidance) | ❌ Gap — M partitions emitted, no `refreshPolicy`/`RangeStart`/`RangeEnd` | Low/Med | Emit a `refreshPolicy` scaffold for large Import datasources. |
| 7 | **Tableau→Power BI action reference card** (Power BI Consulting change-mgmt) | ⚪ Not in scope (adoption enablement) | Low | Optional one-page "in Tableau you clicked X → in Power BI click Y" doc for enablement. |
| 8 | **Tableau Prep (`.tfl`) flow classification** — 10-pattern → Fabric target map (BIChart) | 🔴 Out of current scope — engine parses `.twb/.twbx/.tds/.tdsx`, not `.tfl` | Roadmap | Document as explicit scope boundary; Prep flows map to Dataflow Gen2 / Lakehouse SQL / Pipelines, not the semantic-model path. |

Legend: ✅ done · 🟡 partial · ⚪ additive value · ❌ genuine gap · 🔴 out of scope.

---

## 3. Positioning claims we can defend

- **"We prove translations, we don't just generate them."** None of the consulting guides describe a
  value-level numeric reconciliation oracle; they rely on a manual "match within 2%" parallel-run
  (Power BI Consulting). We keep that human gate *and* automate the arithmetic check where the
  formula is in a safe subset. Note that `cyphou/Tableau-To-PowerBI` does ship post-migration data
  validation (`--validate-data`), so the defensible distinction is not "we validate and they don't"
  — it is **what happens when validation cannot prove equivalence**: here the candidate is rejected
  and the labeled stub stays (faithful-or-stub), rather than shipping the conversion and patching
  known failure patterns afterwards.
- **"Warn-never-wrong."** Untranslatable RLS fails closed to `FALSE()`; unresolved fields are
  never guessed; unsupported calcs become faithful stubs. A red definition-of-done gate is a
  feature, not a failure — it's the honesty other tools omit.
- **"Everything-as-code, reviewable in PRs."** PBIP/TMDL in Git with the original Tableau formula
  annotated for intent-vs-translation diffing.
- **"Offline-first, deterministic, stdlib-only core."** Runs air-gapped; no vendor SaaS upload of
  the customer's semantic estate.

---

## 4. Scope boundaries (say the quiet part)

- **Not zero-touch.** Complex nested LOD, multi-pass table calcs, and non-foldable custom SQL are
  surfaced as explicit human gates — consistent with every public guide.
- **Tableau Prep flows (`.tfl`) are out of scope** for the semantic-model path (see steal #8).
- **DirectLake is never auto-selected.** Storage mode is an explicit decision, not a guess.
- **Adoption / change management** (training, champion networks, quick-reference cards) is an
  engagement activity, not an engine feature.
