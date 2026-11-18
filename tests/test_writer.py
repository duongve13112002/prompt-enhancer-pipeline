"""S4 contract tests with a labelled bound-model double; no HTTP or model use."""
import asyncio
import base64
import json
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from assembly_fixtures import ir, plan
from intake_fixtures import configuration
from profile_fixtures import install_prompts
from prompt_enhancer.config import ConfigurationError, validate_writer_reads
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.writer import render_brief, write


class WriterDouble:
    """Labelled test double, recording all unmodified inputs to the bound writer."""
    role_name = 'writer'

    def __init__(self, value='draft', status='OK'):
        self.calls = []
        self.value, self.status = value, status

    async def call(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.value, {'status': self.status, 'warnings': []}


def view(config, prompt='raw', *, system=b'Admin\r\n', template=None, task='generate', addendum=None):
    manifest = {'profile_id': 'p1', 'template_format': 'plain' if template is None else 'sectioned',
                'output_schema_id': 'none', 'output_language': 'English', 'quote_semantics': 'literal_text',
                'edit_output_semantics': 'whole_edited_image', 'renderer': {'accepts_reference_images': False,
                                                                          'image_reference_syntax': 'none'}}
    profile = SimpleNamespace(system_message=system, user_template=template, manifest=manifest,
                              template_values={'aspect_ratio': '{{original_prompt}}'}, input_variable='original_prompt',
                              addendum=addendum, validator_artifacts=(), validator_hashes={}, validator_checks=())
    payload = {'effective_config': config, 'outcome': 'ready', 'prompt': prompt, 'task': task,
               'request_id': 'r1', 'retrieval': {'state': 'disabled', 'snapshot_id': None}, 'source_image': None}
    if task == 'edit':
        payload['source_image'] = {'data': base64.b64encode(b'fixture image').decode()}
    return SimpleNamespace(payload=payload, profile=profile, snapshot=None)


def setup(model_test_profile, tmp_path, project_root):
    install_prompts(project_root)
    return configuration(model_test_profile, tmp_path)


def test_writer_preserves_system_and_single_pass_template(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    s0 = view(config, '{{aspect_ratio}} café', template=b'{{aspect_ratio}}\n{{original_prompt}}')
    client = WriterDouble()
    result = asyncio.run(write(s0, plan(ir()), None, client, project_root=project_root))
    system, user, kwargs = client.calls[0]
    assert system == b'Admin\r\n'
    assert user.startswith('{{original_prompt}}\n')
    assert '{{aspect_ratio}} café' in user
    assert kwargs['manifest'] is s0.profile.manifest and kwargs['images'] == ()
    assert result['state'] == 'draft' and result['output'] == 'draft'


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',)), min_size=1))
def test_writer_system_bytes_property(model_test_profile, tmp_path, project_root, text):
    config = setup(model_test_profile, tmp_path, project_root)
    client = WriterDouble()
    s0 = view(config, system=text.encode('utf-8'))
    asyncio.run(write(s0, plan(ir()), None, client, project_root=project_root))
    assert client.calls[0][0] == text.encode('utf-8')


@settings(max_examples=50, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',))))
def test_template_substitution_is_single_pass(text):
    config = {'creativity': {}, 'rag': {'enabled': False}}
    s0 = view(config, text, template=b'{{aspect_ratio}}|{{original_prompt}}')
    user = render_brief(s0, plan(ir()), None, b'{{data}}')
    prefix, raw = user.split('|', 1)
    assert prefix == '{{original_prompt}}' and json.loads(raw)['prompt'] == text


def test_only_kept_knowledge_is_rendered(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['rag']['enabled'] = True
    s0 = view(config)
    pack = {'facts': [{'value': 'allowed', 'bound_entity': 'e1'}], 'images': [
            {'image_id': 'unretained-reference', 'channel': 'attach'}],
            'left_out': [{'value': 'forbidden-reference-value'}]}
    rendered = render_brief(s0, plan(ir()), {'pack': pack}, b'{{data}}')
    assert json.loads(rendered)['knowledge']['facts'][0]['value'] == 'allowed'
    assert json.loads(rendered)['knowledge']['images'] == []
    assert 'forbidden-reference-value' not in rendered
    assert json.dumps(pack['facts'][0], ensure_ascii=False) in [line.rstrip(',') for line in rendered.splitlines()]


def test_manifest_output_resolution_rejects_invalid_payload(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    s0 = view(config)
    s0.profile.manifest['output_schema_id'] = 'verifier_result.v0'
    client = WriterDouble({'wrong': 'shape'})
    result = asyncio.run(write(s0, plan(ir()), None, client, project_root=project_root))
    assert result['state'] == 'failed' and result['error']['code'] == 'SCHEMA_INVALID'


@settings(max_examples=60, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',))))
def test_brief_untrusted_data_property(text):
    config = {'creativity': {'mode': text}, 'rag': {'enabled': False}, 'vlm': {'roles': {'writer': {'prompt_id': 'brief_v0'}}}}
    s0 = view(config, text)
    # Rendering is pure: the explicit brief template separates instructions from serialized data.
    intent = ir()
    intent['literal_texts'] = [{'id': 'literal', 'text': text, 'span': text}]
    intent['concept_links'] = [{'mention': text}]
    rendered = render_brief(s0, plan(intent), None, b'Brief\n{{data}}')
    data = json.loads(rendered.split('\n', 1)[1])
    assert data['prompt'] == text and data['creativity']['mode'] == text
    assert data['intent']['literal_texts'][0]['text'] == text
    assert data['intent']['concept_links'][0]['mention'] == text
    assert 'knowledge' not in data


@pytest.mark.parametrize('status', ['INCOMPLETE_OR_REFUSED', 'HTTP_ERROR', 'SCHEMA_INVALID'])
def test_failed_call_never_returns_draft(model_test_profile, tmp_path, project_root, status):
    config = setup(model_test_profile, tmp_path, project_root)
    client = WriterDouble('must not escape', status)
    result = asyncio.run(write(view(config), plan(ir()), None, client, project_root=project_root))
    assert result['state'] == 'failed' and result['output'] is None
    assert len(client.calls) == 1


def test_addendum_reaches_client_separately(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    client = WriterDouble()
    result = asyncio.run(write(view(config, addendum={'text': b'addendum'}), plan(ir()), None, client,
                              project_root=project_root))
    assert client.calls[0][0] == b'Admin\r\n'
    assert client.calls[0][2]['addendum'] == b'addendum'
    assert result['addendum_hash'] is not None


def test_edit_source_and_verbatim_literal_data(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    s0 = view(config, '名 "café"', task='edit')
    intent = ir(task='edit')
    intent['literal_texts'] = [{'id': 'l1', 'text': 'café', 'span': '"café"'}]
    client = WriterDouble()
    asyncio.run(write(s0, plan(intent), None, client, project_root=project_root))
    assert client.calls[0][2]['images'] == (('source_image', b'fixture image'),)
    assert 'café' in client.calls[0][1] and 'whole_edited_image' in client.calls[0][1]


@pytest.mark.parametrize('key,value', [('system_prompt_verbatim', False), ('knowledge_block', 'instructions')])
def test_writer_policy_refusal(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    config['writer'][key] = value
    with pytest.raises(ConfigurationError, match='writer.' + key):
        validate_writer_reads(config, project_root=project_root)


def test_unknown_profile_check_refused_before_call(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    s0, client = view(config), WriterDouble()
    s0.profile.validator_checks = ('unresolved',)
    with pytest.raises(ProfileError, match='VALIDATOR'):
        asyncio.run(write(s0, plan(ir()), None, client, project_root=project_root))
    assert client.calls == []


def test_best_effort_degraded_plan_still_writes(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    intent = ir()
    intent['needs_clarification'] = True
    client = WriterDouble()
    result = asyncio.run(write(view(config), plan(intent, state='degraded'), None, client, project_root=project_root))
    assert result['state'] == 'draft' and len(client.calls) == 1


def test_text_only_writer_role_is_generate_only(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['vlm']['roles']['writer']['image'] = 'none'
    client = WriterDouble()
    result = asyncio.run(write(view(config), plan(ir()), None, client, project_root=project_root))
    assert result['state'] == 'draft' and client.calls[0][2]['images'] == ()
    with pytest.raises(ConfigurationError, match='writer.image'):
        asyncio.run(write(view(config, task='edit'), plan(ir(task='edit')), None, client, project_root=project_root))
    assert len(client.calls) == 1


def test_disabled_required_addendum_is_refused_before_call(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['writer']['runtime_addendum'] = False
    client = WriterDouble()
    with pytest.raises(ConfigurationError, match='required addendum cannot be disabled'):
        asyncio.run(write(view(config, addendum={'text': b'data'}), plan(ir()), None, client,
                          project_root=project_root))
    assert client.calls == []
