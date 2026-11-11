# N118 Dependency-structured yes/no checks: DSG and CRAFT

- **Source:** https://arxiv.org/abs/2310.18235 ; https://arxiv.org/abs/2512.20362
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it (abstracts)
- **Checked:** 2026-11-11
- **Used in:** S5

## What this project relies on

DSG checks a prompt with yes/no questions arranged by dependency, skipping children when a parent fails. CRAFT turns a prompt into such questions, checks them with a vision-language model and edits only where a check fails, keeping quoted text exact.

## Caveats

Both check generated images, not rewritten prompts; applying the pattern to a constraint ledger is this project's proposal.
