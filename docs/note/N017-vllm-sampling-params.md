# N017 vLLM SamplingParams and V1 guide

- **Source:** https://docs.vllm.ai/en/latest/api/vllm/sampling_params/
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** contracts/vlm-request.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

`SamplingParams(max_tokens=1, temperature=0, logprobs=N)` with `allowed_token_ids` and `logprob_token_ids`. The default `raw_logprobs` mode reports values before logit processors; `processed_logprobs` renormalizes. `prompt_logprobs` bypasses the prefix cache.

## Caveats

Unit-test the exact behaviour on the pinned version before relying on it.
