# N119 Composed image retrieval: CIReVL, MagicLens, MegaPairs

- **Source:** https://arxiv.org/abs/2310.09291 ; https://arxiv.org/abs/2403.19651 ; https://arxiv.org/abs/2412.14475
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it (abstracts and snippets)
- **Checked:** 2026-11-11
- **Used in:** S1 (edit queries)

## What this project relies on

CIReVL is training-free: a model captions the reference image, a language model rewrites the caption with the modification, and retrieval runs on the rewritten text.

## Caveats

No controlled comparison of a text-only target query against an image-plus-text query was found.
