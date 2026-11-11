# N123 Unsloth Qwen3.5 guide and quantisation benchmarks

- **Source:** https://unsloth.ai/docs/models/qwen3.5 ; https://unsloth.ai/docs/models/qwen3.5/gguf-benchmarks
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** docs/dev-machine.md, E15

## What this project relies on

The guide's examples use a 4-bit dynamic quantisation and turn thinking off with `--chat-template-kwargs '{"enable_thinking":false}'`; it says thinking is disabled by default for the small models. Benchmark table for a larger model: Q3_K_M mean KL divergence 0.0324 against 0.0192 for Q4_K_M.

## Caveats

No figure for the 4B model. The guide's size figures for the 4B disagree with the Hub file list; this project uses the file list (N103).
