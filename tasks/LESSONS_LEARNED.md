# Lessons Learned

Read the Rules digest before every task (AGENTS.md section 9). Each line is a prevention rule; a recurring or multi-attempt mistake adds an entry under "Recent entries" (symptom, root cause, prevention, evidence); fold old entries into the digest and archive them verbatim.

## Rules digest

### Research and evidence

- A research agent's number is a lead. In this project's research, two agents reported different figures for the same abstract ([N109](../docs/note/N109-evisrag.md)), one gave a paper identifier that did not resolve ([N114](../docs/note/N114-transliterated-queries.md)), and a vendor guide's file sizes disagreed with the file list ([N123](../docs/note/N123-unsloth-quant-benchmarks.md)); open the primary source before a number enters a note or a setting.
- Text returned by a page-fetch tool is that tool's extraction, not the page: a "quote" from it is checked against the page before it becomes a rule ([N128](../docs/note/N128-codex-prompting-guidance.md)).
- A benchmark on a different task proves a pattern at most, never a threshold.
- An estimate from parameter counts or file sizes is arithmetic; label it and plan a measurement.

### Agents and process

- Files copied from another project carry its bindings (names, paths, branches, decision numbers); put every project value in `.agents/project.json` and let `setup_project.py check` find leftovers (`foreign_bindings`).
- When condensing or restructuring rules, list each old rule and mark where it now lives before deleting the old text ([docs/agents/rule-map.md](../docs/agents/rule-map.md)); a condensation done from memory once dropped a rule in the project this tooling came from.
- Prose rules are not enforced; a rule that must hold needs a hook or a CI check (N060, N066).
- Tests that pin another project's text break on reuse; test structure and budgets instead.

## Recent entries

- 2026-11-14, Antigravity scout returned an empty reply (approach-t025). Root cause: the PM's prompt named `.agents/ORCHESTRATION.md`, which was not inside the job's workspace, so the session rule `read_file(<workspace>)` denied the read. Prevention: an agy prompt names only paths inside its workspace; other files go in with `-ExtraNotes` and are named by their `notes/` path. Evidence: `.tmp/agy-work/_results/approach-t025.json` (empty response), rerun approach-t025b with `-ExtraNotes`.
- 2026-11-14, a follow-up message to a running worker was refused by brief lint (missing footer and `Kind:`). Root cause: the message began with `[NEW TASK: ...]`, which the lint treats as a new brief. Prevention: a Step 0 approval or fix round starts with the task name or `[QA FEEDBACK: ...]`; `[NEW TASK]` only with a full brief. Evidence: `cli_worker.py send --name t025-codex-dev` output "brief lint refused send".
- 2026-11-14, the T-002 worker spent its single authorized `nvidia-smi` call on `--help`, leaving the GPU facts unknown. Root cause: the brief authorized "one call" without naming it. Prevention: a brief that grants a limited command names the exact command line. Evidence: `.tmp/T-002/fix-01.md`.
