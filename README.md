# Prompt enhancer pipeline

A documentation-first repository for designing and then building a prompt enhancer: it turns a user's prompt (or a prompt plus one source image to edit) into an enhanced prompt or edit instruction for a downstream image model, in the format the admin's system prompt defines, optionally grounded in a knowledge base of concept folders. **Status: design documents, contracts, configuration examples, a backlog and the multi-agent tooling, plus the pipeline code built so far under `src/`** (`python -m prompt_enhancer`: `kb`, `profile`, `enhance`, `eval`), tested on the CPU with labelled HTTP doubles; only `kb sync` has run against live local models (a dev-GPU smoke on one folder), nothing has run on the deployment, and nothing is measured. The commands and a first deployment are in the [run guide](docs/run-guide.md). Implementation starts only on the user's explicit command ([AGENTS.md](AGENTS.md)).

## Layout

| Path | What lives there |
| --- | --- |
| [AGENTS.md](AGENTS.md) | The single rule file for every agent (Codex and Antigravity read it; [CLAUDE.md](CLAUDE.md) imports it) |
| [docs/](docs/README.md) | The design: [architecture](docs/architecture.md), [stages K0-K5, P, S0-S6 and G](docs/stages/README.md), [execution](docs/execution.md), [serving](docs/serving.md), [dev machine](docs/dev-machine.md), [CI](docs/ci.md), [engineering rules](docs/rules/), [research notes](docs/note/README.md) |
| [contracts/](contracts/README.md), [configs/](configs/enhancer.example.yaml), [prompts/](prompts/README.md) | Data contracts and schemas, example profiles (including an example admin profile), runtime prompt contracts |
| [kb/](kb/README.md), [evalset/](evalset/README.md) | Layout of the knowledge base (text-only example) and of the agent-built evaluation set |
| [tasks/](tasks/STATUS.md) | Project state: status, requirements, plan, decisions, lessons, active task briefs, archive |
| [.agents/](.agents/ORCHESTRATION.md) | How agents work: [project binding](.agents/project.json), orchestration, [policy](.agents/policy/PM_POLICY.md), [role packs](.agents/policy/roles/README.md), [CLI wrappers](.agents/policy/cli/README.md), [skills](.agents/skills/) |
| [tools/agent-workflow/](tools/agent-workflow/README.md) | Multi-agent tooling: worker launcher (Claude, Codex), hooks, brief lint, usage and quota, setup |
| `models/`, `.secrets/` | Local model artifacts and credentials, Git-ignored |

## Start here

1. Set up the agent tooling on this machine: `python -m venv .venv`, then `.venv/bin/python -B tools/agent-workflow/setup_project.py apply` and `... check` (Windows: `.venv\Scripts\python.exe`). Guide: [docs/agents/README.md](docs/agents/README.md).
2. Read [tasks/STATUS.md](tasks/STATUS.md) for the current state and the questions waiting for you.
3. Open Claude Code in this folder and paste the opening prompt kept outside the repository: `user-prompts/kickoff.vi.md` for the first session (it makes the agent prove it understood the project before it proposes work), `user-prompts/session-start.vi.md` for every later one. Both ship in the delivery zip next to this folder.

## Provenance

The design is the PM's proposal ([N000](docs/note/N000-design-provenance.md)) built from the user's proposal and answers of 2026-11-11 ([decisions](tasks/DECISIONS_INDEX.md)). The agent tooling and policies were copied from an earlier project of the user and rebound on 2026-11-11 (decision 23); what changed in the rules is in [docs/agents/rule-map.md](docs/agents/rule-map.md). This repository was delivered as a zip with no remote (decision 2); the user created the GitHub repository, cloned it and set up `gh` (decision 35), and since decision 77 the orchestrator commits and pushes verified work itself, while workers never do.
