# Runs delta tests of an integration worktree through the test slot limiter.
# Usage: int_delta_tests.ps1 -Worktree <name under .tmp/worktrees> -Tests <test paths and pytest options...>
# Each -Tests item is one pytest argument, taken as given (a path may contain spaces). An item is a path
# candidate unless it is an option or the value of a value-taking option (-k, -m, -o, -p, -c,
# --override-ini, --deselect, --rootdir, --basetemp; the --opt=value form is a single option). Every
# candidate must exist ("::node" suffix ignored). Exit 5 (no tests ran) is a failure.
param([Parameter(Mandatory = $true)][string]$Worktree, [string[]]$Tests)
# Project root = two levels above this script (tools/agent-workflow/).
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path -replace '\\', '/'
$py = if ($IsLinux -or $IsMacOS) { "$root/.venv/bin/python" } else { "$root/.venv/Scripts/python.exe" }
$wt = "$root/.tmp/worktrees/$Worktree"

# Validate everything before any environment change or pytest start.
if (-not (Test-Path -LiteralPath $wt -PathType Container)) { [Console]::Error.WriteLine("worktree not found: $wt"); exit 2 }
$pytestArgs = @($Tests | Where-Object { $_ })
$valueOptions = @('-k', '-m', '-o', '-p', '-c', '--override-ini', '--deselect', '--rootdir', '--basetemp')
$paths = @()
$skipNext = $false
foreach ($a in $pytestArgs) {
    if ($skipNext) { $skipNext = $false; continue }
    if ($a.StartsWith('-')) { if ($valueOptions -ccontains $a) { $skipNext = $true }; continue }
    $paths += $a
}
if ($paths.Count -eq 0) { [Console]::Error.WriteLine('no test path given in -Tests'); exit 2 }
$missing = @()
foreach ($p in $paths) {
    $file = $p -replace '::.*$', ''
    $full = if ([System.IO.Path]::IsPathRooted($file)) { $file } else { "$wt/$file" }
    if (-not (Test-Path -LiteralPath $full)) { $missing += $p }
}
if ($missing.Count -gt 0) { [Console]::Error.WriteLine("missing test path(s): $($missing -join ', ')"); exit 2 }
if (-not (Test-Path -LiteralPath $py)) { [Console]::Error.WriteLine("interpreter not found: $py"); exit 2 }

foreach ($v in [Environment]::GetEnvironmentVariables().Keys) { if ($v -like 'ANTHROPIC_*') { [Environment]::SetEnvironmentVariable($v, $null) } }
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = "$wt/src"
$tmp = "$wt/.tmp/pm-int"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$env:TEMP = $tmp; $env:TMP = $tmp
$code = 1
Push-Location $wt
try {
    $out = & $py -B "$root/tools/agent-workflow/test_slot.py" -- $py -m pytest -q -n 2 -p no:cacheprovider -o "pythonpath=$wt/src" --basetemp "$tmp/bt" @pytestArgs 2>&1
    $code = $LASTEXITCODE
    $out | Select-Object -Last 15
}
finally {
    Pop-Location
}
if ($code -eq 5) { [Console]::Error.WriteLine('pytest exit 5: no tests ran (counted as failure)') }
exit $code
