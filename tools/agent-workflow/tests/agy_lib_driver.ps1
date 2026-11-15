param([Parameter(Mandatory = $true)][string]$InputJson)
# Test driver: runs one agy-lib.ps1 function on fake inputs and prints {"ok","value","error"} as JSON.
# Never starts agy, reads a real scheduled task (case 'sched' replaces the scheduler with labelled doubles) or
# touches anything outside the paths in the input.
$ErrorActionPreference = 'Stop'
# The JSON line is UTF-8 whatever the console code page (cp437 or cp1252 on a CI runner), as setup_project.py does.
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
. (Join-Path $PSScriptRoot '..\..\..\scripts\agy-runner\agy-lib.ps1')
$in = [IO.File]::ReadAllText($InputJson) | ConvertFrom-Json
$result = @{ ok = $true; value = $null; error = $null }
try {
    switch ($in.case) {
        'lint' {
            $depth = 0
            if ($in.depth) { $depth = [int]$in.depth }
            $result.value = @(Find-AgyPathViolation -Text $in.text -Source 'prompt' -Depth $depth)
        }
        'expect_assert' { Assert-AgyExpectFile -ExpectFile $in.expect; $result.value = 'valid' }
        'expect_notice' { $result.value = Get-AgyExpectFileNotice -ExpectFile $in.expect -Explicit ([bool]$in.explicit) }
        'lint_ws' { Assert-AgyPathLint -Root $in.root -Workspace $in.workspace -PromptText $in.prompt; $result.value = 'clean' }
        'extra' { $result.value = Resolve-AgyExtraNote -Root $in.root -Path $in.path }
        'suffix' { $result.value = Add-AgyReviewSuffix -Prompt $in.prompt }
        'busy' {
            $last = $null
            if ($in.last_run) { $last = [datetime]::Parse($in.last_run) }
            $result.value = Get-AgyBusyMessage -State $in.state -JobJsonPath $in.job_json -LastRunTime $last
        }
        'incomplete' {
            $before = [datetime]::MinValue
            if ($in.not_before) { $before = [datetime]::Parse($in.not_before) }
            $result.value = @(Get-AgyIncompleteReason -Reply $in.reply -StderrText $in.stderr -WorkspacePath $in.workspace -ExpectFile $in.expect -NotBefore $before)
        }
        'task_name' { $result.value = @($in.roots | ForEach-Object { Get-AgyRunnerTaskName -ProjectRoot $_ }) }
        'classify' {
            # Labelled task doubles: plain objects shaped like a scheduled task (TaskName, TaskPath, State, Actions).
            # Each case goes through every consumer of the classifier, so a test can compare them on one input.
            $result.value = @(foreach ($c in @($in.cases)) {
                $actions = @($c.actions | ForEach-Object { [pscustomobject]@{ Execute = [string]$_.execute; Arguments = [string]$_.arguments } })
                $t = [pscustomobject]@{ TaskName = 'Other'; TaskPath = '\'; State = 'Running'; Actions = $actions }
                $class = Get-AgyTaskClass $t
                [ordered]@{
                    kind = $class.Kind; root = $class.ProjectRoot; reason = $class.Reason
                    bound_root = (Get-AgyTaskBoundRoot $t)
                    listed = @(Get-AgyRunnerTask -Tasks @($t)).Count
                    retained = @(Get-AgyRetainingTask -Tasks @($t)).Count
                    retained_after_own_removal = @(Get-AgyRetainingTask -Tasks @($t) -RemovedTask $t).Count
                    # The same name in another task folder is another task: undo removing that one keeps this one.
                    retained_after_other_folder_removal = @(Get-AgyRetainingTask -Tasks @($t) -RemovedTask ([pscustomobject]@{ TaskName = 'Other'; TaskPath = '\Elsewhere\' })).Count
                    binding = (Get-AgyBindingProblem -ProjectRoot $(if ($c.root) { $c.root } else { $in.root }) -TaskName 'Other' -Task $t -JobsDirExists $true)
                    busy = (Get-AgyOtherBusyMessage -Running @(Get-AgyRunnerTask -Tasks @($t)) -TaskName 'Mine')
                }
            })
        }
        'caller_shape' {
            # Fix round 3: runs a production caller's own statements, the range from the assignment of $in.first to
            # that of $in.last, read from the real file by the PowerShell parser, then evaluates the condition of
            # the if statement that tests $in.guard (never its body). Get-ScheduledTask is a labelled double that
            # returns client-only CIM instances of the production type (no CIM connection, no task read or change)
            # and counts enumerations. No helper result is wrapped here, so the caller's own shape is what is seen.
            Import-Module ScheduledTasks -ErrorAction Stop
            $ns = 'Root/Microsoft/Windows/TaskScheduler'
            function New-CimProperty($Name, $Value, $Type) {
                [Microsoft.Management.Infrastructure.CimProperty]::Create($Name, $Value, $Type, [Microsoft.Management.Infrastructure.CimFlags]::Property)
            }
            $script:doubleTasks = @(foreach ($d in @($in.tasks)) {
                $action = [Microsoft.Management.Infrastructure.CimInstance]::new('MSFT_TaskExecAction', $ns)
                $action.CimInstanceProperties.Add((New-CimProperty 'Execute' 'powershell.exe' ([Microsoft.Management.Infrastructure.CimType]::String)))
                $action.CimInstanceProperties.Add((New-CimProperty 'Arguments' ([string]$d.arguments) ([Microsoft.Management.Infrastructure.CimType]::String)))
                $task = [Microsoft.Management.Infrastructure.CimInstance]::new('MSFT_ScheduledTask', $ns)
                foreach ($p in @(@('TaskName', [string]$d.name), @('TaskPath', '\'), @('State', [string]$d.state))) {
                    $task.CimInstanceProperties.Add((New-CimProperty $p[0] $p[1] ([Microsoft.Management.Infrastructure.CimType]::String)))
                }
                $task.CimInstanceProperties.Add((New-CimProperty 'Actions' ([Microsoft.Management.Infrastructure.CimInstance[]]@($action)) ([Microsoft.Management.Infrastructure.CimType]::InstanceArray)))
                $task
            })
            $script:enumerations = 0
            function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:enumerations++; $script:doubleTasks }
            function Get-ScheduledTaskInfo { [CmdletBinding()] param([string]$TaskName, [string]$TaskPath) [pscustomobject]@{ LastRunTime = [datetime]'2026-11-15 10:00:00' } }
            $ast = [Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText($in.path), [ref]$null, [ref]$null)
            $assigned = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and
                    $n.Left -is [Management.Automation.Language.VariableExpressionAst] }, $true))
            $first = @($assigned | Where-Object { $_.Left.VariablePath.UserPath -eq $in.first })[0]
            $last = @($assigned | Where-Object { $_.Left.VariablePath.UserPath -eq $in.last -and $first -and $_.Parent -eq $first.Parent })[0]
            if (-not $first -or -not $last) { throw "no statement range `$$($in.first)..`$$($in.last) in $($in.path)" }
            $statements = @($first.Parent.Statements)
            $code = (@($statements[$statements.IndexOf($first)..$statements.IndexOf($last)] | ForEach-Object { $_.Extent.Text })) -join "`n"
            # Fail closed: the range must be reads only.
            if ($code -match '(?i)\b(Unregister|Register|Remove|Set|New|Start|Stop|Copy|Add|Enable|Disable)-[A-Za-z]|icacls|schtasks|secedit') {
                throw "the range $($in.first)..$($in.last) holds a command that changes state; not run"
            }
            $TaskName = $in.task_name; $ProjectRoot = $in.root; $root = $in.root; $out = @{ task_name = $in.task_name }
            . ([scriptblock]::Create($code))
            $value = [ordered]@{ enumerations = $script:enumerations }
            foreach ($name in @($in.report)) {
                $x = Get-Variable -Name $name -ValueOnly
                $value[$name] = [ordered]@{ is_array = ($x -is [array]); count = $(if ($x -is [array]) { $x.Count } else { $null })
                    type = $(if ($null -eq $x) { 'null' } else { $x.GetType().FullName }) }
            }
            if ($in.guard) {
                $conditions = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.IfStatementAst] }, $true) |
                    ForEach-Object { $_.Clauses } | ForEach-Object { $_.Item1 } |
                    Where-Object { $_.Extent.Text -match ('\$' + [regex]::Escape($in.guard) + '\b') })
                if ($conditions.Count -ne 1) { throw "expected one if condition on `$$($in.guard), found $($conditions.Count)" }
                $value.guard_text = $conditions[0].Extent.Text
                $value.guard = [bool](. ([scriptblock]::Create($conditions[0].Extent.Text)))
            }
            $result.value = $value
        }
        'shape_lint' {
            # Fix round 3, by class: an assignment from a command pipeline is unrolled, so it must be wrapped in @(...)
            # when the same function (or script) reads its .Count, .Length or an index, or when it reads a collection
            # (scheduler, CIM, folder listing, the runner list helpers). Reports every such assignment left unwrapped.
            $readers = @('Get-ScheduledTask', 'Get-CimInstance', 'Get-ChildItem', 'Get-AgyScheduledTaskAll', 'Get-AgyRunnerTask', 'Get-AgyRetainingTask')
            $enclosing = { param($node) $p = $node.Parent; while ($p -and $p -isnot [Management.Automation.Language.FunctionDefinitionAst]) { $p = $p.Parent }; $p }
            $result.value = @(foreach ($file in @($in.files)) {
                $ast = [Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText($file.path), [ref]$null, [ref]$null)
                foreach ($a in @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] }, $true))) {
                    if ($a.Left -isnot [Management.Automation.Language.VariableExpressionAst]) { continue }
                    if ($a.Right -isnot [Management.Automation.Language.PipelineAst]) { continue }
                    $commands = @($a.Right.PipelineElements | Where-Object { $_ -is [Management.Automation.Language.CommandAst] })
                    if ($commands.Count -eq 0) { continue }  # @(...), literals and operators keep their shape
                    $var = $a.Left.VariablePath.UserPath
                    # Same scope: the nearest enclosing function ($null for the script itself) of the use and the assignment.
                    $fn = & $enclosing $a
                    $counted = @($ast.FindAll({ param($n)
                        ((($n -is [Management.Automation.Language.MemberExpressionAst]) -and ($n.Expression -is [Management.Automation.Language.VariableExpressionAst]) -and
                            ($n.Expression.VariablePath.UserPath -eq $var) -and ($n.Member.Extent.Text -in 'Count', 'Length')) -or
                        (($n -is [Management.Automation.Language.IndexExpressionAst]) -and ($n.Target -is [Management.Automation.Language.VariableExpressionAst]) -and
                            ($n.Target.VariablePath.UserPath -eq $var))) -and ((& $enclosing $n) -eq $fn) }, $true)).Count -gt 0
                    $reads = @($commands | Where-Object { $_.GetCommandName() -in $readers }).Count -gt 0
                    if ($counted -or $reads) { "$($file.label):$($a.Extent.StartLineNumber): $(($a.Extent.Text -split "`n")[0])" }
                }
            })
        }
        'binding' {
            $t = $null
            if ($in.exists) {
                $t = [pscustomobject]@{ TaskName = $in.task_name; TaskPath = '\'
                    Actions = @([pscustomobject]@{ Execute = 'powershell.exe'; Arguments = [string]$in.arguments }) }
            }
            $result.value = Get-AgyBindingProblem -ProjectRoot $in.root -TaskName $in.task_name -Task $t -JobsDirExists ([bool]$in.jobs)
        }
        'other_busy' {
            $running = @($in.running | ForEach-Object { [pscustomobject]@{ TaskName = $_.TaskName
                Actions = @([pscustomobject]@{ Execute = 'powershell.exe'; Arguments = [string]$_.Arguments }) } })
            $result.value = Get-AgyOtherBusyMessage -Running $running -TaskName $in.task_name
        }
        'resolve' { $result.value = @($in.paths | ForEach-Object { Get-AgyResolvedPath -Path $_ }) }
        'same_path' { $result.value = @($in.pairs | ForEach-Object { Test-AgySamePath $_[0] $_[1] }) }
        'exists' { $result.value = @($in.paths | ForEach-Object { Test-AgyPathExists -Path $_.path -PathType $_.type }) }
        'reserved' {
            $result.value = @($in.names | ForEach-Object {
                try { Assert-AgyTaskNameAllowed -TaskName $_; 'allowed' } catch { "refused: $($_.Exception.Message)" }
            })
        }
        'sched' {
            # Labelled doubles: a scheduler that fails or returns the given tasks, and (with path_fail) a path read
            # that fails with access denied. No real scheduled task or folder attribute is read by these doubles.
            if ($in.scheduler -eq 'fail') {
                function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) Write-Error 'labelled scheduler-unavailable double' }
            } else {
                $script:doubleTasks = @($in.tasks | Where-Object { $_ })
                function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:doubleTasks }
            }
            function Get-ScheduledTaskInfo { [CmdletBinding()] param([string]$TaskName, [string]$TaskPath) [pscustomobject]@{ LastRunTime = [datetime]'2026-11-15 10:00:00' } }
            if ($in.path_fail) {
                function Get-AgyPathAttribute { param([string]$Path) throw [UnauthorizedAccessException]"labelled access-denied double: $Path" }
            }
            $result.value = [ordered]@{}
            foreach ($call in @($in.calls)) {
                $result.value[$call] = try {
                    switch ($call) {
                        'runner_tasks' { 'ok:' + @(Get-AgyRunnerTask).Count }
                        'retained' {
                            # undo's keep-list when undo removes this project's own task (the strictest case).
                            $all = Get-AgyScheduledTaskAll
                            $own = Get-AgyNamedTask -TaskName $in.task_name -Tasks $all
                            'ok:' + (@(Get-AgyRetainingTask -Tasks $all -RemovedTask $own | ForEach-Object { $_.TaskName }) -join ',')
                        }
                        'named' { $t = Get-AgyNamedTask -TaskName $in.task_name; if ($t) { 'ok:found' } else { 'ok:absent' } }
                        'binding' { Assert-AgyRunnerBinding -ProjectRoot $in.root -TaskName $in.task_name; 'ok:bound' }
                        'idle' { Assert-AgyIdle -ProjectRoot $in.root -TaskName $in.task_name; 'ok:idle' }
                        'exists' { 'ok:' + (Test-AgyPathExists -Path $in.root) }
                    }
                } catch { "threw: $($_.Exception.Message)" }
            }
        }
        'ace' {
            # Labelled double: plain objects shaped like FileSystemAccessRule, no real ACL is read.
            $rules = @($in.rules | ForEach-Object {
                $prop = 0
                if ($_.propagation) { $prop = $_.propagation }
                [pscustomobject]@{ IdentityReference = $_.identity; AccessControlType = $_.type; IsInherited = [bool]$_.inherited
                    FileSystemRights = [Security.AccessControl.FileSystemRights]$_.rights
                    InheritanceFlags = [Security.AccessControl.InheritanceFlags]$_.flags
                    PropagationFlags = [Security.AccessControl.PropagationFlags]$prop }
            })
            $result.value = Get-AgyAceDecision -Rules $rules -UserName $in.user -Mode $in.mode -Rights $in.rights -Inherit ([bool]$in.inherit)
        }
        'quota' { $result.value = Get-AgyQuotaNotice -StderrPath $in.stderr_path -Now ([datetime]::Parse($in.now)) }
        default { throw "unknown case $($in.case)" }
    }
} catch {
    $result.ok = $false
    $result.error = $_.Exception.Message
}
ConvertTo-Json -InputObject $result -Compress -Depth 4
