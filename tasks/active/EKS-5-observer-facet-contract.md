# EKS-5: K2 observer reply contract states the facet format the stage enforces

Kind: code. R-IDs: R-010, R-015, R-017, R-018, R-026. Process (decision 164): one build, one review, one fix round (this file).

## Why

The GPU smoke of 2026-11-18 (`runs/E-kbsync-20261118-225806/report.md`, main tree, EKS-5, P2) stopped at K2 observation: three real observer replies were HTTP 200, valid against the role schema, and all rejected by the stage with `facet must be <facet_group>.<facet>`. The v0 reply schema allowed any nonempty facet, the v0 prompt never gave the qualified spelling, and the user data sent only group names: the request did not state the format the parser enforced. EKS-6 (P3): `sampling.repetition_penalty` was dropped with `PARAM_UNSUPPORTED` although `contracts/vlm-request.md:48` documents llama.cpp `repeat_penalty` and vLLM `repetition_penalty`.

## Goal

One structural contract between observer request, role schema, prompt and parser: a reply that follows the request is accepted, any other is refused with a typed reason; the stored observation keeps its qualified facet. No word lists or natural-language rules (AGENTS.md section 5). EKS-6: map the penalty for backends whose documented API has it; others keep the warning.

## Step 0: approved design and configuration impact

Design (a), PM-selected: the model returns a nonblank, dot-free facet name; the parser validates it independent of engine schema support and stores `group.name`. Model-facing schema and prompt are versioned (`observer_reply.v1`, `observer_v1`); the stored schema and `observe.facet_schema_version` (`image_observation.v0`) are unchanged. Observation revisions hash prompt id and bytes, the reply schema, the configured schema and the facet schema version, so old checkpoints are not reused.

QA round decisions: F4 versions the two other prompts changed in round one (`folder_namer_v1`, `planner_v1`; the v0 files are restored to their committed bytes and stay as superseded files) and selects them in the example profiles.

Configuration impact (QA round): observer `prompt_id` and `output.schema_id` are role settings (`vlm_role.v0`, `configs/enhancer.example.yaml`) that `validate_observation_reads` now refuses at start unless they select `observer_v1` / `observer_reply.v1`. Planner and folder-namer `prompt_id` change to v1 in the example profile. `knowledge.concept_separator` (existing validated value) enters the naming request payload and checkpoint key. `vlm.roles.*.sampling.repetition_penalty` is mapped by `vlm.backends.*.engine.name`. No model identity, endpoint, path, device, budget, timeout, concurrency or lifecycle value changes.

## Completion report (fix round)

**Behaviour.** All five findings fixed; no Git write, install, network, GPU or live model call.

| Finding | Fix | Evidence |
|---|---|---|
| F1 P2 stale probe test | `tests/test_model_production_probes.py:198-199` asserts no `PARAM_UNSUPPORTED` warning and `double.requests[-1]['repeat_penalty']` equals the role value (llama_cpp double) | file: 84 passed |
| F2 P3 observer v0 profile accepted | `config.py:1285-1288` refuses `prompt_id != observer_v1` and `output.schema_id != observer_reply.v1`, naming the expected id; `test_observer_old_contract_refused_at_start` (both leaves, through the stage and `load_config`) | `test_k2_observe_config.py`: 193 passed |
| F3 P3 docs | `docs/stages/K2-observe.md` and `contracts/README.md` name `observer_reply.v1` and say code qualifies the dot-free facet; `contracts/README.md` says the v0 observer schema and prompt remain as superseded files | read |
| F4 P3 prompts changed under same id | `prompts/folder_namer_v1.txt`, `prompts/planner_v1.txt` added; v0 files not modified in `git status` (CRLF, as committed); example profile selects v1; `prompts/README.md` says the planner v1 change invalidates golden tests, E1 results and verifier calibration inputs, regenerate them | `test_planner.py` 67, `test_k2_naming.py` 25 passed |
| F5 P3 test gaps | `test_blank_facet_names_are_refused` (`" "`, `"\t"`, U+3000), `test_model_mapping.py` `mapped=False` + `llama_cpp` keeps the warning, `test_separator_override_changes_namer_request_and_checkpoint` (`::` reaches the request and a new checkpoint key) | below |

**Class sweep (grep of `tests/` and `src/`).** `repetition_penalty` / `PARAM_UNSUPPORTED` readers: `test_model_production_probes.py:198` (fixed), `test_probe_request_settings.py`, `test_model_mapping.py`, `test_k2_config.py:96`, `test_k3_config.py:75`, `test_k4_config.py:92`, `test_writer_config.py:133`, `test_k2_observe_config.py:90`, `test_model_composition.py:105,282`, `test_model_config.py:227`, `test_validation_config.py:20`, `test_stage_flow.py`: all pass (counts below). `observer_reply.v0` / `observer_v0` / `folder_namer_v0` / `planner_v0` remaining outside `tasks/`: only `contracts/schemas/image_observation.v0.schema.json:5` (a `$comment` naming the v0 source of the stored document; not stale code), the superseded v0 schema and prompt files, and test fixtures that use `observer_v0` as an arbitrary stored `prompt_id` string (`assembly_fixtures.py`, `k3_fixtures.py`, `storage_fixtures.py`, `test_stage_flow.py`, `test_storage_*.py`): they do not select a role prompt, no change needed.

**Commands.** Each: `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest tests/<file> -q -p no:cacheprovider --basetemp=.tmp/EKS-5/bt`, `PYTHONPATH` = this worktree's `src`, one file per process.
`test_model_production_probes` 84 passed 19.71s; `test_eks5_replay` 8 passed 0.68s; `test_model_mapping` 5 passed 0.05s; `test_k2_observe_properties` 4 passed 0.67s; `test_k2_naming` 25 passed 10.89s; `test_k2_observe_config` 193 passed 21.94s; `test_k2_observe` 36 passed 11.31s; `test_k2_observe_edges` 20 passed 7.45s; `test_k2_observe_shared_config` 133 passed 13.75s; `test_k2_observe_shared_keys` 6 passed 4.18s; `test_planner` 67 passed 9.05s; `test_k3_config` 279 passed 43.20s; `test_k4_config` 273 passed 29.63s; `test_writer_config` 213 passed 49.97s; `test_validation_config` 169 passed 10.05s; `test_model_config` 123 passed 19.27s; `test_k3_fix_round1` 31 passed 15.56s; `test_probe_request_settings` 132 passed 19.89s; `test_model_client` 79 passed 16.01s; `test_k2_config` 310 passed 54.56s; `test_model_composition` 54 passed 42.94s; `test_kb_sync_gates` 19 passed 68.83s; `test_stage_flow` 24 passed 27.80s. All use synthetic doubles or labelled captured replies.

**Mutants** (scratch copy of `src/prompt_enhancer`, removed): blank guard weakened to `not facet`: killed (`test_eks5_replay` 3 failed, 5 passed, the three fixed whitespace cases). `mapped and` dropped from the engine mapping: NOT equivalent (retest N2: with injected evidence `{'sampling.repetition_penalty': True}`, `mapped=False`, engine `llama_cpp`, the mutant sends `repeat_penalty` and no warning); the PM added that case to `test_repetition_penalty_local_mappings_and_unknown_backend`, which kills it (`.tmp/pm-checks/m6_check.py`: original keeps the warning, mutant fails the assertion; `test_model_mapping` 5 passed). Retest N1: `docs/stages/K2-observe.md:10` now names `folder_namer_v1` (PM fix). Earlier round (reviewer): dot guard, qualification, uniqueness key, llama field swap, separator context: killed.

**Configuration audit.**

| Value | Path | Validation / profile | Override and refusal evidence | Class |
|---|---|---|---|---|
| Observer prompt id | `vlm.roles.observer.prompt_id` | `vlm_role.v0`; `validate_observation_reads` expects `observer_v1`; example profile | `test_observer_old_contract_refused_at_start[prompt_id]` | protocol-bound (fixed to the parser's contract; its override test was removed) |
| Observer reply schema id | `vlm.roles.observer.output.schema_id` | same, expects `observer_reply.v1`; schema and prompt bytes hashed in observation revisions | `test_observer_old_contract_refused_at_start[output.schema_id]` | protocol-bound (fixed to the parser's contract) |
| Planner and folder-namer prompt ids | `vlm.roles.planner.prompt_id`, `.folder_namer.prompt_id` | `vlm_role.v0`; example profile selects v1; folder-namer id and bytes enter its checkpoint key | existing role-leaf override and refusal tests (`test_k2_config.py`, `test_planner.py`) pass | configurable, artifact-bound |
| Stored facet schema version | `observe.facet_schema_version` | restricted to `image_observation.v0`; unchanged | existing singleton refusal tests | configurable, artifact-bound |
| Repetition penalty | `vlm.roles.*.sampling.repetition_penalty`, backend `engine.name` | `vlm_role.v0` positive number; example profile; wire names llama_cpp `repeat_penalty`, vllm `repetition_penalty` (protocol-bound, from `contracts/vlm-request.md:48`, a lead not re-read at its primary source; no network allowed) | `test_repetition_penalty_local_mappings_and_unknown_backend` (unknown engine and `mapped=False` keep the warning), `test_loaded_nondefault_control_override_in_probe`, `test_repetition_penalty_invalid_values_refused` | configurable; field names protocol-bound |
| Concept separator | `knowledge.concept_separator` | existing validated value, example profile | `test_separator_override_changes_namer_request_and_checkpoint` (`::` in request, new checkpoint key); existing configuration refusal tests | configurable |
| Facet dot and blank guard | parser, reply schema v1 | dot is the artifact-bound stored separator; nonblank guard is protocol-bound | `test_blank_facet_names_are_refused`, property test, replay of three captured replies | artifact-bound, protocol-bound |

**not_run:** ruff lint and `ruff format --check` (`No module named ruff` in the project venv, reported by the reviewer; not retried), tool suite (no tooling change), full suite, GPU, live model, network. Installed-server help check not run (no network, no server).

**Open issue.** `.tmp/EKS-5/pytest` (left by the Codex session) has an unreadable ACL: `rm`, `icacls` and pytest `--basetemp` there fail with access denied, so this round used `--basetemp=.tmp/EKS-5/bt` (removed). The orchestrator should delete `.tmp/EKS-5/pytest` and `.tmp/EKS-5/pmbt` from an elevated shell or when the worktree is removed.

**Noticed, not done:** the `$comment` in `image_observation.v0.schema.json:5` still names `observer_reply.v0`; a schema comment edit changes schema bytes, so left for the PM.

## Handoff

Status: fix round complete, ready for the PM's second look. Nothing is running. Changed files are those in `git status` (code: `config.py`, `k2_naming.py`, `k2_observe.py`, `kb_sync.py`, `model_mapping.py`; contracts, prompts, example profile, docs, tests, fixtures).
