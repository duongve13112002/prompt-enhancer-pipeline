# Agent brief footer (append verbatim to every brief)

These rules hold for every worker; a broken one fails the handoff. Rules for your role are in the role pack above. Each rule gives its reason so you can apply it to cases it does not name.

**Step 0 first:** unless the brief marks the task `trivial`, write the template's Step 0 in your task file, reply with it and wait for approval (read-only roles put it atop the report and continue). Then build step by step, two task-file lines per step.

1. **Environment:** use the project `.venv` only (path in `.agents/project.json`) with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH` set to the worktree's `python.src_dir`; pytest `-n` at most 2; temp and scratch under `<worktree>/.tmp/<task>/`. Installs, GPU, models, downloads and network calls happen only when the brief allows them, because the machine and quotas are shared.
2. **Owned by the PM:** `.secrets/` stays unopened; commits and pushes belong to the user; other version control writes and the files `tasks/STATUS.md`, `PROJECT_CONTEXT.md`, `IMPLEMENTATION_PLAN.md`, `DECISIONS_INDEX.md` belong to the PM unless the brief names the file and the edit. When a permission or policy denies an action, report it; fix your own malformed input and try again.
3. **Evidence, not assumption:** check library behaviour, defaults and file claims in the installed source or primary docs and cite them; write what you could not check as an assumption. Unclear semantics, permissions or model identity: stop and ask.
4. **Report what happened:** quote the exact output of lint and `ruff format --check`; an unrun test or mutant is `not_run` with the reason; a test double is labelled as one.
5. **Mutants and probes** run on a scratch copy of the trees they need, so the working tree stays clean.
6. **Faulthandler:** in subprocess tests use `-X faulthandler` or `faulthandler.enable()`; bound hangs with test timeouts and report crashes and hangs as findings.
7. **Leave nothing running:** stop every process you started, by its recorded PID, so no other session's process is touched.
8. **Clean up** scratch before handoff, keeping the small reports you cite; remove links as links.
9. **Configuration:** AGENTS.md section 5 and the template's audit.
10. **Test slots:** run every pytest, mutant or probe command through the limiter from the project root: `<venv python> -B tools/agent-workflow/test_slot.py -- <interpreter> -m pytest ...`. It allows 4 test commands at once (limit file `.tmp/pm-state/test-slots/max`, else 2); never bypass it.
11. **Economy:** final reply about 10 short lines with details in the task file (reviewers add the full findings index); keep tool output small (`pytest -q`, grep, tail).
12. **GPU for models:** permitted model work runs on the GPU through the profile's device setting; when the GPU is busy, wait or report, because a CPU fallback gives results that mean something else.
13. **Hooks:** follow `[ctx-nudge]` and `[loop-warning]`.
14. **Recall first:** before asking or deciding, `<venv python> -B -I tools/agent-workflow/recall.py build`, then `recall.py search "<words>"`, so a settled point is not asked again.
15. **Tools:** read with Read, search with Grep or Glob on a scoped path; change tracked files with Edit or Write (shell writes go under `.tmp/`, run logs under `runs/`); put multi-line logic in a script file; check `--help` before using a CLI; make one state change per call and verify it.
16. **Meaning from models, not lists:** source holds no natural-language word list or per-language rule (R-015). Propose the experiment-proven design and name the narrower one you dropped.
17. **Focus:** work toward the brief's Goal and Done check inside Scope. List anything else you notice as one line under `Noticed, not done`. Keep a 2-5 item plan with one item in progress and write a progress line about every 8 tool calls. Stop when the done check passes; when the step budget is spent or the same step fails twice, report what blocks. Return a condensed report and file paths.
