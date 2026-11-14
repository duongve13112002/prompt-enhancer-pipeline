# Builds a Gemini workspace .tmp/agy-work/<Workspace> from a committed head: the tracked review
# directories and root files below (git archive, so no .secrets, no ignored files), plus notes/ with the task file,
# delta.diff and stat.txt for Base..Head. Fails if the workspace exists or AgyRunner is busy (Windows PowerShell 5.1).
# -ExtraNotes copies project files (earlier reviews, fix lists) into notes/ so prompts cite notes/<name> only;
# each source must be a .md file inside the project root (no links, no path segment .secrets, models, .venv, .git).
# -Sanitize rewrites every notes/*.md with agy_sanitize.py (project .venv interpreter, -Python overrides it) so the
# runner path lint passes; it changes the copies only, never the source files.
param(
    [Parameter(Mandatory = $true)][string]$Worktree,
    [Parameter(Mandatory = $true)][string]$Base,
    [Parameter(Mandatory = $true)][string]$Head,
    [Parameter(Mandatory = $true)][string]$TaskFile,
    [Parameter(Mandatory = $true)][string]$Workspace,
    [string[]]$ExtraNotes = @(),
    [switch]$Sanitize,
    [string]$Python = '',
    [string]$TaskName = 'AgyRunner',
    # For a gemini-review.ps1 job: it overlaps a runner job, so a busy AgyRunner is no reason to refuse.
    [switch]$ReadOnlyJob
)
$ErrorActionPreference = 'Stop'
# Project root = two levels above this script (tools/agent-workflow/).
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path -replace '\\', '/'
. (Join-Path $root 'scripts/agy-runner/agy-lib.ps1')
if (-not $ReadOnlyJob) { Assert-AgyIdle -ProjectRoot $root -TaskName $TaskName }
$ws ="$root/.tmp/agy-work/$Workspace"
if (Test-Path -LiteralPath $ws) { throw "Workspace exists: $ws" }
# Validate every extra note before anything is created; names must be unique and not clash with built-in notes.
$extra = @()
$taken = @('head.tar', 'delta.diff', 'stat.txt', (Split-Path $TaskFile -Leaf))
foreach ($note in $ExtraNotes) {
    $full = Resolve-AgyExtraNote -Root $root -Path $note
    $leaf = Split-Path $full -Leaf
    if ($taken -contains $leaf) { throw "Extra note name clashes in notes/: $leaf" }
    $taken += $leaf
    $extra += $full
}
if ($Sanitize) {
    if (-not $Python) {
        $Python = Join-Path $root '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $Python)) { $Python = Join-Path $root '.venv/bin/python' }
    }
    if (-not (Test-Path -LiteralPath $Python)) { throw "Python for -Sanitize not found: $Python" }
}
New-Item -ItemType Directory -Path "$ws/notes", "$ws/out" | Out-Null
$tar = "$ws/notes/head.tar"
# Explicit allowlist (never .secrets, models, .venv or .tmp); directories absent from Head are skipped.
$allow = 'src', 'tests', 'configs', 'scripts', 'tools', 'docs', 'tasks', 'contracts', 'prompts', 'AGENTS.md', 'README.md'
$tracked = @(git -C $Worktree ls-tree --name-only $Head)
$paths = @($allow | Where-Object { $tracked -contains $_ })
if ($paths.Count -eq 0) { throw 'no reviewable directories in Head' }
git -C $Worktree archive --format=tar -o $tar $Head @paths
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
# Relative archive path: Git for Windows' GNU tar, first on PATH when PowerShell starts from Git Bash, reads
# `D:` in `-f D:/...` as a remote host and fails; the Windows tar and GNU tar both accept a relative path.
Push-Location -LiteralPath $ws
try { tar -xf notes/head.tar } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'tar extract failed' }
Remove-Item -LiteralPath $tar
$utf8 = New-Object Text.UTF8Encoding $false
[IO.File]::WriteAllText("$ws/notes/delta.diff", ((git -C $Worktree diff "$Base..$Head") -join "`n") + "`n", $utf8)
[IO.File]::WriteAllText("$ws/notes/stat.txt", ((git -C $Worktree diff --stat "$Base..$Head") -join "`n") + "`n", $utf8)
[IO.File]::WriteAllText("$ws/notes/$(Split-Path $TaskFile -Leaf)", (git -C $Worktree show "${Head}:$TaskFile") -join "`n", $utf8)
foreach ($full in $extra) { Copy-Item -LiteralPath $full -Destination "$ws/notes/$(Split-Path $full -Leaf)" }
if ($Sanitize) {
    $notes = @(Get-ChildItem -LiteralPath "$ws/notes" -Filter '*.md' -File | ForEach-Object { $_.FullName })
    if ($notes.Count -gt 0) {
        & $Python -B -I (Join-Path $PSScriptRoot 'agy_sanitize.py') @notes
        if ($LASTEXITCODE -ne 0) { throw 'agy_sanitize.py failed' }
    }
}
"workspace $ws ready"
