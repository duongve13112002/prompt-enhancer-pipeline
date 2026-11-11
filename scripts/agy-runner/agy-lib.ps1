# Helpers shared by invoke-job.ps1 and tools/agent-workflow/gemini-*.ps1 (dot-sourced, PowerShell 5.1,
# Windows only). Pure functions so tests can run them with fake inputs; nothing here starts agy, stops
# the AgyRunner task or reads outside the project. Failure classes F1-F6: scripts/agy-runner/README.md.

# The runner may read only .tmp/agy-work; any other read is auto-denied and ends the turn silently (F1).
# Rules are path-like on purpose: the bare words ".venv", ".secrets" and "/tmp" appear in the mandatory
# Rules text of prompts and task files and read nothing; a separator next to them makes a path.
# Absolute paths are drive-letter, UNC, or "/" plus an OS top-level directory or a workspace directory
# with a separator after it. Any other "/word" is an HTTP route (/ready, /v1/drain, /knowledge-v1/...) and
# is allowed: the real prompts and notes cite routes often and never an absolute path of another shape.
# Environment-variable paths expand outside the workspace: %VAR%\, $env:VAR\ and ${env:VAR}.
function Get-AgyPathRule {
    @(
        @{ Name = '.tmp path outside agy-work'; Pattern = '(?i)(?<![\w])\.tmp[/\\]+(?!agy-work(?![\w-]|\.[\w-]))' }
        @{ Name = 'drive-letter path'; Pattern = '(?<![\w])[A-Za-z]:[/\\]' }
        @{ Name = 'UNC path'; Pattern = '(?<![\w\\])\\\\[\w.$-]+' }
        @{ Name = 'absolute path'; Pattern = '(?i)(?<![\w.:/\\~-])/(?:home|Users|tmp|mnt|etc|var|usr|opt|root|proc|media|srv|bin|sbin|boot|private|Volumes|Library|Applications|src|tests|docs|tasks|configs|scripts|tools|contracts|prompts|notes|out)[/\\]' }
        @{ Name = '.venv path'; Pattern = '(?i)[/\\]\.venv(?![\w])|(?<![\w])\.venv[/\\]' }
        @{ Name = '.secrets path'; Pattern = '(?i)[/\\]\.secrets(?![\w])|(?<![\w])\.secrets[/\\]' }
        @{ Name = 'home path'; Pattern = '(?<![\w])~[\w.-]*[/\\]' }
        @{ Name = 'environment-variable path'; Pattern = '(?i)%[a-z_][\w()]*%(?=[/\\])|\$env:[\w()]+(?=[/\\])|\$\{env:[^}\s]+\}' }
    )
}

# True when a path-like token (one with a separator) has a ".." segment. A Markdown link target
# "](../x)" in a note stays inside the workspace while its leading ".." count is at most Depth (the
# levels the note sits below the workspace root, 1 for notes/) and no ".." follows; anything else
# climbs out of the workspace, also "notes/../.." and ".tmp/agy-work/../x". URLs are skipped.
function Test-AgyTraversal {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Line,
        [int]$Depth = 0
    )
    foreach ($m in [regex]::Matches($Line, '(?<link>\]\()?(?<tok>[^\s()\[\]`''",;<>|*]+)')) {
        $token = $m.Groups['tok'].Value
        if ($token.Contains('://')) { continue }
        $segments = @(($token -split '#')[0] -split '[/\\]')
        if (($segments.Count -lt 2) -or ($segments -notcontains '..')) { continue }
        $lead = 0
        while (($lead -lt $segments.Count) -and ($segments[$lead] -eq '..')) { $lead++ }
        $rest = @($segments | Select-Object -Skip $lead)
        if ($m.Groups['link'].Success -and ($lead -le $Depth) -and ($rest -notcontains '..')) { continue }
        return $true
    }
    $false
}

function Find-AgyPathViolation {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$Source,
        [int]$Depth = 0
    )
    $rules = Get-AgyPathRule
    $lines = $Text -split "\r?\n"
    $found = @()
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $names = @($rules | Where-Object { $lines[$i] -match $_.Pattern } | ForEach-Object { $_.Name })
        if (Test-AgyTraversal -Line $lines[$i] -Depth $Depth) { $names += 'parent-directory path' }
        if ($names.Count -eq 0) { continue }
        $shown = $lines[$i].Trim()
        if ($shown.Length -gt 200) { $shown = $shown.Substring(0, 200) + '...' }
        foreach ($name in $names) { $found += "${Source}:$($i + 1): ${name}: $shown" }
    }
    $found
}

# Fail closed: one list of every offending line in the prompt (Depth 0, relative to the workspace root)
# and in notes/*.md of the workspace (Depth 1). Extra notes are limited to .md, so this covers them all.
function Assert-AgyPathLint {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Workspace,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$PromptText,
        [string]$PromptSource = 'prompt'
    )
    $hits = @(Find-AgyPathViolation -Text $PromptText -Source $PromptSource)
    $notes = Join-Path $Root ".tmp\agy-work\$Workspace\notes"
    if (Test-Path -LiteralPath $notes -PathType Container) {
        foreach ($file in Get-ChildItem -LiteralPath $notes -Filter '*.md' -File) {
            $hits += @(Find-AgyPathViolation -Text ([IO.File]::ReadAllText($file.FullName)) -Source "notes/$($file.Name)" -Depth 1)
        }
    }
    if ($hits.Count -gt 0) {
        throw ("Path lint failed (the runner reads only .tmp/agy-work; copy files into notes/ and cite notes/<name>):`n" + ($hits -join "`n"))
    }
}

# F2: a source file for notes/ must be a Markdown file (the only notes the path lint reads) that is a plain
# file inside the project root, reached without links, and with no path segment named like a directory
# that holds secrets, models, the environment or git data (at any depth, e.g. a worktree's .secrets).
function Resolve-AgyExtraNote {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidate = $Path
    if (-not [IO.Path]::IsPathRooted($Path)) { $candidate = Join-Path $rootFull $Path }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "Extra note not found: $Path" }
    $full = [IO.Path]::GetFullPath($candidate)
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Extra note is outside the project root: $Path" }
    foreach ($segment in ($full.Substring($prefix.Length) -split '[/\\]')) {
        $name = $segment.TrimEnd('.', ' ')
        if ($name -in '.secrets', 'models', '.venv', '.git') { throw "Extra note is under a protected directory ($name): $Path" }
    }
    if ([IO.Path]::GetExtension($full) -ne '.md') { throw "Extra note must be a .md file (only notes/*.md are path-linted): $Path" }
    $walk = $full
    while ($walk.Length -gt $rootFull.Length) {
        if ((Get-Item -LiteralPath $walk -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Extra note path goes through a link or junction: $walk" }
        $walk = Split-Path -Parent $walk
    }
    $full
}

# F4: fixed English suffix for every review prompt; one line and no double quotes, like the prompt itself.
function Get-AgyReviewSuffix {
    'Execution rules: run no background or detached commands; run tests only as one foreground command that ends before you answer; never end a turn while waiting for anything, finish the work in this turn.'
}

function Add-AgyReviewSuffix {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Prompt)
    $suffix = Get-AgyReviewSuffix
    $body = $Prompt.TrimEnd("`r", "`n")
    if ($body.Contains($suffix)) { return $body }
    "$body $suffix"
}

# F6: the message for a busy task, or $null when it is idle. Job id comes from the project job marker,
# the start time from the task's last run time. Never stops the task.
function Get-AgyBusyMessage {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$State,
        [Parameter(Mandatory = $true)][string]$JobJsonPath,
        $LastRunTime = $null,
        [string]$TaskName = 'AgyRunner'
    )
    if ($State -ne 'Running') { return $null }
    $id = 'unknown'
    $written = $null
    if (Test-Path -LiteralPath $JobJsonPath -PathType Leaf) {
        $written = (Get-Item -LiteralPath $JobJsonPath).LastWriteTime
        try {
            $job = Get-Content -LiteralPath $JobJsonPath -Raw | ConvertFrom-Json
            if ($job.id) { $id = [string]$job.id }
        } catch { $id = 'unknown (job.json unreadable)' }
    }
    $started = 'unknown'
    if (($LastRunTime -is [datetime]) -and $LastRunTime.Year -ge 2000) {
        $started = $LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')
    } elseif ($written) {
        $started = $written.ToString('yyyy-MM-dd HH:mm:ss') + ' (job.json written)'
    }
    "$TaskName is running job '$id' (started $started). Refusing to start another job; wait for it to end. This script does not stop the task."
}

function Assert-AgyIdle {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$TaskName = 'AgyRunner'
    )
    $task = Get-ScheduledTask -TaskName $TaskName
    if ([string]$task.State -ne 'Running') { return }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    throw (Get-AgyBusyMessage -State ([string]$task.State) -JobJsonPath (Join-Path $ProjectRoot '.tmp\agy-jobs\job.json') -LastRunTime $info.LastRunTime -TaskName $TaskName)
}

# A 429 names only the wait ("Resets in 4h2m59s"), so the window is inferred from it: a wait of at most
# $ShortWindowHours is the 5-hour window, a longer one the weekly window. $null when no quota error is in the file.
function Get-AgyQuotaNotice {
    param(
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [datetime]$Now = (Get-Date),
        [int]$ShortWindowHours = 5
    )
    if (-not (Test-Path -LiteralPath $StderrPath)) { return $null }
    # [string] cast: an empty file yields no object, and an empty array makes -match/-notmatch filter, not test.
    $text = [string](Get-Content -LiteralPath $StderrPath -Raw) -replace '\s+', ' '
    if ($text -notmatch 'RESOURCE_EXHAUSTED|quota reached') { return $null }
    # [regex]::Match, not -notmatch: in Windows PowerShell 5.1 a successful -notmatch leaves $Matches null.
    $m = [regex]::Match($text, 'Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)(?:\.\d+)?s)?', 'IgnoreCase')
    if (-not $m.Success) { return 'QUOTA: limit reached, reset time not stated; probe before the next call' }
    $parts = foreach ($i in 1..3) { if ($m.Groups[$i].Success) { [int]$m.Groups[$i].Value } else { 0 } }
    $wait = New-TimeSpan -Hours $parts[0] -Minutes $parts[1] -Seconds $parts[2]
    $window = if ($wait.TotalHours -le $ShortWindowHours) { '5-hour window' } else { 'weekly window' }
    "QUOTA: $window (inferred from the wait), resets in $([math]::Floor($wait.TotalHours))h$($wait.Minutes)m at $($Now.Add($wait).ToString('yyyy-MM-dd HH:mm')); probe once before the next call"
}

# F3/F5: -ExpectFile is a workspace-relative path; refuse anything that could point elsewhere before a
# job is submitted (empty means no file is expected).
function Assert-AgyExpectFile {
    param([AllowEmptyString()][string]$ExpectFile = '')
    if (-not $ExpectFile) { return }
    if (($ExpectFile.IndexOfAny([IO.Path]::GetInvalidPathChars()) -ge 0) -or $ExpectFile.Contains(':') -or (($ExpectFile -split '[/\\]') -contains '..') -or [IO.Path]::IsPathRooted($ExpectFile)) {
        throw "ExpectFile must be workspace-relative without '..', a drive or a colon: $ExpectFile"
    }
}

# F5: the review default is easy to forget on a non-review launch, so say when it is in force. $null when
# the caller passed -ExpectFile (or an empty value) explicitly.
function Get-AgyExpectFileNotice {
    param([AllowEmptyString()][string]$ExpectFile = '', [bool]$Explicit = $false)
    if ($Explicit -or -not $ExpectFile) { return $null }
    "Default -ExpectFile $ExpectFile is in force: the job is INCOMPLETE (exit 2) unless the workspace holds a fresh $ExpectFile. Pass -ExpectFile '' for a launch that writes no such file."
}

# F3: why a job that wrote a result still did not finish its work. Empty array means complete.
# ExpectFile is workspace-relative and must be newer than NotBefore, so a leftover file of an earlier run
# does not pass.
function Get-AgyIncompleteReason {
    param(
        $Reply,
        [string]$StderrText = '',
        [string]$WorkspacePath = '',
        [string]$ExpectFile = '',
        [datetime]$NotBefore = [datetime]::MinValue
    )
    $reasons = @()
    if ($null -eq $Reply) { $reasons += 'no reply object' }
    $denied = @($Reply.denied_actions | Where-Object { $null -ne $_ })
    if ($denied.Count -gt 0) { $reasons += ('denied_actions: ' + (ConvertTo-Json -InputObject $denied -Compress)) }
    if ([string]::IsNullOrWhiteSpace([string]$Reply.response)) { $reasons += 'response is empty' }
    if ($StderrText -match '(?i)auto-denied|background task') { $reasons += 'stderr reports an auto-denied action or a background task' }
    if ($StderrText -match '(?i)partial output') { $reasons += 'stderr reports partial output' }
    if ($ExpectFile) {
        Assert-AgyExpectFile -ExpectFile $ExpectFile
        $expected = Join-Path $WorkspacePath $ExpectFile
        if (-not (Test-Path -LiteralPath $expected -PathType Leaf)) {
            $reasons += "expected file missing: $ExpectFile"
        } elseif ((Get-Item -LiteralPath $expected).LastWriteTime -lt $NotBefore) {
            $reasons += "expected file is older than this job (stale): $ExpectFile"
        }
    }
    $reasons
}
