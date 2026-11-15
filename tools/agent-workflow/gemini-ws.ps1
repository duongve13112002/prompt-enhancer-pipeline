# Builds a Gemini workspace .tmp/agy-work/<Workspace> for review: the tracked review directories and root files below,
# plus notes/ with the task file, delta.diff (the patch under review) and stat.txt. Three sources (T-024, decision 77:
# workers never commit, so a review usually has no head commit):
#   -Head <commit>    the tree of a commit (git archive); delta.diff = Base..Head.
#   -WorkingTree      the worktree as it is on disk: tracked plus untracked files that are not ignored; delta.diff =
#                     Base against the working tree, untracked files included (intent-to-add in a temporary index,
#                     so the worktree's own index and object store stay untouched).
#   -Patch <file>     a saved patch (.tmp/pm-state/patches/<task>.patch) applied to Base in a temporary index
#                     (`read-tree`, `apply --cached`); the changed paths, a rename's deletion included, come from
#                     `git diff --cached -z --name-status --no-renames <Base>`; delta.diff = the reviewable part.
#                     A patch touching .secrets, .git, .venv, .tmp or models is refused before anything is created.
#                     (`apply --cached` writes the patch's blobs into the worktree's object store; its index and
#                     files stay untouched.)
# Every mode refuses a -TaskFile that is not in its source (head commit, Base plus the patch, or the disk).
# One path filter (Test-Reviewable: an allowlisted top-level entry, no forbidden segment) decides every output: the
# copied or extracted files, delta.diff and stat.txt (T-024 review F5); excluded content appears in no workspace file.
# Refuses a file reached through a link or junction.
# Fails if the workspace exists or AgyRunner is busy (Windows PowerShell 5.1).
# Ownership (T-024 final F26): -Workspace is one plain folder name. A build writes everything (files, notes, its own
# temporary indexes) into a private staging folder .tmp/agy-work/.build-<Workspace>-<guid> and publishes it with one
# directory move, which fails when the name is taken: of several builds of one name exactly one wins, the others fail
# with "Workspace exists" and remove only their own staging folder. A failed build leaves no workspace at all.
# -ExtraNotes copies project files (earlier reviews, fix lists) into notes/ so prompts cite notes/<name> only;
# each source must be a .md file inside the project root (no links, no path segment .secrets, models, .venv, .git).
# -Sanitize rewrites every notes/*.md with agy_sanitize.py (project .venv interpreter, -Python overrides it) so the
# runner path lint passes; it changes the copies only, never the source files.
[CmdletBinding(DefaultParameterSetName = 'Head')]
param(
    [Parameter(Mandatory = $true)][string]$Worktree,
    [Parameter(Mandatory = $true)][string]$Base,
    [Parameter(Mandatory = $true, ParameterSetName = 'Head')][string]$Head,
    [Parameter(Mandatory = $true, ParameterSetName = 'WorkingTree')][switch]$WorkingTree,
    [Parameter(Mandatory = $true, ParameterSetName = 'Patch')][string]$Patch,
    [Parameter(Mandatory = $true)][string]$TaskFile,
    [Parameter(Mandatory = $true)][string]$Workspace,
    [string[]]$ExtraNotes = @(),
    [switch]$Sanitize,
    [string]$Python = '',
    [string]$TaskName = 'AgyRunner',
    # For a gemini-review.ps1 job: it overlaps a runner job, so a busy AgyRunner is no reason to refuse.
    [switch]$ReadOnlyJob
)
$ErrorActionPreference = 'Stop'
# Project root = two levels above this script (tools/agent-workflow/).
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path -replace '\\', '/'
. (Join-Path $root 'scripts/agy-runner/agy-lib.ps1')
if (-not $ReadOnlyJob) { Assert-AgyIdle -ProjectRoot $root -TaskName $TaskName }
# Windows reserved device names, any case, with or without an extension (protocol-bound: "Naming Files, Paths, and
# Namespaces", https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file, "Do not use the following reserved
# names for the name of a file: CON, PRN, AUX, NUL, COM1-COM9, COM1-COM3 superscripts, LPT1-LPT9, LPT superscripts ...
# Also avoid these names followed immediately by an extension; for example, NUL.txt and NUL.tar.gz are both equivalent
# to NUL"). The superscript digits (U+00B9, U+00B2, U+00B3) are written as char codes so the script stays ASCII for
# Windows PowerShell 5.1. A trailing dot or space is refused by the pattern below (the page: "Do not end a file or
# directory name with a space or a period"). Checked first so a device name gets its own message.
$superscripts = "$([char]0xB9)$([char]0xB2)$([char]0xB3)"
if ($Workspace -match "^(CON|PRN|AUX|NUL|(COM|LPT)[1-9$superscripts])(\.|\z)") { throw "Workspace is a reserved Windows device name: $Workspace" }
# One plain folder name: no separator or dot-dot can leave .tmp/agy-work, a leading dot or underscore cannot collide
# with the staging folders or `_results`, and no space can split the name in a runner command (safety-bound).
if ($Workspace -notmatch '^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9_-])?\z') { throw "Workspace must be one plain folder name (letters, digits, . _ -; starting with a letter or digit, no trailing dot): $Workspace" }
$final = "$root/.tmp/agy-work/$Workspace"
if (Test-Path -LiteralPath $final) { throw "Workspace exists: $final" }
$Worktree = (Resolve-Path -LiteralPath $Worktree).Path -replace '\\', '/'
# Validate every extra note before anything is created; names must be unique and not clash with built-in notes.
$extra = @()
$taken = @('head.tar', 'delta.diff', 'stat.txt', (Split-Path $TaskFile -Leaf))
foreach ($note in $ExtraNotes) {
    $full = Resolve-AgyExtraNote -Root $root -Path $note
    $leaf = Split-Path $full -Leaf
    if ($taken -contains $leaf) { throw "Extra note name clashes in notes/: $leaf" }
    $taken += $leaf
    $extra += $full
}
if ($Sanitize) {
    if (-not $Python) {
        $Python = Join-Path $root '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $Python)) { $Python = Join-Path $root '.venv/bin/python' }
    }
    if (-not (Test-Path -LiteralPath $Python)) { throw "Python for -Sanitize not found: $Python" }
}
# Explicit allowlist (never .secrets, models, .venv or .tmp); directories absent from the source are skipped.
$allow = 'src', 'tests', 'configs', 'scripts', 'tools', 'docs', 'tasks', 'contracts', 'prompts', 'AGENTS.md', 'README.md'
# Path segments a reviewed patch or copied file may never contain (safety-bound).
$forbidden = @('.secrets', '.git', '.venv', '.tmp')
$chunkSize = 200  # paths per git command line, far below the Windows limit (safety-bound)
$utf8 = New-Object Text.UTF8Encoding $false

function Invoke-Native {
    # Runs one native command (git, tar, the sanitizer). Success is its exit code, never its stderr: git warns on stderr
    # ("LF will be replaced by CRLF") and Windows PowerShell 5.1 turns native stderr into a terminating error under
    # $ErrorActionPreference = 'Stop' in a host without a console stderr (a job, an embedded runspace). `2>&1` with a
    # function-local 'Continue' captures stderr as diagnostics only; the caller's preference is untouched. A non-zero
    # exit throws with the command's stderr in the message, unless -AllowFailure (the caller reads $script:NativeExit).
    # Returns the stdout lines.
    param([string]$Exe, [string[]]$CmdArgs = @(), [string]$Fail = '', [switch]$AllowFailure)
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { throw "Command not found: $Exe" }
    $ErrorActionPreference = 'Continue'
    $all = @(& $Exe @CmdArgs 2>&1)
    $script:NativeExit = $LASTEXITCODE
    $errors = [Management.Automation.ErrorRecord]
    $script:NativeErr = (@($all | Where-Object { $_ -is $errors } | ForEach-Object { $_.ToString().TrimEnd() }) -join ' ').Trim()
    if ($script:NativeExit -ne 0 -and -not $AllowFailure) {
        if (-not $Fail) { $Fail = "$Exe $($CmdArgs -join ' ') failed" }
        throw "$Fail (exit $($script:NativeExit)): $($script:NativeErr)"
    }
    $all | Where-Object { $_ -isnot $errors }
}

function Test-Forbidden([string]$Rel) {
    # A path a patch may not touch at all: a forbidden segment anywhere, or the models/ tree.
    $segments = $Rel -split '/'
    [bool](($segments | Where-Object { $forbidden -contains $_ }) -or $segments[0] -eq 'models')
}

function Test-Reviewable([string]$Rel) {
    # The one filter of every workspace output: an allowlisted top-level entry and no forbidden segment.
    ($allow -contains ($Rel -split '/')[0]) -and -not (Test-Forbidden $Rel)
}

function Get-GitNames([string[]]$GitArgs) {
    # NUL-separated names printed by git with -z: no quoting, so spaces and non-ASCII names survive as they are.
    $out = @(Invoke-Native git $GitArgs)
    @((($out -join "`n") -split "`0") | Where-Object { $_ -ne '' })
}

function Get-Chunks([string[]]$Items) {
    # One list of string[] chunks, returned whole (the leading comma) so PowerShell does not unroll a single chunk.
    $list = New-Object 'System.Collections.Generic.List[string[]]'
    for ($k = 0; $k -lt $Items.Count; $k += $chunkSize) {
        $list.Add([string[]]@($Items[$k..([Math]::Min($k + $chunkSize - 1, $Items.Count - 1))]))
    }
    , $list
}

function Get-PatchPaths([string]$File) {
    # Every path a patch touches, from `git apply --numstat -z` (git-apply(1): "do not munge pathnames, but use a
    # NUL-terminated machine-readable format"). A record is `<added>\t<deleted>\t<path>`; for a rename or copy the
    # path field is empty and the old and the new name follow as their own NUL-terminated fields.
    $tokens = @(Get-GitNames @('-C', $Worktree, 'apply', '--numstat', '-z', $File))
    $paths = @()
    $k = 0
    while ($k -lt $tokens.Count) {
        $fields = $tokens[$k] -split "`t", 3
        $k++
        if ($fields.Count -ne 3) { throw "Unexpected git apply --numstat record: $($fields -join ' ')" }
        if ($fields[2] -ne '') { $paths += $fields[2]; continue }
        if ($k + 1 -ge $tokens.Count) { throw 'Truncated rename record from git apply --numstat' }
        $paths += $tokens[$k], $tokens[$k + 1]
        $k += 2
    }
    if ($paths.Count -eq 0) { throw "Not a git patch: $File" }
    $paths
}

function Set-Delta([string[]]$Range) {
    # $delta and $stat (script scope) for the reviewable changed paths only, as literal pathspecs (no glob magic).
    $names = Get-GitNames (@('-C', $Worktree, 'diff', '--name-only', '-z', '--no-renames') + $Range)
    $changed = @($names | Where-Object { Test-Reviewable $_ } | Select-Object -Unique)
    $d = @(); $s = @()
    foreach ($chunk in (Get-Chunks $changed)) {
        $diff = @('-C', $Worktree, '--literal-pathspecs', '-c', 'core.quotepath=false', 'diff')
        $d += @(Invoke-Native git ($diff + @('--binary', '--no-renames') + $Range + @('--') + $chunk) -Fail 'git diff failed')
        $s += @(Invoke-Native git ($diff + @('--stat', '--no-renames') + $Range + @('--') + $chunk) -Fail 'git diff --stat failed')
    }
    $script:delta = $d
    $script:stat = $s
}

function Export-Tree([string]$Tree) {
    # git archive of the reviewable files of $Tree (the same filter), extracted into the workspace chunk by chunk.
    $files = @(Get-GitNames @('-C', $Worktree, 'ls-tree', '-r', '-z', '--name-only', $Tree) |
        Where-Object { Test-Reviewable $_ })
    if ($files.Count -eq 0) { throw "no reviewable files in $Tree" }
    foreach ($chunk in (Get-Chunks $files)) {
        [void](Invoke-Native git (@('-C', $Worktree, '--literal-pathspecs', 'archive', '--format=tar', '-o', "$ws/notes/head.tar", $Tree) + $chunk) -Fail 'git archive failed')
        # Relative archive path: Git for Windows' GNU tar, first on PATH when PowerShell starts from Git Bash, reads
        # `D:` in `-f D:/...` as a remote host and fails; the Windows tar and GNU tar both accept a relative path.
        Push-Location -LiteralPath $ws
        try { [void](Invoke-Native tar @('-xf', 'notes/head.tar') -Fail 'tar extract failed') } finally { Pop-Location }
        Remove-Item -LiteralPath "$ws/notes/head.tar"
    }
}

function Assert-NoLink([string]$Rel) {
    # Refuse a file that is, or sits below, a symbolic link or junction: its content could come from outside.
    $here = $Worktree
    foreach ($segment in ($Rel -split '/')) {
        $here = "$here/$segment"
        $item = Get-Item -LiteralPath $here -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing a linked path: $Rel" }
    }
}

# Staging folder of this build alone (a fresh guid makes the name unique, so nothing here can belong to another build):
# $ws is the workspace under construction (published as $final), the temporary indexes sit beside it, never published.
$stage = "$root/.tmp/agy-work/.build-$Workspace-$([guid]::NewGuid().ToString('N'))"
$ws = "$stage/ws"
$patchIndex = "$stage/patch-index"
$savedIndexFile = $env:GIT_INDEX_FILE
[void][IO.Directory]::CreateDirectory("$root/.tmp/agy-work")
New-Item -ItemType Directory -Path $stage | Out-Null
try {
# git prints UTF-8 (paths with core.quotepath=false or -z, diffs as stored); read it as UTF-8 from the first git call
# on, and restore the caller's setting at the end.
$savedEncoding = [Console]::OutputEncoding
try { [Console]::OutputEncoding = $utf8 } catch { $savedEncoding = $null }
try {
try {
$changes = @()  # Patch mode: [status, path] of every path the patch changes against Base, from git itself
switch ($PSCmdlet.ParameterSetName) {
    'Head' {
        # Item 9: the task file must be in the head commit, else notes/ would get an empty copy.
        [void](Invoke-Native git @('-C', $Worktree, 'cat-file', '-e', "${Head}:$TaskFile") -AllowFailure)
        if ($script:NativeExit -ne 0) { throw "TaskFile $TaskFile is not in the head commit ${Head}: $($script:NativeErr)" }
    }
    'WorkingTree' {
        if (-not (Test-Path -LiteralPath "$Worktree/$TaskFile" -PathType Leaf)) { throw "TaskFile not found: $Worktree/$TaskFile" }
    }
    'Patch' {
        # Refuse before the workspace exists (review F6: a textual header parse missed `a b.txt`).
        $patchFull = (Resolve-Path -LiteralPath $Patch).Path
        foreach ($p in @(Get-PatchPaths $patchFull)) { if (Test-Forbidden $p) { throw "Patch touches a forbidden path: $p" } }
        # Base plus the patch in a temporary index (review R7: numstat names only the new side of a rename), so every
        # changed path, a rename's deletion included, comes from `git diff --cached --name-status --no-renames`.
        $env:GIT_INDEX_FILE = $patchIndex
        [void](Invoke-Native git @('-C', $Worktree, 'read-tree', $Base) -Fail "git read-tree $Base failed")
        [void](Invoke-Native git @('-C', $Worktree, 'apply', '--cached', '--check', $patchFull) -Fail "Patch does not apply to ${Base}: $Patch")
        [void](Invoke-Native git @('-C', $Worktree, 'apply', '--cached', $patchFull) -Fail "Patch failed to apply: $Patch")
        $tokens = @(Get-GitNames @('-C', $Worktree, 'diff', '--cached', '-z', '--name-status', '--no-renames', $Base))
        for ($k = 0; $k + 1 -lt $tokens.Count; $k += 2) { $changes += , @($tokens[$k], $tokens[$k + 1]) }
        foreach ($c in $changes) { if (Test-Forbidden $c[1]) { throw "Patch touches a forbidden path: $($c[1])" } }
        [void](Invoke-Native git @('-C', $Worktree, 'cat-file', '-e', ":$TaskFile") -AllowFailure)
        $inIndex = $script:NativeExit -eq 0
        if (-not $inIndex -and -not (Test-Path -LiteralPath "$Worktree/$TaskFile" -PathType Leaf)) {
            throw "TaskFile $TaskFile is neither in Base plus the patch nor in the worktree"
        }
        $env:GIT_INDEX_FILE = $savedIndexFile
    }
}
New-Item -ItemType Directory -Path "$ws/notes", "$ws/out" | Out-Null
switch ($PSCmdlet.ParameterSetName) {
    'Head' {
        Export-Tree $Head
        Set-Delta @("$Base..$Head")
        $task = @(Invoke-Native git @('-C', $Worktree, 'show', "${Head}:$TaskFile"))
    }
    'WorkingTree' {
        $listed = Get-GitNames @('-C', $Worktree, 'ls-files', '-co', '--exclude-standard', '--full-name', '-z')
        $files = @($listed | Where-Object { Test-Reviewable $_ } | Select-Object -Unique)
        if ($files.Count -eq 0) { throw 'no reviewable files in the working tree' }
        foreach ($rel in $files) {
            if (-not (Test-Path -LiteralPath "$Worktree/$rel" -PathType Leaf)) { continue }  # deleted, not staged
            Assert-NoLink $rel
            [void][IO.Directory]::CreateDirectory((Split-Path "$ws/$rel" -Parent))
            [IO.File]::Copy("$Worktree/$rel", "$ws/$rel")
        }
        # A temporary index: a copy of the worktree's index plus intent-to-add entries for the reviewable untracked
        # files, so `git diff <Base>` shows new files too while the worktree's own index stays as it is.
        $index = "$stage/index"
        $realIndex = ([string](@(Invoke-Native git @('-C', $Worktree, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))[0])).Trim()
        if (Test-Path -LiteralPath $realIndex) { Copy-Item -LiteralPath $realIndex -Destination $index }
        $saved = $env:GIT_INDEX_FILE
        try {
            $env:GIT_INDEX_FILE = $index
            $untracked = @(Get-GitNames @('-C', $Worktree, 'ls-files', '-o', '--exclude-standard', '--full-name', '-z') |
                Where-Object { Test-Reviewable $_ })
            # Arguments, not stdin: PowerShell 5.1 pipes text to a native program in the ANSI code page.
            foreach ($chunk in (Get-Chunks $untracked)) {
                [void](Invoke-Native git (@('-C', $Worktree, '--literal-pathspecs', 'add', '--intent-to-add', '--') + $chunk) -Fail 'git add --intent-to-add failed')
            }
            Set-Delta @($Base)
        } finally {
            $env:GIT_INDEX_FILE = $saved
            Remove-Item -LiteralPath $index -Force -ErrorAction SilentlyContinue
        }
        $task = @(Get-Content -LiteralPath "$Worktree/$TaskFile" -Encoding UTF8)
    }
    'Patch' {
        Export-Tree $Base
        # The reviewable changes of the temporary index become the workspace: deletions removed, the rest checked
        # out from that index (binary-safe), and delta.diff/stat.txt are `git diff --cached <Base>` of them.
        $env:GIT_INDEX_FILE = $patchIndex
        $written = @()
        foreach ($c in $changes) {
            if (-not (Test-Reviewable $c[1])) { continue }
            if ($c[0] -eq 'D') { Remove-Item -LiteralPath "$ws/$($c[1])" -Force -ErrorAction SilentlyContinue }
            else { $written += $c[1] }
        }
        foreach ($chunk in (Get-Chunks $written)) {
            [void](Invoke-Native git (@('-C', $Worktree, 'checkout-index', '-f', "--prefix=$ws/", '--') + $chunk) -Fail 'git checkout-index failed')
        }
        Set-Delta @('--cached', $Base)
        if ($inIndex) { $task = @(Invoke-Native git @('-C', $Worktree, 'show', ":$TaskFile")) }
        else { $task = @(Get-Content -LiteralPath "$Worktree/$TaskFile" -Encoding UTF8) }
        $env:GIT_INDEX_FILE = $savedIndexFile
    }
}
} finally {
    $env:GIT_INDEX_FILE = $savedIndexFile
}
} finally {
    if ($null -ne $savedEncoding) { [Console]::OutputEncoding = $savedEncoding }
}
[IO.File]::WriteAllText("$ws/notes/delta.diff", ($delta -join "`n") + "`n", $utf8)
[IO.File]::WriteAllText("$ws/notes/stat.txt", ($stat -join "`n") + "`n", $utf8)
[IO.File]::WriteAllText("$ws/notes/$(Split-Path $TaskFile -Leaf)", ($task -join "`n"), $utf8)
foreach ($full in $extra) { Copy-Item -LiteralPath $full -Destination "$ws/notes/$(Split-Path $full -Leaf)" }
if ($Sanitize) {
    $notes = @(Get-ChildItem -LiteralPath "$ws/notes" -Filter '*.md' -File | ForEach-Object { $_.FullName })
    if ($notes.Count -gt 0) {
        [void](Invoke-Native $Python (@('-B', '-I', (Join-Path $PSScriptRoot 'agy_sanitize.py')) + $notes) -Fail 'agy_sanitize.py failed')
    }
}
# Publish: one directory move, which fails when the name is taken, so the build that moves first owns the workspace.
try { [IO.Directory]::Move($ws, $final) }
catch {
    if (Test-Path -LiteralPath $final) { throw "Workspace exists: $final (another build published it first)" }
    throw
}
} finally {
    # Only this build's own staging folder; Directory.Delete does not follow a link or junction inside it.
    try { [IO.Directory]::Delete($stage, $true) } catch { Write-Warning "Staging folder not removed: $stage ($($_.Exception.Message))" }
}
"workspace $final ready"
