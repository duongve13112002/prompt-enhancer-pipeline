#Requires -RunAsAdministrator
<#
.SYNOPSIS
Updates Antigravity (agy) for the operator (global install) and refreshes the protected copy
that the restricted agy-runner account uses (local copy). Windows only.

.DESCRIPTION
Run by the machine owner in an elevated Windows PowerShell, from the same Windows account that
installed agy (elevation keeps that account's profile, so the global install is the operator's).
1. Refuses while an agy-runner job or any agy process is running.
2. Global: runs `agy update` on the operator's install (default %LOCALAPPDATA%\agy\bin\agy.exe).
3. Local: copies the updated agy.exe and the repository's agy-job.ps1 into the protected tool
   folder (default C:\Tools\agy) and reapplies its admin-only ACL, as setup.ps1 does.
The account, its password, its Antigravity sign-in, folder rules and the scheduled task are not
touched, so no password is asked. Rerun setup.ps1 only to change those.
#>
param(
    [string]$AgySource = (Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'),
    [string]$ToolDir = (Join-Path $env:SystemDrive 'Tools\agy'),
    [string]$UserName = 'agy-runner',
    [string]$TaskName = 'AgyRunner'
)
$ErrorActionPreference = 'Stop'

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '(missing)' }
    # An earlier run could leave a file unreadable; the copy and /reset below repair it.
    try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash } catch { return '(unreadable)' }
}

if (-not (Test-Path -LiteralPath $AgySource -PathType Leaf)) { throw "agy.exe not found at $AgySource; pass -AgySource." }
if (-not (Test-Path -LiteralPath $ToolDir -PathType Container)) { throw "Tool folder $ToolDir is missing; run setup.ps1 first." }

Write-Host '1/4 Idle check'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') { throw "Task $TaskName is running a job; wait for it to finish." }
$running = Get-Process -Name 'agy' -ErrorAction SilentlyContinue
if ($running) { throw "agy is running (PID $($running.Id -join ', ')); close it first." }

$globalBefore = Get-Sha256 $AgySource
$localBefore = Get-Sha256 (Join-Path $ToolDir 'agy.exe')

Write-Host "2/4 Global update: $AgySource"
& $AgySource update
if ($LASTEXITCODE -ne 0) { throw "agy update failed with exit code $LASTEXITCODE." }
$globalAfter = Get-Sha256 $AgySource
if ($globalAfter -eq $globalBefore) { Write-Host '    already up to date' } else { Write-Host '    updated' }

Write-Host "3/4 Local copy: $ToolDir"
$wrapper = Join-Path $PSScriptRoot 'agy-job.ps1'
# Same folder rule as setup.ps1: only administrators may change these files. The files take
# the folder's inheritable rules (/reset); folder-only flags such as (OI)(CI) must never be
# granted on a file, which leaves it with an unreadable ACL. Reset before the copy (so an
# unreadable file from an earlier run can be overwritten) and again after it.
function Set-ToolAcl {
    icacls $ToolDir /inheritance:r /grant:r 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' `
        "${env:USERNAME}:(OI)(CI)RX" "${UserName}:(OI)(CI)RX" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls (folder) failed with exit code $LASTEXITCODE." }
    if (Get-ChildItem -LiteralPath $ToolDir -File) {
        icacls (Join-Path $ToolDir '*') /reset /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls (files) failed with exit code $LASTEXITCODE." }
    }
}
Set-ToolAcl
Copy-Item -LiteralPath $AgySource -Destination $ToolDir -Force
Copy-Item -LiteralPath $wrapper -Destination $ToolDir -Force
Set-ToolAcl

Write-Host '4/4 Check'
$localAfter = Get-Sha256 (Join-Path $ToolDir 'agy.exe')
if ($localAfter -ne $globalAfter) { throw 'The local copy does not match the global agy.exe.' }
if ((Get-Sha256 (Join-Path $ToolDir 'agy-job.ps1')) -ne (Get-Sha256 $wrapper)) { throw 'The local agy-job.ps1 does not match the repository.' }
Write-Host "    global before $globalBefore"
Write-Host "    global after  $globalAfter"
Write-Host "    local before  $localBefore"
Write-Host "    local after   $localAfter"
if ($task) { Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize }
Write-Host 'Done. The agy-runner sign-in is kept; if a job later reports a sign-in error, run:'
Write-Host "    runas /user:$UserName `"$ToolDir\agy.exe`""
