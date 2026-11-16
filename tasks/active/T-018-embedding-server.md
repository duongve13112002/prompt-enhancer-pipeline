# T-018 Embedding server

- **Task name:** T-018 project-owned embedding server (`transformers`, OpenAI-compatible `/v1/embeddings`) for EmbeddingGemma 2, Qwen3-VL-Embedding-2B and -8B
- **Responsible role:** implementer (builder); Codex reviews (decision 91)
- **Task difficulty and assigned model:** feature; Claude Sonnet 5.5, effort `high`
- **Reasoning effort and reason:** high; new protocol surface, new config keys, GPU environment
- **Preloaded skills:** config-audit, gpu-live-run (P2 and P3 only)
- **Current status:** P1 Step 0 written; waiting for approval. No file outside this task file was changed, nothing downloaded or run.
- **What was already done:** read `k1_embed.py`, `config.py` (resolve_backend_engine, `_validate_embedding`, `EMBEDDING_SHAPE_ENGINE`), the example profiles, K1/model-storage/dev-machine/dependencies docs, N104, N105, the scout report `.tmp/T-018/research.md`; re-read the primary sources below.
- **Expected output:** see the brief (R-016, R-018, R-020, R-015, R-017).
- **Constraints:** one `.venv-gpu`; downloads at most 12 GB in total; no GGUF; no `trust_remote_code`; `<project>` in tracked paths.
- **Ignored evidence paths:** `.tmp/T-018/research.md` (scout leads, superseded by the table below), `.tmp/T-018/web/` if I save page text (none so far).

## P1

### Research evidence (primary sources, read 2026-11-16 through a summarising fetch; quotes are verbatim, a sentence may be cut)

| Question | Source | Finding (quoted words) | Class |
|---|---|---|---|
| EG2 repo identity | `https://huggingface.co/api/models/google/embeddinggemma-2` | `sha` `f9972ac22e696a105c482202646979e33e4b5a1e`; `gated: false`; `license: apache-2.0`; architectures `EmbeddingGemma2Model`, `model_type: embedding_gemma2`; **no `auto_map`**; 744,371,512 params BF16 | documented |
| EG2 files | same API, `siblings` | `model.safetensors` 1,488,915,288 B (sha256 `197a3296…553d79`), `tokenizer.json` 32,170,510 (`4d777ef5…4828be4`), `tokenizer.model` 4,689,013 (`e594c8a9…2ef03`), 12 small JSON/jinja files; **sum 1,525,810,627 B (1.53 GB)** | documented |
| EG2 how to embed | model card `…/embeddinggemma-2/raw/main/README.md` | Only a sentence-transformers recipe: `SentenceTransformer("google/embeddinggemma-2")`, `model.encode(query, prompt_name="SearchQuery")`, `prompt_name="Document"`, `truncate_dim=128, normalize_embeddings=True`, `model_kwargs={"torch_dtype": dtype}` with `torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32`, image input `model.encode({"image": [...]})`. Pooling "Mean Pooling"; "Native Output Dimension | 768"; MRL 128/256/512/768; "slicing a unit-length vector does not preserve unit length." No transformers-only example, no resize code | documented |
| EG2 module chain | `modules.json` | Transformer → `1_Pooling` → `2_Normalize` (sentence-transformers) | documented |
| EG2 model output | `transformers` v5.23.0 `models/embedding_gemma2/modeling_embedding_gemma2.py` | `EmbeddingGemma2Model` returns `EmbeddingGemma2ModelOutput` with `last_hidden_state`…, no `pooler_output`; the text model projects per token (`embedding_projection`, "Projecting per token is equivalent to projecting after mean pooling"); docstring: "Intended to be wrapped by SentenceTransformers' mean pooling and normalization." Mean pooling and L2 are **not** in the model, so the server must add them | source-verified |
| EG2 image path | `processing_embedding_gemma2.py` (v5.23.0), `chat_template.jinja` | "any single modality on its own is a valid embedding input"; the processor expands each image to `boi + image_token*n + eoi`; the template emits `<|image|>` for an image item. Exact `__call__` path lives in a base class: **not read, unknown** (read in P2 and settled by the reference check) | source-verified / unknown |
| EG2 image budget | `processor_config.json` | `max_soft_tokens: 280`, `image_seq_length: 280`, `patch_size 16`, `pooling_kernel_size 3`, `do_normalize: false`; card: soft tokens "from 70 to 1120" | documented |
| EG2 text prompts | `config_sentence_transformers.json` | `Document`: `"title: none \| text: "`; `SearchQuery`/`query`: `"task: search result \| query: "`; requires "sentence-transformers >=6.1.0"; built with `transformers 5.18.0.dev0`, `pytorch 2.14.0+cu130` | documented |
| Min `transformers` for EG2 | GitHub API releases: `v5.21` (2026-10-26) release body "Fixed Gemma4 processor imports (the Gemma4, Gemma4 unified and EmbeddingGemma2 processors)"; latest stable `v5.23.0` (2026-11-13), PyPI `info.version` 5.23.0, `requires_python >=3.11.0` | Card gives no minimum. The first **stable** tag the summary showed mentioning it is v5.21 (v5.20.x bodies were not matched); I pin **5.23.0** (the version whose `modeling` file I read) and let the reference check prove it | documented (pin) / assumption (min) |
| Qwen 2B identity | `…/api/models/Qwen/Qwen3-VL-Embedding-2B` | `sha` `9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda`; `gated: false`; `apache-2.0`; `Qwen3VLForConditionalGeneration`; **no `auto_map`**; `model.safetensors` 4,255,140,312 B (`c73fa9ca…4509c1`); `tokenizer.json` 11,423,705 B (`def76fb0…8b4b50a`); `scripts/qwen3_vl_embedding.py` 13,258 B; **sum 4,271,068,726 B (4.27 GB)** | documented |
| Qwen 8B identity | `…/api/models/Qwen/Qwen3-VL-Embedding-8B` | `sha` `2c4565515e0f265c6511776e7193b22c0968ddc7`; not gated; apache-2.0; shards 4,998,056,552 / 4,915,962,464 / 4,915,962,496 / 1,459,698,112 B (sha256 `79ef275e…e7733c4`, `a4da61f5…78545f0`, `7fb17cf8…8d47007`, `000213b6…d31d8fed1f5`); **sum 16,305,675,665 B (16.31 GB)** — alone above the 12 GB cap, so it is never downloaded | documented |
| Qwen how to embed | model card README | `SentenceTransformer("Qwen/Qwen3-VL-Embedding-2B")`; "By default, all inputs are wrapped with the "Represent the user's input." instruction via a system prompt."; `model.encode(queries, prompt="…")`; transformers path: "transformers>=4.57.0", "qwen-vl-utils>=0.0.14", "torch==2.8.0", `from scripts.qwen3_vl_embedding import Qwen3VLEmbedder`; "Embedding Dimension: Up to 2048, supports user-defined output dimensions ranging from 64 to 2048". `trust_remote_code=True` appears only in the vLLM and SGLang examples | documented |
| Qwen pooling | `1_Pooling/config.json`; helper script `scripts/qwen3_vl_embedding.py` | `{"embedding_dimension": 2048, "pooling_mode": "lasttoken", "include_prompt": true}`; the script: last non-padding token via the attention mask, `padding_side='right'`, `F.normalize(embeddings, p=2, dim=-1)` on by default, no truncation code; `MAX_LENGTH = 8192`, `MIN_PIXELS = 4 * 32 * 32`, `MAX_PIXELS = 1800 * 32 * 32` (1,843,200), `do_resize=False` in the processor call after `qwen_vl_utils.process_vision_info` resizes; system message `instruction or "Represent the user's input."`; the script calls `from_pretrained(..., trust_remote_code=True)` on a class **defined in the script**, and the repo has no `auto_map`, so no remote model code is fetched, but the script itself is downloaded code (read only 4 excerpts so far) | documented / source-verified (excerpts) |
| Qwen ST module chain | `modules.json` of 2B | Transformer → `1_Pooling` → `2_Normalize` | documented |
| Driver and torch | `nvidia-smi` (read now): RTX 5070 Laptop, 8151 MiB, 0 MiB used, driver 616.92, "CUDA UMD Version: 13.4". `https://download.pytorch.org/whl/cu130/torch/` lists `torch-2.14.1+cu130-cp312-cp312-win_amd64.whl` newest; `curl -I` Content-Length **1,990,608,063 B (1.99 GB)**; torchvision 0.29.1+cu130 cp312 listed, size not measured | CUDA 13.4 ≥ 13.0, so cu130 fits; sm_120 (Blackwell) inside the cu130 wheel is an assumption until `torch.cuda.get_arch_list()` in P2. `.venv` is Python 3.12.10 (cp312 matches) | documented / assumption |

**Licence:** all three Apache-2.0, none gated (no login or acceptance token needed). **`trust_remote_code`: not needed by any of the three** (no `auto_map` in the config of any; EG2 and Qwen classes are in `transformers` itself). The server never passes `trust_remote_code=True`; `from_pretrained(local_dir, trust_remote_code=False)` is explicit, so a repo that gained an `auto_map` would fail instead of running code. The one downloadable code file is the Qwen helper script; I would run it only as a second reference after PM approval, pinned to revision `9f2f7e71…` with all 13,258 bytes read first. Primary reference plan avoids it (see Reference check).

**Conflict found:** the Qwen card pins `torch==2.8.0` for its helper script; the EG2 card was built with torch 2.14.0+cu130 and transformers 5.18.0.dev0. One `.venv-gpu` can hold only one torch; I choose **torch 2.14.1+cu130 + transformers 5.23.0** (newer than both cards, reachable by Blackwell) and prove Qwen against the sentence-transformers reference under the same stack. If the reference fails on Qwen only, I stop and ask rather than add a second environment.

### Unknowns and failure modes
1. EG2 image-only processor call path (base class `OmniModalProcessorMixin`) is unread; the sentence-transformers path decides, not my reading.
2. `sentence-transformers>=6.1.0` availability on PyPI is unchecked (card metadata only).
3. Batch-8 VRAM at a 1024 side for either model: unknown, measured in P2 under `gpu-live-run`.
4. bfloat16 batch-size variance may exceed a hand-picked tolerance; the tolerance is fitted from observed spread (below).
5. EG2 must not run in float16 (N104 / card: "Do not use `float16`"); a profile `dtype: float16` for EG2 must be refused.
6. `models/*` is Git-ignored and no manifest file exists anywhere (`models/manifest.json` is not in the tree); the scout and the brief name one.

### Server design

**Placement:** package `src/prompt_enhancer/embed_server/`: `protocol.py` (pure Python: parse request, validate, build response; no torch, runs in the CPU `.venv`), `models.py` (family adapters, torch and transformers imported lazily inside functions), `server.py` (stdlib `http.server.ThreadingHTTPServer`, one GPU lock), `__main__.py`. Command: `python -m prompt_enhancer.embed_server serve --config <profile.yaml> [--overlay <file>] --backend <name>`; exits non-zero with a precise message on any missing or sentinel field (R-015). No FastAPI or uvicorn: stdlib keeps the download inside the cap and the CPU suite free of a web stack. Server is loopback-only (host from `base_urls[0]`, refused when not loopback, same rule `endpoint_url` already applies).

**Request** (exactly what `k1_embed.py` sends): `POST /v1/embeddings` JSON `{model, encoding_format: "float", [dimensions], input}`; `input` is a list whose items are either a string (text, K1 `text_bodies`) or `{content: [parts]}` with parts `{type:"text", text}` and `{type:"image_url", image_url:{url:"data:<image/…>;base64,…"}}` (K1 `image_bodies`, `k1_embed.py:246-252`). Parser refuses (HTTP 400, JSON `{error:{message,code}}`): non-JSON, non-object, missing/empty `input`, more items than `batch_size`, a bare string `input` (K1 never sends it; refuse rather than guess), non-`float` `encoding_format`, `model` ≠ the configured id, `dimensions` (K1 sends it only in `request_field`, which config already refuses for non-vLLM engines), http(s) image URLs (data URIs only: no network fetch from the server; a safety choice of mine, not a cited requirement), a data URI that is not valid base64 or whose media type is not `image/*`, decoded size above a profile cap, an item with 0 or >1 images, unknown part types. Text parts before the image become the **instruction** (system message for Qwen; EG2 configured with `image_instruction: none` refuses a non-empty instruction). Request body size capped by profile.

**Response:** `{object:"list", data:[{object:"embedding", index:i, embedding:[floats]}], model:<id>, usage:{prompt_tokens, total_tokens}}`, `index` equals input position, vectors float32 L2-normalised at the family's native dimension (what `parse_embeddings` and `_reduce` accept in `native`; `client_prefix_l2` slices then re-normalises on the client, N104). `usage` counts tokenizer tokens (model-reported, never invented). Errors never carry a stack trace.

**Batching:** each request is one forward pass in chunks of `server.forward_batch` (≤ `embedding.batch_size`); requests serialise on one lock so VRAM stays bounded (`vlm.backends.embedder.max_concurrency` bounds the client; server concurrency is 1 GPU job). Padding side is per family (EG2: processor default, confirmed by the reference; Qwen: right, last-token via attention mask).

**Config source (R-015):** everything comes from the profile, nothing from source constants: model dir = `models/<model.file>`-style path under the project root (validated by containment after resolving links), `model.id`, `model.revision` (compared with the revision recorded in the manifest and in the local `config.json` hash), host and port from `base_urls[0]`, device from `runtime.gpus` (`cuda:<gpus[0]>`; more than one gpu refused until sharding is needed), dtype, family, token and pixel limits, from a new block `vlm.backends.<name>.server` (Q5). No default for any of them.

**Health:** `GET /health` → 200 `{status:"ok", model:<id>, revision:<rev>, family, dtype, device, loaded:true}` only after weights are loaded and a one-token warm check passed; 503 `{status:"loading"}` before. (`endpoints.health` already exists in the backend schema, value `health`.)

**Lifecycle:** `serve` prints one JSON line with its PID when ready; stops on SIGINT/SIGTERM/CTRL_BREAK and frees the GPU (`torch.cuda.empty_cache`, process exit). `server.idle_exit_s` (profile; int seconds, `0` = never, no hidden default) exits when no request arrived for that long, which is the "stop when its queue drains" rule (AGENTS.md §8). Tests and `gpu-live-run` start it with `subprocess.Popen(..., creationflags=CREATE_NO_WINDOW)` on Windows (flag read through `getattr(subprocess, "CREATE_NO_WINDOW", 0)` so Linux is unaffected) and stop it by recorded PID; the module itself starts no child process. VRAM baseline before and after, per `gpu-live-run`.

### Profile and manifest entries (all three models)

New `vlm.backends.<name>.server` keys (all validated, sentinel refused at start): `family` (`embedding_gemma2` | `qwen3_vl_embedding`, must match the local `config.json` `model_type`), `dtype` (`bfloat16`|`float32`; `float16` refused for `embedding_gemma2`), `forward_batch`, `max_length`, `image` (EG2: `max_soft_tokens`; Qwen: `min_pixels`, `max_pixels`), `text_instruction` (system instruction applied to plain-string inputs; Qwen "Represent the user's input.", EG2 `none`), `idle_exit_s`, `max_request_bytes`, `attn_implementation` (`sdpa` | `eager`; no flash-attn). Engine name: `embed_server` (Q1). `engine.version` = the project package version, `pin_required` until set.

| Model | `model.id` | `revision` | dim | mode | `image_instruction` | `text_template` | server block |
|---|---|---|---|---|---|---|---|
| EG2 | `google/embeddinggemma-2` | `f9972ac2…5a1e` | 768 | `native` (128/256/512 via `client_prefix_l2`, E5) | `none` | `title: none \| text: {text}` (document side; query prefix is a retrieval-side value, N104) | family `embedding_gemma2`, `bfloat16` |
| Qwen 2B | `Qwen/Qwen3-VL-Embedding-2B` | `9f2f7e71…bda` | 2048 | `native` | `Represent the user's input.` | `{text}` with `text_instruction` set | family `qwen3_vl_embedding`, `bfloat16` |
| Qwen 8B | `Qwen/Qwen3-VL-Embedding-8B` | `2c456551…2c7` | 4096 | `native` | same | same | same; `status: not_run_on_dev`; `deploy_required` for host, device, `forward_batch` |

Sentinels vs missing: `embedding.chosen` stays `fit_from_gt` in the full profile (E5 decides; dev comparison never writes it); full-profile `embedding.candidates` gains `qwen3-vl-embedding-8b` (note N105); `engine.version`, `base_urls`, timeouts and `image.max_decode_pixels` stay `pin_required`/`deploy_required`; `model.source`/`file` hold the local directory. Dev example: backend `embedder` (EG2) plus `embedder_qwen3vl2b` (own loopback port); the dev comparison switches with an overlay (`embedding.chosen/backend/revision/dimension/request`), because `embedding.revision` must equal the backend's `model.revision` (`config.py:1211`). Not run on dev: 8B.

Config code touch (runtime): `resolve_backend_engine` (`config.py:636`), the backend check (`config.py:692`), `EMBEDDING_SHAPE_ENGINE` (`config.py:816`: add `embed_server` for `openai_input_content`), and `contracts/schemas/enhancer_config.v0.schema.json:3191` (engine enum + the new `server` object, `additionalProperties:false` like its neighbours).

**Manifest:** `models/*` is Git-ignored, so a manifest inside it cannot be reviewed or versioned. Proposal (Q2): tracked `configs/model_manifest.json` (+ `contracts/schemas/model_manifest.v0.schema.json`) with, per model: repo, revision, licence, gated flag, every file with size and sha256. Verifier `embed_server` checks size and sha256 of each file before load (streamed; refuses a mismatch). Known now from the API: EG2 and Qwen 2B large-file sha256 and all sizes; the small JSON files' sha256 are filled from the downloaded bytes in P2; the 8B lists the five big files (LFS sha256 above) and its small files as `pin_required` since it is never downloaded.

**`.venv-gpu` dependency profile:** new `requirements-gpu.txt` at the project root (CPU `.venv` untouched, AGENTS.md §4) pinning `torch==2.14.1+cu130` (index `https://download.pytorch.org/whl/cu130`), `transformers==5.23.0`, `torchvision==0.29.1+cu130` (Qwen image processor), `Pillow==11.3.0` (same as the project), `safetensors`, `huggingface_hub` and `sentence-transformers` (reference check only; the version is looked up in P2 before install and pinned there). `.venv-gpu` installs the project with `pip install -e .` without torch. `docs/dependencies-and-environments.md` and `docs/dev-machine.md` updated. Download estimate: torch 1.99 GB (measured) + transformers, torchvision, sentence-transformers and their dependencies about 0.5 GB (assumption, re-measured with `pip download --dry-run`-style sizes before P2 installs) + EG2 1.53 GB + Qwen 2B 4.27 GB = **about 8.3 GB**; with pip's wheel cache kept it would double torch (+2 GB = 10.3 GB), so installs use `--no-cache-dir`. Under 12 GB. Installed size is larger than the wheels (several GB on disk; 159 GB free on `D:`).

### Reference check
For each of EG2 and Qwen 2B, on the same one or two images from `database/` (read only, prepared by `prepare_vision_image_with_info` at `vision.max_image_side`): (a) the **documented** path: `SentenceTransformer(<local dir>)` `.encode({"image": img})` (the card's own recipe for both; no downloaded code); (b) the server's `/v1/embeddings` response for the same bytes sent in the K1 shape; compare cosine and max absolute difference. The tolerance is not hand-picked: `server.reference_check.min_cosine` is `design_required` in the profile and is filled in P2 from the measured spread of the reference against **itself** (same model, bf16, batch size 1 vs 2 vs 8; run order swapped) with the evidence in the task file; the server result passes when its cosine to the reference is at least the lowest self-spread cosine observed. The Qwen helper script is a second reference only if the PM approves running the 13,258-byte file at revision `9f2f7e71…` after I have read all of it (Q3). Text: also compare one `Document`-prompted sentence for EG2 and one instruction-wrapped sentence for Qwen. This is a plumbing correctness check, `smoke`, never a quality claim (R-020).

### Dev comparison plan (P3, `gpu-live-run`, one model at a time, VRAM baseline before and after)
On the dev knowledge base (`database/`, read only): K1 part 1 embeds every image with each model in turn (EG2, then Qwen 2B; checkpoints are per revision so reruns are idempotent). Smoke metrics, labelled `smoke-only`, no quality claim, no sentinel written: (1) leave-one-out nearest neighbour: for each image the share whose nearest other image lies in the same concept folder, and recall@5 of same-folder images; (2) concept-name text → image: the folder name embedded as text (K1 part 2 text path), the share of folders whose top-5 images contain an image of that folder; (3) per model: wall time per image and peak VRAM, reported as observations on this machine only. Output: one table in the task file; the PM then asks the user which weights stay (decision 139, the deletion is the PM's to confirm with the user, not mine). Not decided by me: `embedding.chosen` of the full profile.

### Hazards and the test that catches each
| Hazard | Test |
|---|---|
| Server vector differs from the model's documented output (pooling, padding side, prompt, resize) | P2 reference check above; CPU: golden test of mean pooling + L2 and last-token pooling on a tiny labelled fake model |
| Parser accepts a body K1 never sends, or refuses one it sends | property tests (`hypothesis`): every K1 `input` shape built by `Embedder.image_bodies/text_bodies` round-trips; random malformed JSON, nested depth, bad base64, wrong media type, huge sizes always give a 400 JSON error, never a crash |
| `index` or order mismatch, wrong count | K1 `parse_embeddings` fed with the server response in a loopback test (labelled test double model) |
| Server fetches a remote image URL (SSRF) | test: `http://` and `file://` URLs refused without any socket opened |
| Sentinel or missing profile key starts the server | override + refusal tests for each new key (`family`, `dtype`, `forward_batch`, `max_length`, `image.*`, `text_instruction`, `idle_exit_s`, `max_request_bytes`, `attn_implementation`, engine `embed_server`, port from `base_urls`) |
| EG2 run in float16 | refusal test `dtype: float16` for `embedding_gemma2` |
| Family does not match the weights | test with a local `config.json` of the other `model_type`: refused before load |
| Weights differ from the manifest (size, sha256) | test with a flipped byte in a tiny fake file: refused; matching file accepted |
| `trust_remote_code` creeps in | test that `from_pretrained` is called with `trust_remote_code=False` (call spy) and that a config with `auto_map` is refused |
| Non-loopback host | refusal test |
| Idle exit does not free the GPU or leaves a process | test: start with `idle_exit_s` small (labelled fake backend), assert exit code 0 and port closed; P2: VRAM back at baseline |
| Console window on Windows | test asserts `creationflags` includes `CREATE_NO_WINDOW` in the test launcher helper |
| Output dimension disagrees with profile `dimension` | refusal at startup (family native dim vs `embedding.dimension` in `native` mode) |

### Questions (ranked, each with my proposed answer)
1. **Engine identity.** The engine enum is `vllm | llama_cpp` (`config.py:636,692`, schema line 3191). Add `embed_server` for this server and map `openai_input_content` to it (touches `config.py`, the schema, `profile_lint`, tests), or say `llama_cpp`? *Proposed: add `embed_server`; claiming llama.cpp would be false and would hide the real engine in the artifact revision.*
2. **Manifest location.** Tracked `configs/model_manifest.json` + schema, since `models/*` is ignored and no manifest exists. *Proposed: yes.*
3. **Qwen reference.** Use the sentence-transformers path for both models (no downloaded code); run the Qwen helper script as a second reference only with your approval after I read it fully. *Proposed: ST first, helper script optional.*
4. **HTTP stack.** stdlib `http.server`, no FastAPI/uvicorn. *Proposed: stdlib; throughput is not a goal on this machine (R-020).*
5. **Where the server keys live.** Under `vlm.backends.<name>.server` (one place with model id, revision and URL), not a new top-level section. *Proposed: under the backend.*

### Configuration impact
| Setting | Schema / profile source | Validation |
|---|---|---|
| Server model, revision, directory | `vlm.backends.<name>.model.{id,revision,source,file}`; manifest | containment, manifest size/sha256, `config.json` identity |
| Host, port | `base_urls[0]` | loopback only (existing `endpoint_url` rule) |
| Device | `runtime.gpus`, `runtime.vram_budget_gb` | one GPU; VRAM baseline in `gpu-live-run` |
| Family, dtype, attention, `forward_batch`, `max_length`, image limits, `text_instruction`, `idle_exit_s`, `max_request_bytes` | new `vlm.backends.<name>.server` (schema + example profiles) | per-key type/range, sentinels refused at start, family-vs-weights check |
| Engine name | `vlm.backends.*.engine.name` enum + `EMBEDDING_SHAPE_ENGINE` | enum, shape↔engine rule |
| Batch size | `embedding.batch_size` | server item cap equals it |
| Reference tolerance | `server.reference_check.min_cosine` (`design_required` until P2 fits it) | refused while sentinel when the check runs |
| `.venv-gpu` pins | `requirements-gpu.txt` | exact versions, doc update |
| Retained literals | `/v1/embeddings`, `/health`, `encoding_format: float`, `data:` prefix: protocol-bound; JSON field names: protocol-bound | classified in the audit |

### Plan (checkable finish)
- **P1 (CPU, no network, no GPU):** `protocol.py`, `models.py` (adapters + fake-model double in tests), `server.py`, `__main__.py`, config and schema changes, example profiles, `configs/model_manifest.json` + schema, `requirements-gpu.txt`, docs (K1-embed, model-storage, dependencies, dev-machine, models/README), tests. Done check: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -m pytest tests/test_embed_server*.py tests/test_k1_*.py tests/test_model_client.py tests/test_loader.py tests/test_real_contracts.py -q -n 2 --basetemp=.tmp/T-018/bt` green; `ruff check` and `ruff format --check` clean; 3-10 mutants on the changed lines; config audit table complete.
- **P2 (needs your approval; downloads ≤ 12 GB, `gpu-live-run`):** `.venv-gpu`, EG2 and Qwen 2B weights into `models/` by pinned revision, manifest hashes filled, VRAM baseline, reference check, tolerance fitted, server smoke through K1 on 2 images. Done: reference cosine ≥ fitted minimum for both; VRAM back at baseline; no process left.
- **P3 (needs your approval):** dev comparison table (smoke-only); PM asks the user which weights stay.

### Noticed, not done
- `configs/enhancer.dev.example.yaml:24` says "no primary source yet confirms EmbeddingGemma 2 image embedding on llama.cpp"; stale once the server exists (updated in P1 docs).
- `docs/dev-machine.md` says the embedder runs "through its Python library"; the new server replaces that sentence.
- N055 lacks the `/v1/embeddings` image-input rule the scout found (llama.cpp only; not needed for this server).
- The scout's GGUF/llama.cpp material (`.tmp/T-018/research.md` sections 1-2 of each model) is out of scope by decisions 138 and 139.

#### PM answers (P1)

Each is a conservative, reversible PM decision pending user review. P1 only: CPU, no network, no GPU, no downloads, no installs; P2 and P3 need a separate PM message.

1. Q1 yes: engine `embed_server`, `openai_input_content` mapped to it.
2. Q2 changed: the runtime manifest stays `models/manifest.json` (Git-ignored; `"schema": "model_manifest.v0"`, `entries` with `repository`, `revision`, `file`, `size`, `sha256`, `licence`, `path` laid out as `models/<owner>--<name>/<revision>/<file>`); not moved. Add the tracked schema `contracts/schemas/model_manifest.v0.schema.json` for that exact format (optional `gated` boolean) and `configs/model_manifest.example.json` for the three planned models (known LFS sha256 and sizes, small files `pin_required`). The server checks size and sha256 against the runtime manifest before load. Test: the schema against a fixture holding a copy of the two existing entries.
3. Q3 yes: sentence-transformers is the reference for both models; the Qwen helper script is not run here.
4. Q4 yes: stdlib `ThreadingHTTPServer`, one GPU lock.
5. Q5 yes: server keys under `vlm.backends.<name>.server`; two dev embedder backends plus an overlay.
6. The data-URI-only rule is safety-bound (classified so in the audit).
7. Done check: no `-n` (pytest-xdist absent in `.venv`: `pip list` shows no xdist); ruff is not installed, so `ruff check` and `ruff format --check` are `not_run`; focused tests first, then the full `tests/` once at the end, both through `test_slot.py` with `--basetemp=.tmp/T-018/bt`, deleted after each run.
8. `protocol.py` and every module the CPU tests import import without torch or transformers; one test asserts it in a subprocess where those imports raise.
9. 3 to 10 mutants on changed lines, including the parser, the manifest check and the pooling helpers.
10. Ambiguity: take the conservative reversible choice, record it here as "PM decision pending user review", continue.
11. Limits unchanged.

## Steps

1. Config and schema: `LOCAL_ENGINES` + `EMBEDDING_SHAPE_ENGINE` now accept `embed_server` (config.py), command `embed serve`, `validate_embed_server_reads`, `embed_server_address`; schema gains the `server` object (required when engine is `embed_server`) and the engine enum value; `tests/test_field_contracts.py` expectations gained the three enum-valued server keys.
2. Package `src/prompt_enhancer/embed_server/`: `families.py`, `protocol.py`, `pooling.py`, `manifest.py`, `settings.py`, `models.py` (torch adapters, unrun), `server.py`, `__main__.py`.
3. Profiles: dev example (EG2 `embedder`, Qwen 2B `embedder_qwen3vl2b`), new `enhancer.dev-qwen3vl2b.example.yaml`, full example (8B candidate + `embedder_qwen3vl8b`); `contracts/schemas/model_manifest.v0.schema.json`, `configs/model_manifest.example.json` (generated from the Hub sizes and LFS hashes read 2026-11-16), `requirements-gpu.txt`; docs K1-embed, model-storage, dependencies, dev-machine.
4. Tests: `tests/test_embed_server_{protocol,config,manifest,http,cli}.py` + `embed_server_fixtures.py`.
5. Mutants (15, scratch copy of src/contracts/configs under `.tmp/T-018/mut`): 14 killed on first run; M14 (request body limit off by one) survived, a boundary test was added and M14 is now killed.

### PM decisions pending user review (recorded per standing rule 10)

- The comparison selector is a second profile file that `extends` the dev profile (`configs/enhancer.dev-qwen3vl2b.example.yaml`), not an overlay: `_overlay` (config.py) allows only `storage`, `runtime`, `serving` and `vlm.backends` keys with a fixed backend key set, so `embedding.*` and `server` cannot be overlaid; widening the overlay rules would change deployment-overlay semantics.
- `server.default_instruction` replaces the Step 0 name `text_instruction` (it applies to any input with no instruction, image or text).
- Image limits are one `server.image` object with `min_pixels`, `max_pixels`, `max_soft_tokens`; the value `none` marks the members a family does not use (EG2 uses only `max_soft_tokens`, Qwen only the two pixel limits); a wrong combination is refused.
- `server.reference_check.min_cosine` has no consumer in P1 (the reference tool is P2), so it is removed from P1 and returns in P2 with its consumer (an ignored configured field fails the audit).
- Windows: `allow_reuse_address` is False on Windows (SO_REUSEADDR there lets a second process bind the same listening port) and True elsewhere.
- `models.py` calls (`AutoModel`, `AutoProcessor`, `processor.image_processor` limit attributes, chat template, `dtype=`) are from the cards and v5.23.0 source and unverified until P2; the image-limit setters verify after setting and refuse when the library did not take them.
- The `models/README.md` text still says "model manifest" without the format; the format is now in `docs/model-storage.md` (the brief did not allow touching `models/`).

### Configuration audit (P1)

Tests are in `tests/test_embed_server_config.py` (C), `_http.py` (H), `_protocol.py` (P), `_manifest.py` (M), `_cli.py` (L).

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Class |
|---|---|---|---|---|---|
| Engine identity `embed_server` | `vlm.backends.*.engine.name` | schema enum, `LOCAL_ENGINES`, `EMBEDDING_SHAPE_ENGINE`; `server` block required for it | dev `embedder`, `embedder_qwen3vl2b`; full `embedder_qwen3vl8b` | C `test_the_embedding_server_engine_is_a_known_engine_for_k1_too`; refusals `engine.name=llama_cpp` and `unknown_engine`, `test_the_vllm_shape_is_refused...`, `test_an_embed_server_backend_must_hold_a_server_block` | protocol-bound |
| Command `embed serve` read set | `COMMAND_READS['embed serve']`, `validate_embed_server_reads` | sentinel check of every read key, schema node with exact integers | all three example profiles | C `test_a_sentinel_in_a_read_key_is_refused_at_start` (one case per read key), `test_the_embed_serve_command_is_known...`, dev profiles start only after machine values are set | configurable |
| Model, revision | `vlm.backends.<n>.model.{id,revision}`, `embedding.revision` | revisions equal; id must equal the request `model`; manifest entry for `(id, revision)` required | dev: EG2 `f9972ac2…`, Qwen 2B `9f2f7e71…`; full: 8B `2c456551…` | C `test_every_value_comes_from_the_profile`, refusal `model.revision` differs; H `unknown_model` 400; M no entry for another repo or revision | configurable |
| Host, port, routes | `base_urls[0]`, `endpoints.{embeddings,health}` | `embed_server_address`: plain http, IPv4 loopback or `localhost` (**IPv6 refused, fix round 1 R5**), explicit port 1..65535, no credentials; routes distinct, relative | `deploy_required` in examples | C override (`localhost:9555`, `api/embed`, `api/ping`, `127.0.0.2`); refusals https, non-loopback, no port, port 0 or 70000, credentials, empty list, same route, `../up`, `[::1]`, `[::ffff:127.0.0.1]`; `test_embed_server_address*`; H `test_every_accepted_address_can_bind` (127.0.0.1, localhost, 127.0.0.2 bind and answer); H busy port is an OSError | configurable |
| GPU and memory cap | `runtime.gpus` (exactly one), `runtime.vram_budget_gb` (> 0) | refused otherwise; consumer `models.load_engine` (cap by `set_per_process_memory_fraction`, GPU index check) | dev `[0]`, 8 | C override `[1]`, `2.5`; refusals `[0,1]`, `[]`, `auto`, `[-1]`, `0`, `plenty`, sentinel | configurable (GPU consumer not run in P1) |
| `server.family` | schema enum + `FAMILIES` | must match the weights' `model_type` (`check_weights_config`) | `embedding_gemma2`, `qwen3_vl_embedding` | C Qwen family test, refusals `other`, `5`; `test_weights_config_refusals` (other family's weights) | configurable |
| `server.dtype` | schema enum | `float16` refused for `embedding_gemma2` (card) | `bfloat16` | C override `float32`; refusals `float64`, `float16`; M12 mutant killed | configurable |
| `server.attn_implementation` | schema enum `sdpa`, `eager` | schema | `deploy_required` | C override `eager`; refusal `flash_attention_2` | configurable |
| `server.forward_batch` | integer ≥ 1, ≤ `embedding.batch_size` | exact integer | `deploy_required` | C override `1`; refusals `0`, `3`, `1.5`, `True`; M11 mutant killed | configurable |
| `server.max_length` | integer ≥ 1 | exact integer; consumers: text cut in the tokenizer for **both** families (R1: Gemma ignored it), an image over it refused, never cut | EG2 8192, Qwen 8192 (cards) | C override `33`; refusals `0`, `long`; **models** `test_gemma_text_is_cut_at_the_configured_length[3,17]`, `test_qwen_text_is_cut_at_the_configured_length[3,17]`, `test_an_image_is_never_cut_and_one_over_the_limit_is_refused`; I changes the K1 identity | configurable |
| `server.image.{min_pixels,max_pixels,max_soft_tokens}` | per family: EG2 `max_soft_tokens` only, Qwen the two pixel limits (the rest `none`), min ≤ max | cross-field rule; consumer `_apply_image_limits` refuses when the library did not take the value | EG2 280; Qwen 4096 and 1,843,200 | C override `70`, Qwen `100`/`5000`; refusals EG2 `min_pixels`, `max_pixels`, `0`, `none`; Qwen `none`, `70`, `min>max`, `0` | configurable |
| `server.default_instruction` | string ≥ 1 char; `none` for EG2 | EG2 must be `none`; also `embedding.request.image_instruction` | EG2 `none`; Qwen "Represent the user's input." | C Qwen override `Custom instruction.`; refusals `''`, `be brief` (EG2), image_instruction for EG2; P/H instruction reaches the engine for Qwen and is refused for EG2 | configurable |
| `server.idle_exit_s` | integer ≥ 0 | exact integer | `deploy_required` | C override `9`; refusals `-1`, `1.5`; H idle exit after the request ends, not while one runs, `0` never exits; M15 mutant killed | configurable |
| `server.max_request_bytes`, `server.max_image_bytes` | integers ≥ 1; request limit ≥ base64 size of one max image | cross-field | `deploy_required` | C overrides `12345`/`6000`; refusals `0`, `100`; H 413 and exact-limit boundary, and the 413 reaches the client with 300 kB of body pending (R4); P image limit boundary; M14, M4 killed | configurable |
| `image.max_decode_pixels` | `vlm.backends.<n>.image.max_decode_pixels` | integer ≥ 1 | `deploy_required` | C override `777`; refusal `0`; P pixel limit | configurable |
| `embedding.batch_size` | server item cap | integer ≥ 1 | 8 (dev) | C override `5`; refusal `0`; P/H `too_many_inputs` (M1 killed) | configurable |
| `embedding.dimension`, `dimension_mode` | weights' native size | `native` equal, `client_prefix_l2` within 1..native; `request_field` refused | 768, 2048, 4096 | C `test_weights_config_*` (accept and refuse); refusal `request_field`; M13 killed | configurable |
| `embedding.request.shape` | `openai_input_content` only | refused otherwise | dev examples | C refusals `vllm_chat_messages` (both loader paths) | protocol-bound |
| Runtime manifest | `models/manifest.json`, `model_manifest.v0` | schema; size, sha256, layout path, no links, no unlisted file, sentinel refused | `configs/model_manifest.example.json` | M schema accepts the two existing entries and refuses 16 mutations; verification tests (size, flipped byte, missing, extra, sentinel, layout, duplicate, link, 3 MiB file, property over any one-byte change); M8, M9, M10 killed | artifact-bound (location), protocol-bound (format) |
| Images by `data:` URI only, decoded by an explicit in-process format set | `protocol.DATA_PREFIX`, `DATA_SUFFIX`, `protocol.ALLOWED_FORMATS` (`PNG JPEG WEBP GIF BMP TIFF`) | refusal of http, https, file, other media; Pillow opens with `formats=ALLOWED_FORMATS`; the detected format must equal the media type the URI states; K1 `vision.format` outside the set is refused at start for this engine (R6) | none (a safety rule, not a setting) | P `test_only_data_uris_of_an_image_type_are_accepted`, `test_no_network_is_opened...`, `test_the_spy_sees_the_postscript_dispatch_when_the_format_is_not_restricted` (positive control), `test_postscript_bytes_never_dispatch_an_interpreter_under_any_media_type[4]`, `test_decoding_an_allowed_format_starts_no_process_and_creates_no_file`, `test_the_detected_format_must_match_the_declared_media_type[4]`, `test_formats_outside_the_allowed_set_are_refused_even_with_a_matching_media_type`, I `test_the_k1_image_format_must_be_one_the_server_decodes`; M3 killed | safety-bound |
| `trust_remote_code=False`, no `auto_map` | `models.load_engine`, `check_weights_config` | refused at config check | none | C/L `test_weights_config_refusals`, `test_the_engine_refuses_weights_of_another_family...` (`auto_map`) | safety-bound (call spy on `from_pretrained`: `not_run`, needs torch; the literal is fixed in code) |
| Retained literals | `/v1/embeddings` and health routes come from the profile; `float`, `HTTP/1.0`, `Server` header, exit status 2, request field names, `padding_side='right'` (Qwen script), `convert('RGB')` (processors `do_convert_rgb`) | protocol-bound | — | not user values | protocol-bound |
| `HASH_BLOCK = 1 MiB` (`manifest.py`) | read block of the streaming sha256 | bounds memory; the digest does not depend on it | — | M 3 MiB file hashed, property over one-byte changes | safety-bound (PM answer) |
| `IDLE_POLL_S = 0.5` (`server.py`) | poll period of the stop and idle checks | documented next to `server.idle_exit_s` in `docs/stages/K1-embed.md` (exit comes up to 0.5 s after the timeout) | — | H `test_idle_exit_comes_within_the_documented_poll_period` (1 s idle exits between 0.9 s and 2.0 s), `test_the_server_exits_when_idle_and_not_while_a_request_runs` | protocol-bound (PM condition met: documented and tested) |
| `server.reference_check.min_cosine` | removed in P1 (no consumer; returns in P2 with the reference tool) | none left in schema, profiles, fixtures, tests or docs (grep over tracked files finds no `reference_check`) | — | — | removed |
| ~~No per-connection read timeout~~ replaced by `server.read_timeout_s` and `server.max_pending_requests` | `vlm.backends.<n>.server.{read_timeout_s,max_pending_requests}` (fix round 1, R3) | `read_timeout_s`: number > 0, finite, accepted only if the platform socket can hold it; `max_pending_requests`: integer ≥ 1; `deploy_required` in the three example profiles | dev `embedder`, `embedder_qwen3vl2b`, full `embedder_qwen3vl8b` | C overrides `0.75`/`3` (observed in the record), refusals for `0`, `-1`, `1.5`, `True`, text, `inf`, `1e300`, `10**400`, sentinel and missing key; the schema-wide guard `test_every_schema_server_key_reaches_the_settings_record`; consumers in H: `test_the_admission_bound_is_what_the_profile_says`, `test_requests_beyond_the_admission_bound_are_refused_before_they_are_decoded` (no parse of a refused body), `test_a_stalled_upload_gets_408_and_frees_its_slot`, `test_a_trickling_upload_cannot_outlast_the_total_deadline`, `test_a_connection_that_never_finishes_its_headers_is_closed_at_the_deadline`, `test_a_reply_does_not_wait_for_a_body_that_never_comes` | configurable |
| K1 identity of the server's output settings (R2) | `families.output_fingerprint`, `config.EMBED_SERVER_IDENTITY_READS`, `k1_embed.embedder_settings` | K1 reads `server`, `family`, `dtype`, `attn_implementation`, `max_length`, `image`, `default_instruction` for this engine only (sentinel or missing refused); fingerprint = family, model type, pooling, dtype, attention, max_length, image limits, default instruction; excluded and why: host, port, routes, idle exit, byte limits, admission bound, read timeout (transport), GPU and memory cap (placement), `forward_batch` (rounding only) | none | I `test_an_output_setting_changes_the_image_and_text_identity_together[6]`, `test_the_gemma_image_budget_changes_the_identity`, `test_the_family_changes_the_identity`, `test_transport_placement_and_batching_values_keep_the_identity[10]`, `test_other_engines_keep_exactly_their_earlier_identity` (independent re-computation of the earlier formula), `test_an_embed_server_identity_is_the_earlier_one_plus_the_fingerprint`, `test_every_setting_is_classified`, `test_a_sentinel_in_an_identity_setting_is_refused_by_k1`, `test_an_output_override_makes_k1_call_the_server_again_and_a_transport_one_does_not` (HTTP double sees 0 calls, then 2) | configurable |
| Reply delivery after a refusal (R4) | `server.Handler.handle`, `_discard_unread_body`, `DRAIN_BLOCK = 64 KiB` | half-close, then discard the declared unread body (never stored), bounded by the read deadline; an unknown (chunked) length by `max_request_bytes` | none | H `test_refusals_reach_the_client_with_unread_body_bytes_pending[6 cases x 4 repeats]` (404, 405, GET 404, 411 chunked, 413 with 300 kB, 400), `test_a_reply_does_not_wait_for_a_body_that_never_comes`; the two submitted tests that failed in the review pass | `DRAIN_BLOCK`: safety-bound (implementation block, not a limit) |
| Model adapter call sites (R1 class audit) | `models.load_engine`, `TransformersEngine` | every `server.*` value observed at the library call by labelled doubles (numpy tensor, recording processor/model and `from_pretrained`): dtype, attention, `trust_remote_code=False` (both calls), `padding_side` (Qwen only), GPU index and memory fraction, image limits and the refusal when not taken, `forward_batch` chunks with order kept, pooling by family, default and item instruction, padding never pooled | none | `tests/test_embed_server_models.py` (17 tests); not run against the real libraries (P2) | configurable / safety-bound |

### Completion report (P1)

- **Behaviour:** CPU-only embedding server (`src/prompt_enhancer/embed_server/`), engine `embed_server`, config and schema, three example profiles, manifest schema and example, `requirements-gpu.txt`, docs. `models.py` (torch adapters) is written from the cards and v5.23.0 source and is **not run** (P2).
- **`reference_check`:** removed everywhere; `grep reference_check` over the tree outside the task file finds nothing (the file's own history lines remain).
- **Commands (venv `.venv`, through `test_slot.py`, `--basetemp=.tmp/T-018/bt`, no `-n`, free memory 12.4 GiB before):**
  - focused (`tests/test_embed_server_{protocol,config,manifest,http,cli}.py`, `tests/test_field_contracts.py`): `744 passed, 32 skipped in 326.00s`.
  - full `tests`: `4902 passed, 48 skipped, 4 warnings in 1819.88s (0:30:19)` (warnings: Pillow deprecation of saving I-mode PNG in `model_image.py`, existing code).
  - the CPU suite imports without torch (test in `test_embed_server_cli.py`, part of the runs above).
  - A new test `test_idle_exit_comes_within_the_documented_poll_period` was added after the focused run; it ran inside the full run.
- **Constants:** `HASH_BLOCK` safety-bound; `IDLE_POLL_S` protocol-bound, tolerance documented in `docs/stages/K1-embed.md` and tested (rows above).
- **not_run:** `ruff check` and `ruff format --check` (ruff not installed); mutants after the last edit (the 15 earlier mutants stand: 14 killed at first, M14 killed after a boundary test; the last change is a doc sentence and one added test); call spy on `from_pretrained` (needs torch); all of P2 and P3.
- **Cleanup:** `gen_manifest.py`, `mutate.py`, `mut/`, `bt/`, `full.txt` removed. Left in `.tmp/T-018/`: `p1-approve.md`, `research.md`, `stub.md`, `avif.txt` (not created by this finish; PM decides).
- **Open issues:** PM decisions pending user review are listed above.
- **Noticed, not done:** `models/README.md` still says "model manifest" without the format.

### Pending at this point (context checkpoint)

Background test run (focused set + the field-contract tests) was running; after it finishes: remove `reference_check` everywhere (schema, 3 example spots, fixtures, tests, docs), rerun the embed-server and config tests, run the full `tests/` once, fill the Completion report and Configuration audit below, delete `.tmp/T-018/gen_manifest.py`, `.tmp/T-018/mutate.py`, `.tmp/T-018/bt*` and `.tmp/T-018/mut`, reply to the PM.


## P1 fix round 1

Source: `.tmp/T-018/review-p1.md` (R1 to R7). Standing rule S31: each finding names its bug class, the whole package and its callers are audited for other members, and the class is fixed.

### Fix plan (written before the edits)

| Finding | Bug class | Audit of other members | Fix |
|---|---|---|---|
| R1 | a validated setting its consumer never observes | every `server.*`, `embedding.*`, `image.*` key the server reads is traced to a consumer-level test (recording double) or refused: `family`, `dtype`, `attn_implementation`, `forward_batch`, `max_length`, `image.*`, `default_instruction`, `max_decode_pixels`, `max_request_bytes`, `max_image_bytes`, `batch_size`, `dimension*`, `gpus`, `vram_budget_gb` | pass `max_length` to both Gemma branches (`truncation` + `max_length`; the processor expands image tokens), consumer tests with a recording processor and a fake torch, one per key not yet observed at its consumer |
| R2 | an output-affecting value outside the cache key | the K1 identity (`embedder_settings`) gets a `server` fingerprint for engine `embed_server` only; image and text revisions both carry `settings.revision`, so they stay in one space | fingerprint of family, pooling, dtype, attn implementation, max_length, image limits, default instruction (dimension mode is already in the identity). Excluded, with the reason: host, port, routes, idle exit, byte limits, read timeout, admission bound (transport only); forward batch (changes numeric noise only; tuning it must not invalidate a corpus); gpus and VRAM cap (placement) |
| R3 | unbounded work or memory | admission before the body is read; the read path; the write path | `server.max_pending_requests` (a request past its headers holds a slot through body buffering, decode, GPU wait and response) and `server.read_timeout_s` (a total deadline per connection for reading, also the send timeout of the response), both `deploy_required` in the examples; overload is 503 `overloaded` before any body is read or decoded |
| R4 | an error path that closes before the client reads | every early refusal (404, 405, 411, 413, 503 overload) | send the response, half-close the write side, discard the unread body up to its declared length within the read deadline, then close; discarded bytes are never stored |
| R5 | a validated but unusable value | `embed_server_address` | conservative choice: refuse IPv6 at validation (IPv4 loopback and `localhost` only) |
| R6 | a decoder that can start a process or touch files | Pillow plugins audited for `subprocess` and `tempfile` (EPS on load; GIF and JPEG on save only) | `Image.open(..., formats=ALLOWED)` with a safety-bound in-process raster set; the detected format must match the declared media type; spy tests |
| R7 | inconsistent artifact metadata | the third 8B shard in the manifest and in the task text | re-read the public Hub metadata of that file at the pinned revision, correct the wrong record, test the full hash |

### Findings, causes and what was found when the class was audited

- **R7 (evidence).** Public read, no project data: `GET https://huggingface.co/api/models/Qwen/Qwen3-VL-Embedding-8B/tree/2c4565515e0f265c6511776e7193b22c0968ddc7` (2026-11-16) lists shard 3 as `model-00003-of-00004.safetensors`, size 4915962496, `lfs.oid` `7fb17cf8f06d6fe5aaacf114e85c4e6d8318799f24b517f50d1ec154a8d47007`. The manifest example was right; the task-file quote (`…4d47007`) was a transcription slip and is corrected (`…8d47007`). All four shard sizes and hashes are now one test (`test_the_8b_shards_of_the_example_match_the_hub_listing_in_full`, full 64-digit hashes). The reviewer's total mismatch (13/15/19 files, 1,525,787,092 / 4,271,029,649 / 16,305,636,671 B against the research totals 1,525,810,627 / 4,271,068,726 / 16,305,675,665 B) is the same pattern: the research sums listed 12 small files and the example lists the files the loaders read; it was not changed and is not a finding of this round.
- **R6 (audit).** Installed Pillow 11.3.0, plugin sources scanned for `subprocess`, `tempfile`, `Popen`, `startfile`: EPS (on load: Ghostscript with temporary files, `EpsImagePlugin.py`), GIF (`subprocess` only in the netpbm save helper, `GifImagePlugin.py:879-915`), JPEG (`subprocess` in `_save_cjpeg`, line 857, and in `load_djpeg`, line 465, a method nothing inside Pillow calls: `grep load_djpeg PIL/*.py` finds only its definition), and ICNS (flagged by the scan for `startfile`; its context was not read, it is not allowed). Among the openable formats only EPS dispatches an interpreter on a normal load that this review or my scan could find; the spy test (`test_decoding_an_allowed_format_starts_no_process_and_creates_no_file`) observes the six allowed ones directly. The allowed set is `PNG JPEG WEBP GIF BMP TIFF`. New member found by the audit: K1 `vision.format` could name a format the server now refuses (for example `ppm`); `validate_embedder_reads` refuses it at start for the `embed_server` engine.
- **R1 (audit).** Keys traced to their consumer: `family` (pooling and chat path, `test_the_family_chooses_mean_or_last_token_pooling`), `dtype`, `attn_implementation`, `trust_remote_code`, GPU index and memory cap (loader spies), `forward_batch` (chunk sizes), `max_length` (**Gemma ignored it: fixed**; both families' text, never an image), `image.*` (processor attributes, and the refusal when not taken), `default_instruction` (system message), `max_decode_pixels`, `max_request_bytes`, `max_image_bytes`, `batch_size` (HTTP tests, unchanged), `idle_exit_s`, `dimension*` (weights check), `host/port/routes` (bind tests). A schema-level guard `test_every_schema_server_key_reaches_the_settings_record` fails when a key is validated but not carried to the settings record. Qwen's `truncation=True` also cut image inputs, which could drop image tokens: now text only, images are refused when too long (a second member of the class).
- **R2 (audit).** The identity of `embed_server` gets `server` = `families.output_fingerprint`; K1's read set gains the identity keys for this engine only (`config.EMBED_SERVER_IDENTITY_READS`), so a sentinel there is refused by K1 too. `test_every_setting_is_classified` forces every `ServerSettings` field to be named as fingerprinted, already in K1's identity or excluded.
- **R3 (audit).** Other unbounded members found: no bound on the thread count of idle connections (each now lives at most `read_timeout_s`, and holds no body), the reply send (now bounded by the same value) and the header phase (the deadline reader covers it). Stdlib per-connection traces were printed on every reset; they are now one JSON line with the exception type.
- **R4 (audit).** Every early return of `do_GET`, `do_POST` and the method refusals goes through one `handle()` tail, so no refusal path closes with unread input.
- **R5.** PM decision pending user review: IPv6 is refused (narrower, reversible); `[::1]` can be added later with an `AF_INET6` bind and its own test.

### PM decisions pending user review (fix round 1)

- IPv6 loopback is refused at validation instead of bound (R5).
- Two new profile keys, `server.max_pending_requests` and `server.read_timeout_s`, both `deploy_required` in the example profiles; `read_timeout_s` also bounds the send of the reply.
- The decoder allowlist `PNG JPEG WEBP GIF BMP TIFF` is a safety-bound constant of `protocol.py`, not a profile value, and K1 `vision.format` for this engine must be in it.
- `forward_batch` and `attn_implementation` are classified differently on purpose: `attn_implementation` is in the identity (the task names it; a kernel change moves vectors), `forward_batch` is not.
- An image that needs more tokens than `server.max_length` is refused with 500 `engine_failed` (a misconfiguration, not a client error); the real token counts per image are a P2 measurement.
- `tests/test_zz_golden_probe.py` is an emptied scratch file: the tool policy blocked deleting it; delete it.
- `server.read_timeout_s` must be accepted by the platform socket: the validator probes a throwaway socket (`settimeout`) instead of using `threading.TIMEOUT_MAX`, which my own test showed is still too large for a Windows socket (`OverflowError: timeout doesn't fit into C timeval`). A second member of "validated but unusable".

### Completion report (P1 fix round 1)

- **Behaviour:** R1 to R7 fixed at the class level, CPU only; no Git write, install, download, model call or GPU. Only network use: one public read of the Hub file listing of the pinned 8B revision (R7), no project data sent.
  - R1: Gemma text is cut at `server.max_length`; an image is never cut and one over the limit is refused; Qwen images no longer truncated; consumer-level tests for every `server.*` key (`tests/test_embed_server_models.py`) plus a schema-wide guard.
  - R2: K1 identity of `embed_server` carries `families.output_fingerprint`; other engines recomputed with the earlier formula (equal); image and text share one revision; override re-embeds (HTTP double sees 2 calls), a transport value does not (0 calls).
  - R3: `server.max_pending_requests` and `server.read_timeout_s` (both `deploy_required` in the three example profiles, refused when missing or sentinel); one total read deadline per connection; admission before the body is read; 503 `overloaded` without parsing.
  - R4: every reply is followed by half-close and a bounded discard of the unread body; raw-socket tests with 300 kB pending pass, and the two submitted tests that failed in the review pass.
  - R5: IPv6 refused at validation (PM decision pending user review); every accepted address is bound in a test.
  - R6: `Image.open(..., formats=ALLOWED_FORMATS)`, detected format must match the media type, spy tests with a positive control; K1 `vision.format` outside the set refused at start for this engine.
  - R7: the manifest example was right, the task-file quote was a slip, corrected; full-hash test.
- **Commands (venv `.venv`, through `test_slot.py`, no `-n`, `--basetemp=.tmp/T-018/bt` deleted after):**
  - memory right before the final run: free physical 12.2 GiB, commit-free 3.3 GiB (23:32:39). Commit-free was under 6 GiB, so the full `tests/` suite is **`not_run (memory)`** by the brief. (An earlier run at 1.3 GiB commit-free hit `OpenBLAS error: Memory allocation still failed`; the same test passed on rerun.)
  - focused final: `tests/test_embed_server_{cli,config,http,identity,manifest,models,protocol}.py`, all `tests/test_k1_*.py`, `test_field_contracts.py`, `test_loader.py`, `test_real_contracts.py`, `test_model_config.py`, `test_model_client.py`: `1814 passed, 33 skipped in 464.01s (0:07:44)` (log `.tmp/T-018/fix1-focused.log`). The skip count equals the review's run (33); the reasons were not re-listed. No `MemoryError` or `INTERNALERROR`.
  - the P1 full-suite result (`4902 passed, 48 skipped`) predates this round and is not a claim about the current tree.
- **Mutants (12, scratch copy of `src`, `tests`, `contracts`, `configs`; log `.tmp/T-018/fix1-mutants.log`):** all 12 killed. R1 Gemma limit dropped; R1 image limit off by one; R2 identity not extended; R2 attention left out of the fingerprint; R3 admission `>=` to `>`; R3 deadline stretched 100 times; R3 admission removed; R4 discard removed; R5 IPv6 accepted; R6 formats unrestricted; R6 media-type check off; R7 one hash digit changed.
- **`not_run`:** the full pipeline suite (memory, above); `ruff check` and `ruff format --check` (ruff is not installed in `.venv`); the real `transformers` and `torch` behaviour of the adapters (P2: processor `truncation` and `max_length` semantics for Gemma and Qwen images, image token counts); Linux and CI; the Windows reset reproduction of R4 was the reviewer's, here only the passing of the same tests plus the new raw-socket ones.
- **Changed files:** `src/prompt_enhancer/{config.py,k1_embed.py}`, `src/prompt_enhancer/embed_server/{families,models,protocol,server,settings}.py`, `contracts/schemas/enhancer_config.v0.schema.json`, `configs/enhancer.dev.example.yaml`, `configs/enhancer.example.yaml`, `docs/stages/K1-embed.md`, `tests/{embed_server_fixtures,test_embed_server_config,test_embed_server_http,test_embed_server_manifest,test_embed_server_protocol}.py`, new `tests/test_embed_server_identity.py`, `tests/test_embed_server_models.py`, this task file.
- **Cleanup:** removed `.tmp/T-018/scratch`, `bt`, `mut`, and the reviewer's `rev/` scratch except `rev/bt` (a pytest temp directory whose ACL denies access to this account; removing it needs an ACL change, which I did not make): PM cleanup. Kept `review-p1.md`, its logs, and `fix1-focused.log`, `fix1-mutants.log`. `tests/test_zz_golden_probe.py` is an emptied file I could not delete (tool policy): PM, delete it.
- **Open issues:** PM decisions pending user review are listed above; the P2 reference check still decides the unrun adapters.
- **Noticed, not done:** the reviewer's research-total versus example-total difference (a few tens of KB per model, different file subsets) is explained in "Findings" but the example was not changed; a connection cap for idle threads (each lives at most `read_timeout_s`) was not added; `models/README.md` still says "model manifest" without the format.


## P1 fix round 2

Source: `.tmp/T-018/retest1.md` (RT1, RT2, RT3; R3 "class incomplete").

### Root-cause plan (written before the edits)

Root cause of the whole class: the server never owned its connections. `ThreadingMixIn` creates one daemon thread per accepted socket, keeps no list of them, and `serve` stops by closing the listener and the engine, so what the server owns beyond the listener (accepted sockets, handler threads, the engine call in flight) has no count and no end.

| Finding | Resource | Fix |
|---|---|---|
| RT2 | handler threads and accepted sockets | the server owns a registry of live connections (socket to thread); `process_request` is overridden and takes a slot from `server.max_connections` before a thread exists; over the cap the accept thread writes one best-effort 503 on a non-blocking socket, shuts down and closes, creating no thread; refusals are counted, not logged one by one |
| RT1 | every resource, at stop | one order in `serve`: (1) `closing` set (new requests and queued forwards are refused 503 `stopping`); (2) `shutdown()` stops the accept loop, the listener closes; (3) admitted requests get up to `server.read_timeout_s` to finish; (4) every registered socket is shut down (`SHUT_RDWR`), which unblocks readers and writers; (5) every handler thread is joined within a second `read_timeout_s`; (6) the engine is closed only while holding the GPU lock (no forward runs under it), acquired within what is left of the second window; otherwise the engine is **not** closed and `serve` raises `ShutdownIncomplete` (a forward that cannot be interrupted is reported, never raced) |
| RT3 | bytes read after a refusal | one server-owned budget for known and unknown lengths: `server.max_discard_bytes` (validated, `deploy_required`, at least `max_request_bytes`). A budget equal to the request limit could never drain a body over the limit, so the 413 of an ordinary oversized body would be lost again on Windows |
| class | logs | connection and engine error lines: the first of each kind is printed, later ones counted, one summary line at shutdown |
| class | reply send | one send deadline covers header and body together |

Stop takes at most about `2 * server.read_timeout_s`. Health checks at the connection cap are refused like any connection (503 `overloaded`): true (alive, busy), no slot reserved.

### What the work found (differs from the plan where noted)

- **Windows does not wake a timeout-mode `recv` on `shutdown(SHUT_RDWR)`** (probe: `join_connections` took 4.5 s with five held sockets, until the test closed its clients). So the plan's step 4 alone is not enough: `_DeadlineReader` now receives in slices of `CANCEL_POLL_S` (0.1 s) and checks a `cut` event between slices; `cut_connections` sets the event and still shuts the sockets down (that wakes writers where the platform allows it; a send always ends at its own deadline).
- **Grace only for requests past their body read.** A stalled upload is cut at once (the stop does not wait out its deadline); only decoding, GPU-queued, in-engine and replying requests are waited for (`_State.working`). A request queued behind a running forward is refused (503 `stopping`) when it gets the lock, so no forward starts during a stop.
- **Over-cap connections:** the accept thread drops one already-buffered block (non-blocking) before the 503, so the close is not a reset in the common case; a request still arriving may see a reset (best effort, documented).
- **Short body:** a body that ends before its declared length (client left, or the stop cut the read) is 400 `incomplete_body`, never parsed as a complete one.

### PM decisions pending user review (fix round 2)

- `server.max_connections` and `server.max_discard_bytes` are two new required profile keys (`deploy_required` in the three examples). `max_connections` must be at least `max_pending_requests`; `max_discard_bytes` at least `max_request_bytes` (a budget equal to the request limit could not drain any body over it, so a 413 would be lost on Windows). Rejected narrower choice: reuse `max_request_bytes` as the drain budget (reviewer's example), which would make the 413 of an ordinary oversized body undeliverable.
- No key for the stop time: it is derived, `read_timeout_s` for the grace of working requests plus `read_timeout_s` for joining, so at most about `2 * read_timeout_s`.
- At the connection cap, `/health` is refused like any connection; no slot is reserved for it. Reversible: a reserve can be added later.
- A forward pass that cannot be interrupted inside the stop window leaves the engine open, `serve` raises `ShutdownIncomplete`, the command exits with status 3 (the process exit frees the GPU). Closing the engine under a running forward was rejected.
- Log lines: first of each kind printed, the rest counted, one `log_summary` line at stop (changes the earlier one-line-per-error behaviour; the engine-error test still sees the first line).
- `tests/test_embed_server_lifecycle.py` is a new test file (kept: it is the regression set). `tests/test_zz_golden_probe.py` is untouched, the PM strips it.

### Configuration audit (fix round 2: rows new or changed)

Tests: `L` = `tests/test_embed_server_lifecycle.py`, `C` = `_config.py`, `I` = `_identity.py`.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Class |
|---|---|---|---|---|---|
| Accepted sockets and handler threads | `vlm.backends.<n>.server.max_connections` | integer ≥ 1 (schema), at least `max_pending_requests`; consumer `_Server.process_request` checks before a thread exists | dev `embedder`, `embedder_qwen3vl2b`, full `embedder_qwen3vl8b`: `deploy_required` | C override 7, exact bound 8 (`test_the_connection_and_discard_limits_accept_their_exact_lower_bounds`); refusals `0`, `1.5`, `True`, `'many'`, `7` (below pending 8), sentinel, missing key; L cap tests at 2 and 4: exactly `cap` threads created and alive, 25 more connections get 503 with no thread (thread-creation spy), slots return after the deadline; L 40 slow connections, peak 4 | configurable |
| Bytes dropped after a refusal | `vlm.backends.<n>.server.max_discard_bytes` | integer ≥ 1, at least `max_request_bytes`; consumer `_discard_unread_body` → `drain` | same three profiles | C override 54321 and exact bound 100000; refusals `0`, `1.5`, `True`, `'all'`, `99999` (below request limit), sentinel, missing key; L `drain` dropped exactly 150000 and 250000 for a declared 10^12 bytes and for a chunked body, exactly the declared 300000 when that is smaller, property test of `drain` | configurable |
| Stop window | derived: `server.read_timeout_s` twice (grace of working requests, join of handlers); no new key | existing `read_timeout_s` validation | none | L stop with a stalled upload returns in under 5 s with `read_timeout_s` 10; a forward past the window raises `ShutdownIncomplete` in under 3 s with `read_timeout_s` 0.5 | configurable (derived) |
| `CANCEL_POLL_S = 0.1` (`server.py`) | slice of a blocked receive, so a stop or cut is noticed | none | none | L stop tests (a cut read ends within the slice, not at its 10 s deadline) | protocol-bound (implementation poll, documented in `docs/stages/K1-embed.md`) |
| Exit status 3 `INCOMPLETE` (`__main__.py`) | none | none | none | L `test_the_command_reports_an_unfinished_stop_with_its_own_exit_status` | protocol-bound |
| Reply codes `overloaded` (over-cap), `stopping`, `incomplete_body`; thread name `embed-conn` | none | none | none | L over-cap reply body, queued-request 503 `stopping`; the thread name is what L counts | protocol-bound |
| Log volume | no setting: first line of each kind, counts, one summary | none | none | L `test_a_flood_of_turned_away_connections_logs_once_and_is_summed_up_at_stop` (30 refusals, one line, count 30); H engine-error test still sees the type | safety-bound (structure, not a value) |
| K1 identity | `max_connections` and `max_discard_bytes` are transport values | `EXCLUDED` set | none | I `test_every_setting_is_classified` (forces both into the excluded set), `test_transport_placement_and_batching_values_keep_the_identity` gained both | configurable |
| `DRAIN_BLOCK` (row of P1 audit) | block size of the discard; total is now `max_discard_bytes` | none | none | unchanged | safety-bound |

Rows of the P1 audit changed by this round: `server.max_request_bytes` (413 drain: now bounded by `max_discard_bytes`, the "300 kB pending" test uses a fixture value 400000), "Reply delivery after a refusal (R4)" (budget is `max_discard_bytes`, not the declared length), "No per-connection read timeout" row (idle connections: now capped by `max_connections`).

### Completion report (P1 fix round 2)

- **Behaviour (CPU only; no Git write, install, download, network, model or GPU):**
  - RT2: the server owns a registry of accepted sockets and handler threads; `server.max_connections` is checked before a thread is created; an over-cap connection (a health probe included) gets one best-effort 503 `overloaded` from the accept thread and is closed, no thread.
  - RT1: one stop order in `stop_server`: closing flag, accept loop and listener stop, requests past their body read get up to `read_timeout_s`, blocked reads are cut (event, 0.1 s slices) and sockets shut down, handler threads joined within a second `read_timeout_s`, leftover sockets closed, then the engine is closed only while holding the GPU lock. A forward that cannot finish leaves the engine open, `ShutdownIncomplete`, exit status 3.
  - RT3: the post-refusal discard is `min(declared, server.max_discard_bytes)` and `max_discard_bytes` for a chunked body; a declared 10^12 bytes drains exactly the budget.
  - Class members found and fixed: reply send now has one window for header and body; log volume bounded (first line per kind, counts, one summary); a short body is 400 `incomplete_body`; admission refuses once the stop began.
- **Commands (venv `.venv`, through `test_slot.py`, no `-n`, `--basetemp` deleted after):**
  - lifecycle file alone, three runs during development: `15 passed in 30.46s`, `15 passed in 30.72s` after the last test fix (earlier runs found two test-design problems, below); it has 16 tests now (one unit test added after).
  - focused final (`tests/test_embed_server_*.py` incl. the new `_lifecycle.py`, all `tests/test_k1_*.py`, `test_field_contracts.py`, `test_loader.py`, `test_real_contracts.py`, `test_model_config.py`, `test_model_client.py`): **`1849 passed, 33 skipped in 447.32s (0:07:27)`**, exit 0 (log `.tmp/T-018/fix2-focused.log`). Round 1's same set was `1814 passed, 33 skipped`; the delta is the new tests.
  - the P1 full-suite result and the round 1 `not_run (memory)` for the full suite stand; the PM runs the full suite.
- **Mutants (15, scratch copy of `src`, `contracts`, `configs`, `tests`; log `.tmp/T-018/fix2-mutants.log`):** all 15 killed, and the unmutated copy passes the same selection (`31 passed`). RT2: cap `>=` to `>`, cap removed. RT1 (shutdown order and parts): engine closed before the working requests are waited for, closing flag never set, cut event not signalled, `ShutdownIncomplete` never raised, engine closed without the GPU lock, handler join skipped. RT3: declared length used as the budget, chunked budget uses the request limit, `drain` ignores its budget. Log: every line printed. Config: `max_discard_bytes` and `max_connections` bounds made exclusive (2) and the connections check removed. No mutant survived. Two tests were added before the mutant run because I judged the admit-after-closing path and the equality bounds would otherwise survive (`test_nothing_is_admitted_once_the_stop_began_and_work_is_waited_for_only_past_its_body`, `test_the_connection_and_discard_limits_accept_their_exact_lower_bounds`); no mutant on the admit-after-closing line was run, so that test is not mutation-checked.
- **Test-design problems found on the way (not server defects):** the polling POST in the queued-request test was itself admitted as a third request and hung (now a parse counter); the over-cap health probe may see a Windows reset when its request is already sent (the test accepts a reset, the contract is best effort).
- **not_run:** the full pipeline suite (the PM runs it); `ruff check` and `ruff format --check` (ruff is not installed in `.venv`); Linux and CI (the Windows behaviour that `shutdown` does not wake a blocked receive is handled by the event, the POSIX path was not run); Python 3.13+ `ThreadingMixIn` internals (the override does not use `_threads` and was only run on 3.12.10); real `transformers` and `torch`, P2 and P3.
- **Changed files:** `src/prompt_enhancer/{config.py}`, `src/prompt_enhancer/embed_server/{server,settings,families,__main__}.py`, `contracts/schemas/enhancer_config.v0.schema.json`, `configs/enhancer.dev.example.yaml`, `configs/enhancer.example.yaml`, `docs/stages/K1-embed.md`, `tests/{embed_server_fixtures,test_embed_server_config,test_embed_server_identity}.py`, new `tests/test_embed_server_lifecycle.py`, this task file.
- **Cleanup:** removed my scratch (`mut/`, `bt/`, the probe, runner and edit scripts); kept `fix2-focused.log`, `fix2-mutants.log`. Still there from before, not mine: `.tmp/T-018/rev`, `retest1/` (reviewer scratch, ACL), `tests/test_zz_golden_probe.py` (PM strips it). No process of mine is left running.
- **Open issues:** PM decisions pending user review are listed above; the P2 reference check still decides the unrun adapters; a connection or request refused at the cap while the stop runs is answered best effort only.
- **Noticed, not done:** the `serve` entry waits `window` twice in the worst case, so a `read_timeout_s` of 3600 would allow a two-hour stop; the example profiles carry `deploy_required` for it, and a value chosen with that in mind is the operator's, but a separate `stop` bound may be wanted; `models/README.md` still says "model manifest" without the format.

## P1 fix round 3

Source: `.tmp/T-018/retest2.md` (RT2F1). R-016, R-017, R-018, R-015, R-025.

### Root-cause plan (written before the edits)

Root cause (verified in `server.py`): `stop_server` ends with `if engine_open: raise`, so "complete" means one fact, the GPU lock was free to close the engine. The bounded join result `alive`, the admission counters `in_flight` and `working`, the connection registry, the accept thread and the listener are read but never judged. A handler decoding or building a reply outside the GPU lock therefore leaves stop returning success while its thread, memory and admission slot live on.

Class: "stop reports success while any server-owned resource is still live". Resources and where each is judged after the change:

| Resource | Judged by |
|---|---|
| accept thread | `join` with the window, then `is_alive` |
| listener | `server.socket.fileno() == -1` |
| handler threads | alive count after the bounded join |
| registry | sockets still registered after the leftover sockets were closed |
| `in_flight`, `working` | counters read under the guard after the join |
| engine | closed under the GPU lock, or named as open (lock not free in the window, or `close()` raised) |

`stop_server` collects survivors into one list and raises `ShutdownIncomplete` naming counts and kinds only (no request content); the command prints it and exits 3. The engine rule is kept: it is closed only while holding the GPU lock, and a decoding handler does not hold it, so the engine is still closed in that case and the stop still reports the handler. Exception, docstrings, comments and command text are generalised beyond "forward pass".

Tests: decode, reply-build and reply-send gates and a forward each give `ShutdownIncomplete` and exit status 3; unit tests with injected state for counters, registry, listener and accept thread (labelled); normal and idle paths still return with everything released. Mutants: drop the alive check, the counter check, the registry check (and the listener and accept checks).

### PM decisions pending user review (fix round 3)

- A failing `engine.close()` is now a survivor (`ShutdownIncomplete`, exit 3, the error type only) instead of an unhandled exception out of `stop_server`; conservative and reversible (before, the traceback ended the process with another status).
- The accept-thread join is bounded by `read_timeout_s` (was unbounded) so a stuck accept loop is reported rather than hung on; `server.shutdown()` itself still waits for the loop to leave.
- A failed load plus an incomplete stop raises `ShutdownIncomplete` with the load error as its context (the stop's report wins; the load error stays visible in the chain).

### Configuration audit (fix round 3: rows changed; no new setting)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Class |
|---|---|---|---|---|---|
| Stop window | unchanged: derived from `server.read_timeout_s` (grace of working requests, join of handlers, GPU-lock wait; now also the accept-thread join) | existing `read_timeout_s` validation | none | existing override and refusal tests (round 2 `C` rows); L `test_a_stop_with_a_handler_held_at_any_stage...` uses 0.25 and the unit tests 0.2, none of them the default | configurable (derived) |
| Exit status 3 `INCOMPLETE` | none | none | none | L `test_the_command_exits_with_status_3_for_a_handler_held_at_any_stage` (decode, forward, build, send) and `test_the_command_reports_an_unfinished_stop_with_its_own_exit_status` | protocol-bound |
| What counts as a complete stop (list of survivors, message wording) | none: structure of the server's own resources | none | none | L unit tests per survivor kind (counters, registry, listener, accept thread, handler join, engine close error, GPU lock) | safety-bound (structure, not a value) |
| Literals added | none beyond messages | | | | the message texts are operator output, test-only assertions on their kinds |

No unclassified literal, no hidden default, no new setting.

### Completion report (P1 fix round 3)

- **Behaviour (CPU only; no Git write, install, download, network, model or GPU):** `stop_server` no longer judges "complete" from the GPU lock alone. After the bounded waits it raises `ShutdownIncomplete` naming every survivor (kinds and counts, no request content) unless all hold: accept thread joined, listener closed, no handler thread alive, registry empty, `in_flight` and `working` zero, engine closed. A failing `engine.close()` is a survivor too (type only). The engine is still closed only under the GPU lock, so a handler decoding or replying (no lock) leaves the engine closed and is reported; a forward leaves it open and is reported. The command prints `stopped incompletely: ...` and exits 3. Exception, docstrings, comments, `INCOMPLETE` comment and `docs/stages/K1-embed.md` no longer speak only of forward passes.
- **Root cause confirmed in source:** the old tail was `if engine_open: raise` (`alive`, counters and registry were computed or readable but never judged). The retest probe `test_stop_reports_a_handler_still_decoding` now passes (`1 passed`, message `1 handler thread(s) still running; 1 accepted socket(s) still registered; 1 request(s) still admitted, 1 of them past their body read`, `ENGINE_CLOSED True`).
- **Commands (venv `.venv`, through `test_slot.py`, no `-n`, `faulthandler` on):**
  - `tests/test_embed_server_lifecycle.py`: `34 passed in 54.77s` (16 before, 18 new: decode, forward, build and send held at stop, each as `ShutdownIncomplete` and as command status 3; unit tests for nothing left, counters (1,0), (0,1), (2,2), registry, handler join, listener, accept thread, engine close error, message with several survivors).
  - focused final set (`tests/test_embed_server_*.py`, `test_k1_*.py`, `test_field_contracts.py`, `test_loader.py`, `test_real_contracts.py`): **`1666 passed, 33 skipped in 421.57s (0:07:01)`**, exit 0 (round 2's `1849` also ran `test_model_config.py` and `test_model_client.py`, which this brief's set leaves out; the 33 skips are the same count) (log `.tmp/T-018/fix3-focused.log`).
- **Mutants (10, scratch copy of `src`, `tests`, `contracts`, `configs`; log `.tmp/T-018/fix3-mutants.log`):** all 10 killed, and the unmutated copy passes the same selection (`20 passed`). Dropped: the alive check, the counter check (also each half alone), the registry check, the listener check, the accept-thread check, the engine note, the `engine.close()` error handling, and the raise itself. One mutant pass showed no output for a long time and I stopped it by hand (my own PIDs); the stalled mutant, rerun alone, was killed in 30 s; the cause of the stall is not established.
- **Class audit (resource by exit path):** normal stop and idle exit (`test_a_stop_with_nothing_left...`, round 2 stop tests: success, everything released); load failure (round 2 gated-load tests unchanged, listener and handler closed); engine error and client reset (per-request, the engine stays usable, release in `finally`, round 2 probes); stop during decode, forward, reply build, reply send (new tests); close failure (new test). A source trace, not a new test: a handler that finishes between `join` and the counter read cannot leave a counter nonzero, because `release` and `work_done` run in its `finally` before the thread ends.
- **not_run:** the full pipeline suite (the PM runs it); `ruff check` and `ruff format --check` (ruff is not installed in `.venv`); Linux and CI; real `torch` and `transformers`, P2 and P3.
- **Changed files:** `src/prompt_enhancer/embed_server/{server,__main__}.py`, `docs/stages/K1-embed.md`, `tests/test_embed_server_lifecycle.py`, this task file.
- **Cleanup:** removed `.tmp/T-018/fix3/` scratch tree contents except the logs `.tmp/T-018/fix3-focused.log`, `.tmp/T-018/fix3-mutants.log` (the folder keeps `README.txt` and `mutants.py` as the runner of the mutants cited). Not mine and left alone: `.tmp/T-018/rev`, `retest1`, `retest2`, `tests/test_zz_golden_probe.py`. No process of mine is left running.
- **Open issues:** PM decisions pending user review are listed above (close failure as survivor, bounded accept join, load error chained under the stop report).
- **Noticed, not done:** `server.shutdown()` still waits for the accept loop with no bound (a stuck accept loop would hang there, before the report); the `serve` entry can wait `window` twice, as noted in round 2.

## P1 fix round 4

Source: `.tmp/T-018/retest3.md` (RT3F1, P2). R-016, R-017, R-018, R-015, R-025. Small: yes (Step 0 and the audit first, then the build without waiting).

### Step 0

- **Unknowns:** whether a daemon writer blocked inside `sys.stdout` can break the process exit (CPython takes the `BufferedWriter` lock at finalization with a 1 s grace and then aborts: `Modules/_io/bufferedio.c` `_enter_buffered_busy`, read from memory, not from installed source: **assumption**, avoided by design below); whether `accept()` after `select` can block (yes on a blocking listener when the pending connection vanished; `socketserver.py:305-311` only "assumes" it cannot).
- **Questions (proposed answers, none blocking):** (1) Is output that cannot be delivered a survivor (status 3)? Proposed: no, it holds no port, thread slot, admission or GPU; the stop returns, the lines are counted as dropped. (2) A new profile key for the output queue? Proposed: no, a safety-bound constant (memory bound of an implementation buffer, like `DRAIN_BLOCK`). (3) Bound for `engine.close()`? Proposed: its own `read_timeout_s` window on a closer thread, a late close is a survivor. (4) Bound for the CLI's exit lines? Proposed: the same writer, drained within `read_timeout_s`. (5) The configuration refusal before any settings exist? Proposed: unchanged direct `print` to stderr (nothing is owned yet, no window exists), recorded below.
- **Plan:** (a) audit table below; (b) `_Output` bounded non-blocking writer for every server and command line, own accept loop with a stop event and polled `select`, listener timeout, bounded engine close; (c) protective tests: real full pipe, stuck accept loop, blocked summary write, CLI exit 3 under a full stderr pipe in a subprocess, overflow counter; (d) mutants; (e) focused set, report.
- **Done check:** every row of the audit has a bound after the change; the converted retest-3 probe returns within the window plus tolerance; the four required mutants are killed; the focused set passes.

### Class audit: calls that can block on the accept thread, a refusal path, `stop_server` and the command's exit

Before = the tree at the start of this round (`server.py`, `__main__.py`); stdlib = installed Python 3.12.10 `Lib/`. `W` = `server.read_timeout_s`.

| # | Call | Thread | Can wait on | Bound before (file:line) | Bound after |
|---|---|---|---|---|---|
| 1 | `_Log.note` → `print(..., flush=True)` (first refusal, `connection_error`, `engine_error`) | accept (refusal, `handle_error`), handler (engine error, connection error) | the stdout reader (full pipe, paused console) | none (`server.py:86`); RT3F1 | `_Output.emit`: O(1) under a lock never held across I/O; a full queue (`OUTPUT_QUEUE_LINES`) counts the line as dropped; the write happens on the daemon `embed-output` thread |
| 2 | `_Log.summary` → `print(..., flush=True)` | stop (`stop_server`) | the stdout reader | none (`server.py:92`) | `emit` with one reserved slot (the summary carries `dropped_lines`); then `_Output.close(W)` waits at most `W` for delivery and returns |
| 3 | `announce` (default `print`) of the ready line, later flushed at interpreter exit | main (start), main (exit flush of the buffered line) | the stdout reader | none (`server.py:474,489`; exit flush) | default `announce` is the same `_Output`; nothing of the server is left in `sys.stdout`'s buffer at exit |
| 4 | `print('stopped incompletely ...' / 'refused ...', file=sys.stderr)` after `serve` | main (command exit) | the stderr reader | none (`__main__.py:53,56`) | `_Output(sys.stderr)`, `close(W)`; the status (2, 3) is returned after at most `W` |
| 5 | writer blocked in `sys.stdout` at interpreter finalization | `embed-output` vs main | `BufferedWriter` lock held by the blocked writer (CPython source mechanism: assumption above; experiment: mutant M9, writing through the buffered stream, made the `complete` subprocess case not exit within 60 s) | n/a (no writer before) | the writer writes to `stream.buffer.raw` (no `BufferedWriter` lock), daemon thread; an in-memory stream (`StringIO`, pytest capture) is written directly and cannot block |
| 6 | `server.shutdown()` → `__is_shut_down.wait()` | stop | the accept loop leaving (any blocking call in it) | none (`server.py:437`, stdlib `socketserver.py:254-255`) | own loop: `stop_accepting()` sets an event, never waits; `accept_thread.join(W)`, a live thread is the survivor `the accept thread is still running`; `shutdown()` is overridden to the same non-waiting call |
| 7 | `selector.select(poll)` in `serve_forever` | accept | poll period | 0.5 s (stdlib `socketserver.py:218,235`) | `CANCEL_POLL_S` (0.1 s); an `OSError` from a listener closed under it ends the loop when stopping |
| 8 | `get_request` → `socket.accept()` after `select` | accept | a pending connection that vanished before `accept` (blocking listener) | none (stdlib `socketserver.py:305-313,505-511`) | listener timeout `CANCEL_POLL_S`; a `TimeoutError` is an `OSError`, which `_handle_request_noblock` already drops; accepted sockets are forced blocking by `socket.accept` (stdlib `socket.py:297-301`) |
| 9 | `process_request` registry lock `_Server._lock` | accept | handlers forgetting, stop reading the registry | O(1) dict work, never held across I/O (`server.py:329-340,356-358,387,398,406,413`) | unchanged |
| 10 | `Thread.start()` → `self._started.wait()` (no timeout) | accept (handler thread), first `emit` (writer), stop (closer) | the OS starting the thread (no peer) | stdlib `threading.py:975-999`; OS-bound | unchanged: no wait on any peer; failure raises and is cleaned up (`server.py:342-345`) |
| 11 | `_turn_away`: `recv`, `send` | accept | the client | non-blocking socket (`server.py:367-373`) | unchanged |
| 12 | `shutdown_request` → `shutdown(SHUT_WR)`, `close()` | accept, handler `finally`, stop (`close_connections`) | nothing (no `SO_LINGER` set; close returns at once) | stdlib `socketserver.py:513-526` | unchanged |
| 13 | uncaught error in the accept loop → `threading.excepthook` → stderr | accept | the stderr reader | none (stdlib loop, no catch) | own loop catches `Exception`, notes `accept_error` through `_Output`, ends |
| 14 | `state.begin_closing`, `admit`, `release`, `work_*`, `counts` (`guard`) | stop, handlers | O(1) under the condition | `server.py:113-153` | unchanged |
| 15 | `state.wait_work_done(W)` | stop | working requests | `W` (`server.py:440`) | unchanged |
| 16 | `cut_connections`: event, `shutdown(SHUT_RDWR)` | stop | nothing | `server.py:383-393` | unchanged |
| 17 | `join_connections(W)` | stop | handler threads | `W` total (`server.py:395-402`) | unchanged |
| 18 | `server_close()` → `socket.close()`, `_NoThreads.join()` | stop | nothing (our `process_request` never fills `_threads`) | stdlib `socketserver.py:489-495,713-715` | unchanged, now after the bounded accept join |
| 19 | `state.gpu.acquire(timeout=...)` | stop | a running forward | rest of the second window (`server.py:447`) | unchanged |
| 20 | `state.engine.close()` (real: `gc.collect()`, `torch.cuda.empty_cache()`, `models.py:40-43`) | stop | the collector, the CUDA driver | none (`server.py:449`) | runs on a daemon `embed-close` thread holding the GPU lock, joined within `W`; still running → survivor `the engine close did not finish within W s`, engine and lock kept (no forward can start: `closing` is checked under the lock); the engine is already used from handler threads (`models.py:102-103`), so another thread is no new condition |
| 21 | handler refusal `_fail` → `_send` | handler | the client | one send deadline `W` (`server.py:195-203`) | unchanged |
| 22 | `_discard_unread_body` → `shutdown(SHUT_WR)`, `drain` | handler | the client | read deadline and `max_discard_bytes` (`server.py:293-309`) | unchanged |
| 23 | stdlib `send_error` (bad request line) | handler | the client | socket timeout last set by `_DeadlineReader` (≤ 0.1 s slice) | unchanged |
| 24 | `handle_error` | accept, handler | as row 1 | as row 1 | as row 1 |
| 25 | `print('refused: ...')` for a configuration refusal | main, before any server-owned resource | the stderr reader | none (`__main__.py:43`) | unchanged (owns nothing, no window exists yet): **not bounded, outside the class; PM decision pending user review** |
| 26 | interpreter exit: `threading._shutdown` joins non-daemon threads | main | non-daemon threads | every server thread is daemon (`embed-http`, `embed-conn`, new `embed-output`, `embed-close`) | unchanged; no non-daemon thread added |

Worst case of a stop after the change: accept join `W` + work grace `W` + handler join and GPU lock `W` + engine close `W` + output delivery `W` = `5 W` plus scheduling (in practice the accept join ends within `CANCEL_POLL_S` and the close and the delivery take what they take). The command adds at most `W` for its own exit line.

### Configuration impact (round 4, before the edits)

No new profile key. Derived windows grow from two uses of `server.read_timeout_s` to five (accept join, grace, join and lock, close, output delivery) plus one in the command. New literals: `OUTPUT_QUEUE_LINES` (safety-bound: memory of lines waiting for a slow console), thread names `embed-output` and `embed-close` (protocol-bound, like `embed-conn`); `CANCEL_POLL_S` gains two consumers (accept poll, listener timeout), still protocol-bound.

### Configuration audit (round 4: rows new or changed; no new setting)

Tests: `L` = `tests/test_embed_server_lifecycle.py`, `H` = `tests/test_embed_server_http.py`.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Class |
|---|---|---|---|---|---|
| Stop window (derived) | `vlm.backends.<n>.server.read_timeout_s`, now five uses in `stop_server` (accept join, grace, join and GPU lock, engine close, output) and one in the command (`_last_line`) | unchanged (round 1 and 2 `C` rows) | unchanged, `deploy_required` | unchanged override and refusal tests; consumers observed at 0.2, 0.25 and 0.5 in L (`stop_bound` = `5 W` + 1 s slack), never the example value | configurable (derived) |
| `OUTPUT_QUEUE_LINES = 64` (`server.py`) | none | none | none | L `test_lines_beyond_the_output_queue_are_dropped_counted_and_reported_in_the_summary` (74 kinds, written + dropped = 74, summary still delivered) | safety-bound (memory of an implementation buffer; first lines are one per kind, so 64 is generous) |
| `CANCEL_POLL_S` (accept poll, listener accept timeout) | none | none | none | L stuck-accept test, `test_an_accept_with_nothing_pending_gives_up_within_one_slice` | protocol-bound (implementation poll) |
| Thread names `embed-output`, `embed-close` | none | none | none | L `output_threads()` counts the writer | protocol-bound |
| Undelivered output is not a survivor; `dropped_lines` key of `log_summary` | none: structure | none | none | L full-pipe tests return `{'reason': 'stop'}`; overflow test reads `dropped_lines` | safety-bound (structure); PM decision pending user review |
| Command exit line bound | `server.read_timeout_s` (settings already loaded) | unchanged | unchanged | L `test_the_command_exits_with_its_status_while_stdout_and_stderr_are_full_pipes[complete-0, incomplete-3]` (real subprocess, both pipes full) | configurable (derived) |
| Configuration refusal line (`__main__.py:43`) | none (no settings yet) | n/a | n/a | unchanged | not bounded; outside the class (nothing owned); PM decision pending user review |

No hidden default, no unclassified literal; `SLACK_S = 1.0` in L is test-only.

### PM decisions pending user review (fix round 4)

- Output that cannot be delivered (a console or pipe nobody reads) is not a survivor: the stop returns, exit 0 when everything else is released; lost lines are counted (`dropped_lines`). Reversible: it could become a survivor or a stderr note.
- `OUTPUT_QUEUE_LINES = 64` is a safety-bound constant, not a profile key.
- `engine.close()` gets its own `read_timeout_s` on a daemon thread; the worst-case stop grows from about `2 W` to `5 W` (documented in `stop_server` and `docs/stages/K1-embed.md`). A separate stop-budget key was not added.
- The configuration refusal line before any settings exist keeps a direct `print` to stderr (audit row 25): nothing is owned, no window exists.
- Server output is written as raw bytes with `\n` line ends (was text-mode `print`, `\r\n` on Windows pipes); no consumer in `src/` reads the ready line (grep `'ready'`).

### Completion report (P1 fix round 4)

- **Behaviour (CPU only; no Git write, install, download, network, model or GPU):** every row of the class audit above has a bound except row 25 (recorded). (1) All server and command output goes through `BoundedOutput`: `emit` queues and returns, one daemon `embed-output` thread writes to the stream's raw file, a full queue or failed write counts the line, the stop summary has a reserved slot and reports `dropped_lines`, and the stop waits at most `W` for delivery. (2) The server owns its accept loop: `stop_accepting()`/`shutdown()` set an event and never wait, the loop polls every `CANCEL_POLL_S`, the listener has a `CANCEL_POLL_S` accept timeout, the loop catches its own errors (logged, not printed); `stop_server` joins the accept thread within `W`, then closes the listener whether or not it ended, and reports a live accept thread. (3) `engine.close()` runs on a daemon `embed-close` thread holding the GPU lock and is joined within `W`; a late close is a survivor. (4) The command's last line (`refused` after start, `stopped incompletely`) is written within `W`, then the status (2, 3) is returned. Round 2 and 3 guarantees kept: cap check before a thread exists, 0.1 s read slices, stop order, survivor list, engine never closed under a running forward, exit status 3 (all their tests pass unchanged).
- **Tests added (L):** `test_a_refusal_logged_into_a_full_pipe_does_not_hold_the_accept_loop_or_the_stop` (the retest-3 probe made protective: real full OS pipe as stdout, the refusal's 503 arrives, stop returns `{'reason': 'stop'}` within `5 W` + 1 s, the line arrives once the pipe is read); `test_a_stop_summary_that_cannot_be_written_does_not_hold_the_stop`; `test_lines_beyond_the_output_queue_are_dropped_counted_and_reported_in_the_summary`; `test_a_stuck_accept_loop_is_reported_within_the_window_and_the_listener_still_closes` (real accept thread blocked in `process_request` by a labelled gate); `test_an_engine_close_that_does_not_finish_is_reported_within_the_window`; `test_an_accept_with_nothing_pending_gives_up_within_one_slice`; `test_the_command_exits_with_its_status_while_stdout_and_stderr_are_full_pipes[complete-0, incomplete-3]` (real subprocess, `-X faulthandler`, `CREATE_NO_WINDOW`, both pipes full and never read while it runs; it records stop-to-return time).
- **Tests changed:** L `test_the_command_reports_an_unfinished_stop_with_its_own_exit_status` passes `SimpleNamespace(read_timeout_s=1)` instead of `object()` (the command now reads the window). H `test_idle_exit_comes_within_the_documented_poll_period`: the start mark moved before `running_server`. **A wrong test, reported:** it measured from after the readiness probe, later than the idle clock's origin, and its lower bound held only because the stdlib loop's 0.5 s shutdown poll slowed the stop (observed `0.891 < 0.9` after the fix); idle tests then passed 3 of 3 runs.
- **Commands (venv `.venv`, `test_slot.py`, no `-n`, `-X faulthandler`, `--basetemp=.tmp/T-018/fix4/bt`, deleted after):**
  - new tests alone: `11 passed, 29 deselected in 15.81s`; then `2 passed, 40 deselected in 1.99s` for the two added later.
  - lifecycle + HTTP + CLI: `1 failed, 84 passed in 102.60s` (the idle test above), after the fix `3 passed, 34 deselected` three times for `-k idle`.
  - focused set (`tests/test_embed_server_*.py`, `tests/test_k1_*.py`, `test_field_contracts.py`, `test_loader.py`, `test_real_contracts.py`): **`1674 passed, 33 skipped in 405.24s (0:06:45)`**, exit 0 (log `.tmp/T-018/fix4-focused.log`); round 3 had `1666 passed, 33 skipped`, the delta is the 8 new tests.
- **Mutants (10, scratch copy of `src/prompt_enhancer` only, tests run from the worktree; runner `.tmp/T-018/fix4/mutants.py`, log `.tmp/T-018/fix4-mutants.log`):** unmutated control `19 passed`; all 10 killed: M1 blocking `print` in the log note (the converted probe); M2 stdlib loop and its waiting `shutdown()` restored (stuck-accept test); M3a output close unbounded, M3b summary by blocking `print` (full-pipe tests); M4 dropped counter removed, M5 summary without its reserved slot (overflow test); M6 close joined without a bound (close test); M7 listener without timeout (accept test); M8 command line by blocking `print` (`incomplete-3` subprocess: no exit within 60 s); M9 writer through the buffered stream (`complete-0` subprocess: no exit within 60 s, evidence for audit row 5).
- **not_run:** `ruff check` and `ruff format --check` (`No module named ruff` in `.venv`); full pipeline suite (the PM runs it); Linux and CI (POSIX `select` on a closed listener and the `PollSelector` path not run here); real `torch` close on another thread (P2).
- **Changed files:** `src/prompt_enhancer/embed_server/{server,__main__}.py`, `tests/test_embed_server_lifecycle.py`, `tests/test_embed_server_http.py` (one test's start mark), `docs/stages/K1-embed.md`, this task file.
- **Cleanup:** removed `.tmp/T-018/fix4/bt`, `mut` and my before-copies; kept `.tmp/T-018/fix4/mutants.py`, `fix4-focused.log`, `fix4-mutants.log`. Reviewer folders and `tests/test_zz_golden_probe.py` untouched. Every child process of the tests was waited for or killed by the test; no process of mine is left running.
- **Noticed, not done:** the configuration-refusal `print` (row 25); `announce` on the start path is now non-blocking, but `serve` still does not notice an accept loop that ended on its own error (it is logged as `accept_error`, the main loop keeps waiting until stop or idle); `tests/test_embed_server_lifecycle.py` shows as untracked (`??`) in `git status`, so the PM must stage it.

## P1 fix round 5

Source: `.tmp/T-018/retest4.md` (RT4F1, RT4F2, P2; its audit lines 46 to 91). R-016, R-017, R-018, R-015, R-025. Small: yes (Step 0 and the audit first, then the build without waiting).

### Step 0

- **Unknowns:** whether `socket.close()` can raise on a valid descriptor (CPython `sock_close` raises `OSError` for a failed close other than `ECONNRESET`; native source not installed: **assumption**, treated as possible); whether `Thread.start` can fail after the OS thread exists (installed `threading.py:990-999`: `_start_new_thread` raises before the thread runs; `_started.wait()` can only be interrupted by a `BaseException`: source-verified); whether settings validation owns anything (one probe socket in a `with` block, `config.py:1061-1065`, closed before any refusal: source-verified).
- **Questions (proposed answers, none blocking):** (1) Exit status of an unexpected post-settings exception? Proposed: new protocol-bound `FAILED = 1` (Python's own status for an uncaught exception, so a supervisor sees the same number), one line `failed: <Type>`; PM decision pending user review. (2) Closer start failure: survivor note or synchronous `close()`? Brief: survivor, no synchronous fallback. (3) Does `BoundedOutput.close` return False after a failed start? Proposed: no, `close` means "nothing left waiting"; a lost line is counted in `dropped`, as a failed write already is. (4) `KeyboardInterrupt` after settings? Proposed: the same catch-all (the signal handlers normally turn SIGINT into the stop event; this covers the instant before they are installed). (5) Pre-settings paths (argparse, configuration refusal, an unexpected error inside settings loading): Python's synchronous stderr, the accepted exception, PM decision pending user review.
- **Plan:** (a) exception audit below; (b) contain the closer start, the writer start, `server_close`, `shutdown_request`, an unstarted registered handler in `join_connections`, the accept-thread start in `serve`; command catch-all to `FAILED`; `_last_line` never raises; (c) protective tests from the four retest-4 probes, the unexpected-exception status, and a pre-settings no-thread/no-socket test; (d) mutants; (e) focused set, report.
- **Done check:** every row of the audit is contained after the change or is the documented pre-settings exception; the converted probes and new tests pass; the three required mutants are killed; the focused set passes.

### Exception audit: what can raise after settings exist, and what happens then

Extends the round 4 table (its rows keep their wait bounds; this table adds "can raise"). Before = the tree at the start of this round; `S` = `server.py`, `C` = `__main__.py`, stdlib = installed Python 3.12.10 `Lib/`. "Contained": bookkeeping still runs, the resource is reported, the command's status is its own, and no synchronous unbounded write follows.

| # | Call | Thread | Can raise, and what happened before | After |
|---|---|---|---|---|
| E1 | accept `Thread.start` (`serve`, S:651-652) | main | `RuntimeError` (OS refuses), `MemoryError`; raised before the `try`, so the listener stayed open and the error reached Python's traceback printing | listener closed (`server_close`), re-raised; the command maps it to `FAILED` (E19) |
| E2 | handler `Thread.start` (`process_request`, S:472-481) | accept | `RuntimeError`; `_forget` and `shutdown_request` run, re-raised to stdlib `_handle_request_noblock` (`socketserver.py:316-324`), which calls `handle_error` (type-only note) and `shutdown_request` again (a second close is a no-op) | unchanged: contained |
| E3 | closer `Thread.start` (`_close_engine`, S:575-580) | stop | `RuntimeError`: GPU lock released, re-raised, skipping summary, output close and survivor checks (RT4F1) | survivor note `the engine was not closed: its closing thread could not start (<Type>)` after the lock is released; the stop continues; `ShutdownIncomplete`, status 3; no synchronous `close()` |
| E4 | writer `Thread.start` (`BoundedOutput.emit`, S:113-117) | any caller of `emit` | `RuntimeError` escaped `emit`; the writer reference stayed set, so queued and later lines stayed queued and `close` waited its whole window; in `_last_line` it replaced status 3 and reached the traceback printing (RT4F2) | `_start_writer` catches `Exception`: under the condition the output is marked failed, queued lines are counted as dropped and cleared, waiters woken, `emit` returns False; later lines are counted as dropped; nothing raised |
| E5 | `threading.Thread(...)` construction of the writer | as E4 | `MemoryError` only, inside the condition, escaped `emit` | moved into `_start_writer`'s `try`: as E4 |
| E6 | selector construction, `register`, `select` (S:437-452) | accept | `OSError`, `ValueError`: caught by the loop's `except Exception`, noted `accept_error`, loop ends; the stop joins it | unchanged: contained (a loop ended by its own error is not surfaced to `serve`: round 4 `Noticed`) |
| E7 | listener `accept` (`get_request`) | accept | `OSError` (incl. `TimeoutError`), dropped by stdlib `_handle_request_noblock` | unchanged |
| E8 | `_turn_away` `setblocking`, `recv`, `send` (S:502-511) | accept | `OSError`, caught | unchanged |
| E9 | `shutdown_request` = `shutdown(SHUT_WR)` (guarded by stdlib) + `close()` (not guarded) (`socketserver.py:513-526`) | accept, handler `finally`, stop | `close()` `OSError` (assumption above): in a handler `finally` it skipped `_forget` and reached `threading.excepthook` (a synchronous stderr traceback from that thread); in `close_connections` it skipped the remaining sockets and the rest of the stop | `_Server.shutdown_request` overridden with both calls guarded; `_serve_connection` forgets in its own `finally`; a socket whose close failed is still judged by the registry and listener checks |
| E10 | `server_close()` = listener `close()` + `_NoThreads.join()` (S:616) | stop | `OSError` from the close skipped the whole rest of the stop | guarded in `stop_server`; the listener check (`fileno() != -1`) judges it |
| E11 | `cut_connections` `shutdown(SHUT_RDWR)` (S:526-530) | stop | `OSError`, caught | unchanged |
| E12 | `join_connections` `thread.join` (S:537-538) | stop | `RuntimeError` "cannot join thread before it is started" for a thread the accept loop registered and has not started yet (the accept thread still inside E2's start, i.e. its join timed out): skipped the rest of the stop | caught per thread; such a thread is not alive, its socket is still registered and the registry check reports it |
| E13 | `state.gpu.acquire(timeout=...)`, `release` (S:562, 573, 579) | stop, closer | none for `Exception` (timeout clamped at 0; released only by its holder) | unchanged |
| E14 | `engine.close()` (S:568) | closer | any `Exception`: caught, type-only survivor | unchanged |
| E15 | raw `write`, `encode` (`BoundedOutput._write`, S:154-160) | writer | `OSError`, `LookupError`, `ValueError`: inside `_run`'s `try`, counted as dropped | unchanged |
| E16 | fallback `stream.write`/`flush` (S:150-153) | writer | any `Exception` (also `sys.stdout is None` under `pythonw`: `AttributeError`): counted. **Can block**: the fallback serves any stream without `buffer.raw` (pytest capture, `StringIO`, a replaced or wrapped `sys.stdout`/`sys.stderr`); the round 4 wording "in-memory, cannot block" was too strong | behaviour unchanged; wording qualified in the class docstring: only for a stream without `buffer.raw`, it can block the writer thread (never the caller) and a buffered one can hold that stream's lock at exit; production `sys.stdout`/`sys.stderr` on a console or a pipe take the raw path |
| E17 | locks and conditions (`_Log._lock`, `_State.guard`, `_Server._lock`, `BoundedOutput._cond`; `wait_for`, `notify_all`) | all | no `Exception` (always owned before wait and notify; only a `BaseException` from a signal in the main thread) | unchanged |
| E18 | daemon thread top levels (accept `serve_forever`, handler `_serve_connection`, closer `close`, writer `_run`) | daemon | an escaping exception goes to `threading.excepthook`, a synchronous stderr write from that thread; only E9 could escape (handler `finally`) | E9 fixed; every top level catches `Exception` |
| E19 | **missing row 1:** an exception out of `serve` other than `ManifestError`, `ServerStartError`, `OSError`, `ShutdownIncomplete` (E1, a `load` error of another type such as a library `RuntimeError`, a defect), or from the signal set-up | main | Python's default traceback printing: a synchronous, unbounded stderr write after settings and resources existed; status 1 by Python's rule, not the command's | `main` catches `Exception` and `KeyboardInterrupt` once settings exist: one line `failed: <Type>` through `_last_line` (bounded by `W`), status `FAILED = 1`; no message text, no traceback |
| E20 | `_last_line` itself (`BoundedOutput`, `emit`, `close`) (C:60-67) | main | E4 replaced the status | never raises (E4, plus its own `except Exception`); returns `status` after at most `W` |
| E21 | **missing row 2:** `argparse` (`parse_args`: usage, `--help`, errors, then `SystemExit`) (C:34-40) | main, before settings | synchronous stdout/stderr write, unbounded under backpressure | unchanged: **pre-settings exception** (no thread, listener, engine or window exists); test: no thread started, no socket left open |
| E22 | configuration refusal `print` (C:43-45), and any other exception inside `load_server_settings` | main, before settings | synchronous stderr write; the probe socket of `config.py:1062` is closed by its `with` before the refusal | unchanged: same pre-settings exception (round 4 row 25); test as E21 |
| E23 | interpreter exit after `main` returns | main | the daemon writer may be blocked in a raw write; nothing is buffered in `sys.stderr`'s text layer after settings | unchanged (round 4 row 5) |

Accepted exception (PM decision pending user review; the reviewer agrees for the configuration refusal): E21 and E22, the only paths that write synchronously, both before any settings exist: nothing is owned and no window is configured.

### Configuration impact (round 5, before the edits)

No new profile key and no new derived window. New literal: `FAILED = 1` in `__main__.py` (protocol-bound exit status); survivor message text of E3 (operator output). The windows of round 4 are unchanged; E19 adds no wait beyond `_last_line`'s one `W`.

### Configuration audit (round 5: rows new or changed; no new setting)

Tests: `L` = `tests/test_embed_server_lifecycle.py`.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Class |
|---|---|---|---|---|---|
| Stop window and command last-line window (derived) | `vlm.backends.<n>.server.read_timeout_s`, unchanged consumers; the `failed:` line uses the same `_last_line` window | unchanged (round 1 and 2 `C` rows) | unchanged, `deploy_required` | unchanged override and refusal tests; new consumers observed at 0.2 and 0.5 in L (`test_an_engine_closer_that_cannot_start...`, `test_a_failure_path_keeps_its_status...[closer-3, writer-3, unexpected-1]`), never the example value | configurable (derived) |
| Exit status `FAILED = 1` (`__main__.py`) | none | none | none | L `test_an_unexpected_error_once_the_profile_is_read_is_one_type_line_and_status_1[LabelledUnexpected, KeyboardInterrupt]`, `test_a_failure_path_...[unexpected-1]` | protocol-bound (Python's own uncaught-exception status); PM decision pending user review |
| Exit status 3 on a refused closer, kept on a refused writer | none | none | none | L `test_a_failure_path_...[closer-3, writer-3]`, `test_the_last_line_keeps_its_status_when_its_writer_cannot_start` | protocol-bound (unchanged value) |
| Survivor note of a refused closer, `dropped` on a refused writer | none: structure | none | none | L `test_an_engine_closer_that_cannot_start...`, `test_an_output_writer_that_cannot_start_counts_every_line_and_never_raises` | safety-bound (structure; type only in the message) |
| Pre-settings direct writes (argparse, configuration refusal) | none (no settings yet) | n/a | n/a | L `test_nothing_is_started_or_left_open_before_the_profile_is_read[argparse, configuration]` (no thread started, no socket left open) | not bounded; outside the class (nothing owned); PM decision pending user review |

No hidden default, no unclassified literal; the labelled doubles (`refuse_thread_start`, `Unclosable`, `LabelledUnexpected`, the harness `start`) are test-only.

### PM decisions pending user review (fix round 5)

- An unexpected error after the profile is read exits with a new status `FAILED = 1` and one line `failed: <Type>` (no message, no trace). Before, it was Python's traceback with the same number 1, written synchronously. Reversible: it could map to 2 or 3, or print the message.
- `KeyboardInterrupt` after the profile is read is caught the same way (the signal handlers normally turn SIGINT into a clean stop; this covers the instant before they are installed).
- Pre-settings writes (argparse usage, help and errors; the configuration refusal; an unexpected error inside settings loading) stay direct and unbounded: nothing is owned and no window exists (the reviewer agrees for the configuration refusal).
- `BoundedOutput.close` returns True after a refused writer ("nothing left waiting"); the lost lines are in `dropped`, as for a failed write.

### Completion report (P1 fix round 5)

- **Behaviour (CPU only; no Git write, install, download, network, model or GPU):** every row of the exception audit above is contained, except the documented pre-settings exception (E21, E22). (1) RT4F1: a closer the OS refuses gives back the GPU lock and becomes the survivor `the engine was not closed: its closing thread could not start (RuntimeError)`; the stop continues through the summary, output close and survivor checks and raises `ShutdownIncomplete` (status 3); no synchronous `close()`. (2) RT4F2: `BoundedOutput._start_writer` contains a refused writer: under the condition the output is marked failed, queued lines (also those another caller queued meanwhile) are counted as dropped and cleared, waiters are woken, `emit` returns False and never raises, later lines are counted; `_last_line` returns its status whatever happens. (3) Once the profile is read, `main` maps any other `Exception` or `KeyboardInterrupt` to one line `failed: <Type>` through `_last_line` (bounded by `W`) and `FAILED = 1`. (4) Raise points found by the audit and fixed with it: the accept-thread start in `serve` closes the listener before re-raising (E1); `_Server.shutdown_request` guards the `close` too, and a handler forgets its socket in its own `finally` (E9); `stop_server` guards `server_close` (E10); `join_connections` survives a registered handler not started yet (E12). (5) The fallback `stream.write` wording is qualified in the `BoundedOutput` docstring (E16). (6) Pre-settings paths are documented in the `__main__` docstring and `docs/stages/K1-embed.md`, and tested to start no thread and leave no socket open.
- **Tests added (L, 14 cases):** `test_an_engine_closer_that_cannot_start_is_reported_and_the_stop_still_completes`, `test_an_output_writer_that_cannot_start_counts_every_line_and_never_raises`, `test_the_last_line_keeps_its_status_when_its_writer_cannot_start` (retest-4 direct probes made protective); `test_a_failure_path_keeps_its_status_and_exits_while_stdout_and_stderr_are_full_pipes[closer-3, writer-3, unexpected-1]` (retest-4 CLI probes made protective plus the unexpected path: real subprocess, `-X faulthandler`, `CREATE_NO_WINDOW`, both pipes full, failure-to-return under `5 W + 1 s + W`, child killed and waited on timeout); `test_an_unexpected_error_once_the_profile_is_read_is_one_type_line_and_status_1[LabelledUnexpected, KeyboardInterrupt]` (stderr is exactly the one type line); `test_nothing_is_started_or_left_open_before_the_profile_is_read[argparse, configuration]`; `test_an_accept_thread_that_cannot_start_leaves_no_listener`; `test_a_listener_close_that_fails_is_reported_and_the_stop_still_runs`; `test_a_handler_registered_but_not_started_yet_is_reported_not_raised`; `test_a_socket_whose_close_fails_is_reported_and_does_not_end_the_stop`. Thread-start refusals, the socket double and the unexpected error are labelled doubles; pipes, server, accept loop and stop are real.
- **Commands (venv `.venv`, `test_slot.py`, no `-n`, `-X faulthandler`, `--basetemp=.tmp/T-018/fix5/bt`, deleted after):**
  - new tests alone: `14 passed, 42 deselected in 8.34s`.
  - focused set (`tests/test_embed_server_*.py`, `tests/test_k1_*.py`, `test_field_contracts.py`, `test_loader.py`, `test_real_contracts.py`): **`1688 passed, 33 skipped in 385.40s (0:06:25)`**, exit 0 (log `.tmp/T-018/fix5-focused.log`); round 4 had `1674 passed, 33 skipped`, the delta is the 14 new cases. Property and configuration tests are in that set, unchanged.
  - after the M6 test revision below: lifecycle + CLI + HTTP `101 passed in 106.22s`, exit 0 (log `.tmp/T-018/fix5-lifecycle.log`).
  - `ruff check`: `No module named ruff` (not installed in `.venv`): **not_run**; `ruff format --check`: not_run, same reason.
- **Mutants (9, scratch copy of `src/prompt_enhancer`, tests from the worktree; runner `.tmp/T-018/fix5/mutants.py`, log `.tmp/T-018/fix5-mutants.log`):** unmutated control `17 passed`. Killed: M1 closer start error re-raised (direct and `closer-3` subprocess tests), M2 writer start escapes `emit` (round 4 code), M3 catch-all removed (unexpected-error test; its `KeyboardInterrupt` case then interrupted that pytest session, exit 2), M4 queued lines not counted, M5 failed output still queues, M7 listener close error ends the stop, M8 unstarted handler join raises, M9 socket close unguarded. **M6 (accept start failure leaves the listener open) survived the first run:** the test judged the listener by `port_is_free`, which passed anyway (cause not established; likely the socket was closed when the server object was garbage-collected). I changed the test to record the real `_Server` and check `socket.fileno() == -1`; rerun of M6: killed. Equivalent by design, not run: removing `_last_line`'s own `except` (nothing inside it raises after M2's fix).
- **not_run:** full pipeline suite (the PM runs it); Linux and CI; ruff; real `torch` close on another thread (P2); machine-wide thread exhaustion (refusals are labelled doubles, as in the retest).
- **Changed files:** `src/prompt_enhancer/embed_server/{server,__main__}.py`, `tests/test_embed_server_lifecycle.py` (still untracked `??`: the PM must stage it), `docs/stages/K1-embed.md`, this task file.
- **Cleanup:** removed `.tmp/T-018/fix5/bt`, `hypothesis`, `mut` and my run scripts; kept `.tmp/T-018/fix5/mutants.py`, `fix5-focused.log`, `fix5-mutants.log`, `fix5-lifecycle.log`. Reviewer folders (`rev`, `retest1` to `retest4`) and `tests/test_zz_golden_probe.py` untouched. Every child process was waited for or killed by its test; nothing of mine is running.
- **Noticed, not done:** `serve` still does not notice an accept loop that ended on its own error (E6, from round 4); the retest-4 scratch `.tmp/T-018/retest4/bt` and `hypothesis` are still there (the reviewer's cleanup was blocked); a `BaseException` other than `KeyboardInterrupt` inside `Thread.start` (after the OS thread exists) is still re-raised, as before.

## P2

Builder: fresh Claude Opus, `high`, worktree `.tmp/worktrees/T-018-p2` at `ca57f32`. R-016, R-017, R-018, R-015, R-025. **Status: Step 0 written, waiting for PM approval. Nothing downloaded, installed or run on the GPU.** Network reads so far: Hub model API and tree at the pinned revisions, both model cards (raw README at the pinned revision), PyPI JSON, the PyTorch cu130 index and `HEAD` of two wheels and of the Qwen card's demo image. Saved under `.tmp/T-018-p2/hub/` (scratch, deleted at handoff).

### Research evidence (P2 Step 0, read 2026-11-17)

| Question | Source | Finding | Class |
|---|---|---|---|
| EG2 pinned identity and gating | `api/models/google/embeddinggemma-2?blobs=true`, `…/tree/f9972ac2…5a1e?recursive=true` | `sha f9972ac22e696a105c482202646979e33e4b5a1e`, `gated False`, `private False`, `lastModified 2026-10-28T07:19:46Z`, `model_type embedding_gemma2`, no `auto_map`; 15 files, 1,525,810,627 B | documented |
| Qwen 2B pinned identity and gating | same for `Qwen/Qwen3-VL-Embedding-2B` at `9f2f7e71…7bda` | `gated False`, `private False`, `lastModified 2026-04-16T08:54:25Z`, `model_type qwen3_vl`, no `auto_map`; 18 files, 4,271,068,726 B; **no `2_Normalize/` folder** although `modules.json` names the Normalize module (P1 row) | documented |
| Sizes vs manifest example | `configs/model_manifest.example.json` (13 EG2 + 15 Qwen entries) vs the tree | every listed size equals the tree size; the example leaves out `README.md`, `.gitattributes` and (Qwen) `scripts/qwen3_vl_embedding.py`; small-file hashes `pin_required` | documented |
| Small-file integrity source | tree JSON `oid` per non-LFS file (e.g. `config.json` `"oid":"dc524b2c…340a","size":4455`) | the git blob SHA-1 lets each small file be checked independently of the downloader (`sha1(b"blob <size>\0" + bytes)`); LFS files carry the SHA-256 `lfs.oid` | documented |
| Qwen documented reference output | card at the pinned revision, lines 133-144 and 197-205 | ST recipe prints `tensor([[0.8160, 0.7155, 0.7054], [0.5173, 0.3295, 0.4446], [0.3863, 0.2987, 0.3312], [0.1061, 0.0433, 0.0839]])` for 4 text queries × (text, image `…/Qwen-VL/assets/demo.jpeg`, text+image); the helper-script path prints `[[0.8158, 0.7178, 0.7173], [0.5195, 0.3303, 0.4392], [0.3884, 0.2858, 0.3314], [0.1093, 0.0387, 0.0695]]` (rounded here) for the same inputs. The two documented implementations differ by up to 0.0144 (row 4 col 3), 0.0129 within cols 1-2 | documented |
| Qwen demo image | `HEAD https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg` | `200`, `image/jpeg`, `Content-Length: 496395`, `ETag F10E0F33…1A8C`, `Last-Modified 2023-08-22` | documented |
| EG2 documented reference output | card lines 111-123, 196-227 | the card prints `model.similarity(...)` but **publishes no numbers**; the documented reference is the card's recipe (`SentenceTransformer`, `prompt_name="Document"`, `encode({"image": …})`, bf16 if supported else float32) run locally | documented (recipe only) |
| torch wheel | `download.pytorch.org/whl/cu130/torch/` index; `HEAD download-r2.pytorch.org/…/torch-2.14.1%2Bcu130-cp312-cp312-win_amd64.whl` | listed with `sha256=0a09031e…5be9`, `Content-Length: 1990608063`; `.metadata` `Requires-Dist`: filelock, typing-extensions, setuptools, sympy, networkx, jinja2, fsspec (**no `nvidia-*` wheels on Windows**: CUDA runtime is inside the wheel) | documented |
| torchvision wheel | same index | `torchvision-0.29.1+cu130-cp312-cp312-win_amd64.whl` `sha256=14d3195e…91c6`, `Content-Length: 6566385` | documented |
| PyPI pins | `pypi.org/pypi/<name>[/<ver>]/json` | `transformers 5.23.0` py3 wheel 13,336,114 B (needs `huggingface-hub>=1.31,<3`, `tokenizers>=0.23.1,<0.24`, `safetensors>=0.8.0`); `sentence-transformers 6.3.0` (2026-11-11) 780,346 B (needs `transformers>=5,<6`, `torch>=2.2`, scikit-learn, scipy); `safetensors 0.8.0` 355,540; `huggingface-hub 2.4.0` 857,021 (needs `hf-xet`, `httpx2`); `tokenizers 0.23.3` 2,872,101 | documented |
| Sm_120 in the cu130 wheel | — | not readable without the wheel; checked after install by `torch.cuda.get_arch_list()` | assumption until P2 |
| GPU now | `nvidia-smi --query-gpu=…` | `RTX 5070 Laptop GPU, 0 MiB, 8151 MiB, 616.92` (not the baseline; that is taken right before the job) | documented |
| Disk | `Get-PSDrive D` | 155.4 GB free | documented |
| Dev VRAM budget | `configs/enhancer.dev.example.yaml:20` `vram_budget_gb: 8`; `models.py:141-145` | 8 GiB = 8192 MiB > 8151 MiB total, so `load_engine` refuses this profile value on this GPU (`runtime.vram_budget_gb exceeds the memory of the configured GPU`) | source-verified |
| Integration fix `BoundedOutput` | `server.py:94-95`; installed Python 3.12.10 `Lib/io.py` | `io.py` does `RawIOBase.register(FileIO)` and `RawIOBase.register(_WindowsConsoleIO)`; a probe printed `FileIO True` for stdout to a pipe and to a file. A pytest capture or `StringIO` has no `buffer.raw` (or a `BytesIO` buffer) and takes `stream.write`. The console case (`_WindowsConsoleIO`) is source-verified only: opening a console is refused here (footer 7) | source-verified / experiment |
| RT5F1 | `.tmp/pm-archive/T-018/T-018/retest5.md:105-115`; `server.py:502-546` | `_serve_connection`'s `finally` always `_forget`s (S:511) after `shutdown_request` swallowed the close `OSError` (S:523); `_turn_away` never registers its socket; so `registered()` in `stop_server` (S:670) cannot see a socket whose close failed | source-verified |

**Gemma authentication:** the brief calls the Gemma download gated; the Hub says `gated: False` for both repositories at the pinned revisions (rows above). So the download reads **no credential**: every call passes `token=False` (no stored or `.secrets/` token is looked up) with `HF_HOME` under `<project>/.cache/hf`. If the Hub answers 401 or 403 the download stops and reports; adding a credential path then is a new PM decision (only project Python would read it from `.secrets/`, and nothing here opens, lists or prints it).

### Downloads (total against the 12 GB cap of decision 139)

| What | Files | Bytes |
|---|---|---|
| EG2 `google/embeddinggemma-2` @ `f9972ac22e696a105c482202646979e33e4b5a1e` | the 13 manifest-example files: `model.safetensors` 1,488,915,288; `tokenizer.json` 32,170,510; `tokenizer.model` 4,689,013; `config.json` 4,455; `processor_config.json` 1,788; `tokenizer_config.json` 1,599; `config_sentence_transformers.json` 1,565; `chat_template.jinja` 1,016; `sentence_bert_config.json` 747; `preprocessor_config.json` 511; `modules.json` 413; `2_Normalize/config.json` 97; `1_Pooling/config.json` 90 | 1,525,787,092 |
| Qwen 2B `Qwen/Qwen3-VL-Embedding-2B` @ `9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda` | the 15 manifest-example files: `model.safetensors` 4,255,140,312; `tokenizer.json` 11,423,705; `vocab.json` 2,776,833; `merges.txt` 1,671,853; `chat_template.jinja` 5,519; `tokenizer_config.json` 5,404; `config.json` 1,570; `video_preprocessor_config.json` 817; `preprocessor_config.json` 783; `sentence_bert_config.json` 770; `added_tokens.json` 707; `special_tokens_map.json` 613; `modules.json` 429; `config_sentence_transformers.json` 238; `1_Pooling/config.json` 96 | 4,271,029,649 |
| Not downloaded | `README.md` (read already), `.gitattributes`, `scripts/qwen3_vl_embedding.py` (downloaded code, not run: P1 Q3) | 0 |
| Wheels measured | torch 1,990,608,063; torchvision 6,566,385; transformers 13,336,114; tokenizers 2,872,101; huggingface-hub 857,021; sentence-transformers 780,346; safetensors 355,540 | 2,015,375,570 |
| Wheels not yet measured | the rest of the resolved set (numpy, scipy, scikit-learn, sympy, networkx, hf-xet, httpx2, regex, pyarrow, Pillow, jsonschema, …): measured from a `pip install --dry-run --report` (metadata only; the torch index serves PEP 658 `.metadata`, so no wheel is fetched) summed against index sizes **before** the real install | estimate ≤ 0.3 GB (assumption) |
| Qwen card demo image (reference only, Q2) | `demo.jpeg` | 496,395 |
| **Total** | | **7,812,688,706 B measured (weights 5,796,816,741 + wheels 2,015,375,570 + image) + ≤ 0.3 GB = ≤ 8.2 GB** (cap 12 GB; I stop and ask if the dry-run sum pushes the total past 12 GB) |

**Download method:** a scratch driver under `.tmp/T-018-p2/` run with `.venv-gpu` Python `-I`: `huggingface_hub.hf_hub_download(repo_id, filename, revision=<40-hex>, local_dir=<project>/.cache/hf-staging/<owner>--<name>/<revision>, token=False)` per listed file (never `snapshot_download`, so no unlisted file arrives). Staging is needed because `local_dir` mode keeps its own `.cache/huggingface/` metadata in that folder, which `manifest.verify_model` refuses as an unlisted entry (assumption from the huggingface_hub docs, re-read in the installed source before use). Each file is then checked twice before it moves: size equal to the tree size, and LFS SHA-256 equal to `lfs.oid` or git blob SHA-1 equal to `oid` for small files; then moved (same drive, a rename, no second copy) to `models/<owner>--<name>/<revision>/<file>`; staging removed. `models/manifest.json` (Git-ignored) gets the 28 entries with the measured SHA-256 (a copy of the old file kept under `.tmp/T-018-p2/` until verification passes); `configs/model_manifest.example.json` gets the measured small-file SHA-256 in place of `pin_required` (EG2, Qwen 2B only; 8B unchanged). Then `manifest.verify_model` (the production check) runs on both. Resumable: a file already in place with the right size and hash is skipped.

### `.venv-gpu` install plan

1. Base interpreter: the one the CPU `.venv` was made from (Python 3.12.10, cp312; read from `.venv/pyvenv.cfg`, not a guess). `python -m venv <project>/.venv-gpu`.
2. Environment for every install command: `PYTHONNOUSERSITE=1`, `PIP_NO_CACHE_DIR=1` (`--no-cache-dir`: torch is not stored twice), `TEMP`/`TMP` = `<project>/.tmp/T-018-p2/tmp`, `HF_HOME=<project>/.cache/hf`, `HF_HUB_DISABLE_TELEMETRY=1`.
3. Pins finished in `requirements-gpu.txt` (tracked): add `sentence-transformers==6.3.0`, `safetensors==0.8.0`, `huggingface-hub==2.4.0`, `tokenizers==0.23.3` (all satisfy transformers 5.23.0's ranges above). The rest stays resolver-chosen and is recorded from `pip freeze` into `docs/dependencies-and-environments.md` (observed, not pinned; no lockfile is invented).
4. `pip install --dry-run --report .tmp/T-018-p2/pip-report.json -e . -r requirements-gpu.txt`, sum sizes, compare against the cap; then the real install. Wheel source: PyPI plus `--extra-index-url https://download.pytorch.org/whl/cu130` (already in `requirements-gpu.txt`); pip checks each wheel's `#sha256=` from the index.
5. **Blackwell and cu130 check** (first GPU touch, after the VRAM baseline): `torch.__version__ == '2.14.1+cu130'`, `torch.version.cuda` starts with `13.0`, `'sm_120' in torch.cuda.get_arch_list()`, `torch.cuda.get_device_capability(0) == (12, 0)`, and a 256×256 bf16 matmul on `cuda:0` equals the CPU float32 result within bf16 rounding. Any failure: stop and report; no other torch is tried without the PM.
6. The server must not import `sentence_transformers` (it is reference-only): a CPU test asserts no `embed_server` runtime module names it except the reference module's lazy import.

### Reference check method

Tool: new `src/prompt_enhancer/embed_server/reference.py` and the subcommand `python -m prompt_enhancer.embed_server reference-check --config <profile> --backend <name> --inputs <dir> --out runs/T-018-p2/reference/<utc>/ [--fit]`, run in `.venv-gpu`. Pure-Python comparison and fitting functions sit in the module (CPU-tested with labelled doubles); `sentence_transformers` and `torch` are imported lazily inside the run function. It is the consumer of **`vlm.backends.<name>.server.reference_check.min_cosine`** (schema: number in (0, 1]; sentinel `fit_from_bench` in the examples; `check` mode refuses a missing value or a sentinel with a precise message; `--fit` mode does not read it and prints the fitted value with its evidence file). Inputs: two images from `database/` (read only, copied to `.tmp/T-018-p2/inputs/` as files so nothing writes under `database/`), prepared once by `prepare_vision_image_with_info` at `vision.max_image_side` so both paths see the same bytes, plus two short texts.

- **Reference vectors (documented recipe, in process):** `SentenceTransformer(<models/… local dir>, local_files_only=True, trust_remote_code=False, model_kwargs={"dtype": torch.bfloat16})`, `encode([...], normalize_embeddings=True)`; EG2 text with `prompt_name="Document"`, images as `{"image": PIL}`; Qwen with its default prompt (system "Represent the user's input."). The model is unloaded (`del`, `gc.collect()`, `empty_cache`) before the server starts, so one model is resident at a time.
- **Fit (`--fit`):** the spread that the documented recipe itself allows: the same inputs under ST bf16 at batch size 1, 2 and 4 (and the input order reversed), and ST float32 (the EG2 card names both dtypes: "`torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32`"). `min_cosine` = the lowest pairwise cosine across those runs per input, floored to 4 decimals; the per-input table and the floor rule go into the evidence JSON and this file. If every pair is ≥ 0.99999 (the spread collapses), I report that and ask instead of writing a value that would demand bit-identical output (Q1).
- **Check:** the server, started from the same profile, embeds the same items sent in the K1 shape (`Embedder.image_bodies`/`text_bodies`, so the bytes are K1's); pass when cosine(server, ST bf16 batch 1) ≥ `min_cosine` for every item, plus server native dimension and unit norm.
- **Qwen card agreement (documented numbers):** with the demo image (Q2), the server reproduces the card's 4 queries × columns 1-2 (text document, image document; the card's column 3 text+image item has no K1 shape: a text part before an image is the instruction) and the 8 similarities are compared with the card's ST matrix; reported with the documented spread between the card's two implementations (≤ 0.0129 in those columns) as context, not as a second gate (Q3).
- This is a `smoke` plumbing check (R-020), never a quality claim; dev values never enter a deployment threshold (gpu-live-run preflight 6).

### VRAM plan and server smoke

Order (one job at a time, `gpu-live-run`; one model resident): baseline (`nvidia-smi` per device and the holder processes) → arch check → EG2: ST fit/reference (unload) → EG2 server reference check → EG2 K1 smoke → server stop → VRAM back at baseline → the same for Qwen 2B → final baseline. **K1 smoke on 2 images:** a scratch profile `extends` the dev example (`data_root`, `kb_store`, `knowledge.root` under `.tmp/T-018-p2/`, a 2-image scratch knowledge root copied from `database/`, the loopback port, `vram_budget_gb` 7, Q4), K0 sync then `k1_embed.embed_images` (the production client); evidence: both vectors present, dimension, unit norm, cosine to the reference vector of the same image. Server started with `subprocess.Popen(..., creationflags=CREATE_NO_WINDOW)`, stopped by recorded PID; then port free, no child left, VRAM equal to the baseline. Runs go under `runs/T-018-p2/` (Git-ignored).

### RT5F1 regression and fix

Tests first (they fail now, using the retest's labelled close-refusal double: a wrapper over a real `socketpair` that raises `OSError` before closing): (a) a finished handler whose close fails, (b) a turned-away connection whose close fails; each expects `stop_server` to raise `ShutdownIncomplete` naming the unclosed socket, and the backing socket still open. Fix: `shutdown_request` records a socket whose `close` raised **and** whose `fileno()` is still not `-1` in a separate `_unclosed` set (under `_lock`), independent of `_connections`; `close_connections` retries those; `stop_server` adds `(unclosed, f'{n} accepted socket(s) could not be closed')`. A failed close after which `fileno()` is `-1` is not a survivor (the retest's limit: a native close can invalidate the handle). Mutants: drop the `fileno` test, drop the refusal-path record, drop the survivor row.

### Hazards and the test that catches each

| Hazard | Caught by |
|---|---|
| cu130 wheel lacks sm_120 or cannot run on this driver | arch check step 5 (stop) |
| A downloaded file differs from the Hub (size, LFS SHA-256, blob SHA-1) or the wrong revision lands | driver double check; `manifest.verify_model` (existing tests + new size property test) |
| huggingface_hub `local_dir` metadata or an unlisted file in `models/…` | staging + move; `verify_model` refuses unlisted files (existing test) |
| A credential is picked up silently | `token=False` on every call; `HF_HOME` under `.cache/`; a scratch check that no `Authorization` header is sent is not possible without a proxy, so this is source-verified only |
| Qwen `2_Normalize` folder absent makes ST fail to load | reference run itself (the error, if any, is reported, not patched) |
| Server and ST differ (pooling, padding, prompt, image limits, `AutoModel` choosing `Qwen3VLModel` for a `…ForConditionalGeneration` checkpoint) | the reference check; the server profile's image limits must equal the processor's own `preprocessor_config.json` values for the comparison, else the check compares different images (asserted in the tool) |
| Tolerance fitted from a spread that is too wide hides a defect | fit uses only recipe-sanctioned variations; the Qwen card numbers are a second, external comparison |
| `vram_budget_gb: 8` refused on an 8151 MiB GPU | Q4; the existing `load_engine` refusal |
| OOM at batch 4 on 8 GB | `forward_batch` 1 for the smoke; OOM maps to `EngineError` (existing test) |
| A server or child left running, VRAM not back | teardown checks; PID-scoped stop |
| `server.reference_check.min_cosine` ignored, defaulted or accepted while a sentinel | config override + refusal tests (below) |
| RT5F1 | the two new lifecycle tests above |

### Questions (ranked, each with my proposed answer)

1. **Where may the fitted `min_cosine` live?** gpu-live-run says dev runs never enter thresholds. *Proposed: write the fitted value only in the dev example profiles (`purpose: smoke_only`) with the evidence path in a comment; the full example keeps `fit_from_bench` until the deployment measures it.*
2. **Fetch the Qwen card's demo image (496,395 B, a non-Hub URL)** to compare with the card's published numbers? *Proposed: yes, into `.tmp/T-018-p2/inputs/`, deleted at handoff; it is the only documented numeric reference either model has.*
3. **Card agreement as a gate or as evidence?** *Proposed: evidence only (no second key); a mismatch beyond 0.0129 is reported and stops P2 for a PM decision.*
4. **`runtime.vram_budget_gb`** for the dev runs: 8 is refused on this GPU (8151 MiB). *Proposed: 7 in the scratch run profile only, and a one-line fix of the dev example to 7 with the reason (it is the dev machine's own value, decision 20; PM decision pending user review).*
5. **`run_approval_gate.py check`** (gpu-live-run preflight 5) needs a user-approved profile record, which does not exist for these runs. *Proposed: the PM records/approves the scratch profile, or waives the gate for P2's smoke runs in the approval message.*

#### PM answers (P2; each a PM decision pending user review)

1. The fitted `min_cosine` goes only into the dev example profiles; the base example keeps `fit_from_bench`.
2. Fetch the Qwen card demo image into `.tmp/T-018-p2/inputs/`, record size and SHA-256, open it only through Pillow, delete it at handoff (reference data, never code).
3. Agreement with the card's numbers is reported evidence, not a second gate.
4. `vram_budget_gb: 7` in the dev example profile, the measured total (8151 MiB) quoted as the reason.
5. `run_approval_gate.py` is waived for P2's smoke runs: the dev profile is `purpose: smoke_only`, and no record may claim a user approval that does not exist.
6. Two stages: A (no GPU, no CUDA context: install, downloads, arch check from torch metadata, RT5F1, `reference-check`, focused tests) then `READY FOR GPU`; B only after `GPU GO`, reply `GPU DONE` as soon as the GPU is released.

### Configuration impact (P2, before any runtime edit)

| Setting | Schema / profile | Validation and tests |
|---|---|---|
| `vlm.backends.<name>.server.reference_check.min_cosine` (new, returns with its consumer) | schema `server.reference_check` object (`additionalProperties: false`), `min_cosine` number `(0, 1]` or sentinel; dev examples: fitted value (Q1); full example and `dev-qwen3vl2b`: fitted or `fit_from_bench` | refused missing, sentinel, ≤ 0, > 1, non-number, bool; override test: a non-default value changes the verdict of the comparison; the server itself ignores the key (only `reference-check` consumes it; an audit row says so) |
| `runtime.vram_budget_gb` (dev example) | existing | Q4; existing refusal at load |
| `requirements-gpu.txt` pins | tracked file | exact versions; doc update in `docs/dependencies-and-environments.md` |
| `models/manifest.json` entries, example small-file SHA-256 | existing schema `model_manifest.v0` | existing schema and manifest tests; new size property test |
| Download driver settings (repo, revision, file list, staging dir) | read from `configs/model_manifest.example.json` and the profile, never typed in the driver | scratch only, not runtime |
| Reference inputs dir, output dir | CLI arguments (validated: inside the project root, after resolving links) | refusal test for a path outside the root |
| Retained literals | `token=False` safety-bound; `fit_from_bench` protocol-bound sentinel; the 4-decimal floor of the fit: `artifact-bound` (a rule of the fit method, recorded with the value in the evidence JSON) | classified in the audit |

### Plan (checkable finish)

1. CPU work in the worktree (no network, no GPU): RT5F1 tests + fix; `reference.py` + subcommand + schema key + example profiles + tests; `requirements-gpu.txt` pins; size property test. Done: the brief's test sets green through `test_slot.py` (one process, no `-n`).
2. `.venv-gpu`: dry-run size sum ≤ cap, install, arch check. Done: the five assertions of step 5 printed.
3. Downloads into staging, double check, move, `models/manifest.json`, `verify_model` on both. Done: both verified; total bytes reported.
4. GPU runs per model (EG2, then Qwen 2B): fit, reference check, K1 2-image smoke, teardown. Done: cosine ≥ fitted `min_cosine` for every item of both models; Qwen card table; VRAM back at baseline; no process left; port free.
5. Docs (`dependencies-and-environments.md` tested versions, `model-storage.md` "Design only" line, K1-embed reference check), Configuration audit, mutants (3-10 on changed lines), cleanup of `.tmp/T-018-p2/`, Completion report.

### Steps (P2 Stage A)

1. Pins: `requirements-gpu.txt` gains `tokenizers==0.23.3`, `safetensors==0.8.0`, `huggingface-hub==2.4.0`, `sentence-transformers==6.3.0`; the install line no longer says `-e .` (an editable install would write build metadata into whichever tree it ran from; the package runs from `PYTHONPATH=<tree>/src`, as in the CPU `.venv`).
   Dry run (`pip install --dry-run --report`, metadata only): 57 wheels, **2,130,737,465 B** by `HEAD` of each index URL; with the weights (5,796,816,741 B) and the demo image (496,395 B) **7,928,050,601 B**, under 12 GB.
2. `.venv-gpu` created from the CPU venv's base interpreter (`.venv/pyvenv.cfg`: Python 3.12.10), installed with `--no-cache-dir`, temp under `.tmp/T-018-p2/tmp` (empty afterwards): exit 0, **3.6 GB** installed (`du -sh`).
   Torch metadata without a CUDA context (`torch.cuda.is_initialized()` False before and after): `2.14.1+cu130`, `torch.version.cuda` `13.0`, arch list `sm_75 … sm_100, sm_120`.
3. RT5F1: tests first (`CloseRefused` double over a real `socketpair`; handler and refusal paths; a refused close that keeps the handle stays a survivor, one that releases it does not): 3 failed before the fix (the two survivor cases and the updated `Unclosable` test), all 60 of the lifecycle file pass after. Fix: `_Server._unclosed`, filled by `shutdown_request` when `close` raises and `fileno()` is not -1, retried once by `close_connections`, named by `stop_server` as `N accepted socket(s) could not be closed`.
4. `reference-check reference|compare` (`embed_server/reference.py`, `__main__.py`), loader command `embed reference-check` with `validate_reference_check_reads` (config.py), schema `server.reference_check.min_cosine` (`(0, 1]` or `fit_from_bench`, optional for `serve`), `Family.reference_text_prompt_name`; examples hold `fit_from_bench`; dev `vram_budget_gb: 7`. Tests: `tests/test_embed_server_reference.py` (49), the no-heavy-import test now also covers `reference` and `sentence_transformers`, a manifest size property test.
5. Demo image: 496,395 B, SHA-256 `9eeaa87013b4e800930e8a411b58ff9e2fd5383906b1a022f4a712720af34cc2`, Pillow `JPEG (2048, 1365) RGB`, in `.tmp/T-018-p2/inputs/` (deleted at handoff).
6. Downloads (`.tmp/T-018-p2/download.py`, `token=False`, `HF_HUB_DISABLE_IMPLICIT_TOKEN=1`, `HF_HOME` under `<project>/.cache/hf`, staging under `<project>/.cache/hf-staging`): source-verified that `token=False` sends no token (`huggingface_hub/utils/_headers.py:132`) and that `local_dir` keeps `.cache/huggingface` inside it (`_local_folder.py:446-448`), hence the staging and move.

7. Downloads finished: 28 files, **5,796,816,741 B**, every file size == Hub tree, LFS SHA-256 / blob SHA-1 == Hub oid (`.tmp/T-018-p2/download.log`, `download-result.json`); staging removed (`.cache` holds only `hf` 576 KB and a `pip` folder from 11-16 that is not mine). `models/manifest.json`: 30 entries (2 GGUF kept, 28 new; old copy in `.tmp/T-018-p2/manifest.before.json`). `configs/model_manifest.example.json`: 23 `pin_required` small-file hashes filled with the measured SHA-256 (identical to the generated `.tmp/T-018-p2/model_manifest.example.json`); 8B entries unchanged.
8. Focused sets (first run, before the guard-test fix): `1 failed, 1608 passed, 34 skipped in 383.70s`; the failure is `test_every_schema_server_key_reaches_the_settings_record` (`reference_check` is not in `ServerSettings` by design: the server does not read it).
9. Guard test `test_every_schema_server_key_reaches_the_settings_record` now names `reference_check` as read by another command (`REFERENCE_CHECK_READS`) and still requires every other server key, unchanged otherwise.
10. `manifest.verify_model` (production check) on the downloaded trees: `google/embeddinggemma-2 verified, files 13`; `Qwen/Qwen3-VL-Embedding-2B verified, files 15`.
11. Focused sets rerun through `test_slot.py` (one process, no `-n`): `tests/test_embed_server_*.py tests/test_k1_*.py tests/test_field_contracts.py tests/test_real_contracts.py` → **`1609 passed, 34 skipped in 359.28s`** (log `.tmp/T-018-p2/focused2.log`).
12. Mutants (scratch copy of `src/prompt_enhancer` under `.tmp/T-018-p2/mut`, removed; log `mutants.log`): **10/10 killed** — M1 fileno test dropped, M2 unclosed not recorded, M3 no retry, M4 survivor row dropped (lifecycle); M5 floor → half-even, M6 `>=` → `>`, M7 lowest → highest, M8 sentinel not refused, M9 identity not checked, M10 compare read through `embed serve` (reference tests). Control on the unmutated copy: `5 passed, 104 deselected` (the `-k close_fail` filter; the full reference file passed in step 11).
13. `ruff check` / `ruff format --check`: **not_run** (`No module named ruff` in `.venv`).
14. Stage A stopped here: `READY FOR GPU` sent. Stage B (after `GPU GO`): EG2 then Qwen 2B, each `reference-check reference` (batch sizes 1, 2, 4), server start by PID with `CREATE_NO_WINDOW`, `reference-check compare`, K1 2-image smoke, teardown with VRAM back at baseline.

#### Configuration audit (P2 Stage A: rows new or changed)

| Setting / literal | Consumer | Validation | Example profile | Override test | Refusal test / class |
|---|---|---|---|---|---|
| `vlm.backends.<name>.server.reference_check.min_cosine` | `reference-check compare` (`__main__._reference_check` → `reference.judge`) | schema `(0, 1]` or `fit_from_bench`; `validate_reference_check_reads` refuses missing, sentinel, NaN; bool refused by the schema's number (jsonschema `is_number`) | `fit_from_bench` in the full example and both dev embedders (dev values written in Stage B, Q1) | `test_the_configured_tolerance_decides_the_verdict` (0.99 → exit 4, 0.9 → exit 0 on the same vectors); `test_reference_check_reads_a_measured_tolerance` | `test_reference_check_refuses_a_missing_unresolved_or_out_of_range_tolerance` (10 cases), `test_compare_refuses_an_unresolved_tolerance_before_contacting_the_server`; configurable |
| same key, not read by `serve` or `reference` | — | — | — | `test_the_server_does_not_read_the_tolerance` (absent and sentinel both load for `embed serve`) | configurable (documented non-consumers) |
| loader command `embed reference-check` | `config.load_config` | `COMMAND_READS` entry; runs `validate_embed_server_reads` first | — | via the tests above | protocol-bound |
| `--batch-size` (repeatable, required, 1 required, positive, distinct) | `reference.run_reference` | `check_batch_sizes` before any import or manifest read | — | — | `test_batch_sizes_need_the_baseline_one…` (7 cases), `test_the_reference_step_refuses_before_loading_anything`; configurable (CLI, recorded in reference.json) |
| `--image`, `--texts`, `--out`, `--reference` paths | `reference.prepare_inputs`, `_inside` | inside the project root after resolving links; texts a JSON list of non-empty strings; images through K1's own preparation | — | `test_inputs_are_prepared_as_k1_prepares_them…` | same test (outside root, bad JSON, empty text, broken image, no inputs); configurable |
| `runtime.vram_budget_gb` (dev example 8 → 7) | `models.load_engine`, `reference.run_reference` | existing refusal above the device total | dev example 7, reason quoted | existing `test_every_value_comes_from_the_profile` (2.5) | existing; configurable |
| `FIT_DECIMALS = 4` | `reference.floor_to` | — | — | `test_the_fit_floors_to_four_decimals`, floor property | artifact-bound (recorded in fit.json) |
| `FORMAT = 'embed_reference.v0'` | reference files | `load_reference` refuses another | — | — | `test_a_reference_made_for_something_else_is_refused`; artifact-bound |
| `REFERENCE_FALLBACK_DTYPE = 'float32'`, `ORDERS`, `BASELINE_ORDER` | `run_reference` | — | — | Stage B | artifact-bound (the card's sanctioned dtype; the fit method) |
| `MISMATCH = 4` | CLI exit status | — | — | `test_the_configured_tolerance_decides_the_verdict` | protocol-bound |
| `Family.reference_text_prompt_name` (`Document`, None) | `reference._encode` | — | — | Stage B (the reference itself) | artifact-bound (card / `config_sentence_transformers.json`) |
| `_Server._unclosed` survivor row | `stop_server` | — | — | `test_a_close_failure_that_leaves_the_socket_open_stays_a_survivor[handler,refusal]` | `test_a_close_failure_that_released_the_socket_is_not_a_survivor`; not a setting |
| `requirements-gpu.txt` pins | `.venv-gpu` | exact `==` pins | — | installed versions recorded (`arch-check.json`) | — ; configurable (file) |
| download driver values (repos, revisions, file list) | scratch `download.py` | read from `configs/model_manifest.example.json` and the Hub tree, nothing typed | — | — | scratch, removed at handoff |

#### PM decisions pending user review (P2 Stage A)

- `server.reference_check` is optional in the schema: only `reference-check compare` reads it (and refuses it missing or a sentinel); requiring it for `serve` would force an unread key on a deployment that never runs the check (an ignored configured field).
- `reference-check reference` reads the profile through `embed serve` (the tolerance is what it measures); its batch sizes are command-line values recorded in `reference.json`, not profile keys (a measurement procedure, not a deployment choice).
- The reference compares the K1-rendered text with the card recipe: EmbeddingGemma 2 `prompt_name="Document"` on the raw text (its prefix is K1's `text_template`), Qwen the recipe's default prompt; a fact of each card, held in `families.py`.

### Stage B (GPU, 2026-11-17 04:16-04:41 +07:00; builder continuing the retired stage A worker)

Step 0 (approved plan, continued without waiting): re-read `## P2` (plan, PM answers, method), skill `gpu-live-run`, `embed_server/{__main__,reference,models,families,manifest}.py`, both dev example profiles; ran the plan's order (device check, EG2 reference, EG2 server compare and K1 smoke, teardown, then Qwen likewise, card agreement, final baseline).

Run harness (scratch, deleted; profiles kept in `runs/T-018-p2/profiles/`): `monitor.py` started each process with `CREATE_NO_WINDOW`, recorded the launcher PID and its interpreter child (the venv `python.exe` is a redirector), sampled `nvidia-smi` and each process's peak working set every 0.5 s. `launch.py` called the production CLI `prompt_enhancer.embed_server.__main__.main(argv)` unchanged except that `verify_model` read the main checkout's `models/` (this worktree has none; the manifest check refuses a link, and `--project-root <main>` would load main's schema, which lacks `server.reference_check`). Scratch run profiles extend the dev examples and set only the `deploy_required` values of this run (port 18431/18432, `forward_batch 1`, `idle_exit_s 120`, `vision.quality 90`, storage under `.tmp/T-018-p2/`, `engine.version` the worktree label). Servers stopped by their own `idle_exit_s`, watched by recorded PID (fallback `taskkill /PID <launcher> /T /F`, not needed).

1. Device check (`gpu_check.py`, first CUDA context): `torch 2.14.1+cu130`, CUDA `13.0`, `sm_120` present, capability `[12, 0]`, `bf16_supported true`, 256×256 bf16 matmul vs CPU float32 max relative error 0.0043 (< 4·2⁻⁷). VRAM 0 → 0 MiB.
2. EG2 reference, attempt 1: refused, `vision.quality: unresolved sentinel design_required` (scratch profile then set 90). Attempt 2: manifest not found (worktree has no `models/`; harness `launch.py` above). Attempt 3: **`attn_implementation: sdpa` refused at load**: `ValueError: Gemma4AudioModel does not support an attention implementation through torch.nn.functional.scaled_dot_product_attention yet` (transformers 5.23.0, `modeling_utils.py:1666`); EG2 runs with `eager`. Attempt 4: **CUDA OOM on the float32 load** (`Tried to allocate 20.00 MiB … 4.02 GiB is free. 7.00 GiB allowed; 2.78 GiB allocated`; `CUDACachingAllocator.cpp:3934 … free: 4319084544`). Probes (`probe_load.py`): float32 alone 2842 MiB allocated, bf16 then float32 in one process 1432 then 2842 MiB, both released to 0; attempt 5 (unchanged inputs) passed. **Not reproduced; cause unknown** (finding F3).
3. EG2 reference (attempt 5): `{"min_cosine": 0.9998, "raw_min_cosine": 0.9998735781429398}`; 12 runs (bf16/float32 × b1/b2/b4 × forward/reversed); per input lowest 0.99991 / **0.99987** (image 2, bf16/b1 vs float32/b1) / 0.99994 / 0.99993 (texts, bf16/b1 vs bf16/b2). 44.9 s, peak VRAM 5186 MiB, peak RSS 5.61 GB, VRAM after 0.
4. EG2 server 1 (PID 25996, launcher 27032, 127.0.0.1:18431): health `ok`; `compare` → **HTTP 500** on the image request; the body: `engine_failed: The total number of <|image|> tokens in the prompts should be the same as the number of images passed. Found total 0 <|image|> tokens and 1 images.`; a text request returned 200. Defect F1 in `models._gemma_inputs` (images sent with `text=['']`). Stopped by idle exit: exit 0, 195.9 s, peak VRAM 1624 MiB, RSS 2.58 GB, no child left, port free, VRAM 0.
5. Fix F1 (`models.py`): each EG2 image is rendered through the model's own chat template as a lone user image part (the card recipe's sentence-transformers "message" modality: `transformer.py:1407-1421`; the model's `chat_template.jinja` writes `<|image|>` for an image part); the processor then gets the rendered text and the image. `processing_utils.py:2404-2406`: `apply_chat_template(tokenize=True)` drops special tokens only when the prompt starts with BOS, which this template never writes, so rendering then calling the processor equals the recipe. Test `test_a_gemma_image_is_rendered_through_the_model_chat_template` (labelled template double that writes a marker per image part).
6. EG2 server 2 (PID 16552, launcher 15652): `compare` **passed**: `min_cosine 0.9998`, cosines `0.9999975, 0.9999972` (images), `0.9999964, 0.9999959` (texts), dim 768. K1 smoke (`stage_b.py k1`: K0 scan of a 2-image scratch knowledge root copied from `database/Ao_nhat_binh_5day/`, then the production `k1_embed.embed_images`): `embedded 2, failed {}`, both dim 768, norm 1.0000000 ± 2e-9. Stopped by idle exit: exit 0, 214.5 s, peak VRAM 2566 MiB, RSS 2.58 GB, no child, port free, VRAM 0.
7. Qwen reference, attempt 1: encodes done (peak 4678 MiB) but writing failed: `TypeError: Object of type SizeDict is not JSON serializable` (transformers 5 holds `image_processor.size` as `SizeDict`, `image_utils.py:1034-1063`, not a dict). Fix F2 (`reference._recipe`: non-scalar limits through `dict()`, which keeps the set fields) + test. Before that run, `reference_dtypes` (F4): the fit's float32 run is the EG2 card's fact; the Qwen card names no other dtype (its script `--dtype` default `bfloat16`, card lines 312, 434) and float32 2B (≈ 8.5 GB) exceeds the 7 GiB budget; now `Family.reference_extra_dtypes` (EG2 `('float32',)`, Qwen `()`) + test.
8. Qwen reference (attempt 2): `{"min_cosine": 0.9996, "raw_min_cosine": 0.9996996817750564}`; 6 runs (bf16 × b1/b2/b4 × both orders). 23.1 s, peak VRAM 4678 MiB, RSS 5.25 GB, VRAM after 0. Recorded processor limits `size {longest_edge 1310720, shortest_edge 4096}` = the model's `preprocessor_config.json` (`max_pixels 1310720`), **not** the dev profile's `[A]` 1843200 (the card script's 1800·32·32); `compare` refuses differing limits, so the dev example now holds 1310720 (PM decision pending user review, below).
9. Qwen server (PID 4656, launcher 14900, :18432): `compare` **passed**: `min_cosine 0.9996`, all four cosines 0.9999986, dim 2048. K1 smoke: `embedded 2`, dim 2048, norm 1 ± 3e-9, cosine to the reference 0.9999986 / 0.9999987. Stopped by idle exit: exit 0, 164.6 s, peak VRAM 4662 MiB, RSS 5.19 GB, no child, ports 18431 and 18432 free, **VRAM 0 of 8151 MiB = baseline, 04:41:40 (GPU DONE)**.
10. Qwen card agreement (evidence only, PM answer 3; card lines 118-144): 4 queries × (text document, demo image) through K1's client. With K1's preparation (`max_image_side 1024`: the 2048×1365 demo sent at 1024×683): text column |Δ| ≤ 0.0044, image column |Δ| up to **0.0351** (above the cards' 0.0129 spread). With the image at its original size (scratch `qwen-full.yaml`, `max_image_side: none`): **all 8 |Δ| ≤ 0.0083**, inside the documented 0.0129. The gap is K1's downscale (the card embeds the original), not the server.
11. Fitted values (step 3 of the brief): dev example `embedder` `min_cosine 0.9998`, `embedder_qwen3vl2b` `0.9996`, each with its evidence path; `configs/enhancer.example.yaml` keeps `fit_from_bench`.
12. CPU: focused sets `1614 passed, 34 skipped in 375.30s` (`.tmp/T-018-p2/focused3.log`); after the models test's double was sharpened, `tests/test_embed_server_models.py` `18 passed`. Mutants (scratch copy, `mutants.py`): M11 Gemma image text empty again, M12 dtypes ignore the family, M13 Qwen gains float32, M14 `SizeDict` kept as is: **4/4 killed** (`1 failed` each); control = the same tests on the tree, passed.

#### K1 smoke, per item

| Model | Image | Sent size | Dim, norm | Cosine to reference | Verdict |
|---|---|---|---|---|---|
| EG2 | `ao_nhat_binh_000430.jpg` (source 1024×1380) | 759×1024 | 768, 1.0000000 | 0.9999975 | meets target (plumbing: embedded, native dim, unit norm, matches the card recipe) |
| EG2 | `ao_nhat_binh_000916.jpg` (source 1080×1621) | 682×1024 | 768, 1.0000000 | 0.9999972 | meets target |
| Qwen 2B | `ao_nhat_binh_000430.jpg` | 759×1024 | 2048, 1.0000000 | 0.9999986 | meets target |
| Qwen 2B | `ao_nhat_binh_000916.jpg` | 682×1024 | 2048, 1.0000000 | 0.9999987 | meets target |

The input image (opened: a red brocade court robe, lotus pond) decodes normally; the verdict concerns plumbing only, never retrieval quality (R-020).

#### gpu-live-run evidence

- Label: GPU live (smoke). Dev profile, never a threshold, calibration or timing claim (preflight 6).
- Commands (from the worktree root; env `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<tree>/src`, `HF_HOME=<project>/.cache/hf`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `TEMP`/`TMP` under `.tmp/T-018-p2/tmp`): `.venv-gpu python -B monitor.py <out> <timeout> -- .venv-gpu python -B -X faulthandler launch.py reference-check reference --config <eg2|qwen>.yaml --image <kb>/ao_nhat_binh_000430.jpg --image <kb>/ao_nhat_binh_000916.jpg --texts texts.json --out runs/T-018-p2/<m>/reference --batch-size 1 --batch-size 2 --batch-size 4`; `… launch.py serve --config <m>.yaml`; `.venv python -B launch.py reference-check compare … --reference runs/T-018-p2/<m>/reference/reference.json --out runs/T-018-p2/<m>/compare`; `stage_b.py k1 …`, `stage_b.py card …`.
- Profiles (SHA-256 prefix): dev example `dd5482d4c45eb7ab` (after this stage's edits), dev-qwen3vl2b `8fc7f566bc43efe7`; run profiles `runs/T-018-p2/profiles/eg2.yaml` `92812a23d56623bc`, `qwen.yaml` `8907c03a0d2ca648`, `qwen-full.yaml` `f56ee847cc48f99a`.
- Models: `google/embeddinggemma-2@f9972ac22e696a105c482202646979e33e4b5a1e` (`model.safetensors` 1,488,915,288 B, SHA-256 `197a3296…5d79`), `Qwen/Qwen3-VL-Embedding-2B@9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda` (4,255,140,312 B, `c73fa9ca…09c1`); every file verified by `manifest.verify_model` at each load.
- Runtime: driver 616.92, RTX 5070 Laptop GPU 8151 MiB (`mem_get_info` free at start 7013 MiB), torch 2.14.1+cu130 (CUDA 13.0), transformers 5.23.0, sentence-transformers 6.3.0, Python 3.12.10.
- Outputs: `runs/T-018-p2/{gpu-check,eg2,qwen,profiles}/` (Git-ignored): `reference.json` `aa750fbe…`/`b1331e95…`, `fit.json` `b6e90737…`/`bde4265d…`, `check.json` `6404bcb6…`/`412ffbdb…`, `k1.json` `d9307516…`/`451482e7…`, `card.json` `71e6ff59…`, `card-original-size.json` `107cb083…`, per-process `monitor.json` with the VRAM samples.
- Durations, peaks: in steps 1-9 (reference EG2 44.9 s incl. load; Qwen 23.1 s; servers ready ≈ 15 s after start by health polling, total lifetimes include 120 s idle). No throughput claim.
- Teardown: every process stopped by its own exit, watched by recorded PID; `children_alive_after []` each time; ports 18431 and 18432 free; VRAM 0 MiB after every job.
- `run_approval_gate.py`: **waived** (PM answer 5): the dev profile is `purpose: smoke`; no user-approved run record exists and none was created or claimed.

#### Configuration audit (Stage B: rows new or changed)

| Setting / literal | Consumer | Validation | Example profile | Override test | Refusal test / class |
|---|---|---|---|---|---|
| `server.reference_check.min_cosine` (dev values) | `reference-check compare` | as Stage A | dev example 0.9998 (EG2), 0.9996 (Qwen) with evidence paths; full example `fit_from_bench` | `test_the_configured_tolerance_decides_the_verdict`; the GPU compares ran with the fitted values | Stage A refusal tests (sentinel, missing, range); configurable |
| `server.image.max_pixels` (dev Qwen 1843200 → 1310720) | `models._apply_image_limits`, `reference.processor_limits` | existing schema | dev example, reason quoted | existing `test_embed_server_config` overrides (5000) | existing refusals; configurable |
| `server.attn_implementation` (EG2 note: eager) | `models.load_engine` | existing (`deploy_required` kept) | comment only | existing | existing; configurable |
| `Family.reference_extra_dtypes` (EG2 `('float32',)`, Qwen `()`) | `reference.reference_dtypes` → `run_reference` | — | — | `test_the_fit_runs_the_profile_dtype_first_then_the_card_s_other_dtypes` (3 cases) | artifact-bound (card facts; replaces `REFERENCE_FALLBACK_DTYPE`) |
| Gemma image message `{'role': 'user', 'content': [{'type': 'image', …}]}` | `models._gemma_inputs` | — | — | `test_a_gemma_image_is_rendered_through_the_model_chat_template` | protocol-bound (the chat-message shape the model's template reads) |
| scalar types `(bool, int, float, str)` in `_recipe` | `reference._recipe` | — | — | `test_the_recipe_records_image_limits_held_in_a_non_dict_size_type` | protocol-bound (JSON scalars) |
| scratch run values (ports, `forward_batch 1`, `idle_exit_s 120`, `vision.quality 90`, storage paths, `engine.version` label) | this run only | the loader | `runs/T-018-p2/profiles/` | — | scratch, not tracked |

#### Findings

- F1 (fixed): EG2 image requests failed with HTTP 500 (no `<|image|>` placeholder); now through the model's chat template.
- F2 (fixed): `reference._recipe` could not serialise the Qwen processor's `SizeDict`.
- F3 (open): one transient CUDA OOM on the EG2 float32 reference load with 4.1 GB reported free and 2.78 of 7.00 GiB allocated; not reproduced in 3 later loads; cause unknown (WDDM, a GPU peer for a moment, or the allocator: not measured).
- F4 (fixed, PM decision pending user review): the fit's extra dtype became a per-family card fact; Qwen's fit has no float32 run.
- F5 (open, not fixed): `reference-check compare` lets a K1 `EmbeddingError` (e.g. HTTP 500) escape as a traceback, exit 1, instead of a one-line refusal; `__main__._reference_check` catches `ConfigurationError, ManifestError, ReferenceCheckError, OSError` only.
- F6 (open): EG2 rejects `attn_implementation: sdpa` at load; nothing refuses that combination before the weights load (a load-time `ServerStartError`/traceback instead).

#### PM decisions pending user review (Stage B)

- Dev Qwen `max_pixels` 1310720 (the model's `preprocessor_config.json`, the recipe's own) instead of the card script's 1843200; the full example still holds 1843200 (`[A]`), not changed here.
- `Family.reference_extra_dtypes`: Qwen's fit runs bf16 only (card names no other dtype; float32 does not fit 8 GB).
- Run harness `launch.py` (models from the main checkout) instead of moving or linking 5.8 GB of weights into the worktree.

#### Noticed, not done

- `configs/enhancer.example.yaml:213` Qwen `max_pixels: 1843200 [A]` differs from the processor's 1310720; a deployment comparing with the recipe would be refused by `compare`.
- K1 sends images at `max_image_side` (1024 in dev); the Qwen card's numbers were made at full size, so card agreement depends on that setting (step 10).
- F5 and F6 above.

## Completion report

- **Behaviour:** both dev embedders were checked on the dev GPU against their cards' sentence-transformers recipes. Every served item reached at least 0.99999 cosine, against fitted tolerances of 0.9998 (EG2) and 0.9996 (Qwen 2B), now in the dev example. The K1 2-image smoke through the server meets target for both models. Stage B fixed two defects: EG2 images (HTTP 500) and Qwen reference serialisation. The fit's extra dtype is now per family. Card agreement holds within the documented spread at full image size.
- **Commands and output:** see Stage B steps 1-12 and the gpu-live-run evidence; focused sets `1614 passed, 34 skipped in 375.30s`; models file `18 passed`; mutants 4/4 killed (stage A 10/10).
- **not_run:** `ruff check` / `ruff format --check` (`No module named ruff` in `.venv`; not installed, per brief); `run_approval_gate.py check` (waived, PM answer 5); full test suite (not required by the brief); EG2/Qwen `forward_batch > 1` in the server (smoke used 1); F3 root cause.
- **Changed files (stage B):** `src/prompt_enhancer/embed_server/models.py`, `families.py`, `reference.py`; `tests/test_embed_server_models.py`, `tests/test_embed_server_reference.py`; `configs/enhancer.dev.example.yaml`; `docs/stages/K1-embed.md`; this task file.
- **Open issues:** F3, F5, F6; the full example's Qwen `max_pixels`; the PM decisions pending user review above.

## P2 fix round 1

Builder: fresh Claude Opus, `high`, continuing the retired P2 builder on top of the staged P2 change. Review `.tmp/T-018/review-p2.md` (REWORK: N1 P2, OPEN-F6 P2, E1 = F5 P3, E2 P3). R-015, R-016, R-017, R-018, R-025. **Status: Step 0 approved by the PM; implemented; handed to the Codex reviewer for retest.**

### Step 0

**Research done (read in place, CPU only):**

| Question | Source | Finding | Class |
|---|---|---|---|
| Where the norm is ignored | `embed_server/reference.py:98-100` | `norm` is computed and recorded; `passed` uses only cosine and length | source-verified |
| Norms already recorded | scratch `.tmp/T-018-p2/fix1/norms.py` over `runs/T-018-p2/{eg2,qwen}/{reference/reference.json,compare/check.json}` | recipe runs: EG2 12 runs / 48 vectors, max \|‖v‖−1\| **0.0015440438051930183**; Qwen 6 runs / 24 vectors **0.0022919215404879623** (the card recipe normalises in bf16). Served: EG2 **4.18e-9**, Qwen **2.63e-9** | experiment (replayed evidence) |
| What K1 does to served vectors | `k1_embed.py:193-202, 205-225` | native mode keeps the scale (no normalisation), rounds to float32, refuses non-finite values, a wrong count and (native) a wrong length with `EmbeddingError` | source-verified |
| K1 error types | `k1_embed.py:52-57, 282-294` | `EmbeddingError(RuntimeError)` with `.code` `HTTP_ERROR` (status or httpx exception type name), `RESPONSE_INVALID`, `DIMENSION_MISMATCH`; details are structural, never the body | source-verified |
| EG2 attention kernels | installed transformers 5.23.0: `models/embedding_gemma2/modeling_embedding_gemma2.py:649` (`self.audio_tower = AutoModel.from_config(config.audio_config)`), `models/gemma4/modeling_gemma4.py:1963,1968-1971` (`Gemma4AudioModel`: "The chunked local attention is only implemented in eager", `_supports_sdpa = False`), `modeling_utils.py:1665-1669` (raises `ValueError` "... does not support an attention implementation through torch.nn.functional.scaled_dot_product_attention yet"); Stage B attempt 3 hit it | EG2 supports `eager` only of the schema's `sdpa`/`eager` | source-verified + experiment |
| Qwen attention kernels | `models/qwen3_vl/modeling_qwen3_vl.py:598,606` (`Qwen3VLPreTrainedModel._supports_sdpa = True`) | `sdpa` and `eager` | source-verified |
| Where the shared validator runs | `config.py:1045-1048` (dtype check), callers `config.py:1581,1583`, `settings.py:59`; `__main__.py:94-101` | `serve`, `reference` (`embed serve`) and `compare` (`embed reference-check`) all pass `validate_embed_server_reads` before `verify_model`, `load_engine` or a bind | source-verified |
| Fixture pairing EG2 with `sdpa` | `tests/embed_server_fixtures.py:34` | one `attn_implementation: sdpa` for both families | source-verified |
| Reference parser | `reference.py:263-283` | `json.loads` and `reference['identity']`, `['recipe']['image_limits']`, `['runs'][baseline]` unchecked | source-verified |
| Prior decisions | `recall.py search "unit norm reference check bound"`, `"attn_implementation sdpa eager family"` | no hits | documented |

**Unknowns and failure modes:** a bound fitted from the server under test would be circular; a bound tighter than the recipe's own bf16 rounding would fail a correct server that normalises in bf16; JSON `NaN`/`Infinity` and integers beyond the float range are accepted by `json.loads`; a deeply nested file raises `RecursionError`; DIMENSION_MISMATCH is raised by K1 before `judge` sees a vector, so a length failure reaches the CLI as an error, not a verdict.

**Questions (ranked, each with my proposed answer):**

1. **Source and name of the norm bound.** *Proposed:* `server.reference_check.max_norm_deviation`, the largest allowed \|‖v‖−1\| of a served vector (the name states the measured quantity and its direction, like `min_cosine`). Fitted from the **recipe's own runs**, not from the served vectors: the tolerance must come from the documented reference, as `min_cosine` does, and fitting it on the server under test would pass whatever the server does. Rule: the largest deviation across every run and input, rounded **up** to `FIT_DECIMALS` (4; outward for a maximum, as the cosine is floored for a minimum). Dev values EG2 **0.0016**, Qwen **0.0023**, each citing `runs/T-018-p2/<m>/reference/reference.json` and this table; base example `fit_from_bench`. Schema: number in (0, 1) or `fit_from_bench`, required beside `min_cosine` in `reference_check`. The narrower design dropped: a fixed numerical epsilon in code (an unmeasured literal).
2. **Does `reference-check reference` also write the new fit?** *Proposed:* yes: `fit.json` gains `max_norm_deviation` and its raw value, and the command prints it, so a deployment fits both keys from one run.
3. **Exit status of a K1 `EmbeddingError` in `compare`.** *Proposed:* one line `refused: the server's embeddings were refused by K1 (<code>: <structural detail>)` and status 2 (`REFUSED`) for every code, DIMENSION_MISMATCH included: K1 refuses the response before any vector exists to judge.
4. **EG2 `attn_implementation` in the dev example.** *Proposed:* keep `deploy_required` (a deployment value) and change the comment to say `sdpa` is refused at start; no example pairs EG2 with `sdpa` today.
5. **Unexpected errors in `reference-check`** (a CUDA out-of-memory, a library bug). *Proposed:* not translated: Python's own traceback and status 1 (`FAILED`, documented as Python's uncaught status), so programming errors stay visible; the sweep table lists them.

**Plan (checkable finish):**

1. N1: `judge` passes an item only with a finite norm within `max_norm_deviation` (no normalising); `fit_max_norm_deviation` in the fit; schema key, `REFERENCE_CHECK_READS`, examples, docs; tests: scaled vectors fail (exit 4), rounding inside the bound passes, override changes the verdict, refusal (missing, sentinel, 0, 1, bool, NaN). Done: the review's ×2 mutant dies.
2. F6: `Family.attn_implementations` (EG2 `('eager',)`, Qwen `('sdpa', 'eager')`, artifact-bound with the source lines above), refused in `validate_embed_server_reads` next to the dtype; fixture per family; tests: EG2/eager accepted, EG2/sdpa refused by `serve`, `reference` and `compare` with spies proving no `verify_model`, `load_engine` or bind; Qwen sdpa and eager accepted.
3. E1 and E2: `EmbeddingError` caught in `_reference_check`; `load_reference` validates the whole artifact (format, identity, inputs, recipe, baseline, runs, counts, dimensions, finite floats) and maps decode errors (`ValueError`, `RecursionError`) to `ReferenceCheckError`; property tests (malformed, truncated, wrong-typed JSON) and CLI refusal-before-contact tests.
4. Rerun the review's probes and mutant, 3-5 own mutants, the brief's test sets through `test_slot.py` (one process, fixed `--basetemp` under `.tmp/T-018-p2/`, deleted after). Done: green, mutants reported.
5. Class-sweep tables, Configuration audit rows, `### Completion report (P2 fix round 1)`; remove `.tmp/T-018-p2/fix1/` except the cited norms output.

#### PM answers (P2 fix round 1; each a PM decision pending user review)

1. `server.reference_check.max_norm_deviation`, fitted from the card recipe's own recorded runs (not from the server under test): the largest \|norm − 1\| rounded up to 4 decimals; dev EG2 0.0016, Qwen 0.0023 with the evidence path; base `fit_from_bench`; required next to `min_cosine` for `compare` only (the server never reads it).
2. `reference-check reference` writes and prints the norm fit in `fit.json`.
3. Every K1 `EmbeddingError` in `compare` becomes one `refused: <code>` line (code and structural detail only, no payload or response text) and exit status 2.
4. Per-family supported attention kernels as artifact-bound capability facts with the cited source lines; refused in the shared validator before manifest hashing, binding or load; the fixture fixed; the dev example comment updated.
5. No catch-all for programming errors beyond the existing convention: `reference` and `compare` share `cli.main` with `serve`, so once the profile is read an unexpected error is one `failed: <Type>` line and exit 1 (type only, no message text), exactly as `serve`.

#### PM decisions pending user review (fix round 1, builder)

- Schema range of `max_norm_deviation`: `[0, 1)` (inclusive 0), so any value the fit writes can be stored; a fitted 0 demands exactly unit length. `min_cosine` keeps `(0, 1]`.
- `fit.json` does not gain the norm fit retroactively: the recorded dev runs predate it, so the dev values were recomputed on the CPU from `reference.json` (rule identical to `fit_max_norm_deviation`).
- Before the profile is read, `reference-check` keeps the existing behaviour of `serve`: a configuration refusal is printed directly; anything else there is Python's own traceback (nothing is owned yet).

### Completion report (P2 fix round 1)

**Behaviour.** N1: `reference.judge` passes an item only when cosine ≥ `min_cosine`, length = native dimension **and** the received norm is finite and within `max_norm_deviation` of 1; the checker never normalises. `fit.json` gains `max_norm_deviation` / `raw_max_norm_deviation` (`fit_max_norm_deviation`, `ceil_to`); the command prints them; `compare` reads the new key through `embed reference-check` (required, sentinel refused). F6: `Family.attn_implementations` (EG2 `('eager',)`, Qwen `('sdpa', 'eager')`) is enforced in `validate_embed_server_reads`, which `serve`, `reference` and `compare` all run before `verify_model`, a bind or `load_engine`. E1: `EmbeddingError` is a refusal (`refused: HTTP_ERROR: status 500`, exit 2). E2: `load_reference` validates the whole artifact and maps decode failures to `ReferenceCheckError`; it runs before `run_compare` sends anything. PM answer 5: after the profile is read, an unexpected error in `reference-check` is `failed: <Type>`, exit 1, through the same bounded `_last_line` as `serve`.

**Norm evidence (CPU recomputation of recorded runs, scratch `norms.py`, read only, real output):**

```
eg2 reference runs=12 vectors=48 max|norm-1|=0.0015440438051930183
eg2 served vectors=4 max|norm-1|=4.175389145544273e-09 recorded norms=[0.9999999980545426, 1.0000000021537825, 0.9999999975128938, 1.0000000041753891]
qwen reference runs=6 vectors=24 max|norm-1|=0.0022919215404879623
qwen served vectors=4 max|norm-1|=2.6263941022008908e-09 recorded norms=[0.9999999973736059, 1.0000000020983384, 0.9999999978366492, 1.0000000010318235]
```

`ceil_to` gives 0.0016 and 0.0023 (asserted in `test_the_norm_fit_rounds_up_to_four_decimals`). The recorded served vectors stay inside both bounds by about 10⁶×, so the dev compares recorded in Stage B would still pass.

#### Class sweep (a): every measured quantity of a verdict

| Quantity | Where measured | Decides? | Test that fails if it is ignored |
|---|---|---|---|
| cosine to the baseline | `judge` | yes, item fails below `min_cosine` | `test_the_configured_tolerance_decides_the_verdict`; earlier mutant M6 `>=`→`>` |
| norm of the served vector | `judge` (as received) | **yes (new)**, finite and \|norm−1\| ≤ `max_norm_deviation` | `test_the_verdict_needs_a_norm_within_the_bound_and_never_normalises` (7 cases), `test_the_norm_bound_decides_the_verdict_end_to_end` (3); mutants R1, M1, M2 killed |
| served vector length | `judge`; K1 `_reduce` (native) refuses first | yes (fail in `judge`; DIMENSION_MISMATCH refusal via K1) | `test_the_verdict_passes_at_the_tolerance_and_fails_below_it_or_at_another_length`; `test_a_k1_embedding_error_is_one_refusal_line[DIMENSION_MISMATCH]` |
| served count | `judge` raises; K1 `parse_embeddings` refuses first | yes (refusal) | `test_the_verdict…` (`ReferenceCheckError`); `…refusal_line[expected]` |
| served finiteness | K1 `parse_embeddings`/`_reduce` refuse; `judge` `isfinite(norm)` | yes (refusal / fail) | K1's own tests; `(1e30, 0.5)` case |
| reference count, dimension, finiteness, one length | `load_reference` / `_check_runs` | **yes (new)**, refusal before HTTP | `test_a_malformed_reference_file_is_a_reference_refusal` (26), `test_a_non_finite_reference_number_is_refused` (4), property test; mutant M4 killed |
| reference identity, inputs, limits, baseline, native mode | `load_reference` | yes, refusal | `test_a_reference_made_for_something_else_is_refused` (8) |
| fit: lowest pairwise cosine | `fit_min_cosine` | writes `min_cosine` | existing fit tests |
| fit: largest \|norm−1\| | `fit_max_norm_deviation` | **writes `max_norm_deviation` (new)** | `test_the_norm_fit_is_the_largest_deviation_of_any_run_rounded_up`, ceiling property |

#### Class sweep (b): every family capability and where it is enforced

| Capability (`Family`) | Enforced by | Before hash / bind / load? | Test |
|---|---|---|---|
| `model_type` | `check_weights_config` in `load_engine` | after the manifest hash, before weights | `test_weights_config_refusals` |
| `forbidden_dtypes` | `validate_embed_server_reads` | yes | `test_refusal[...dtype='float16']` |
| `attn_implementations` (**new**) | `validate_embed_server_reads` | yes | `test_embedding_gemma_2_with_sdpa_is_refused_by_the_shared_validator`, `test_embedding_gemma_2_with_sdpa_is_refused_before_hashing_binding_or_loading[serve,reference,compare]` (spies on `verify_model` in both modules, `load_engine`, `serve`, `run_reference`, `load_reference`, `run_compare`: none touched), `test_a_kernel_the_family_supports_is_accepted` (EG2 eager, Qwen sdpa, Qwen eager); mutant M8 killed |
| `accepts_instruction` | `validate_embed_server_reads` | yes | `test_refusal[...default_instruction...]`, `QWEN_REFUSALS` |
| image limits (`min/max_pixels` vs `max_soft_tokens`) | `validate_embed_server_reads` | yes | `test_refusal`, `test_qwen_refusal` |
| `native_dimension_path` | `check_weights_config` | reads `config.json` only, after the hash | `test_weights_config_refusals` |
| `pooling`, `reference_text_prompt_name`, `reference_extra_dtypes` | used, not limits (no invalid profile value) | — | existing |

#### Class sweep (c): exceptions that can reach `cli.main`

| Command | Exception | Maps to |
|---|---|---|
| all | `argparse` errors | `SystemExit(2)` from argparse (before the profile; unchanged) |
| all | `ConfigurationError` while reading the profile | `refused: …`, 2 |
| `serve` | `ManifestError`, `ServerStartError`, `OSError` | `refused: …`, 2 (bounded last line) |
| `serve` | `ShutdownIncomplete` | `stopped incompletely: …`, 3 |
| `serve` | any other `Exception`/`KeyboardInterrupt` after settings | `failed: <Type>`, 1 |
| `reference`, `compare` | `ConfigurationError` (K1 settings), `ManifestError`, `ReferenceCheckError` (inputs, batch sizes, missing libraries, reference file), `OSError` | `refused: …`, 2 |
| `compare` | K1 `EmbeddingError`: `HTTP_ERROR` (status, `ConnectError`/`ConnectTimeout`, `ReadTimeout`), `RESPONSE_INVALID`, `DIMENSION_MISMATCH` | **`refused: <code>: <detail>`, 2 (new)** |
| `reference`, `compare` | any other `Exception`/`KeyboardInterrupt` after settings (e.g. a CUDA out-of-memory, a library `ValueError`, a programming error) | **`failed: <Type>`, 1 (new; was a traceback)** |
| `compare` | item verdict fail | JSON line on stdout, `MISMATCH` 4 |

#### Review probes and mutants (scratch copies under `.tmp/T-018-p2/fix1/`, removed; the reviewer's own scratch left untouched)

Probe copy changes: `setup()` adds the now-required `max_norm_deviation`; the F6 probe sets `sdpa` itself; the two E2 assertions split into two probes. Each probe asserts the **old defect**, so a failure means the defect is gone:

```
FAILED ...::test_observe_http_500_escapes            Failed: DID NOT RAISE EmbeddingError
FAILED ...::test_observe_scaled_vectors_pass_comparison   AssertionError: assert 4 == 0
FAILED ...::test_observe_eg2_sdpa_accepted           ConfigurationError: ...server.attn_implementation: sdpa is not supported by embedding_gemma2 in the pinned trans…
FAILED ...::test_observe_bad_reference_json_escapes  Failed: DID NOT RAISE JSONDecodeError   (stderr: refused: the reference file is not UTF-8 JSON (JSONDecodeError))
FAILED ...::test_observe_missing_identity_escapes    Failed: DID NOT RAISE KeyError
5 failed in 3.32s
```

The scaled probe's own stdout: `{"step": "compare", "passed": false, ..., "cosines": [1.0, 1.0, 1.0, 1.0], "norms": [2.0, 2.0, 2.0, 2.0]}`.

Mutants (in-memory `exec` over the imported module, as the reviewer's `run_mutant.py`; M8 on a scratch package copy because `exec` over `config.py` would create a second `ConfigurationError`); `pytest -x`:

| Id | File, change | Result |
|---|---|---|
| control | `reference.py` unmutated | `125 passed in 17.09s` |
| R1 (review) | `norm = 2 * math.sqrt(...)` | **killed** `1 failed, 12 passed` |
| M1 | norm dropped from `passed` | **killed** `1 failed, 13 passed` |
| M2 | bound replaced by constant `0.01` | **killed** `1 failed, 17 passed` |
| M4 | `_check_runs` finiteness dropped | **killed** `1 failed, 96 passed` |
| control | `__main__.py` unmutated | `125 passed in 15.48s` |
| M6 | `EmbeddingError` dropped from the refusals | **killed** `1 failed, 108 passed` |
| control | `config.py` copy unmutated (`-k "sdpa or kernel"`) | `7 passed, 278 deselected` |
| M8 | attention check disabled | **killed** `1 failed, 3 passed, 278 deselected` |

#### Commands and real output

- Environment: interpreter `<project>/.venv/Scripts/python.exe` (worktree has none), `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src` (probes: `src;tests`); every pytest through `tools/agent-workflow/test_slot.py`, one process, no `-n`, `-X faulthandler`, `--basetemp=.tmp/T-018-p2/fix1/bt` deleted after each run.
- Focused sets `tests/test_embed_server_*.py tests/test_k1_*.py tests/test_field_contracts.py tests/test_real_contracts.py`: **`1691 passed, 34 skipped in 410.03s (0:06:50)`** (was 1614 + 34 before this round).
- After the final line-wrapping edits, `tests/test_embed_server_{reference,config,cli,identity}.py`: **`320 passed in 33.68s`**.
- `ruff check …/reference.py`: `D:\Project_github\prompt-enhancer-pipeline\.venv\Scripts\python.exe: No module named ruff`; `ruff format --check`: **not_run** (same cause; no install, per brief).
- `git diff --check`: exit 0. No Git writes.

#### Configuration audit (fix round 1: rows new or changed)

| Setting / literal | Consumer | Validation | Example profile | Override test | Refusal test / class |
|---|---|---|---|---|---|
| `vlm.backends.<name>.server.reference_check.max_norm_deviation` (new) | `reference-check compare` → `reference.judge` | schema `[0, 1)` or `fit_from_bench`, required in `reference_check`; `validate_reference_check_reads` refuses missing, sentinel, NaN, inf; bool refused by the schema number | dev EG2 0.0016, dev Qwen 0.0023 (evidence `runs/T-018-p2/<m>/reference/reference.json`, the table above); base `fit_from_bench` | `test_the_norm_bound_decides_the_verdict_end_to_end[1.0003-0.001 → 0, 1.0003-0.0002 → 4]`; `test_reference_check_reads_a_measured_tolerance` (0.0023, 0, 0.5) | `test_reference_check_refuses_…` (missing, sentinel, −0.0001, 1, True, string, NaN, inf), `test_compare_refuses_an_unresolved_tolerance_before_contacting_the_server`; configurable |
| same key, not read by `serve` / `reference` | — | — | — | `test_the_server_does_not_read_the_tolerance` (absent; both sentinels) | configurable (documented non-consumers) |
| `server.reference_check.min_cosine` | unchanged | `required` now beside the new key | dev comments moved into a block mapping | existing | existing; configurable |
| `server.attn_implementation` × family | `validate_embed_server_reads` | value must be in `Family.attn_implementations` | dev EG2 comment: eager only, sdpa refused when the profile is read; Qwen `deploy_required` | `test_a_kernel_the_family_supports_is_accepted` (EG2 eager, Qwen sdpa, Qwen eager) | `test_embedding_gemma_2_with_sdpa_is_refused_…` (validator + 3 commands); configurable value, closed by an artifact-bound fact |
| `Family.attn_implementations` | the row above | — | — | — | artifact-bound: transformers 5.23.0 `modeling_embedding_gemma2.py:649`, `gemma4/modeling_gemma4.py:1968-1971`, `modeling_utils.py:1665-1669`, `qwen3_vl/modeling_qwen3_vl.py:606` |
| `FIT_DECIMALS` for the norm fit (ceiling) | `reference.ceil_to` | — | — | `test_the_norm_fit_rounds_up_to_four_decimals`, ceiling property | artifact-bound (recorded in fit.json) |
| `1.0` (unit length) in `judge`/`norm_deviation` | — | — | — | — | protocol-bound (the servers return L2-normalised vectors, K1 native mode) |
| floats-only rule of reference vectors | `_check_runs` | — | — | — | artifact-bound (the reference step writes `float` values only) |
| `FAILED = 1` for `reference-check` | `_reference_check` | — | — | `test_an_unexpected_error_after_the_profile_is_one_failed_line` | protocol-bound (the existing `serve` status) |
| test-only: `CHECK` tolerances, `ScaledEngine`, MockTransport handlers, fixture kernel per family, `SERVER_VALUES` eager | tests | — | — | — | test-only, labelled |

#### Changed files (this round, unstaged on top of the staged P2 change)

`src/prompt_enhancer/embed_server/{reference,__main__,families}.py`, `src/prompt_enhancer/config.py`, `contracts/schemas/enhancer_config.v0.schema.json`, `configs/enhancer.dev.example.yaml`, `configs/enhancer.example.yaml`, `docs/stages/K1-embed.md`, `tests/embed_server_fixtures.py`, `tests/test_embed_server_config.py`, `tests/test_embed_server_reference.py`, this task file.

#### not_run

`ruff check` / `ruff format --check` (ruff not installed); full suite (PM-owned); Linux run of the closed-port case (the test accepts `ConnectError` or `ConnectTimeout`: Windows gave `ConnectTimeout` against the 1 s test connect timeout); any GPU or model run (forbidden; F3 stays open as reported, not rerun or explained).

#### Noticed, not done

- `refused: {error}` for an `OSError` in `reference-check` (pre-existing) prints the OS message, which can hold a path; `serve` does the same.
- The full example's Qwen `max_pixels` 1843200 still differs from the processor's 1310720 (carried from Stage B).
- `docs/stages/K1-embed.md` paragraph 15 still says the adapters are "not yet run" (carried from the review).

#### Cleanup

`.tmp/T-018-p2/fix1/` (scripts, logs, mutant copies, basetemp) deleted after this report; the evidence they produced is quoted above. The reviewer's scratch under `.tmp/T-018-p2/` was left for the PM. No process started by this round is running.
