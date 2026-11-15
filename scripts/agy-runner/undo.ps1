#Requires -RunAsAdministrator
<#
.SYNOPSIS
Reverses setup.ps1 for one project: removes this project's scheduled task and its folder rules. The parts every
project shares (the local account, the protected agy copy, the -DenyAllRoots, drive-root and Python-home rules)
are removed only when no other runner task remains. Pass the same -DenyAllRoots (and other overrides) used for setup.
A task whose -ProjectRoot is another folder is reported and left as it is.

Every check runs first with terminating errors: a scheduler, folder or account read that fails stops the script
before anything is removed, because a failed read must never look like "no other runner task" (S42 fix round 1).
Safe to rerun: each removal acts only on what the checks found.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$DenyAllRoots,
    [string]$UserName = 'agy-runner',
    [string]$ProjectRoot = '',
    [string]$PythonHome = '',
    [string]$ToolDir = (Join-Path $env:SystemDrive 'Tools\agy'),
    [string]$TaskName = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agy-lib.ps1')
# $PSScriptRoot is empty inside param defaults on Windows PowerShell 5.1, so resolve here.
if (-not $ProjectRoot) { $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if (-not $TaskName) { $TaskName = Get-AgyRunnerTaskName -ProjectRoot $ProjectRoot }
Assert-AgyTaskNameAllowed -TaskName $TaskName

# ---- Checks (read-only, all before the first removal) ----
if (-not $PythonHome) {
    $cfgPath = Join-Path $ProjectRoot '.venv\pyvenv.cfg'
    if (Test-AgyPathExists -Path $cfgPath -PathType Leaf) {
        $cfg = Get-Content -LiteralPath $cfgPath -ErrorAction Stop | Where-Object { $_ -match '^home\s*=' }
        if ($cfg) { $PythonHome = ($cfg -split '=', 2)[1].Trim() }
    }
}
# @(...) on every collection read (fix round 3): PowerShell unrolls a returned array, so zero results become $null
# and one result a bare CimInstance, which in Windows PowerShell 5.1 has no Count.
$all = @(Get-AgyScheduledTaskAll)
$task = Get-AgyNamedTask -TaskName $TaskName -Tasks $all
$removeTask = $false
$bound = $null
if ($task) {
    $bound = Get-AgyTaskBoundRoot $task
    $removeTask = [bool]$bound -and (Test-AgySamePath $bound $ProjectRoot)
}
# Every runner task that stays after this run keeps the shared parts alive (runner or bound, any project).
$removed = $null
if ($removeTask) { $removed = $task }
$others = @(Get-AgyRetainingTask -Tasks $all -RemovedTask $removed)
$userExists = @(Get-LocalUser -ErrorAction Stop | Where-Object { [string]::Equals($_.Name, $UserName, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
$projectPaths = @(@($ProjectRoot, (Join-Path $ProjectRoot '.secrets'), (Join-Path $ProjectRoot '.tmp\agy-work')) |
    Where-Object { Test-AgyPathExists -Path $_ })
$sharedPaths = @(@($DenyAllRoots) + @([IO.Path]::GetPathRoot($ProjectRoot), $PythonHome) |
    Where-Object { $_ -and (Test-AgyPathExists -Path $_) })
$toolDirExists = Test-AgyPathExists -Path $ToolDir -PathType Container

# ---- Removals ----
function Remove-RunnerAce([string[]]$Paths) {
    # icacls names the account; once it is deleted no rule can be removed by name, so say so instead of failing.
    if (-not $userExists) { Write-Host "No account ${UserName}: no rule can be removed by name on $($Paths -join ', ')."; return }
    foreach ($p in $Paths) {
        icacls $p /remove:g $UserName /remove:d $UserName /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls failed with exit code $LASTEXITCODE on $p." }
    }
}

if (-not $task) {
    Write-Host "No task $TaskName."
} elseif ($removeTask) {
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
    Write-Host "Removed task $TaskName."
} else {
    Write-Host "Kept task ${TaskName}: it is bound to '$bound' (empty: not the action layout setup.ps1 writes), not to $ProjectRoot."
}
Remove-RunnerAce $projectPaths
Write-Host "Removed the $UserName rules on this project's root, .secrets and .tmp\agy-work (where present)."

if ($others.Count -gt 0) {
    foreach ($t in $others) {
        Write-Host "Kept the account $UserName, $ToolDir and the shared folder rules: still used by $(Get-AgyTaskLabel $t)."
    }
    return
}
Remove-RunnerAce $sharedPaths
if (-not $PythonHome) { Write-Host 'Python home unknown (no .venv\pyvenv.cfg): pass -PythonHome to remove its rule.' }
if ($toolDirExists) { Remove-Item -LiteralPath $ToolDir -Recurse -Force }
if ($userExists) { Remove-LocalUser -Name $UserName }
Write-Host "No runner task remains: removed $UserName, the shared folder rules and $ToolDir."
Write-Host "The 'Log on as a batch job' entry for the deleted account's SID can be left; it no longer resolves."
Write-Host "Delete C:\Users\$UserName (if it exists) in Settings > Accounts > Other users."
