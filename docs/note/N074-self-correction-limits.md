# N074 Large language models cannot self-correct reasoning yet (arXiv 2310.01798)

- **Source:** https://arxiv.org/abs/2310.01798
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it (abstract; numbers via secondary)
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Models cannot reliably self-correct reasoning without external feedback; on GSM8K accuracy fell from 75.9% to 74.7% after two rounds (secondary figures).

## Caveats

Supports separate reviewers with an external check.
