# Agent Task and Configuration Audit Template

## Brief header

- **Goal:** one result, finished in one run; a redo loop is the failure.
- **Done check:** a runnable command or checkable item (a passing test, a rendered file, a size, hash or command output), never vague; the worker stops when it passes.
- **Scope:** the files the task may change; one owner per file.
- **Out of scope:** what the worker must not do (side refactors, audits, other stages, optional checks); found issues go under `Noticed, not done`.
- **Output:** the report format and a size cap (for example "report under 15 lines; details in the task file").
- **Step budget:** about how many tool calls the task should take; past it, stop and report (split larger work into phases).
- **Run limit:** time, plus what is not allowed (GPU, network, paid calls).
- **May assume / Must ask:** what the worker may decide alone and what it must bring back as a question (always filled for Codex workers, whose default is to assume).
- **Role pack and CLI wrapper:** the worker's role (`implementer`, `reviewer`, `scout`, `researcher`, `runner`) and CLI; the PM pastes `.agents/policy/roles/<role>.md` above the footer and `.agents/policy/cli/<cli>.md` at the top.
- **Test sets:** the tests this change must keep green, or `not applicable: <reason>` (the lint's built-in list still names another project's suites; task T-020 rebinds it).
- **Trivial:** `yes` only for a wording or configuration-value edit; it skips Step 0.

## Step 0 (before any edit)

Unless the header says `Trivial: yes`, write into the task file first:

1. **Unknowns and failure modes:** how this could fail (tools, data, formats, edge cases, rule conflicts, hooks or CI reading the files).
2. **Questions:** at most 5, ranked by impact, each with a proposed answer.
3. **Plan:** what exists, what is missing, the steps, and finish checks mapped to the done check.

A file-changing task then replies with Step 0 and stops until the plan is approved (who reads it: [PM_POLICY](PM_POLICY.md) section 4). Reviewers, scouts, checkers and researchers put Step 0 at the top of their report and continue. Step 0 reuses `Research required before code` and the hazard map; it adds no separate research step.

After approval, build one step at a time inside scope, with a two-line summary per step under `## Steps` in the task file. Deliver the output, the plan, the check results and `not_run` (what was not done, with the reason).

> [!IMPORTANT]
> Runtime work cannot start until `Configuration impact` is complete and cannot be accepted until every changed operational value has a tested row in `Configuration audit`. Missing or ignored configuration is a failed task.

Use this compact template for every delegated task. Remove irrelevant rows only when the `Configuration impact` section explains why they are not applicable.

## Task brief

- **Task name:**
- **Responsible role:**
- **Task difficulty and assigned model:** (host, model and `.claude/agents/*.md` definition on Claude Code)
- **Reasoning effort and reason:**
- **Preloaded skills:**
- **Current status:**
- **What was already done:**
- **Expected output:** (goal, done check and scope: brief header)
- **Constraints and security cautions:**
- **What must not be repeated:**
- **Ignored evidence paths:** (required for review and audit briefs) local, Git-ignored files the verdict depends on, such as `configs/**/*.local.yaml`, `runs/` reports and `.tmp/` evidence; write `None` with a reason.

### Research required before code

List the questions that must be answered from primary sources before any implementation: official docs, model cards, papers, library source at the installed version, and existing notes under `docs/note/`. Write `None` only with a concrete reason, for example "pure refactor of project-owned code, no external API or algorithm". The implementer records answers in `Research evidence` before the first code change. A handoff without it fails.

### Configuration impact

Name every affected model/provider/path/device/resource/concurrency/inference/timeout/lifecycle setting and its schema plus profile source. Write `None` only with a concrete reason.

## Completion report

- **Work completed:**
- **Result and evidence:** (each claim with its pointer: file and line, command and real output, or URL and quoted words; the done-check output pasted)
- **Important changes and reasons:**
- **Problems, risks and remaining work:**

### Research evidence

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
|  |  |  |  |  |

Every external function, class, algorithm, formula, model convention or protocol the change relies on has a row. Re-use an existing note by citing it rather than re-researching. Reviewers check this table first; an unbacked external claim is a finding.

### Configuration audit

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
|  |  |  |  |  | Configurable / protocol-bound / artifact-bound / safety-bound / test-only / defect |

An unclassified runtime literal, ignored configured field, hidden material default, or a setting without both an override test and a refusal test, is a release blocker.

## Handoff

- **Current status:**
- **What was already done:**
- **What the next agent must do:**
- **What must not be repeated:**
