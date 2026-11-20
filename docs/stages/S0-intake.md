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

**Library contract (T-013 P1).** `request.v0` or `request.v1` (MS-3: `source_images`, a list) and `intake_result.v1` (T017-FIX3: its refusal envelope is `response_envelope.v1`) define the input and result payload; a request with several images gets `intake_result.v2` (see "Several source images" below).
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

**Several source images (request and intake built by MS-3; planner, writer and envelope follow in MS-4 and MS-5; decisions 176 and 179).** S0 takes `request.v0` (one optional `source_image`) or `request.v1` (`source_images`, a list in caller order); the field present tells them apart, and both fields sent is refused. `cli_input.parse_request` (request file and HTTP body) and `intake.validate_request` share one set of rules, `request_shape.py`.
- **One image keeps today's bytes.** A `request.v0`, and a `request.v1` with one item, build the same `intake_result.v1` payload (`source_image`, no `request_version`), so S1 to S6 and the envelope are unchanged for them. The `intake_result.v2` text elsewhere that says S0 builds the list "for every request" is superseded: S0 builds the list shape only for two or more images (design Q3, decision 179).
- **Two or more images** build `intake_result.v2`: `request_version`, and `source_images` as `[{index, image_id, role, data}]`, 1-based, in request order; the payload has no `source_image` key, so no stage can take image 1 for all of them. Until MS-4 and MS-5 the pipeline refuses such a result before S1 with `MULTI_SOURCE_UNAVAILABLE` (`intake.refuse_multi_source`, called in `Pipeline._run_request`): a failed `response_envelope.v1`, null output, `source_binding: null` (that envelope holds one binding; the n bindings are MS-5's `response_envelope.v2`). MS-4 and MS-5 remove the call.
- **The cap.** `min(edit.max_source_images, renderer.max_source_images)`; a version-1 manifest (`schema_version 1`) states no limit and means one image. It is read after the profile resolves, and a list over it is refused on its length before any image is decoded (`SOURCE_IMAGES_TOO_MANY`, never truncated). A request cannot raise it (`request_overrides.allowed_keys`).
- **Refusal codes**, each naming a field and an index or integer and never a request value (R-026): `SOURCE_IMAGE_REQUIRED` (`edit` without an image, or an empty list), `SOURCE_IMAGE_UNEXPECTED` (`generate` with one), `SOURCE_IMAGES_CONFLICT` (both fields), `SOURCE_IMAGES_TOO_MANY`, `SOURCE_IMAGE_DUPLICATE` (two items with the same bytes, compared after base64 decoding and before any image decoding), `IMAGE_UNDECODABLE` and `IMAGE_REJECTED` (`source_images[i]`). A `request.v0` refusal keeps its short text (`intake refused (CODE)`); a refused `request.v1` adds the field-and-index detail (`intake refused (CODE): source_images[1] is not valid base64`). Over the file or HTTP route the parser gives the same field wording (exit status 2 or HTTP 400).
- **Decoding.** Each image is decoded under the existing `vision.max_decode_pixels` and resized per `vision.max_image_side`; the intake deadline is checked between images; `max_request_bytes` (request file and HTTP body) bounds all images together, so the run guide must say a request with n images is about n times one.
Versions, and what a schema checks against what code checks: [contracts, multi-source section](../../contracts/README.md#multi-source-contracts-designed-not-yet-implemented).

**References.** N102.
