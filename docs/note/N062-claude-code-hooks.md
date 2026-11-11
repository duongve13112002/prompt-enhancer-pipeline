# N062 Claude Code hooks

- **Source:** https://code.claude.com/docs/en/hooks
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Events include SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, SubagentStop, PreCompact, PostCompact, Stop. Exit code 2 blocks on PreToolUse, UserPromptSubmit, Stop, PreCompact and others; it does not block on PostToolUse or SessionStart. Matchers: `*`, exact or list, otherwise unanchored regex.

## Caveats

An invalid hook entry is skipped with a warning; a hook that never fires looks like a pass.
