# VLM request contract

Every model call is built from a **role** in the configuration profile (`vlm.roles.<name>`) plus the **backend** the role binds to (`vlm.roles.<name>.backend`, defined in `vlm.backends.<backend>`). The configuration profile uses engine-neutral keys; an adapter maps them to the request of the engine. Nothing here is implemented or measured. Requirements R-015, R-018, R-024, R-026; the call rules for agents are in [VLM_CALL_RULES](../docs/rules/VLM_CALL_RULES.md).

Roles: `planner`, `writer`, `verifier`, `observer`, `card_builder`, `alias_generator`, `manifest_drafter`, `scenario_generator`, `info_assessor` (K2, text only: judges whether an info file gives usable concept names, decision 40), `folder_namer` (K2, names a folder that has no usable info file from images drawn at random, decisions 30 and 39). All have mode `generate`. This project has no first-token scoring role, so the logprob rules of the reference design do not apply and the role has no `logprobs` block.

## Rules

1. **Every field is required**; there are no implicit sampling defaults. A server's own defaults are not neutral: llama.cpp's server starts with non-zero sampling values and a repeat penalty ([N055](../docs/note/N055-llama-cpp-server.md)), so a role that omitted a field would get a value nobody chose. A value not yet measured is a sentinel (R-015).
2. **Warn and continue on unsupported parameters.** A value the engine cannot take is dropped from the request with a `PARAM_UNSUPPORTED` warning naming the field, stored with the call, and the run continues (`vlm.backends.<backend>.unsupported_param_policy: warn`); it is never dropped silently. Engine-specific fields go in `vlm.backends.<backend>.extra_body` and are merged last.
3. **Reasoning is a profile value.** `reasoning.mode` (`off`, `on`, `auto`), `reasoning.effort` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `backend_default`) and `reasoning.budget_tokens`; the name of the template switch is the configuration profile value `reasoning.template_kwargs_key`, never written in source, because it differs by model ([N121](../docs/note/N121-vllm-qwen38-serving.md)). Thinking on or off per role is decided by experiment E10.
4. **Structured output** uses `output.format` `json_object` or `json_schema` with a schema file, and `output.validate_response` is true for both. A constrained engine does not remove the check.
5. **Validate after the engine.** The response is parsed and checked against the schema file; an invalid one is stored with the warning `SCHEMA_INVALID` and never repaired by guessing. Engines can ignore schema keywords without saying so ([N122](../docs/note/N122-llama-cpp-multimodal-status.md)).
6. **Schemas are files.** JSON Schema draft 2020-12 in [schemas/](schemas/), versioned in the name; a change is a new version; each call stores `schema_id` and `schema_hash`. The role has its own schema, [vlm_role.v0](schemas/vlm_role.v0.schema.json).
7. **Reasoning together with structured output must be tested.** Strict output formats can lower reasoning quality ([N117](../docs/note/N117-format-restrictions-and-reasoning.md)), and whether a constraint applies before or after the thinking text depends on the engine ([N121](../docs/note/N121-vllm-qwen38-serving.md), [N122](../docs/note/N122-llama-cpp-multimodal-status.md)). Experiment E12 (image input plus JSON schema plus thinking off) and E10 (thinking on versus off) run before both are combined in a role.
8. **The writer's output** follows the admin profile: when the manifest names a schema, the role uses `json_schema`; when the format is prose or tags, `output.format` is `none`.

**Writer role.** `vlm.roles.writer.output.format` and `output.schema_id` hold `from_profile_manifest`: they are resolved for each request from the manifest of the request's admin profile, because the admin's system prompt defines the output format (R-004, decision 4). Its `prompt_id` names the Enhancement Brief template; the system message is the admin's prompt.

## Providers and backends (decision 28)

A backend under `vlm.backends` has a `provider`: `openai_compatible` (a local server: the user's deployment, or `llama-server` on the dev machine), `anthropic` (Claude) or `gemini`. Each role binds to one backend; `vlm.default_backend` is the local one, so cloud is used only where the configuration profile binds a role to a cloud backend. The same role fields apply to every provider and an adapter maps them; a field the provider cannot take is dropped with a `PARAM_UNSUPPORTED` warning. A cloud backend needs its key file under `.secrets/` (`api_key_file`), a number of requests granted by the user (`call_budget`, sentinel `ask_user`), and two policies (decision 29): when the budget is spent the role continues on the default backend and the response carries the warning `BUDGET_EXHAUSTED` (`on_budget_exhausted: fallback_to_default_backend`; the alternative is `stop`); images are sent to the provider when a role is bound to it, unless `allow_images` is false, in which case a request that carries an image runs on the default backend. The engine mapping rows for the two cloud providers are written by task T-009 from the providers' primary documentation; nothing about them is verified here **[A]**.

## Role fields

| Key | Meaning |
| --- | --- |
| `mode` | `generate` (the only value) |
| `backend` | name of an entry in `vlm.backends` |
| `prompt_id` | prompt contract id ([prompts/README.md](../prompts/README.md)); for `writer` the profile's system prompt and `brief_v0` |
| `max_tokens`, `sampling.{temperature, top_p, top_k, min_p, presence_penalty, frequency_penalty, repetition_penalty, seed, stop}` | generation limits and sampling. Effect of a presence penalty on JSON output: experiment E11 ([N017](../docs/note/N017-vllm-sampling-params.md)) |
| `reasoning.{mode, effort, budget_tokens, template_kwargs_key}` | thinking control; `template_kwargs_key` is the name of the chat-template switch |
| `image` | `none` (a text-only call), or an object with the image source (`source`, for example the edit source image), `max_side` (an integer longer side, `none` for the original, or `from_vision`; aspect ratio kept, never upscaled, decision 84), `format` and, for the offline observer, `quality` and `transport`; `from_vision` takes the value of the configuration profile's `vision` block |
| `output.{format, schema_id, strict, transport, validate_response}` | `format` is `none`, `json_object` or `json_schema`; `schema_id` names a file in `contracts/schemas/`; `strict` asks the engine for strict adherence; `transport` is `response_format` or `engine_native` |


## Engine mapping

Status per cell: **doc** with the note that supports it, or **A** = not verified, to test (E12 for the dev server, E13 for the deployment). The OpenAI column is the request shape the adapter targets, taken from the public API; no note in this repository covers it, so every cell in it is **[A]**. Backend versions are pinned in the profile (`pin_required`).

| Profile key | OpenAI-compatible request | vLLM | llama.cpp server |
| --- | --- | --- | --- |
| `temperature`, `top_p`, `seed` | same name | same name, doc [N017](../docs/note/N017-vllm-sampling-params.md) | same name, doc [N055](../docs/note/N055-llama-cpp-server.md) |
| `top_k`, `min_p` | not in the OpenAI schema | `top_k`, `min_p` via `extra_body`, doc N017 | same names, doc N055 |
| `presence_penalty`, `frequency_penalty` | same name | same name, doc N017 | same name, doc N055 |
| `repetition_penalty` | not in the OpenAI schema | `repetition_penalty`, doc N017 | named `repeat_penalty`, doc N055 |
| `max_tokens`, `stop` | same name | same name, doc [N057](../docs/note/N057-vllm-openai-server.md) | A |
| `reasoning.mode` | not in the OpenAI schema | `chat_template_kwargs {<template_kwargs_key>: bool}` per request, or a server default flag, doc [N121](../docs/note/N121-vllm-qwen38-serving.md) | A |
| `reasoning.effort` | `reasoning_effort` | A | A |
| `reasoning.budget_tokens` | not in the OpenAI schema | A | A |
| `output.format: json_object` | `response_format {type: json_object}` | `response_format`, doc N057 | A |
| `output.format: json_schema` | `response_format {type: json_schema, json_schema: {name, schema, strict}}` | `response_format` with schema, or the engine's native structured outputs, doc N121 | `response_format` with schema, doc [N122](../docs/note/N122-llama-cpp-multimodal-status.md); `strict` A |
| image input in a message | content part with an image URL or data URI | doc N121 (image limits are a server setting) | doc N122 (`mmproj` file required) |

## Images in requests

Images are downscaled by the client before sending, because one visual token covers 32x32 pixels and the default pixel cap of the deployment model is very large, so an unscaled photo can cost thousands of tokens ([N102](../docs/note/N102-qwen3-8-27b-serving-facts.md)). The limit is the profile value `vision.max_image_side`, with the encoding in `vision` (a role's `image` field names where it takes them from); it is `fit_from_bench`. Whether several images in one message and a JSON schema together with image input work on the dev server is unverified ([N122](../docs/note/N122-llama-cpp-multimodal-status.md), experiment E12). The observer reads one image per call by design ([K2](../docs/stages/K2-observe.md), [N107](../docs/note/N107-delimiter-token-scaling.md)). Text read from an image is data, never instruction ([N131](../docs/note/N131-image-prompt-injection.md), R-026).

## Stored with every call

Role, prompt id, `schema_id` and `schema_hash`, `request_hash` (role, prompt id, image settings, sampling, reasoning, output settings, engine and version, model revision), engine and engine version, model revision, warnings, latency. Online calls are stored in `request_traces`, offline calls in `sync_state` ([contracts/README.md](README.md#tables)), so an artifact or an audit can be replayed.

## Tests (planned)

A mapping test per engine from profile fragments to request bodies; a warning test for an unsupported field (dropped, warned, run continues); a test that a missing required field is refused; a test that every file in `schemas/` is a valid draft 2020-12 schema, that sample outputs validate and that an invalid output is recorded as `SCHEMA_INVALID`; probe tests with a fake server labelled as a double; E12 as the smoke test on the dev server and on the deployment.
