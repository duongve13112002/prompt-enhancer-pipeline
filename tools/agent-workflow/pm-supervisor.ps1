# PM supervisor: watches every registered agent job and prints one line per event.
# Run under the Monitor tool; each stdout line wakes the PM. It never exits silently:
# it ends with a HEARTBEAT summary after $MaxMinutes, so the PM is woken at least that often.
# Registry: .tmp/pm-state/agents.json, an array of
#   {kind: codex|gemini|claude, id, cwd (codex), label, started (ISO), max_minutes}
# -ExitOnEvent: exit after the first poll that reports an event (used as a background job,
# because the Monitor tool cannot spawn here); the completion notice is the wake-up.
# -Companion: path of the Codex plugin's codex-companion.mjs; default $env:CODEX_COMPANION, else the newest
# version under the user's Claude plugin cache (read-only lookup). Without it, codex jobs report status_unavailable.
# CLI workers (tools/agent-workflow/cli_worker.py) are watched too, through `cli_worker.py status --json`, with the
# project .venv interpreter (-Python overrides it). Events: dead_launcher (busy, launcher process gone), stalled (busy,
# log idle for -CliStallMinutes; 0 disables), finished (was busy, now idle or error). Their registry needs no agents.json entry.
param([int]$MaxMinutes = 45, [int]$PollSeconds = 60, [int]$StallMinutes = 30, [int]$CliStallMinutes = 60, [switch]$ExitOnEvent, [string]$Companion = $env:CODEX_COMPANION, [string]$Python = '', [string]$HeartbeatFile = 'pm-supervisor.json')
$script:events = 0
$ErrorActionPreference = 'Continue'
# Project root = two levels above this script (tools/agent-workflow/).
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$state = Join-Path $root '.tmp/pm-state'
$reg = Join-Path $state 'agents.json'
$cliWorker = Join-Path $PSScriptRoot 'cli_worker.py'
if (-not $Python) {
    $venvPython = Join-Path $root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = Join-Path $root '.venv/bin/python' }
    $Python = $venvPython
}
$cliBusy = @{}
New-Item -ItemType Directory -Force -Path $state | Out-Null
# Heartbeat (hooks/pm_invariants.py rule a): this process's PID and creation time (epoch seconds) in THIS project's
# state directory, removed at exit. -HeartbeatFile must equal `supervisor_heartbeat_file` of pm_invariants_config.json.
$heartbeat = Join-Path $state (($HeartbeatFile -split '[\\/]')[-1])
try {
    $created = [DateTimeOffset]::new((Get-Process -Id $PID).StartTime).ToUnixTimeMilliseconds() / 1000.0
    $record = @{ pid = $PID; created = $created; root = $root } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($heartbeat, $record, (New-Object Text.UTF8Encoding $false))
} catch {
    "heartbeat not written: $($_.Exception.Message)"
}
function Remove-Heartbeat {
    # Only our own record: a newer supervisor may have replaced it.
    try {
        $own = Get-Content -LiteralPath $heartbeat -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($own.pid -eq $PID) { Remove-Item -LiteralPath $heartbeat -Force -ErrorAction SilentlyContinue }
    } catch {}
}
$companion = $Companion
if (-not $companion) {
    $found = Get-ChildItem -Path (Join-Path $HOME '.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs') -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Directory.Parent.Name -replace '[^0-9.].*$', '') } -Descending | Select-Object -First 1
    if ($found) { $companion = $found.FullName }
}
$results = Join-Path $root '.tmp/agy-work/_results'
$reported = @{}
$end = (Get-Date).AddMinutes($MaxMinutes)

function Emit([string]$s) { [Console]::Out.WriteLine("$(Get-Date -Format HH:mm) $s"); [Console]::Out.Flush() }
# A persistent fault is reported once, not on every poll.
function EmitOnce([string]$s) { if (-not $reported.ContainsKey($s)) { $reported[$s] = $true; Emit $s } }

function Get-CodexStatus($job) {
    # Bounded call: a hung `node status` must surface as an event, never block the loop.
    if (-not $companion) { return @{ state = 'status_unavailable' } }
    $out = Join-Path $state "status-$($job.id).out"
    $p = Start-Process -FilePath node -ArgumentList @($companion, 'status', $job.id, '--cwd', $job.cwd) `
        -RedirectStandardOutput $out -RedirectStandardError "$out.err" -NoNewWindow -PassThru
    if (-not $p.WaitForExit(45000)) { try { $p.Kill() } catch {} ; return @{ state = 'status_timeout' } }
    $text = Get-Content $out -Raw -ErrorAction SilentlyContinue
    if ($text -match "$([regex]::Escape($job.id)) \| (\w+)") {
        $log = if ($text -match 'Log: (.+)') { $Matches[1].Trim() } else { $null }
        $phase = if ($text -match 'Phase: (.+)') { $Matches[1].Trim() } else { '' }
        return @{ state = $Matches[1]; log = $log; phase = $phase }
    }
    return @{ state = 'status_unparsed' }
}

function Get-CliRows {
    # Bounded call: a hung status command must surface as an event, never block the loop. Returns $null on failure.
    if (-not (Test-Path -LiteralPath $Python)) { EmitOnce "EVENT cli_watch_unavailable python not found: $Python"; return $null }
    $out = Join-Path $state 'cli-status.out'
    $p = Start-Process -FilePath $Python -ArgumentList @('-B', "`"$cliWorker`"", 'status', '--all', '--json') `
        -RedirectStandardOutput $out -RedirectStandardError "$out.err" -NoNewWindow -PassThru
    $null = $p.Handle  # Windows PowerShell 5.1: ExitCode reads $null for a -PassThru process unless the handle is cached
    if (-not $p.WaitForExit(45000)) { try { $p.Kill() } catch {} ; EmitOnce 'EVENT cli_status_timeout'; return $null }
    if ($p.ExitCode -ne 0) { EmitOnce "EVENT cli_status_failed exit=$($p.ExitCode)"; return $null }
    try { return @(Get-Content -LiteralPath $out -Raw -Encoding UTF8 | ConvertFrom-Json | ForEach-Object { $_ }) } catch { EmitOnce "EVENT cli_status_unreadable $($_.Exception.Message)"; return $null }
}

function Check-Cli {
    $rows = Get-CliRows
    if ($null -eq $rows) { return @() }
    $summary = @()
    foreach ($w in $rows) {
        $name = $w.name
        if ($w.status -eq 'busy') {
            $cliBusy[$name] = $w.label
            $st = 'running'; $note = ''
            if ($w.launcher_alive -eq $false) { $st = 'dead_launcher'; $note = if ($w.worker_alive -eq $true) { "launcher pid $($w.launcher_pid) is gone but worker pid $($w.worker_pid) still runs; send refuses until it ends" } elseif ($w.worker_state -eq 'unknown') { "launcher pid $($w.launcher_pid) is gone but worker pid $($w.worker_pid) cannot be verified; send refuses until --pid-checked or retire --force" } else { "launcher pid $($w.launcher_pid) is gone; cli_worker.py send resumes it" } }
            elseif ($CliStallMinutes -gt 0 -and $null -ne $w.log_idle_minutes -and $w.log_idle_minutes -ge $CliStallMinutes) { $st = 'stalled'; $note = "log idle $($w.log_idle_minutes)m" }
            $summary += "$name=$st"
            $key = "cli:${name}:$($w.current_log)|$st"
            if ($st -ne 'running' -and -not $reported.ContainsKey($key)) {
                $reported[$key] = $true
                $script:events++
                Emit "EVENT $st cli $name [$($w.label)] $note"
            }
        } elseif ($cliBusy.ContainsKey($name)) {
            $cliBusy.Remove($name)
            $script:events++
            Emit "EVENT finished cli $name [$($w.label)] status=$($w.status)"
        }
    }
    return $summary
}

function Check-Agents {
    $jobs = @()
    # PowerShell 5.1 emits a JSON array as one object; ForEach-Object unrolls it.
    if (-not (Test-Path -LiteralPath $reg)) { return @() }
    try { $jobs = @(Get-Content $reg -Raw -Encoding UTF8 | ConvertFrom-Json | ForEach-Object { $_ }) } catch { Emit "REGISTRY_ERROR $($_.Exception.Message)"; return @() }
    $summary = @()
    foreach ($j in $jobs) {
        $age = [int]((Get-Date) - [datetime]$j.started).TotalMinutes
        $key = "$($j.kind):$($j.id)"
        $st = 'running'; $note = ''
        if ($j.kind -eq 'codex') {
            $s = Get-CodexStatus $j
            # Only completed, failed and cancelled are terminal; queued, running, verifying,
            # finalizing and similar phases count as running.
            $st = if ($s.state -match '^(completed|failed|cancelled|status_\w+)$') { $s.state } else { 'running' }
            # A job whose phase is `done` has written its result even if its state lags.
            if ($st -eq 'running' -and $s.phase -eq 'done') { $st = 'completed' }
            # Codex reports failed during a transient reconnect while the turn continues
            # (2026-10-08 N4): a failed job with a log written in the last 5 min is still running.
            if ($st -eq 'failed' -and $s.log -and (Test-Path $s.log) -and
                ((Get-Date) - (Get-Item $s.log).LastWriteTime).TotalMinutes -lt 5) {
                $st = 'running'; $note = 'failed state but log active'
            }
            if ($st -eq 'running' -and $s.log -and (Test-Path $s.log)) {
                $idle = [int]((Get-Date) - (Get-Item $s.log).LastWriteTime).TotalMinutes
                if ($idle -ge $StallMinutes) { $st = 'stalled'; $note = "log idle ${idle}m" }
            }
            if ($s.phase) { $note = ($note + " phase=$($s.phase)").Trim() }
        } elseif ($j.kind -eq 'gemini') {
            if (Test-Path (Join-Path $results "$($j.id).done")) {
                $st = 'completed'
                try {
                    $r = Get-Content (Join-Path $results "$($j.id).json") -Raw -Encoding UTF8 | ConvertFrom-Json
                    if (-not $r.response) { $st = 'empty_response' }
                    if ($r.denied_actions) { $note = "denied=$(@($r.denied_actions).Count)" }
                } catch { $st = 'result_unreadable' }
            }
        } else {
            $st = 'claude(notified natively)'
        }
        if ($j.max_minutes -and $age -gt $j.max_minutes -and $st -match 'running') { $st = 'overtime' }
        $summary += "$($j.label)=$st/${age}m"
        if ($st -notmatch '^running|^claude' -and -not $reported.ContainsKey("$key|$st")) {
            $reported["$key|$st"] = $true
            $script:events++
            Emit "EVENT $st $($j.kind) $($j.id) [$($j.label)] $note"
        }
    }
    return $summary
}

function Check-Once { return @(Check-Agents) + @(Check-Cli) }

Emit "START supervisor ($MaxMinutes min)"
try {
    while ((Get-Date) -lt $end) {
        $sum = Check-Once
        if ($ExitOnEvent -and $script:events -gt 0) { Emit ("STATE " + ($sum -join '; ')); exit 0 }
        Start-Sleep -Seconds $PollSeconds
    }
    $sum = Check-Once
    Emit ("HEARTBEAT " + ($sum -join '; ') + " | re-arm supervisor, fill free slots from .tmp/pm-state/queue.md")
} finally {
    Remove-Heartbeat
}
exit 0
