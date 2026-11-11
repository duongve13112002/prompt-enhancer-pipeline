# evalset/ (evaluation scenarios)

**Status: mock.** No gold prompts exist (R-023, decision 22). The orchestrator has agents generate scenarios from the user's knowledge base; this folder stays documentation until real prompts arrive.

## Where things live

Generated data is never committed here. Under `data_root`:

| Table | Content |
| --- | --- |
| `eval_scenarios` | The generated requests and their kinds. |
| `eval_labels` | Answer keys, derived from the folder declarations. |
| `eval_results` | Scores per run, with run and snapshot ids. |

## Scenario kinds

Single concept, several concepts, family mention, variant unspecified, no match, negation, literal text, local edit, global transform, subject recompose, retrieval off. The configured list is `evaluation.scenario_kinds` in [enhancer.example.yaml](../configs/enhancer.example.yaml).

## Independence rules

1. A generator never judges scenarios whose key it wrote.
2. The verifier under test never scores its own labels.
3. The `split` value is `fit` or `test`, by concept, so a concept is never in both. The split has nothing to do with the dev machine ([G-evaluation](../docs/stages/G-evaluation.md)).

## Tiers

**Silver**: agent-generated scenarios. **Gold**: scenarios a human has confirmed. The user reviews a sample of the silver set. Real user prompts placed here later become gold.

Procedure and metrics: [G-evaluation](../docs/stages/G-evaluation.md).
