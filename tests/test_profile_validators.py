"""Closed profile validator artifacts, strict structural parsing and activation binding."""
import asyncio
import json

from hypothesis import given, settings, strategies as st
import pytest

from profile_fixtures import RunnerDouble, install_prompts, set_manifest
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.profile_validators import check_output, load_validators, strict_json
from prompt_enhancer.profiles import check_profile, resolve_profile
from test_profiles import activate


def artifact(tmp_path, name='cap', **changes):
    value = {'id': name, 'operator': 'string_length', 'parameters': {'pointer': '', 'minimum': 0, 'maximum': 4}}
    value.update(changes)
    folder = tmp_path / 'validators'
    folder.mkdir(exist_ok=True)
    (folder / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
    return value


def test_parameter_override_and_missing_refusal(tmp_path, project_root):
    artifact(tmp_path)
    validators, hashes = load_validators(tmp_path, ['cap'], project_root=project_root)
    assert check_output('abcde', validators, project_root=project_root)
    artifact(tmp_path, parameters={'pointer': '', 'minimum': 0, 'maximum': 6})
    updated, newer = load_validators(tmp_path, ['cap'], project_root=project_root)
    assert not check_output('abcde', updated, project_root=project_root) and hashes != newer
    artifact(tmp_path, parameters={'pointer': '', 'minimum': 0})
    with pytest.raises(ProfileError, match='VALIDATOR'):
        load_validators(tmp_path, ['cap'], project_root=project_root)


@settings(max_examples=40, deadline=None)
@given(st.text().filter(lambda x: x not in {'json', 'single_line', 'minified_json', 'keys', 'string_length',
                                          'number_range', 'exact_text', 'schema'}))
def test_unknown_operator_refusal_property(operator):
    from prompt_enhancer.profile_validators import validate_artifact
    with pytest.raises(ProfileError, match='VALIDATOR'):
        validate_artifact({'id': 'x', 'operator': operator, 'parameters': {}}, project_root=None)


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '[Infinity]'])
def test_strict_json_refuses_ambiguous_or_nonfinite(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_profile_activation_binds_validator_bytes(tmp_path, project_root):
    install_prompts(project_root)
    folder, config = activate(tmp_path, project_root)
    artifact(folder, parameters={'pointer': '', 'minimum': 0, 'maximum': 8})
    set_manifest(folder, validator={'checks': ['cap']})
    asyncio.run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    view = resolve_profile(config, project_root=project_root)
    assert view.validator_hashes['cap'] == view.activation['validator_hashes']['cap']
    artifact(folder, parameters={'pointer': '', 'minimum': 0, 'maximum': 5})
    with pytest.raises(ProfileError, match='PROFILE_NOT_CHECKED'):
        resolve_profile(config, project_root=project_root)


def test_unknown_name_and_path_refused(tmp_path, project_root):
    for names in (['unknown'], ['../bad'], ['single_line', 'single_line']):
        with pytest.raises(ProfileError, match='VALIDATOR'):
            load_validators(tmp_path, names, project_root=project_root)


def test_golden_json_text_uses_declared_contract_at_root(tmp_path, project_root):
    from profile_fixtures import envelope
    install_prompts(project_root)
    folder, config = activate(tmp_path, project_root)
    artifact(folder, name='json', operator='json', parameters={})
    artifact(folder, name='keys', operator='keys', parameters={'pointer': '', 'keys': ['text'], 'ordered': True})
    set_manifest(folder, validator={'checks': ['json', 'keys']})
    value = envelope('success')
    value['output'] = '{"text":"raw JSON output"}'
    asyncio.run(check_profile(config, 'p1', RunnerDouble(raw=value), project_root=project_root))
    assert resolve_profile(config, project_root=project_root).validator_checks == ('json', 'keys')


@settings(max_examples=50, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',))))
def test_exact_literal_property(text):
    checks = [{'id': 'literal', 'operator': 'exact_text', 'parameters': {'pointer': '/text', 'value': text}}]
    assert not check_output({'text': text}, checks, project_root=None)
    assert check_output({'text': text + 'x'}, checks, project_root=None)


@pytest.mark.parametrize('operator,parameters,good,bad', [
    ('json', {}, '{"value":1}', '{"value":NaN}'),
    ('single_line', {}, 'line', 'line\u2028'),
    ('minified_json', {}, '{"text":"two words"}', '{ "text": "two words" }'),
    ('keys', {'pointer': '', 'keys': ['a', 'b'], 'ordered': True}, {'a': 1, 'b': 2}, {'b': 2, 'a': 1}),
    ('number_range', {'pointer': '/items/0', 'minimum': 1, 'maximum': 3}, {'items': [2]}, {'items': [True]}),
    ('exact_text', {'pointer': '/a~1b/~0key', 'value': 'é'}, {'a/b': {'~key': 'é'}}, {'a/b': {'~key': 'e'}}),
    ('schema', {'schema_id': 'verifier_result.v0'}, {'results': []}, {'wrong': []}),
])
def test_structural_operators(operator, parameters, good, bad, project_root):
    checks = [{'id': 'check', 'operator': operator, 'parameters': parameters}]
    assert not check_output(good, checks, project_root=project_root)
    assert check_output(bad, checks, project_root=project_root) == ['check']


@pytest.mark.parametrize('change', [
    {'operator': 'unknown'}, {'parameters': {'pointer': ''}},
    {'parameters': {'pointer': '/bad~2', 'minimum': 0, 'maximum': 3}},
    {'parameters': {'pointer': '', 'minimum': True, 'maximum': 3}},
    {'parameters': {'pointer': '', 'minimum': 4, 'maximum': 3}},
    {'parameters': {'pointer': '', 'minimum': 0, 'maximum': 3, 'hidden_default': 2}},
    {'debug': 'forbidden'},
])
def test_closed_artifact_refusals(tmp_path, project_root, change):
    artifact(tmp_path, **change)
    with pytest.raises(ProfileError, match='VALIDATOR'):
        load_validators(tmp_path, ['cap'], project_root=project_root)


def test_changed_artifact_during_golden_run_refuses_activation(tmp_path, project_root):
    install_prompts(project_root)
    folder, config = activate(tmp_path, project_root)
    artifact(folder)
    set_manifest(folder, validator={'checks': ['cap']})

    class MutatingDouble(RunnerDouble):
        async def run(self, profile, request):
            artifact(folder, parameters={'pointer': '', 'minimum': 0, 'maximum': 9})
            return await super().run(profile, request)

    with pytest.raises(ProfileError, match='PROFILE_CHANGED'):
        asyncio.run(check_profile(config, 'p1', MutatingDouble(), project_root=project_root))
    assert not (folder / 'activation.json').exists()


def test_referenced_schema_hash_changes_and_fallback_metadata_is_closed(tmp_path, project_root):
    artifact(tmp_path, operator='schema', parameters={'schema_id': 'verifier_result.v0'},
             fallback={'template': '{{original_prompt}}', 'input_variable': 'original_prompt'})
    _, before = load_validators(tmp_path, ['cap'], project_root=project_root)
    schema = project_root / 'contracts/schemas/verifier_result.v0.schema.json'
    original = schema.read_bytes()
    try:
        schema.write_bytes(original + b'\n')
        _, after = load_validators(tmp_path, ['cap'], project_root=project_root)
        assert after['schema:verifier_result.v0'] != before['schema:verifier_result.v0']
    finally:
        schema.write_bytes(original)
    artifact(tmp_path, operator='schema', parameters={'schema_id': 'verifier_result.v0'},
             fallback={'template': '{{original_prompt}} suffix', 'input_variable': 'original_prompt'})
    _, changed = load_validators(tmp_path, ['cap'], project_root=project_root)
    assert changed['cap'] != before['cap']
    artifact(tmp_path, fallback={'template': '{{original_prompt}}'})
    with pytest.raises(ProfileError, match='VALIDATOR'):
        load_validators(tmp_path, ['cap'], project_root=project_root)


@pytest.mark.parametrize('operator,parameters', [
    ('json', {}), ('single_line', {}), ('minified_json', {}),
    ('keys', {'pointer': '', 'keys': ['a'], 'ordered': True}),
    ('string_length', {'pointer': '', 'minimum': 0, 'maximum': 4}),
    ('number_range', {'pointer': '', 'minimum': 0, 'maximum': 4}),
    ('exact_text', {'pointer': '', 'value': 'literal'}), ('schema', {'schema_id': 'verifier_result.v0'}),
])
def test_each_artifact_parameter_is_required(operator, parameters, project_root):
    from prompt_enhancer.profile_validators import validate_artifact
    value = {'id': 'check', 'operator': operator, 'parameters': parameters}
    missing = dict(value)
    del missing['parameters']
    with pytest.raises(ProfileError, match='VALIDATOR'):
        validate_artifact(missing, project_root=project_root)
    for key in parameters:
        missing = dict(value, parameters={name: item for name, item in parameters.items() if name != key})
        with pytest.raises(ProfileError, match='VALIDATOR'):
            validate_artifact(missing, project_root=project_root)


@pytest.mark.parametrize('operator,parameters,key,alternate,output', [
    ('keys', {'pointer': '', 'keys': ['a'], 'ordered': True}, 'keys', ['b'], {'a': 1}),
    ('keys', {'pointer': '', 'keys': ['a', 'b'], 'ordered': False}, 'ordered', True, {'b': 1, 'a': 2}),
    ('string_length', {'pointer': '', 'minimum': 0, 'maximum': 4}, 'minimum', 4, 'abc'),
    ('string_length', {'pointer': '', 'minimum': 0, 'maximum': 4}, 'maximum', 2, 'abc'),
    ('number_range', {'pointer': '', 'minimum': 0, 'maximum': 4}, 'minimum', 3, 2),
    ('number_range', {'pointer': '', 'minimum': 0, 'maximum': 4}, 'maximum', 1, 2),
    ('exact_text', {'pointer': '/a', 'value': 'yes'}, 'pointer', '/b', {'a': 'yes', 'b': 'no'}),
    ('exact_text', {'pointer': '', 'value': 'é'}, 'value', 'e', 'é'),
    ('schema', {'schema_id': 'verifier_result.v0'}, 'schema_id', 'writer_result.v0', {'results': []}),
])
def test_artifact_parameter_override_changes_verdict(operator, parameters, key, alternate, output, project_root):
    value = {'id': 'check', 'operator': operator, 'parameters': parameters}
    assert not check_output(output, [value], project_root=project_root)
    value['parameters'] = dict(parameters, **{key: alternate})
    assert check_output(output, [value], project_root=project_root) == ['check']
