# S42: purge_tmp.py prunes empty protected-name trees before archive-remove and reports access-denied directories

Kind: code. Small: yes. Standing rule S42 (decision 72): a defect of existing agent tooling, fixed by the PM, reviewed by another family (Codex).

## Why (2026-11-18 cleanup, user request "dọn dẹp những thứ ko cần thiết")

- `archive-remove` refused the finished worktrees `.tmp/worktrees/T-018-p2` and `T-018-p3` with `refused, protected tree <wt>/runs`, although both `runs` folders were empty (T-018-p3's evidence had been moved to the main `runs/T-018-p3` first). `_find_protected` judged a nested `runs`, `pm-state`, `cli-jobs` or `pm-archive` by name only, so an empty folder blocked the whole tree. The shell write guard refuses `Remove-Item` of a worktree's `runs` folder, so no other route exists.
- `purge-children .tmp/worktrees <26 empty leftovers>` printed `could not remove <dir>: a link or not a directory now` for real directories whose open is denied. `_Win.open` dropped the Windows error code, so the message named the wrong cause.

## Root cause

1. `_find_protected` had one exception (tracked placeholders `.secrets/README.md`, `models/README.md`); a protected name with nothing inside had none.
2. `_Win.open` returned None without keeping the Windows error, so `_remove_tree_nt` could not tell ERROR_ACCESS_DENIED from a link or a non-directory.

## First attempt (failed the Codex review, 2026-11-18)

The PM's first change added an emptiness waiver (`_holds_nothing`) inside `_find_protected` for every command. Codex review FAIL: F1 (P1) a file written after the final gate check was deleted by the ordinary recursive removal, because the scan's verdict authorized a delete that did not carry the emptiness condition; F2 (P2) the scan listed below a nested `.secrets` (any case); F3 (P2) the waiver bypassed `PROTECTED_PAIRS` (an empty nested `.tmp/agy-jobs`); F4 (P2) `_is_link` returns False on a failed stat, so an unreadable child counted as empty; F5 (P3) `last_error` was lost when the attribute query failed after CreateFileW, and the message claimed an ACL cause. A fresh Codex implementer reproduced F1 to F5 red-first on scratch fixtures, then was blocked by the Codex guard (no `tools/agent-workflow/` edits), so the PM built fix round 1 under S42.

## Fix round 1 (PM implementation)

Design: the waiver is gone and `_find_protected` is strict again for every command. Only `archive-remove` prunes (`Gate(prune_empty=True)`). `Gate.prune` first runs every other check (`check(nested=False)`: containment, protected ancestors, kept containers, ownership, manifests); then `_prune_empty_protected` plans each nested protected-name tree with `_only_directories` and removes it with `_rmdir_each` (`os.rmdir` only, deepest first, each inside `_held_way` after a fresh no-follow check). The unchanged strict `check` and `remove` then judge what is left. A dry run removes nothing and passes the planned trees to `check(pruned=...)`, so its verdict is the one a real run reaches.

| Finding | Fix | Regression |
|---|---|---|
| F1 | Removal of an empty tree is `os.rmdir` only: the operating system refuses a directory that holds any entry at that moment, so a file written after the plan and after the last check survives, and the strict gate then refuses the worktree. No recursive fallback. | `test_f1_a_file_written_into_the_empty_tree_after_the_last_check_survives` (the write lands inside the patched `os.rmdir`, right before the real call); `test_f1_the_removal_itself_never_deletes_a_file_it_did_not_plan` |
| F2 | `_walk` never descends into a directory whose name case-folds to `.secrets` (the consumer still sees the name and refuses it); this also covers `_manifest_roots`. `_only_directories` stops at such a name without listing it; `_prune_empty_protected` never prunes or enters one. Only the pre-existing placeholder check lists a `.secrets` directory's own entries. | `test_f2_nothing_below_a_nested_secrets_directory_is_listed_or_removed[.secrets, .SeCrEtS]` (records every `os.scandir`); `test_an_empty_secrets_directory_keeps_its_own_rule` |
| F3 | `PROTECTED_PAIRS` stays strict: the prune never enters or prunes a pair directory, and `_only_directories` refuses a tree with one below it. The old fixture `scratch-2/agy-jobs` was not a pair; the new test uses real pairs. | `test_f3_an_empty_path_pair_directory_is_never_pruned[direct, below a protected tree]` |
| F4 | `_real_directory`: a no-follow stat must succeed and show a directory that is no link or reparse point; a failed stat is never a directory. Checked for every planned entry and again right before each `rmdir`. | `test_f4_a_failed_no_follow_stat_below_a_protected_tree_fails_closed`; `test_only_directories_fails_closed_on_a_link_a_file_or_a_listing_error`; `test_a_link_inside_an_empty_protected_tree_blocks_the_prune` (a junction on Windows) |
| F5 | `_Win.open` reads the error of a failed attribute query before `CloseHandle` and resets it to 0 only after a full open; the message is `access denied`, with no claimed cause (`_WIN_ERROR_ACCESS_DENIED = 5`, protocol-bound). | `test_f5_win_open_keeps_the_error_of_a_failed_attribute_query`; `test_an_access_denied_directory_is_reported_as_such` |

Also new: `test_archive_remove_prunes_an_empty_nested_protected_tree_then_removes_the_worktree` (5 names including `RUNS`), `test_a_dry_run_lists_the_prune_and_its_verdict_but_removes_nothing`, `test_only_archive_remove_prunes_empty_protected_trees` (purge-children, purge-older, sweep), `test_an_owned_worktree_is_not_pruned`, and the property `test_property_archive_remove_never_removes_a_file_of_a_protected_tree` (40 generated layouts of guarded and plain names with and without files; an independent oracle decides removed or refused; no file of a guarded tree is ever removed).

## Class sweep

Every caller of `_find_protected` goes through `Gate.check` (`archive-remove`, `purge-children`, `purge-older`, `sweep`, `prune-archive`); all keep the strict rule, only `archive-remove` prunes first. `_walk` callers: `_find_protected`, `_manifest_roots`, `_archive_reports` (it also skipped `.secrets` by exact name), `_prune_empty_protected`. `_is_link` stays a link test only; every safety decision of the prune uses `_real_directory`. `_Win.open` callers: `_hold_directory` (reports by message), `_clear_read_only` and `_open_report` (boolean result, unchanged).

## Configuration impact and audit

No setting, key, profile or default added; the three `cleanup_config.json` keys (`sweep_age_hours`, `archive_retention_hours`, `scan_timeout_seconds`) keep their validators and tests (`test_sweep_age_is_the_configured_value`, `test_prune_archive_deletes_only_old_children_and_keeps_the_archive`, `test_tmp_line_names_a_missing_config_key` and the per-key invalid tests, all green). Retained literals: `_WIN_ERROR_ACCESS_DENIED` protocol-bound; protected names and pairs artifact-bound; rmdir-only pruning safety-bound.

## Evidence (PM, Windows, project venv, `test_slot.py`, fixed basetemp removed after each run)

```text
test_slot.py -- python -B -m pytest -q -p no:cacheprovider --tb=short -rs --basetemp .tmp\S42-purge-fix\basetemp tools\agent-workflow\tests\test_purge_tmp.py
207 passed, 2 skipped in 22.87s   (skips: POSIX dir_fd test; file symlink privilege, both pre-existing)
same command, tools\agent-workflow\tests\test_session_context.py
21 passed in 5.11s
git diff --check -- tools/agent-workflow/purge_tmp.py tools/agent-workflow/tests/test_purge_tmp.py   (exit 0, no output)
```

Scratch mutants (module and tests copied under `.tmp/S42-purge-fix-m/`, removed after the run; the unchanged control fails only `test_shipped_cleanup_config_loads`, which reads the config next to the real script): M1 `rmdir` to `shutil.rmtree`, M2 `_walk` enters `.secrets`, M3 pair not refused in the plan, M4 failed stat counts as a directory, M5 `last_error` read after `CloseHandle`, M6 dry run ignores the plan, M7 `.secrets` pruned, M8 prune skips the ownership check: 8 of 8 killed, each by its targeted regression above.

## Noticed, not done

- Residual, pre-existing and unchanged: a protected-name tree created after the final strict check and before `_remove` finishes is deleted by the recursive removal, as for every target before S42. Closing it needs a protected-name check inside `_remove_tree_nt` and an fd-based POSIX walker in place of `shutil.rmtree`.
- Native POSIX not run on this machine (CI runs Linux). Ruff is absent from the project venv.
- PM lesson: a Codex tooling implementer cannot edit `tools/agent-workflow/` (Codex guard); the same mismatch blocked S42-statedir. Check the write scope of the CLI before dispatching a tooling implementer.

## Review

- 2026-11-18, fresh Codex reviewer (`codex-review`, gpt-6.2-sol high): ended rc=1 before its report with OpenAI's "flagged for possible cybersecurity risk"; decision 122 already routes deletion-safety reviews to Claude or Gemini (PM routing error). Its partial note held one valid lead: `Gate.prune`'s precheck (`nested=False`) skipped the kept-container check, so `archive-remove .tmp/agy-work` pruned empty trees inside the kept container before the strict gate refused it. Fixed: `_find_protected(nested=False)` keeps the ancestor and kept-container checks and skips only the walk; regression `test_a_kept_container_is_not_pruned`. After the fix: `test_purge_tmp.py` 208 passed, 2 skipped; `test_session_context.py` 21 passed; `git diff --check` clean.
- 2026-11-18 (decision 159: started at once, not after 21:00), fresh Claude Sonnet 5.5 `high` reviewer: **PASS**, no P0 to P2, four P3 (report `.tmp/S42-purge-rev3/review-claude.md`): P3-1 an open failure other than access denied (winerror 32, in use) still printed "a link or not a directory now"; P3-2 the dry run reported "would remove" where the real run refuses a directory it cannot remove, and a worktree refused for another reason had already lost its empty trees (also seen live: four worktrees "left partially" by Codex-sandbox directories the PM cannot remove); P3-3 mutants MF (prune descends into `PROTECTED_PAIRS`) and MA (no fresh no-follow recheck before `rmdir`) survived; P3-4 the module docstring named one waiver.

## Fix round 2 (PM, P3-1 to P3-4)

- `archive_remove` plans the prune first (`Gate.prune(dry_run=True)`) and judges the worktree with that plan (`Gate.check(pruned=plan)`); it prunes only when that verdict is clean, then runs the strict check again on what is left. A refused worktree keeps its empty trees; the dry-run line now says "(a directory in use or denied still refuses)".
- `_remove_tree_nt` prints `open failed (winerror N)` for any open failure other than access denied, and "a link or not a directory now" only when the open succeeded and the attribute check refused it.
- Module docstring names the second waiver.
- Tests: `test_f3_an_empty_protected_tree_below_a_path_pair_is_never_pruned`, `test_a_refused_worktree_keeps_its_empty_protected_trees`, `test_a_planned_directory_swapped_for_a_link_before_its_rmdir_survives`; the access-denied test also covers winerror 2 and 32; the F1 race test asserts the strict refusal before any archive. `test_purge_tmp.py` plus `test_session_context.py`: 232 passed, 2 skipped.
- Mutants (scratch copy, `.tmp/S42-purge/pm-mutants-round2b.log`): control M0 survives as expected; MA, MP (prune before the verdict), MC (code message) and MV (no final check after the prune) killed; MF survives and is equivalent now: a `PROTECTED_PAIRS` directory always refuses the worktree in the planned check, so no prune runs when a pair exists.
- Retest 2026-11-18, same Claude Sonnet 5.5 `high` reviewer (report `.tmp/S42-purge-rev3/retest1-claude.md`): **PASS**. P3-1 to P3-4 fixed; no path prunes a tree the strict gate refuses or removes a worktree it refuses (probes: late file, new ledger, in-use tree, worker-state tree all refused with the file intact); MF equivalence confirmed by probe (it rests on the gate order in `archive_remove`, not on `Gate.prune` alone); reviewer mutants MV, MS, MQ, MR killed. `test_purge_tmp.py` 211 passed, 2 skipped; `test_session_context.py` 21 passed.
- Accepted residual N-1 (P3, not fixed): when one empty tree is in use and another is not after a clean plan, the real run prunes the second and then refuses the worktree; only an empty directory is lost. A later caller of `Gate.prune` outside `archive_remove` must keep the plan-then-check order.
