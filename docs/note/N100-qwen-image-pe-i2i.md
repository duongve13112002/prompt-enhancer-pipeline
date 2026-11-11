# N100 Qwen-Image-2.1-PE-I2I: official edit-prompt rewriter and its system prompt

- **Source:** https://huggingface.co/Qwen/Qwen-Image-2.1-PE-I2I (README.md, system_prompt.txt)
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (model card in full; the first 14,000 of 18,344 bytes of system_prompt.txt)
- **Checked:** 2026-11-11
- **Used in:** S2, S4, S5 (edit)

## What this project relies on

A fine-tuned 9B vision-language rewriter that "takes a vague editing instruction plus input image(s) and produces a precise, actionable prompt"; run with thinking enabled; output JSON `rewritten_prompt`, `wh_ratio`, `ratio_follow`. Its system prompt branches on intent ("this picture changed" versus "a new picture of this subject"), names two symmetric failures ("Leakage" and "Under-editing"), and states: "Preservation locks content, never edit strength"; "A preservation description reads to the model as a generation instruction"; "When identity comes from a reference image, point at that image rather than describing features in words"; "Do not add operations the user did not request". Multi-image prompts refer to inputs as `<image1>`, `<image2>`.

## Caveats

Vendor guidance for its own editor, not an independent measurement. Licence: Qwen Research License. Its output carries an aspect-ratio field, which this project excludes (R-005). The last quarter of the system prompt was not read.
