#Requires -RunAsAdministrator
<#
.SYNOPSIS
Creates the restricted Windows account that lets Antigravity (agy) edit code and run tests
without reaching .secrets/ or the operator's profile. Windows only.

.DESCRIPTION
Run once per project by the machine owner in an elevated Windows PowerShell. The script is safe to
rerun: a folder that already carries the needed explicit rule for the account is skipped, and this
project's scheduled task (AgyRunner-<leaf>-<hash8>, derived from the project root) is re-registered
with the password typed on that run. Before any change it stops when a task of that name is bound to
another folder, or when a folder it must grant carries an explicit Deny for the account; it never
removes a rule. The account and the tool folder are shared by every project. See README.md.

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
    [string]$TaskName = '',
    [int]$TaskHours = 2
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agy-lib.ps1')
# $PSScriptRoot is empty inside param defaults on Windows PowerShell 5.1, so resolve here.
if (-not $ProjectRoot) { $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if (-not $TaskName) { $TaskName = Get-AgyRunnerTaskName -ProjectRoot $ProjectRoot }
Assert-AgyTaskNameAllowed -TaskName $TaskName
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
if (-not (Test-AgyPathExists -Path $AgySource -PathType Leaf)) { throw "agy.exe not found at $AgySource; pass -AgySource." }

# Folder rules, in order. Explicit rules on a closer folder take precedence over inherited ones, so the project
# grant re-opens read access under a denied parent, and .secrets is denied again. FileSystemRights names match the
# icacls specs: F = FullControl, W = Write, RX = ReadAndExecute, M = Modify.
$aceSpecs = @(
    @($DenyAllRoots | ForEach-Object { @{ Path = $_; Mode = 'deny'; Spec = '(OI)(CI)F'; Rights = 'FullControl'; Inherit = $true } })
    @{ Path = $driveRoot; Mode = 'deny'; Spec = '(W)'; Rights = 'Write'; Inherit = $false }              # no new folders at the drive root
    @{ Path = $ProjectRoot; Mode = 'grant'; Spec = '(OI)(CI)RX'; Rights = 'ReadAndExecute'; Inherit = $true } # read the project ...
    @{ Path = $secrets; Mode = 'deny'; Spec = '(OI)(CI)F'; Rights = 'FullControl'; Inherit = $true }        # ... except .secrets
    @{ Path = $work; Mode = 'grant'; Spec = '(OI)(CI)M'; Rights = 'Modify'; Inherit = $true }               # the only writable place
    @{ Path = $PythonHome; Mode = 'grant'; Spec = '(OI)(CI)RX'; Rights = 'ReadAndExecute'; Inherit = $true } # base interpreter of .venv
)
# Returns the decision for one folder; throws on an explicit Deny where a grant is needed (never removes it).
function Get-RunnerAceDecision($Ace) {
    # Fail closed: an unreadable folder or ACL stops setup; only a folder that is really absent is skipped.
    if (-not (Test-AgyPathExists -Path $Ace.Path)) { return 'missing' }
    $decision = Get-AgyAceDecision -Rules @((Get-Acl -LiteralPath $Ace.Path -ErrorAction Stop).Access) -UserName $UserName `
        -Mode $Ace.Mode -Rights $Ace.Rights -Inherit $Ace.Inherit
    if ($decision -like 'blocked:*') {
        throw ("Stopped: $($Ace.Path) carries an explicit Deny rule for $UserName ($($decision.Substring(9))), so the " +
            "$($Ace.Spec) grant would not take effect. This script never removes a rule it did not create. The runner " +
            "needs Allow ReadAndExecute on the project root and Modify on .tmp\agy-work; decide how to clear the Deny " +
            "on this project's folders (keep the one on .secrets), then rerun. See README.md, 'Set up the runner for this project'.")
    }
    $decision
}

# Preflight, before any change: the task name must not belong to another folder, and no grant may be blocked.
# Every read here stops on failure, so an unreadable scheduler never looks like "no such task".
$existing = Get-AgyNamedTask -TaskName $TaskName
if ($existing) {
    $bound = Get-AgyTaskBoundRoot $existing
    if (-not $bound -or -not (Test-AgySamePath $bound $ProjectRoot)) {
        throw "Stopped: the task $TaskName is bound to '$bound' (empty: not the action layout this script writes), not to $ProjectRoot. It is left as it is; pass another -TaskName or remove that task yourself."
    }
}
foreach ($ace in $aceSpecs) { Get-RunnerAceDecision $ace | Out-Null }
# Enumerated, not looked up by name: Get-LocalUser -Name reports a missing account as an error.
$userExists = @(Get-LocalUser -ErrorAction Stop | Where-Object { [string]::Equals($_.Name, $UserName, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0

Write-Host '1/7 Account'
$pw = Read-Host -AsSecureString "Password for $UserName (choose one on the first run; type the current one on a rerun)"
if (-not $userExists) {
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
    if ($LASTEXITCODE -ne 0) { throw "secedit /export failed with exit code $LASTEXITCODE." }
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
        if ($LASTEXITCODE -ne 0) { throw "secedit /configure failed with exit code $LASTEXITCODE." }
        Write-Host '    granted'
    }
} finally {
    Remove-Item -LiteralPath $secDir -Recurse -Force
}

Write-Host '3/7 Folder permissions (the first run can take many minutes on large folders)'
New-Item -ItemType Directory -Force $work, $jobs | Out-Null
# Re-evaluated here: the folders created just above may carry rules of their own.
foreach ($ace in $aceSpecs) {
    $decision = Get-RunnerAceDecision $ace
    if ($decision -eq 'missing') { Write-Host "    skip (missing) $($ace.Path)"; continue }
    if ($decision -eq 'present') { Write-Host "    already set   $($ace.Path)"; continue }
    Write-Host "    applying      $($ace.Path)"
    icacls $ace.Path "/$($ace.Mode)" "${UserName}:$($ace.Spec)" /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed with exit code $LASTEXITCODE on $($ace.Path)." }
}

Write-Host '4/7 Protected copy of agy and the job wrapper'
New-Item -ItemType Directory -Force $ToolDir | Out-Null
Copy-Item -LiteralPath $AgySource -Destination $ToolDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'agy-job.ps1') -Destination $ToolDir -Force
# Only administrators may change these files, so the account cannot plant code the operator later runs.
icacls $ToolDir /inheritance:r /grant:r 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' `
    "${env:USERNAME}:(OI)(CI)RX" "${UserName}:(OI)(CI)RX" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed with exit code $LASTEXITCODE on $ToolDir." }

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
Get-AgyNamedTask -TaskName $TaskName | Select-Object TaskName, State

Write-Host '7/7 Done. Sign the account in to Antigravity once (and again after any admin password reset):'
Write-Host "    runas /user:$UserName `"$ToolDir\agy.exe`""
Write-Host '    In that console, select the sign-in URL with the mouse and press Enter to copy it,'
Write-Host '    open it in any browser, finish the Google sign-in, then type /exit.'
