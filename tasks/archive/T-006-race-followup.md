# T-006 follow-up: Windows lease race in store path resolution

Kind: code. Small: yes. R-IDs: R-007, R-014, R-017, R-025.

## Goal

Make store path resolution and the lease users tolerate a file that vanishes during resolution on Windows. The fix must keep link refusal intact.

## Evidence

The finding is R-1 in `.tmp/T-006-race/recheck-2.md`: P2, Windows only, fails closed, no data loss.

- `PinnedSnapshot.release()` (`src/prompt_enhancer/storage/store.py`, near 561-566) unlinks its lease without the store lock.
- GC's `_live_leases` (near 753-772) lists leases through `_children` (near 289-294) and resolves each name.
- `config._resolve_within` (`src/prompt_enhancer/config.py`, near 97-100) calls `os.path.realpath`. On Windows, CPython 3.12 `ntpath.realpath` keeps the `\\?\` prefix when the file exists at its first probe and is gone at its second. `is_relative_to(root)` is then false, and the result is "resolved outside project root".

## Plan, pre-approved by the PM

1. **Class audit first (S31).** List every caller of `_resolve_within`, `inside` and `real_inside`, and every `os.path.realpath` or `Path.resolve` in `src/prompt_enhancer/`. For each one, say whether a vanished or delete-pending file can reach it, and whether the `\\?\` or `\\?\UNC\` prefix can survive. List every unlink of a store file done without the store lock. Classify each site as a defect or as safe, with the reason. Put the table in the "Fix" section below.
2. **Normalise the result.** Strip a Windows extended-length prefix from the `realpath` result before the containment check, exactly as the `readlink` branch already does (`config.py`, near 111-114). Share one helper, with no second copy. Linked files and junctions must still be refused: keep a test that proves it.
3. **Tolerate a vanished lease.** `release()` tolerates a lease that is already gone, including a `StorageError` raised for that reason, but not other errors. `_live_leases` treats a vanished `.lease` entry as released. Any other error still fails closed.
4. **Add a regression test.** It patches `ntpath._getfinalpathname` so the file vanishes at the second probe, and must raise nothing. A bounded threaded create/delete loop against `real_inside` must give no false "outside project root". Keep the loop under 10 s. On POSIX, skip it with a reason or run it as-is.
5. **Run 3 to 10 mutants** on the changed lines.
6. **Run the tests:** the storage tests, then the full pipeline suite once, through `tools/agent-workflow/test_slot.py` with `PYTHONPATH=src`.

## Fix

### Step 0

Unknowns: none blocking. Ranked question 1 (proposed answer used): may delete-pending files be tolerated? Proposal: only the provable vanish, plus the NTFS `$Extend\$Deleted` name of a non-link (see below). Plan: the pre-approved steps 1-6.

### Class audit (S31)

| Site | Can a vanished or delete-pending file reach it? Prefix survive? | Verdict |
| --- | --- | --- |
| `config._resolve_within` realpath branch (`config.py`) | Yes, through `storage/_fs.inside` on a lease name. Prefix `\\?\` survived; experiment also found a second spelling `<drive>\$Extend\$Deleted\<id>` for a delete-pending file (about 0.8% of probes under churn) | defect, fixed |
| `config._resolve_within` readlink branch | Prefix already stripped; now uses the shared helper. `PermissionError` 5 on a delete-pending file still gives "cannot inspect link" | safe; residual fail-closed kept |
| callers `config._inside` (124), 239, 644, 647, `k0_sync` 121, `store.py` 189 | configuration paths read at start; no concurrent unlink | safe |
| `_fs.inside`, `real_inside` (store file paths) | the only path reaching per-file store names | covered by the fix |
| `Path.resolve` / `realpath` in `config` 571/668/690, `k0_checkpoint` 180-181, `k0_sync` 54-55, `k0_scan` 198, `model_client` 32, `model_secrets` 15-16, `store` 221/265/1106, `tables` 159, `__main__` 25 | roots and fixed directories, which exist for the whole run | safe |
| lockless unlink of a store file | only `PinnedSnapshot.release()` (`store.py`): leases are the only store files a reader deletes | defect, fixed |
| `_unlink` at registry sweep, `pin`, `release_hold`, `_live_holds`, `_live_leases`, state rewrite, artifact GC (`store.py` 360/483/513/698/767/784/863/880), `_fs` tmp unlinks | under the store lock or the single writer mutex; `missing_ok=True` | safe |

### Changes

- `config.py`: `_strip_extended_prefix` (one helper; `\\?\` and `\\?\UNC\`) used by the realpath and readlink branches; `_is_deleted_name` (`<drive>\$Extend\$Deleted\...`, Windows only). A non-link whose realpath is a deleted name resolves to its spelled path, so `real_inside` passes its equality check and the later open fails closed. Link refusal is untouched: this branch runs only after `readlink` said "not a link"; a link still resolves to another spelling or outside the root and is refused.
- `store.py`: `_vanished(path)` is true only for `FileNotFoundError` from `lstat`; `PermissionError` or delete-pending is not proof. `release()` tolerates `StorageError` only if the lease is `_vanished`; `_children(..., skip_vanished=True)` used by `_live_leases` drops an entry that is `_vanished` and re-raises every other refusal.
- Chosen way for delete-pending: tolerate it only in the one case I proved by experiment (realpath of a non-link returning the `$Deleted` name); `readlink` `PermissionError` 5 still fails closed, because it cannot be told from an access-control error. Residual: that error (about 8 per 3800 probes under a 4 s hot churn) can still make `release()` or one GC raise; GC fails closed, no data loss. Dropped alternative: a retry loop, which needs a configured retry setting.
- `tests/test_storage_vanish_race.py` (new, 14 tests): property of both prefix spellings, patched `ntpath._getfinalpathname` race, `$Deleted` name, 4 s threaded create/delete loop (none outside project root; only "cannot inspect link" allowed), junction refused, symlink refused (skipped here: no symlink right), `release`, `_children`, `_live_leases` tolerance and fail-closed cases, `_vanished`.

### Evidence

- Before the second fix the loop failed with "resolved outside project root"; probe showed 48% prefix results and 0.8% `$Deleted` results of 17000 realpath calls under churn.
- Storage tests: `362 passed, 2 skipped in 155.58s`. Full suite: `1838 passed, 32 skipped, 4 warnings in 408.87s` (log `full.log`). New file alone, 3 runs: `13 passed, 1 skipped` each (before the last test was added).
- Mutants (`mutants.log`, scratch copy): 9 made, 9 killed (M7 survived once, a test was added, then killed): no prefix strip, deleted name unrecognised, deleted name offset, strip length, any OSError is a vanish, release swallows all, children ignore flag, live leases do not skip, UNC prefix dropped.

### Configuration impact / audit

No setting changes: no new configurable value; the literals `\\?\`, `\\?\UNC\`, `$Extend\$Deleted` are protocol-bound (Windows/NTFS names). No example profile, override or refusal test applies.

## Fix round 1 (Codex review FAIL: F1 P1, F2 P2, F3 P3)

### Step 0

Unknowns: (1) whether a `$Deleted` result can be followed by a live object at the same name in real use (lease names are fresh UUIDs, so only a swap can do it); (2) how often each spelling occurs natively (F3 probe). Questions, with the answer used: Q1 may a `$Deleted` result whose name exists again be tolerated? No: refused, because no handle identity ties it to the resolved object. Q2 is a `FileNotFoundError` swallowed by `_unlink(missing_ok=True)` acceptable in `release()`? Yes: nothing was deleted, and `path()` ran its full check just before. Plan (PM pre-approved): audit, F1 fail closed with a distinct vanished signal proven by a top-down no-follow `lstat` of the chain, F2 tolerate only that signal, F3 native probe with log, regressions before/after, 3-10 mutants, storage then full suite. Finish: the done check of the brief.

### Configuration impact

None: no setting is added, read or changed. New literals: `$Extend`/`$Deleted` (unchanged, NTFS names) and `stat.FILE_ATTRIBUTE_REPARSE_POINT` (Windows attribute constant), both protocol-bound.

### Class audit (S31): every exception or absence turned into success, skip or tolerance

`config.py` and `src/prompt_enhancer/storage/` (line numbers of the previous patch). Re-raising wrappers that only change the message (`config.py` 205/211/216/218/239/393/544/549/618/627/650/660/681/703/710/728; `_fs.py` 133/168/173; `store.py` 145/177/202/225/416/422/464; `identity.py` 51/116; `tables.py` 166/233/279/348) turn nothing into success and are not rows.

| Site | Signal | What it proves | Can it hide a link/containment refusal? | Verdict / fix |
|---|---|---|---|---|
| `config._resolve_within` readlink `EINVAL/ENOENT/ENOTDIR` -> realpath branch (121-122) | readlink error | not a link (or absent) at readlink time only | no, as long as the realpath result itself is compared: a link swapped in later resolves to another spelling and is refused by containment or `real_inside` equality | safe, because nothing is substituted for the realpath result (after the F1 fix) |
| same, `\\?\` prefix stripped from realpath (125) | second realpath probe failed | the value is still the first probe's final path of the object then in place | no: a link there resolves elsewhere and is refused | safe, because the stripped value is a spelling of the resolved object, not a replacement |
| same, `$Deleted` result replaced by `target` (126-129) | `$Extend\$Deleted` spelling | nothing about the object now at `target` (a junction to that spelling gives it too) | yes (F1) | defect, fixed: raises `PathVanished` only when no link was followed and a top-down no-follow `lstat` chain proves a component missing and every existing one a non-link; otherwise "changed while it was resolved" refusal |
| `config._validate_configured_paths` `_get` error -> `continue` (286) | key absent | the key is absent | no: no path is read; a command that reads the key refuses it in `_get`/`_sentinel` | safe |
| `_fs._commit`, `write_table_file` tmp `unlink(missing_ok=True)` (72-73, 118-119) | error during write | cleanup only; the error is re-raised | no | safe |
| `_fs.FileLock.try_acquire` `PermissionError`/`OSError` -> False (214, 224) | sharing violation, delete-pending | lock not obtained | no: path came from `real_inside`; False reads as "live lease" / keep polling (retention) | safe, because it fails toward retention |
| `store._unlink` `missing_ok=True` (101) | `FileNotFoundError` of the delete itself | nothing was deleted at that name now | no: `path()` refused or passed just before; no refusal is an `OSError` | safe, because a missing file deletes nothing |
| `store._vanished` (104-113) and `release()` `except StorageError` (589-593) | full-path `lstat` FNF | only that the leaf is absent along whatever ancestors are now in place | yes (F2): an ancestor junction hides the real refusal | defect, fixed: helper removed; `release()` tolerates only `PathVanishedError` |
| `release()` `except OSError: pass` (587, pre-existing) | unlink failure (GC holding the lease open) | the delete did not happen | no: refusals are `StorageError`, never `OSError`; the lease stays and GC removes it as stale | safe, because it leaves the file (retention) |
| `_children` skip of any `StorageError` + `_vanished` (312-314) | same as above | same | yes (F2, GC probe) | defect, fixed: with `skip_vanished` only `PathVanishedError` is skipped |
| `_children` `not is_dir()` -> `[]` (306) | no directory | no directory at the name `path()` just checked | no: a swapped junction would be listed, but each entry is re-checked by `path()` | safe |
| `_read_current` / `_registry_name` FNF -> None (343, 353) | file absent | absent directly below root / below `registry` checked by `path()` | no: `path()` raised first for a link | safe |
| `snapshots`/`current`/`pin` `not root.exists()` (399, 405, 489) | store root absent | nothing stored yet | no: root is the validated configured path | safe |
| `pin()` cleanup `except OSError: pass`, then re-raise (501-507) | cleanup failure | error re-raised | no | safe |
| `release_hold` / `_hold_path` `exists()` (533, 542) | hold absent | nothing to release; under the store lock | no: `path()` first | safe |
| `_state_segments`, `_sweep_registry` `is_file()` filters (380, 697) | not a file | entries checked by `path()`; writer-owned | no | safe |
| `_live_leases` stale-lease `_unlink` `except OSError: pass` (795) | delete failed | stale lease stays, retried next GC | no | safe, retention |
| `gc` `_remove_tree` FNF -> collected (851) | part of a `deleting` tree vanished | the tree was being removed | no: `walk` refusals are `StorageError` and go to `skipped`; rmtree removes junctions as links (CPython 3.12 `shutil.py:606-640`) | safe, residual: a partial tree could be left as orphan |
| `gc` `(OSError, StorageError)` -> `report.skipped` (853) | foreign handle or link | not deleted, reported, retried | no: reported in the GC report, nothing deleted | safe |
| `_referenced` `manifest.exists()` (874) | manifest of a LIVE snapshot absent | corruption | no link hidden (`path()` first); but its artifacts lose protection | outside this class (not a link/containment refusal): `Noticed, not done` |
| `_gc_artifacts` `check_artifact_path` refusal -> skipped (894) | foreign spelling | never deleted, reported | no | safe |
| `Checkpoints.done` `_verify` error -> None (956) | missing/corrupt artifact | redo is conservative; `path()` sits outside the `try` | no | safe |
| `DataStore.read` `is_dir`/`is_file` filters (1153, 1156) | absent / not a file | every entry is `real_inside`-checked first | no | safe |

### Changes (round 1)

- `config.py`: `PathVanished(ConfigurationError)` carries `target`; `_is_link_stat` (S_ISLNK or `FILE_ATTRIBUTE_REPARSE_POINT`); `_proven_vanished(root, target)`: top-down no-follow `lstat` from root, True only when a component is missing and every one above it exists as a non-link. In `_resolve_within` a `$Deleted` realpath result is never a location: `PathVanished` when no link was followed and the chain proves the vanish, else the refusal "changed while it was resolved" (F1). Prefix stripping unchanged (it only respells the resolved object).
- `storage/_fs.py`: `PathVanishedError(StorageError)` with `target`; `inside` maps `PathVanished` to it; `real_inside` re-raises it only when `target` is the exact spelled path or a spelled parent, else the "link or another spelling" refusal.
- `storage/store.py`: `_vanished` removed; `_children(skip_vanished=True)` skips only `PathVanishedError`; `release()` returns on `PathVanishedError` from `path()`, then `_unlink` (missing_ok) with the pre-existing `except OSError` (F2). Every link, containment or protected-name refusal propagates.
- `tests/test_storage_vanish_race.py`: the review's four bug probes are refusal regressions (`test_junction_swapped_in_after_readlink_is_refused[False/True]`, `test_release_refuses_an_ancestor_junction`, `test_gc_refuses_an_ancestor_junction_swapped_in_after_listing`), each asserting a plain `StorageError`, never the vanished signal; `test_dangling_junction_is_never_a_proven_vanish` (real dangling junctions, kills `lstat` -> `stat`); `test_an_unreadable_component_is_never_a_proven_vanish` (labelled `lstat` double); `test_vanish_after_a_followed_junction_is_a_refusal`; `test_vanish_under_another_spelling_is_a_spelling_refusal`; `$Deleted` vanished/still-in-place cases for `real_inside`, `release`, `_children`, `_live_leases` (labelled realpath double); generic `StorageError` with the file gone still raises in `release` and `_live_leases`; threaded loop `join(timeout=10)` plus `is_alive` assert. The previous `_vanished` unit test is removed with the helper.

### Research evidence (round 1)

| Question | Source / command | Finding | Class |
|---|---|---|---|
| What does native realpath/readlink return for a file deleted while resolved? | `.tmp/T-006-race/fix1/ntfs_probe.py`, run as `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <same> -B -X faulthandler .tmp/T-006-race/fix1/ntfs_probe.py .tmp/T-006-race/fix1/probe-scratch 8`, PYTHONPATH=`src`; log `.tmp/T-006-race/fix1/ntfs_probe.log`. Windows 11 build 26300 (`Windows-11-10.0.26300-SP0`), CPython 3.12.10 (MSC v.1943 64 bit), volume NTFS (`GetVolumeInformationW`), 8 s per phase, one churn thread | Phase `fresh` (new uuid name per round, like leases): 3432 resolutions; realpath plain 1363, `\\?\`-prefixed 1413, `$Deleted` 656; after every one of the 656 `$Deleted` results, `lstat` of the name gave `FileNotFoundError` (656/656); readlink EINVAL 2595, ENOENT 833, EACCES/winerror 5 4. Phase `same` (one name recreated): 2242 resolutions; `$Deleted` 245, then `lstat` FNF 45, exists 200; store path ok 1899, vanished signal 48, refused "changed while it was resolved" 281, "cannot inspect link" 14, "outside project root" 0 | experiment, this machine and this volume only; counts depend on timing and are not rates for other hosts |
| Does `$Deleted` prove a non-link? | review probe and `test_junction_swapped_in_after_readlink_is_refused` | No: a junction to a literal `$Deleted` target yields the same spelling; the fix proves on the object now in place | experiment |
| Does `os.lstat` follow a final junction on Windows 3.12? | `test_dangling_junction_is_never_a_proven_vanish` passes with `lstat`, fails with `stat` (mutant M1) | `lstat` reports the junction itself (reparse attribute); `stat` follows it to FileNotFoundError | experiment |
| Does `shutil.rmtree` enter a junction (GC `_remove_tree` row)? | installed `Lib/shutil.py:606-640` (`_rmtree_islink`, `os.walk(followlinks=os._walk_symlinks_as_files)`) | junctions are treated as files and unlinked as links | source-verified |
| Round-0 figures (17000 calls, 48%, 0.8%, 8 per 3800) | `task.md` round 0 Evidence | not reproduced and superseded by the probe above; no claim rests on them | assumption (withdrawn) |

Narrowed claim: in the `fresh` phase a `$Deleted` result was always followed by a missing name, so for leases (fresh uuid names) the vanished signal is the expected outcome; a recreated name is refused (fails closed), it is never tolerated.

### Tests and mutants (round 1)

Commands (worktree root; runner `.tmp/T-006-race/fix1/run.ps1` sets PYTHONNOUSERSITE, PYTHONDONTWRITEBYTECODE, PYTHONPATH, TEMP under `.tmp/T-006-race/fix1/` and calls `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <same> -B -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=.tmp/T-006-race/fix1/<name> ...`):

- Regressions before/after: the test file copied to `.tmp/T-006-race/fix1/before/test_regressions_before.py` with only its import lines shimmed (the previous patch lacks the new names; diff shown in the session), scratch `conftest.py` re-exports `tests/conftest.py`; `-k 'swapped_in_after_readlink or release_refuses_an_ancestor_junction or gc_refuses_an_ancestor_junction'`. Previous patch (saved copy `.tmp/T-006-race/fix1/prev/src`): `4 failed, 18 deselected in 3.23s` (`regressions-before.log`); fix: `4 passed, 18 deselected in 2.44s` (`regressions-after.log`). The copy predates two later-added tests; the four regression bodies are identical.
- `tests/test_storage_vanish_race.py`: `23 passed, 1 skipped in 15.15s` (skip: no symlink right; junction tests cover links natively).
- All ten `tests/test_storage*.py`: `371 passed, 2 skipped in 164.80s (0:02:44)` (`storage.log`).
- Full suite `tests` (serial; `-n 2` first refused with `unrecognized arguments: -n`, xdist not installed, nothing ran): `1847 passed, 32 skipped, 4 warnings in 934.75s (0:15:34)` (`full.log`; warnings are Pillow deprecations in `test_model_image.py`).
- Mutants (`mutate.py`, scratch copy per mutant, log `mutants.log`): 10/10 KILLED: M1 `lstat`->`stat` (4 failed), M2 drop no-link-followed guard, M3 chain `OSError` counts as vanished (killed after adding the labelled-double test; it survived the first run), M4 reparse attribute ignored, M5 `$Deleted` accepted (previous patch, 5 failed), M6 vanish spelling unchecked, M7 `_children` skips any `StorageError`, M8 `release` tolerates any `StorageError`, M9 existing non-link counts as vanished, M10 `skip_vanished` ignored.
- `git diff --check -- src tests`: exit 0. `ruff check` / `ruff format --check`: not_run, `No module named ruff` in `.venv`, no install permitted. Linux: not_run (CI).

### Configuration audit (round 1)

No setting, schema or profile change; override and refusal setting tests do not apply.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| `\\?\`, `\\?\UNC\` prefixes, slice lengths 4/8 | none: Windows path syntax | prefix property tests | n/a | n/a | protocol-bound |
| `$Extend`/`$Deleted`, parts 1:3 | none: NTFS namespace | deleted-name property; native probe | n/a | n/a | protocol-bound |
| `stat.FILE_ATTRIBUTE_REPARSE_POINT`, `S_ISLNK` | none: Windows/POSIX attribute | dangling-junction test, M4 | n/a | n/a | protocol-bound |
| vanish tolerated only as `PathVanishedError` (release, GC lease listing) | none: safety invariant | four regressions, M7/M8/M10 | n/a | n/a | safety-bound |
| message texts "vanished/changed while it was resolved" | none | matched by tests | n/a | n/a | safety-bound (refusal text) |
| test `join(timeout=10)`, probe 8 s phases | none | n/a | n/a | n/a | test-only |

### Completion report (round 1)

- Behaviour: a `$Deleted` realpath result is never accepted as a location; only a vanish proven by a top-down no-follow chain check, with no link followed and the exact spelling, becomes `PathVanishedError`, and only lease release and GC's lease listing tolerate it. Every link, containment, spelling or protected-name refusal propagates (F1, F2). A lease that genuinely vanishes is still skipped (tests above; native `fresh` phase: 656/656 `$Deleted` results followed by a missing name).
- Changed files: `src/prompt_enhancer/config.py`, `src/prompt_enhancer/storage/_fs.py`, `src/prompt_enhancer/storage/store.py`, `tests/test_storage_vanish_race.py` (new, untracked). Kept evidence in `.tmp/T-006-race/fix1/`: `ntfs_probe.py`, `ntfs_probe.log`, `mutate.py`, `mutants.log`, `regressions-before.log`, `regressions-after.log`, `storage.log`, `full.log`, `run.ps1`, `cleanup.py`, `before/` (shimmed test copy, scratch conftest), `prev/` (the previous patch's `config.py`, `store.py` and test file only). Scratch trees (basetemps, mutant copies, probe scratch, temp, hypothesis) removed by `cleanup.py`, which found no leftover links; no process left running.
- not_run: `ruff check`, `ruff format --check` (ruff absent); Linux (CI).
- Noticed, not done:
  - `Writer._referenced` skips a LIVE snapshot whose manifest is missing (`exists()`), so its artifacts lose GC protection after retention; not a link refusal, outside this class.
  - Every path-based operation after `path()` still follows an ancestor junction swapped in afterwards (general TOCTOU of name-based APIs; handle-relative operations would close it).
  - readlink `EACCES`/winerror 5 on a delete-pending file still refuses ("cannot inspect link": 4 of 3432 `fresh`, 14 of 2242 `same` resolutions), so a release or GC can rarely fail closed.
  - GC `_remove_tree` `FileNotFoundError` marks a snapshot collected and can leave a partial orphan tree.

## Fix round 2 (retest 1 FAIL: R1 P2; residuals N1 P1, N2 P1, N4 P3; user decision 123)

### Step 0

Unknowns: (1) whether NTFS refuses renaming a directory whose descendant is open without `FILE_SHARE_DELETE` (decides how long an anchored chain stays in place; native probe); (2) whether a relative `NtOpenFile` on a verified directory handle is available from ctypes on this host (probe). Questions, with the answer used: Q1 how does `release()` report a store-lock timeout? It raises `StorageError` naming the lease left for GC, after closing the lease handle (the reader's view ends either way; fail visible, nothing removed). Q2 does "snapshot entry" cover GC's `_remove_tree`? Yes: every file and directory of a collected snapshot is removed through the anchored primitive. Q3 other name-based deletes (holds, registry, state segments, artifacts)? Listed in the audit; out of this round's scope unless they remove a lease or snapshot entry. Plan (PM pre-approved): audit; remove `PathVanished`; lock on release; anchored removal primitive (Windows handle chain with relative `NtOpenFile`, POSIX `openat` chain with `O_NOFOLLOW`), refuse where neither exists; keep-set fails closed; collected only when verified absent; regressions before/after; 3-10 mutants; storage then full suite. Finish: the brief's done check.

Process note: the first `config.py` edit of this round (removing `PathVanished`) was made before this Configuration impact was written; it was written immediately after, before any other runtime edit.

### Configuration impact

- `storage.lock.timeout_s`, `storage.lock.poll_s` (existing, validated in `store._settings` and `KnowledgeStore.__init__`, declared in the example profiles): now also bound `PinnedSnapshot.release()`'s wait for the store lock. No new setting.
- New literals: Windows access masks, share modes, NT open options, NTSTATUS codes, `FILE_INFO_BY_HANDLE_CLASS` values, `FILE_DISPOSITION_FLAG_*`; POSIX `O_NOFOLLOW`/`O_DIRECTORY`: all protocol-bound.

### Class audit (S31), round 2: operations on a path validated earlier

Locks: S = store lock, W = writer mutex, none = no lock. "Name" = the operation re-resolves the name (an ancestor swapped since `path()` is followed); "anchored" = it acts on an object verified at the time of the operation. Line numbers are round 1's (`store.py`, `_fs.py`, `config.py`).

| Operation (site) | Lock | Acts on | Fix / verdict |
|---|---|---|---|
| `PinnedSnapshot.release()` lease unlink (`store.py:573-579`) | none | name (N2) | fixed: store lock (`storage.lock.*`), then anchored removal; FNF from the removal is "already gone" |
| `pin()` failure cleanup `_unlink(lease.path)` (`:492`) | S | name | fixed: anchored removal |
| `_live_leases` stale-lease `_unlink` (`:781`) | S, W | name | fixed: anchored removal |
| `_remove_tree` `shutil.rmtree` (`:320-323`; GC `:836`, `_recover` `:667`) | W (GC: after the S section) | name | fixed: every file, then every directory bottom-up, through anchored removal after the whole tree was listed and checked |
| `k0_sync._previous_for_plan` lease-directory `rmdir` (`k0_sync.py:38,43`) | S | name | fixed: anchored directory removal (a lease entry; outside `storage/` but the same class) |
| `FileLock.try_acquire` `mkdir` + `open('a+b')` (`_fs.py:229-231`; pin lease, GC probe, LOCK, WRITER) | S or none | name; creates a missing file | not fixed: a creation, not a removal; Noticed, not done |
| `atomic_write_bytes`, `write_table_file`: `mkdir`, temp open, `os.replace` (`_fs.py:87-131`; registry, CURRENT, holds, state, artifacts, snapshot files, DataStore) | S and/or W | name | not fixed this round (writes, not lease/snapshot removal); Noticed, not done |
| `_sweep_registry`, `release_hold`, `_live_holds`, `_rewrite_state`, `_gc_artifacts` `_unlink` (`:369,522,798,712,877,894`) | S and/or W | name | not fixed this round (registry, hold, state and artifact files, not lease/snapshot entries); Noticed, not done |
| `SnapshotBuilder.finish` `os.rename` (`:1098`), builder `mkdir` (`:1015`) | W | name | not fixed this round; Noticed, not done |
| reads: `_read_current`, `_registry_name`, `_registry`, `_manifest`, `_read_hold`, `_state_rows`, `PinnedSnapshot._entry`, `Checkpoints.load`, `DataStore.read`, `sha256_file`, `_verify` | S, W or none | name | safe for removal (none deletes); snapshot and artifact reads are verified by size and SHA-256; registry/CURRENT/hold reads are parsed and schema-checked; a swapped read can make an operation refuse, not delete |
| `config._read_yaml`, `_cached_schema`, `load_profile` reads (`config.py:188,199,711`) | none | name | safe: read-only configuration, schema-validated, outside the store |
| `_children`/`walk` `listdir` (`:297`) | caller's | name | safe: listing only, each entry re-checked by `path()` before use |

GC keep-set inputs (what protects a snapshot or artifact):

| Input | Missing | Unreadable / unparsable | Verdict |
|---|---|---|---|
| registry (`_registry`) | no pointer: empty registry, so no candidates | `read_table_file`/catalog validation raise | safe |
| CURRENT (`_read_current`) | None: published rows become retired, newest `keep` retained | corrupt pointer raises | safe (pre-existing semantics) |
| leases (`_live_leases`) | no `leases/`: none | resolution refusal raises; a lock that cannot be opened (`PermissionError`) counts as live | safe |
| holds (`_live_holds`) | none | `_read_hold` raises | safe |
| session builds (`self._built`) | n/a | n/a | safe |
| artifact references (`_referenced`): LIVE manifests | skipped (`exists()` False, `:861`) | `_manifest` raises | defect N1, fixed: a missing LIVE manifest raises a precise `StorageError` before anything is marked or deleted |
| artifact list (`walk('artifacts')`) and GC marks (`_state_rows`) | none | raise | safe |
| snapshot removal result (`gc` `:835-841`) | FNF from `rmtree` marks collected | other errors skipped, stay `deleting` | defect N4, fixed: collected only when the tree is verified absent, else kept `deleting` and listed in `report.skipped` |

### Changes (round 2)

- `config.py`: `PathVanished`, `_is_link_stat`, `_proven_vanished` and the `stat` import removed; a `$Deleted` realpath result is the plain refusal "changed while it was resolved".
- `storage/_fs.py`: `PathVanishedError` and its handling in `inside`/`real_inside` removed.
- `storage/_anchored.py` (new): `remove_entry(root, relative, *, directory=False)`: `real_inside` first, then Windows: root by `CreateFileW` (`FILE_FLAG_OPEN_REPARSE_POINT`), every component by `NtOpenFile` relative to its parent handle with `FILE_OPEN_REPARSE_POINT`, each handle checked (no reparse attribute, expected type, `GetFinalPathNameByHandleW` equal to the exact spelled path), directories held without `FILE_SHARE_DELETE`, delete on the leaf handle (`FileDispositionInfoEx` POSIX semantics, else `FileDispositionInfo`). POSIX: `os.open(O_DIRECTORY|O_NOFOLLOW, dir_fd=parent)` chain, `stat(follow_symlinks=False)`, `unlink`/`rmdir` with `dir_fd`; refused when `dir_fd`/`O_NOFOLLOW` support is missing. Absence is `FileNotFoundError` from the removal itself.
- `storage/store.py`: `KnowledgeStore._remove`; `_children` without `skip_vanished`; `walk(directories=...)`; `_remove_tree` lists and checks the whole tree, then removes files and directories bottom-up through `_remove` (FNF per entry skipped); `pin()` failure cleanup and `_live_leases` stale-lease removal through `_remove`; `PinnedSnapshot.release()` drops its lease lock, takes the store lock (`storage.lock.timeout_s`/`poll_s`; on timeout raises "released, file left for GC as a stale lease"), removes through `_remove`, FNF = already removed by GC; GC marks `collected` only when `os.path.lexists(path(snapshot))` is false, else `skipped` and still `deleting` (N4); `_referenced` raises a precise `StorageError` for a LIVE snapshot whose manifest is missing or unreadable (N1); `shutil` import removed; module docstring: removal rule and `release` in the lock order.
- `k0_sync.py`: the two lease-directory `rmdir` calls go through `store._remove(..., directory=True)`.
- Tests: `tests/test_storage_anchored.py` (new); `tests/test_storage_vanish_race.py` rewritten (no vanished signal; deletion name is a plain refusal); `tests/test_storage_review_r1.py`: the two `shutil.rmtree` scheduling hooks now hook `store_module.remove_entry`, which replaced rmtree (test intent unchanged).

### Research evidence (round 2)

| Question | Source / command | Finding | Class |
|---|---|---|---|
| Relative open below a verified handle on Windows | `NtOpenFile` with `OBJECT_ATTRIBUTES.RootDirectory` (ntifs.h / Microsoft docs "NtOpenFile", "OBJECT_ATTRIBUTES"); exercised by every Windows test in `test_storage_anchored.py` | works from ctypes on this host (Windows 11 build 26300, CPython 3.12.10, NTFS): files and directories removed, absence reported as `STATUS_OBJECT_NAME_NOT_FOUND` -> `FileNotFoundError` | experiment |
| Are held directories protected from a swap? | `test_directories_held_during_removal_cannot_be_renamed` (renames `leases/sid` and `leases` while the chain is held, real `os.rename`) | both renames raise `PermissionError`; mutant M5 (held with `FILE_SHARE_DELETE`) is the control | experiment |
| Does the rebuilt round-1 tree match the retested patch? | `rebuild_round1.py`, then retest 1's `test_new_schedules.py` on it (`fidelity` run) | `6 failed, 2 passed in 4.53s`, identical to retest 1 `new.log` | experiment |
| Does `os.unlink`/`os.rmdir` with `dir_fd` follow a final symlink? | POSIX `unlinkat(2)`/`rmdir(2)`: they remove the name, never follow it | not followed; POSIX branch `not_run` locally (no `dir_fd` on Windows), covered by Linux CI tests | documented; not_run locally |
| Who else deletes leases? | `store.py` `pin` cleanup, `_live_leases`, `release`; `k0_sync._previous_for_plan` | all now under the store lock and through `_remove` | source-verified |

### Tests and mutants (round 2)

Runner `.tmp/T-006-race/fix2/run.ps1` (same environment and `test_slot.py` limiter as round 1, scratch under `.tmp/T-006-race/fix2/`).

- Round-1 tree for the before-runs: not snapshotted before this round's edits, so rebuilt by `fix2/rebuild_round1.py` (copy of `src/`, each round-2 edit reversed with its exact text, `k0_sync.py` from HEAD; docstrings and comments may differ, behaviour is round 1's). Fidelity: retest 1's own `test_new_schedules.py` on it gives `6 failed, 2 passed in 4.53s`, identical to retest 1 `new.log`.
- Regressions (`tests/test_storage_anchored.py`, copied to `fix2/before/test_regressions_before.py` with only the `_anchored` import shimmed; `-k 'late_junction or never_unlinks_through or live_manifest or verified_absent'`): round 1 `10 failed, 16 deselected in 8.27s` (`regressions-before.log`); fix `10 passed, 16 deselected in 6.43s` (`regressions-after.log`). The ten: R1 three schedules times release and GC lease listing (6); N2 post-check swap (release never unlinks the outside victim); N1 `missing` (round 1 deleted the leased snapshot's artifact) and `corrupt` (round 1 already refused; it fails there only on the new, more precise message, so it is not a behaviour regression); N4 a removal that reports "not found" while the tree remains (round 1 marked it collected).
- Original race kept: `test_release_after_gc_removed_the_stale_lease_is_silent`, `test_gc_skips_a_lease_that_vanished_after_listing`; lock: `test_release_removes_its_lease_only_under_the_store_lock`, `test_release_lock_timeout_leaves_the_stale_lease_for_gc`; property: `test_gc_keep_set_property` (generated references, intact/missing/corrupt manifests for both snapshots, keep 0/1, leased or not; 12 examples).
- POSIX branch: `not_run` locally (no `dir_fd` on Windows). Linux CI runs every cross-platform test of `test_storage_anchored.py` with symlinks as the links, plus `test_remove_entry_refuses_without_a_dir_fd_primitive` and `test_remove_entry_removes_a_symlink_leaf_never_its_target` (POSIX only).
- Three race files (`test_storage_anchored.py`, `test_storage_vanish_race.py`, `test_storage_review_r1.py`): `82 passed, 3 skipped in 54.10s` (before the last test and the release `ExitStack` change).
- Storage suite, first run: `1 failed, 387 passed, 4 skipped`: `test_lock_order_never_takes_the_writer_mutex_under_the_store_lock` failed with `AttributeError: 'Tracked' object has no attribute 'acquire'`: its tracker wraps `store_lock()` as a context manager, and `release()` called `acquire()` directly. Fixed in `release()` (now `with ExitStack()` around `store_lock()`, so the lock-order check covers release too); rerun of all ten storage files: `388 passed, 4 skipped in 205.54s (0:03:25)` (`storage.log`; 4 skips: the two new POSIX-only tests, the no-symlink-right skip and one pre-existing skip, which round 1 also had).
- Mutants (`fix2/mutate.py`, scratch copy each, the three race files; `mutants.log`): 8/10 killed. KILLED: M1 release unlinks by name; M2 release skips the store lock; M3 reparse attribute unchecked; M4 final path not compared; M6 keep-set skips an unreadable LIVE manifest; M7 collected without verifying absence; M8 snapshot files removed by name; M9 stale lease removed by name. SURVIVED: M5 held directories opened with `FILE_SHARE_DELETE`: the rename test still sees `PermissionError` because NTFS refuses to move a directory with an open descendant, and in the chain every held directory has one, so the share mode is a second guard this design cannot observe separately; M10 directories opened by name instead of relative: every handle's exact final-path check already refuses a swapped ancestor and the held chain cannot move afterwards, so it is equivalent in effect; the relative open is kept as the brief's anchoring. M1/M2 rerun on the final `release()`: `2/2 killed` (M1 `2 failed, 80 passed, 3 skipped in 54.64s`; M2 `2 failed, 80 passed, 3 skipped in 49.66s`; `mutants-m1-m2-rerun.log`).
- Full suite `tests` (serial): `1864 passed, 34 skipped, 4 warnings in 555.06s (0:09:15)` (`full.log`; warnings are the known Pillow deprecations). The only source change after it started: the rewrap of one docstring line in `store.py`. `git diff --check -- src tests`: exit 0 (new untracked files checked separately: no trailing whitespace or tabs; no new line over 120 characters). `ruff check`, `ruff format --check`: not_run (`No module named ruff`).

### Configuration audit (round 2)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| `release()` waits for the store lock | `storage.lock.timeout_s`, `storage.lock.poll_s` | `_positive_finite` in `store._settings` and `KnowledgeStore.__init__`; poll <= timeout | `configs/enhancer.example.yaml` and the dev example (existing) | override: `test_release_lock_timeout_leaves_the_stale_lease_for_gc` (0.3 s / 0.05 s, observed in release); refusal: `test_lock_timeout_override_and_refusal`, `test_lock_poll_override_and_refusal` (`test_storage_config.py`), `test_f9_loader_refuses_non_finite_or_non_positive`, `test_f9_store_refuses_non_finite_or_non_positive` (`test_storage_review_r1.py`) | configurable (existing) |
| Windows access masks, share modes, open options, NTSTATUS codes, info classes, disposition flags, `_MAX_PATH_CHARS` | none | `_anchored` tests on real NTFS | n/a | n/a | protocol-bound |
| POSIX `O_DIRECTORY`, `O_NOFOLLOW`, `O_CLOEXEC`, `ELOOP`/`EMLINK`/`ENOTDIR` | none | POSIX tests (Linux CI) | n/a | n/a | protocol-bound |
| removal refused without an anchored primitive | none | `test_remove_entry_refuses_without_a_dir_fd_primitive` | n/a | n/a | safety-bound |
| keep-set refusal on an unreadable LIVE manifest; collected only when absent | none | N1/N4 regressions, property test, M6/M7 | n/a | n/a | safety-bound |
| test sleeps/timeouts (0.5 s, 10 s join), property `max_examples=12` | none | n/a | n/a | n/a | test-only |

### Completion report (round 2)

- Behaviour: no name-based vanish proof remains; every deletion-name or link anomaly is a plain `StorageError`. `release()` removes its lease under the store lock (bounded by `storage.lock.*`; on timeout the stale lease stays for GC and the timeout is raised). Lease and snapshot entries are removed only through `remove_entry`, which acts on the object verified inside the root at the moment of removal and refuses where no anchored primitive exists. A missing or unreadable LIVE manifest refuses GC before anything is marked or deleted (N1). A snapshot is `collected` only when verified absent (N4). The original race stays fixed: release and GC are serialised by the store lock, and a lease already gone is reported by the removal itself.
- Changed files against HEAD `b4eb856`: `src/prompt_enhancer/config.py`, `src/prompt_enhancer/storage/store.py` (`storage/_fs.py` is back to HEAD after removing round 1's `PathVanishedError`), `src/prompt_enhancer/k0_sync.py`, `src/prompt_enhancer/storage/_anchored.py` (new), `tests/test_storage_anchored.py` (new), `tests/test_storage_vanish_race.py` (new in this task, rewritten), `tests/test_storage_review_r1.py` (two scheduling hooks). Evidence kept in `.tmp/T-006-race/fix2/`: `run.ps1`, `rebuild_round1.py`, `make_before.py`, `mutate.py`, `before/` (shimmed copies, scratch conftest), the logs cited above.
- not_run: POSIX branch locally (Linux CI covers it); `ruff check`, `ruff format --check` (ruff absent); Linux run.
- Noticed, not done:
  - Name-based writes and creates remain (`atomic_write_bytes`, `write_table_file`, `FileLock.try_acquire` creating a missing lease, `SnapshotBuilder.finish` rename) and name-based deletes of registry, hold, state and artifact files: the N2 class outside lease/snapshot entries (audit table).
  - N3 kept as is: readlink EACCES on a delete-pending file still refuses ("cannot inspect link").
  - `_referenced` also feeds `Checkpoints.commit`'s immutability check, so a LIVE snapshot with an unreadable manifest now also refuses artifact commits, and GC refuses every run, until the manifest is repaired (fail closed by design; there is no repair or forced-retire command yet).

### Progress (round 2)

- Done: `config.py` vanish proof removed (deletion name = plain refusal); `_fs.py` `PathVanishedError` removed; new `storage/_anchored.py` (`remove_entry`); `store.py`: `_remove`, anchored `_remove_tree` (walk then files then directories), `release()` under the store lock, `pin()` cleanup and `_live_leases` anchored, GC collected-only-when-absent (N4), `_referenced` fails closed (N1), `shutil` import dropped; `k0_sync.py` lease-directory removals anchored; `test_storage_review_r1.py` two rmtree scheduling hooks moved to `remove_entry`; `test_storage_vanish_race.py` rewritten; new `tests/test_storage_anchored.py`. Runner `.tmp/T-006-race/fix2/run.ps1`.
- First run of the two race files: all pass (3 skips: POSIX-only).
- Next: before/after regressions against the round-1 copy (`.tmp/T-006-race/fix1/prev` holds round 0; round 1 must be copied from git diff state? see below), mutants, storage suite, full suite, Configuration audit, report.

## Completion report

Behaviour, commands, output and files are above. `not_run`: Linux run; `ruff check` and `ruff format --check` (ruff is not installed in `.venv`, not installed per rules); the reader-side live reproduction. Open: residual `readlink` PermissionError under extreme churn (fails closed). Noticed, not done: none.

## Fix round 3 (retest 2 and Gemini: P1-A, P1-B, P2-A, P3-A, P3-B, P3-C, intermittent test)

### Step 0

Unknowns: (1) whether `SetFileInformationByHandle(FileRenameInfo)` honours `RootDirectory` from ctypes on this host and whether a held parent handle (no `FILE_SHARE_DELETE`) blocks the rename-target open (native experiment); (2) whether `NtCreateFile` relative to a parent handle opened with list/traverse access may create children (experiment); (3) which snapshots the artifact keep-set needs (source reading, below). Questions, with the answer used (the PM pre-approved the plan; logged as PM choices pending user review): Q1 how is a `release()` failure reported when the caller's work succeeded? There is no logging path in `src/` (grep: no `logging`/`warnings`), so a `StorageWarning` (a `UserWarning`) is emitted; when the work raised, the failure is added as a note to the caller's exception, which propagates unchanged. Q2 does the commit immutability check also tolerate a damaged manifest? No: commit cannot tell whether the snapshot is needed without probing leases and holds; it refuses with the operator action, and GC (whose snapshot pass no longer needs manifests) collects an unneeded damaged snapshot, which ends the refusal. Q3 may `FileLock` keep a name-based open for callers outside a store? No: it takes an opener; every opener in `src/` is the anchored one (tests that built `FileLock(path)` use `_anchored.lock_file`). Plan: audit; one anchored file layer (create, write, rename, lock-file open, mkdir, remove) in `_anchored.py`; remove `_unlink`, `_temporary`, `_commit`, name-based `atomic_write_bytes`/`write_table_file`/`fsync_dir` and `FileLock`'s own open; structural test; P2-A keep-set; P3-B; 50-run loops; regressions before/after; 3-10 mutants; storage, K0, full suite. Finish: the brief's done check.

### Configuration impact

- No new setting. Existing `storage.lock.timeout_s`/`poll_s` keep bounding every lock (LOCK, WRITER, leases, GC probe); `storage.table_format` keeps choosing the table format of every anchored table write; `sync.snapshots.keep`, `sync.gc.retention` keep defining the keep-set and the artifact age; `storage.kb_store`/`storage.data_root` stay the anchored roots. None changes meaning or default.
- New literals: Windows `NtCreateFile` dispositions and options, `FILE_GENERIC_READ/WRITE`, `FileRenameInfo` class, `STATUS_OBJECT_NAME_COLLISION`, `ERROR_ALREADY_EXISTS`; POSIX `O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0o666`/`0o777` (umask applies, as with `open`/`mkdir` before): protocol-bound. Temporary name `.<name>.<uuid>.tmp`: artifact-bound (unchanged form; GC removes it).

### Class audit (S31), round 3: every create, write, rename, unlink and rmdir

Scope: `src/prompt_enhancer/storage/` and `config.py` (plus the two K0 callers of store mutations). Lines are round 2's. Lock: S = store lock, W = writer mutex + WRITER lock, none. "Name" = the act re-resolves a name after the check; "anchored" = the act is relative to a parent handle (Windows) or `dir_fd` (POSIX) verified at that moment (no reparse point or link, expected type, Windows final path equal to the spelled path), held until the act ends.

| Call site (round 2) | Act | Object acted on (round 2) | Lock | Fix / verdict (round 3) |
|---|---|---|---|---|
| `_fs._commit` `os.replace` (`_fs.py:69-75`) | rename over target | name | caller's | removed; `_anchored._commit`: rename on the temp file's handle into its verified parent (`FileRenameInfo`, `RootDirectory` = parent; POSIX `os.rename(..., src_dir_fd=, dst_dir_fd=)`) |
| `_fs._commit`, `write_table_file` temp `unlink` (`:73`, `:119`) | delete temp | name | caller's | removed; delete-on-handle of the temp (Windows) / `unlink(dir_fd=)` (POSIX) |
| `atomic_write_bytes` `mkdir(parents)` + temp `open('wb')` (`:86-91`) | create dirs, create temp | name | caller's | removed; anchored `atomic_write_bytes(root, relative, ...)`: each missing directory created relative to its verified parent, temp created `FILE_CREATE`/`O_CREAT|O_EXCL|O_NOFOLLOW` in the verified parent |
| `write_table_file` `mkdir` + temp `open` + `tmp.stat`/`sha256_file(tmp)` (`:103-115`) | create, write, read back by name | name | caller's | removed; anchored `write_table_file(root, relative, ...)`; size and SHA-256 read back through the temp's own handle |
| `fsync_dir` `os.open(path)` (`:58`, POSIX) | fsync parent | name | caller's | removed; `fsync` on the verified parent descriptor |
| `FileLock.try_acquire` `mkdir` + `open('a+b')` (`:211-213`) | create dirs and lock file | name (P3-A) | S (pin) / none (LOCK, WRITER) | fixed: `FileLock` has no open of its own; its opener is `_anchored.open_lock_file` (create relative to verified parents; the leaf opened without following a link and verified) |
| `FileLock.release` (`:244-255`) | unlock, close | held handle | n/a | safe, because it acts on the handle it holds |
| `store._unlink` (`store.py:104-106`) | delete | name | n/a | removed (P1-A); every delete is `KnowledgeStore._remove` -> `remove_entry` |
| `store_lock` `FileLock(LOCK)` (`:292`) | create LOCK | name | none | fixed: anchored lock file |
| `_remove`, `_remove_tree` (`:319-339`) | delete | anchored (round 2) | W / S | safe, because `remove_entry` acts on the verified leaf handle |
| `_write_registry` table + pointer (`:376-377`) | create/replace | name (W1) | S | fixed: anchored writes |
| `_sweep_registry` `_unlink` (`:385`) | delete | name (D1) | S | fixed: `_remove` |
| `pin` lease `FileLock` (`:502`) | create lease and `leases/<sid>` | name (C1) | S | fixed: anchored lock file |
| `pin` failure cleanup `_remove` (`:509`) | delete | anchored | S | safe |
| `hold` `write_table_file` (`:531`) | create/replace hold | name (W2) | S | fixed: anchored write |
| `release_hold` `exists` + `_unlink` (`:538-539`) | delete | name (hold case) | S | fixed: `_remove`, absence is `FileNotFoundError` from the removal |
| `_hold_path` `exists`/`_read_hold` (`:546-551`) | read | name | S | safe, because it only reads and parses a schema-checked hold; a swapped read can refuse, not write |
| `PinnedSnapshot.release` `_remove` (`:600`) | delete | anchored | S | safe |
| `Writer.__enter__` `FileLock(WRITER)` (`:663`) | create WRITER | name | none | fixed: anchored lock file |
| `_recover` `_remove_tree` (`:691`), registry writes (`:700-702`) | delete, write | anchored / name | W, S | delete safe; writes fixed (above) |
| `_append_state` (`:729`) | create segment | name | W | fixed: anchored write |
| `_rewrite_state` `_unlink` (`:736`) | delete | name (D4) | W | fixed: `_remove` on the store-relative segment name |
| `_move_current` CURRENT (`:784`) | replace | name | S, W | fixed: anchored write |
| `_live_leases` probe `FileLock(path).try_acquire()` (`:799-803`) | open `a+b`: re-creates a vanished lease | name | S, W | fixed: anchored open without create; a vanished lease (`FileNotFoundError`) is skipped as released |
| `_live_leases` `_remove` (`:806`) | delete | anchored | S, W | safe |
| `_live_holds` `_unlink` (`:823`) | delete expired hold | name | S, W | fixed: `_remove` |
| `gc` `_remove_tree` (`:862`), `os.path.lexists` (`:863`) | delete; read | anchored; name | W | safe (read only decides `collected`) |
| `_gc_artifacts` `_unlink` `.tmp` (`:912`) and artifact (`:929`) | delete | name (D5) | W | fixed: `_remove` |
| `commit` artifact writes (`:1005`, `:1010`) | create/replace | name (W3) | W | fixed: anchored writes; `check` still runs before the replace |
| `SnapshotBuilder.__init__` `mkdir(parents=True)` (`:1050`) | create `.building-<id>` | name | W | fixed: anchored `make_directory` (new leaf, `FILE_CREATE`/`mkdir(dir_fd=)`) |
| `write_table`, `add_file`, `write_manifest` (`:1089`, `:1096`, `:1120`) | create | name | W | fixed: anchored writes |
| `finish` `os.rename` + `fsync_dir` (`:1133-1134`) | rename directory | name | W | fixed: anchored `rename_entry` (source opened and verified relative to `snapshots/`, renamed on its handle into the same verified parent, never over an existing entry) |
| `DataStore.append` (`:1168`) | create segment | name | none (append-only, unique name) | fixed: anchored write under `storage.data_root` |
| `k0_sync._previous_for_plan` `_remove` directories (`k0_sync.py:39,44`) | rmdir | anchored | S | safe |
| `config.py` | none: no create, write, rename, unlink or rmdir (grep for `open(`, `write`, `mkdir`, `unlink`, `rename`, `rmdir`, `os.replace`, `os.remove`: matches are reads, string `replace`, key names) | n/a | n/a | safe, because it only reads |
| `identity.py`, `tables.py` | none (grep as above) | n/a | n/a | safe |
| `_anchored` store-root open (`CreateFileW` by name; POSIX `os.open(root, O_DIRECTORY|O_NOFOLLOW)`) | open root | name of the resolved, validated root | caller's | safe, because Windows verifies the root handle (no reparse point, final path equal to the root) and POSIX refuses a final link; the root's ancestors are validated configuration paths outside the store tree (POSIX residual: an ancestor of the root swapped by someone who can rename the project tree is not detected) |

### Progress (round 3)

- Done: audit table above; `_anchored.py` rewritten as the one file layer (`remove_entry`, `atomic_write_bytes`, `write_table_file`, `make_directory`, `rename_entry`, `open_lock_file`, `lock_file`; Windows `NtCreateFile` chain + `NtSetInformationFile(FileRenameInformation)` with `RootDirectory`; POSIX `dir_fd` chain); `_fs.py` lost `_temporary`, `_commit`, `atomic_write_bytes`, `write_table_file`, `fsync_dir`, and `FileLock` takes an `opener`; `store.py` every mutation through `_remove`/`_discard`/`_write_table`/`_write_bytes`/`_lock`/`make_directory`/`rename_entry`, `_unlink` removed, GC keep-set (P2-A: no early `_referenced`, `report.damaged`, actionable refusal), `keep_outcome` + `StorageWarning` (P3-B) used by `PinnedSnapshot.__exit__`, `k0_sync`, `k0_checkpoint`.
- Native finding: kernel32 `SetFileInformationByHandle(FileRenameInfo)` refuses a non-NULL `RootDirectory` (WinError 87); `NtSetInformationFile` class 10 and 65 accept it (`fix3/probe_rename.log`).
- Round-2 snapshot for before-runs: `fix3/prev/` (copy of `src/`, `tests/`, `contracts/`, `configs/` taken after only `_anchored.py` had changed; that file restored verbatim from the round-2 text).
- Intermittent test, round 2 tree, 50 runs: `50 passed in 188.01s` (`fix3/loop-prev.log`).
- Regressions before: see Tests below (the first copy sat in a folder pytest later used as basetemp; rerun from `fix3/regress/`).

### P2-A: which snapshots the keep-set needs (source-verified)

`Writer.gc` (`store.py`, marking section): `protected = kept | leased | held | self._built`; candidates are `deleting` rows and `retired`/`validated`/`failed` rows outside `protected`; `published` is never a candidate. So the keep-set is exactly the LIVE rows that are not candidates: CURRENT (published), the newest `sync.snapshots.keep` retired (rollback targets, which the brief's list omitted), every leased or live-held snapshot, this session's validated snapshots, plus unfinished builders' entries (`self._builders`). A candidate's references protect nothing (it is collected in the same GC), so its manifest is not needed.

- Fix: the early `self._referenced()` before the lock is gone. Inside the marking section, once the keep-set is known and before anything is marked, every keep-set manifest is read (`Writer._live_manifest`); one that cannot be read refuses the whole GC, registry unchanged, with a message naming the snapshot, `snapshots/<id>/MANIFEST.parquet`, "nothing deleted or changed", and the action (restore it from a backup, or end what keeps the snapshot and run `kb gc`). A candidate with a damaged manifest is read only to be listed in `GcReport.damaged`, then collected. Commit's immutability check (`_referenced`) still refuses on any damaged LIVE manifest (it cannot know leases and holds) with the same message; GC now collects an unneeded damaged snapshot, which ends that refusal (`test_gc_collects_an_unneeded_snapshot_whose_manifest_is_damaged`).
- Design note: a first draft let the snapshot pass run and refused only the artifact pass (retest 2's suggestion); `test_storage_review_r4.py::...[gc reads a live snapshot]` showed it rewrote the registry before refusing, so the refusal moved before any mark (stricter). Removal of stale leases and expired holds while the keep-set is found still happens (each is correct on its own).

### Research evidence (round 3)

| Question | Source / command | Finding | Class |
|---|---|---|---|
| Does `SetFileInformationByHandle(FileRenameInfo)` accept `RootDirectory`? | `fix3/probe_rename.py`, run as `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <same> -B -X faulthandler .tmp/T-006-race/fix3/probe_rename.py src .tmp/T-006-race/fix3/probe-scratch`; log `fix3/probe_rename.log` | `k32-root: winerror 87`; `k32-null-fullpath: ok`; `nt10-root: ok`; `nt65-root: ok` (Windows 11 build 26300, CPython 3.12.10, NTFS) | experiment |
| May `NtCreateFile` create children relative to a parent handle opened with list/traverse access? | every Windows write test in `tests/test_storage_anchored_writes.py` and the storage suite | yes: files, directories and lock files are created; `STATUS_OBJECT_NAME_COLLISION` maps to `FileExistsError` | experiment |
| Can a by-name rename (M2b) be steered outside while the chain is held? | M2b run, `fix3/mutants-m2b-m3.log` | the by-name `os.replace` fails at once with WinError 32 (the temp file is open share-read only), so no schedule reaches a link; the held chain cannot be renamed either (round 2 evidence) | experiment |
| Without the reparse check (M3), does a relative open through a junction handle reach the target? | M3 run | no: `OSError: [WinError 1921] The name of the file cannot be resolved by the system`; the reparse check turns this into the intended `StorageError` refusal | experiment |
| Is `os.replace` in `os.supports_dir_fd`? | installed `Lib/os.py:112-123` (`_add("HAVE_RENAMEAT", "rename")`, no `replace`) | the POSIX branch uses `os.rename(..., src_dir_fd=, dst_dir_fd=)` (renameat replaces a file like `replace`) | source-verified |

### Changes (round 3)

- `storage/_anchored.py`: the one file layer: `remove_entry`, `atomic_write_bytes(root, relative, data, check)`, `write_table_file(root, relative, table, fmt, check)`, `make_directory`, `rename_entry(..., directory)`, `open_lock_file(..., create)`, `lock_file`. Windows `_WindowsDirectory`: root by `CreateFileW` then verified; each component by `NtCreateFile` relative to its parent with `FILE_OPEN_REPARSE_POINT` (`FILE_OPEN` or `FILE_OPEN_IF`), each verified (no reparse point, type, exact final path) and held without `FILE_SHARE_DELETE`; temp file `FILE_CREATE` in the verified parent; size and SHA-256 read back through the temp handle; rename by `NtSetInformationFile(FileRenameInformation)` with `RootDirectory` = the verified parent; discard by delete-on-handle. POSIX `_PosixDirectory`: `O_DIRECTORY|O_NOFOLLOW` chain, `mkdir`/`open(O_CREAT|O_EXCL|O_NOFOLLOW)`/`rename`/`unlink`/`rmdir` with `dir_fd`, `fsync` on the parent descriptor; refused without `dir_fd` support. The store root itself is created by name on first write, then verified (residual below).
- `storage/_fs.py`: `_temporary`, `_commit`, `atomic_write_bytes`, `write_table_file`, `fsync_dir` and the `uuid` import removed; `FileLock(path, timeout_s, poll_s, *, opener)` has no open of its own.
- `storage/store.py`: `_unlink` removed; `KnowledgeStore._lock`, `_write_table`, `_write_bytes`, `_discard`; every audit row through them; `_hold_path` and `_state_segments` return store-relative names; the `_live_leases` probe opens without create and skips a vanished lease; GC keep-set as above, `GcReport.damaged`, `Writer._live_manifest`; `SnapshotBuilder` uses `make_directory` and `rename_entry`; `DataStore.append` anchored; `StorageWarning`, `keep_outcome`; `PinnedSnapshot.__exit__` keeps the reader's outcome (P3-B); module docstring: one mutation rule.
- `k0_sync.py`, `k0_checkpoint.py`: `with pinned:`, and `keep_outcome` around the lease-directory clean-up, so neither a release nor a clean-up failure replaces the result or the exception.
- Tests: new `tests/test_storage_anchored_writes.py` (regressions D1, hold x2, D4, D5 x2, W1, W2, W3, C1, probe recreation; primitive tests; structural AST test and its control; P3-B both orders for both K0 callers, plus a real lock-timeout case; four POSIX-only tests). `tests/test_storage_anchored.py`: keep-set property extended (three snapshots; row state published/retired/validated; leases kept on retired snapshots; live or expired holds; keep; 20 examples) and two P2-A tests. Adapted to the removed name-based API, same intent: `test_storage_tables.py`, `test_storage_config.py`, `test_storage_review_r1.py` (crash hook `_unlink` -> `remove_entry`; the F3 double now blocks only snapshot entries, since registry sweeps also go through `remove_entry`), `test_storage_review_r3.py` (`_fs._commit` -> `_anchored._commit`; the lease-open refusal hooks `_anchored.open_lock_file`), `test_storage_review_r4.py`, `test_storage_snapshots.py` (crash hook `os.rename` -> `rename_entry`), `test_k0_review_fix.py` (`lock_file`; open hook on `_anchored`).

### Tests and mutants (round 3)

Runner `fix3/run.ps1 <src> <tests> <basetemp> <pytest args>` (PYTHONNOUSERSITE, PYTHONDONTWRITEBYTECODE, PYTHONPATH=`<src>;<tests>`, TEMP under `fix3/`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <same> -B -X faulthandler -m pytest -q -p no:cacheprovider`).

- Regressions, `-k "d1_ or hold_removal or d4_ or d5_ or w1_ or w2_ or w3_ or c1_ or never_recreates or no_store_module"` (19 selected). Round 2 tree (`fix3/prev/`; test file copied to `fix3/regress/test_regressions_before.py`, `cmp`: identical): 13 FAILED: D1, hold x2, D4, D5 x2 with `FileNotFoundError` reading the outside victim (deleted); W1/W2/W3 victim now holds the registry pointer / a parquet / `b'attacker-visible bytes'`; C1 `[<uuid>.lease]` created outside; probe `assert not True` (lease directory recreated); structural store.py `104: _unlink ...` and `_fs.py` `49: fsync_dir ... mkdir()` (`fix3/regressions-before.log`; pytest's count line is missing from the captured log, the FAILED list is complete). Round 3: junit `tests=19 failures=0 errors=0 skipped=0 time=4.301` (`fix3/regressions-after.xml`).
- Intermittent `test_threads_pin_while_publishing`, 50 runs each (`fix3/loop/test_loop.py`): round 2 `50 passed in 188.01s` (`fix3/loop-prev.log`); round 3 `50 passed in 182.25s` (`fix3/loop-new.log`). Not reproduced. Its T-008 error ("lease path resolved outside project root" on base `b4eb856`, the original T-006 code) is the realpath-prefix race this task fixed in round 0/1 (round 0 Evidence: the same loop failed with that text before the fix), so it is that named class, not the anchoring class; nothing changed for it here.
- All `tests/test_storage*.py` and `tests/test_k0*.py` (18 files): `exit 0`, progress marks 575 passed, 8 skipped (`fix3/storage.log`; skips: 6 POSIX-only, 1 no symlink right, 1 pre-existing). The earlier run `fix3/storage1.log` had 3 failures, fixed as described (r4 "gc reads a live snapshot" x2: refusal moved before marking; r1 F3 double narrowed).
- Mutants (`fix3/mutate.py`: scratch copy of `src/` each, the three race files, `-x`; `fix3/mutants.log`, `fix3/mutants-m2b-m3.log`): 10/10 KILLED. M1 writes by name (W1); M2a finish renames by name in store (structural); M2b commit renames by name in `_anchored` (D1, WinError 32 from the temp's own share mode: killed functionally, not by a link schedule; see Research); M3 reparse check skipped (D1, WinError 1921 `OSError` instead of the refusal); M4 lease probe creates (probe test); M5 release failure replaces a result; M6 release failure replaces an exception (P3-B tests); M7 GC reads every manifest first; M8 damaged snapshot not reported (property); M9 directories created by name (`write-deep` swap test).
- POSIX branches (`_PosixDirectory`, `_posix_available`, POSIX lock open and rename): `not_run` locally (no `dir_fd` on Windows). Linux CI runs `test_posix_writes_refuse_without_a_dir_fd_primitive`, `test_posix_lock_file_refuses_a_symlink_leaf`, `test_posix_write_replaces_a_symlink_leaf_as_a_name`, `test_posix_rename_refuses_a_symlink_source`, round 2's two POSIX tests, and every cross-platform test of both anchored files with symlinks as the links.
- `git diff --check -- src tests`: exit 0; no new line over 120 characters (one pre-existing at `k0_checkpoint.py:86`). `ruff check`, `ruff format --check`: not_run (`No module named ruff`).
- Full pipeline suite `tests` (serial): junit `tests=1950 failures=0 errors=0 skipped=38 time=489.057` (`fix3/full.log`, `fix3/full.xml`; warnings are the known Pillow deprecations).

### Configuration audit (round 3)

No setting, schema or profile is added or changed. Every `storage.*` key the changed code reads, with its existing tests (all green in `fix3/storage.log`):

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| wait of every anchored lock (LOCK, WRITER, lease, GC probe, release) | `storage.lock.timeout_s`, `storage.lock.poll_s` | `_positive_finite` in `store._settings` and `KnowledgeStore.__init__`; poll <= timeout | `configs/enhancer.example.yaml` (existing) | `test_lock_timeout_override_and_refusal`, `test_lock_poll_override_and_refusal` (`test_storage_config.py`); release/clean-up under a 0.3 s override: `test_a_lock_timeout_after_reading_keeps_the_plan_rows`, `test_release_lock_timeout_leaves_the_stale_lease_for_gc` | configurable (existing) |
| format of every anchored table write | `storage.table_format` | `x in SUFFIX` | example profile (existing) | `test_table_format_override_and_refusal` | configurable (existing) |
| anchored roots | `storage.kb_store`, `storage.data_root` | `_resolved_path` (links, protected roots, project root) | example profile (existing) | `test_kb_store_override_and_refusal`, `test_data_root_override_and_refusal`; anchored `DataStore.append`: `test_data_store_append_never_writes_outside` | configurable (existing) |
| keep-set and artifact age (P2-A) | `sync.snapshots.keep`, `sync.gc.retention` | integer >= 0; `<n>s|m|h|d` | example profile (existing) | `test_keep_override_and_refusal`, `test_retention_override_and_refusal`; keep 0/1 in `test_gc_keep_set_property` | configurable (existing) |
| Windows `NtCreateFile` dispositions/options, `FILE_GENERIC_READ/WRITE`, `FileRenameInformation` (10), `STATUS_OBJECT_NAME_COLLISION`, share modes | none | native tests in both anchored files | n/a | n/a | protocol-bound |
| POSIX `O_CREAT|O_EXCL|O_NOFOLLOW|O_DIRECTORY|O_CLOEXEC`, modes `0o666`/`0o777` (umask applies, as `open`/`mkdir` did) | none | POSIX tests (Linux CI) | n/a | n/a | protocol-bound |
| temporary name `.<name>.<uuid>.tmp` | none | GC `.tmp` sweep, D5 tmp regression | n/a | n/a | artifact-bound (unchanged form) |
| refusal without an anchored primitive; keep-set refusal before marking; `StorageWarning`/note instead of replacing an outcome | none | POSIX refusal test, property, P2-A and P3-B tests, M5-M8 | n/a | n/a | safety-bound |
| property `max_examples=20`, lock-timeout test 0.3 s / 0.05 s | none | n/a | n/a | n/a | test-only |

### Completion report (round 3)

- Behaviour: every create, write, rename, lock-file open and delete in the stores goes through `_anchored.py` and acts relative to directories verified inside the root at that moment; an ancestor swapped for a link after the check is refused, never followed, and no outside file is deleted, overwritten or created (D1, hold x2, D4, D5 x2, W1, W2, W3, C1 fail on round 2, pass now). The name-based helpers are gone and a structural test keeps them out of the store modules. GC needs no manifest outside the keep-set (it collects and reports such a snapshot) and refuses before marking anything, with an operator action, when a keep-set manifest is damaged. A failed release or lease-directory clean-up never replaces a K0 reader's result or exception. R1, N1, N2, N4 and the original race stay green (`test_storage_anchored.py`, `test_storage_vanish_race.py` in the storage run).
- Changed files against HEAD `b4eb856`: `src/prompt_enhancer/config.py` (round 2), `src/prompt_enhancer/k0_checkpoint.py`, `src/prompt_enhancer/k0_sync.py`, `src/prompt_enhancer/storage/_fs.py`, `src/prompt_enhancer/storage/store.py`, `src/prompt_enhancer/storage/_anchored.py` (new), `tests/test_storage_anchored_writes.py` (new), `tests/test_storage_anchored.py` (new in this task), `tests/test_storage_vanish_race.py` (new in this task, unchanged this round), `tests/test_k0_review_fix.py`, `tests/test_storage_config.py`, `tests/test_storage_review_r1.py`, `tests/test_storage_review_r3.py`, `tests/test_storage_review_r4.py`, `tests/test_storage_snapshots.py`, `tests/test_storage_tables.py`. Evidence kept in `.tmp/T-006-race/fix3/` (logs, junit files, `run.ps1`, `mutate.py`, `probe_rename.py`, `cleanup.py`, `loop/`, `regress/`, `prev/` = the round 2 tree for reruns). Scratch trees removed by `cleanup.py` (0 links found); no process left running.
- not_run: POSIX branches locally (Linux CI tests named above); `ruff check`, `ruff format --check` (ruff absent); Linux run; ReFS/SMB.
- Residuals: (1) the store root itself is created by name on first write (`_directory`), then verified; an ancestor of the root swapped in that window could get empty directories created outside, nothing written (Windows refuses at the root verify; POSIX opens the root with `O_NOFOLLOW`, which checks only its last component). (2) POSIX `rename_entry` checks the target is absent, then `renameat`: an empty directory created in between would be replaced; only the writer creates there. (3) Commit still refuses while any LIVE manifest is damaged, until GC collects the snapshot (if unneeded) or the manifest is restored.
- Noticed, not done:
  - No repair or forced-retire command for a needed snapshot whose manifest is damaged (brief: not this round); the refusal names the manual remedy.
  - `pytest`'s final count line is missing from logs captured through PowerShell `*>` when tests fail; junit files were used for counts.
  - `k0_checkpoint.py:86` is 122 characters (pre-existing).
