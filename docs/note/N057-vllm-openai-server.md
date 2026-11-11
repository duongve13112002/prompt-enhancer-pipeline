# N057 vLLM OpenAI-compatible server and multimodal inputs

- **Source:** https://docs.vllm.ai/en/latest/serving/openai_compatible_server/
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the raw markdown of docs/serving/openai_compatible_server.md at tag v0.11.0 and docs/features/multimodal_inputs.md at main on 2026-11-09 (the website page returned only a script shell)
- **Checked:** 2026-11-09
- **Used in:** contracts/vlm-request.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

vLLM serves a Chat Completions API (`/v1/chat/completions`) that it states is compatible with OpenAI's; vLLM-specific parameters go through the OpenAI client's `extra_body`. Image input uses `image_url` content parts with a URL, a base64 data URI or a `file://` path, with an optional `uuid`; `image_url.detail` is not supported; a chat template is required. Source check (vllm/entrypoints/openai/protocol.py at v0.11.0): `ChatCompletionRequest` has temperature, top_p, top_k, min_p, max_tokens, max_completion_tokens, presence_penalty, frequency_penalty, repetition_penalty, seed, logprobs (bool), top_logprobs (int), logit_bias, allowed_token_ids, stop, stop_token_ids, reasoning_effort (only low, medium or high), chat_template_kwargs, include_reasoning and n. Structured output fields on the same request (v0.11.0): `response_format` (with a JSON-schema form having `name`, `schema` and `strict`), `guided_json`, `guided_regex`, `guided_choice`, `guided_grammar`, `guided_decoding_backend`, `guided_whitespace_pattern` and `structured_outputs`.

## Caveats

Field names were verified at v0.11.0, older than the 0.33.0 pinned in N016; behaviour, not only names, is verified in the smoke test (experiment A2). No per-request logprobs-mode field and no thinking-budget field were found. Later versions may prefer `structured_outputs` over the `guided_*` names (assumption), and whether constraints apply before or after reasoning text was not checked.
