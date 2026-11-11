---
name: agent-setup
description: Set up, verify or repair the multi-agent tooling (hooks, skills, Claude/Codex/Antigravity workers, project binding) for this repository. Use on a new machine or fresh clone, after copying the tooling into another repository, after a CLI upgrade, or when hooks, skills, workers or brief lint misbehave.
---

# Agent tooling setup

Everything project-specific is in `.agents/project.json`; everything else derives the project root from the tools' own location, so a copied checkout needs no code edit. `tools/agent-workflow/setup_project.py` checks and repairs the binding. Never touch `.secrets/`, `~/.claude`, `~/.codex` or `~/.gemini` during setup.

## Fresh machine or fresh clone

1. Create the venv the binding names (`python.venv_dir`, default `.venv`) and install the tool test dependencies there: `python -m venv .venv`, then `<venv python> -m pip install pytest hypothesis`.
2. `<venv python> -B tools/agent-workflow/setup_project.py apply` (creates `.tmp/pm-state`, copies `.agents/skills` to `.claude/skills`, renders `.claude/settings.local.json` for this checkout, writes the integration branch into `memory_hooks.json`).
3. `<venv python> -B tools/agent-workflow/setup_project.py check`; fix every `FAIL`; read every `WARN` (a missing CLI or the Windows agy runner scripts are machine facts, not repository defects).
4. Claude Code: run `claude doctor`, then `/memory` to confirm `AGENTS.md` loaded through `CLAUDE.md`; hooks take effect in a new session.
5. Codex: copy `tools/agent-workflow/codex_profiles.json` to `.tmp/pm-state/codex.local.json`, set `runtime.codex_command` (native `codex.exe` on Windows); pass it with `cli_worker.py start --backend codex --codex-config`. Details: `tools/agent-workflow/CODEX_JOBS.md`.
6. Antigravity: put the runner scripts in `agents.antigravity.runner_scripts_dir` (Windows only) and, for quota reporting, set `usage.antigravity_command` in the local Codex config copy.
7. Prove one hook fires with a deliberate violation (for example ask for a process kill by name) and see the block, then run `<venv python> -B tools/agent-workflow/usage_snapshot.py` to confirm the quota sources.
8. Report each step as passed, blocked (with the exact blocker) or `not_run`.

## Copying the tooling into another repository

1. Copy `tools/agent-workflow/`, `.agents/` (without `registry.json`, `log.md`), `AGENTS.md`, `CLAUDE.md` and the `.gitignore` entries; create the files the hooks read (`setup_project.py check` lists them).
2. Edit `.agents/project.json`: `project.name`, `python.*` (set `package` and `runtime_config_module` once code exists), `git.*`, enabled `agents`, and put the old project's names, paths and branch in `foreign_bindings`.
3. Rewrite the project sections of `AGENTS.md` (sections 10 and 11) and the policy files that name the old project; empty `tasks/` state files.
4. Run `apply`, then `check` until no `FAIL` remains; the `foreign` check lists every leftover token of the old project.

## When something misbehaves

- Hooks silent or pointing elsewhere: `check` shows `settings`; repair with `apply --force-settings`.
- Skills stale in Claude Code: edit only `.agents/skills/`, then `apply`.
- Brief lint refuses every brief: the footer moved or changed; `check` lists the files hooks read, and the brief must carry `.agents/policy/AGENT_BRIEF_FOOTER.md` verbatim.
- After a CLI upgrade: `check` (versions), re-read the model lists (`/model`, `agy models`) and update `.agents/policy/PM_POLICY.md` section 2 and its notes in the same change.
