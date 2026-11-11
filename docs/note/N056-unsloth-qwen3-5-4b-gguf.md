# N056 unsloth/Qwen3.5-4B-GGUF file list

- **Source:** https://huggingface.co/unsloth/Qwen3.5-4B-GGUF
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the Hugging Face API record (files and sizes) on 2026-11-09; the model card text was not read
- **Checked:** 2026-11-09
- **Used in:** docs/dev-machine.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Task image-text-to-text, licence apache-2.0, base model Qwen/Qwen3.5-4B, created 2026-03-02. Two-bit files: UD-IQ2_XXS 1.52 GB, UD-IQ2_M 1.76 GB, UD-Q2_K_XL 1.94 GB; other quantisations from Q3 up to BF16 (8.42 GB). Projector files needed for images: mmproj-F16 0.67 GB, mmproj-BF16 0.68 GB, mmproj-F32 1.33 GB (sizes in decimal GB from the API).

## Caveats

Which 2-bit file the user means is not specified. The quality of a 2-bit 4B model for this task is unmeasured. Whether Qwen3.5-4B thinks by default was not checked.
