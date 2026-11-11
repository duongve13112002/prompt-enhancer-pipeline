# N010 Qwen3-VL-Embedding (arXiv 2601.04720)

- **Source:** https://arxiv.org/abs/2601.04720
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** K1, S1
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

2B (dim 2048) and 8B (dim 4096), 32K context, MRL. MMEB-V2 overall 73.2 (2B) and 77.8 (8B); image retrieval 74.8 (2B) and 80.2 (8B, as the agent read Table 2; an earlier sibling proposal said 80.0). 33 languages listed in the README (https://github.com/QwenLM/Qwen3-VL-Embedding) including Vietnamese. Apache-2.0.

## Caveats

No published image throughput; no head-to-head with SigLIP 2; MMEB measures retrieval with instructions, not threshold-calibrated presence.
