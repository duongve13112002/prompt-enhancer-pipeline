# T-020 Rebind the brief-lint test-set rule to this repository's suites

Kind: code
R-IDs: R-021, R-025
Task file: tasks/active/T-020-brief-lint-test-sets.md
Approach note: .tmp/T-020/research.md (the scout's research and hazard note, in the worktree)

Started 2026-11-15 alongside T-024 (decision 97); tooling, so a Claude worker implements and Codex reviews (decision 91, `hooks/codex_guard.py:104-105`).

## Brief header

- **Goal:** `tools/agent-workflow/brief_lint.py` rule `test-sets` (#25) stops naming another project's suites. Today `_SET_PATTERNS` (`brief_lint.py:222-233`) hardcodes the old project's class test sets (the configs sweep, `test_config_key_effect`, `test_evidence_hash_scopes`, `test_json_depth`, digest-pin suites, importers), so every feature, code or review brief here must carry a `Test sets: not applicable` waiver. After this task the class test sets come from validated, repository-bound configuration (the project binding `.agents/project.json` or a configuration file it names), list this repository's real suites (the tool suite `tools/agent-workflow/tests/` and the pipeline suite T-005 creates, with the path it chooses), and an absent or invalid configuration is refused with a precise message (R-015, S23, S27); the waiver line keeps working.
- **Done check:** the brief-lint tests pass through `tools/agent-workflow/test_slot.py` with the project venv python, plus new tests: a brief naming the configured sets passes; one missing set is reported by its configured label; the waiver still passes; a missing, empty or malformed configuration is refused; a non-default configuration (another suite list) changes the result; and the whole tool suite `tools/agent-workflow/tests/` stays green apart from known unrelated failures. The PM's real briefs `.tmp/T-005/brief.md` and `.tmp/T-024/brief.md` lint as before or better (run them, decision 99).
- **Scope:** `tools/agent-workflow/brief_lint.py` (the test-set rule and its configuration loading only), its tests `tools/agent-workflow/tests/test_brief_lint.py`, the configuration file or binding key you choose (Step 0), the one documentation line in `.agents/policy/AGENT_TASK_TEMPLATE.md` that says the list names another project's suites, the `brief_lint` rows of `tools/agent-workflow/README.md`, `.tmp/T-020/` and this task file.
- **Out of scope:** every other lint rule; `hooks/`, `cli_worker.py`, `settings.example.json`, `setup_project.py` and every other file of the T-024 patch (T-024 is in flight); brief order and the scope-guard (T-021); network; installs; GPU.
- **Output:** the patch in the worktree (unstaged), the Completion report in this task file, a reply under 15 lines.
- **Step budget:** about 40 tool calls after Step 0 approval.
- **Run limit:** CPU only; no network, no GPU, no paid calls.
- **May assume / Must ask:** may choose the configuration shape and helper names; must ask (Step 0 question) about which suites count as class test sets and how a brief names them, and about any change to the waiver.
- **Role pack and CLI wrapper:** implementer; Claude (profile `dev-medium`).
- **Test sets:** not applicable: this task replaces the hardcoded list that names another project's suites, so no class set of this repository is defined yet.
- **Trivial:** no. **Small:** yes (one rule and its configuration).
- **Base:** pushed `main` a983485 (worktree `.tmp/worktrees/T-020`, detached); no predecessor patches.

### Research required before code

1. The scout's note `.tmp/T-020/research.md`; re-read the lines it cites (R-021).
2. How `brief_lint.py` reads the project binding today (`.agents/project.json`, `read_task_file`) and how other tools in `tools/agent-workflow/` load validated configuration (for example `cleanup_config.json`, `guard_config.json`, `worker_profiles.json`), quoted.

### Configuration impact

- **New file:** `tools/agent-workflow/brief_lint_config.json`, loaded by `brief_lint.load_class_sets` (no source default).
- **Schema:** `required`: list of `{label, pattern}`; `alternatives`: non-empty object of named groups, each a non-empty list of `{label, pattern}`. `_`-prefixed keys are comments. Pattern: case-insensitive regex that must compile and must not match the empty text.
- **Shipped values:** required = configuration override and refusal tests, property tests (`docs/test-plan.md:1-16`); alternatives = `tooling` (`tools/agent-workflow/tests`), `pipeline` (root `tests/`, not preceded by a path character).
- **Override and refusal tests:** `test_a_non_default_class_set_file_changes_the_result`, `test_the_nearest_group_is_reported_and_ties_keep_file_order`, `test_the_waiver_holds_for_any_valid_configuration`; refusal: 18 parametrized `test_an_invalid_class_set_file_is_refused_with_a_precise_message`, `test_a_missing_empty_or_unreadable_class_set_file_is_refused`.

## Step 0

- **Unknowns:** where the sets live; how a brief names the group that fits; whether the waiver changes. The pipeline suite path is `tests/` (PM, T-005 Step 0).
- **Q1 (answered by the PM direction, location chosen here):** a new `brief_lint_config.json` next to `brief_lint.py`, in the `cleanup_config.json` style (`purge_tmp.load_cleanup_config`: `cannot read`, missing key, unknown key refusals). Reason: `project_config.parse` is a strict schema shared by hooks and `setup_project.py` (T-024 in flight), so a new key there would touch files outside this scope; the rule's own file keeps the change in `brief_lint.py`.
- **Q2 (proposed answer applied):** a brief names the group that fits by naming that group's suite path: it must name every `required` set and every set of at least one `alternatives` group (`tooling`, `pipeline`), so no brief declares a group name. A missing group reports the nearest group's labels.
- **Q3 (proposed answer applied):** the waiver is unchanged (`Test sets: not applicable: <reason of 10+ characters>`).
- **Q4 (decision):** a broken file becomes a blocking `test-sets` finding, not an exception, because `cli_worker.lint_gate` only warns when the linter raises. It is also reported behind a waiver (a broken repository binding is not a brief's choice).
- **Plan:** loader and rule; update fixtures and tests; template and README lines; lint the two real briefs; mutate. Finish check: the done check of the brief.

## Steps

## Completion report

**Behaviour.** Rule `test-sets` reads `tools/agent-workflow/brief_lint_config.json`. A feature, code or review brief must name every `required` set (configuration override and refusal tests; property tests) and every set of one group (`tooling`: `tools/agent-workflow/tests`; `pipeline`: root `tests/`), or carry the unchanged waiver. A missing, empty, malformed or invalid file is a blocking `test-sets` finding `the class test sets cannot be loaded: <precise reason>`, also behind a waiver. `lint_message` has a new optional `class_sets_path` parameter. No other rule changed.

**Commands (project venv python, through `test_slot.py`):**
- `pytest tools/agent-workflow/tests/test_brief_lint.py -q`: first run 1 failed (my test used pattern `x`, which the footer also contains), 159 passed; fixed to a distinctive token. Final whole-suite run below includes it.
- `pytest tools/agent-workflow/tests -q` (no xdist in the venv, so no `-n`): `1 failed, 3230 passed, 4 skipped in 341.07s`. The failure is `test_standing_rules.py::test_the_real_list_is_well_formed_within_budget` (`assert 'tasks/DECISIONS_INDEX.md' in text`, about `.agents/policy/USER_STANDING_RULES.md`); I did not touch that file or that test. After the one-line test fix, `test_brief_lint.py` plus `test_standing_rules.py`: `1 failed, 174 passed in 4.06s` (the same standing-rules failure only); the whole-suite run above predates only that fix.
- Real briefs `brief_lint.py --require-footer --brief`: `.tmp/T-005/brief.md` exit 0 and `.tmp/T-024/brief.md` exit 0, before (baseline) and after.
- Mutants on a scratch copy (9): all killed (empty-text refusal, `all`/`any`, `min`/`max`, waiver bypass, required filter inverted, missing-key check, empty group check, case flag, config error handler).

**not_run.** `ruff check` and `ruff format --check`: ruff is not installed in the project venv and not on PATH (no installs allowed).

**Changed files.** `tools/agent-workflow/brief_lint.py`, `tools/agent-workflow/brief_lint_config.json` (new), `tools/agent-workflow/tests/test_brief_lint.py`, `.agents/policy/AGENT_TASK_TEMPLATE.md` (line 14), `tools/agent-workflow/README.md` (`brief_lint` row), this file.

### Configuration audit

| Value | Source | Validation / refusal | Tests | Class |
|---|---|---|---|---|
| required sets, alternatives groups, labels, patterns | `brief_lint_config.json` | `load_class_sets`: missing or unknown key, wrong type, blank label, invalid regex, empty-matching pattern, empty group | shipped-file test, non-default override test, 18 refusal cases, missing/unreadable file test | configurable |
| waiver pattern `_SETS_WAIVER` (10-character reason) | `brief_lint.py` | unchanged | `test_code_brief_needs_every_class_set_or_a_reasoned_waiver`, waiver-with-config test | protocol-bound |
| `re.IGNORECASE` on set patterns | `brief_lint.py` | none | `test_a_non_default_class_set_file_changes_the_result` (mutant 8) | protocol-bound (Windows path case) |
| `SETS_KINDS` | `brief_lint.py` | unchanged | `test_the_class_sets_are_required_for_feature_code_and_review_only` | protocol-bound |

The patterns are regexes over suite paths and fixed test names in a brief, not natural-language rules (section 5); they live in the configuration, not in source.

## Noticed, not done

- `test_standing_rules.py::test_the_real_list_is_well_formed_within_budget` fails in this base (`tasks/DECISIONS_INDEX.md` not in `USER_STANDING_RULES.md`); outside this scope.
- `ruff` is absent from the project venv, so formatting of the new test lines (several long single-line dict entries) is unchecked; a formatter pass may rewrap them.
- The scout's lines said the template note was at line 12; it was at line 14.

## Completion report: fix round 1 (QA FEEDBACK, Codex FAIL, F1 and F2)

**Root cause per class.**
- **Fail-open validation of the configuration (F1).** `load_class_sets` caught only the exceptions I expected (`re.error`, `OSError`, `ValueError`); the regex compiler also raises `OverflowError` (`x{4294967296}`) and `RecursionError` (600 nested groups), and `json.loads` raises `RecursionError` on deep nesting. Such an exception escaped `lint_message`, and `cli_worker.lint_gate` (`cli_worker.py:463-468`) prints a warning and allows a crashed linter, so the brief went through.
- **Under-constrained requirement (F2).** The required entry `override and refusal tests | configuration (override|refusal) tests` let either word satisfy both categories.

**Audit of the load path** (every call, the exceptions it can raise, and the handling now):

| Call | Can raise | Handling |
|---|---|---|
| `path.read_text(encoding="utf-8-sig")` | `OSError` (absent, directory, denied), `UnicodeDecodeError` (a `ValueError`), `MemoryError` | `ClassSetsConfigError` "cannot read" |
| `json.loads` | `ValueError`, `RecursionError` (deep nesting; test with 5000 levels) | same |
| shape checks (`isinstance`, key sets, `startswith`) | none: JSON keys are strings | explicit refusals |
| `re.compile(pattern, IGNORECASE)` and `pattern.search("")` | `re.error`, `OverflowError`, `RecursionError`, `MemoryError` | `except Exception` becomes `ClassSetsConfigError` naming the type |
| `pattern.search(body)` in `missing_class_sets` | `RecursionError`/`MemoryError` possible, not reproduced | `except Exception` becomes a blocking `test-sets` finding "cannot be applied" |
| `lint_message` | a `ClassSetsConfigError` | finding "cannot be loaded", also behind a waiver |

**Callers.** `cli_worker.lint_gate` blocks on a finding and only warns on a crash (fail open), so the linter must never raise from this rule; I did not change `cli_worker.py` (out of scope, T-024). `gemini-launch.ps1:35` and `gemini-review.ps1:27` run `brief_lint.py` without `--brief` (no `test-sets` rule) and refuse on a non-zero exit, so a crash there is fail closed.

**Alternation audit of `brief_lint_config.json`.** `required` entry 1 (`override and refusal`) was the defect: split into `override(?:\s+and\s+refusal)?\s+tests?` and `(?:override\s+and\s+)?refusal\s+tests?`; the optional part lets the existing phrase "override and refusal tests" (docs, template) satisfy both, while "override tests" or "refusal tests" alone satisfies one. `property\s+tests?` has no alternation. `alternatives`: `tooling` and `pipeline` stay as alternatives because a change is to the tools or to the pipeline and naming either suite is enough; each group has one entry and no alternation.

**Changes.** `brief_lint.py` (loader exception handling; `cannot be applied` finding), `brief_lint_config.json` (split entry; stale function name in the comment), `tests/test_brief_lint.py` (new tests below).

**Commands (venv python, through `test_slot.py`).**
- `pytest tools/agent-workflow/tests/test_brief_lint.py`: `166 passed` (before the last added assertion; it is part of the whole-suite run below).
- Reviewer's `probes.py` (run from a copy, so `.tmp/T-020-review/` is unchanged): `override_only`, `refusal_only` cli=1 BLOCK; `overflow_regex_waived`, `nested_regex_waived` cli=1 BLOCK (before: ALLOW with a crash warning); `valid_tooling`, `valid_pipeline`, `valid_waiver`, `custom_override` ALLOW; the rest as before.
- `pytest tools/agent-workflow/tests -q -p no:cacheprovider` after the last edit: `1 failed, 3236 passed, 4 skipped in 346.10s`; the failure is `test_standing_rules.py::test_the_real_list_is_well_formed_within_budget`, fixed on main in d6005ea, not in this worktree.
- Real briefs `.tmp/T-005/brief.md` and `.tmp/T-024/brief.md` with `--require-footer --brief`: exit 0 both.
- Mutants on the changed lines (8): all killed (compile-exception breadth, empty-text check, read-exception tuple, match-failure handler, type name in the message, override pattern widened, refusal pattern widened, property pattern weakened). The last one first survived; I added the "property values" case, then it was killed.

**Tests added:** `test_a_pattern_the_regex_compiler_rejects_blocks_with_and_without_a_waiver` (OverflowError, RecursionError, re.error; code, review and feature; with and without waiver; through `load_class_sets` and `lint_message`), `test_a_deeply_nested_class_set_file_is_refused`, `test_a_failure_while_matching_blocks_instead_of_escaping`, `test_override_and_refusal_tests_are_two_independent_requirements`.

### Configuration audit (rows added or changed)

| Value | Source | Refusal | Tests | Class |
|---|---|---|---|---|
| required: override tests, refusal tests (two entries) | `brief_lint_config.json` | `load_class_sets` as before | `test_override_and_refusal_tests_are_two_independent_requirements` | configurable |
| pattern that the compiler rejects (any exception type) | file | `ClassSetsConfigError` naming the type, finding also behind a waiver | the parametrized compiler-failure test | safety-bound |

**not_run.** `ruff check` and `ruff format --check` (not installed in the venv, no installs allowed).

**PM decisions pending user review.** None new. One reading: `except Exception` at the compile and match steps is broader than a list of known types, taken because the brief's expected result is that every failure of the configuration blocks.

**Noticed, not done.** A pattern with catastrophic backtracking would hang the lint (stdlib `re` has no timeout); not reproduced, not covered. `cli_worker.lint_gate` allows a crashed linter, which is why this rule converts every failure into a finding.
