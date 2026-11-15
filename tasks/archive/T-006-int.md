# T-006 integration: the anchored storage layer meets T-012 profiles

Kind: code. Small: no. R-IDs: R-014, R-015, R-017 (T-006 storage race follow-up, decision 123; T-012 P profiles).

## Why

The T-006 race follow-up (round 3, accepted by its reviews: Sonnet retest 3 PASS with 4 P3, Gemini PASS) was built on commit b4eb856. It replaces every by-name file mutation below a store root with the anchored layer `src/prompt_enhancer/storage/_anchored.py`, and removes the by-name helpers `_temporary`, `fsync_dir` and the path-based `atomic_write_bytes` from `storage/_fs.py`. Main has since gained T-012 (commit 350df5e), whose `src/prompt_enhancer/profiles.py` still imports and uses those helpers by absolute path. This worktree is main (350df5e) with the T-006 race patch applied unchanged; collecting the suite fails (`collection-error.log` beside this file):

```text
from .storage._fs import FileLock, StorageError, _temporary, atomic_write_bytes, fsync_dir
ImportError: cannot import name '_temporary' from 'prompt_enhancer.storage._fs'
```

Files beside this one: `race-task.md` (the race follow-up's full task file: rounds, decisions, Configuration audit), `race-retest3-review.md` (the accepting review and its P3 findings), `ci-link-test.log` (CI run 43729022948, the `test_link_resolved_containment` failure on both hosts), `collection-error.log`.

## Goal

One storage write discipline for the whole package: every file create, write, rename, lock-file open and delete in `src/prompt_enhancer` goes through the anchored layer (or is refused), with T-012's behaviour unchanged, and the suite green.

## Scope

- `src/prompt_enhancer/profiles.py` (T-012): `_publish_new` (no-replace publish: hard link of a fsynced temporary, fallback to exclusive create), `atomic_write_bytes` for manifests, golden results and `activation.json`, and the `FileLock` of `check.lock`. Keep every T-012 guarantee: register never overwrites an existing manifest (`FileExistsError` -> its refusal code), confirm's final comparison, `PROFILE_BUSY`, `PROFILE_CHANGED`, the lock timeout keys `storage.lock.timeout_s`/`poll_s`. The profile bundle root and its relation to the project root decide the anchored root: read how T-012 resolves `profiles/<id>/` and choose the root the containment rule needs; say which in Step 0.
- Any other by-name mutation in `src/` on main that the race follow-up did not see (it was built before T-012 landed): grep and list them all.
- The AST test of the race follow-up (`tests/test_storage_anchored*.py`): extend it to every module of `src/prompt_enhancer` (not a list of eight), and cover the forms retest 3 found missing: `from os import replace`, `getattr(os, ...)`, `Path.open('wb')`, `io.open`, `tempfile`, ctypes calls.
- `tests/test_storage_properties.py::test_link_resolved_containment`: CI shows it over-strict on both hosts (`ci-link-test.log`); retest 3 showed `a/link` never passes through `root/link`. Correct the expectation to what containment means, keep it a property test, and show it still fails on a real escape (a mutant of `inside`).
- Retest 3 P3s, if small: a depth-2 snapshot test that kills the surviving top-down `_remove_tree` mutant; `os.makedirs(root)` by name for the store root (`_anchored.py:155-158`): anchor it or record why not.

## Out of scope

- T-012's open P3 items (R2-1 to R2-7) and its R2-5 fingerprint scope, except where the storage change touches them: R2-2 (the lost-edit window between confirm's comparison and its replace) may shrink if the anchored rename allows it; report, do not redesign.
- T-008 (K1, in another worktree), tooling under `tools/`.

## Done check

- the suite collects and passes: the storage, K0 and P tests, then the full pipeline suite once, through this folder's `tools/agent-workflow/test_slot.py` with `<project>/.venv/Scripts/python.exe` and `PYTHONPATH=src`;
- the extended AST test passes on your code and fails on a planted by-name write in `profiles.py`;
- real-junction probes on Windows for the moved `profiles.py` writes (a junction swapped into `profiles/<id>/` before and during register, check and confirm): refused, nothing written outside;
- 3 to 10 mutants on the changed lines;
- an updated Configuration audit (no new keys expected; say so if one is needed).

## Round 1

### Round 1, Step 0

Unknowns: (1) whether `FILE_RENAME_INFORMATION` with `ReplaceIfExists = 0` on the temporary's handle refuses an existing target atomically on NTFS from ctypes (documented: `STATUS_OBJECT_NAME_COLLISION`; to confirm by the F2 regression on this host); (2) whether the Windows chain held from the project root (directories without `FILE_SHARE_DELETE`) conflicts with anything T-012 tests do meanwhile (expected no: they only read). Both are checked by tests, not assumed.

**Anchored root for profile bundles: the resolved project root**, relative name = the resolved bundle below it plus the file name (`configs/profiles/<id>/manifest.yaml` in the example profile). Reason: T-012's containment rule is `config._inside(path, project_root)` (`config.py:157-162`): `_bundle` (`profiles.py:67-82`) resolves `profiles.root` and then `<root>/<id>` through `_resolve_within`, which follows links that stay inside the project and refuses only what leaves it or enters `.secrets`. The project root is therefore the only directory the rule trusts; anchoring there (a) keeps T-012 behaviour, since a bundle reached through an in-project link is written at its resolved, link-free spelling, (b) verifies every component from the project root down at the moment of each act on both OSes (POSIX `O_NOFOLLOW` chain, Windows final-path check), so a junction swapped into `profiles/<id>/` or into any ancestor after `_bundle` is refused, and (c) never needs the by-name `os.makedirs` of `_directory`, because the project root always exists. Dropped: the resolved bundle as root (its own ancestors are opened by name; on POSIX `O_NOFOLLOW` checks only the last component) and the resolved `profiles.root` as root (same ancestor gap, and it would refuse a bundle that is an in-project link, a T-012 behaviour change). Cost: every segment below the project root must pass `check_segment` (an 8.3-like `~<digit>`, non-NFC or trailing-dot folder in the profiles path is now refused; fail closed, see Q4).

**`_publish_new` onto the anchored layer: a new primitive `publish_new_bytes(root, relative, data)`**, the `_write` path (temporary `FILE_CREATE`/`O_CREAT|O_EXCL|O_NOFOLLOW` in the verified parent, write, fsync, read back) with a no-replace commit:
- Windows: the rename on the temporary's handle with `ReplaceIfExists = 0` and `RootDirectory` = the held parent: atomic, never replaces (`STATUS_OBJECT_NAME_COLLISION` -> `FileExistsError`, already mapped at `_anchored.py:471-472`); the reader sees no file or the whole file. No fallback is needed (rename exists on every Windows file system).
- POSIX: `os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)` (`linkat`, in `os.supports_dir_fd` and `supports_follow_symlinks`: installed `Lib/os.py:114,176`), which fails with `EEXIST` and never replaces; then `unlink(temp, dir_fd=fd)` and `fsync(fd)`. Fallback, as T-012: any other `OSError` from `linkat` -> exclusive create `O_CREAT|O_EXCL|O_NOFOLLOW` relative to the same verified parent (no-replace kept, partial file visible while written).
- On any failure the temporary is discarded on its handle, so the bundle holds no leftover (T-012 test asserts the folder listing).
- Why not `rename_entry` without replace: on NTFS it would keep the guarantee, but its POSIX branch is stat-then-`renameat`, and `renameat` replaces an existing file, so an admin's manifest created in between would be overwritten (the F2 class). `rename_entry` also needs the temporary to exist by name first.

**Other profile writes:** confirm's manifest, `golden_results.json`, `activation.json` -> `_anchored.atomic_write_bytes(project_root, relative, data)`; check's activation removal -> `remove_entry` with `FileNotFoundError` tolerated (same as `missing_ok`); `check.lock` -> `_anchored.lock_file(project_root, relative, timeout_s, poll_s, create=True)` (same `storage.lock.*` values; a sharing violation stays "not acquired" -> `PROFILE_BUSY`). Anchored refusals (`StorageError`, link or junction) propagate unchanged; the CLI already reports `StorageError` (`__main__.py:32`).

**R2-2** (lost hand edit between confirm's comparison and replace): the anchored rename does not shrink it (an editor takes no lock; the window is compare-then-replace). Reported, not redesigned.

**Plan** (finish = the brief's done check):
1. `_anchored.py`: `publish_new_bytes` (Windows no-replace commit, POSIX `linkat` + exclusive-create fallback); store root created anchored from the project root instead of `os.makedirs` by name, if Q3 is accepted.
2. `profiles.py`: drop the `_fs` helpers, `os.link` and the unlink; route the five sites through the anchored layer at the project root; `_bundle_lock` through `lock_file`.
3. Tests: adapt T-012 tests (`FileLock(path, ...)` -> `lock_file`; the two F2 tests hook the anchored no-replace commit instead of `profiles.os.link`, labelled scheduling doubles, same intent); real-junction probes for register, confirm and check (junction swapped into `profiles/<id>/` before the call and at the commit hook); AST test over every module of `src/prompt_enhancer` except `_anchored.py`, with the forms below and a control per form, plus a planted by-name write in a copy of `profiles.py`; `test_link_resolved_containment` corrected (links at depth 1 and 2, junction fallback on Windows) and a mutant of `inside` that fails it; depth-2 snapshot test for the `_remove_tree` mutant.
4. Mutants 3-10 on the changed lines (scratch copy, per-run timeout).
5. Storage, K0, P tests, then the full suite once, via `test_slot.py`; Configuration audit; cleanup.

**Questions (ranked, proposed answer in bold):**
1. Anchored root for bundles = **the resolved project root** (above), rather than the bundle or `profiles.root`?
2. POSIX fallback when `linkat` fails with an error other than `EEXIST`: **keep T-012's breadth (any other `OSError` -> anchored exclusive create)**, since exclusive create also never replaces; narrowing to "no hard links" errnos would add a protocol list and change T-012's behaviour.
3. Store-root P3-B (`_anchored.py:155-158`): **anchor it**: the stores pass their project root, and a missing store root is created by the anchored chain from the project root on first write (no creation on read-only commands, as now); `_directory` then refuses a missing root instead of `os.makedirs`. If it grows past about 30 changed lines in `store.py`, record why not and leave it.
4. Profile paths below the project root that fail `check_segment` (`~<digit>`, non-NFC, trailing dot or space): **refuse (fail closed) with the anchored layer's message**, no new rule; T-012 accepted them only because it wrote by name.
5. A junction refusal during register/confirm/check: **propagate the `StorageError` unchanged** (no new `ProfileError` code; the contract has no code for it and the CLI reports it), rather than mapping to `PROFILE_CHANGED`.

### PM approval (round 1)

Approved as written. Answers: Q1 project root; Q2 keep T-012's fallback breadth, but the fallback is an anchored exclusive no-follow create relative to the verified parent, tested with a forced `linkat` failure; Q3 anchor the store-root creation from the project root (residual if it exceeds about 30 lines); Q4 refuse profile folder names that fail `check_segment`, one test per class: **PM decision pending user review**; Q5 `StorageError` propagates unchanged; report how the profile commands surface it.

### Configuration impact (round 1)

- No new setting. Read unchanged: `profiles.root`, `profiles.exclude` (bundle resolution, `_bundle`); `storage.lock.timeout_s`/`poll_s` (the bundle lock, now `_anchored.lock_file`); `storage.kb_store`, `storage.data_root` (store roots, now created anchored from the project root that `_settings` resolves). None changes meaning or default.
- New literals: POSIX `linkat` (`follow_symlinks=False`), `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, `ReplaceIfExists = 0`: protocol-bound. Temporary name `.<name>.<uuid>.tmp` in the bundle: artifact-bound (the form T-012 already used via `_temporary`).
- Behaviour change, fail closed (Q4): a profile path component below the project root that fails `check_segment` is refused at write time.

### Class audit (S31), round 1

**By-name file mutations in `src/` on main (350df5e + race patch)**: grep of `src/prompt_enhancer/**/*.py` outside `_anchored.py` for `open(`, `.write_*`, `.mkdir`, `makedirs`, `.unlink`, `.rmdir`, `.rename`, `.replace(`, `.touch`, `os.(remove|rename|replace|link|symlink|truncate|chmod|utime|unlink|rmdir|open|write)`, `shutil`, `tempfile`, `io.open`, `getattr(os`, `from os import`, `ctypes`/`WinDLL`, `pq.write_table`/`ipc.new_file`/`OSFile`, `json.dump(`/`yaml.dump(` to a stream, `.save(`, `subprocess`:

| # | Call | Module:line | What it writes | Verdict |
|---|---|---|---|---|
| 1 | `_temporary(path)` + `open(tmp, 'xb')` + `write` + `fsync` | `profiles.py:226-231` | temporary `.manifest.yaml.<uuid>.tmp` in the bundle | by name: move (`publish_new_bytes`) |
| 2 | `os.link(tmp, path)` | `profiles.py:233` | `manifest.yaml` (no-replace publish, F2) | by name: move |
| 3 | `open(path, 'xb')` + `write` + `fsync` | `profiles.py:237-240` | `manifest.yaml` (no-hard-link fallback) | by name: move (POSIX fallback) |
| 4 | `fsync_dir(path.parent)` | `profiles.py:241` | bundle directory entry durability | by name: move (fsync on the held parent fd) |
| 5 | `tmp.unlink(missing_ok=True)` | `profiles.py:243` | removes the temporary | by name: move (unlink by `dir_fd` / delete on handle) |
| 6 | `atomic_write_bytes(path, data)` | `profiles.py:372` | `manifest.yaml` (confirm, replace) | removed helper: move |
| 7 | `FileLock(path, timeout, poll)` (opens `a+b`, creates) | `profiles.py:439` | creates `check.lock` | removed form (no `opener`): move to `lock_file` |
| 8 | `.unlink(missing_ok=True)` | `profiles.py:562` | removes `activation.json` (check start) | by name: move (`remove_entry`) |
| 9 | `atomic_write_bytes(path, report_bytes)` | `profiles.py:604` | `golden_results.json` | removed helper: move |
| 10 | `atomic_write_bytes(path, record)` | `profiles.py:629` | `activation.json` | removed helper: move |
| 11 | `os.makedirs(root, exist_ok=True)` | `storage/_anchored.py:158` | the store root (first write) | by name, inside the layer: Q3 |
| - | `open(path, 'rb')` `_fs.py:42`; `pa.OSFile(.., 'rb')` `_fs.py:58`; `os.open(.., O_RDONLY|O_NOFOLLOW, dir_fd)` + ctypes `CreateFileW`/`GetFileInformationByHandleEx` `k0_scan.py:72,78-100` (read access only); `Image.open(BytesIO)`/`image.save(BytesIO)` `model_image.py:52,80`; `yaml.dump(...)` to a string `profiles.py:186`; `.replace` on strings `config.py:76,404`; `getattr(os, 'sched_getaffinity')` `k0.py:13` | n/a | nothing | reads / in-memory: not mutations |

No other module (`k0*`, `model_*`, `profile_lint`, `profile_prompt`, `config`, `__main__`, `storage/{identity,tables,store,_fs,__init__}`) writes a file by name; `store.py` writes only through `_anchored` (`store.py:71,309-357,1233`).

**T-012 guarantees that depend on a write primitive:**

| Guarantee (T-012) | Primitive today | Anchored mapping | Test that holds it |
|---|---|---|---|
| G1 register never replaces an existing manifest; `FileExistsError` -> `PROFILE_EXISTS ... kept as written` | `os.link` (no replace); fallback `open('xb')` | `publish_new_bytes`: Windows rename `ReplaceIfExists=0`; POSIX `linkat`, fallback `O_EXCL` | `test_register_never_replaces_an_interposed_manifest[supported/unsupported]` (rehooked) |
| G2 a reader never sees a partial manifest (where hard links exist) | link of a fsynced temporary | rename / `linkat` of the fsynced temporary | same; folder listing has no temporary |
| G3 confirm: final comparison, then atomic replace (`PROFILE_CHANGED` before it) | `atomic_write_bytes` (temp + `os.replace`) | `_anchored.atomic_write_bytes` (replace on handle) | confirm `PROFILE_CHANGED` tests |
| G4 a new check starts by removing `activation.json`, so a failed newer check leaves the profile inactive (F1) | `unlink(missing_ok=True)` | `remove_entry`, `FileNotFoundError` tolerated | check-failure-leaves-inactive tests |
| G5 `golden_results.json` and G6 `activation.json` are whole when visible, written after the final comparison | `atomic_write_bytes` | `_anchored.atomic_write_bytes` | resolve tests (hash of results) |
| G7 one bundle lock for register, confirm, check; `PROFILE_BUSY` after `storage.lock.timeout_s`, polled at `poll_s`; created if missing, never deleted | `FileLock` open `a+b` by name | `lock_file(project_root, rel, timeout_s, poll_s, create=True)` | `test_check_lock_wait_follows_storage_lock`, `test_check_lock_refusal`, register/confirm busy tests |
| G8 durability: file fsync before publish, directory fsync after (POSIX) | `os.fsync`, `fsync_dir` | fsync of the temporary's handle and of the held parent fd | not observable in tests (assumption, as in round 3) |

**AST forms the extended test must catch** (every `src/prompt_enhancer/**/*.py` found by glob except `storage/_anchored.py`; a read-only allowance only for `open`/`os.open` with a literal read-only mode or flags): `os.<mutation>(...)` and `import os as o; o.<mutation>(...)`; `from os import replace/rename/remove/unlink/link/symlink/makedirs/mkdir/rmdir/truncate/open` (any alias); `getattr(os, <anything>)` other than a literal non-mutating name; `open(p, <write or non-literal mode>)`, `io.open`, `from io import open`, `codecs.open` with a write mode; `<x>.open('wb'|'w'|'a'|'x'|'+'...)` (`Path.open`, `os.fdopen` non-read modes); `Path` mutators `.unlink/.rmdir/.mkdir/.rename/.replace(one arg)/.touch/.write_bytes/.write_text/.symlink_to/.hardlink_to/.chmod`; `shutil` and `tempfile` imports or calls (any alias); `ctypes` / `_winapi` imports outside the allowed read-only scanner (`k0_scan.py`, listed with its reason) and calls through `windll`/`WinDLL`; `msvcrt` calls other than `locking` on a held handle (`_fs.py`); pyarrow writers (`write_table`, `new_file`, `new_stream`, `ParquetWriter`, `write_feather`, `OSFile`/`output_stream` with a write mode) under any alias; the removed helper names (`_unlink`, `_temporary`, `_commit`, `fsync_dir`) and a direct `FileLock(` outside `_anchored`. Controls: one source per form must be flagged, and the read-only forms in `src/` today must not be.

As built, the guard differs from this plan in two places. `msvcrt` has no rule of its own: `_fs.py` uses only `locking`, and `k0_scan.py` uses `open_osfhandle` with `O_RDONLY`. Instead, any reference to a write flag (`O_WRONLY/O_RDWR/O_CREAT/O_TRUNC/O_APPEND/O_EXCL/O_TMPFILE`) is flagged, so `os.open` stays read-only everywhere. The guard also flags `subprocess` imports, `__import__` and `import_module`.

### Changes (round 1)

- `storage/_anchored.py`:
  - `publish_new_bytes(root, relative, data)`: the `_write` path with a no-replace commit through `_publish` (hookable). On Windows it renames on the temporary's handle with `ReplaceIfExists = 0`. On POSIX it uses `linkat` relative to the verified parent with `follow_symlinks=False`. Any other `OSError` falls back to an anchored exclusive create (`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, `dir_fd`). The temporary is then unlinked by `dir_fd` and the parent fsynced.
  - `make_root(project_root, root)`: creates a store root through the verified chain from the project root, and refuses a root that is not strictly inside it.
  - `_directory` no longer calls `os.makedirs` (a missing root is the open's own `FileNotFoundError`). `_write` takes a `commit`. `_posix_available` also requires `os.link` in `supports_dir_fd` and `supports_follow_symlinks`.
- `storage/store.py`: `_ensure_root` (`make_root` when the root is absent), used by `_lock(create=True)`, `_write_table`, `_write_bytes` and `DataStore.append` (12 lines; the PM's limit was about 30).
- `profiles.py`:
  - All five sites now go through the anchored layer with the resolved project root as root: `_publish_new` -> `publish_new_bytes`; confirm, golden results and activation -> `_write_bundle_file` (`atomic_write_bytes`); the activation removal -> `remove_entry` with `FileNotFoundError` tolerated; `_bundle_lock` -> `lock_file(..., create=True)`.
  - `_anchored_name` applies `check_segment` to every component below the project root. `_bundle` calls it, so a bad name is refused before any read, paid call or write (Q4).
  - Removed: the `os` import, the `_fs` helper imports and `FileLock`.
- Tests:
  - `test_profiles.py`: `FileLock(path, ...)` -> `lock_file(folder, 'check.lock', ...)`. The two F2 tests now hook `_anchored._publish` (scheduling double) instead of `profiles.os.link`. The forced `linkat` failure (`EPERM` double on `_anchored.os.link`) is POSIX-only, both for the interposed case and for publish-without-hard-links.
  - New `test_profiles_anchored.py`:
    - junction or symlink swapped into `profiles/<id>/` after the anchored name check, for the 5 writes and the 3 lock opens (bundle lock moved elsewhere by a labelled double for the writes);
    - Windows: with the real lock held, the swap itself is refused (`PermissionError`) and the write lands in the bundle;
    - before the call: a bundle linked out of the project is refused by `_inside`, as in T-012;
    - Q4: one test per segment class (`~1`, NFD, control character, Windows-forbidden character) across register, confirm, check and resolve, with no drafter call, plus a `profiles.root` containing `prof~1`;
    - the bundle listing is unchanged after register, confirm and check.
  - `test_storage_anchored_writes.py`:
    - AST guard over every module found by glob (24 modules, `_anchored.py` excluded), 45 form controls and a read-only control;
    - planted by-name writes in the real `profiles.py` source (3 forms);
    - depth-2 snapshot GC (P3-C);
    - store root created anchored with a swap at its parent (P3-B);
    - the layer never creates a missing root;
    - `make_root` refuses a root outside the project or equal to it.
  - `test_storage_properties.py::test_link_resolved_containment`: links leaving the root at depth 1 (`root/link`) and depth 2 (`root/a/link`), with a junction fallback on Windows. A name that crosses a link must be refused, and any other valid name must be accepted.
  - `test_storage_config.py` (2 tests) and `test_storage_review_r3.py` (2 tests) wrote with the anchored layer into a store root that did not exist yet. They now create it with `make_root` first; their intent is unchanged.

### Q5: how the profile commands surface a `StorageError`

- No profile command is on the command line yet: `__main__.py:14-35` defines only `kb sync`. That entry point catches `(ConfigurationError, StorageError, OSError, ValueError)`, prints the message to stderr and returns 1, with no traceback.
- At the library API (`register_profile`, `confirm_profile`, `check_profile`, `resolve_profile`), the anchored refusal propagates unchanged as `StorageError`. The bundle lock is released in each `finally`.
- Once the profile commands are wired, the same `except` clause will cover them. I did not wire them (not in scope).

### Research evidence (round 1)

| Question | Source / command | Finding | Class |
|---|---|---|---|
| Does `linkat` with `dir_fd` exist and refuse an existing name? | installed `Lib/os.py:114` (`_add("HAVE_LINKAT", "link")` in `supports_dir_fd`), `:176` (`supports_follow_symlinks`); POSIX `link(2)`: `EEXIST` "newpath already exists" | yes; never replaces | source-verified; POSIX branch not run on this machine (Linux CI) |
| Does the rename with `ReplaceIfExists = 0` refuse an existing name on NTFS? | `test_register_never_replaces_an_interposed_manifest[supported]` on this host; mutant M1 (`replace=True`) kills it | `FileExistsError` -> `PROFILE_EXISTS`; the admin's bytes are kept and no temporary is left | experiment |
| Did T-012's by-name writes really escape? | `.tmp/T-006-int/before/` (`build_before.py`: `src/` with main's `profiles.py` and its helpers from main's `_fs.py`), `test_regress_profiles.py` (real junction swapped in right after `profiles._inside`); logs `regress-before.log`, `regress-after.log` | before: `7 failed in 2.86s`. A manifest was created outside (register, no victim); confirm overwrote the outside manifest; check deleted the outside `activation.json`, wrote `golden_results.json` and `activation.json` outside, and created `check.lock` outside. Register with a victim present was stopped only by no-replace. After: `7 passed in 2.58s`, every case `StorageError`, outside unchanged | experiment (real junctions, Windows) |
| Does the chain refuse a swap after its own name check? | `test_profiles_anchored.py::test_a_link_swapped_into_the_bundle_during_a_write_is_refused` (8 cases) | `StorageError ... is a link or junction: refused`; outside unchanged | experiment (real junctions, Windows); POSIX via symlinks on CI |
| Can the bundle be swapped while the lock is held? | `test_the_held_bundle_lock_stops_the_swap_on_windows` (5 cases) | no: `PermissionError` on the rename, because the open `check.lock` is a descendant | experiment, Windows only |

Per fix (PM request):
- Reproduced by a real-junction probe on Windows: the profile writes (before/after above), the store-root creation (`test_store_root_is_created_relative_to_the_project_root`; mutant M3 restores the by-name creation and is killed), and the containment property (junction fallback; mutant M7 killed).
- Reproduced by a real test: the depth-2 GC (no junction needed).
- Reasoned and source-verified only: the POSIX `linkat` and exclusive-create fallback (the forced-failure tests run only on Linux CI).

### Tests and mutants (round 1)

Runner `.tmp/T-006-int/run.sh <src> <basetemp> <args>`: PYTHONNOUSERSITE, PYTHONDONTWRITEBYTECODE, PYTHONPATH=`<src>;tests`, TEMP under `.tmp/T-006-int/`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <same> -B -X faulthandler -m pytest -q -p no:cacheprovider`.

- First run after the source change (P and anchored files): `264 passed, 8 skipped in 99.30s`.
- New tests (probes, store root, containment): `73 passed, 4 skipped, 71 deselected in 18.20s`. AST guard: `74 passed, 41 deselected in 0.80s`.
- Storage, K0 and P (`tests/test_storage*.py tests/test_k0*.py tests/test_profile*.py`): first run `12 failed, 845 passed, 10 skipped in 262.41s` (`skp.log`). All 12 were the root-creation test setups named above (`FileNotFoundError: [WinError 2]`). After the fix, those two files gave `140 passed in 58.63s`. The full suite below reruns all of them.
- Mutants (`mutate.py`: scratch copy of `src/` per mutant, the named tests, 600 s timeout with kill, `CREATE_NO_WINDOW`; `mutants.log`): **10/10 KILLED**.
  - M1: Windows publish replaces (DID NOT RAISE ProfileError).
  - M2: `_directory` creates the root by name (DID NOT RAISE FileNotFoundError).
  - M3: `_ensure_root` uses `os.makedirs` (DID NOT RAISE StorageError).
  - M4: `_bundle` without the segment check (resolve reached `PROFILE_NOT_ACTIVE`).
  - M5: the activation removal swallows `StorageError` (the check went on through the junction, then a ConfigurationError, not the anchored refusal).
  - M6: register replaces (DID NOT RAISE).
  - M7: `inside` without resolution ("link ... was accepted").
  - M8: `make_root` without containment (message mismatch).
  - M9: a by-name `path.write_bytes` planted in a real copy of `profiles.py` (AST: `381: .write_bytes()`).
  - M10: the bundle lock opened without create (`FileNotFoundError`).
- `git diff --check -- src tests`: exit 0. The only lines over 120 characters in changed files already exist on main (`profiles.py` 4, `test_profiles.py` 1). `ruff check` and `ruff format --check`: not_run (`No module named ruff` in `.venv`; no installs allowed).
- Full pipeline suite (`tests`, serial, once): `2194 passed, 40 skipped, 4 warnings in 541.79s (0:09:01)`; junit `tests=2234 failures=0 errors=0 skipped=40` (`full.log`, `full.xml`; warnings are the known Pillow deprecations in `test_model_image.py`).

### Configuration audit (round 1)

No setting, schema or example profile is added or changed.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| bundle lock wait (register, confirm, check) | `storage.lock.timeout_s`, `storage.lock.poll_s` | `store._settings` (`_positive_finite`, poll <= timeout) | `configs/enhancer.example.yaml` (existing) | override: `test_check_lock_wait_follows_storage_lock`, `test_bundle_lock_serializes_writers[register/confirm]` (0.4 s / 0.1 s); refusal: `test_check_lock_refusal[missing/sentinel/poll_above_timeout]` | configurable (existing) |
| bundle location (anchored below the project root) | `profiles.root`, `profiles.exclude` | `_configured_path`, `_inside`, now also `check_segment` per component (Q4) | example profile (existing) | refusals: `test_a_profiles_root_outside_the_segment_rule_is_refused`, segment-class tests; T-012's existing `profiles.root` tests | configurable (existing) |
| store roots created anchored | `storage.kb_store`, `storage.data_root` | `_resolved_path`; `make_root` strictly inside the project root | example profile (existing) | `test_kb_store_override_and_refusal`, `test_data_root_override_and_refusal` (existing); `test_make_root_refuses_a_root_that_is_not_strictly_inside_the_project` | configurable (existing) |
| `ReplaceIfExists = 0`, `linkat` `follow_symlinks=False`, `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0o666` | none | F2 tests (Windows); POSIX forced-failure tests (CI) | n/a | n/a | protocol-bound |
| temporary `.<name>.<uuid>.tmp` in the bundle | none | folder-listing asserts in the F2 tests | n/a | n/a | artifact-bound (T-012's form) |
| fallback breadth: any `OSError` but `EEXIST` -> exclusive create | none | `test_register_publishes_without_hard_links` (POSIX) | n/a | n/a | safety-bound (never replaces) |
| segment refusal of bundle paths | none | segment-class tests, M4 | n/a | n/a | safety-bound (PM decision pending user review) |
| `EPERM` double, 600 s mutant timeout | none | n/a | n/a | n/a | test-only |

### Completion report (round 1)

- Behaviour:
  - Every file create, write, rename, lock-file open and delete in `src/prompt_enhancer` goes through `storage/_anchored.py`. Profile bundles are anchored at the resolved project root, and the store roots are created from it.
  - T-012's guarantees are kept: no-replace registration (`PROFILE_EXISTS`); confirm's final comparison (`PROFILE_CHANGED`); activation removed at the start of a check; whole-file replaces; `PROFILE_BUSY` bounded by `storage.lock.*`.
  - A link or junction swapped into a bundle or above it is refused at the act; real-junction probes show T-012's code wrote, created and deleted outside.
  - The AST guard covers every module and fails on a planted by-name write in `profiles.py`.
  - The containment property is corrected and kills the `inside` mutant.
  - Retest 3's P3-B and P3-C are closed.
- R2-2 (confirm's lost-edit window): unchanged, because an editor takes no lock. Reported, not redesigned.
- not_run:
  - the POSIX branches (`linkat`, the exclusive-create fallback, symlink probes), which run on Linux CI;
  - `ruff check` and `ruff format --check` (ruff is not installed);
  - Linux, ReFS and SMB.
- Changed files: `src/prompt_enhancer/storage/_anchored.py`, `src/prompt_enhancer/storage/store.py`, `src/prompt_enhancer/profiles.py`, `tests/test_profiles.py`, `tests/test_profiles_anchored.py` (new), `tests/test_storage_anchored_writes.py`, `tests/test_storage_properties.py`, `tests/test_storage_config.py`, `tests/test_storage_review_r3.py`. Unchanged from the accepted race patch: everything else in `git status`.
- Evidence kept in `.tmp/T-006-int/`: `run.sh`, `mutate.py`, `mutants.log`, `skp.log`/`skp.xml`, `full.log`/`full.xml`, `regress-before.log`, `regress-after.log`, `before/` (the builder, main's two source files, the probe and its scratch conftest), `cleanup.py`. Scratch removed by `cleanup.py` (6 links removed as links, then the trees). No process is left running.
- Noticed, not done:
  - No profile command is on the command line yet (`__main__.py` has `kb sync` only).
  - Reads of bundle files (`_read_bytes`) still go by name. That is outside the mutation class, and the final hash comparisons fail closed.
  - Lines over 120 characters already on main: `profiles.py:175,211,213,690` and `test_profiles.py:169`.
