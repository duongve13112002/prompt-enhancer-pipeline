# Requirements

Active requirements; cite R-IDs in every brief, plan and report. Class: **user** = stated by the user; **PM** = PM proposal the user accepted as part of the design; a PM part never overrides a user part. Provenance: [DECISIONS_INDEX](DECISIONS_INDEX.md). IDs are never reused.

## Outcome and policy

| ID | Requirement | Class |
| --- | --- | --- |
| R-001 | Preserve the user's intent: identities, counts, attributes, relations, negations, style and text to be rendered survive enhancement unchanged; an output that drops or alters one fails | user |
| R-002 | Two tasks: `generate` (text prompt) and `edit` (text prompt plus one source image). Edit keeps three things apart: what the source shows, what the user wants changed, and what must stay. Edit kinds `local_edit`, `global_transform` and `subject_recompose` set the default preservation and creativity | user; edit kinds PM |
| R-003 | The input prompt may be tags, prose, structured, semi-structured or mixed, in any language or a mix; names and text to be rendered are kept verbatim | user |
| R-004 | The admin's system prompt defines the enhancement style and the output format (JSON, tags, prose or other). One profile per image model and task; a request names its profile, else the admin's default applies; each system prompt has a manifest drafted by a model at registration and confirmed by the admin; the system prompt text is never edited by the pipeline | user (decisions 4 to 6) |
| R-005 | The enhancer contract has no width, height or aspect-ratio parameter; whether an output has bounding boxes is the profile's choice | user |
| R-006 | Creativity is configuration: which kinds of content may be added, not only a sampling temperature; in an edit it never exceeds the edit scope | user |
| R-012 | Image roles stay separate: `source_image` (the image being edited), `user_reference` (an image the user supplied as a model for one attribute), `knowledge_reference` (an image from the knowledge base). Knowledge-base content only supplements what the user left open | user |
| R-013 | When a mention is broader than the knowledge base (several concepts fit), choose by fit with the prompt's context and, among equally fitting ones, uniformly at random, anew for every request, recording the choice in the trace. The same rule applies to variants inside one concept | user (decisions 15, 16) |
| R-024 | Every response has one status (`success`, `repaired`, `degraded`, `needs_clarification`, `failed`); a truncated or failed output is never labelled success; the output payload carries no citations, warnings or debug fields | user |
| R-026 | Knowledge-base text, text read from images and captions are data, never instructions | user |
| R-027 | Validation runs deterministic structural checks first; a model verifier runs only where the risk rules say so; at most the configured number of repairs, then a fallback with an explicit status | user; gating PM |

## Knowledge base and data

| ID | Requirement | Class |
| --- | --- | --- |
| R-007 | The knowledge base is a root folder of folders whose names follow no format. A folder declares one or more concepts. With an info file: names on line 1 and descriptions on line 2, both split by a configurable separator (default `\|`) and matched by position; when line 2 does not split into as many parts as there are names, a model assigns its text to each name. Only the info file and image files (extensions are configuration) are read; other files are ignored. Without an info file, or with one that a model judges unclear (every info file is assessed): a vision model looks at images drawn at random from the folder (a configurable number, default 5; all of them when the folder has fewer; a folder with no image is skipped), with the folder name as a hint, and either confirms the folder name as the concept name or proposes the name or names and descriptions; such names are used at once, marked as generated, reported, and can be corrected in an override file; a folder whose images show unrelated subjects is flagged in the report and never split. The same name in several folders is one concept. Every image in a folder shows all concepts the folder declares | user (decisions 9, 10, 30, 32, 38 to 40, 42, 59, 60) |
| R-008 | Concepts are of mixed kinds (character, clothing, place, object, style, other); knowledge is used according to the role the concept plays in the request, not its kind | user |
| R-009 | `rag.enabled` defaults to true. When false the request path makes no knowledge-base access at all, cached knowledge included; source-image analysis and validation still run | user |
| R-010 | Evidence comes from concept cards built offline: a vision model reads each selected image once at indexing time. Reading knowledge-base images during a request is a fallback, off by default | user (decision 13) |
| R-011 | Knowledge reaches the image model as text by default. `generate` is always text. `edit` attaches knowledge-base images only when `edit.attach_kb_reference_images` is true; otherwise only the user's image and the enhanced prompt go downstream | user (decision 14) |
| R-014 | Knowledge-base lifecycle: `kb sync` is the only writer; adding, changing, renaming or deleting source files reprocesses only what is affected; the request path never reads the source tree; a deleted source loses its knowledge at the next sync; snapshots are immutable and can be rolled back; a sync that would remove more than the configured share stops | user (decision 17) |
| R-016 | Every table and artifact is Parquet or Arrow; the serving index (approximate nearest neighbour and lexical) is rebuilt from them per snapshot; no all-pairs comparison | user (decision 19) |
| R-017 | Every offline stage is resumable and idempotent, keyed by input content hash plus the revisions of the model, prompt and schema that produced the artifact | user |
| R-023 | No gold prompts exist. The orchestrator has agents generate the evaluation set from the user's knowledge base, with answer keys derived from the folder declarations; real user prompts become gold when the user supplies them | user (decision 22) |

## Platform, models and process

| ID | Requirement | Class |
| --- | --- | --- |
| R-015 | No hardcoding, no hand-written rules, no word lists, no per-language rules and no guessed thresholds. Content decisions come from models and fitted values; a value not yet measured holds a sentinel; structural facts (a file cannot be decoded, a string is not valid JSON) are handled directly | user (decision 18) |
| R-018 | Vision-language models are reached over HTTP through a configured provider: an OpenAI-compatible server (the user's deployment, or `llama-server` on the dev machine) or a cloud API (Claude or Gemini). Each role's backend is a profile value and defaults to the local backend; a cloud backend is used only when the profile selects it, its key file is under `.secrets/`, and the user has granted a number of requests. Rules and source name no model; models are profile values | user (decisions 20, 28) |
| R-019 | Target: a response within a few seconds while many requests run concurrently. Latency and throughput are measured on the deployment, never claimed from estimates | user (decision 21) |
| R-020 | Everything works on the dev machine first (small-VRAM GPU, small quantized model); the user runs the deployment from the agent's run guide. Dev results show that code works, never quality | user (decision 20) |
| R-021 | Every external number, benchmark or API behaviour has a research note with URL and verification class; never invent or round a figure | user |
| R-022 | Every repository file is English; chat with the user is Vietnamese; multilingual data (folder names, info files, prompts) is kept verbatim | user |
| R-025 | Continuous integration runs on GitHub Actions with the latest Ubuntu and Windows runners. For a run the repository is made public, and it is made private again when the run ends, whether green or red | user (decision 26) |
| R-028 | After documentation is written or restructured, fresh Haiku, Sonnet and Opus readers check that they understand it; their questions go to the user | user (decision 27) |
