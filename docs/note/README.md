# Research notes index

Purpose: one note per external reference or research finding, so an agent can cite a claim and see how far it was verified. Usage: before using an external number, API behaviour or threshold, find its note here; if none exists, research it from the primary source and add one in the same change (AGENTS.md section 3, requirement R-021). Update this index on every note change.

Classes: **source-verified** = the PM or an agent read the primary source itself and recorded the exact figure; **documented** = a research agent read the primary page and the PM did not re-read it; **experiment** = measured in this repository; **assumption** = secondary source, search snippet, memory or proposal. A `documented` number is a lead, not evidence, until a task re-reads the source.

Two groups: N008 to N097 were carried over from the project the tooling came from (each says so in a `Carried over` line; numbers they mention for stages, experiments, requirements and decisions belong to that project); N000 and N100 onward were written for this repository on 2026-11-11.

Counts: assumption 7, documented 51, source-verified 17.

| ID | Note | Class | Used in |
| --- | --- | --- | --- |
| N000 | [Origin of this repository's design](N000-design-provenance.md) | assumption | all stages |
| N008 | [SigLIP 2 (arXiv 2502.14786) and transformers documentation](N008-siglip2.md) | documented | K1 embedder candidates (baseline) |
| N010 | [Qwen3-VL-Embedding (arXiv 2601.04720)](N010-qwen3-vl-embedding.md) | documented | K1, S1 |
| N014 | [Qwen3.8-27B model card and Hugging Face API record](N014-qwen3-8-27b.md) | source-verified | docs/serving.md, deployment overlay |
| N016 | [vLLM 0.33.0 requirements on PyPI](N016-vllm-torch-pin.md) | source-verified | docs/dependencies-and-environments.md |
| N017 | [vLLM SamplingParams and V1 guide](N017-vllm-sampling-params.md) | documented | contracts/vlm-request.md |
| N018 | [vLLM recipe for Qwen3.5](N018-vllm-qwen35-recipe.md) | documented | docs/serving.md |
| N047 | [Lance format](N047-lance.md) | assumption | K5 (option) |
| N048 | [FAISS on the GPU (wiki)](N048-faiss-gpu.md) | documented | K5 |
| N055 | [llama.cpp server README (llama-server)](N055-llama-cpp-server.md) | source-verified | docs/dev-machine.md, contracts/vlm-request.md |
| N056 | [unsloth/Qwen3.5-4B-GGUF file list](N056-unsloth-qwen3-5-4b-gguf.md) | source-verified | docs/dev-machine.md |
| N057 | [vLLM OpenAI-compatible server and multimodal inputs](N057-vllm-openai-server.md) | source-verified | contracts/vlm-request.md |
| N060 | [Claude Code memory (CLAUDE.md)](N060-claude-code-memory.md) | documented | docs/agents/research.md |
| N061 | [Claude Code subagents](N061-claude-code-sub-agents.md) | documented | docs/agents/research.md |
| N062 | [Claude Code hooks](N062-claude-code-hooks.md) | documented | docs/agents/research.md |
| N063 | [Claude Code settings and permissions](N063-claude-code-settings-permissions.md) | documented | docs/agents/research.md |
| N064 | [Claude Code headless mode and CLI reference](N064-claude-code-headless.md) | documented | docs/agents/research.md |
| N065 | [Claude Code skills](N065-claude-code-skills.md) | documented | docs/agents/research.md |
| N066 | [Claude Code best practices](N066-claude-code-best-practices.md) | documented | docs/agents/research.md |
| N067 | [AGENTS.md specification](N067-agents-md.md) | documented | docs/agents/research.md |
| N068 | [Codex AGENTS.md loading](N068-codex-agents-md.md) | documented | docs/agents/research.md |
| N069 | [Effective context engineering for AI agents](N069-anthropic-context-engineering.md) | documented | docs/agents/research.md |
| N070 | [Building effective agents](N070-anthropic-building-effective-agents.md) | documented | docs/agents/research.md |
| N071 | [How we built our multi-agent research system](N071-anthropic-multi-agent-research.md) | documented | docs/agents/research.md |
| N072 | [Effective harnesses for long-running agents](N072-anthropic-long-running-harness.md) | documented | docs/agents/research.md |
| N073 | [Writing effective tools for agents](N073-anthropic-writing-tools.md) | documented | docs/agents/research.md |
| N074 | [Large language models cannot self-correct reasoning yet (arXiv 2310.01798)](N074-self-correction-limits.md) | documented | docs/agents/research.md |
| N075 | [LLM evaluators recognise and favour their own generations (arXiv 2404.13076)](N075-self-preference.md) | assumption | docs/agents/research.md |
| N076 | [GitHub Spec Kit](N076-spec-kit.md) | documented | docs/agents/research.md |
| N077 | [IFScale: instruction-following at scale (arXiv 2507.11538)](N077-ifscale.md) | documented | docs/agents/research.md |
| N078 | [Lost in the middle (arXiv 2307.03172)](N078-lost-in-the-middle.md) | documented | docs/agents/research.md |
| N079 | [Fabricated references and plausible-but-wrong results in agent work](N079-agent-provenance-failures.md) | assumption | docs/agents/research.md |
| N093 | [Which Claude model to build with (Anthropic blog, 2026-11-09)](N093-claude-model-selection.md) | documented | .agents/policy/PM_POLICY.md section 2 |
| N094 | [OpenAI models and reasoning effort in Codex CLI](N094-openai-codex-models.md) | documented | .agents/policy/PM_POLICY.md, tools/agent-workflow/codex_profiles.json |
| N095 | [Gemini models, thinking level and Antigravity CLI (`agy`) settings](N095-gemini-agy-models.md) | documented | .agents/policy/PM_POLICY.md |
| N096 | [One instruction file and one skills folder for Claude Code, Codex and Antigravity](N096-agent-instruction-files.md) | assumption | CLAUDE.md, AGENTS.md, tools/agent-workflow/setup_project.py |
| N097 | [Keeping agents on target: context loss, goal drift and side work](N097-agent-focus-and-drift.md) | documented | .agents/policy/PM_POLICY.md section 6, footer rule 17, task template |
| N100 | [Qwen-Image-2.1-PE-I2I: official edit-prompt rewriter and its system prompt](N100-qwen-image-pe-i2i.md) | source-verified | S2, S4, S5 (edit) |
| N101 | [Qwen-Image-2.1-PE-T2I: official generate-prompt rewriter](N101-qwen-image-pe-t2i.md) | source-verified | S2, S4 |
| N102 | [Qwen3.8-27B: sizes, thinking default, sampling, image tokens](N102-qwen3-8-27b-serving-facts.md) | source-verified | docs/serving.md, deployment overlay, S0 (image downscale), contracts/vlm-request.md |
| N103 | [unsloth/Qwen3.5-4B-GGUF: file sizes including Q3_K_M](N103-unsloth-qwen3-5-4b-gguf-sizes.md) | source-verified | docs/dev-machine.md, dev profile |
| N104 | [google/embeddinggemma-2 model card](N104-embeddinggemma-2.md) | source-verified | K1 embedder candidates, dev profile |
| N105 | [Qwen3-VL-Embedding and Qwen3-VL-Reranker model cards](N105-qwen3-vl-embedding-cards.md) | source-verified | K1 embedder candidates, S1, E3, E5 |
| N106 | [Licences of the baseline retrieval models](N106-retrieval-baseline-licences.md) | source-verified | K1 embedder candidates, E3, E5 |
| N107 | [Cross-image information leakage in vision-language models (arXiv 2602.01984)](N107-delimiter-token-scaling.md) | source-verified | K2 (one image per call), S4 |
| N108 | [MIMIC: failure modes with multiple images (arXiv 2601.07812)](N108-mimic-multi-image.md) | source-verified | K2 |
| N109 | [VisRAG 2.0 / EVisRAG: per-image evidence before the answer (arXiv 2510.09733)](N109-evisrag.md) | source-verified | K2, K3 |
| N110 | [KITTEN: knowledge-intensive entity image generation (arXiv 2410.11824)](N110-kitten.md) | source-verified | S3 (scope filter), E9 |
| N111 | [Retrieval-augmented image generation: ImageRAG, IA-T2I, Cross-modal RAG](N111-retrieval-augmented-generation.md) | documented | S1, S3 |
| N112 | [Prompt rewriters: PromptEnhancer and FaithRewriter](N112-prompt-rewriters.md) | source-verified | S4, G |
| N113 | [Fusion functions for hybrid retrieval (arXiv 2210.11934)](N113-fusion-functions.md) | documented | S1 |
| N114 | [Dense retrievers on transliterated queries](N114-transliterated-queries.md) | assumption | K4, S1, E1 |
| N115 | [Entity linking with a model choosing from a shortlist and an explicit no-match](N115-llm-entity-linking-nil.md) | documented | S2 |
| N116 | [Image order changes vision-language model predictions (arXiv 2503.13792)](N116-image-order-bias.md) | documented | K2, S4 |
| N117 | [Format restrictions and reasoning quality (arXiv 2408.02442)](N117-format-restrictions-and-reasoning.md) | documented | S2, contracts/vlm-request.md, E10 |
| N118 | [Dependency-structured yes/no checks: DSG and CRAFT](N118-structured-checks.md) | documented | S5 |
| N119 | [Composed image retrieval: CIReVL, MagicLens, MegaPairs](N119-composed-image-retrieval.md) | documented | S1 (edit queries) |
| N120 | [Finding one item in a long context (Context Rot report)](N120-long-lists-in-context.md) | documented | S1 (no catalogue in the planner context) |
| N121 | [vLLM for Qwen3.8-27B: recipe, structured outputs, engine arguments](N121-vllm-qwen38-serving.md) | documented | docs/serving.md, contracts/vlm-request.md, deployment overlay |
| N122 | [llama.cpp server: image input, JSON schema, slots, open issues](N122-llama-cpp-multimodal-status.md) | documented | docs/dev-machine.md, contracts/vlm-request.md, E12 |
| N123 | [Unsloth Qwen3.5 guide and quantisation benchmarks](N123-unsloth-quant-benchmarks.md) | documented | docs/dev-machine.md, E15 |
| N124 | [Incremental indexing and orphan cleanup](N124-incremental-indexing.md) | documented | K0, docs/execution.md |
| N125 | [Reader snapshots and deleting vectors](N125-snapshots-and-vector-deletion.md) | documented | K0, K5 |
| N126 | [Content-addressed caches for derived artifacts](N126-content-addressed-caches.md) | documented | K0, docs/execution.md |
| N127 | [Claude prompting best practices](N127-claude-prompting-best-practices.md) | documented | AGENTS.md, .agents/policy/roles/, .agents/policy/cli/claude.md, docs/agents/research.md |
| N128 | [OpenAI guidance for Codex and its models](N128-codex-prompting-guidance.md) | documented | .agents/policy/cli/codex.md, .agents/policy/PM_POLICY.md |
| N129 | [Google prompt design strategies for Gemini and its agent template](N129-gemini-prompting-guidance.md) | documented | .agents/policy/cli/antigravity.md |
| N130 | [Antigravity: rule files, modes, permissions, headless runs](N130-antigravity-rules-and-headless.md) | documented | AGENTS.md size budget, .agents/policy/cli/antigravity.md |
| N131 | [Text inside images acting as instructions](N131-image-prompt-injection.md) | documented | S3, S4, docs/rules/VLM_CALL_RULES.md |
| N132 | [Retrieval evidence for Vietnamese and rare entities](N132-multilingual-retrieval-evidence.md) | documented | G, E1, E4 |
| N133 | [Benchmarks for instruction-based image editing](N133-edit-evaluation-benchmarks.md) | documented | G (downstream evaluation) |
| N134 | [GitHub repository visibility changes and Actions billing](N134-github-visibility-and-actions.md) | assumption | docs/ci.md, task T-022 |
| N135 | [Usage-limit reset times and in-session scheduling](N135-quota-reset-and-scheduling.md) | documented | PM_POLICY section 1, ORCHESTRATION, tasks T-025, T-026 |
| N136 | [Codex CLI built-in image generation](N136-codex-image-generation.md) | documented | task T-034, G (E8 stand-in), .agents/policy/cli/codex.md |
| N137 | [Public guidance on multi-agent coding workflows](N137-orchestration-guidance-review.md) | documented | decisions 44 to 48, ORCHESTRATION, PM_POLICY, AGENTS.md |
