#Requires -RunAsAdministrator
<#
.SYNOPSIS
Creates the restricted Windows account that lets Antigravity (agy) edit code and run tests
without reaching .secrets/ or the operator's profile. Windows only.

.DESCRIPTION
Run once by the machine owner in an elevated Windows PowerShell. The script is safe to rerun:
folders that already carry an explicit rule for the account are skipped, and the scheduled task
is re-registered with the password typed on that run. See scripts/agy-runner/README.md.

The script never prints, stores or logs the password; Task Scheduler keeps it for the task.
Undo everything with undo.ps1 and the same parameters.

.PARAMETER DenyAllRoots
Folders the account may neither read nor write, for example the parent folder of your projects
and any personal data folders on the same drive. Required: the right set is machine-specific.
The project itself is re-allowed read-only, and .tmp/agy-work read-write.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$DenyAllRoots,
    [string]$UserName = 'agy-runner',
    [string]$ProjectRoot = '',
    [string]$PythonHome = '',
    [string]$AgySource = (Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'),
    [string]$ToolDir = (Join-Path $env:SystemDrive 'Tools\agy'),
    [string]$TaskName = 'AgyRunner',
    [int]$TaskHours = 2
)
$ErrorActionPreference = 'Stop'
# $PSScriptRoot is empty inside param defaults on Windows PowerShell 5.1, so resolve here.
if (-not $ProjectRoot) { $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$secrets = Join-Path $ProjectRoot '.secrets'
$work = Join-Path $ProjectRoot '.tmp\agy-work'
$jobs = Join-Path $ProjectRoot '.tmp\agy-jobs'
$driveRoot = [IO.Path]::GetPathRoot($ProjectRoot)
if (-not $PythonHome) {
    # The .venv launcher needs read access to its base interpreter, which pyvenv.cfg names.
    $cfg = Get-Content (Join-Path $ProjectRoot '.venv\pyvenv.cfg') | Where-Object { $_ -match '^home\s*=' }
    if (-not $cfg) { throw 'Cannot read home= from .venv\pyvenv.cfg; pass -PythonHome.' }
    $PythonHome = ($cfg -split '=', 2)[1].Trim()
}
if (-not (Test-Path -LiteralPath $AgySource -PathType Leaf)) { throw "agy.exe not found at $AgySource; pass -AgySource." }

Write-Host '1/7 Account'
$pw = Read-Host -AsSecureString "Password for $UserName (choose one on the first run; type the current one on a rerun)"
if (-not (Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name $UserName -Password $pw -Description 'Restricted runner for Antigravity agy' `
        -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    Add-LocalGroupMember -Group 'Users' -Member $UserName
}

Write-Host '2/7 "Log on as a batch job" right (a stored-password task needs it; otherwise 0x80070569)'
$sid = ([Security.Principal.NTAccount]$UserName).Translate([Security.Principal.SecurityIdentifier]).Value
$secDir = Join-Path $ProjectRoot '.tmp\secpol'
New-Item -ItemType Directory -Force $secDir | Out-Null
try {
    $cur = Join-Path $secDir 'current.inf'
    secedit /export /cfg $cur /areas USER_RIGHTS /quiet | Out-Null
    $line = Get-Content $cur | Where-Object { $_ -match '^SeBatchLogonRight\s*=' }
    $deny = Get-Content $cur | Where-Object { $_ -match '^SeDenyBatchLogonRight\s*=' }
    if ($deny -and $deny -match [regex]::Escape($sid)) { throw "$UserName is listed in 'Deny log on as a batch job'." }
    if ($line -and $line -match [regex]::Escape("*$sid")) {
        Write-Host '    already granted'
    } else {
        $value = if ($line) { ($line -split '=', 2)[1].Trim() + ",*$sid" } else { "*$sid" }
        $new = Join-Path $secDir 'new.inf'
        @('[Unicode]', 'Unicode=yes', '[Version]', 'signature="$CHICAGO$"', 'Revision=1',
          '[Privilege Rights]', "SeBatchLogonRight = $value") | Set-Content $new -Encoding Unicode
        secedit /configure /db (Join-Path $secDir 'secedit.sdb') /cfg $new /areas USER_RIGHTS /quiet | Out-Null
        Write-Host '    granted'
    }
} finally {
    Remove-Item -LiteralPath $secDir -Recurse -Force
}

Write-Host '3/7 Folder permissions (the first run can take many minutes on large folders)'
New-Item -ItemType Directory -Force $work, $jobs | Out-Null
function Set-RunnerAce([string]$Path, [string]$Mode, [string]$Spec) {
    if (-not (Test-Path -LiteralPath $Path)) { Write-Host "    skip (missing) $Path"; return }
    $explicit = (Get-Acl -LiteralPath $Path).Access |
        Where-Object { -not $_.IsInherited -and $_.IdentityReference -match [regex]::Escape($UserName) }
    if ($explicit) { Write-Host "    already set   $Path"; return }
    Write-Host "    applying      $Path"
    icacls $Path "/$Mode" "${UserName}:$Spec" /C /Q | Out-Null
}
# Explicit rules on a closer folder take precedence over inherited ones, so the project
# grant below re-opens read access under a denied parent, and .secrets is denied again.
foreach ($root in $DenyAllRoots) { Set-RunnerAce $root 'deny' '(OI)(CI)F' }
Set-RunnerAce $driveRoot 'deny' '(W)'             # no new folders at the drive root itself
Set-RunnerAce $ProjectRoot 'grant' '(OI)(CI)RX'   # read the project ...
Set-RunnerAce $secrets 'deny' '(OI)(CI)F'         # ... except .secrets
Set-RunnerAce $work 'grant' '(OI)(CI)M'           # the only writable place
Set-RunnerAce $PythonHome 'grant' '(OI)(CI)RX'    # base interpreter of .venv

Write-Host '4/7 Protected copy of agy and the job wrapper'
New-Item -ItemType Directory -Force $ToolDir | Out-Null
Copy-Item -LiteralPath $AgySource -Destination $ToolDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'agy-job.ps1') -Destination $ToolDir -Force
# Only administrators may change these files, so the account cannot plant code the operator later runs.
icacls $ToolDir /inheritance:r /grant:r 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' `
    "${env:USERNAME}:(OI)(CI)RX" "${UserName}:(OI)(CI)RX" /Q | Out-Null

Write-Host '5/7 Scheduled task'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ToolDir\agy-job.ps1`" -ProjectRoot `"$ProjectRoot`""
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours $TaskHours) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings -User $UserName `
        -Password $plain -RunLevel Limited -Force | Out-Null
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    $plain = $null
}
# Let the operator's normal, non-elevated account start and read the task.
$operatorSid = ([Security.Principal.NTAccount]$env:USERNAME).Translate([Security.Principal.SecurityIdentifier]).Value
$svc = New-Object -ComObject Schedule.Service; $svc.Connect()
$svc.GetFolder('\').GetTask($TaskName).SetSecurityDescriptor("D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;GRGX;;;$operatorSid)", 0)

Write-Host '6/7 Check'
icacls $secrets | Select-String $UserName
icacls $work | Select-String $UserName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State

Write-Host '7/7 Done. Sign the account in to Antigravity once (and again after any admin password reset):'
Write-Host "    runas /user:$UserName `"$ToolDir\agy.exe`""
Write-Host '    In that console, select the sign-in URL with the mouse and press Enter to copy it,'
Write-Host '    open it in any browser, finish the Google sign-in, then type /exit.'
