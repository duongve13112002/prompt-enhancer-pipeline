"""`eval run`: a named scenario set through the production S0-S6 coordinator, one durable result per scenario (T-016 P4).

Order of effects: configuration, set (every record and referenced id checked), named snapshot, run identity and manifest,
then the scenarios. Retrieval on and off run in separate owned `Pipeline` lifecycles; a retrieval-off unit never opens a
store or a gateway (R-009). Scenarios run concurrently, at most `evaluation.run.max_concurrency` at once in total (user
decision 149): a producer feeds a bounded queue from the streaming adapter, workers call `Pipeline.enhance`, and ONE writer
task does every result write, finished-set check and `after_delivery`, so no duplicate row can race. A worker keeps its
slot (and its trace slot) until the writer has finished with its response.

Resume is by content identity (`run_identity`), never by a random id: the finished set is read from the durable results at
the start and re-checked before every write. One process owns a run at a time: `prepare` takes the run's lock without
waiting (`DataStore.run_lock`, built from `storage.lock`) and a second `eval run` or an `eval import` of that run is refused
at once, because the finished-set check cannot see another process (fix round 1). A result is one segment file written
whole or not at all, so a killed run leaves whole results only. The summary is recomputed from the durable state at every
end; nothing in it is trusted input, and a repeated result is a storage error, never counted twice.
Results hold structural evidence only; semantic metrics and intervals are `not_run` until scores are imported.
"""
import asyncio
import base64
from collections import Counter
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from .cli_input import Refusal
from .config import ConfigurationError, validate_evaluation_settings
from .eval_set import (VARIANTS, digest_tag, read_limited, read_records, selected, source_file, survey_set,
                       variants_for, check_snapshot_ids)
from .pipeline import GoldenAdapter, Pipeline
from .profile_prompt import ProfileError, load_prompt
from .profiles import resolve_profile
from .response_lifecycle import LifecycleFull
from .storage import StorageError, open_data_store, open_knowledge_store
from .storage.store import check_snapshot_id

EVAL_RUN_REVISION = 'eval-run-1'  # artifact-bound: declared revision of this runner (the stage-revision convention)
RUN_DIR = 'eval/runs'  # artifact-bound: where run sidecars live under data_root
INCOMPLETE_DIR = 'incomplete'  # artifact-bound: one marker per unit that has no result (failed, or not run for a reason)
TRACE_CAPACITY = 'trace_capacity'  # protocol-bound reason: the pipeline refused the unit for want of a trace slot
NOT_ATTEMPTED = 'not_attempted'  # protocol-bound reason: a selected unit nothing has tried yet
ENVELOPE_SCHEMA = 'response_envelope.v0'  # artifact-bound
METRICS_SCHEMA = 'eval_result_metrics.v0'  # artifact-bound
NOT_RUN_SEMANTIC = 'scoring contract not approved'  # protocol-bound reason text (PM answer Q1, 2026-11-18)
NOT_RUN_INTERVALS = 'interval method not approved'
NOT_RUN_LINKS = 'trace row unavailable'  # no bounded read of one trace row by trace_id exists (trace_store.py)


class RunAborted(RuntimeError):
    """The run cannot go on (an operational failure): the named snapshot changed, or a result could not be stored."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def config_hash(config):
    """The configuration of one variant without `evaluation`: those leaves bound the run, they never change a result."""
    return digest_tag(canonical({key: value for key, value in config.items() if key != 'evaluation'}))


def marker_name(variant, scenario_id):
    return f'{variant}-{hashlib.sha256(scenario_id.encode("utf-8")).hexdigest()[:32]}.json'


def hold_run(store, rid, *, create):
    """Take the exclusive lock of run `rid` without waiting, or refuse at once: every writer of the run's results and
    sidecars (and of its summary) holds it, so two processes never both run, or run and import, one run (R-017).
    Raises Refusal when another process holds it, or (with `create` false) when the run never started."""
    lock = store.run_lock(rid, create=create)
    try:
        acquired = lock.try_acquire()
    except FileNotFoundError:
        raise Refusal('--run-id: no such run in the data root') from None
    if not acquired:
        raise Refusal('this run is in use by another process (an eval run or eval import of the same run); '
                      'run the command again after that process has ended')
    return lock


@dataclass
class Run:
    config: dict
    root: object
    settings: dict
    store: object
    data: bytes
    set_sha: str
    mode: str
    split: str
    survey: object
    snapshot_id: str
    source_dir: str
    rid: str
    manifest: dict
    pin: object
    rubric: bytes
    lock: object = None  # the run lock, held from the manifest write to the end of the invocation

    @property
    def cap(self):
        return self.settings['run.max_sidecar_bytes']

    @property
    def catalog(self):
        return self.store.catalog

    @property
    def variants(self):
        return [variant for variant in VARIANTS if self.survey.selected[variant]]

    def run_id(self, variant):
        return f'{self.rid}-{variant}'

    def relative(self, name):
        return f'{RUN_DIR}/{self.rid}/{name}'


def _model_ids(config):
    result = {}
    for role in ('planner', 'writer', 'verifier'):
        settings = config['vlm']['roles'].get(role)
        backend = config['vlm']['backends'].get(settings['backend']) if isinstance(settings, dict) else None
        result[role] = backend.get('model') if isinstance(backend, dict) else None
    return result


def _prompt_hashes(config, root):
    result = {}
    for role in ('planner', 'writer', 'verifier'):
        settings = config['vlm']['roles'].get(role)
        if isinstance(settings, dict) and isinstance(settings.get('prompt_id'), str):
            try:
                result[role] = digest_tag(load_prompt(root, settings['prompt_id']))
            except ConfigurationError:
                result[role] = None
    return result


def prepare(config, root, args):
    """Everything that can refuse, before the first model call. Raises Refusal or ConfigurationError."""
    settings = validate_evaluation_settings(config, 'eval run', project_root=root)
    store = open_data_store(config, 'eval run', project_root=root)
    data = read_limited(root, args.set, settings['run.max_set_bytes'], '--set', 'evaluation.run.max_set_bytes')
    survey = survey_set(data, store.catalog, kinds=settings['scenario_kinds'], mode=args.variants, split=args.split)
    if survey.selected['on'] == survey.selected['off'] == 0:
        raise Refusal('--split: no scenario is selected')
    if survey.any_on and not config['rag']['enabled']:
        raise Refusal('retrieval on needs rag.enabled true in the profile (R-009: a profile with retrieval off has no store)')
    if survey.any_on and args.snapshot is None:
        raise Refusal('--snapshot: required, because some scenario runs with retrieval on')
    if not survey.any_on and args.snapshot is not None:
        raise Refusal('--snapshot: not accepted when every scenario runs with retrieval off (R-009: no knowledge-base access)')
    if bool(survey.off_image_ids) != (args.source_images is not None):
        raise Refusal('--source-images: given exactly when an edit scenario runs with retrieval off')
    for image_id in sorted(survey.off_image_ids):
        source_file(root, args.source_images, image_id, settings['run.max_source_image_bytes'],
                    'evaluation.run.max_source_image_bytes')
    try:
        profile = resolve_profile(config, project_root=root)
    except ProfileError as error:
        raise Refusal(f'an active admin profile is required ({error.code})') from None
    rubric = load_prompt(root, settings['judge.rubric_prompt_id'])
    pin = manifest_sha = None
    if survey.any_on:
        try:
            check_snapshot_id(args.snapshot)
        except StorageError:
            raise Refusal('--snapshot: not a snapshot id') from None
        knowledge = open_knowledge_store(config, 'eval run', project_root=root)
        pin = knowledge.pin()
        try:
            if pin is None or pin.snapshot_id != args.snapshot:
                raise Refusal('--snapshot: not the current published snapshot (S0 pins only the current one)')
            check_snapshot_ids(survey, pin)
            manifest_sha = next(row['manifest_sha256'] for row in knowledge.snapshots()
                                if row['snapshot_id'] == pin.snapshot_id)
        except BaseException:
            if pin is not None:
                pin.release()
            raise
    off_config = deepcopy(config)
    off_config['rag']['enabled'] = False
    parts = {'revision': EVAL_RUN_REVISION, 'pipeline': GoldenAdapter.identity, 'set': digest_tag(data),
             'snapshot': None if pin is None else {'id': pin.snapshot_id, 'manifest_sha256': manifest_sha},
             'config': {'on': config_hash(config) if survey.selected['on'] else None,
                        'off': config_hash(off_config) if survey.selected['off'] else None},
             'models': _model_ids(config), 'prompts': _prompt_hashes(config, root),
             'profile': {'id': profile.profile_id, 'system_prompt_sha256': profile.system_prompt_sha256,
                         'manifest_sha256': profile.manifest_sha256},
             'variants': args.variants, 'split': args.split}
    rid = hashlib.sha256(canonical(parts)).hexdigest()[:16]
    manifest = {'schema': 'eval_run_manifest.v0', 'run_id': rid, 'inputs': parts,
                'units': {'total': survey.units, 'skipped': survey.skipped, 'selected': dict(survey.selected)}}
    run = Run(config, root, settings, store, data, parts['set'], args.variants, args.split, survey,
              None if pin is None else pin.snapshot_id, args.source_images, rid, manifest, pin, rubric)
    try:
        run.lock = hold_run(store, rid, create=True)  # before the first read of the durable state (R-017)
        _write_manifest(run)
    except BaseException:
        release(run)
        raise
    return run


def release(run):
    """Give back what `prepare` took: the run lock and the snapshot pin; safe to call twice."""
    lock, run.lock = run.lock, None
    pin, run.pin = run.pin, None
    try:
        if lock is not None:
            lock.release()
    finally:
        if pin is not None:
            pin.release()


def _write_manifest(run):
    data = canonical(run.manifest)
    try:
        existing = run.store.read_sidecar(run.relative('manifest.json'), run.cap)
    except FileNotFoundError:
        run.store.write_sidecar(run.relative('manifest.json'), data)
        return
    if existing != data:
        raise Refusal('the run directory holds another manifest (never overwritten)')


def scan_results(run):
    """One pass over the durable results of this run, a segment at a time: where each lives and the small statistics
    the summary needs. A malformed or repeated result is a storage error, never counted."""
    wanted = {run.run_id(variant): variant for variant in run.variants}
    keys = {}
    stats = {variant: {'completed': 0, 'status': Counter(), 'items': 0, 'present': 0, 'absent': 0, 'unchecked': 0}
             for variant in run.variants}
    for entry in run.store.segments('eval_results'):
        for row in run.store.read_segment('eval_results', entry):
            if row['run_id'] not in wanted:
                continue
            key = (row['run_id'], row['scenario_id'])
            if key in keys:
                raise StorageError('eval_results: a result is stored twice for one run and scenario')
            metrics = row['metrics']
            problem = run.catalog.document_error(METRICS_SCHEMA, metrics)
            if problem is not None or metrics['structural']['envelope_status'] != row['status']:
                raise StorageError(f'eval_results: a result of this run is malformed ({problem or "status"})')
            keys[key] = entry
            stat = stats[wanted[row['run_id']]]
            stat['completed'] += 1
            stat['status'][row['status']] += 1
            for item in metrics['structural']['literal']['items']:
                stat['items'] += 1
                stat['absent' if item['present'] is False else 'unchecked' if item['present'] is None else 'present'] += 1
    return keys, stats


def refresh_summary(run, keys, stats):
    """Recompute `summary.json` from the durable state and replace it atomically; returns it."""
    names = {marker_name(key[0].rsplit('-', 1)[1], key[1]) for key in keys}  # a unit with a result has no open marker
    failed, reasons = {variant: 0 for variant in run.variants}, Counter()
    for name in run.store.list_sidecars(run.relative(INCOMPLETE_DIR)):
        variant = name.split('-', 1)[0]
        if name in names or variant not in failed:
            continue
        marker = json.loads(run.store.read_sidecar(run.relative(f'{INCOMPLETE_DIR}/{name}'), run.cap))
        if marker.get('disposition') == 'failed':
            failed[variant] += 1
        elif marker.get('disposition') == 'not_run' and isinstance(marker.get('reason'), str):
            reasons[marker['reason']] += 1
        else:
            raise StorageError(f'{run.relative(INCOMPLETE_DIR)}/{name}: a marker is malformed')
    selected_units = run.survey.selected
    units = {'total': run.survey.units, 'skipped': run.survey.skipped, 'selected': sum(selected_units.values()),
             'completed': sum(stat['completed'] for stat in stats.values()), 'failed': sum(failed.values())}
    units['not_run'] = units['selected'] - units['completed'] - units['failed']
    if units['not_run'] < 0 or sum(reasons.values()) > units['not_run']:  # a result or marker the run cannot account for
        raise StorageError('eval_results: the durable results do not fit the selected units of this run')
    reasons[NOT_ATTEMPTED] = units['not_run'] - sum(reasons.values())
    imports = []
    for name in run.store.list_sidecars(run.relative('imports')):
        imports.append(json.loads(run.store.read_sidecar(run.relative(f'imports/{name}'), run.cap))['summary'])
    summary = {
        'schema': 'eval_run_summary.v0', 'run_id': run.rid, 'units': units,
        'not_run_reasons': {name: count for name, count in sorted(reasons.items()) if count},
        'by_variant': {variant: {'selected': selected_units[variant], 'completed': stats[variant]['completed'],
                                 'failed': failed[variant],
                                 'not_run': selected_units[variant] - stats[variant]['completed'] - failed[variant],
                                 'envelope_status': dict(sorted(stats[variant]['status'].items()))}
                       for variant in run.variants},
        'structural': {'literal': {'denominator': 'constraints of type literal_text with a span, in completed units',
                                   **{name: sum(stat[name] for stat in stats.values())
                                      for name in ('items', 'present', 'absent', 'unchecked')}},
                       'links': {'state': 'not_run', 'reason': NOT_RUN_LINKS}},
        'semantic': ({'state': 'agent_judged', 'evidence': 'agent_judged', 'pilot_only': True, 'imports': imports}
                     if imports else {'state': 'not_run', 'reason': NOT_RUN_SEMANTIC}),
        'intervals': {'state': 'not_run', 'reason': NOT_RUN_INTERVALS},
        'packs': [name for name in run.store.list_sidecars(run.relative('packs'))]}
    run.store.write_sidecar(run.relative('summary.json'), canonical(summary))
    return summary


# ----------------------------------------------------------------------------- the scenarios


@dataclass
class Unit:
    record: object
    variant: str
    request: dict


def build_request(run, record, variant):
    """The `request.v0` of one unit: the prompt as stored; an edit's source bytes from the snapshot by content id (on) or
    from `--source-images` (off), never from the knowledge-base tree."""
    scenario = record.scenario
    request = {'prompt': scenario['prompt'], 'task': scenario['task']}
    if scenario['task'] == 'edit':
        image_id = scenario['source_image']['image_id']
        raw = (run.pin.read_image(image_id) if variant == 'on' else
               source_file(run.root, run.source_dir, image_id, run.settings['run.max_source_image_bytes'],
                           'evaluation.run.max_source_image_bytes'))
        request['source_image'] = base64.b64encode(raw).decode('ascii')
    return request


def literal_items(scenario, output):
    """Exact literal constraints: each `literal_text` constraint with a span is `present` when the span is a byte-for-byte
    part of a text output; for an output that is not text the answer is None (unchecked), never a guess."""
    return [{'constraint_id': item['id'],
             'present': None if not isinstance(output, str) else item['span'] in output}
            for item in scenario['expected_constraints']['constraints'] if item['type'] == 'literal_text' and item['span']]


def build_metrics(run, unit, envelope):
    return {'schema': METRICS_SCHEMA, 'evidence': 'structural', 'variant': unit.variant, 'set_sha256': run.set_sha,
            'snapshot_id': envelope['retrieval']['snapshot_id'], 'scenario_kind': unit.record.scenario['kind'],
            'split': unit.record.scenario['split'], 'envelope': envelope,
            'structural': {'schema_valid': True, 'envelope_status': envelope['status'],
                           'literal': {'state': 'checked', 'items': literal_items(unit.record.scenario, envelope['output'])},
                           'links': {'state': 'not_run', 'reason': NOT_RUN_LINKS}},
            'semantic': {'state': 'not_run', 'reason': NOT_RUN_SEMANTIC},
            'intervals': {'state': 'not_run', 'reason': NOT_RUN_INTERVALS}}


async def execute(run):
    """Run every selected unit that has no durable result yet. Returns the counts of this invocation."""
    keys, _ = scan_results(run)
    done = set(keys)
    limit, loop = run.settings['run.max_concurrency'], asyncio.get_running_loop()
    queue, outcomes = asyncio.Queue(maxsize=limit), asyncio.Queue()
    counts = Counter()
    async with AsyncExitStack() as stack:
        pipelines = {}
        for variant in run.variants:
            config = run.config
            if variant == 'off':
                config = deepcopy(run.config)
                config['rag']['enabled'] = False
            pipelines[variant] = await stack.enter_async_context(Pipeline(config, project_root=run.root))

        async def produce():
            for record in read_records(run.data, run.catalog):
                if not selected(record, run.split):
                    continue
                for variant in variants_for(record, run.mode):
                    if (run.run_id(variant), record.scenario['scenario_id']) not in done:
                        await queue.put(Unit(record, variant, build_request(run, record, variant)))
            for _ in range(limit):
                await queue.put(None)

        async def work():
            try:
                while (unit := await queue.get()) is not None:
                    try:
                        outcome = (unit, await pipelines[unit.variant].enhance(unit.request), None)
                    except LifecycleFull:  # no trace slot, refused before any model call: not run, retried next time
                        outcome = (unit, None, ('not_run', TRACE_CAPACITY))
                    except Exception as error:  # an operational failure of one unit: counted, never a result
                        outcome = (unit, None, ('failed', type(error).__name__))
                    acknowledged = loop.create_future()
                    await outcomes.put((outcome, acknowledged))
                    await acknowledged  # the slot is held until the writer has finished with the response
            finally:
                await outcomes.put(None)

        async def write():
            finished = 0
            while finished < limit:
                item = await outcomes.get()
                if item is None:
                    finished += 1
                    continue
                (unit, response, error), acknowledged = item
                try:
                    await record_outcome(run, unit, response, error, done, counts)
                finally:
                    if not acknowledged.done():
                        acknowledged.set_result(None)

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                group.create_task(write())
                for _ in range(limit):
                    group.create_task(work())
        except ExceptionGroup as group_error:
            raise group_error.exceptions[0] from None
    return counts


async def record_outcome(run, unit, response, error, done, counts):
    """The writer's step for one finished unit: a result row and the acknowledgement, or a marker. `error` is None or
    `(disposition, reason)`: ('failed', exception class) or ('not_run', reason); the marker (one file per unit, replaced
    by a later one) carries the class or the reason only, never a message, a prompt or an output."""
    scenario_id = unit.record.scenario['scenario_id']
    key = (run.run_id(unit.variant), scenario_id)
    if response is not None:
        envelope = response.envelope
        problem = run.catalog.document_error(ENVELOPE_SCHEMA, envelope)
        snapshot = envelope['retrieval']['snapshot_id'] if problem is None else None
        if problem is not None:
            error = ('failed', 'EnvelopeInvalid')
        elif unit.variant == 'on' and snapshot is not None and snapshot != run.snapshot_id:
            await response.abandon()
            raise RunAborted('the current snapshot changed during the run')
    if response is None or error is not None:
        if response is not None:
            await response.abandon()
        disposition, text = error
        marker = {'scenario_id': scenario_id, 'variant': unit.variant, 'disposition': disposition, 'reason': text}
        await asyncio.to_thread(run.store.write_sidecar,
                                run.relative(f'{INCOMPLETE_DIR}/{marker_name(unit.variant, scenario_id)}'), canonical(marker))
        counts[disposition] += 1
        return
    if key in done:  # defence: a key is written once per run, whatever produced it
        await response.abandon()
        raise RunAborted('a result for this run and scenario already exists')
    metrics = build_metrics(run, unit, envelope)
    row = {'run_id': key[0], 'scenario_id': scenario_id, 'metrics': metrics, 'status': envelope['status'],
           'config_hash': run.manifest['inputs']['config'][unit.variant]}
    try:
        await asyncio.to_thread(run.store.append, 'eval_results', [row])
    except BaseException:
        await response.abandon()
        raise
    done.add(key)
    counts['completed'] += 1
    await response.after_delivery()


def run_eval(config, root, args):
    """Prepare, execute and summarise; returns the summary (also written to `summary.json`). The summary is rewritten
    from the durable state whatever ended the run, so an interrupted run says what is done and what is not. `prepare`
    refuses before any side effect (exit 2); once it has returned, the run lock is held and the manifest is written, so a
    refusal raised later (a source file that changed since `prepare`) is an operational failure, `RunAborted` (exit 1).
    The lock is held until the summary and the pack are written and is released on every exit path."""
    run = prepare(config, root, args)
    try:
        try:
            asyncio.run(execute(run))
        except Refusal as refusal:
            raise RunAborted(str(refusal)) from None
        finally:
            keys, stats = scan_results(run)
            summary = refresh_summary(run, keys, stats)
        if summary['units']['failed'] == 0 and summary['units']['not_run'] == 0:
            from .eval_judge import write_pack
            summary['pack'] = write_pack(run, keys)
            summary = refresh_summary(run, keys, stats) | {'pack': summary['pack']}
        return summary
    finally:
        release(run)
