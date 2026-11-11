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

None yet in this repository.
