# G Evaluation: scenarios from the knowledge base

**Purpose.** Measure the pipeline and fit every `fit_from_*` value on data it did not produce. No gold prompts exist (R-023, decision 22), so agents generate scenarios from the user's knowledge base; answer keys come from the folder declarations. Agent-generated scenarios are **silver**; real user prompts become **gold** when the user supplies them. Nothing is built or measured yet; layout: [evalset/README.md](../../evalset/README.md).

**Input and output.** A published snapshot and its folder declarations, then `eval_scenarios` (prompt, task, source image for edit, scenario kind, language and script), `eval_labels` (the answer key) and `eval_results` (per scenario and metric), plus a fitting report per value.

**Method.**
1. **Generate (`eval build`).** The orchestrator has agents, and the `scenario_generator` role, write prompts in every input format (tags, prose, structured, mixed, R-003) and in several languages and scripts, covering: exact names; paraphrased or indirect mentions built from a description; family mentions (a general mention that several concepts fit); negated mentions; unknown names absent from the knowledge base; multi-concept prompts; edit instructions over example images.
2. **Answer keys** come from the declarations, not from a model's judgement: the concept a scenario was built from; `family` with the valid members for a general mention; `NEGATED_BY_USER` for a negated concept; `no_match` for an unknown name; for edit, the targets, delta and preserve list the scenario specifies.
3. **No self-grading.** A scenario generator never evaluates scenarios whose key it wrote; another agent or a model of another family scores them.
4. **User review.** The user reviews a sample of scenarios and keys before any value is fitted.
5. **Splits** into `fit` (used to fit values) and `test` (used to report) by concept, so a concept is never in both. Sentinels are fitted on the `fit` split from runs that use the deployment's models; runs on the dev machine never fit a sentinel (R-020). The split is unrelated to the dev machine.

**Metrics.** Shortlist recall at the shortlist size; resolution precision and coverage; no-match accuracy; family picks that avoid `conflicts`; constraint survival per type (R-001); leakage and under-editing rates; card fact and label precision; status correctness (a truncated output is never `success`); latency and throughput on the deployment only (R-019). Every metric is reported with its confidence interval and its denominator, never as a point value alone.

**Experiments.** Each experiment decides one design question and fits the sentinels named; "the interval" means the confidence interval of the metric on the test split.

| Exp. | Decides | Accepted when | Fits |
| --- | --- | --- | --- |
| E1 | linking quality per language and script | the chosen settings maximise resolution precision at the coverage the user accepts | `candidates.*` shortlist size, fusion weight, n-gram length (`fit_from_gt`) |
| E2 | `rag.evidence_mode` | `cards` is kept unless another mode is better beyond the interval | `rag.evidence_mode` |
| E3 | reranker before resolution | a reranker is added only if it improves E1 beyond the interval | `candidates` reranker switch |
| E4 | which candidate sources run | a source stays when removing it lowers shortlist recall | `candidates` sources |
| E5 | the embedder | best E1 on the user's knowledge base | `embedding.*` (then `pin_required`) |
| E6 | planner and writer split or merged without knowledge | merged only if no constraint metric drops | `planner`, `writer` merge switch |
| E7 | verifier gating | risk-gated is kept if it misses no more failures than always | `validation` gating and sampling share (`fit_from_pilot`) |
| E8 | `verbalize` versus `attach` | measured on downstream images when the user supplies an image model | informs the user's `edit.attach_kb_reference_images` choice |
| E9 | the scope matrix | changing out-of-scope content of knowledge images changes no output | validates `assembly.scope_matrix` |
| E10 | thinking on or off | on only if quality gains outweigh measured latency | `planner`, `writer` thinking |
| E11 | presence penalty with JSON | the value with no schema failures and no quality loss | `planner`, `writer` sampling |
| E12 | dev smoke | image input, JSON schema and thinking off work together on the dev server | gate only |
| E13 | latency and throughput under concurrency | meets the user's target on the deployment | `serving.*`, `runtime.*` (`fit_from_bench`) |
| E14 | representatives per folder | the smallest number whose card precision matches observing all images | `observe.representatives_per_folder` (`fit_from_gt`) |
| E15 | quantisation on the dev machine | reports whether a higher quantisation changes the rate of invalid outputs on the dev machine; information for the user, who chooses the dev file (decision 20) | nothing: the dev file is the user's choice and is not fitted |
| E17 | folder naming | hide the info file of folders that have one, let the `folder_namer` role name them, and compare with the user's names; also run the `info_assessor` role on the same info files and report how many it calls unclear, for the user to review a sample; the labels are ones the user already wrote; accepted when naming accuracy and the share of clear files wrongly called unclear meet the user's bar | none: `knowledge.naming.sample_size` is the user's value (5, decision 39); E17 reports accuracy at that size and at others so the user can change it |
| E16 | card audit | fact and label precision on a human-checked sample meet the user's bar | `cards.min_visible_images`, low-diversity value (`fit_from_gt`) |

**Decisions and configuration keys.** `evaluation.*` (scenario counts per kind, split seed, call budget `ask_user` for paid backends), `vlm.roles.scenario_generator`.

**Compute.** Agent sessions and model calls; quality runs on the deployment model, plumbing on the dev machine (R-020).

**Depends on.** K0 to K5, P; every online stage is under test.

**Failure modes.** Scenarios that only echo the generator's style (the user's review and gold prompts later correct it); keys that are wrong when a folder's images do not show its concepts; too few scenarios per language for a useful interval.

**References.** [N115](../note/N115-llm-entity-linking-nil.md), [N118](../note/N118-structured-checks.md), [N133](../note/N133-edit-evaluation-benchmarks.md).
