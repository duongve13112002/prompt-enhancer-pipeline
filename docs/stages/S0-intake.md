# S0 Intake: admin profile, configuration, image, snapshot

**Purpose.** Fix everything a request depends on before any model call: which admin profile, which effective configuration, which task, which image and which snapshot. Errors here are explicit and early; nothing downgrades silently to another task.

**Input and output.** The raw prompt (verbatim, any format or language, R-003), an optional source image, an optional `profile_id`, the task, typed configuration overrides allowed by the configuration profile, then a `request_id`, the effective configuration, `task` (`generate` or `edit`), the prepared source image with its `image_id`, and with retrieval on the pinned `snapshot_id`.

**Method.**
1. Resolve the admin profile: the request's `profile_id`, else `profiles.default_profile_id` (decision 4). Refuse an admin profile that is not active.
2. Build the effective configuration from the configuration profile and the typed overrides; refuse an unknown key or a value of the wrong type.
3. Check the task against the manifest: an `edit` to a generate-only admin profile is refused, never reformatted (decision 5); an `edit` without a source image is refused with a clear message. A refused request still returns an envelope, with status `failed` and the reason ([contracts](../../contracts/README.md)).
4. Decode the source image; an undecodable image is an error, never a silent switch to text only. Resize on the client per `vision.max_image_side` (decision 84): `none` keeps the original; an integer N downscales so the longer side is N, keeping the aspect ratio, and never upscales a smaller image. One visual token covers about 32x32 pixels and the server's default pixel cap allows very large inputs ([N102](../note/N102-qwen3-8-27b-serving-facts.md)), so the cap controls latency and cloud cost; doing it here keeps behaviour independent of server flags. The image role is `source_image` (R-012).
5. With `rag.enabled: true`, read the current snapshot pointer once and pin it ([execution](../execution.md#how-a-request-pins-a-snapshot)). With `rag.enabled: false`, the knowledge gateway is not constructed and S1 and S3 leave the graph (R-009).
6. A phrase such as "rag=false" inside the prompt has no effect: the prompt is data (R-026).

**Decisions and configuration keys.** `profiles.*`, `rag.enabled` (default true, decision 12), `vision.max_image_side` (`none` by default, 1024 for experiments and benchmarks, decision 84) and encoding (`fit_from_bench`, E12, E13), `runtime.*` deadlines (`fit_from_bench`). The enhancer contract has no width, height or aspect ratio (R-005).

**Compute.** CPU; image decode and resize. Not implemented; nothing measured.

**Depends on.** P (an active admin profile); K0 (a published snapshot, when retrieval is on).

**Failure modes.** No published snapshot (warning `SNAPSHOT_MISSING`, retrieval state `failed`, status `degraded`); an image too large to decode within limits (refused); an override the configuration profile does not allow (refused).

**Experiments.** E12 (dev smoke with image input), E13 (latency of the whole path).

**References.** N102.
