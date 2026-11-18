"""Generated refusal properties at every strict model/artifact/evidence boundary (G2)."""
import asyncio
import json

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.profile_validators import load_validators
from prompt_enhancer.verifier_policy import require_calibrated_verifier
from test_model_addendum import local_client
from validation_fixtures import calibrate, draft, environment, run

TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)), max_size=25)


@given(TEXT, st.integers())
def test_model_structured_decoder_refuses_generated_duplicate(key, number):
    raw = '{' + json.dumps(key) + ':' + str(number) + ',' + json.dumps(key) + ':0}'
    client, _ = local_client(raw, structured=True)
    output, record = asyncio.run(client.call('planner', b'admin', 'data'))
    assert output is None and record['status'] == 'SCHEMA_INVALID'


@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(TEXT)
def test_artifact_decoder_refuses_generated_duplicate(tmp_path, project_root, text):
    folder = tmp_path / 'validators'
    folder.mkdir(exist_ok=True)
    target = folder / 'check.json'
    valid = '{"id":"check","operator":"json","parameters":{}}'
    target.write_text(valid, encoding='utf-8')
    assert load_validators(tmp_path, ['check'], project_root=project_root)[0][0]['id'] == 'check'
    target.write_text('{"id":' + json.dumps(text) + ',' + valid[1:], encoding='utf-8')
    with pytest.raises(ProfileError):
        load_validators(tmp_path, ['check'], project_root=project_root)


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity', '1e400'])
def test_artifact_decoder_refuses_nonfinite_fallback_data(tmp_path, project_root, number):
    folder = tmp_path / 'validators'
    folder.mkdir()
    target = folder / 'check.json'
    control = ('{"id":"check","operator":"json","parameters":{},'
               '"fallback":{"input_variable":"original_prompt","template":{"value":NUMBER}}}')
    target.write_text(control.replace('NUMBER', '1'), encoding='utf-8')
    assert load_validators(tmp_path, ['check'], project_root=project_root)[0][0]['fallback']['template'] == {'value': 1}
    target.write_text(control.replace('NUMBER', number), encoding='utf-8')
    with pytest.raises(ProfileError, match='unresolved check'):
        load_validators(tmp_path, ['check'], project_root=project_root)


@settings(max_examples=12, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.booleans())
def test_calibration_decoder_refuses_duplicate_attestation(model_test_profile, tmp_path, project_root, flag):
    env = environment(model_test_profile, tmp_path, project_root)
    target = calibrate(env)
    raw = target.read_text(encoding='utf-8')
    target.write_text('{"human_checked":' + json.dumps(flag) + ',' + raw[1:], encoding='utf-8')
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)


@settings(max_examples=12, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(TEXT)
def test_provenance_decoder_refuses_generated_duplicate(model_test_profile, tmp_path, project_root, value):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    result = draft(env)
    raw = result['calls'][-1]['record_json']
    result['calls'][-1]['record_json'] = '{"model_id":' + json.dumps(value) + ',' + raw[1:]
    checked = run(env, result=result)
    assert checked['status'] in ('degraded', 'failed') and env.events == []


@settings(max_examples=12, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(TEXT)
def test_monitor_provenance_decoder_refuses_generated_duplicate(model_test_profile, tmp_path, project_root, value):
    from prompt_enhancer.pipeline import monitor_output
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    checked = draft(env)
    raw = checked['calls'][-1]['record_json']
    checked['calls'][-1]['record_json'] = '{"model_id":' + json.dumps(value) + ',' + raw[1:]
    with pytest.raises(ValueError):
        asyncio.run(monitor_output(env.s0, env.s2, None, checked, None, project_root=project_root))
    assert env.events == []


@settings(max_examples=4, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=10_000, max_value=10_500), st.booleans())
def test_deep_model_output_uses_typed_fallback(model_test_profile, tmp_path, project_root, depth, decoded):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['validation'].update(max_repairs=0, fallbacks=['failed'])
    env.s0.profile.validator_checks = ('json',)
    env.s0.profile.validator_artifacts = ({'id': 'json', 'operator': 'json', 'parameters': {}},)
    output = '[' * depth + '0' + ']' * (depth - 1)
    if decoded:
        output = 0
        for _ in range(depth):
            output = [output]
        env.s0.profile.manifest['output_schema_id'] = 'verifier_result.v0'
    assert run(env, output)['status'] == 'failed'
