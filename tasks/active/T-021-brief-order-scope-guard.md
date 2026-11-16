# T-021: brief order, scope guard and report lint

Kind: code (tooling). R-IDs: R-021. Depends on: T-024, T-026 (both accepted). Decisions 24, 80, 97, 105. Family split (decision 91, tooling): Claude builds, Codex reviews; Gemini scouts first and reviews first, but is never the only reviewer.

## Goal

Three tooling changes, from [IMPLEMENTATION_PLAN](../IMPLEMENTATION_PLAN.md) row T-021:

1. **Brief order:** `brief_lint.py` checks that a brief puts context first and the task last. Long-context guidance places the material before the request: see the notes N078 and N127 in `docs/note/`, and re-read them.
2. **Scope guard:** a hook that blocks a worker's file edits outside its brief's `Scope`, and leaves the orchestrator unaffected.
3. **Report lint:** it requires the done-check output and evidence pointers in a worker's Completion report (R-021, S37).

All three stay compatible with briefs already in flight, or give a migration path: see `tasks/STATUS.md` "Questions" item 4.

## Scope

- In scope: `tools/agent-workflow/` (`brief_lint.py`, the hooks under `hooks/`, `settings.example.json`, `cli_worker.py` where it calls the lints), their tests in `tools/agent-workflow/tests/`, `tools/agent-workflow/README.md`, `.agents/policy/AGENT_TASK_TEMPLATE.md`, `.agents/policy/AGENT_BRIEF_FOOTER.md` and the role packs, if their order must change.
- Out of scope: pipeline code (`src/`, `tests/`), `AGENTS.md` rule meaning (propose a change, do not make it), and `.secrets/`.

## Research required before code

The scout note `.tmp/T-021/research.md` answers these:

1. **Today's brief lint:** what `brief_lint.py` checks now, how `cli_worker.py start/send` call it, and how waivers work. Give file:line.
2. **The hooks:** which hooks exist (`hooks/`, `settings.example.json`), how a hook knows that a session is a worker or the orchestrator, and where a brief's `Scope` could be read from (the task file, the brief, or the worker registry).
3. **Report shape:** what a Completion report holds today (`.agents/policy/AGENT_TASK_TEMPLATE.md`, `handoff-report` skill), and what a lint could check structurally, without rules over natural language.
4. **Claude Code hook events:** which events and matchers can block an edit (PreToolUse on Write, Edit, PowerShell and Bash), from the primary docs. The notes N062 and N063 in `docs/note/` are leads.
5. **Codex and Antigravity workers:** what a Codex or Antigravity worker can enforce in the same way, or why it cannot.

## Step 0

Builder: Claude Opus, 2026-11-15, worktree at `b4eb856`. R-021 (evidence pointers), R-015 (no silent defaults, no word lists), R-025 (Windows and Linux).

### Re-read evidence (scout claims I rely on, and primary docs)

| Claim | Pointer | Class |
|---|---|---|
| A full lint runs on `start`, or on a `send` that has a `[NEW TASK` line or a `Kind:` header | `tools/agent-workflow/brief_lint.py:374-382`, `cli_worker.py:556` | source-verified |
| Everything after the footer heading is cut before linting, so a task block after the footer is invisible today | `brief_lint.py:97-99` (`_strip_footer`), `.agents/policy/cli/README.md:11` | source-verified |
| Lint findings refuse the launch; `--lint-waive` logs to `lint-waivers.jsonl`; a linter crash also refuses | `cli_worker.py:543-595` | source-verified |
| Claude worker hooks come only from the per-turn `--settings` JSON built from the registry entry; the PM's hooks come from `settings.example.json` with `--git-mode pm` | `cli_worker.py:465-528,1332-1341`; `settings.example.json:25-29` | source-verified |
| Shell writes, for workers and the PM, are already limited to `write_guard.allowed_dirs` (`.tmp`, `runs`) | `hooks/tool_guard.py:5,389-414`; `guard_config.json:27-30` | source-verified |
| `Edit` and `Write` are never blocked by `tool_guard` | `hooks/tool_guard.py:62-68,1759-1762` | source-verified |
| Native writes are already denied by permission rules in the main tree's tooling, `.agents/`, `.git`, `.claude` and the state directory | `cli_worker.py:112-134` | source-verified |
| Workers run with `--permission-mode dontAsk`, `--add-dir <project>`; `NotebookEdit` is a denied tool | `cli_worker.py:73-77,1342-1345` | source-verified |
| Codex `apply_patch` targets are already parsed (`patch_targets`) and checked by `target()`; the policy is a root-owned file checked by digest, and it already carries `task_file` | `hooks/codex_guard.py:113,287-294,419-443`; `codex_hooks.py:308-322` | source-verified |
| **Correction to the scout:** `turn_git_changes` compares refs, root refs, worktree HEADs and reflog fingerprints, not changed file paths, so today it is no scope backstop | `cli_worker.py:1664-1668,1708-1716` | source-verified |
| Antigravity has no tool hook we can call; its isolation is the copied workspace | `gemini-ws.ps1:1-26`; `docs/note/N130-antigravity-rules-and-headless.md` (agent-read lead, not re-read) | documented (lead) |
| Hook matcher: "Letters, digits, `_`, `-`, spaces, `,`, `|`: Exact string, or list of exact strings separated by `\|` or `,`" | https://code.claude.com/docs/en/hooks | documented |
| PreToolUse exit 2 "Blocks the tool call"; "The message that comes with the block is your hook's stderr." | same URL | documented |
| "Hook entries merge across settings levels rather than replacing each other"; "its matching hooks run in parallel" | same URL | documented |
| Fail closed: "To block the action instead, set `\"onFailure\": \"block\"` on a `command` or `http` hook. Requires Claude Code v2.1.295 or later." It covers "Can't start", "Exit code other than 0 or 2", "Timeout", "Invalid output". Without it, "On `PreToolUse`, a timed-out `command` ... hook doesn't block the tool call" | same URL. Installed: `claude --version` → `2.1.325 (Claude Code)` | documented, experiment |
| "For the file tools `Write`, `Edit`, and `Read`, `tool_input.file_path` is always absolute." | same URL | documented |
| Long context: "Place your long documents and inputs near the top of your prompt, above your query, instructions, and examples." "Queries at the end can improve response quality by up to 30 percent in tests" | https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices (re-read 2026-11-15; N127 matches) | documented |
| N078: the middle of a long input is used worst (retrieval QA, not rule following) | `docs/note/N078-lost-in-the-middle.md:12-16`; arXiv not re-read | documented (lead) |

**The "known" failure, checked.** At `eb3df16` (tracked tree written to scratch with read-only `git ls-tree`/`git cat-file`; `git archive` is refused for workers), `test_slot.py -- python -m pytest -q tools/agent-workflow/tests/test_setup_project.py` gave `33 passed in 9.37s`. At this worktree's base `b4eb856` the same command gives `2 failed, 12 passed`: `test_the_real_checkout_has_no_failures` and `test_apply_binds_a_copy_and_is_idempotent`, both `foreign: 11 previous-project token(s); first: tasks/active/T-007-k0-sync.md:275`, which `eb3df16` fixed. So it is a base-only failure; a rebase onto `eb3df16` removes it.

### Unknowns and failure modes

- A hook that crashes or times out lets the edit through unless it sets `onFailure: "block"`.
- The footer is appended last today; moving the task after it changes what the lint sees, so every rule then also reads the task block.
- Workers already in flight carry the old order; a new blocking rule must not stop their next `[NEW TASK phase n]`.
- Shell writes through a program the guard does not model (`python script.py` writing a tracked file) are not visible before the call.
- Windows: paths are case-insensitive, can use `\`, and can reach the file through links or junctions; the minimum Python is 3.11 (`.agents/project.json`), so `PurePath.full_match` (3.13) is not available.

### Design

**1. Brief order (rule `brief-order`, structure only).** "Context first, task last" means the brief ends with one task block: a fixed heading `## Task` (protocol-bound marker, like `FOOTER_HEADING`) outside fences, placed after the footer's verbatim span and after the role pack's `## Role:` heading, and it is the last level-1 or level-2 heading. The Goal, Done check, Scope, Out of scope and Output go inside it; the header lines (`Kind:`, `R-IDs:`, `Task file:`, ...) may stay at the top. No wording is checked. `_strip_footer` changes to remove only the footer's verbatim span when it is found (the old cut at the heading stays as the fallback when it is not), so the task block after the footer is linted by every rule. The footer text stays byte-identical, so briefs in flight still match it. Migration (STATUS, PM proposals item 4): `start` records `brief_order: 2` in the registry entry; a `send` to a worker without that marker runs every old rule but not `brief-order`; the standalone `brief_lint.py --brief` checks the order unless `--legacy-order` is given. Template, cli wrapper and role pack texts that say "footer last" get the new order.

**2. Scope guard (Claude: new hook `hooks/scope_guard.py`).**
- **Source:** structured, never parsed from prose: `cli_worker.py start --scope <glob>` (repeatable; project-relative to the worker's checkout, `/` separators, `**` allowed), validated at start (no absolute path, no drive, no `..`, not empty, not a pattern that matches everything) and stored as `hooks.scope` in the registry. Always writable as well: the registered `--task-file` and the scratch folders `write_guard.allowed_dirs` of `guard_config.json` (one source with the shell guard). `start` without `--scope` means read-only apart from those (fail closed); `--scope-guard off --scope-waive "<reason>"` is the logged way out, like `--lint-waive`. `send --scope ...` replaces the list and is logged.
- **Event and matcher:** `PreToolUse`, `matcher: "Write|Edit"` (exact list per the docs; `NotebookEdit` is already a denied tool), with `onFailure: "block"` and `timeout: 30`.
- **Worker against orchestrator:** the `--git-mode` pattern: the hook is added only to the worker's `--settings` (`hook_settings`) with its arguments `--cwd <checkout> --session <session_id> --allow <glob>...`; `settings.example.json` never names it, so the PM session is untouched. The hook also refuses an event whose `session_id` differs from `--session`, so a copy in the wrong settings blocks rather than passes.
- **Decision:** resolve `tool_input.file_path` (links resolved, as `codex_guard.target` does), require it inside the resolved checkout, take the relative POSIX path (case-folded on Windows), and match it against the allowed globs with a small segment matcher of our own (`*` within a segment, `**` across segments). Exit 2 with the allowed list on stderr when outside; exit 2 as well on missing or malformed arguments, unreadable `guard_config.json`, a missing or non-string `file_path`, or a path that cannot be resolved.
- **Shell tools:** no new shell matcher: `write_guard` already confines Bash, PowerShell and Monitor writes to `.tmp/` and `runs/`, which are inside every scope. What it cannot see (a script run by an interpreter) gets a backstop: after each turn `cli_worker` lists the checkout's changed paths with read-only `git status --porcelain=v1 -z --untracked-files=all` and records those outside the scope in the registry (`scope_changes`) with a stderr warning, the way `git_changes` is recorded.
- **Codex:** the launcher writes `scope` into the root-owned, digest-checked policy, and `codex_guard.check_tool` checks every `apply_patch` target against it with the same matcher (shell writes already pass through `tool_guard.checks`). The same turn-end path audit runs.
- **Antigravity:** no tool hook exists, so nothing runs during the turn; isolation stays the copied workspace of `gemini-ws.ps1`, and its workers are scouts and reviewers whose notes the PM copies by hand. Documented as a gap, not hidden.

**3. Report lint (new `report_lint.py`, structure only, no word lists).** Input: a task file; it reads the `## Completion report` section. A section with no table, no fenced block and no `###` heading counts as not yet written and is skipped. Otherwise:
- `done-check-output`: a non-empty fenced code block between the `Result and evidence` label (the template's bold label or a `###` heading) and the next heading;
- `evidence-table`: the `### Research evidence` table has the template's five columns and at least one data row; each row's class cell is one of the template's four classes (protocol-bound vocabulary, like `KINDS`);
- `evidence-pointer`: each row's source cell holds at least one pointer of a fixed form: `path:line` or `path:line-line`, an `http(s)://` URL, or an inline code span (a command).
It runs standalone (`report_lint.py <task file>`, exit 0 or 1, `path:line: [rule] text`) and after each worker turn on the registered task file, where findings are recorded in the registry and printed as a warning (the turn has already ended, so it cannot block).

### Phasing (each phase its own patch, review by Codex, and PM acceptance)

1. **P1 brief order:** `brief_lint.py`, `cli_worker.py` (registry marker), template, cli wrapper texts, role packs, README, tests.
2. **P2 report lint:** `report_lint.py`, the turn-end call in `cli_worker.py`, README, tests. It is independent of P1.
3. **P3 scope guard:** `hooks/scope_guard.py`, `hook_settings` and the start and send flags, the turn-end path audit, Codex policy and `codex_guard`, README, tests. Last because it is the largest and changes what workers may do.

### Questions (ranked, each with a proposed conservative answer)

1. **Where does the scope come from?** Proposed: the `--scope <glob>` start flag in the registry (above), the brief's `<scope>` prose stays for the reader, and no start without it except through a logged `--scope-guard off --scope-waive`. Rejected: parsing a brief header (two-source drift and text parsing).
2. **Files outside my Scope that the new order touches:** `.agents/policy/cli/README.md:11` and `cli/claude.md` say "footer last", and `PM_POLICY.md:65` gives the assembly order. Proposed: the PM adds the two `cli/` files to my Scope and edits `PM_POLICY.md:65` itself (PM-owned).
3. **Briefs in flight:** proposed: the registry marker above; old workers keep the old order until retired, and nothing blocks them.
4. **Should the report lint ever block?** Proposed: advisory at turn end, plus a PM step "run `report_lint.py` before accepting", which is a PM_POLICY change the PM decides on. No blocking in this task.
5. **Base:** proposed: rebase this worktree onto `eb3df16` before P1, so the full tool suite has no base-only failure; otherwise I report those two failures as base-only with the evidence above.

### Plan and finish checks

- **P1:** property tests (Hypothesis, as in the current suite) on `brief-order`: a random order of the context blocks with the task block last passes, any context block moved after it fails, fences hide the marker; tests that a legacy-marked worker's `send` skips the rule and a new one is refused; a test that the git-write rule now reads text after the footer. Mutants (about 6): flip the order comparison, drop the last-heading check, ignore fences, take the legacy branch always, keep the old cut, accept two task blocks.
- **P2:** property tests on the section and table parser (random tables, separators, CRLF); unwritten section skipped; each rule failing alone. Mutants (about 6): accept an empty fence, accept a missing column, accept an unknown class, accept a pointer-free source cell, find the fence after the next heading, treat a written section as unwritten.
- **P3:** property tests on the glob matcher (against a brute-force reference on random paths and patterns) and on scope validation; hook tests for inside, outside, task file, scratch, links or junctions, `..`, the main tree reached through `--add-dir`, a wrong session, bad arguments and bad JSON (all exit 2); the `onFailure` field present in `hook_settings`; Codex `apply_patch` inside and outside; the turn-end audit records an out-of-scope path; override tests (a non-default `--scope`, a non-default `allowed_dirs`) and refusal tests (absolute, `..`, empty, match-all, `--scope-guard off` without a reason). Mutants (about 8): drop the session check, skip link resolution, case-sensitive compare on Windows, `*` crossing `/`, allow outside cwd, exit 0 on bad arguments, drop the task-file allowance, drop `onFailure`.
- **Every phase:** `ruff check` and `ruff format --check` on changed files with `tools/agent-workflow/ruff.toml`; `test_slot.py -- python -X faulthandler -m pytest -q -n 2 tools/agent-workflow/tests` green (or only the two base-only failures if Q5 is answered no); the Configuration audit rows for `--scope`, `--scope-guard`, `--scope-waive`, the `brief_order` marker and `--legacy-order`; scratch removed.

## Phase P1: brief order

PM answers (2026-11-15): Q1 yes (`--scope` belongs to P3), Q2 yes (`.agents/policy/cli/README.md` and `cli/claude.md` added to Scope; the PM edits `PM_POLICY.md:65` itself), Q3 yes (registry marker), Q4 advisory, Q5 no rebase: the two `test_setup_project.py` failures are base-only (evidence in Step 0).

### Configuration impact

- `brief_order` registry marker (new, per worker, written by `cli_worker.py start`): value `2` means the worker was started under the `brief-order` rule; absent means a worker from before T-021 (legacy order, the rule is skipped on its sends); any other value refuses the send with a precise message. Protocol-bound marker value, set by the launcher only, never by a user.
- `brief_lint.py --legacy-order` (new CLI flag, standalone use): skips `brief-order` for a brief written in the old order; refused without `--brief`, where it means nothing.
- `lint_message(..., order=)` (new required keyword): every caller states whether the order rule runs; there is no default.
- `## Task` heading (`TASK_HEADING`): protocol-bound marker of the task block, documented in the template, like `FOOTER_HEADING`.
- No deployment, model, path, device or timeout setting changes.

### Steps

1. `brief_lint.py`: `_strip_footer` now cuts only the span from the footer heading to the next `## Task` line outside fences, blanking its characters but keeping its line breaks; without such a line it cuts to the end as before. New `_order_findings` (rule `brief-order`), new required keyword `order` on `lint_message`, new CLI flag `--legacy-order` (refused without `--brief`).
2. `cli_worker.py`: `BRIEF_ORDER_KEY`/`BRIEF_ORDER = 2` written into new Claude and Codex entries; `brief_order_applies` decides per `send` (absent: legacy; `2` as an int: order; anything else: refused); `lint_gate` passes `order` explicitly.
3. Texts: template Brief header gets the "Brief order" line; `cli/README.md:11` and `cli/claude.md:4` describe the closing `## Task` block; `audit_brief.md` now has a `<FOOTER>` placeholder line above its `## Task` block (Output and Hard limits became `###`); tools README rows updated.
4. Tests: the `brief()` helper builds the new order and `legacy_brief()` the old one; 3 property tests and 11 unit or gate tests for the order rule; marker tests in `test_cli_worker.py` and `test_cli_worker_codex.py`; the audit test fills `<FOOTER>`.
5. Mutants (scratch copy, through `test_slot.py`): 9 of 9 killed; then the full tool suite.

### Exact replacement proposed for `PM_POLICY.md:65` (the PM edits it)

Fenced so its links, which are relative to `.agents/policy/`, are not read from this folder:

```markdown
- **Brief assembly (context first, task last, T-021):** CLI wrapper for the worker's CLI ([cli/](cli/README.md)), the template header lines, the context material, the role pack for the worker's role ([roles/](roles/README.md)), the footer, then one closing `## Task` block with Goal, Done check, Scope, Out of scope and Output ([AGENT_TASK_TEMPLATE](AGENT_TASK_TEMPLATE.md), Brief order). A worker receives only its own role pack.
```

### Configuration audit (P1)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| `brief_order` registry marker | written by `cli_worker.py start` (`claude_entry`, Codex entry); read by `brief_order_applies` | absent means legacy, int `2` means order, any other value refuses the send | written on every start; no user input | override: `test_a_send_follows_the_registry_marker` (absent marker lets an old-order brief through, the marker holds it to the order), `test_start_records_the_brief_order_marker`, the Codex assertion in `test_start_command_shape_prompt_on_stdin_and_inline_permissions`; refusal: `test_an_unknown_marker_refuses_a_brief_send` (3, "2", True, None, 2.0) | protocol-bound (launcher-written schema marker) |
| `brief_lint.py --legacy-order` | argparse flag in `brief_lint.main` | refused without `--brief` (exit 2) | README Brief gate line | override and refusal: `test_the_cli_legacy_order_flag` (old-order brief: 1 without the flag, 0 with it; the flag alone exits 2) | configurable (CLI choice) |
| `lint_message(order=)` | required keyword, no default | a caller that omits it fails with `TypeError` | every caller passes it (`cli_worker.lint_gate`, `brief_lint.main`, tests) | `test_the_old_order_passes_only_without_the_order_rule` (both values) | configurable (explicit caller choice) |
| `TASK_HEADING = "## Task"` and the exact-line pattern | `brief_lint.py` constant | exact line, fences hide it, `## Task brief` is not it | AGENT_TASK_TEMPLATE Brief order line | `test_the_task_heading_is_exact_and_fences_hide_it` | protocol-bound (documented layout marker) |
| Footer span end | `_strip_footer` | ends at the first unfenced `## Task` line after the heading, else end of text | none | `test_the_footer_span_keeps_line_numbers_and_a_header_in_the_task_block_makes_a_brief`, `test_the_task_block_after_the_footer_is_linted_with_its_own_line_numbers` | protocol-bound |

## Phase P1 fix round 1: one Markdown block scanner

Codex review `.tmp/T-021/review-p1/review.md`: FAIL, F1-F3 (P2). R-021, R-015, R-025.

### Step 0 (plan pre-approved by the PM)

- **Root cause:** the lint read Markdown with separate partial line regexes: a backtick-only fence toggle (`_FENCE`, `_task_line_after`, `_order_findings`, `_blocks`), column-0 ATX only (`_TOP_HEADING`), and a footer heading prefix never bound to the verbatim footer (`_strip_footer`, `_order_findings`).
- **Unknowns:** lists and block quotes are containers that I do not model (the brief names leaf blocks only). An indented-code reading would hide nested list text from `git-write`, so lines indented 4 or more columns stay text.
- **Questions:** none open. The brief fixes the design; the answer I chose is "an unreadable footer fails closed in `brief-order` too".
- **Plan:** (1) `_scan` plus span helpers; (2) move every reader to it; (3) probe refusal tests, property tests, class-audit tests; (4) mutants, focused suite, full suite. **Finish:** the 6 probes refuse, the tests fail on the pre-fix file and pass after it, the mutants are killed, the suites are green apart from the two base-only failures.

### Design

`brief_lint._scan(text)` returns per-line fenced-code flags and the headings `(first line, level)`. It follows CommonMark 0.31: fenced code 4.5 (backtick or tilde, opener of length 3 or more with up to 3 spaces of indent, a backtick info string without backticks, closer of the same kind and at least the same length with only trailing whitespace, unclosed runs to the end); ATX 4.2 (1-6 `#`, up to 3 spaces); Setext 4.3 (`=`/`-` run under an open paragraph) before thematic breaks 4.1; paragraphs 4.8; a tab stop of 4. The footer span `_footer_span` is the first line-aligned copy of the cleaned footer (`_clean` line comparison) whose first line is outside a fence. `_strip_lines`: new order (a `## Task` marker after the span) blanks exactly the span. Old order: from the span to the end, as before. Footer unknown or not verbatim: from the first footer heading outside code to the next marker, else to the end. `brief-order` needs a marker, the span ending before it, and no level 1-2 heading after it. The marker stays exact (`## Task` at column 0), so an indented or Setext "Task" heading after it counts as a late heading. The footer text now reaches every strip: `lint_brief`, `is_brief_message` and `task_file_findings` take a required `footer=` keyword, and `cli_worker.lint_gate` reads the footer before them.

Rejected: a Markdown library (a new dependency, which the brief forbids). Also rejected: reading 4-space lines as indented code (it would hide nested list items, which are text, because lists are not modelled).

### Class audit (every Markdown read by a line regex in `brief_lint.py` and `cli_worker.py`)

| Site | Before | Now |
|---|---|---|
| `_strip_footer` / `_task_line_after` | heading substring, backtick toggle | scanner + verbatim span (F1) |
| `_order_findings` | backtick toggle, column-0 ATX, heading prefix | scanner (F1-F3) |
| `_blocks` (git-write paragraphs) | `startswith("```")` toggle | scanner code flags |
| `header_fields` (Kind, Task file, ...) | `_FENCE` toggle | scanner code flags |
| `_SETS_WAIVER` in `_brief_findings` | every line, a fenced waiver waived | outside fences only |
| `_slice_ids` (task-file table) | fences ignored | outside fences only |
| `main --require-footer` | substring anywhere | footer heading outside fences (`_footer_heading`) |
| `_line_findings` (gemini path, inline Python) | every line | safe: commands live in code blocks by design |
| `_base_url_findings` | whole body | safe: "Checked in code and quotes too" by design |
| `_TAG_LINE` (`is_brief_message`), `_TAG_TEXT` (`is_fix_brief`) | every line | safe: fail closed by design, a fence does not hide a tag |
| `_HEADER`, `_SETS_WAIVER` regexes themselves | one line | safe: a header is a one-line structure; fence state now comes from the scanner |
| `cli_worker.py` | no Markdown read (`gitdir:` link file line 104, `/proc` stat line 721, a heading written at line 1869) | safe: not Markdown structure |

### Configuration impact (fix round 1)

No configuration value is added or changed. Protocol-bound: CommonMark tab stop and indent limit (`_TAB_STOP`, `_CODE_INDENT` = 4) and the delimiter forms. The new required keyword `footer=` on `lint_brief`, `is_brief_message` and `task_file_findings` is a caller argument with no default, like `order=`.

### Configuration audit (fix round 1)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Unchanged rows of P1 (`brief_order` marker, `--legacy-order`, `order=`) | as above | as above | as above | rerun green: `test_a_send_follows_the_registry_marker`, `test_an_unknown_marker_refuses_a_brief_send`, `test_the_cli_legacy_order_flag`, `test_the_old_order_passes_only_without_the_order_rule` | as above |
| Footer span | `_footer_span` from the footer file text | first verbatim copy outside fences; unknown footer fails closed | `.agents/policy/AGENT_BRIEF_FOOTER.md` | override: `test_the_task_follows_the_end_of_the_verbatim_span` (a non-default footer text); refusal: `test_the_review_bypasses_are_refused[F1-...]`, `test_property_the_footer_counts_only_outside_a_fence`, `test_footer_must_be_verbatim` | protocol-bound |
| `footer=` keyword | `lint_brief`, `is_brief_message`, `task_file_findings` | required, no default | `cli_worker.lint_gate`, `brief_lint.main` | `test_the_footer_span_keeps_line_numbers_...`, `test_a_message_with_a_tag_line_...`; a call without it raises `TypeError` | configurable (explicit caller choice) |
| `_TAB_STOP`, `_CODE_INDENT`, fence/ATX/Setext/break patterns | `brief_lint.py` constants | CommonMark 0.31 sections 2.2, 4.1-4.5 | none | property tests: fences, ATX indent 0-6, Setext, thematic breaks | protocol-bound |

## Phase P1 fix round 2: fail closed on Markdown the scanner does not model

Retest 1 `.tmp/T-021/retest1/review.md` FAIL (F4, F5 P2); Gemini `.tmp/T-021/gemini-fix1-review.md` (two P3). R-021, R-015, R-025.

### Step 0 (plan pre-approved by the PM)

- **Root cause:** `_scan` read unmodelled Markdown (containers, HTML blocks) as top-level text, so those lines changed the global fence, paragraph and heading state. Separately, `_strip_lines` inferred the layout for legacy sends too.
- **Unknowns:** CommonMark's condition-6 tag list could not be re-read offline. I use the 0.30 list plus `search`, a superset (assumption; a missing name only turns an HTML start into type 7).
- **Deviation from the brief, for the PM:** "a line that opens an HTML block is refused" would refuse every brief that uses the wrapper's `<goal>`/`<scope>` tag lines. Those are CommonMark type-7 HTML blocks; `.tmp/E12/brief.md:21-48` has five. So an HTML block is refused only when one of its lines looks like a heading, a Setext underline or a fence delimiter, which is the same predicate as for containers. Containers take the PM's fail-closed path (no narrow model): any structure inside them is refused, also above the task.
- **Plan:** (1) legacy path = the base readers verbatim, chosen by `order`; (2) the scanner tracks outer container, HTML block and fence extents, and container and HTML lines are inert; (3) named `brief-order` finding; (4) regressions from the retest table and `cases/`, composition properties, BOM; (5) mutants, suites, lint of all `.tmp/*/brief.md`. **Finish:** the done check of the brief.

### Class audit: CommonMark 0.31 block grammar against `_scan` (new order)

| Construct | Before (fix 1) | Can it change fence or heading state | Now |
|---|---|---|---|
| Thematic break (4.1) | modelled; ends a paragraph | no (it closes a paragraph) | unchanged; checked before a list item |
| ATX heading (4.2) | modelled, indent 0-3 | adds a heading | unchanged |
| Setext heading (4.3) | modelled under an open paragraph | adds a heading | unchanged |
| Indented code (4.4) | text, never a heading, fence or underline, never starts a paragraph | no: a line indented 4 or more columns is never a block start | unchanged; safe because inside a list it is text, and it adds no state |
| Fenced code (4.5) | top-level only, but a container's fence line opened a global fence (F4) | yes | top-level fences only; a fence line inside a container or HTML block is refused and never toggles state |
| HTML block (4.6) | flattened to text (F5) | yes: a phantom `## Task`, or backticks inside `<pre>` | extents per start conditions 1-7 (types 1-5 end at their marker, 6-7 at a blank line; type 7 never interrupts a paragraph); lines inert; a heading-, underline- or fence-looking line inside is refused |
| Link reference definition (4.7) | read as a paragraph | only adds a Setext heading under it, never hides one | safe: fail closed |
| Paragraph (4.8) | modelled | holds Setext underlines | unchanged |
| Block quote (5.1) | text (F4) | yes | outer extent: lines opening with `>` (indent 0-3); no lazy continuation; lines inert; structure inside refused |
| List item (5.2) | text (F4) | yes | outer extent: marker line, then blank lines and lines indented at least the content offset (5.2 rule 1, tabs at 4); a new marker at a lower indent is a sibling; no lazy continuation; lines inert; structure inside refused |

No lazy continuation: a line that CommonMark would add lazily to a container's paragraph is read here as a new top-level paragraph. That can only add a Setext heading under it (a refusal), never hide one. The only state a lazy line could affect is paragraph state, and type-7 HTML cannot interrupt a paragraph in either reading.

**Readers that infer the layout, and the legacy path.** `order` (the marker) chooses one of two reader sets. `order=False` runs the base readers byte for byte: `_strip_footer` cuts at `text.find(FOOTER_HEADING)`, and `_blocks`, `header_fields`, the waiver loop and `_slice_ids` run with the base fence toggle or none. They never call `_scan`, `_strip_lines`, `_footer_span` or `_task_lines`; mutant M-legacy and a differential experiment against `git show HEAD:...brief_lint.py` check this. `order=True` runs the scanner readers. `cli_worker.lint_gate` reads the marker before `is_brief_message`, so a send to a markerless worker is legacy from its first reader on.

### Progress (fix round 2)

- Done: `_scan` tracks the outer container, HTML block and fence extents (container and HTML lines are inert, structure inside them goes to `unsupported`); `order` selects the base readers (`_legacy_code`, base cut) or the scanner readers; `cli_worker.lint_gate` reads the marker first; BOM handled in `_scan.bare`; the `_blocks` docstring is fixed.
- Evidence so far: `.tmp/T-021/fix2/probes.log` (every retest case; legacy cases equal HEAD). Differential `.tmp/T-021/fix2/differential.py`: 3000 generated texts and all 119 real `.tmp/*/brief.md` give legacy-path results equal to the base commit. In the new order, only `T-006-fable`, `T-006-fix2` and `T-006-rereview5` (fenced output inside list items) would get the unsupported finding; all three are old-order briefs.
- Then: regression tests, composition properties, BOM test, mutants and suites (see the Completion report).

### Configuration impact (fix round 2)

No configuration value is added. Changed signatures, required and without defaults: `order=` on `lint_brief`, `is_brief_message`, `task_file_findings`, `header_fields` and `_strip_footer`. Protocol-bound literals: the HTML start and end conditions and the condition-6 tag names (CommonMark 4.6), and the list marker forms (5.2).

### Configuration audit (fix round 2)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| `brief_order` marker | `cli_worker.brief_order_applies`, now read before `is_brief_message` | absent: legacy, int `2`: order, other: refused for a brief | written on every start | override: `test_a_markerless_send_reads_as_the_base_commit_did` (the same tilde-fenced `Kind:` send is feedback with the marker and a footerless brief without it), `test_a_send_follows_the_registry_marker`; refusal: `test_an_unknown_marker_refuses_a_brief_send` | protocol-bound |
| Legacy readers (`order=False`) | `_strip_footer`, `_code_lines`/`_legacy_code`, waiver, `_slice_ids` | equal to the base commit | none | differential: 3000 generated texts and 120 real briefs equal (`.tmp/T-021/fix2/differential.log`); `test_a_markerless_send_reads_as_the_base_commit_did`; mutants M5, M6 killed | protocol-bound |
| `--legacy-order` | `brief_lint.main`, `order = args.brief and not args.legacy_order` | refused without `--brief` | README | `test_the_cli_legacy_order_flag`; `test_the_cli_requires_a_footer_heading_outside_fences` (a message lint keeps the base substring) | configurable |
| `order=` on the readers | required keyword | `TypeError` without it | every caller | `test_a_message_with_a_tag_line_or_a_kind_header_is_a_brief` (both values) | configurable (explicit caller choice) |
| HTML start and end conditions, condition-6 names, list markers, `_BOM` | `brief_lint.py` constants | CommonMark 0.31 4.6, 5.2; the tag list is a superset (assumption: not re-read offline) | none | `test_the_retest_rows`, the three wrapped-structure properties, `test_a_leading_byte_order_mark_is_no_text`; mutants M2, M4, M8 killed | protocol-bound |
| Unsupported-structure refusal | `_scan.unsupported` into `_order_findings` | refuses, never accepts | template Brief order line | refusal: `test_the_retest_rows`, `test_property_wrapped_structure_*`; accepted plain wrappers: `test_property_wrapped_plain_text_passes`, rows `goal-tags`, `blank-line-after-the-tag`; mutants M1, M3, M10 killed | protocol-bound |

## Phase P2: report lint

Builder: Claude Sonnet, 2026-11-15, worktree at `ac95cfa` (main with the accepted P1). R-021 (evidence pointers), R-015 (no word lists, no silent defaults), R-025 (Windows and Linux).

### Step 0 (design pre-approved by the PM: Step 0 "Design" item 3 and "Plan and finish checks" P2; Q4 answered: advisory, never blocks)

- **Unknowns (read, not assumed):** (1) what the scanner exposes: `_scan` gives per-line `code` and `inert` flags, top-level headings as (first line, level), `unsupported`; it gave no fence extents, so a closing line could not be told from the next opener without a second fence reader; (2) the turn-end path: `run_turn` and `run_codex_turn` end in one registry update under `RegistryLock` after `close_git_audit`, and the registered task file is `hooks.task_file` (an absolute POSIX path written by `validate_task_file`); (3) what the real task files look like (`tasks/active/*.md`): some use `## Completion report (phase 1)` or `: fix round 1`, bold bullet labels and `### Result and evidence (round 3)` headings.
- **New questions, each with the answer I took (the PM may change it):** (a) Title match: exact, as the design says (`## Completion report`, `Research evidence`, `Result and evidence`); a section with a suffix is skipped, not linted. Prefix matching would lint fix-round sections but then each would need its own evidence table. (b) An untouched copy of the template (headings, placeholder table row) counts as written (it holds a table and headings), so it warns on every turn until filled; the rule text is literal. A table whose only row has empty cells is "no data row". (c) A fence inside a list item, quote or HTML block is invisible to the scanner (P1's fail-closed design); the finding says so instead of saying the output is missing. (d) One additive change in P1's file: `_Scan.fences` (opening line, closing line, `len(lines)` when never closed), because a second fence reader is what the PM ruled out.
- **Plan (one item in progress at a time):** (1) `fences` in `_scan`; (2) `report_lint.py` on the scanner; (3) the turn-end record in `cli_worker.py`; (4) tests, then mutants; (5) README, audit, lint of three real task files, suite once. **Finish:** the done check of the brief.

### Configuration impact

- No new CLI flag and no new config key: `report_lint.py` takes only file arguments, and the turn-end call has no switch (advisory by decision Q4; a switch would be a hidden way to skip the check).
- Protocol-bound vocabulary of the template in `report_lint.py`: `SECTION_TITLE`, `RESULT_TITLE`, `EVIDENCE_TITLE`, `EVIDENCE_COLUMNS` (5), `SOURCE_COLUMN`, `CLASS_COLUMN`, `CLASSES` (the four classes); a test compares `CLASSES` with the template line.
- `cli_worker.py`: registry key `report_lint` (written by the launcher after a completed turn, never by a user), `REPORT_LINT_MAX_FINDINGS = 50` (bounds the record, the registry is rewritten whole on each update) and `REPORT_LINT_SHOWN = 5` (findings printed; display only).
- `brief_lint._Scan.fences`: a new field of the scanner result, no setting. No deployment, model, path, device or timeout setting changes.

### Configuration audit (P2)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| `report_lint.py <task file>...` arguments | argparse in `report_lint.main` | at least one file; an unreadable or undecodable file prints `cannot read` and exits 2, never passes | README row and Report gate | override: `test_the_cli_prints_path_line_rule_and_exits_1` (two files), `test_the_script_runs_isolated_with_no_import_path`; refusal: `test_the_cli_refuses_an_unreadable_file_and_a_missing_argument` | configurable (CLI choice) |
| Titles, table width and the four classes | module constants in `report_lint.py` | exact title; 5 columns; class case-folded and one of `CLASSES` | AGENT_TASK_TEMPLATE `Completion report` | `test_the_class_vocabulary_is_the_templates_and_case_is_folded`, `test_each_rule_fails_alone` (unknown class, 3 and 6 columns); mutants M2, M2b, M3 | protocol-bound |
| Pointer forms (`path:line[-line]`, `http(s)://`, code span) | `_PATH_LINE`, `_URL`, `_has_code_span` | a fixed form, no wording; a bare time `12:30` is refused | none | `test_pointer_forms_are_accepted`, `test_other_cells_are_not_pointers`, `test_property_one_bad_cell_is_exactly_one_finding_on_its_row`; mutants M4, M13, M14 | protocol-bound |
| Turn-end call (no switch) | `cli_worker.report_lint_record`, `store_report_lint` | the registered task file only; no file registered: no record and a stale one is dropped; lint crash, loader exit, missing or undecodable file: recorded in `error` and warned, never silent, never blocking | none | `test_a_turn_records_the_findings_and_warns_but_does_not_block`, `test_a_worker_without_a_task_file_keeps_no_record`, `test_a_lint_crash_or_unreadable_file_is_recorded_never_silent` (3 cases), `test_a_loader_exit_is_recorded_too`; mutants M8, M11, M12, M17 | configurable (advisory by decision Q4) |
| `REPORT_LINT_MAX_FINDINGS` | constant in `cli_worker.py` | the record keeps at most this many findings and the true `total` | none | override: `test_the_record_is_bounded_and_the_bound_is_a_module_constant` (bound 2); refusal: not applicable (a constant, not user input); mutant M9 | safety-bound |
| `REPORT_LINT_SHOWN` | constant in `cli_worker.py` | stderr shows at most this many findings | none | override: `test_the_warning_shows_a_bounded_number_of_findings` (1); refusal: not applicable (a constant, not user input) | safety-bound (bounds the volume printed per turn) |
| `_Scan.fences` | `brief_lint._scan` | closed fence: (opening, closing); unclosed: (opening, `len(lines)`) | none | `test_an_unclosed_fence_runs_to_the_end_of_the_file`; mutant M16; the P1 suite is unchanged and green | artifact-bound (scanner result) |

## Phase P2 fix round 1: structural report discovery and block visibility

Fixer: fresh Claude Sonnet 5.5 (2026-11-16), same worktree. Findings: Codex C-F1..C-F5 and Gemini G-F1..G-F4 (`.tmp/T-021/review-p2/review.md`, `gemini-review.md`). R-021, R-015, R-025.

### Step 0 (the PM's plan, pre-approved)

- **Unknowns (read, not assumed):** (1) the scanner marks fences, containers and HTML blocks but not indented code, comments or the block kind of an HTML line; (2) what the real task files do (`## Completion report (phase 1)`, `: fix round 1`, `### Phase P2 (report lint)` with its own bullets, `### Research evidence (fix round 1)`); (3) the template's own `## Completion report` text is the only "untouched" reference.
- **Questions with the answer I took:** (a) what opens a nested report without a word list: a deeper heading that starts with the title, or whose own body carries the template's `Work completed` or `Result and evidence` bold label (the `### Result and evidence` and `### Research evidence` template headings never open one); a nested report with neither is merged into the report above it (cannot be told from a template subsection); (b) title match is case-folded (`Completion Report`); (c) the untouched template is read from the checkout (`.agents/policy/AGENT_TASK_TEMPLATE.md`, same pattern as the brief lint's footer file); unreadable: nothing counts as untouched, so every report with content is linted (loud, not silent); (d) the standalone CLI prints a `note:` on stderr for unwritten reports and keeps its exit status.
- **Plan:** (1) class audit; (2) scanner `html` field; (3) discovery, written, visibility, pointers in `report_lint.py`; (4) turn-end `unwritten`; (5) regressions before and after, differential, mutants, real files, suite. **Finish:** the brief's done check.

### Class audit (S31): every structural decision of `report_lint.py`, the CommonMark construct that fakes or hides it, whether the scanner models it

| Decision | Construct that can fake or hide it | Scanner models it? | Before | Now |
|---|---|---|---|---|
| Which heading is a report | suffix `(phase 1)`, `: fix round 1`, `Completion Report` case, `Completion reports`, Setext, closing `#` run, level 1 or 3, heading in a fence, comment or container | top-level ATX and Setext yes; fence, HTML and container contents are never headings (`unsupported` lists look-alikes) | exact equality: skipped in silence (C-F1, G-F1) | level-2 + title prefix (case-folded) + end or ` ` `(` `:`; other starts with the title: `report-section` finding |
| Where a report ends and the next begins | `### Phase P2` blocks in one `## Completion report`, `### Completion report (phase 3)`, `### Research evidence` under a phase | headings yes | the first report borrowed by the later phases (C-F1) | nested report opens at a title heading or a heading whose body carries the `Work completed` or `Result and evidence` label; each judged alone; residual: a nested report with neither is merged upward |
| Whether a report is written | blank lines, only headings, only HTML comment, prose only, list, quote, indented text, link definition, template copy, CRLF/BOM | `code` and `inert` yes; comment vs other HTML needed the block kind | needed heading, fence or table (C-F2, G-F2) | any non-blank line that is no heading and no comment block; the exact template text is unwritten, listed in `unwritten` |
| Table | indented code (4+ columns or tab), fence, list item, quote, HTML block or comment, escaped pipe, lazy continuation | fence, container, HTML yes; indented code no (P1 keeps it visible for Git checks) | indented table accepted (C-F3) | `_indented_code` (top-level, 4+ columns) is no table line; scanner unchanged for P1 |
| Result label | bullet item (own form), bare bold, `### Result and evidence`, fence, HTML comment or block, indented code, comment opened earlier in the same paragraph | fence, HTML yes (block kind added); indented code and inline comment no | comment and indented label accepted (C-F4) | `_label_line`: not fenced, not in an HTML block, not indented code, a bare label not inside an open inline `<!--` |
| Done-check fence | fence in list, quote, HTML, comment, indented 4+, never closed | yes (`fences`: top-level only; unclosed runs to the end) | already right | unchanged |
| Code span | escaped backtick `\`` (odd backslashes), `\\` then a real backtick, run length, whitespace only, unmatched run | no (inline) | `\`x\`` taken as a span (C-F5) | an odd backslash run escapes the first backtick of an opener; closers keep their raw length (6.1) |
| Pointer `path:line` | `Note:1`, `Step:0`, `v1.2:3`, `12:30`, `D:\p\x.py:5` | no (inline) | any non-numeric prefix (G-F3) | prefix has `/`, `\` or an extension `.ext`; residual: `Makefile:3` is refused |
| Tables under the evidence heading | two or more tables, a heading with a suffix `(fix round 1)` | headings yes | only the first table (G-F4); exact heading title | every table; title match by the same prefix rule |

### Configuration impact (fix round 1)

- No new CLI flag, config key or setting. `report_lint.py` still takes only file arguments; the turn-end call has no switch (decision Q4).
- New protocol-bound constants in `report_lint.py`: `WORK_TITLE` (the template's `Work completed` label), `TITLE_SEPARATORS` (what the template family puts after a title), `TEMPLATE_PATH` (the checkout's task template, read like the brief lint reads its footer file); a test pins `TEMPLATE` equality (the untouched copy is read from it).
- `cli_worker.py`: the registry record gains `unwritten` (a list of lines, bounded by `REPORT_LINT_MAX_FINDINGS`, no new constant); `status --json` shows the record also when it holds `unwritten`.
- `brief_lint._Scan.html`: a new field of the scanner result (line of an HTML block to the block's first line), no setting; P1 decisions are unchanged (differential below).

### Configuration audit (fix round 1, rows added or changed)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Report heading forms | `_form`, `TITLE_SEPARATORS`, `SECTION_TITLE` in `report_lint.py` | level 2, title prefix (case-folded), then end, space, `(` or `:`; any other start with the title is a `report-section` finding | AGENT_TASK_TEMPLATE `Completion report`, README Report gate | override: `test_suffixed_report_titles_are_reports_and_linted` (8 forms); refusal: `test_a_heading_that_looks_like_a_report_but_is_not_one_is_a_finding_never_skipped` (5 forms); mutants M1, M2 | protocol-bound |
| Nested report boundary | `_nested_starts`, `WORK_TITLE`, `RESULT_TITLE` | title heading or a heading whose own body has the template's bold label | the template's bullets | `test_each_nested_phase_report_is_linted_on_its_own`, `test_a_result_heading_or_a_blank_parent_does_not_split_a_report`, property `test_property_every_nested_report_is_judged_alone`; mutant M9 | protocol-bound |
| Written / unwritten | `_has_content`, `lint_report`, `load_template`, `TEMPLATE_PATH` | any non-blank line that is no heading and no comment block is content; the exact template text is unwritten; an unreadable template: nothing is untouched | the template file | `test_unwritten_sections_are_skipped_and_listed`, `test_prose_only_reports_are_written_and_linted`, `test_the_untouched_template_is_unwritten_and_one_edit_makes_it_written` (no template given: refusal to skip), property prose test; mutants M8, M10, M11 | protocol-bound |
| Visibility of tables, labels, code spans | `_indented_code`, `_comment`, `_in_open_comment`, `_label_line`, `brief_lint._Scan.html` | indented code, HTML blocks and comments hold none | none | `test_tables_in_indented_code_are_no_tables`, `test_a_label_in_a_comment_an_html_block_or_indented_code_is_no_label` (6), `test_a_label_in_an_inline_comment_that_spans_lines_is_hidden`, property `test_property_a_label_or_table_hidden_by_markdown_never_counts` (8 wrappers); mutants M3, M4, M11, M12, M14 | artifact-bound (scanner result) |
| Pointer forms | `_PATH_LINE`, `_EXTENSION`, `_has_code_span` | path with separator or extension; escaped backtick is literal | none | `test_pointer_forms_are_accepted`, `test_other_cells_are_not_pointers`, `test_a_citation_that_is_no_file_is_no_pointer`, `test_escaped_backticks_are_not_a_code_span`, property backslash parity; mutants M5, M6 | protocol-bound |
| Evidence tables | `_evidence` | every table under every `Research evidence` heading | none | `test_every_table_under_the_evidence_heading_is_checked`, property `test_property_every_evidence_table_is_checked`; mutant M7 | protocol-bound |
| Record `unwritten` | `cli_worker.report_lint_record`, `status_rows` | a list of heading lines, at most `REPORT_LINT_MAX_FINDINGS`; the turn result is unchanged | README Report gate | `test_a_clean_report_and_an_unwritten_one_leave_a_quiet_record`, `test_a_registered_task_file_is_linted_after_every_codex_turn`; refusal: not applicable (a record, not input); mutant M13 | safety-bound |

## Phase P2 fix round 2: one structural visibility step for labels, pointers and code spans

Fixer: fresh Claude Sonnet 5.5 (2026-11-16), same worktree, base ac95cfa. Findings: Codex retest RT1..RT4 (`.tmp/T-021/retest-p2/review.md`; Gemini round 1 PASS, two P3 as named there). R-021, R-015, R-025.

### Step 0 (the PM's plan, pre-approved)

- **Unknowns (read, not assumed):** (1) `_scan` marks the lines of a list item as `inert` but parses nothing inside, so a fence, indented code or HTML nested in an item is invisible to the scanner; (2) the inline checks (`_has_code_span`, `_has_pointer`, `_in_open_comment`) each read raw text with their own partial rule; (3) what real reports do: `grep` finds no indented `Result and evidence` label in `tasks/` or `.agents/` (nine files, all at column 0).
- **Questions with the answer I took:** (a) block level: expose the item structure additively and scan the item's own content with the same scanner (container-relative block kinds), not refuse every nested label, because `  - **Result and evidence:**` under a bullet is a legitimate nested item the round 1 tests already accept; a block quote is never read (a label cannot start with `>`); (b) RT3: keep refusing and make the diagnostic recommend `./Makefile:3` (see Decisions); (c) no new flag or key.
- **Plan:** (1) class audit; (2) `brief_lint._Scan.items`; (3) `_inline` and the item scan in `report_lint.py`, the label, pointer and code-span checks read only visible text; (4) before and after runs, differential, mutants, real files, suite. **Finish:** the brief's done check.

### Class audit (S31): every check in `report_lint.py` that reads label, pointer, code-span, fence or table text

"Block" = top level (T), list item (L), block quote (Q), HTML block (H), fence (F), indented code (I). "Inline" = code span (C), HTML comment or tag (M), escaped text (E). `Sees` is what the check could read before this round; the last column says whether hidden content could satisfy it (a false pass, the dangerous direction) or block it (a false finding).

| Check (function) | Reads | Sees before round 2 | Sees now | Hidden content could satisfy / block it before | Now |
|---|---|---|---|---|---|
| Result label, `_label_line` (also the nested-report test of `_nested_starts`, which uses the same function) | a bold label line | T paragraph; L opener (bullet); not F, H, I at top level; L continuation lines were read as visible whatever they nest; M across lines of one paragraph by raw `<!--` and `-->` positions; C not recognized | T paragraph; L opener and any nested item or paragraph of an item, by scanning the item's own content (nested F, I, H hide it; a nested bullet that shows is read); Q never; M and C by one left-to-right inline scan of the whole paragraph (back to a blank, code, heading or HTML line, forward to the next) | satisfy: label in a list item's nested fence, indented code, raw HTML or comment (RT1); block: a code-span `<!--` hid the next label (RT4) | none: both closed |
| Done-check fence, `_done_check` (`scan.fences`) | a fenced block with a non-blank line | T fences only (F in L, Q, H are not parsed and the finding says so); an unclosed fence runs to the end | unchanged | satisfy: none (a fence in a comment or HTML block is no fence for the scanner); block: only the documented "put it at the top level" | unchanged |
| Written or unwritten, `_has_content` | any non-blank line that is no heading and no comment block | every line of every block kind, a one-line inline comment included | unchanged | can only make a report "written", so it is linted: a loud finding, never a silent pass | unchanged (accepted) |
| Evidence tables, `_tables` | a header, a delimiter row and rows | T lines that are not F, not inert, not indented 4+ columns | unchanged | satisfy: none (C-F3 closed in round 1); block: none | unchanged |
| Class cell, `_check_table` | one cell compared with `CLASSES` | the cell text; an inline comment around it fails the exact match | unchanged | satisfy: none; block: a comment beside the class is a loud finding | unchanged |
| Pointer, `_has_pointer` (URL and `path:line`) | the source cell | the raw cell: a URL or path inside an HTML comment, a tag or its attributes counted (RT2) | the visible segments of the cell only (comments, tags and their attributes cut out; code spans handled apart) | satisfy: RT2; block: none | none |
| Code span, `_has_code_span` | the source cell | its own backtick-run scan: escapes, run length; no HTML | the shared `_inline` scan: escapes, run length, and a span inside a comment is hidden while a comment inside a span is literal | satisfy: a span only inside an HTML comment (RT2) | none |
| Path form, `_EXTENSION`, `_PATH_LINE` | `path:line` | a bare basename with no separator or extension is refused (RT3) | unchanged, plus a diagnostic that recommends `./Makefile:3` | block: `Makefile:3` (a real file) | decided, see below |
| Section and nested-report headings, `_headings`, `_form`, `_nested_starts` | heading text of top-level ATX and Setext headings | T headings only; fence, container and HTML contents are never headings | unchanged | none found in round 1 or the retest (deep, Setext, suffixed forms probed) | unchanged |
| Template comparison, `_template_lines` | the template's section as normalized lines | text equality | unchanged | none (no visibility notion) | unchanged |

### Decisions

- **RT3: keep the rule, recommend `./Makefile:3`.** A lookup of the bare name in the task's checkout would make the verdict depend on the working tree at lint time (the turn-end call, the standalone run and CI would disagree after a rename), would be ambiguous with several `Makefile`s, and needs a new argument for the root; `./Makefile:3` is the same file named unambiguously by a path form the lint already accepts, and `Note:1` and `Step:0` stay refused because they have neither a separator nor an extension.
- **Block level: scan the item's own content, not refuse nested labels.** `brief_lint._Scan.items` (line of every top-level list item opener, with its content column) is the only scanner addition; `report_lint._item_content` builds the item's content as text (marker off the first line, content indentation off the others) and runs the same `_scan` on it, recursively, so a nested fence, indented code, HTML block or deeper item is judged by the scanner's own rules. Refusing every nested label would have rejected `  - **Result and evidence:**` under the work bullet, which is plain visible text.
- **Inline: one left-to-right scan (`_inline`).** An escape, a code span and a raw HTML item each win by where they start (CommonMark 6: code spans, autolinks and raw HTML bind tighter than emphasis and links, and parsing is left to right), so `` `<!--` `` opens no comment and `<!-- `x` -->` holds no span. An unterminated `<!--` is literal text; the comment-end rule is the scanner's own (`<!-->` and `<!--->` end at once).

### Configuration impact (fix round 2)

- No new CLI flag, config key or setting; `report_lint.py` still takes only file arguments.
- Protocol-bound constants added to `report_lint.py`: `_ESCAPABLE` (the ASCII punctuation a backslash escapes, CommonMark 2.4), `_RAW_HTML` (the raw inline HTML forms of 6.6, built on the scanner's own tag grammar), and two message texts, `_HIDDEN_LABEL` and `_NO_POINTER_HINT`. `brief_lint._Scan.items` is a new field of the scanner result (artifact-bound); the P1 decisions and the six HEAD fields are unchanged (differential below).

### Configuration audit (fix round 2, rows changed)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Visibility of labels, tables, pointers, code spans | `_inline`, `_paragraph_visible`, `_item_content`, `_visible`, `_label_line`, `brief_lint._Scan.items` | a label is read only where Markdown shows it: not in code, HTML or a comment, not in a list item's nested fence, indented code or HTML, not in a block quote; a comment or code span over the lines of its paragraph is read as a whole | none | override: `test_property_a_label_shown_inside_a_list_item_still_counts` (12 shapes x 3 markers x LF/CRLF x BOM); refusal: `test_property_a_label_nested_in_code_html_or_a_comment_of_a_list_item_is_no_label` (14 shapes x same), `test_the_finding_for_a_hidden_label_names_its_line`, `test_the_scanner_lists_top_level_list_items_with_their_content_column`; mutants M4 to M8, M11, M14, M16 | artifact-bound (scanner result) |
| Pointer forms | `_has_pointer`, `_has_code_span`, `_inline`, `_RAW_HTML`, `_ESCAPABLE`, `_NO_POINTER_HINT` | a pointer or code span counts only in visible text; a bare basename needs `./` | none | override: `test_property_a_pointer_beside_a_comment_or_a_tag_still_counts`, `test_a_bare_file_name_needs_a_dot_slash_and_the_finding_says_so` (`./Makefile:3`, `Makefile.am:3`); refusal: `test_property_a_pointer_or_span_hidden_in_a_comment_or_a_tag_is_no_pointer` (5 wrappers x every pointer form), `test_the_inline_scan_splits_text_code_and_html` (11), `Makefile:3`, `Note:1`; mutants M1 to M3, M9, M10, M13, M15 | protocol-bound |

All other rows of the round 1 audit are unchanged.

## Phase P2 fix round 3: every CommonMark construct read exactly or failed closed

Fixer: fresh Claude Opus 5.5 (2026-11-16), same worktree, base ac95cfa; decision 121, first extra round. Findings: Codex retest 2 R2F1 (P2), R2F2 (P3) (`.tmp/T-021/retest2-p2/review.md`). R-021, R-015, R-025.

### Step 0 (the PM's plan, pre-approved)

- **Root cause (read, not assumed):** round 2 modelled part of CommonMark and treated the rest as shown text. R2F1: `_RAW_HTML` reused the scanner's line-bound tag grammar (`brief_lint.py:154-156`, `[ \t]` only), so a tag whose attributes run over lines was text. R2F2: `_paragraph_visible` rebuilt paragraph extents with its own walk, which missed thematic breaks and Setext underlines, while the scanner already knew them (`brief_lint.py:329-335`).
- **Answers taken (no new question for the user):** (a) paragraph extents come from the scanner (an additive `_Scan.paragraphs`), never from a second walk; (b) raw HTML gets the report lint's own tag grammar with "spaces, tabs and up to one line ending" (CommonMark 6.6), and the scanner's line-bound grammar stays as P1 has it; (c) what the lint does not model fails closed with a finding naming the construct, a false finding being acceptable and a false pass not.
- **Plan:** (1) the enumeration below; (2) scanner `paragraphs`; (3) one inline tokenizer for every inline construct, lazy continuation, fail-closed reasons; (4) regressions before and after, mutants, P1 differential, suites, real files. **Finish:** the brief's done check.

### Class audit (S31): every CommonMark 0.31.2 construct against `report_lint.py`

"Before" is the round 2 tree. "Hides or reveals" says whether the construct can make a label, pointer or code span count when it is not shown (a false pass) or stop it counting when it is (a false finding). Every row marked yes has a hidden and a shown case in `CONSTRUCTS` (`test_property_every_construct_hides_or_shows_the_label_as_commonmark_does`, top level and inside a list item, LF/CRLF, BOM) or the named test.

| Construct (spec section) | Modelled before | Hides or reveals | Decision now |
|---|---|---|---|
| Thematic break (4.1) | scanner yes; the report's own paragraph walk no (R2F2) | reveals: it ends a paragraph, so a span cannot pair across it | modelled exactly: paragraph extents are `_Scan.paragraphs` |
| ATX heading (4.2) | scanner yes; the title read from its source | reveals (ends a paragraph); its title can hide or show a report or `Result and evidence` title through inline HTML, escapes, entities | modelled exactly: extents from the scanner; titles are compared by the text they show (`_shown`) |
| Setext heading (4.3) | scanner yes; its underline was no boundary of the walk | reveals (the underline ends the paragraph) | modelled exactly: text lines in `paragraphs`, underline not; title by `_shown` |
| Indented code (4.4) | top level: any line indented 4+ was code, also a paragraph continuation line (a false finding) | hides | modelled exactly: indented code only when the line is in no paragraph; in a list item by the item's own scan |
| Fenced code (4.5) | yes (top level; in an item by the item's scan) | hides | unchanged |
| HTML blocks, kinds 1 to 7 (4.6) | yes (scanner extents: kinds 1-5 to their end marker, 6-7 to a blank line) | hides | unchanged; residual false finding: a kind-7 line or list marker right after an item, which CommonMark may read as a lazy line |
| Link reference definition (4.7) | no (read as paragraph text) | hides: a title may run over lines and hold the label | fails closed: a paragraph that starts with `[` and has `]:` before the label (`_DEFINITION`) |
| Paragraph (4.8) | partly (own walk) | hides or reveals: inline constructs run over its lines, never past it | modelled exactly: the scanner's extent |
| Blank line (4.9) | yes | reveals (ends a paragraph and a list item's last paragraph) | unchanged, now through `paragraphs` |
| Block quote (5.1) | extent only, content not read | hides | fails closed: never a label (`_QUOTE`); a paragraph right after a non-blank quote line fails closed too (`_LAZY_QUOTE`), since a lazy line may continue the quote's paragraph |
| List item (5.2), lazy continuation | content scanned as its own document; no lazy continuation (a span closed by a lazy line was missed, a false pass) | hides or reveals | modelled: a top-level paragraph right after an item is joined to the item's last paragraph (`_tail`), and an item paragraph that runs to the item's end takes the lazy lines after it (`_continued`), recursively; a lazy line indented as code fails closed (`_LAZY_CODE`) |
| List (5.3), tight or loose | not needed | no: looseness changes spacing, never what shows | not modelled (cannot hide) |
| Backslash escape (2.4) | yes | reveals (an escaped backtick, bracket or `<` opens nothing) | unchanged; `\` before a line ending is a hard break, no escape |
| Entity and numeric reference (2.5) | text | reveals: `&#96;` is a literal backtick that opens no span; in a heading title it is the character | modelled exactly: text (opens and closes nothing); `_shown` resolves exact HTML5 names and numbers |
| Code span (6.1) | yes | hides | unchanged (run length, escape at the opener only) |
| Emphasis and strong emphasis (6.2) | text | no: delimiters show the text between them | not modelled (cannot hide) |
| Link, inline (6.3) | no: the title and destination were text, and a span or tag starting there was tokenized | hides (a title over lines; a backtick in a title opens no span) | modelled exactly: cmark's destination and title grammar, link text shown, title hidden, destination counted as a pointer (a link's target); a link inside a link deactivates the outer `[` |
| Link, reference: full, collapsed, shortcut (6.3) | no | hides or reveals depending on a definition the lint does not resolve | the safe reading: a reference label is hidden; where the two readings (with and without a definition) could tokenize the rest differently, the rest fails closed (`unsure`) |
| Image (6.4) | no | hides (the description, source and title are not shown text) | modelled: all three hidden; a reference or shortcut image hides its description (safe reading) |
| Autolink (6.5) | no: a backtick inside one opened a span | reveals | modelled exactly: URI and e-mail forms, shown text, nothing inside opens anything |
| Raw HTML (6.6): open tag | line-bound attributes (R2F1) | hides (attributes) | modelled exactly: whitespace with up to one line ending, at every place the grammar allows it |
| Raw HTML: closing tag | line-bound | no text of its own; whitespace before `>` may hold a line ending | modelled exactly |
| Raw HTML: comment, processing instruction, declaration, CDATA | yes (over lines already) | hides | unchanged (`<!-->` and `<!--->` end at once) |
| Hard and soft line breaks (6.7, 6.8) | text | no | not modelled (cannot hide) |
| Textual content (6.9) | text | no | text |

GFM tables are no CommonMark construct and keep the round 1 reading (top-level lines, never indented 4+); a cell's inline content goes through the same `_inline`.

### Decisions

- **Paragraphs from the scanner, not a second walk.** `brief_lint._Scan.paragraphs` (line of a top-level paragraph to its first line) is the only scanner addition; it is recorded where `_scan` already opens and continues a paragraph, so the P1 fields and decisions are unchanged (differential below). Dropped: a fix of the report walk's `ends()` (the narrower fix the retest named), because a walk of its own would drift from the scanner again at the next construct.
- **The report lint has its own inline tag grammar.** Dropped: widening `brief_lint._HTML_COMPLETE_TAG`, which decides kind-7 HTML block starts, a one-line test, and would change P1.
- **Inline links exactly, references the safe way.** A pointer in a link destination counts (round 2 accepted it as raw text, and a link to a file is a pointer); an image source and every title do not. Dropped: fail closed on any `[` in a paragraph or cell, which would refuse `[file](path:3)` pointers.
- **Block quotes fail closed, not parsed.** Dropped for this round: scanning a quote's content like a list item's; fail closed is allowed, and no real report holds a label in a quote.
- **Accepted false findings (each message names the construct and asks for a top-level item after a blank line):** a label right after a block quote whose last block is no paragraph (retest round 1 case `quote_fenced_lazy_label`, exit 0 before, 1 now); a label in a shortcut image without a definition; a label after an unsure reference.

### Configuration impact (fix round 3)

- No new CLI flag, config key or setting; `report_lint.py` still takes only file arguments.
- Protocol-bound constants added to `report_lint.py`: `_WS`, `_WS1`, `_ATTRIBUTE`, `_RAW_HTML` (now its own), `_AUTOLINK`, `_PUNCT`, `_TITLE`, `_LINK_LABEL`, `_LINK_LABEL_MAX` (999, CommonMark 4.7), `_DESTINATION_DEPTH` (32, cmark's bound, see Research evidence), `_ENTITY_OR_ESCAPE`, `_POINTING`; message texts `_HIDERS`, `_FENCED`, `_HTML_BLOCK`, `_INDENTED`, `_QUOTE`, `_LAZY_QUOTE`, `_LAZY_CODE`, `_DEFINITION`, `_HIDDEN_LABEL` (now with the construct), `_HIDDEN_POINTER`. `brief_lint._Scan.paragraphs` is a new field of the scanner result (artifact-bound).

### Configuration audit (fix round 3, rows changed)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Paragraph extents | `brief_lint._Scan.paragraphs`, `report_lint._paragraph`, `_last_line` | the scanner's own paragraph state; Setext text lines in, underline out | none | `test_the_scanner_records_paragraph_extents`, `test_property_inline_state_never_crosses_a_thematic_break` (5 markers x 1-4 backticks x item x LF/CRLF x BOM); P1 differential; mutants M2 | artifact-bound (scanner result) |
| Label visibility (blocks, lazy continuation, fail closed) | `_locate`, `_tail`, `_continued`, `_after_container`, `_hidden`, `_visible`, `_label_line`, `_item_content` | a label counts only as shown text; quotes, definitions and unsure text fail closed with the construct named | none | shown: the shown half of `test_property_every_construct_hides_or_shows_the_label_as_commonmark_does` (30 constructs), lazy rows `True`; refusal: its hidden half, `test_a_lazy_continuation_line_joins_the_paragraph_it_continues`, `test_the_finding_names_the_construct_that_hides_the_label`, `test_the_retest_2_regressions`; mutants M3, M7, M8 | protocol-bound |
| Inline grammar | `_inline`, `_close_bracket`, `_inline_link`, `_destination`, `_space`, `_RAW_HTML`, `_AUTOLINK`, `_TITLE`, `_LINK_LABEL`, `_LINK_LABEL_MAX`, `_DESTINATION_DEPTH` | CommonMark 0.31.2 section 6, left to right | none | shown: autolink, link text, entity, escape rows; `test_a_link_inside_a_link_and_a_reference_are_read_both_ways`; refusal: tag, title, image, reference rows, `test_property_a_tag_whose_attributes_run_over_lines_hides_its_value`; `test_the_inline_scan_splits_text_code_and_html` (11, unchanged); mutants M1, M4, M5, M6, M9 | protocol-bound |
| Pointer reading | `_has_pointer`, `_points`, `_POINTING`, `_pointer_hider`, `_HIDDEN_POINTER` | a pointer in shown text or a link destination; never in a title, an image or a reference label | README row | shown: `test_a_pointer_counts_only_where_a_link_or_image_shows_it` (7 shown); refusal: the same (7 hidden), `test_the_pointer_finding_names_the_construct_that_hides_it`; earlier pointer properties unchanged; mutant M11 | protocol-bound |
| Heading titles | `_headings`, `_shown`, `_resolve`, `_ENTITY_OR_ESCAPE` | titles compared by the text they show; named references by exact HTML5 name | none | `test_a_heading_is_read_by_the_title_it_shows` (a commented or entity title is linted, never skipped; exact names only); earlier title tests unchanged; mutant M10 | protocol-bound |
| Nested tabs | `_item_content` (unchanged) | a tab is a tab stop | none | `test_property_tabs_in_a_list_item_indent_by_tab_stops` (the retest's M17 shape) | protocol-bound |

All other rows of the round 1 and round 2 audits are unchanged.

## Phase P2 fix round 4: deterministic linear readers, fail closed on uncertain splits

Fixer: fresh Claude Opus 5.5 (2026-11-16), same worktree, base ac95cfa; decision 121, the last allowed round. Findings: Codex retest 3 R3F1, R3F2, R3F3 (P2 each, `.tmp/T-021/retest3-p2/review.md`). R-021, R-015, R-025.

### Step 0 (the PM's plan, pre-approved)

- **Root causes (read):** R3F1: `_TITLE` (round 3 `report_lint.py:113-115`) pairs `\\{punct}` with a class that also takes `\`, so an escaped quote can be reread as the closer, and the choices multiply over a backslash run. R3F2: the scanner records every sibling marker in `items` (`brief_lint.py:298-302`) and `_item_content` (:503) stops there, though `_list_item` (:205-217) knows which markers may interrupt a paragraph. R3F3: `_shown` (:379-389) keeps emphasis delimiters and reference brackets, so `## **Completion report**` never matches and `lint_report` (:789-791) skips it.
- **Answers taken (no question for the user):** (a) every reader that a regex with overlapping alternatives or a restart-per-position search did is rewritten as left-to-right code, and the code-span closer and every `find` of an end marker come from an index built once per text; (b) an uncertain block split is refused by name, never guessed; the scanner is not touched, so the P1 decisions stay those of HEAD; (c) a heading is matched to the section title by its markup-stripped text (more reports linted, never fewer), and to `Result and evidence` and `Research evidence` by its exact shown text, with a named finding when only the stripped text matches.
- **Plan:** (1) the three audits below; (2) readers, (3) the block refusals, (4) the heading reading; (5) the reviewer's cases and properties as tracked tests, a running-time property, the 110-case matrix, mutants, P1 differential, suites, real files. **Finish:** the brief's done check.

### Audit 1: every regex left on the report path, with its argument or its replacement

A regex is kept only when each character can belong to one atom given the one before it (no two alternatives or adjacent quantified atoms take the same character), so a failed match gives back at most a bounded number of characters, and when the search cannot restart at each position of a long run. "Linear" means time O(length of the input), measured by `test_property_adversarial_inputs_finish_in_bounded_time` (Completion report).

| Regex (round 3) | Used for | Decision | Argument or replacement |
|---|---|---|---|
| `_TITLE` | link title | **replaced** by `_title_end` | one pass: a backslash before ASCII punctuation consumes two characters, so the parity of a backslash run decides whether the quote after it is escaped; the first unescaped closer ends the title; an unescaped `(` ends a parenthesized one as invalid |
| `_RAW_HTML` (comment, PI, CDATA, declaration, open and closing tag) | inline HTML | **replaced** by `_html_end` | the tag is read once: name, then per attribute whitespace (at most one line ending), name, optional `=` value; the four end markers (`-->`, `?>`, `]]>`, `>`) and the two quotes are found in a position index built once per text (`_Index.next`, bisect), so an unclosed `<!--` repeated a thousand times costs one index, not a rescan each |
| `_AUTOLINK` | URI and e-mail autolinks | **replaced** by `_autolink_end` | the scheme, the local part and each domain label are maximal runs (their classes exclude the character that must follow), checked for length; the round 3 `[A-Za-z0-9-]{0,61}[A-Za-z0-9]` overlapped |
| `_LINK_LABEL` | reference label | **replaced** by `_label_end` | one pass with the escape rule of `_title_end`, stops at an unescaped `[` or after 1001 characters (`_LINK_LABEL_MAX`); the round 3 `\\{punct}` and `\\` alternatives overlapped (possessive, so linear, but not argued) |
| `_BACKTICKS` | code-span runs | **kept** for one `finditer` that builds the run index; the closer is found by bisect | `` `+ `` has one atom; round 3 rescanned the rest of the text for each unmatched opener |
| (no regex) the bracket stack | links, images, references | **replaced**: `_Brackets` | round 3 walked every open `[` at each link or reference (quadratic in nested brackets); an event now records its time and an opener reads the latest events when it closes; image descriptions are marked once, at the end |
| (no regex) per-line rescans | items, paragraphs, fences | **replaced**: `_Facts`, `_filled`, `_starts` | each candidate line rescanned its item, walked its paragraph and every fence; these are now built once per scan and bisected |
| `_ATX_CLOSE` | ATX closing `#` run | **replaced** by `_atx_text` | `rstrip` of spaces, `#`, spaces; round 3's `.sub` search restarted inside a long run of spaces (quadratic) |
| `_PATH_LINE` | `path:line` pointers | **replaced** by `_path_lines` | the path is a maximal run of path characters; one pass over runs gives the same paths `finditer` gave (a match from inside a run ends at the same `:`, so the earliest start wins; a failed run is skipped whole); round 3 restarted at each position of a run of, for example, `%` (quadratic) |
| `_ENTITY_OR_ESCAPE` | heading text | **replaced** by `_resolve_text` for escapes; `_ENTITY` **kept** for `&...;` | `_ENTITY` is applied with `match` at an `&` only; its alternatives start with distinct characters (`#` digit, `#x`, letter) and every quantifier is bounded (7, 6, 31) |
| `_LABEL`, `_WORK_LABEL` | the template's bold label at a line start | kept | `match` (anchored): `[ \t]*`, an optional marker that needs a space or tab after it, then a literal; a failed optional group gives back one character |
| `_PIPE`, `_CLOSING_PIPE` | table cells | kept | one character with a one-character lookbehind |
| `_DELIMITER` | GFM delimiter row | kept | `fullmatch`; per cell `[ \t]*`, `:?`, `-+`, `:?`, `[ \t]*`, then `\|`: adjacent atoms take disjoint characters |
| `_EXTENSION` | file extension of a path | kept | `search` succeeds only at a `.`; the run after it excludes `.`, so runs after different dots are disjoint |
| `_URL` | URL pointer | kept | `search` with a literal prefix and one final run with nothing after it |
| scanner (`brief_lint.py`, unchanged, P1): `_FENCE_OPEN`, `_FENCE_CLOSE`, `_FENCE_LIKE`, `_ATX`, `_SETEXT`, `_BREAK`, `_LIST_MARKER`, `_HTML_MARKED`, `_HTML_BLOCK_TAG`, `_HTML_COMPLETE_TAG` | block structure, one line at a time | kept, not edited (P1 decisions must equal HEAD) | all anchored to one line; `_ATX` and `_LIST_MARKER` have bounded runs (6, 9); `_BREAK`'s `X[ \t]*` repeats are split uniquely by `X`; `_HTML_COMPLETE_TAG`'s attributes are separated by required whitespace and a name holds none, so a failed line gives back each whitespace run a bounded number of times; measured on adversarial lines in the same property |

The P1 brief regexes of `brief_lint.py` (`_VERB`, `_QUOTED`, `_CODE` and the like) read brief text, not reports, and are out of this round's scope (unchanged since P1).

### Audit 2: every place the block reader picks one reading of an uncertain split

| Split | CommonMark (0.31.2) | Decision |
|---|---|---|
| Top-level paragraph, then a list marker | a marker interrupts a paragraph only when its item is not empty and an ordered one starts at 1 (5.2) | modelled exactly by the scanner (`_list_item`, `brief_lint.py:205-217`, used at :353) |
| List item paragraph, then a sibling marker of the same list type with text | a new item of the same list | modelled (scanner `items`) |
| List item paragraph, then a marker of another list type that may not interrupt a paragraph (ordered not starting at 1), or an empty marker of any type (R3F2) | spec prose: the line would otherwise be paragraph continuation text, so it is a lazy line; implementations differ on it (not verifiable offline) | **fail closed**: `_UNSURE_MARKER` names it, for a label in the paragraph that ends the item above and for every line of the new item |
| List item paragraph, then a line of the item's list type, same delimiter and any number | a sibling item | modelled |
| List item paragraph, then a kind-7 HTML start line (a lone complete tag) | kind 7 cannot interrupt a paragraph, so the line is lazy; the scanner opens an HTML block there | **fail closed**: `_LAZY_HTML` for a label in the paragraph above (round 3 picked the HTML reading: a false pass) |
| List item paragraph, then an HTML start of kinds 1-6, a fence, an ATX heading, a block quote, a thematic break | each may interrupt a paragraph | modelled (scanner) |
| List item paragraph, then a line indented 4+ that is no item content | lazy continuation, the scanner reads indented code | fail closed (`_LAZY_CODE`, round 3) |
| List item paragraph, then a top-level paragraph line | lazy continuation | modelled (`_tail`, `_continued`, round 3) |
| Anything right after a non-blank block quote line | may be lazy, quote content not read | fail closed (`_QUOTE`, `_LAZY_QUOTE`, round 3) |
| `---` or `===` after a list item paragraph | an underline cannot be lazy: a thematic break or paragraph text | modelled (scanner) |
| A paragraph that opens with `[` and holds `]:` | a link reference definition, title over lines | fail closed (`_DEFINITION`, round 3) |
| Nested items (an item's content scanned as its own document) | the same rules one level down | the same code runs on each level (`_locate` recursion) |

### Audit 3: every way a heading is matched to a template title

`_Heading.text` is the exact shown text (escapes and entity references resolved, code-span contents, link text; HTML, destinations, titles and reference labels dropped; emphasis delimiters and reference brackets kept, as without a definition). `_Heading.markup` is the same with emphasis and strike delimiters (`*`, `_`, `~`) and reference brackets (`[`, `]`, `![`) dropped and image descriptions kept: the most that markup could show.

| Match | Where | Reading | Why this direction |
|---|---|---|---|
| Section title `Completion report` (discovery, level and suffix) | `lint_report` | `text`, else `markup` (`_section_form`) | reading more headings as reports only adds checks; a written report is never skipped (R3F3); an odd suffix or level stays a `report-section` finding |
| Section title of a nested report | `_nested_starts` | `text`, else `markup` | same |
| `Result and evidence` heading as the label | `_done_check` | `text`; a heading whose `markup` only matches gets a named `done-check-output` finding | accepting a label through unread markup would pick a reading |
| `Research evidence` heading | `_evidence` | `text`; `markup`-only gets a named `evidence-table` finding | same |
| A heading that is a `Result` or `Evidence` title is no nested report | `_nested_starts` | `text` or `markup` | so the named finding above fires in its parent unit |
| Template's own section | `_template_lines` | `text == SECTION_TITLE` | the checked-in template, plain text |

The section title uses the exact text when it starts with the title and the markup reading only when it does not (`_section_form`), so an image or emphasis after a plain title never turns a report into an odd title.

### Progress (fix round 4)

- Done: readers (`_title_end`, `_label_end`, `_escaped_end`, `_html_end`, `_autolink_end`, `_Index`, `_path_lines`, `_atx_text`); `_Brackets` (event times instead of a sweep of every opener) and image descriptions marked once at the end; per-scan `_Facts` (item bounds, item content scans, paragraph ends, filled fences) and `_inline`/`_starts` caches cleared per `lint_report`; block refusals `_UNSURE_MARKER`, `_LAZY_HTML`, `_DEEP` (`_NESTING_MAX = 16`), `_padded_one`; headings `markup` reading, `_section_form`, `_markup_titles`.
- Evidence so far: `.tmp/T-021/p2fix4/confirm4.log` (round 3: 6 of 23 as expected; round 4: 21 of 23, the two `reference_result_heading` cases now a named refusal); `new-contract.log` (round 3: 3 failed; round 4: 3 passed); `matrix110.log` (0 wrong); `rt3-rerun.log` (retest 3 probes unchanged, P1 differential equal); `adversarial-round3.log` (title families: 5-second watchdog at 4000), `adversarial-round4.log` and `adversarial-round4-40k.log` (every family under 0.1 s at 40000).
- Then: tracked tests (`test_report_lint.py`, 311), mutants (11 of 11 killed), real files, the full suite.

### Decisions (fix round 4)

- **Readers, not grammars.** Each reader is a loop that consumes each character once; where a search must find the next closer, `_Index` builds the positions once per text and bisects. Dropped: keeping the regexes and making them possessive or atomic (Python 3.11 has both), which would remove the backtracking but not the restart-per-position searches (`_PATH_LINE`, `_ATX_CLOSE`, the backtick closer, the end markers), and leaves each kept regex to be argued again at the next review.
- **The bracket stack by event times.** Round 3 walked every opener at each link or reference (quadratic in nested brackets, measured 4 s at 40000 characters); `_Brackets` gives each opener the state the walk gave it (differential: `test_a_link_inside_a_link_and_a_reference_are_read_both_ways`, a new reactivation row passes on the round 3 tree and on this one; mutant M8).
- **Uncertain marker splits fail closed (R3F2).** Dropped: reading the spec prose exactly (a non-interrupting marker is a lazy line), because it could not be checked against a renderer or the spec offline, and a wrong exact reading is a false pass; a refusal is a false finding at worst, named. The same list's next item with text and a marker that may interrupt stay modelled, so normal lists never fail closed. The empty sibling of the same list is refused too (believed a sibling, not verified: assumption).
- **Discovery reads more, labels read exactly (R3F3).** A section heading is linted as a report when its markup reading shows the title, which can only add findings; a label or evidence heading is accepted only by its exact text, and a markup-only one is named. Dropped: resolving link reference definitions for headings, which would need definitions inside containers, which this lint does not read.
- **Per-scan caches.** `_Facts`, `_inline` and `_starts` are cleared by each `lint_report`, so memory stays one text's worth; they turn the per-label rescans (0.42 s at 4000 characters in round 3) into lookups.

### Configuration impact (fix round 4)

- No new CLI flag, config key or setting: `report_lint.py` still takes only file arguments (`main`, unchanged).
- Protocol-bound constants added (CommonMark 0.31.2 grammar): `_PATH_STOP`, `_PATH_AFTER`, `_LETTERS`, `_ALNUM`, `_TAG_NAME`, `_ATTRIBUTE_START`, `_ATTRIBUTE_NAME`, `_UNQUOTED_STOP`, `_SCHEME`, `_SCHEME_MIN` (2), `_SCHEME_MAX` (32), `_EMAIL_LOCAL`, `_DOMAIN_LABEL`, `_DOMAIN_LABEL_MAX` (63), `_TITLE_CLOSE`, `_ENTITY`, `_MARKUP`; removed: `_ATX_CLOSE`, `_PATH_LINE`, `_WS`, `_WS1`, `_ATTRIBUTE`, `_RAW_HTML`, `_AUTOLINK`, `_PUNCT`, `_TITLE`, `_LINK_LABEL`, `_ENTITY_OR_ESCAPE`. Message texts added: `_UNSURE_MARKER`, `_LAZY_HTML`, `_DEEP`, `_MARKUP_TITLE`. Safety-bound: `_NESTING_MAX = 16`. Caches: `_FACTS`, `lru_cache(256)` on `_inline` and `_starts`. `brief_lint.py` is not edited in this round.

### Configuration audit (fix round 4, rows changed or added)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Input task files | `report_lint.main` / `task_files` | unchanged: one or more paths; unreadable or undecodable exits 2 | README row | unchanged: `test_the_cli_prints_path_line_rule_and_exits_1`, `test_the_cli_refuses_an_unreadable_file_and_a_missing_argument`; new `test_the_cli_reads_every_adversarial_family_in_bounded_time` (30 files in one run) | configurable (no new flag or key in this round) |
| Inline readers | `_escaped_end`, `_title_end`, `_label_end`, `_inline_link`, `_destination`, `_html_end`, `_autolink_end`, `_Index`, `_Brackets`, `_inline`, `_starts` | left to right, each character once; parity of a backslash run decides an escape | none | equal to the round 3 grammar: `test_property_the_html_reader_reads_the_round_3_grammar`, `..._on_edge_tags` (27), `test_property_the_autolink_reader_reads_the_round_3_grammar`, `test_property_the_label_and_title_readers_respect_escape_parity`; refusal: `test_property_link_title_closers_respect_backslash_parity` (odd: the label is hidden), `test_the_retest_3_regressions` (title rows); mutants M1, M2, M8, M9, M10 | protocol-bound |
| Running time | every reader, `_Facts`, `_filled` | adversarial families finish within `TIME_BOUND` (1.0 s) at 4000 characters | none | `test_property_adversarial_inputs_finish_in_bounded_time` (30 families), `test_property_random_adversarial_text_finishes_in_bounded_time`, the CLI test above; measured logs below | test-only (`TIME_BOUND`, `SIZE`) |
| Pointer paths | `_path_lines`, `_PATH_STOP`, `_PATH_AFTER`, `_EXTENSION`, `_URL` | the paths `finditer` of the round 3 regex gave | README row | `test_property_the_path_reader_finds_the_round_3_pointers`, `..._on_edge_cells` (16, Unicode digits and spaces included); earlier pointer tests unchanged; mutant M11 | protocol-bound |
| Uncertain block splits | `_unsure_marker`, `_opener`, `_ends_open`, `_continued`, `_tail`, `_paragraph`, `_padded_one`, `_padded`, `_locate`, `_UNSURE_MARKER`, `_LAZY_HTML`, `_DEEP` | an uncertain split refuses the label with the construct named; a certain split reads the label | none | shown: `test_a_certain_split_still_shows_the_label` (4), the certain half of `test_property_a_marker_after_an_item_paragraph_splits_it_or_fails_closed`; refusal: its uncertain half, `test_an_uncertain_split_fails_closed_by_name` (4), `test_property_noninterrupting_ordered_marker_does_not_close_item_paragraph`; mutants M3, M4, M5 | protocol-bound |
| Nesting bound | `_NESTING_MAX` = 16 | deeper lists fail closed by name | none | `test_an_uncertain_split_fails_closed_by_name` (20 levels); input of 16 levels or fewer reads as before (`nested_markers_lines` family) | safety-bound |
| Heading titles | `_Heading.text`, `_Heading.markup`, `_shown(markup=)`, `_resolve_text`, `_MARKUP`, `_ENTITY`, `_section_form`, `_markup_only`, `_markup_titles`, `_MARKUP_TITLE`, `_atx_text` | section title: exact text, else the markup reading; label and evidence titles: exact text, markup-only named | none | shown: `test_property_shown_report_heading_is_linted_or_explicitly_refused` (the full report is clean), `test_a_heading_is_read_by_the_title_it_shows` (unchanged), `test_property_the_atx_closing_sequence_is_read_as_round_3`; refusal: `test_the_retest_3_heading_cases`, `test_property_a_title_shown_only_through_markup_is_named`; mutants M6, M7 | protocol-bound |
| Caches | `_FACTS`, `_inline` and `_starts` (`lru_cache(maxsize=256)`) | cleared at each `lint_report`; a `_Facts` entry holds its scan, so the identity key is never reused while it lives | none | every test that lints two texts in one process (the whole file runs in one interpreter); N/A as a user value | safety-bound (memory bound) |

All other rows of the earlier audits are unchanged.

## Phase P2 rebuild on markdown-it-py

Builder: fresh Claude Opus 5.5 (2026-11-16), worktree `T-021-p2r` at `aed0cd2`; decision 127 (rebuild on `markdown-it-py==4.2.0`, installed in `.venv` by the PM). Findings carried: Codex retest 4 R4F1, R4F2 (`<project>/.tmp/worktrees/T-021-p2/.tmp/T-021/retest4-p2/review.md:15-16`). R-021, R-015, R-025.

### Step 0 (approved by the PM 2026-11-16 with the answers below; PM decisions pending user review)

**Unknowns, read rather than assumed:**
1. What the parser exposes, and where it reads differently from CommonMark: research table 1.
2. Whether the parser is linear: it is not on adversarial inline input. The fix is under "Linear-time argument".
3. What the old lint decided: the class audit, table 2.

**Probe logs** are in `.tmp/T-021/p2r/`: `parser-scaling.log`, `parser-scaling-capped.log`, `nesting-probe.log`, `inline-lengths.log`. Their scripts sit next to them. They are scratch and are removed before handoff.

#### 1. Research: the tokens and fields the new lint relies on (`.venv/Lib/site-packages/markdown_it/`, 4.2.0, `METADATA:3`)

| Fact | Source | Class |
|---|---|---|
| A token holds `type`, `nesting`, `map` (`[line_begin, line_end]`, 0-based), `level`, `children`, `content`, `markup`, `info` and `attrs` | `token.py:23-62` | source-verified |
| `level` is the block nesting depth when the token is pushed: a close decrements it before the push, an open increments it after | `rules_block/state_block.py:119-127` | source-verified |
| The CommonMark preset sets `maxNesting: 20` and `html: True`. Block rules: blockquote, code, fence, heading, hr, html_block, lheading, list, reference, paragraph. `table` is not among them, so the lint calls `.enable("table")`. Strikethrough and linkify are off | `presets/commonmark.py:16-18,44-72` | source-verified |
| **Block depth limit:** at `state.level >= maxNesting` the block tokenizer skips to the container's end, and that content is silently dropped. Probe: 20 nested quotes or 18 nested lists drop a label, and the highest token level is 19 | `parser_block.py:75-79`; `nesting-probe.log` | source-verified, experiment |
| **Inline depth limit:** at `state.level >= maxNesting`, `skipToken` jumps to the end of the block, which misreads links (the library's own TODO). Probe: 21 `[` before `[x](u "src/a.py:3")` turns the whole link, title included, into text, which would be a false pointer | `parser_inline.py:146-168,189`; `nesting-probe.log` | source-verified, experiment |
| Core rule `inline` parses each `inline` token's `content` into `children`. `text_join` turns `text_special` (escapes and entities) into `text`, but only among the top-level children, not inside image children | `rules_core/inline.py:4-10`; `rules_core/text_join.py:18-53` | source-verified |
| **ATX heading:** `heading_open`, tag `h1`-`h6`, `markup` `#`×level, `map [startLine, line]`. The `inline` content has its closing sequence removed and is stripped | `rules_block/heading.py:57-67` | source-verified |
| **Setext heading:** `map [startLine, state.line]` includes the underline. The inline map ends before the underline | `rules_block/lheading.py:72-82` | source-verified |
| **Paragraph:** `paragraph_open`, `map [startLine, line]`. The `inline` content is the joined paragraph lines, lazy lines included (the parser decides) | `rules_block/paragraph.py:54-62` | source-verified |
| **Fence:** type `fence`, `info`, `content` (the body lines), `markup`, `map`. An unclosed fence runs to the end of its container or of the document | `rules_block/fence.py:52,91-92,125-138` | source-verified |
| **Indented code** is `code_block` and an **HTML block** is `html_block`, each with `content` and `map` | `rules_block/code.py:32-34`; `rules_block/html_block.py:86-88` | source-verified |
| **Lists:** `bullet_list_open` or `ordered_list_open`, then `list_item_open` with `markup`, `map` and `info` (the ordered number). **Block quotes:** `blockquote_open` and `blockquote_close` with a map. GitHub alerts appear only with the `alerts` option, which this preset does not set | `rules_block/list.py:174-182,232-236,304`; `rules_block/blockquote.py:281,316-323` | source-verified |
| **Reference definitions** go to `env["references"]` with no token, unless the `inline_definitions` option is set. Links and images resolve against that dictionary, so full, collapsed and shortcut references are read exactly as the document defines them | `rules_block/reference.py:160-181`; `rules_inline/link.py:89-121`; `rules_inline/image.py:90-122` | source-verified |
| **Table:** the header row fixes the column count. Each body row is padded or cut to that count: extra cells are dropped silently. Cells are split by the parser's `escapedSplit`, in which `\|` is a literal pipe. Each cell is an `inline` token with the row's `map`. Growth is bounded by `MAX_AUTOCOMPLETED_CELLS` | `rules_block/table.py:17,28-56,129-144,165-175,210-233` | source-verified |
| **Inline tokens:** `link_open` has `attrs` `href` and `title`; an autolink has `markup "autolink"` and `info "auto"`. `image` has `attrs` `src`, `alt` and `title`, and `children` parsed by a fresh inline parse of the description. `code_inline` `content` has newlines turned into spaces. `html_inline` `content` is the raw tag. `softbreak` and `hardbreak` mark line ends. `text_special` carries escapes and entities | `rules_inline/link.py:131-143`; `autolink.py:41-51`; `image.py:128-140`; `backticks.py:51-59`; `html_inline.py:34-35`; `newline.py:29-35`; `escape.py:51-54`; `entity.py:30-48` | source-verified |
| **Normalization:** line endings become `\n`; a BOM is not removed, so `﻿## x` is no heading. The lint strips one leading BOM before parsing | `rules_core/normalize.py` (read during implementation); BOM: assumption until the property test confirms it | assumption |
| **The parser is superlinear inside one inline block:** `parseLinkLabel` walks from each `[` to the end of the block, and `skipToken` is cached per position | `helpers/parse_link_label.py:12-44` | source-verified |
| Measured with the full parse, in seconds at 100k, 200k, 400k and 800k characters: `[` gives 0.94, 2.05 and 6.1, then the 20-second watchdog. `<a ` gives 0.18, 0.53, 2.6 and 15.3. `&amp;` gives 0.08, 0.20, 0.86 and 3.66. Two 2 MB runs ended in `0xC0000005` while the watchdog was dumping | `parser-scaling.log` | experiment |
| Block parse plus inline parse capped at 16384 characters per inline block: every family is linear up to 2 MB, roughly 2× per doubling. The worst families are paragraphs of `[` or `![` just under the cap, at 10 s per MB | `parser-scaling-capped.log` | experiment |
| The longest inline block in the repository's Markdown is 7392 characters (`USER_STANDING_RULES.md`). The longest in a task file is 2184 (`T-024`) | `inline-lengths.log` (166 files) | experiment |

#### 2. Class audit (S31): each old decision, finding and probe class, and the token rule that answers it

| Old decision, finding or probe class | How the old lint answered it | Token-level rule in the new lint |
|---|---|---|
| Which heading is a report (C-F1, G-F1, suffixes, case, Setext, closing `#`, levels 1 and 3) | `_form` on the text of top-level ATX or Setext headings | Only top-level `heading_open` tokens (`level == 0`) count. Their level comes from the tag. The title is the heading's **plain text**: the content of `text` and `code_inline` tokens at any depth, including image `children`, with HTML dropped and breaks turned into spaces. `_form`, `TITLE_SEPARATORS` and the level-2 rule are unchanged. A heading inside a container whose plain text starts with the title is a `report-section` finding, never skipped |
| Headings with emphasis, references or images (R3F3); code or HTML inside an image description (R4F1) | A second `markup` reading of the text, which lost the inner structure | The parser has already resolved emphasis, references and images. The plain text above therefore reads `**Completion report**`, `[Completion report][r]`, ``![`Completion report`](u)`` and `![<b>Completion report</b>](u)` as the title, and lints the report. The `markup`-only refusal (`_MARKUP_TITLE`) goes |
| `Result and evidence` and `Research evidence` headings | Exact text, with a named finding when only the markup reading matched | The same plain text and `_form`, read exactly. No named markup case remains |
| Where a report ends; nested reports (C-F1) | Line ranges, and `inner` lists rebuilt for each section (R4F2) | One forward pass over the token indices of top-level headings. A section ends at the next top-level heading of level 2 or above. A nested report opens at a deeper heading whose plain text starts with the title, or whose body (up to the next top-level heading) holds the `Work completed` or `Result and evidence` label. Units are index ranges, never rebuilt |
| Written or unwritten (C-F2, G-F2); the untouched template | Any non-blank line that is no heading and no comment block | The same rule over source lines. A line is covered when it lies in a heading `map` or in an `html_block` whose content starts with `<!--`. A per-line prefix sum gives each unit's answer in O(1). A reference definition line still counts as content, as before. The template comparison is unchanged and uses the parser's section range |
| The label (C-F4, RT1, RT4, R2F1, R2F2, R3F1, R3F2, list item, comment, HTML, indented code, inline comment, lazy line, quote) | `_LABEL` regex on a line, then the visibility machinery | A label is an `inline` token directly under `paragraph_open`, at any container depth, whose children hold `strong_open`, then exactly one `text` equal to the title with or without `:`, then `strong_close`. The group must be the first child or follow a `softbreak` or `hardbreak`. Code, HTML, comments, fences, link titles and lazy lines are decided by the parser. There is no regex over Markdown |
| The done-check fence (empty, unclosed, in a container, after the next heading) | Top-level `scan.fences` only, with "put it at the top level" | The first `fence` token with non-blank `content` after the label token and before the next top-level heading token. It is found through a `next_fence` array built in one backward pass. **Change:** a fence nested in the same list item or quote now counts (Q2) |
| Table discovery (C-F3, indented, fenced, in a list, quote or HTML) | Top-level plain lines with a delimiter regex | `table_open` tokens with `level == 0` between the `Research evidence` heading and the next top-level heading (G-F4: every table). The parser decides indented code, fences and HTML |
| Row width, escaped pipe, class cell | `_cells` with `(?<!\\)\|` | The cell count is `escapedSplit` from `rules_block/table.py`, the parser's own splitter, on the row's source line (`tr_open.map[0]`) with its outer pipes removed as the parser removes them. This keeps the old "the row has N cells" finding that the padding would otherwise hide. The class cell is the `inline` content, case-folded, checked against `CLASSES`. A row of empty cells is a placeholder, as before |
| Pointer in the source cell (G-F3, RT2, RT3, code span, hidden pointer, link destination, image, title, reference) | `_inline` segments, `_path_lines`, `_URL` | Shown text is the content of `text` and `text_special` tokens, with `softbreak`, `code_inline` and `image` boundaries read as a space and `html_inline` dropped. The candidates are that text plus every non-autolink `link_open` `href`. Any `code_inline` with non-blank content also counts. Not counted: `html_inline`, image `src`, `title` and `children`, and link `title`. The plain-text readers `_path_lines`, `_points`, `_EXTENSION`, `_URL` and the `./Makefile:3` hint carry over unchanged: they read pointer forms in plain text, not Markdown structure. `_HIDDEN_POINTER` names the token kind that holds pointer-like text |
| Uncertain splits that failed closed (R3F2 markers, a lone HTML tag after an item, `01.`, empty items, quotes, lazy quotes, lazy code, definitions, unsure references) | Named refusals | The parser decides each one, and its reading is the answer. These refusals go (Q2). The retest cases get a verdict against the reviewer's CommonMark expectation |
| Deep nesting (`_NESTING_MAX` 16) | Named refusal | Any token in a unit with `level >= maxNesting - 1` gets a named finding (Q4), because the parser may have dropped content there |
| Inline nesting misread | none | A guard rule added through the public `md.inline.ruler.before("text", ...)` returns False and records when `state.level >= maxNesting`. A block it flags gets the named finding |
| Running time (R4F2, R3F1 hang) | Hand-written readers with caches | See the linear-time argument below |
| Every retest probe (`review-p2`, `retest-p2`, `retest2-p2`, `retest3-p2` and `retest4-p2`: `cases/`, `confirm-cases/`, `regression-cases/`, the generators `probes.py`, `prior_probes.py`, `regressions.py`, `fix3_regressions.py` (110 cases), `own_probes.py`, `test_composition.py`, `test_new_contract.py`, `test_review_contract.py`) | Old exits | Each case runs through the new real CLI with a verdict against the reviewer's expected reading (done check). The generators run in place, with this worktree as the current directory, so no tree is copied. P1 differentials do not apply, because `brief_lint.py` is untouched |

#### 3. Linear-time argument

1. **Block parse:** each block rule advances `state.line`. The tests in table 1 show linear growth for all 26 families up to 2 MB (`parser-scaling-capped.log`). This is measured for the library, not proven.
2. **Inline parse:** this is the superlinear part (table 1). The lint parses inline only for the tokens it reads: top-level headings and the `inline` tokens inside report units. It parses them one at a time with `md.inline.parse(content, md, env, children)` after a block-only `md.parse` with core `inline` disabled, so `env["references"]` is complete first. A block longer than `INLINE_MAX` characters is not parsed and gets a named finding (fail closed). One block of length `m` costs at most `c·m·INLINE_MAX`, so the total is at most `c·n·INLINE_MAX`, which is O(n).
3. **Lint pass:** one forward pass collects the heading indices, label tokens and table rows. One backward pass builds `next_fence` and `next_heading`. Content lines use a prefix sum. Each unit is a slice of indices that is visited once. No list is copied or filtered per section or per label, which was R4F2. The total is O(tokens + lines + characters).
4. **Evidence:** a size sweep of these families, plus the R4F2 shapes (16000 labels in one paragraph, 16000 sections), through the real CLI at 2 MB, each in a child with a faulthandler watchdog. Linearity means the time ratio per doubling stays near 2. A tracked test checks that ratio at small sizes.

#### 4. The old tests (`<project>/.tmp/worktrees/T-021-p2/tools/agent-workflow/tests/test_report_lint.py`, 86 functions, 311 cases)

The final split comes from running them against the new lint, and it goes in the Completion report.

- **Carry over unchanged, expected:** about 54 functions (a count from the names, not yet run).
  - The contract: `complete_report_is_clean`, `unwritten_sections`, `prose_only`, `untouched_template`, `each_rule_fails_alone`, the `heading`, `row` and `line` findings, `class_vocabulary`, `heading_forms_label_forms`.
  - Fences: `heading_inside_a_fence`, `label_inside_a_fence`, `unclosed_fence`.
  - Tables: `table_in_a_fence_or_list`, `escaped_pipe`, `tables_in_indented_code`, `every_table_under`.
  - Section structure: `text_after_the_section`, `suffixed_report_titles`, `looks_like_a_report`, `nested_phase_report`, `result_heading_or_a_blank_parent`, `unwritten_nested`.
  - Labels: `label_in_a_comment_an_html_block_or_indented_code` (assertions on rule and line only), `inline_comment_that_spans_lines`, `label_in_a_list_item`.
  - Pointers: `citation_that_is_no_file`, `escaped_backticks`, `bare_file_name`, `pointer_forms`, `other_cells` (through `_has_pointer` and `_has_code_span`, whose names and signatures are kept).
  - The pointer and table properties; the BOM and line-ending property; the fence, nested-report, prose and hidden-label properties; `arbitrary_markup_never_crashes`; `tag_whose_attributes_run_over_lines`; `thematic_break`; `lazy_continuation_joins`; `link_inside_a_link`; `tabs`; `retest_2_regressions`.
  - The `_path_lines` oracle tests: the function is kept.
  - The CLI tests and the seven `cli_worker` record tests.
- **Their expectation follows the parser (rewritten):** about 20.
  - Fences: `fence_in_a_container_is_not_read`, `fence_under_a_container_is_never_taken`.
  - Message wording: `finding_for_a_hidden_label_names_its_line`, `finding_names_the_construct`, `pointer_finding_names_the_construct`.
  - Quote, definition and reference rows: `every_construct_hides_or_shows`, `pointer_counts_only_where_a_link_or_image_shows_it`.
  - Headings: `heading_is_read_by_the_title_it_shows`, `retest_3_heading_cases`, `title_shown_only_through_markup_is_named`, `shown_report_heading_is_linted_or_explicitly_refused`.
  - The retest 3 regressions.
  - Uncertain splits: `marker_after_an_item_paragraph`, `uncertain_split_fails_closed`, `certain_split`, `noninterrupting_ordered_marker`.
  - Titles: `link_title_closers_respect_backslash_parity`.
- **Dropped:** 12 tests of readers that no longer exist.
  - Scanner fields: `scanner_lists_top_level_list_items`, `scanner_records_paragraph_extents`.
  - `_inline`: `inline_scan_splits_text_code_and_html`.
  - The HTML, autolink, label, title and ATX oracles, and the round 3 grammar property tests.
  - The three fixed-size running-time tests, replaced by the ratio test.

#### 5. Questions (ranked; each with the answer I propose)

1. **Inline cap:** `INLINE_MAX = 8192` characters per inline block (paragraph, heading or cell), safety-bound. A longer block inside a report, or any longer top-level heading, gets a named finding and is not parsed. *Proposed: yes.* It is 3.7× the longest block in any task file, and the worst-case 2 MB file then takes about 5 s. 16384 doubles that time.
2. **The parser's reading replaces the old fail-closed refusals.**
   - A label inside a block quote or after a lazy line counts.
   - A fence nested in a list item or quote counts as done-check output.
   - Bold written `__…__` counts like `**…**`.
   - Evidence tables stay top-level only, as in the old contract.

   *Proposed: yes.* The parser is the CommonMark reading that decision 127 chose.
3. **The runtime version pin:** `report_lint.py` refuses to run unless `markdown_it.__version__ == "4.2.0"`. A missing package or another version exits 2 on the CLI and goes to `error` in the turn-end record. *Proposed: yes.* Another parser version can read structure differently, and R-015 rules out silent drift.
4. **Rule name for the new fail-closed findings** (an unread deep or over-cap part): reuse `report-section` and name the cause, so the rule set and exit codes stay the same. *Proposed: yes.*
5. **The pin's extent:** add `markdown-it-py==4.2.0` to the `test` extra, which is what CI installs. `mdurl` is not pinned, because the package declares `mdurl~=0.1` (`METADATA:23`). Add a row to `docs/dependencies-and-environments.md`. *Proposed: yes.*

#### Configuration impact (before the first runtime edit)

- **No new CLI flag and no config key.** `report_lint.py <task file>...` keeps exit codes 0, 1 and 2. The turn-end call keeps no switch (Q4 of P2: advisory).
- **New protocol-bound or artifact-bound constants:**
  - `PARSER_VERSION = "4.2.0"`, artifact-bound (Q3).
  - The parser preset `"commonmark"` plus `"table"`, protocol-bound by decision 127.
  - `INLINE_MAX = 8192`, safety-bound (Q1).
- **Carried unchanged:** the template vocabulary (`SECTION_TITLE`, `WORK_TITLE`, `RESULT_TITLE`, `EVIDENCE_TITLE`, `TITLE_SEPARATORS`, `TEMPLATE_PATH`, `EVIDENCE_COLUMNS`, `SOURCE_COLUMN`, `CLASS_COLUMN`, `CLASSES`) and the pointer readers (`_PATH_STOP`, `_PATH_AFTER`, `_EXTENSION`, `_URL`).
- **Removed:** every hand-written block and inline reader, together with its constants.
- **`cli_worker.py`:** the P2 turn-end record is ported as it was accepted for review (`REPORT_LINT_KEY`, `REPORT_LINT_MAX_FINDINGS = 50`, `REPORT_LINT_SHOWN = 5`, `report_lint_record`, `store_report_lint`, `status_rows`).
- **`pyproject.toml`:** `test` gains `markdown-it-py==4.2.0`.
- **`brief_lint.py`** is not edited: the P2 scanner additions of the old tree (`fences`, `html`, `items`, `paragraphs`) are not ported, because nothing needs them.

#### Plan and finish check

1. `report_lint.py` on tokens, plus the port in `cli_worker.py`.
2. Carry over the tests, then the new property and ratio tests.
3. Every old probe and confirm case through the CLI, with a verdict for each, and the 2 MB sweep.
4. 5 to 10 mutants.
5. The focused suites, then the tool suite once with `--basetemp=.tmp/T-021/p2r/pytest`, deleted afterwards.
6. Real task files, README, `pyproject.toml`, the dependencies row, the Configuration audit, the Completion report and `## Worker completion`.

The finish check is the brief's done check.

#### PM answers (2026-11-16, PM decisions pending user review)

All five questions were accepted, with these conditions:
- **Q1:** test at the cap and at the cap plus one; name the log and the curve, including the 16384 point.
- **Q2:** each changed expectation names its old test and the reason.
- **Q3:** one version constant, a test that compares it with the pyproject pin, and exit 2 with a message naming both versions.
- **Q4:** the message names the cause: depth, the cap, or an unresolved link.
- **Q5:** record `mdurl 0.1.2` as the observed transitive version.

Also:
- detect deep nesting from the parser's structure, never by counting characters;
- run all 86 old tests and report the real three-way split;
- remove `.tmp/T-021/p2r/` probes before handoff, keeping the small cited logs.

#### Progress (rebuild)

- **Done:**
  - the `cli_worker.py` turn-end record, ported verbatim from the reviewed P2 diff (`report_lint_record`, `store_report_lint`, the `status_rows` row, the calls in `run_codex_turn` and `run_turn`);
  - `report_lint.py` rewritten on tokens: block parse with core `inline` disabled, inline parsing per needed block up to `INLINE_MAX`, the `_depth_guard` inline rule, container depth from `token.level` on `blockquote_open` and `list_item_open`, per-index `next_heading` and `next_fence` arrays, content prefix sums, and units as index slices.
- **Old tests, first run:** a `.tmp` copy pointed at this tree gave `81 failed, 230 passed` (`.tmp/T-021/p2r/oldtests/run2.log`). The failures fall in 23 functions:
  - 6 test readers that no longer exist;
  - 17 involve expectations where the parser now decides what the old lint refused, or hint wording that is gone.
- **One reading difference to report:**
  - `## ~~Completion report~~` is no report heading, because the CommonMark preset has strikethrough off and the tildes are literal text (old test `test_property_shown_report_heading_is_linted_or_explicitly_refused`).
  - It is a PM question: enabling `strikethrough` (GFM) would read it as the title.
- **Then:** each disputed case was verified through a script file (`.tmp/T-021/p2r/oldtests/rows.py`, since PowerShell strips quotes from arguments); the tracked `test_report_lint.py` was written; the probes, the sweep, the mutants, the suites and the docs followed (Completion report, "Phase P2 rebuild").

#### Configuration audit (rebuild)

No value of this change is a deployment or user choice: the lint has no profile, flag or config key, and its turn-end call stays advisory with no switch (Q4 of P2). Every retained literal is classified below; "n/a" in the override column means the value is not configurable by design.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Parser version `PARSER_VERSION = "4.2.0"` (`report_lint.py:53`) | the pin `markdown-it-py==4.2.0` in `pyproject.toml` `[project.optional-dependencies] test` (what CI installs) | import-time comparison with `markdown_it.__version__`; a mismatch or a missing package raises `ParserUnavailable` and exits 2 naming both versions | `pyproject.toml:11` | `test_the_parser_version_is_the_pin_in_pyproject` (constant equals the pin); refusal: `test_another_parser_version_exits_2_and_names_both_versions` (a stub 9.9.9 package, real CLI), `test_a_missing_parser_raises_and_the_cli_exits_2`, `test_a_parser_of_another_version_is_recorded_by_the_turn_end_call` | artifact-bound (decision 127, PM Q3) |
| Parser preset `PARSER_PRESET = "commonmark"` plus `PARSER_EXTRAS = ("table",)` | none (decision 127) | the parser refuses an unknown preset or rule name at construction | n/a | n/a (not configurable); covered by every reading test | protocol-bound |
| Inline cap `INLINE_MAX = 8192` (`report_lint.py:86`) | none | a longer paragraph, heading or cell is not parsed and is a `report-section` finding naming `INLINE_MAX` | n/a | n/a (not configurable); at the cap and one over: `test_the_inline_cap_reads_a_block_at_the_cap_and_names_one_over_it` (paragraph and cell), `test_a_heading_over_the_cap_is_a_finding_never_skipped` | safety-bound (PM Q1) |
| Depth limit: the parser's own `maxNesting` (20 in the preset), read from `md.options`, never a literal in the lint | the preset (`presets/commonmark.py:16-18`) | container opens with `level >= maxNesting - 1` and the `_depth_guard` inline rule give a `report-section` finding | n/a | n/a; `test_a_container_at_max_nesting_is_named_never_read_as_absent` (18, 19 clean; 20, 25 and a 20-level list named), `test_a_link_nested_to_max_nesting_is_named_not_misread` (5 and 21 brackets) | protocol-bound (the parser's value) |
| `_CONTAINERS` (`blockquote_open`, `list_item_open`) and `_DEEP_FLAG` | none | the two block rules that recurse (`blockquote.py:309,320`, `list.py:286`); an internal `env` key | n/a | n/a; the two depth tests above | protocol-bound |
| Template vocabulary (`SECTION_TITLE`, `WORK_TITLE`, `RESULT_TITLE`, `EVIDENCE_TITLE`, `TITLE_SEPARATORS`, `EVIDENCE_COLUMNS`, `SOURCE_COLUMN`, `CLASS_COLUMN`, `CLASSES`, `TEMPLATE_PATH`) | `.agents/policy/AGENT_TASK_TEMPLATE.md` | carried unchanged from the reviewed P2 build | the template | n/a; `test_the_class_vocabulary_is_the_templates_and_case_is_folded`, `test_each_rule_fails_alone`, `test_suffixed_report_titles_are_reports_and_linted` | protocol-bound (carried) |
| Pointer readers (`_PATH_STOP`, `_PATH_AFTER`, `_EXTENSION`, `_URL`, `_path_lines`, `_points`) | none | plain-text pointer forms, carried unchanged | n/a | n/a; the path oracle tests, `test_pointer_forms_are_accepted`, `test_other_cells_are_not_pointers` | protocol-bound (carried) |
| Turn-end record bounds `REPORT_LINT_MAX_FINDINGS = 50`, `REPORT_LINT_SHOWN = 5`, key `REPORT_LINT_KEY` (`cli_worker.py`) | module constants, ported verbatim from the reviewed P2 diff | the record holds at most 50 findings and the warning prints 5 | n/a | override: `test_the_record_is_bounded_and_the_bound_is_a_module_constant` (2), `test_the_warning_shows_a_bounded_number_of_findings` (1); refusal n/a (advisory) | safety-bound (carried) |
| Test timings `TIME_BOUND`, `SIZE`, `RATIO`, the ratio test sizes 20000 and 80000 | tests only | n/a | n/a | n/a | test-only |

#### Fix round 1

Builder: fresh Claude Opus 5.5 (2026-11-16), same worktree, base `aed0cd2`, taking over the uncommitted rebuild. Findings: Codex review FAIL, P2R-F1, P2R-F2, P2R-F3, all P2 (`.tmp/T-021/review-p2r/review.md:14-77`). R-021, R-015, R-025. Step 0 below waits for the PM's approval; no tracked runtime file has been edited.

**Unknowns, read rather than assumed:**
1. Which parser stages and code paths run before each guarantee is checked: audit A.
2. Whether the reference quadratic can be removed without changing the reading: probe `.tmp/T-021/fix1/ref_probe.log` (script `ref_probe.py` beside it).
3. What real files need from the new bounds: `.tmp/T-021/fix1/real_probe.log` (29 files in `tasks/active` and `tasks/archive`: slowest 0.053 s, largest 266161 bytes, largest record of findings 1390 bytes, longest message 227 bytes, most findings 10).

##### Audit A (S31): every promised guarantee, each path that runs before its check, and the check that covers it

The guarantees (module docstring `report_lint.py:1-38`, P2 contract): **G1** fail-closed discovery (nothing that can be a report heading or label is lost without a finding); **G2** linear time; **G3** bounded record and warning; **G4** advisory exit codes (CLI 0, 1, 2 keep their meaning; the turn-end call never blocks, stalls or goes silent).

| Guarantee | Path that runs before the check (source) | Check today | Fix |
|---|---|---|---|
| G1 | Block tokenizer at `maxNesting` skips the container's tail (`parser_block.py:75-79`), inside `md.parse` (`report_lint.py:179`) | `_Doc.deep` is computed for the whole document (`:193-197`) but reported only inside a discovered unit (`:708-711`): **P2R-F1** | report at document scope (F1 below) |
| G1 | Inline depth limit, `INLINE_MAX` and `RecursionError` on a heading's text (`parser_inline.py:146-168`; `report_lint.py:223,230`) | document scope already: `head.text is None` for every top-level heading (`:642-644`), `_nested_headings` over all tokens for container headings (`:645`, `:612-629`) | none; a property case is added (an unread heading before, between and after reports) |
| G1 | Paragraphs and cells over the cap or unresolved (`:248-262`, `:501-504`) | unit scope | none: a paragraph or cell holds no heading, and a label only opens a nested report inside a section, where it is checked |
| G1 | `table` stops at `MAX_AUTOCOMPLETED_CELLS` (`rules_block/table.py:209-211`); later rows become paragraph text | unit scope | none: reaching the limit needs 65536 missing cells, so earlier rows already give "the row has N cells" findings (exit 1) |
| G1, G4 | Any exception in `md.parse` or the passes | CLI: an uncaught traceback exits 1, read as "findings" (`:740`); turn-end: caught (`cli_worker.py:1945`) | CLI catches per file and exits 2, `cannot lint` (Q4) |
| G2 | core `normalize` (`rules_core/normalize.py`, `str.replace`), `StateBlock` line marks (`rules_block/state_block.py`) | — | O(n) |
| G2 | `table` (`table.py:62-147` reads two lines; body `:184-239` one line per row plus one terminator check; cells bounded by `:209-211`) | — | O(lines read) |
| G2 | `code` (`code.py:18-33`), `fence` (`fence.py:89-136`, a scan to the closer; silent returns at the opener), `hr` (`hr.py:36`), `heading` (`heading.py:34`) | — | O(lines), one line for `hr` and `heading` |
| G2 | `blockquote` (`blockquote.py:132-325`: per line, terminators on lazy lines, nested tokenize one level down, restore loop `:332`) and `list` (`list.py:195-354`: nested tokenize per item, `markTightParagraphs` over the list's tokens `:354`) | — | O(n × depth), depth ≤ `maxNesting` |
| G2 | `reference` label and destination loops (`reference.py:31-106`; `string +=` on a local is amortized: label family 0.50, 1.03, 2.01, 4.05, 8.14 s at 125k to 2 MB, `ref_probe.log`) | — | O(chars) |
| G2 | **`reference` title continuation** (`reference.py:111-118`): `parseLinkTitle` is called per line and copies the accumulated title (`helpers/parse_link_title.py:37,74`) | none: before `INLINE_MAX` (`report_lint.py:223`): **P2R-F2**. `ref_probe.log`, stock: 0.60, 1.36, 3.67, 13.55 s at 125k to 1 MB, 2 MB past the 60 s watchdog (both `"` and `(` titles) | instance-level linear title helper (F2a) |
| G2 | `html_block` (`html_block.py:53-78`): seven regexes on the first line (`common/html_re.py:5-15`: anchored; attributes are separated by a mandatory `\s+` and names exclude spaces, so a split is unique), then the close regex per line | — | O(chars) |
| G2 | `lheading` (`lheading.py:24-68`) scans the paragraph with the paragraph terminators, then `paragraph` (`paragraph.py:24-50`) scans it again | — | O(lines), twice |
| G2 | Terminator checks (silent, `alt` chains `parser_block.py:30-42`): one line each (`table` reads one more) | — | O(1) lines per scanned line |
| G2 | Inline parsing, then the lint's own passes | capped per block; one pass (rebuild "Linear-time argument" 2-3) | none |
| G2, G4 | The turn-end call (`cli_worker.py:1939`) runs synchronously before the turn's state is saved, with no deadline; a linear 2 MB worst case still takes about 48 s (rebuild `sweep.log`) | none: **P2R-F2** | turn-end deadline (F2b) |
| G3 | `_check_table` embeds the raw class cell (`report_lint.py:498`); that cell is never inline-parsed, so `INLINE_MAX` does not bound it | none: **P2R-F3** | preview (F3) |
| G3 | `_odd_title` (`:557`) and `_nested_headings` (`:625`) embed the heading text | ≤ `INLINE_MAX` characters only | preview (F3) |
| G3 | The recorder stores whole messages (`cli_worker.py:1941-1943`) and the whole error text (`:1946`); the warning prints them (`:1948-1961`); `status_rows` prints the stored record (`:1998-2000`) | count only (`REPORT_LINT_MAX_FINDINGS`, `REPORT_LINT_SHOWN`) | byte bounds (F3) |

##### The fix for each path

- **F1 (fail-closed discovery):** `lint_report` reports dropped containers at document scope, before and independent of discovery: one `report-section` finding per top-level block that holds a container at `level >= maxNesting - 1`, at the line of its first such container, naming that a report heading or label there is not read. The unit-scope emission (`report_lint.py:708-711`) goes, so a dropped container inside a report gives one finding, not two. Tests: a Hypothesis property places a dropped report (quote chain, list chain, mixed quote and list chain) at depths `maxNesting - 3` to `maxNesting + 5` before, between and after valid reports, with LF and BOM plus CRLF, and asserts the finding exactly when the parser drops it and the valid reports unchanged; the reviewer's four `own.log` files as fixed cases.
- **F2a (block-level work, parser-native):** this lint's parser gets its own helpers namespace, `md.helpers = SimpleNamespace(parseLinkLabel=…, parseLinkDestination=…, parseLinkTitle=_title_continued)`. `MarkdownIt.__init__` sets `self.helpers = helpers`, the shared package module (`main.py:50`), and every rule calls `state.md.helpers.*` (`reference.py:78,110,118`; `rules_inline/link.py:20,51,70,98`; `rules_inline/image.py:22,50,69,100`), so an instance attribute changes this parser only, never a library global. `_title_continued` passes a first call through unchanged; for a continuation it calls the library's `parseLinkTitle` with a fresh state that carries only the marker, so nothing is copied, keeps the unescaped parts in a list and joins them once when the title ends. The library unescapes each call's slice on its own (`parse_link_title.py:62,74`), so the joined parts equal its string. Probe: 0.56, 1.16, 2.27, 4.50, 9.23 s at 125k to 2 MB (ratio 2.0 to 2.05), with the same tokens and references as stock wherever stock finishes. Test: a differential property (tokens and `env["references"]` equal to the stock parser) over generated definitions (each title marker, escapes, unterminated, garbage after the title, blank lines, terminators, nesting), plus a tracked ratio test for multiline titles and labels. **Rejected:** disabling `reference` (option 1 of the brief). It would make `## [Completion report][r]` literal text, so a defined-reference report heading would be skipped in silence (fail open), and `[x][a]` pointers in source cells would stop resolving.
- **F2b (turn-end limit):** `lint_report(text, template, deadline=None)`. The turn-end call passes `time.monotonic() + timeout_seconds`; the standalone CLI passes none. A rule that matches nothing, `report_lint_deadline`, is inserted first in the block chain and in the terminator chains (`md.block.ruler.before("table", …, {"alt": ["paragraph", "reference", "blockquote", "list"]})`, `ruler.py:128-148`). It therefore runs at every block start and at every line a multi-line rule scans; `_depth_guard` runs at every inline position, and the lint's own loops check too. On expiry it raises `LintDeadline`, which the recorder stores in `error`, and the turn completes. What runs between checks is one line of work, or the `fence`, `code` and `html_block` line scans (`fence.py:89`, `code.py:18`, `html_block.py:70`), each linear with no terminator calls. The overshoot is measured per sweep family with a 1 s deadline at 2 MB. A test shows that tokens are equal with and without the rule. **Rejected:** a child process killed at the deadline. Its bound is hard, but the venv `python.exe` is a redirector: a kill must go through the turn's job-object machinery (`cli_worker.py:904-917`) or it orphans the interpreter, and every turn pays a process start (Q2). Also rejected: an input byte cap alone, which bounds time only through the measured constant.
- **Sweep (done check):** the real CLI on each enabled block rule's worst shapes (the families of the audit rows: long, many and unclosed forms of table, code, fence, blockquote, list, reference title, label and destination, each `html_block` type, heading, lheading, paragraph) at 256k, 512k, 1M and 2M, each child in a faulthandler watchdog, then the same families with a 1 s deadline.
- **F3 (bounded record and warning):** in `report_lint.py`, `_preview(text)` keeps the first `PREVIEW_MAX` characters plus `… (N characters)` in the class-cell, odd-title and container-heading messages, so the standalone CLI is bounded too. In the recorder, each message and the error are cut to `message_max_bytes` and end with a visible `… [cut: K of N bytes]`. Findings are added while the record's JSON stays within `record_max_bytes`; `total` keeps the full count and a new `truncated` field the number dropped. Sizes are bytes of the JSON the registry writes (`save` uses `json.dumps` with the default ASCII escaping, `cli_worker.py:1319`, so bytes equal characters). The stderr warning is built from the bounded record and cut to `record_max_bytes` UTF-8 bytes. Tests: the reviewer's `record.log` shapes (1000, 100000 and 2000000 characters in a class cell, and one huge error), with byte assertions on the record and on stderr.

##### Configuration impact (fix round 1, before the first runtime edit)

New section `report_lint` in `tools/agent-workflow/guard_config.json`, validated by `hooks/guard_config.py` `SCHEMA` with `_positive_int`, like `worker_progress` (`guard_config.py:292-300`), and read through `cli_worker.guard_config()` (`cli_worker.py:297-307`) at the turn end. The two count bounds move there from module constants (Q1):

| Key | Value | Evidence | Class |
|---|---|---|---|
| `report_lint.max_findings` | 50 (moved from `REPORT_LINT_MAX_FINDINGS`) | carried, P2 | safety-bound |
| `report_lint.shown` | 5 (moved from `REPORT_LINT_SHOWN`) | carried, P2 | safety-bound (display) |
| `report_lint.message_max_bytes` | 512 | longest real message 227 bytes (`real_probe.log`) | safety-bound |
| `report_lint.record_max_bytes` | 16384 | largest real record 1390 bytes; 50 messages of real size are about 11 KB | safety-bound |
| `report_lint.timeout_seconds` | 10 | slowest real file 0.053 s, 29 files | safety-bound |

- **Refusal:** a missing key or a value that is not a positive integer gives `ConfigError` naming `guard_config.report_lint.<key>`. At the turn end it reaches `error` through the `SystemExit` that `guard_config()` raises (caught at `cli_worker.py:1945`), and the turn completes.
- **Override tests:** a temporary config with non-default values for each key.
- **Module constants** (the standalone lint has no config):
  - `report_lint.PREVIEW_MAX = 80` (characters), safety-bound, tested at the bound and one over;
  - `LintDeadline` and the rule name `report_lint_deadline`, protocol-bound.
- **Unchanged:** `INLINE_MAX`, `PARSER_VERSION`, the preset and exit codes 0, 1, 2. Exit 2 also covers an internal error (Q4).
- **Untouched:** `brief_lint.py`.

##### Questions (ranked; each with the answer I propose)

1. **Where the bounds live:** the `report_lint` section of `guard_config.json` above, with the two existing count bounds moved there. *Proposed: yes.* It is the validated file `cli_worker` already loads, so each bound gets a validated path, an example (the tracked JSON), an override test and a refusal test. The alternative is module constants with a validation at the turn-end call: no config change, but no example profile.
2. **Deadline mechanism:** cooperative and in-process, through the matching-nothing block rule in the main and terminator chains plus the inline guard. *Proposed: yes*, with the overshoot measured and reported. The dropped alternative is a killed child process (hard bound; needs the job-object path on Windows and a process start per turn).
3. **Values:** `message_max_bytes` 512, `record_max_bytes` 16384, `timeout_seconds` 10, `PREVIEW_MAX` 80. *Proposed: yes* (evidence in the table).
4. **CLI internal error:** an exception while linting one file prints `<file>: cannot lint (<type>: <message, cut to PREVIEW_MAX>)` and exits 2 instead of a traceback with exit 1, which reads as "findings". The standalone CLI keeps no deadline. *Proposed: yes.*
5. **F1 granularity:** one finding per top-level block that holds a dropped container, at its first dropped container's line, anywhere in the file, inside a report or not. *Proposed: yes*, because the dropped content may itself be a report. Real files have none: no real file reaches the depth (to be confirmed by the standalone run).

##### PM answers (fix round 1)

Approved 2026-11-16; the user is away, so each answer is a conservative, reversible PM decision pending user review.
1. **Bounds location: accepted.** A `report_lint` section in `guard_config.json`, validated in `hooks/guard_config.py`, with the two count bounds moved there. Because every hook loads `guard_config.py`: `test_guard_config.py`, `test_tool_guard.py` and the hook tests join the focused set; a config without the new section is shown refused with its exact message, and the hooks shown to start with the tracked file.
2. **Deadline: the cooperative in-process rule is accepted.** Report the overshoot per sweep family as the maximum over runs. Expiry is recorded in `error` naming `LintDeadline`, and the turn completes and saves its state (a test proves both). Keep the token-equality test with and without the rule. The killed child stays rejected; if any family overshoots by more than 2 s at 2 MB, stop and report before changing the design.
3. **Values: accepted** (512, 16384, 10, `PREVIEW_MAX` 80, all safety-bound, with the real-file evidence).
4. **CLI internal error: accepted.** `cannot lint`, exit 2, documented in the module docstring and the README row.
5. **F1 granularity: accepted.** One finding per top-level block, anywhere in the file.

Also: the rejection of disabling `reference` is accepted; the differential property against the stock parser (tokens and `env["references"]` equal wherever stock finishes, over each title marker, escapes, unterminated titles, blank lines and terminators) is the key proof. One fixed `--basetemp` under `.tmp/T-021/fix1/`, deleted after each run. No network, installs or Git writes; children use `CREATE_NO_WINDOW`.

##### Progress (fix round 1)

- **Done (code):**
  - `report_lint.py`:
    - `_dropped` reports dropped containers at document scope, one finding per top-level block (`_Doc.top`), and the unit-scope emission is removed;
    - `_title_continued` plus an instance `md.helpers` namespace;
    - `_deadline_rule`, first in the block chain and the terminator chains, plus `_check_deadline` in `_depth_guard` and the lint passes; `lint_report(..., timeout=None)` raises `LintDeadline`;
    - `_preview` with `PREVIEW_MAX = 80` in the class-cell, odd-title and container-heading messages;
    - the CLI prints `cannot lint` and exits 2;
    - docstring updated.
  - `cli_worker.py`: `report_lint_record` reads `guard_config()["report_lint"]`, passes the timeout, cuts messages and the error with `cut_bytes`, keeps findings within `record_max_bytes` (new field `truncated`), and cuts stderr in UTF-8 bytes. `REPORT_LINT_FALLBACK_BYTES = 512` is used when the section itself fails. The two count constants were removed.
  - `hooks/guard_config.py`: `_report_lint` section, with floors `REPORT_LINT_MESSAGE_FLOOR = 64` and `REPORT_LINT_RECORD_FIXED = 256` (record ≥ 2 × message + 256). The `report_lint` section was added to `guard_config.json`.
- **Done (tests):** new tests in `test_report_lint.py` (F1, F2a, F2b and F3, the refusal and hook tests) and in `test_guard_config.py` (INVALID rows, the exact missing-section message, floors).
- **Run on the current tree:** `test_report_lint.py` plus `test_guard_config.py`, 512 passed; the one failure was a test bug, since fixed.
- **Run on the pre-fix build:** scratch tree `.tmp/T-021/fix1/before/` (pre-fix files rebuilt by `revert.py` in `baseline/`). 26 of the new report-lint tests fail; `test_guard_config.py` fails at collection (`before.log`). Seven pass:
  - the unread-heading property, already at document scope before the fix;
  - the stock-differential property (stock equals stock);
  - the five reference ratio tests: at 20k to 80k characters the quadratic is below the per-line constant, so the copy counter test is the fail-before proof for F2a.
- **Done (checks):** everything in the plan below is done: focused suites, both sweeps, the reviewer's three probes rerun in place, 10 mutants, the whole suite once, the real files, README rows, the audit, the report and the cleanup. Results are in the Completion report section `Phase P2 rebuild, fix round 1`.

##### Configuration audit (fix round 1, rows added or changed; the rebuild rows above stand otherwise)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Record count `max_findings` = 50 (moved from `REPORT_LINT_MAX_FINDINGS`) | `guard_config.json` `report_lint.max_findings` | `hooks/guard_config.py` `_report_lint`: `_positive_int`, required, unknown keys refused | `tools/agent-workflow/guard_config.json` | override: `test_the_record_is_bounded_by_the_configured_max_findings` (2); refusal: `test_invalid_values_are_refused[report_lint.max_findings=0]`, `test_every_field_is_required[report_lint.max_findings]` | safety-bound |
| Warning count `shown` = 5 (moved from `REPORT_LINT_SHOWN`) | `report_lint.shown` | as above | same | override: `test_the_warning_shows_the_configured_number_of_findings` (1); refusal: `…[report_lint.shown=True]`, `test_an_invalid_report_lint_section_is_recorded_by_name_never_silent` (`shown` 0 at the turn end) | safety-bound (display) |
| Per-message bound `message_max_bytes` = 512 (JSON bytes as the registry writes them) | `report_lint.message_max_bytes` | `_positive_int`, and at least `REPORT_LINT_MESSAGE_FLOOR` (64, room for the cut marker) | same | override: `test_the_record_and_the_warning_stay_within_their_byte_bounds[*-bounds1,2]` (64, 1000), `test_report_lint_bounds_accept_non_default_values_at_their_floors`; refusal: `…[report_lint.message_max_bytes=63]`, `…='512'` | safety-bound (evidence: longest real message 227 bytes, `real_probe.log`) |
| Aggregate bound `record_max_bytes` = 16384 (the record's JSON; the stderr line in UTF-8 bytes) | `report_lint.record_max_bytes` | `_positive_int`, and at least 2 × `message_max_bytes` + `REPORT_LINT_RECORD_FIXED` (256) | same | override: the byte-bounds test (384, 4000), the floors test; refusal: `…[report_lint.record_max_bytes=1279]` | safety-bound (largest real record 1390 bytes) |
| Turn-end deadline `timeout_seconds` = 10 | `report_lint.timeout_seconds` | `_positive_int` | same | override: `test_a_deadline_at_the_turn_end_is_recorded_and_the_turn_saves_its_state` (1 s on a 2 MB file), the fake's `timeouts` assertion in the byte-bounds test; refusal: `…[report_lint.timeout_seconds=0]`, `…=1.5` | safety-bound (slowest real file 0.053 s) |
| The `report_lint` section itself | `guard_config.json` | required section; every hook loads the file | same | refusal: `test_a_config_without_the_report_lint_section_is_refused_with_its_exact_message` (`old.json is missing required keys: report_lint`), `test_the_hooks_start_with_the_tracked_config_and_refuse_one_without_the_section` (tool_guard exit 0 with the tracked file, exit 2 naming the section without it), the turn-end refusal test above; `test_every_field_is_required[report_lint]` | protocol-bound (schema) |
| Floors `REPORT_LINT_MESSAGE_FLOOR = 64`, `REPORT_LINT_RECORD_FIXED = 256` (`hooks/guard_config.py`) | none (validation constants) | used by `_report_lint` | n/a | n/a; at the floors: the floors test; one under: the INVALID rows | safety-bound |
| `REPORT_LINT_FALLBACK_BYTES = 512` (`cli_worker.py`) | none: used only when the `report_lint` section cannot be loaded, so there is no bound to read | cuts the error, the path and the stderr line on that path | n/a | n/a; `test_an_invalid_report_lint_section_is_recorded_by_name_never_silent` | safety-bound |
| `PREVIEW_MAX = 80` characters (`report_lint.py`) | none (the standalone lint has no config; PM answer 3) | `_preview` in the class-cell, odd-title and container-heading messages and the `cannot lint` line | n/a | n/a; at the bound and one over: `test_raw_text_in_a_message_is_cut_to_the_preview_bound` | safety-bound |
| Deadline rule `report_lint_deadline`, its env key and chains `paragraph`, `reference`, `blockquote`, `list`; `LintDeadline` | none | inserted first in the block ruler (`ruler.py:128-148`); matches nothing | n/a | n/a; `test_property_the_deadline_rule_changes_no_token`, `test_a_passed_deadline_stops_the_lint_by_name` | protocol-bound |
| Instance helpers namespace (`parseLinkLabel`, `parseLinkDestination`, `parseLinkTitle=_title_continued`) and the private `_State` import of the pinned 4.2.0 | none (`PARSER_VERSION` pin) | the version check at import | n/a | n/a; `test_property_the_joined_title_reads_as_the_stock_parser`, `test_the_title_helper_is_this_parsers_own_and_the_library_is_untouched`, `test_a_multiline_reference_title_is_copied_once_not_per_line` | artifact-bound (the pinned parser's API) |
| CLI exit 2 for an internal error (`cannot lint`) | none | `main` catches `Exception` per file | README row | n/a; `test_the_cli_exits_2_on_an_internal_error_and_says_cannot_lint` | protocol-bound (exit codes) |
| Test values: `REFERENCE_FAMILIES` sizes 20000 and 80000, the 0.2 s and 2.0 s deadline bounds, `FakeLint` (a labelled double), the sweep's sizes and its 120 s watchdog | tests and scratch only | n/a | n/a | n/a | test-only |

##### Plan and done check (after approval)

1. F1 and F2a in `report_lint.py`, with their tests written first, failing on the current build.
2. F2b and F3 in `report_lint.py`, `cli_worker.py`, `guard_config.json` and `hooks/guard_config.py`, with their tests.
3. The reviewer's `own.log`, `performance.log` and `record.log` rerun in place, a verdict per case; the block-rule sweep and the deadline sweep.
4. 5 to 10 mutants on the changed lines; the focused tests, then the tool suite once with `--basetemp=.tmp/T-021/fix1/pytest`, deleted afterwards.
5. The standalone lint on the real `tasks/active/*.md`, README rows, the updated Configuration audit, a fix round 1 Completion report and `## Worker completion`. Scratch `.tmp/T-021/fix1/` is removed except the small cited logs.

## Completion report

### Phase P2 rebuild, fix round 1 (P2R-F1, P2R-F2, P2R-F3)

- **Work completed:**
  - **F1:** `report_lint.py` reports parser-dropped containers at document scope (`_dropped`): one `report-section` finding per top-level block, anywhere in the file, before discovery. The per-unit copy is gone.
  - **F2a:** the reference rule's multi-line title is joined once by `_title_continued`, installed as this parser's own `md.helpers` namespace; the library is not patched.
  - **F2b:** a deadline rule that matches nothing sits first in the block chain and the terminator chains, with checks in `_depth_guard` and in the lint passes. `lint_report(..., timeout=None)` raises `LintDeadline`; the turn-end call passes `report_lint.timeout_seconds`.
  - **F3:** `_preview` (`PREVIEW_MAX = 80`) cuts raw text in messages. `cli_worker.report_lint_record` cuts each message and the error to `message_max_bytes` with a visible `… [cut: N bytes]`, keeps findings while the record's JSON fits `record_max_bytes` (new field `truncated`; `total` keeps the full count), and cuts the stderr line in UTF-8 bytes.
  - **Configuration:** the bounds are a validated `report_lint` section of `guard_config.json` (`hooks/guard_config.py`, with floors). The two count constants moved there.
  - **CLI:** an internal error prints `cannot lint` and exits 2.
  - **Docs:** module docstring and README row and Report-gate bullet.
  - **Tests:** 33 new test functions (28 in `test_report_lint.py`, 2 functions and 8 rows in `test_guard_config.py`), and 2 rewritten for the moved bounds.
  - **Answers:** PM answers 1 to 5, recorded above.
- **Result and evidence:**

```text
# the reviewer's probes, rerun in place (.tmp/T-021/review-p2r/*-fix1.log)
own (review_driver.py own; own-compare.log): 33 cases, 20 unchanged, 13 changed, each a dropped-container case now named:
  dropped_report_quote_20/21/25, _list_19/20/21/25, dropped_before_valid_20/21/25, quote_20_bom_crlf: exit 0 -> 1
  [report-section] "... maxNesting ...";  quote_19 / before_valid_19: + the dropped `> * 19 - label` line   VERDICT: F1 closed
performance (extra.py performance): reference 250k 1.23 s, 500k 2.51, 1M 5.32, 2M 10.15, 4M 20.67 (was 1.37, 3.38, 14.24,
  watchdog, watchdog); instructions 500k 10.88, 1M 21.81, 2M 42.88 (was 12.71, 22.86, 46.79)          VERDICT: F2 closed
record (extra.py record): class 1000 / 100000 / 2000000 chars -> record 455 / 457 / 458 bytes, stderr 411 / 413 / 414
  (was 1335 / 100335 / 2000335)                                                                        VERDICT: F3 closed

# block-rule sweep, real CLI, 44 families x 256k/512k/1M/2M, 120 s faulthandler watchdog each (sweep.py scale; sweep.log)
{"runs": 176, "watchdog": 0, "bad_exit": 0, "max_seconds": 13.661, "max_ratio": 2.12}
  slowest at 2 MB: list_items 13.66 s, list_ordered 10.65, reference_title_paren 10.64, reference_title 10.13, reference_label 10.01
# deadline sweep, each family at 2 MB with timeout 1 s, three runs, overshoot = max over runs (sweep.py deadline; deadline.log)
{"families": 44, "max_overshoot": 0.377, "worst": "table_wide_header"}   next: table_delimiter_lines 0.106, html_div 0.071
  19 families finish inside 1 s at 2 MB (no deadline reached); no family near the PM's 2 s stop line

# the new tests on the pre-fix build (scratch tree .tmp/T-021/fix1/before/, pre-fix files rebuilt by revert.py; before.log)
test_report_lint.py -k <new tests>: 26 failed, 7 passed;  test_guard_config.py: ERROR at collection (no report_lint floors)
  the 7 that pass there: the unread-heading property and the stock differential (true before the fix too), and the five
  reference ratio tests at 20k/80k (the quadratic shows only past ~500k; the copy-counter test is F2a's fail-before proof)
# after the fix
test_report_lint.py test_guard_config.py: 513 passed in 30.18s (run2.log), then +1 test (large max_findings): 10 passed -k

# mutants on the changed lines, scratch copies of the tool tree (mutants.py; mutants.log, mutants-m5.log)
{"mutants": 10, "killed": 9}; M5 (no byte check while adding findings; the final trim still bounds bytes but re-measures
the record per dropped finding) survived, killed by the added test_a_large_max_findings_keeps_the_record_linear: 1 of 1
  M1 one finding per container, M2 no document-scope check, M3 join per line, M4 deadline rule out of the terminator chains,
  M5 above, M6 marker not budgeted, M7 preview off by one, M8 internal error exits 1, M9 record floor 1x, M10 no timeout

# focused suites (test_report_lint, test_guard_config, test_cli_worker, test_cli_worker_codex, test_tool_guard,
# test_guard_settings, test_worker_progress, test_hook_robustness, test_codex_hooks, test_codex_hook_probe, test_push_guard,
# test_question_guard, test_write_guard_nested, test_final_guard, test_process_guard, test_process_guard_consumers,
# test_brief_lint; focused.log)
1 failed, 4157 passed, 3 skipped in 370.90s   the 1: my byte-bounds test expected a cut marker on a warning that fits; fixed,
  then 513 passed (run2.log)
# whole tool suite once (tools/agent-workflow/tests, --basetemp .tmp/T-021/fix1/pytest, deleted after; full-suite.log)
2 failed, 6262 passed, 12 skipped in 928.01s   both test_setup_project (real_checkout, apply_binds_a_copy): "links: 36 dead
  relative link(s); first: tasks/active/T-010-k2-k3.md -> ../../.tmp/T-010/p1-targeted-final.log ..." - T-010's .tmp logs
  are not in this worktree (31 of the links are in T-010-k2-k3.md; none in the T-021 file); environmental, not this change

# standalone lint on the real files (17 in tasks/active; real-files.log, real-files-before.log)
exit 1, 48 findings in 14 files; the same file:line:rule list as the pre-fix build; no maxNesting or cannot-lint finding
# static
ruff: No module named ruff (not in the project .venv);  git diff --check -- tools/agent-workflow docs pyproject.toml
tasks/active/T-021-brief-order-scope-guard.md -> exit 0
```

- **Important changes and reasons:**
  - **F2a stays on the parser's own reading:** the differential property (400 examples) compares tokens and `env["references"]` with the stock parser. Disabling `reference` was rejected (PM-accepted): it would silently skip a reference-linked report heading.
  - **Why the deadline is cooperative:** its checks cover every block start, every line a multi-line rule scans for a terminator, and every inline position. The worst gap measured is one table-header split (0.377 s at 2 MB).
  - **Moved bounds:** they live in `guard_config.json`, which every hook validates. A config without the section is refused by name (`old.json is missing required keys: report_lint`), and tool_guard blocks shells with that message.
- **Problems, risks and remaining work:**
  - **`report_lint.py` imports `markdown_it.helpers.parse_link_title._State`:** this is a private name, held by the exact version pin and the differential test.
  - **The standalone CLI has no deadline:** PM answer 4. The 2 MB worst case is still about 43 s there, and 13.7 s for the block families.
  - **The turn-end record changed shape:** it gained `truncated`, and stderr warnings can carry cut markers.
  - **Not run:** ruff, Linux and CI (below).

### Research evidence (fix round 1)

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| Where the reference rule is quadratic | `.venv/Lib/site-packages/markdown_it/rules_block/reference.py:111-118`, `markdown_it/helpers/parse_link_title.py:37,74`, 4.2.0 | raw | each continuation copies the title so far | source-verified |
| Whether a helper can be replaced for one parser only | `markdown_it/main.py:50`; `markdown_it/rules_block/reference.py:78,110,118`; `markdown_it/rules_inline/link.py:20,51,70,98`; `markdown_it/rules_inline/image.py:22,50,69,100` | raw | `self.helpers = helpers` (module); every call goes through `state.md.helpers` | source-verified |
| Where a block rule placed first runs | `markdown_it/parser_block.py:30-42,60-95`; `markdown_it/ruler.py:128-148`; `markdown_it/rules_block/reference.py:195-229` | raw | main chain at each block start; `alt` chains as terminators per scanned line | source-verified |
| The joined title is linear and reads as stock | `.tmp/T-021/fix1/ref_probe.log`; `test_property_the_joined_title_reads_as_the_stock_parser` | raw output | stock 13.55 s at 1 MB, 2 MB past 60 s; joined 4.50 and 9.23 s; same tokens | experiment |
| Block rules scale linearly, deadline overshoot | `.tmp/T-021/fix1/sweep.log`, `.tmp/T-021/fix1/deadline.log` | raw output | 176 runs, ratio ≤ 2.12; overshoot ≤ 0.377 s | experiment |
| Values for the new bounds | `.tmp/T-021/fix1/real_probe.log` | raw output | slowest 0.053 s, record 1390 bytes, message 227 bytes | experiment |
| Registry byte measure | `tools/agent-workflow/cli_worker.py:1319` (`save`, `json.dumps(data, indent=1)`, default ASCII escapes) | raw | bounds measure `json.dumps` of the record; the registry's indentation adds a bounded per-line overhead | source-verified |

### Not run (fix round 1)

- `ruff check` and `ruff format --check`: not_run, because ruff is not in the project `.venv` (`No module named ruff`) and no installs are allowed.
- Linux and CI: not_run (Windows only; CI belongs to the PM's window, R-025).
- Live Claude or Codex workers: not_run. Turn-end behaviour is covered by the labelled fake-CLI turns and the `FakeLint` double.
- The reviewer's `timings` and `replay` probes: not rerun. The brief named `own`, `performance` and `record`, and replay reads the old worktree.

### Noticed, not done (fix round 1)

- The standalone CLI's worst case is still about 43 s on 2 MB of unclosed `<?` paragraphs just under `INLINE_MAX`, which is linear and approved. A CLI deadline would be a PM choice.
- `README.md` has no list of `guard_config.json` sections. The `report_lint` keys are described only in the Report-gate bullet.

### Changed files (fix round 1)

- **Edited:** `tools/agent-workflow/report_lint.py`, `tools/agent-workflow/cli_worker.py`, `tools/agent-workflow/hooks/guard_config.py`, `tools/agent-workflow/guard_config.json`, `tools/agent-workflow/tests/test_report_lint.py`, `tools/agent-workflow/tests/test_guard_config.py`, `tools/agent-workflow/README.md`, this task file.
- **Scratch kept** (small cited logs only), in `.tmp/T-021/fix1/`: `ref_probe.log`, `real_probe.log`, `before.log`, `run1.log`, `run2.log`, `focused.log`, `full-suite.log`, `mutants.log`, `mutants-m5.log`, `sweep.log`, `deadline.log`, `own-compare.log`, `real-files.log`, `real-files-before.log`.
- **In the reviewer's folder:** `own-fix1.log`, `performance-fix1.log`, `record-fix1.log`.
- **Removed:** scripts, the baseline and before trees, mutant copies, sweep inputs, the pytest basetemp and the regenerated case files.

### Phase P2 rebuild on markdown-it-py

- **Work completed:** `tools/agent-workflow/report_lint.py` rebuilt on the token stream of `markdown-it-py==4.2.0` (CommonMark preset plus `table`; block parse with core `inline` disabled, inline parsing per block the lint reads, capped at `INLINE_MAX = 8192`; container depth from `token.level`, inline depth from the `_depth_guard` rule added with `md.inline.ruler.before`; next-heading and next-fence arrays from one backward pass, content prefix sums, units as bisect slices). No hand-written block or inline scanner and no regex over Markdown structure remain; the plain-text pointer readers carry over. The `cli_worker.py` turn-end record is ported from the reviewed P2 diff. `test_report_lint.py` holds the old tests (split below) plus 12 new ones; the Codex turn test is ported into `test_cli_worker_codex.py`. `pyproject.toml` pins `markdown-it-py==4.2.0` in `test`; a row in `docs/dependencies-and-environments.md`; the README row and "Report gate" bullet. `brief_lint.py` untouched. R4F1 and R4F2 closed (evidence below).
- **Result and evidence:**

```text
# every retest case file R1-R4 (review-p2, retest-p2 .. retest4-p2, read in place in the old worktree), the real CLI of
# this tree against the reviewed P2 CLI (.tmp/T-021/p2r/cases.py; cases.log)
{"cases": 223, "same": 190, "different": 33}
  verdict on the 33, each the expected reading under the new contract:
  12 R3F2 markers (non1_ordered_lazy_span/html, empty_marker_lazy_span; retest3 and retest4, lf and bom_crlf): exit 0,
     a new list starts (list.py:130-135); old exit 1 was the named refusal
  9 fences in a list item or quote (list_fence, quote_fence x3 folders) and quote_fenced_lazy_label x3: exit 0, Q2;
     a fence takes no lazy line, so the label after `> ```` is a top-level paragraph
  3 retest2 confirm list_ordered_label, list_same_line_nested, list_same_line_ordered: exit 0, a label in a list item
     ("not newly ranked" in retest2-p2/review.md)
  4 reference_result_heading: exit 0, the reference resolves (old: named refusal, reviewer expected 0)
  4 R4F1 image_code/image_code_part/image_html/image_reference_code headings: now exit 1 [done-check-output], the
     report is linted (old exit 0: skipped, the finding); empty_result_image: exit 0, the image title is read

# the reviewers' own contract tests, run in place (--import-mode=importlib, this tree as cwd; contracts.log)
retest4 test_composition.py, test_new_contract.py, test_review_contract.py; retest3 test_new_contract.py,
test_review_contract.py; retest2 test_review_contract.py:  3 failed, 15 passed in 2.52s
  the 3: test_image_description_inline_formatting_gets_a_named_title_refusal (asks for the old refusal message; the
  title is now read, clean) and test_noninterrupting_ordered_marker_does_not_close_item_paragraph x2 (new list, above)

# 2 MB sweep of the real CLI, 32 families x {whole, chunked at INLINE_MAX - 200} x {256k, 512k, 1M, 2M}, each child in
# a 120 s faulthandler watchdog (.tmp/T-021/p2r/sweep.py; sweep.log)
{"runs": 256, "max_seconds": 48.395, "max_doubling_ratio": 2.14}      watchdog fired: 0; every exit 0 or 1
  worst per doubling: instructions_unclosed/chunked [2.14, 1.94, 1.9] 48.4 s at 2 MB; cdata_unclosed/chunked
  [2.0, 2.05, 2.03] 31.7 s; comments_unclosed/chunked 30.8 s; references_unclosed/chunked 17.4 s; brackets/chunked
  1.47, 2.80, 5.51, 10.79 s
  R4F2 shapes at 2 MB: paragraph_label_lines whole 0.84 s (one block over the cap: named), chunked 2.36 s (clean);
  item_label_lines 0.79 / 2.29 s; sections (81620 reports) 3.81 s; nested_reports (56609) 4.78 s
  the cap curve: at 16384 (Step 0, parser-scaling-capped.log) chunked `[` 2.24, 4.65, 10.0 s at 250k, 500k, 1M, then
  the 20 s watchdog at 2M; at 8192 the same family 1.47, 2.80, 5.51, 10.79 s at 256k .. 2M

# old tests on this tree: the copy (.tmp/T-021/p2r/oldtests/run2.log) 81 failed, 230 passed; split from the source
# (oldtests/split.log): 92 functions = 69 unchanged + 15 new expectation + 8 dropped (list below)

# mutants on the changed lines, scratch copies of the tool tree (.tmp/T-021/p2r/mutants.py; mutants.log, mutants-m5.log)
{"mutants": 11, "killed": 10}  then M5 (a label anywhere on a line) killed by the added
test_a_bold_title_inside_a_line_is_no_label: {"mutants": 1, "killed": 1}  -> 11 of 11
  M1 container depth off by one, M2 inline guard off by one, M3 cap + 1, M4 empty fence counts, M5 label mid-line,
  M6 link title shown, M7 nested tables count, M8 BOM kept, M9 no container-heading finding, M10 fence/heading
  boundary, M11 cell count from the padded row

# focused suites (before the M5 test and one restored no-break space in a path-oracle row)
test_slot.py -- python -m pytest test_report_lint.py test_brief_lint.py test_cli_worker.py test_cli_worker_codex.py -q
956 passed, 1 skipped in 125.95s (0:02:05)
# the whole tool suite, once, serial, AGENT_WORKFLOW_STATE_DIR unset (full-suite.log)
test_slot.py -- python -X faulthandler -B -m pytest tools/agent-workflow/tests -q -p no:cacheprovider --basetemp=.tmp/T-021/p2r/pytest
FAILED tools/agent-workflow/tests/test_setup_project.py::test_the_real_checkout_has_no_failures
FAILED tools/agent-workflow/tests/test_setup_project.py::test_apply_binds_a_copy_and_is_idempotent
2 failed, 6213 passed, 12 skipped in 1014.83s (0:16:54)
  both: "links: 36 dead relative link(s); first: tasks/active/T-010-k2-k3.md -> ../../.tmp/T-010/p1-targeted-final.log"
  environmental: T-010 is unchanged in this worktree (git status empty, HEAD aed0cd2); its links point into the
  untracked .tmp/T-010/, present in the main checkout (Test-Path True) and absent here (False); no link in a changed file

# standalone lint on the real task files (17 in tasks/active; real-files.log, real-files-old.log)
this tree: exit 1, 48 lines in 15 files; the reviewed P2 CLI: the same 48 file:line:rule triples; only the
report-section text differs ("a top-level level-2 heading", T-010:594, :796, :998: headings of level 3)

# static checks
python -B -m ruff --version  ->  No module named ruff (not in the project .venv; no installs allowed)
git diff --check -- tools/agent-workflow docs pyproject.toml tasks/active/T-021-brief-order-scope-guard.md  ->  exit 0
```

- **Old tests: the real three-way split** (`.tmp/T-021/p2r/oldtests/split.log`, an AST diff of every top-level `test_` function and of the module-level case tables; the old file has 92 `def test_` functions, not the 86 the brief counted):
  - **Unchanged expectation, 69:** 66 functions with identical source and identical case data, `test_a_lazy_continuation_line_joins_the_paragraph_it_continues` (one comment reworded), and the two path-oracle tests (the oracle moved from `ORACLES["path"]` to `PATH_ORACLE`, same regex).
  - **New expectation, 15**, each named with its reason in the test's docstring or row comment:
    - `test_a_fence_in_a_container_is_not_read_and_the_finding_says_so` → `test_a_fence_in_the_labels_list_item_or_a_quote_is_the_done_check_output`, and `test_property_a_fence_under_a_container_is_never_taken_for_the_done_check` → `test_property_a_fence_in_a_container_counts_and_one_in_html_or_code_does_not`: the parser reads a fence in a list item or quote, so it counts (PM Q2); in an HTML block or indented code it still does not.
    - `test_the_finding_for_a_hidden_label_names_its_line` → `test_a_hidden_label_gets_the_missing_label_finding_on_the_report_heading`, and `test_the_finding_names_the_construct_that_hides_the_label` → `test_a_construct_that_hides_the_label_gives_the_missing_label_finding`: the hint came from the old line scanner's label-like candidates, which the parser does not produce; the finding is the plain missing-label one. `> q\n{b}` (a lazy line of a quote) and `[x][a`b]\n{b}` (no definition: literal text) now show the label.
    - `test_property_noninterrupting_ordered_marker_does_not_close_item_paragraph` → `test_property_an_ordered_marker_after_an_item_paragraph_starts_a_new_list`, `test_property_a_marker_after_an_item_paragraph_splits_it_or_fails_closed` → `test_property_a_marker_after_an_item_paragraph_ends_it`, `test_an_uncertain_split_fails_closed_by_name` → `test_an_uncertain_split_of_the_old_lint_is_read_by_the_parser`, and the three R3F2 rows of `test_the_retest_3_regressions`: a marker at the outer item's column starts a new list whatever its number (`rules_block/list.py:130-135` sets `isTerminatingParagraph` only at `blkIndent`, so the start-at-1 rule of `:146` does not apply); the old lint refused these. The 20-level list row moved to the new depth test.
    - `test_property_a_title_shown_only_through_markup_is_named` → `test_property_a_title_through_markup_is_read_as_the_parser_shows_it`, `test_the_retest_3_heading_cases` (the `[Result and evidence][r]` row) and `test_property_shown_report_heading_is_linted_or_explicitly_refused` → `test_property_shown_report_heading_is_linted`: the parser resolves emphasis, references and images, so the title is read, not refused; `~~Completion report~~` left the list (strikethrough is off in CommonMark: open question below).
    - `test_a_link_inside_a_link_and_a_reference_are_read_both_ways` (3 rows), `test_property_every_construct_hides_or_shows_the_label_as_commonmark_does` (the block quote and reference link rows), `test_a_pointer_counts_only_where_a_link_or_image_shows_it` (2 rows): without a definition a reference is literal text, and a label in a quote counts; a row with a definition was added for each, and `test_a_defined_reference_label_shows_no_pointer`.
    - `test_a_heading_is_read_by_the_title_it_shows`: its `rl._shown` assertion is dropped with that reader.
  - **Dropped, 8**, each testing a reader that no longer exists: `test_the_scanner_lists_top_level_list_items_with_their_content_column` and `test_the_scanner_records_paragraph_extents` (the P2 additions to `brief_lint._scan`, not ported), `test_the_inline_scan_splits_text_code_and_html` (`_inline`), `test_property_the_html_reader_reads_the_round_3_grammar` and `test_the_html_reader_reads_the_round_3_grammar_on_edge_tags` (`_html_end`, `_Index`), `test_property_the_autolink_reader_reads_the_round_3_grammar` (`_autolink_end`), `test_property_the_label_and_title_readers_respect_escape_parity` (`_label_end`, `_title_end`), `test_property_the_atx_closing_sequence_is_read_as_round_3` (`_atx_text`).
- **Important changes and reasons:** the parser decides every construct, so the old named refusals became readings (PM Q2), and only what the parser cannot read stays a `report-section` finding naming its cause: a container at `maxNesting`, a link nested to `maxNesting`, a recursion limit, a block over `INLINE_MAX`. A heading inside a list item or quote that shows the section title is a finding, never a skip. The version check runs at import, so `cli_worker` still loads the module and records the refusal.
- **Problems, risks and remaining work:**
  - **Worst-case constant:** linear, but an adversarial 2 MB file of unclosed `<?` blocks, each just under the cap, takes 48 s in the synchronous turn-end call (`sweep.log`). Real task files take milliseconds. Each block costs about its length times the cap, so `INLINE_MAX = 4096` would roughly halve that time (reasoned, not measured). PM choice; 8192 stays as approved.
  - **Strikethrough:** `~~Completion report~~` is not read as a report (Noticed, not done).
  - **Verdicts:** the CommonMark verdicts rest on the parser's source and output, not on the spec, which was not opened (no network).
  - **Not run:** ruff, Linux and CI (below).

### Research evidence (rebuild)

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| Which tokens and fields the lint reads | `.venv/Lib/site-packages/markdown_it/token.py:23-62`, 4.2.0 | raw | `type`, `nesting`, `map`, `level`, `children`, `content`, `attrs`, `info` (full table: Step 0 of this phase) | source-verified |
| Where depth drops content | `markdown_it/parser_block.py:75-79`, `markdown_it/parser_inline.py:146-168` | raw | at `maxNesting` blocks are skipped and links misread; detected by token level and `_depth_guard` | source-verified |
| Why a non-1 marker at the outer column starts a list | `markdown_it/rules_block/list.py:130-135`, `markdown_it/rules_block/list.py:146` | raw | `isTerminatingParagraph` needs `sCount >= blkIndent` | source-verified |
| Why the row width is read from the source line | `markdown_it/rules_block/table.py:220-233` | raw | body rows are padded or cut to the header width | source-verified |
| Why the inline cap exists | `.tmp/T-021/p2r/parser-scaling.log`, `.tmp/T-021/p2r/parser-scaling-capped.log` | raw output | full parse superlinear (`[`: 0.94, 2.05, 6.1 s at 100k, 200k, 400k, then the watchdog); capped, linear (at 16384: chunked `[` 2.24, 4.65, 10.0 s at 250k, 500k, 1M, watchdog at 2M) | experiment |
| Is the cap above real use | `.tmp/T-021/p2r/inline-lengths.log` | raw output | longest block 7392 (`USER_STANDING_RULES.md`), longest in a task file 2184 | experiment |
| Do the depth limits behave as read | `.tmp/T-021/p2r/nesting-probe.log` | raw output | 20 quotes or 18 lists drop a label (highest level 19); 21 brackets turn a link into text | experiment |
| How the old tests split | `.tmp/T-021/p2r/oldtests/split.log` | raw output | 69 unchanged, 15 new expectation, 8 dropped of 92 | experiment |
| Does every retest case get the expected reading | `.tmp/T-021/p2r/cases.log`, `.tmp/T-021/p2r/contracts.log` | raw output | 190 of 223 equal to the reviewed build; 33 differ, each in a class above; reviewer contract tests 15 passed, 3 failed by the changed contract | experiment |
| Is the time linear to 2 MB | `.tmp/T-021/p2r/sweep.log` | raw output | 256 runs to 2 MB, no watchdog, largest time ratio per doubling 2.14, worst 48.4 s (`instructions_unclosed` chunked) | experiment |
| CommonMark: no lazy continuation for fenced code; strikethrough is not CommonMark | https://spec.commonmark.org/0.31.2/ (not opened: no network) | not read, from memory | used only to give verdicts, each also matching the parser's output | assumption |

### Not run (rebuild)

- `ruff check`, `ruff format --check`: not_run, `No module named ruff` in the project `.venv` (no installs allowed).
- Linux and CI runs: not_run (Windows dev machine only; CI belongs to the PM's window, R-025).
- Live Claude or Codex worker turns: not_run (no live worker allowed); the turn-end record is covered by the labelled fake-CLI tests in `test_report_lint.py` and `test_cli_worker_codex.py`.
- The reviewers' generator scripts that compare against old snapshot trees (`p2fix3/before/tree`, `prior_probes.py` for P1): not rerun as scripts, because `brief_lint.py` is untouched and those trees exist only in the old worktree. Every case file they wrote was run (`cases.log`).

### Noticed, not done (rebuild)

- `## ~~Completion report~~` is no report heading under the CommonMark preset (strikethrough off, the tildes are text), where the old lint read it as one; enabling the GFM `strikethrough` rule would read it as the title. PM question, not changed silently.
- The reviewers' scripts in `<project>/.tmp/worktrees/T-021-p2/.tmp/T-021/` still assert the old refusal messages (`test_composition.py:22`, the retest 3 `test_new_contract.py` marker property); they were run as they are, not edited.
- `test_setup_project.py`'s link check fails in any worktree for `tasks/active/T-010-k2-k3.md`, whose 31 Markdown links point into the untracked `.tmp/T-010/` (36 dead links counted by the check). It passes only in the main checkout. Either the links or the check needs a PM decision.

### Changed files (rebuild)

- New: `tools/agent-workflow/report_lint.py`, `tools/agent-workflow/tests/test_report_lint.py`
- Edited: `tools/agent-workflow/cli_worker.py` (the turn-end record), `tools/agent-workflow/tests/test_cli_worker_codex.py` (one test), `tools/agent-workflow/README.md` (row and "Report gate"), `pyproject.toml` (`test` extra), `docs/dependencies-and-environments.md` (one row), this task file
- Not edited: `tools/agent-workflow/brief_lint.py`
- Scratch kept (small cited logs only): `.tmp/T-021/p2r/*.log` and `.tmp/T-021/p2r/oldtests/{run2,rows,split}.log`; scripts, case copies, the sweep inputs and the pytest basetemp are removed

### Phase P2 fix round 4 (deterministic linear readers, fail closed on uncertain splits)

- **Work completed:** the three audits ("Phase P2 fix round 4" above); in `report_lint.py` every inline reader rewritten as left-to-right code (`_escaped_end`, `_title_end`, `_label_end`, `_html_end`, `_autolink_end`, `_path_lines`, `_atx_text`, the `_Index` of closers, the `_Brackets` stack) and per-scan `_Facts`; uncertain block splits refused by name (`_unsure_marker`, `_LAZY_HTML`, `_padded_one`, `_NESTING_MAX`); headings read twice (`text`, `markup`), the section title discovered by either, the label and evidence titles by `text` with a named finding for markup-only ones; 114 new test cases in `test_report_lint.py` (the retest's three properties copied, its confirm cases, oracle differentials against the round 3 grammar, interruption and heading properties, the running-time properties on `lint_report` and the CLI); README row and module docstring. `brief_lint.py` not edited. R3F1, R3F2, R3F3 closed; one confirm case answered by a named refusal instead of exit 0 (below).
- **Result and evidence:**

```text
# the retest 3 confirm cases (.tmp/T-021/p2fix4/confirm4.py <worktree>: every file of .tmp/T-021/retest3-p2/confirm-cases,
# the real CLI of the round 3 snapshot (before/tree) and of this tree, each under a 5-second faulthandler watchdog)
confirm4.log: summary: {'round3': '6 of 23 as expected', 'round4': '21 of 23 as expected'}
  round 3 wrong: title_escaped_double/single (exit 0), non1_ordered_lazy_span/html, empty_marker_lazy_span,
  strong_report_heading, reference_report_heading (exit 0), unfinished_title (watchdog), reference_result_heading (generic)
  round 4 "wrong": reference_result_heading_lf/bom_crlf only: exit 1 with
  "[done-check-output] the heading '[Result and evidence][]' may show `Result and evidence` through emphasis,
  strikethrough, an image or a link reference, which this lint does not resolve" (the review accepts a named limitation)

# the retest's three properties (.tmp/T-021/p2fix4/test_new_contract_tree.py, RL_TREE selects the tree): new-contract.log
round 3 tree: 3 failed in 1.29s        this tree: 3 passed in 0.67s

# test_report_lint.py of this tree on the round 3 tree (before-tests.log; deselected: the adversarial and unfinished_title
# cases, which hang there: see adversarial-round3.log)
test_slot.py -- python -X faulthandler -B -m pytest -q -p no:cacheprovider -k "not adversarial and not unfinished_title" <before>/tests/test_report_lint_round4.py
27 failed, 206 passed, 34 deselected in 11.27s
# and on this tree (report-lint-tests.log)
test_slot.py -- python -X faulthandler -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_report_lint.py
311 passed in 10.62s

# running time, one child interpreter per family, faulthandler watchdog (adversarial.py <tree> <size> <seconds>)
adversarial-round3.log (4000 chars): title_backslashes_double/single: watchdog at 5 s; item/paragraph_label_lines 0.42 s
adversarial-round4.log (4000 chars): 37 families, max 0.0612 s (item_label_lines)
adversarial-round4-40k.log (40000 chars): 42 families, every one under 0.1 s

# the round 3 matrix (.tmp/T-021/retest3-p2/fix3_regressions.py .): matrix110.log
110 cases, 0 not as CommonMark reads them
# the retest 3 probes, regressions and P1 differential, rerun with output moved (rt3_rerun.py): rt3-rerun.log
probes: exit 0, 21 cases, changed vs retest 3: {}
regressions: exit 0, 14 cases, changed vs retest 3: {}
prior_probes: exit 0, 19 cases, changed vs retest 3: {} ['P1 differential: 4368 old scanner results equal (and the 9 round 2 fields); 8736 legacy/new-order decisions equal to HEAD']

# mutants on the changed lines (.tmp/T-021/p2fix4/mutants.py, scratch copies; mutants.log): 11 of 11 killed
M1 escape consumes one character, M2 parenthesized title may hold `(`, M3 no same-list exemption, M4 a marker is never
unsure, M5 no lazy HTML refusal, M6 section title by exact text only, M7 no named markup titles, M8 a maybe-link
reactivates nothing, M9 an attribute needs no whitespace, M10 image descriptions not marked, M11 a path starts anywhere
(M8, M9, M11 survived the first run; the rows that kill them were added: a reactivation link row, 27 edge tags, 16 edge cells)

# standalone lint (real-files.log): this tree and the round 3 tree print the same 8 lines, exit 1 both
T-020-brief-lint-test-sets.md       :48, :79 [done-check-output] no label; [evidence-table] no heading
T-024-tooling-no-agent-commits.md   :141 [evidence-table] no heading; :144 [done-check-output] no fence after the label
T-026-quota-tooling.md              :157 [done-check-output] no fence; :190 [evidence-table] no table under the heading

# full tooling suite, once, serial, AGENT_WORKFLOW_STATE_DIR unset (full-suite.log)
test_slot.py -- python -X faulthandler -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests
6200 passed, 12 skipped in 918.14s (0:15:18)

# static checks
python -B -m ruff --version   ->  <project>\.venv\Scripts\python.exe: No module named ruff
git diff --check -- tools/agent-workflow/ tasks/active/T-021-brief-order-scope-guard.md   ->  exit 0 (LF-to-CRLF warnings only)
```

- **Real files explained:** the findings of T-020, T-024 and T-026 are the historical formatting gaps of rounds 1-3 (no template label or evidence heading, output in bullets or prose, prose instead of a table); none of them uses a construct this round changed, so both trees give the same lines. Reconciling them with the template is PM-owned.
- **Important changes and reasons:** S31 asked for code, not grammars: no reader keeps a regular expression whose alternatives overlap, and every closer search is a bisect, so time is linear and measured at ten times the brief's size. Where the block reading is uncertain the lint refuses by name and never picks; the scanner is untouched, so P1 equals HEAD. Discovery reads markup generously (more reports linted, never one skipped); labels read it strictly (named when only markup shows them).
- **Problems, risks and remaining work:** residuals, each a named false finding, never a false pass: (a) a `Result and evidence` or `Research evidence` heading through a link reference, even with its definition (`reference_result_heading`, reviewer expected 0); (b) an empty item right after an item paragraph, also of the same list (believed a sibling in CommonMark; not verified offline); (c) a `01.` marker inside a paragraph, which the P1 scanner continues and CommonMark interrupts; (d) a lone HTML tag right after an item paragraph; (e) lists nested more than 16 deep. CommonMark and cmark semantics remain from memory (assumption rows). `ruff check` and `ruff format --check`: not_run, no ruff in the `.venv`. Linux and CI: not_run.

### Research evidence (fix round 4)

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| What the retest reproduced | `.tmp/T-021/retest3-p2/review.md:24-97` | raw | R3F1 overlapping title alternatives; R3F2 sibling markers cut paragraphs; R3F3 markup headings skipped | source-verified |
| Where the scanner records siblings and knows interruption | `tools/agent-workflow/brief_lint.py:205-217`, `tools/agent-workflow/brief_lint.py:298-302` | raw | `_list_item` returns whether a marker may interrupt; the sibling branch records every marker | source-verified |
| Do the confirm cases and properties flip? | `.tmp/T-021/p2fix4/confirm4.log`, `.tmp/T-021/p2fix4/new-contract.log` | raw output | round 3: 6 of 23 and 3 failed; this tree: 21 of 23 (2 named refusals) and 3 passed | experiment |
| Is the running time bounded? | `.tmp/T-021/p2fix4/adversarial-round3.log`, `.tmp/T-021/p2fix4/adversarial-round4-40k.log` | raw output | round 3: watchdog at 4000 characters; this tree: under 0.1 s at 40000 | experiment |
| Do the new readers read the round 3 grammar? | `tools/agent-workflow/tests/test_report_lint.py:1136` and the tests after it | raw | the round 3 regexes as test-only oracles agree on random and edge inputs (the title oracle without the overlap) | experiment |
| Are P1 and the round 3 matrix unchanged? | `.tmp/T-021/p2fix4/rt3-rerun.log`, `.tmp/T-021/p2fix4/matrix110.log` | raw output | 4368 scans and 8736 decisions equal to HEAD; 110 of 110 | experiment |
| Python `re` classes the path reader matches (`\s`, `\d`, `\w`) | `test_report_lint.py::test_the_path_reader_finds_the_round_3_pointers_on_edge_cells` | raw output | `isspace`, `isdecimal`, `isalnum` or `_` give the regex's paths | experiment |
| CommonMark 0.31.2: escapes (2.4), interruption by list items (5.2), HTML kind 7 (4.6), titles (6.3), autolinks (6.5) | https://spec.commonmark.org/0.31.2/ (not opened: no network) | not read, from memory | as encoded in the readers and the refusals | assumption |

### Not run (P2 fix round 4)

- `ruff check` / `ruff format --check`: `No module named ruff` in the project `.venv` (no installs allowed).
- Linux and CI runs; a CommonMark renderer comparison (none installed; no network).

### Noticed, not done (P2 fix round 4)

- `_item_content` counts a block quote line right after an item as item content (round 3 behaviour, kept); every path through it still fails closed (`_QUOTE`, `_LAZY_QUOTE`), so no change was needed in this round.

### Phase P2 fix round 3 (every construct read exactly or failed closed)

- **Work completed:** the class audit of every CommonMark 0.31.2 block and inline construct ("Phase P2 fix round 3" above); `brief_lint._Scan.paragraphs` (additive); in `report_lint.py` one inline tokenizer (`_inline`, `_close_bracket`, `_inline_link`, `_destination`: escapes, code spans, autolinks, raw HTML whose tags run over lines, inline links, images, references), paragraph location with lazy continuation (`_locate`, `_paragraph`, `_tail`, `_continued`), fail-closed reasons named in the findings (`_hidden`, `_HIDERS`, `_pointer_hider`), heading titles by the text they show (`_shown`, `_resolve`); README row; 55 new test cases in `test_report_lint.py` and one message assertion updated (the finding now names the construct). R2F1 and R2F2 closed.
- **Result and evidence:**

```text
# CLI regressions (.tmp/T-021/p2fix3/regressions.py <tree>): the 7 retest 2 cases plus a hidden and a shown case for
# 24 constructs of the audit, each LF and BOM+CRLF = 110 CLI runs; before = snapshot of the round 2 tree (before/tree)
regressions-before.log: 110 cases, 32 not as CommonMark reads them
  wrong before (each in both encodings): the 7 retest 2 cases; hidden: link_reference_definition, lazy_after_label,
  link (title), image, reference_link, open_tag (false passes); shown: thematic_break, indented_code, autolink (false findings)
regressions-after.log: 110 cases, 0 not as CommonMark reads them

# test_report_lint.py of this tree run against the round 2 tree (before-tests.log) and against this tree
test_slot.py -- python -X faulthandler -B -m pytest -q -p no:cacheprovider .tmp/T-021/p2fix3/before/tree/tools/agent-workflow/tests/test_report_lint.py
38 failed, 153 passed in 10.15s
test_slot.py -- python -X faulthandler -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_report_lint.py
197 passed in 9.13s

# P1 differential (.tmp/T-021/p2fix3/prior_probes.py: the retest's generator, plus the round 2 scanner's 9 fields)
P1 differential: 4368 old scanner results equal (and the 9 round 2 fields); 8736 legacy/new-order decisions equal to HEAD
# the 19 earlier CLI fixtures: good, escaped_pipe, crlf_bom, setext_section exit 0; every other exit 1 (as round 2)
# RT1..RT4 probes (.tmp/T-021/p2fix3/rt_probes.py): every exit as round 2 except quote_fenced_lazy_label 0 -> 1
#   (accepted false finding, see Decisions: a paragraph right after a block quote fails closed)

# mutants on the changed lines (.tmp/T-021/p2fix3/mutants.py, scratch copies, final code): 11 of 11 killed
M1 tag whitespace without a line ending, M2 indented continuation not in the paragraph, M3 no definition check,
M4 link title shown, M5 no deactivation of outer links, M6 no unsure after a reference, M7 no lazy line after an item,
M8 no lazy tail before a paragraph, M9 no autolinks, M10 heading titles from source, M11 a link destination is no pointer

# standalone lint (python -B -I tools/agent-workflow/report_lint.py tasks/active/<file>): the same findings as rounds 1 and 2
T-020-brief-lint-test-sets.md        exit 1  :48, :79 [done-check-output] and [evidence-table]
T-024-tooling-no-agent-commits.md    exit 1  :141 [evidence-table], :144 [done-check-output] (no fence after the label)
T-026-quota-tooling.md               exit 1  :157 [done-check-output] (no fence), :190 [evidence-table] (no table)

# full tooling suite, once, serial, AGENT_WORKFLOW_STATE_DIR unset (.tmp/T-021/p2fix3/full-suite.log)
test_slot.py -- python -X faulthandler -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests
6086 passed, 12 skipped in 1018.77s (0:16:58)

# this task file's own lint: the round 3 report is clean; the three P1 reports (:672, :722, :765) have no evidence table (as before)
```

- **Real files explained:** T-020's two reports have no bold `Result and evidence` label and no `Research evidence` heading; T-024's label is a bullet whose output is in sub-bullets, not a fence, and it has no evidence heading; T-026's label has prose, not a fence, and its evidence heading has prose, not a table. None of them uses a construct this round changed, so the findings are the same; reconciling those reports with the template is PM-owned (noticed in rounds 1 and 2).
- **Important changes and reasons:** the class is closed by enumeration: each construct is either read as CommonMark reads it or fails closed with its name in the finding. Paragraphs come from the scanner itself, so the report lint cannot drift from the block reading again; the inline tag grammar is the report lint's own, so P1's block starts are untouched (differential).
- **Problems, risks and remaining work:** (a) accepted false findings, each named in its message: a label right after a block quote whose last block is no paragraph, a shortcut image without a definition, a label after an unsure reference, a kind-7 HTML line or list marker right after an item where CommonMark would continue lazily. (b) `_DESTINATION_DEPTH` (32) and the "longest title" reading follow cmark as remembered, not re-read (assumption, below). (c) `ruff check` and `ruff format --check`: not_run, `No module named ruff` in the project `.venv` (no installs allowed). (d) Linux, CI: not_run (Windows CPU only).

### Research evidence (fix round 3)

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| What the retest reproduced | `.tmp/T-021/retest2-p2/review.md:20-69` | raw | R2F1: line-bound tag whitespace; R2F2: the walk misses thematic breaks | source-verified |
| Where the line-bound grammar and the paragraph state live | `tools/agent-workflow/brief_lint.py:154-156`, `tools/agent-workflow/brief_lint.py:329-335` | raw | `[ \t]` only in the tag grammar; the scanner resets `paragraph` at a break | source-verified |
| Do the reproducers and the construct cases flip? | `.tmp/T-021/p2fix3/regressions-before.log`, `.tmp/T-021/p2fix3/regressions-after.log` | raw output | 32 wrong before, 0 after, of 110 CLI runs | experiment |
| Are P1 decisions and the round 2 scanner fields unchanged? | `.tmp/T-021/p2fix3/prior-probes.log:20` | raw output | 4368 scans and 8736 decisions equal | experiment |
| How the stdlib resolves references | `<python 3.12.10>/Lib/html/__init__.py:91-132` | raw | `unescape` also takes the longest HTML5 prefix of a name; so names are looked up exactly in `html.entities.html5` | source-verified |
| Does any real report hold a label in a block quote? | `grep` of `^\s*>.*\*\*Result and evidence` over `tasks/` and `.agents/` | raw output | no match | experiment |
| CommonMark 0.31.2 grammar: raw HTML whitespace (6.6), links and titles (6.3), autolinks (6.5), references (4.7), lazy continuation (5.1, 5.2) | https://spec.commonmark.org/0.31.2/ (not opened: no network) | not read, from memory | as encoded in the constants and the class audit | assumption |
| cmark: destination parenthesis bound 32, link-title longest match, no lazy line after an item's fence | cmark `inlines.c`, `blocks.c` (not available offline) | not read, from memory | as encoded in `_destination`, `_TITLE`, `_tail` | assumption |

### Not run (P2 fix round 3)

- `ruff check` / `ruff format --check`: `No module named ruff` (exact output in the shell, no install allowed).
- Linux and CI runs; a second Markdown renderer (none installed; no network).

### Noticed, not done (P2 fix round 3)

- Block quote content could be scanned like a list item's to remove the quote false findings; not needed for any real report (no label in a quote).

### Phase P2 fix round 2 (one structural visibility step)

- **Work completed:** RT1 (a label nested in a list item's fence, indented code or HTML), RT2 (a pointer or code span only inside an inline comment or tag), RT4 (a code-span `<!--` hiding a real label) and RT3 (`Makefile:3`: kept refused, diagnostic recommends `./Makefile:3`) in `tools/agent-workflow/report_lint.py` (`_inline`, `_paragraph_visible`, `_item_content`, `_visible`, `_has_pointer`, `_has_code_span`; `_in_open_comment` and `_BULLET` removed); `brief_lint._Scan.items` (additive field); README row; 19 new test cases in `test_report_lint.py` and a fix of its flaky table generator (a blank question cell); the class audit, decisions and configuration audit above.
- **Result and evidence:**

```text
# Codex RT1..RT4 probes (.tmp/T-021/retest-p2/probes.py, output folder moved: .tmp/T-021/p2fix2/probes.py); before = tree of round 1, after = this tree
# before-probes.log -> after-probes.log (exit code of report_lint.py per case)
list_fenced_label 0 -> 1 | list_indented_label 0 -> 1 | list_raw_html_label 0 -> 1 | bom_crlf_list_fenced_label 0 -> 1   (RT1)
comment_pointer 0 -> 1 | comment_code_pointer 0 -> 1 | html_attribute_pointer 0 -> 1                                   (RT2)
makefile_pointer 1 -> 1 (the finding now says: write `./Makefile:3`) | makefile_qualified 0 -> 0 | makefile_span 0 -> 0   (RT3, decision)
inline_comment_false_finding 1 -> 0                                                                                  (RT4)
quote_fenced_lazy_label 0 -> 0, list_html_label 1 -> 1, deep_reports, two_reports_one_heading, empty_fence, unknown_class, second_bad_table: unchanged

# new tests against the round 1 code (scratch copy .tmp/T-021/p2fix2/before, labelled: round 1 functions swapped back) and against this tree
test_slot.py -- python -m pytest -q .tmp/T-021/p2fix2/before/tools/agent-workflow/tests/test_report_lint.py
17 failed, 125 passed in 9.97s     (RT1 property, RT4 visible property, hidden-label line hint, 11 inline-scan cases, RT2 property, comment-with-pointer, RT3 hint)
test_slot.py -- python -m pytest -q tools/agent-workflow/tests/test_report_lint.py
142 passed in 7.67s

# P1 differential (.tmp/T-021/p2fix2/prior_probes.py, the retest's generator): the six HEAD fields of _scan and both order modes
P1 differential: 4368 old scanner results equal; 8736 legacy/new-order decisions equal to HEAD
# the 19 earlier CLI fixtures: good, escaped_pipe, crlf_bom, setext_section exit 0; every bad one exit 1, as in round 1

# mutants on the changed lines (.tmp/T-021/p2fix2/mutants.log, mutants-m15.log; scratch copies): 16 mutants, 15 killed, 1 survived
M1 no backslash escape, M2 closer of any length, M3 tags shown, M4 paragraph stops at the label, M5 paragraph starts at the label,
M6 nested block not read, M7 content indentation kept, M8 first-line padding dropped, M9 URL from raw cell, M10 path from raw cell,
M11 no visibility in the label check, M12 hint always off, M13 comment end changed, M14 sibling item not recorded, M15 blank span counts: all killed
M16 (drop the `not scan.inert[start]` guard of the walk back to the item opener): SURVIVED, equivalent: for every reachable line the sub-scan returns the same answer without it

# standalone lint, same findings as round 1 (real-files-now.log, real-files-before.log identical)
tasks/active/T-020-brief-lint-test-sets.md   exit 1   :48 and :79 [done-check-output] and [evidence-table]: no label and no Research evidence heading in either report
tasks/active/T-024-tooling-no-agent-commits.md   exit 1   :141 [evidence-table] no Research evidence heading; :144 [done-check-output] label is a bullet with sub-bullets, no fence
tasks/active/T-026-quota-tooling.md   exit 1   :157 [done-check-output] no fence; :190 [evidence-table] heading with prose, no table

# full tooling suite, once, serial, AGENT_WORKFLOW_STATE_DIR unset (.tmp/T-021/p2fix2/full-suite.log)
6031 passed, 12 skipped in 956.12s (0:15:56)
```

- **Important changes and reasons:** one visibility step instead of five partial rules: a left-to-right inline scan (`_inline`) decides what a cell or paragraph shows, and a list item is scanned as its own document with the scanner that already reads the top level, so nested code, HTML and comments hide a label for the same reasons they do at the top level. The scanner gained only `items`; P1 decisions are unchanged (differential). RT3 is decided in the Decisions above: the lint reads text, never the checkout.
- **Problems, risks and remaining work:** (a) a block quote is never read for labels (it cannot start a label line); a quote that holds a fence still gets the existing "not read" finding. (b) A label that continues a list item by lazy continuation is treated as a new top-level paragraph, as the scanner does; the paragraph run still includes the item line that opened it, so a real comment opened there hides the label (conservative). (c) A comment or code span that spans lines is read only within one paragraph (back to a blank, code, heading or HTML line; forward to the next blank, code, heading or container line). (d) `_has_content` still counts hidden text (an inline comment) as content: that only makes a report "written" and linted, never a silent pass. (e) `ruff check` and `ruff format --check` are not_run (not installed).

### Research evidence (fix round 2)

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| What the retest reproduced and its minimal fixes | `.tmp/T-021/retest-p2/review.md:26-61` | raw | RT1 to RT4 name each site, probe and a minimal fix; RT1 is "expose container-relative block kinds or refuse" | source-verified |
| What the scanner marks inside a list item | `tools/agent-workflow/brief_lint.py:284-302` | raw | item lines are `inert`; only a heading, underline or fence look-alike is reported; nothing nested is parsed | source-verified |
| Do the probes now fail or pass as expected? | `.tmp/T-021/p2fix2/before-probes.log`, `after-probes.log` | raw output | RT1 and RT2 cases exit 0 before and 1 after; RT4 exit 1 before and 0 after; RT3 kept refused with a `./` hint | experiment |
| Are P1 decisions unchanged? | `.tmp/T-021/p2fix2/prior_probes.py` output | raw output | 4368 scanner results (six HEAD fields) and 8736 decisions equal HEAD | experiment |
| Do real task files hold an indented label? | `grep` over `tasks/` and `.agents/` for an indented `Result and evidence` label | raw output | no hit; nine files hold it at column 0 | experiment |
| CommonMark rules for escapes (2.4), code spans (6.1), raw HTML (6.6), autolinks (6.5) and left-to-right precedence | https://spec.commonmark.org/0.31.2/ (not opened: no network) | not read, from memory and the scanner's own comments | a code span, an autolink and raw HTML bind tighter than emphasis; an unterminated `<!--` is text; `<!-->` and `<!--->` are comments | assumption |

### Phase P2 fix round 1 (structural report discovery and block visibility)

- **Work completed:** C-F1/G-F1 (suffixed and nested reports), C-F2/G-F2 (prose-only reports), C-F3 (indented table), C-F4 (hidden label), G-F3 (`Note:1` pointers), C-F5 (escaped backticks), G-F4 (every evidence table) in `tools/agent-workflow/report_lint.py`; `brief_lint._Scan.html` (additive field); `cli_worker.py` record field `unwritten` and a `NOTE:` line; README rows; tests (`test_report_lint.py` 123 tests, one Codex registered-task lifecycle test in `test_cli_worker_codex.py`); the class audit and configuration audit above.
- **Result and evidence:**

```text
# regressions on the PRE-FIX tree (scratch copy of the P2 builder's tree + a LABELLED SHIM mapping lint_report/load_template onto the old lint_task_file; turn-end tests deselected)
test_slot.py -- python -m pytest -q .tmp/T-021/p2fix/before/tools/agent-workflow/tests/test_report_lint.py -k "not cli and not turn ..."
48 failed, 63 passed, 11 deselected in 3.01s      (every C-F1..C-F5 and G-F1..G-F4 case is among the 48: .tmp/T-021/p2fix/before-run.log)

# the same tests on this tree
test_slot.py -- python -m pytest -q tools/agent-workflow/tests/test_report_lint.py "tools/agent-workflow/tests/test_cli_worker_codex.py::test_a_registered_task_file_is_linted_after_every_codex_turn"
124 passed in 12.62s      (123 in the report file, 1 Codex registered-task test; the report file alone: 123 passed in 9.02s)

# reviewer probes through the real CLI (.tmp/T-021/p2fix/probes.log; the reviewers' fixtures, P1 differential at the end)
good, escaped_pipe, crlf_bom, setext_section: exit 0, no output
no_output, empty_fence, unknown_class, missing_column, no_pointer, list_fence, quote_fence, html_fence: exit 1 (as before)
suffixed_section, prose_only_written, later_phase_without_checks, indented_table, commented_label, indented_label, escaped_code_pointer: exit 1 (were exit 0, no output)
P1 differential: 4368 old scanner results equal; 8736 legacy/new-order decisions equal to HEAD

# mutants (.tmp/T-021/p2fix/mutants.log and mutants-rerun.log, scratch copies): 14 mutants, 14 killed
M1 survived the first run (`(` dropped from the separators: every test title had a space before it); a `Completion report(phase 1)` case was added; rerun: killed
M2 odd title not ambiguous, M3 indented code at 5 columns, M4 label in an HTML block, M5 no backtick escape, M6 no extension form,
M7 first table only, M8 comment counts as content, M9 no label-opened nested report, M10 template linted, M11 every HTML block a comment,
M12 inline comment hides nothing, M13 record drops `unwritten`, M14 HTML block first line not an HTML line: all killed

# standalone lint on the same real task files (python -B -I tools/agent-workflow/report_lint.py tasks/active/<file>)
T-020-brief-lint-test-sets.md   exit 1
  :48 [done-check-output] no `Result and evidence` label ... ; :48 [evidence-table] no `Research evidence` heading ...
  :79 [done-check-output] ... ; :79 [evidence-table] ...      (:79 is `## Completion report: fix round 1`, skipped before)
T-024-tooling-no-agent-commits.md   exit 1 (was exit 0, no output: a prose-only report)
  :141 [evidence-table] no `Research evidence` heading with its table in a written report
  :144 [done-check-output] no non-empty fenced code block (the done-check output) between the `Result and evidence` label and the next heading
T-026-quota-tooling.md   exit 1 (was exit 0, skipped by its suffix `(phase 1)`)
  :157 [done-check-output] no non-empty fenced code block ... ; :190 [evidence-table] no table under the `Research evidence` heading

# full tooling suite, once (.tmp/T-021/p2fix/full-suite.log), serial, AGENT_WORKFLOW_STATE_DIR an empty scratch folder
6012 passed, 12 skipped in 1090.91s (0:18:10)      (no failure; an earlier run with AGENT_WORKFLOW_STATE_DIR set failed the waiver-log tests of test_brief_lint.py because they honour it, so it was stopped and rerun with it unset)
```

- **Important changes and reasons:** one scanner, not two: `html` (line of an HTML block to its first line) is the only addition to P1's file and lets the lint tell a comment or HTML block from a list item; P1 decisions are identical (differential). Discovery is structural: a title prefix plus separator, and nested reports by the template's own bold labels, so no word list. The three real files now give findings that are true: T-024 and T-026 never held an output fence or evidence table in the section the lint read. The untouched template is "unwritten" (exact text, read from the checkout); it was a warning on every turn before.
- **Problems, risks and remaining work:** (a) a nested report that has neither a title heading nor a `Work completed` or `Result and evidence` label is merged into the report above it; telling it from a template subsection would need a word list. (b) `Makefile:3`-style pointers (no separator, no extension) are refused; a code span passes. (c) A bare label that continues a paragraph after an inline `<!--` is hidden even when a code span holds that `<!--` (a loud finding, not a silent pass). (d) A template that cannot be read leaves every report with content linted. (e) `ruff` is not installed: not_run.

### Research evidence

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| What each review reproduced | `.tmp/T-021/review-p2/review.md:27-35` and `gemini-review.md:37-60` | raw | F1..F5 and G-F1..G-F4 name each site and a reproducing fixture | source-verified |
| What the scanner marks | `tools/agent-workflow/brief_lint.py:244-349` | raw | indented code is left unflagged (lines 304-306); HTML blocks are `inert`; no block kind existed | source-verified |
| Do the fixtures now fail as the reviews expect? | `.tmp/T-021/p2fix/probes.log` (the reviewers' `probes.py`, output folder moved) | raw output | seven former exit-0 cases exit 1; four good cases stay exit 0 | experiment |
| Are P1 decisions unchanged? | `.tmp/T-021/p2fix/probes.log` last line | raw output | 4368 scanner results (six HEAD fields) and 8736 decisions equal HEAD | experiment |
| Which headings do real files use for reports? | `tasks/active/T-026-quota-tooling.md:154`, `tasks/active/T-020-brief-lint-test-sets.md:79`, `tasks/active/T-022-ci.md:263` | raw | `(phase 1)`, `: fix round 1` and `### Round N` forms exist | source-verified |
| CommonMark rules for indented code (4.4), HTML blocks (4.6), code spans (6.1) and raw HTML (6.6) | https://spec.commonmark.org/0.31.2/ (not opened: no network) | not read, from memory and the scanner's own comments | indented code is 4+ columns; a type-2 block runs to `-->`; an odd backslash run escapes a backtick; an inline comment may span lines of one paragraph | assumption |

### Not run (P2 fix round 1)

- `ruff check` and `ruff format --check`: not_run (`No module named ruff`; installs not allowed).
- Linux: not_run (Windows dev machine; no OS-specific code added). Live Claude or Codex worker: not_run (the Codex turn test uses the labelled fake CLI of `test_cli_worker_codex.py`).
- A Markdown reference parser (markdown_it, commonmark): not available (reviewer's `markup.log`); the CommonMark rules above are an assumption read from the spec's rules as the scanner already encodes them.

### Noticed, not done (P2 fix round 1)

- The earlier phases of this task file (`### Phase P2 (report lint)`, `### Phase P1 fix round 2`, `### Phase P1 fix round 1`, `### Phase P1`) are now linted on their own and some lack an evidence table; not edited here (history).
- `.agents/policy/AGENT_TASK_TEMPLATE.md` could say that the Completion report is linted after every turn and that a nested phase report needs its own `Result and evidence` fence and evidence table (PM decision; same note as P2).

### Phase P2 (report lint)

- **Work completed:** `tools/agent-workflow/report_lint.py` (rules `done-check-output`, `evidence-table`, `evidence-pointer`; standalone CLI); `_Scan.fences` in `tools/agent-workflow/brief_lint.py`; the turn-end call in `tools/agent-workflow/cli_worker.py` (`report_lint_record`, `store_report_lint`, a `report_lint` row in `status --json`, called by `run_turn` and `run_codex_turn`); README rows (script table, Report gate); `tests/test_report_lint.py` (70 tests); this section.
- **Result and evidence:**

```text
# P2 tests, test_slot.py -- python -X faulthandler -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_report_lint.py
70 passed in 6.97s

# mutants (.tmp/T-021/p2/mutants.log, scratch copy): baseline 70 passed; 18 of 18 killed, survivors: none
M1 empty fence accepted, M2 missing column accepted, M2b missing cell accepted, M3 unknown class accepted,
M4 pointer-free source accepted, M5 fence searched past the next heading, M6 written section treated as unwritten,
M7 placeholder row counts as data, M8 lint crash not recorded, M9 record bound ignored, M10 label inside a fence counts,
M11 loader exit escapes the turn, M12 stale record kept, M13 URLs not pointers, M14 a time is a pointer,
M15 heading label ignored, M16 unclosed fence loses its last line, M17 warning never printed

# full tooling suite, once (.tmp/T-021/p2/full-suite.log), serial, AGENT_WORKFLOW_STATE_DIR unset
5959 passed, 11 skipped in 1027.18s (0:17:07)      (no failure: the two base-only setup_project failures of P1 are gone at ac95cfa)

# standalone lint on real task files (python -B -I tools/agent-workflow/report_lint.py tasks/active/<file>)
T-020-brief-lint-test-sets.md   exit 1
  tasks\active\T-020-brief-lint-test-sets.md:48: [done-check-output] no `Result and evidence` label (the template's bold bullet label or a heading) in a written report
  tasks\active\T-020-brief-lint-test-sets.md:48: [evidence-table] no `Research evidence` heading with its table in a written report
T-024-tooling-no-agent-commits.md   exit 0, no output
T-026-quota-tooling.md   exit 0, no output   (its sections are titled `Completion report (phase 1)`: skipped by the exact title)
T-021-brief-order-scope-guard.md before this section was written   exit 1
  tasks\active\T-021-brief-order-scope-guard.md:243: [evidence-table] no `Research evidence` heading with its table in a written report
T-021-brief-order-scope-guard.md with the P2 Completion report and its Research evidence table   exit 0, no output
```

- **Important changes and reasons:** one scanner, not two: the lint calls `brief_lint._scan` for fences, headings and containers, and the only change to P1's file is the additive `fences` field (a second fence reader is what the PM ruled out). The turn-end call runs after the turn's processes and Git audit have ended, outside the registry lock, and never changes the turn's status or exit code; a crash, a loader exit, a missing or undecodable file all land in `report_lint.error` and on stderr. A Setext heading is level 1 or 2 and ends the section, as CommonMark says.
- **Problems, risks and remaining work:** (a) exact titles: `## Completion report (phase 1)` and `: fix round 1` sections are skipped, so the fix-round reports of real files are not linted; prefix matching is a one-constant change but would require an evidence table in each such section (PM decision). (b) An untouched template copy counts as written and warns each turn (rule text is literal). (c) A fence under a list item is not seen; the finding names that cause. (d) The PM step "run `report_lint.py` before accepting" is a PM_POLICY change and is not made here (Q4). (e) The lint reads the file once per turn, so a report written in the last seconds is seen at the next turn. (f) P3 is not started.

### Research evidence

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| What can the lint reuse from P1's scanner? | `tools/agent-workflow/brief_lint.py:170-183` and `:243-349` at `ac95cfa` | raw | `_scan` returns `code`, `inert`, `headings` and `unsupported` but no fence extents; `fences` is added at lines 183, 252-253, 266, 312 and 348-349 | source-verified |
| Where is the registered task file and where does a turn end? | `tools/agent-workflow/cli_worker.py:310-329` (`validate_task_file`), `:2760` (`hooks.task_file`), `:2290` and `:2311` (Claude turn), `:2403` and `:2428` (Codex turn) | raw | the task file is an absolute POSIX path under `hooks.task_file`; each turn ends in one registry update under `RegistryLock`, after `close_git_audit` | source-verified |
| Which classes, headings and columns does the template define? | `.agents/policy/AGENT_TASK_TEMPLATE.md:59-72` | raw | the Completion report bullets, a `### Research evidence` table of 5 columns and the four classes; `test_the_class_vocabulary_is_the_templates_and_case_is_folded` pins the class line | source-verified |
| Does a Setext heading end the Completion report section? | `tools/agent-workflow/tests/test_report_lint.py:166-167` | experiment | `Research evidence` over `-----` scans as a level-2 heading, so it ends `## Completion report` (my first test expected otherwise and failed, then was corrected) | experiment |
| GFM table rules: the delimiter row has the header's cell count, `\|` is a literal pipe, excess cells are dropped | https://github.github.com/gfm/#tables-extension- (not opened: no network allowed) | not read, from memory | the parser requires equal header and delimiter cell counts and splits on unescaped pipes; a row with other than five cells is flagged rather than guessed | assumption |
| Do real task files behave as designed? | `python -B -I tools/agent-workflow/report_lint.py tasks/active/T-020-brief-lint-test-sets.md` (and T-024, T-026) | raw output above | T-020 has no `Result and evidence` label and no evidence heading (2 findings); T-024 is clean; T-026 is skipped by its suffixed titles | experiment |

### Not run (P2)

- `ruff check` and `ruff format --check` on the changed files: not_run (`python -m ruff` gives `No module named ruff`; no `ruff` on PATH; no installs allowed). The line lengths of the new code follow the surrounding files.
- Linux: not_run (Windows dev machine; no OS-specific code: paths come from `pathlib`, the CLI test passes `CREATE_NO_WINDOW` only through `getattr`).
- Live Claude or Codex worker turn with a real task file: not_run (no live worker allowed); the turn-end path is covered with a labelled test double of the worker process.

### Noticed, not done (P2)

- `.agents/policy/AGENT_TASK_TEMPLATE.md` does not say that the Completion report is linted after every turn; the PM may want one line there (the file is in the task's Scope, but the P2 brief names only `report_lint.py`, `cli_worker.py`, README and tests).
- `T-020` has an old-style report (bold labels without the template bullets); it was not edited.

### Phase P1 fix round 2

- **Work completed:** `tools/agent-workflow/brief_lint.py`: `_scan` tracks container and HTML extents and reports structure inside them; the legacy reader branch; `_BOM`; the `_blocks` docstring. `tools/agent-workflow/cli_worker.py`: `lint_gate` reads the marker first. Texts: template Brief order line, README Brief gate, module doc. Tests: `test_brief_lint.py`.
- **Result and evidence:**

```text
# retest cases (.tmp/T-021/fix2/probes.log, every file of .tmp/T-021/retest1/cases)
list_fence_hides_real_heading: brief-order 33, 35 (unsupported) + 37 (late heading)
list_fence_hides_second_task:  brief-order 33, 35 (unsupported) + 37 (second task)
html_only_task / html_div_only_task: brief-order 1 (no task) + 30 (unsupported)
html_pre_fake_close_hides_heading: brief-order 34 (unsupported) + 37 (late heading)
quote/list late heading, quote/list/html fenced or commented example: unsupported only
legacy_html_task_after_footer: []  HEAD: []   (the three other legacy cases equal HEAD too)
original_* and indented/unclosed/nested rows: unchanged

# new tests on the fix-1 lint (scratch tree): 18 failed, 9 passed (the 9 are the rows whose verdict is unchanged)
# differential (.tmp/T-021/fix2/differential.log)
generated: 3000 examples, legacy path equal to base
real briefs: 120, legacy path equal to base for all
new order, unsupported-structure finding: ["T-006-fable", "T-006-fix2", "T-006-rereview5"]
# main-tree briefs with a top-level `## Task` line (new order): none (.tmp/T-021/fix2/new-order-briefs.log)

# mutants (.tmp/T-021/fix2/mutants.log): 10 of 10 killed
M1 a container fence line toggles the global fence, M2 HTML blocks not recognized, M3 a fence delimiter inside is
not structure, M4 a lone tag interrupts a paragraph (survived once; killed by row tag-inside-a-paragraph),
M5 legacy strip runs the new inference, M6 legacy readers use the scanner, M7 marker ignored by is_brief_message,
M8 byte order mark kept, M9 footer span may start inside a container (survived once; killed by an HTML-wrapped
plain footer), M10 unsupported structure not reported

# focused: test_brief_lint.py test_cli_worker.py test_cli_worker_codex.py test_audit_extract.py
674 passed, 1 skipped in 141.43s (0:02:21)
# full tool suite (.tmp/T-021/fix2/full-suite.log), serial
FAILED tools/agent-workflow/tests/test_setup_project.py::test_the_real_checkout_has_no_failures
FAILED tools/agent-workflow/tests/test_setup_project.py::test_apply_binds_a_copy_and_is_idempotent
2 failed, 5497 passed, 10 skipped in 917.35s (0:15:17)
# both: only `foreign: 11 previous-project token(s)`, base-only (Step 0)
```

- **Behaviour changes:** in a new-order brief, a list item, block quote or HTML block that holds a heading, a Setext underline or a code fence is refused by name, also above the task, including fenced output inside an evidence list item (3 of the 120 old briefs have one). A tag line such as `<goal>` with only text below it passes; a fence directly under a tag line needs a blank line between them. Legacy sends and message lints use the base readers.
- **Deviation:** HTML is refused only when it holds structure, not at every HTML start (Step 0; the `<goal>` tags of the Claude wrapper are HTML blocks). PM decision pending user review, as is the whole fail-closed design.

### Not run (fix round 2)

- `ruff check` and `ruff format --check`: not_run (`No module named ruff`). `git diff --check`: no output, exit 0.
- Linux: not_run (Windows dev machine). Re-reading CommonMark 0.31.2 online for the condition-6 tag list: not_run (no network); a superset is used.

### Noticed, not done (fix round 2)

- The `_HEADER` pattern accepts a header line inside a list item (`- Kind: code`) by design, and a header line inside an HTML block still counts as one. This is unchanged and fails closed.

### Phase P1 fix round 1

- **Work completed:** `_scan` and the span helpers in `tools/agent-workflow/brief_lint.py`, used by all the sites in the class audit; `cli_worker.lint_gate` reads the footer first and passes it on; the docs, template and README say "verbatim footer span" and "ATX or Setext"; the tests.
- **Result and evidence:**

```text
# reviewer probes (.tmp/T-021/review-p1/*.md), lint_message(full=True, order=True)
fenced_tildes: [["brief-order", 1]]   fence_length: [["brief-order", 37]]
setext_h1: [["brief-order", 33]]      setext_h2: [["brief-order", 33]]
indented_h2: [["brief-order", 33]]    spoof_footer: [["brief-order", 6]]
good/crlf/bom: []  duplicate, fenced_backticks, task_before_footer: unchanged

# new tests on the pre-fix brief_lint.py (scratch tree)
13 failed, 1 passed (the thematic-break property passes before and after)
# the 6 test_the_review_bypasses_are_refused cases are among the 13

# mutants (.tmp/T-021/fix1/mutants.log): 11 of 11 killed
M1 any closer length closes, M2 any closer kind closes, M3 tildes open no fence, M4 indented ATX ignored,
M5 Setext ignored, M6 dash underline taken as a break, M7 footer span inside a fence accepted,
M8 order not bound to the span end (survived once: the real footer's `#` heading also counts as late;
   killed by test_the_task_follows_the_end_of_the_verbatim_span), M9 strip hides text up to the task,
M10 git-write reads fenced code, M11 fenced waiver waives

# focused: test_brief_lint.py test_cli_worker.py test_cli_worker_codex.py test_audit_extract.py
647 passed, 1 skipped in 117.04s (0:01:57)
# full tool suite (.tmp/T-021/fix1/full-suite.log), serial
FAILED tools/agent-workflow/tests/test_setup_project.py::test_the_real_checkout_has_no_failures
FAILED tools/agent-workflow/tests/test_setup_project.py::test_apply_binds_a_copy_and_is_idempotent
2 failed, 5470 passed, 10 skipped in 843.21s (0:14:03)
# both: only `foreign: 11 previous-project token(s); first: tasks/active/T-007-k0-sync.md:275`, base-only (Step 0)
```

- **Behaviour changes to note:** under the order rule, an edited footer or an unreadable footer file now gives `brief-order` beside `footer`; legacy sends still give only `footer`. Under both orders, text between the footer's end and `## Task` is linted. A fenced test-set waiver no longer waives. `_blocks` splits on LF, like the other rules (before: `str.splitlines`, which also split at U+2028 and similar characters).

### Not run (fix round 1)

- `ruff check` and `ruff format --check`: not_run (`No module named ruff`; `ruff: command not found`). `git diff --check`: no output, exit 0.
- Linux: not_run (Windows dev machine; no OS-specific code is involved).

### Noticed, not done (fix round 1)

- Block quotes, list items and HTML blocks are not modelled. Their lines are read as text, so a `> ## Role` or a `- ## Role` after the task is not counted as a heading, and a `## Task` line inside a multi-line HTML comment counts. Model them, or refuse them after the task, if the PM wants containers covered.

### Phase P1 (brief order)

- **Work completed:** rule `brief-order` and the footer span in `tools/agent-workflow/brief_lint.py`; the `brief_order` registry marker and `brief_order_applies` in `tools/agent-workflow/cli_worker.py`; layout texts in `.agents/policy/AGENT_TASK_TEMPLATE.md`, `.agents/policy/cli/README.md`, `.agents/policy/cli/claude.md`, `tools/agent-workflow/README.md`; `tools/agent-workflow/audit_brief.md` restructured (footer placeholder above its `## Task` block); tests in `test_brief_lint.py`, `test_audit_extract.py`, `test_cli_worker.py`, `test_cli_worker_codex.py`.
- **Result and evidence:**

```text
# focused (unit + property), test_slot.py -- python -X faulthandler -m pytest -q -p no:cacheprovider
#   test_brief_lint.py test_audit_extract.py test_cli_worker.py test_cli_worker_codex.py
631 passed, 1 skipped in 109.21s (0:01:49)

# mutants (scratch copy of tools/agent-workflow and .agents, tests test_brief_lint.py + test_cli_worker.py)
baseline green
M1 footer-before-task check dropped killed
M2 last-heading check dropped killed
M3 fences ignored by the order rule killed
M4 legacy branch always taken killed
M5 old cut kept (task block hidden) killed
M6 two task blocks accepted killed
M7 task heading not exact killed
M8 marker type not checked killed
M9 footer span drops its line breaks killed

# full tool suite, serial (pytest-xdist absent: `ModuleNotFoundError: No module named 'xdist'`)
# test_slot.py -- python -X faulthandler -m pytest -q -p no:cacheprovider tools/agent-workflow/tests
FAILED tools/agent-workflow/tests/test_setup_project.py::test_the_real_checkout_has_no_failures
FAILED tools/agent-workflow/tests/test_setup_project.py::test_apply_binds_a_copy_and_is_idempotent
2 failed, 5455 passed, 9 skipped in 966.49s (0:16:06)
```

  The two failures are base-only: `Left contains one more item: 'foreign: 11 previous-project token(s); first: tasks/active/T-007-k0-sync.md:275`, fixed in `eb3df16`, where the same file gives `33 passed` (Step 0). In the suite run my task file added a second `links` finding (relative links of the proposed `PM_POLICY.md` sentence); I fenced that sentence, and `setup_project.py check` and `test_setup_project.py` are back to the one `foreign` item (`2 failed, 12 passed`, each `Left contains one more item: 'foreign: ...T-007-k0-sync.md:275`).
- **Important changes and reasons:** the footer text is unchanged, so briefs in flight still match it verbatim; the footer is now the span from its heading to the `## Task` line, so the task block is linted by every rule (git-write included) with its true line numbers. The `## Task` heading is exact, so `## Task brief` of the template body is not taken for it. `lint_message(order=)` has no default, so every caller decides. The marker value is checked by type, so `True` or `2.0` is refused, not taken as `2`. Rejected: a version number on the footer (it would break the verbatim check of every brief in flight).
- **Problems, risks and remaining work:** P2 and P3 are not started. The live PM briefs must move to the new order from the next `start`; any `start` in the old order is now refused with `[brief-order]` (or passes with a logged `--lint-waive`).

### Not run

- `ruff check` and `ruff format --check`: not_run. `python -m ruff` → `No module named ruff`; `ruff --version` → `ruff: command not found`.
- Live Claude or Codex worker start with the new marker: not_run (the brief allows no live worker); covered by the unit tests named in the audit.

### Noticed, not done

- The footer title still says "append verbatim to every brief" though the task block now follows it; changing it would break the verbatim check of every brief in flight, so reword it once the workers started before T-021 are retired.
- `.agents/policy/roles/README.md:3` ("above the footer") stays correct; no role pack needed a change.

### Changed files

- `.agents/policy/AGENT_TASK_TEMPLATE.md`, `.agents/policy/cli/README.md`, `.agents/policy/cli/claude.md`
- `tools/agent-workflow/brief_lint.py`, `tools/agent-workflow/cli_worker.py`, `tools/agent-workflow/audit_brief.md`, `tools/agent-workflow/README.md`
- `tools/agent-workflow/tests/test_brief_lint.py`, `test_audit_extract.py`, `test_cli_worker.py`, `test_cli_worker_codex.py`
- `tasks/active/T-021-brief-order-scope-guard.md` (this file)
- Scratch kept: `.tmp/T-021/full-suite.log` (the cited suite run) and the scout note `.tmp/T-021/research.md`; the mutant script and copies are removed.

## Worker completion

- [x] **Changes (fix round 1):**
  - P2R-F1: dropped containers are found at document scope, one finding per top-level block.
  - P2R-F2: the multi-line reference title is joined once, and there is a cooperative turn-end deadline, `LintDeadline`.
  - P2R-F3: messages, the record and stderr are byte-bounded, with a `truncated` field.
  - The bounds are in a validated `guard_config.json` `report_lint` section.
  - The CLI exits 2 with `cannot lint`.
  - PM answers 1 to 5 are applied.
- [x] **Checks (fix round 1):**
  - Reviewer probes: `own` (33 cases, the 13 changed are all dropped-container cases now named), `performance` (reference 4 MB in 20.67 s, was a watchdog timeout) and `record` (455 to 458 bytes, was up to 2000335).
  - New tests: 26 fail on the pre-fix build; 513 pass after the fix.
  - Mutants: 10 of 10 killed; M5 needed an added test.
  - Sweeps: 176 runs, ratio ≤ 2.12; deadline overshoot ≤ 0.377 s.
  - Focused suites: 4157 passed, with the 1 test bug fixed.
  - Whole suite: 6262 passed, 2 failed (environmental: T-010 `.tmp` links), 12 skipped.
  - Real files: the same 48 findings.
  - `git diff --check`: exit 0. Ruff: not_run.
- [x] **Cleanup (fix round 1):** I left no process running. The scripts, baseline and before trees, mutant copies, sweep inputs, the basetemp and the regenerated reviewer case files are removed. Only the cited logs remain, in `.tmp/T-021/fix1/` and the three `*-fix1.log` files in `.tmp/T-021/review-p2r/`.
- [x] **Remaining (fix round 1):** Codex re-review; the PM decisions recorded as pending user review; ruff, Linux and CI; the standalone CLI deadline (Noticed, not done).
- [x] **Changes (P2 rebuild):** `report_lint.py` on the markdown-it-py 4.2.0 token stream; the `cli_worker.py` turn-end record; tests; the `pyproject.toml` pin; the dependencies row; the README. R4F1 and R4F2 closed. `brief_lint.py` untouched.
- [x] **Checks:**
  - 223 retest case files: 190 equal to the reviewed build, 33 changed, each with its verdict.
  - Reviewer contract tests: 15 passed, 3 failed, all 3 by the agreed contract change.
  - 2 MB sweep: 256 runs, largest ratio per doubling 2.14, no watchdog.
  - Mutants: 11 of 11 killed.
  - Focused suites: 956 passed, 1 skipped.
  - Whole tool suite: 6213 passed, 2 failed (environmental: T-010 links into `.tmp/`), 12 skipped.
  - Real task files: the same 48 findings as the reviewed build.
  - Ruff: not_run (not installed).
- [x] **Cleanup:** no process left running. Probe scripts, the sweep inputs, the mutant copies and the pytest basetemp are removed; only the cited logs remain in `.tmp/T-021/p2r/`.
- [x] **Remaining:** PM decisions on strikethrough and on the 8192 cap's worst case (48 s at 2 MB); Codex review; ruff, Linux and CI.

## AUTO PROGRESS 2026-11-16T11:38:20+07:00 (PreCompact, trigger=auto)
Written by the worker's PreCompact hook (mechanical); the sections above stay authoritative.
- git status (`<project>/.tmp/worktrees/T-021-p2r/.tmp/T-021/review-p2r`):
  - `## HEAD (no branch)`
  - `M ../../../docs/dependencies-and-environments.md`
  - `M ../../../pyproject.toml`
  - `M ../../../tasks/active/T-021-brief-order-scope-guard.md`
  - `M ../../../tools/agent-workflow/README.md`
  - `M ../../../tools/agent-workflow/cli_worker.py`
  - `M ../../../tools/agent-workflow/guard_config.json`
  - `M ../../../tools/agent-workflow/hooks/guard_config.py`
  - `M ../../../tools/agent-workflow/tests/test_cli_worker_codex.py`
  - `M ../../../tools/agent-workflow/tests/test_guard_config.py`
  - `?? ../../../tools/agent-workflow/report_lint.py`
  - `?? ../../../tools/agent-workflow/tests/test_report_lint.py`
- last changed files (worktree-relative; machine paths replaced by the worker): `tasks/active/T-021-brief-order-scope-guard.md`; `tools/agent-workflow/report_lint.py`; `.tmp/T-021/fix1/long_lines.py`; `tools/agent-workflow/tests/test_report_lint.py`; `.tmp/T-021/fix1/compare_own.py`; `.tmp/T-021/fix1/mutants.py`; `tools/agent-workflow/README.md`; `.tmp/T-021/fix1/sweep.py`; `.tmp/T-021/fix1/baseline_tree.py`; `tools/agent-workflow/tests/test_guard_config.py`
- last assistant text: While the suite finishes, writing the Completion report. First checking which case files my 'own' rerun left in the reviewer's folder.
