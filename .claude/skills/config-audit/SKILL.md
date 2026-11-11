---
name: config-audit
description: Blocking configuration gate for any runtime change in this repository. Use before editing runtime code (fill Configuration impact) and before handing off (fill the row-by-row Configuration audit with tests).
---

# Configuration impact and audit gate

The authoritative rule is section 5 of `AGENTS.md`. This skill is the working checklist. A missing row, ignored setting, hidden material default or unclassified literal fails the task.

## Before the first runtime edit

1. Open the task file under `tasks/active/` (created from `.agents/policy/AGENT_TASK_TEMPLATE.md`). If `### Configuration impact` is empty, stop and fill it.
2. Inventory every setting the change can touch: model family/variant/task/format, provider (managed or external), paths and endpoints, device and GPU topology, dtype, RAM/VRAM budgets, worker/concurrency/batch limits, image size sent to a model, sampling and seed, retries and deadlines, cache/temp/output directories, lifecycle (load/unload, idle, teardown).
3. For each, name the schema class in the runtime configuration module (`python.runtime_config_module` in `.agents/project.json`; none yet before task T-005) and every profile that must declare it (`configs/enhancer.example.yaml`, `configs/enhancer.dev.example.yaml`, the overlays in `configs/overlays/`, and ignored `configs/**/*.local.yaml` when a live run uses them). Sentinels (`fit_from_gt`, `fit_from_bench`, `fit_from_pilot`, `pin_required`, `deploy_required`, `design_required`) are refused for a real run.
4. `None` is valid only with a concrete reason (for example, documentation-only change).

## While implementing

- User-controlled values live in validated configuration: strict types (`StrictInt`, `StrictFloat`, `StrictBool`, `Literal`), finite bounds, cross-field validators where values interact (for example, a client timeout must cover startup plus inference).
- No hidden material default in a source model field when omission changes cost, resources, quality or deployment behavior; declare it in every profile instead.
- No silent fallback when a field is absent; fail at startup with a precise message.
- Paths must resolve inside the project root after normalization; outside paths fail.
- Hardcode only named wire/schema constants, immutable artifact identity, bounded parser/protocol limits or explicit safety invariants. Name the constant and state its category where it is not obvious.

## Before handoff

1. Scan the runtime diff for literals: `git diff -U0 -- src/ | grep -E '^\+' | grep -E '[0-9]|"[^"]+"'` and classify every new or changed literal.
2. Fill `### Configuration audit` with one row per changed operational value:

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|

   Retained-literal class is one of: configurable, protocol-bound, artifact-bound, safety-bound, test-only, defect.
3. Every configurable row needs a test that sets a non-default value and observes its effect, and a test that a missing or invalid value is refused. Name the test function.
4. Run the tests you named and report the exact command and result.
5. Report any row you could not complete as an open defect; do not mark the task done.
