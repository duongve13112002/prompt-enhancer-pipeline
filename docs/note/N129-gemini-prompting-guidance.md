# N129 Google prompt design strategies for Gemini and its agent template

- **Source:** https://aistudio.google.com/docs/prompting-strategies ; https://aistudio.google.com/docs/thinking ; https://aistudio.google.com/docs/whats-new-gemini-3.6
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** .agents/policy/cli/antigravity.md

## What this project relies on

"supply all the context first. Place your specific instructions or questions at the very end of the prompt." "Be precise and direct". Grounding: "If the exact answer is not explicitly written in the context, you must state that the information is not available." Agent template: "you must change your strategy or arguments, not repeat the same failed call"; "Verify your claims by quoting the exact applicable information". Newer models ignore custom temperature, top_p and top_k. A thinking level of medium or high is advised "to prevent premature tool termination" (stated for one small model).

## Caveats

Agent-read. One prompting guide URL returned 404 and one developer guide did not load. No official statement on negative constraints was found.
