# Project Context Snapshot

Compact authoritative snapshot, 2026-11-11 (documentation release 0.1). Requirements: [REQUIREMENTS.md](REQUIREMENTS.md); plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md); day-to-day detail: [STATUS.md](STATUS.md); provenance: [DECISIONS_INDEX.md](DECISIONS_INDEX.md); dispatch rules: [PM_POLICY.md](../.agents/policy/PM_POLICY.md); rules: [../AGENTS.md](../AGENTS.md).

## Objective and scope

- **Product:** a prompt enhancer. Input: a text prompt in any format and language (`generate`), or a text prompt plus one source image (`edit`). Output: an enhanced prompt or edit instruction in the format the admin's system prompt defines, with an honest status. It writes text only.
- **Knowledge base:** a root of folders, each declaring one or more concepts (an info file, or names a vision model derives from the images when there is none); large; managed by `kb sync`; read only for agents. With retrieval on (the default) the enhancer adds correct knowledge about named concepts from concept cards built offline.
- **Profiles:** one admin system prompt per image model and task, used verbatim, with a confirmed manifest.
- **Hardware:** dev machine with a small-VRAM GPU and a small quantized model (smoke only); the user runs the deployment (a GPU of at least 45 GB) from the agent's run guide. No model is named in rules; models are profile values.
- **Agents:** Claude Code, Codex and Antigravity (`agy`) on subscriptions with 5-hour and weekly limits.
- **Out of scope:** rendering or editing pixels, training, choosing the downstream image model.

## Facts that bind decisions

- No hardcoding, hand-written rules, word lists or guessed thresholds (R-015); unmeasured values are sentinels.
- Tables and artifacts are Parquet or Arrow with a derived serving index (R-016).
- No gold prompts exist; agents build the evaluation set from the knowledge base (R-023).
- CI uses Ubuntu and Windows runners inside a visibility window (R-025); the repository is private outside it.
- Research notes: [docs/note/README.md](../docs/note/README.md); most are agent-read; carried notes are leads.

## State

Documentation seeded on 2026-11-11; nothing implemented or measured. Tooling bound through `.agents/project.json`.
