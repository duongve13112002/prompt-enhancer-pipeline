# FOLLOWUPS-1: small accepted follow-ups from T-016 and the tooling

Status: built, tested, waiting for review. Kind: code (tests, docs, comments, one tool). R-IDs: R-015, R-020, R-021. Small: yes.

## Step 0

**Unknowns, with the answer taken** (none changes product meaning, so no stop):

1. What marks a pytest base temporary directory when `--basetemp` is explicit? Nothing is written there (pytest `tmpdir.py` `getbasetemp` only removes and creates it). Taken: pytest's own naming of a test's directory (`_mk_tmp`, `tmpdir.py:321-325`: node name, non-word characters to `_`, cut to 30, then a number) for a test function defined in the worktree's own test files. Dropped: "any `.tmp` child" (would treat evidence as test data) and "name starts with `pytest`" (does not match `.tmp/pm/bt`).
2. Does `kb show` print lint? Yes in part, see item 2: the card holds `lint_findings`. Taken: fix the sentence by the code, and the same stale phrase in `docs/execution.md:22`.
3. Why 12 tests instead of 14? Found from `git diff`, item 4.

**Plan:** items 1 to 5 in order; one pytest process at a time through `test_slot.py`, fixed basetemp `.tmp/FOLLOWUPS-1/pytest`; mutants on scratch copies; finish = Done check of the brief.

**Configuration impact (AGENTS.md section 5):** no runtime key, default or literal in `src/` changes. The edits are test code, two docs, four YAML comment blocks, and `tools/agent-workflow/purge_tmp.py`. Its new literals are classified in the audit below.

## Per item: root cause, change, test

### 1. Abbreviated flag accepted by the guide test (confirmed)

Root cause: `build_parser()` uses argparse defaults, so `allow_abbrev` is on and `parse_args` accepts `--snap` for `--snapshot`; `tests/test_run_guide.py` only called `parse`. Confirmed by `test_an_abbreviated_or_unknown_long_option_is_caught...`: `parse(['eval','run',...,'--snap',...])` is not `None`.
Change (`tests/test_run_guide.py`): `misspelled_options(arguments)` descends the real argparse tree to the command's parser and compares every `--option` word with that parser's `option_strings` (exact). A word after an option that takes a value (`nargs != 0`) is a value; `--opt=value` is judged by its name; a bare `--` ends the check; single-dash words are not checked. It is called for every invocation in the guide and in `docs/execution.md`. Runtime parser unchanged. Fenced blocks only (existing `command_lines`), so prose is not read.
Tests: 4 new unit tests (abbreviation and unknown flags caught; valid positionals, `--request --odd`, `--request=--odd`, `--`, `-h -x` pass; a flag of another command fails; prose with `--pla` ignored, the same line fenced caught) plus the two document tests. File: 8 passed.

### 2. K3 lint sentence (confirmed, and the brief's premise is half wrong)

Root cause: `docs/stages/K3-cards.md:25` said the report goes through `kb show` and `kb status`. Code: `kb status` prints row counts only (`cli_commands.py` `STATUS_TABLES`). The separate report is `reports['k3']` in the `kb sync` result under `stages` (`kb_sync.py:244,304`, `k3_cards.py:365`). But the `cards` table is the whole `concept_card.v0` document (`storage/tables.py:108`, columns checked with `Catalog.columns`: `lint_findings` is a required, stored column), so `kb show` does print each card's own `lint_findings` inside `card`. So `docs/execution.md:22` ("no lint: the snapshot tables store none") was wrong too.
Change: K3 sentence now says: report in the `kb sync` result under `stages.k3` (with `cards.lint_report: true`), not stored in the snapshot; `kb status` counts only; `kb show` prints the card's own `lint_findings`. `execution.md:22` corrected to the same facts. Test: `test_kb_show_finds_a_concept...` asserts `card['lint_findings']` is a list (passes: the field is printed). Mutant: dropping the assertion's subject is not meaningful; the assertion is killed if `kb show` stops printing the card field (not run as a mutant, `not_run`: no code changed).

### 3. YAML headers (confirmed)

Root cause: headers written before the loader and commands existed. Change: comment lines only in the four files; each states: built and tested on the CPU with labelled doubles, values are sentinels until fitted, and links `docs/run-guide.md` (the manifest says it is an example, not a deployment profile). `.tmp/FOLLOWUPS-1/yaml_same.py` (scratch, removed): for all four files `yaml.safe_load` of the file equals the HEAD version and the non-comment lines are identical. Configuration tests, one process each, 24 files: all passed (list in the report). No key added, so no new override or refusal test is owed.

### 4. 14 versus 12 tests (confirmed: not lost)

`git diff 199bafe 18706c8 -- tests/test_cli_commands.py` (T-016 P3 commit to the P4 commit): two parametrize rows of `test_commands_this_build_does_not_serve_are_refused_precisely_before_any_side_effect` were removed, `['eval','run']` and `['eval','import']` with owner `T-016 phase 4`. 14 minus 2 is 12, and T-016 P4's report already records 12 (`T-016-P4-eval-runner.md:80,154`). They asserted that those commands are refused, which stopped being true when P4 implemented them. Their behaviour now is covered by `tests/test_eval_cli.py` (`eval run`: 9 tests, e.g. `test_a_run_writes_one_result_per_scenario...`, `test_a_set_the_run_cannot_trust_is_refused_before_any_model_call...`) and `tests/test_eval_judge.py` (`eval import`: `test_an_import_unblinds_flags_every_disagreement...`, `test_other_refused_imports`); the refusal list keeps `eval build`, `eval fit`, `kb sync --only`. Nothing restored, no test added.

### 5. `archive-remove` refuses a pytest basetemp (confirmed by reading the code and by the new tests failing without the change)

Root cause: `_find_protected` walks the worktree and refuses the first directory whose name is protected, wherever it is. A test fixture made `.tmp/pm/bt/<test>/data/eval/runs`, so the worktree stayed (`T-016d`).
Rule (`purge_tmp.py`, `_inside_pytest_tmp`, `_pytest_directory_name`, `_test_names`): a protected-name directory other than `.secrets`, found by `archive-remove` in a worker worktree (a `.git` file, direct child of `.tmp/worktrees`), is test data when, below `<worktree>/.tmp`, some directory on its way (not the first, not itself) is named as pytest's `tmp_path` names the directory of a test function defined in the worktree's own `test_*.py`/`*_test.py` files, and no protected name lies from `.tmp` down to that directory. The name check follows pytest: stem equals the function name, or is a 30-character cut of it, or is the function name + `_` + parameters, followed by a counter without leading zeros (every digit split tried, because a stem can end in digits). Names are read lazily, only when a candidate appears, never from `.tmp`, `.git`, `.venv*`, links or protected trees. Everything else stays refused: `runs/`, `models/`, `.venv`, `.secrets/` at the root or anywhere else, `.tmp/pm-state`, `.tmp/scratch/deeper/runs`, a test-named directory directly under `.tmp` or under `data/`, other entry points (`purge-children`, `purge-older`, `sweep` never use the waiver). Links: `_walk` never enters one, so a junction/symlink named `runs`, or named like the test directory, pointing out of the worktree is removed as a link and its target stays.
Layout check without touching T-016d: the path shape `.tmp/pm/bt/test_the_run_lock_is_held_whil0/data/eval/runs` is accepted against this worktree's test names; the same name at the root or under `scratch` is refused (`layout_check.py`, scratch). The PM runs it on `T-016d`; the rule needs that worktree's own `tests/` to define the test (it does if it is the worktree that ran it).
Tests (`tools/agent-workflow/tests/test_purge_tmp.py`): worktree with models, `.venv`, `cli-jobs`, `pm-archive`, `runs` in pytest directories is removed (return 0); 5 name shapes incl. a 70-character name, parameters with spaces and brackets, a stem ending in digits; a directory really made by pytest for the test itself (not a reimplementation); 14 refusal shapes; root-level protected names beside a pytest directory; `.secrets` never test data; the other three entry points; dry run; two link tests; a property test over generated layouts (root, pytest, other `.tmp` child, behind a link, with or without a file: the worktree goes exactly when no blocking file exists, the file behind the link survives); a property test of the name rule. Whole file: 248 passed, 2 skipped (the two skips are older POSIX-only tests).

## Pre-mortem (decision 169): how each could fail, and the check that rules it out

| Failure | Check |
| --- | --- |
| Option check refuses a valid positional | `kb rollback 1 --config ...` and `kb show 1 ...` in `test_a_valid_command_with_positionals...` |
| ... or a value that starts with `--` | `--request --odd` (value after a value option) and `--request=--odd`; shlex removes quotes, so the value rule is positional, not by quoting. Argparse itself refuses `--request --odd` as a document line, which is why the helper is tested directly |
| Option check reads prose | `test_only_fenced_blocks_are_read...`: `--pla` in prose is ignored, the same text in a fence is caught |
| Config comment edit changes a value or breaks YAML | `yaml_same.py`: data equal and non-comment lines identical for all four files; 24 configuration test files pass |
| A lost test is "restored" although covered | none restored; the removal is shown by `git diff` and the coverage is named |
| Purge deletes real evidence | refusal shapes, root-level and `.secrets` tests, property test with blocking files; mutants P1 to P3, P11, P12 |
| ... follows a link out of the worktree | two link tests and the property's linked entries; the outside file survives |
| ... treats any `.tmp` child as test data | shapes `directly under .tmp`, `ordinary scratch`, `name not from a test`, `name defined only under .tmp` (a planted test file in `.tmp`), `test directory outside .tmp` |

## Completion report

**Behaviour.** Items 1 to 5 as above. Product behaviour is unchanged: no file of `src/` changed.
**Intent check (decision 170):** R-001 (intent unchanged), R-004 (system prompt verbatim), R-009 (retrieval off reads no knowledge base), R-026 (data, not instructions): none touched or weakened; the only product-adjacent edits are two sentences about what `kb show` prints and YAML comments.
**Commands and output** (all through `test_slot.py`, `.venv` python, `PYTHONPATH` this worktree's `src`, `--basetemp .tmp/FOLLOWUPS-1/pytest`, one process each):

| File | Result |
| --- | --- |
| `tests/test_run_guide.py` | 8 passed |
| `tests/test_cli_commands.py` | 12 passed |
| `tests/test_cli_config.py` | 70 passed |
| `tests/test_field_contracts.py` | 532 passed, 34 skipped |
| embed_server_config 160, enhance_serve_config 197, eval_config 211, fix01 60 (+1 skipped), fix02 221, fix03 13, k1_config 558, k2_observe_config 193, k4_config 273, k5_config 52, loader 134, model_client_image_side 36, model_composition 54, model_config 123, model_production_probes 84, profiles 177 (+2 skipped), real_contracts 3, retained_images 70 (+2 skipped), storage_config 10, storage_review_r1 42 | all passed |
| `tools/agent-workflow/tests/test_purge_tmp.py` | 248 passed, 2 skipped |

Basetemp `.tmp/FOLLOWUPS-1/pytest` was 1.8 MB at the end (the loop removed it after each file); removed with the rest of `.tmp/FOLLOWUPS-1/` (43 KB).
**Changed files:** `tests/test_run_guide.py`, `tests/test_cli_commands.py`, `docs/stages/K3-cards.md`, `docs/execution.md`, `configs/enhancer.example.yaml`, `configs/enhancer.dev.example.yaml`, `configs/overlays/deploy-vllm.example.yaml`, `configs/profiles/_example/manifest.yaml`, `tools/agent-workflow/purge_tmp.py`, `tools/agent-workflow/tests/test_purge_tmp.py`, this file.

## Fix round 1 (QA: Gemini Flash PASS, Claude Sonnet PASS with F1 to F3, all on item 5)

All three confirmed by reading the code and by the new tests; only `purge_tmp.py` and its tests changed, nothing in `src/`. Every change narrows what `archive-remove` may delete; nothing new became deletable.

- **F1 (crash), fixed.** `_pytest_directory_name` now requires `number.isascii() and number.isdigit()` (pytest's counter is ASCII). Tests: `'²'` and Arabic-Indic `'٣'` after a test name are refused (shape tests) and `test_a_non_ascii_digit_after_a_test_name_is_refused_and_later_worktrees_are_still_judged` runs two worktrees in one call: the first prints `wt-first: refused, protected tree ...` and stays, the second (a real pytest name) is removed, return 1.
- **Class audit** (`isdigit`, `isnumeric`, `isdecimal`, `int(`, `float(` over `purge_tmp.py`): line 662/664 were the only ones fed by a path component (fixed). Line 120 `float(value)`: a JSON config value already checked `isinstance(int|float)`, finite, positive, so cannot raise. Line 1477 `float(rest[1])`: a command-line argument inside `try/except ValueError` that exits with a message (existing test `test_purge_older_rejects_a_non_numeric_age`). No other conversion.
- **F2 (looser than documented), fixed.** Source re-read in the installed pytest (`.venv/Lib/site-packages/_pytest/tmpdir.py:320-325`, `_mk_tmp`: `name = re.sub(r"[\W]", "_", name)`, `MAXVAL = 30`, `name = name[:MAXVAL]`, then `factory.mktemp(name, numbered=True)`). The rule is now: stem has no non-word character and at most 30 characters; and it is the function name as it is, or exactly 30 characters (a cut) starting with the function name or being a prefix of it, or, below 30, the function name + `_` + parameters + a final `_`, with at least two characters after the name (`f[]` gives `f__`). A second tightening found while writing the property test: `startswith`/`endswith` alone accepted a stem with a space or `-`, which pytest would have turned into `_` (non-word characters now refuse). Negative tests: `test_real_one_2`, `test_real_one_2_x1`, a 32-character stem, a 49-character stem, `test_real_one_a-b_0`, `..a b_1`, `..[x]_2`, three non-ASCII counters; positives: `f_a_b_3`, `f__0`, `f` + 16 characters cut at 30. The test that uses a directory really made by pytest for the test itself stays green.
- **F3 (gaps), fixed.** `.Secrets` and `.SECRETS` inside a pytest directory (secrets test now parametrized); `test_test_names_come_from_test_files_only_and_never_from_a_protected_tree` (test files planted in `runs/`, `.venv/`, `pm-archive/` supply no name; a `*_test.py` file does; a non-test file does not); a generated property `test_property_a_generated_name_is_recognised_exactly_when_pytest_could_have_made_it` (random stems cut anywhere, with and without counters, non-ASCII digits, spaces, `-`, `[]`; a second strategy builds the closed `_..._` shape) compared with `pytest_could_make`, an oracle written with regular expressions and the real rule, not with the code's own string tests, so the negative side cannot be filtered by the code under test.
- **Mutants rerun** (scratch copy, 48 selected tests, 15 mutants): Q2 `.secrets` case-sensitive KILLED; Q3 `_test_names` enters protected trees KILLED; Q4 no `*_test.py` KILLED; R1 `isdigit` without `isascii` KILLED; R2 stem length unbounded KILLED; R3 closing `_` not required KILLED; R4 two-character minimum dropped KILLED; R5 non-word stem accepted **survived the first time** (the random generator rarely builds `_..-.._`), then KILLED after the three explicit refused names and the shape-biased strategy; R6 every stem treated as cut KILLED; R7 leading zero accepted KILLED; R8 exact stem refused KILLED; R9 cut of a long name refused KILLED; R10 no `_` after the function name KILLED; R11 first digit split only KILLED; R12 `archive_remove` without `test_data` KILLED. Q5 (link check in `_test_names`, needs a file symlink; POSIX) and Q6/Q8 (equivalent) are unchanged from the review.
- **Results:** `tools/agent-workflow/tests/test_purge_tmp.py`: 259 passed, 2 skipped (was 248 + 2); basetemp `.tmp/FOLLOWUPS-1/pytest` 1.9 MB, removed after the run. The rule was never run on a real worktree.
- **Not done:** memoizing `_test_names` per `Gate` (optional): a gate sees one worktree per call and reads names only when a candidate appears, so the gain is small; left under Noticed.

## Configuration audit

| Row | Finding |
| --- | --- |
| New user or deployment value | none; no key added to any schema, profile or example (YAML data identical) |
| Hidden default / ignored setting | none in `src/`; the tool has no configuration file entry for the new rule: it is a safety rule, not a user choice |
| `PYTEST_NAME_LIMIT = 30` | protocol-bound (pytest `tmpdir.py` `MAXVAL`; the test reads the installed pytest's rule and a real `tmp_path` name) |
| `TEST_FILE_PATTERNS`, `TEST_FUNCTION` | protocol-bound (pytest's default collection); regular expression over code, not natural language |
| Test-file read bound | reuses `MAX_REPORT_BYTES`, safety-bound |
| Test literals (`LONG_TEST`, names, sizes) | test-only |
| Override and refusal tests | not owed (no key); the refusal side of the new rule is the 14 shapes and the root-level tests |

## Mutants

Item 1 (scratch copy, test helper): A1 to A8 killed after one strengthening (A6, single-dash words judged too, survived until `-x` was added to the valid test; rerun killed). Two document mutants killed: `--snap` for `--snapshot` (G1) and `--over` for `--overlay` on `kb status` (G2) in a scratch copy of the guide.
Item 5 (scratch copy of `purge_tmp.py` and its tests, 15 mutants, the 37 new tests): P1 `.tmp` child as base, P2 protected name on the way ignored, P3 `.secrets` waived, P4 leading zero accepted, P5 cut length not required, P6 no underscore after the function name, P7 any directory is a worker worktree, P8 `archive_remove` without `test_data`, P9 exact stem refused, P10 long cut name refused, P11 test files under `.tmp` counted, P12 paths outside `.tmp` judged, P13 flag not passed to the walk, P14 gate drops the flag, P15 only the first digit split: **all 15 killed**. Items 2 and 3: no executable change, so no mutant (`not_run`).

## not_run

- Lint and `ruff format --check`: ruff not installed (`not_run`).
- The purge rule on the real `T-016d`: left to the PM as the brief says; only the path shape was checked on pure functions.
- POSIX run of the purge tests (Windows only here); the symlink variant of the link tests is written but not run on POSIX.
- The full pipeline suite and the full tool suite (not asked).
- A mutant for item 2 (docs and one assertion).

## Noticed, not done

- `kb/_example/kb_overrides.yaml:2` still says "Documentation only; nothing is implemented" (not one of the four files named).
- `PROTECTED_NAMES` holds `.venv` only, compared exactly, so `.venv-gpu` is not a protected name for `purge_tmp.py`; the brief says `.venv*`. Not changed.
- `_archive_reports` copies `*.json`/`*.md`/`*.log` from a basetemp like `.tmp/pm/bt` (it only skips names starting `pytest`/`tests`/`mypy`), so archiving T-016d will copy test-fixture JSON into `pm-archive`; not changed (size-capped per file).
- `_test_names` runs once per `_find_protected` call that meets a candidate (not memoized per `Gate`); a multi-worktree call rescans each worktree's tests once.
- `Q5` (a test file that is a symlink is skipped) is untested here: a file symlink needs privilege on Windows; POSIX `not_run`.
- A pytest directory made by `tmp_path_factory.mktemp("custom")` (a name not derived from a test function) is not recognised, so a protected name inside it still refuses; conservative on purpose.
