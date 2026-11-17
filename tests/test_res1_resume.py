"""RES-1 interrupted-run differential properties, synthetic CPU doubles only."""
import asyncio
import json
from itertools import count
from random import Random
import threading

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from k1_fixtures import EmbeddingServer, image_bytes, k0_handoff, k1_profile, make_source
from k2_fixtures import NamingServer, environment, folder, invoke, scan
from storage_fixtures import make_store
from test_res1_review_probes import BatchSensitiveServer
from test_stage_flow import stored
from prompt_enhancer import k1_embed
from prompt_enhancer.storage import StorageError, concept_id
from prompt_enhancer.storage.store import Checkpoints


def test_k1_repeated_stop_joins_workers_and_cancels_queued_reads(project_root, tmp_path, model_test_profile, monkeypatch):
    source = make_source(tmp_path / 'source', {f'{i}.png': image_bytes(color=(i, 2, 3)) for i in range(8)})
    store = make_store(project_root, tmp_path / 'store')
    handoff = k0_handoff(store, source)
    config = k1_profile(model_test_profile, batch_size=8)
    config['runtime']['cpu_workers'] = 1
    entered, release = threading.Event(), threading.Event()
    calls, completed = [], []
    original = k1_embed._decode

    def held(data, settings):
        calls.append(threading.current_thread())
        entered.set()
        assert release.wait(3), 'labelled native decode timeout'
        try:
            return original(data, settings)
        finally:
            completed.append(True)

    monkeypatch.setattr(k1_embed, '_decode', held)

    async def exercise():
        task = asyncio.create_task(k1_embed.embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                                         transport=BatchSensitiveServer().transport()))
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                for _ in range(3):
                    await asyncio.sleep(.01)
                    task.cancel()
                    assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(exercise())
    assert completed == [True] and len(calls) == 1
    assert not calls[0].is_alive()


PROPERTY = settings(max_examples=4, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
CASES = count()


def semantic(store, stages):
    """Latest logical state and records; physical event history is excluded by the PM boundary."""
    return stored(store, stages)


@PROPERTY
@given(batch=st.integers(1, 3), cap=st.integers(1, 3), workers=st.sampled_from([1, 3]))
@pytest.mark.parametrize('kind', ['images', 'texts'])
def test_k1_every_checkpoint_boundary(project_root, tmp_path, model_test_profile, monkeypatch, kind, batch, cap, workers):
    case = tmp_path / f'{kind}-{batch}-{cap}-{workers}-{next(CASES)}'
    case.mkdir(exist_ok=True)
    config = k1_profile(model_test_profile, batch_size=batch)
    config['runtime']['cpu_workers'] = workers
    config['vlm']['backends']['embedder']['max_concurrency'] = cap
    stage = k1_embed.IMAGE_STAGE if kind == 'images' else k1_embed.TEXT_STAGE
    source = make_source(case / 'source', {**{f'{i}.png': image_bytes(color=(i * 35, 2, 4)) for i in range(4)},
                                         'bad.png': b'labelled undecodable'})
    clean_store = make_store(project_root, case / 'clean')
    handoff = k0_handoff(clean_store, source)
    changed = sorted(handoff['images'], key=lambda r: r['image_id'])[2]['image_id']
    for row in handoff['folder_images']:
        if row['image_id'] == changed:
            (source / row['folder_id'] / row['relative_path']).write_bytes(b'labelled changed source')
    texts = [k1_embed.ConceptText(concept_id(str(i)), 'card_summary', f'text {i}') for i in range(5)]

    def run(store, server):
        if kind == 'images':
            return asyncio.run(k1_embed.embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                                    transport=server.transport()))
        return asyncio.run(k1_embed.embed_concept_texts(store, config, texts, transport=server.transport()))

    commits, original_commit, original_fail = [], Checkpoints.commit, Checkpoints.fail

    def capture(self, *args, **kwargs):
        value = original_commit(self, *args, **kwargs)
        if self.stage == stage:
            commits.append(('commit', args[0]))
        return value

    def capture_fail(self, *args, **kwargs):
        value = original_fail(self, *args, **kwargs)
        if self.stage == stage:
            commits.append(('fail', args[0]))
        return value

    clean_server = BatchSensitiveServer()
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', capture)
        patch.setattr(Checkpoints, 'fail', capture_fail)
        clean = run(clean_store, clean_server)
    expected_state = semantic(clean_store, {stage})
    expected_batches = {json.dumps(body, sort_keys=True) for _, body in clean_server.requests}
    # Inject a fatal interruption after EVERY durable item checkpoint, including deterministic refusals/failures.
    for boundary in range(1, len(commits) + 1):
        stopped = make_store(project_root, case / f'stop-{boundary}')
        seen = []

        def wrap(original):
            def interrupt(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                if self.stage == stage:
                    seen.append(args[0])
                    if len(seen) == boundary:
                        raise StorageError('labelled checkpoint interruption')
                return result
            return interrupt

        with monkeypatch.context() as patch:
            patch.setattr(Checkpoints, 'commit', wrap(original_commit))
            patch.setattr(Checkpoints, 'fail', wrap(original_fail))
            with pytest.raises(StorageError, match='labelled checkpoint'):
                run(stopped, BatchSensitiveServer())
        server = BatchSensitiveServer()
        resumed = run(stopped, server)
        table = 'image_embeddings' if kind == 'images' else 'concept_text_embeddings'
        assert resumed[table].equals(clean[table])
        if kind == 'images':
            assert resumed['images'] == clean['images']
            assert resumed['report']['decode_failed'] == clean['report']['decode_failed']
        assert list(resumed['report']['failed'].items()) == list(clean['report']['failed'].items())
        assert semantic(stopped, {stage}) == expected_state
        assert all(json.dumps(body, sort_keys=True) in expected_batches for _, body in server.requests)
        # Once fully checkpointed, no embedding request is repeated.
        again = BatchSensitiveServer()
        rerun = run(stopped, again)
        assert rerun[table].equals(clean[table]) and not again.requests


@PROPERTY
@given(batch=st.integers(2, 4), boundary_choice=st.integers(0, 2),
       cap=st.integers(1, 3), workers=st.sampled_from([1, 3]))
@pytest.mark.parametrize('damage', ['changed', 'undecodable', 'decode_refusal'])
def test_k1_interrupted_done_companion_is_immutable(project_root, tmp_path, model_test_profile, monkeypatch,
                                                   damage, batch, boundary_choice, cap, workers):
    """Changed source bytes and a labelled decoder refusal cannot alter an already committed companion."""
    case = tmp_path / f'companion-{damage}-{next(CASES)}'
    source = make_source(case / 'source', {f'{i}.png': image_bytes(color=(i * 35, 2, 4)) for i in range(batch)})
    store = make_store(project_root, case / 'store')
    handoff = k0_handoff(store, source)
    config = k1_profile(model_test_profile, batch_size=batch)
    config['runtime']['cpu_workers'] = workers
    config['vlm']['backends']['embedder']['max_concurrency'] = cap
    stage = k1_embed.IMAGE_STAGE
    boundary = 1 + boundary_choice % (batch - 1)
    original_commit, original_fail, original_decode = Checkpoints.commit, Checkpoints.fail, k1_embed._decode
    committed = []

    def run(server):
        return asyncio.run(k1_embed.embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                                transport=server.transport()))

    def interrupt(self, *args, **kwargs):
        result = original_commit(self, *args, **kwargs)
        if self.stage == stage:
            committed.append(args[0])
            if len(committed) == boundary:
                raise StorageError('labelled checkpoint interruption')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', interrupt)
        with pytest.raises(StorageError, match='labelled checkpoint'):
            run(BatchSensitiveServer())

    settings_ = k1_embed.embedder_settings(config)
    done_ids = {row['image_id'] for row in handoff['images']
                if k1_embed.image_key(settings_, row['image_id']) in committed}
    with store.writer() as writer:
        checkpoints = writer.checkpoints(stage)
        before = {key: (dict(checkpoints.done(key)), checkpoints.load(key)) for key in committed}
    companion = sorted(done_ids)[boundary_choice % len(done_ids)]
    location = next(row for row in handoff['folder_images'] if row['image_id'] == companion)
    path = source / location['folder_id'] / location['relative_path']
    original_bytes = path.read_bytes()
    if damage == 'changed':
        path.write_bytes(image_bytes(color=(255, 1, 2)))
    elif damage == 'undecodable':
        path.write_bytes(b'labelled corrupt source')

    def decode(data, settings):
        # Same verified bytes, but this decoder double now refuses the done companion.
        return original_decode(b'labelled undecodable' if data == original_bytes else data, settings)

    writes = []

    def track(original):
        def wrapped(self, *args, **kwargs):
            if self.stage == stage:
                writes.append(args[0])
            return original(self, *args, **kwargs)
        return wrapped

    server = BatchSensitiveServer()
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', track(original_commit))
        patch.setattr(Checkpoints, 'fail', track(original_fail))
        if damage == 'decode_refusal':
            patch.setattr(k1_embed, '_decode', decode)
        resumed = run(server)
    with store.writer() as writer:
        checkpoints = writer.checkpoints(stage)
        after = {key: (dict(checkpoints.done(key)), checkpoints.load(key)) for key in committed}
    assert after == before
    assert not set(writes).intersection(committed), 'done keys must receive neither commit nor fail calls'
    assert resumed['report']['failed'] == {} and resumed['report']['decode_failed'] == {}
    assert resumed['report']['embedded'] == batch - boundary
    assert resumed['report']['skipped_done'] == boundary
    assert server.input_counts() == [batch - 1]
    rows = {row['image_id']: row for row in resumed['image_embeddings'].to_pylist()}
    assert set(rows) == {row['image_id'] for row in handoff['images']}
    for image_id in done_ids:
        record = json.loads(before[k1_embed.image_key(settings_, image_id)][1])
        assert rows[image_id]['vector'] == k1_embed.unpack_vector(k1_embed.base64.b64decode(record['vector']))


@PROPERTY
@given(batch=st.integers(2, 5), boundary_choice=st.integers(0, 3), cap=st.integers(1, 3))
@pytest.mark.parametrize('status', [200, 500])
def test_k1_interrupted_done_text_is_immutable(project_root, tmp_path, model_test_profile, monkeypatch,
                                            status, batch, boundary_choice, cap):
    """Physical done records and invocation accounting survive both success and failure of a text replay."""
    case = tmp_path / f'text-companion-{status}-{next(CASES)}'
    store = make_store(project_root, case / 'store')
    config = k1_profile(model_test_profile, batch_size=batch)
    config['vlm']['backends']['embedder']['max_concurrency'] = cap
    items = [k1_embed.ConceptText(concept_id(str(i)), 'name', f'text {i}') for i in range(batch)]
    boundary = 1 + boundary_choice % (batch - 1)
    stage = k1_embed.TEXT_STAGE
    committed, writes = [], []
    original_commit, original_fail = Checkpoints.commit, Checkpoints.fail

    def run(server):
        return asyncio.run(k1_embed.embed_concept_texts(store, config, items, transport=server.transport()))

    def interrupt(self, *args, **kwargs):
        result = original_commit(self, *args, **kwargs)
        if self.stage == stage:
            committed.append(args[0])
            if len(committed) == boundary:
                raise StorageError('labelled text checkpoint interruption')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', interrupt)
        with pytest.raises(StorageError, match='labelled text checkpoint'):
            run(BatchSensitiveServer())
    with store.writer() as writer:
        checkpoints = writer.checkpoints(stage)
        before = {key: (dict(checkpoints.done(key)), checkpoints.load(key)) for key in committed}

    def track(original):
        def wrapped(self, *args, **kwargs):
            if self.stage == stage:
                writes.append(args[0])
            return original(self, *args, **kwargs)
        return wrapped

    server = BatchSensitiveServer() if status == 200 else EmbeddingServer(status=status)
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', track(original_commit))
        patch.setattr(Checkpoints, 'fail', track(original_fail))
        resumed = run(server)
    with store.writer() as writer:
        checkpoints = writer.checkpoints(stage)
        after = {key: (dict(checkpoints.done(key)), checkpoints.load(key)) for key in committed}
    assert not set(writes).intersection(committed), 'done texts must receive neither commit nor fail calls'
    assert after == before
    assert len(writes) == batch - boundary
    assert resumed['report']['embedded'] == (batch - boundary if status == 200 else 0)
    assert resumed['report']['skipped_done'] == boundary
    assert len(resumed['report']['failed']) == (0 if status == 200 else batch - boundary)
    assert resumed['concept_text_embeddings'].num_rows == (batch if status == 200 else boundary)
    assert server.input_counts() == [batch]  # done companions still supply batch context


@PROPERTY
@given(seed=st.integers(0, 1000), cap=st.integers(1, 3), sample=st.integers(1, 3))
def test_k2_seeded_resume_at_every_checkpoint(project_root, tmp_path, model_test_profile, monkeypatch, seed, cap, sample):
    case = tmp_path / f'{seed}-{cap}-{sample}-{next(CASES)}'
    case.mkdir(exist_ok=True)

    def setup(name):
        base = case / name
        base.mkdir(exist_ok=True)
        env = environment(project_root, base, model_test_profile)
        env.config['vlm']['backends']['main']['max_concurrency'] = cap
        env.config['knowledge']['naming']['sample_size'] = sample
        for i in range(3):
            folder(env, f'folder-{i}', count=5)
        return env, scan(env)

    env, scanned = setup('clean')
    original, commits = Checkpoints.commit, []
    stages = {'k2_draw', 'k2_draw_head', 'k2_redraw', 'k2_naming'}

    def capture(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.stage in stages:
            commits.append((self.stage, args[0]))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', capture)
        clean = invoke(env, NamingServer(), result=scanned, rng=Random(seed))
    expected = semantic(env.store, stages)
    for boundary in range(1, len(commits) + 1):
        stopped, inputs = setup(f'stop-{boundary}')
        seen = []

        def interrupt(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            if self.stage in stages:
                seen.append(args[0])
                if len(seen) == boundary:
                    raise StorageError('labelled checkpoint interruption')
            return result

        with monkeypatch.context() as patch:
            patch.setattr(Checkpoints, 'commit', interrupt)
            with pytest.raises(StorageError, match='labelled checkpoint'):
                invoke(stopped, NamingServer(), result=inputs, rng=Random(seed))
        before = semantic(stopped.store, {'k2_naming'})
        server = NamingServer()
        resumed = invoke(stopped, server, result=inputs, rng=Random(seed))
        assert resumed['rows'] == clean['rows']
        assert resumed['report']['model_named_folders'] == clean['report']['model_named_folders']
        assert resumed['report']['redraws'] == clean['report']['redraws']
        assert semantic(stopped.store, stages) == expected
        assert len(server.requests) == 3 - len(before)
