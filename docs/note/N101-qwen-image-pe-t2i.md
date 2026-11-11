# N101 Qwen-Image-2.1-PE-T2I: official generate-prompt rewriter

- **Source:** https://huggingface.co/Qwen/Qwen-Image-2.1-PE-T2I (README.md, system_prompt.txt)
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (model card in full; the first 9,000 bytes of system_prompt.txt)
- **Checked:** 2026-11-11
- **Used in:** S2, S4

## What this project relies on

A fine-tuned 9B rewriter that "turns a brief image request in any language into a detailed English prompt plus a recommended aspect ratio"; the example runs with `enable_thinking=True` and `max_new_tokens=16256`. Step 1 of its system prompt splits the brief into what the user fixed (text strings, named objects, counts, colours, positions) and what is open, treats instructions about the job as not content, and copies text strings "character for character, in their own script".

## Caveats

Licence: Qwen Research License. It encourages hedged wording, while the example profile here bans it: such style rules belong to the profile. The rest of the system prompt was not read.
