# Tableau → Power BI / Fabric construct mapping

Companion reference for the [`tableau-migration`](../SKILL.md) skill. This is the detailed
construct-by-construct map behind the workload table in `SKILL.md`. Guiding principle throughout:
**warn, never wrong** — anything without a proven deterministic translation is emitted as a labeled
stub and surfaced in the migration report, never guessed.

## Source formats

| Tableau file | Format | How it is read |
|---|---|---|
| `.tds` | XML datasource | Parsed as XML; connection metadata → Power Query M |
| `.tdsx` | Zip (packaged `.tds` + `.hyper`) | Unzipped; extract read via `tableauhyperapi` (x64) |
| `.twb` | XML workbook | Parsed as XML; worksheets/dashboards → report; datasources → model |
| `.twbx` | Zip (packaged `.twb` + `.hyper`) | Unzipped; same as `.twb` plus embedded extract |

The XML schema is stable across most Tableau versions. Where an attribute spelling drifts between
versions/drivers, check each known variant **newest-first** (e.g. a Databricks SQL-warehouse HTTP path
may appear as `v-http-path`, `http-path`, `httppath`, or `http_path`) so a real file resolves
regardless of the Tableau version that produced it. This is version-*tolerant* parsing, not
version-*specific* dispatch — exotic version-specific constructs flag for review rather than break.

## Calculations → DAX

| Tableau construct | Translation | Confidence |
|---|---|---|
| Arithmetic / logical / string / date expressions | Direct DAX; formula kept as `TableauFormula` annotation | High — auto |
| Standard aggregations (`SUM`, `AVG`, `MIN`, `MAX`, `COUNT`, `COUNTD`) | DAX aggregation functions | High — auto |
| Division (`[a] / [b]`) | `DIVIDE(a, b)` — preserves Tableau safe-division | High — auto |
| `IF` / `CASE` / `IIF` | DAX `IF` / `SWITCH` | High — auto |
| Date functions (`DATEADD`, `DATEDIFF`, `DATETRUNC`, `DATEPART`) | DAX date functions | High — auto |
| Table calcs (`RUNNING_SUM`, `WINDOW_SUM/AVG/MIN/MAX`, `RANK`, `INDEX`, nested) | Inert labeled stub with original formula | Flagged for review |
| `LOD FIXED` over a real/derived grain | Calculated column or measure bound to a real column | High — auto (tractable cases) |
| `LOD INCLUDE` / `EXCLUDE` / nested LOD / argmax | Handed off (no clean 1:1 DAX) | Flagged for review |

## Parameters

| Tableau parameter | Power BI target | Confidence |
|---|---|---|
| Value / what-if | Disconnected what-if table + `[<Param> Value]` measure | High — auto |
| Field / measure swap | Native **Power BI field parameters** | High — auto |
| Plain filter | Surfaced for review (filter card ≠ slicer) | Flagged for review |

## Custom SQL

| Tableau custom SQL | Translation | Confidence |
|---|---|---|
| Foldable single-source query | Native query in M; de-escaped; parameter references extracted | High — auto |
| Unfoldable cross-engine joins/unions, unknown connector | Reported for manual rebind | Flagged for review |

## Storage modes

The customer chooses; the tool derives a safe default and never auto-lands data in OneLake.

| Mode | When it is the default | Data movement | Where interactive queries run |
|---|---|---|---|
| **Import** | Extract-backed, flat-file, or driver-only ODBC source | None (cached in the model at refresh) | Fabric capacity |
| **DirectQuery** | Live relational source (SQL Server, Snowflake, Postgres, Databricks, …) | None (query stays at the source) | The source |
| **DirectLake** | Never auto-selected — explicit opt-in only | Lands data as Delta in OneLake | Fabric capacity |

Infeasible requests are kept safe: asking for DirectQuery on a flat file or an offline extract keeps
the derived Import mode and records a loud follow-up in the report — the engine never emits a model
that opens broken.

## What lands in the migration report

- Per-workbook coverage: *translated / total / coverage % / needs-review count*.
- Per-calc "needs review": name, original Tableau formula, category, and the concrete reason.
- Per-datasource manual follow-ups: credentials, gateway, custom-SQL review, flat-file binding,
  storage-mode confirmations.
- Per-visual fidelity punch-list.
- Status stamp: `migrated` or `migrated_with_followups` (never a silent clean report when work remains).
