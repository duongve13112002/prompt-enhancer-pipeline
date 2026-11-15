# Implementation notes

Decisions, deviations, assumptions, tradeoffs, rejected alternatives and risks, with links (AGENTS.md section 9). A proposal here is never an approval.

## 2026-11-11 repository seeding

- **Built from:** the user's proposal v1.1 and the PM's design v2.2 ([N000](note/N000-design-provenance.md)), the user's answers of 2026-11-11 (decisions 1 to 27), and the agent tooling, policies and note format of an earlier project of the user.
- **Tooling:** `tools/agent-workflow/` and `scripts/agy-runner/` were copied without code changes; only `.agents/project.json` and `tools/agent-workflow/doc_budgets.json` hold new values. Known leftovers that need code changes are tasks T-020 to T-022.
- **Rules:** `AGENTS.md`, the brief footer and the template were reworded per decision 24; section and footer-rule numbers were kept because skills and tools cite them. Mapping: [agents/rule-map.md](agents/rule-map.md).
- **Deviations from the PM's design v2.2, forced by decisions 18 and 19:** no transliteration tables or word lists (aliases come from a model, negation is judged by the verifier); Parquet and Arrow instead of an embedded SQL store; every hand-typed size or count replaced by a sentinel.
- **Assumptions:** a folder's images all show every concept the folder declares (the user's statement for one concept, extended to several); the same concept name after Unicode normalisation is the same concept (decision 10).
- **Rejected:** a tool-calling agent loop for the writer (unpredictable latency, weaker isolation of retrieval-off and of scope); placing the whole concept catalogue in the planner context ([N120](note/N120-long-lists-in-context.md)); a code skeleton (AGENTS.md forbids stubs and implementation before an explicit command).
- **Risks:** a wrong concept card affects every request that uses it (audit E16 is the gate); most research notes are agent-read; the workflow changes of decision 24 rest on vendor guidance and are unmeasured (T-023); GitHub facts behind the CI window are unverified ([N134](note/N134-github-visibility-and-actions.md)).
- **Not done:** no tool, hook or test was run in this repository beyond the read-only `setup_project.py check` at seeding (result in [tasks/STATUS.md](../tasks/STATUS.md)); nothing was benchmarked.

## 2026-11-16 storage: one anchored write layer (T-006 race follow-up, T-006-int)

- **Decision (user, decision 123):** the store is locked while a release deletes; a junction or link swapped into a store path between check and act must never redirect a write.
- **Design:** every create, write, rename, lock-file open and delete in `src/prompt_enhancer` goes through `storage/_anchored.py`, which opens each path component relative to its verified parent without following links (Windows `NtCreateFile` with a root handle and rename on the handle; POSIX `dir_fd` with `O_NOFOLLOW`); an AST test refuses by-name write forms in every module. Profile bundles (T-012) are anchored at the resolved project root; register's no-replace publish is a handle rename without replace on Windows and `linkat` on POSIX, with an anchored exclusive-create fallback. Task files: [T-006-race-followup](../tasks/archive/T-006-race-followup.md), [T-006-int](../tasks/archive/T-006-int.md).
- **PM decisions pending user review:** round 3 covered the whole class instead of accepting a P1 residual; a failed `release()` becomes a note on the caller's exception, or a `StorageWarning` when the work succeeded; profile folder names that fail `check_segment` (8.3 short names, non-NFC, control or forbidden characters) are refused.
- **Rejected:** `rename_entry` without replace for register's publish (its POSIX branch checks, then `renameat` overwrites a manifest created in between); anchoring at the bundle or `profiles.root` (the folders above would still be opened by name).
- **Risks:** POSIX branches are source-read only until a Linux CI run; NTFS rename durability rests on metadata journaling (no directory fsync on Windows); the AST guard misses some write forms no code uses today (listed in [tasks/STATUS.md](../tasks/STATUS.md)).
