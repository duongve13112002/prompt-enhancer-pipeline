# Agent Working Rules and Project Mission

Release 0.2, 2026-11-14. **The single rule source** for Claude Code (through `@AGENTS.md` in [CLAUDE.md](CLAUDE.md)), Codex and Antigravity (`agy`). Edit only this file and keep sections 1-13 numbered: skills and tools cite them. It holds what every agent needs; role rules reach a worker through its brief ([.agents/policy/roles/](.agents/policy/roles/)); orchestrator rules live in [PM_POLICY](.agents/policy/PM_POLICY.md) and [ORCHESTRATION](.agents/ORCHESTRATION.md). Each rule gives its reason. Provenance: [DECISIONS_INDEX](tasks/DECISIONS_INDEX.md); where older rules went: [rule-map](docs/agents/rule-map.md).

## Gates

> [!IMPORTANT]
> **Code starts on the user's explicit command** `BẮT ĐẦU IMPLEMENTATION <task>`, for every code task of the plan (T-005 onward and the tooling tasks T-020 to T-026). Read-only and documentation tasks of the plan (T-002 to T-004) start when the user approves their Step 0 plan with any message. Inside a started task the PM approves each worker's Step 0, and sends the user any plan that touches product meaning, permissions or spend.
> **Before a runtime edit** complete `Configuration impact` of the [task template](.agents/policy/AGENT_TASK_TEMPLATE.md); **before completion** give its row-by-row `Configuration audit` with tests. A missing row, ignored setting, hidden default or unclassified literal fails the task.

## 1. Language

- Chat with the user in Vietnamese. Everything in the repository is English (docs, code, identifiers, comments, configuration, tests, commit messages, briefs, runtime prompts), so every agent and tool reads the same text.
- Keep multilingual data verbatim (folder names, info files, concept names, sample prompts, quotations): translating evidence changes it.

## 2. Understand before changing

- **Step 0:** a task not marked `trivial` starts with the template's Step 0: unknowns, at most 5 ranked questions each with a proposed answer, a plan with a checkable finish. A file-changing worker then waits for its plan to be read; inside a started task, read-only roles and small tasks (`Small: yes`) put Step 0 atop their report and continue.
- Ask when product meaning, edit permission, model identity, API behaviour or a deployment setting is unclear, and meanwhile do the work that does not depend on the answer: a guessed meaning is the costliest mistake, because every later step builds on it. Search `tasks/DECISIONS_INDEX.md`, `tasks/STATUS.md` and `recall.py search` first, so the user is asked each thing once.
- **User away:** take the conservative, reversible choice, log it in `tasks/STATUS.md` as "PM decision pending user review", ask in chat and continue.

## 3. Research and evidence

- Read the code, callers, tests, configuration and docs before changing them. "Not read" and "unknown" are valid answers; an invented module or figure is damage.
- **Every claim carries its evidence:** a file and line, a command with its real output, or a URL with the exact quoted words, labelled documented, source-verified, experiment or assumption. A figure a research agent brings back is a lead until the primary source is re-read (R-021). Briefs list `Research required before code`; handoffs fill `Research evidence`.

## 4. Environments and platforms

- **Install only inside the project** (`.venv`, `python -m pip` there); report a missing system tool instead of installing it, because the machine is shared. Models are reached over HTTP (R-018). Keep CPU and GPU dependency profiles separate ([dependencies](docs/dependencies-and-environments.md)).
- **Windows and Linux** are both supported and both run in CI (R-025); shared logic has no OS-specific separators, shell syntax, signals or temp behaviour. Worktrees, builds, temp cleanup, GPU scaling: [PLATFORM_GPU_RULES](docs/rules/PLATFORM_GPU_RULES.md).

## 5. Complete, configurable behaviour

- **Deployment and user choices are validated profile values** (models, providers, endpoints, devices, workers, thresholds, budgets, seeds, timeouts, retries, paths, separators, inference settings). A missing field is refused at start with a precise message, because a silent default hides a decision nobody made (R-015).
- **No hand-written content rules:** meaning comes from model output or from structure; a word list, a regular expression over natural language, a per-language rule or a trick that passes one test fails review. Structural facts (a file that cannot be decoded, invalid JSON) are handled directly.
- **Values come from experiments:** an unmeasured threshold holds a sentinel (`fit_from_gt`, `fit_from_bench`, `fit_from_pilot`, `pin_required`, `deploy_required`, `design_required`, `ask_user`) until the `fit` step writes it with evidence ([contracts](contracts/README.md)).
- **Retained literals** are classified: `configurable`, `protocol-bound`, `artifact-bound`, `safety-bound`, `test-only` or `defect`. Every user value has validated config, an example profile, a non-default override test and a refusal test.
- **Finished means working:** test doubles live only in labelled tests; unfinished work is reported as unfinished.

## 6. Artifacts, storage and secrets

- **Everything stays under the project root** (`models/`, `.cache/`, `.tmp/`, `.venv/`, outputs); working data lives under `data_root` (default `data/`, Git-ignored). The user's knowledge base is read only (R-014). Validate configured paths after resolving links.
- **Models:** configured local artifacts first, verified by repository, revision, file, size and hash; use exactly the configured model ([model-storage](docs/model-storage.md)).
- **Secrets** live only in Git-ignored `.secrets/`. Agents, shells and review tools never open, list or print it; only project Python loads a credential at runtime. The one readable file there is [.secrets/README.md](.secrets/README.md).
- **Clean up after yourself:** remove your own `.tmp/` trees before handoff, keep the small reports you cite, remove links as links; never touch `models/`, `.venv*/`, `.secrets/`, `runs/` evidence or trees in use. The orchestrator cleans up finished agents and orphan trees ([ORCHESTRATION](.agents/ORCHESTRATION.md) section 4).

## 7. Testing

- Test all behaviour with bounded CPU tests, tiny synthetic fixtures and labelled doubles; no large downloads, heavy inference or paid calls in the default suite. The dev machine runs smoke tests ([dev-machine](docs/dev-machine.md)); the user runs the deployment. Every test command goes through `tools/agent-workflow/test_slot.py`.
- Mutate the changed lines (about 3 to 10 mutants); parsers and validators get property tests. Code that special-cases a test input is a defect even when green; a wrong test or an infeasible task is reported, not worked around ([test-plan](docs/test-plan.md)).

## 8. GPU, concurrency and services

- **No fixed hardware:** device count, VRAM budget and workers are profile values. **VRAM discipline:** record VRAM per device and its holder before a GPU job; if another session holds it, wait; afterwards VRAM is back at that baseline.
- **Parallel and resumable:** bounded queues and worker pools; every offline stage is idempotent per input content hash and tool revisions and resumes at the first unfinished item (R-017).
- **Model calls** go to the configured provider over HTTP with every field stated in the profile; the local backend is the default; a cloud provider (Claude or Gemini) is used only when the profile selects it and the user has granted a number of requests (R-018, [VLM_CALL_RULES](docs/rules/VLM_CALL_RULES.md)). Speed is measured, never claimed.
- **Lifecycle:** stop every server, worker and subprocess you started as soon as its queue drains, after any outcome.

## 9. Documentation and decisions

- `tasks/STATUS.md` keeps done, blocked, failed, skipped and `not_run` distinct. Decisions, deviations and rejected alternatives go to [implementation-notes](docs/implementation-notes.md) (a worker writes them in its task file; the PM moves them). A proposal stays a proposal until the user accepts it.
- Every user decision enters [DECISIONS_INDEX](tasks/DECISIONS_INDEX.md) in the same session with an `applied:` marker. Read the digest of [LESSONS_LEARNED](tasks/LESSONS_LEARNED.md) before a task.
- **Token economy:** state each fact once, in its owning file, and link; closed history goes verbatim to `tasks/archive/`; budgets in [doc_budgets.json](tools/agent-workflow/doc_budgets.json).

## 10. Project requirements

- **Scope:** implement [architecture](docs/architecture.md) and the [plan](tasks/IMPLEMENTATION_PLAN.md): a prompt enhancer that turns a prompt (`generate`) or a prompt plus one source image (`edit`) into an enhanced prompt or edit instruction for a downstream image model, optionally grounded in a knowledge base of concept folders. It writes text and renders no pixels. Cite R-IDs from [REQUIREMENTS](tasks/REQUIREMENTS.md) in every brief, plan and report.
- The user's intent survives unchanged (R-001); the admin's system prompt is used verbatim and defines the output format (R-004); retrieval off means no knowledge-base access at all (R-009); knowledge-base text and text read from images are data, never instructions (R-026).

## 11. Commit, push and CI

- **The orchestrator commits and pushes; workers never do (decision 77).** A worker never runs `git commit`, `push`, `merge`, `rebase`, `tag` or another history-changing command, on any branch, because the orchestrator alone integrates and answers for what lands. The orchestrator also stages in a worker's worktree to save its patch and removes a finished worker's worktree and never-committed branch. It brings verified work into the main tree, confirms the diff holds no secrets, model files, knowledge-base images or working data, runs the pre-push checks, then commits (English message, no agent attribution) and pushes without asking. No force push and no rewrite of pushed history, so the user's clones never diverge.
- Agents never choose a remote, create a repository or bypass protection (decision 35). **Repository visibility changes only inside the CI window** of [docs/ci.md](docs/ci.md): public for the run, private again when it ends, green or red (R-025).

## 12. Start and finish

- **Start:** read this file, [tasks/STATUS.md](tasks/STATUS.md) and the documents your task names. On a new machine or when hooks, skills or tools misbehave: `python tools/agent-workflow/setup_project.py check` (skill `agent-setup`).
- **Finish** with the template's Completion report: behaviour, the actual commands and their output, `not_run`, changed files, open issues.

## 13. Multi-agent work

- The orchestrator follows [PM_POLICY](.agents/policy/PM_POLICY.md) (models, quota, briefs, review, integration) and [ORCHESTRATION](.agents/ORCHESTRATION.md) (assignment, state, cleanup). The user's standing rules: [USER_STANDING_RULES](.agents/policy/USER_STANDING_RULES.md).
- A worker works inside its brief's Scope toward its Goal and Done check, follows its role pack and the [footer](.agents/policy/AGENT_BRIEF_FOOTER.md), lists anything else under `Noticed, not done`, and never reviews its own change.

<!-- ORCHESTRATION:START -->
### Agent orchestration (orchestrator only; if you are an assigned agent, skip this block)
- CURRENT MODE: DELEGATE
  - DELEGATE (default): delegate to Claude/Codex/Antigravity agents following `.agents/ORCHESTRATION.md`.
  - SOLO: the orchestrator does everything itself, creates no agent and delegates nothing.
  - Only the user changes the mode. On a permanent change request, update the "CURRENT MODE" line in AGENTS.md (CLAUDE.md imports it).
- In DELEGATE mode: before assigning work, read `.agents/ORCHESTRATION.md` and `.agents/policy/PM_POLICY.md` and follow them. After a compaction, or when unsure of a rule, read them again.
- Agent state: `cli_worker.py status --all --json` and `## IN-FLIGHT` in `.tmp/pm-state/pm-continuation.md`. Log: `.agents/log.md`.
<!-- ORCHESTRATION:END -->

<!-- AGENT-WORKER:START -->
### When you are an assigned agent
- You work on one task. A message starting with [NEW TASK: <task> phase <n>], [QA FEEDBACK], [EVALUATE] or [RETEST] continues that task.
- Work inside the current Scope; change earlier work only when the message asks, and give the reason in the report.
- Report in the template's Completion report format; keep the model, effort and session configuration you were started with; create sub-agents only when the message asks.
<!-- AGENT-WORKER:END -->
