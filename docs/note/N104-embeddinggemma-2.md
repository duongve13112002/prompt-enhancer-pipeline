# N104 google/embeddinggemma-2 model card

- **Source:** https://huggingface.co/google/embeddinggemma-2
- **Class:** source-verified (documented, source-verified, experiment or assumption)
- **Verified by:** PM read the primary source on 2026-11-11 (first 12,000 bytes of the model card)
- **Checked:** 2026-11-11
- **Used in:** K1 embedder candidates, dev profile

## What this project relies on

Apache-2.0. 740M parameters (text 270M, vision 170M, audio 300M); text, images, video and audio in one 768-dimensional space; truncated dimensions 128, 256, 512; 8,192-token context; "understands 100+ languages". Reported at 768 dimensions: MTEB multilingual v2 61.36; MMEB v2 image 57.28; MMEB v2 overall 59.01. Task prefixes apply to text only. "Run inference in `bfloat16` or `float32`. Do not use `float16`." Unused modality encoders can be left unloaded (text and image: 440M).

## Caveats

Vendor-reported figures; no figure for Vietnamese. The rest of the card was not read.
