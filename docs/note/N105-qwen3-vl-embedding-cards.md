# N105 Qwen3-VL-Embedding and Qwen3-VL-Reranker model cards

- **Source:** https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B ; https://huggingface.co/Qwen/Qwen3-VL-Embedding-8B ; https://huggingface.co/Qwen/Qwen3-VL-Reranker-2B
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (2B card text; Hub metadata of the 8B and the reranker)
- **Checked:** 2026-11-11
- **Used in:** K1 embedder candidates, S1, E3, E5

## What this project relies on

Apache-2.0. 2B: embedding dimension up to 2048 (user-defined 64 to 2048); 8B: up to 4096; context 32k; "30+ Languages"; text, images, screenshots, video and mixed inputs. Instruction-aware: "using instructions (instruct) typically yields an improvement of 1% to 5%", and instructions are advised in English. MMEB-V2 "All" on the card: 73.2 (2B), 77.8 (8B). The reranker scores a (query, document) pair of any modality mix.

## Caveats

The languages are not listed, so Vietnamese must be tested (E1, E5). Vendor table.
