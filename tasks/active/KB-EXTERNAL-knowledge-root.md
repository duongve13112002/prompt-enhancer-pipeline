# KB-EXTERNAL: a knowledge base outside the project root (decision 175) and the reference-image profile refusal (decision 177)

Kind: code
R-IDs: R-009, R-014, R-015, R-026
Task file: tasks/active/KB-EXTERNAL-knowledge-root.md

Status: Step 0 approved by the PM (answers 1 to 5 agreed as proposed; 177 check only when `edit` in `supported_tasks`; run-guide must say `profile check` is needed again). Implementation finished and verified on the CPU; fix round 1 (decision 164) done as unstaged changes on top of the reviewed staged diff; waiting for the retest. Nothing is committed (the orchestrator commits).

## Steps (progress log)

- Step 1 done: realpath experiment (`.tmp/KB-EXTERNAL/probe.log`, table in Research evidence); rule step 3 unchanged (no widening).
- Step 2 done in code: `config.resolve_knowledge_root` + `same_physical_path` + `PROTECTED_NAMES`/`PROTECTED_VENV_PREFIX`; `_validate_configured_paths`, `validate_sync_config`, `_validate_k0_reads` (param `stores` removed) use it; `_configured_path` lost its unused `links` param; `_resolved_knowledge_root` removed.
- Step 3 done in code: `k2_naming`, `k2_observe`, `k3_cards`, `k4_aliases._inputs` call it; `kb_sync.sync_knowledge` resolves once and passes `source` (+ `_resolved_root=`) to K0, K1, K2, overrides, `retain_images`; `dev_compare` likewise; `KB_SYNC_REVISION = kb-sync-6` + pin row `2f461e01...` appended.
- Tests: `tests/test_config_links.py` updated (10 pass); `test_stage_revisions.py` was red only on the kb_sync pin before the new row.
- Step 4 done: `profiles._require_attachable_renderer`, called first in `profiles._view`; `writer.render_brief` now raises `ValueError` in place of the degraded mode (defence); tests in `tests/test_profiles.py`, `tests/test_writer_attachments.py`, `tests/test_intake.py`; `tests/intake_fixtures.activate` gives a renderer that accepts images when the configuration attaches them.
- Step 5 done: `docs/execution.md` (two paragraphs), `docs/run-guide.md` (paths, where a knowledge base may live, profile re-check note, retrieval-off sentence), `configs/enhancer.example.yaml` (two comments). The `implementation-notes` entry is drafted below for the PM (not edited by the worker).
- Step 6 done: 13 mutants and one control (table below); test files run one per process (list in the Completion report).

## Step 0

### What exists (read, with file and line)

**A. Why an external root is refused today**
- `config._lexical_path` (`config.py:453-470`) refuses an absolute path that is not under the project root (`:459-463`) and a relative `..` that leaves it (`:468-469`) with `outside project root`, before any link is looked at. Every stage calls it first (`k2_naming.py:490`, `k2_observe.py:200`, `k3_cards.py:211`, `k4_aliases.py:107`, `config._resolved_knowledge_root` `:2291`). The brief names `_resolve_within`; this earlier refusal is a second site that must change too.
- `config._resolve_within` (`:157-207`) refuses a link target outside the root at `:184-185` and `:198-199` (`resolved outside project root`) even with `links=True`.
- The protected names differ by call site: `{.secrets, models}` in `_resolved_knowledge_root` and `_validate_k0_reads`; `{.secrets, models, .git, .venv}` in `_configured_path`, K2, K3, K4. No call site refuses `.venv-gpu` (`PROTECTED_PREFIX = '.venv'` exists only in `embed_server/reference.py:167`).
- Two more places never resolve the root: `kb_sync.py:217` (`source = project / knowledge['root']`, resolved later by `.resolve()` in `sync`, `read_source_file` callers and `retain_images`) and `embed_server/dev_compare.py:242,262` (`project / config['knowledge']['root']`, passed unresolved to `embed_images`). `k0_sync.sync:63`, `k0_checkpoint.scan_to_checkpoint:179` and `k0_scan.scan_source:246-247` call `Path.resolve()` themselves. These are the "re-resolution per stage" the brief wants gone.
- K0's own reads: `scan_source` and `iter_source_file` open the root from its drive anchor down and refuse a link or reparse point at every component (`k0_scan.py:123-139`, `:211-219`, `:253-259`); inside the knowledge base a link is reported and not followed (`:270-275`: `links`, `protected_folders`; `:296-297`, `read_source_file` refuses a link on any component). So **K0 never follows a link inside the knowledge base, whatever it points at.** With a physically resolved root no component of the root is a link, so the root walk passes.
- `storage/_fs.inside` (`:103-117`) is called with a store root and a store-relative path, never with the knowledge root (grep of `inside(`: only storage modules). Expected: no change. I confirm this in step 1.
- Retrieval off: `validate_sync_config` guards with `config['rag']['enabled'] and ...` (`config.py:1621`); `_validate_loaded_config` returns before `_validate_configured_paths` for `kb sync` (`:2183-2184`); request commands skip the two keys through `knowledge_access` (`:2200-2202`).

**B. Where decision 177 has to act**
- `edit.attach_kb_reference_images` is validated as a boolean by `_require_attachment_support` (`config.py:1818-1823`) and read by `validate_assembly_reads` (`:2101`). `renderer.accepts_reference_images` is a manifest field (`contracts/schemas/profile_manifest.v0.schema.json:121-130`), read by `writer.render_brief:92` (the degraded mode, no `attachments` key) and `pipeline.retain_attachments:84` (raises `profile cannot receive knowledge attachments`).
- `load_config` never sees a manifest, so no configuration load can know both values. They meet in `profiles._view(profile_id, manifest, ..., config, ...)` (`profiles.py:488`), the one function called by both `check_profile` (`:634`, base config plus overlay) and `resolve_profile` (`:743`, the request's effective config, which includes permitted `request.overrides` of the key and the overlay). `intake.py:253-254` turns a `ConfigurationError` from there into a `CONFIGURATION_INVALID` envelope; `cli_commands.py:192` turns it into the refusal exit.
- Side effects to report: `config.py` and `profiles.py` are inputs of `CHECK_CODE_SHA256` (`profiles.py:153-163`), so every stored `activation.json` becomes stale and `profile check` has to be run again after this lands (true of any `config.py` edit). `kb_sync.sync_knowledge` is a pinned function (`tests/stage_guard.py:107-112`): `KB_SYNC_REVISION` goes to `kb-sync-6` with a new row in `tests/test_stage_revisions.py`; the first sync after the upgrade re-plans once. `name_folders`, `observe_images`, `build_cards`, `_inputs` are classified DRIVER or CHECK (`stage_guard.py:153-161`): no stage bump there.

### Unknowns and failure modes

1. Windows `..` after a link: Win32 normalises `link\..` lexically, POSIX `realpath` follows the link first. The brief says "as the OS reads it", so I use `os.path.realpath` on the unmodified candidate and never `abspath` before it. To verify in an experiment with a real junction (Windows) and a symlink (Linux CI): `<link>/../x`.
2. `realpath` on Windows may return `\\?\` forms and, for a volume mount point, `\\?\Volume{GUID}\...`; after `_strip_extended_prefix` that is not an absolute path. I refuse a result that is not absolute.
3. External drive file systems: K0 raises `MissingFileID` when `st_ino` is 0 (`k0_scan.py:131-132`). FAT/exFAT/network shares may do that. Not testable here; reported as a limit, no claim.
4. `.secrets/` must never be opened or listed. `realpath` of a candidate spelled through a link into `.secrets/` makes a metadata-only handle open of that directory on Windows (no read, no listing) before my check refuses it. A lexical pre-check on the spelled path avoids even that for the direct spelling.
5. `.git` is a file in a worktree (this one); a protected entry that does not exist or is a file is compared as a path, not required to be a directory.
6. A retargeted link between K0 and K2 must fail closed, not read another directory: K2 compares its argument with a fresh result of the same function.

### Questions (ranked by impact; proposed answers)

1. **"Inside the project root" is refused by the brief's wording, but `database/` is the dev knowledge base (`configs/enhancer.dev.example.yaml:24`, decision 61) and every in-project test fixture.** Proposed: read it as "inside a protected directory". Refused: equal to or containing the project root; equal to, inside or containing `models/`, `.secrets/`, `.git`, any `.venv*`, `storage.data_root`, `storage.kb_store`. A root inside the project outside those stays valid. Decision 175's own text ("never equal to, inside or containing the project root, `models/` or `.secrets/`") reads the same way.
2. **Decision 177, where and for which pairs.** Proposed: one check in `profiles._view` (so `profile check` refuses before activation, and every request or eval run refuses at profile resolution), raising `ConfigurationError`, with the message naming `renderer.accepts_reference_images: false` (profile id, no file path, because the request boundary must not expose paths) and `edit.attach_kb_reference_images: true` and saying to set one of them. The pair is refused only when `edit` is in the profile's `supported_tasks` (a generate-only profile never receives attachments, so refusing it would be over-refusal and would stop one config serving a generate-only and an edit profile); no exemption for `rag.enabled` or for the request task, because the profile and config pair is what the decision names. `load_config` stays unchanged: it has no manifest. The writer's guard stays as defence (an unreachable branch through a validated profile, still tested through a double). Alternative dropped: a startup scan of every manifest under `profiles.root` in `load_config`; it couples the loader to profile files and still misses a profile added later.
3. **Which "store paths" does the root have to stay apart from?** Proposed: `storage.data_root` and `storage.kb_store` (the two keys the stages already compare). `.cache/`, `.tmp/`, `runs/`, `eval/runs/` are written project directories that are not in the brief's list; a root inside one is accepted, a root that contains one contains the project root and is refused.
4. **`.secrets/`: compare lexically only.** Proposed: its protected path is `<resolved project root>/.secrets`, never resolved or stat-ed by me. Known limit: if `.secrets` itself is a link to another place, a root at that other place is not recognised. `models/` and `.git`, `.venv*` are resolved (they are not secret), so a `models` junction to another drive is protected at its target.
5. **Scope of "outside".** Proposed: local drives and mounted directories are the supported case; UNC shares and volume-GUID mount points are not claimed (docs say so; GUID form refused by the absolute-path rule). Not run here: any second physical drive (the dev machine test is a sibling directory, plain and through a junction).

### The validation rule (proposed, one function in `config.py`)

`resolve_knowledge_root(config, project_root, path, *, require_dir)` is the only place that turns `knowledge.root` into a path; every consumer calls it and none keeps its own `_resolve_within` for this key.

1. The value is a non-empty string without NUL, passes `_validate_segments` (navigation allowed); a Windows drive spelling on POSIX is refused as today. A sentinel never arrives here (the callers skip it as `_configured_path` does today).
2. Candidate = the value if absolute, else the project root joined with it. Lexical pre-check of the spelled path against the protected set (no file system access to `.secrets`).
3. `physical = Path(_strip_extended_prefix(os.path.realpath(candidate)))`, once. It must be absolute; with `require_dir` it must be an existing directory (`knowledge.root: not an existing directory`, as today).
4. Protected set, each side resolved with `realpath` except `.secrets`: project root (refuse equal or containing), `models`, `.git`, every top-level `.venv*` name, `.secrets` (lexical), `storage.data_root`, `storage.kb_store` (both strict, as decision 174 keeps them; a sentinel is skipped). For each: refuse when `physical == p`, `p in physical.parents` or `physical in p.parents`, compared by path components with `os.path.normcase` (so `data` and `database`, `models` and `models2` are different, `.venvX` and `.venv` are both protected by the prefix rule).
5. Return `physical`. `sync_knowledge` calls it once after `validate_sync_config` and passes that one value to `sync(_resolved_root=)`, `embed_images`, `_read_overrides`, `retain_images`, `name_folders`, `observe_images`; K2/K3/K4 re-call the same function only to compare (K2) or to check overlap (K3, K4). Dropped alternative: a token object threaded through every stage signature; the same drift check without five signature changes and pins.

Layouts the tests cover (P = fake project root, X = a sibling temp directory):

| Layout | Verdict |
|---|---|
| `P/kb`, `P/database`, `P/a/b` (inside, unprotected) | accepted |
| `X/kb` absolute, `../kb` relative, a link `P/kb -> X/kb` (junction on Windows, symlink elsewhere) | accepted, `physical == realpath(X/kb)` |
| `P`, `P/..` (contains P), `X` that contains `P` | refused |
| `P/models`, `P/models/x`, a `models` junction target and its parent, `P/.git`, `P/.venv`, `P/.venv-gpu/x`, `P/.secrets`, `P/.secrets/x` | refused |
| `P/data`, `P/data/x`, `P/data/..`-spelled, `storage.kb_store` and its parent | refused |
| each refused root above spelled through a link, a junction, another case, a `\\?\` prefix, `.` and `..` components | same verdict as the physical path |
| `P/database` next to `data_root=data`, `P/models2` next to `models` | accepted (component comparison, not string prefix) |
| a link inside the knowledge base to `P` or to `P/.secrets` | K0 reports it (`links`, `protected_folders`) and does not follow it, as today; `kb sync` fails `k0: SOURCE_READ_FAILED` as today |
| `rag.enabled: false` | knowledge root never resolved, stat-ed or read (`test_disabled_sync_never_accesses_knowledge` keeps passing, with an external root too) |

### Plan

1. Experiment (read-only, scratch under `.tmp/KB-EXTERNAL/`): `realpath` of `link\..`, `\\?\`, case, short names and a root with a junction on Windows; record in `Research evidence`. Done check: the rule's step 3 is confirmed or the rule is revised and sent back.
2. `config.py`: add `resolve_knowledge_root`; route `_validate_configured_paths`, `_validate_k0_reads`, `validate_sync_config` and the `_resolved_knowledge_root` callers through it; fix the `_resolve_within` docstring (only `storage._fs.inside` still takes `links=True`). Tests: new `tests/test_knowledge_root.py` (table above, property test over generated layouts), `tests/test_config_links.py` updated (the old "inside the project" refusal becomes the external acceptance plus the protected refusals).
3. Stages: `k2_naming`, `k2_observe`, `k3_cards`, `k4_aliases._inputs` call the function; `kb_sync.sync_knowledge` and `dev_compare` pass the one physical path; `KB_SYNC_REVISION = 'kb-sync-6'` with its pin row. Tests: `test_kb_sync_flow.py` gets a flow with the knowledge base in a sibling temp directory (plain and through a link) that publishes; a link inside the external knowledge base; stage-argument drift; retrieval off with an external root.
4. Decision 177: the check in `profiles._view`; tests next to `tests/test_writer_attachments.py` and `tests/test_profiles.py` (refusal naming both keys and values; acceptance for each of the other three combinations, for a generate-only profile, with the overlay and an allowed request override setting the key; example profiles stay valid).
5. Docs: `docs/execution.md` lines 50 (the link sentence) and 56 (K0 paragraph), `docs/run-guide.md` (where a knowledge base may live), `configs/enhancer.example.yaml` comments (`knowledge.root`, `edit.attach_kb_reference_images`), `docs/implementation-notes.md` entry drafted for the PM.
6. Mutants (3 to 10), the listed pipeline test files one per process through `test_slot.py` (`--basetemp .tmp/KB-EXTERNAL/pytest`, size reported then removed), the report.

Finish checks map to the done check: (A) tests of steps 2 and 3 and at least one killed mutant of the root rule; (B) tests of step 4 and one killed mutant; stage pins pass (`tests/test_stage_revisions.py`); class-sweep table complete (below, draft).

### Configuration impact (draft; no new key)

| Setting | Schema and profile source | Change |
|---|---|---|
| `knowledge.root` | `enhancer_config.v0` `knowledge.root`; `configs/enhancer.example.yaml:19`, `enhancer.dev.example.yaml:24` | meaning widens: may be outside the project root; validated by `resolve_knowledge_root`; refusal messages keep the `knowledge.root:` prefix |
| `storage.data_root`, `storage.kb_store` | same schema; example lines 401-402 | unchanged values, now compared in both directions with the physical root in one place; still strict (no link) |
| `edit.attach_kb_reference_images` and manifest `renderer.accepts_reference_images` | schema `enhancer_config.v0`, `profile_manifest.v0`; example line 140, `_example/manifest.yaml:22` | new cross-check in `profiles._view`; both examples (false and false) stay valid |
| `rag.enabled` | existing | read before any knowledge-root access, unchanged (R-009) |
| `KB_SYNC_REVISION` | `kb_sync.py:33` | `kb-sync-6` (artifact-bound) |

## Root causes confirmed

- **(A) confirmed against the code.** The inside-the-root rule is in `_lexical_path` (`config.py:459-463`, `:468-469`) and in `_resolve_within` (`:184-185`, `:198-199`) and is repeated per stage; it was written for decision 174 ("`knowledge.root` ... must stay inside the project root", `execution.md:50`, `implementation-notes.md:92`), which decision 175 replaces.
- **(B) confirmed against the code.** `writer.render_brief:90-98` omits `attachments` when `accepts_reference_images` is false (PM degraded mode, `implementation-notes.md:87`); `_require_attachment_support` checks only that the key is a boolean (`config.py:1818-1823`), and no code path compares it with the manifest. Decision 177 replaces the degraded mode with a refusal.

## Class sweep

Draft from the reading above; confirmed row by row in step 2 to 4 (a grep of `knowledge.root`, `knowledge_root`, `knowledge['root']`, `_resolve_within(`, `_resolved_knowledge_root`, `inside(`, `_lexical_path(` is rerun then).

| Site | Today | New behaviour |
|---|---|---|
| `config._lexical_path` (`:453`) | refuses outside the root | unchanged for every owned path; `knowledge.root` no longer goes through it |
| `config._validate_configured_paths` (`:505-514`) | `_configured_path(..., links=True)` | `resolve_knowledge_root(require_dir=False)`, skipped for a sentinel and when `knowledge_access` is false |
| `config._resolved_knowledge_root` (`:2287`) | `_resolve_within(links=True)` | replaced by `resolve_knowledge_root` |
| `config._validate_k0_reads` (`:2296`) | root vs stores, `{.secrets, models}` | the function's protected-set check (stores included) |
| `config.validate_sync_config` (`:1614`) | root resolved and `is_dir` only with `rag.enabled` | same guard, `require_dir=True` |
| `config` scoped dicts (`:1350`, `:1429`, `:1496`) | reach `_validate_configured_paths` | same function through `_validate_configured_paths` |
| `k2_naming.py:488-498` | own `_resolve_within`, arg compared with `Path(arg).resolve()` | function result compared with the arg; overlap check in the function |
| `k2_observe.py:196-211` | same | same |
| `k3_cards.py:208-227` | own `resolved()` | the function |
| `k4_aliases.py:100-123` (`_inputs`) | own `resolved()` | the function (no K4 bump: `_inputs` is CHECK) |
| `kb_sync.py:217` and the five `source.resolve()` uses | `project / root`, re-resolved | `source = resolve_knowledge_root(...)` once, used everywhere; `kb-sync-6` |
| `k0_sync.sync:63`, `k0_checkpoint:179`, `k0_scan.py:246` | `Path.resolve()` fallback when no root is passed | `_resolved_root` always supplied by `sync_knowledge`; the fallbacks stay for direct low-level callers (labelled) |
| `embed_server/dev_compare.py:242,262,275` | `project / root`, unresolved | the function; `check_out_dir` compares with the physical root |
| `storage/_fs.inside` (`:103`) | `links=True`, store roots | not a knowledge-root call: unchanged |
| `accepts_reference_images`: `writer.py:92`, `pipeline.py:84` | degraded / raise | unchanged defence; new check in `profiles._view` |
| `attach_kb_reference_images`: `assembly.py:278,346`, `config.py:1819,2086,2101`, `intake.py:232,235`, `writer.py:88` | reads | unchanged; `profiles._view` reads it too |

## Pre-mortem (decision 169)

Each failure mode below was checked; the evidence is a test that exists and passes.

| Failure mode | Check (test, file) | Result |
|---|---|---|
| Root equal to or containing the project root, spelled or through a link | `test_the_project_root_and_the_protected_directories_are_refused[.|..]`, `test_a_parent_of_the_project_root_is_refused`, `test_a_link_to_a_parent_of_the_project_root_is_refused_with_the_root_message` (`tests/test_knowledge_root.py`) | refused, the message names the root |
| Root equal to, inside or containing `models/`, `.secrets/`, `.git`, `.venv*` (also `.venvother`, not yet created) | the same parametrized table; `test_a_protected_directory_that_is_itself_a_link_protects_its_target` | refused |
| `storage.data_root` or `storage.kb_store`, both directions, any spelling, through a link | `test_a_store_is_refused_in_both_directions_whatever_its_key` and the link tests | refused |
| Another case, a `\\?\` prefix, `.` and `..` components | Windows tests of the same file; the hypothesis oracle test | same verdict as the physical path |
| `.secrets/` opened or listed | `test_the_secrets_directory_is_compared_by_name_and_never_resolved` (spy on `_physical`) | never resolved for a direct spelling |
| A link inside the external knowledge base (to the project, to a `.secrets`-named decoy) | `test_a_link_inside_an_outside_knowledge_base_is_reported_and_never_followed` (`tests/test_kb_sync_flow.py`) | `k0: SOURCE_READ_FAILED`, no model request |
| A stage that still checks against the project root (the P3-SWEEP-2 K4 miss) | `test_a_sync_reads_a_knowledge_base_outside_the_project_root_in_every_stage` (K0 to K5 through absolute, relative, inside link and outside link; second run `unchanged`); `grep` below shows no stage keeps its own `_resolve_within` for this key | publishes |
| A root retargeted between K0 and K2 | `test_k2_stages_refuse_a_root_argument_that_is_not_the_configured_physical_root` | refused |
| Retrieval off touching the root | `test_disabled_sync_never_accesses_knowledge[inside|outside]` (spies on `resolve_knowledge_root` and `_physical`) | root never resolved |
| An owned path accepting a link | `tests/test_config_links.py` (10) | still refused |
| Decision 177 missing for an overlay or a request override | `test_resolving_refuses_a_request_that_turns_attachments_on_for_such_a_profile` (effective config copy), the matrix through `check_profile` (base plus overlay) | refused at check and at resolve |
| Decision 177 over-refusing `attach: false`, a generate-only profile, or an accepting renderer | the 12-case matrix in `test_attaching_needs_a_renderer_that_accepts_reference_images`; `test_resolving_accepts_the_override_for_a_generate_only_profile`; the example pair test | accepted |
| A stale activation after the change | not a failure: documented in `docs/run-guide.md` | `profile check` again |

## Completion report

**Behaviour.**
- (A, decision 175) `knowledge.root` may lie anywhere. `config.resolve_knowledge_root` resolves it once with `os.path.realpath` and is the only function that does; `_validate_configured_paths`, `validate_sync_config` (with `require_dir=True` when `rag.enabled`), `_validate_k0_reads`, K2 naming, K2 observation, K3, K4 `_inputs`, `kb_sync.sync_knowledge` and `dev_compare` call it. `sync_knowledge` resolves once and hands that path to K0, K1, K2, the overrides reader and `retain_images`. It refuses a physical root that equals or contains the project root, or equals, lies inside or contains `models/`, `.secrets/` (compared by name only), `.git`, any `.venv*`, `storage.data_root` or `storage.kb_store`. A root inside the project outside those (`database/`) stays valid. Owned paths still refuse any link (decision 174). Retrieval off never calls the function. `KB_SYNC_REVISION` is `kb-sync-6` with its appended pin row.
- (B, decision 177) `profiles._require_attachable_renderer`, called first in `profiles._view` (so `profile check` and every `resolve_profile`), raises `ConfigurationError` when `edit` is in `supported_tasks`, `edit.attach_kb_reference_images` is true and `renderer.accepts_reference_images` is false. The message holds the profile id, both keys, both values and "set renderer.accepts_reference_images to true or edit.attach_kb_reference_images to false", with no path. `writer.render_brief` now raises `ValueError` for the same pair (defence, tested through a double).
- **Intent check.** R-001 and R-004: no prompt, system prompt or output-format code was touched. R-009: retrieval off never resolves, stats or reads the root (spy test, inside and outside). R-014: the root is only read; all new tests leave the source tree unchanged. R-026: a link inside an external knowledge base is reported and never followed; no text from the knowledge base became an instruction.

**Commands and real output** (each one pytest process through `tools/agent-workflow/test_slot.py`, `--basetemp .tmp/KB-EXTERNAL/pytest`, logs in `.tmp/KB-EXTERNAL/logs/`):

| File | Result |
|---|---|
| `tests/test_knowledge_root.py` (new) | 45 passed, 1 skipped (POSIX-only drive spelling test, on Windows) |
| `tests/test_kb_sync_flow.py` | 33 passed |
| `tests/test_kb_sync_gates.py` | 21 passed |
| `tests/test_config_links.py` | 10 passed |
| `tests/test_stage_revisions.py` | 397 passed |
| `tests/test_profiles.py` | 192 passed, 2 skipped |
| `tests/test_writer_attachments.py` / `test_writer.py` | 18 passed / 17 passed |
| `tests/test_intake.py` | 72 passed |
| `tests/test_retained_images.py` | 72 passed, 2 skipped |
| `tests/test_pipeline_s0_s6.py`, `test_pipeline.py`, `test_pipeline_config.py` | 14, 18, 62 passed |
| `tests/test_cli_config.py`, `test_cli_commands.py`, `test_cli_enhance.py` | 70, 12, 24 passed |
| `tests/test_storage_config.py`, `test_trace_store.py`, `test_assembly.py`, `test_assembly_config.py`, `test_assembly_properties.py`, `test_run_guide.py` | 10, 7, 45, 18, 16, 8 passed |
| K0, K2, K3, K4, fix01/02, loader, dev_compare files (earlier in the task) | passed (logs `k0_review`, `k2naming`, `k2observe`, `k3*`, `k4`, `fix01`, `fix02`, `loader`, `devcmp`) |

Two existing tests changed because the behaviour changed on purpose: `test_writer_attachments` (the degraded brief became a refusal) and `test_intake::test_legacy_snapshot_refuses_edit_attachments_before_model_work` (it now uses a profile that accepts images; a new test holds the 177 refusal at intake, code `CONFIGURATION_INVALID`). `tests/intake_fixtures.activate` gives a configuration that attaches images a renderer that accepts them.

**Changed files.** `src/prompt_enhancer/`: `config.py`, `kb_sync.py`, `k2_naming.py`, `k2_observe.py`, `k3_cards.py`, `k4_aliases.py`, `embed_server/dev_compare.py`, `profiles.py`, `writer.py`. `tests/`: `test_knowledge_root.py` (new), `test_kb_sync_flow.py`, `test_kb_sync_gates.py`, `test_config_links.py`, `test_stage_revisions.py`, `test_profiles.py`, `test_writer_attachments.py`, `test_intake.py`, `intake_fixtures.py`. Docs: `docs/execution.md`, `docs/run-guide.md`, `configs/enhancer.example.yaml`. This task file.

**Effects the PM must know.** (1) `config.py` and `profiles.py` changed, so every existing `activation.json` is stale: run `profile check` again for each profile (stated in `docs/run-guide.md` and above). (2) The first `kb sync` after this lands re-plans once (`kb-sync-6`). (3) A deployment whose profile manifest says `accepts_reference_images: false` and whose configuration says `attach_kb_reference_images: true` now fails at `profile check` and at every request; the shipped examples (false, false) are valid.

**Draft entry for `docs/implementation-notes.md` (the PM merges it; PM decision pending user review for the two lines marked).**
- Built (decision 175): `knowledge.root` may be outside the project; one physical resolver (`config.resolve_knowledge_root`) used by every stage; refuses the project root or a container of it, and `models/`, `.secrets/`, `.git`, `.venv*`, `data_root`, `kb_store` by equality, containment either way. Interpretation: "inside the project" in decision 175 means "inside a protected directory"; `database/` stays valid (PM approved at Step 0).
- Built (decision 177): `profiles._view` refuses `renderer.accepts_reference_images: false` with `edit.attach_kb_reference_images: true` when the profile supports `edit`; the writer degraded mode became a defensive error.
- PM decision pending user review: `tests/intake_fixtures.activate` picks an image-accepting renderer when the configuration attaches images.
- PM decision pending user review: a generate-only profile is not refused (attachments exist only for edit).

## Configuration audit

No new configuration key. Row by row:

| Setting | Source of truth | Validation and refusal | Test | Class |
|---|---|---|---|---|
| `knowledge.root` | `enhancer_config.v0`; `configs/enhancer.example.yaml` (`deploy_required`), dev example `database/` | `resolve_knowledge_root`: non-empty string, no NUL, segments, absolute after `realpath`, protected set, `require_dir`; a sentinel refused where the root is needed, skipped where it is not | `test_knowledge_root.py` (table, hypothesis oracle, sentinel, non-path values, `require_dir` order); acceptance of an external root in `test_kb_sync_flow.py` | configurable |
| `storage.data_root`, `storage.kb_store` | same schema; example lines 401-402 | owned paths stay strict (no link); compared with the physical root in both directions | `test_knowledge_root.py` (both directions), `test_config_links.py` | configurable |
| `edit.attach_kb_reference_images` | `enhancer_config.v0`; example line 140 | boolean check unchanged; cross-checked with the manifest in `profiles._view` | `test_profiles.py` matrix, resolve and generate-only tests, `test_intake.py` | configurable |
| `renderer.accepts_reference_images` | `profile_manifest.v0`; `_example/manifest.yaml` | same cross-check; type must be the JSON boolean `false` to refuse | `test_profiles.py`, `test_writer_attachments.py` (double) | configurable (admin's manifest) |
| `rag.enabled` | existing | read before any root access (R-009) | `test_disabled_sync_never_accesses_knowledge[inside|outside]` | configurable |
| `KB_SYNC_REVISION` | `kb_sync.py` | pinned by AST digest | `test_stage_revisions.py` | artifact-bound |
| `PROTECTED_NAMES = ('.secrets', '.git', 'models')`, `PROTECTED_VENV_PREFIX = '.venv'` (`config.py`) | project layout (AGENTS.md section 6) | used by the resolver only | `test_knowledge_root.py` table | safety-bound |
| Literals `'storage.data_root'`, `'storage.kb_store'`, `'knowledge.root'` in the resolver | key names of the schema | none needed | the same tests | protocol-bound |
| Existing hidden default or unclassified literal | none found or added | none | grep of the resolver and `_require_attachable_renderer`: no number, no default value | none |

Every user value has validated config, an example profile and a refusal test. The non-default override test for `knowledge.root` is the external-root flow (`test_kb_sync_flow.py`); for the pair, the matrix row with `attach: true`.

## Mutants

13 mutants, each on a scratch copy of only the changed file, served to pytest by `.tmp/KB-EXTERNAL/scripts/mutplug.py` (a meta-path finder for that one module; no tree copy). Control (unmutated, same harness): 44 passed, 1 skipped. Raw rows: `.tmp/KB-EXTERNAL/mutants.md`.

| Id | Mutation | Killed by |
|---|---|---|
| M1 | `_overlaps`: `min` to `max` | `..._are_refused[.secrets/x]` |
| M2 | physical `_contains(physical, root)` to `physical == root` | **survived at first**: the later protected-name loop refuses a container of the root with the wrong message (it blames `models`). Added `test_a_link_to_a_parent_of_the_project_root_is_refused_with_the_root_message`; now killed |
| M3 | `_folded` without `os.path.normcase` | `test_the_secrets_directory_is_compared_by_name_and_never_resolved` |
| M4 | lexical pre-check removed | the same test (`_physical` would touch `.secrets`) |
| M5 | `.venv*` prefix refusal removed | `..._are_refused[.venvother/x]` |
| M6 | store overlap check removed | `..._are_refused[data]` |
| M7 | `require_dir` check removed | `test_the_directory_requirement_comes_last_and_names_the_key` |
| M8 | protected entries lexical only (no target after links) | `test_a_protected_directory_that_is_itself_a_link_protects_its_target` |
| M9 | 177 without the `edit in supported_tasks` condition | matrix `[True-False-tasks0]` |
| M10 | 177 `accepts is False` to `is True` | matrix `[True-True-tasks1]` |
| M11 | writer guard removed | `test_writer_guard_refuses_attached_images_for_a_profile_that_accepts_none` |
| M12 | K2 naming drift check removed | `test_k2_stages_refuse_a_root_argument_that_is_not_the_configured_physical_root` |
| M13 | K2 observation drift check removed | the same test |

## Research evidence

Experiment of Step 1 (synthetic directories under `.tmp/KB-EXTERNAL/probe-data/`, removed afterwards; script `.tmp/KB-EXTERNAL/scripts/probe.py`; output `.tmp/KB-EXTERNAL/probe.log`; Python 3.12.10, `ntpath.realpath`, Windows 11, NTFS). Label: experiment.

| Input (shortened) | `os.path.realpath` output | Meaning for the rule |
|---|---|---|
| `X\kb` (plain, outside P) | `X\kb` | unchanged |
| junction `P\lnk` to `Y\deep` | `Y\deep` | the target is what gets validated |
| `P\lnk\..\x` | `P\x` | Windows normalises `..` lexically before the link (matches `ntpath`); the physical result is what is checked |
| `P\lnk\..` | `P` | a root spelled through a link and `..` lands on the project: refused |
| chain of two junctions | `Y\deep` | followed to the end |
| `P\models\..\models` | `P\models` | normalised |
| upper-case spelling | on-disk case | the comparison also folds case (`normcase`) |
| `\\?\D:\...\kb`, and with a junction | keeps the `\\?\` prefix | `_strip_extended_prefix` removes it |
| short name `LONGFO~1` | not expanded | 8.3 expansion not shown here (the volume may disable it): `not_run` |
| nonexistent path | the absolute path | `require_dir` decides existence |
| `P\models\ml` junction to `X\kb` | `X\kb` | a root there is outside `models` after resolution; a `models` junction itself is protected at its target |
| drive root `D:\` | `D:\` | contains the project root: refused |
| relative `nope_rel` | joined to the working directory | the resolver joins to the project root first |

Rule step 3 was confirmed unchanged: nothing in the experiment makes it accept more. Primary-source reading of `os.path.realpath` for these cases is the Python 3.12.10 `ntpath` code run above (source-verified by behaviour); POSIX `..` behaviour is documented in `os.path.realpath` and is not run here.

## not_run

- Ruff is not installed: lint `not_run`.
- A second physical drive, a UNC share (`\\server\share`) and a volume mount point (`\\?\Volume{GUID}`): only a labelled double of the GUID result is tested; FAT, exFAT and network shares that report `st_ino` 0 (`MissingFileID`) are a documented limit.
- POSIX behaviour (a link target holding `..` after another link, symlink creation without privilege): the Linux CI runs it; here only Windows junctions were exercised. The 8.3 short-name expansion is not shown.
- The full suite, a live model call, a GPU run and any network access: not run, by the rules.
- Known limits: a `.secrets` that is itself a link is not recognised (the name is compared, never resolved); on Windows `realpath` of a candidate spelled through a link into `.secrets` makes a metadata-only open before the check refuses it (the lexical pre-check avoids this for the direct spellings).
- Measured effects: basetemp `.tmp/KB-EXTERNAL/pytest` was 601 KB after the last run (removed at the end).

## Noticed, not done

- `embed_server/reference.py:166-167` and `storage/store.py:88` each keep their own copy of the protected names, and `config.py` now has a third (`PROTECTED_NAMES`). Importing one shared definition is a refactor of the two other readers, not part of this task.
- `k0_sync.sync`, `k0_checkpoint.scan_to_checkpoint` and `k0_scan.scan_source` still call `Path.resolve()` when no `_resolved_root` is passed (low-level direct callers). `sync_knowledge` and `dev_compare` always pass it. A direct caller that skips it would not use `resolve_knowledge_root`.
- Follow-up from P3-SWEEP-2 still open: sync recovery from a damaged snapshot; the volume mount point wording on Windows.

## Fix round 1 (decision 164; two reviews PASS: Claude Opus P2-1, P3-1 to P3-4, Gemini Flash P3-2 to P3-4 and surviving mutants)

All changes are unstaged on top of the staged reviewed diff. No Git write.

**Root cause first (S31): error clarity lost at a redacting boundary.** `intake._refuse` (`intake.py:138-150`) keeps only `intake refused (<type>)` for every exception except a `fixed` tuple, because exception text may hold request values or paths. Every `ConfigurationError` raised for a profile and configuration mismatch that a request can reach after `profile check` (a changed configuration or an allowed override) was grepped (`profiles.py`, `profile_prompt.py`, `writer.py`, `pipeline.py`, `intake.py`):

| Site | Mismatch | Reached through | Text survives `intake.py:148-149,253-254`? |
|---|---|---|---|
| `profiles.py` `_require_attachable_renderer` (decision 177) | renderer accepts no images, configuration attaches them | `resolve_profile` in `intake` | **was lost; fixed here** (`AttachmentRendererMismatch`, in the fixed tuple) |
| `profiles.py:447` `_require_keys` | a key a conflict cites is missing or an unresolved sentinel in the effective configuration | `resolve_profile` in `intake` | no: `intake refused (ConfigurationError)`; the text holds the manifest path, so redaction is right here. Noticed |
| `profiles.py:460` `_config_fingerprint` | a cited value is not JSON data | `resolve_profile` in `intake` | no, same; the text holds the encoder's message. Noticed |
| `profile_prompt.py:166,171` `load_prompt` via `render_addendum` | addendum prompt file unreadable | `_view` in `resolve_profile` | no; file problem with an OS message, not a mismatch. Noticed |
| `profiles.py:79` | profile id not an id | `resolve_profile` (request `profile_id`) | no; the request schema checks the id first. Noticed |
| `writer.py:140` `writer.runtime_addendum: a required addendum cannot be disabled` | profile needs an addendum, configuration disables it | `prepare_write` in `pipeline._run_request:240`, after `intake` | not via `intake`: the exception leaves `_run_request`; `enhance once` prints `refused: <text>` (`cli_enhance.py:42`), `enhance serve` returns 400 `request overrides change the deployment configuration` (`cli_serve.py:339`) whatever the text. Text is fixed and path-free. Noticed |
| `writer.py:101,145,156` | writer prompt, cloud structured output, edit image role | `prepare_write` | same path as the row above; configuration problems independent of the profile |

Only the first row was changed. The others are listed under Noticed, not done.

**Item by item.**
1. **P2-1.** `profiles.AttachmentRendererMismatch(ConfigurationError)` with a fixed `MESSAGE` (the two keys, the two values `false` and `true`, the remedy; no path, no profile id, no configured value; the id was dropped from the text for that reason, `profile check` is run per profile so the user knows which). `intake._refuse` passes it through. `_require_attachable_renderer(manifest, config)` lost its unused `profile_id` argument. Tests: `test_intake.py::test_attachments_with_a_renderer_that_accepts_no_images_are_refused_before_any_snapshot` (exact text, edit and generate request), `test_the_refusal_names_both_keys_and_leaks_nothing_else` (the reviewer's reproduction: attach false serves, then true by an allowed request override `edit.attach_kb_reference_images` and by the configuration, edit request; text equals the constant, no root path, no `p1`, no profiles path), `test_every_other_configuration_error_stays_redacted_at_intake`; `test_profiles.py` matrix asserts the class, the constant and no path. An override is allowed for this key (`request_overrides.allowed_keys`), and it reaches the same path.
2. **P3-1.** `test_intake_config.py::test_rag_off_skips_knowledge_path_resolution` forbids `resolve_knowledge_root` and `_physical` with retrieval off, now with a real (non-sentinel) `knowledge.root` (with a sentinel the resolver was skipped whatever `knowledge_access` said, which is why MR1 survived), plus a control that the spy sees the call with retrieval on. Mutant F5 (`if knowledge_access:` to `if True:`) is killed by this file.
3. **P3-2.** `docs/run-guide.md`: the sentence is narrowed to `kb sync` and the request commands; the other commands still resolve the path (metadata only), as before. Behaviour unchanged.
4. **P3-3.** `docs/run-guide.md` and `docs/execution.md` say "may be refused (`MissingFileID`), not verified for FAT, exFAT or network shares"; `kb sync --plan` on the real drive is added to the first-deployment order (step 3).
5. **P3-4.** `config.py` strips the extended-length prefix from the spelled path before the lexical pre-check (`_strip_extended_prefix` already handles `\\?\UNC\`, which matters only when the project itself is on a UNC path). `test_the_secrets_directory_is_compared_by_name_and_never_resolved` now also spells `\\?\...\.secrets`, `\\?\...\.secrets\x`, `\\?\...\.SECRETS\x` and `//?/...` with the `_physical` spy (nothing resolved). Junction and 8.3 spellings stay a documented limit. Real UNC is `not_run`.
6. **Gemini P3-2 (K3, K4).** The line numbers in the finding (`k3_cards.py:439`, `k4_aliases.py:470`) do not exist (the files have 372 and 286 lines). The assignment is at `k3_cards.py:215` and `k4_aliases.py:111`, and **`source` is used**: `k3_cards.py:228` and `k4_aliases.py:124` compare it with the store and the data root. K3 and K4 never read the knowledge base (their docstring says so), so no `same_physical_path` comparison is needed. The overlap loop is a defence behind the resolver, which refuses the same overlap first. New test `test_k3_and_k4_refuse_a_root_that_overlaps_the_store_or_the_data_root` (4 cases). No stage source changed, so no pin or revision change.
7. **Gemini P3-4 (`_resolved_root`).** Both kept, because `k0_sync.sync`, `k0_checkpoint.scan_to_checkpoint` and `k0_scan.scan_source` are pinned low-level functions that the K0 tests and the fixtures (`k1_fixtures`, `k2_fixtures`, `kb_sync_fixtures`, `test_k0_sync`, `test_k0_review_fix`) call directly with a plain tree; dropping the fallback would edit pinned K0 functions (a K0 revision bump that invalidates every K0 checkpoint) for no behaviour change. Instead the production callers are pinned by tests: `test_kb_sync_hands_k0_the_one_physical_root` and `test_dev_compare_hands_k0_the_one_physical_root` (spy on `sync`: root and `_resolved_root` both equal the resolver result). Mutant F6 (the kwarg removed from `kb_sync`) is killed.
8. **Surviving mutants.** `test_a_resolving_failure_is_a_clean_refusal_without_the_exception_text` (a `ValueError` and an `OSError` from the resolving step give `knowledge.root: cannot be resolved: <type>` with no exception text; an embedded NUL is refused earlier, by the value check, so a labelled double of `_physical` is used) kills F3. `test_a_root_that_resolves_to_a_delete_pending_name_is_refused` (Windows only, labelled double returning `<drive>\$Extend\$Deleted\kb`) kills F4 (`_is_deleted_name`).

**Pre-mortem checks.** The new class carries only its constant (no argument, so nothing from the configuration can enter; test asserts equality with the constant and absence of the project root, the profiles path and `p1`). Every other `ConfigurationError` still redacts (new test with a path in the text). The prefix strip is a no-op for a non-prefixed path (`_strip_extended_prefix` returns the value unless it starts with `\\?\`); the two property tests (oracle over generated layouts, link verdicts) pass unchanged. No K3 or K4 source changed: `test_stage_revisions.py` 397 passed, no revision bump.

**Commands and output** (one pytest process per file through `test_slot.py`, `--basetemp .tmp/KB-EXTERNAL/pytest`, 438 KB at the end, removed): `test_knowledge_root.py` 55 passed, 1 skipped; `test_config_links.py` 10 passed; `test_intake_config.py` 31 passed; `test_intake.py` 74 passed; `test_profiles.py` 192 passed, 2 skipped; `test_kb_sync_gates.py` 21 passed; `test_kb_sync_flow.py` 33 passed; `test_stage_revisions.py` 397 passed; `test_writer_attachments.py` 18 passed; `test_retained_images.py` 72 passed, 2 skipped (it reads intake). Lint `not_run` (ruff not installed).

**Mutants of this round** (scratch copies of the one changed file, `.tmp/KB-EXTERNAL/scripts/mutate.py`; raw rows in `.tmp/KB-EXTERNAL/mutants.md`): F1 removing the subclass from the fixed tuple, F2 removing the prefix strip, F3 `except (OSError, ValueError)` to `except OSError`, F4 `_is_deleted_name` check removed, F5 `if knowledge_access:` to `if True:`, F6 the `_resolved_root` kwarg removed in `kb_sync`. All killed. F5 survived once: with a sentinel `knowledge.root` the test never reached the resolver, so the test was strengthened (real value, control) and F5 is killed on the rerun (the first F5 row in `mutants.md` is the survivor, the later ones are the kills).

**Draft `implementation-notes` entry, additions (the PM merges).**
- Built (decision 177, fix round): `profiles.AttachmentRendererMismatch`, a `ConfigurationError` with a fixed text, is passed through by `intake._refuse`, so `enhance once`, `enhance serve` (through the envelope) and `eval run` show the two keys at request time like `profile check`. The profile id is not in the text (fixed text by design).
- Built: the knowledge-root pre-check strips a `\\?\` prefix before comparing, so `.secrets` is never resolved for that spelling either.
- Docs: retrieval off skips the root for `kb sync` and the request commands only; FAT, exFAT and network shares are not verified; `kb sync --plan` on the real drive is a deployment check.
- PM decision pending user review: unchanged (fixture default of an image-accepting renderer; generate-only profile not refused).

**Noticed, not done (fix round).**
- The `ConfigurationError` sites in the table above that stay redacted at request time (`_require_keys`, `_config_fingerprint`, the addendum prompt, profile id): a user who changes a conflict-cited key and sees only `intake refused (ConfigurationError)` gets the same clarity loss. Each text holds a path or a library message, so a fixed-text subclass per case is a separate decision.
- `edit.attach_kb_reference_images` is not in the activation fingerprint (`_config_fingerprint` hashes conflict-cited keys only), so turning it on after `profile check` keeps the activation valid and the refusal appears at request time. The alternative the reviewer named (add it to the fingerprint) was not taken: the brief asked for the fixed-text subclass, and the check at every request stays the authority.
- `enhance serve` answers 400 `request overrides change the deployment configuration` for any `ConfigurationError` raised after `intake` (`cli_serve.py:339`), whatever its text.
