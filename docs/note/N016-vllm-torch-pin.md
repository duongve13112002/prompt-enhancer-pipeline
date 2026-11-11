# N016 vLLM 0.33.0 requirements on PyPI

- **Source:** https://pypi.org/pypi/vllm/0.33.0/json
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-09 (requires_dist: torch==2.14.0, torchaudio==2.11.0, torchvision==0.29.0, torchcodec>=0.14)
- **Checked:** 2026-11-09
- **Used in:** docs/dependencies-and-environments.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

vLLM 0.33.0 (2026-11-02) pins torch 2.14.0. A research agent reports 0.27 to 0.32 pin 2.13.0, 0.22 to 0.26 pin 2.11.0, and no recent wheel pins 2.12.

## Caveats

The server has torch 2.12 (user statement). Only 0.33.0 was re-read by the PM.
