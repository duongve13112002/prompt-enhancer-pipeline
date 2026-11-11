# N128 OpenAI guidance for Codex and its models

- **Source:** https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide ; https://developers.openai.com/api/docs/guides/latest-model ; https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it; quotes are the fetch tool's extraction, not checked against the page
- **Checked:** 2026-11-11
- **Used in:** .agents/policy/cli/codex.md, .agents/policy/PM_POLICY.md

## What this project relies on

"Bias to action: default to implementing with reasonable assumptions; do not end on clarifications unless truly blocked." "Unless asked for a plan, never end the interaction with only a plan." Prompting for an upfront plan "can cause the model to stop abruptly before the rollout is complete". One model "can be more tentative about how far to take a task"; the fix is to "define completion upfront". "Guidance that helps Sol or Luna may overconstrain GPT-6 Astra." Two of the models have no `none` reasoning level.

## Caveats

Re-read the primary pages before changing policy: several Codex documentation pages redirected and were not opened (instruction-file rules, configuration reference, non-interactive mode); a 32 KiB cap on combined instruction files comes from a search snippet only. No official guidance on hallucination was found.
