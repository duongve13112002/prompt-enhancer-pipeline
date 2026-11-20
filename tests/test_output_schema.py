"""T017-FIX3 (decision 194; R-004, R-015): the admin bundle's output schema is checked at load, sent on every writer
attempt and validated against the same bytes; `output_schema_id: none` sends nothing. Synthetic schemas only."""
import asyncio
import hashlib
import json

import pytest

from assembly_fixtures import ir, plan
from prompt_enhancer.model_client import CAPABILITIES, BoundModelClient, ModelClientRun
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.profiles import OUTPUT_SCHEMA_FILE, load_output_schema
from prompt_enhancer.writer import write
from test_model_client import DoubleTransport
from test_writer import setup, view
from validation_fixtures import draft as s5_draft, environment, run as run_s5

SCHEMA_ID = 'admin_caption.v1'  # test-only bundle schema id, no project contract of that name
SCHEMA = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': SCHEMA_ID, 'type': 'object',
          'required': ['caption'], 'properties': {'caption': {'type': 'string', 'minLength': 1}},
          'additionalProperties': False}


def bundle_schema(tmp_path, value=SCHEMA, raw=None):
    folder = tmp_path / 'bundle'
    folder.mkdir(exist_ok=True)
    data = raw if raw is not None else json.dumps(value).encode('utf-8')
    (folder / OUTPUT_SCHEMA_FILE).write_bytes(data)
    return folder, data


def test_bundle_schema_loads_with_hash_of_its_bytes(tmp_path, project_root):
    folder, data = bundle_schema(tmp_path)
    loaded = load_output_schema({'output_schema_id': SCHEMA_ID}, folder, project_root)
    assert loaded == (SCHEMA_ID, SCHEMA, hashlib.sha256(data).hexdigest())


def test_none_without_file_sends_nothing_and_contract_id_without_file_is_kept(tmp_path, project_root):
    folder = tmp_path / 'bundle'
    folder.mkdir()
    assert load_output_schema({'output_schema_id': 'none'}, folder, project_root) is None
    assert load_output_schema({'output_schema_id': 'verifier_result.v0'}, folder, project_root) is None


@pytest.mark.parametrize(('case', 'match'), [
    ('missing_id', 'no output_schema.json in the bundle'),
    ('file_with_none', 'present but output_schema_id is none'),
    ('not_json', 'not a valid JSON Schema'),
    ('not_object', 'not a valid JSON Schema'),
    ('bad_schema', 'not a valid JSON Schema'),
    ('id_mismatch', r'\$id must equal'),
    ('contract_clash', 'also a project contract'),
])
def test_bundle_schema_refusals(tmp_path, project_root, case, match):
    manifest = {'output_schema_id': SCHEMA_ID}
    if case == 'missing_id':
        folder = tmp_path / 'bundle'
        folder.mkdir()
    elif case == 'file_with_none':
        folder, _ = bundle_schema(tmp_path)
        manifest = {'output_schema_id': 'none'}
    elif case == 'not_json':
        folder, _ = bundle_schema(tmp_path, raw=b'{"type": "object",}')
    elif case == 'not_object':
        folder, _ = bundle_schema(tmp_path, raw=b'[]')
    elif case == 'bad_schema':
        folder, _ = bundle_schema(tmp_path, {**SCHEMA, 'type': 'no-such-type'})
    elif case == 'id_mismatch':
        folder, _ = bundle_schema(tmp_path, {**SCHEMA, '$id': 'other.v1'})
    else:
        folder, _ = bundle_schema(tmp_path, {**SCHEMA, '$id': 'verifier_result.v0'})
        manifest = {'output_schema_id': 'verifier_result.v0'}
    with pytest.raises(ProfileError, match=match) as raised:
        load_output_schema(manifest, folder, project_root)
    assert raised.value.code == 'PROFILE_OUTPUT_SCHEMA_INVALID'


def writer_run(config, project_root, reply, output_schema):
    """The production per-request binding (`ModelClientRun.use_output_schema`) over a labelled HTTP double."""
    s0 = view(config)
    if output_schema is not None:
        s0.profile.manifest['output_schema_id'] = output_schema[0]
    s0.profile.output_schema = output_schema
    s0.profile.output_schema_id = None if output_schema is None else output_schema[0]
    owner = ModelClientRun(config, project_root, probe_cases={}, local_probe_plans={})
    transport = DoubleTransport(reply)
    owner.transports['main'] = transport
    owner.probes['main'] = ProbeResult('complete', dict.fromkeys(CAPABILITIES, True), {})
    owner.use_output_schema(output_schema)
    result = asyncio.run(write(s0, plan(ir()), None, BoundModelClient(owner, 'writer'), project_root=project_root))
    return transport.body, result


def test_writer_sends_bundle_schema_and_keeps_system_bytes(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    baseline, plain = writer_run(config, project_root, 'free text', None)
    assert 'response_format' not in baseline and plain['state'] == 'draft'  # `none` sends no schema
    loaded = (SCHEMA_ID, SCHEMA, 'digest-not-read-by-the-writer')
    body, result = writer_run(config, project_root, '{"caption":"a cat"}', loaded)
    assert body['response_format']['type'] == 'json_schema'
    assert body['response_format']['json_schema']['schema'] == SCHEMA
    assert result['state'] == 'draft' and result['output'] == {'caption': 'a cat'}
    # R-004: the system message bytes and the message order are unchanged by the schema.
    assert body['messages'] == baseline['messages']
    assert body['messages'][0]['content'].encode('utf-8') == b'Admin\r\n'


@pytest.mark.parametrize('reply', ['{"caption":""}', '{"caption":"x","extra":1}', '{"text":"x"}'])
def test_writer_validates_reply_against_bundle_schema(model_test_profile, tmp_path, project_root, reply):
    config = setup(model_test_profile, tmp_path, project_root)
    _, result = writer_run(config, project_root, reply, (SCHEMA_ID, SCHEMA, 'digest'))
    assert result['state'] == 'failed' and result['calls'][-1]['status'] == 'SCHEMA_INVALID'


def test_binding_again_replaces_the_previous_schema(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    owner = ModelClientRun(config, project_root, probe_cases={}, local_probe_plans={})
    owner.use_output_schema((SCHEMA_ID, SCHEMA, 'digest'))
    other = {**SCHEMA, '$id': 'admin_other.v1'}
    owner.use_output_schema(('admin_other.v1', other, 'digest'))  # no duplicate-id refusal, no stacking
    assert owner.client.output_schema_id == 'admin_other.v1'
    with pytest.raises(Exception):
        owner.schemas.get(SCHEMA_ID)
    owner.use_output_schema(None)
    assert owner.client.output_schema_id is None and owner.schemas is owner._contract_schemas


def s5_environment(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s0.profile.manifest['output_schema_id'] = SCHEMA_ID
    env.s0.profile.output_schema = (SCHEMA_ID, SCHEMA, 'digest')
    env.s0.profile.output_schema_id = SCHEMA_ID
    return env


def test_s5_accepts_a_draft_valid_under_the_bundle_schema(model_test_profile, tmp_path, project_root):
    env = s5_environment(model_test_profile, tmp_path, project_root)
    result = run_s5(env, result=s5_draft(env, output={'caption': 'kept'}))
    assert result['status'] == 'success' and env.writer.calls == []


def test_s5_repair_is_checked_against_the_bundle_schema(model_test_profile, tmp_path, project_root):
    env = s5_environment(model_test_profile, tmp_path, project_root)
    env.writer.replies = [{'caption': 'kept'}]
    result = run_s5(env, result=s5_draft(env, output={'text': 'kept'}))  # the draft breaks the bundle schema
    assert result['status'] == 'repaired' and result['repair_count'] == 1
    assert result['output'] == {'caption': 'kept'}
    env.writer.replies = [{'still': 'wrong'}]
    result = run_s5(env, result=s5_draft(env, output={'text': 'kept'}))
    assert result['status'] != 'repaired' and result['output'] != {'still': 'wrong'}
