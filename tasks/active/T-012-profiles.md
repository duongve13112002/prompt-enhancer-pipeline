# T-012 P profiles: register, manifest drafting, conflict lint, golden tests

Kind: feature
R-IDs: R-001, R-004, R-005, R-015, R-018, R-021, R-026
Task file: tasks/active/T-012-profiles.md
Approach note: .tmp/T-012/research.md (the scout's research note, copied into the worktree)

Starts now that T-009 is accepted (decision 96, `tasks/IMPLEMENTATION_PLAN.md:20`; T-009 accepted in commit 6b03510). This is an even pipeline task, so Claude (Opus 5.5 `high`) implements and Codex reviews. The patch is never integrated on a Gemini pass alone (decision 91, family split). While the user is away, a product-meaning question gets the PM's conservative, reversible choice, logged as a PM decision pending user review, unless it changes a requirement (decision 94).

## Brief header

- **Goal:** the P stage (`docs/stages/P-profiles.md`; the `profile register` and `profile check` flows in `docs/execution.md`):
  - **Read the system prompt:** `system_prompt.txt` is read as bytes, verbatim. It is hashed and never edited: LF and CRLF are kept byte for byte (R-004).
    - **Sectioned form:** `[META]`, `[SYSTEM]` and `[USER]` sections are split structurally. `[SYSTEM]` is used verbatim, and `[META]` is recorded in the manifest only.
    - **Plain form:** the whole file is the system message (decision 33).
  - **Register:** `profile register` calls the `vlm.roles.manifest_drafter` role once through the T-009 model client. The call uses the `profile_manifest.v0` schema and passes the prompt text as data, never as instructions (R-026). It writes a draft manifest and the drafted conflict list into the profile bundle under `profiles.root`, with status `draft`.
    - Output language follows decision 67.
    - Edit semantics follow decision 73.
    - `{{aspect_ratio}}` takes the value `auto` from `template_values` (decisions 70, 81; R-005).
  - **Confirm:** the admin's confirmation moves the manifest to `confirmed`, with one admin decision per conflict. The pipeline never infers profile rules at run time.
  - **Conflict lint:** structural only. The code validates each conflict's shape and each admin decision (`profile_wins`, `config_wins`, `block_activation`, or undecided). It compares the configuration keys the manifest's conflicts cite with the resolved configuration.
    - Finding which rules clash is the drafter model's job. Code holds no word list, regular expression over natural language, or per-language rule (AGENTS.md section 5).
    - `config_wins` produces the runtime addendum that S4 appends: versioned, and shown in the trace.
  - **Check:**
    - `profile check` runs the golden tests through a pipeline-runner interface. S0 to S6 do not exist yet (T-013 to T-015), so tests use a labelled double for it.
    - It activates the profile only when every golden test passes, no conflict blocks or is undecided, and the stored hash still matches the bytes on disk.
    - A changed prompt or manifest after activation needs a new check.
  - **Read API for S0, S4 and S5:**
    - the active profile;
    - `supported_tasks`, with an edit request refused on a generate-only profile (decision 5);
    - the system message, the user template and its variables;
    - the addendum;
    - the validator checks.
  - Every setting comes from the validated configuration of T-005, and the keys P reads are added to the read sets of `profile register` and `profile check`. A missing value or a sentinel in a read key is refused at start (R-015).
- **Done check:** the new tests pass through your worktree's `tools/agent-workflow/test_slot.py` with the main checkout's `.venv/Scripts/python.exe` and `PYTHONPATH=src`, covering at least the scout's test list (`.tmp/T-012/research.md` section 7):
  - sectioned and plain parsing, including `[META]` kept out of the system text;
  - a byte-exact round trip of LF, CRLF and non-ASCII prompts;
  - registration with a labelled drafter double: a valid draft, an invalid draft refused, and a model error passed through as the T-009 error code;
  - each conflict decision, plus an undecided conflict;
  - the `config_wins` addendum;
  - hash drift blocking activation;
  - edit refused on a generate-only profile;
  - the example profile `configs/profiles/_example/` registering against the double, with its known conflicts c1 to c4 carried through.
  - Also: property tests (Hypothesis) for the section parser and the lint validator; a non-default override test and a refusal test per key P reads; 3 to 10 mutants per changed module; and the whole pipeline suite.
- **Scope:**
  - New P modules under `src/prompt_enhancer/` and their tests; the `profile register` and `profile check` read sets in `config.py`; `.tmp/T-012/` of the worktree; this task file.
  - A schema, contract, prompt (`manifest_drafter_v0`) or configuration change that P needs is proposed in Step 0 with its exact text, and made only after approval.
  - Read-only otherwise: `configs/`, `contracts/`, `docs/`, `tasks/REQUIREMENTS.md`, and the T-005, T-006 and T-009 modules.
- **Out of scope:**
  - S0 to S6 logic, beyond the read API and the runner interface.
  - Network, GPU and cloud calls; installs outside the project `.venv` (a missing package is a Step 0 question).
  - `.secrets/`, `database/`, `data/` and `tools/agent-workflow/`.
- **Output:** the patch in the worktree (unstaged), the Completion report in this task file, and a reply under 15 lines.
- **Step budget:** about 80 tool calls after Step 0 approval.
- **Run limit:** CPU only. Every test is bounded to seconds. Synthetic bundles go under the worktree `.tmp/T-012/`. Child processes use `CREATE_NO_WINDOW`.
- **May assume / Must ask:**
  - May choose module, class and function names, the CLI wiring inside the existing command layout, and the test layout.
  - Must ask (Step 0 questions) about:
    - how the input variable is designated (scout question 1);
    - where the runtime addendum lives and how it is versioned (scout question 2);
    - whether the `profile check` read set gains `creativity` and `validation` (scout question 3);
    - the golden-test file format inside the bundle (scout question 4);
    - the status transitions and who writes them (scout question 5);
    - any new configuration key and its sentinel;
    - any schema or contract change.
- **Role pack and CLI wrapper:** implementer; Claude Opus 5.5 `high`. Step 0 is its own message; implementation starts after the PM approves.
- **Test sets:** the pipeline suite under `tests/`; run it all plus the new tests.
- **Trivial:** no. **Small:** no.
- **Base:** commit 6b03510, which accepts T-009; working folder `.tmp/worktrees/T-012`.

### Research required before code

1. The scout's note `.tmp/T-012/research.md`. Re-read every line it cites before relying on it (R-021). It was written before T-009 was accepted, so work from the accepted model client (`src/prompt_enhancer/model_*.py`).
2. `docs/stages/P-profiles.md`, `S0-intake.md`, `S4-writer.md` and `S5-validate.md`; `docs/execution.md`; `contracts/README.md`; `contracts/schemas/profile_manifest.v0.schema.json`; `configs/profiles/_example/` (README, manifest, system prompt); `configs/enhancer.example.yaml` (the profiles section and `vlm.roles.manifest_drafter`).
3. The T-005 loader (`load_config`, `load_profile`, `COMMAND_READS` in `src/prompt_enhancer/config.py`) and the T-006 storage API, if the bundle or its activation record uses storage.

### Configuration impact

To be filled in Step 0: every key P reads, its validation, example value, and override and refusal tests (template `.agents/policy/AGENT_TASK_TEMPLATE.md`).

## Steps

1. Step 0 (this file, new heading "Step 0"), then STOP for PM approval.
2. Implementation, tests, mutants, the full suite, and the Completion report.

## Completion report

To be filled by the implementer.
