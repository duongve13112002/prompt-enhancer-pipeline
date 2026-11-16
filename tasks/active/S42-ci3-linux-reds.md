# S42-ci3: Linux-only CI failures of CI window 3

Kind: code. R-IDs: R-014, R-015, R-025. Owner: the PM (S42, decision 72). Base: `b2273d4`. Builder: Claude Code, Opus 5.5, effort high.

## Goal

Close the three POSIX-only test failures of CI window 3 (run 43764148659) at their root, and audit the POSIX-only branches and tests in `src/prompt_enhancer/storage/` and `tools/agent-workflow/purge_tmp.py`. The dispatch lists the three failures and the rules.

## Step 0 (pre-approved by the dispatch)

1. **Unknowns and failure modes:** no Linux on this machine, so every POSIX conclusion rests on reading CPython 3.12 source (`Lib/os.py`, `Lib/shutil.py`) and the CI log; a fix that only looks right on Windows would come back red in the next window. A fix to a test could hide a real code defect (S31). A test that patches a module global through the shared `monkeypatch` fixture can be undone by the test itself.
2. **Questions (proposed answers, none blocking):**
   1. L1: is the capability check fooled by function identity? Proposed: yes; capture the checked functions at import, keep reading the live `os.supports_*` sets so the refusal tests still work.
   2. L2: is "path rule first, then no-follow open" the right order? Proposed: yes, documented at `_anchored.py:5`; change the test to cover both layers.
   3. L3: can a production path reach a link's target? Proposed: `_remove_link` cannot (`unlink(2)` never follows), but `_on_error` chmods through a link; fix it.
3. **Plan:** (a) root causes with evidence; (b) L1 code fix plus test; (c) L2 test split; (d) L3 test fix plus `_on_error` fix and cross-platform tests of every removal path; (e) class audit; (f) mutants, focused tests, both suites. Finish checks map one-to-one to the dispatch's done check.

### Configuration impact

None. No profile key, setting or default is added, read or changed: the edits are a platform capability check (`_anchored._posix_available`), the rmtree error handler of a PM tool (`purge_tmp._on_error`) and tests. The only literal touched is `stat.S_IWRITE` (protocol-bound: the Windows read-only attribute), which now applies on Windows only.

## Steps

1. Read the CI log, `_anchored.py`, `_fs.py` (`real_inside`, `FileLock`), `config._resolve_within`, `purge_tmp.py` (`_is_link`, `_on_error`, `_remove_link`, `_remove`), the three tests and their fixtures, and CPython 3.12 `Lib/os.py` and `Lib/shutil.py`.
   All three root causes were found before any edit (below). L3 was a test defect, but reading `_on_error` turned up a real POSIX defect next to it.
2. L1: `_anchored._posix_available` checks the platform functions captured at import (`_anchored.py:193-205`). New test `test_storage_anchored_writes.py:526` runs on Windows too.
   `test_profiles.py::test_register_publishes_without_hard_links` is unchanged: it already forces the fallback the way production meets it, through a `linkat` failure (EPERM).
3. L2: the code's order is right and documented (`_anchored.py:5-7`), so only the test changed (`test_storage_anchored_writes.py:543`). It now covers both layers.
   The path rule refuses an outside link (`resolved outside project root`) and an inside link (`is a link or another spelling`). The `O_NOFOLLOW` open alone refuses both once the path rule is bypassed (the race).
4. L3: the test now uses `monkeypatch.context()` and a real directory link on both platforms. `_on_error` (`purge_tmp.py:187-203`) no longer chmods through a link, and it retries only a removal.
   Two new cross-platform tests (`test_purge_tmp.py:615`, `:652`) cover every removal path and check the target's content and mode.
5. Mutants (5, all killed on Windows), focused tests, both whole suites (results below).

## Root causes

### L1: `test_register_publishes_without_hard_links` refused at start (code defect, plus a fragile check)

- **Cause (source-verified):** `_posix_available()` checked `os.link in os.supports_dir_fd` and `os.link in os.supports_follow_symlinks` at call time (`_anchored.py:193-197` at `b2273d4`). These sets hold the function objects `os` defined at import: CPython 3.12 `Lib/os.py:104-106` `def _add(str, fn): if (fn in _globals) and (str in _have_functions): _set.add(_globals[fn])`, then `_add("HAVE_LINKAT", "link")` (`:114`, `:176`).
  The test replaces `_anchored.os.link`, which is the global `os.link`. The double was then not in the set, so `_PosixDirectory.__init__` refused (`_anchored.py:215`, CI log line 113). Any wrapper around `os.link` (instrumentation, a debugger, a test double) would have shut every store write on POSIX.
- **Fix:** capture the seven checked functions at import (`_DIR_FD_FUNCTIONS`, `_NO_FOLLOW_FUNCTIONS`). The check still reads the live `os.supports_*` sets, so the three refusal tests that empty `os.supports_dir_fd` keep refusing.
- **Test:** the forced-fallback test was right. Its double raises `OSError(EPERM)`, which linkat(2) documents for "The filesystem containing oldpath and newpath does not support the creation of hard links" (man-pages linkat(2), quoted from memory, not re-fetched: assumption). `publish` is the only `os.link` caller in `src/` (grep).
  New `test_the_capability_check_is_not_fooled_by_a_replaced_link_function` runs on Windows as well: it fakes the POSIX flags and sets. It kills mutants M1 and M2.
- **Linux outcome:** assumption until the next CI window. By reading: `_posix_available()` is now True under the double, `publish` reaches the `OSError` branch, and the exclusive no-follow create publishes the file.

### L2: `test_posix_lock_file_refuses_a_symlink_leaf` refused with another message (test defect)

- **Order (source-verified):** `open_lock_file` calls `_checked` → `real_inside` (`_anchored.py:42-45`, `:130`) before any descriptor is opened. For a symlink leaf, `config._resolve_within` reads the link (`config.py:126`) and refuses a target outside the root with `resolved outside project root` (`config.py:150-151`). The store message is `StorageError(str(error))`, `_fs.py:100-101`.
  This order is documented: "Each operation checks the name by the path rule first (`real_inside`), then opens … without following a link" (`_anchored.py:5-7`).
- **Safety gap?** No. Either layer refuses, and the `O_NOFOLLOW` open (`_anchored.py:307-314`) is the one that holds against a link swapped in after the path rule. Neither order follows the link.
  The message names the reason of the layer that refused: the link leaves the root. The sibling POSIX tests (`:551`, `:565`; `test_storage_anchored.py:202`) already bypass `real_inside` with a labelled double to reach the anchored layer. This test forgot to.
- **Fix:** the test checks the path-rule refusal for an outside and an inside link, then the no-follow refusal for both with the path rule bypassed. Both targets keep their bytes and both links remain.

### L3: `test_remove_link_reports_success_only_when_the_entry_is_really_gone` (test defect, plus one production defect next to it)

- **Cause of the red (source-verified, CI log lines 280-281):** the `pt` fixture patches `PROJECT` through the test's own function-scoped `monkeypatch` (`test_purge_tmp.py:30-32`). The test's `monkeypatch.undo()` then also undid that patch and the `delenv`.
  After it, `pt.PROJECT` was the real checkout (`/home/runner/work/...`, log line 281). The assertion looked for `<checkout>/.tmp/stuck-target`, which never existed; the real target under `tmp_path/proj/.tmp` was never touched.
  On Windows the assertion short-circuited on `os.name == "nt"`, so it never ran there.
- **`_remove_link` cannot reach a target (source-verified; the man-page quotes are from memory, an assumption):** POSIX runs only `os.unlink(path)` (`purge_tmp.py`, `_remove_link`), and unlink(2) removes the name: "If the name referred to a symbolic link, the link is removed" (man-pages unlink(2)).
  `_remove` checks `_is_link` (lstat) before `rmtree`. Linux `rmtree` is the fd-based variant (`shutil.py:705-708`, `_use_fd_functions`), which unlinks link entries by `dir_fd` (`:698`) and refuses a swapped top-level link through the lstat/fstat `samestat` check (`:668-680`).
- **Production defect found (source-verified, P0 class: a link's target reached through the link, POSIX):** `_on_error` ran `os.chmod(path, stat.S_IWRITE)` on every path rmtree reported, then `func(path)`. `os.chmod` follows links.
  - When the unlink of a link entry fails (`shutil.py:697-700`; for example EACCES from a parent directory without write permission), the hook set the target's mode to `0o200`. That target can be outside the project: its owner loses read access.
  - When a directory is swapped for a link during the walk, rmtree reports `os.path.islink` (`:676-680`), and the hook chmodded the link's target directory the same way.
  - When rmtree reports `os.open` (`:673-674`), `func(path)` is `os.open(path)`, a `TypeError` that the hook's `except OSError` does not catch, so the purge aborts.
  - On POSIX the chmod never helps: unlink(2) EACCES means "Write access to the directory containing pathname is not allowed" (from memory, assumption). The entry's own mode never matters.
- **Fix (makes the existing policy hold; no policy change):** the chmod runs on Windows only and never on a link. Only `os.unlink`, `os.remove` and `os.rmdir` are retried; any other report is printed as "could not remove".
- **Tests:**
  - L3 is rewritten with `monkeypatch.context()` and a real directory link on both platforms (junction or symlink). It asserts the target's content and mode.
  - `test_no_removal_path_reaches_a_link_target[link|tree|tree-refused]` covers the link itself, a tree that holds it, and a tree whose link entry refuses removal, with a recording chmod double.
  - `test_rmtree_error_hook_never_acts_through_a_link_or_crashes[open|scandir|islink|lstat]` covers rmtree's non-removal reports.
  - On POSIX the old hook fails the mode assertion. On Windows it fails the chmod-on-a-link assertion (M3, M4) and the `os.open` case (M5).

## Class audit: POSIX-only branches and tests

The only CI evidence on POSIX is run 43764148659 (`b2273d4`): pipeline `2 failed, 2885 passed, 55 skipped` (log line 149), tools `1 failed, 5658 passed, 277 skipped` (line 292). I found no earlier Ubuntu run in `tasks/STATUS.md` or `docs/ci.md` (grep), so "passed on CI" below means that run only.

| # | Branch or test | Checked on POSIX | Holds | Action |
|---|---|---|---|---|
| B1 | `_anchored._posix_available` | CI run, red (L1) | no: function identity | fixed (captured functions) |
| B2 | `_PosixDirectory.__init__`: root and components with `O_DIRECTORY\|O_NOFOLLOW`, `mkdirat` | every store write on CI, plus the `Swap` link tests (symlinks on POSIX), passed | yes | none |
| B3 | `_open_directory` ELOOP/EMLINK/ENOTDIR → refusal | `Swap` tests passed; open(2): O_NOFOLLOW gives ELOOP | yes | none |
| B4 | `_PosixDirectory._lstat` / `remove` | `test_remove_entry_removes_a_symlink_leaf_never_its_target` passed | yes | none |
| B5 | `create_temporary` `O_EXCL\|O_NOFOLLOW` | every write, passed | yes | none |
| B6 | `commit` renameat + directory fsync | `test_posix_write_replaces_a_symlink_leaf_as_a_name` passed | yes | none |
| B7 | `publish` linkat, then the exclusive-create fallback | linkat: `[supported]` passed; fallback: `[unsupported]` passed (double set after the check), forced test red only through B1 | yes after B1 | B1 fix; Linux pending CI |
| B8 | `rename` lstat + exists check | `test_posix_rename_refuses_a_symlink_source` passed | yes | none |
| B9 | `open_lock` ELOOP → `is a link`; non-regular → `expected a file` | L2 red (test); `expected a file` untested | yes (order documented) | L2 test fixed; see Noticed |
| B10 | `_fs.FileLock` `fcntl.flock` acquire and release | `test_k0_review_fix` flock-interrupt test and the store concurrency tests passed | yes | none |
| T1 | `test_storage_anchored.py::test_remove_entry_refuses_without_a_dir_fd_primitive` | passed | yes, also after B1 (empties the live set) | none |
| T2 | `test_storage_anchored.py::test_remove_entry_removes_a_symlink_leaf_never_its_target` | passed | yes | none |
| T3 | `test_storage_anchored_writes.py::test_posix_writes_refuse_without_a_dir_fd_primitive` | passed | yes, also after B1 | none |
| T4 | `test_storage_anchored_writes.py::test_posix_lock_file_refuses_a_symlink_leaf` | red (L2) | no: missed the path rule | fixed |
| T5 | `test_storage_anchored_writes.py::test_posix_write_replaces_a_symlink_leaf_as_a_name` | passed | yes | none |
| T6 | `test_storage_anchored_writes.py::test_posix_rename_refuses_a_symlink_source` | passed | yes | none |
| T7 | `test_profiles.py::test_register_never_replaces_an_interposed_manifest[unsupported]` | passed | yes | none |
| T8 | `test_profiles.py::test_register_publishes_without_hard_links` | red (L1) | the test, yes; the code, no | code fixed (B1) |
| T9 | POSIX branches of the test helpers `_dir_link`, `_drop_link`, `_is_link` (`test_storage_anchored.py:34-53`) and `_outside_link` (`test_storage_properties.py:51-60`) | passed through every test using them | yes | none |
| P1 | `purge_tmp._remove_link` POSIX (unlink only) | L3 red (test) | yes (unlink(2)) | test fixed |
| P2 | `purge_tmp._is_link` (`S_ISLNK`) | the purge suite on CI | yes | none |
| P3 | `purge_tmp._on_error` chmod and retry | never (no POSIX test reached it) | no: chmod through a link; TypeError on `os.open` | fixed, plus 2 tests |
| P4 | `purge_tmp._remove` → fd-based `rmtree` on Linux | CI, but no link test on POSIX | yes after P3 | new `[tree]` and `[tree-refused]` tests |
| P5 | `test_purge_tmp.py` L3 (the only POSIX branch in that file); 13 link tests are Windows-only (`skipif(os.name != "nt")`, plus `:222`) | L3 red | no (fixture undo) | fixed; the 13 Windows-only tests are listed in Noticed |

## Configuration audit

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| POSIX anchored-primitive capability (`_DIR_FD_FUNCTIONS`, `_NO_FOLLOW_FUNCTIONS`) | none: a platform fact, not a choice | the store refuses at start without it (`_anchored.py:220-221`) | n/a | `test_the_capability_check_is_not_fooled_by_a_replaced_link_function` (accepts, and refuses without `link`); T1, T3 refusals | protocol-bound (POSIX `*at` calls) |
| `stat.S_IWRITE` in `_on_error`, Windows only | none: the Windows read-only attribute | n/a | n/a | `test_no_removal_path_reaches_a_link_target`, `test_rmtree_error_hook_never_acts_through_a_link_or_crashes` | protocol-bound |
| retried removal functions `os.unlink`, `os.remove`, `os.rmdir` | none: the removal calls rmtree reports | n/a | n/a | `test_rmtree_error_hook_never_acts_through_a_link_or_crashes` | protocol-bound |

No profile key was added, read or changed, so no override or refusal test of configuration applies. The property tests of parsers and validators do not apply beyond the existing storage path properties (`tests/test_storage_properties.py`), which this change does not touch.

## Research evidence

| Question | Source | Read raw or summary | Finding | Class |
|---|---|---|---|---|
| What do `os.supports_dir_fd` / `supports_follow_symlinks` hold? | CPython 3.12.10 `Lib/os.py:104-125`, `:176-182` | raw | the function objects of `os` at import | source-verified |
| How does fd-based `rmtree` treat links and report errors? | CPython 3.12.10 `Lib/shutil.py:642-708` | raw | link entries unlinked by `dir_fd`; a swapped top-level link reported as `os.path.islink`; `os.open`, `os.scandir`, `os.unlink`, `os.rmdir` passed to the hook | source-verified (Linux uses this branch; Windows uses `_rmtree_unsafe`, `:619-639`) |
| Does unlink follow a link? Which mode governs EACCES? | man-pages unlink(2) | summary from memory, not re-fetched (no network) | removes the link itself; EACCES is the containing directory's write permission | assumption (well-known POSIX) |
| linkat errno on a file system without hard links | man-pages linkat(2) | summary from memory, not re-fetched | EPERM | assumption |
| open with O_NOFOLLOW on a symlink leaf | man-pages open(2) | summary from memory, not re-fetched | ELOOP | assumption; covered by passing T2, T6 on CI |
| Which check refuses a symlink leaf first? | `_anchored.py:5-7`, `:42-45`; `config.py:115-154` | raw | the path rule, then the no-follow open | source-verified |

## Completion report

- **Work completed:** root causes of L1, L2 and L3 found before any edit; L1 fixed in code (fragile capability check); L2 and L3 fixed in tests; a POSIX link-target defect in `purge_tmp._on_error` found and fixed; class audit of 24 rows; 5 mutants; focused tests and both whole suites.
- **Result and evidence (Windows dev machine; Linux is assumption until the next CI window):**
  - Focused (`test_purge_tmp.py`, `test_storage_anchored_writes.py`, `test_storage_anchored.py`, `test_profiles.py`, through `test_slot.py`): `474 passed, 8 skipped in 70.42s`.
  - Pipeline suite `tests/` (`test_slot.py -- python -X faulthandler -m pytest -q -p no:cacheprovider tests`): `2901 passed, 42 skipped, 4 warnings in 579.61s (0:09:39)` (`.tmp/S42-ci3/pipeline-suite.log`).
  - Tool suite `tools/agent-workflow/tests/`: `5931 passed, 12 skipped in 887.83s (0:14:47)` (`.tmp/S42-ci3/tools-suite.log`).
  - Mutants (`.tmp/S42-ci3/mutants-report.txt`, scratch copy; baseline `9 passed`): M1 live `os.link` identity KILLED; M2 `link` dropped from the no-follow set KILLED; M3 chmod unconditional KILLED; M4 chmod on links too KILLED; M5 retry any reported function KILLED. No survivors.
  - The L2 test and the POSIX branches of the new purge tests run only on Linux: their mutants are `not_run` here (no Linux, no WSL, installs forbidden).
- **Important changes and reasons:**
  - `src/prompt_enhancer/storage/_anchored.py:193-203`: the capability check uses functions captured at import, so a replaced `os.link` no longer shuts every POSIX store write.
  - `tools/agent-workflow/purge_tmp.py:187-203`: `_on_error` chmods on Windows only and never on a link, and retries only a removal. Before, POSIX could chmod a link's target to `0o200` and crash on an `os.open` report.
  - `tests/test_storage_anchored_writes.py:526-569`: new capability test (runs on Windows); the symlink-leaf test covers both layers in their documented order.
  - `tools/agent-workflow/tests/test_purge_tmp.py:567-660`: `dir_link` helper (`CREATE_NO_WINDOW`); L3 without `monkeypatch.undo()`; two new cross-platform tests over every removal path, asserting the target's content and mode.
- **Problems, risks and remaining work:**
  - `not_run`: `ruff check` and `ruff format --check` (ruff is not installed in `.venv`: `No module named ruff`; installs forbidden). Formatting was done by hand to the 120-column style.
  - Linux execution of every changed test is pending the next CI window.

### Noticed, not done

- 13 `test_purge_tmp.py` link tests are Windows-only (`skipif(os.name != "nt")`, plus the skip at `:222`). With `dir_link` most could run on POSIX symlinks too: the sweep, prune and archive paths with links have no POSIX test.
- The `junction` helper in `test_purge_tmp.py:162` starts `cmd` without `CREATE_NO_WINDOW` (footer rule 7).
- `_archive_reports` (`purge_tmp.py`, `shutil.copy2(source, destination)`) follows a link already present at the destination under `pm-archive` (both platforms; not POSIX-only).
- `_PosixDirectory.open_lock`'s `expected a file` branch has no test. A directory at the lock name raises `IsADirectoryError` before `fstat`, and `FileLock.try_acquire` lets it through (it catches only `PermissionError`). A crash, not a safety gap.
- The pipeline suite shows Pillow's deprecation of saving `I`-mode PNGs (`model_image.py:162`, removed in Pillow 13).

## Handoff

- **Current status:** done on Windows; Linux pending CI.
- **What was already done:** all of the above.
- **What the next agent must do:** the PM runs the next CI window and checks the three tests and the two new purge tests on `ubuntu-latest`.
- **What must not be repeated:** re-deriving L1-L3. The class audit table is the record.
- **Worktree, base and partial patch:** `<project>/.tmp/worktrees/S42-ci3`, base `b2273d4`; uncommitted changes in 4 tracked files plus this task file.
- **Decisions and notes for the PM to merge:**
  - `docs/implementation-notes.md`: "A platform capability check over `os.supports_*` compares captured function objects, never live `os.<name>` lookups (S42-ci3 L1)."
  - "`purge_tmp`'s rmtree hook never chmods on POSIX or through a link, and retries removals only (S42-ci3 L3)."
  - Lesson candidate: "a test that calls `monkeypatch.undo()` also undoes its fixtures' patches; use `monkeypatch.context()`."

## Fix round 1

Input: the Sonnet review `.tmp/S42-ci3/review/review.md` (P2-1, P2-2, P3-1 to P3-4, mutants A to E, noticed items) and the PM's decisions in the fix-round dispatch (pending user review). Builder: Claude Code, Opus 5.5, effort high.

### Step 0 (pre-approved by the dispatch)

1. **Unknowns and failure modes:** no Linux here, so the POSIX arms rest on reading CPython 3.12.10 `Lib/os.py` and `Lib/shutil.py`. A fix that checks a path and then acts on the same string leaves the window open, which is the finding itself. Windows share-mode claims need a measurement, not a recollection.
2. **Questions (proposed answers, none blocking):**
   1. Does the fix stop at the hook? Proposed: no. CPython 3.12.10 Windows `rmtree` is `_rmtree_unsafe` (`shutil.py:619-639`). It walks by path string through `os.walk(topdown=False)`, which queues a directory path at listing time (`os.py:416-417`) and scans it later by name (`os.py:366`). A directory swapped for a junction in between is entered, and every file behind it is unlinked. That is the P2-1 class, wider than the hook, so the class audit has to fix it too.
   2. Capability mechanism: import-time capture or a name check? Proposed: the name check (reasons and evidence under "Capability check").
   3. The leaf chmod: `os.chmod` by path, or an attribute change on the entry's own handle? Proposed: the handle, because a leaf swapped after a check is then never followed.
   4. `_archive_reports` meets a link on the destination's way: refuse the worktree, or crash? Proposed: refuse that worktree (`refused, report not archived`). The policy stays the same: archive first, then delete, and never delete what was not archived. Before, a copy error crashed the command.
3. **Plan:** (a) class audit table (below, before any edit); (b) shared name-based capability check in `_anchored` and `k0_scan`, plus literal tests; (c) `purge_tmp`: every act below `.tmp` runs inside a verified, held chain of directories (POSIX descriptors with `dir_fd`; Windows handles without `FILE_SHARE_DELETE`), with a Windows tree walker in place of `rmtree`, a hook that only reports on POSIX, and an archive copy that never writes through a link; (d) the POSIX store root opened component by component; (e) tests, mutants, focused tests, both suites, report. Each item maps to one line of the done check.

### Class audit: acts on a path string after a check (`purge_tmp.py`, `src/prompt_enhancer/storage/`)

"Swap" means an ancestor directory or the leaf replaced by a link or junction between the check and the act. Lines are at the first-round tree.

| # | Site | Act | Checked before | Redirectable by a swapped ancestor | By a swapped leaf | Action |
|---|---|---|---|---|---|---|
| A1 | `purge_tmp._on_error` (`:195-198`) | `os.chmod`, then `func(path)` by path | `_is_link(path)`, leaf only | yes (review probe2: an outside read-only file deleted) | Windows: chmod follows a link swapped after `_is_link` | fixed: POSIX reports only; Windows retries only while every directory from the root down is held, and clears the attribute on the entry's own handle |
| A2 | `purge_tmp._remove` → `shutil.rmtree` on Windows (`:492`) | `_rmtree_unsafe`: `os.walk` by path, `os.unlink`/`os.rmdir` by path | `_is_link(path)` on the top only | yes: a directory queued by `os.walk` and swapped for a junction before its scan is entered (`os.py:366`, `:416-417`), and its files are unlinked | no: unlink/rmdir remove a junction itself | fixed: `_remove_tree_nt` holds every directory from `.tmp` down to the one it empties |
| A3 | `purge_tmp._remove` → `shutil.rmtree` on POSIX (`:492`) | fd-based `_rmtree_safe_fd` below the top; the top by path (`shutil.py:669`, `:674`) | the Gate's `_path_problem`, then `_is_link` | yes, for the top's ancestors in the window between the Gate and rmtree's `lstat` | no: lstat/open/fstat `samestat` (`:678`) | fixed: the top's parent chain is opened from `.tmp` with `O_DIRECTORY\|O_NOFOLLOW`, and `rmtree(name, dir_fd=parent)` is used |
| A4 | `purge_tmp._remove_link` (`:477-484`) | `os.rmdir`/`os.unlink` by path | the Gate and `_is_link` | yes, top level (same window as A3) | no (unlink/rmdir never follow the final link) | fixed: runs inside the held chain (POSIX `dir_fd`) |
| A5 | `purge_tmp._remove` file branch (`:494-495`) | `os.remove` by path | the Gate | yes (same window) | no | fixed: inside the held chain; Windows clears a read-only attribute on the handle and retries (review "Noticed") |
| A6 | `purge_tmp._archive_reports` destination (`:523-525`) | `mkdir(parents=True)`, then `shutil.copy2` writes through `destination` | none | yes: a link under `pm-archive` sends the copy outside | yes: copy2 opens and truncates a link's target | fixed: each directory is created and held without following a link; the copy goes to a new exclusive temporary and is renamed over the name, so a leaf link is replaced as a name; a link on the way refuses that worktree |
| A7 | `purge_tmp._archive_reports` source (`:516-525`) | `stat()`, then copy2 opens `source` | `_is_link(source)`, leaf only; `_walk` skips link directories at listing time | yes: a directory swapped after listing sends the read elsewhere, for example into a `.secrets` tree, which the module promises never to read | yes: the read follows a link swapped after `_is_link` | fixed: the source is opened without following a link, inside a held chain, and its size is checked on the descriptor |
| A8 | `purge_tmp` checks (`_walk`, `_size`, `_find_protected`, `_is_placeholder`, `_protected_paths`, `_busy_conflict`) | reads of names and metadata, manifests read by path | n/a | they decide, they do not act | n/a | not changed: the acts above now hold their chain; a protected tree moved into a target after the Gate is a content race, not a link redirect (Noticed) |
| S1 | `_anchored._PosixDirectory.__init__`: root opened by full path (`:225`) | `os.open(str(root), O_DIRECTORY\|O_NOFOLLOW)` | `real_inside` (the path rule) | yes: `O_NOFOLLOW` covers the last component only; Windows verifies the root handle's final path (`_verify`, `:485`), POSIX did not | no (`O_NOFOLLOW`) | fixed: every component from `/` is opened relative to its parent with `O_NOFOLLOW` (`O_PATH` above the root, so a directory without read permission still works) |
| S2 | `_anchored` POSIX components, `*at` acts (`:226-319`) | `mkdirat`, `openat`, `renameat`, `unlinkat`, `linkat` on held descriptors | descriptors | no | no (`O_NOFOLLOW`, `AT_SYMLINK_NOFOLLOW`, rename/unlink never follow) | none |
| S3 | `_anchored` Windows (`:525-617`) | `NtCreateFile` relative to held handles, delete or rename on the handle | `_verify` (attribute plus final path) | no | no (`FILE_OPEN_REPARSE_POINT`) | none |
| S4 | `_fs.sha256_file`, `_fs.read_table_file`, `store.py` reads (`read_bytes`, `read_table_file`, `os.listdir`) | read by path | `real_inside` / `Store.path` | yes: a read only. It returns other bytes and writes nothing; the store's write contract (`_anchored.py:1-3`) is about changes | yes, read only | not changed: anchored reads would redesign the store's read path, which is out of this round's scope (Noticed, for the PM) |
| S5 | `_fs.FileLock` (`:100-101`) | opens only through `open_lock_file` (anchored) | n/a | no | no | none |
| C1 | Capability checks `_anchored._posix_available`, `k0_scan:66`, `:113`, `:143` (review P3-3, P3-4) | not a path act: same class as L1 (live identity) | n/a | n/a | n/a | fixed: one name-based check, `prompt_enhancer._os_support.os_supports` |

### Configuration impact (fix round 1)

None. No profile key, setting or default is added, read or changed. The new literals are platform facts:

- the Windows constants in `purge_tmp.py` (`_WIN_*`: winnt.h, fileapi.h, minwinbase.h), protocol-bound;
- the primitive names in `_anchored._DIR_FD_NAMES` / `_NO_FOLLOW_NAMES` and in the three `k0_scan` checks (the `os` function names the code calls), protocol-bound;
- the archive temporary's form `.<name>.<uuid>.tmp`, artifact-bound (it lives only between create and rename).

### Capability check: name-based, not import-time capture (review P3-3)

- **Evidence (source-verified, CPython 3.12.10 `Lib/os.py:104-106`, `:112-136`):** `_add(str, fn)` puts `_globals[fn]`, the native function `os` bound by `from posix import *`, into the set. `importlib.reload(os)` re-runs the module and rebuilds the sets from the same native module.
- **Import-time capture fails one case:** a module imported while a primitive is already wrapped captures the wrapper, and every store write refuses at start. The new subprocess test shows the first-round code returns `False` there (`FIRST-ROUND storage+k0` in the mutants report).
- **The name check survives every case the dispatch names:** a double installed before or after import, and a reload. It still refuses a primitive the platform lacks: the name is absent from the set (one test per primitive). The native names equal the attribute names (experiment on this host: `{'open': 'open', ..., 'link': 'link', 'listdir': 'listdir'}`). Linux builds `posix` from the same `Modules/posixmodule.c` method table as `nt`, so the names match there too (source-verified by reasoning, not run: assumption until CI).
- **Narrower design dropped:** keeping identity and only adding a reload test. It leaves the before-import case open.
- **Accepted residual (as the review noted):** the check asks "does the platform have `linkat`", not "does the current `os.link` accept `dir_fd`". A wrapper without `dir_fd` fails at the call with `TypeError`, not at start.

### Check-then-act windows that remain

- **`purge_tmp` on POSIX:** none below `.tmp`. Every act uses `dir_fd` on descriptors opened with `O_NOFOLLOW` (source-verified: `shutil.py:669-674` and `:698` use `dir_fd` throughout).
- **`purge_tmp` on Windows:** none from `.tmp` down. Each directory is held without `FILE_SHARE_DELETE` (rename refused, measured: `test_a_held_way_cannot_be_renamed_or_replaced_while_the_act_runs`), and the leaf's attribute changes on its own handle.
- **What remains on Windows:** turning a held, empty directory into a junction in place (`FSCTL_SET_REPARSE_POINT`). That needs a process that empties a directory the purge is walking and converts it within microseconds. Such a same-user process could delete the victim directly, so this is accepted (assumption: NTFS refuses a reparse point on a non-empty directory; not measured).
- **Above `.tmp` (`<project>`):** not held, which matches the Gate's own scope.
- **`_anchored`:** none on POSIX after S1. Windows was already closed by `_verify`.

### Completion report (fix round 1)

- **Work completed:**
  - Review findings: P2-1, P2-2, P3-1 to P3-4 and the noticed items. The `junction()` helper now uses `CREATE_NO_WINDOW`; `_archive_reports` never writes through a link; a top-level read-only file is removed on Windows.
  - Class audit (A1-A8, S1-S5, C1): every redirectable act is fixed, except the read-only store reads (S4). S4 is listed for the PM.
  - 10 mutants, the first-round regression run, focused tests and both whole suites.
- **Result and evidence (Windows; Linux is assumption until the next CI window):**
  - First-round tree with the new tests (`.tmp/S42-ci3/fix1/mutants-report.txt`):
    - purge: `7 failed, 1 skipped`. The skip is `leaf-file`: no symlink privilege on Windows.
    - storage and k0: `1 failed` and `1 failed`.
    - The current tree passes the same selections (`BASELINE` lines).
  - Mutants: 10 of 10 killed on Windows, among them the reviewer's A (drop `mkdir`), C (`os.name != "nt"`), C2 (attribute never cleared) and D (no `return` after a retry). New ones:
    - A2 drop `rename`;
    - M6 a reparse point accepted as held;
    - M7 held handles share delete;
    - M8 Windows back to `shutil.rmtree`;
    - M10 k0 checks `scandir` instead of `listdir`;
    - M11 `any` instead of `all`;
    - M12 archive copy not renamed over the name.
    - The k0 half of M11 survives, as expected: each k0 check names one primitive, so `any` equals `all` there. The storage tests kill it.
  - Focused (`test_purge_tmp`, `test_session_context`, `test_storage_anchored*`, `test_storage_properties`, `test_profiles`, `test_k0_scan`, `test_k0_review_fix`, `test_k0_checkpoint`, `test_k0_sync`): `619 passed, 11 skipped in 85.03s`.
  - Read-only directory probe (scratch, on this host): `rmdir of a read-only directory: PermissionError 5`. So the directory case in the P2-2 test does exercise the attribute clear.
  - Pipeline suite `tests/` (`test_slot.py -- python -X faulthandler -m pytest -q -p no:cacheprovider tests`): `2916 passed, 43 skipped, 4 warnings in 646.57s (0:10:46)` (`.tmp/S42-ci3/fix1/pipeline-suite.log`).
  - Tool suite `tools/agent-workflow/tests/`: `5939 passed, 14 skipped in 1033.98s (0:17:13)` (`.tmp/S42-ci3/fix1/tools-suite.log`).
  - POSIX-only tests, run only on Linux CI (`not_run` here: no Linux, no WSL, installs forbidden):
    - `test_a_posix_held_way_acts_on_the_directory_it_opened`;
    - `test_posix_root_reached_through_a_swapped_ancestor_is_refused`;
    - the POSIX arms of the cross-platform purge tests.
    - Their mutants are `not_run` as well.
- **Important changes and reasons:**
  - `tools/agent-workflow/purge_tmp.py`:
    - `_held_way`, `_hold_posix`, `_hold_nt`, `_hold_directory`: every delete and archive copy acts inside a held chain of real directories from `.tmp`.
    - `_remove_tree_nt`: replaces Windows `rmtree`, which entered a swapped junction (A2).
    - `_on_error`: POSIX only reports. Windows retries only inside the held chain, after `_clear_read_only` worked on the entry's own handle.
    - `_remove`: POSIX uses `rmtree(name, dir_fd=parent)`.
    - `_archive_copy`, `_open_report`: no-follow read; exclusive temporary plus rename. `archive_remove` refuses a worktree whose reports cannot be archived (`_NotArchived`).
  - `src/prompt_enhancer/_os_support.py` (new) with `os_supports`, used by `_anchored._posix_available` and `k0_scan:67`, `:114`, `:144`.
  - `src/prompt_enhancer/storage/_anchored.py` `_open_root`: the POSIX root is opened from `/` one component at a time (S1).
  - Tests:
    - `tools/agent-workflow/tests/test_purge_tmp.py`: 8 new tests (Windows junction-ancestor regression, probe2, read-only and silence, held way on both platforms, archive write and read); `_on_error` callers pass `root`; two doubles take `dir_fd`.
    - `tests/test_storage_anchored_writes.py`: literal capability tests (one per primitive), a subprocess test for a double before import and for `reload(os)`, and the POSIX root-ancestor test.
    - `tests/test_k0_scan.py`: k0 capability tests through a labelled `os` view.
- **Problems, risks and remaining work:**
  - `not_run`: `ruff check` and `ruff format --check` (`No module named ruff`, installs forbidden). Formatting is by hand to the file's existing style.
  - Linux runs everything POSIX above for the first time in the next CI window. The riskiest new POSIX code is `_open_root` (`O_PATH` ancestors) and `rmtree(dir_fd=...)`.
  - The tool guard refused my removal of four `__pycache__/*.pyc` files that my `py_compile` check wrote. They are Git-ignored: `src/prompt_enhancer/__pycache__/{_os_support,k0_scan}.cpython-312.pyc`, `src/prompt_enhancer/storage/__pycache__/_anchored.cpython-312.pyc`, `tools/agent-workflow/__pycache__/purge_tmp.cpython-312.pyc`. The PM may delete them.

### Noticed, not done (fix round 1)

- S4: store reads (`read_bytes`, `read_table_file`, `os.listdir` by path after `real_inside`) can be redirected by a swapped ancestor. They return other bytes and write nothing. Anchored reads would be a separate store task.
- The Gate judges protected names and manifests, then the purge removes the tree. A protected tree moved into a target in between would be deleted: a content race, not a link redirect.
- `purge_tmp` still passes `onerror=` (deprecated since Python 3.12, `shutil.py:727`). `onexc` does not exist on 3.11, the project minimum.
- `_PosixDirectory.open_lock`'s `expected a file` branch is still untested (first round, still open).

### Handoff (fix round 1)

- **Decisions for the PM to merge into `docs/implementation-notes.md`:**
  - "A platform capability check over `os.supports_*` judges by function name (`prompt_enhancer._os_support.os_supports`), never by identity or an import-time capture (S42-ci3 fix round 1)."
  - "`purge_tmp` acts only inside a held chain of real directories from `.tmp` (POSIX `dir_fd`, Windows handles without `FILE_SHARE_DELETE`). It does not use `shutil.rmtree` on Windows (`_rmtree_unsafe` follows a swapped junction). The rmtree hook only reports on POSIX."
  - "`archive-remove` refuses a worktree whose reports cannot be archived without following a link."
- **Lesson candidate:** "A check of a path followed by an act on the same string is the bug, not the fix: hold the way (descriptor or handle) and act relative to it."

## Fix round 2

Input: the Sonnet review `.tmp/S42-ci3/review2/review.md` (FAIL: P0-1, P3-1 to P3-4) and the Gemini review `.tmp/S42-ci3/gemini-fix1-review.md` (PASS, missed P0-1). Builder: Claude Code, Opus 5.5, effort high. Base: the fix-round-1 tree (uncommitted on `b2273d4`); copies of its `_anchored.py` and `purge_tmp.py` are kept in `.tmp/S42-ci3/fix2/round1/` for the regression run.

### Step 0 (pre-approved by the dispatch)

1. **Unknowns and failure modes:** no Linux here, so a POSIX arm can only run on Windows under a double. A double that only replays the code's own calls proves nothing. So the store double models the kernel's documented refusals (`O_NOFOLLOW` gives ELOOP, `O_DIRECTORY` gives ENOTDIR, a multi-component name is refused). A rename that misses one caller leaves the bug in place: two callers, `:246` and `:552`.
2. **Questions (proposed answers, none blocking):**
   1. Rename which helper? Proposed: both, `_open_posix_root` and `_open_windows_root`, so neither branch can shadow the other again, and each name says its platform.
   2. AST rule: what counts as a redefinition? Proposed: a module-level name bound twice by `def`, `class`, `import` or assignment to a plain name. Bindings in exclusive branches (`if`/`else`, `try`/`except`) do not count: they are the platform switch and the optional-import fallback. The test scans `tools/agent-workflow/` and the project's `python.src_dir` (`.agents/project.json`), so the copied tooling checks its own host project.
   3. P3-3 (read-only archive file): replace or refuse? Proposed: replace. On Windows, a `PermissionError` from `os.replace` clears the read-only attribute on the destination's own handle (`_clear_read_only` opens the entry itself and leaves a link or junction alone), then retries once, still inside the held way. The copy still never writes through a name: the temporary is renamed over it.
   4. R6 without symlink privilege? Proposed: a hard link at the destination to an outside file. It needs no privilege on NTFS and aliases the outside file exactly as a file symlink does, so a write through the name changes the outside file and a rename over the name does not.
3. **Plan:** (a) class audit (below, before any edit); (b) rename both root helpers, add the AST test, rename the test-only `NAMES` duplicate; (c) storage: an in-memory POSIX double, plus cross-platform tests that reach `_PosixDirectory` (P0) and the untested POSIX arms; kill mutant E; (d) purge: tests for R4 and R5, a hard-link case for R6, a `_hold_posix` double test, and the read-only archive replace with its test; (e) the regression run on the round-1 copies, mutants, focused tests, both suites, report.

### Class audit 1: module-level names bound more than once (AST scan, before any edit)

Scan: a scratch AST script (removed; its rule now lives in `test_module_redefinitions.py`) over `src/`, `tools/agent-workflow/` and, for information, `tests/`. It counts bindings by `def`, `class`, `import` and assignment to a plain name, nested `if`/`try`/`with` included. An assignment to an attribute or a subscript is a mutation, not a binding.

| # | File | Name | Bindings | Changes behaviour | Action |
|---|---|---|---|---|---|
| D1 | `src/prompt_enhancer/storage/_anchored.py` | `_open_root` | `:216` POSIX `def (root, flags)`; `:457` Windows `def (api, root)`, both at top level | yes: P0-1, every POSIX store write raises `AttributeError` | fixed: `_open_posix_root`, `_open_windows_root` |
| D2 | `tools/agent-workflow/tests/test_purge_tmp.py` | `NAMES` | `:510` and `:1388`, both at top level, two different values | no today: each is read only by the `@given` decorator right below it, at definition time | renamed (`SWEEP_NAMES` at `:1388`), so the AST test can stay strict and a later runtime read cannot get the wrong list |
| D3 | `tools/agent-workflow/tests/test_process_guard.py`, `test_process_guard_consumers.py` | `st` | `try: import` / `except ImportError: st = None` | no: exclusive branches, the optional-import fallback | none (allowed by the rule) |
| D4 | `tests/test_profile_properties.py` (outside the scan scope, information only) | `_conflict` | `:334` strategy, `:376` function | no today: the strategy is read only by the decorator at `:344` | Noticed, not done (outside the brief's `src/` and `tools/agent-workflow/` scope) |

The first scan also counted `process_guard.PS_VALUE_OPTS`, `profiles._ManifestDumper` and `trusted_state._k32`. These are false positives, attribute and subscript assignments that the corrected scan no longer counts.

### Class audit 2: POSIX-only arms and what reaches them on Windows

"Double" means a labelled double that runs on Windows. "Linux CI" means a POSIX-only test that runs only in the CI window. Unchanged POSIX arms outside these files were green on Linux in CI window 3 (run 43764148659), apart from the three reds this task closed. They are not audited per arm here.

| # | Arm | Before this round on Windows | Action |
|---|---|---|---|
| X1 | `_anchored._PosixDirectory.__init__`, `_open_posix_root` (root from `/`, `O_PATH` above, the non-absolute refusal) | nothing (P0-1 hid here) | double: `PosixTree` tests |
| X2 | `_anchored._open_directory` ELOOP / ENOTDIR refusal | nothing | double: a link above, at and below the root |
| X3 | `_PosixDirectory.remove`, `_lstat` (link refusal, type mismatch), `mkdir`, `rename` (existing target) | nothing | double |
| X4 | `_PosixDirectory.create_temporary`, `commit`, `publish` (linkat, EEXIST, fallback), `discard` | nothing | double (files are real scratch files behind the double's descriptors) |
| X5 | `_PosixDirectory.open_lock` ELOOP and `expected a file` (never tested, Noticed in rounds 0 and 1) | nothing | double (a FIFO-like entry) |
| X6 | `_open_posix_root` `fstat` `S_ISDIR` backstop | nothing | `not_run`: Linux `openat(O_PATH\|O_DIRECTORY\|O_NOFOLLOW)` never returns a link (ENOTDIR first), so only a double that breaks the kernel contract could reach it. It is defence in depth. |
| X7 | `_anchored._posix_available` flags (`O_NOFOLLOW`, `O_DIRECTORY`) | sets only (mutant E survived) | test: each flag absent refuses |
| X8 | `purge_tmp._hold_posix` (root by full path with `O_NOFOLLOW`, components by `dir_fd`, `create`, close) | nothing | double over the real tree (`PosixOs`) |
| X9 | `purge_tmp._remove` fd-based `rmtree(dir_fd=...)`, `_remove_link` POSIX, `_open_report` POSIX, `_archive_copy` with `dir_fd` | Linux CI only | `not_run` on Windows: they are the kernel's and `shutil`'s fd behaviour. A double would test itself. Linux CI runs `test_a_posix_held_way_acts_on_the_directory_it_opened`, the `[tree]`/`[tree-refused]` removal tests and the archive tests. |
| X10 | `_fs.FileLock` `fcntl.flock`, `k0_scan` POSIX `_native_open` / `_lstat` / `_names` | Linux CI (green in window 3; k0 capability checks by `PosixView`) | none: unchanged by this task except the capability names, which round 1 covered |

### Configuration impact (fix round 2)

None. No profile key, setting or default is added, read or changed. The renames are internal. The read-only retry uses the existing `_clear_read_only`. The double's flag values are test-only, Linux x86-64 values from `asm-generic/fcntl.h`, labelled as a double.

### Configuration audit (fix round 2)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| read-only retry of the archive replace (Windows) | none: a platform fact (MoveFileExW refuses a read-only destination) | n/a | n/a | `test_archiving_replaces_an_older_read_only_copy`; refusal stays for a junction at the name (`test_archiving_never_writes_through_a_link[leaf-directory]`) | protocol-bound |
| `PosixTree` / `PosixOs` flag values (`0o200000`, `0o400000`, ...) | none: test-only doubles | n/a | n/a | n/a | test-only |
| AST rule's skipped directories (`__pycache__`, `.venv`, `node_modules`) | none: structural (generated or third-party trees) | n/a | n/a | `test_the_scan_covers_the_tooling_and_the_project_sources` | test-only |

No new key, so no override or refusal test of configuration applies. Property tests: no parser or validator changed, and the existing storage path properties (`tests/test_storage_properties.py`) ran in the focused set.

### Linux reasoning (no Linux here)

- `_open_posix_root` is unchanged apart from its name, so the review-2 trace stands (`O_PATH|O_DIRECTORY|O_NOFOLLOW` above the root, `fstat` on an `O_PATH` descriptor, one `close` per parent). Assumption: from open(2) and fstat(2), not re-fetched. Linux CI checks it.
- The cross-platform tests added here also run on Linux. They patch only `_anchored.os`, `_anchored.Path` and `_anchored.real_inside` (storage), or `purge_tmp.os` and `purge_tmp.shutil` (purge), through `monkeypatch`, which restores them. Source-verified by reading the tests.
- `test_a_store_root_swapped_for_a_link_is_refused_by_its_own_open` expects `is a link or not a directory: refused` on Linux. openat on a symlink with `O_DIRECTORY|O_NOFOLLOW` gives ENOTDIR or ELOOP, and `_open_directory` maps both (`_anchored.py:211`). Assumption on the exact errno, labelled.
- The read-only retry is Windows-only (`os.name != "nt"` re-raises). rename(2) replaces a read-only file when the directory is writable, so POSIX never reaches it. Assumption from rename(2).

### Completion report (fix round 2)

- **Work completed:**
  - P0-1: the POSIX root helper is `_open_posix_root` (`_anchored.py:216`, called at `:246`), the Windows one `_open_windows_root` (`:457`, called at `:553`).
  - Class audit 1: an AST scan. One redefinition changed behaviour (D1). The test-only `NAMES` duplicate is renamed (`SWEEP_NAMES`).
  - New tool test `tools/agent-workflow/tests/test_module_redefinitions.py`: scans `tools/agent-workflow/` and the project `src/` and fails on any top-level redefinition. It also has form tests: 8 forms caught, 5 forms allowed.
  - Class audit 2: POSIX arms X1-X10. X1-X5, X7 and X8 now run on Windows under labelled doubles. X6 and X9 are `not_run` with their reasons. X10 is unchanged.
  - P3-1 (R4, R5), P3-2 (R6, by a hard link), P3-3 (read-only replace), P3-4 (mutant E).
- **Result and evidence (Windows):**
  - Round-1 tree with the new tests:
    - storage `6 failed, 2 passed` (`.tmp/S42-ci3/fix2/round1-storage.txt`). Every `PosixTree` test fails with the review's symptom, `AttributeError: 'PurePosixPath' object has no attribute 'create'` at round-1 `_anchored.py:458`. The two passes are the flag tests, which guard unchanged code against mutant E.
    - purge: `1 failed, 4 passed`. The failure is `test_archiving_replaces_an_older_read_only_copy`. The 4 passes are guard tests of code that was already right; mutants R4, R5, R6, H1 and H2 show that they bite.
  - Mutants (`.tmp/S42-ci3/fix2/mutants-report.txt`, `mutants-report-n5n6.txt`; baselines `164 passed, 7 skipped` storage, `179 passed, 2 skipped` purge). 16 run, 16 killed:
    - review: E (drop `O_DIRECTORY`), R4 (pardir guard), R5 (root not verified), R6 (copy straight to the name, killed by `leaf-hardlink`);
    - own, on the renamed helpers:
      - E2 drop `O_NOFOLLOW`;
      - N1 root by full path;
      - N2 ancestors without `O_NOFOLLOW`;
      - N3 the loop opens the root's name twice;
      - N4 ancestor descriptor leaked;
      - N5 Windows root follows a junction;
      - N6 Windows root not verified.
    - N5 and N6 survived the first run, because the final-path check backs up each of them. The new `test_a_store_root_swapped_for_a_link_is_refused_by_its_own_open` kills both by pinning the refusal to the root's own check.
    - own, in purge: R4b (root-itself guard), RO1 (no retry), RO2 (retry without clearing), H1 (`_hold_posix` root without `O_NOFOLLOW`), H2 (create skipped).
  - Focused: storage, k0 and profiles `351 passed, 9 skipped in 66.10s`; purge and AST `194 passed, 2 skipped in 7.98s`.
  - Whole suites (serial, since the venv has no xdist; one fixed `--basetemp` each under `.tmp/S42-ci3/fix2/`, deleted afterwards):
    - pipeline `tests/` (`test_slot.py -- python -X faulthandler -m pytest -q -p no:cacheprovider tests`): `2925 passed, 43 skipped, 4 warnings in 579.92s (0:09:39)` (`.tmp/S42-ci3/fix2/pipeline-suite.log`);
    - tools `tools/agent-workflow/tests/`: `5959 passed, 14 skipped in 862.20s (0:14:22)` (`.tmp/S42-ci3/fix2/tools-suite.log`).
- **Important changes and reasons:**
  - `src/prompt_enhancer/storage/_anchored.py`: the two renames. One name per platform helper, so neither definition can replace the other.
  - `tools/agent-workflow/purge_tmp.py` `_archive_copy`: a `PermissionError` from `os.replace` on Windows clears the read-only attribute on the destination's own handle (`_clear_read_only` leaves a link or junction alone) and replaces once more inside the held way. Otherwise it raises as before (`_NotArchived`). Replace rather than refuse: the copy still goes through an exclusive temporary and a rename, so nothing is ever written through the name, and refusing would keep a worktree forever because of a file the tool itself archived.
  - `tests/test_storage_anchored_writes.py`:
    - `PosixTree` (labelled double: an in-memory POSIX tree with the kernel's documented refusals);
    - 5 cross-platform tests, `:848`, `:879`, `:894`, `:908` and the flag test `:674`;
    - the root-link test `:450`.
  - `tools/agent-workflow/tests/test_purge_tmp.py`:
    - R4 and R5 tests at `:831` and `:844`;
    - `PosixOs` (labelled double) with the `_hold_posix` test at `:900`;
    - the read-only archive test at `:925`;
    - `leaf-hardlink` at `:937`;
    - `SWEEP_NAMES`.
  - `tools/agent-workflow/tests/test_module_redefinitions.py`: new, the AST guard.
- **Problems, risks and remaining work:**
  - `not_run`: `ruff check` and `ruff format --check` (`No module named ruff`; installs forbidden). CI has no ruff either (`.github/workflows/ci.yml`, grep), so the AST test is the only redefinition guard.
  - `not_run`: Linux; X6 and X9 (reasons in class audit 2); `test_archiving_never_writes_through_a_link[leaf-file]` on Windows (no symlink privilege; `leaf-hardlink` now covers mutant R6 here); review mutant R8 (`_open_report` refusing a reparse point: a Windows file symlink needs privilege, and a hard link is no reparse point).

### Noticed, not done (fix round 2)

- `tests/test_profile_properties.py:334` and `:376` bind `_conflict` twice: a strategy, then a function. Behaviour is unchanged today, because only the decorator at `:344` reads the strategy. It is outside the brief's scan scope; extending the AST test to `tests/` would need that rename.
- Adding ruff (F811) to CI would catch the same class one step earlier, plus unused names.

## Worker completion

- Task: S42-ci3 fix round 2. Role: implementer (Claude Code, Opus 5.5, high).
- Done check:
  - class audit table and the AST redefinition test: yes (audits 1 and 2; `test_module_redefinitions.py`);
  - fixes: yes (P0-1 renames, the read-only replace, `SWEEP_NAMES`);
  - P0 test failing on the round-1 tree and passing now: yes (`6 failed, 2 passed` on round 1; green now);
  - mutants: R4, R5 and E killed; R6 killed through `leaf-hardlink`; 12 of my own, all killed (16 of 16);
  - focused tests, then both whole suites: green on Windows (`351 passed, 9 skipped` and `194 passed, 2 skipped`; then `2925 passed, 43 skipped` and `5959 passed, 14 skipped`);
  - Completion report for fix round 2: yes.
- Not run: ruff (not installed); Linux; X6 and X9; `leaf-file` and R8 on Windows (no symlink privilege).
- Scratch: removed (basetemps, the round-1 copies, the scan and mutant scripts). Kept for citation, under `.tmp/S42-ci3/fix2/`: `mutants-report.txt`, `mutants-report-n5n6.txt`, `round1-storage.txt`, `pipeline-suite.log`, `tools-suite.log`.
- No commits, no network, no installs, `.secrets/` not opened, nothing written outside the worktree's scratch, and no processes left running (both suites and all monitors finished or stopped).

### Worker completion (fix round 1)

- Task: S42-ci3 fix round 1. Role: implementer (Claude Code, Opus 5.5, high).
- Done check:
  - class audit table: yes;
  - fixes: yes;
  - tests that fail on the first-round tree and pass now: yes, including the Windows junction-ancestor regression and probe2 as a test;
  - mutants: 10 run, all killed on Windows, A, C, C2 and D among them;
  - focused tests and both whole suites: green on Windows (`619 passed, 11 skipped`; `2916 passed, 43 skipped`; `5939 passed, 14 skipped`);
  - Completion report: yes.
- Not run: ruff (not installed); Linux execution (no Linux here).
- Scratch: removed (`fix1/scratch`, basetemps, probes). Kept for citation: `.tmp/S42-ci3/fix1/mutants-report.txt`, `pipeline-suite.log`, `tools-suite.log`.
- No commits, no network, no installs, `.secrets/` not opened, nothing written outside the worktree's scratch, and no processes left running.

### Worker completion (first round)

- Task: S42-ci3. Role: implementer (Claude Code, Opus 5.5, high).
- Done check: root causes with evidence, yes; class audit table, yes; fixes with tests that would have failed, yes (L1 test unchanged but now reachable, L2 and L3 tests rewritten, 3 new tests); 5 mutants killed on Windows; focused tests and both whole suites green on Windows; Completion report, yes.
- Not run: ruff (not installed); Linux execution (no Linux here).
- Scratch: removed (`mut/`, `mutants.py`, basetemps); kept the cited `ci3-failed.log`, `pipeline-suite.log`, `tools-suite.log`, `mutants-report.txt` under `.tmp/S42-ci3/`.
- No commits, no network, no installs, `.secrets/` not opened, no processes left running.
