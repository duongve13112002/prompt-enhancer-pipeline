# User standing rules (injected whole at every PM session start)

Only rules the user stated, one per line: `Sxx | rule | source | check`. `remembered only` = no mechanical gate yet. Decisions: `tasks/DECISIONS_INDEX.md`. Rules already in AGENTS.md are not repeated. S01 to S33 were carried from the user's earlier project (source column "seed rules" or a date before 2026-11-11); S05 and S08 to S12 named that project's runs, budgets and branches and are not used here; IDs are never reused.

S01 | Never assume; unclear means ask. Ask only what is the user's: new product meaning, overriding a decision, spend beyond budget | seed rules 2026-10-22 | remembered only
S02 | Do every decidable next step yourself; the user must not chase | seed rules 2026-10-11 "đừng để tui giục" | remembered only
S03 | Never re-ask a settled point: search DECISIONS_INDEX, the archive and `recall.py search` first | seed rules | `question_guard.py` (warns)
S04 | Outcome-deciding defaults (route, verifier, deadline, fail-closed) go to the user with an example BEFORE an expensive run; smoke one job and read its record | seed rules 2026-10-18 | `run_approval_gate.py check`
S06 | Questions to the user: recommendation and example per question, end with "Việc bạn cần làm:" | seed rules 2026-10-11 | `question_guard.py` (warns)
S07 | GPU: `nvidia-smi` first, tell the peer "starting GPU job" and "GPU free", busy = recheck every 30 min and never kill, one heavy model | seed rules; `.agents/policy/AGENT_PEER_RULES.md` | remembered only
S13 | Every user decision enters DECISIONS_INDEX in the same session with an `applied:` marker | seed rules | `decisions_applied.py check`
S14 | Delegate per `.agents/ORCHESTRATION.md` (read it before each assignment); at most 8 running agents; Claude workers only via `cli_worker.py` | seed rules | `cli_worker.py`
S15 | Reuse a worker only within the same task (fix rounds, next phase on the same worktree); a new task gets a fresh worker and worktree; retire a worker over about 250K | changed by the user 2026-11-13 (decision 45) | remembered only
S16 | Every brief: Step 0 then STOP (small tasks: Step 0 atop the report, then continue; Codex still two messages), footer, `Kind:`, R-IDs (feature), class test sets (code) | seed rules; changed 2026-11-13 (decision 49) | `brief_lint.py` via `cli_worker.py start/send`
S17 | Effort per task: start at the lowest effort that can meet the gate; raise effort before changing model; `high` for builds, P2+ and high-risk reviews | seed rules; 2026-11-10 model guidance (N093) | remembered only
S18 | Models and reviewers per `.agents/policy/PM_POLICY.md` section 2: Gemini 3.8 Flash reviews ordinary work first once task T-024 lets it review uncommitted patches; high-risk = pre-mortem plus 2 reviewers of different families; Fable 5.2 only after Opus 5.5 `high` fails | user 2026-11-10 (Fable 5.1 to 5.2) | remembered only
S19 | Relay every worker finding in full, conflicts with decisions first; check a cited decision verbatim before relaying a question | seed rules | remembered only
S20 | Each own mistake gets a line in LESSONS_LEARNED with its prevention rule or gate | seed rules 2026-10-17 | remembered only
S21 | Scout audit for forgotten plan items once per wave and before a long absence | seed rules 2026-10-28 "thường xuyên kiểm tra plan" | `audit_brief.md`
S22 | Periodic audit: Haiku 5.5 read-only scout, one-shot; daily while active and before GPU rounds; met / not met / cannot tell + evidence; the PM verifies every "not met" | seed rules 2026-10-29 | `audit_brief.md`, `audit_extract.py`
S23 | Config everything: no source constant for a limit, window, provider or model; an absent key is refused | seed rules | config sweep tests
S24 | Lean live files within their character budgets; repository text English, replies to the user in Vietnamese | seed rules | `doc_budgets.json` via `session_context.py`
S25 | Facts from the store or the run record, never from memory | seed rules 2026-10-18 | remembered only
S26 | Supervisor every 45 min while work is active (never above 50, never 120): it wakes the PM to check status and keeps the PM's prompt cache warm; procedure in PM_POLICY section 3 | seed rules; user 2026-11-10 | `pm_invariants.py`
S27 | No hardcode to pass, nor as a checker: anything that may vary is config or model; no per-language rules | seed rules 2026-11-07 | remembered only
S28 | A status-update request also refreshes `tasks/STATUS.md` | seed rules | remembered only
S29 | Wait for the user only on a truly important decision, else keep working | seed rules 2026-10-28 | remembered only
S30 | Prefer the ambitious, experiment-proven option over the safest; budgets, gates and S01 unchanged | seed rules | remembered only
S31 | No fix loops: root cause + full audit of the bug class BEFORE any fix; one complete fix | seed rules | `brief_lint.py` (RCA section in fix briefs)
S32 | All three agent CLIs run on subscriptions: check the 5-hour and weekly usage before every dispatch round; use Codex and Antigravity until their quota runs out, Claude workers until 90% (the rest is the orchestrator's); a provider that reports no limit is not checked; move work to another provider; at a known reset time schedule a wake and resume only approved tasks | user 2026-11-10; changed 2026-11-13 (decisions 44, 46) | `usage_snapshot.py`
S33 | Goal of agent orchestration: the fewest tokens for the best quality | user 2026-11-10 | remembered only
S34 | No hardcoding, no rule-based logic, no tricks: defaults and thresholds come from configuration, models and experiments | user 2026-11-11 (decision 18) | config sweep tests
S35 | CI runs on the latest Ubuntu and on Windows; the repository is public only for the run and private again when it ends, green or red; confirm it is private at session start | user 2026-11-11 (decision 26) | remembered only (task T-022 adds the gate)
S36 | After documents are written, fresh Haiku, Sonnet and Opus readers check they understand; their questions go to the user | user 2026-11-11 (decision 27) | PM_POLICY section 8
S37 | Every claim in a report carries its evidence pointer; "unknown" is a valid answer; nothing is invented | user 2026-11-11 (decision 24) | remembered only (task T-021 adds the report lint)
S38 | Cloud models are optional backends chosen in the configuration (Claude, default Haiku 5.5; Gemini, default gemini-3.1-flash-lite); the default is local; keys live in `.secrets/`; ask the user how many requests are allowed before any cloud call | user 2026-11-11 (decision 28) | remembered only
S39 | The user makes every commit and every push; no agent commits, pushes or merges; the PM proposes the commit message | user 2026-11-11 (decisions 36, 37) | AGENTS.md section 11
S40 | The orchestrator cleans `.tmp` itself: after each task, at session start and end, on every wake, orphan trees past the configured age; never protected trees | user 2026-11-13 (decision 43) | remembered only (task T-025 adds the sweep)
S41 | Codex image generation tests the flow; generate only what a test needs and reuse images, because it spends the Codex subscription | user 2026-11-13 (decision 47) | remembered only
S42 | Fix defects of the existing agent tooling found while running agents yourself, without waiting for a task command; new tooling features stay in T-020 to T-026; report each fix and have another family review it when one is available | user 2026-11-14 (decision 72) | remembered only
