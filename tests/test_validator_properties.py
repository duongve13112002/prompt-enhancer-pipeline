"""Generated structural contracts; synthetic values, no model calls (G1/G2)."""
import json
from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest
from jsonschema import ValidationError

from prompt_enhancer.model_json import strict_json
from prompt_enhancer.profile_validators import check_output
from prompt_enhancer.model_schemas import cached_registry

ROOT = Path(__file__).resolve().parents[1]
TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)), max_size=30)
JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | TEXT,
                    lambda child: st.lists(child, max_size=4) | st.dictionaries(TEXT, child, max_size=4), max_leaves=12)


@given(JSON)
def test_strict_decoder_roundtrip(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    assert strict_json(raw) == value
    assert strict_json(raw.encode()) == value


@given(TEXT, st.integers(), st.integers())
def test_strict_decoder_duplicate_refusal(key, first, second):
    with pytest.raises(ValueError):
        strict_json('{' + json.dumps(key) + ':' + str(first) + ',' + json.dumps(key) + ':' + str(second) + '}')


@given(st.sampled_from(['NaN', 'Infinity', '-Infinity', '1e999']), st.lists(st.integers(), max_size=4))
def test_strict_decoder_nonfinite_refusal(raw, prefix):
    with pytest.raises(ValueError):
        strict_json(json.dumps(prefix)[:-1] + (',' if prefix else '') + raw + ']')


@pytest.mark.parametrize('nested', [False, True])
@given(st.integers(-10000, 10000))
def test_json_root_and_nested_number_range(nested, number):
    pointer = '/value' if nested else ''
    checks = [{'id': 'n', 'operator': 'number_range', 'parameters': {
        'pointer': pointer, 'minimum': number, 'maximum': number}}]
    value = {'value': number} if nested else number
    assert not check_output(json.dumps(value), checks, project_root=ROOT, output_format='json_text')
    assert not check_output(value, checks, project_root=ROOT, output_format='json')
    assert check_output(json.dumps({'value': number + 1} if nested else number + 1), checks,
                        project_root=ROOT, output_format='json_text') == ['n']


@pytest.mark.parametrize('nested', [False, True])
@given(st.lists(TEXT, unique=True, max_size=5))
def test_json_keys_property(nested, keys):
    data = dict.fromkeys(keys)
    value = {'value': data} if nested else data
    checks = [{'id': 'k', 'operator': 'keys', 'parameters': {
        'pointer': '/value' if nested else '', 'keys': keys, 'ordered': True}}]
    assert not check_output(json.dumps(value), checks, project_root=ROOT, output_format='json_text')
    assert not check_output(value, checks, project_root=ROOT, output_format='json')
    assert check_output(json.dumps(value), checks, project_root=ROOT, output_format='none') == ['k']


@pytest.mark.parametrize('nested', [False, True])
@given(TEXT)
def test_string_selectors_property(nested, text):
    value = {'value': text} if nested else text
    pointer = '/value' if nested else ''
    for operator, parameters in [('exact_text', {'value': text}),
                                 ('string_length', {'minimum': len(text), 'maximum': len(text)})]:
        checks = [{'id': 's', 'operator': operator, 'parameters': {'pointer': pointer, **parameters}}]
        assert not check_output(json.dumps(value), checks, project_root=ROOT, output_format='json_text')
        assert not check_output(value, checks, project_root=ROOT, output_format='json')
        if not nested:
            assert not check_output(text, checks, project_root=ROOT)


@given(JSON)
def test_wire_operators_property(value):
    raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    for operator in ('json', 'minified_json', 'single_line'):
        check = [{'id': 'w', 'operator': operator, 'parameters': {}}]
        assert not check_output(raw, check, project_root=ROOT, output_format='json_text')
        assert not check_output(raw, check, project_root=ROOT, output_format='none')
        if operator != 'json':
            assert check_output('\n' + raw, check, project_root=ROOT, output_format='json_text') == ['w']
            assert check_output('\n' + raw, check, project_root=ROOT, output_format='none') == ['w']
    assert not check_output(value, [{'id': 'j', 'operator': 'json', 'parameters': {}}],
                            project_root=ROOT, output_format='json')


@pytest.mark.parametrize('operator', ['json', 'minified_json', 'single_line', 'schema'])
@given(TEXT)
def test_nonselector_operators_refuse_nonroot_parameters(operator, pointer):
    from prompt_enhancer.profile_validators import validate_artifact
    from prompt_enhancer.profile_prompt import ProfileError
    parameters = {'schema_id': 'verifier_result.v0'} if operator == 'schema' else {}
    with pytest.raises(ProfileError):
        validate_artifact({'id': 'x', 'operator': operator, 'parameters': {**parameters, 'pointer': '/' + pointer}},
                          project_root=ROOT)


@given(st.integers(), TEXT)
def test_plain_json_looking_text_is_never_selected_or_decoded(number, suffix):
    text = json.dumps(number) + suffix
    check = [{'id': 'text', 'operator': 'exact_text', 'parameters': {'pointer': '', 'value': text}}]
    assert not check_output(text, check, project_root=ROOT, output_format='none')


@pytest.mark.parametrize('nested', [False, True])
@given(st.integers(-10000, 10000), TEXT)
def test_selector_plain_text_matrix(nested, number, text):
    pointer = '/value' if nested else ''
    rows = [('keys', {'keys': ['value'], 'ordered': True}, '{"value":0}', False),
            ('number_range', {'minimum': number, 'maximum': number}, str(number), False),
            ('string_length', {'minimum': len(text), 'maximum': len(text)}, text, not nested),
            ('exact_text', {'value': text}, text, not nested)]
    for operator, params, value, passes in rows:
        check = [{'id': 'matrix', 'operator': operator, 'parameters': {'pointer': pointer, **params}}]
        assert check_output(value, check, project_root=ROOT, output_format='none') == ([] if passes else ['matrix'])


@given(JSON)
def test_wire_decoded_matrix_has_no_invented_serialization(value):
    assert check_output(value, [{'id': 'm', 'operator': 'minified_json', 'parameters': {}}],
                        project_root=ROOT, output_format='json') == ['m']
    if not isinstance(value, str):
        assert check_output(value, [{'id': 'l', 'operator': 'single_line', 'parameters': {}}],
                            project_root=ROOT, output_format='json') == ['l']


@given(st.text(alphabet=st.characters(blacklist_categories=('Cs', 'Cc', 'Zl', 'Zp')), max_size=30))
def test_single_line_decoded_strings_property(text):
    check = [{'id': 'line', 'operator': 'single_line', 'parameters': {}}]
    assert not check_output(text, check, project_root=ROOT, output_format='json')
    assert check_output(text + '\n', check, project_root=ROOT, output_format='json') == ['line']


@given(st.lists(TEXT, max_size=4))
def test_schema_operator_property(notes):
    value = {'results': [{'constraint_id': str(i), 'verdict': 'fail', 'evidence_span': None, 'note': note}
                         for i, note in enumerate(notes)]}
    checks = [{'id': 's', 'operator': 'schema', 'parameters': {'schema_id': 'verifier_result.v0'}}]
    assert not check_output(json.dumps(value), checks, project_root=ROOT, output_format='json_text')
    assert not check_output(value, checks, project_root=ROOT, output_format='json')
    assert check_output(json.dumps(value), checks, project_root=ROOT, output_format='none') == ['s']
    value['foreign'] = True
    assert check_output(value, checks, project_root=ROOT, output_format='json') == ['s']


@pytest.mark.parametrize('schema', ['writer_result.v0', 'writer_result.v1', 'validation_result.v0',
                                    'validation_result.v1', 'profile_validator.v0', 'verifier_calibration.v0'])
@given(TEXT, JSON)
def test_closed_result_schema_generated_extra_refusal(schema, key, payload):
    registry = cached_registry(ROOT / 'contracts/schemas')
    tag = 'sha256:' + 'a' * 64
    if schema.startswith('writer_result.'):
        value = {'state': 'draft', 'output': [payload], 'calls': [], 'warnings': [], 'error': None,
                 'brief_hash': tag, 'system_prompt_hash': tag, 'addendum_hash': None}
        if schema == 'writer_result.v1':
            value['effective_system_hash'] = tag
    elif schema.startswith('validation_result.'):
        value = {'status': 'success', 'output': [payload], 'calls': [], 'warnings': [], 'error': None,
                 'verdicts': [], 'repair_count': 0, 'fallback': None, 'fallback_attempts': [],
                 'attempts': [], 'monitoring_selected': False}
    elif schema == 'profile_validator.v0':
        value = {'id': 'generated', 'operator': 'exact_text', 'parameters': {'pointer': '', 'value': key}}
    else:
        model = {'provider': 'openai_compatible', 'model': {'id': key, 'revision': 'test', 'source': 'none',
                                                         'file': 'none', 'mmproj_file': 'none'}}
        value = {'human_checked': True, 'writer_models': [model], 'verifier_models': [model],
                 'writer_settings_hash': tag, 'verifier_settings_hash': tag, 'writer_prompt_hash': tag,
                 'verifier_prompt_hash': tag, 'system_prompt_hash': tag, 'addendum_hash': None,
                 'ledger_schema_hash': tag, 'evidence_ref': 'test.json', 'evidence_hash': tag}
    registry.validate(schema, strict_json(json.dumps(value)))
    value['@unknown:' + key] = payload
    with pytest.raises(ValidationError):
        registry.validate(schema, strict_json(json.dumps(value)))
