# One read-only Gemini job under the user account (PM_POLICY "Review-only runs"; Windows PowerShell 5.1).
# Several may overlap, also with an AgyRunner job (probed 2026-11-06: 3 runs at once beside a runner job):
# start one background call per job. The default permission policy applies: reads inside the workspace only,
# no writes, no shell commands, so the whole result is the reply, saved to _results/<JobId>.json.
# Never add --dangerously-skip-permissions. Exits 2 when the job is INCOMPLETE; SUCCESS alone is never trusted.
param(
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$PromptPath,
    [Parameter(Mandatory = $true)][string]$JobId,
    [int]$TimeoutMinutes = 20,
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Model = 'gemini-3.8-flash-high',
    [string]$LintWaive = ''
)
$ErrorActionPreference = 'Stop'
# Project root = two levels above this script (tools/agent-workflow/).
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $root 'scripts\agy-runner\agy-lib.ps1')
$ws = Join-Path $root ".tmp\agy-work\$Workspace"
if (-not (Test-Path -LiteralPath $ws -PathType Container)) { throw "Workspace missing: $Workspace" }
$prompt = [IO.File]::ReadAllText((Join-Path $root $PromptPath))
if ($prompt.Contains('"')) { throw "Prompt contains double quotes: $PromptPath" }
if ($prompt.TrimEnd("`r", "`n").Contains("`n")) { throw "Prompt is not one line: $PromptPath" }
# Decision 581: an agy brief passes the same lint as a Claude worker brief, or names why it cannot.
if (-not $LintWaive) {
    $brief = Join-Path $ws 'notes\brief.md'
    if (-not (Test-Path -LiteralPath $brief)) { throw "Workspace has no notes/brief.md (or pass -LintWaive): $Workspace" }
    & (Join-Path $root '.venv\Scripts\python.exe') -B (Join-Path $root 'tools\agent-workflow\brief_lint.py') $brief
    if ($LASTEXITCODE -ne 0) { throw "Brief lint refused $brief (fix it or pass -LintWaive <reason>)" }
}
$results = Join-Path $root '.tmp\agy-work\_results'
foreach ($suffix in '.done', '.json', '.stderr.txt') {
    if (Test-Path -LiteralPath (Join-Path $results "$JobId$suffix")) { throw "Result for $JobId already exists" }
}
$rules = ' Rules of this run: read only files in this folder; you cannot write files or run commands; use no web tool and no subagents; put your whole result in your final reply.'
$agy = (Get-Command agy).Source
$jsonPath = Join-Path $results "$JobId.json"
$errPath = Join-Path $results "$JobId.stderr.txt"
Push-Location -LiteralPath $ws
# Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'.
$ErrorActionPreference = 'Continue'
try {
    $out = & $agy -p ($prompt.TrimEnd() + $rules) --model $Model --output-format json --print-timeout "${TimeoutMinutes}m" 2> $errPath
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
$ErrorActionPreference = 'Stop'
[IO.File]::WriteAllText($jsonPath, ($out -join "`n"), (New-Object Text.UTF8Encoding $false))
[IO.File]::WriteAllText((Join-Path $results "$JobId.done"), "exit $code`n")
$reason = $null
try { $reply = ($out -join "`n") | ConvertFrom-Json } catch { $reply = $null; $reason = 'reply is not JSON' }
if (-not $reason) {
    if ($code -ne 0) { $reason = "exit code $code" }
    elseif ($reply.status -ne 'SUCCESS') { $reason = "status $($reply.status)" }
    elseif (-not "$($reply.response)".Trim()) { $reason = 'empty reply' }
    elseif (@($reply.denied_actions).Where({ $_ }).Count -gt 0) { $reason = 'a tool call was denied' }
    # A cut reply can still report SUCCESS (caption-side finding, 2026-11-06).
    elseif ((Test-Path -LiteralPath $errPath) -and (Select-String -LiteralPath $errPath -Pattern 'partial output' -Quiet)) { $reason = 'partial output reported' }
}
if ($reason) {
    $quota = Get-AgyQuotaNotice -StderrPath $errPath
    if ($quota) { Write-Host $quota }
    Write-Host "INCOMPLETE ${JobId}: $reason (see $jsonPath)"
    exit 2
}
Write-Host "DONE $JobId -> $jsonPath"
