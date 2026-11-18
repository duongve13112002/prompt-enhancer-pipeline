"""Coordinator failure/cancellation/golden boundaries with explicitly labelled stage doubles."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from prompt_enhancer.intake import intake
from prompt_enhancer.pipeline import GoldenAdapter, Pipeline
from prompt_enhancer.profiles import resolve_profile
from intake_fixtures import activate, configuration
from test_pipeline_config import runtime
from validation_fixtures import environment
from validation_fixtures import calibrate, draft


@pytest.mark.parametrize('case', ['binding', 'preflight', 'pin_close', 'late_clarification', 'late_monitor',
                                  'late_owner_close'])
def test_pipeline_review_boundaries(model_test_profile, tmp_path, project_root, monkeypatch, case):
    import prompt_enhancer.pipeline as module
    from prompt_enhancer.config import ConfigurationError
    from prompt_enhancer.model_client import ModelCallError
    from prompt_enhancer.storage import StorageError

    env = environment(model_test_profile, tmp_path, project_root)
    env.config['validation']['fallbacks'] = ['user_prompt_with_status_degraded']
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    env.writer.replies = ['kept']
    now = [0]
    closed, probed, rows = [], [], []
    env.s0.payload.update(profile_id='p1', config_hash='test', error=None)

    def release():
        if case == 'pin_close':
            raise StorageError('labelled pin lock timeout')

    env.s0.close = release
    if case == 'preflight':
        env.config['writer']['runtime_addendum'] = False
        env.s0.profile.addendum = {'id': 'test', 'text': b'test'}

    class OwnerDouble:
        async def for_role(self, role):
            probed.append(role)
            if case == 'binding':
                raise ModelCallError('PROBE_INCOMPLETE: probe allowance exhausted')
            return env.writer

        async def close(self):
            closed.append(True)
            if case == 'late_owner_close':
                now[0] = 61

    async def compose(*args, **kwargs):
        return OwnerDouble()

    async def plan(*args, **kwargs):
        if case == 'late_clarification':
            now[0] = 61
            return dict(env.s2, state='needs_clarification', intent_ir=None)
        return env.s2

    async def checked(*args, **kwargs):
        if case == 'late_monitor':
            now[0] = 61
        return {'status': 'success', 'output': 'kept', 'warnings': [], 'monitoring_selected': True}

    async def monitor(*args, **kwargs):
        pytest.fail('discarded draft reached monitoring')

    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module, 'intake', lambda *a, **k: env.s0)
    monkeypatch.setattr(module, 'compose_model_client', compose)
    monkeypatch.setattr(module, 'plan', plan)
    monkeypatch.setattr(module, 'monitor_output', monitor)
    if case == 'late_monitor':
        monkeypatch.setattr(module, 'validate', checked)
    env.config['clarification']['mode'] = 'ask' if case == 'late_clarification' else 'best_effort'

    async def run():
        async with Pipeline(env.config, project_root=project_root) as pipeline:
            pipeline.lifecycle.sink = rows.append
            if case == 'preflight':
                with pytest.raises(ConfigurationError, match='required addendum'):
                    await pipeline.enhance({'task': 'generate', 'prompt': 'original'})
                assert probed == []
            else:
                response = await pipeline.enhance({'task': 'generate', 'prompt': 'original'})
                await response.after_delivery()
                expected = {'binding': 'degraded', 'pin_close': 'success',
                            'late_clarification': 'failed', 'late_monitor': 'failed', 'late_owner_close': 'failed'}[case]
                assert response.envelope['status'] == expected
        assert closed == [True] and pipeline.lifecycle._used == 0
        if case == 'pin_close':
            assert 'RESOURCE_CLOSE_FAILED' in rows[0]['warnings']
        if case.startswith('late'):
            assert 'REQUEST_DEADLINE' in rows[0]['warnings']
            assert 'monitoring' not in rows[0]['body']
    asyncio.run(run())


@pytest.mark.parametrize('change_id', [False, True])
def test_activation_refuses_writer_brief_drift(model_test_profile, tmp_path, project_root, change_id):
    from prompt_enhancer.profile_prompt import ProfileError
    config = configuration(model_test_profile, tmp_path)
    activate(config, project_root)
    brief = project_root / 'prompts/brief_v0.txt'
    if change_id:
        config['vlm']['roles']['writer']['prompt_id'] = 'brief_other'
        brief.with_name('brief_other.txt').write_bytes(brief.read_bytes())
    else:
        brief.write_bytes(b'Changed\n{{data}}')
    with pytest.raises(ProfileError, match='brief|writer prompt'):
        resolve_profile(config, project_root=project_root)


def test_golden_refuses_drift_but_uses_and_traces_snapshotted_brief(model_test_profile, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.pipeline as module
    from profile_fixtures import write_golden
    from prompt_enhancer.profile_prompt import ProfileError, sha256_tag
    from prompt_enhancer.profiles import check_profile
    from prompt_enhancer.storage import open_data_store

    env = environment(model_test_profile, tmp_path, project_root)
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    folder = activate(env.config, project_root)
    write_golden(folder, 'g1', ['c1'], expect=('degraded',))
    path = project_root / 'prompts/brief_v0.txt'
    path.write_bytes(b'Checked brief\n{{data}}')
    env.writer.replies = ['kept']

    class OwnerDouble:
        async def for_role(self, role):
            return env.writer

        async def close(self):
            pass

    async def compose(*args, **kwargs):
        return OwnerDouble()

    async def planned(*args, **kwargs):
        path.write_bytes(b'Unchecked brief\n{{data}}')
        return env.s2

    monkeypatch.setattr(module, 'compose_model_client', compose)
    monkeypatch.setattr(module, 'plan', planned)

    async def run():
        async with Pipeline(env.config, project_root=project_root) as pipeline:
            with pytest.raises(ProfileError, match='changed during the check') as error:
                await check_profile(env.config, 'p1', GoldenAdapter(pipeline), project_root=project_root)
            assert error.value.code == 'PROFILE_CHANGED'
    asyncio.run(run())
    assert not (folder / 'activation.json').exists()
    row, = open_data_store(env.config, 'enhance once', project_root=project_root).read_rows('request_traces')
    executed = env.writer.calls[0][1]
    assert 'Checked brief\n' in executed and row['body']['brief'] == executed
    assert row['body']['writer']['brief_hash'] == sha256_tag(executed.encode('utf-8'))


@pytest.mark.parametrize('deadline,expected', [(.01, 'failed'), (3, 'success')])
def test_request_deadline_override_cancels_owned_work(model_test_profile, tmp_path, project_root, monkeypatch, deadline, expected):
    import prompt_enhancer.pipeline as module
    env = environment(model_test_profile, tmp_path, project_root)
    config = env.config
    config['runtime'].update(runtime())
    config['runtime']['request_deadline_s'] = deadline
    config['storage']['data_root'] = str(tmp_path / 'data')
    env.s0.payload.update(profile_id='p1', config_hash='test', error=None)
    env.s0.close = lambda: None
    env.writer.replies = ['kept']
    closed = []
    class OwnerDouble:
        async def for_role(self, role):
            return env.writer
        async def close(self):
            closed.append('owner')
    async def compose(*args, **kwargs):
        try:
            await asyncio.sleep(.04)
            return OwnerDouble()
        finally:
            closed.append('compose')
    async def planned(*args, **kwargs):
        return env.s2
    monkeypatch.setattr(module, 'intake', lambda *a, **k: env.s0)
    monkeypatch.setattr(module, 'compose_model_client', compose)
    monkeypatch.setattr(module, 'plan', planned)
    async def run():
        async with Pipeline(config, project_root=project_root) as pipeline:
            response = await pipeline.enhance({'task': 'generate', 'prompt': 'original prompt'})
            assert response.envelope['status'] == expected
            assert 'compose' in closed
        assert pipeline.lifecycle._used == 0
    asyncio.run(run())


def test_cancellation_releases_capacity(model_test_profile, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.pipeline as module
    config = configuration(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    config['storage']['data_root'] = str(tmp_path / 'data')
    activate(config, project_root)
    entered, stopped = None, []
    async def compose(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)
    monkeypatch.setattr(module, 'compose_model_client', compose)
    async def run():
        nonlocal entered
        entered = asyncio.Event()
        async with Pipeline(config, project_root=project_root) as pipeline:
            task = asyncio.create_task(pipeline.enhance({'task': 'generate', 'prompt': 'original'}))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pipeline.lifecycle._used == 0 and stopped == [True]
    asyncio.run(run())


def test_pipeline_close_joins_active_request_teardown(model_test_profile, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.pipeline as module
    from prompt_enhancer.response_lifecycle import LifecycleFull
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    env.s0.payload.update(profile_id='p1', config_hash='test', error=None)
    closed = []

    class OwnerDouble:
        async def close(self):
            closed.append('owner')

    async def compose(*args, **kwargs):
        return OwnerDouble()

    async def run():
        entered, releasing, finish_release = (asyncio.Event() for _ in range(3))

        async def planned(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def release():
            releasing.set()
            await finish_release.wait()
            closed.append('pin')

        env.s0.close = release
        monkeypatch.setattr(module, 'intake', lambda *a, **k: env.s0)
        monkeypatch.setattr(module, 'compose_model_client', compose)
        monkeypatch.setattr(module, 'plan', planned)
        pipeline = Pipeline(env.config, project_root=project_root)
        request = {'task': 'generate', 'prompt': 'original'}
        active = asyncio.create_task(pipeline.enhance(request))
        await entered.wait()
        active.cancel()
        await releasing.wait()
        close = asyncio.create_task(pipeline.close())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(close), .05)
            with pytest.raises(LifecycleFull, match='closing'):
                await pipeline.enhance(request)
        finally:
            finish_release.set()
            assert isinstance((await asyncio.gather(active, return_exceptions=True))[0], asyncio.CancelledError)
            await close
        assert closed == ['pin', 'owner'] and pipeline.lifecycle._used == 0
    asyncio.run(run())


def test_golden_intake_uses_confirmed_view_before_activation(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    activate(config, project_root)
    profile = replace(resolve_profile(config, project_root=project_root), activation=None)
    result = intake({'task': 'generate', 'prompt': 'golden', 'profile_id': 'p1'}, config,
                    project_root=project_root, profile_view=profile)
    assert result.payload['outcome'] == 'ready' and result.profile is profile
    result.close()


def test_golden_adapter_passes_view_and_acknowledges_delivery():
    events = []
    profile = SimpleNamespace(profile_id='p1')
    class ResponseDouble:
        envelope = {'status': 'success'}
        async def after_delivery(self):
            events.append('delivered')
    class PipelineDouble:
        async def _enhance(self, request, view):
            assert view is profile and request['source_image'] == 'aW1hZ2U='
            return ResponseDouble()
    assert asyncio.run(GoldenAdapter(PipelineDouble()).run(profile, {'source_image': b'image'})) == {'status': 'success'}
    assert events == ['delivered']


def test_monitor_uses_one_independent_client_with_no_repairs(model_test_profile, tmp_path, project_root):
    from prompt_enhancer.pipeline import monitor_output
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    roles = []
    class OwnerDouble:
        async def for_role(self, role):
            roles.append(role)
            return env.verifier
    checked = {'output': 'kept', 'calls': draft(env)['calls']}
    result = asyncio.run(monitor_output(env.s0, env.s2, None, checked, OwnerDouble(), project_root=project_root))
    assert roles == ['verifier'] and env.events == ['verifier'] and result['status'] == 'checked'
    assert len(result['calls']) == 1 and not env.writer.calls


@pytest.mark.parametrize('retained', [False, True])
def test_trace_build_failure_releases_deferred_client_and_capacity(model_test_profile, tmp_path, project_root, monkeypatch, retained):
    import prompt_enhancer.pipeline as module
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['runtime'].update(runtime())
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    closed, released = [], []
    env.s0.payload['retrieval']['snapshot_id'] = 's1'
    env.s0.knowledge = SimpleNamespace(store=SimpleNamespace(release_hold=lambda *args: released.append(args)))
    env.s0.close = lambda: setattr(env.s0, 'knowledge', None)
    class OwnerDouble:
        async def for_role(self, role):
            return env.writer
        async def close(self):
            closed.append(True)
    async def compose(*args, **kwargs):
        return OwnerDouble()
    async def planned(*args, **kwargs):
        return env.s2
    async def written(*args, **kwargs):
        return draft(env)
    async def checked(*args, **kwargs):
        return {'status': 'success', 'output': 'kept', 'warnings': [], 'monitoring_selected': True}
    def fail_trace(*args, **kwargs):
        raise ValueError('labelled trace-builder failure')
    for name, value in [('intake', lambda *a, **k: env.s0), ('compose_model_client', compose),
                        ('plan', planned), ('write', written), ('validate', checked), ('build_trace_row', fail_trace)]:
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, 'retain_attachments', lambda *args: {'i1': b'image'} if retained else {})
    async def run():
        async with Pipeline(env.config, project_root=project_root) as pipeline:
            with pytest.raises(ValueError, match='labelled trace-builder failure'):
                await pipeline.enhance({'task': 'generate', 'prompt': 'original'})
            assert closed == [True] and pipeline.lifecycle._used == 0
            assert len(released) == int(retained)
    asyncio.run(run())
