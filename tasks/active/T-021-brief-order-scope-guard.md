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

## Completion report

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
