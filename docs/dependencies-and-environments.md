# Dependencies and environments

No lockfile exists and none may be invented. This file records facts and the planned profile split.

## Facts

| Fact | Source | Class |
| --- | --- | --- |
| The deployment server is run by the user outside this repository; models are reached over HTTP (R-018) | user statement | user requirement |
| Hardware is not fixed: GPU count, VRAM, CUDA, torch and Python versions are read on each machine by the environment-report task and recorded here | user statement | user requirement |
| Dev profile: a small-VRAM GPU; a small quantised model with its projector file served by `llama-server`; the embedder through its Python library | decision 20; [N103](note/N103-unsloth-qwen3-5-4b-gguf-sizes.md), [N055](note/N055-llama-cpp-server.md), [N122](note/N122-llama-cpp-multimodal-status.md) | files verified in notes; GPU and OS support of the installed build to check on the machine |
| A vLLM release can pin a particular torch version | [N016](note/N016-vllm-torch-pin.md) | carried lead from an earlier project: re-check against the user's server before relying on it |

## Planned profiles

| Profile | Contents | Status |
| --- | --- | --- |
| `.venv` (main) | client, embedder, tables, index build, tooling | to create by the environment task |
| CPU profile | the bounded test suite and replay tests, no GPU ([test-plan](test-plan.md), [ci](ci.md)) | to create |

Whether the embedder shares the main environment is decided by the environment task, after it reads the machine. Measured per machine, never asked: Python version, whether `pip` works online or only from a wheelhouse, CPU cores, GPUs and VRAM. Tested versions are recorded here once measured.
