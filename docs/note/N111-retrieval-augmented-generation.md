# N111 Retrieval-augmented image generation: ImageRAG, IA-T2I, Cross-modal RAG

- **Source:** https://arxiv.org/abs/2502.09411 ; https://arxiv.org/abs/2505.15779 ; https://arxiv.org/abs/2505.21956
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the three abstracts on 2026-11-11; method details come from a research agent
- **Checked:** 2026-11-11
- **Used in:** S1, S3

## What this project relies on

ImageRAG "dynamically retrieves relevant images based on a given text prompt, and uses them as context" without retrieval-specific training. IA-T2I has "an active retrieval module" that decides "whether a reference image is needed". Cross-modal RAG "decomposes both queries and images into sub-dimensional components" because "no single image contains all desired elements from a complex user query". Agent-read detail: ImageRAG asks a model which concepts are missing and writes one caption per concept as the query.

## Caveats

No figure from these papers is used. They pass reference images to the generator; this project writes text, so transfer is an inference.
