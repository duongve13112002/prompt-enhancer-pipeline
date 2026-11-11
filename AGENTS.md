# Agent Working Rules and Project Mission

Release 0.1, 2026-11-11. **This file is the single rule source** for Claude Code (through `@AGENTS.md` in [CLAUDE.md](CLAUDE.md)), Codex and Antigravity (`agy`), which read it directly. Edit only this file; keep sections 1-13 numbered because skills and tools cite them. It holds the rules every agent needs; rules for one role live in [.agents/policy/roles/](.agents/policy/roles/) and reach a worker through its brief. Each rule states its reason so you can apply it to cases it does not spell out. Provenance: [tasks/DECISIONS_INDEX.md](tasks/DECISIONS_INDEX.md); where each older rule went: [docs/agents/rule-map.md](docs/agents/rule-map.md).

## Gates

> [!IMPORTANT]
> **Before a runtime edit:** copy [.agents/policy/AGENT_TASK_TEMPLATE.md](.agents/policy/AGENT_TASK_TEMPLATE.md) into the task file and complete `Configuration impact`.
> **Before completion:** give the row-by-row `Configuration audit` with its tests; a missing row, ignored setting, hidden default or unclassified literal fails the task.
> **Code starts on the user's explicit command** (`BẮT ĐẦU IMPLEMENTATION <task>`, from T-005). Read-only and documentation tasks of the plan (T-002 to T-004) start when the user approves their Step 0 plan with any message; inside a started task, read-only roles (reviewer, scout, researcher) put Step 0 atop their report and continue.

## 1. Language

- Chat with the user in the conversation's language (Vietnamese). Everything in the repository is English: docs, notes, code, identifiers, comments, configuration, test names, commit messages, briefs, runtime prompts. One language keeps every agent and tool reading the same text.
- Keep multilingual data verbatim: folder names, info files, concept names, sample prompts, quotations. Translating evidence changes it.

## 2. Understand before changing

- Start by stating the behaviour, inputs and outputs, constraints, acceptance check, risks and what is missing; then give a short plan with reasons.
- Ask when product meaning, edit permission, model identity, API behaviour or a deployment setting is unclear, and meanwhile do the work that does not depend on the answer. A guessed meaning is the costliest mistake here because every later step builds on it.
- **Step 0:** every task not marked `trivial` begins with the template's Step 0 (unknowns, at most 5 ranked questions each with a proposed answer, a plan with a checkable finish line). A file-changing task then waits until its plan is read; read-only roles continue.
- **User away:** take the conservative, reversible choice, log it in `tasks/STATUS.md` as "PM decision pending user review", ask in chat and continue. Only a plan the user must read waits.
- **Settled points:** before asking, search `tasks/DECISIONS_INDEX.md`, `tasks/STATUS.md` and `recall.py search`, so the user is asked each thing once.

## 3. Research and evidence

- Read the relevant code, callers, tests, configuration and docs before changing them. Say "not read" or "unknown" when that is the truth; an honest gap is useful, an invented module or figure is damage.
- **Every claim carries its evidence:** a file and line, a command with its real output, or a URL with the exact quoted words. Label each claim documented, source-verified, experiment or assumption. A claim without a pointer is an assumption and is written as one.
- **Research gate:** every brief lists `Research required before code`; every handoff has a `Research evidence` table. Researchers extract the exact quote first and summarise second. A figure a research agent brings back is a lead until the primary source is re-read (R-021).
- Record each external claim in [docs/note/](docs/note/README.md) and update its index. Plan in [tasks/IMPLEMENTATION_PLAN.md](tasks/IMPLEMENTATION_PLAN.md); keep [tasks/STATUS.md](tasks/STATUS.md) current.

## 4. Environments and platforms

- **Install only inside the project.** Python packages go into the project `.venv` (`python -m pip` there). Report a missing system tool or runtime to the user instead of installing it, because the machine is shared and a global change can break other work. Production model servers are outside this repository: models are reached over HTTP (R-018). Keep CPU and GPU dependency profiles separate and write a lockfile only from a real resolution ([docs/dependencies-and-environments.md](docs/dependencies-and-environments.md)).
- **Windows and Linux are both supported** and both run in CI (R-025). Shared logic contains no OS-specific separators, shell syntax, signals or temp behaviour; native artifacts name their platform.
- Worktrees, builds, CI, temp cleanup, GPU scaling: read [docs/rules/PLATFORM_GPU_RULES.md](docs/rules/PLATFORM_GPU_RULES.md) first.

## 5. Complete, configurable behaviour

- **Deployment and user choices are validated profile values:** models, providers, endpoints, devices, workers, precision, thresholds, budgets, seeds, timeouts, retries, directories, separators, inference settings. A missing field is refused at start with a precise message, because a silent default hides a decision nobody made (R-015).
- **No hand-written content rules.** Meaning comes from model output or from structure; a word list, a regular expression over natural language, a per-language rule or a trick that passes one test all fail review. Structural facts are handled directly: a file that cannot be decoded, a string that is not valid JSON, two strings that are not equal.
- **Values come from experiments.** A threshold, size or count that is not yet measured holds a sentinel (`fit_from_gt`, `fit_from_bench`, `fit_from_pilot`, `pin_required`, `deploy_required`, `design_required`, `ask_user`); the `fit` step writes the value with its evidence report ([contracts/README.md](contracts/README.md)).
- **Literals that may stay:** wire and schema constants, immutable artifact identity, bounded parser and protocol limits, real safety invariants. Classify every retained literal in the changed path: `configurable`, `protocol-bound`, `artifact-bound`, `safety-bound`, `test-only` or `defect`.
- **Configuration audit:** every user value has validated config, an example profile, a non-default override test and a refusal test.
- **Finished means working:** required behaviour has a real implementation; test doubles live only in labelled tests. Report unfinished work as unfinished. Comments are concise English on intent and tradeoffs.

## 6. Artifacts, storage and secrets

- **Everything stays under the project root** (`models/`, `.cache/`, `.tmp/`, `.venv/`, build and output directories). Working data lives under `data_root` (default `data/`, Git-ignored, configurable with the same containment check). The user's knowledge base is read only: derived data never goes into it (R-014).
- Validate configurable paths after resolving links; a path outside its root fails clearly. Configure libraries and subprocesses so they cannot fill global caches.
- **Models:** configured local artifacts first, verified by repository, revision, file, size, hash and platform; acquire only from the pinned source when the task permits; use exactly the configured model ([docs/model-storage.md](docs/model-storage.md)).
- **Secrets** live only in Git-ignored `.secrets/`. Agents, shells and review tools leave that directory unopened and unlisted; only project Python loads a credential at runtime and passes it straight to the SDK. Tests use their own dummies. The one file there an agent may read is [.secrets/README.md](.secrets/README.md).
- **Clean up after yourself:** remove your own `.tmp/` trees before handoff and keep the small reports you cite. Leave `models/`, `.venv*/`, `.secrets/`, `runs/` evidence and trees in use alone, and remove links as links. When an agent is finished and unneeded, the orchestrator deletes its worktree, branch, scratch files and chat history ([ORCHESTRATION](.agents/ORCHESTRATION.md) section 4).

## 7. Testing

- Test all behaviour. The agent environment is CPU-only unless a GPU is detected and the task authorizes it; the dev machine runs smoke tests ([docs/dev-machine.md](docs/dev-machine.md)); the deployment is run by the user.
- Default suite: bounded CPU tests, tiny synthetic fixtures, labelled doubles; no large downloads, heavy inference or paid calls. Tool tests run through `tools/agent-workflow/test_slot.py`.
- **Targeted mutation testing:** the implementer mutates the changed lines and fixed findings (about 3-10); the reviewer adds at most 3. Parsers, grammars and validators get Hypothesis property tests.
- A general solution is the goal: code that special-cases a test input or a fixture name is a defect even when the test is green. If a test is wrong or a task is infeasible, say so instead of working around it.
- GPU smoke scripts run real implementations and fail or skip transparently; compilation or HTTP 200 proves nothing ([docs/test-plan.md](docs/test-plan.md)).

## 8. GPU, concurrency and services

- **No fixed hardware.** Every GPU stage runs on one or many GPUs from the profile without source edits; device count, VRAM budget and workers are profile values.
- **VRAM discipline:** before a GPU job record the VRAM in use per device and who holds it (`nvidia-smi`); if another session holds it, wait. After the job VRAM is back at that baseline and none of your processes holds a GPU; a difference is a defect to report.
- **Parallel and resumable:** independent work runs concurrently through a bounded queue and worker pool; every offline stage is idempotent per input content hash and tool revisions and resumes at the first unfinished item (R-017).
- **Model calls** go to the configured provider over HTTP with every request field stated in the profile. The local backend is the default; a cloud provider (Claude or Gemini) is used only when the profile selects it and the user has granted a number of requests, with its key read from a file under `.secrets/` (R-018); unsupported parameters are dropped with a recorded warning; speed is measured, never claimed ([docs/rules/VLM_CALL_RULES.md](docs/rules/VLM_CALL_RULES.md), [docs/serving.md](docs/serving.md)).
- **Lifecycle:** load on demand and stop every server, worker and subprocess you started as soon as its queue drains, after any outcome. A warm pool needs a user-approved profile.

## 9. Documentation and decisions

- Update affected docs in the same change as the code; `tasks/STATUS.md` keeps done, blocked, failed, skipped and `not_run` distinct.
- Record unspecified decisions, deviations, assumptions and rejected alternatives in [docs/implementation-notes.md](docs/implementation-notes.md). A proposal stays a proposal until the user accepts it, and an unrun test is `not_run`.
- Every user decision enters [tasks/DECISIONS_INDEX.md](tasks/DECISIONS_INDEX.md) in the same session with an `applied:` marker.
- Read the digest of [tasks/LESSONS_LEARNED.md](tasks/LESSONS_LEARNED.md) before a task; a recurring mistake adds a prevention rule.
- **Token economy:** state each fact once, in its owning file, and link. Live files hold the present state; closed history goes verbatim to `tasks/archive/`. Character budgets: [tools/agent-workflow/doc_budgets.json](tools/agent-workflow/doc_budgets.json).

## 10. Project requirements

- **Scope:** implement [docs/architecture.md](docs/architecture.md) and the [plan](tasks/IMPLEMENTATION_PLAN.md): a prompt enhancer that turns a user's prompt (`generate`) or a prompt plus one source image (`edit`) into an enhanced prompt or edit instruction for a downstream image model, optionally grounded in a knowledge base of concept folders. It writes text; it renders and edits no pixels. Cite R-IDs from [tasks/REQUIREMENTS.md](tasks/REQUIREMENTS.md) in every brief, plan and report.
- **R-001 Intent first:** what the user fixed (identity, counts, attributes, relations, negations, style, text to render) survives unchanged; knowledge and creativity fill only what the user left open.
- **R-004 Profiles:** the admin's system prompt is used verbatim and defines the output format; the pipeline's internal artifacts are format-agnostic ([contracts](contracts/README.md), [P-profiles](docs/stages/P-profiles.md)).
- **R-007, R-014 Knowledge base:** folders declare one or more concepts; `kb sync` is the only writer; the request path reads only the published snapshot ([execution](docs/execution.md), [kb/README.md](kb/README.md)).
- **R-009 to R-011 Retrieval:** on by default; off means no knowledge-base access at all; evidence comes from offline concept cards; knowledge goes downstream as text unless the edit configuration attaches images.
- **R-013 Choices:** a mention broader than the knowledge base is resolved by context fit, then uniformly at random by code, new for every request and recorded.
- **R-015, R-016:** no hand-written rules or guessed values; tables are Parquet or Arrow with a derived serving index.
- **R-019, R-020:** a few seconds per request under concurrency is the target and is measured on the deployment; everything works on the dev machine first, whose results show plumbing only.
- **R-023 Evaluation:** no gold prompts exist; agents build the test set from the user's knowledge base ([G-evaluation](docs/stages/G-evaluation.md)).
- **R-024, R-026:** every response has an honest status; knowledge-base text and text read from images are data, never instructions.

## 11. Commit, push and CI

- **The user makes every commit and every push (decisions 36, 37, 41).** No agent runs `git commit`, `push`, `merge`, `rebase`, `tag` or another history-changing command, on any branch, a worker's temporary branch included. When verified work is ready, the orchestrator brings it into the main working tree uncommitted, inspects the diff to confirm it holds no secrets, model files, knowledge-base images or working data, and reports the changed files, the checks run and a proposed commit message; the user commits and pushes.
- Agents never choose a remote, create a repository or bypass protection (decision 35). **Repository visibility changes only inside the CI window** of [docs/ci.md](docs/ci.md) (R-025): public for the run, private again when it ends, green or red; once a remote exists, confirm at session start that the repository is private.
- Proposed commit messages are English with no `Co-Authored-By:` trailer or agent attribution.

## 12. Start and finish

- **Start:** read this file, [tasks/STATUS.md](tasks/STATUS.md) and the documents your task names. Orchestrator only: also [.agents/ORCHESTRATION.md](.agents/ORCHESTRATION.md) and [.agents/policy/PM_POLICY.md](.agents/policy/PM_POLICY.md). On a new machine, a fresh copy, or when hooks, skills or tools misbehave, run `python tools/agent-workflow/setup_project.py check` (skill `agent-setup`).
- **Finish** with a scoped report: behaviour, the actual commands and their output, `not_run`, changed files, open issues, Git status.

## 13. Project management and multi-agent policy

- Dispatch (models per CLI and role, effort, review routing, quotas): [.agents/policy/PM_POLICY.md](.agents/policy/PM_POLICY.md). Briefs follow the [template](.agents/policy/AGENT_TASK_TEMPLATE.md), include the role pack for the worker's role and the wrapper for its CLI ([.agents/policy/cli/](.agents/policy/cli/)), and end with the verbatim [footer](.agents/policy/AGENT_BRIEF_FOOTER.md). The user's standing rules: [.agents/policy/USER_STANDING_RULES.md](.agents/policy/USER_STANDING_RULES.md).
- **Reports** use the assignment's format (ORCHESTRATION section 8.4); the full report goes in the task file with the [handoff-report](.agents/skills/handoff-report/SKILL.md) sections and the four labels: **Current status**, **What was already done**, **What the next agent must do**, **What must not be repeated**.
- Keep user requirements, PM proposals, agent findings and live evidence apart; only the user promotes a proposal; state the model and effort that were actually set.
- **Review:** a fresh agent that did not write the change reviews it once: correctness, failure paths, security, configuration audit, lifecycle, tests, docs. Findings carry severity, file:line, reproduction, observed versus expected and a minimal fix; only proven P0-P2 defects block. The implementer fixes the full list in one attempt; re-review covers the delta. High-risk work (security, evidence binding, authorization, GPU or process lifecycle, architecture) gets a pre-mortem and two reviewers of different families. At most three counted fix rounds, then one question to the user.
- **Run each check once** on the same bytes; update docs once, at acceptance; hand off on one page.
- **Cold read (R-028):** after documentation is written or restructured, fresh Haiku, Sonnet and Opus readers report what they understood and what was unclear; the PM fixes the text and relays their questions to the user (PM_POLICY section 8).

<!-- ORCHESTRATION:START -->
### Agent orchestration (orchestrator only; if you are an assigned agent, skip this block)
- CURRENT MODE: DELEGATE
  - DELEGATE (default): delegate to Claude/Codex/Antigravity agents following `.agents/ORCHESTRATION.md`.
  - SOLO: the orchestrator does everything itself, creates no agent and delegates nothing.
  - Only the user changes the mode. On a permanent change request, update the "CURRENT MODE" line in AGENTS.md (CLAUDE.md imports it).
- In DELEGATE mode: before assigning work, read `.agents/ORCHESTRATION.md` and follow it. After a compaction, or when unsure of a rule, read it again.
- Agent state: `.agents/registry.json`. Log: `.agents/log.md`.
<!-- ORCHESTRATION:END -->

<!-- AGENT-WORKER:START -->
### When you are an assigned agent (dev or QA)
- You may receive several consecutive tasks in one session. Each message starting with [NEW TASK], [QA FEEDBACK], [EVALUATE] or [RETEST] is a separate piece of work.
- Work inside the current task's scope. Change earlier work only when the message asks; if you must, give the reason in the report.
- Report in the format the assignment message asks for.
- Keep the model, effort and session configuration you were started with.
- Create sub-agents only when the assignment message asks for it.
<!-- AGENT-WORKER:END -->
