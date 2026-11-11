# N055 llama.cpp server README (llama-server)

- **Source:** https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the raw README, and tools/server/server-common.cpp and server-context.cpp at master, on 2026-11-09
- **Checked:** 2026-11-09
- **Used in:** docs/dev-machine.md, contracts/vlm-request.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

llama-server exposes OpenAI-compatible chat completions, responses and embeddings routes; multimodal support is described as experimental. `--mmproj FILE` loads the multimodal projector (with `-hf` it is downloaded automatically unless `--no-mmproj`). `/v1/chat/completions` accepts `image_url` content parts (remote URL, base64 or data URI, or a local path with `--media-path` and a `file://` prefix; formats from stb_image such as jpeg, png, bmp, gif). `chat_template_kwargs` per request (example `{"enable_thinking": false}`) and `reasoning_effort` (`none` disables thinking); server flags `--reasoning [on|off|auto]` and `--chat-template-kwargs`. The native `/completion` route has `n_probs` and returns `completion_probabilities` with `top_logprobs`. Source check (server-common.cpp, master): the chat route reads `logprobs` and maps it to `n_probs = top_logprobs` (default 20 when absent), reads `reasoning_effort` (`none` removes it and disables reasoning) and `chat_template_kwargs`; by default pre-sampling logits are used for the probabilities (server-context.cpp). Native-route defaults from the README: temperature 0.8, top_k 40, top_p 0.95, min_p 0.05, repeat_penalty 1.1, presence and frequency penalties 0.0, random seed. Structured output: the chat route reads `response_format` with `json_object` (optional `schema`) or `json_schema` (nested `json_schema.schema`, or `schema`) and the native `json_schema` and `grammar` fields (not both together); server flags `--json-schema` and `--json-schema-file`.

## Caveats

Read at master, not at the build the user will install. `max_tokens` and `stop` handling on the chat route were not checked. Build and CUDA support for the RTX 5070 (Blackwell) and for Windows were not checked. The probe in experiment A2 confirms behaviour on the installed build.
