# T-025 Cleanup tooling

## Step 0

- **Base:** `14f5765` (verified with `git rev-parse --short HEAD`). R-ID: R-021.
- **Existing / missing:** `purge_tmp.py` already provides three deleting commands and protected-tree and busy-worker checks; the SessionStart hook assembles bounded context. Configured sweep, archive retention, dry runs and cleanup metrics remain to be designed and verified.
- **Unknowns and failure modes:** ownership ledger formats and missing or malformed state could lead to deletion of active work; links, Windows junctions, nested protected evidence and races can cross safety boundaries. Archive retention must coexist with the protected `pm-archive` rule. A recursive size scan may exceed the hook's time budget or report an incomplete result as exact. Config schema and test fixtures may have callers outside the initial scope. Python 3.12 link behavior and the actual configuration loaders require primary-source verification before code.
- **Question 1 (highest impact):** How should archive retention operate while `pm-archive` is protected and protected-tree rules cannot change without approval? Proposed: preserve the current rule and report retention candidates without deleting them until the PM supplies an approved rule; please clarify whether an authorized narrowly scoped retention exception is intended.
- **Question 2:** What should happen when ownership state cannot be read or interpreted? Proposed: fail closed for deletion and report unavailable orphan metrics rather than assume no owner; verify missing-file semantics against existing conventions.
- **Plan:** (1) Read the existing callers, tests, configuration loaders, ownership writers and installed Python 3.12 sources; record evidence and configuration impact before runtime edits. (2) Implement validated age and retention settings, ownership-aware selection and dry runs while preserving existing safety protections; handle archive retention as clarified. (3) Add bounded, read-only SessionStart metrics and temporary-tree tests covering ownership, protected paths, links/junctions, missing or invalid config, overrides and dry runs. (4) Run the brief's test-slot done check, relevant lint/format and 3–10 scratch-copy mutants; run only `sweep --dry-run` on the real checkout, paste actual output, and complete the configuration audit and handoff.
- **Finish check:** both named pytest files pass through `test_slot.py`; the real-checkout dry run lists candidates without deletion; any unrun checks or unresolved safety issues are explicit. No real deletion is authorized by this plan.

## Worker completion

- [x] Changes: Step 0, research and approved configuration impact recorded; no runtime implementation started.
- [x] Checks: `git rev-parse --short HEAD` returned `14f5765`; Windows `shutil.rmtree.avoids_symlink_attacks` returned `False`. Implementation tests, mutations and dry run not_run because implementation is blocked.
- [x] Cleanup: No scratch files or processes created by this worker.
- [x] Remaining: Resolve the PreToolUse protection for the scoped files, implement, and complete the checks in Step 0.

Kind: code
R-IDs: R-021
Task file: tasks/active/T-025-cleanup-tooling.md
Approach note: .tmp/T-025/approach.md

## Brief header

- **Goal:** `.tmp` cleanup becomes one safe command instead of hand work (decision 43, standing rule S40): a dry run, an orphan sweep by configured age, a retention for `.tmp/pm-archive`, and a SessionStart line that shows the size of `.tmp` and the number of orphans.
- **Done check:** `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_purge_tmp.py tools/agent-workflow/tests/test_session_context.py` passes with the new tests, and `purge_tmp.py sweep --dry-run` on the real checkout prints the list it would delete without deleting anything (paste the output).
- **Work items** (plan row T-025):
  1. `purge_tmp.py` gains `--dry-run` for every deleting command and a `sweep` command: list the children of `.tmp/` that no worker (`cli-workers.json`), task in flight (`## IN-FLIGHT` of `.tmp/pm-state/pm-continuation.md`) or Antigravity job (`.tmp/pm-state/agents.json`) owns and that are older than a configured age, then delete them; never `.tmp/pm-state/`, `.tmp/pm-archive/`, `.tmp/cli-jobs/`, worktrees of tasks in flight, trees a busy worker uses, or anything reached through a link.
  2. The age is configuration (plan default 72 hours) with a validated key and a refusal for a missing or invalid value (AGENTS.md section 5); a retention for `.tmp/pm-archive` (age or size) is configuration too.
  3. The SessionStart hook prints one line: size of `.tmp` and number of orphans past the age (read-only, bounded time).
  4. ORCHESTRATION section 4 text that says "task T-025 makes the age a configured value" is updated by the PM at integration, not by the worker.
- **Scope:** `tools/agent-workflow/purge_tmp.py`, the configuration file that holds tool settings (find where `purge_tmp.py` and the hooks read their settings; add the keys there), `tools/agent-workflow/hooks/session_context.py` (or the hook that prints the SessionStart lines; find it), their tests, `.tmp/T-025/`.
- **Out of scope:** guards and Git (T-024), quota files and the quota line of the SessionStart hook (T-026), deleting anything for real outside tests during the task (the dry run on the real checkout is the only real run).
- **Output:** the patch in the worktree, the completion report in this file, a reply under 15 lines.
- **Step budget:** about 40 tool calls.
- **Run limit:** no network, no GPU, no installs; tests use temporary trees only.
- **May assume / Must ask:** may choose names and the configuration file layout inside existing conventions; must ask before deleting any real path and before changing a protected-tree rule.
- **Role pack and CLI wrapper:** implementer; Codex (`.agents/policy/cli/codex.md`).
- **Test sets:** not applicable: the lint's built-in suites belong to the earlier project (T-020 rebinds them); this task keeps `tools/agent-workflow/tests/` green (report changes in the 10 known Windows failures).
- **Trivial:** no. **Small:** no.
- **Base:** the user's latest commit on `main` at assignment (record it here).

## Task brief

- **Task name:** T-025 cleanup tooling.
- **Responsible role:** implementer.
- **Task difficulty and assigned model:** medium, clear spec; Codex `gpt-6.1-terra` `medium`, role `codex-dev` (PM_POLICY section 2).
- **Reasoning effort and reason:** medium; the risk is deleting the wrong tree, which tests on temporary trees cover.
- **Preloaded skills:** `handoff-report`, `config-audit`.
- **Current status:** `purge_tmp.py` has `archive-remove` and `purge-children`; the orchestrator sweeps by hand.
- **What was already done:** nothing for this task.
- **Expected output:** see the brief header.
- **Constraints and security cautions:** deletion code: every delete path is tested against links, junctions on Windows, protected trees and busy workers; a dry run deletes nothing.
- **What must not be repeated:** none yet.
- **Ignored evidence paths:** `.tmp/T-025/`.

### Research required before code

1. How Python's `shutil.rmtree` and `os.scandir` treat Windows junctions and symlinks at the installed Python 3.12 version (source or docs), so a link is never followed.
2. Where the existing hooks read their configuration (`project_config.py`, `.agents/project.json`, `guard_config.json`): source-verified.

### Configuration impact

Dedicated `tools/agent-workflow/cleanup_config.json`, validated by `purge_tmp.py` and consumed by SessionStart. `sweep_age_hours`, `archive_retention_hours`, and `scan_timeout_seconds` require positive finite numbers; missing or invalid fields are refused with their names. PM-selected unmeasured examples are respectively 72, 336, and 2. Tests will exercise non-default values and missing/invalid refusal for each key. The project root and `.tmp`/archive locations remain artifact-bound; protected paths and link boundaries remain safety-bound. No model, provider, device or inference settings are affected.

## Research evidence (partial)

| Question | Source | Finding | Class |
|---|---|---|---|
| Python 3.12 link handling | Installed Python 3.12.10 `C:/Users/ADMIN/AppData/Local/Programs/Python/Python312/Lib/shutil.py:619` and `os.py:282`, `os.py:387` | `rmtree` uses `os.walk` with a sentinel that classifies junctions and symlinks as files; default `entry.is_dir()` follows links. The Windows implementation is explicitly described as race-vulnerable (`shutil.py:618`). | source-verified |
| Race resistance on this host | `..\\..\\..\\.venv\\Scripts\\python.exe -B -c "import shutil; print(shutil.rmtree.avoids_symlink_attacks)"` | `False`; static link checks alone cannot guarantee safety against concurrent replacement. | experiment |
| Hook configuration | `tools/agent-workflow/hooks/session_context.py:48`, `:50`, `:352`; `tools/agent-workflow/memory_hooks_config.py:1` | SessionStart loads `memory_hooks.json` using `memory_hooks_config.py`; settings are validated per field. | source-verified |
| Absent ownership files | `tools/agent-workflow/purge_tmp.py:124`, `:128`; `tools/agent-workflow/hooks/precompact_snapshot.py:44`, `:94`, `:109` | Missing worker registry means no workers; snapshot reader maps unavailable job JSON to no jobs and an empty or absent IN-FIGHT section to none. Malformed/unreadable sources must nevertheless fail closed per PM instruction. | source-verified |
| AGY ownership | `tools/agent-workflow/pm-supervisor.ps1:4`; `scripts/agy-runner/invoke-job.ps1:37` | Registry records list job kind, ID, label and start time; workspace is separately named by the runner. Mapping an arbitrary registered job to its workspace is not yet verified. | source-verified |

## Completion report

- **Work completed (Claude worker, second attempt):** `purge_tmp.py`: `--dry-run` on all four deleting commands; new `sweep` and `prune-archive`; validated `CleanupConfig` loader (`cleanup_config.json`, new); ownership reader (`cli-workers.json` busy cwds, `## IN-FLIGHT` names, `agents.json` unfinished jobs). `hooks/session_context.py`: one read-only `.tmp` line (size, orphans, partial flag). Tests added to both test files. Changed: `purge_tmp.py`, `hooks/session_context.py`, `cleanup_config.json` (new), the two test files.
- **Behaviour:** `sweep` judges the children of `.tmp`, and of the containers `worktrees` and `agy-work` (the container is never judged). Protected names, `pm-state`/`pm-archive`/`cli-jobs`/`runs`/`.secrets`/`agy-work/_results`, stay untouched and `sweep` never acts on `pm-archive`. Owned = name appears as a whole token in `## IN-FLIGHT` (structural match, no word list), or a busy worker or non-gemini job cwd contains/lies inside it, or (`agy-work` children) a gemini job in `agents.json` has no `_results/<id>*` file. Absent ownership files = no owner (as `precompact_snapshot.py:44,94,109` and `purge_tmp.py:124-129`); malformed or unreadable = every candidate "ownership unknown", nothing deleted, exit 1. `prune-archive` deletes only direct children of `.tmp/pm-archive` older than `archive_retention_hours` (a link child is removed as a link); the archive stays. Age is the entry's own `lstat` mtime, like `purge_older`. Deletion reuses `_remove` (link check right before each delete, junction removed with `rmdir`).
- **Done check:** `test_slot.py -- python -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_purge_tmp.py tools/agent-workflow/tests/test_session_context.py` -> `128 passed in 5.83s`.
- **Mutants:** 10 on a scratch copy (inverted age, ownership problem ignored, token lookahead dropped, zero accepted, dry run deleting, archive age inverted, agy always idle, protected-name skip removed, partial scan reported complete, IN-FLIGHT section ignored): all killed. One survived the first run (protected-name skip in `plan_sweep`, masked by `_refusal`); `test_the_cheap_plan_never_counts_a_protected_name_as_an_orphan` kills it. Scratch removed.
- **Real checkout dry run** (read-only, script loaded from this worktree with `PROJECT` pointed at the real checkout; nothing deleted):
  ```
  $ purge_tmp.py sweep --dry-run
  D:\Project_github\prompt-enhancer-pipeline\.tmp: would delete 0 orphan(s) older than 72 h
  $ purge_tmp.py prune-archive --dry-run
  D:\Project_github\prompt-enhancer-pipeline\.tmp\pm-archive: would delete 0 entries older than 336 h
  ```
  Everything under `.tmp` is younger than 72 h, so the configured list is empty. To show a non-empty plan, the same dry run with an illustrative age of 0.01 h (not the configured value) printed: `kept T-002 ... T-026, cold-read, pm-checks, worktrees/T-002, T-024, T-025: owned (named in ## IN-FLIGHT)`, `would delete T-002-review (~0.0 MB)`, `would delete T-002-review2 (~0.0 MB)`, `would delete 2 orphan(s)`.
- **not_run:** `ruff check` and `ruff format --check` (ruff is not installed in the project `.venv`; no installs allowed); full `tools/agent-workflow/tests/` suite (only the two named files, so the 10 known Windows failures are unchecked); CLI end-to-end `sweep` with a real deletion (forbidden); POSIX symlink tests (this host is Windows; junction tests are `skipif(os.name != "nt")`).
- **Residual risks:** (1) time-of-check race: `shutil.rmtree.avoids_symlink_attacks` is `False` on this host (`shutil.py:618-619`), so a directory swapped for a link between the `_is_link` check in `_remove` (`purge_tmp.py`, `_remove`) and the delete can be followed; accepted by the PM for the local-agent threat model. (2) Age uses the entry's own mtime, not its newest descendant, so an old-looking directory that is being written deep inside is protected only by ownership. (3) IN-FLIGHT ownership is a whole-token name match: a name that is only mentioned in passing keeps the tree (safe direction); a task referred to by a different name would not be recognised.
- **Notes:** the hook count of orphans skips the protected-tree walk (`plan_sweep(refusals=False)`), so it can exceed the `sweep --dry-run` list by the trees that list refuses. Loading `purge_tmp.py` by file path forbids `@dataclass` (`from __future__ import annotations` fails there), hence `NamedTuple` and a plain class. `.secrets/` was not opened; `.secrets/README.md` not in the patch.
- **Noticed, not done:** map single Antigravity jobs to their workspaces (today any unfinished gemini job owns all of `.tmp/agy-work` children); ORCHESTRATION section 4 text "T-025 makes the age a configured value" (PM at integration); `sweep` could use the newest descendant mtime for age; T-026 shares the SessionStart hook (new `tmp cleanup` block sits after the git block).

### QA round 1 (Codex review FAIL: 4 P1, 1 P2) - fixes

Superseded by this section: in the first round's Behaviour line, an Antigravity job is no longer finished by any `_results/<id>*` file; and the single Done check and 10 mutants above are replaced by the figures below.

**Root cause (S31):** a deleting path that did not go through the full authorization. Every function that selects or deletes a path, and what it does now:

| Function | Selects / deletes | Before | Now |
|---|---|---|---|
| `_remove` (and `_remove_link`) | the only delete primitive | called by the four below | unchanged; every caller authorizes first (link checked right before each delete) |
| `archive_remove` | explicit worktrees | `_refusal` (protected, busy) | `authorize(check_inflight=False)` after `_ownership_or_exit()` |
| `purge_children` | matching children | manifest + `_refusal` | manifest + `authorize(check_inflight=False)` |
| `purge_older` | old children | manifest + `_refusal` | manifest + `authorize(check_inflight=False)` |
| `sweep` via `plan_sweep_bounded` | orphans of `.tmp` | name/age + `_owner` + `_refusal` | `authorize(check_inflight=True)` |
| `prune_archive` | old archive children | none (age only) | `_path_problem(ARCHIVE)` + `authorize(check_inflight=True, exempt=ARCHIVE)` per child |
| `_archive_reports` | copies reports (no delete) | under `archive_remove` | unchanged |

`authorize` (`purge_tmp.py`) checks in order: (1) containment inside `<project>/.tmp`, no link or junction on any ancestor from the project root, resolved parent inside `.tmp` (`_path_problem`); (2) protected names of the target and its ancestors, and with `deep` the trees nested inside; (3) ownership: unreadable or malformed state = `unknown` (fail closed), a name in `## IN-FLIGHT` (only for `sweep` and `prune-archive`), an unfinished Antigravity job, a busy worker cwd; (4) manifest-bound files. The one waiver is `exempt=ARCHIVE` for the protected name of `pm-archive` as the parent. Decision for the PM: the explicit commands (`archive-remove`, `purge-children`, `purge-older`) skip only the IN-FLIGHT name match, because ORCHESTRATION section 4 keeps a task listed in IN-FLIGHT as pending cleanup while retrying `archive-remove`; they otherwise refuse unreadable ownership (SystemExit "refusing"), exactly as for the worker registry before.

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | `<id>.*` ended an Antigravity job | finished only when `_results/<id>.done` is a file (`agy-job.ps1` writes it last, `.stderr.txt` during the run) | `test_qa1_a_running_antigravity_job_is_not_finished_by_its_stderr_file`, updated `test_sweep_keeps_agy_work_...` |
| 2 | `[{}]` failed open | strict records: object, kind in gemini/codex/claude, usable id, `cwd` string (required for codex); any violation = ownership unknown | `test_qa1_a_malformed_agent_record_refuses_every_deletion` (9 cases, sweep and purge) |
| 3 | `prune-archive` skipped checks | same `authorize` | `test_qa1_prune_archive_keeps_...` (busy cwd, nested `runs/`, unreadable ownership, in-flight name) |
| 4 | linked ancestor followed | `_path_problem` on every path, also `.tmp` and `pm-archive` themselves | `test_qa1_a_linked_tmp_is_never_enumerated`, `test_qa1_a_linked_ancestor_refuses_the_explicit_commands` (junction, `skipif` not Windows: junctions need NTFS) |
| 5 | hook line past its bound | one deadline for size scan, `load_ownership(deadline)` between sources and per candidate in `plan_sweep_bounded`; past it the line says `partial: time bound reached` | `test_tmp_line_shares_one_deadline_with_the_ownership_reads`, `test_qa1_planning_stops_at_the_deadline_and_says_incomplete` |

Property test: `test_qa1_property_no_command_removes_an_owned_or_protected_path` (40 random trees of old, protected, owned and busy names, plus a protected tree nested in an archive entry) runs `sweep` and `prune_archive` and asserts the owned, busy and protected paths survive.

- **Done check:** `test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_purge_tmp.py tools/agent-workflow/tests/test_session_context.py` -> `148 passed in 6.86s`.
- **Mutants on the shared authorization** (8, scratch copy, removed): link ancestor ignored, archive exemption always on, ownership problem ignored, nested protected walk skipped, any result file finishes a job, unknown kind accepted, busy cwd ignored, planning deadline ignored: the last one survived the first run and is now killed by `test_qa1_planning_stops_at_the_deadline_and_says_incomplete`; all 8 killed.
- **Real checkout, both dry runs** (read-only, nothing deleted): `sweep --dry-run`: `...\\.tmp: would delete 0 orphan(s) older than 72 h` (exit 0); `prune-archive --dry-run`: `...\\.tmp\\pm-archive: would delete 0 entries older than 336 h` (exit 0).
- **not_run:** `ruff check` / `ruff format --check` (ruff not in the project `.venv`); the full `tools/agent-workflow/tests/` suite (only the two files); POSIX symlink variants (host is Windows).
- **Residual risks:** unchanged (time-of-check race on Windows accepted by the PM; age by the entry's own mtime). New: a blocking read of an ownership file cannot be interrupted, so the hook can exceed its bound by one read, but it then reports `partial`; the check is cooperative between reads.
- **Noticed, not done:** `hooks/precompact_snapshot.py:98-105` still treats any `<id>*` result as a finished job (same defect as finding 1, outside Scope).

### QA round 2 (Codex retest FAIL: 2 P1) - fixes

Root cause (S31): the gate was right but callers could hand it a pre-resolved path (`_inside_project` called `Path.resolve()`, which erased a linked ancestor) or partial inputs (`archive-remove` passed no manifests). Structural fix in `purge_tmp.py`: `authorize()` is now private (`_authorize`) and the only public gate is `Gate(check_inflight, exempt, deep, strict, deadline)`. A caller states only its policy; the `Gate` itself loads ownership (workers, `## IN-FLIGHT`, `agents.json`) and candidate manifests (project-level plus those of the target's enclosing root), judges the path exactly as given (`os.path.abspath`, never resolved; any `..` component refused because collapsing it lexically differs from the OS behind a link), resolves only after the no-link-on-any-ancestor check, and `Gate.remove()` re-checks and then deletes, so no command deletes what it did not just authorize. `_inside_project` no longer resolves and `_link_location` is gone. `strict=True` (explicit commands) turns unreadable ownership into SystemExit "refusing"; a directory argument reached through a link is refused before it is enumerated (`_refuse_linked_directory`).

| Entrypoint | Path given to the gate | Inputs loaded by | Deletes through |
|---|---|---|---|
| `archive_remove` | `_inside_project(raw)`, lexical | `Gate(strict)` | `gate.remove` |
| `purge_children` | children of the lexical parent (parent link refused) | `Gate(strict)` | `gate.remove` |
| `purge_older` | same | `Gate(strict)` | `gate.remove` |
| `sweep` (planning and deletion share one gate) | children of `.tmp` as listed | `Gate` | `gate.remove` |
| `prune_archive` | children of `.tmp/pm-archive`, `exempt=ARCHIVE` | `Gate` | `gate.remove` |
| SessionStart count (`plan_sweep_bounded`, read-only) | children of `.tmp` | `Gate(deep=False, deadline)` | none |
| `_remove` | private primitive, called only by `Gate.remove` | - | - |

| # | Finding | Test (entrypoint level) |
|---|---|---|
| 1 | linked ancestor hidden by resolve | `test_qa2_archive_remove_refuses_a_path_through_a_linked_ancestor` (dry and real), `test_qa2_purge_commands_refuse_a_directory_reached_through_a_link` (`purge_children`, `purge_older`, CLI `main`), `test_qa2_sweep_never_selects_through_a_link`; junction tests `skipif` not Windows (NTFS junctions) |
| 2 | no manifests for `archive-remove` | `test_qa2_archive_remove_keeps_a_manifest_bound_tree` (dry and real), `test_qa2_every_deleting_command_keeps_a_manifest_bound_tree` (`purge_children`, `purge_older`, `sweep`, `prune_archive`) |
| - | gate itself | `test_qa2_gate_remove_authorizes_again_...`, `test_qa2_a_parent_reference_is_refused_by_every_entrypoint`, `test_qa1_authorize_refuses_outside_and_protected_paths` (now through `Gate`) |
| - | property | `test_qa2_property_no_command_selects_a_path_through_a_junction` (all five commands, dry or real, names via a junction; `skipif` not Windows) next to the round-1 property test |

Behaviour change to note: `purge_older` used to skip a manifest-bound entry silently; it now reports `refused ...: holds a manifest-bound file` and counts it as kept (the same wording the gate gives every command).

- **Done check:** `test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_purge_tmp.py tools/agent-workflow/tests/test_session_context.py` -> `159 passed in 8.44s`.
- **Mutants** (4, scratch copy, removed): entrypoint resolves the path again, gate drops its manifests, `..` accepted, `Gate.remove` skips the re-check: all killed (the last needed `test_qa2_gate_remove_authorizes_again_...`).
- **Real checkout dry runs** (read-only, nothing deleted): `sweep --dry-run`: `...\\.tmp: would delete 0 orphan(s) older than 72 h` (exit 0); `prune-archive --dry-run`: `...\\.tmp\\pm-archive: would delete 0 entries older than 336 h` (exit 0).
- **not_run:** `ruff check` / `ruff format --check` (ruff not in the project `.venv`); full `tools/agent-workflow/tests/` suite (two files only); POSIX symlink variants (Windows host).
- **Residual risks:** the Windows time-of-check race (accepted); `Gate.remove` narrows but cannot close it.

### QA round 3 (Codex retest FAIL: 1 P1) - fix

Root cause (S31): the manifest roots the gate read were derived from the target's ancestors only (`_protected_paths(absolute.parent)`), so a worktree's own `.tmp/review/candidate-manifest.json` was skipped when the target was that worktree, or a directory holding it. One rule now (`_manifest_roots(target)`, used by `Gate._manifests_for`): the gate reads the manifests of the project root, every ancestor worktree (a directory with `.git`, file or directory) between the project and the target, the target itself when it is a worktree, and every worktree below the target (walk without entering links or `.git`). Roots are cached per gate; `deep=False` (the read-only SessionStart count) reads none.

**Audit of every place that computes manifest or ownership roots:**

| Place | Roots | Result |
|---|---|---|
| `Gate._manifests_for` | was parent ancestors + project | fixed: `_manifest_roots` (project, ancestor worktrees, target, worktrees below) |
| `_protected_paths(path)` | climbs upward from the given path to the first `.git` or the project | correct when given a root; no other caller remains (all callers now pass roots from `_manifest_roots`) |
| `_holds_protected` | resolves the candidate and compares with bound files | unchanged; bound paths are resolved at load |
| `plan_sweep_bounded`, `sweep`, `prune_archive`, `archive_remove`, `purge_children`, `purge_older` | no root logic of their own since round 2 | all call `Gate.check` / `Gate.remove` |
| `load_ownership` | project `.tmp/pm-state` only (`cli-workers.json`, `pm-continuation.md`, `agents.json`) | no omission found: every writer derives these from its own checkout root (`cli_worker.py:41` `REGISTRY = ROOT/.tmp/pm-state/cli-workers.json`; `pm-supervisor.ps1:4`); a worktree has no ledger of its own that the PM reads. Source-verified for the registry and the supervisor only; the note path is by `ORCHESTRATION.md` section 3 (assumption) |
| `_busy_conflict` / `_owner` | busy cwds, absolute, from the project registry | any cwd of a worktree is judged against the target; unchanged |

Tests (public commands and CLI): `test_qa3_archive_remove_keeps_a_worktree_bound_by_its_own_manifest` (dry and real), `test_qa3_the_cli_refuses_a_worktree_bound_by_its_own_manifest` (`main`, dry and real, exit 1), `test_qa3_a_parent_holding_a_worktree_with_a_manifest_is_kept_by_every_command` (`archive_remove`, `purge_children`, `sweep`, `purge_older`), `test_qa3_a_target_below_a_worktree_is_bound_by_that_worktrees_manifest`, `test_qa3_prune_archive_keeps_an_archived_worktree_bound_by_its_own_manifest`, and `test_qa3_property_no_command_removes_a_file_bound_by_any_root` (shapes self, holder, below x five commands x dry or real, also with an archived copy for `prune`).

- **Done check:** `test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_purge_tmp.py tools/agent-workflow/tests/test_session_context.py` -> `166 passed in 9.25s`.
- **Mutants** (4, scratch copy, removed): target and below never scanned, ancestor worktrees ignored, a `.git` file not a worktree marker: killed. "Gate reads roots of the parent only" survived. It is not equivalent: it also reads sibling worktrees' manifests, so a sibling manifest binding `../target/keep.txt` makes it refuse where the original selects (reproduced by the Codex reviewer, final retest P3). The difference only retains more data, so no test demands the original's behaviour.
- **Full suite once** (from the main root against this worktree's paths, through `test_slot.py`): `1 failed, 3195 passed, 4 skipped in 348.77s`; the one failure is the known `test_pm_invariants.py::test_read_of_the_result_silences_it_until_it_changes`; nothing else failed.
- **Real checkout dry runs** (read-only, nothing deleted): `sweep --dry-run`: `...\\.tmp: would delete 0 orphan(s) older than 72 h` (exit 0); `prune-archive --dry-run`: `...\\.tmp\\pm-archive: would delete 0 entries older than 336 h` (exit 0).
- **not_run:** `ruff check` / `ruff format --check` (ruff not in the project `.venv`); POSIX symlink variants (Windows host).
- **Open, stated plainly:** (1) the Windows time-of-check race (accepted by the PM; `Gate.remove` narrows it); (2) the manifest walk below a target costs one extra tree walk per candidate in `sweep`/`prune-archive` (no measurement made); (3) `hooks/precompact_snapshot.py:98-105` still treats any `<id>*` result as finished (outside Scope); (4) ruff not run.

### Configuration audit

| Row | Value | Config key / validation | Test | Class |
|---|---|---|---|---|
| Sweep age | 72 h (PM example, not measured) | `cleanup_config.json` `sweep_age_hours`; positive finite number; missing, bool, string, null, 0, negative, nan, inf refused with the key name | `test_cleanup_config_refuses_a_missing_key`, `..._an_invalid_value`, `..._accepts_non_default_values` (1.5), `test_sweep_age_is_the_configured_value` (5 h), `test_sweep_refuses_a_broken_config_from_the_command_line` | configurable |
| Archive retention | 336 h (PM example) | `archive_retention_hours`, same validation | same refusal tests, `test_prune_archive_deletes_only_old_children_and_keeps_the_archive` (1 h override) | configurable |
| SessionStart scan bound | 2 s (PM example) | `scan_timeout_seconds`, same validation | refusal tests, `test_tmp_line_says_partial_when_the_scan_bound_is_hit` (3 s), `test_bounded_size_reports_a_partial_scan` | configurable |
| Unknown keys / unreadable file | refused | `load_cleanup_config` | `test_cleanup_config_refuses_unknown_keys_and_bad_files`, `test_tmp_line_names_a_missing_config_key` | safety-bound |
| Protected names, link boundaries, fail-closed ownership | existing `PROTECTED_NAMES`, `_is_link`; new fail-closed | code | `test_sweep_never_touches_protected_trees`, `..._fails_closed_...` (3), junction tests | safety-bound |
| `CONTAINERS` (`worktrees`, `agy-work`), `INFLIGHT_HEADING`, config file name, `.tmp`/`pm-archive` locations | literals | code | `test_sweep_keeps_a_task_named_in_flight`, `..._agy_work_while_a_job_is_unfinished` | artifact-bound |
| QA round 1: `AGENT_KINDS` (gemini, codex, claude), completion marker `<id>.done`, `scan_timeout_seconds` now also bounding ownership reads and planning | literals / existing key | `purge_tmp.py` `_read_agent_jobs`, `load_ownership(deadline)` | `test_qa1_a_malformed_agent_record_...`, `test_qa1_a_running_antigravity_job_...`, `test_tmp_line_shares_one_deadline_...` | protocol-bound (runner and supervisor formats), configurable (bound) |
| Hook line text, `GB`/`MB` units | literals | code | `test_tmp_line_*` | protocol-bound (display) |

No model, provider, device, endpoint or inference setting is touched. Unclassified literals: none; hidden defaults: none (the loader has no fallback; the hook prints `.tmp cleanup not configured: <reason>` instead).

## Handoff

- **Current status:** implemented; done check green; ready for review (Sonnet 5.5 `medium`).
- **What was already done:** everything in the Completion report.
- **What the next agent must do:** review; at integration update ORCHESTRATION section 4, run `ruff check` and `ruff format --check` where ruff exists, run the full `tools/agent-workflow/tests/`.
- **What must not be repeated:** no real deletion; only `--dry-run` on the checkout.
- **Worktree, base and partial patch:** T-025 worktree, base `14f5765`; uncommitted patch in `tools/agent-workflow/` (`purge_tmp.py`, `hooks/session_context.py`, `cleanup_config.json`, two test files) plus this file. `.tmp/T-025/` holds only `approach.md`.
- **Decisions and notes for the PM to merge:** archive retention is its own command `prune-archive`; `sweep` still treats `pm-archive` as protected. Config example values are PM examples, not measured.

## PM assignment (2026-11-14, decision 75)

- **Approach note:** a Gemini 3.8 Flash `medium` read-only job writes `.tmp/T-025/approach.md` first.
- **Step 0:** Codex stops at Step 0 (two messages, cli/codex.md); the PM approves it (no permission or product meaning involved) and sends the go.
- **Review:** ordinary work: Sonnet 5.5 `medium` (Antigravity first once T-024 lets it read an uncommitted patch).
- **Reassigned 2026-11-14 (PM):** the Codex worker could not edit `tools/agent-workflow/` (`hooks/codex_guard.py:104-105` protects it, worktree copies included). Implementer now Claude Sonnet 5.5 `medium` (`cli_worker.py start --profile dev-medium`), with the Codex Step 0 and research as handoff; reviewer Codex `gpt-6.1-sol` `medium` (read-only), another family.
- **Parallel:** beside T-024; T-026 waits for this task (shared SessionStart hook).
- **Brief assembly:** `.agents/policy/cli/codex.md`, this file, `.agents/policy/roles/implementer.md`, `.agents/policy/AGENT_BRIEF_FOOTER.md` into `.tmp/T-025/brief.md`; lint it.
