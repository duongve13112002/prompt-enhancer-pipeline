# K1 Embed: images and concept text

**Purpose.** Give every image and every concept a vector, so K2 can group near-duplicates and pick representatives, S1 can find concepts described indirectly, and S3 has a fallback for attributes outside the facet schema. Embedding every image (part 1) is the cheap first tier of indexing ([K2](K2-observe.md)).

**Input and output.** K1 has two parts. Part 1 (after K0, before K2): new or changed `image_id`s from K0. Part 2 (after K3 and K4): the text of each concept (name, description, aliases from K4, card summary from K3). Output: `image_embeddings` and `concept_text_embeddings` as Arrow, each row keyed by content plus embedder revision (R-016, R-017). Image metadata (format, dimensions, decode status) goes to `images`.

**Method.** Part 1: decode each image once, record dimensions, embed with the configured embedder; the embeddings group near-duplicates, and those groups let the naming step of [K2](K2-observe.md) choose a varied subset of a folder without an info file. Part 2: embed concept text, which includes the card summary and aliases and so waits for K3 and K4, with the same encoder so text and image vectors share a space. Concept text is re-embedded when the name, description, aliases or card summary change.

Online, S1 embeds only short text (the prompt and its segments), so image embedding never runs on the request path. One embedding space per snapshot: vectors from two embedders are never mixed.

**Decisions and configuration keys.** `embedding.*`: the backend, model and revision (`pin_required`), the dimension if the embedder supports truncation (`fit_from_gt`, E5), batch size and devices (`deploy_required`); every image goes through the shared client-side resize of `vision.max_image_side` before embedding (`none` by default, decision 84, [VLM_CALL_RULES](../rules/VLM_CALL_RULES.md) rule 7). The embedder is chosen by E5 on the user's knowledge base, not from published leaderboards, because published figures are measured on other data. Candidate embedders, their licences and their text-length limits are in the references.

**Compute.** GPU on the dev machine and the deployment, batched; the embedder can share the GPU with the serving model only within the VRAM budget of [PLATFORM_GPU_RULES](../rules/PLATFORM_GPU_RULES.md). Not implemented; nothing measured.

**Depends on.** Part 1: K0. Part 2: K3 and K4.

**Failure modes.** Text longer than the encoder's token limit is truncated silently (check per encoder); weak coverage of a language or script in the encoder (measured in E1 and E5 per language and script); a changed embedder without a new index (prevented by keying on the revision).

**Experiments.** E5 (embedder comparison), E1 and E4 (contribution of dense search to linking).

**References.** [N008](../note/N008-siglip2.md), [N104](../note/N104-embeddinggemma-2.md), [N105](../note/N105-qwen3-vl-embedding-cards.md), [N106](../note/N106-retrieval-baseline-licences.md), [N132](../note/N132-multilingual-retrieval-evidence.md).
