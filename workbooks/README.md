# Drop your Tableau workbooks here

This is the ready-made **input folder** for your own files. Put any Tableau
workbooks/datasources here, then run one command from the repo root.

**Accepted:** `.twb`, `.twbx`, `.tds`, `.tdsx` — a single file or many.

```powershell
# convert everything in this folder, in one deterministic batch
.\scripts\Convert-TableauToPowerBI.ps1 -Source .\workbooks -Output .\out
```

The results land in `.\out` (a typed TMDL model, calc→DAX, an openable `.pbip`,
plus `report.json` + `summary.md`). Open `out\pbip\<Name>\<Name>.pbip` in Power BI Desktop.

## Notes

- **Your files are never committed.** Everything in this folder is git-ignored
  except this README, so customer workbooks can't be pushed by accident.
- If a single `.twb` relies on a separate `.tds` datasource, keep **both in this
  folder** so calculations resolve.
- You are not required to use this folder — `-Source` accepts any path, e.g.
  `-Source C:\exports\all-workbooks`. This folder just gives you an obvious place to start.
