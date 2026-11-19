# User standing rules (injected at each PM session start)

Only rules the user stated: `Sxx | rule | source | check`. `remembered only` = no mechanical gate yet. Decisions: `tasks/DECISIONS_INDEX.md`. Not repeated from AGENTS.md. "Seed rules": the user's earlier project; IDs never reused.

S01 | Never assume: test on a real run first (decision 99); unclear means ask. Ask only what is the user's: new product meaning, overriding a decision, spend beyond budget | seed rules 2026-10-22 | remembered only
S02 | Do every decidable next step yourself; the user must not chase; while away work every unblocked task until none is left (decision 95) | seed rules 2026-10-11 "đừng để tui giục" | remembered only
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
S22 | Periodic audit: Haiku 5.5 read-only scout; daily while active and before GPU rounds; met / not met / cannot tell + evidence; the PM verifies every "not met" | seed rules 2026-10-29 | `audit_brief.md`, `audit_extract.py`
S23 | Config everything: no source constant for a limit, window, provider or model; an absent key is refused | seed rules | config sweep tests
S24 | Lean live files within their character budgets; repository text English, replies to the user in Vietnamese | seed rules | `doc_budgets.json` via `session_context.py`
S25 | Facts from the store or the run record, never from memory | seed rules 2026-10-18 | remembered only
S26 | Supervisor every 45 min while work is active (never above 50): it wakes the PM and keeps its prompt cache warm; PM_POLICY section 3 | seed rules; user 2026-11-10 | `pm_invariants.py`
S27 | No hardcode to pass, nor as a checker: anything that may vary is config or model; no per-language rules | seed rules 2026-11-07 | remembered only
S28 | A status-update request also refreshes `tasks/STATUS.md` | seed rules | remembered only
S29 | Wait for the user only on a truly important decision, else keep working | seed rules 2026-10-28 | remembered only
S30 | Prefer the ambitious, experiment-proven option over the safest; budgets, gates and S01 unchanged | seed rules | remembered only
S31 | No fix loops: root cause + full audit of the bug class BEFORE any fix; one complete fix | seed rules | `brief_lint.py` (RCA section in fix briefs)
S32 | Subscriptions: check 5-hour and weekly usage before each dispatch round; Codex and Antigravity until their quota runs out, Claude workers until 90%, else move providers; at a known reset schedule a wake, resume only approved tasks | user 2026-11-10; changed 2026-11-13 (decisions 44, 46) | `usage_snapshot.py`
S33 | Goal of agent orchestration: the fewest tokens for the best quality | user 2026-11-10 | remembered only
S34 | No hardcoding, no rule-based logic, no tricks: defaults and thresholds come from configuration, models and experiments | user 2026-11-11 (decision 18) | config sweep tests
S35 | CI runs on the latest Ubuntu and on Windows; the repository is public only for the run and private again when it ends, green or red; confirm it is private at session start | user 2026-11-11 (decision 26) | remembered only (task T-022 adds the gate)
S36 | Fresh Haiku, Sonnet and Opus readers cold-read new documents; their questions go to the user | user 2026-11-11 (decision 27) | PM_POLICY section 8
S37 | Every report claim carries its evidence pointer; "unknown" is a valid answer; nothing is invented | user 2026-11-11 (decision 24) | remembered only (task T-021 adds the report lint)
S38 | Cloud models are optional backends chosen in the configuration (Claude, default Haiku 5.5; Gemini, default gemini-3.1-flash-lite); the default is local; keys live in `.secrets/`; ask the user how many requests are allowed before any cloud call | user 2026-11-11 (decision 28) | remembered only
S39 | The orchestrator commits and pushes verified work itself, without asking and without force; workers never commit, push or merge | user 2026-11-11 (decisions 36, 37); changed by the user 2026-11-14 (decision 77) | AGENTS.md section 11
S40 | The orchestrator cleans `.tmp` after each task, at session start and end and on every wake (orphans past the configured age); never protected trees | user 2026-11-13 (decision 43) | remembered only (task T-025 adds the sweep)
S41 | Codex image generation tests the flow; generate only what a test needs and reuse images (it spends Codex quota) | user 2026-11-13 (decision 47) | remembered only
S42 | Fix defects of existing agent tooling yourself, no task command needed; new tooling features stay in T-020 to T-026; report each fix, another family reviews it | user 2026-11-14 (decision 72) | remembered only
S43 | Spread work by each CLI's strengths across Claude, Codex and Antigravity; where Codex and Claude fit equally, alternate or draw between them; no single agent does all the implementation | user 2026-11-14 (decision 83) | remembered only
S44 | Pipeline code first, tooling second (workers, fixes) | user 2026-11-15 (decision 100) | remembered only
S45 | Spare the shared SSD: no whole-tree copies, one fixed basetemp deleted after each run, focused tests, delete `.tmp` and worktrees at once, few workers | user 2026-11-16 (decision 129) | brief footer rule 18
S46 | Mandatory: model servers busy to configured capacity (concurrent requests, no stage-wide barrier); remove repeated work first; never trade accuracy for speed; back off only on real 429, overload or OOM; a differential test proves each faster path equal to the serial one; machine limits hold | user 2026-11-17 (decision 149) | remembered only (task RES-1)
S47 | Lean process: 1 build, 1 review, max 1 fix round; diff-only retest; task file ~20 KB; 1 reviewer unless high-risk; no Step 0 stop once the cause is clear; PM measures quality | user 2026-11-18 (decision 164) | remembered only
S48 | Every build brief asks for a pre-mortem (the builder lists how its change could fail, by class, and tests each before handing in); send the retest as soon as the fix lands, and when the reviewer has idled long past its cache window give the diff-only retest to a fresh reviewer; the PM keeps measuring whether coordination improves | user 2026-11-19 (decision 169) | remembered only
