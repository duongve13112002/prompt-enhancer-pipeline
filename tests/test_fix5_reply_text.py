"""T017-FIX5: text-property checks judge the exact reply text of a schema-bound reply; one forced tool call; S5
diagnostics. Synthetic values and labelled doubles only; no model call."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pickle
import re

from hypothesis import given, settings, strategies as st
import pytest

from prompt_enhancer.model_client import RoleClient
from prompt_enhancer.model_cloud import TOOL_FORCED_NAME, cloud_request
from prompt_enhancer.model_probe import ProbeResult
from test_cloud2_tool_forced import BACKEND, FULL, ToolDouble, role, schemas  # noqa: F401 - schemas is a fixture
from prompt_enhancer.model_json import JsonArray, JsonObject, reply_text, strict_json, with_reply_text
from prompt_enhancer.pipeline import _failed_items, _repair_calls, case_diagnostics
from prompt_enhancer.profile_validators import check_output

ROOT = Path(__file__).resolve().parents[1]
MINIFIED = [{'id': 'm', 'operator': 'minified_json', 'parameters': {}}]
LINE = [{'id': 'l', 'operator': 'single_line', 'parameters': {}}]
JSON_OP = [{'id': 'j', 'operator': 'json', 'parameters': {}}]
KEYS = [{'id': 'k', 'operator': 'keys', 'parameters': {'pointer': '', 'keys': ['a'], 'ordered': False}}]
TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)), max_size=12)
VALUE = st.recursive(st.none() | st.booleans() | st.integers(-9, 9) | TEXT,
                     lambda child: st.lists(child, max_size=3) | st.dictionaries(TEXT, child, max_size=3), max_leaves=8)
SPACE = st.text(alphabet=' \t\n\r', max_size=3)


def carried(text):
    return with_reply_text(strict_json(text), text)


def run(text, checks, fmt='json'):
    return check_output(carried(text), checks, project_root=ROOT, output_format=fmt)


# -- the class fix: a schema-bound reply (output_format json) is judged on the text the model sent ---------------------
def test_minified_one_line_reply_passes_both_text_checks():
    text = '{"a":"two  words","b":[1,2]}'
    assert run(text, MINIFIED + LINE + JSON_OP) == []


def test_pretty_printed_reply_fails_minified_and_single_line():
    text = '{\n  "a": 1\n}'
    assert run(text, MINIFIED) == ['m'] and run(text, LINE) == ['l'] and run(text, JSON_OP) == []


def test_spaces_inside_strings_do_not_break_minified_but_a_newline_after_the_comma_does():
    assert run('{"a":"x y\\n z"}', MINIFIED + LINE) == []
    assert run('{"a":1,\n"b":2}', MINIFIED + LINE) == ['m', 'l']


def test_text_is_the_model_text_not_a_reserialization():
    text = '{"b":1, "a":"x"}'  # key order and a space after the comma, as the model sent it
    decoded = carried(text)
    assert decoded == {'a': 'x', 'b': 1}
    assert json.dumps(decoded) != text and reply_text(decoded) == text
    assert check_output(decoded, MINIFIED, project_root=ROOT, output_format='json') == ['m']
    assert check_output(json.loads(json.dumps(decoded, separators=(',', ':'))), MINIFIED, project_root=ROOT,
                        output_format='json') == ['m']  # a plain decoded value has no text: closed, never re-serialized


def test_a_value_without_text_fails_text_checks_as_before_and_keeps_structure_checks():
    plain = {'a': 1}
    assert check_output(plain, MINIFIED + LINE, project_root=ROOT, output_format='json') == ['m', 'l']
    assert check_output(plain, JSON_OP + KEYS, project_root=ROOT, output_format='json') == []


def test_changed_value_never_reuses_the_old_text():
    value = carried('{"a":1}')
    value['a'] = 2
    assert reply_text(value) is None
    assert check_output(value, MINIFIED, project_root=ROOT, output_format='json') == ['m']


def test_repaired_reply_is_judged_on_its_own_text():
    first, repaired = '{\n"a":1}', '{"a":1}'
    assert run(first, MINIFIED) == ['m'] and run(repaired, MINIFIED) == []


def test_array_root_and_scalar_root():
    assert run('[1, 2]', MINIFIED) == ['m'] and run('[1,2]', MINIFIED) == []
    assert isinstance(with_reply_text(strict_json('3'), '3'), int)  # a scalar has no carrier
    assert check_output(strict_json('"a b"'), LINE, project_root=ROOT, output_format='json') == []  # as before


def test_carrier_survives_deepcopy_and_pickle_and_equals_a_plain_value():
    value = carried('{"a":[1, 2]}')
    for copy in (deepcopy(value), pickle.loads(pickle.dumps(value))):
        assert isinstance(copy, JsonObject) and copy == {'a': [1, 2]} and reply_text(copy) == '{"a":[1, 2]}'
        assert copy is not value
    arr = carried('[1,2]')
    assert isinstance(deepcopy(arr), JsonArray) and reply_text(deepcopy(arr)) == '[1,2]'
    assert json.dumps(value) == '{"a": [1, 2]}'


# -- paths that keep their bytes and verdicts -------------------------------------------------------------------------
@pytest.mark.parametrize('fmt,output,expected', [
    ('json_text', '{"a":1}', []), ('json_text', '{ "a":1}', ['m']), ('json_text', '{"a":1}\n', ['m']),
    ('none', '{"a":1}', []), ('none', '{ "a":1}', ['m']), ('none', {'a': 1}, ['m'])])
def test_json_text_and_none_formats_keep_their_verdicts(fmt, output, expected):
    assert check_output(output, MINIFIED, project_root=ROOT, output_format=fmt) == expected


@pytest.mark.parametrize('fmt,output,expected', [
    ('none', 'one line', []), ('none', 'two\nlines', ['l']), ('none', '', []), ('none', {'a': 1}, ['l']),
    ('json_text', '{"a":1}', []), ('json_text', '{"a":\n1}', ['l'])])
def test_single_line_keeps_its_verdict_off_the_schema_bound_path(fmt, output, expected):
    assert check_output(output, LINE, project_root=ROOT, output_format=fmt) == expected


# -- property: text operators follow the raw text, structure operators the decoded value -------------------------------
def _outer_whitespace(text):
    """Oracle: JSON whitespace left once every string literal is removed."""
    return re.search(r'[ \t\r\n]', re.sub(r'"(?:[^"\\]|\\.)*"', '""', text, flags=re.S)) is not None


@settings(max_examples=150, deadline=None)
@given(VALUE.filter(lambda v: isinstance(v, (dict, list))), st.sampled_from([(',', ':'), (', ', ': ')]),
       st.sampled_from([None, 1]), SPACE, SPACE, st.booleans())
def test_text_verdict_follows_raw_text_structure_verdict_follows_decoded_value(value, separators, indent, before,
                                                                               after, ascii_only):
    text = before + json.dumps(value, ensure_ascii=ascii_only, separators=separators, indent=indent,
                               allow_nan=False) + after
    decoded = strict_json(text)
    assert decoded == value
    carrier = with_reply_text(decoded, text)
    minified = check_output(carrier, MINIFIED, project_root=ROOT, output_format='json') == []
    one_line = check_output(carrier, LINE, project_root=ROOT, output_format='json') == []
    assert minified == (not _outer_whitespace(text))
    assert one_line == (text.splitlines() == [text])
    assert check_output(carrier, JSON_OP, project_root=ROOT, output_format='json') == []
    # the structure verdict is the same for the carrier and a plain decoded value: it reads the decoded value only
    if isinstance(value, dict):
        keys = [{'id': 'k', 'operator': 'keys', 'parameters': {'pointer': '', 'keys': list(value), 'ordered': True}}]
        wrong = [{'id': 'k', 'operator': 'keys', 'parameters': {'pointer': '', 'keys': ['absent'], 'ordered': True}}]
        for artifacts, expected in ((keys, []), (wrong, ['k'])):
            assert check_output(carrier, artifacts, project_root=ROOT, output_format='json') == expected
            assert check_output(dict(value), artifacts, project_root=ROOT, output_format='json') == expected


# -- B1: one forced tool call, only on a tool_forced Anthropic request ------------------------------------------------
def test_tool_choice_disables_parallel_tool_use_only_on_the_forced_anthropic_tool():
    forced, _ = cloud_request('anthropic', BACKEND, role(), 's', 'u', schema=FULL)
    assert forced['tool_choice'] == {'type': 'tool', 'name': TOOL_FORCED_NAME, 'disable_parallel_tool_use': True}
    text, _ = cloud_request('anthropic', BACKEND, role('response_format'), 's', 'u', schema=FULL)
    assert 'tool_choice' not in text and 'tools' not in text
    plain, _ = cloud_request('anthropic', BACKEND, role('response_format', output={
        'format': 'none', 'schema_id': 'none', 'strict': False, 'transport': 'response_format',
        'validate_response': False}), 's', 'u')
    assert 'disable_parallel_tool_use' not in json.dumps(plain)
    for provider in ('gemini', 'openai_compatible'):  # tool_forced never runs there: refused, nothing is sent
        with pytest.raises(ValueError):
            cloud_request(provider, BACKEND, role(), 's', 'u', schema=FULL)


# -- the client: a writer reply carries its text, other roles and the local path keep their values -----------------------
def _client(tmp_path, schemas, replies, role_name, output):
    role_value = role('response_format', output=output)
    if role_name == 'writer':  # a writer takes its format from the manifest
        role_value['output'] = dict(output, format='from_profile_manifest', schema_id='from_profile_manifest')
    (tmp_path / '.secrets').mkdir(exist_ok=True)
    (tmp_path / '.secrets' / 'fake.key').write_text('synthetic', encoding='utf-8')
    local = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 't'},
             'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'}}
    config = {'vlm': {'roles': {role_name: role_value}, 'backends': {'cloud': dict(BACKEND), 'main': local},
                      'default_backend': 'main'}}
    probe = ProbeResult('complete', {'json_schema': True, 'text': True}, {})
    client = RoleClient(config, {'cloud': ToolDouble(replies)}, {'cloud': probe, 'main': probe}, schemas,
                        project_root=tmp_path)
    client.output_schema_id = output['schema_id']  # the bundle schema id the run binds (use_output_schema)
    return client


def _call_writer(client, role_name):
    manifest = {'output_schema_id': client.output_schema_id}
    return asyncio.run(client.call(role_name, b'ADMIN', 'user', manifest=manifest))


SCHEMA_OUTPUT = {'format': 'json_schema', 'schema_id': 'cloud2_test.v0', 'strict': False,
                 'transport': 'response_format', 'validate_response': True}


@pytest.mark.parametrize('role_name,carries', [('writer', True), ('planner', False)])
def test_only_a_writer_value_carries_its_reply_text(tmp_path, schemas, role_name, carries):
    text = '{"n": 2,  "s":"a  b"}'
    reply = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}], 'usage': {}}
    client = _client(tmp_path, schemas, reply, role_name, SCHEMA_OUTPUT)
    value, record = _call_writer(client, role_name)
    assert record['status'] == 'OK' and value == {'n': 2, 's': 'a  b'}
    assert (reply_text(value) == text) is carries and isinstance(value, JsonObject) is carries
    assert 'a  b' not in json.dumps(record)  # R-026: the record never holds reply text


def test_the_client_hands_s5_the_text_the_model_sent_not_its_reserialization(tmp_path, schemas):
    text = '{"s":"ab","n":2}'  # model key order differs from sorted and from the schema
    reply = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}], 'usage': {}}
    client = _client(tmp_path, schemas, reply, 'writer', SCHEMA_OUTPUT)
    value, _ = _call_writer(client, 'writer')
    assert check_output(value, MINIFIED + LINE, project_root=ROOT, output_format='json') == []
    spaced = '{"s":"ab",\n"n":2}'
    reply['content'][0]['text'] = spaced
    value, _ = _call_writer(_client(tmp_path, schemas, reply, 'writer', SCHEMA_OUTPUT), 'writer')
    assert check_output(value, MINIFIED + LINE, project_root=ROOT, output_format='json') == ['m', 'l']


def test_a_writer_without_a_schema_returns_the_text_as_before(tmp_path, schemas):
    reply = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"a": 1}'}], 'usage': {}}
    output = {'format': 'none', 'schema_id': 'none', 'strict': False, 'transport': 'response_format',
              'validate_response': False}
    value, _ = _call_writer(_client(tmp_path, schemas, reply, 'writer', output), 'writer')
    assert value == '{"a": 1}' and type(value) is str


# -- C: the diagnostics name the failed S5 items and count the repair call -----------------------------------------------
MARK = 'ZQX-FIX5-MARK-77'


def _call(status, **record):
    return {'status': status, 'record_json': json.dumps({'status': status, **record})}


def test_diagnostics_count_the_repair_call_and_name_failed_items_without_text():
    draft = _call('OK', attempt={'attempt': 0})
    repair = _call('OK', attempt={'attempt': 1})
    verifier = _call('OK', role='verifier')
    written = {'state': 'draft', 'error': None, 'calls': [draft]}
    verdicts = [{'id': '@s5:complete:0', 'verdict': 'pass', 'method': 'structural', 'note': MARK},
                {'id': '@s5:contract:0', 'verdict': 'fail', 'method': 'structural', 'note': MARK},
                {'id': '@s5:validator:single_line_minified_json:0', 'verdict': 'fail', 'method': 'structural',
                 'note': MARK},
                {'id': 'c1', 'verdict': 'fail', 'method': 'structural', 'note': MARK},
                {'id': 'c2', 'verdict': 'fail', 'method': 'verifier', 'note': MARK, 'evidence_span': MARK},
                {'id': MARK + ' spaced text', 'verdict': 'fail', 'method': 'structural', 'note': MARK}]
    checked = {'status': 'failed', 'error': {'code': 'VALIDATION_FAILED', 'message': MARK}, 'warnings': [],
               'fallback_attempts': [], 'calls': [draft, verifier, repair], 'verdicts': verdicts}
    result = case_diagnostics(None, written, checked)
    assert result['writer']['calls'] == ['OK', 'OK']  # the draft and the repair; the verifier call is not a writer call
    assert result['validation']['failed_items'] == [
        {'id': '@s5:contract:0', 'code': 'PROFILE_CONTRACT'},
        {'id': '@s5:validator:single_line_minified_json:0', 'code': 'VALIDATOR_CHECK'},
        {'id': 'c1', 'code': 'STRUCTURAL_FAIL'}, {'id': 'c2', 'code': 'VERIFIER_FAIL'},
        {'id': 'UNRECOGNISED', 'code': 'STRUCTURAL_FAIL'}]
    assert MARK not in json.dumps(result)


def test_diagnostics_without_a_repair_or_verdicts_keep_their_shape():
    written = {'state': 'draft', 'error': None, 'calls': [_call('OK', attempt={'attempt': 0})]}
    result = case_diagnostics(None, written, {'status': 'success', 'error': None, 'warnings': []})
    assert result['writer']['calls'] == ['OK'] and result['validation']['failed_items'] == []
    assert _repair_calls(written, None) == [] and _failed_items({}) == []
    bad = {'status': 'failed', 'error': None, 'warnings': [], 'calls': [{'status': 'OK'}, {'record_json': '{'}, {}]}
    assert _repair_calls(written, bad) == []


# -- S5 (`validation._check`, `_decoded_output`) and a copied envelope output --------------------------------------------
def test_s5_check_of_a_schema_bound_profile_judges_the_carried_text_and_survives_the_envelope_copy():
    from types import SimpleNamespace

    from prompt_enhancer.validation import _check, _decoded_output
    profile = SimpleNamespace(manifest={'output_schema_id': 'bundle.v1'}, validator_artifacts=tuple(MINIFIED + LINE))
    good, spaced = carried('{"a":"x y","b":[1,2]}'), carried('{"a": "x y"}')
    assert _check(good, profile, ROOT) == [] and _check(spaced, profile, ROOT) == ['m']
    assert _check(deepcopy(good), profile, ROOT) == []  # build_envelope deep-copies the output the golden check reads
    assert _decoded_output(good, profile) == {'a': 'x y', 'b': [1, 2]}
    # without a schema the same validators keep judging a string reply
    plain = SimpleNamespace(manifest={'output_schema_id': 'none'}, validator_artifacts=tuple(MINIFIED + LINE))
    assert _check('{"a":"x y"}', plain, ROOT) == [] and _check('{"a": 1}', plain, ROOT) == ['m']
