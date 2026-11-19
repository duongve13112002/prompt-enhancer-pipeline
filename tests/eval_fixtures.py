"""Shared setup of the evaluation-runner tests (T-016 P4).

Labelled substitutes: the loopback model double (`KnowledgePlanner`, a `FullServerDouble`), its recording and gating
wrappers, and a synthetic set built here (tiny, hand-written, never the real draft). The CLI, adapter, coordinator, data
store and snapshot reads are production code. Evidence class of every test using these: `mock_contract`."""
import base64
from copy import deepcopy
import hashlib
import json
import os
import subprocess
import sys
from threading import Lock

import pyarrow as pa
import pyarrow.parquet as pq

from cli_fixtures import (KnowledgePlanner, NO_WINDOW, ROOT, attaching_config, finish_attaching_setup, model_server,
                          published_knowledge, write_yaml)
from prompt_enhancer.storage.identity import concept_id

S = pa.string()
SOURCE = pa.struct([('image_id', S), ('folder_id', S)])
LINK = pa.struct([('mention', S), ('status', S), ('concept_id', S), ('role', S)])
CONSTRAINT = pa.struct([('id', S), ('type', S), ('text', S), ('span', S), ('bound_entity', S)])
DETAIL = pa.struct([('mention', S), ('negated_by_user', pa.bool_()), ('family_member_ids', pa.list_(S)),
                    ('family_conflict_ids', pa.list_(S)), ('note', S)])
GENERATOR = pa.struct([('role', S), ('model_revision', S), ('prompt_id', S)])
SCHEMA = pa.schema([
    ('scenario_id', S), ('kind', S), ('task', S), ('prompt', S), ('source_image', SOURCE),
    ('expected_concept_links', pa.list_(LINK)), ('expected_constraints', pa.struct([('constraints', pa.list_(CONSTRAINT))])),
    ('generator', GENERATOR), ('tier', S), ('split', S), ('draft_link_details', pa.list_(DETAIL)), ('draft_edit_key', S),
    ('draft_rag_enabled', pa.bool_()), ('draft_language', S), ('draft_format', S)])
CONCEPT = concept_id('Name')  # the concept `published_knowledge` publishes (folder `f`, info `Name`)
EDIT_KEY = '{"targets": ["the thing"], "delta": ["change it"], "preserve": ["the rest"]}'
SEED = 11


def record(scenario_id, prompt, *, task='generate', kind='single_concept', split='test', rag=True, linked=True,
           literal=None, image=None, language='en', fmt='prose'):
    """One synthetic scenario of the draft layout; `linked` adds a resolved link to the published concept `Name`."""
    links = [{'mention': 'Name', 'status': 'resolved', 'concept_id': CONCEPT, 'role': 'subject_identity'}] if linked else []
    constraints = [] if literal is None else [{'id': 'c1', 'type': 'literal_text', 'text': 'keep the text', 'span': literal,
                                                'bound_entity': None}]
    return {'scenario_id': scenario_id, 'kind': kind, 'task': task, 'prompt': prompt,
            'source_image': None if image is None else {'image_id': image, 'folder_id': 'f'},
            'expected_concept_links': links, 'expected_constraints': {'constraints': constraints},
            'generator': {'role': 'scenario_generator', 'model_revision': 'synthetic test set', 'prompt_id': 'test-only'},
            'tier': 'silver', 'split': split,
            'draft_link_details': [{'mention': 'Name', 'negated_by_user': False, 'family_member_ids': [],
                                    'family_conflict_ids': [], 'note': ''}] if linked else [],
            'draft_edit_key': EDIT_KEY if task == 'edit' else None, 'draft_rag_enabled': rag,
            'draft_language': language, 'draft_format': fmt}


def write_set(path, records, schema=SCHEMA):
    """The records as a Parquet file of the draft layout, in small row groups so batches are read lazily."""
    pq.write_table(pa.Table.from_pylist(list(records), schema=schema), path, row_group_size=2)
    return path


def standard_set(originals):
    """Four scenarios: two generate and one edit with retrieval on (one literal constraint), one with retrieval off."""
    image = sorted(originals)[0]
    return [record('gen-1', 'A Name in the garden "OPEN" sign', literal='"OPEN"', split='fit'),
            record('gen-2', 'Une Name très calme, 静かな Name', split='fit', language='mixed'),
            record('edit-1', 'Change the Name please', task='edit', kind='edit_local', image=image, split='fit'),
            record('off-1', 'A plain bowl, no extras', kind='rag_off', rag=False, linked=False)]


def evaluation_values(config, *, concurrency=2, seed=SEED):
    """Every consumed `evaluation` leaf resolved (test-only values); `min_items_per_class` is left unresolved on
    purpose: the runner does not read it."""
    config['evaluation'].update(
        run={'max_set_bytes': 1_000_000, 'max_source_image_bytes': 1_000_000, 'max_concurrency': concurrency,
             'max_sidecar_bytes': 5_000_000},
        judge={'blinding_seed': seed, 'score_min': 1, 'score_max': 5, 'min_scorers': 2,
               'under_test_families': ['family-under-test'], 'rubric_prompt_id': 'eval_rubric_v0',
               'max_score_file_bytes': 100_000})
    # test-only: a trace slot frees only after its trace write finishes, so `queue_limit == max_concurrency` can still
    # refuse a unit for want of a trace slot (retest N1); room for every unit of the small test sets keeps these tests
    # about the runner, while the trace_capacity path has its own test
    config['runtime']['trace']['queue_limit'] = max(config['runtime']['trace']['queue_limit'], concurrency + 16)
    # test-only: the client's own limit stays above the runner's bound, so the number of requests the double sees at once is
    # decided by `evaluation.run.max_concurrency` and by nothing else
    config['vlm']['backends']['main']['max_concurrency'] = concurrency + 4
    return config


def eval_config(k2_env, model_test_profile, tmp_path, project_root, port, **evaluation):
    config = attaching_config(k2_env, model_test_profile, tmp_path, project_root, port)
    return evaluation_values(config, **evaluation)


def start(args, project_root, **popen):
    """The real entry point as a hidden child process (the caller waits or kills it)."""
    environ = dict(os.environ, PYTHONPATH=os.environ.get('PROMPT_ENHANCER_TEST_SRC', str(ROOT / 'src')),
                   PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    return subprocess.Popen([sys.executable, '-X', 'faulthandler', '-m', 'prompt_enhancer', *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ, creationflags=NO_WINDOW,
                            cwd=project_root, **popen)


def run_args(config_path, project_root, set_path, snapshot, *, variants='both', split='all', extra=()):
    args = ['eval', 'run', '--config', config_path, '--project-root', project_root, '--set', set_path,
            '--variants', variants, '--split', split, *extra]
    return args if snapshot is None else [*args, '--snapshot', snapshot]


class VariantDouble(KnowledgePlanner):
    """Labelled double whose writer answer tells the retrieval variants apart (the brief of an on request carries a
    `knowledge` object), so a test can see which output a judge pack shows under which label."""
    real_writer = None  # the writer system text; `prepared` moves it here so the answer below is reached

    def scripted_answer(self, body):
        if self.real_writer is not None and body['messages'][0]['content'] == self.real_writer:
            parts = body['messages'][-1]['content']
            text = parts if isinstance(parts, str) else next(item['text'] for item in parts if item['type'] == 'text')
            return 'with knowledge' if '"knowledge"' in text else 'without knowledge'
        return None


class Recording:
    """Counts planner requests by prompt and holds those past `free` until the gate is released."""

    def __init__(self, gate=None, free=None):
        self.lock, self.prompts, self.gate, self.free = Lock(), [], gate, free

    def double(self, base=VariantDouble):
        parent, recording = base.planner_answer, self

        def planner(data):
            with recording.lock:
                recording.prompts.append(data['prompt'])
                held = recording.gate is not None and (recording.free is None or len(recording.prompts) > recording.free)
            if held:
                recording.gate.hold()
            return parent(data)
        return type('RecordingDouble', (base,), {'planner_answer': staticmethod(planner)})


def prepared(k2_env, model_test_profile, tmp_path, project_root, port, handler, **evaluation):
    """A published knowledge base and an activated profile; returns (config, originals)."""
    originals = published_knowledge(k2_env)
    config = eval_config(k2_env, model_test_profile, tmp_path, project_root, port, **evaluation)
    finish_attaching_setup(config, project_root, handler)
    handler.real_writer, handler.writer_system = handler.writer_system, None  # after activation, which needs the plain answer
    return config, originals


def snapshot_of(k2_env):
    return k2_env.store.current()[0]


def result_rows(config, project_root):
    from prompt_enhancer.storage import open_data_store
    return open_data_store(config, 'eval run', project_root=project_root).read_rows('eval_results')


def comparable(row):
    """A result row without what legitimately differs between two runs of one set (ids of the run and of the trace)."""
    row = deepcopy(row)
    for name in ('run_id', 'config_hash'):
        row.pop(name)
    row['metrics']['envelope'].pop('trace_id')
    return row


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def b64(data):
    return base64.b64encode(data).decode('ascii')


__all__ = ['model_server', 'write_yaml', 'json']


# ----------------------------------------------------------------------------- synthetic runs (no model, no snapshot)


def synthetic_run(tmp_path, project_root, model_test_profile, records, *, mode='both', seed=SEED, split='all',
                  concurrency=2, fill=True, output=lambda scenario_id, variant: f'{variant} output of {scenario_id}'):
    """A run built without any model: with `fill`, finished, its result rows written through the production row builder
    and data store; without, ready to execute. Labelled synthetic: the envelopes are hand-made (valid against
    `response_envelope.v0`), so what a judge pack shows under which label can be checked against known outputs."""
    from cli_fixtures import pipeline_config
    from prompt_enhancer.config import validate_evaluation_settings
    from prompt_enhancer.eval_run import Run, canonical
    from prompt_enhancer.eval_set import digest_tag, read_records, selected, survey_set, variants_for
    from prompt_enhancer.storage import open_data_store
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1, retrieval=True)
    config['rag']['enabled'] = True  # the on variant keeps it; the runner makes the off variant's copy
    evaluation_values(config, seed=seed, concurrency=concurrency)
    store = open_data_store(config, 'eval run', project_root=project_root)
    data = write_set(tmp_path / 'synthetic.parquet', records).read_bytes()
    settings = validate_evaluation_settings(config, 'eval run', project_root=project_root)
    survey = survey_set(data, store.catalog, kinds=settings['scenario_kinds'], mode=mode, split=split)
    rid = 'a1b2c3d4e5f60718'
    manifest = {'schema': 'eval_run_manifest.v0', 'run_id': rid,
                'inputs': {'config': {'on': 'sha256:' + '0' * 64, 'off': 'sha256:' + '1' * 64}},
                'units': {'total': survey.units, 'skipped': survey.skipped, 'selected': dict(survey.selected)}}
    rubric = (ROOT / 'prompts' / 'eval_rubric_v0.txt').read_bytes()
    run = Run(config, project_root, settings, store, data, digest_tag(data), mode, split, survey, None, None, rid,
              manifest, None, rubric)
    store.write_sidecar(run.relative('manifest.json'), canonical(manifest))
    lock = store.run_lock(rid, create=True)  # a started run has its lock file (taken and given back, as `prepare` does)
    assert lock.try_acquire()
    lock.release()
    for index, rec in enumerate(read_records(data, store.catalog) if fill else ()):
        for variant in variants_for(rec, mode) if selected(rec, split) else ():
            store.append('eval_results', [result_row(run, rec, variant, output(rec.scenario['scenario_id'], variant),
                                                     f'trace-{variant}-{index}')])
    return run, config


class SpiedLock:
    """Labelled wrapper: the real lock, plus a record of the explicit `release` (garbage collection would hide a leak)."""

    def __init__(self, lock):
        self.lock, self.released = lock, False

    def release(self):
        self.released = True
        self.lock.release()


def spy_on_locks(monkeypatch, module):
    """Wrap `module.hold_run` so every lock it hands out is recorded; returns the list of `SpiedLock`."""
    taken, real = [], module.hold_run

    def spied(*args, **kwargs):
        taken.append(SpiedLock(real(*args, **kwargs)))
        return taken[-1]
    monkeypatch.setattr(module, 'hold_run', spied)
    return taken


def result_row(run, rec, variant, output, trace_id):
    """One `eval_results` row of a synthetic run, built by the production metrics builder."""
    from prompt_enhancer.eval_run import Unit, build_metrics
    envelope = {'status': 'success', 'output': output, 'attachments': [], 'source_binding': None,
                'retrieval': {'state': 'retrieved' if variant == 'on' else 'disabled', 'snapshot_id': None},
                'warnings': [], 'trace_id': trace_id}
    return {'run_id': run.run_id(variant), 'scenario_id': rec.scenario['scenario_id'],
            'metrics': build_metrics(run, Unit(rec, variant, {}), envelope), 'status': 'success',
            'config_hash': 'sha256:' + '0' * 64}


def pack_lines(run, pack_id):
    path = run.store.root / 'eval' / 'runs' / run.rid / 'packs' / pack_id[7:23] / 'pack.jsonl'
    return [json.loads(line) for line in path.read_bytes().decode('utf-8').split('\n') if line]


def score_document(header, items, scorer, value=lambda scenario_id, label, dimension, ref: 3):
    """A valid score file for a pack: every label x dimension of every item rated once."""
    return {'schema': 'eval_judge_scores.v0', 'pack_id': header['pack_id'], 'scorer': scorer,
            'scores': [{'scenario_id': item['scenario_id'],
                        'ratings': [{'label': output['label'], 'dimension': entry['dimension'], 'ref': entry['ref'],
                                     'value': value(item['scenario_id'], output['label'], entry['dimension'], entry['ref'])}
                                    for output in item['outputs'] for entry in item['dimensions']]}
                       for item in items]}
