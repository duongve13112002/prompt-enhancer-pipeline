# Runs one Antigravity (agy) job as the restricted agy-runner account.
# setup.ps1 installs this file read-only next to agy.exe; only the scheduled task starts it.
# The operator writes the job into .tmp/agy-jobs, which the account can read but not change.
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot
)
$ErrorActionPreference = 'Stop'

$jobsDir = Join-Path $ProjectRoot '.tmp\agy-jobs'
$workDir = Join-Path $ProjectRoot '.tmp\agy-work'
$resultsDir = Join-Path $workDir '_results'
New-Item -ItemType Directory -Force $resultsDir | Out-Null
$jobId = 'invalid-job'

try {
    $job = Get-Content (Join-Path $jobsDir 'job.json') -Raw | ConvertFrom-Json
    # Validate every field before use; nothing from the job may point outside the two folders.
    if ($job.id -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw 'invalid job id' }
    $jobId = $job.id
    if ($job.workspace -notmatch '^[A-Za-z0-9._-]{1,64}$' -or $job.workspace -eq '_results') { throw 'invalid workspace name' }
    if ($job.prompt_file -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw 'invalid prompt file name' }
    if ($job.model -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw 'invalid model' }
    if ($job.timeout -notmatch '^[0-9]{1,4}m$') { throw 'invalid timeout' }
    if ($job.conversation -and $job.conversation -notmatch '^[0-9a-f-]{36}$') { throw 'invalid conversation id' }

    $ws = Join-Path $workDir $job.workspace
    $promptPath = Join-Path $jobsDir $job.prompt_file
    if (-not (Test-Path -LiteralPath $ws -PathType Container)) { throw "workspace missing: $ws" }

    # Keep temporary files and Python side effects inside the workspace.
    $scratch = Join-Path $ws ".agy-tmp-$jobId"
    New-Item -ItemType Directory -Force $scratch | Out-Null
    $env:TMP = $scratch; $env:TEMP = $scratch; $env:TMPDIR = $scratch
    $env:PYTHONNOUSERSITE = '1'; $env:PYTHONDONTWRITEBYTECODE = '1'

    # agy's own permission rules for this account. The real boundary is the Windows account and
    # its folder permissions; these rules only stop the headless auto-deny from ending the turn
    # on file edits and commands inside the workspace.
    $agySettingsDir = Join-Path $env:USERPROFILE '.gemini\antigravity-cli'
    New-Item -ItemType Directory -Force $agySettingsDir | Out-Null
    $agySettings = [ordered]@{
        trustedWorkspaces = @($workDir)
        permissions       = [ordered]@{
            allow = @("read_file($workDir)", "write_file($workDir)", 'command(*)')
            deny  = @("read_file($(Join-Path $ProjectRoot '.secrets'))")
        }
    }
    [IO.File]::WriteAllText((Join-Path $agySettingsDir 'settings.json'), ($agySettings | ConvertTo-Json -Depth 5),
        (New-Object Text.UTF8Encoding $false))

    $agyArgs = @('-p', (Get-Content -LiteralPath $promptPath -Raw), '--model', $job.model,
        '--output-format', 'json', '--print-timeout', $job.timeout)
    if ($job.conversation) { $agyArgs += @('--conversation', $job.conversation) }

    Push-Location -LiteralPath $ws
    # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'.
    $ErrorActionPreference = 'Continue'
    try {
        $out = & (Join-Path $PSScriptRoot 'agy.exe') @agyArgs 2> (Join-Path $resultsDir "$jobId.stderr.txt")
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Set-Content -LiteralPath (Join-Path $resultsDir "$jobId.json") -Value $out -Encoding utf8
} catch {
    # Always leave a marker so the operator's wait ends with a reason instead of a timeout.
    Set-Content -LiteralPath (Join-Path $resultsDir "$jobId.error.txt") -Value $_.Exception.Message -Encoding utf8
    $code = 90
}
Set-Content -LiteralPath (Join-Path $resultsDir "$jobId.done") -Value $code -Encoding ascii
