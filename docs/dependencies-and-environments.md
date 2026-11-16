# Dependencies and environments

No lockfile exists and none may be invented. This file records facts and the planned profile split.

## Facts

| Fact | Source | Class |
| --- | --- | --- |
| The deployment server is run by the user outside this repository; models are reached over HTTP (R-018) | user statement | user requirement |
| Hardware is not fixed: GPU count, VRAM, CUDA, torch and Python versions are read on each machine by the environment-report task and recorded here | user statement | user requirement |
| Dev profile: a small-VRAM GPU; a small quantised model with its projector file served by `llama-server`; the embedder through its Python library | decision 20; [N103](note/N103-unsloth-qwen3-5-4b-gguf-sizes.md), [N055](note/N055-llama-cpp-server.md), [N122](note/N122-llama-cpp-multimodal-status.md) | files verified in notes; GPU and OS support of the installed build to check on the machine |
| A vLLM release can pin a particular torch version | [N016](note/N016-vllm-torch-pin.md) | carried lead from an earlier project: re-check against the user's server before relying on it |
| `markdown-it-py==4.2.0` is pinned in the `test` extra (`pyproject.toml`); the report lint `tools/agent-workflow/report_lint.py` reads Markdown with it and refuses another version with exit 2 (`PARSER_VERSION`). Its one dependency installed with it in `.venv` is `mdurl 0.1.2` (observed, not pinned) | decision 127; T-021 phase P2 | pinned; transitive version observed |

| The embedding server runs in a separate GPU environment `.venv-gpu`, so the CPU `.venv` never holds torch; its pins are in `requirements-gpu.txt`: `torch==2.14.1+cu130` (download.pytorch.org cu130 index; the Windows cp312 wheel is 1,990,608,063 bytes by `Content-Length`; the driver reports CUDA 13.4), `transformers==5.23.0` (stable on PyPI 2026-11-13; `embedding_gemma2` is in its source and the v5.21 release notes name its processors) | decisions 138 and 139; T-018 research | pinned and installed (T-018 P2, below) |
| `.venv-gpu` on the dev machine (T-018 P2, 2026-11-17): Python 3.12.10; pinned `torch 2.14.1+cu130`, `torchvision 0.29.1+cu130`, `transformers 5.23.0`, `tokenizers 0.23.3`, `safetensors 0.8.0`, `huggingface-hub 2.4.0`, `sentence-transformers 6.3.0` (the reference check only); resolver-chosen and observed, not pinned: `numpy 2.5.5`, `pillow 11.3.0`, `pyarrow 26.0.1`, `scipy 1.18.1`, `scikit-learn 1.9.1`. The package itself is not installed: it runs from `PYTHONPATH=<tree>/src`, as in the CPU `.venv`. 57 wheels, 2,130,737,465 bytes downloaded (`pip install --dry-run --report`, sizes from the index), 3.6 GB installed | `torch.version.cuda` `13.0` and `torch.cuda.get_arch_list()` `['sm_75', 'sm_80', 'sm_86', 'sm_90', 'sm_100', 'sm_120']` read without creating a CUDA context | build metadata read; running on the GPU is checked by the reference check |

## Planned profiles

K5 adds `numpy>=2.0.0` to the CPU/client dependencies (decision 134). The API floor follows bounded candidate merging with `np.concat`, documented in installed numpy source as added in 2.0. Local validation uses PM-installed numpy 2.5.5; that distribution requires Python >=3.12. The project's declared Python minimum is unchanged. Python 3.11 with a resolver-selected older numpy and other numpy releases are `not_run`, rather than inferred compatible. No GPU dependency or FAISS dependency is added, and this task installs nothing.

| Profile | Contents | Status |
| --- | --- | --- |
| `.venv` (main) | client, embedder, tables, index build, tooling | to create by the environment task |
| CPU profile | the bounded test suite and replay tests, no GPU ([test-plan](test-plan.md), [ci](ci.md)) | to create |

Whether the embedder shares the main environment is decided by the environment task, after it reads the machine. Measured per machine, never asked: Python version, whether `pip` works online or only from a wheelhouse, CPU cores, GPUs and VRAM. Tested versions are recorded here once measured.
