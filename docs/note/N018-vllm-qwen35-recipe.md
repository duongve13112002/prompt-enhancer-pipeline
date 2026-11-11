# N018 vLLM recipe for Qwen3.5

- **Source:** https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/serving.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Flags `--mm-encoder-tp-mode data`, `--mm-processor-cache-type shm`, `--default-chat-template-kwargs '{"enable_thinking": false}'`; do not use `--language-model-only` (it drops the vision encoder). Prefix caching on the hybrid GatedDeltaNet models is experimental.

## Caveats

Recipe is for Qwen3.5, a sibling of Qwen3.8; not tested on Qwen3.8.
