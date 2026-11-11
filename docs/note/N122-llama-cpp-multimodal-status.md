# N122 llama.cpp server: image input, JSON schema, slots, open issues

- **Source:** https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md ; https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md ; https://github.com/ggml-org/llama.cpp/issues/22582 ; https://github.com/ggml-org/llama.cpp/issues/20081
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it (the README was long and partly unread)
- **Checked:** 2026-11-11
- **Used in:** docs/dev-machine.md, contracts/vlm-request.md, E12

## What this project relies on

Chat route: `image_url` as a URL, base64 string or data URI; `--mmproj` loads the projector; `response_format` accepts a JSON schema; request field `chat_template_kwargs` and flag `--chat-template-kwargs`; `--reasoning-budget`; `-np/--parallel` slots; prompt caching on by default. Open issues: slow image projection for a Qwen3.5 model in the server (22582) and Vulkan failures with a projector (20081).

## Caveats

Not stated in the documents read: a JSON schema together with image input, several images in one message, how context is split across slots. Whether the issues are fixed in the installed build is unknown.
