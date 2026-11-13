# N136 Codex CLI built-in image generation

- **Source:** https://developers.openai.com/codex/image-generation ; https://developers.openai.com/codex/pricing ; github.com/openai/codex at commit e73744b3f46dcc6786ab08405c2715c898511281 (`codex-rs/ext/image-generation/src/lib.rs`, `.../tool.rs`, `.../artifact.rs`, `codex-rs/features/src/lib.rs`, `codex-rs/skills/src/assets/samples/imagegen/SKILL.md`)
- **Class:** documented and source-verified by a research agent; the PM did not re-read
- **Verified by:** a research agent on 2026-11-13
- **Checked:** 2026-11-13
- **Used in:** task T-034, docs/stages/G-evaluation.md (E8 stand-in), .agents/policy/cli/codex.md, the `codex-image` role in tools/agent-workflow/codex_profiles.json

## What this project relies on

- The tool is namespace `image_gen`, name `imagegen` (the tooling's flattened `image_genimagegen`); feature flag `image_generation`, stable and on by default; `--enable` / `--disable image_generation` or `[features] image_generation` in `config.toml`; available only with OpenAI authentication.
- Model: `const IMAGE_MODEL: &str = "gpt-image-2";`. Docs: "Built-in image generation uses gpt-image-2 and counts toward your general Codex usage limits." and "Image generations use included limits 3–5x faster on average than similar turns without image generation."
- Editing: the tool has an edit request with up to 5 input images (`MAX_EDIT_IMAGES = 5`); docs: "Attach an existing image with -i or --image when it should guide the result."
- Output: `generated_images/<session_id>/<call_id>.png` under the save root (the Codex home by default, `<cwd>/generated_images` as fallback); the project's Codex hooks move each file into `runtime.hooks.image_output_dir`.

## Caveats

Not verified: that the tool works in non-interactive `codex exec` (the source shows no interactive dependency; nothing was run) and that `--image` is accepted by plain `codex exec`. The images show what one OpenAI model renders from the enhancer's output; they say nothing about the user's own image model. Task T-034 runs one generation and one edit first and records the result here.
