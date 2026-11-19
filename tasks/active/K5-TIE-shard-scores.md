# K5-TIE: shard-independent K5 vector scores

Kind: code. R-IDs: R-013, R-016, R-017, R-009, R-001, R-026. Small: yes. Worker: Claude Sonnet 5.5, worktree `.tmp/worktrees/K5-TIE`.

## Step 0

1. **Unknowns and failure modes:** (a) whether the shape dependence is the product, the norms in `_matrix`, or the query scaling; (b) a fix bitwise stable only on this BLAS; (c) memory or speed cost at real shard sizes; (d) a change to `_matrix` would move the pinned `k5` digest and force a revision bump.
2. **Questions (proposed answers; none blocks):** (1) Add a setting? No: scoring is not a deployment choice. (2) Touch stored bytes or `_matrix`? No; the fix stays in `ServingIndex.search`, which is in the unpinned SERVING list (`tests/stage_guard.py:167`), so no revision bump or pin row. (3) Value for `block_rows`? Examples hold the sentinel `fit_from_bench` (`configs/enhancer.example.yaml:94`), so the benchmark uses synthetic 8192 and 16384 rows.
3. **Plan:** confirm the cause by experiment; replace `matrix @ query` with an in-place row-wise product and `sum(axis=1)`; add the `@example`, a shard-independence property, a differential test and a tiny-score test; run the K5 and dependent suites; mutate; measure.

## Root cause (experiment, this machine: numpy 2.5.5, Windows)

Script `.tmp/K5-TIE/rca.py` (removed at the end). A 257-row random matrix was scored whole, then in blocks of 1, 2, 3, 5, 7, 16 and 33 rows at every offset, and each score was compared bitwise with the whole-matrix score. Mismatching row scores per operation, summed over blocks:

| dim | `block @ q` | `(block * q).sum(axis=1)` | `sqrt(sum(block * block, axis=1))` (`_matrix` norm) | `einsum('ij,j->i')` |
|---|---|---|---|---|
| 3 | 75 | 0 | 0 | 0 |
| 64 | 661 | 0 | 0 | 0 |
| 129 | 769 | 0 | 0 | 0 |
| 300 | 783 | 0 | 0 | 0 |
| 1024 | 843 | 0 | 0 | 0 |
| 2048 | 864 | 0 | 0 | 0 |

The PM's diagnosis holds: the BLAS matrix-vector product is the only shape-dependent step. The `_matrix` row norms and the division by them (`k5_index.py:209-212`) are per-row and stable, and the query scaling (`:417-422`) sees only the query, so it cannot depend on a shard. `_matrix` is unchanged.

## Change

`src/prompt_enhancer/k5_index.py:435-437`: `weighted = _matrix(...)`, `weighted *= query`, `scores = weighted.sum(axis=1)`. Each row is reduced alone, so its bits depend on its own `dim` elements and not on the other rows. `_matrix` returns a fresh array, so the in-place product adds no second B x D buffer. The inner-product and cosine metrics share this path. Stored artifacts, `K5_INDEX_REVISION` and the `k5` pin are untouched (`test_stage_revisions.py` 397 passed). `docs/stages/K5-index.md:18` now states the shard independence and that cross-machine bitwise equality is still not promised.

## Pre-mortem

| Way it could fail | Check that rules it out |
|---|---|
| A score still depends on shard size, row position or shard count | `test_score_of_a_vector_is_bitwise_the_same_in_any_shard_position_and_count`: float vectors, dim 1 to 300, blocks 1, 2, 3, 5 and all rows, three row orders, a repeated row, both metrics and tables; per stored vector the set of scores has one element. Mutant M1 (old `@`) fails it. |
| Norms or query scaling are the shape-dependent part | `rca.py` columns above (norm: 0 mismatches); the property above covers the whole `search` path, so a norm effect would fail it too. |
| Rounding, quantising or zero special case | `test_tiny_true_scores_are_kept_not_rounded_or_zeroed` (1e-10 x 1e-10 stays 1e-20, a zero stays 0); differential test with a rounding bound. M5 and M7 are killed. |
| Stable only on this BLAS | The new path uses no BLAS: it is numpy's own pairwise row reduction over a C-contiguous array. Linux CI is `not_run` here. Residual risk: the array layout (see Fix round 1, which also corrects my first reading of M8). |
| Slowdown or memory growth | Measured below: no extra peak memory; scoring step 13x slower but 2% of the whole shard step. |
| The metrics diverge | One shared code path; property and differential tests run both metrics. |
| Stored bytes or revision change | None: `_matrix`, `_parts` and the build path are untouched; `test_k5_index.py` and `test_stage_revisions.py` pass unchanged. |

## Class sweep

| Place | Operation | Result |
|---|---|---|
| `k5_index.py:431` (now `:435-437`) `ServingIndex.search` | `matrix @ query` | affected, fixed here |
| `k5_index.py:209` `_matrix` | `np.sum(values * values, axis=1)` norms | not affected: row-wise, 0 mismatches in `rca.py` |
| `k5_index.py:422` query norm | `np.sum(query * query)` | not affected: query only, no shard input |
| `k5_index.py:435,438` (now `:439,442`) | `np.lexsort`, `sorted` with key tuples | not affected: orderings by exact score then primary key; they only need equal scores to be equal, which the fix gives |
| `k5_index.py:84, 183, 390, 396` | `sorted` over keys and counts | not affected: no floats |
| `candidates.py:113` RRF | `sorted` on `Fraction` scores | not affected: exact rationals |
| `candidates.py:166` `_rank` | `sorted` on `-score` then concept id | not affected: consumes `search` scores, fixed upstream |
| `assembly.py`, `k4_aliases.py` | `sorted` of strings and tuples | not affected: no numeric reductions (grep: no numpy there) |
| `embed_server/dev_compare.py:58-67` | `np.linalg.norm`, max | out of scope: a dev embedding diagnostic, no ranking; not changed |
| `resolution*.py`, `embedding*.py`, `k4_*.py` named in the brief | none exist in this tree except `k4_aliases.py` | not applicable |

## Completion report

**Work completed:** shard-independent scoring in `ServingIndex.search`; tests `@example` on `test_exact_search_matches_scalar_reference` (`tests/test_k5_properties.py:24`), the shard property, the differential test (`test_scores_equal_the_blas_product_within_the_rounding_bound`: per row `|new - old| <= 2 * dim * eps * sum|a_i b_i|`, S46) and the tiny-score test. Helper `_reader` builds the labelled in-memory shard double.

**Evidence (real output, `test_slot.py`, one file per process, `PYTHONPATH` = worktree `src;tests`):**

```
test_k5_config.py -> 52 passed          test_k5_index.py -> 57 passed
test_k5_properties.py -> 20 passed      test_candidates.py -> 34 passed
test_stage_revisions.py -> 397 passed   test_kb_sync_alias_failure.py -> 29 passed
test_kb_sync_flow.py -> 33 passed       test_kb_sync_invalidation.py -> 26 passed
test_profiles.py -> 192 passed, 2 skipped
test_k5_properties.py with --hypothesis-seed 11, 22, 33, 44 -> 20 passed each
```

**Measurement (labelled CPU smoke, this machine only, no timing claim beyond it; synthetic shard, `block_rows` is the sentinel `fit_from_bench` in the examples):**

```
rows=8192  dim=1024 old @         median=  0.74 ms  extra peak= 0.06 MiB (matrix 64 MiB)
rows=8192  dim=1024 new in-place  median=  9.75 ms  extra peak= 0.06 MiB
rows=8192  dim=1024 temp product  median= 21.20 ms  extra peak=64.06 MiB   (rejected variant)
rows=16384 dim=1024 old @         median=  2.31 ms  extra peak= 0.13 MiB (matrix 128 MiB)
rows=16384 dim=1024 new in-place  median= 19.82 ms  extra peak= 0.13 MiB
whole shard step (`_matrix` + score), 8192 x 1024: old 433.8 ms, new 443.1 ms (+2%)
```

Decoding and `to_pylist` in `_matrix` dominate the shard step, so the slower reduction costs about 2% there. I dropped the temporary-product variant (`(m * q).sum(1)`) because it doubles the shard buffer.

**Intent check:** R-001 user intent, R-004 admin system prompt, R-009 retrieval-off access and R-026 data-not-instructions are untouched: the change is in query-time scoring of already stored vectors, reads nothing new, and reaches no prompt text. R-013 gains: equal vectors now tie exactly, so the choice among equal concepts sees real ties. R-016/R-017: same snapshot and query now rank identically for any `block_rows`.

**Mutants** (scratch copy of `src`, 1.1 MB, runner `.tmp/K5-TIE/mutants.py`): M1 revert to `@` killed (scalar-reference example, shard property); M2 query reversed killed; M3 wrong axis killed; M4 product dropped killed; M5 round to 12 places killed (differential, tiny-score); M6 float32 accumulation killed (differential); M7 zero below 1e-15 killed (tiny-score, found by a first run that survived and led me to add that test, and to set `abs=0` in its `approx`); M8 sum over a reversed view: killed in this first run, but not reproducible later (see Fix round 1). Seven of eight killed after Fix round 1.

**Configuration audit:** no setting added or changed. `block_rows` remains the validated `index.build.block_rows` (class configurable, `config.py` unchanged); the retained literals `1e-300` and `2 * dim * eps` exist only in tests (test-only). No new runtime literal.

**not_run:** ruff lint and `ruff format --check` (not installed); Linux CI and other BLAS/numpy builds (this Windows machine only); the full suite (barred); other tests in the repository that do not touch `ServingIndex.search`; a benchmark with a deployed `block_rows` (none fitted).

**Cleanup:** `.tmp/K5-TIE/pytest` held 0.33 MB before removal; scratch scripts and `mut/` removed at handoff.

## Noticed, not done

- `tests/test_k5_properties.py` still builds the shard double twice more inline (`test_exact_search_matches_scalar_reference`, `test_equal_scores_at_cutoff_use_complete_key`); they could use `_reader`. Left alone to keep the change small.
- `test_exact_search_matches_scalar_reference` uses `approx(abs=1e-12)` for scores; a coarser assertion than the tiny-score test needs, and it hides absolute errors below 1e-12.
- The new guarantee is bitwise within one numpy build and machine; the docs now say cross-machine equality is not promised.

## Fix round 1 (QA, Gemini Flash: PASS, two P3)

1. **Layout enforced in code.** `k5_index.py:435-438`: `weighted = np.ascontiguousarray(_matrix(...), dtype=SCORE_DTYPE)`. I chose this over a guarantee inside `_matrix` because `_matrix` is pinned in the `k5` stage digest (a change would force a revision bump and a pin row), while `search` is unpinned. It adds no copy on today's path: `_matrix` returns a C-ordered float64 array and `np.ascontiguousarray` returns the same object; any other layout gets a fresh C-ordered copy, so the in-place product also never writes into a shared view. Not guarded: a future `_matrix` returning a C-contiguous array that aliases shared storage would be multiplied in place.
   New test `test_scores_do_not_depend_on_the_memory_layout_of_the_matrix` (labelled double: `_matrix` wrapped to return the same values as a Fortran-order array, every other column of a wider array, or a negative-row-stride view; float vectors, dim 1 to 300, both metrics): scores bitwise equal to those from the real `_matrix`.
2. **Tiny-score test, cosine.** A cosine case is meaningful, so I added it rather than a comment: stored vectors are normalised, so a tiny score is a near-orthogonal pair (`[1, 1e-17, 0]` against `[0, 1, 0]` gives 1e-17). Inner product keeps the 1e-10 x 1e-10 case; the test is parametrised over both, with the reason in a comment.

**Mutants (scratch copy of `src`, 1.1 MB):** M9 "layout not enforced" (the `ascontiguousarray` removed) is killed by the new layout test alone, in 7 of 7 runs (default plus Hypothesis seeds 1 to 6). M10 "enforce Fortran order" is killed by the shard property, which shows that a non-C layout does break shard independence. M1 (BLAS) and M7 (zero below 1e-15) stay killed, M7 now by both tiny-score cases.
**M8 correction:** you asked for M8 killed by the layout test. It is not, and I withdraw my first claim. M8 (`weighted[:, ::-1].sum(axis=1)`) survived in the default run and in 6 of 6 seeds, and a direct probe (dims 1 to 100, blocks 1 to 16) shows a reversed-view sum is itself shard-independent (0 mismatches; it only sums in another order, within the differential bound). So M8 is an equivalent mutant for shard independence. The one early kill could not be reproduced and I cannot explain it (assumption: a replayed Hypothesis example from earlier mutant runs). The evidence for the layout claim is M9 and M10 instead.

**Tests (real output, `test_slot.py`, one file per process):** `test_k5_config.py` 52 passed; `test_k5_index.py` 57 passed; `test_k5_properties.py` 23 passed; `test_stage_revisions.py` 397 passed (no pin change: `_matrix` untouched). `.tmp/K5-TIE/pytest` held 1.9 MB before removal. `not_run`: ruff (not installed); candidate and kb_sync suites not re-run, since `search`'s signature and results are unchanged.
