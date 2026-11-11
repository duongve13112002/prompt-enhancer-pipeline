# N112 Prompt rewriters: PromptEnhancer and FaithRewriter

- **Source:** https://arxiv.org/abs/2509.04545 ; https://arxiv.org/abs/2606.08492
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (abstracts)
- **Checked:** 2026-11-11
- **Used in:** S4, G

## What this project relies on

PromptEnhancer trains a chain-of-thought rewriter by reinforcement learning with a reward model built on "a systematic taxonomy of 24 key points" and decouples the rewriter from the generator. FaithRewriter: "the rewriter may over-infer missing details, causing an intent-generation gap"; its augmentations "are distilled into a small-scale LLM".

## Caveats

Both report gains on the authors' own benchmarks. No independent comparison was found, so the evaluation keeps a no-enhancement baseline.
