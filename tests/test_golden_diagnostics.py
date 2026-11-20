"""T017-FIX F3: a golden case keeps why it failed, as codes only (R-026).

Labelled CPU replay: the production `Pipeline`, `GoldenAdapter`, planner, S4-S6 and `check_profile` run; the model
owner is a labelled double whose planner call fails like J4a2 (a `PROBE_MALFORMED` model-call error with probe events)
or answers with a synthetic reply. Nothing is copied from `runs/`. A unique marker sits in the admin system prompt,
the golden prompts, the planner reply, the writer reply, the exception message and unrecognised probe fields; it must
reach neither `golden_results.json` nor the diagnostics.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from planner_fixtures import entity, reply
from profile_fixtures import (SECTIONED, DrafterDouble, RunnerDouble, bundle, draft, install_prompts, set_manifest,
                              write_golden)
from prompt_enhancer.model_client import ModelCallError
from prompt_enhancer.pipeline import GoldenAdapter, Pipeline, case_diagnostics
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.profiles import check_profile, confirm_profile, register_profile
from validation_fixtures import environment

MARKER = 'ZQX-T017-MARKER-41c9'  # unique synthetic text


def marked_bundle(env, project_root):
    """Register and confirm p1 with an admin system prompt that holds the marker (test-only text)."""
    install_prompts(project_root)
    prompt = SECTIONED.replace(b'Keep caf', MARKER.encode() + b'. Keep caf')
    assert MARKER.encode() in prompt
    folder = bundle(Path(env.config['profiles']['root']).parent, 'p1', prompt)
    value = draft(profile_id='p1', supported_tasks=['generate', 'edit'], edit_output_semantics='whole_edited_image')
    asyncio.run(register_profile(env.config, 'p1', DrafterDouble(value), project_root=project_root))
    confirm_profile(env.config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    write_golden(folder, 'g1', ['c1'], prompt=f'case-one {MARKER} sign', expect=('success',))
    write_golden(folder, 'g2', ['c1'], prompt=f'case-two {MARKER} sign', expect=('failed',))
    set_manifest(folder, golden_tests=['g1', 'g2'])
    return folder


PROBE_EVENT = {'role': 'planner', 'capability': 'image', 'status': 'failed', 'sent': 2, 'reason': 'PROBE_MALFORMED',
               'detail': MARKER, 'challenges': [{'challenge': '0', 'status': 'failed', 'reason': 'PROBE_MALFORMED',
                                                 'text': MARKER}]}


class OwnerDouble:
    """Labelled model-owner double: planner-bound, scripted by the golden prompt it receives."""
    role_name = 'planner'

    def __init__(self, writer):
        self.writer = writer
        self.run = SimpleNamespace(client=SimpleNamespace(probe_events=[dict(PROBE_EVENT)]))

    async def call(self, system, user, **kwargs):
        if 'case-one' in json.loads(user)['prompt']:
            error = ModelCallError(f'PROBE_MALFORMED: the reply said {MARKER}')
            error.probe_evidence = [dict(PROBE_EVENT)]
            raise error
        return reply(entities=[entity('e1', MARKER)], unresolved=[f'note {MARKER}']), {
            'role': 'planner', 'status': 'OK', 'request_hash': 'test-only', 'image_roles': [], 'warnings': []}

    async def for_role(self, role):
        return self.writer

    async def close(self):
        pass


@pytest.fixture
def golden(model_test_profile, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.pipeline as module

    env = environment(model_test_profile, tmp_path, project_root)
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    folder = marked_bundle(env, project_root)
    env.writer.replies = [f'caption {MARKER}'] * 4

    async def compose(*args, **kwargs):
        return OwnerDouble(env.writer)

    monkeypatch.setattr(module, 'compose_model_client', compose)

    async def run():
        async with Pipeline(env.config, project_root=project_root) as pipeline:
            with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
                await check_profile(env.config, 'p1', GoldenAdapter(pipeline), project_root=project_root)
    asyncio.run(run())
    raw = (folder / 'golden_results.json').read_bytes()
    return SimpleNamespace(raw=raw, results=json.loads(raw), env=env)


def test_real_golden_adapter_writes_the_planner_cause_and_probe_reasons(golden):
    """Replay F3: the J4a2 shape (planner PROBE_MALFORMED) is visible in golden_results.json."""
    assert golden.results['runner'] == 's0-s6.v4'
    first, second = golden.results['cases']
    assert first['id'] == 'g1' and first['diagnostics'] is not None
    planner = first['diagnostics']['planner']
    assert planner['state'] == 'failed' and planner['code'] == 'PLANNER_CALL_FAILED'
    assert planner['cause'] == 'PROBE_MALFORMED'
    assert first['diagnostics']['writer']['code'] == 'INTENT_UNAVAILABLE'
    events = first['diagnostics']['probe_events']
    assert events and all(event['reason'] == 'PROBE_MALFORMED' for event in events)
    assert events[0]['challenges'] == [{'challenge': '0', 'status': 'failed', 'reason': 'PROBE_MALFORMED'}]
    assert isinstance(first['warnings'], list) and first['passed'] is False
    assert second['diagnostics']['planner']['state'] == 'ready' and second['diagnostics']['planner']['cause'] is None
    assert second['diagnostics']['writer']['state'] == 'draft'


def test_r026_marker_reaches_neither_golden_results_nor_diagnostics(golden):
    """R-026: admin, prompt, planner-reply, writer-reply, exception and probe text never enter the report."""
    assert MARKER.encode() not in golden.raw
    for case in golden.results['cases']:
        assert MARKER not in json.dumps(case['diagnostics'], ensure_ascii=False)
    assert len(golden.env.writer.calls) >= 1 and MARKER in golden.env.writer.calls[0][1]  # the marker did flow


@pytest.mark.parametrize('fallbacks, status, fallback', [
    (['failed'], 'failed', 'failed'),
    (['user_prompt_with_status_degraded'], 'degraded', 'user_prompt_with_status_degraded'),
])
def test_planner_failure_skips_s4_and_ends_by_the_profile_fallbacks(model_test_profile, tmp_path, project_root,
                                                                   monkeypatch, fallbacks, status, fallback):
    """Decision 192, `docs/stages/S2-planner.md` failure modes: a failed planner call leaves no IR, S4 does not run
    (INTENT_UNAVAILABLE), S5 runs no verifier and walks `validation.fallbacks`; the envelope is `failed` unless a
    fallback yields contract-valid output, which is returned as `degraded`. No path without knowledge exists."""
    import prompt_enhancer.pipeline as module

    env = environment(model_test_profile, tmp_path, project_root)
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    env.config['validation']['fallbacks'] = fallbacks
    folder = marked_bundle(env, project_root)
    set_manifest(folder, golden_tests=['g1'])

    async def compose(*args, **kwargs):
        return OwnerDouble(env.writer)

    monkeypatch.setattr(module, 'compose_model_client', compose)

    async def run():
        async with Pipeline(env.config, project_root=project_root) as pipeline:
            with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
                await check_profile(env.config, 'p1', GoldenAdapter(pipeline), project_root=project_root)
    asyncio.run(run())
    case, = json.loads((folder / 'golden_results.json').read_bytes())['cases']
    diagnostics = case['diagnostics']
    assert case['status'] == status
    assert diagnostics['planner']['state'] == 'failed' and diagnostics['planner']['cause'] == 'PROBE_MALFORMED'
    assert diagnostics['writer'] == {'state': 'failed', 'code': 'INTENT_UNAVAILABLE', 'calls': []}
    assert diagnostics['validation']['status'] == status
    assert diagnostics['validation']['fallback_attempts'][-1] == {'name': fallback, 'available': True}
    assert env.writer.calls == [] and env.verifier.calls == []


FATAL_MARKER = 'ZQX-T017-FATAL-7d3e'  # unique synthetic text, only in the fatal error message and its probe evidence


def test_fatal_model_call_error_reaches_case_and_trace_as_its_code_only(model_test_profile, tmp_path, project_root,
                                                                       monkeypatch):
    """Review F2: a ModelCallError raised outside the planner (here: composing the model owner fails like a probe
    failure) is fatal; its code reaches `diagnostics.fatal` and the trace row, its text reaches nothing (R-026)."""
    import prompt_enhancer.pipeline as module
    from prompt_enhancer.storage import open_data_store

    env = environment(model_test_profile, tmp_path, project_root)
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    folder = marked_bundle(env, project_root)
    set_manifest(folder, golden_tests=['g1'])

    async def compose(*args, **kwargs):
        error = ModelCallError(f'PROBE_MALFORMED: the probe reply said {FATAL_MARKER}')
        error.probe_evidence = [dict(PROBE_EVENT, detail=FATAL_MARKER)]
        raise error

    monkeypatch.setattr(module, 'compose_model_client', compose)

    async def run():
        async with Pipeline(env.config, project_root=project_root) as pipeline:
            with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
                await check_profile(env.config, 'p1', GoldenAdapter(pipeline), project_root=project_root)
    asyncio.run(run())
    raw = (folder / 'golden_results.json').read_bytes()
    case, = json.loads(raw)['cases']
    assert case['diagnostics']['fatal'] == ['PROBE_MALFORMED']
    assert case['diagnostics']['planner'] is None and case['diagnostics']['writer'] is None  # S2 never ran
    assert [event['reason'] for event in case['diagnostics']['probe_events']] == ['PROBE_MALFORMED']
    rows = open_data_store(env.config, 'enhance once', project_root=project_root).read_rows('request_traces')
    row, = rows
    assert row['status'] == 'failed'
    assert 'ModelCallError' in row['warnings'] and 'PROBE_MALFORMED' in row['warnings']
    assert FATAL_MARKER.encode() not in raw
    assert FATAL_MARKER not in json.dumps(case, ensure_ascii=False)
    assert FATAL_MARKER not in json.dumps(row, ensure_ascii=False, default=str)


def test_runner_double_without_run_case_records_null_diagnostics(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    folder = marked_bundle(env, project_root)
    set_manifest(folder, golden_tests=['g1'])
    asyncio.run(check_profile(env.config, 'p1', RunnerDouble(), project_root=project_root))
    case, = json.loads((folder / 'golden_results.json').read_bytes())['cases']
    assert case['diagnostics'] is None and case['passed'] is True and case['warnings'] == []


def test_runner_exception_records_class_and_checked_code(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    folder = marked_bundle(env, project_root)
    set_manifest(folder, golden_tests=['g1'])
    error = ModelCallError(f'PROBE_MALFORMED: {MARKER}')
    with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
        asyncio.run(check_profile(env.config, 'p1', RunnerDouble(error=error), project_root=project_root))
    raw = (folder / 'golden_results.json').read_bytes()
    case, = json.loads(raw)['cases']
    assert case['error'] == 'ModelCallError' and case['code'] == 'PROBE_MALFORMED' and case['diagnostics'] is None
    assert MARKER.encode() not in raw


@pytest.mark.parametrize('value', [MARKER.lower(), 'free text', '', None, 7, 'A' * 65, 'lower_case'])
def test_case_diagnostics_reduce_any_unrecognised_value(value):
    planned = {'state': 'failed', 'error': {'code': value, 'message': MARKER, 'cause': value},
               'warnings': [value], 'calls': [{'status': value}]}
    written = {'state': value, 'error': {'code': value}, 'calls': [{'status': value}]}
    checked = {'status': value, 'error': None, 'warnings': [value], 'fallback_attempts': [{'name': value,
                                                                                          'available': True}]}
    result = case_diagnostics(planned, written, checked, [value], [{'reason': value}])
    text = json.dumps(result)
    assert MARKER not in text and MARKER.lower() not in text and 'free text' not in text
