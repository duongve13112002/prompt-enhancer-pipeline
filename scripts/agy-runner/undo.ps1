#Requires -RunAsAdministrator
<#
.SYNOPSIS
Reverses setup.ps1: removes the scheduled task, every folder rule for the account, the protected
agy copy and the local account. Pass the same -DenyAllRoots (and other overrides) used for setup.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$DenyAllRoots,
    [string]$UserName = 'agy-runner',
    [string]$ProjectRoot = '',
    [string]$PythonHome = '',
    [string]$ToolDir = (Join-Path $env:SystemDrive 'Tools\agy'),
    [string]$TaskName = 'AgyRunner'
)
$ErrorActionPreference = 'Continue'
# $PSScriptRoot is empty inside param defaults on Windows PowerShell 5.1, so resolve here.
if (-not $ProjectRoot) { $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if (-not $PythonHome) {
    $cfg = Get-Content (Join-Path $ProjectRoot '.venv\pyvenv.cfg') | Where-Object { $_ -match '^home\s*=' }
    if ($cfg) { $PythonHome = ($cfg -split '=', 2)[1].Trim() }
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$paths = @($DenyAllRoots) + @([IO.Path]::GetPathRoot($ProjectRoot), $ProjectRoot,
    (Join-Path $ProjectRoot '.secrets'), (Join-Path $ProjectRoot '.tmp\agy-work'), $PythonHome)
foreach ($p in $paths) {
    if ($p -and (Test-Path -LiteralPath $p)) { icacls $p /remove:g $UserName /remove:d $UserName /C /Q | Out-Null }
}
Remove-Item -LiteralPath $ToolDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-LocalUser -Name $UserName -ErrorAction SilentlyContinue
Write-Host "Removed $UserName, its task, folder rules and $ToolDir."
Write-Host "The 'Log on as a batch job' entry for the deleted account's SID can be left; it no longer resolves."
Write-Host "Delete C:\Users\$UserName (if it exists) in Settings > Accounts > Other users."
