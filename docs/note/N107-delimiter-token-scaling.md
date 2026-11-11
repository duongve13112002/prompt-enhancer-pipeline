# N107 Cross-image information leakage in vision-language models (arXiv 2602.01984)

- **Source:** https://arxiv.org/abs/2602.01984
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (abstract)
- **Checked:** 2026-11-11
- **Used in:** K2 (one image per call), S4

## What this project relies on

"performance declines when multiple images are provided as input. One major reason is the cross-image information leakage, where the model struggles to distinguish information across different images." Existing delimiter tokens "fail to effectively block cross-image information leakage".

## Caveats

Abstract only. The paper's fix changes hidden states and is not available through an HTTP API; it is cited here only as evidence for reading one image per call.
