# N008 SigLIP 2 (arXiv 2502.14786) and transformers documentation

- **Source:** https://arxiv.org/abs/2502.14786
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** K1 embedder candidates (baseline)
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Sizes B 86M, L 303M, So400m 400M, g 1B; ImageNet zero-shot 83.2 to 84.1 (So400m) and 84.5 to 85.0 (g). Text is a hard 64 tokens, lowercased, and must be padded to max_length 64 (https://huggingface.co/docs/transformers/model_doc/siglip2). About 10% of WebLI data is non-English; Vietnamese is not named. NaFlex checkpoints keep native aspect ratio. Apache-2.0.

## Caveats

Long `info.txt` descriptions must be shortened. Embedding dimension 1152 for So400m is from memory (unverified).
