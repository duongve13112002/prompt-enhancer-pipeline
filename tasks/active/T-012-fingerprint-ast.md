# T-012-fp: the activation fingerprint hashes the normalized AST of `config.py` (decision 124)

Kind: code. R-IDs: R-015, R-017. Owner: PM. Builder: fresh Claude Sonnet. Reviewer: Codex.

## Goal

`check_code_sha256` in `src/prompt_enhancer/profiles.py` (`_code_fingerprint`, near line 136) fingerprints `config.py` by its normalized AST instead of its bytes. A comment, docstring or whitespace edit to `config.py` leaves the fingerprint unchanged; any code change still changes it.

## Step 0

Small: yes. Unknowns: none blocking (`ast.dump` default omits attributes; confirmed by the mutant `line_attrs`).
Questions: none. Plan: (1) add `_ast_digest`, use it for `config.py` only; (2) tests: docstring, comment, whitespace, one-token edit, property; (3) 5 mutants; (4) focused tests, then the full suite.
Finish check: new tests pass, mutants die, suite green.

Configuration impact: no new key, no new user value. `check_code_sha256` stays protocol-bound.

## Completion report

Behaviour: `_ast_digest(source)` in `src/prompt_enhancer/profiles.py` parses, drops the first string-constant statement of the module, each class and each function (sync and async), dumps with `include_attributes=False`, and hashes. `_code_fingerprint` uses it for `config.py`; `profile_prompt`, `profile_lint`, `profiles`, library and Python versions are unchanged.

Invalidation: the new fingerprint differs from the old one, so every existing activation record is invalidated once (state `PROFILE_NOT_CHECKED`, a new golden check needed). No committed artifact holds the old value (no `activation.json` is tracked). `contracts/README.md` described the hash as "over the bytes of the P modules"; I updated that sentence.

Commands (project `.venv`, through `test_slot.py`):
- focused `pytest tests/test_profiles.py -k "ast_digest or fingerprint or code_drift"`: 14 passed (before the two extra tests; final run below).
- mutants on a scratch copy of `src/` (`.tmp/T-012-fp/mut.py`): keep_docstrings, line_attrs, bytes_again, no_class_doc, non_str_too: all 5 killed (the first run left no_class_doc and non_str_too alive; two tests added, then killed).
- full suite (`pytest tests -q`, one `--basetemp` under `.tmp/T-012-fp/`, deleted after): `3410 passed, 42 skipped, 4 warnings in 893.24s (0:14:53)`.
- ruff: `not_run` (the module is not installed in `.venv`; installs are not allowed).

Configuration audit (row by row): new config key: none. Literals: `'utf-8'` in the dump encoding is protocol-bound (hash input). `include_attributes=False` is protocol-bound (the decision). No hidden default, no ignored setting. Override and refusal tests: not applicable, no user value added.

Changed files: `src/prompt_enhancer/profiles.py`, `tests/test_profiles.py`, `contracts/README.md`, this file.

Research evidence (added by the PM at integration, from the Codex review `.tmp/T-012-fp/review/review.md`):
- `ast.dump(..., include_attributes=False)` omits line and column attributes. This is an experiment: the builder's mutant `line_attrs` is killed.
- The fingerprint is stable across processes. This is an experiment: 3 fresh interpreters, with `PYTHONHASHSEED` 1, 2 and random, all return `sha256:e66b946e…5249`.
- Docstring-only bodies, leading f-strings or bytes, annotations, decorators, `__debug__` blocks and nested edits all behave as decision 124 requires. This is an experiment: 22 reviewer cases and 3 reviewer mutants, all killed.
- The encoding-cookie remark below is an assumption: no test covers it.

Noticed, not done:
- `profile_prompt.py`, `profile_lint.py` and `profiles.py` still hash bytes, so a comment edit in them still forces a new golden run. Decision 124 names `config.py` only, so I left them.
- `ast.parse` reads the source with its own encoding rules, so a changed coding cookie in `config.py` is a comment-level edit that would still be seen only if it changes the parsed tree (it does not). Harmless.

## Worker completion
Done check met: change, tests that fail on old code (`_ast_digest` absent) and pass now, property test, 5 mutants killed, focused tests, full suite green. `not_run`: ruff (not installed).
