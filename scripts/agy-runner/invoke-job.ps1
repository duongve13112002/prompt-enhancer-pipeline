<#
.SYNOPSIS
Submits one Antigravity (agy) job to the restricted agy-runner account and waits for it.
Run from the operator's normal (non-elevated) account after setup.ps1.

.DESCRIPTION
The workspace must already exist under .tmp/agy-work/<Workspace> and hold only the files the job
needs: a copy of the relevant sources and tests, never .secrets/. The prompt is copied into
.tmp/agy-jobs, then the AgyRunner task starts. The result is .tmp/agy-work/_results/<JobId>.json
(agy's JSON reply), with .stderr.txt, and .error.txt when the wrapper refused the job.

Only one job runs at a time. A second start while one is running is ignored by the task, so the
script refuses to submit while the task is running (printing that job's id and start time).

The result is judged after it is read: Status is INCOMPLETE and the exit code is 2 when denied_actions
is not empty, the reply is empty, stderr mentions an auto-denied action or a background task, or the
workspace-relative -ExpectFile (for reviews, REVIEW.md) is missing or older than this job.
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$PromptPath,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$JobId,
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Model = 'gemini-3.8-flash-high',
    [ValidateRange(1, 110)][int]$TimeoutMinutes = 30,
    [ValidatePattern('^$|^[0-9a-f-]{36}$')][string]$Conversation = '',
    [string]$ProjectRoot = '',
    [string]$TaskName = 'AgyRunner',
    [string]$ExpectFile = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agy-lib.ps1')
Assert-AgyExpectFile -ExpectFile $ExpectFile
# $PSScriptRoot is empty inside param defaults on Windows PowerShell 5.1, so resolve here.
if (-not $ProjectRoot) { $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$jobs = Join-Path $ProjectRoot '.tmp\agy-jobs'
$results = Join-Path $ProjectRoot '.tmp\agy-work\_results'
$ws = Join-Path $ProjectRoot ".tmp\agy-work\$Workspace"
if (-not (Test-Path -LiteralPath $ws -PathType Container)) { throw "Create the workspace first: $ws" }
if (Test-Path -LiteralPath (Join-Path $ws '.secrets')) { throw 'The workspace must not contain .secrets.' }
Assert-AgyIdle -ProjectRoot $ProjectRoot -TaskName $TaskName

foreach ($suffix in '.json', '.stderr.txt', '.error.txt', '.done') {
    Remove-Item -LiteralPath (Join-Path $results "$JobId$suffix") -Force -ErrorAction SilentlyContinue
}
$promptName = "prompt-$JobId.md"
Copy-Item -LiteralPath $PromptPath -Destination (Join-Path $jobs $promptName) -Force
[ordered]@{
    id = $JobId; workspace = $Workspace; prompt_file = $promptName
    model = $Model; timeout = "${TimeoutMinutes}m"; conversation = $Conversation
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $jobs 'job.json') -Encoding utf8

$submitted = Get-Date
schtasks /run /tn $TaskName | Out-Null
$done = Join-Path $results "$JobId.done"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes + 2)
while (-not (Test-Path -LiteralPath $done) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
if (-not (Test-Path -LiteralPath $done)) { throw "No result for $JobId within $($TimeoutMinutes + 2) minutes." }

$code = (Get-Content -LiteralPath $done -Raw).Trim()
$errorFile = Join-Path $results "$JobId.error.txt"
if (Test-Path -LiteralPath $errorFile) { throw "Job refused: $(Get-Content -LiteralPath $errorFile -Raw)" }
$reply = Get-Content -LiteralPath (Join-Path $results "$JobId.json") -Raw | ConvertFrom-Json
$stderrFile = Join-Path $results "$JobId.stderr.txt"
$stderrText = ''
if (Test-Path -LiteralPath $stderrFile) { $stderrText = [string](Get-Content -LiteralPath $stderrFile -Raw) }
# The wrapper writes exit code 0 and agy status SUCCESS even after an auto-denied action or an idle wait
# on a background task, so the work is judged here (failure class F3, scripts/agy-runner/README.md).
$reasons = @(Get-AgyIncompleteReason -Reply $reply -StderrText $stderrText -WorkspacePath $ws -ExpectFile $ExpectFile -NotBefore $submitted)
$status = $reply.status
if ($reasons.Count -gt 0) { $status = 'INCOMPLETE' }
[pscustomobject]@{
    JobId          = $JobId
    ExitCode       = $code
    Status         = $status
    AgyStatus      = $reply.status
    Reasons        = ($reasons -join '; ')
    Conversation   = $reply.conversation_id
    DeniedActions  = ($reply.denied_actions | ConvertTo-Json -Compress)
    ResultPath     = Join-Path $results "$JobId.json"
}
if ($reasons.Count -gt 0) { exit 2 }
