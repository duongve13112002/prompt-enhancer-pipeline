# T-004 Cold read, round 2 (read-only)

Kind: scout
R-IDs: R-028
Task file: tasks/active/T-004-cold-read-round2.md

## Brief header

- **Goal:** check that a new agent understands the repository after the 2026-11-14 changes (decisions 54 to 77): the info-file contract, profiles and the admin prompt `v1`, family choice, clarification, output language, scale, and the orchestrator's commit rule (AGENTS.md section 11, decision 77).
- **Done check:** a report file `.tmp/cold-read/<your worker name>.md` with the four parts below, each claim with a file and line.
- **Scope:** read only; write only your report under `.tmp/cold-read/`.
- **Out of scope:** fixing anything; reading `.secrets/`, `database/` images or `data/`.
- **Output:** the report (about one page) plus a reply under 10 lines.
- **Step budget:** about 30 tool calls.
- **Run limit:** 30 minutes; no network, no GPU, no installs.
- **May assume / Must ask:** read as a new agent would; ask nothing, list your questions in the report.
- **Role pack and CLI wrapper:** scout; Claude.
- **Test sets:** not applicable: read-only report, no code.
- **Trivial:** no. **Small:** yes.

## Task brief

- **Task name:** cold read round 2.
- **Responsible role:** scout (cold-read bullet).
- **Task difficulty and assigned model:** easy; three readers, Haiku 5.5, Sonnet 5.5 and Opus 5.5, each a fresh `cli_worker.py` session.
- **Reasoning effort and reason:** medium; reading and judging clarity.
- **Preloaded skills:** none.
- **Current status:** round 1 is in tasks/archive/T-004-cold-read.md.
- **What was already done:** round 1.
- **Expected output:** the report: (1) what the project is and what you would do first; (2) every place that is unclear, contradictory or missing, with file and line; (3) your answers to these checks, each with a pointer: what the enhancer does when every family candidate seems to contradict the scene; what `clarification.mode: best_effort` changes; how the output language is chosen; what an edit request produces with the example profile; who commits and pushes, and what workers may never do; (4) the questions you would ask the user.
- **Constraints and security cautions:** read only; never open `.secrets/`.
- **What must not be repeated:** none.
- **Ignored evidence paths:** none: the readers read tracked files only.

### Research required before code

None: a read-only cold read of tracked documents, no code and no external API.

### Configuration impact

None, because nothing is changed.
