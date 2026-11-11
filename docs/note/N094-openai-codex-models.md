# N094 OpenAI models and reasoning effort in Codex CLI

- **Source:** https://learn.chatgpt.com/docs/models ; https://developers.openai.com/api/docs/pricing ; https://learn.chatgpt.com/docs/developer-commands?surface=cli ; https://learn.chatgpt.com/docs/changelog
- **Class:** documented (prices re-read verbatim by a second agent; model descriptions from a page summary)
- **Verified by:** Haiku research agent, then a second agent re-read the models and pricing pages (2026-11-10); the PM did not read them
- **Checked:** 2026-11-10
- **Used in:** .agents/policy/PM_POLICY.md, tools/agent-workflow/codex_profiles.json
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

| Model ID | Input / cached / output per 1M (standard, <=272K) | Positioning (page summary) |
| --- | --- | --- |
| `gpt-6-astra` | $10.00 / $1.00 / $50.00 | most capable, most demanding end-to-end work |
| `gpt-6.1-sol` | $2.00 / $0.10 / $10.00 | near-Astra for complex work at lower cost; repeated long-running work where cost matters; 500,000-token context in Codex |
| `gpt-6.1-terra` | $1.00 / $0.05 / $5.00 | fast, efficient; writing, visual understanding, computer use; needs Codex CLI 0.159.0+ |
| `gpt-6-luna` | $0.10 / $0.01 / $0.50 | most efficient; focused, high-volume, repeatable tasks (extraction, classification) |

- Effort levels: Low (CLI; "Light" in the app), Medium, High, Extra High, Max, Ultra (Ultra uses subagents). "Higher effort can improve results for complex tasks, but it takes longer and uses more tokens." Luna supports up to Max, not Ultra.
- Selection: `codex -m <id>`, `codex exec -m <id>`, `/model <id> --reasoning=<level>`, profiles with `--profile`. The launcher here passes model and `model_reasoning_effort` through `codex_profiles.json` roles; effort `ultra` is refused by `codex_backend.py`.
- The page names no default model ("uses a recommended model").

## Caveats

- No primary benchmark page was readable (openai.com returned 403); third-party Terminal-Bench and SWE-bench figures are not used here.
- Config key names in `config.toml` were not confirmed from the config reference (404).
