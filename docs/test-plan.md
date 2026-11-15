# Test plan

Plan only; no tests exist yet. Categories are reported separately, and a double is never reported as live evidence.

| Category | What | Where |
| --- | --- | --- |
| Unit and contract | Info-file parsing with one and with several concepts; folder naming by a model; count-mismatch error; concept identity across folders; sync diff and the mass-deletion guard; snapshot publish and rollback; retrieval-off isolation, proven by running with no knowledge base present (R-009); scope filter; family choice uniformity and its trace (R-013); profile loader refusals (sentinels, unknown keys, missing fields); manifest validation; an edit request to a generate-only profile is refused | CPU, default suite |
| Property tests | Invariants of the sync diff (a rename changes no artifact key), of the family choice (uniform over the eligible set) and of path containment | CPU |
| Configuration | Every profile value has a non-default override test and a refusal test | CPU |
| VLM request mapping | Per engine: profile fragment to request body; warning on an unsupported field (dropped, warned, run continues); a missing field is refused; a labelled fake server as a double ([contracts/vlm-request.md](../contracts/vlm-request.md)) | CPU |
| Replay | S3 and S5 on stored artifacts with a frozen profile; the outcome must be reproducible from the stored inputs | CPU |
| Dev-GPU smoke (E12) | The client against the dev server: image input, JSON schema, thinking off, several images; plumbing only, never quality or timing ([dev-machine](dev-machine.md)) | the dev machine |
| Deployment smoke and measurement (E13) | One request with image plus schema plus thinking off; latency and throughput under concurrency ([serving](serving.md)) | the user's deployment, reported as measurements |
| Quality | Metrics on the evaluation set ([G-evaluation](stages/G-evaluation.md)), card audit (E16), experiments E1 to E17 | deployment plus agent review |
| Cross-platform | The suite above the dev-GPU line runs on Ubuntu and Windows in CI (R-025, [ci](ci.md)); OS- or hardware-specific live tests are marked `not_run` where unavailable, never inferred from another platform | GitHub Actions |

Fixtures are tiny and synthetic; no real knowledge-base images and no model weights are committed to Git.
