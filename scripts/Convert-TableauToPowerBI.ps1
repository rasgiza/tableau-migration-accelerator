<#
.SYNOPSIS
    Convert a Tableau workbook/datasource into a Power BI / Microsoft Fabric
    semantic model (TMDL) and an openable PBIP project — offline, one command.

.DESCRIPTION
    Wraps the bundled tableau-migration engine so anyone can run:

        .\Convert-TableauToPowerBI.ps1 -Input .\sample\Superstore.twb

    It parses the Tableau file, rebuilds the data model as typed TMDL, translates
    the safe subset of calculations to DAX (originals preserved as annotations),
    and emits an openable .pbip. Complex logic it cannot prove (LOD / table calcs)
    is stubbed with the original formula kept, and any human decision (e.g. storage
    mode) is surfaced rather than guessed.

    No live Tableau, no Tableau Desktop, and no internet required.

.PARAMETER Input
    Path to a Tableau file (.twb / .twbx / .tds / .tdsx) OR a folder containing
    several of them (whole-estate mode).

.PARAMETER Output
    Where to write the migration bundle. Default: .\output next to this script.

.PARAMETER Scratch
    Working root for intermediate run folders. Default: the system temp dir.

.PARAMETER SkipScan
    Skip the pre-build discovery pass. Not recommended for real estates — the scan
    flags published datasources that must be fetched before a faithful build.

.PARAMETER Verify
    After the build, re-run the engine's second-compiler pass over the freshly built
    bundle with the reconciliation oracle active. The oracle re-parses BOTH the original
    Tableau formula and the candidate DAX, evaluates both over the data actually landed
    on disk, and reports which calculations were proven to agree.

    Without this switch nothing in the run is checked against data: a calculation is
    "translated" because its construct was mappable, not because its value was verified.
    The oracle only proves the subset it can evaluate, so expect some calcs to come back
    unverified — that is the honest answer, not a failure.

    Two preconditions decide whether it can report anything at all. First, rows must
    have LANDED: a workbook whose data is a bundled .hyper extract needs the optional
    Tableau Hyper API (pip install tableauhyperapi) — without it nothing is written to
    disk and there is nothing to evaluate against. Second, the oracle currently examines
    the calculations recovered by the second-compiler pass, not the ones the deterministic
    pass already translated, so a model whose calcs all translated on the first pass
    reports "nothing in scope". Both cases are stated explicitly in the summary rather
    than reported as a silent zero.

.EXAMPLE
    .\Convert-TableauToPowerBI.ps1 -Input .\sample\Superstore.twb

.EXAMPLE
    .\Convert-TableauToPowerBI.ps1 -Input C:\exports\revenue-cycle -Output C:\out\rc -Verify
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [Alias('i', 'Path', 'Input')]
    [string] $Source,

    [Parameter(Position = 1)]
    [Alias('o')]
    [string] $Output,

    [string] $Scratch,

    [switch] $SkipScan,

    [switch] $Verify
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here   # tableau-accelerator/
# Forward slashes so Join-Path resolves on Windows AND PowerShell 7 on macOS/Linux
# (backslash literals stay literal on Unix and would break Test-Path below).
$skill = Join-Path $root 'engine/skills/tableau-migration'
$migrate = Join-Path $skill 'scripts/migrate_estate.py'

if (-not (Test-Path $migrate)) {
    throw "Migration engine not found at '$migrate'. Is the 'engine/' folder present?"
}

# --- pick a Python 3.11 interpreter -----------------------------------------
# Order matters. An ACTIVE virtual environment comes first: the optional Hyper API that -Verify needs
# is installed with `pip install tableauhyperapi`, which lands it in whatever venv the customer had
# activated -- and if we then launched the `py` launcher's system interpreter instead, that install
# would be silently invisible and every numeric check would report "no rows landed". Among otherwise
# valid interpreters we then PREFER one that can actually import the Hyper API, so a customer who
# followed the README gets the verification they installed for instead of a quiet no-op.
function Resolve-Python {
    $candidates = @()
    if ($env:VIRTUAL_ENV) {
        foreach ($rel in @('Scripts/python.exe', 'bin/python')) {
            $p = Join-Path $env:VIRTUAL_ENV $rel
            if (Test-Path $p) { $candidates += @{ Exe = $p; Args = @() } }
        }
    }
    $candidates += @(
        @{ Exe = 'py';      Args = @('-3.11') },
        @{ Exe = 'python';  Args = @() },
        @{ Exe = 'python3'; Args = @() }
    )

    $valid = @()
    foreach ($candidate in $candidates) {
        $exe = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        try {
            $v = & $candidate.Exe @($candidate.Args + @('-c', 'import sys;print("%d.%d"%sys.version_info[:2])')) 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v.Trim().Split('.')
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
                    $valid += [pscustomobject]@{ Exe = $candidate.Exe; Prefix = @($candidate.Args) }
                }
            }
        } catch { }
    }
    if (-not $valid) {
        throw "Python 3.11+ is required but was not found. Install it (e.g. 'winget install Python.Python.3.11') and retry."
    }
    foreach ($v in $valid) {
        try {
            & $v.Exe @($v.Prefix + @('-c', 'import tableauhyperapi')) 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $v }
        } catch { }
    }
    return $valid[0]
}
$py = Resolve-Python
Write-Host "Using Python: $("$($py.Exe) $($py.Prefix -join ' ')".Trim())" -ForegroundColor DarkGray

function Invoke-Py {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $PyArgs)
    & $py.Exe @($py.Prefix + $PyArgs)
}

# --- resolve paths -----------------------------------------------------------
$Source = (Resolve-Path $Source).Path
if (-not $Output) { $Output = Join-Path $root 'output' }
if (-not $Scratch) { $Scratch = Join-Path ([System.IO.Path]::GetTempPath()) 'tfmig' }

# Build a clean input folder the engine can read (it takes a folder, not a file).
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runRoot = Join-Path $Scratch $stamp
$inDir = Join-Path $runRoot 'in'
$outDir = Join-Path $runRoot 'out'
New-Item -ItemType Directory -Force -Path $inDir | Out-Null

$exts = @('.twb', '.twbx', '.tds', '.tdsx')
if (Test-Path $Source -PathType Container) {
    $files = Get-ChildItem $Source -File | Where-Object { $exts -contains $_.Extension.ToLower() }
    if (-not $files) {
        # An empty folder is a setup mistake, not a crash. `sample-workbooks/` in particular
        # ships empty by design -- this repo does not redistribute other people's Tableau
        # workbooks -- so this is the first thing a new user can hit. Say what to do about it
        # and exit 2 ("refused to run"), rather than throwing a PowerShell stack trace.
        Write-Host ""
        Write-Host "[STOP] No Tableau files ($($exts -join ', ')) in '$Source'." -ForegroundColor Yellow
        Write-Host ""
        if ((Split-Path $Source -Leaf) -eq 'sample-workbooks') {
            Write-Host "  That folder ships empty on purpose: this repo does not redistribute" -ForegroundColor Gray
            Write-Host "  third-party Tableau workbooks. Fill it first --" -ForegroundColor Gray
            Write-Host "  see sample-workbooks\README.md for two ways to do that." -ForegroundColor Gray
        }
        else {
            Write-Host "  Drop a .twb / .twbx / .tds / .tdsx into that folder, or point -Source" -ForegroundColor Gray
            Write-Host "  at a single file instead." -ForegroundColor Gray
        }
        Write-Host ""
        Write-Host "  Nothing to convert? Try the bundled synthetic sample:" -ForegroundColor Gray
        Write-Host "    .\scripts\Convert-TableauToPowerBI.ps1 -Source .\sample\Superstore.twb" -ForegroundColor Gray
        Write-Host ""
        exit 2
    }
    $files | Copy-Item -Destination $inDir -Force
    Write-Host "Staged $($files.Count) Tableau file(s) from folder." -ForegroundColor DarkGray
}
else {
    if ($exts -notcontains ([System.IO.Path]::GetExtension($Source).ToLower())) {
        throw "Input '$Source' is not a Tableau file ($($exts -join ', '))."
    }
    Copy-Item $Source -Destination $inDir -Force
    Write-Host "Staged: $(Split-Path $Source -Leaf)" -ForegroundColor DarkGray

    # A .twb carries visuals; its datasource schema + calculated fields often live in a
    # sibling .tds/.tdsx. Pull in any same-named sibling datasource so measures resolve.
    $srcDir = Split-Path $Source -Parent
    $base = [System.IO.Path]::GetFileNameWithoutExtension($Source)
    foreach ($dsExt in @('.tds', '.tdsx')) {
        $sibling = Join-Path $srcDir ($base + $dsExt)
        if ((Test-Path $sibling) -and ((Resolve-Path $sibling).Path -ne $Source)) {
            Copy-Item $sibling -Destination $inDir -Force
            Write-Host "Staged sibling datasource: $(Split-Path $sibling -Leaf)" -ForegroundColor DarkGray
        }
    }
}

# --- 1) discovery scan (flags published datasources that must be fetched) ----
$steps = if ($Verify) { 3 } else { 2 }
if (-not $SkipScan) {
    Write-Host "`n[1/$steps] Scanning for datasource bindings..." -ForegroundColor Cyan
    Invoke-Py $migrate '-i' $inDir '-o' $outDir '--scan'
    $scanExit = $LASTEXITCODE
    if ($scanExit -ne 0) {
        Write-Warning "Scan reported published datasource(s) not present in the input (exit $scanExit)."
        Write-Warning "For a faithful build, export/fetch those .tds/.tdsx files and add them alongside the workbook."
        Write-Host    "See $outDir\scan.json for details." -ForegroundColor Yellow
        # continue to build anyway so the user still gets the model for what IS present
    }
}

# --- 2) build the semantic model + PBIP -------------------------------------
Write-Host "`n[2/$steps] Building semantic model + PBIP..." -ForegroundColor Cyan
Invoke-Py $migrate '-i' $inDir '-o' $outDir '--force'
$buildExit = $LASTEXITCODE

# --- 2b) opt-in numeric verification ----------------------------------------
# The reconciliation oracle needs the PRIOR build's landed CSVs and TMDL on disk to evaluate both
# sides of a translation, so it can only run as a re-run over $outDir -- which is why this is a
# second invocation rather than a flag on the build above. --second-compile bypasses the stale-output
# rebuild guard by design, so no --force is needed here.
if ($Verify) {
    Write-Host "`n[3/$steps] Verifying translations against landed data..." -ForegroundColor Cyan
    Invoke-Py $migrate '-i' $inDir '-o' $outDir '--second-compile'
    $buildExit = $LASTEXITCODE
}

# --- collect the bundle into -Output ----------------------------------------
if (Test-Path $Output) { Remove-Item $Output -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Output | Out-Null
Copy-Item (Join-Path $outDir '*') -Destination $Output -Recurse -Force

# --- report ------------------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " Conversion complete" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
$reportPath = Join-Path $Output 'report.json'
if (Test-Path $reportPath) {
    $summaryScript = @'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
s = d.get("summary", d)
def g(*k):
    for key in k:
        if key in s: return s[key]
    return "-"
# Show a counter only when it has a denominator. Printing "Measures translated: 0 / 0" for an
# estate of embedded-datasource workbooks reads as total failure when the real coverage lives in
# the workbook-calc counters -- so pick whichever counters this estate actually populated.
if s.get("datasources_total"):
    print(f"  Datasources migrated : {g('datasources_migrated')} / {g('datasources_total')}")
if s.get("measures_total"):
    print(f"  Measures translated  : {g('measures_translated')} / {g('measures_total')}")
    print(f"  Measures stubbed     : {g('measures_stubbed')}  (need review)")
if s.get("workbook_calcs_total"):
    print(f"  Workbook calcs       : {g('workbook_calcs_translated')} / {g('workbook_calcs_total')}"
          f" translated ({g('workbook_calcs_coverage_pct')}% coverage)")
    print(f"  ...flagged for review: {g('workbook_calcs_needs_review')}")
if s.get("workbooks_total"):
    # "reports bound" (not "workbooks converted") -- a datasource-only estate still emits an
    # openable .pbip, so "0 / 1 converted" would contradict the "Open in Power BI Desktop" path
    # printed just below. This counter means the same thing the definition-of-done gate reports.
    print(f"  Workbook reports bound: {g('workbooks_pbip_built')} / {g('workbooks_total')}")
print(f"  Visuals rebuilt      : {g('visuals_rebuilt')}")
# Landed rows are the PRECONDITION for numeric verification -- the oracle evaluates both sides of a
# translation over real data, so an estate that lands nothing can never be verified however many
# calcs translate. Report the precondition and its cause; an unexplained "nothing was in scope"
# leaves the reader guessing whether the check passed or never happened.
landed = s.get("data_assets_landed", 0)
not_landed = s.get("data_not_landed_reasons") or {}
_why = {
    "hyperapi_unavailable": "a .hyper extract is bundled but no Hyper reader is installed --"
                            " pip install tableauhyperapi (no Windows ARM64 build is published)",
    "no_bundled_data": "neither the source file nor a .hyper extract is bundled --"
                       " re-export from Tableau with the extract included",
    "not_a_package": "no bundled data to land (a bare .twb/.tds, or a live connection)",
}
for reason, count in sorted(not_landed.items()):
    print(f"  Data not landed      : {count} source(s) -- {_why.get(reason, reason)}")
# Never let "translated" be read as "checked". Say which one this run actually did.
if s.get("numeric_verification_active"):
    ver, unver = s.get("calcs_numeric_verified", 0), s.get("calcs_numeric_unverified", 0)
    if ver + unver:
        print(f"  Numerically verified : {ver} of {ver + unver} landed calc(s) proven equal to the"
              f" Tableau formula over landed data")
        if unver:
            print(f"  ...unproven          : {unver}  (outside the oracle's evaluable subset --"
                  f" translated, not verified)")
    elif not landed:
        # The check could not run at all. This is a different fact from "it ran and found nothing",
        # and the customer's next action is different too -- land the data, then re-run.
        print("  Numerically verified : nothing -- no rows were landed, so no calculation could be")
        print("                         evaluated against data (see 'Data not landed' above).")
    else:
        # Rows ARE on disk and the oracle ran, but no calculation was in its evaluable subset --
        # every measure here came from the deterministic pass. Saying "0 of 0 verified" would imply
        # a check happened and found nothing wrong.
        print("  Numerically verified : n/a -- rows landed, but no calculation in this model was in")
        print("                         scope for the oracle (nothing was checked against data).")
else:
    print("  Numerically verified : none -- no calculation was evaluated against data.")
    print("                         Re-run with -Verify to check translations against landed rows.")
# The static DAX read. Needs no landed data and no opt-in pass, so unlike the counters above it
# reports on EVERY run. Two separate facts, deliberately not merged: DAX the engine will reject, and
# DAX it will happily run to a wrong number.
if s.get("semantics_checked"):
    _blocking, _advisory = s.get("semantics_blocking", 0), s.get("semantics_advisory", 0)
    if _blocking:
        print(f"  Invalid DAX          : {_blocking} of {s['semantics_checked']} expression(s) will be"
              f" rejected when the measure is evaluated -- see summary.md.")
    if _advisory:
        print(f"  Suspect numbers      : {_advisory} expression(s) re-aggregate a non-additive value"
              f" (a distinct count or a ratio).")
        print("                         Valid DAX -- the model loads -- but the total is likely wrong.")
    if not _blocking and not _advisory:
        print(f"  DAX checked          : {s['semantics_checked']} expression(s), no invalid DAX and no"
              f" suspect re-aggregation found.")
# The deterministic-translation sweep: the oracle pointed at the DAX the ORDINARY build produced,
# rather than only at the second compiler's output. That is the population that matters -- most of
# every model comes from here -- so all three outcomes are printed, never just the flattering one.
_swept = s.get("sweep_checked", 0)
_disagree = s.get("sweep_disagreements", 0)
if _disagree:
    print(f"  WRONG NUMBERS        : {_disagree} translation(s) PROVEN to disagree with the original")
    print("                         Tableau formula over landed data. Do not ship these -- report.json")
    print("                         has both values and the grain they diverge at.")
if _swept:
    _proven, _unproven = s.get("sweep_verified", 0), s.get("sweep_unproven", 0)
    print(f"  Translations proven  : {_proven} of {_swept} checked against landed rows")
    if _unproven:
        print(f"  ...neither proven nor disproven: {_unproven} (the oracle could not decide)")
        for _reason, _n in sorted((s.get("sweep_unproven_reasons") or {}).items(),
                                  key=lambda kv: -kv[1])[:3]:
            print(f"       - {_n}: {_reason}")
    if not _proven and not _disagree:
        print("                         A low proven count is a limit of the checker. It is not")
        print("                         evidence the translations are wrong -- nor that they are right.")
'@
    Invoke-Py '-c' $summaryScript $reportPath
}
Write-Host "`n  Output bundle : $Output" -ForegroundColor White
Write-Host "  Semantic model: $Output\semantic_models\" -ForegroundColor White
$pbip = Get-ChildItem (Join-Path $Output 'pbip') -Filter *.pbip -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pbip) {
    Write-Host "  Open in Power BI Desktop:" -ForegroundColor White
    Write-Host "    $($pbip.FullName)" -ForegroundColor Yellow
}
$htmlReport = Join-Path $Output 'migration-report.html'
if (Test-Path $htmlReport) {
    Write-Host "  Open the migration report (double-click, opens in your browser):" -ForegroundColor White
    Write-Host "    $htmlReport" -ForegroundColor Yellow
}
Write-Host "  Also: $Output\summary.md (human summary) and $Output\report.json (machine-readable)" -ForegroundColor White

if ($buildExit -ne 0) {
    Write-Host "`n  Note: the definition-of-done gate flagged a human decision" -ForegroundColor Yellow
    Write-Host "  (e.g. storage mode Import vs DirectLake). The model + calc→DAX" -ForegroundColor Yellow
    Write-Host "  still generated; see summary.md for the exact item to resolve." -ForegroundColor Yellow
    Write-Host "  Exit code $buildExit (3 = human decision required, 2 = refused to run)." -ForegroundColor Yellow
}
# Propagate the engine's status. Exiting 0 unconditionally would hide an unconverted workbook from
# any caller that checks $LASTEXITCODE -- CI, a scheduled run, or a customer's pipeline.
exit $buildExit
