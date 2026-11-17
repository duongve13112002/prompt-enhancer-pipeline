# RES-2: cross-stage pipelining in `kb sync`

Status: **Step 0 drafted (Gemini 3.8 Flash scout, 2026-11-18 04:5x), PM-reviewed; awaiting a builder dispatch.** Not started: T-015, KBS-F2 integration and the T-017 pilot come first (plan targets in `tasks/STATUS.md`).

## Task brief

- **Goal:** within `kb sync`, start a later stage's work on an item as soon as that item's own inputs are ready, instead of waiting for the whole previous stage, so model and embedding servers stay busy up to their configured `max_concurrency` (decision 149, rule S46), with results identical to the serial run (R-017 resume, differential proof).
- **Origin:** RES-1 removed the barriers inside stages (K1 to K4, S1, S2); its handoff lists the cross-stage barriers (`tasks/active/RES-1-resource-use.md`, "RES-2 handoff").
- **Requirements:** R-015, R-017, R-018, R-020, R-026.
- **Out of scope:** model choice, prompts, thresholds or any profile value; GPU runs; the residual synchronous storage and schema reads listed in RES-1.

## Step 0 (scout draft; PM review below)

### Unknowns and failure modes

- **Spend before a failure is known:** `require_success` (`kb_sync.py:29-36`) fails a stage only after it completes; pipelining admits later-stage calls before an earlier failure is known.
- **K1 image to K2 observation is global:** near-duplicate grouping over all K1 vectors (`k2_observe.py:189-192`, `group_images` `:40-80`); group IDs hash all members (`:79-80`). Streaming would change group IDs.
- **K2 naming overrides are global:** `apply_name_overrides` across all folders (`k2_naming.py:697-707`); K3 on a concept before naming finishes could use stale concept IDs.
- **K4 alias collisions are global:** `mark_collisions` (`k4_aliases.py:32-49`); only `accepted` aliases are embedded (`kb_sync.py:43-44`).
- **Drain and cancel across overlapping stages:** RES-1 `flow` (`model_resources.py:227-269`) drains one list; overlapping stages need one supervisor that drains and cancels every stage without hanging on bounded queues.
- **(PM addition) Servers that cannot be resident together:** on the dev machine the model server and the embedder do not fit on the GPU together (`docs/dev-machine.md:31`). Overlap between an embedder stage (K1 image, K1 text) and a VLM stage (K2 to K4) is possible only where both backends serve at once, which is a deployment property, not a code fact.

### Dependency table

| Stage pair | Input the downstream item needs | Granularity | Pipelining |
|---|---|---|---|
| K0 -> K1 image | `handoff['k1']` rows (`kb_sync.py:146`) | per chunk | already parallel inside K1 |
| K1 image -> K2 naming | the folder's own `decode_status` (`k2_naming.py:633-634`) | per folder | yes, but crosses embedder -> VLM |
| K1 image -> K2 observation | every image vector (`k2_observe.py:185-192`) | global | no |
| K2 naming -> K2 observation | global overrides (`k2_naming.py:701-702`) | global | no |
| K2 observation -> K3 | observations per concept; representatives span concepts (`k2_observe.py:114-126`) | per concept, cross-linked | keep the barrier |
| K3 -> K4 | the concept's card summary (`k4_aliases.py:160-205`) | per concept | yes, same VLM server |
| K3 -> K1 text (card summary) | `card['summary']` (`kb_sync.py:45`) | per concept | yes, but crosses VLM -> embedder |
| K4 -> K1 text (aliases) | accepted aliases after global `mark_collisions` | global | no |
| K1/K4 -> K5 | all tables (`kb_sync.py:176-184`) | global | no |

### Ranked questions with proposed answers

1. **`require_success` under pipelining.** Options: (a) fail fast with drain: on any item failure stop intake to every stage, let in-flight requests finish and checkpoint, raise the earliest error in serial stage order; (b) let admitted queue items finish, raise at the stage barrier; (c) run everything, fail at publish. **Proposed (a):** spend is bounded by the sum of `max_concurrency`, received answers are checkpointed and reused on rerun (R-017, R-020).
2. **(PM) Which overlaps run when servers cannot be resident together?** **Proposed:** RES-2 phase 1 implements only the same-server chain K3 -> K4 per concept (always useful); the cross-server overlaps (K1 image -> K2 naming, K3 -> K1 text) are phase 2 and run only when a validated profile value says both backends serve at once (a new key with sentinel `deploy_required`, refused when absent), never inferred.
3. **Queue bound:** a bounded queue between stages; HTTP concurrency stays bounded by each backend's `max_concurrency`. **Proposed:** the queue bound is `safety-bound` and derived from `max_concurrency` (no new key), classified in the audit.
4. **Capacity log:** extend `CapacityLog` (`model_resources.py:160-179`) across the whole sync so in-flight counts are visible across stage transitions.
5. **Differential proof:** labelled async doubles (`tests/test_stage_flow.py`) with seeded jitter: a pipelined run equals the serial run (`max_concurrency=1`) on tables, artifacts and checkpoints, including failure drain, stop and resume; a generated-schedule property over completion orders and failure points.

### Design (proposed)

- Typed bounded queues between pipelined stages; one supervisor owns every stage task, stops intake on the first fatal error, drains in-flight items to their checkpoints, raises the earliest error in serial stage order, and on an external stop cancels and awaits every owned task (RES-1 `await_owned`, `model_resources.py:212-224`).
- Checkpoint keys stay content- and revision-derived (`k1_embed.py:143-159`, `k3_cards.py:271`, `k4_aliases.py:167`), so resume equals one uninterrupted run.
- Kept barriers: K1 image -> K2 observation, K2 naming -> K2 observation, K4 collisions -> K1 alias text, final publication.

### Configuration impact

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Request capacity across stages | `vlm.backends.<b>.max_concurrency` | existing | existing examples | combined peak equals the cap; 0, negative, non-int refused | configurable |
| Cross-server overlap (phase 2 only) | new key, name to be fixed in the build Step 0 | boolean, sentinel `deploy_required` | dev example `false` | overlap on/off differential; absent or sentinel refused at start | configurable |
| Inter-stage queue bound | derived from `max_concurrency` | n/a | n/a | queue never exceeds the bound | safety-bound |

### Plan with a checkable finish

1. Phase 1: pipeline primitives (bounded channels, one supervisor with drain and cancel) plus the generated-schedule property; then K3 -> K4 per concept in `kb_sync.py` with answer 1 (a). Done when the differential test (serial versus pipelined, failure, stop, resume) passes and the capacity log shows in-flight requests across the K3 -> K4 transition.
2. Phase 2 (only after the user confirms answer 2): the cross-server overlaps behind the validated profile value.
3. Each phase: one file per process through `tools/agent-workflow/test_slot.py`, about 8 mutants, Configuration audit, two reviewers of different families.

## PM review (2026-11-18)

- Barriers and line references re-checked at `kb_sync.py:29-36,146-170`; the draft matches the source.
- Added the resident-servers unknown and question 2 from `docs/dev-machine.md:31`: the scout's plan would overlap embedder and VLM stages that the dev machine cannot hold together.
- Answers 1, 3, 4 and 5 are PM decisions pending user review; answer 2 needs the user's confirmation before phase 2 (it adds a deployment key).
