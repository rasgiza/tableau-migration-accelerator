# Sample workbooks — real Tableau Public "Viz of the Day" dashboards

These are **real, public Tableau workbooks** (Tableau Public *Viz of the Day* samples) bundled
so anyone can convert something realistic on a fresh clone — no Tableau account, no exporting,
no setup. Point the converter at the whole folder:

```powershell
.\scripts\Convert-TableauToPowerBI.ps1 -Source .\sample-workbooks -Output .\out
```

| File | What it exercises |
|------|-------------------|
| `Sample-Superstore-Sales-Performance.twbx` | Classic Superstore KPIs + calcs |
| `Superstore-Sales-Dashboard.twbx` | Compact sales dashboard, multiple sheets |
| `Golf-Superstore-Performance-Overview.twbx` | Different schema + calc mix |
| `Superstore-Bar-Chart-Way.twbx` | Larger, multi‑datasource (federated) workbook |

Each run produces, per workbook, a typed **TMDL** model, calc→**DAX** (originals kept as
annotations), an openable **`.pbip`**, plus `report.json` + `summary.md`. Some calculations are
intentionally flagged for human review — that's the correct‑or‑abstain design, not a failure.

## Which folder do I use?

- **`sample/`** — the single tiny file used by the 60‑second Quick start.
- **`sample-workbooks/`** (this folder) — a gallery of real workbooks to try more realistic runs.
- **`workbooks/`** — **your own / customer files.** Drop them there; that folder is git‑ignored
  so nothing sensitive is ever committed.
