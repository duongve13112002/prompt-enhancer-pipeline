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

**Compute.** CPU; library intake, image decode and resize have bounded synthetic contract/property tests.
Live intake latency and whole-pipeline behavior are unmeasured; P1 makes no model calls.

**Depends on.** P (an active admin profile); K0 (a published snapshot, when retrieval is on).

**Failure modes.** No published snapshot (warning `SNAPSHOT_MISSING`, retrieval state `failed`, status `degraded`); an image too large to decode within limits (refused); an override the configuration profile does not allow (refused).

**Experiments.** E12 (dev smoke with image input), E13 (latency of the whole path).

**Library contract (T-013 P1).** `request.v0` and `intake_result.v1` (T017-FIX3: its refusal envelope is `response_envelope.v1`) define the input and result payload.
Call `prompt_enhancer.intake.intake(request, config, project_root=...)` as a context manager; use its
`payload` for JSON, `profile` for the checked admin view and `snapshot` for downstream pinned reads.
Images are supplied as base64 bytes, never a request-specified URL or path. Configuration profiles explicitly
declare `request_overrides.allowed_keys` (empty in both examples); only those exact existing leaf paths may
be overridden, with strict types and effective configuration revalidation. The policy itself is not overridable.
An intake refusal includes `error: {code, message}` beside an unchanged failed response envelope with null output.
Before an effective configuration exists, refusal retrieval state follows base `rag.enabled` (`disabled` when off); afterwards it follows the effective value.
Refusal diagnostics contain fixed structural text and exception class names, never request values, paths or exception text.
`generate` with a non-null `source_image` is refused as `SOURCE_IMAGE_UNEXPECTED` before any image decoding (PM decision pending user review, R-005/R-012).

The independent decode bound is `vision.max_decode_pixels: deploy_required`; there is no inferred backend value.
`runtime.intake_deadline_s: fit_from_bench` bounds elapsed time checked before and after each stage.
It does not preempt an in-progress filesystem or Pillow call. A deadline refusal releases any acquired lease.
The caller owns the returned result's snapshot lease until it closes the result after downstream processing.
S0 calls `pin()` once, which reads the pointer internally; it never calls `current()` first. Retrieval off
constructs no knowledge gateway/store and skips filesystem resolution of knowledge-only paths during loading.
`none` preserves native image scale; explicit encoding, EXIF orientation and alpha policy can still change bytes.

**Several source images (designed, not yet implemented; MS-2 to MS-7, decisions 176 and 179).** Today S0 takes one optional `source_image` (`request.v0`). The design adds `request.v1` with `source_images`, a list in caller order, and S0 builds one internal list, `intake_result.v2.source_images` (`[{index, image_id, role, data}]`, 1-based, request order); a `request.v0` becomes a list of one and keeps the envelope of today's path. Structural refusals each name the field and never a value: `edit` with no image, `generate` with any, an empty list, both fields sent, more images than the smaller of `edit.max_source_images` and the manifest's `renderer.max_source_images` (`SOURCE_IMAGES_TOO_MANY`, never truncated; both keys are added by MS-2), two images with the same bytes (`SOURCE_IMAGE_DUPLICATE`, a proposal) and bad base64 at index i. Each image is decoded under the existing `vision.max_decode_pixels` and resized per `vision.max_image_side`, the deadline is checked after each image, and `serving.http.max_request_bytes` then bounds all images together. Versions, and what a schema checks against what code checks: [contracts, multi-source section](../../contracts/README.md#multi-source-contracts-designed-not-yet-implemented).

**References.** N102.
