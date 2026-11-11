# N121 vLLM for Qwen3.8-27B: recipe, structured outputs, engine arguments

- **Source:** https://recipes.vllm.ai/Qwen/Qwen3.8-27B ; https://docs.vllm.ai/en/stable/features/structured_outputs/ ; https://docs.vllm.ai/en/stable/configuration/engine_args/ ; https://docs.vllm.ai/en/stable/features/multimodal_inputs/
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** docs/serving.md, contracts/vlm-request.md, deployment overlay

## What this project relies on

Recipe: `--reasoning-parser qwen3`; an FP8 command with `--kv-cache-dtype fp8`; server default `--default-chat-template-kwargs '{"enable_thinking": false}'`; "Text serving is what this recipe covers and what has been verified." Structured outputs: backends "xgrammar or guidance", `response_format` of type `json_schema`. Engine arguments: `--gpu-memory-utilization` is a per-instance limit; `--limit-mm-per-prompt`; a per-request `mm_processor_kwargs` is rejected unless the server starts with `--trust-request-mm-kwargs`. The SGLang cookbook gives FP8 weights of about 28.5 GB.

## Caveats

Agent-read. No throughput figure for a 48 GB card was found. Image input with a JSON schema is not covered by the recipe (first deployment smoke test).
