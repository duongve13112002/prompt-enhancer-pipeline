# Dev machine (small-VRAM GPU)

Source: decision 20 and R-020. Nothing here was run or measured; the GPU and its VRAM are read per machine by the environment-report task ([dependencies-and-environments](dependencies-and-environments.md)). **Role: smoke tests and plumbing only.** Dev results show that the code works, never quality or timing, because the dev model differs from the deployment model (see the table below and [serving](serving.md)).

## What runs here

| Item | Choice | Evidence |
| --- | --- | --- |
| Test model | Qwen3.5-4B GGUF from `unsloth/Qwen3.5-4B-GGUF`: file `Qwen3.5-4B-Q3_K_M.gguf` (2.29 GB) plus the projector file `mmproj-F16.gguf` (0.67 GB) for images | decision 20; [N103](note/N103-unsloth-qwen3-5-4b-gguf-sizes.md), [N056](note/N056-unsloth-qwen3-5-4b-gguf.md) |
| Server | `llama-server` started with `--mmproj`; thinking off through the chat-template switch (per request or at the server). Read `--help` of the installed build first, because flag names change between builds | [N055](note/N055-llama-cpp-server.md), [N122](note/N122-llama-cpp-multimodal-status.md) |
| Client | The client the deployment uses; only `base_url` and the model name differ (provider `openai_compatible`, R-018) | [N055](note/N055-llama-cpp-server.md) |
| Embedder | `google/embeddinggemma-2` and `Qwen/Qwen3-VL-Embedding-2B` (decision 137), each from its original weights through this project's embedding server in `.venv-gpu` ([K1-embed](stages/K1-embed.md)); the dev comparison keeps one model's weights (decision 139). Qwen3-VL-Embedding-8B is supported in the profiles and the manifest and never runs here. Not llama.cpp | [N104](note/N104-embeddinggemma-2.md), [N105](note/N105-qwen3-vl-embedding-cards.md) |
| Knowledge base | The example under `kb/_example/` or a small copy | [kb/README.md](../kb/README.md) |

Model files go under `models/` (Git-ignored) with a manifest entry before use ([model-storage](model-storage.md)). The server listens on loopback only, because the dev server has no authentication.

## Differences from the deployment

| | Dev machine | Deployment |
| --- | --- | --- |
| Model | small, 3-bit quantised | the profile's deployment model ([serving](serving.md)) |
| Engine | `llama-server`, GGUF | the user's server, outside this repository |
| Concurrency | what the VRAM allows, often one request | many concurrent requests |
| Thinking, structured output, images | to be probed (E12) | to be probed (E13) |
| Use | smoke, plumbing | quality, latency, throughput |

Why dev results never fit a sentinel: `fit_from_gt`, `fit_from_bench` and `fit_from_pilot` values depend on the model, the quantisation, the prompt and the image size, and all of these differ here (R-020). A value fitted on the dev model is a value for the dev model only and is never written into a deployment profile.

## VRAM before and after every job

A small GPU cannot hold the model server and the embedder together. Before a job, record the VRAM in use and the processes holding it; load one heavy model at a time; stop the server when its queue drains; after the job, confirm that VRAM is back at the recorded baseline and that no process of yours remains on the GPU ([PLATFORM_GPU_RULES](rules/PLATFORM_GPU_RULES.md)). A difference from the baseline is a defect to report.

## Capability probe

The first dev smoke test (E12) sends requests to the dev server and records each result separately, because the open issues in [N122](note/N122-llama-cpp-multimodal-status.md) concern exactly these combinations:

1. one image in a message;
2. a JSON schema in `response_format`;
3. image plus JSON schema in the same request;
4. thinking off (the response carries no reasoning text);
5. several images in one message.

Each outcome is stored with the run ([VLM_CALL_RULES](rules/VLM_CALL_RULES.md)). A combination that fails is a recorded finding, not a reason to change the pipeline for the dev machine alone.

## Quantisation choice

The 3-bit file is the user's choice and a profile value (decision 20). Larger files also fit this machine: Q4_K_M 2.74 GB, Q6_K 3.53 GB, Q8_0 4.48 GB ([N103](note/N103-unsloth-qwen3-5-4b-gguf-sizes.md)). A measured divergence table between 3-bit and 4-bit exists only for a larger model ([N123](note/N123-unsloth-quant-benchmarks.md)); none exists for this one. If the dev model often returns invalid JSON, try a higher quantisation before suspecting pipeline logic (E15).

## Not verified

- Image input speed and correctness with this model in the installed build ([N122](note/N122-llama-cpp-multimodal-status.md)).
- JSON schema output together with images.
- Several images in one message.
- GPU and operating-system support of the installed build.
- Whether the model thinks by default.

Profile: [configs/enhancer.dev.example.yaml](../configs/enhancer.dev.example.yaml).
