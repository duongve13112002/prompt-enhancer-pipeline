"""RES-1 (decision 149): each concurrent path equals its serial run, failures and stops included.

Every stage that sends model or embedder requests is run with `max_concurrency` 1 (serial) and with a higher value.
The labelled async transport double holds the measured first arrivals, then applies seeded delays and counts flight.
Rows, reports (minus the timing-only capacity record), checkpoint rows and stored artifacts must be equal, and the
peak in flight must reach the configured capacity. No network or inference.
"""

import asyncio
from dataclasses import replace
import hashlib
import json
import random

import httpx
import pytest

from candidates_fixtures import QueryEncoderDouble, candidate_env, source_only  # noqa: F401  (fixture)
from concurrency_fixtures import ArrivalGate
from k1_fixtures import EmbeddingServer, image_bytes, k0_handoff, k1_profile, make_source
from k2_fixtures import NamingServer, assessment, environment, folder, naming, scan
from k2_observe_fixtures import ObserverServer, k1_result
from k3_fixtures import CardServer
from k4_fixtures import AliasServer, inputs as alias_inputs
from test_planner import kb  # noqa: F401  (fixture: a real tiny snapshot with the alias source only)
from prompt_enhancer import planner as planner_module
from prompt_enhancer.candidates import build_candidates
from prompt_enhancer.k1_embed import embed_concept_texts, embed_images, ConceptText
from prompt_enhancer.k2_naming import _digest, name_folders
from prompt_enhancer.k2_observe import observe_images
from prompt_enhancer.k3_cards import build_cards
from prompt_enhancer.k4_aliases import build_aliases
from prompt_enhancer.model_client import BoundModelClient
from prompt_enhancer.model_resources import TIMING_FIELDS, flow
from prompt_enhancer.model_transport import ModelTransport
from prompt_enhancer.storage import StorageError, concept_id
from prompt_enhancer.storage.store import Checkpoints, new_snapshot_id
from storage_fixtures import make_store

CAP = 3  # the non-default capacity of the concurrent runs


@pytest.mark.parametrize('stage', ['k1_images', 'k1_texts', 'k2_naming', 'k2_observe', 'k3', 'k4'])
def test_failure_maps_sorted_under_reversed_completion(project_root, tmp_path, model_test_profile, monkeypatch, stage):
    """Labelled failures finish in reverse order of the first admitted requests; map iteration stays canonical."""
    finished, admitted = [], []

    async def reversed_failure(self, request):
        index = len(admitted)
        admitted.append(index)
        await asyncio.sleep(.15 if index == 0 else .001)
        finished.append(index)
        return httpx.Response(500, json={'error': 'labelled reversed-completion failure'})

    monkeypatch.setattr(Flight, '__call__', reversed_failure)
    if stage == 'k1_images':
        result, *_ = run_k1_images(project_root, tmp_path, model_test_profile, CAP, 3, 1)
    elif stage == 'k1_texts':
        config = k1_profile(model_test_profile, batch_size=2)
        config['vlm']['backends']['embedder']['max_concurrency'] = CAP
        items = [ConceptText(concept_id(str(i)), 'name', str(i)) for i in range(7)]
        result = asyncio.run(embed_concept_texts(make_store(project_root, tmp_path / 'kb'), config, items,
                                               transport=Flight(EmbeddingServer().handler, 1).transport()))
    else:
        env = environment(project_root, tmp_path, model_test_profile)
        if stage == 'k2_naming':
            result, _ = run_naming(env, CAP, 1)
        elif stage == 'k2_observe':
            result, _ = run_observer(env, CAP, 1)
        elif stage == 'k3':
            data = card_inputs(env, 6)
            data['snapshot_id'] = new_snapshot_id()
            result, _ = run_cards(env, CAP, 1, data)
        else:
            result, _ = run_aliases(env, CAP, 1, NAMES)
    assert finished[0] != 0
    report = result['report']
    assert len(report['failed']) > 1
    assert list(report['failed']) == sorted(report['failed'])
    if 'decode_failed' in report:
        assert list(report['decode_failed']) == sorted(report['decode_failed'])


class Flight:
    """Labelled counting double at the HTTP boundary: seeded delays, requests in flight, answers returned."""

    def __init__(self, handler, seed, fail=None, slow=None, *, target=1):
        self.handler, self.fail, self.slow = handler, fail, slow
        self.gate = ArrivalGate(target)
        self.rng = random.Random(seed)
        self.now = self.peak = 0
        self.returned = []

    async def __call__(self, request):
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await self.gate.arrive()
            await asyncio.sleep(self.rng.uniform(0.001, 0.02) + (self.slow(request) if self.slow else 0))
            failed = self.fail(request) if self.fail is not None else None
            response = failed if failed is not None else self.handler(request)
            self.returned.append(json.loads(request.content))
            return response
        finally:
            self.now -= 1

    def transport(self):
        return httpx.MockTransport(self)


def timed_run(server, env, flight):
    run = server.run(env)
    for name in list(run.transports):
        run.transports[name] = ModelTransport(name, env.config['vlm']['backends'][name], run.concurrency,
                                              run.ledger, transport=flight.transport())
    return run


def payload_of(body):
    content = body['messages'][1]['content']
    return json.loads(content[0]['text'] if isinstance(content, list) else content)


def strip(value):
    """Wall-clock values removed: call-record latency and the timing fields of capacity records."""
    if isinstance(value, dict):
        return {key: strip(item) for key, item in value.items() if key not in ('latency_s', 'capacity')}
    if isinstance(value, list):
        return [strip(item) for item in value]
    return value


def stored(store, stages):
    """Checkpoint rows and JSON artifacts of the stages, independent of write order and timing."""
    result = {}
    with store.writer() as writer:
        rows = writer._state_rows()
        for (stage, key), row in sorted(rows.items()):
            if stage not in stages:
                continue
            artifact = None
            if row['state'] == 'done' and row['output_key'].endswith('.bin'):
                artifact = strip(json.loads(writer.checkpoints(stage).load(key)))
            result[(stage, key)] = (row['state'], row['output_key'], row['error'],
                                    strip(row['call_records']), artifact)
    return result


def check_capacity(report, backend, requests, cap):
    record = report['capacity'][backend]
    assert set(record) >= set(TIMING_FIELDS) and record['max_concurrency'] == cap
    assert record['requests'] == requests
    assert record['peak_in_flight'] == min(cap, requests)
    assert record['in_flight_slot_s'] > 0 and record['busy_s'] > 0


def twin_envs(project_root, tmp_path, profile):
    envs = []
    for name in ('serial', 'concurrent'):
        (tmp_path / name).mkdir()
        envs.append(environment(project_root, tmp_path / name, profile))
    return envs


# K4 aliases (sweep row 6) ----------------------------------------------------------------------------------------

def alias_fail(request):
    payload = payload_of(json.loads(request.content))
    if payload['concept_name'] == 'Concept 04':
        return httpx.Response(500, json={'error': 'labelled server failure'})
    if payload['concept_name'] == 'Concept 07':
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'not': 'an alias reply'})}}]})
    return None


def run_aliases(env, cap, seed, names, flights=None, *, measure=False):
    env.config['vlm']['backends']['main']['max_concurrency'] = cap
    server = AliasServer()
    flight = Flight(server.handler, seed, alias_fail, target=min(cap, len(names)) if measure else 1)
    if flights is not None:
        flights.append(flight)

    async def execute():
        run = timed_run(server, env, flight)
        try:
            return await build_aliases(env.store, env.config, client=BoundModelClient(run, 'alias_generator'),
                                       **alias_inputs(names))
        finally:
            await run.close()
    return asyncio.run(execute()), flight


NAMES = tuple(f'Concept {index:02d}' for index in range(10))


def test_k4_concurrent_equals_serial_with_failures(project_root, tmp_path, model_test_profile):
    serial_env, concurrent_env = twin_envs(project_root, tmp_path, model_test_profile)
    serial, one = run_aliases(serial_env, 1, 1, NAMES, measure=True)
    concurrent, many = run_aliases(concurrent_env, CAP, 2, NAMES, measure=True)
    assert sorted(serial['report']['failed'].values()) == ['HTTP_ERROR', 'SCHEMA_INVALID']
    assert strip(concurrent) == strip(serial)
    assert stored(concurrent_env.store, {'k4_generation', 'k4_call_failure', 'k4_aliases'}) == stored(
        serial_env.store, {'k4_generation', 'k4_call_failure', 'k4_aliases'})
    assert one.peak == 1 and many.peak == CAP
    check_capacity(serial['report'], 'main', len(NAMES), 1)
    check_capacity(concurrent['report'], 'main', len(NAMES), CAP)


def test_k4_fatal_error_drains_and_keeps_every_received_answer(k2_env, monkeypatch):
    original, flights, at_failure = Checkpoints.commit, [], []

    def broken(self, input_key, kind, *args, **kwargs):  # labelled storage failure on the second generated commit
        result = original(self, input_key, kind, *args, **kwargs)
        if kind == 'k4_generation' and not at_failure and json.loads(args[0])['payload']:
            committed.append(input_key)
            if len(committed) == 2:
                at_failure.append((len(flights[0].returned), flights[0].now))
                raise StorageError('labelled storage failure')
        return result

    committed = []
    monkeypatch.setattr(Checkpoints, 'commit', broken)
    with pytest.raises(StorageError, match='labelled'):
        run_aliases(k2_env, CAP, 3, NAMES, flights)
    monkeypatch.setattr(Checkpoints, 'commit', original)
    flight = flights[0]
    (returned, _), = at_failure
    # After the failure nothing new is admitted: only the other items then running (at most CAP - 1) still reach the
    # server, and they finish instead of being cancelled.
    assert len(flight.returned) - returned <= CAP - 1 and flight.now == 0
    rows = stored(k2_env.store, {'k4_generation'})
    # Every answer received has its checkpoint row (done, or failed for the labelled HTTP and schema failures).
    assert len(rows) == len(flight.returned) < len(NAMES)
    assert {row[0] for row in rows.values()} <= {'done', 'failed'}


def test_k4_stop_cancels_at_once_and_resume_equals_serial(project_root, tmp_path, model_test_profile):
    serial_env, stopped_env = twin_envs(project_root, tmp_path, model_test_profile)
    serial, _ = run_aliases(serial_env, 1, 4, NAMES)
    stopped_env.config['vlm']['backends']['main']['max_concurrency'] = CAP
    server = AliasServer()
    flight = Flight(server.handler, 5, alias_fail)

    async def stop():
        run = timed_run(server, stopped_env, flight)
        try:
            task = asyncio.create_task(build_aliases(stopped_env.store, stopped_env.config,
                                                     client=BoundModelClient(run, 'alias_generator'),
                                                     **alias_inputs(NAMES)))
            while len(flight.returned) < 4:
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert [other for other in asyncio.all_tasks() if other is not asyncio.current_task()] == []
            assert flight.now == 0
        finally:
            await run.close()

    asyncio.run(stop())
    done = {key for (_, key), row in stored(stopped_env.store, {'k4_generation'}).items() if row[0] == 'done'}
    assert 1 <= len(done) < len(NAMES)
    resumed, again = run_aliases(stopped_env, CAP, 6, NAMES)
    assert len(again.returned) == len(NAMES) - len(done)  # a finished concept is never asked again
    assert resumed['report']['reused'] == len(done)
    assert resumed['report']['generated'] + len(done) == serial['report']['generated']
    for report in (resumed['report'], serial['report']):
        report.pop('reused'), report.pop('generated')
    assert strip(resumed) == strip(serial)


# K3 cards (row 5) ------------------------------------------------------------------------------------------------

def card_inputs(env, count):
    for index in range(count):
        folder(env, f'f{index}', f'Card {index}\nHuman description {index}', count=2)
    current = k1_result(env)
    named = NamingServer()
    from k2_fixtures import invoke
    names = invoke(env, named)
    concepts = {row['folder_id']: row['concept_id'] for row in names['rows']['folder_concepts']}
    images = sorted(current['images'], key=lambda row: row['image_id'])
    observations = []
    for index, image in enumerate(images):
        image['near_duplicate_group'] = 'group-' + str(index)
    for member in sorted(current['folder_images'], key=lambda row: row['image_id']):
        observations.append({'context_key': _digest({'context': member['image_id']}), 'image_id': member['image_id'],
                             'concept_id': concepts[member['folder_id']], 'facet': 'identity.hair',
                             'model_revision': 'observer-test', 'prompt_id': 'observer_v0',
                             'facet_schema_version': 'image_observation.v0', 'facet_group': 'identity',
                             'value': 'value ' + member['image_id'][-1], 'visibility': 'clear', 'region': None,
                             'view': 'front', 'quality': 1.0, 'origin': 'generated'})
    return {'handoff': names['rows'], 'observations': observations, 'images': images,
            'folder_images': current['folder_images'], 'overrides': names['effective_overrides'],
            'snapshot_id': 'snapshot-' + '0' * 20}


def run_cards(env, cap, seed, data):
    env.config['vlm']['backends']['main']['max_concurrency'] = cap
    server = CardServer()

    def fail(request):
        if payload_of(json.loads(request.content))['concept_name'] == 'Card 2':
            return httpx.Response(503, json={'error': 'labelled overload'})
        return None
    flight = Flight(server.handler, seed, fail, target=min(cap, len(data['handoff']['concepts'])))

    async def execute():
        run = timed_run(server, env, flight)
        try:
            return await build_cards(env.store, env.config, client=BoundModelClient(run, 'card_builder'), **data)
        finally:
            await run.close()
    return asyncio.run(execute()), flight


def test_k3_concurrent_equals_serial_with_a_failure(project_root, tmp_path, model_test_profile):
    serial_env, concurrent_env = twin_envs(project_root, tmp_path, model_test_profile)
    snapshot = new_snapshot_id()
    results = []
    for env, cap, seed in ((serial_env, 1, 7), (concurrent_env, CAP, 8)):
        data = card_inputs(env, 6)
        data['snapshot_id'] = snapshot
        results.append(run_cards(env, cap, seed, data))
    (serial, one), (concurrent, many) = results
    assert list(serial['report']['failed'].values()) == ['HTTP_ERROR']
    assert strip(concurrent) == strip(serial)
    stages = {'k3_grouping', 'k3_card', 'k3_call_failure'}
    assert stored(concurrent_env.store, stages) == stored(serial_env.store, stages)
    assert one.peak == 1 and many.peak == CAP
    check_capacity(concurrent['report'], 'main', 6, CAP)


# K2 observation (row 4) ------------------------------------------------------------------------------------------

def run_observer(env, cap, seed):
    env.config['vlm']['backends']['main']['max_concurrency'] = cap
    env.config['observe']['representatives_per_folder'] = 2
    for index in range(4):
        folder(env, f'o{index}', f'Observed {index}\nDescription', count=3)
    vectors = [[1.0, index / 10] for index in range(12)]
    inputs = k1_result(env, vectors=vectors)
    server = ObserverServer()

    def fail(request):
        if payload_of(json.loads(request.content))['concept_names'][0]['name'] == 'Observed 1':
            return httpx.Response(500, json={'error': 'labelled failure'})
        return None
    flight = Flight(server.handler, seed, fail, target=min(cap, 8))  # four folders, two representatives each

    async def execute():
        run = timed_run(server, env, flight)
        try:
            return await observe_images(env.store, env.config, knowledge_root=env.source,
                                        client=BoundModelClient(run, 'observer'), **inputs)
        finally:
            await run.close()
    return asyncio.run(execute()), flight


def test_k2_observation_concurrent_equals_serial_with_a_failure(project_root, tmp_path, model_test_profile):
    serial_env, concurrent_env = twin_envs(project_root, tmp_path, model_test_profile)
    serial, one = run_observer(serial_env, 1, 9)
    concurrent, many = run_observer(concurrent_env, CAP, 10)
    assert serial['report']['failed'] and serial['report']['queued']
    assert strip(concurrent) == strip(serial)
    assert stored(concurrent_env.store, {'k2_observation'}) == stored(serial_env.store, {'k2_observation'})
    assert one.peak == 1 and many.peak == CAP
    check_capacity(concurrent['report'], 'main', len(many.returned), CAP)


# K2 naming (row 3): seeded draws keep serial order -----------------------------------------------------------------

def run_naming(env, cap, seed, *, stop_after=None, measure=False):
    env.config['vlm']['backends']['main']['max_concurrency'] = cap
    env.config['knowledge']['naming'].update(sample_size=2)
    for index in range(4):
        folder(env, f'declared{index}', f'Declared {index}\nDescription {index}', count=4)
    for index in range(5):
        folder(env, f'unnamed{index}', count=4, payload=None)
    scanned = scan(env)
    # Two declared folders are judged unusable: they draw only after an awaited assessment, so without the ordered
    # gate a later folder (drawing at once) would take the seeded draws first.
    server = NamingServer({('info_assessor', 'declared0'): assessment(usable=False),
                           ('info_assessor', 'declared1'): assessment(usable=False),
                           ('folder_namer', 'declared0'): naming(('Drawn zero',), info_fits=False),
                           ('folder_namer', 'declared1'): naming(('Drawn one',), info_fits=False),
                           ('folder_namer', 'unnamed2'): {'invalid': 'reply'},
                           ('folder_namer', 'unnamed4'): naming(('Shared spelling',)),
                           ('folder_namer', 'unnamed1'): naming(('shared SPELLING',))})

    def fail(request):
        if payload_of(json.loads(request.content))['folder_name'] == 'declared2':
            return httpx.Response(500, json={'error': 'labelled failure'})
        return None

    def slow(request):  # the first folder's assessment answers last, so later folders reach their draw first
        data = payload_of(json.loads(request.content))
        return 0.15 if data['folder_name'] == 'declared0' and 'sampled_image_ids' not in data else 0
    flight = Flight(server.handler, seed, fail, slow, target=min(cap, 9) if measure else 1)

    async def execute():
        run = timed_run(server, env, flight)
        try:
            task = asyncio.create_task(name_folders(
                env.store, env.config, scanned['handoff']['k2'], images=scanned['rows']['images'],
                folder_images=scanned['rows']['folder_images'], knowledge_root=env.source,
                client=BoundModelClient(run, 'info_assessor'), rng=random.Random(42)))
            if stop_after is None:
                return await task
            while len(flight.returned) < stop_after:
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert [other for other in asyncio.all_tasks() if other is not asyncio.current_task()] == []
            return None
        finally:
            await run.close()
    return asyncio.run(execute()), flight


def test_k2_naming_concurrent_equals_serial_with_seeded_draws_and_failures(project_root, tmp_path,
                                                                          model_test_profile):
    serial_env, concurrent_env = twin_envs(project_root, tmp_path, model_test_profile)
    serial, one = run_naming(serial_env, 1, 11, measure=True)
    concurrent, many = run_naming(concurrent_env, CAP, 12, measure=True)
    assert sorted(serial['report']['failed'].values()) == ['HTTP_ERROR', 'SCHEMA_INVALID']
    # Two folders give the same concept in different spellings: the first folder in order names it, as in serial.
    shared = concept_id('Shared spelling')
    assert {row['concept_id']: row['display_name'] for row in serial['rows']['concepts']}[shared] == 'shared SPELLING'
    assert strip(concurrent) == strip(serial)
    stages = {'k2_assessment', 'k2_naming', 'k2_draw', 'k2_draw_head', 'k2_redraw', 'k2_call_failure',
              'k2_overrides', 'k2_names_applied'}
    assert stored(concurrent_env.store, stages) == stored(serial_env.store, stages)
    assert one.peak == 1 and many.peak == CAP
    check_capacity(concurrent['report'], 'main', len(many.returned), CAP)


def test_k2_naming_stop_then_resume_equals_serial(project_root, tmp_path, model_test_profile):
    serial_env, stopped_env = twin_envs(project_root, tmp_path, model_test_profile)
    serial, whole = run_naming(serial_env, 1, 13)
    _, first = run_naming(stopped_env, CAP, 14, stop_after=3)
    resumed, again = run_naming(stopped_env, CAP, 15)  # the resume rescans the same source bytes
    assert len(first.returned) >= 3
    assert len(again.returned) < len(whole.returned)  # answers checkpointed before the stop are not asked again
    assert strip(resumed['rows']) == strip(serial['rows'])


# K1 images and concept texts (rows 1 and 2) ------------------------------------------------------------------------

def k1_source(root):
    files = {f'{index:02d}.png': image_bytes(size=(4 + index, 3), color=(index * 9, 20, 30)) for index in range(9)}
    files['03.png'] = b'not an image'  # undecodable, inside a chunk: it is left out of that chunk's request
    files['06.png'] = b'still not an image'
    return make_source(root, files)


def content_fails(body):
    """Labelled failure choice by request content, so the serial and concurrent runs fail the same chunk."""
    return hashlib.sha256(json.dumps(body['input'], sort_keys=True).encode('utf-8')).digest()[0] % 2 == 0


def run_k1_images(project_root, root, profile, cap, workers, seed, *, failing=False):
    config = k1_profile(profile, batch_size=2)
    config['vlm']['backends']['embedder']['max_concurrency'] = cap
    config['runtime']['cpu_workers'] = workers
    source = k1_source(root / 'source')
    store = make_store(project_root, root / 'kb')
    handoff = k0_handoff(store, source)
    server = EmbeddingServer()

    def fail(request):
        body = json.loads(request.content)
        if failing and content_fails(body):
            server.requests.append(('failed', body))
            return httpx.Response(500, json={'error': 'labelled failure'})
        return None
    flight = Flight(server.handler, seed, fail, target=min(cap, 4))  # seven valid images in batches of two
    result = asyncio.run(embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                      transport=flight.transport()))
    return result, flight, store, server


def batches(server):
    return sorted(json.dumps(body['input'], sort_keys=True) for _, body in server.requests)


@pytest.mark.parametrize('failing', [False, True])
def test_k1_images_concurrent_equal_serial_with_same_batches(project_root, tmp_path, model_test_profile, failing):
    serial, one, serial_store, serial_server = run_k1_images(project_root, tmp_path / 'one', model_test_profile,
                                                             1, 1, 16, failing=failing)
    concurrent, many, store, server = run_k1_images(project_root, tmp_path / 'many', model_test_profile,
                                                    CAP, 3, 17, failing=failing)
    assert batches(server) == batches(serial_server)  # batch composition never depends on concurrency (bf16)
    assert sorted(len(json.loads(item)) for item in batches(server))[0] == 1  # a chunk lost an undecodable image
    assert serial['report']['decode_failed'] and bool(serial['report']['failed']) == failing
    for report in (serial['report'], concurrent['report']):  # every image has exactly one outcome
        assert report['embedded'] + len(report['decode_failed']) + len(report['failed']) == 9
    for key in ('images', 'embedder_revision', 'dim'):
        assert concurrent[key] == serial[key]
    assert concurrent['image_embeddings'].equals(serial['image_embeddings'])
    assert strip(concurrent['report']) == strip(serial['report'])
    assert stored(store, {'k1_image'}) == stored(serial_store, {'k1_image'})
    assert one.peak == 1 and many.peak == CAP
    check_capacity(concurrent['report'], 'embedder', len(many.returned), CAP)


def test_k1_images_fatal_error_keeps_every_received_vector(project_root, tmp_path, model_test_profile, monkeypatch):
    original = Checkpoints.commit
    vectors = []

    def broken(self, input_key, kind, data, *args, **kwargs):  # labelled storage failure on the third vector
        result = original(self, input_key, kind, data, *args, **kwargs)
        if kind == 'k1_image' and json.loads(data)['vector'] is not None:
            vectors.append(input_key)
            if len(vectors) == 3:
                raise StorageError('labelled storage failure')
        return result

    monkeypatch.setattr(Checkpoints, 'commit', broken)
    with pytest.raises(StorageError, match='labelled'):
        run_k1_images(project_root, tmp_path, model_test_profile, CAP, 2, 18)
    monkeypatch.setattr(Checkpoints, 'commit', original)
    # Every vector the server returned before the stop of admissions was committed, not only the first three.
    assert len(vectors) >= 3


def test_k1_texts_concurrent_equal_serial(project_root, tmp_path, model_test_profile):
    items = [ConceptText(concept_id(f'Text {index}'), 'name', f'Text {index}') for index in range(9)]
    outcomes = []
    for name, cap, seed in (('one', 1, 19), ('many', CAP, 20)):
        config = k1_profile(model_test_profile, batch_size=2)
        config['vlm']['backends']['embedder']['max_concurrency'] = cap
        store = make_store(project_root, tmp_path / name)
        server = EmbeddingServer()
        flight = Flight(server.handler, seed, target=min(cap, 5))
        result = asyncio.run(embed_concept_texts(store, config, items, transport=flight.transport()))
        outcomes.append((result, flight, store, server))
    (serial, one, serial_store, serial_server), (concurrent, many, store, server) = outcomes
    assert batches(server) == batches(serial_server)
    assert concurrent['concept_text_embeddings'].equals(serial['concept_text_embeddings'])
    assert strip(concurrent['report']) == strip(serial['report'])
    assert stored(store, {'k1_text'}) == stored(serial_store, {'k1_text'})
    assert one.peak == 1 and many.peak == CAP
    check_capacity(concurrent['report'], 'embedder', 5, CAP)


@pytest.mark.parametrize('workers', [0, 'pin_required', True])
def test_k1_refuses_an_unusable_cpu_worker_count_before_any_read(project_root, tmp_path, model_test_profile, workers):
    from prompt_enhancer import ConfigurationError

    config = k1_profile(model_test_profile)
    config['runtime']['cpu_workers'] = workers
    store = make_store(project_root, tmp_path / 'kb')
    server = EmbeddingServer()
    with pytest.raises(ConfigurationError, match='runtime.cpu_workers'):
        asyncio.run(embed_images(store, config, {'images': [], 'folder_images': []}, knowledge_root=tmp_path,
                                 transport=server.transport()))
    assert server.requests == []


def test_k1_decode_pool_follows_cpu_workers(project_root, tmp_path, model_test_profile, monkeypatch):
    """Override test: `runtime.cpu_workers` 3 decodes three images at once."""
    import threading

    from prompt_enhancer import k1_embed

    active, peak, lock = [0], [0], threading.Lock()
    arrived = threading.Barrier(3, timeout=5)  # three source images, three configured workers
    original = k1_embed._decode

    def counted(data, settings):  # labelled counting wrapper around the real decode
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            arrived.wait()
            return original(data, settings)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(k1_embed, '_decode', counted)
    config = k1_profile(model_test_profile, batch_size=3)
    config['runtime']['cpu_workers'] = 3
    source = make_source(tmp_path / 'source', {f'{index}.png': image_bytes(color=(index, 2, 3)) for index in range(3)})
    store = make_store(project_root, tmp_path / 'kb')
    asyncio.run(embed_images(store, config, k0_handoff(store, source), knowledge_root=source.resolve(),
                             transport=EmbeddingServer().transport()))
    assert peak[0] == 3


# S1 dense batches (row 7) and S2 second-chance lookups (row 8) ---------------------------------------------------

class SlowEncoder(QueryEncoderDouble):
    """Labelled encoder double with seeded delays that counts batches in flight."""

    def __init__(self, config, seed):
        super().__init__(config)
        self.rng = random.Random(seed)
        self.now = self.peak = 0
        self.gate = ArrivalGate(min(self.settings.backend['max_concurrency'], 8))

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await self.gate.arrive()
            await asyncio.sleep(self.rng.uniform(0.001, 0.02))
            return [[1.0, 0.0, 0.0, 0.0] if index % 2 else [0.0, 1.0, 0.0, 0.0] for index, _ in enumerate(texts)]
        finally:
            self.now -= 1


def test_s1_dense_batches_concurrent_equal_serial(candidate_env):  # noqa: F811
    serial, one = s1_outcome(candidate_env, 1, 21, SlowEncoder)
    concurrent, many = s1_outcome(candidate_env, CAP, 22, SlowEncoder)
    assert serial['state'] == 'ready' and serial['candidates']
    assert concurrent == serial
    assert len(many.calls) == len(one.calls) > CAP * 2
    assert sorted(map(tuple, many.calls)) == sorted(map(tuple, one.calls))  # the same batches, whatever the timing
    assert one.peak == 1 and many.peak == CAP


class FailingEncoder(SlowEncoder):
    """Labelled failure chosen by content: every batch holding a 'word5' window fails, whatever the timing."""

    async def embed_texts(self, texts):
        vectors = await super().embed_texts(texts)
        if any('word5' in text for text in texts):
            raise StorageError('labelled encoder failure for ' + repr(list(texts)))
        return vectors


def s1_outcome(candidate_env, cap, seed, encoder_type):
    prompt = ', '.join(f'word{index}' for index in range(14))  # 14 segments plus the whole prompt: 8 batches of 2
    config = json.loads(json.dumps(candidate_env.intake.payload['effective_config']))
    source_only(config, 'dense')
    config['embedding']['batch_size'] = 2
    config['vlm']['backends'][config['embedding']['backend']]['max_concurrency'] = cap
    intake = replace(candidate_env.intake, payload=dict(candidate_env.intake.payload, prompt=prompt,
                                                        effective_config=config))
    encoder = encoder_type(config, seed)
    try:
        value = asyncio.run(build_candidates(intake, embedder=encoder))
    except StorageError as error:  # the stage's own handling decides what surfaces; both paths must agree
        value = ('raised', str(error))
    return value, encoder


def test_s1_dense_failure_is_the_serial_one(candidate_env):  # noqa: F811
    serial, one = s1_outcome(candidate_env, 1, 23, FailingEncoder)
    concurrent, many = s1_outcome(candidate_env, CAP, 24, FailingEncoder)
    assert serial == ('raised', serial[1]) if isinstance(serial, tuple) else serial['state'] == 'failed'
    assert concurrent == serial
    assert one.peak == 1 and many.peak == CAP


# S2 second chance (row 8): the real planner, its real S1 lookups delayed and counted ---------------------------------

S2_PROMPT = 'Kimono with a Donut, Áo dài and 着物 near a Lamp'
S2_MENTIONS = ('Donut', 'Áo dài', '着物', 'Lamp')


def s2_outcome(kb, monkeypatch, *, serial, seed):
    from planner_fixtures import PlannerClientDouble, intake_view, link, reply

    kb.config['planner']['second_chance_rounds'] = 1
    kb.config['candidates']['shortlist_size'] = 1
    shortlist = asyncio.run(build_candidates(intake_view(kb.config, S2_PROMPT, snapshot=kb.snapshot)))
    real, rng, flight = build_candidates, random.Random(seed), {'now': 0, 'peak': 0, 'order': []}
    gate = ArrivalGate(1 if serial else len(S2_MENTIONS))

    async def delayed(intake_result, **kwargs):  # labelled counting wrapper around the real S1 lookup
        flight['now'] += 1
        flight['peak'] = max(flight['peak'], flight['now'])
        try:
            await gate.arrive()
            await asyncio.sleep(rng.uniform(0.001, 0.02))
            flight['order'].append(intake_result.payload['prompt'])
            if intake_result.payload['prompt'] == 'Lamp':  # labelled operational S1 failure for one mention
                return {'snapshot_id': kb.snapshot.snapshot_id, 'state': 'failed', 'candidates': [], 'warnings': [],
                        'error': {'code': 'WINDOW_BUDGET_EXCEEDED', 'message': 'labelled'}}
            return await real(intake_result, **kwargs)
        finally:
            flight['now'] -= 1

    monkeypatch.setattr(planner_module, 'build_candidates', delayed)
    monkeypatch.setattr(planner_module, 'flow', (lambda items, worker, _: flow(items, worker, 1)) if serial else flow)
    first = reply(links=[link(mention, 'no_match', needs_lookup=True) for mention in S2_MENTIONS])
    client = PlannerClientDouble(first)
    intake = intake_view(kb.config, S2_PROMPT, snapshot=kb.snapshot)
    result = asyncio.run(planner_module.plan(intake, shortlist, client, project_root=kb.root))
    monkeypatch.undo()
    return result, client, flight


def test_s2_second_chance_lookups_concurrent_equal_serial(kb, monkeypatch):
    serial, serial_client, one = s2_outcome(kb, monkeypatch, serial=True, seed=25)
    concurrent, client, many = s2_outcome(kb, monkeypatch, serial=False, seed=26)
    assert sorted(one['order']) == sorted(many['order']) == sorted(S2_MENTIONS)
    assert len(serial_client.calls) == 2 and 'RETRIEVAL_FAILED' in serial['warnings']
    # Independent oracle, the pre-RES-1 serial loop: one lookup per sorted mention, new candidates appended in order.
    from planner_fixtures import intake_view
    first = [entry['name'] for entry in json.loads(serial_client.calls[0]['text'])['shortlist']]
    expected = list(first)
    for mention in sorted(set(S2_MENTIONS) - {'Lamp'}):
        found = asyncio.run(build_candidates(intake_view(kb.config, mention, snapshot=kb.snapshot)))
        expected.extend(entry['name'] for entry in found['candidates'] if entry['name'] not in expected)
    assert len(expected) > len(first) + 1  # more than one added candidate, so their order is observable
    assert [entry['name'] for entry in json.loads(client.calls[1]['text'])['shortlist']] == expected
    # The second planner call sees the added candidates in mention order, exactly as the serial loop built them.
    assert [call['text'] for call in client.calls] == [call['text'] for call in serial_client.calls]
    assert concurrent == serial
    assert one['peak'] == 1 and many['peak'] == len(S2_MENTIONS)
