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
function Resolve-Python {
    foreach ($candidate in @(
            @{ Exe = 'py';     Args = @('-3.11') },
            @{ Exe = 'python'; Args = @() },
            @{ Exe = 'python3'; Args = @() }
        )) {
        $exe = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        try {
            $v = & $candidate.Exe @($candidate.Args + @('-c', 'import sys;print("%d.%d"%sys.version_info[:2])')) 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v.Trim().Split('.')
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
                    return [pscustomobject]@{ Exe = $candidate.Exe; Prefix = @($candidate.Args) }
                }
            }
        } catch { }
    }
    throw "Python 3.11+ is required but was not found. Install it (e.g. 'winget install Python.Python.3.11') and retry."
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
    if (-not $files) { throw "No Tableau files ($($exts -join ', ')) found in folder '$Source'." }
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
# Never let "translated" be read as "checked". Say which one this run actually did.
if s.get("numeric_verification_active"):
    ver, unver = s.get("calcs_numeric_verified", 0), s.get("calcs_numeric_unverified", 0)
    if ver + unver:
        print(f"  Numerically verified : {ver} of {ver + unver} landed calc(s) proven equal to the"
              f" Tableau formula over landed data")
        if unver:
            print(f"  ...unproven          : {unver}  (outside the oracle's evaluable subset --"
                  f" translated, not verified)")
    else:
        # Verification ran but had nothing to check: the second-compiler landed no new calcs, so
        # every measure in this model came from the deterministic pass and none was data-checked.
        # Saying "0 of 0 verified" would imply a check happened and found nothing wrong.
        print("  Numerically verified : n/a -- verification ran, but no calculation in this model")
        print("                         was in scope for it (nothing was checked against data).")
else:
    print("  Numerically verified : none -- no calculation was evaluated against data.")
    print("                         Re-run with -Verify to check translations against landed rows.")
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
