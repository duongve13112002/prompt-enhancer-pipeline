# Helpers shared by invoke-job.ps1, setup.ps1, undo.ps1, update.ps1, tools/agent-workflow/gemini-*.ps1 and the
# runner row of tools/agent-workflow/setup_project.py (dot-sourced, PowerShell 5.1, Windows only). Pure functions
# and read-only queries so tests can run them with fake inputs; nothing here starts agy, changes or stops a task,
# or changes a rule. Failure classes F1-F7: scripts/agy-runner/README.md.

# ---- Fail-closed reads (S42 fix round 1) ------------------------------------------------------------------------
# A read that fails must never look like "absent": absence makes a launcher proceed and undo remove shared parts.
# FileSystemInfo.Attributes is -1 only when the path, a parent or the drive is not found; any other failure (access
# denied, a bad name) throws. Kept as its own function so tests can replace it with a failing double.
function Get-AgyPathAttribute {
    param([Parameter(Mandatory = $true)][string]$Path)
    [int](New-Object IO.DirectoryInfo ([IO.Path]::GetFullPath($Path))).Attributes
}

# $true or $false for a path that was read; throws when the path cannot be read. Used instead of the provider's
# path test, which returns $false on errors too.
function Test-AgyPathExists {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet('Any', 'Container', 'Leaf')][string]$PathType = 'Any'
    )
    $attr = Get-AgyPathAttribute -Path $Path
    if ($attr -eq -1) { return $false }
    $isDir = ($attr -band [int][IO.FileAttributes]::Directory) -ne 0
    switch ($PathType) { 'Container' { $isDir } 'Leaf' { -not $isDir } default { $true } }
}

# Every registered task. A scheduler read failure throws, so it never reads as "no task". Like every helper here
# that returns a list, the result is unrolled on return: callers assign it as @(Get-AgyScheduledTaskAll).
function Get-AgyScheduledTaskAll {
    @(Get-ScheduledTask -ErrorAction Stop)
}

# The task of that name from a full enumeration, or $null when the enumeration holds none (a real absence, not a
# failed read). Two tasks of one name in different folders are ambiguous, and the caller stops.
function Get-AgyNamedTask {
    param([Parameter(Mandatory = $true)][string]$TaskName, [object[]]$Tasks = $null)
    if ($null -eq $Tasks) { $Tasks = @(Get-AgyScheduledTaskAll) }
    $found = @($Tasks | Where-Object { $_ -and [string]::Equals([string]$_.TaskName, $TaskName, [StringComparison]::OrdinalIgnoreCase) })
    if ($found.Count -gt 1) {
        throw "Task name $TaskName is ambiguous: $(@($found | ForEach-Object { "$($_.TaskPath)$($_.TaskName)" }) -join ', ')."
    }
    if ($found.Count -eq 1) { return $found[0] }
    $null
}

# The runner rules' path lint: the runner may read only .tmp/agy-work; any other read is auto-denied and ends the
# turn silently (F1).
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
    if (Test-AgyPathExists -Path $notes -PathType Container) {
        foreach ($file in Get-ChildItem -LiteralPath $notes -Filter '*.md' -File -ErrorAction Stop) {
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
    if (-not (Test-AgyPathExists -Path $candidate -PathType Leaf)) { throw "Extra note not found: $Path" }
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
        if ((Get-Item -LiteralPath $walk -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Extra note path goes through a link or junction: $walk" }
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
# the start time from the task's last run time. Never stops the task. The message is always a refusal, so an
# unreadable marker is named in it (with its error) rather than stopping without the busy facts.
function Get-AgyBusyMessage {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$State,
        [Parameter(Mandatory = $true)][string]$JobJsonPath,
        $LastRunTime = $null,
        [string]$TaskName = 'The runner task'
    )
    if ($State -ne 'Running') { return $null }
    $id = 'unknown'
    $written = $null
    try {
        if (Test-AgyPathExists -Path $JobJsonPath -PathType Leaf) {
            $written = (Get-Item -LiteralPath $JobJsonPath -ErrorAction Stop).LastWriteTime
            $job = Get-Content -LiteralPath $JobJsonPath -Raw -ErrorAction Stop | ConvertFrom-Json
            if ($job.id) { $id = [string]$job.id }
        }
    } catch { $id = "unknown (job.json unreadable: $($_.Exception.Message))" }
    $started = 'unknown'
    if (($LastRunTime -is [datetime]) -and $LastRunTime.Year -ge 2000) {
        $started = $LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')
    } elseif ($written) {
        $started = $written.ToString('yyyy-MM-dd HH:mm:ss') + ' (job.json written)'
    }
    "$TaskName is running job '$id' (started $started). Refusing to start another job; wait for it to end. This script does not stop the task."
}

# ---- Runner binding (S42; per-project task is a PM decision pending user review, decision 94) ----------------
# Every project gets its own task, so setting up one project never rebinds another. This file is the single source
# of the name, the action parse, the path comparison and the rule decision: setup_project.py's runner row asks
# these functions through one PowerShell call instead of reimplementing them (fix round 1, F2).
# The legacy single task 'AgyRunner' belongs to another project: never derived, and refused as an explicit name.
$script:AgyLegacyTaskName = 'AgyRunner'

function Assert-AgyTaskNameAllowed {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    if ([string]::Equals($TaskName.Trim().TrimStart('\'), $script:AgyLegacyTaskName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The task name $($script:AgyLegacyTaskName) is reserved: it is the legacy single runner task of another project on this machine, and these scripts never use it. Omit -TaskName to use this project's own task name."
    }
}

# F3: the full path with every existing component canonical. A link or junction anywhere on the path (the path
# itself or any ancestor, chained links too) is replaced by its target and the walk starts again; each existing
# name becomes its on-disk long name (8.3 short names and case are spelling variants of one folder).
# Policy for a path that does not exist: its longest existing prefix is canonicalized and the rest is kept as
# written (GetFullPath-normalized), so two spellings of one missing path compare equal and nothing is invented.
# An unreadable component throws (fail closed). A reparse point without a link target (a cloud placeholder, for
# example) is an ordinary folder here. Returned without a trailing separator; a comparison key, not a path to open.
function Get-AgyResolvedPath {
    param([Parameter(Mandatory = $true)][string]$Path, [int]$MaxLinks = 32)
    $full = [IO.Path]::GetFullPath($Path)
    $links = 0
    while ($true) {
        $cur = [IO.Path]::GetPathRoot($full)
        $parts = @($full.Substring($cur.Length).Split([char[]]@('\', '/'), [StringSplitOptions]::RemoveEmptyEntries))
        $restart = $false
        for ($i = 0; $i -lt $parts.Count; $i++) {
            $next = [IO.Path]::Combine($cur, $parts[$i])
            $attr = Get-AgyPathAttribute -Path $next
            if ($attr -eq -1) {
                foreach ($part in @($parts | Select-Object -Skip $i)) { $cur = [IO.Path]::Combine($cur, $part) }
                break
            }
            if ($attr -band [int][IO.FileAttributes]::ReparsePoint) {
                $target = [string](@((Get-Item -LiteralPath $next -Force -ErrorAction Stop).Target) | Select-Object -First 1)
                if ($target) {
                    $links++
                    if ($links -gt $MaxLinks) { throw "More than $MaxLinks links while resolving $Path." }
                    if (-not [IO.Path]::IsPathRooted($target)) { $target = [IO.Path]::Combine($cur, $target) }
                    foreach ($part in @($parts | Select-Object -Skip ($i + 1))) { $target = [IO.Path]::Combine($target, $part) }
                    $full = [IO.Path]::GetFullPath($target)
                    $restart = $true
                    break
                }
            }
            $entry = @((New-Object IO.DirectoryInfo $cur).GetFileSystemInfos($parts[$i]))
            $name = $parts[$i]
            if ($entry.Count -eq 1) { $name = $entry[0].Name }
            $cur = [IO.Path]::Combine($cur, $name)
        }
        if (-not $restart) { return $cur.TrimEnd('\', '/') }
    }
}

function Test-AgySamePath {
    param([Parameter(Mandatory = $true)][string]$A, [Parameter(Mandatory = $true)][string]$B)
    [string]::Equals((Get-AgyResolvedPath $A), (Get-AgyResolvedPath $B), [StringComparison]::OrdinalIgnoreCase)
}

# AgyRunner-<leaf>-<hash8>. hash8 = first 8 hex of SHA-256 over UTF-8 of the upper-cased canonical root: NTFS
# compares names by upper-casing them, and the canonical root (Get-AgyResolvedPath) removes links, short names and
# case of the existing part. <leaf> = the root's last name, every character outside [A-Za-z0-9._-] replaced by '-'
# (per UTF-16 unit), lower-cased, cut to 40.
function Get-AgyRunnerTaskName {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $full = Get-AgyResolvedPath -Path $ProjectRoot
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($full.ToUpperInvariant())) } finally { $sha.Dispose() }
    $hash = -join ($bytes[0..3] | ForEach-Object { $_.ToString('x2') })
    $leaf = [regex]::Replace(($full -split '[\\/]')[-1], '[^A-Za-z0-9._-]', '-').ToLowerInvariant()
    if ($leaf.Length -gt 40) { $leaf = $leaf.Substring(0, 40) }
    "AgyRunner-$leaf-$hash"
}

# ---- Task action classification (S42 fix round 2: one classifier for every caller) ---------------------------
# The wrapper setup.ps1 installs, and the program and host options it writes before -File (protocol-bound to
# setup.ps1's action and to powershell.exe's grammar: -NoProfile takes no value, -ExecutionPolicy takes exactly the
# next argument, and everything after -File <path> goes to the script). The layouts are token sequences, compared
# token by token, so one quoted argument "-ExecutionPolicy Bypass" never equals the two arguments setup writes.
$script:AgyWrapperName = 'agy-job.ps1'
$script:AgyHostProgram = 'powershell.exe'
$script:AgyHostOptionLayouts = [string[][]]@(
    [string[]]@(),
    [string[]]@('-NoProfile'),
    [string[]]@('-ExecutionPolicy', 'Bypass'),
    [string[]]@('-NoProfile', '-ExecutionPolicy', 'Bypass')
)
# The wrapper named as a whole file name: no file-name character directly before it, and after it nothing but
# trailing dots (Windows drops them) before a non-name character. Broad on purpose (decision of fix round 2): a
# false "runner" only delays a launch or keeps shared parts, a missed one allows overlap and shared teardown.
$script:AgyWrapperMention = [regex]('(?i)(?<![\w.-])' + [regex]::Escape($script:AgyWrapperName) + '(?!\.*[\w-])')

# F4: the tokens of a task's argument string, each "quoted" (no quote inside) or bare, separated by white space.
# $null when the string is anything else: an unbalanced or embedded quote, a token glued to a quote, or a quoted
# value ending in a backslash (Windows argument splitting reads \" as an escaped quote).
function Split-AgyTaskArgument {
    param([AllowEmptyString()][string]$Arguments = '')
    $text = $Arguments.TrimEnd()
    $token = [regex]'\G\s*(?:"(?<q>[^"]*)"|(?<b>[^\s"]+))(?=\s|$)'
    $tokens = New-Object 'System.Collections.Generic.List[string]'
    $pos = 0
    while ($pos -lt $text.Length) {
        $m = $token.Match($text, $pos)
        if (-not $m.Success) { return $null }
        if ($m.Groups['q'].Success) {
            if ($m.Groups['q'].Value.EndsWith('\')) { return $null }
            $tokens.Add($m.Groups['q'].Value)
        } else { $tokens.Add($m.Groups['b'].Value) }
        $pos = $m.Index + $m.Length
    }
    , $tokens.ToArray()
}

# Drive letter plus separator, or a UNC share: not relative, drive-relative (D:x) or rooted at the current drive (\x),
# whose meaning depends on the task's working directory or drive.
function Test-AgyFullyQualifiedPath {
    param([AllowNull()][AllowEmptyString()][string]$Path = '')
    if ((-not $Path) -or ($Path.IndexOfAny([IO.Path]::GetInvalidPathChars()) -ge 0)) { return $false }
    ($Path -match '^[A-Za-z]:[\\/]') -or ($Path -match '^[\\/]{2}[^\\/]+[\\/][^\\/]')
}

function Test-AgyTokenSequence {
    param([string[]]$Actual = @(), [string[]]$Expected = @())
    if ($Actual.Count -ne $Expected.Count) { return $false }
    for ($i = 0; $i -lt $Actual.Count; $i++) {
        if (-not [string]::Equals($Actual[$i], $Expected[$i], [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    $true
}

function New-AgyActionClass {
    param([string]$Kind, [string]$Reason, $ProjectRoot = $null)
    [pscustomobject]@{ Kind = $Kind; ProjectRoot = $ProjectRoot; Reason = $Reason }
}

# Pure, the single classifier of one task action (Execute + Arguments). Kind:
#   other  - the action does not name the wrapper;
#   runner - it names the wrapper but is not the exact layout setup.ps1 writes: refused as a binding, still counted
#            by the idle check and by undo's keep-list (a bare or relative wrapper, a combined host option, ...);
#   bound  - powershell.exe <host options> -File <fully qualified ...\wrapper> -ProjectRoot <fully qualified root>,
#            nothing after it; ProjectRoot holds the root. Bound is decided only after the wrapper test, so every
#            bound action is also a runner action by construction.
function Get-AgyActionClass {
    param([AllowNull()][AllowEmptyString()][string]$Execute = '', [AllowNull()][AllowEmptyString()][string]$Arguments = '')
    if (-not ($script:AgyWrapperMention.IsMatch([string]$Execute) -or $script:AgyWrapperMention.IsMatch([string]$Arguments))) {
        return New-AgyActionClass 'other' 'the action does not name the wrapper'
    }
    if (-not [string]::Equals(([string]$Execute -split '[\\/]')[-1], $script:AgyHostProgram, [StringComparison]::OrdinalIgnoreCase)) {
        return New-AgyActionClass 'runner' "the program is '$Execute', not $($script:AgyHostProgram)"
    }
    $tokens = Split-AgyTaskArgument -Arguments $Arguments
    if ($null -eq $tokens) { return New-AgyActionClass 'runner' 'the arguments do not split into plain or quoted tokens' }
    $fileAt = -1
    for ($i = 0; $i -lt $tokens.Count; $i++) {
        if ([string]::Equals($tokens[$i], '-File', [StringComparison]::OrdinalIgnoreCase)) { $fileAt = $i; break }
    }
    if (($fileAt -lt 0) -or ($tokens.Count -ne $fileAt + 4)) {
        return New-AgyActionClass 'runner' 'expected -File <wrapper> -ProjectRoot <root> with nothing after it'
    }
    # A slice, not Select-Object -First: in Windows PowerShell 5.1 -First 0 does not give an empty result.
    [string[]]$hostTokens = @()
    if ($fileAt -gt 0) { $hostTokens = [string[]]$tokens[0..($fileAt - 1)] }
    $layoutFound = $false
    foreach ($layout in $script:AgyHostOptionLayouts) {
        if (Test-AgyTokenSequence -Actual $hostTokens -Expected $layout) { $layoutFound = $true; break }
    }
    if (-not $layoutFound) {
        return New-AgyActionClass 'runner' ("the host options before -File (" + (@($hostTokens | ForEach-Object { "[$_]" }) -join ' ') +
            ") are not a layout setup.ps1 writes")
    }
    $wrapper = $tokens[$fileAt + 1]
    $value = $tokens[$fileAt + 3]
    if (-not [string]::Equals(($wrapper -split '[\\/]')[-1], $script:AgyWrapperName, [StringComparison]::OrdinalIgnoreCase)) {
        return New-AgyActionClass 'runner' "the -File value '$wrapper' is not the wrapper"
    }
    if (-not (Test-AgyFullyQualifiedPath $wrapper)) {
        return New-AgyActionClass 'runner' "the wrapper path '$wrapper' is not fully qualified, so it depends on the task's working directory"
    }
    if (-not [string]::Equals($tokens[$fileAt + 2], '-ProjectRoot', [StringComparison]::OrdinalIgnoreCase)) {
        return New-AgyActionClass 'runner' 'the parameter after the wrapper is not -ProjectRoot'
    }
    foreach ($v in $wrapper, $value) {
        if ($v -match '(?i)(^|\s)-(ProjectRoot|File)\b') { return New-AgyActionClass 'runner' "the value '$v' holds a parameter name" }
    }
    if ((-not $value) -or $value.StartsWith('-') -or (-not (Test-AgyFullyQualifiedPath $value))) {
        return New-AgyActionClass 'runner' "the -ProjectRoot value '$value' is not a fully qualified path"
    }
    New-AgyActionClass 'bound' '' $value
}

# Pure: the class of a task over all its actions. Bound only with exactly one action and that action bound (setup.ps1
# writes one); runner when any action names the wrapper; other otherwise.
function Get-AgyTaskClass {
    param($Task)
    $classes = @(@($Task.Actions | Where-Object { $null -ne $_ }) | ForEach-Object {
        Get-AgyActionClass -Execute ([string]$_.Execute) -Arguments ([string]$_.Arguments)
    })
    if (@($classes | Where-Object { $_.Kind -ne 'other' }).Count -eq 0) { return New-AgyActionClass 'other' 'no action names the wrapper' }
    if ($classes.Count -ne 1) { return New-AgyActionClass 'runner' "the task has $($classes.Count) actions; setup.ps1 writes one" }
    $classes[0]
}

# The -ProjectRoot of a bound task, or $null for any other class.
function Get-AgyTaskBoundRoot {
    param($Task)
    $class = Get-AgyTaskClass $Task
    if ($class.Kind -eq 'bound') { return $class.ProjectRoot }
    $null
}

# Pure: why this project's runner cannot take a write job, or $null. $Task is $null when the task is missing.
function Get-AgyBindingProblem {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$TaskName,
        $Task = $null,
        [bool]$JobsDirExists = $false
    )
    $setup = "the user runs scripts/agy-runner/setup.ps1 as administrator for $ProjectRoot (scripts/agy-runner/README.md, 'Set up the runner for this project'); read-only jobs can use gemini-review.ps1 meanwhile."
    if ($null -eq $Task) { return "The runner task $TaskName for this project is missing: $setup" }
    $class = Get-AgyTaskClass $Task
    $bound = $null
    if ($class.Kind -eq 'bound') { $bound = $class.ProjectRoot }
    if (-not $bound) {
        return "The runner task $TaskName does not have the action layout setup.ps1 writes (powershell.exe -File <fully qualified tool folder wrapper> -ProjectRoot `"<root>`" and nothing else; here $($class.Reason)), so its project is unknown: it is left as it is; $setup"
    }
    if (-not (Test-AgySamePath $bound $ProjectRoot)) {
        return "The runner task $TaskName is bound to $bound, not to this project ${ProjectRoot}: it is left as it is; $setup"
    }
    if (-not $JobsDirExists) {
        return "The runner job folder .tmp\agy-jobs is missing (never set up, or removed): $setup This script never creates it, because a default ACL would break the runner boundary."
    }
    $null
}

# Read-only. Refuses before any side effect when the task is missing, bound elsewhere or the job folder is missing;
# a failed read stops with its error instead.
function Assert-AgyRunnerBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$TaskName
    )
    $task = Get-AgyNamedTask -TaskName $TaskName
    $jobs = Test-AgyPathExists -Path (Join-Path $ProjectRoot '.tmp\agy-jobs') -PathType Container
    $problem = Get-AgyBindingProblem -ProjectRoot $ProjectRoot -TaskName $TaskName -Task $task -JobsDirExists $jobs
    if ($problem) { throw $problem }
}

# Read-only: every scheduled task with a runner or bound action, any project. -Tasks reuses one enumeration.
function Get-AgyRunnerTask {
    param([object[]]$Tasks = $null)
    if ($null -eq $Tasks) { $Tasks = @(Get-AgyScheduledTaskAll) }
    @($Tasks | Where-Object { $_ -and (Get-AgyTaskClass $_).Kind -ne 'other' })
}

# Pure: undo's keep-list, the runner tasks that keep the shared parts (account, tool folder, shared rules) after this
# run. $RemovedTask is the task undo removes, $null when it keeps its own.
function Get-AgyRetainingTask {
    param([object[]]$Tasks = @(), $RemovedTask = $null)
    @(Get-AgyRunnerTask -Tasks @($Tasks) | Where-Object {
        -not ($RemovedTask -and $_.TaskName -eq $RemovedTask.TaskName -and $_.TaskPath -eq $RemovedTask.TaskPath)
    })
}

# "<name> (<root or 'project unknown'>)" for messages.
function Get-AgyTaskLabel {
    param($Task)
    $root = Get-AgyTaskBoundRoot $Task
    if (-not $root) { $root = 'project unknown' }
    "$($Task.TaskName) ($root)"
}

# Pure: the message when another project's runner task is running, or $null. One account serves every project,
# so one job at a time across all of them. $Running holds task objects (TaskName, Actions).
function Get-AgyOtherBusyMessage {
    param([object[]]$Running = @(), [Parameter(Mandatory = $true)][string]$TaskName)
    $others = @($Running | Where-Object { $_ -and $_.TaskName -ne $TaskName })
    if ($others.Count -eq 0) { return $null }
    $names = $others | ForEach-Object { Get-AgyTaskLabel $_ }
    "Another runner task is running: $($names -join ', '). The runner account takes one job at a time across projects; wait for it to end. This script does not stop the task."
}

function Assert-AgyIdle {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$TaskName
    )
    $all = @(Get-AgyScheduledTaskAll)
    $own = Get-AgyNamedTask -TaskName $TaskName -Tasks $all
    if ($own -and [string]$own.State -eq 'Running') {
        $info = Get-ScheduledTaskInfo -TaskName $own.TaskName -TaskPath $own.TaskPath -ErrorAction Stop
        throw (Get-AgyBusyMessage -State ([string]$own.State) -JobJsonPath (Join-Path $ProjectRoot '.tmp\agy-jobs\job.json') -LastRunTime $info.LastRunTime -TaskName $TaskName)
    }
    $running = @(Get-AgyRunnerTask -Tasks $all | Where-Object { [string]$_.State -eq 'Running' })
    $other = Get-AgyOtherBusyMessage -Running $running -TaskName $TaskName
    if ($other) { throw $other }
}

# Pure: what Set-RunnerAce must do for one folder. $Rules are FileSystemAccessRule-like objects (IdentityReference,
# AccessControlType, FileSystemRights, IsInherited, InheritanceFlags, PropagationFlags). Present = an explicit rule
# of the wanted type for the account that holds every needed right and applies where it must: to the folder itself
# (no InheritOnly) and, when inheritance is wanted, to all its children (ContainerInherit and ObjectInherit, no
# NoPropagateInherit). For a grant, an explicit Deny for the account blocks (an explicit Deny wins over an explicit
# Allow), and the caller stops. Never suggests removing a rule.
function Get-AgyAceDecision {
    param(
        [object[]]$Rules = @(),
        [Parameter(Mandatory = $true)][string]$UserName,
        [Parameter(Mandatory = $true)][ValidateSet('grant', 'deny')][string]$Mode,
        [Parameter(Mandatory = $true)][Security.AccessControl.FileSystemRights]$Rights,
        [bool]$Inherit = $true
    )
    $wantInherit = [Security.AccessControl.InheritanceFlags]::None
    if ($Inherit) { $wantInherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' }
    $mine = @($Rules | Where-Object {
        $id = [string]$_.IdentityReference
        (-not $_.IsInherited) -and ($id -ieq $UserName -or $id.EndsWith("\$UserName", [StringComparison]::OrdinalIgnoreCase))
    })
    $type = if ($Mode -eq 'grant') { 'Allow' } else { 'Deny' }
    if ($Mode -eq 'grant') {
        $deny = @($mine | Where-Object { [string]$_.AccessControlType -eq 'Deny' }) | Select-Object -First 1
        if ($deny) { return "blocked: explicit Deny $($deny.FileSystemRights) ($($deny.InheritanceFlags); $($deny.PropagationFlags)) for $($deny.IdentityReference)" }
    }
    foreach ($rule in $mine) {
        if ([string]$rule.AccessControlType -ne $type) { continue }
        $has = [Security.AccessControl.FileSystemRights]$rule.FileSystemRights
        $flags = [Security.AccessControl.InheritanceFlags]$rule.InheritanceFlags
        $prop = [int][Security.AccessControl.PropagationFlags]$rule.PropagationFlags
        if (($prop -band [int][Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) { continue }
        if ($Inherit -and (($prop -band [int][Security.AccessControl.PropagationFlags]::NoPropagateInherit) -ne 0)) { continue }
        if ((($has -band $Rights) -eq $Rights) -and (($flags -band $wantInherit) -eq $wantInherit)) { return 'present' }
    }
    'apply'
}

# A 429 names only the wait ("Resets in 4h2m59s"), so the window is inferred from it: a wait of at most
# $ShortWindowHours is the 5-hour window, a longer one the weekly window. $null when no quota error is in the file.
function Get-AgyQuotaNotice {
    param(
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [datetime]$Now = (Get-Date),
        [int]$ShortWindowHours = 5
    )
    if (-not (Test-AgyPathExists -Path $StderrPath)) { return $null }
    # [string] cast: an empty file yields no object, and an empty array makes -match/-notmatch filter, not test.
    $text = [string](Get-Content -LiteralPath $StderrPath -Raw -ErrorAction Stop) -replace '\s+', ' '
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
        if (-not (Test-AgyPathExists -Path $expected -PathType Leaf)) {
            $reasons += "expected file missing: $ExpectFile"
        } elseif ((Get-Item -LiteralPath $expected -ErrorAction Stop).LastWriteTime -lt $NotBefore) {
            $reasons += "expected file is older than this job (stale): $ExpectFile"
        }
    }
    $reasons
}
