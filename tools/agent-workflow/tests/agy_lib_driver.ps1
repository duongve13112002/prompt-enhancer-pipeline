param([Parameter(Mandatory = $true)][string]$InputJson)
# Test driver: runs one agy-lib.ps1 function on fake inputs and prints {"ok","value","error"} as JSON.
# Never starts agy, reads the scheduled task or touches anything outside the paths in the input.
$ErrorActionPreference = 'Stop'
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
        'quota' { $result.value = Get-AgyQuotaNotice -StderrPath $in.stderr_path -Now ([datetime]::Parse($in.now)) }
        default { throw "unknown case $($in.case)" }
    }
} catch {
    $result.ok = $false
    $result.error = $_.Exception.Message
}
ConvertTo-Json -InputObject $result -Compress -Depth 4
