# T-007 K0 sync: scan, diff, plan, mass-deletion guard, info-file parsing

Kind: feature
R-IDs: R-007, R-014, R-015, R-016, R-017, R-025, R-026
Task file: tasks/active/T-007-k0-sync.md
Approach note: .tmp/T-007/research.md (the scout's research note, copied into the worktree)

Starts once T-006 is accepted (decision 96, `tasks/IMPLEMENTATION_PLAN.md:15`). Odd pipeline task: Codex (`high`) implements. Gemini 3.8 Flash reviews first and Claude reviews second; the patch is never integrated on the Gemini pass alone (decision 91, family split). While the user is away, a product-meaning question gets the PM's conservative, reversible choice, logged as a PM decision pending user review, unless it changes a requirement (decision 94).

## Brief header

- **Goal:** the K0 stage of `kb sync` (`docs/stages/K0-sync.md`, `docs/execution.md` "The kb sync flow"):
  - **Scan:** the whole source tree under `knowledge.root`, read only (R-014). Hash only files whose size or modification time changed; `image_id = sha256:<hex>` of the bytes; the work runs over a bounded worker pool.
  - **Parse:** each concept folder's info file (R-007, decisions 9, 10, 30, 59 and 60).
    - Line 1 holds the names and line 2 the descriptions, both split by `knowledge.concept_separator` and matched by position.
    - Names are compared after NFKC plus case folding only (`contracts/README.md:22`).
    - When line 2 does not split into as many parts as line 1 has names, the line is kept verbatim and the folder is flagged for K2.
    - A folder with no usable info file gets a pending name.
    - Only the info file and the configured image extensions are read; every other file is ignored.
  - **Record:** the rows of `folders`, `concepts`, `folder_concepts`, `images` and `folder_images` through the T-006 storage layer.
  - **Diff:** against the current snapshot, giving a plan: added, changed, renamed, moved and removed folders, images and concepts; a moved image keeps its `image_id`.
  - **Plan only:** the `--plan` mode stops after the diff.
  - **Guard:** the mass-deletion guard (`sync.mass_deletion_guard.max_removed_fraction`, `--force` overrides).
  - **Checkpoints:** in `sync_state`, so a rerun resumes and is idempotent (R-017).
  - **The sync report:** model-named and pending folders, description-count mismatches, and unreadable files.
  - K1 to K5 are not built here. K0 exposes the plan and the rows they consume, and the hand-off points where `kb sync` will call them.
  - Every setting comes from the validated configuration of T-005, with the keys K0 reads added to the `kb sync` read set. A missing value or a sentinel in a read key is refused at start (R-015).
  - Text read from info files is data, never instructions (R-026).
- **Done check:** the new tests pass through your worktree's `tools/agent-workflow/test_slot.py` with `D:/Project_github/prompt-enhancer-pipeline/.venv/Scripts/python.exe` and `PYTHONPATH=src`, covering at least the scout's test list (`.tmp/T-007/research.md` section 6):
  - scan and a first snapshot;
  - NFKC and case-folding identity, with the name kept as written;
  - an identical image in two folders;
  - removal and the incremental diff;
  - folder rename and image move;
  - missing, empty and undecodable info files;
  - a line-2 count mismatch;
  - ignored files;
  - an unreadable image file;
  - the guard with and without `--force`;
  - an interrupted sync resumed twice with identical state.
  - Also: property tests (Hypothesis) for the parser and the diff (a rename changes no artifact key; a rerun on an unchanged tree plans nothing); a non-default override test and a refusal test per key K0 reads; Windows and POSIX path forms (`folder_id` uses forward slashes, byte for byte); 3 to 10 mutants per changed module; and the whole pipeline suite.
- **Scope:**
  - New K0 modules under `src/prompt_enhancer/` and their tests; the `kb sync` read set in `config.py` for the keys K0 reads; `.tmp/T-007/` of the worktree; this task file.
  - A schema, contract or storage-layer change that K0 needs is proposed in Step 0 with its exact text, and made only after approval.
  - Read-only otherwise: `configs/`, `contracts/`, `docs/`, `tasks/REQUIREMENTS.md`, and the T-006 storage modules.
- **Out of scope:**
  - K1 to K5 logic: embeddings, model calls for info assessment, naming and description assignment, cards, aliases and the index.
  - The serving path.
  - Network, GPU and cloud calls; installs outside the project `.venv` (a missing package is a Step 0 question).
  - `.secrets/`, `database/` (the user's knowledge base: never read by tests; use synthetic trees under the worktree `.tmp/`), `data/` and `tools/agent-workflow/`.
- **Output:** the patch in the worktree (unstaged), the Completion report in this task file, and a reply under 15 lines.
- **Step budget:** about 80 tool calls after Step 0 approval.
- **Run limit:** CPU only. Every test is bounded to seconds. Synthetic trees go under the worktree `.tmp/T-007/`. Child processes use `CREATE_NO_WINDOW`.
- **May assume / Must ask:**
  - May choose module, class and function names, the CLI wiring inside the existing command layout, and the test layout.
  - Must ask (Step 0 questions) about:
    - the guard's denominator (scout question 1);
    - the pending-name representation in `folders` (scout question 2);
    - how K0 gets its worker count, given `runtime.cpu_workers` is not in the `kb sync` read set (scout question 3). A silent `os.cpu_count()` default is not allowed (R-015).
    - info-file precedence when several configured names exist (scout question 4);
    - any new configuration key and its sentinel;
    - any storage-layer change;
    - any contract ambiguity.
- **Role pack and CLI wrapper:** implementer; Codex (`high`). Step 0 is its own message; implementation starts after the PM approves.
- **Test sets:** the pipeline suite under `tests/`; run it all plus the new tests.
- **Trivial:** no. **Small:** no.
- **Base:** the pushed commit that accepts T-006; working folder `.tmp/worktrees/T-007`.

### Research required before code

1. The scout's note `.tmp/T-007/research.md`. Re-read every line it cites before relying on it (R-021). It was written before T-006 was accepted, so work from the accepted storage code.
2. The contracts `contracts/README.md:15-47,97,115-117,128-146`, `docs/execution.md`, `docs/model-storage.md`, `docs/stages/K0-sync.md`, `kb/README.md`, and the stages that consume K0 (`K1-embed.md`, `K2-observe.md`).
3. The T-006 storage API as accepted (`src/prompt_enhancer/storage/`), with its writer, checkpoint and snapshot rules, and its single-writer and link rules.
4. Stat resolution on Windows and Linux for the size-or-mtime shortcut: measure it on this machine with a bounded test, and leave Linux to CI.

### Configuration impact

To be filled in Step 0: every key K0 reads, its validation, example value, and override and refusal tests (template `.agents/policy/AGENT_TASK_TEMPLATE.md`).

## Steps

1. Step 0 (plan only), then stop for PM approval.
2. Implement, test, mutate, and write the Completion report.

## Completion report

Not started.
