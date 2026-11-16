"""Additional lifecycle, bounded concurrency and storage/source boundaries with labelled doubles."""

import asyncio
import base64
from copy import deepcopy
from io import BytesIO

from PIL import Image
import pytest

from k2_fixtures import folder, image_bytes
from k2_observe_fixtures import ObserverServer, invoke_observer, k1_result, observation, states
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.k2_observe import observe_images
from prompt_enhancer.model_client import BoundModelClient
from prompt_enhancer.model_resources import BudgetExhausted
from prompt_enhancer.storage import StorageError


@pytest.mark.parametrize('value', ['mixed_revision', 'wrong_dim', 'missing', 'zero', 'unknown_image'])
def test_grouping_requires_current_k1_vectors(k2_env, value):
    folder(k2_env, 'f', 'A')
    inputs = k1_result(k2_env)
    row = inputs['image_embeddings'][0]
    if value == 'mixed_revision':
        row['embedder_revision'] = 'wrong'
    elif value == 'wrong_dim':
        row['dim'] = 3
    elif value == 'zero':
        row['vector'] = [0, 0]
    elif value == 'unknown_image':
        row['image_id'] = 'sha256:' + 'b' * 64
    else:
        inputs['image_embeddings'] = []
        result = invoke_observer(k2_env, inputs=inputs)
        assert not result['rows']['observations'] and result['report']['ineligible_images']
        return
    with pytest.raises((ValueError, StorageError)):
        invoke_observer(k2_env, inputs=inputs)
    assert not states(k2_env)


@pytest.mark.parametrize('change', ['mismatch_root', 'mismatch_store', 'overlap'])
def test_observe_path_refusal_before_source_reads(k2_env, monkeypatch, change):
    import prompt_enhancer.k2_observe as module
    folder(k2_env, 'f', 'A')
    inputs = k1_result(k2_env)
    if change == 'mismatch_root':
        k2_env.config['knowledge']['root'] = 'other'
    elif change == 'mismatch_store':
        k2_env.config['storage']['kb_store'] = 'other-store'
    else:
        k2_env.config['storage']['data_root'] = k2_env.config['knowledge']['root']
    def forbidden(*args, **kwargs):
        pytest.fail('invalid path reached a source read')
    monkeypatch.setattr(module, '_read_verified', forbidden)
    with pytest.raises(ConfigurationError):
        invoke_observer(k2_env, inputs=inputs)


def test_observe_bounded_concurrency_uses_configured_backend_limit(k2_env):
    env = k2_env
    folder(env, 'f', 'A', count=4)
    env.config['observe']['representatives_per_folder'] = 4
    env.config['vlm']['backends']['main']['max_concurrency'] = 2
    active = []
    peak = []
    class YieldingServer(ObserverServer):
        """Labelled yielding HTTP double; no latency/performance measurement."""
        async def handler(self, request):
            active.append(1)
            peak.append(len(active))
            await asyncio.sleep(0)
            response = super().handler(request)
            active.pop()
            return response
    result = invoke_observer(env, YieldingServer())
    assert result['report']['observed'] == 4 and max(peak) == 2 and not active


@pytest.mark.parametrize('outcome', ['success', 'invalid', 'cancelled'])
def test_stage_owned_client_is_closed_on_all_outcomes(k2_env, monkeypatch, outcome):
    import prompt_enhancer.k2_observe as module
    env = k2_env
    folder(env, 'f', 'A')
    inputs = k1_result(env)
    server = ObserverServer(observation() if outcome == 'success' else {'bad': True})
    closed = []
    async def execute():
        run = server.run(env)
        original = run.close
        async def close():
            closed.append(True)
            await original()
        run.close = close
        monkeypatch.setattr(module, 'ModelClientRun', lambda *args, **kwargs: run)
        if outcome == 'cancelled':
            async def cancelled(*args, **kwargs):
                raise asyncio.CancelledError('labelled cancellation')
            monkeypatch.setattr(run, 'for_role', cancelled)
            with pytest.raises(asyncio.CancelledError):
                await observe_images(env.store, env.config, knowledge_root=env.source, **inputs)
        else:
            result = await observe_images(env.store, env.config, knowledge_root=env.source, **inputs)
            assert bool(result['report']['failed']) == (outcome == 'invalid')
    asyncio.run(execute())
    assert closed == [True]


def test_injected_client_remains_caller_owned(k2_env):
    folder(k2_env, 'f', 'A')
    inputs = k1_result(k2_env)
    async def execute():
        run = ObserverServer().run(k2_env)
        try:
            await observe_images(k2_env.store, k2_env.config, knowledge_root=k2_env.source,
                client=BoundModelClient(run, 'observer'), **inputs)
            assert not run.transports['main'].client.is_closed
        finally:
            await run.close()
    asyncio.run(execute())


def test_fatal_batch_error_awaits_cancelled_siblings_before_writer_exit(k2_env):
    env = k2_env
    folder(env, 'f', 'A', count=2)
    env.config['observe']['representatives_per_folder'] = 2
    env.config['vlm']['backends']['main']['max_concurrency'] = 2
    inputs = k1_result(env)
    finished = []
    async def execute():
        started = asyncio.Event()
        blocked = asyncio.Event()
        calls = []
        class BlockingClient:
            """Labelled fatal/cancellation double; no transport, model or sleep deadline inference."""
            async def for_role(self, role):
                return self
            async def call(self, *args, **kwargs):
                index = len(calls)
                calls.append(index)
                try:
                    if index == 0:
                        started.set()
                        await blocked.wait()
                    else:
                        await started.wait()
                        raise StorageError('labelled fatal storage/input boundary')
                finally:
                    finished.append(index)
        with pytest.raises(StorageError, match='labelled fatal'):
            await asyncio.wait_for(observe_images(env.store, env.config, knowledge_root=env.source,
                client=BlockingClient(), **inputs), timeout=2)
        assert sorted(finished) == [0, 1]
        with env.store.writer():
            pass
    asyncio.run(execute())


def test_actual_fallback_records_actual_model_provenance(k2_env):
    env = k2_env
    folder(env, 'f', 'A')
    env.config['vlm']['roles']['observer']['backend'] = 'alternate'
    alternate = deepcopy(env.config['vlm']['backends']['main'])
    alternate['on_budget_exhausted'] = 'fallback_to_default_backend'
    alternate['model']['revision'] = 'primary-not-used'
    env.config['vlm']['backends']['alternate'] = alternate
    inputs = k1_result(env)
    async def execute():
        run = ObserverServer().run(env)
        run.client._probe_remaining['main'] = 0  # labelled already-started fallback proof, no fresh allowance
        class ExhaustedTransport:
            """Labelled primary budget refusal, no HTTP."""
            async def post(self, *args, **kwargs):
                raise BudgetExhausted('alternate')
            async def close(self):
                pass
        await run.transports['alternate'].close()
        run.transports['alternate'] = ExhaustedTransport()
        try:
            return await observe_images(env.store, env.config, knowledge_root=env.source,
                client=BoundModelClient(run, 'observer'), **inputs)
        finally:
            await run.close()
    result = asyncio.run(execute())
    assert result['rows']['observations'][0]['model_revision'] == 'test'
    assert states(env)[0]['call_records'][0]['backend'] == 'main'


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
def test_completed_rest_stays_done_with_both_storage_formats(k2_env, fmt):
    env = k2_env
    folder(env, 'f', 'A', count=3)
    env.store._table_format = env.config['storage']['table_format'] = fmt
    env.config['observe']['representatives_per_folder'] = 3
    server = ObserverServer()
    first = invoke_observer(env, server)
    env.config['observe']['representatives_per_folder'] = 1
    second = invoke_observer(env, server)
    assert second['rows'] == first['rows'] and len(server.requests) == 3
    assert not second['report']['queued'] and all(row['state'] == 'done' for row in states(env))


@pytest.mark.parametrize('inherited', [False, True])
def test_image_format_side_and_alpha_overrides_reach_body(k2_env, inherited):
    env = k2_env
    folder(env, 'f', 'A', payload=image_bytes(mode='RGBA', color=(0, 0, 0, 0)))
    env.config['vision'].update(format='png', max_image_side=8, alpha_policy={'mode': 'flatten', 'background': '#123456'})
    env.config['vlm']['roles']['observer']['image'].update(
        format='from_vision' if inherited else 'png', max_side='from_vision' if inherited else 16)
    server = ObserverServer()
    result = invoke_observer(env, server)
    assert not result['report']['failed']
    url = server.requests[0]['messages'][1]['content'][1]['image_url']['url']
    assert url.startswith('data:image/png;')
    with Image.open(BytesIO(base64.b64decode(url.split(',')[1]))) as image:
        assert image.size == ((8, 6) if inherited else (12, 9))
        assert image.getpixel((0, 0)) == (18, 52, 86)


def test_referenced_completed_bundle_reused_with_resume_disabled(k2_env):
    from storage_fixtures import dataset
    from prompt_enhancer.storage import SPECS
    env = k2_env
    folder(env, 'f', 'A')
    server = ObserverServer()
    result = invoke_observer(env, server)
    with env.store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, rows in dataset().items():
            builder.write_table(name, rows, dim=4 if SPECS[name].vector else None)
        builder.reference_artifacts(result['artifacts'])
        sid = builder.finish()
        writer.publish(sid)
    env.store._resume = False
    replay = invoke_observer(env, server)
    assert replay['rows'] == result['rows'] and len(server.requests) == 1
    assert len(replay['report']['reused_referenced']) == 1
