# T-008: K1 embeddings for images and concept text

Kind: feature. R-IDs: R-016, R-017, R-018, R-015, R-025. Depends on: T-007 (accepted 2026-11-15, `b4eb856`). Needs: dev GPU for the live smoke only.

## Goal

Implement K1 as [docs/stages/K1-embed.md](../../docs/stages/K1-embed.md) describes.

- **Part 1:** embed the new or changed `image_id`s that K0 reports. Each image is decoded once, its metadata goes to `images`, and it passes through the shared client-side resize of T-009 (`vision.max_image_side`) before embedding.
- **Part 2:** embed concept text with the same encoder. The text inputs come from K3 and K4, which do not exist yet, so build and test the interface with fixtures.
- **Output:** `image_embeddings` and `concept_text_embeddings` as Arrow tables. Each row is keyed by content hash plus embedder identity and revision.
- **Storage:** through the T-006 storage layer.
- **Resumability:** idempotent and resumable per input hash (R-017).
- **Embedder location:** the embedder is reached over HTTP through the configured backend (R-018), with every `embedding.*` field validated (R-015). An absent key is refused.

## Scope

- In scope: `src/prompt_enhancer/` (a K1 module, configuration keys `embedding.*`, the `vision.transport` key that `contracts/README.md` assigns to T-008), `contracts/`, `configs/*.example.yaml`, `docs/stages/K1-embed.md`, `tests/`.
- Out of scope: choosing the embedder (E5 decides it); K2 to K5; any download; any live GPU run without the PM's go under the `gpu-live-run` skill.

## Research required before code

The scout note `.tmp/T-008/research.md` answers these:

1. Which embedder serving APIs over HTTP fit the candidates in the references (N008, N104, N105, N106, N132)? Cover the llama.cpp server embedding endpoint, the vLLM embeddings or pooling API, and others. For each, give the request and response shape, image input support, and truncation and dimension options. Quote primary sources.
2. What do T-009 (`model_client.py`, `model_image.py`), T-006 storage and T-007 K0 already provide that K1 must reuse: the client, the resize, tables, checkpoints, the K0 change list? Give file:line.
3. Which configuration keys `contracts/README.md`, `configs/enhancer.example.yaml` and `contracts/schemas/enhancer_config.v0.schema.json` already define or reserve for K1, and which are missing?
4. Which questions about product meaning are open? Give each one a proposed conservative answer.

## Step 0

Builder: Claude Opus (decision 91; Codex reviews). Base `b4eb856`. R-IDs: R-015, R-016, R-017, R-018, R-025 (and R-014: the knowledge base is read only). Web copies read for this step: `<project>/.tmp/worktrees/T-008/.tmp/T-008/web/` (scratch, removed at handoff).

### A. Primary-source research (R-021)

**A1. llama.cpp server: documented.** Source: `https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md`, last commit on that file `32bdc3a8` (2026-11-14), read raw.
- Lines 348-354: "Multimodal support ... is currently an experimental feature. It is currently available in the following endpoints: The OAI-compatible chat endpoint. The non-OAI-compatible completions endpoint. The non-OAI-compatible embeddings endpoint."
- Lines 1730-1745, `POST /v1/embeddings`:
  - "This endpoint requires that the model uses a pooling different than type `none`. The embeddings are normalized using the Eucledian norm."
  - "For multimodal models (loaded with `--mmproj`), each element of `input` can also be an object with a `content` array, using the same parts as `/v1/chat/completions`". The image part is `{ "type": "image_url", "image_url": { "url": "..." } }`: "remote URL, base64 data URI, or local file (`file://`, requires `--media-path`)".
  - "Each object gives one embedding. This input shape is not part of the OpenAI Embeddings API".
  - The example sends a list of such objects in one request, so a request can carry a batch.
- Line 763, native `/embedding`: "This endpoint also supports multimodal embeddings", using the `/completions` prompt shape `{ "prompt_string": ..., "multimodal_data": ["base64"] }`, with one `<__media__>` marker per item (line 524).
- Server needs: `--embedding` ("use only with dedicated embedding models"), a pooling other than `none`, and `--mmproj FILE` for images.
- The README documents no `dimensions` request field for `/v1/embeddings`. It only points to the OpenAI documentation. Treat it as unsupported.
- Not confirmed by any primary source: that a GGUF plus mmproj of Qwen3-VL-Embedding or EmbeddingGemma 2 loads in llama.cpp. Neither model card covers llama.cpp.
- The README is read at master. The user's pinned build must contain this feature, and the live smoke checks it.

**A2. vLLM: documented and source-verified.**
- Page: `https://docs.vllm.ai/en/latest/models/pooling_models/embed/`.
  - Multimodal input requires "passing a list of `messages` in the request".
  - Some models need a custom `--chat-template`. VLM2Vec needs "we have to explicitly pass `--runner pooling`".
  - "`dimensions` must not exceed the model's output embedding size", and vLLM "returns an error" for models without Matryoshka support.
  - Truncation is a request field, `truncate_prompt_tokens`.
  - "`Qwen3-VL-Embedding` officially uses `qwen_vl_utils` for image preprocessing", and vLLM's preprocessing may differ slightly.
- Example `examples/pooling/embed/vision_embedding_online.py` at main, last commit `32e0f407`, read raw:
  - The request is `client.post("/embeddings", body={"messages": ..., "model": ..., "encoding_format": ...})`, one conversation per request.
  - Lines 157-225, Qwen3-VL-Embedding-2B: the server starts with `vllm serve Qwen/Qwen3-VL-Embedding-2B --runner pooling --max-model-len 8192`.
  - The image request is: a system message with the text "Represent the user's input."; a user message holding `[{"type":"image_url",...},{"type":"text","text":""}]`; an empty assistant message; and `continue_final_message=True, add_special_tokens=True`.
- EmbeddingGemma 2: PR `https://github.com/vllm-project/vllm/pull/60254`, "[Model] Support EmbeddingGemma2 multimodal pooling architecture", is merged (2026-10-06). It shows image input only through the offline `llm.embed(... multi_modal_data ...)`. The HTTP image route is not shown, so it is unconfirmed.

**A3. Candidate embedders.**
- `https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B`:
  - "transformers>=4.57.0", "qwen-vl-utils>=0.0.14".
  - The vLLM example uses `runner="pooling"`, dtype `bfloat16`.
  - "Up to 2048, supports user-defined output dimensions ranging from 64 to 2048".
  - Instruction "Represent the user's input." is the default.
  - The card says nothing on normalisation and nothing on llama.cpp.
- `https://huggingface.co/google/embeddinggemma-2`:
  - "Prefixes apply to text only. Pass images, video, and audio without any prefix."
  - "Run inference in `bfloat16` or `float32`. Do not use `float16`."
  - "The supported dimensions are 768, 512, 256, and 128."
  - "The shortened vector must be L2-normalized before it is used for cosine similarity."
  - "280 tokens per image (default)".
  - The card has no HTTP serving instructions, so image embedding over HTTP is not confirmed for either server.

**Conclusion.**
- Image embedding over HTTP is documented for two cases:
  - llama.cpp: generic, for any model loaded with `--mmproj`;
  - vLLM: per model, through `messages`; an official example exists for Qwen3-VL-Embedding.
- The exact request layout differs per engine and per model. So the design is a backend adapter with a configured request shape, and the image route defaults to `design_required`. K1 refuses to run until a profile sets the shape, and the later live smoke under `gpu-live-run` proves it.
- The dev profile pairs EmbeddingGemma 2 with llama.cpp. No primary source confirms that pairing (Q3).

### B. Scout claims re-read (each opened at the cited lines)

- Confirmed:
  - `model_image.py:24-31,34-83` (resize, alpha, EXIF, preserved bytes);
  - `model_transport.py:10-25` (`endpoint_url`: loopback or HTTPS) and `:28-65` (`ModelTransport`: retries, concurrency);
  - `model_resources.BackendConcurrency.for_backend` (line 66);
  - `config.py:16-17` (sentinels) and `:34-37` (`kb sync` reads all of `embedding` and `vision`, and then `_sentinel` runs on them, at `:626-629`);
  - `tables.py:87-100` (specs), `:206-213` (dim required), `:291-292` (one revision per file), `:313-322` (`check_embedding_space`: one revision and one dim across both vector tables);
  - `store.py:901-991` (`Checkpoints`: `done` re-hashes; `commit` refuses changed bytes of a referenced artifact at `:944-947`; vector tables are written as Arrow at `:951`);
  - `identity.py:55-66` (`artifact_key(kind, input_hash, {model,prompt,schema})`) and `:69-72` (keys are 64-hex);
  - `k0_sync.py:85-88` (handoff);
  - `k0.py:168-171` (K0 leaves `format/width/height/decode_status` null);
  - `k0_checkpoint.py:171-172` (K0 keeps the previous `images` row for an unchanged `image_id`, so the K1 metadata persists);
  - schema `embedding` at `enhancer_config.v0.schema.json:651-797` (`additionalProperties: false`), `vision.transport` free string at `:2970-2987`;
  - README `:144` (owner of backend, dimension, batch and devices is T-008).
- Corrections to the scout:
  - The handoff carries every image row, not only new or changed ones. Change shows only in `plan['images']`. K1 must cover every image in the snapshot under the current embedder, because the snapshot holds one embedding space. Images already done are skipped by key, so in effect only new or changed images, or a changed embedder, cost a call.
  - `model_client.py:280` hard-codes the data URI for roles. Role `image.transport` is a separate per-role key.
  - Backend `endpoints` require `chat` and `health` with `additionalProperties: false` (schema `:3480-3527`). `config.py:508,519` index `endpoints['chat']`.

### C. Configuration design (Configuration impact to be filled before the first runtime edit)

Q1 is the design choice: the embedder is a dedicated `vlm.backends` entry, referenced by name. This reuses `ModelTransport`, `http` retries, `max_concurrency`, `image.max_decode_pixels`, the base URL rules and deployment overlays (`config.py:389` already lets an overlay change backends) instead of copying them.

| Key | Schema change | Sentinel / example | Override test | Refusal test |
| --- | --- | --- | --- | --- |
| `embedding.backend` | new required; string, minLength 1, or sentinel | `deploy_required`; dev: `embedder` | a profile naming backend `emb_b` sends to that backend's base URL and route (HTTP double records the URL) | unknown name; provider not `openai_compatible` (embedding stays local, R-018); missing key; sentinel at `kb sync` |
| `vlm.backends.*.endpoints.embeddings` | new optional route; `chat` becomes required only when a role binds the backend (`config.py:508,519` switch to that rule) | example `deploy_required`; dev `v1/embeddings` | non-default route `alt/embed` reaches the double | absent on the backend `embedding.backend` names; absolute or `..` route (`endpoint_url`) |
| `embedding.request.shape` | new; enum `openai_input_content` (llama.cpp A1), `vllm_chat_messages` (vLLM A2), or sentinel | `design_required` | each shape produces its documented body (golden JSON) | sentinel; unknown value; `openai_input_content` on engine `vllm` or `vllm_chat_messages` on `llama_cpp` |
| `embedding.request.image_instruction` | new; string or `none` | `design_required` (Qwen card default "Represent the user's input."; EmbeddingGemma: `none`) | the value appears in the system message or text part | sentinel; non-string |
| `embedding.request.extra_body` | new; object restricted to `continue_final_message`, `add_special_tokens` (booleans, from A2), merged verbatim | `design_required` | values reach the body | unknown member; non-boolean |
| `embedding.dimension` | new; integer >= 1 or sentinel | `fit_from_gt` (E5); dev 768 | dim 256 builds a `fixed_size_list<float32,256>` table | 0, bool, sentinel; a response vector of another length fails the item `DIMENSION_MISMATCH` |
| `embedding.dimension_mode` | new; enum `native`, `request_field` (sends `dimensions`, vLLM A2), `client_prefix_l2` (leading values then L2, EmbeddingGemma card A3) | `design_required` | `client_prefix_l2` cuts 8 to 4 and normalises | `request_field` on `llama_cpp` (undocumented, A1); `native` with a response length that differs from `dimension` |
| `embedding.batch_size` | new; integer >= 1 or sentinel | `deploy_required`; dev 8 | 5 images with batch 3 give requests of 3 and 2 | 0, bool, sentinel; > 1 with `vllm_chat_messages` (one conversation per request, A2) |
| `embedding.devices` | not added (Q2) | n/a | n/a | n/a |
| `vision.transport` | free string becomes enum `data_uri` or sentinel (protocol-bound: both engines document base64 data URIs; `file://` needs a server `--media-path` and would expose knowledge-base paths) | `data_uri` (unchanged) | only one value is accepted, so no non-default exists: the test asserts that `data:image/<vision.format>;base64,` reaches the body | `file_uri`, `url`, empty |

- `load_config` also refuses `embedding.revision` different from `vlm.backends[embedding.backend].model.revision`, because two revisions of one embedder must not coexist, and `embedding.chosen` not listed in `embedding.candidates`.
- `COMMAND_READS['kb sync']` gains a read of the referenced backend, using the same dynamic pattern as roles (`config.py:636-637`).
- K1 also reads `vision.max_image_side`, `format`, `quality` and `alpha_policy`, all through `prepare_vision_image`.
- `embedding.query_text_limit` stays an S1 key and is not read by K1.

### D. Data flow (Part 1)

1. **K0 handoff.** Input is `sync()['handoff']['k1']`, which holds `images` and `folder_images`, plus the K0-resolved knowledge root.
2. **Read and verify.** For each `image_id`, K1 reads the first member path in sorted order. It reuses K0's no-follow `_opened` per path component (`k0_scan.py:118-137`), read only (R-014), and verifies `sha256(bytes) == image_id`. On a mismatch it tries the next path. When every path fails, the item gets a `fail` row with `SOURCE_CHANGED` and is retried at the next sync.
3. **Single decode.** A new `model_image.prepare_image_with_info` opens the image once. It returns `{format, width, height}` (the stored image before EXIF orientation) and the prepared bytes. `prepare_image` becomes a thin wrapper, so T-009 behaviour and tests stay as they are.
4. **`images` metadata.**
   - `decode_status` is `ok`, `undecodable`, `exceeds_max_decode_pixels` or `alpha_refused`. These values join the contracts enum table, because no enum exists yet (`tables.py:89`).
   - These are deterministic failures. They are committed as done artifacts without a vector, so they are not retried until the key changes.
5. **Resize.** The shared resize applies `vision.max_image_side` and the format and quality, with `max_decode_pixels` from the referenced backend.
6. **Embed.**
   - Items are sent in batches of `batch_size`, with bounded concurrency through `BackendConcurrency`, over `ModelTransport` (retries, timeouts).
   - The adapter builds the body. The parser refuses:
     - a count that differs from the inputs;
     - indices that are duplicated or out of range;
     - non-finite values;
     - a length different from `dimension` after `dimension_mode`.
   - Transport, HTTP or parse errors give a `fail` row (retried), never a done row.
7. **Checkpoints.**
   - Each image gets one `k1_image` blob artifact: canonical JSON with layout version, `image_id`, metadata, `embedder_revision`, `dim`, and the vector as base64 of float32 little-endian bytes (exact round trip).
   - The artifact is committed atomically before its done row (`store.py:934-966`). A rerun skips done keys (`done()` re-hashes) and redoes the rest (R-017).
   - The input key is `artifact_key('k1_image', image_id, {model: embedder_revision, prompt: H(shape, image_instruction, extra_body), schema: H(K1 layout version, vision.max_image_side/format/quality/alpha_policy/transport, max_decode_pixels, Pillow version)})`.
   - `embedder_revision` is `H(engine name and version, model id and revision, embedding.chosen and revision, dimension, dimension_mode, shape)`. It leaves out instructions and image settings, so image and text rows share one revision, as `check_embedding_space` requires.
8. **Arrow tables.**
   - Output is the updated `images` rows and an `image_embeddings` Arrow table, built by `Catalog.build(..., dim)` and checked by `check_embedding_space`.
   - The tables are returned to the caller. K1 never publishes, like K0 (`k0_sync.py:50`).

**Determinism under the immutability check.**
- Server output is not guaranteed bit-identical across runs or batch compositions (assumption, not measured). So K1 never recomputes a key whose artifact is intact.
- With `sync.resume: false`, if the key's artifact is referenced by a live snapshot, K1 reuses it, because its bytes cannot change (`store.py:944-947`). The report notes this as `REUSED_REFERENCED`. An unreferenced artifact is recomputed and replaced (Q5).

### E. Part 2 (interface and fixtures only)

- Interface: `embed_concept_texts(writer, embedder, items)`, where `items` are `ConceptText(concept_id, text_kind, text)`.
- `text_hash = sha256(UTF-8 text)`. The key is `artifact_key('k1_text', text_hash, ...)`, with `text_kind` and the template hash in `prompt`.
- The text is sent verbatim through `embedding.request.text_template`, a string with exactly one `{text}` placeholder (sentinel `design_required`, because EmbeddingGemma uses per-task prefixes). Per-kind templates wait for K3 and K4.
- Output is a `concept_text_embeddings` Arrow table with the same `embedder_revision`.
- Over-long text is never truncated by K1. A server refusal fails the item with its reason.
- Tests use synthetic `ConceptText` fixtures and the HTTP double, with several aliases of one concept to exercise the `text_hash` key.

### F. Questions (ranked; proposed conservative answers)

1. **Where does the embedder's backend live?** Proposed: a named `vlm.backends` entry, as in C. It reuses the transport and overlay patterns. `load_config` requires the revision to agree with `embedding.revision`.
2. **`embedding.devices`.** Over HTTP (R-018) the client cannot place a model on a device. The server launch does, through `runtime.gpus`, `vram_budget_gb` and the `gpu-live-run` plan. Proposed: do not add an unread key, which would fail the audit. Amend `contracts/README.md:144` and `K1-embed.md:11` to say that devices belong to the embedding server launch. PM decision pending user review.
3. **Image request shape before a live proof.** Proposed: implement both documented adapters; leave `request.shape`, `image_instruction` and `extra_body` as `design_required` in the example and dev profiles; refuse to run K1 until the live smoke fixes them. Flag to the user: the dev pairing EmbeddingGemma 2 on llama.cpp has no primary confirmation, while Qwen3-VL-Embedding-2B on vLLM does. PM decision pending user review.
4. **Stored image dimensions.** Proposed: the stored file's pixel size before EXIF orientation, and the format as Pillow names it (for example `JPEG`). This keeps `images` a fact about the file. Reversible: it is one documented column rule.
5. **`sync.resume: false` against a referenced artifact.** Proposed: reuse the referenced bytes with `REUSED_REFERENCED`, rather than fail the sync, as in D.

### G. Plan (checkable finish)

1. **Configuration.** Schema, example, dev and overlay profiles, `config.py` validation and `kb sync` reads, with tests `tests/test_k1_config.py`: one override and one refusal per row of C. First fill `Configuration impact`.
2. **Decode once.** `model_image.prepare_image_with_info` and the safe reader, with tests. The T-009 image tests stay green.
3. **K1 module.** `src/prompt_enhancer/k1_embed.py` (adapters, parser, keys, checkpoints, tables), with `tests/test_k1_embed.py`.
   - Tests use a labelled `httpx.MockTransport` double, with no network.
   - They cover: resume after a simulated crash, rerun with no calls, an embedder change that re-embeds, `SOURCE_CHANGED`, the decode failures, and `REUSED_REFERENCED`.
   - Property tests (hypothesis): response parsing, which refuses malformed `data`/index/length/NaN, and key derivation, which changes with each keyed field and stays stable otherwise.
4. **Part 2.** `embed_concept_texts`, with `tests/test_k1_text.py`.
5. **Finish.**
   - Docs: `K1-embed.md`, the contracts README enums and the owner line.
   - The full suite: `PYTHONPATH=src` through `test_slot.py`, `-n 2`.
   - `ruff check` and `ruff format --check` on changed files.
   - 3 to 10 mutants on a scratch copy, for example: the batch slice off by one, the dimension check removed, the index order ignored, the hash verification removed, `max_image_side` dropped from the key, done-skip removed, the revision agreement check removed. Survivors are reported.
   - Configuration audit row by row, then the Completion report.
   - Finished means all of this is green. The live GPU smoke (`gpu-live-run`, needs a model download the user has not approved) stays `not_run`.

### PM answers to Step 0 (2026-11-15, PM decisions pending user review, decision 94)

- **Q1:** yes. The embedder is a named `vlm.backends` entry, and `load_config` requires its `model.revision` to equal `embedding.revision`. `endpoints.chat` is required only when a role binds the backend.
- **Q2:** yes. There is no `embedding.devices`. `contracts/README.md:144` and `K1-embed.md:11` now say that devices belong to the embedding server's launch.
- **Q3:** yes. Both adapters are built. `request.shape`, `image_instruction`, `extra_body` and `text_template` stay `design_required` in the example and dev profiles, and K1 refuses to run until they are set.
  - **Dev-pairing note:** no primary source confirms EmbeddingGemma 2 image embedding on llama.cpp. Qwen3-VL-Embedding-2B on vLLM has an official example (A2). The PM takes this to the user with E5.
- **Q4:** yes. `images` records the stored file's pixel size before EXIF orientation, and Pillow's format name.
- **Q5:** yes. An artifact a live snapshot references is reused and reported as `REUSED_REFERENCED`.
- **Entry point:** K1 keeps its own entry point, and the tests go through it. No CLI command in this task.

### Configuration impact

Every setting K1 touches, with its schema location (`contracts/schemas/enhancer_config.v0.schema.json`, validated in `src/prompt_enhancer/config.py`) and its profiles:

- **Model, provider, endpoint:**
  - `embedding.backend` names an entry under `vlm.backends.<name>`. From that entry K1 uses `provider`, `engine`, `base_urls`, `model.id`, `model.revision`, `endpoints.embeddings`, `http.*`, `max_concurrency` and `image.max_decode_pixels`.
  - Profiles:
    - full example: `deploy_required`;
    - dev example: an `embedder` backend with deployment sentinels;
    - `configs/overlays/deploy-vllm.example.yaml`: unchanged. An overlay may override `vlm.backends` but not `embedding.backend` (`config.py:376`), so a deployment names its embedder backend in its own profile. See Noticed.
- **Model identity:** `embedding.chosen`, which must be a listed candidate; `embedding.revision`, which must equal the backend's `model.revision`.
- **Request layout:** `embedding.request.shape`, `image_instruction`, `extra_body`, `text_template`. All are `design_required` in both example profiles.
- **Output size:** `embedding.dimension` (`fit_from_gt` in the full example, 768 in dev), and `embedding.dimension_mode` (`design_required`).
- **Batch:** `embedding.batch_size` (`deploy_required` in the full example, 8 in dev). Concurrency comes from the backend's `max_concurrency`.
- **Image sent to the model:** `vision.max_image_side`, `format`, `quality` and `alpha_policy` (existing keys), plus `vision.transport`, which becomes an enum with the single value `data_uri`.
- **Resume:** `sync.resume` (existing, read through the storage layer).
- **Paths:** `knowledge.root`, which K1 reads only; `storage.kb_store` (existing).
- **Not applicable here:**
  - Devices, VRAM, dtype and lifecycle belong to the server launch over HTTP (R-018, Q2).
  - K1 has no sampling or seed.
  - Timeouts and retries are the backend's existing `http.*` keys.

### Build log

- **Step 1, configuration (done):**
  - Changed: the schema, the example and dev profiles, and `config.py` (`_validate_embedding`, `embedder_reads`, the `kb sync` reads, and a chat route required only when a role or the default binds the backend).
  - Updated existing tests for the contract change: `test_model_config` (the `vision.transport` row), `test_field_contracts` (the new keys' cases), `conftest`'s synthetic embedder, and `test_k0_config` (the chosen embedder is a candidate). The affected files pass.
- **Step 2, decode once (done):**
  - `model_image.prepare_image_with_info` and `ImageRejected`; `prepare_image` is now a wrapper.
  - `k0_scan.read_source_file`, which refuses a link on every path component.
  - `Checkpoints.reusable`, for Q5.
- **Step 3, the K1 module (done):** `k1_embed.py`, with `tests/k1_fixtures.py` (a labelled HTTP double) and `tests/test_k1_embed.py`. 23 tests pass.

- **Step 4, Part 2 and configuration tests (done):**
  - `tests/test_k1_text.py` (7 tests) and `tests/test_k1_config.py` (54 tests) pass.
  - Docs updated: `K1-embed.md`, the `contracts/README.md` owner line, and the `decode status` enum.
- **Step 5, finish (done):** the full suite passed, all 10 mutants are killed, and the audit and Completion report are below. The scratch is removed; `.tmp/T-008/research.md` is the scout's note and is kept.

### Noticed, not done

- `model_client.py:280` sends roles' images as data URIs regardless of `vision.transport` or the role's `image.transport`. Who owns that is outside this task.
- `kb sync` checks every key of `observe`, `cards`, `aliases` and `index` for sentinels (`config.py:34-37,626-629`). So K1 cannot run under `kb sync` until those stages' sentinels are fitted. The K1 tests call K1's entry point directly.
  - The wholesale `embedding` read also checks `embedding.query_text_limit` (an S1 key, `design_required` in the example) and `candidates`.
- A deployment overlay can override `vlm.backends` but not `embedding.backend` (`config.py:376`). The full example sets `embedding.backend: deploy_required`, so each deployment names its embedder backend in its own profile.
- The shared resize refuses a palette or 16-bit image without transparency when `vision.format` is `jpeg` (`encode_failed`). This is existing T-009 behaviour, and K1 now records it per image.

## Fix round 1

Codex review `.tmp/T-008/review/review.md`: FAIL, F1 to F3 (P2). Q1 to Q5 stand. R-IDs: R-007, R-015, R-017, R-018.

### Step 0 (pre-approved by the PM; recorded, then continued)

- Unknowns: whether any other response-to-number step escapes the parser boundary besides the two the review names; whether any other read key is missing from the read set; which stored metadata values lack an exact property.
- Questions: none. The brief fixes the plan; Q1 to Q5 unchanged.
- Plan: (1) the three class tables below; (2) regressions written first and run against the unfixed code; (3) fixes; (4) mutants including M11; (5) K1 files, then the full suite once, through the test slot; (6) the audit updated.
- Finish: every row below is contained or shown safe and tested; regressions fail before and pass after; the mutants are killed or reported.

### Table 1: model response to numbers (image and text paths share `Embedder._post`)

Both `embed_images` and `embed_concept_texts` reach the response only through `Embedder._post` (`k1_embed.py:256-268`), then `parse_embeddings` (`:183-204`), `_reduce` (`:157-180`) and `pack_vector` (`:148`). Every row is contained as `EmbeddingError`, so `_in_batches` (`:282-289`) turns it into a `failed` checkpoint and re-raises only genuine defects.

| Step | Where | Failure mode | Disposition | Test |
|---|---|---|---|---|
| HTTP status and transport | `:258-264` | transport error, non-2xx | contained: `HTTP_ERROR` (unchanged) | `test_http_error_fails_items_without_done_rows`, `test_server_refusal_fails_the_item_without_truncation` |
| JSON decode | `:265-267` | `ValueError` (bad JSON, bad UTF-8, an integer over 4300 digits, all measured); `RecursionError` from nesting deeper than the decoder (measured: `json.loads('[' * 100000)` raises `RecursionError`) | **fixed**: both contained as `RESPONSE_INVALID`; `RecursionError` used to escape and abort the stage | `test_undecodable_json_response_fails_the_item_with_a_failed_checkpoint` |
| `data` list and count | `:185-187` | not a list, wrong length | contained (unchanged) | `test_parse_refuses_malformed_responses` |
| `index` | `:190-192` | non-int, bool, out of range, repeated | contained; only comparisons on an exact Python int, no conversion (safe) | same |
| value types | `:194-195` | non-number, bool | contained; `type()` excludes bool | same |
| int to float | `:196-199` | a JSON integer beyond the float range (`10**400`): `math.isfinite(int)` raised `OverflowError` (review F1, native) | **fixed**: every value is converted with `float()` inside the boundary; `OverflowError` becomes `RESPONSE_INVALID` | `test_unrepresentable_response_fails_the_item_with_a_failed_checkpoint`, `test_unrepresentable_response_fails_the_text_with_a_failed_checkpoint`, `test_parse_native_over_the_whole_numeric_range` |
| finiteness | `:200-201` | NaN, infinity (JSON `1e400` decodes to infinity) | contained; now on floats only | `test_parse_refuses_malformed_responses` (`nan`), both whole-range properties |
| length | `:160-161`, `:171-172` | length differs from `dimension` | contained: `DIMENSION_MISMATCH` (unchanged) | `test_response_of_another_length_fails_the_items` |
| prefix L2 norm | `:162-170` | `fsum` of squares overflowed at about 1e154 (review F1, prefix); squares of subnormals underflowed to 0 and a valid vector was refused | **fixed**: divide by the largest magnitude, then `math.hypot` over values in [-1, 1], so the norm lies in [1, sqrt(dimension)] (scaling chosen over bare `hypot`, which returns `inf` for `hypot(1.7e308, 1.7e308)`, measured); an all-zero prefix is `RESPONSE_INVALID` | `test_client_prefix_l2_normalises_finite_extremes` (1e154, 1.7e308, mixed signs, 5e-324), `test_parse_client_prefix_l2_over_the_whole_numeric_range` |
| float32 packing | `:173-176` | magnitude at or above 2^128 - 2^103 | contained: `RESPONSE_INVALID` (unchanged); float64 subnormals round to a float32 subnormal or zero, which is finite and accepted | `test_parse_refuses_values_outside_float32`, `test_parse_native_over_the_whole_numeric_range` |
| unpack check | `:177-179` | none after a successful pack | kept as a guard (safe) | properties |
| `count // len(bodies)` | `:271` | none: both from K1's own batches | safe, not response data | n/a |
| checkpoint base64 and unpack | `:297-307`, `:423`, `:484` | K1's own bytes, length checked by `_load_record` | safe, not response data | resume tests |

The properties draw from unbounded integers, integers to 2^1100, all finite floats, floats within 1e-300 (subnormals) and fixed extremes (float32 and float64 overflow boundaries and their neighbours, 5e-324, the float32 subnormal 1e-45, signed zeros). Their oracle uses the IEEE 754 boundaries, not the parser.

### Table 2: configuration keys each K1 entry point reads

Entry points: `embed_images` and `embed_concept_texts` (standalone) call `embedder_settings` first (`k1_embed.py:357`, `:436`); nothing reads a source file, opens the store or builds a request before it returns. `kb sync` reads the whole `embedding` subtree (`config.py:34`) plus `embedder_reads` (`config.py:732`).

| Key read | Read at | In read set before | Now | Sentinel and missing refusal (standalone) |
|---|---|---|---|---|
| `embedding.chosen` | `_validate_embedding`, revision | yes | yes | `test_standalone_entry_points_refuse_a_sentinel_in_every_read_key`, `..._missing_read_key` |
| `embedding.candidates` and each `id` | `_validate_embedding` `config.py:612-615` | **no** (F2): the membership check was skipped for a sentinel | **yes** (`K1_READS`) | same two, `..._nested_sentinel[embedding.candidates.0.id]`, `test_candidates_sentinel_refuses_k1_after_a_command_that_does_not_read_it` (review probe) |
| `embedding.revision`, `backend`, `dimension`, `dimension_mode`, `batch_size` | `embedder_settings`, `_validate_embedding` | yes | yes | same two |
| `embedding.request` | `embedder_settings` | yes (container only) | yes | same two |
| `embedding.request.shape`, `image_instruction`, `extra_body`, `text_template` | indexed at `k1_embed.py:105-114` | only via the container: a missing leaf raised `KeyError` | **leaves added** | same two |
| `vision.max_image_side`, `format`, `quality`, `alpha_policy`, `transport` | `check_vision_settings`, `EmbedderSettings.vision` | yes | yes | same two; `..._nested_sentinel[vision.alpha_policy.mode]`; alpha members validated by `check_vision_settings` |
| backend `provider`, `model.id`, `model.revision`, `endpoints.embeddings`, `max_concurrency`, `image.max_decode_pixels` | `embedder_settings`, `ModelTransport` | yes | yes | same two |
| backend `engine`, `engine.name`, `engine.version` | `resolve_backend_engine` | container only | **leaves added** | same two |
| backend `http` and its 8 fields | `ModelTransport` `model_transport.py:36-65` | container only | **leaves added** | same two |
| backend `base_urls` (element 0) with `endpoints.embeddings` | `ModelTransport.post` `model_transport.py:47` | yes, but an empty list or an invalid URL surfaced only at the first request, after source reads | **checked in `embedder_settings`** with `endpoint_url` | `test_standalone_entry_points_refuse_an_unusable_endpoint` (empty, `ftp://`, `../escape`) |
| `embedding.query_text_limit` | not read by K1 (S1 key) | no | no | `UNKEYED` in the key property; refused only by `kb sync`'s whole-subtree read |
| `sync.resume`, `storage.*` | the store's own settings (`store.py:228`, `:922`), not K1's `config` argument | n/a | n/a | T-006 storage tests |

`test_standalone_read_set_is_the_list_k1_reads` pins `embedder_reads` to the explicit list, so a key added to or dropped from the read set fails a test. Each standalone refusal test records source reads with a labelled double of `read_source_file` and asserts no read and no request.

### Table 3: image metadata K1 stores

`_decode` (`k1_embed.py:336-348`) gives the values the `k1_image` artifact and the `images` row carry.

| Value | Meaning | Property or test |
|---|---|---|
| `format` | Pillow's name for the stored file's format (Q4) | `test_decode_records_the_stored_files_format_and_size` (PNG, JPEG, WEBP, BMP, GIF, TIFF); entry point: `test_embeds_every_image_once...`, `test_size_is_recorded_before_exif_orientation` |
| `width`, `height` | the stored file's pixel size before EXIF orientation (Q4) | the same property over sizes 1 to 24, untagged and all 8 orientations, also asserting the prepared image is turned for 5 to 8 |
| metadata of a refused image | exact file metadata for `exceeds_max_decode_pixels`, `alpha_refused`, `encode_failed` | **new** `test_refused_images_keep_the_stored_files_metadata` (sizes 2 to 24); `test_policy_refusals_are_recorded_with_file_metadata` now asserts exact `(format, width, height)` instead of truthiness (F3) |
| metadata of an unreadable image | null `format`, `width`, `height` for `undecodable` (garbage, truncated PNG) and for Pillow's decompression-bomb refusal, which raises before a size is available | **new** `test_undecodable_images_store_no_metadata`, `test_decompression_bomb_stores_no_metadata` |
| `decode_status` | `ok`, `undecodable`, `exceeds_max_decode_pixels`, `alpha_refused`, `encode_failed` (contracts enum) | each asserted exactly by the tests above |
| no K1 record (`SOURCE_CHANGED`) | the K0 row is left as is | `test_changed_source_fails_then_recovers` |

### Regressions: before and after

Command (each run): `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=line tests/test_k1_embed.py tests/test_k1_text.py tests/test_k1_config.py -k "unrepresentable or undecodable_json or extremes or whole_numeric or stored_files or refused_images or undecodable_images or bomb or standalone or candidates_sentinel or policy_refusals"`, `PYTHONPATH=src`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`.

- Before the fix (tests added, runtime unchanged): `33 failed, 65 passed, 88 deselected in 33.36s`. Failing: the numeric regressions and both whole-range properties (F1, image and text), `test_standalone_read_set_is_the_list_k1_reads`, the candidates sentinel, nested and missing cases and the reviewer's probe (F2), the missing-leaf cases for `request.*`, `engine.*`, `http.*`, and the three unusable-endpoint cases. The metadata properties pass on the unfixed code, as expected: F3 is a test gap, and they kill M11 below.
- After the fix: `98 passed, 88 deselected in 9.72s`.

### Fixes

- `k1_embed.parse_embeddings`: values are converted to float inside the boundary (`OverflowError` becomes `RESPONSE_INVALID`), then checked for finiteness. `Embedder._post` also contains `RecursionError` from the JSON decoder.
- `k1_embed._reduce`: `client_prefix_l2` divides by the largest magnitude, then by `math.hypot`; a zero prefix is `RESPONSE_INVALID`. Dropped: bare `math.hypot`, which returns `inf` for finite vectors near the float limit (measured above).
- `config.K1_READS`, `EMBEDDER_BACKEND_READS`: `embedding.candidates`, the four `request` leaves, `engine.name/version` and the 8 `http` fields join the read set (containers first).
- `k1_embed.embedder_settings`: `endpoint_url(base_urls[0], endpoints.embeddings)` is checked before anything else runs.
- Tests: the regressions and properties named in the tables; `test_policy_refusals_are_recorded_with_file_metadata` asserts exact metadata.

### Mutants (`.tmp/T-008/fix1/mutate.py`, scratch copies of `src/`, through the test slot, `-x`)

All 9 killed (`.tmp/T-008/fix1/mutants.log`):

| ID | Mutation | Result |
|---|---|---|
| M11 (reviewer) | `ImageRejected.info` forced to width 1, height 1 | killed: `1 failed, 7 passed, 1 skipped` |
| M12 | int-to-float handler catches `ZeroDivisionError` instead of `OverflowError` | killed: `1 failed, 23 passed, 1 skipped` |
| M13 | normalisation back to `sqrt(fsum(v*v))` on unscaled values | killed: `1 failed, 27 passed, 1 skipped` |
| M14 | scaling by the largest magnitude removed | killed: `1 failed, 28 passed, 1 skipped` |
| M15 | zero-prefix check `== 0` to `< 0` | killed: `1 failed, 37 passed, 1 skipped` |
| M16 | `RecursionError` dropped from the JSON handler | killed: `1 failed, 26 passed, 1 skipped` |
| M17 | endpoint check in `embedder_settings` removed | killed: `1 failed, 126 passed` |
| M18 | `embedding.candidates` removed from `K1_READS` | killed: `1 failed, 51 passed` |
| M19 | `http.retry_statuses` removed from the read set | killed: `1 failed, 51 passed` |

### Results

- K1 files: `tests/test_k1_embed.py tests/test_k1_text.py tests/test_k1_config.py`, the command above without `-k`: `185 passed, 1 skipped in 53.76s` (the skip is the symbolic-link test on this Windows account).
- Full pipeline suite, once: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=line tests`, `PYTHONPATH=src`: `2016 passed, 33 skipped, 4 warnings in 493.47s (0:08:13)`, `rc=0`. The 4 warnings are the inherited Pillow I-mode PNG deprecation at `model_image.py:122`. While it ran, one comment in `_reduce` was reworded; no code changed.
- Lint: `ruff check` and `ruff format --check` are `not_run`: `<venv python> -m ruff --version` gives `No module named ruff`, and installs are not allowed. `eol_check.py` on the changed files: `eol_check: clean`. No line added in fix round 1 exceeds 121 characters.
- Test doubles, all labelled: `EmbeddingServer` (HTTP), the `read_source_file` recorder in `test_k1_config.py`, Pillow's `MAX_IMAGE_PIXELS` lowered by `monkeypatch` in the bomb test.
- `not_run`: Linux; live engines (no model calls allowed).

### Noticed, not done

- `contracts/README.md:70` does not say that `format`, `width` and `height` stay null for `undecodable` and the decompression-bomb case; one sentence there would document table 3.
- The reviewer's scratch under `.tmp/T-008/review/` (generated test roots, mutant copy) still awaits PM cleanup, as the review reports.

## Fix round 2

Retest 1 `.tmp/T-008/retest1/review.md`: F1 to F3 closed; FAIL on RT1 to RT3 (P2) and RT4 (P3). Q1 to Q5 stand. R-IDs: R-007, R-014, R-015, R-017.

### Step 0 (pre-approved by the PM; recorded, then continued)

- Unknowns: which Pillow formats report an oriented size at open (TIFF is known); which exception types file bytes can raise past Pillow's own error mapping; whether Arrow builds an empty table at the largest list size.
- Questions: none; the brief fixes the plan.
- Plan: (1) probes for the unknowns, then the class audit below; (2) regressions from the reviewer's probes, run on the round 1 code; (3) fixes RT1 to RT4; (4) 3 to 10 mutants; (5) K1 files, then the full suite once, through the test slot; (6) the audit updated.
- Finish: every audit row is refused at preflight, contained per item, or shown safe; regressions fail before and pass after.

### Configuration impact

- `embedding.dimension` gains an upper bound, `2147483647` (`2**31 - 1`): Arrow's fixed-size list length is a C `int` (`pyarrow/types.pxi:4959`, `def list_(value_type, int list_size=-1)`; measured: `2**31 - 1` builds an empty table, `2**31` raises `OverflowError`). It is `protocol-bound` (artifact representability), not fitted. Schema `maximum` and `config.EMBEDDING_DIMENSION_MAX`, checked in `_validate_embedding`, which runs at load and in the standalone preflight. Profiles unchanged (full `fit_from_gt`, dev 768).
- New `artifact-bound` constant `k1_embed.IMAGE_METADATA_RULE = 2`: the rule for stored image metadata enters the `k1_image` key, so rows written under the old TIFF rule are recomputed, not reused. Text keys are unchanged.
- Added after the audit: `vision.quality` gains a standalone preflight range, `model_image.QUALITY_MIN, QUALITY_MAX = 1, 100` (`protocol-bound`, mirrored from the schema, which a test keeps equal). The schema and profiles are unchanged.
- No other setting changes. RT1 and RT2 change behaviour on file bytes only.

### Probes behind the audit (scratch `.tmp/T-008/fix2/`, synthetic images only)

- `formats.py` saves a 9x7 image tagged with orientation 6 in every format Pillow 11.3.0 can write, then reopens it. Only TIFF reports a swapped size, `TIFF: opened_format=TIFF size=(7, 9) exif_orientation=6`. AVIF, JPEG, PNG and WEBP keep `size=(9, 7)` with the tag; BMP, DDS, GIF, IM, JPEG2000, PCX, PPM, QOI, SGI, SPIDER and TGA keep `(9, 7)` and have no tag. ICNS is a fixed-size icon, and ICO and PDF do not reopen. Source: `TiffImagePlugin.py:1463-1467` sets `self._size = ysize, xsize` for orientations 5 to 8.
- `fuzz.py` makes 1,500 seeded 1-to-4-byte mutations of each of PNG, JPEG, WEBP, TIFF, BigTIFF, GIF and BMP (10,500 files) and runs each through the real `k1_embed._decode`. On the round 1 code: `ESCAPED ('BigTIFF', 'OverflowError') 1 Python int too large to convert to C ssize_t`, `total escaped 1` (`fuzz-before.log`). After the fix: `total escaped 0`, with every file ending `ok`, `undecodable`, `exceeds_max_decode_pixels` or `encode_failed` (`fuzz-after.log`).
- `arrow.py`, pyarrow 26.0.1: `2147483647 ok 0 2147483647`, and `2147483648 OverflowError Python int too large to convert to C long`.

### Audit: outside numbers that reach a C-level or fixed-width conversion

| Source | Value | Conversion | Disposition | Fix or reason | Test |
|---|---|---|---|---|---|
| file | header fields at `Image.open` | plugin `_open` (`struct.unpack`, C decoders) | contained per item: `undecodable` | Pillow turns `IndexError`, `TypeError`, `KeyError`, `EOFError` and `struct.error` into `SyntaxError` (`ImageFile.py:153`), and `Image.open` turns those into `UnidentifiedImageError`, an `OSError` (`Image.py:3550`). An `OverflowError` here now becomes `OSError` too | fuzz; `test_undecodable_images_store_no_metadata` |
| file | stored width x height | Python int product against `max_decode_pixels` before `load()` | contained per item: `exceeds_max_decode_pixels` with exact metadata. Pillow's own bomb refusal at open (above 2x `MAX_IMAGE_PIXELS`) is `exceeds_max_decode_pixels` with null metadata | safe: Python ints | `test_refused_images_keep_the_stored_files_metadata`, `test_decompression_bomb_stores_no_metadata` |
| file | TIFF ImageWidth and ImageLength (tags 256, 257) | Pillow checks they are ints (`TiffImagePlugin.py:1459-1461`) | **RT2 fixed**: the stored size comes from these tags, not from Pillow's oriented `size` | `model_image._stored_size` | `test_tiff_stores_its_own_size_tags`, both metadata properties with tagged TIFF |
| file | EXIF block (`getexif`) | offsets to `BytesIO.seek` inside Pillow's IFD reader | contained per item: inside the new `OverflowError` block; truncation is only a Pillow warning | wrapped with `load()` | fuzz (PNG, JPEG, WEBP, TIFF tags mutated) |
| file | strip and tile offsets at `load()` | `BytesIO.seek` takes a C `ssize_t` (`ImageFile.py:352`) | **RT1 fixed**: `OverflowError` becomes `OSError`, so the image is `undecodable` with a durable no-vector record and the other images go on. Decoder errors were already `OSError` (`ImageFile.py:371-409`) | the `try` in `prepare_image_with_info` covers only the file-driven block (open, size, EXIF, load, orientation). Encoding with settings stays outside, so a settings fault is never turned into an image refusal | `test_file_offset_beyond_the_c_range_is_undecodable`, `test_file_offset_failure_is_checkpointed_and_isolated`, fuzz |
| file | orientation value | lookup in `exif_transpose` (`ImageOps.py:701-709`) | safe | an unknown value means no transpose | metadata properties |
| file | decoded size, flatten and resize | Python arithmetic, then C resize on sizes no larger than the decoded image | safe: bounded by `max_decode_pixels` | none | properties |
| profile | `embedding.dimension` | `pa.list_(..., int list_size)` C int (`types.pxi:4959`, via `tables.py:212`) | **RT3 fixed**: refused by name at preflight; schema `maximum` 2147483647 at load; `_validate_embedding` (at load and standalone) refuses anything outside 1 to `EMBEDDING_DIMENSION_MAX` | `config.EMBEDDING_DIMENSION_MAX` (protocol-bound) | `test_dimension_*` in `test_k1_config.py` |
| profile | `vision.quality` | Pillow encoder (C int) | refused at load (schema 1 to 100). **Found by this audit**: the standalone preflight checked no range; `quality.py` shows Pillow accepts `101` and `2**31`, and `2**64` raises `OverflowError: Python int too large to convert to C ssize_t` while encoding, after the sources were read | **fixed**: `check_vision_settings` refuses a non-int or a value outside `QUALITY_MIN`..`QUALITY_MAX` (1, 100, mirrored from the schema), before any read or request | `test_standalone_entry_points_refuse_a_quality_outside_the_schema_range` (0, 101, `2**64`, `True`, `80.0`), `test_quality_bounds_match_the_schema` |
| profile | `vision.max_image_side`, `image.max_decode_pixels` | Python comparisons; resize sizes never above the decoded image; `Image.MAX_IMAGE_PIXELS` is compared in Python | safe | none | existing T-009 and K1 tests |
| profile | `embedding.batch_size`, `max_concurrency` | Python slicing and `asyncio.Semaphore`, no C-level conversion | refused at load (schema minimum 1); outside this class | none (Noticed, not done) | existing |
| profile | `http.*` | `httpx.Timeout` floats, finite at load (`config.py:691-692`) | safe | none | existing |
| response | numbers in the embeddings response | `float()`, `struct.pack('<f')` | contained per item (Fix round 1, table 1; confirmed by retest 1) | none | Fix round 1 tests |

### Stored image metadata per supported format

`images.format`, `width` and `height` come from `_decode` (`k1_embed.py`, via `model_image.prepare_image_with_info`).

| Format (Pillow name) | Size source | Orientation | Property or test |
|---|---|---|---|
| PNG, JPEG, WEBP, AVIF | `image.size`, which these openers leave unoriented (probe) | EXIF tag; the prepared bytes are turned | `test_decode_records_the_stored_files_format_and_size` (untagged and orientations 1 to 8), refusal property (PNG) |
| TIFF | tags 256 and 257 (RT2) | TIFF's own tag 274; the prepared bytes are turned | same property, `test_tiff_stores_its_own_size_tags` (1 to 8), `test_tiff_metadata_persists_with_its_own_size` (entry point and resume), refusal property (TIFF, orientations 1 to 8) |
| BMP, GIF | `image.size` | none | first property; GIF is `encode_failed` with exact metadata |
| other installed openers | `image.size`, unswapped (probe) | none written | `formats.py` probe only; not in the knowledge-base extension example (`.jpg`, `.jpeg`, `.png`, `.webp`) |
| any undecodable file, Pillow bomb refusal | none: null `format`, `width`, `height` (RT4, `contracts/README.md:70`) | n/a | `test_undecodable_images_store_no_metadata`, `test_decompression_bomb_stores_no_metadata`, `test_file_offset_beyond_the_c_range_is_undecodable` |

Resume: `IMAGE_METADATA_RULE = 2` enters the `k1_image` key (`image_revisions`), so artifacts written with the round 1 TIFF metadata are recomputed. `test_metadata_rule_keys_image_artifacts_only` covers it; text keys are unchanged.

### Fixes

- `model_image.prepare_image_with_info`: `_stored_size` reads TIFF tags 256 and 257. The file-driven block (open, size check, EXIF, `load`, `exif_transpose`) turns an `OverflowError` into `OSError`; flattening, resizing and encoding now run after the file is closed, on the oriented copy. Prepared-bytes behaviour is unchanged (all T-009 image tests pass).
- `model_image.check_vision_settings` (K1's preflight only; T-009 `prepare_image` is unchanged): `vision.quality` must be an int from `QUALITY_MIN` to `QUALITY_MAX`.
- `k1_embed`: `IMAGE_METADATA_RULE` in the image artifact key.
- `config`: `EMBEDDING_DIMENSION_MAX` and the range check in `_validate_embedding`; schema `maximum: 2147483647`.
- `contracts/README.md:70` (RT4) and `docs/stages/K1-embed.md:13`: the stored size is before any orientation (a TIFF gives its own size tags); metadata stays null when Pillow cannot open or decode the file, and for its oversized-image refusal.
- Tests: the regressions below; `k0_handoff` gained an `extensions` argument; `bigtiff_with_strip_offset` builds the RT1 file.
- Dropped: catching `OverflowError` in `k1_embed._decode` around the whole preparation. It would also have turned a settings fault during encoding into an image refusal, which the brief forbids.

### Regressions: before and after

Command: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=line tests/test_k1_embed.py tests/test_k1_config.py -k "stored_files or refused_images or tiff or metadata_rule or file_offset or dimension"`, `PYTHONPATH=src`.

- Before (round 1 runtime, new tests): `21 failed, 18 passed, 159 deselected in 11.43s`. Failing: both metadata properties (falsifying example `fmt='TIFF', orientation=5` in each), `test_tiff_stores_its_own_size_tags[5..8]`, `test_tiff_metadata_persists_with_its_own_size`, `test_metadata_rule_keys_image_artifacts_only`, both file-offset tests, the schema-bound test, the loader refusal above the bound, all 7 standalone dimension cases, the nonempty-input case and the dimension property. Passing before, as expected: the accepted side (`2**31 - 1` loads and builds empty tables), TIFF orientations 1 to 4, and the existing metadata tests. One honest note: before the fix, the schema-bound test failed with a `KeyError` from a wrong schema path in the test itself. I corrected the path; mutant N6 shows the corrected test fails without the schema bound.
- After: `39 passed, 159 deselected in 6.79s`.
- `vision.quality`, added after the audit row above, same command with `tests/test_k1_config.py -k quality`: before `6 failed, 2 passed, 143 deselected in 1.74s` (the 2 are existing tests that also match `quality`); after `8 passed, 143 deselected in 2.17s`.

### Mutants (`.tmp/T-008/fix2/mutate.py`, scratch copies of `src/`, `tests/`, `contracts/`, `configs/`, through the test slot, `-x`)

All 9 killed (`.tmp/T-008/fix2/mutants.log`):

| ID | Mutation | Result |
|---|---|---|
| N1 | TIFF branch of `_stored_size` disabled | killed: `1 failed, 9 passed, 1 skipped` |
| N2 | the file block catches `ZeroDivisionError` instead of `OverflowError` | killed: `1 failed, 21 passed, 1 skipped` |
| N3 | `IMAGE_METADATA_RULE` dropped from the image key | killed: `1 failed, 20 passed, 1 skipped` |
| N4 | dimension bound one above Arrow's maximum | killed: `1 failed, 133 passed` |
| N5 | dimension type check removed | killed: `1 failed, 137 passed` |
| N6 | schema `maximum` removed | killed: `1 failed, 130 passed` |
| N7 | TIFF width and height tags swapped | killed: `1 failed, 9 passed, 1 skipped` |
| N8 | quality upper bound removed | killed: `1 failed, 141 passed` |
| N9 | quality type check removed | killed: `1 failed, 143 passed` |

### Results

- K1 files, T-009 image tests and K0 configuration before the quality fix: `tests/test_k1_embed.py tests/test_k1_text.py tests/test_k1_config.py tests/test_model_image.py tests/test_k0_config.py`: `444 passed, 1 skipped, 4 warnings in 56.17s`.
- K1 files on the final code: `215 passed, 1 skipped in 48.55s` (the skip is the symbolic-link test on this Windows account).
- Full pipeline suite, `... -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=line tests`, `PYTHONPATH=src`:
  - Run 1, all of round 2 except the `vision.quality` check (whose tests were added after collection): `2040 passed, 33 skipped, 4 warnings in 576.57s (0:09:36)`, rc 0.
  - Run 2, final code: `1 failed, 2045 passed, 33 skipped, 4 warnings in 635.17s (0:10:35)`, rc 1. The failure is `tests/test_storage_concurrency.py::test_threads_pin_while_publishing:107`, with `errors == ['...\kb: leases/s20261115T145103087583Z-2b2c01e2/7a97...lease: resolved outside project root']`. A reader thread pinned while the publisher's GC removed that lease. Reruns through the test slot: the whole file gave `3 passed in 12.61s`, then the single test passed 4 times (`1 passed, 2 deselected`). Neither round 1 nor round 2 changed `storage/` or that test; `store.py` differs from base only by `Checkpoints.reusable` from the original T-008 build. Reported as an intermittent T-006 finding (below), not fixed here.
  - The 4 warnings are the inherited Pillow I-mode PNG deprecation, now at `model_image.py:137`.
- Lint: `ruff check` and `ruff format --check` are `not_run`: `No module named ruff`, and installs are not allowed. `eol_check.py` on the changed files: `eol_check: clean`. `git diff --check -- src tests contracts docs`: no output, rc 0. No line added in fix round 2 exceeds 121 characters.
- Test doubles, all labelled: `EmbeddingServer` (HTTP), the `read_source_file` recorder, Pillow's `MAX_IMAGE_PIXELS` lowered by `monkeypatch` in the bomb test. The RT1 BigTIFF and every other image are synthetic; `database/` was never read.
- `not_run`: Linux; live engines (no model calls allowed).

### Noticed, not done

- Intermittent T-006 failure, 1 of 7 runs: `test_threads_pin_while_publishing` reported `lease ...: resolved outside project root` while GC ran concurrently with `store.pin()` (details under Results). The lease path check seems to treat a lease removed in between as a path escape; a storage owner should look at it.
- `model_transport.py:65` (T-009, shared): `backoff_initial_s * (2 ** attempt)` overflows a float after 1,024 attempts when `http.max_retries` is at least that large. This is outside K1 and `model_image.py`.
- A raw profile with `embedding.batch_size` or `max_concurrency` of 0 that skips the loader is not refused by K1's preflight; these values reach Python `range` and `asyncio.Semaphore`, not a C conversion, so they are outside this class.
- The reviewers' scratch under `.tmp/T-008/review/` and `.tmp/T-008/retest1/` still awaits PM cleanup.

## Fix round 3

Retest 2 `.tmp/T-008/retest2/review.md`: RT1 to RT4 closed; FAIL on R2F1 and R2F2 (P2); Gemini F-01 (P3) is R2F2's class. Q1 to Q5 stand. R-IDs: R-007, R-014, R-015, R-017, R-018.

### Step 0 (pre-approved by the PM; recorded, then continued)

- Unknowns: which exception types Pillow's openers and decoders raise on bad bytes (answered by the probe below, not by a list); whether the schema node of each read key can be found without a second hand-written list; whether the loader itself accepts an integral float for an integer key.
- Questions: none; the brief fixes the plan.
- Plan: (1) the two class audits; (2) regressions, run on the round 2 code; (3) one decode boundary in `model_image`, one shared read-set validation in `config` that the loader's `kb sync` and the standalone preflight both call; (4) 3 to 10 mutants; (5) K1 files, then the full suite once; (6) the audit updated.
- Finish: no probe input escapes `_decode`; every invalid numeric value of the read set is refused by name before any read or request on both paths; regressions fail before and pass after.

### Configuration impact

- No key, default or profile value changes. K1's read set is now checked against the configuration schema's own node for each key (types, minimum, maximum, exclusive bounds, enums), with exact integers (`type(value) is int`) and finite floats, plus the existing backoff order rule. The loader's `kb sync` and the standalone entry points call the same function, `config.validate_embedder_reads`.
- Retired: the two hand-written mirrors of schema bounds added in round 2, `model_image.QUALITY_MIN`/`QUALITY_MAX` and the `EMBEDDING_DIMENSION_MAX` range check in `_validate_embedding`; the schema is the one source. The schema maximum of `embedding.dimension` (2147483647, protocol-bound, Arrow) stays.
- Behaviour on file bytes: any exception the decoder raises on the bytes is `undecodable`; settings and encoding stay outside.

### Audit 1: calls that turn file bytes into pixels or metadata

Probe `.tmp/T-008/fix3/stages.py 83` (`stages.log`): every format Pillow 11.3.0 can write (26: AVIF, BLP, BMP, DDS, DIB, EPS, GIF, ICNS, ICO, IM, JPEG, JPEG2000, MPO, MSP, PALM, PCX, PDF, PNG, PPM, QOI, SGI, SPIDER, TGA, TIFF, WEBP, XBM), each mode of RGB, RGBA, L, 1, P that saves, tagged with EXIF orientation 6 where the format stores EXIF; every prefix up to 128 bytes, 60 seeded prefixes, 300 seeded 1-to-4-byte mutations and 6 malformed EXIF blocks. Each call runs alone; the first exception is counted under it. 45,651 inputs, no crash or hang.

| Call (in `prepare_image_with_info`) | Turns bytes into | Exceptions measured (count) | Pillow's own mapping | Boundary |
|---|---|---|---|---|
| `Image.open` | header, mode, size, `format`, `info` | `UnidentifiedImageError` 16379, `OSError` 4433, `ValueError` 374 (EPS, IM, PNG, PPM, SGI, TIFF), `RuntimeError` 184 (AVIF "Failed to decode image"), `NotImplementedError` 4 (DDS), `DecompressionBombError` 153 | `ImageFile.__init__` turns `IndexError`, `TypeError`, `KeyError`, `EOFError`, `struct.error` into `SyntaxError` (`ImageFile.py:153`); `Image._open_core` tries the next opener on `SyntaxError`, `IndexError`, `TypeError`, `struct.error` and raises `UnidentifiedImageError` when none accepts (`Image.py:3550`, `:3580`), but re-raises any other exception unchanged (`Image.py:3553`) | inside; `DecompressionBombError` passes through (its own status) |
| `_stored_size` (TIFF tags 256, 257; else `size`) | stored grid | none | | inside |
| `getexif().get(274)` | orientation | `SyntaxError` 59 (WEBP "not a TIFF file") | | inside |
| `load()` | pixels | `OSError` 8240, `IndexError` 159 (QOI, R2F1), `ValueError` 140 (BLP, DIB, GIF, ICNS, IM, PPM, QOI, TGA, TIFF), `SyntaxError` 110 (AVIF, ICNS, PNG), `RuntimeError` 69 (AVIF), `ZeroDivisionError` 9 (DDS), `BLPFormatError` 2 (a `NotImplementedError`) | decoder errors become `OSError` (`ImageFile.py:371-409`), but plugin `load_read`/`load_prepare` and Python decoders (`QoiImagePlugin.py:63`) raise raw | inside |
| `ImageOps.exif_transpose` | oriented pixels | none | | inside |
| `convert('RGBA')` (flatten only) | pixels with the file's transparency | none | | inside: it reads the file's `transparency`, takes no setting |
| composite on the configured background, `resize(max_image_side)` | | none | | outside: they take settings |
| `save(format, quality)` | request bytes | `OSError` 6022 (mode not writable) | | outside: `OSError` is `encode_failed` (unchanged); anything else is a defect and fails the stage |

Round 2 caught `OSError`, `SyntaxError`, `EOFError`, `ValueError` and `OverflowError`; `IndexError` (QOI), `RuntimeError` (AVIF), `ZeroDivisionError` and `NotImplementedError` (DDS, BLP) escaped. The installed openers are not a closed set, so the boundary is structural: every call that reads the bytes sits inside it and nothing else does.

### Audit 2: numbers K1 reads

Schema: `contracts/schemas/enhancer_config.v0.schema.json`. Before: the loader validates the schema at load (jsonschema Draft 2020-12, which counts an integral float such as `2.0` as an integer, `jsonschema/_types.py:195-201`), plus finiteness of `http`/`probe` floats and the backoff order (`config.py:700-710`); the standalone preflight checked presence and sentinels only, plus `dimension` and `quality` by hand.

| Key | Schema domain | Cross-field | Consumer | Loader before | Standalone before | Both now (`validate_embedder_reads`) |
|---|---|---|---|---|---|---|
| `embedding.dimension` | integer 1..2147483647 | none | Arrow list size, length check, vLLM `dimensions` | schema (accepts `4.0`), hand check | hand check | schema node, exact int |
| `embedding.batch_size` | integer >= 1 | 1 for `vllm_chat_messages` (`_validate_embedding`) | `range` step, slices | schema (accepts `2.0`) | vLLM rule only: `-1` silently embedded nothing, `0` `ValueError`, `2.0` `TypeError` | schema node, exact int; cross-field unchanged |
| `vision.max_image_side` | integer >= 1 or `none` | none | resize comparison | schema | `_check_settings` | schema node, exact int (`_check_settings` kept for T-009) |
| `vision.quality` | integer 1..100 | none | Pillow encoder (C int) | schema | hand mirror | schema node, exact int; mirror retired |
| `vlm.backends.<b>.max_concurrency` | integer >= 1 | none | `BackendConcurrency`, window | schema | late generic `ValueError` | schema node, exact int |
| `vlm.backends.<b>.image.max_decode_pixels` | integer >= 1 | none | size check, Pillow backstop | schema | `_check_settings` | schema node, exact int |
| `http.connect/read/write/pool_timeout_s` | number > 0 | none | `httpx.Timeout` | schema + finite | none: `nan` accepted | schema node + finite |
| `http.max_retries` | integer >= 0 | none | retry `range` | schema | none: `-1` made the loop empty, then `AttributeError` | schema node, exact int |
| `http.backoff_initial_s`, `backoff_max_s` | number >= 0 | initial <= max | `asyncio.sleep` | schema + finite + order | none | schema node + finite + order (one helper) |
| `http.retry_statuses` | unique integers 100..599 | none | membership test | schema | none | schema node, exact int items |
| `embedding.query_text_limit`, `probe.*` | | | not read by K1 | | | unchanged (not in the read set) |

The non-numeric members of the read set (enums, strings, the `extra_body` booleans, `base_urls`) are checked by the same schema nodes, so one rule covers the whole read set.

### Fixes

- **One decode boundary** (`model_image.py`): `_decoding_file_bytes()` maps any `Exception` from the code inside it to `UndecodableImage` (an `OSError`), except `DecompressionBombError` (own status) and `MemoryError`; `KeyboardInterrupt` and `SystemExit` are `BaseException` and never caught. `prepare_image_with_info` uses it in three blocks, which by structure hold only: `Image.open(stream)`; `_stored_size(source)` and `source.format`; `getexif().get(274)`, `load()`, `exif_transpose`, the transparency test and `convert('RGBA')`. Outside it: `_check_settings`, `BytesIO(data)`, the `max_decode_pixels` refusal, the alpha policy, the composite on the configured background, resize, `save` (its `OSError` stays `encode_failed`). `k1_embed._decode` catches `ImageRejected`, `DecompressionBombError` and `UndecodableImage` only; round 2's `(OSError, SyntaxError, EOFError, ValueError)` and the `OverflowError` block are gone, so an encoder or setting `ValueError` now fails the stage (it used to be stored as `undecodable`; `test_the_boundary_holds_no_setting_or_encoding_fault` shows it). T-009's `model_client` already turns any preparation exception into `IMAGE_INVALID`, so its behaviour is unchanged.
- **One numeric validation** (`config.py`): `validate_embedder_reads(config, path, sources, project_root=None)` does presence and sentinels (as before), then validates every read key's value against its node in `enhancer_config.v0` (`_schema_at` walks `properties`, `additionalProperties` and object branches of `anyOf`/`oneOf`, so there is no second list of bounds), through the loader's own `_validate` with `_EXACT_INTEGERS`, then `_require_finite` on every float, then `_check_backoff_order`. `embedder_settings` and the loader's `kb sync` branch both call it; the loader's per-backend backoff check uses the same helper.
- Retired mirrors: `model_image.QUALITY_MIN`/`QUALITY_MAX` and the quality check in `check_vision_settings`; `config.EMBEDDING_DIMENSION_MAX` and the dimension check in `_validate_embedding`. The schema keeps both bounds.
- Docs: `docs/stages/K1-embed.md` states the decode rule and the shared preflight.
- Tests changed for these reasons: `test_dimension_bound_is_arrows_list_size_in_schema_and_code` becomes `..._in_the_schema` (no code constant left); `test_quality_bounds_match_the_schema` is removed (no mirror left); the quality refusal now matches `vision\.quality: ` (the schema message names the key; it used to say `vision: quality must be ...`).
- Dropped: a hand-written table of numeric bounds in K1 (the brief forbids a second list), and applying `_EXACT_INTEGERS` to the whole loader schema (it would change every other command's integer keys; see Noticed, not done).

### Regressions: before and after

Command: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=line tests/test_k1_embed.py tests/test_k1_config.py -k "sweep_covers or truncated_qoi or cut_or_damaged or malformed_exif or decode_property or boundary_holds or pass_through_the_boundary or invalid_number_by_name or numeric_override or schema_domain or retry_statuses or backoff_order or negative_batch"`, `PYTHONPATH=src`.

- Before (round 2 runtime, new tests; `.tmp/T-008/fix3/before.log`): `136 failed, 154 passed, 202 deselected, 11 warnings in 21.54s`. Failing: `test_truncated_qoi_is_undecodable[13, 14, 18]`, `test_truncated_qoi_is_checkpointed_and_isolated`, the format sweep (first escape `('AVIF', 'RGB', ..., 'RuntimeError')`), `test_the_boundary_holds_no_setting_or_encoding_fault` (an encoder `ValueError` was stored as `undecodable`), 112 standalone invalid-number cases, 6 loader cases (`2.0` for every integer key except `dimension`: the loader accepted integral floats), 8 retry-status cases, the backoff order, both negative-batch entry points and the domain property. Passing before, as expected: the overrides, the malformed-EXIF cases, `MemoryError`/`KeyboardInterrupt` pass-through and the loader's refusals that the schema already made. One honest note: my first version of the boundary test patched `Image.Image.save` before building its fixture image, so it passed vacuously on round 2; I moved the fixture before the patch and it now fails before and passes after.
- After: included in the K1 run below, all passing.
- Reviewer probes `.tmp/T-008/retest2/test_retest2.py` on the fix (`retest2-probes.log`): `2 failed, 16 passed`. All 15-format truncation cases are contained (`CLASS formats=15 truncation_and_malformed_exif=1045 escaped=0`), the QOI entry-point probe passes, and every `NUMBER` probe is now a named refusal with `requests=0`, for example `embedding.batch_size: -1 is less than the minimum of 1`, `vlm.backends.embedder.http.read_timeout_s: expected a finite number`. The 2 failures are `test_negative_batch_checkpoint[image, text]`, which end in an unconditional `assert False` after calling `embedder_settings` on `batch_size: -1`; that call now raises the named refusal.
- Probes: `after.py 83` (audit 1 inputs through the real `_decode`, flatten policy): `inputs 45651 statuses {'encode_failed': 3829, 'exceeds_max_decode_pixels': 2219, 'ok': 10524, 'undecodable': 29079}`, `total escaped 0`. Round 2's seed `fuzz_seed.py tests 1500`: `total escaped 0`.

### Mutants (`.tmp/T-008/fix3/mutate.py`, scratch copies of `src/`, `tests/`, `contracts/`, `configs/`, through the test slot, `-x`, 900 s timeout each)

All 9 killed (`.tmp/T-008/fix3/mutants.log`):

| ID | Mutation | Result |
|---|---|---|
| P1 (brief) | the boundary narrowed back to round 2's type list `(OSError, SyntaxError, EOFError, ValueError, OverflowError)` | killed: `1 failed, 53 passed, 1 skipped` |
| P2 (brief) | the standalone path skips the shared check (round 2's sentinel-only loop) | killed: `1 failed, 133 passed` |
| P3 | the boundary also swallows `DecompressionBombError` | killed: `1 failed, 25 passed, 1 skipped` |
| P4 | the boundary also swallows `MemoryError` | killed: `1 failed, 66 passed, 1 skipped` |
| P5 | `_decode` again catches `ValueError` (encoder faults stored as `undecodable`) | killed: `1 failed, 65 passed, 1 skipped` |
| P6 | jsonschema's default integer check (accepts `2.0`) | killed: `1 failed, 138 passed` |
| P7 | finiteness check removed | killed: `1 failed, 215 passed` |
| P8 | backoff order skipped in the shared check | killed: `1 failed, 418 passed` |
| P9 | the loader's `kb sync` skips the shared check | killed: `1 failed, 276 passed` |

Not mutated: moving `_stored_size` or `convert('RGBA')` out of the boundary. No probe input raised in either call (audit 1), so such a mutant would survive; they sit inside because they read file data.

### Results

- K1 files with the T-009 image and configuration tests, `tests/test_k1_embed.py tests/test_k1_text.py tests/test_k1_config.py tests/test_model_image.py tests/test_k0_config.py tests/test_model_config.py` (`k1.log`): `860 passed, 1 skipped, 15 warnings in 86.21s`. The skip is the symbolic-link test on this Windows account. 11 of the warnings came from the malformed-EXIF test's own encoder calls; I then wrapped those calls in `catch_warnings`, before the full run.
- Full pipeline suite, once, final code: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=short tests`, `PYTHONPATH=src` (`full.log`): `2333 passed, 33 skipped, 4 warnings in 517.97s (0:08:37)`, rc 0. The 4 warnings are the inherited Pillow I-mode PNG deprecation (`model_image.py:158`). The intermittent T-006 test passed in this run.
- Lint: `ruff check` and `ruff format --check` are `not_run`: `<venv python> -m ruff --version` gives `No module named ruff`, and installs are not allowed. `eol_check.py` on the changed files: `eol_check: clean`. `git diff --check -- src tests configs contracts docs`: no output, rc 0. No line added in this round is longer than 121 characters.
- Test doubles, all labelled: `EmbeddingServer` (HTTP), the `read_source_file` recorder, `Image.Image.save` raising a labelled `ValueError`, `ImageFile.ImageFile.load` raising a labelled `MemoryError`/`KeyboardInterrupt`. Every image is synthetic. `database/` was not read.
- `not_run`: Linux; live engines (no model calls allowed).

### Noticed, not done

- The loader's schema check (jsonschema Draft 2020-12) accepts an integral float such as `2.0` for every integer key outside K1's read set too (`jsonschema/_types.py:195-201`), for example `vlm.roles.*.max_tokens` and `probe.max_requests`. I applied exact integers to K1's read set only.
- `vision.format` is checked as a string only on K1's path. An encoder name Pillow does not have would fail at the first `save`, after source reads; the loader checks `Image.SAVE` only for role image formats.
- The HTTP timeouts have no schema maximum. An integer above the float range (e.g. `10**400`) passes both paths and would fail inside httpx/asyncio when used. I did not add a cap: the reviewer asked for none without a demonstrated limit, and I did not measure this one.
- The T-009 backoff overflow (`model_transport.py:65`) and the intermittent T-006 lease race are unchanged; both were reported in round 2.
- The `.tmp/T-008/review/`, `retest1/` and `retest2/` reviewer scratch still awaits PM cleanup.

## Fix round 4

Retest 3 `.tmp/T-008/retest3/review.md`: R2F1 and R2F2 closed; FAIL on R3F1 and R3F2 (P2), one class: a K1 setting checked for its schema type but not for the domain its consumer needs. Gemini passed round 3. Q1 to Q5 stand. R-IDs: R-007, R-015, R-017, R-018.

### Step 0 (pre-approved by the PM; recorded, then continued)

- Unknowns: the exact value range the installed timer primitives accept (answered by the probe below, not by a cap); whether any other K1 read value reaches an API with a narrower domain than its schema; every place a configured value is replaced by a fallback.
- Questions: none; the brief fixes the plan. Recall `T-008 encoder timer` found no other ruling.
- Plan: (1) class audit; (2) regressions from the retest 3 probes, run on a round 3 snapshot (`.tmp/T-008/fix4/round3/`); (3) one encoder check and one timer check, both in `config.validate_embedder_reads` (loader `kb sync` and standalone), the fallback removed; (4) 3 to 10 mutants; (5) K1 files, then the full suite once; (6) audit updated.
- Finish: an empty or unavailable `vision.format` and a timer that does not convert to a finite float are refused by name before any read or request on both paths; regressions fail before and pass after.

### Configuration impact

- No key, default or profile value changes. Two domain rules join the shared read-set check `config.validate_embedder_reads` (both paths): `vision.format` must name an encoder in Pillow's initialized registry (`config.installed_encoder`, the same lookup `Image.save` makes); every read key whose schema node is a `number` (the six HTTP timers and backoffs) must convert to a finite float.
- Fallback removed: `model_image.prepare_image_with_info` used `output_format or original_format`, so a configured `''` became the source format. Now only `output_format=None` (a caller that configures no format, the T-009 default path) keeps the source format; a configured value is checked by `_check_settings` and used as given.

### Probe: the timer primitives' real domain

`.tmp/T-008/fix4/timer_probe.py` (`timer_probe.log`, no network) calls the installed `anyio.fail_after` (httpcore's read, write, connect and pool deadline, `httpcore/_backends/anyio.py:33`; `deadline = current_time() + delay`, `anyio/_core/_tasks.py:177`) and `loop.call_later` (what `asyncio.sleep` does with the backoff, `self.call_at(self.time() + delay, ...)`, `asyncio/base_events.py:765`):

```text
10**300 (int)                                 float()=1e+300                                 anyio.fail_after: ok deadline=1e+300
float max                                     float()=1.7976931348623157e+308                anyio.fail_after: ok deadline=1.7976931348623157e+308
2**1024 - 2**970 - 1 (rounds to float max)    float()=1.7976931348623157e+308                loop.call_later: ok deadline=1.7976931348623157e+308
first int that rounds to inf: 2**1024 - 2**970 float()=OverflowError: int too large to convert to float anyio.fail_after: OverflowError: int too large to convert to float
10**400                                       float()=OverflowError: int too large to convert to float loop.call_later: OverflowError: int too large to convert to float
```

Both primitives accept exactly the values `float(value)` converts to a finite float, and fail on exactly the others; that is the rule, not a deployment cap.

### Audit: every setting K1 reads, its consumer's domain, and the gap

Both paths run `validate_embedder_reads` since round 3 (schema node with exact integers, finite floats, backoff order); the standalone path adds `_validate_embedding` and `check_vision_settings`, which the loader runs as `_validate_embedding` at load. "Both" below means both paths.

| Setting | Schema domain | Domain the consumer needs (API reached) | Checked today (both paths unless noted) | Gap |
|---|---|---|---|---|
| `embedding.chosen` | string | a listed candidate id; enters the revision digest only | schema; `_validate_embedding` membership | none (`''` passes only if a candidate's id is `''`; no API consumes it) |
| `embedding.candidates` | array of `{id, note}` | membership list | schema | none |
| `embedding.revision` | string | equal to `model.revision` (minLength 1) | schema; `_validate_embedding` equality | none (`''` differs from every revision) |
| `embedding.backend` | string, minLength 1 | a configured local `openai_compatible` backend with `endpoints.embeddings` | schema; `_validate_embedding` | none |
| `embedding.request.shape` | enum | engine's documented layout | schema; engine pairing | none |
| `embedding.request.image_instruction` | string | request text, sent verbatim; `none` sends none | schema | none (`''` is sent as given, nothing substituted) |
| `embedding.request.extra_body` | object of two booleans | vLLM body fields | schema; empty for llama | none (an absent member is not sent; K1 substitutes nothing) |
| `embedding.request.text_template` | string, minLength 1 | exactly one `{text}` | schema; `_validate_embedding` | none |
| `embedding.dimension` | integer 1..2**31-1 | Arrow C-int list size, vLLM `dimensions` | schema, exact int | none |
| `embedding.dimension_mode` | enum | engine pairing | schema; `_validate_embedding` | none |
| `embedding.batch_size` | integer >= 1 | Python `range` step and slices (arbitrary precision) | schema, exact int; 1 for vLLM | none: `10**400` embeds (test below) |
| `vision.max_image_side` | integer >= 1 or `none` | Python comparison; the resize size is `< width` | schema, exact int; `_check_settings` | none: `10**400` embeds |
| `vision.format` | **string** | **a name in Pillow's encoder registry, `SAVE[name.upper()]` after `init()` (`PIL/Image.py:2564-2573`)**; the data URI subtype | schema (string); the loader's encoder check covers role image formats only (`config.py:570-575`) | **R3F1: `''` fell back to the source format (`model_image.py:152`) and sent `data:image/;base64`; an unknown name raised `KeyError` at the first `save`, after the source read** |
| `vision.quality` | integer 1..100 | Pillow encoder C int | schema, exact int | none |
| `vision.alpha_policy` | oneOf refuse / flatten `#RRGGBB` | composite colour | schema; `_check_settings` | none |
| `vision.transport` | const `data_uri` | protocol | schema | none |
| `<b>.provider`, `engine.*`, `model.*` | enums, strings minLength 1 | local backend, identity digest, body `model` | schema; `_validate_embedding`; `resolve_backend_engine` | none |
| `<b>.base_urls`, `endpoints.embeddings` | array minItems 1, string minLength 1 | `endpoint_url` (loopback or HTTPS, relative route); only `[0]` is used | schema; `embedder_settings` and loader `endpoint_url` | none (`[]` gives `''`, which `endpoint_url` refuses: a refusal, not a fallback) |
| `<b>.http.{connect,read,write,pool}_timeout_s` | **number > 0** | **`httpx.Timeout` -> httpcore -> `anyio.fail_after`: a finite float (probe)** | schema; finite floats only (`_require_finite` skips ints) | **R3F2: an integer >= 2**1024 - 2**970 passes and overflows in AnyIO after the source read** |
| `<b>.http.backoff_initial_s`, `backoff_max_s` | **number >= 0** | **`asyncio.sleep` -> `loop.call_later`: a finite float (probe)** | schema; finite floats; order | **same class (found here): `10**400` passes and overflows at the first retry** |
| `<b>.http.max_retries` | integer >= 0 | Python `range` | schema, exact int | none in its own domain: `10**400` embeds. The product `backoff_initial_s * 2**attempt` (`model_transport.py:65`) overflows after about 1024 failed attempts with a float initial value: transport arithmetic of two values, T-009 (Noticed) |
| `<b>.http.retry_statuses` | unique integers 100..599 | membership | schema, exact int items | none |
| `<b>.max_concurrency` | integer >= 1 | `asyncio.Semaphore` (Python int), window product | schema, exact int; `BackendConcurrency` | none: `10**400` embeds |
| `<b>.image.max_decode_pixels` | integer >= 1 | Python comparisons and `Image.MAX_IMAGE_PIXELS` (`PIL/Image.py:3439-3451`, Python ints only) | schema, exact int; `_check_settings` | none: `10**400` embeds |

Fallbacks searched in `k1_embed.py`, `model_image.py` and K1's part of `config.py`: `output_format or original_format` was the only configured value replaced by another value (fixed). Strings: unknown enum values, booleans and wrong types are refused by the schema nodes (round 3 tests); integral floats and booleans for integer keys by `_EXACT_INTEGERS`; NaN and infinities by `_require_finite`.

### Fixes

- **Encoder (R3F1):** `config.installed_encoder(name)` runs `Image.init()` and tests `name.upper() in Image.SAVE`, the lookup `Image.save` makes; the role image check uses it too (same behaviour). `validate_embedder_reads` refuses `vision.format` by name when it names no encoder (`vision.format: no installed Pillow encoder for ''`), so `kb sync` and both standalone entry points refuse before any read or request. `model_image._check_settings` refuses a configured `output_format` the same way (a `ValueError`, never an image outcome), and `prepare_image_with_info` keeps the source format only for `output_format=None`. The data URI keeps the configured name, which is now always the encoder used.
- **Timers (R3F2):** `config._require_float_seconds` refuses an integer that `float()` cannot convert (`<key>: the number does not convert to a finite float, which the timer clocks require`). Which keys it covers comes from the schema, not a list: every read key whose node admits `number` (`_schema_types`), which for K1 is exactly the six HTTP timers and backoffs (`test_the_float_rule_covers_exactly_the_schema_numbers_k1_reads`). `validate_embedder_reads` applies it on both paths; the loader's per-backend loop, which already applies the finite rule to every backend's `http` and `probe` numbers, applies the same function before its backoff order check, so `backoff_initial_s: 10**400` is refused under its own key rather than as an order fault. Exact integer counters are unaffected: they have no float consumer.
- **Rest of the class:** the audit found one more member, the two backoffs (same consumer class, `asyncio.sleep`), covered by the same rule; no other K1 key has a consumer narrower than its schema.
- **Docs:** `docs/stages/K1-embed.md` states both rules.
- **Scope note for the PM:** applying the timer rule in the loader's every-backend loop also refuses, at load, an unconvertible timer on a backend K1 does not use (T-009's). That loop already refused NaN for every backend, and such a value crashes T-009's transport the same way; the narrower alternative (skip the order check for unconvertible values) was dropped because it weakened an existing loader refusal.
- Dropped: a list of format words (forbidden; the registry decides); a timer cap (no measured limit other than float conversion); mapping the data URI subtype through `Image.MIME` (not asked; see Noticed, not done).

### Regressions: before and after

Command (both runs): `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=line -k "installed_encoder or never_replaced or non_default_encoder or float_rule_covers or does_not_convert or large_convertible or real_deadline or huge_integer_counters or invalid_number_by_name or schema_domain" tests/test_k1_config.py`; before with `PYTHONPATH=.tmp/T-008/fix4/round3/src;tests` (a copy of the round 3 `src/`, `contracts/`, `configs/` taken before any edit), after with `PYTHONPATH=src;tests`.

- Before (`.tmp/T-008/fix4/before.log`): `42 failed, 302 passed, 175 deselected in 42.53s`. Failing: `test_vision_format_without_an_installed_encoder_is_refused_before_any_read['', no_such_encoder]` (`DID NOT RAISE ConfigurationError` at load), `test_a_configured_format_is_never_replaced_by_the_source_format['', no_such_encoder]`, `test_timer_that_does_not_convert_to_a_float_is_refused_before_any_read` (6 timers x `10**400` and `2**1024 - 2**970`), the 12 new standalone and 12 loader cases of the invalid-number tests (the same values; standalone reached `store.writer`), the domain property, `test_the_float_rule_covers_exactly_the_schema_numbers_k1_reads` (no `_schema_types`). Passing before, as expected: the valid overrides (`png`/`webp` body, large convertible timers, the AnyIO deadline double, huge integer counters).
- After (`.tmp/T-008/fix4/after.log`): `344 passed, 175 deselected in 32.34s`.
- Tests added in `tests/test_k1_config.py`: the two retest 3 probes as regressions on a real one-image K0 handoff with role formats set to `jpeg` independently of `vision.format`, each on the loader and both standalone entry points with a labelled source-read recorder and the labelled HTTP double (`reads == []`, `requests == []`); `test_non_default_encoder_reaches_the_request_body_on_both_paths` (`png`, `webp`: header `data:image/<fmt>;base64`, decoded format); `test_large_convertible_timer_is_accepted_on_both_paths` (`10**300`, float max, `2**1024 - 2**970 - 1`); `test_large_timers_reach_the_real_deadline_primitive` (image and text, a labelled double entering the installed `anyio.fail_after` with each of the four request timeouts); `test_huge_integer_counters_embed_on_both_paths` (`10**400` for `batch_size`, `max_image_side`, `max_concurrency`, `max_retries`, `max_decode_pixels`); `test_the_encoder_registry_is_complete_in_a_fresh_process` (subprocess, `CREATE_NO_WINDOW`); `INVALID` gains `''`, `10**400`, `2**1024 - 2**970`, and the domain property gains huge integers around the float limit. The backoff pair in `numeric_profile` now opens to `2**1024 - 2**970 - 1`, the largest valid timer (it was `10**400`, now invalid).

### Mutants (`.tmp/T-008/fix4/mutate.py`, scratch copies of `src/`, `tests/`, `contracts/`, `configs/`, through the test slot, `-x`, 900 s timeout each, `tests/test_k1_config.py`)

`.tmp/T-008/fix4/mutants.log`, `mutants-q7.log`:

| ID | Mutation | Result |
|---|---|---|
| Q1 (brief) | encoder fallback restored as in round 3 (`output_format or original_format`, no format check in `model_image`) | killed: `1 failed, 482 passed` (`test_a_configured_format_is_never_replaced_by_the_source_format[]`) |
| Q1b | only the fallback line restored | survived (`519 passed`): equivalent, since `_check_settings` refuses every configured non-encoder before that line runs; `None` is the only value that reaches the source format |
| Q2 (brief) | the standalone path (no `project_root`) skips the timer check | killed: `1 failed, 231 passed` |
| Q3 | the loader's per-backend timer check removed | killed: `1 failed, 440 passed` (`backoff_initial_s=10**400` refused as an order fault) |
| Q4 | the shared timer rule removed on both paths | killed: `1 failed, 231 passed` |
| Q5 | the shared `vision.format` check removed | killed: `1 failed, 480 passed` |
| Q6 | encoder lookup without `.upper()` | killed: `1 failed` |
| Q7 | encoder lookup without `Image.init()` | first survived (`519 passed`: the test process had registered every plugin already); after adding the fresh-process test, killed: `1 failed, 484 passed` |
| Q8 | `_schema_types` ignores anyOf/oneOf | killed: `1 failed, 231 passed` |
| Q9 | conversion rule tests floats instead of ints | killed: `1 failed, 231 passed` |

### Results

- K1 files with the T-009 model and configuration tests, `tests/test_k1_embed.py tests/test_k1_text.py tests/test_k1_config.py tests/test_model_image.py tests/test_k0_config.py tests/test_model_config.py tests/test_model_client.py tests/test_model_transport.py tests/test_model_probe.py tests/test_loader.py tests/test_field_contracts.py`, `PYTHONPATH=src` (`.tmp/T-008/fix4/k1.log`): `1598 passed, 28 skipped, 4 warnings in 254.72s`. That run came before the fresh-process encoder test was added; the full run below includes it.
- Full pipeline suite, once, final code: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=short tests`, `PYTHONPATH=src` (`full.log`): `2431 passed, 33 skipped, 4 warnings in 478.47s (0:07:58)`, rc 0. The 4 warnings are the inherited Pillow I-mode PNG deprecation.
- Lint: `ruff check` and `ruff format --check` are `not_run`: `<venv python> -m ruff --version` gives `No module named ruff`; no install. `eol_check.py` on the changed files: `eol_check: clean`. `git diff --check -- src tests configs contracts docs`: no output, rc 0. No line added this round in `src/` or `tests/` is longer than 120 characters.
- Test doubles, all labelled: `EmbeddingServer`, the `read_source_file` recorder, the AnyIO deadline double (`labelled_deadline_double`, no socket). Every image is synthetic; `database/` not read; no network, model or GPU.
- `not_run`: Linux; live engines.

### Noticed, not done

- P3 for T-005/T-009 (retest 3): the loader accepts integral floats outside K1's read set, for example `vlm.roles.observer.max_tokens: 2.0` and `vlm.backends.embedder.probe.max_requests: 2.0` (jsonschema Draft 2020-12, `jsonschema/_types.py:195-201`). Not a K1 key; not changed.
- T-009: `model_transport.py:65` computes `backoff_initial_s * 2 ** attempt` before `min(..., backoff_max_s)`; with a float initial value the product overflows after about 1024 failed attempts (`max_retries` above that). Two valid values combine, so it is transport arithmetic, not a single-key domain; unchanged (reported since round 2).
- K1 and T-009 both build the data URI subtype from the configured name (`data:image/{fmt}`); for an encoder whose MIME type differs from its name (Pillow's `Image.MIME`, for example `JPEG2000` -> `image/jp2`) the header names no registered type. Not measured against an engine; a mapping through `Image.MIME` would change both callers, so it is a PM decision.
- The `.tmp/T-008/review/`, `retest1/`, `retest2/` and `retest3/` reviewer scratch still awaits PM cleanup.

## Fix round 5

Retest 4 `.tmp/T-008/retest4/review.md`: R3F1 and R3F2 closed; FAIL on R4F1 (P2, media type from the encoder name) and the inherited N1 (P2, retry backoff overflow, T-009 code on K1's paths). Gemini passed round 4. Last round allowed (decision 121). R-IDs: R-015, R-017, R-018.

### Step 0 (pre-approved by the PM; recorded, then continued)

- Unknowns: which installed encoders register no `image/` media type (answered by `.tmp/T-008/fix5/registry.log`); every builder that interpolates a format name into a media type; every request/retry expression over configured values that can overflow before its cap.
- Questions: none; the brief fixes the plan. Recall `media type MIME` and `backoff overflow`: no ruling.
- Plan: (1) class audit, two tables; (2) one shared `config.image_media_type`, used by K1, the T-009 client builder and both preflights; (3) overflow-free capped backoff in `ModelTransport.post`; (4) regressions run on the round 4 snapshot `.tmp/T-008/fix5/round4/` and on the fix; (5) mutants, K1 and T-009 files, full suite once, audit.
- Finish: JPEG2000 is sent as `image/jp2`; every installed encoder's request header equals `Image.MIME`; an encoder with no registered `image/` type is refused by name before any read or request; 1025 retries with zero backoffs end in the ordinary `HTTP_ERROR` checkpoint on both K1 paths.

### Configuration impact

- No key, default or profile value changes. `vision.format` and every role `image.format` gain one domain rule: the encoder must register an `image/` media type in Pillow's initialized registry (`Image.MIME`), checked by the loader (role formats, `kb sync`) and by K1's standalone preflight (`validate_embedder_reads`). Encoders refused newly: those printing `None` or `application/*` in the table below. The request header is the registered media type, not the configured name.
- `http.backoff_initial_s`, `backoff_max_s`, `max_retries`: same domain and same delays for every value that did not overflow (exact doubling); a delay that would exceed the cap is the cap, for every retry count.

### Audit 1: format or encoder name to media type (installed Pillow 11.3.0, `.tmp/T-008/fix5/registry.log`)

Sites in `src/` (grep `image/`, `data:`, `media_type`, `mime`): `k1_embed.py:232` (`Embedder._image_part`, data URI), `model_client.py:279` (`cloud_images` media type, consumed by `model_cloud.py:49` as Anthropic `media_type`) and `model_client.py:280` (data URI). `model_probe` sends the probe plans' own data URIs (no format name). All three built `image/<configured name>`.

| Encoder (`Image.SAVE`) | Registered (`Image.MIME`) | Sent before (configured lowercase name) | Sent after |
|---|---|---|---|
| AVIF | image/avif | image/avif | image/avif |
| BMP, DIB | image/bmp | image/bmp, **image/dib** | image/bmp |
| GIF | image/gif | image/gif | image/gif |
| ICNS | image/icns | image/icns | image/icns |
| ICO | image/x-icon | **image/ico** | image/x-icon |
| JPEG | image/jpeg | image/jpeg | image/jpeg |
| JPEG2000 | image/jp2 | **image/jpeg2000** (R4F1) | image/jp2 |
| MPO | image/mpo | image/mpo | image/mpo |
| PALM | image/palm | image/palm | image/palm |
| PCX | image/x-pcx | **image/pcx** | image/x-pcx |
| PNG | image/png | image/png | image/png |
| PPM | image/x-portable-anymap | **image/ppm** | image/x-portable-anymap |
| SGI | image/sgi | image/sgi | image/sgi |
| TGA | image/x-tga | **image/tga** | image/x-tga |
| TIFF | image/tiff | image/tiff | image/tiff |
| WEBP | image/webp | image/webp | image/webp |
| XBM | image/xbm | image/xbm | image/xbm |
| EPS | application/postscript | **image/eps** | refused at preflight (not an `image/` type) |
| PDF | application/pdf | **image/pdf** | refused at preflight |
| BLP, BUFR, DDS, GRIB, HDF5, IM, MSP, QOI, SPIDER, WMF | none | **image/<name>** | refused at preflight (no registered type) |

### Audit 2: arithmetic over configured values in the request and retry path

| Expression | Operands (configured) | Overflow or lost cap before applied | Status |
|---|---|---|---|
| `model_transport.py:65` `min(backoff_initial_s * (2 ** attempt), backoff_max_s)` | backoffs (number >= 0, finite float rule), `max_retries` (int >= 0) | yes: a float initial times the int `2**1024` raises `OverflowError` (int to float) at attempt 1024 whatever the cap; an int initial forms an unbounded int (memory/time, no error) | **N1, fixed**: running delay doubled and capped each attempt |
| `model_transport.py:49` `range(max_retries + 1)`, `attempt == max_retries` | `max_retries` | no: Python ints | none |
| `httpx.Timeout(...)` -> AnyIO `current_time() + delay` | four timeouts | not after round 4 (float rule) | none |
| `asyncio.sleep(delay)` -> `self.time() + delay` | delay <= `backoff_max_s`, which converts to a finite float (round 4) | `time + float max` is `inf` in float arithmetic, no error (probe `timers.log`, retest 4) | none |
| `model_probe.py:33` `asyncio.wait_for(timeout=timeout_s)` | probe timeout | not after round 4 (loader float rule for every backend's `probe` numbers) | none |
| `model_resources.py:35,54` budget `-= 1`, `+= 1` | `call_budget` (int) | no: Python ints | none |
| `k1_embed.py:297` `batch_size * max_concurrency` (window) | ints | no: Python ints (round 4, `10**400` embeds) | none |
| `model_image.py:131,152` `width * height`, `width * max_side // limit` | `max_decode_pixels`, `max_side` ints | no: Python ints | none |
| `model_client.py:355` `time.monotonic() - started` | none configured | no | none |

### Fixes

- **Media type (R4F1):** `config.image_media_type(name)` returns `Image.MIME[name.upper()]` after `installed_encoder(name)` (which runs `Image.init()`), or `None` when the registered type is missing or not `image/`. Used by `Embedder` (K1 data URI), `RoleClient._call` (data URI and the cloud `media_type`), the loader's role image check and `validate_embedder_reads` (both K1 paths). Refusal: `vision.format: the Pillow encoder 'pdf' registers no image/ media type`. No list of format words or media types: the installed registry decides.
- **Retry arithmetic (N1):** `ModelTransport.post` keeps the running delay, starting at `min(backoff_initial_s, backoff_max_s)` and updated `min(delay * 2, backoff_max_s)` after each sleep. Doubling is exact, so every delay equals the old `min(initial * 2**attempt, cap)` wherever that did not overflow; the factor `2**attempt` is never formed. Retry counts are not capped and the bad-image catch is unchanged.
- A role format without a media type reaching `RoleClient._call` (a profile that skipped the loader) raises `ConfigurationError`, which `RoleClient.call` reports as `CONFIGURATION_INVALID`, before any request. `docs/stages/K1-embed.md` states the rule.
- Dropped: taking the media type from the encoded bytes (`Image.open(prepared).format`), which would label single-frame MPO output `image/jpeg` (see Noticed); it adds a per-image decode and failure path and is not what the brief approved.

### Regressions: before and after

Command (both runs): `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> -k "registered_media_type or every_installed_encoder or without_a_registered or long_retry or capped_backoff or capped_doubling or jpeg2000" tests/test_k1_config.py tests/test_model_client.py tests/test_model_transport.py`; before with `PYTHONPATH=.tmp/T-008/fix5/round4/src;tests` (round 4 `src/`, `contracts/`, `configs/` copied before any edit), after with `PYTHONPATH=src;tests`.

- Before (`.tmp/T-008/fix5/before.log`): `58 failed, 39 passed, 573 deselected in 59.04s`. Failing: the JPEG2000 request on both K1 paths (`image/jpeg2000`) and on the T-009 builder; the 6 encoders whose name differs from their registered type (DIB, ICO, JPEG2000, PCX, PPM, TGA) on K1 and T-009; the 12 encoders without an `image/` type (BLP, BUFR, DDS, EPS, GRIB, HDF5, IM, MSP, PDF, QOI, SPIDER, WMF) not refused on K1 (loader and standalone) and T-009, and the role format at load; the long retry sequence with float zero or tiny backoffs on both K1 paths (4, `OverflowError`) and in the transport (2 real-sleep, 12 past the exponent limit, the property). Passing before, as expected: the 12 encoders whose name equals their type, and integer zero backoffs (no float conversion).
- After (`.tmp/T-008/fix5/after.log`): `97 passed, 573 deselected in 25.06s`. Added afterwards for mutant M4: `test_cloud_image_states_the_registered_media_type` (before: `1 failed`; after: passes, in the client run `33 passed`).
- Tests added: `tests/test_k1_config.py` (`test_registered_media_type_reaches_the_request_body_on_both_paths`, `test_every_installed_encoder_sends_its_registered_media_type` x18, `test_an_encoder_without_a_registered_image_media_type_is_refused_before_any_read` x12 with the labelled read recorder and HTTP double, `test_a_role_format_without_a_registered_image_media_type_is_refused_at_load`, `test_a_long_retry_sequence_ends_in_the_failed_item_checkpoint` image and text x (0.0, 0.0), (0, 0), (5e-324, 1e-9) with real `asyncio.sleep`, 1026 requests and a `failed` checkpoint); `tests/test_model_client.py` (JPEG2000 real encode, x18 header with a labelled `prepare_image` double, x12 refusal, cloud `media_type`); `tests/test_model_transport.py` (1025 retries with real sleep; 1024, 1025, 1100 retries over zero, subnormal, float max, int up to the largest convertible timer and capped pairs against an exact `Fraction` oracle; a Hypothesis property over retry counts 0 to 1100 and any valid ordered pair). The per-encoder oracles iterate Pillow's initialized registry; no format or media type list is written.

### Mutants (`.tmp/T-008/fix5/mutate.py`, scratch copies of `src/`, `tests/`, `contracts/`, `configs/`, through the test slot, `-x`, 900 s timeout each, the transport, client and K1 config tests)

`.tmp/T-008/fix5/mutants.log`, `mutants-m4.log`:

| ID | Mutation | Result |
|---|---|---|
| M1 (brief) | K1 data URI from the encoder name again | killed: `1 failed, 632 passed` (`test_registered_media_type_reaches_the_request_body_on_both_paths`) |
| M2 (brief) | the cap applied after the power again | killed: `1 failed, 7 passed` (real-sleep 1025 retries `[0.0-0.0]`) |
| M3 | T-009 data URI from the encoder name | killed: `1 failed, 74 passed` |
| M4 | T-009 cloud `media_type` from the encoder name | first survived (`670 passed`: no test sent a cloud image); after adding `test_cloud_image_states_the_registered_media_type`, killed: `1 failed, 109 passed` |
| M5 | any registered type accepted, not only `image/` | killed (`[EPS]`) |
| M6 | K1 shared preflight media-type check removed | killed (`[BLP]`) |
| M7 | loader role-format media-type check removed | killed |
| M8 | running delay doubled without the cap | killed (`test_configured_timeouts_and_bounded_backoff`) |
| M9 | running delay quadrupled | killed (`[1024-1.0-1.797e+308]`) |
| M10 | registry lookup without upper-casing | killed |

### Results

- K1 files with the T-009 model and configuration tests (`test_k1_embed`, `test_k1_text`, `test_k1_config`, `test_model_image`, `test_k0_config`, `test_model_config`, `test_model_client`, `test_model_transport`, `test_model_probe`, `test_model_composition`, `test_loader`, `test_field_contracts`), `PYTHONPATH=src` (`.tmp/T-008/fix5/k1.log`): `1750 passed, 28 skipped, 4 warnings in 312.90s`. That run came before the cloud media-type test and two test line wraps; the full run below includes them.
- Full pipeline suite, once, final code: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=<scratch> --tb=short tests`, `PYTHONPATH=src` (`.tmp/T-008/fix5/full.log`): `2529 passed, 33 skipped, 4 warnings in 521.83s (0:08:41)`, rc 0. The 4 warnings are the inherited Pillow I-mode PNG deprecation.
- Scratch kept as cited evidence: `.tmp/T-008/fix5/` logs, `mutate.py`, `registry.py` and `encode_probe.py` (the audit probes); snapshot, mutant trees and pytest temp dirs removed.
- Lint: `ruff check` and `ruff format --check` are `not_run`: `<venv python> -m ruff --version` gives `No module named ruff`; no install. `eol_check.py` on the changed files: `eol_check: clean`. `git diff --check -- src tests docs`: no output, rc 0. Lines added in `src/` stay within 120 characters except `model_client.py` data URI line (124, the T-009 line it replaces was longer).
- Test doubles, all labelled: `EmbeddingServer`, the retry double (always 503), the `read_source_file` recorder, the recording `asyncio.sleep` double (transport tests with large delays only; K1 paths and the real-sleep transport test use real `asyncio.sleep`), the `prepare_image` double (T-009 per-encoder header test), the client transport doubles. Synthetic images only; `database/` not read; no network, model or GPU.
- `not_run`: Linux; live engines (whether a server accepts `image/jp2` or another registered type is not measured).

### Noticed, not done

- N2 (P3, T-005/T-009): the loader accepts integral floats for integer keys outside K1's read set (`config.py:308`, Draft 2020-12 `jsonschema/_types.py:195-204`); not changed.
- MPO single frame: Pillow's MPO encoder writes plain JPEG bytes (`PIL/MpoImagePlugin.py:37-38`) while registering `image/mpo` (`:202`); the header states the encoder's registered type as briefed, but the bytes reopen as `JPEG` (`.tmp/T-008/fix5/encode_probe.log`). Labelling from the encoded bytes would differ; PM decision.
- ICO with an image smaller than 16 px: the encoded bytes do not reopen (`UnidentifiedImageError`, `encode_probe.log`); PALM and XBM cannot encode RGB (`encode_failed`). Encoder mode or size support, not media type; T-009 image preparation.
- The `.tmp/T-008/review/`, `retest1/` to `retest4/` reviewer scratch still awaits PM cleanup.

## Completion report

### Configuration audit

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Which backend serves the embedder | `embedding.backend` | schema string or sentinel; `_validate_embedding`: the name must exist, provider `openai_compatible`, has `endpoints.embeddings`; `kb sync` and `embedder_settings` refuse a sentinel | full `deploy_required`; dev `embedder` | `test_override_reaches_the_k1_settings[embedding.backend]`, `test_backend_and_route_override_reach_the_request`; `test_invalid_value_is_refused_at_load[embedding.backend=*]` (unknown, cloud, missing, no route), `test_unresolved_sentinel_is_refused_by_kb_sync_and_by_k1[embedding.backend]` | configurable |
| Embeddings route | `vlm.backends.*.endpoints.embeddings`, with `base_urls[0]` | schema string, minLength 1, or sentinel; `endpoint_url` checks the route and base (`config.py` routes loop); fix round 1: `embedder_settings` also checks the pair, so a profile that skipped the loader is refused before any read (`test_standalone_entry_points_refuse_an_unusable_endpoint`: empty list, `ftp://`, `../escape`) | full: none, because no embedder backend is shipped; dev `v1/embeddings` | `...[vlm.backends.embedder.endpoints.embeddings]`, `test_backend_and_route_override_reach_the_request`; refusal `...endpoints.embeddings='../escape'`, `...endpoints={'health'}`, and the sentinel test | configurable |
| Chat route only for chatting backends | `vlm.backends.*.endpoints.chat` | schema needs `health` and one of `chat`/`embeddings`; a role or default bound to a backend without `chat` is refused | unchanged | existing role tests stay green; `test_role_and_default_backend_need_a_chat_route` | configurable |
| Embedder identity | `embedding.chosen`, `embedding.candidates`, `embedding.revision`, plus `vlm.backends.<backend>.model.id/revision` and `engine.name/version` | `chosen` must be a listed candidate; `revision` must equal the backend's `model.revision`; fix round 1: `candidates` and the `engine` leaves are in K1's read set, so a sentinel or missing value is refused by the standalone entry points before any read or request (F2) | full `fit_from_gt` / `pin_required`; dev `embeddinggemma-2` / `pin_required`; candidates listed in both | `test_embedder_change_reembeds_every_image`, key property `test_key_is_a_stable_function_of_content_and_settings` (model id, engine version); refusal `...embedding.revision='other'`, `...embedding.chosen='not-a-candidate'`, `test_standalone_entry_points_refuse_a_sentinel_in_every_read_key` and `..._missing_read_key` (every read key), `..._nested_sentinel[embedding.candidates.0.id]`, `test_candidates_sentinel_refuses_k1_after_a_command_that_does_not_read_it` | configurable |
| Request layout | `embedding.request.shape` | enum `openai_input_content`/`vllm_chat_messages` or sentinel; it must match the engine (`EMBEDDING_SHAPE_ENGINE`); fix round 1: the four `request` leaves are each in the read set, so a missing one is refused by name | `design_required` (full and dev) | `test_vllm_chat_messages_body`, `test_embeds_every_image_once_with_metadata_and_documented_body`, `...[embedding.request.shape]`; refusal `...shape='grpc'`, both engine mismatches, sentinel | configurable (the shape enum is protocol-bound to the engines' documentation) |
| Image instruction | `embedding.request.image_instruction` | schema string; `none` sends none | `design_required` | `test_instruction_override_reaches_llama_body`, `test_vllm_chat_messages_body`; refusal `...image_instruction=12`, sentinel | configurable |
| vLLM extra fields | `embedding.request.extra_body` | object of `continue_final_message`/`add_special_tokens` booleans, or sentinel; must be empty for `openai_input_content` | `design_required` | `test_vllm_chat_messages_body`, `...[embedding.request.extra_body]`; refusal unknown member, non-boolean, non-empty on llama, sentinel | configurable |
| Concept text template | `embedding.request.text_template` | schema string, minLength 1; exactly one `{text}` | `design_required` | `test_text_template_override_reaches_the_request_and_the_key`; refusal no placeholder, two placeholders, sentinel | configurable (`{text}` protocol-bound) |
| Vector length | `embedding.dimension` | integer 1 to 2147483647 or sentinel; schema `maximum` is Arrow's C `int` list size (RT3); fix round 3: applied on both paths by `validate_embedder_reads` (the schema node, exact int), and the round 2 code mirror `EMBEDDING_DIMENSION_MAX` is retired; a response of another length fails the item `DIMENSION_MISMATCH` | full `fit_from_gt`; dev 768 | `test_dimension_override_builds_the_wider_table`, `test_dimension_at_the_upper_bound_loads_and_builds_empty_tables`, `test_numeric_override_reaches_the_settings_on_both_paths[embedding.dimension]`; refusal `test_dimension_bound_is_arrows_list_size_in_the_schema`, `test_dimension_above_the_upper_bound_is_refused_by_name_at_load`, `test_standalone_entry_points_refuse_an_unrepresentable_dimension`, `test_unrepresentable_dimension_is_refused_before_any_request_on_nonempty_input`, `test_standalone_entry_points_refuse_an_invalid_number_by_name[embedding.dimension=*]`, `test_kb_sync_refuses_an_invalid_number_by_name[embedding.dimension=*]`, properties `test_dimension_is_accepted_exactly_inside_the_representable_range`, `test_standalone_preflight_accepts_exactly_the_schema_domain` | configurable value; the schema maximum is protocol-bound |
| Vision quality range | `vision.quality` (existing) | schema 1 to 100; fix round 3: both paths through `validate_embedder_reads`; the round 2 mirror `QUALITY_MIN`/`QUALITY_MAX` is retired | existing (`design_required` in the full example) | `test_numeric_override_reaches_the_settings_on_both_paths[vision.quality]`; refusal `test_standalone_entry_points_refuse_a_quality_outside_the_schema_range` (now matching `vision.quality: `), the two `..._invalid_number_by_name[vision.quality=*]` tests, the domain property | configurable value; bounds owned by the schema |
| How the length is reached | `embedding.dimension_mode` | enum `native`/`request_field`/`client_prefix_l2` or sentinel; `request_field` only on vLLM; fix round 1: `client_prefix_l2` scales by the largest magnitude before `math.hypot`, so every finite vector normalises (F1) | full `design_required`; dev `native` | `test_client_prefix_l2_keeps_leading_values_normalised`, `test_client_prefix_l2_normalises_finite_extremes`, `test_parse_client_prefix_l2_over_the_whole_numeric_range`, `test_vllm_chat_messages_body` (`dimensions`); refusal `request_field` on llama_cpp, `truncate`, sentinel | configurable |
| Items per request | `embedding.batch_size` | integer >= 1 or sentinel; 1 for `vllm_chat_messages`; fix round 3 (R2F2, F-01): exact int on both paths through `validate_embedder_reads`, so `-1`, `0`, `True`, `2.0` are refused by name before any read (the loader used to accept `2.0`) | full `deploy_required`; dev 8 | `test_batch_size_override` (5 images, batch 3: requests of 3 and 2), `..._on_both_paths[embedding.batch_size]`; refusal 0, `True`, 2 with vLLM, sentinel, the two `..._invalid_number_by_name[embedding.batch_size=*]` tests, `test_negative_batch_is_refused_before_any_read_or_checkpoint` (image and text), the domain property | configurable |
| Concurrent requests | `vlm.backends.<backend>.max_concurrency` (existing) | schema integer >= 1; fix round 3: both paths, exact int, refused by name (was a late generic `ValueError` standalone) | dev 1 | `..._on_both_paths[...max_concurrency]` (3); refusal the two `..._invalid_number_by_name[...max_concurrency=*]` tests, the domain property, sentinel and missing tests | configurable |
| Timeouts and retries | `vlm.backends.<backend>.http.*` (existing, 8 fields) | schema: timeouts number > 0, `max_retries` integer >= 0, backoffs number >= 0, `retry_statuses` unique integers 100 to 599; finite floats; backoff initial <= max. Fix round 1: each field in K1's read set. Fix round 3: the same rules on both paths through `validate_embedder_reads` (standalone used to accept `max_retries: -1` and `read_timeout_s: nan`); the backoff order is one helper, `_check_backoff_order`, which the loader also uses for every backend. Fix round 4 (R3F2): every schema `number` among them (the four timeouts and two backoffs) must convert to a finite float, the domain of the AnyIO deadline and asyncio timer they reach (measured, `timer_probe.log`); one function, `_require_float_seconds`, in the shared check and in the loader's every-backend loop before the order check; exact integer counters (`max_retries`) are unaffected. Fix round 5 (N1, T-009 transport): the backoff delay is the running delay doubled and capped each attempt, never `initial * 2**attempt`, so any valid `max_retries` with any valid ordered backoff pair finishes its attempts | existing | `..._on_both_paths[...http.*]` (2.5, 2, 0.5, 1.5), `test_retry_statuses_override_reaches_the_settings_on_both_paths`, `test_large_convertible_timer_is_accepted_on_both_paths` (`10**300`, float max, `2**1024 - 2**970 - 1`), `test_large_timers_reach_the_real_deadline_primitive`, `test_huge_integer_counters_embed_on_both_paths` (`max_retries` `10**400`); refusal the two `..._invalid_number_by_name[...http.*=*]` tests (including `nan`, `inf`, `-inf`, `''`, `10**400`, `2**1024 - 2**970`), `test_timer_that_does_not_convert_to_a_float_is_refused_before_any_read`, `test_retry_statuses_are_refused_by_name_on_both_paths` (8 values), `test_backoff_order_is_refused_by_name_on_both_paths`, the domain property, sentinel and missing tests. Fix round 5: `test_a_long_retry_sequence_ends_in_the_failed_item_checkpoint` (both K1 paths, 1025 retries), `test_a_long_retry_sequence_with_zero_or_tiny_backoff_finishes_with_real_sleep`, `test_capped_backoff_past_the_float_exponent_limit`, `test_backoff_delays_are_the_capped_doubling_for_any_valid_pair` | configurable |
| Image transport | `vision.transport` | `const data_uri` or sentinel | `data_uri` | there is no non-default value: `test_embeds_every_image_once...` asserts `data:image/jpeg;base64`; refusal `file_uri`, `url`, `''`, sentinel, and the updated `test_model_config` row | protocol-bound |
| Image preparation | `vision.max_image_side`, `format`, `quality`, `alpha_policy`; `vlm.backends.<backend>.image.max_decode_pixels` | existing schema, plus `check_vision_settings` before any image; fix round 3: every one of these keys is also checked against its schema node on both paths (`validate_embedder_reads`; `max_image_side` 3 and `max_decode_pixels` 5000 overrides, invalid-number refusals and the domain property). Fix round 4 (R3F1): `vision.format` must name an encoder in Pillow's initialized registry (`config.installed_encoder`, both paths, before any read); a configured value is used as given, with no fallback to the source format (`model_image._check_settings`, `prepare_image_with_info`). Fix round 5 (R4F1): the encoder must register an `image/` media type (`config.image_media_type`, loader and both standalone paths, and every role `image.format` at load); the data URI states that type | existing | fix round 5: `test_registered_media_type_reaches_the_request_body_on_both_paths` (`jpeg2000` -> `image/jp2`), `test_every_installed_encoder_sends_its_registered_media_type` (K1 and T-009, every registered encoder); refusal `test_an_encoder_without_a_registered_image_media_type_is_refused_before_any_read` (12 encoders, both paths), `..._refused_at_load` (role), `..._refused_before_the_request` (T-009). Fix round 4: `test_non_default_encoder_reaches_the_request_body_on_both_paths` (`png`, `webp`), `test_huge_integer_counters_embed_on_both_paths` (`max_image_side`, `max_decode_pixels` `10**400`); refusal `test_vision_format_without_an_installed_encoder_is_refused_before_any_read` (`''`, `no_such_encoder`), `test_a_configured_format_is_never_replaced_by_the_source_format`, `test_the_encoder_registry_is_complete_in_a_fresh_process`. Earlier: `test_image_setting_change_reembeds_in_the_same_space` (`max_image_side` 3), `test_policy_refusals_are_recorded_with_file_metadata` (exact metadata since fix round 1), the metadata properties of Fix round 1 table 3 (since fix round 2 also tagged TIFF and AVIF, and refusals of tagged PNG and TIFF); refusal by the existing T-009 tests and the sentinel and missing tests (every `vision` key K1 reads, `max_decode_pixels`) | configurable |
| Devices | none (Q2) | n/a | n/a | n/a | not a client setting (R-018) |
| Checkpoint layout, stage names | `LAYOUT_VERSION = 1`, `IMAGE_METADATA_RULE = 2` (fix round 2: keys image artifacts only, so round 1 TIFF metadata is recomputed), `k1_image`, `k1_text`, `emb-` prefix, `<{n}f` float32 little-endian | n/a | n/a | `test_metadata_rule_keys_image_artifacts_only`, `test_tiff_metadata_persists_with_its_own_size` | artifact-bound |
| Stored image metadata rule (fix round 2) | none: `model_image._stored_size` (TIFF tags 256 and 257, else `image.size`) | n/a | n/a | metadata properties with tagged TIFF and refusals, `test_tiff_stores_its_own_size_tags`, the two file-offset tests | artifact-bound (TIFF tag numbers are protocol-bound, TIFF 6.0) |
| Decode boundary (fix round 3, R2F1) | none: `model_image._decoding_file_bytes` wraps only the calls that read the file's bytes (open, stored size, EXIF, load, orientation, RGBA conversion of the file's transparency); any exception there becomes `UndecodableImage`, so `undecodable`; `DecompressionBombError` keeps `exceeds_max_decode_pixels`; `MemoryError` and interrupts pass through; settings, policy and encoding stay outside, and `_decode` no longer catches `ValueError` | n/a | n/a | `test_truncated_qoi_is_undecodable` (13, 14, 18), `test_truncated_qoi_is_checkpointed_and_isolated`, `test_every_writable_format_cut_or_damaged_is_a_per_image_outcome`, `test_malformed_exif_is_a_per_image_outcome`, `test_decode_property_any_cut_and_damage_of_any_writable_format`, `test_the_boundary_holds_no_setting_or_encoding_fault`, `test_memory_exhaustion_and_interrupts_pass_through_the_boundary` | n/a (no literal; the file-error policy) |
| Encoder registry rule (fix round 4) | none: `config.installed_encoder`, Pillow's `Image.SAVE` after `Image.init()`, `name.upper()` as `Image.save` does | n/a | n/a | the R3F1 tests in the image preparation row; mutants Q1, Q5 to Q7 | protocol-bound (Pillow's registry) |
| Media type rule (fix round 5) | none: `config.image_media_type`, Pillow's `Image.MIME[name.upper()]` after `installed_encoder` (`Image.init()`), only an `image/` type; used by K1's data URI, T-009's data URI and cloud `media_type`, and both preflights | n/a | n/a | the R4F1 tests in the image preparation row, `test_cloud_image_states_the_registered_media_type`; mutants M1, M3 to M7, M10 | protocol-bound (Pillow's registry; the `image/` top-level type of a data URI image) |
| Timer float rule (fix round 4) | none: `config._require_float_seconds` for every read key whose schema node admits `number` (`_schema_types`) | n/a | n/a | the R3F2 tests in the timeouts row, `test_the_float_rule_covers_exactly_the_schema_numbers_k1_reads`; mutants Q2 to Q4, Q8, Q9 | protocol-bound (IEEE 754 binary64 clocks of AnyIO and asyncio) |
| Exact-integer schema check (fix round 3) | none: `config._EXACT_INTEGERS`, Draft 2020-12 with `integer` meaning `type(value) is int` | applied to K1's read set only | n/a | the `2.0` cases of `test_kb_sync_refuses_an_invalid_number_by_name` | protocol-bound (K1's consumers need exact ints) |
| Wire constants | `encoding_format: 'float'`, `data[].index/embedding`, `image_url`, `messages`, `continue_final_message` assistant turn | n/a | n/a | the parsing properties `test_parse_*`, since fix round 1 over the whole numeric range (Fix round 1 table 1) | protocol-bound (Step 0 A1, A2) |
| Test oracle constants (fix round 1) | `FLOAT32_OVERFLOW = 2**128 - 2**103`, `FLOAT64_OVERFLOW = 2**1024 - 2**970` in `tests/test_k1_embed.py` | n/a | n/a | used only by the whole-range properties; fix round 4 adds `FLOAT_MAX` and `FIRST_UNCONVERTIBLE = 2**1024 - 2**970` and the `TIMERS` key list in `tests/test_k1_config.py`, used only by the timer tests and the domain oracle | test-only (IEEE 754) |
| Report and status codes | `SOURCE_CHANGED`, `REUSED_REFERENCED`, `HTTP_ERROR`, `RESPONSE_INVALID`, `DIMENSION_MISMATCH`, the `decode_status` values | n/a | contracts enum table; fix round 2 (RT4): `contracts/README.md:70` states when `format`, `width`, `height` stay null | `test_changed_source_fails_then_recovers`, `test_resume_false_reuses_referenced_artifacts_only`, `test_http_error_fails_items_without_done_rows` | protocol-bound (the project's contract) |

### Work completed

K1 is implemented as approved in Step 0 and the PM answers.

- **`src/prompt_enhancer/k1_embed.py`:**
  - `embedder_settings`, the embedder revision and the artifact keys.
  - The two documented request adapters (`openai_input_content`, `vllm_chat_messages`).
  - `parse_embeddings`.
  - `embed_images` (Part 1) and `embed_concept_texts` (Part 2, the interface tested with fixtures).
- **Configuration:**
  - New keys `embedding.backend`, `request.{shape, image_instruction, extra_body, text_template}`, `dimension`, `dimension_mode`, `batch_size`.
  - New route `vlm.backends.*.endpoints.embeddings`.
  - `vision.transport` becomes an enum holding only `data_uri`.
  - New load-time cross checks (`_validate_embedding`) and the `kb sync` reads (`embedder_reads`).
  - The chat route is required only for a role-bound or default backend.
- **Shared code:**
  - `model_image.prepare_image_with_info`, `ImageRejected` and `check_vision_settings`. `prepare_image` is a wrapper, so its behaviour is unchanged.
  - `k0_scan.read_source_file`: read-only, refusing a link on every path component.
  - `Checkpoints.reusable`, for Q5.
- **Docs:** `docs/stages/K1-embed.md`; in `contracts/README.md`, the owner line (devices, Q2) and the `decode status` enum.

### Result and evidence

- **Full pipeline suite** (run once): `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider tests`, with `PYTHONPATH=src`, `PYTHONNOUSERSITE=1` and `PYTHONDONTWRITEBYTECODE=1`.
  - Output: `1918 passed, 33 skipped, 4 warnings in 532.53s (0:08:52)`, `rc=0`.
  - Serial, because `pytest-xdist` is not installed: `ModuleNotFoundError: No module named 'xdist'`.
- **K1 files after the three tests added for the mutant survivors:** `tests/test_k1_embed.py tests/test_k1_text.py tests/test_k1_config.py`, same command.
  - Output: `89 passed, 1 skipped in 29.77s`.
  - The skip is `test_linked_source_file_is_never_read`: this Windows account cannot create a symbolic link. The test runs on Linux CI.
- **No known failures.** None needed checking against main `eb3df16`.
- **Mutants** (`.tmp/T-008/mutate.py`, scratch copy, through the test slot): 10 run, and all 10 are killed in the end.
  - First run killed M2 to M7 and M9. These survived:
    - M1: batch slice `+ 1`. It was masked because `max_concurrency` 1 makes the window equal the batch.
    - M8: the `Checkpoints.reusable` referenced check removed.
    - M10: the `is_image_id` input check removed.
  - Added `test_batch_size_override` with concurrency 2, `test_resume_false_recomputes_an_unreferenced_done_artifact` and `test_invalid_handoff_image_id_is_refused`.
  - Rerun: `M1 killed ... 1 failed, 10 passed`, `M8 killed ... 1 failed, 12 passed`, `M10 killed ... 1 failed, 11 passed`.
- **Lint:**
  - `ruff` is `not_run`: `<venv python> -m ruff --version` gives `No module named ruff`, and installs are not allowed.
  - `eol_check.py` on the changed tracked files: `eol_check: clean`.
- **Test doubles,** all labelled: `tests/k1_fixtures.py::EmbeddingServer` (an `httpx.MockTransport` embedding server; no network, no model); the interruption double in `test_interrupted_run_resumes_at_the_first_unfinished_image`.
- **`not_run`: the live GPU smoke.** It needs the `gpu-live-run` skill and a model download the user has not approved. So no engine has been probed live: both request shapes rest on the primary documentation in Step 0, A1 and A2.

### Research evidence

| Question | Source | Read raw or summary | Finding | Class |
|---|---|---|---|---|
| llama.cpp image embedding over HTTP | `https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md`, file commit `32bdc3a8` | raw | `/v1/embeddings` `input` objects with a `content` array, `image_url` as a data URI, `--mmproj`; "Each object gives one embedding"; normalised with the Euclidean norm; no `dimensions` field documented | documented |
| vLLM image embedding over HTTP | `https://docs.vllm.ai/en/latest/models/pooling_models/embed/`; `examples/pooling/embed/vision_embedding_online.py` at main, file commit `32e0f407` | page summary; example raw | `/v1/embeddings` with `messages`, one conversation per request; Qwen3-VL-Embedding layout with system, user (image then empty text) and an empty assistant turn, `continue_final_message`, `add_special_tokens`; `dimensions` for Matryoshka models | documented, source-verified |
| EmbeddingGemma 2 on vLLM | `https://github.com/vllm-project/vllm/pull/60254` | page summary | merged 2026-10-06; offline `llm.embed` only; the HTTP image route is not shown | documented (HTTP: unconfirmed) |
| Candidate needs | `https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B`, `https://huggingface.co/google/embeddinggemma-2` | page summary | Qwen: dimensions 64 to 2048, default instruction, vLLM `runner="pooling"`. Gemma: no image prefix, bf16 or fp32 only, dimensions 768/512/256/128, the shortened vector must be L2-normalised. Neither card covers llama.cpp | documented |
| Reuse map | the repository files cited in Step 0 B | raw | as listed in B, with the scout corrections | source-verified |
| Server output bit-reproducible across runs | none | n/a | not guaranteed, hence Q5 | assumption |

### Important changes and reasons

- **Existing tests changed for the contract change, with their reasons:**
  - `tests/test_model_config.py`: the `vision.transport` row. The free string is now an enum.
  - `tests/test_field_contracts.py`: `vision.transport` is a protocol-bound constant. The new keys need coherent alternatives: the backend name, a candidate, a template with `{text}`, plus the `openai_input_content` and `native` options.
  - `tests/conftest.py`: the synthetic embedder backend and resolved K1 keys. `kb sync` now reads them.
  - `tests/test_k0_config.py`: `embedding.chosen` must be a listed candidate.
- **`embedder_revision`** is `emb-` plus a SHA-256 of the engine, model id and revision, chosen, revision, shape, dimension and dimension mode. Instructions and image settings enter the artifact keys only, so image and text rows share one embedding space (`check_embedding_space`, verified by `test_text_and_image_rows_share_one_embedding_space`).
- **Checkpoints:** one JSON artifact per input, with the vector stored as base64 of float32 little-endian (an exact round trip).
  - Policy refusals of an image are stored as done records, with `decode_status` and the file's metadata.
  - A changed or unreadable source, an HTTP error or an invalid response gives a `failed` row, retried at the next sync.
- **Identical concept text is embedded once** (keyed by its hash and the template), and each `(concept_id, text_kind, text_hash)` keeps its own row.
- **Decision for `docs/implementation-notes.md`** (the PM moves it): with `vllm_chat_messages`, concept text is sent without the image instruction. Only `text_template` shapes text requests. A per-kind text template waits for K3 and K4.

### Problems, risks and remaining work

- The live GPU smoke is `not_run`: it needs the user's approval for a model download. The dev pairing note under Q3 stands.
- Image preparation runs sequentially inside each window. Throughput is not measured.

### Handoff

- **Current status:** fix round 5 done (R4F1 registered media type, N1 overflow-free capped backoff, see Fix round 5); waiting for the retest. Earlier: fix round 4 (R3F1 encoder registry check with no fallback, R3F2 timer float rule).
- **Worktree, base and patch:** `.tmp/worktrees/T-008`, base `b4eb856`. The changes are uncommitted in the worktree. Changed files are listed under Work completed, plus the tests.
- **Decisions and notes for the PM to merge:**
  - the PM answers Q1 to Q5 above, as PM decisions pending user review;
  - the text-instruction note above.
