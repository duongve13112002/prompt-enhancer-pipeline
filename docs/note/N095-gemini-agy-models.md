# N095 Gemini models, thinking level and Antigravity CLI (`agy`) settings

- **Source:** https://aistudio.google.com/docs/pricing ; https://aistudio.google.com/docs/whats-new-gemini-3.8 ; https://deepmind.google/models/model-cards/gemini-3-8-flash/ ; https://antigravity.google/docs/settings?app=cli ; https://antigravity.google/docs/cli/commands/effort/ ; https://antigravity.google/docs/plans?app=cli
- **Class:** documented (prices, thinking levels and agy settings re-read verbatim by a second agent)
- **Verified by:** Haiku research agent, then a second agent re-read pricing, what's-new and agy settings (2026-11-10); the PM did not read them
- **Checked:** 2026-11-10
- **Used in:** .agents/policy/PM_POLICY.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

| Model ID | Input / output / cached per 1M | Context | Notes |
| --- | --- | --- | --- |
| `gemini-3.8-flash` | $0.75 / $3.75 / $0.075 through 2026-12-31; $1.50 / $7.50 / $0.15 from 2027-01-01 | 1,048,576 in / 65,536 out | output includes thinking tokens; card: "cost-effective scaling of general-purpose, production-ready agents" |
| `gemini-3.1-pro-preview` | $2.00 / $12.00 / $0.20 at <=200K | 1M in / 64K out | "advanced coding, long context and/or multimodal understanding" |
| `gemini-4-argon` | $2.00 / $10.00 / $0.10 through 2026-11-30; $4.00 / $20.00 / $0.20 from 2026-12-01 | 1M | flagship reasoning; not on the free tier |

- API thinking level for 3.8 Flash: `low`, `medium` (default), `high`; `minimal` returns an error. Medium is "Recommended for complex code and agentic use cases, providing higher first-pass accuracy."
- agy: settings in `~/.gemini/antigravity-cli/settings.json` (only non-default values); `model` is saved by its label; `--model` selects the model for one session (example `agy --sandbox --model="Gemini 3.8 Flash"`; list with `agy models`); `/effort` takes `low`, `medium`, `high`, `xhigh`, `max` (the last two only on some models) and saves an interactive choice as the default.
- Quota: plans give "a meaningful quota, refreshed weekly"; no numbers on the page.
- agy reads workspace rules from `AGENTS.md`, `GEMINI.md` or `.agents/rules/*.md` and workspace skills from `.agents/skills/` (search summary of https://antigravity.google/docs/rules, not re-read).

## Caveats

- The string `gemini-3.8-flash-high` appears in no primary source read, but the user's runner (`scripts/agy-runner/invoke-job.ps1`) used it as the `--model` value in practice; the documented form is the label "Gemini 3.8 Flash" with a separate effort. Confirm with `agy models` on the machine.
- Benchmarks: the 3.8 Flash card and the Cloud doc disagree on Terminal-bench 2.1 (89.4% vs 90.8%); no figure is used here.
