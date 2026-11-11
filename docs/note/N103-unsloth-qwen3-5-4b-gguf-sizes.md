# N103 unsloth/Qwen3.5-4B-GGUF: file sizes including Q3_K_M

- **Source:** https://huggingface.co/unsloth/Qwen3.5-4B-GGUF
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (file list through the Hub API; head of the model card)
- **Checked:** 2026-11-11
- **Used in:** docs/dev-machine.md, dev profile

## What this project relies on

Licence apache-2.0, base model Qwen/Qwen3.5-4B. File sizes in decimal GB: Q3_K_S 2.11, Q3_K_M 2.29, Q4_K_M 2.74, UD-Q4_K_XL 2.91, Q5_K_M 3.14, Q6_K 3.53, Q8_0 4.48, BF16 8.42. Projector files for image input: mmproj-F16 0.67, mmproj-BF16 0.68, mmproj-F32 1.33. The card states a context of 262,144 tokens and 201 languages and dialects.

## Caveats

The quality of a 3-bit 4B model for this task is unmeasured (E15). Whether this model thinks by default was not read from the card.
