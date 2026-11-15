# T-005 Profile schema and loader

Kind: feature
R-IDs: R-015, R-004
Task file: tasks/active/T-005-profile-loader.md
Approach note: .tmp/T-005/research.md (the scout's research and hazard note, in the worktree)

Started 2026-11-15 (decisions 87 and 95); Codex implements, Claude reviews (decision 91); while the user is away a product-meaning question gets the PM's conservative, reversible choice, logged as a PM decision pending user review, unless it changes a requirement (decision 94).

## Brief header

- **Goal:** the first pipeline code: a Python package with a configuration loader that reads the enhancer configuration (`configs/enhancer.example.yaml`, `configs/enhancer.dev.example.yaml`, the overlay in `configs/overlays/`) and an image-model profile (`configs/profiles/_example/`: `manifest.yaml`, `system_prompt.txt`), validates them against the schemas under `contracts/schemas/` (`profile_manifest.v0.schema.json`, `vlm_role.v0.schema.json` and any schema the configuration files name), and refuses at start, with a precise message naming the key and file, every missing field, unknown key, wrong type, and every sentinel (`fit_from_gt`, `fit_from_bench`, `fit_from_pilot`, `pin_required`, `deploy_required`, `design_required`, `ask_user`) in a key the requested command reads (R-015, AGENTS.md section 5); keys a command does not read may keep their sentinel. VLM roles are validated per `contracts/vlm-request.md`. The admin's system prompt is loaded byte-for-byte and never edited (R-004).
- **Done check:** the new test suite passes through `tools/agent-workflow/test_slot.py` with the project venv python, covering: each example configuration and the example profile load for a command whose keys are all filled; one refusal test per failure kind (missing field, unknown key, wrong type, each sentinel in a read key, an invalid VLM role, a missing or unreadable system prompt file, a YAML 1.1 boolean trap on an enum value per `contracts/README.md:130`); a non-default override test per configurable value the loader reads; a byte-identity test of the system prompt; property tests for the validator (Hypothesis, if it is installed in the venv; report if not).
- **Scope:** a new package directory and its tests (layout chosen in Step 0 from `docs/dependencies-and-environments.md`, `docs/architecture.md` and `docs/execution.md`), a project metadata file if the layout needs one, `.tmp/T-005/` of the worktree, and this task file. Read-only: `configs/`, `contracts/`, `docs/`, `tasks/REQUIREMENTS.md`.
- **Out of scope:** any other stage (storage, sync, models, serving); network; installs outside the project `.venv` (report a missing package as a question instead of installing it); GPU; cloud calls; `.secrets/`; `database/` (read only, not needed); `tools/agent-workflow/`; editing `configs/`, `contracts/` or `docs/` (a defect found there goes under `Noticed, not done` with the proposed fix).
- **Output:** the patch in the worktree (unstaged), the Completion report in this task file, a reply under 15 lines.
- **Step budget:** about 60 tool calls after Step 0 approval.
- **Run limit:** CPU only; no network, no GPU, no paid calls; tests bounded (seconds each).
- **May assume / Must ask:** may choose module and function names and the test layout; must ask (Step 0 question) about the package name and layout, any new dependency, any schema or contract ambiguity, and any behaviour the documents do not define.
- **Role pack and CLI wrapper:** implementer; Codex (profile `codex-dev`).
- **Test sets:** not applicable: no pipeline test suite exists yet; this task creates the first one, and the lint's built-in list names another project's suites until T-020 rebinds it.
- **Trivial:** no. **Small:** no.
- **Base:** pushed `main` 4baea72 (worktree `.tmp/worktrees/T-005`, detached); no predecessor patches.

### Research required before code

1. The scout's note `.tmp/T-005/research.md` in the worktree (file:line facts and hazards); re-read the lines it cites before relying on them (R-021).
2. The configuration contract: `contracts/README.md` (sentinels, YAML pitfalls, versioning), the schemas named above, `contracts/vlm-request.md`, `docs/stages/P-profiles.md`, `docs/stages/S0-intake.md`, the example configurations and profile.
3. The installed versions of the YAML and JSON-schema libraries in the project venv (`python -m pip list` inside `.venv`), quoted; which keys each command reads, from the stage documents.

### Configuration impact

Fill before the first edit: every key the loader reads, its schema, its example value, and its override and refusal tests.

## Step 0

(Codex writes Step 0 here and stops.)

## Steps

## Completion report

## Noticed, not done
