# N102 Qwen3.8-27B: sizes, thinking default, sampling, image tokens

- **Source:** https://huggingface.co/Qwen/Qwen3.8-27B ; https://huggingface.co/Qwen/Qwen3.8-27B-FP8
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (README quickstart and best-practice sections, file lists and preprocessor_config.json through the Hub API)
- **Checked:** 2026-11-11
- **Used in:** docs/serving.md, deployment overlay, S0 (image downscale), contracts/vlm-request.md

## What this project relies on

Apache-2.0; native image and video input; context 262,144 tokens. Thinking is on by default; `reasoning_effort` is `xhigh` (default), `medium` or `low`; thinking is turned off with `chat_template_kwargs: {"enable_thinking": false}`. Recommended sampling, non-thinking: temperature 0.7, top_p 0.80, top_k 20, min_p 0.0, presence_penalty 1.5, repetition_penalty 1.0; thinking: temperature 1.0, top_p 0.95, top_k 20, presence_penalty 0.0. Weight files: the BF16 repository has 18 shards totalling about 55.6 GB; the FP8 repository totals about 30.9 GB. Image preprocessing: patch_size 16, merge_size 2, `size.longest_edge` 16,777,216, `size.shortest_edge` 65,536. Serving engines named on the card: SGLang, vLLM, TokenSpeed.

## Caveats

Sizes are on disk, not memory in use. One visual token per 32x32 pixels and a default cap near 16,384 tokens per image are derived from the config values, not stated on the card. The card gives no throughput figure.
