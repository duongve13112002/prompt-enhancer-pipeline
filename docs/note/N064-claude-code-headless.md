# N064 Claude Code headless mode and CLI reference

- **Source:** https://code.claude.com/docs/en/headless
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it (also https://code.claude.com/docs/en/cli-reference)
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

`claude -p` loads CLAUDE.md, settings and hooks like an interactive session; `--bare` skips them. Flags include `--output-format`, `--resume`, `--allowedTools`, `--tools`, `--settings`, `--append-system-prompt`, `--permission-mode`, `--max-turns`, `--worktree`. Calls needing approval are denied in print mode unless a hook or prompt tool approves.

## Caveats

`--allowedTools` pre-approves; only `--tools` restricts the tool set.
