# Validates a Gemini (agy-runner) prompt before launching invoke-job.ps1 (Windows PowerShell 5.1).
# agy rejects double quotes (argument splitting) and multi-line prompts; fail before launch instead of after.
# Also: refuses while AgyRunner is busy (prints the running job), fails on any path the runner cannot read
# (prompt and notes/*.md), appends the fixed execution-rules suffix, and exits 2 when the job is INCOMPLETE.
# Review default of 20 minutes (the same-day successes took 7 to 20); pass -TimeoutMinutes for longer jobs.
# -ExpectFile defaults to REVIEW.md (a notice is printed when the default is in force); pass -ExpectFile ''
# for a launch that writes no such file, or it ends INCOMPLETE. It is checked before anything is submitted.
param(
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$PromptPath,
    [Parameter(Mandatory = $true)][string]$JobId,
    [int]$TimeoutMinutes = 20,
    [ValidatePattern('^$|^[0-9a-f-]{36}$')][string]$Conversation = '',
    [string]$ExpectFile = 'REVIEW.md',
    [string]$TaskName = 'AgyRunner',
    [string]$LintWaive = ''
)
$ErrorActionPreference = 'Stop'
# Project root = two levels above this script (tools/agent-workflow/).
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $root 'scripts\agy-runner\agy-lib.ps1')
Assert-AgyExpectFile -ExpectFile $ExpectFile
$notice = Get-AgyExpectFileNotice -ExpectFile $ExpectFile -Explicit $PSBoundParameters.ContainsKey('ExpectFile')
if ($notice) { Write-Host $notice }
Assert-AgyIdle -ProjectRoot $root -TaskName $TaskName
$prompt = [IO.File]::ReadAllText((Join-Path $root $PromptPath))
if ($prompt.Contains('"')) { throw "Prompt contains double quotes: $PromptPath" }
if ($prompt.TrimEnd("`r", "`n").Contains("`n")) { throw "Prompt is not one line: $PromptPath" }
if (-not (Test-Path -LiteralPath (Join-Path $root ".tmp\agy-work\$Workspace") -PathType Container)) { throw "Workspace missing: $Workspace" }
Assert-AgyPathLint -Root $root -Workspace $Workspace -PromptText $prompt -PromptSource $PromptPath
# Decision 581: an agy brief passes the same lint as a Claude worker brief, or names why it cannot.
if (-not $LintWaive) {
    $brief = Join-Path $root ".tmp\agy-work\$Workspace\notes\brief.md"
    if (-not (Test-Path -LiteralPath $brief)) { throw "Workspace has no notes/brief.md (or pass -LintWaive): $Workspace" }
    & (Join-Path $root '.venv\Scripts\python.exe') -B (Join-Path $root 'tools\agent-workflow\brief_lint.py') $brief
    if ($LASTEXITCODE -ne 0) { throw "Brief lint refused $brief (fix it or pass -LintWaive <reason>)" }
}
foreach ($suffix in '.done', '.json', '.stderr.txt') {
    if (Test-Path -LiteralPath (Join-Path $root ".tmp\agy-work\_results\$JobId$suffix")) { throw "Result for $JobId already exists" }
}
# invoke-job copies the prompt file, so the suffixed prompt goes to a temporary file that is removed afterwards.
$sent = Join-Path $root ".tmp\agy-jobs\suffixed-$JobId.md"
[IO.File]::WriteAllText($sent, (Add-AgyReviewSuffix -Prompt $prompt), (New-Object Text.UTF8Encoding $false))
try {
    & (Join-Path $root 'scripts\agy-runner\invoke-job.ps1') -Workspace $Workspace -PromptPath $sent -JobId $JobId -TimeoutMinutes $TimeoutMinutes -Conversation $Conversation -TaskName $TaskName -ExpectFile $ExpectFile
    $code = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $sent -Force -ErrorAction SilentlyContinue
}
if ($code) {
    $quota = Get-AgyQuotaNotice -StderrPath (Join-Path $root ".tmp\agy-work\_results\$JobId.stderr.txt")
    if ($quota) { Write-Host $quota }
    exit $code
}
