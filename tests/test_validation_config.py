"""S5 explicit read policy and eligibility; labelled calibration fixtures only."""
from copy import deepcopy
import asyncio
import json

import pytest

from prompt_enhancer.config import ConfigurationError, validate_validation_reads
from prompt_enhancer.verifier_policy import require_calibrated_verifier
from assembly_fixtures import entity, link
from test_writer_config import ROLE_OVERRIDES, BACKEND_OVERRIDES, assert_inference_override
from validation_fixtures import calibrate, environment, run


POLICY_KEYS = ['validation.structural_checks', 'validation.verifier.gating_rules', 'validation.verifier.inline_for',
               'validation.verifier.sampled_fraction_for_rest', 'validation.verifier.calibration_ref',
               'validation.max_repairs', 'validation.fallbacks', 'resolution.random_seed']
ROLE_KEYS = ['backend', 'mode', 'prompt_id', 'max_tokens', 'image', 'sampling.temperature', 'sampling.top_p',
             'sampling.top_k', 'sampling.min_p', 'sampling.frequency_penalty', 'sampling.presence_penalty',
             'sampling.repetition_penalty', 'sampling.seed', 'sampling.stop', 'reasoning.mode', 'reasoning.effort',
             'reasoning.budget_tokens', 'reasoning.template_kwargs_key', 'output.format', 'output.schema_id',
             'output.strict', 'output.transport', 'output.validate_response']


def set_key(config, key, value, remove=False):
    node = config
    parts = key.split('.')
    for part in parts[:-1]:
        node = node[part]
    if remove:
        del node[parts[-1]]
    else:
        node[parts[-1]] = value


@pytest.mark.parametrize('key', POLICY_KEYS + ['vlm.roles.verifier.' + leaf for leaf in ROLE_KEYS])
@pytest.mark.parametrize('failure', ['missing', 'sentinel'])
def test_each_s5_read_refuses(model_test_profile, tmp_path, project_root, key, failure):
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    set_key(env.config, key, 'fit_from_gt', remove=failure == 'missing')
    with pytest.raises(ConfigurationError):
        validate_validation_reads(env.config, project_root=project_root, require_verifier=True)


@pytest.mark.parametrize('key,value', [
    ('validation.structural_checks', False), ('validation.max_repairs', True), ('validation.max_repairs', -1),
    ('validation.verifier.sampled_fraction_for_rest', float('nan')),
    ('validation.verifier.gating_rules', ['unknown']), ('validation.verifier.gating_rules', ['task_is_edit'] * 2),
    ('validation.verifier.inline_for', ['unknown']), ('validation.fallbacks', ['invented']),
    ('validation.fallbacks', ['failed', 'failed']), ('vlm.roles.verifier.image', {}),
    ('vlm.roles.verifier.output.validate_response', False), ('vlm.roles.verifier.output.schema_id', 'intent_ir.v0'),
    ('vlm.roles.verifier.output.strict', False), ('vlm.roles.verifier.output.transport', 'prompt'),
    ('vlm.roles.verifier.mode', 'edit'), ('vlm.roles.verifier.output.format', 'none'),
    ('vlm.backends.check.unsupported_param_policy', 'ignore'),
])
def test_incompatible_s5_policy_refuses(model_test_profile, tmp_path, project_root, key, value):
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    set_key(env.config, key, value)
    with pytest.raises(ConfigurationError):
        validate_validation_reads(env.config, project_root=project_root, require_verifier=True)


def test_legacy_s5_requires_explicit_migration(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['schema_version'] = 1
    with pytest.raises(ConfigurationError, match='migrat'):
        validate_validation_reads(env.config, project_root=project_root)


def test_omitted_risk_policy_cannot_claim_semantic_pass(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    env.config['validation']['verifier'].update(gating_rules=[], inline_for=[])
    assert run(env)['status'] == 'degraded' and env.verifier.calls == []


@pytest.mark.parametrize('mode', ['id', 'source', 'file'])
def test_verifier_route_can_share_writer_artifact(model_test_profile, tmp_path, project_root, mode):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['vlm']['backends']['main']['model'][mode] = 'shared-artifact'
    env.config['vlm']['backends']['check']['model'][mode] = 'shared-artifact'
    calibrate(env)
    binding = require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    assert binding['writer_models'][0]['model'][mode] == binding['verifier_models'][0]['model'][mode]


@pytest.mark.parametrize('path', ['../outside.json', '.secrets/never-read.json', 'models/never-read.json'])
def test_calibration_paths_are_protected(model_test_profile, tmp_path, project_root, path):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['validation']['verifier']['calibration_ref'] = path
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)


def test_calibration_hash_bound_revision_override_and_missing_field(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    target = calibrate(env)
    first = require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    env.config['vlm']['backends']['check']['model']['revision'] = 'alternate-pin'
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    calibrate(env)
    second = require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    assert first != second
    value = json.loads(target.read_text('utf-8'))
    del value['human_checked']
    target.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)


def risk_environment(model_test_profile, tmp_path, project_root, risk):
    env = environment(model_test_profile, tmp_path, project_root, task='edit' if risk == 'task_is_edit' else 'generate')
    assembly = None
    if risk == 'profile_has_known_conflict':
        env.s0.profile.has_known_conflict = True
    if risk == 'ledger_entry_needs_meaning':
        env.s2['intent_ir']['constraint_ledger']['constraints'] = [dict(id='c1', type='style', text='kept',
                                                                      span='kept', bound_entity=None)]
    if risk == 'identity_facts_supplied':
        env.config['rag']['enabled'] = True
        env.s0.payload['retrieval'].update(state='pinned', snapshot_id='s1')
        env.s2['intent_ir']['entities'] = [entity('e1', 'kept')]
        env.s2['intent_ir']['concept_links'] = [link('concept:x', 'subject_identity', 'e1', mention='kept')]
        assembly = {'retrieval_state': 'retrieved', 'error': None,
                    'pack': {'request_id': 'r1', 'snapshot_id': 's1', 'images': [], 'left_out': [], 'selections': [],
                             'facts': [{'id': 'f1', 'concept_id': 'concept:x', 'facet': 'identity.hair', 'value': 'kept',
                                        'stability': 'curated', 'origin': 'info_file', 'bound_entity': 'e1', 'image_ids': []}]}}
    calibrate(env)
    return env, assembly


@pytest.mark.parametrize('risk', ['task_is_edit', 'identity_facts_supplied', 'profile_has_known_conflict',
                                 'ledger_entry_needs_meaning'])
def test_each_risk_trigger_override(model_test_profile, tmp_path, project_root, risk):
    env, assembly = risk_environment(model_test_profile, tmp_path, project_root, risk)
    env.config['validation']['verifier'].update(gating_rules=[risk], inline_for=[])
    assert run(env, assembly=assembly)['status'] == 'success'
    assert len(env.verifier.calls) == 1
    env.verifier.calls.clear()
    env.config['validation']['verifier']['gating_rules'] = []
    assert run(env, assembly=assembly)['status'] == 'degraded' and env.verifier.calls == []


@pytest.mark.parametrize('kind,risk', [('edit', 'task_is_edit'), ('identity_facts', 'identity_facts_supplied')])
def test_inline_for_is_additional_or_policy(model_test_profile, tmp_path, project_root, kind, risk):
    env, assembly = risk_environment(model_test_profile, tmp_path, project_root, risk)
    env.config['validation']['verifier'].update(gating_rules=[], inline_for=[kind])
    assert run(env, assembly=assembly)['status'] == 'success' and len(env.verifier.calls) == 1
    env.verifier.calls.clear()
    env.config['validation']['verifier']['gating_rules'] = [risk]
    assert run(env, assembly=assembly)['status'] == 'success' and len(env.verifier.calls) == 1


VERIFIER_OVERRIDES = {key: value for key, value in ROLE_OVERRIDES.items()
                      if not key.startswith('image.') and key != 'output.strict'}


@pytest.mark.parametrize('key,value', list(VERIFIER_OVERRIDES.items()))
def test_verifier_role_override_changes_actual_request(model_test_profile, tmp_path, project_root, key, value):
    from prompt_enhancer.model_client import CAPABILITIES, RoleClient
    from prompt_enhancer.model_probe import ProbeResult
    from prompt_enhancer.model_schemas import cached_registry
    from prompt_enhancer.profile_prompt import load_prompt
    from test_model_client import DoubleTransport
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    def call():
        transport = DoubleTransport('{"results":[]}')
        client = RoleClient(env.config, {'check': transport},
                            {'check': ProbeResult('complete', {cap: True for cap in CAPABILITIES}, {})},
                            cached_registry(project_root / 'contracts/schemas'), project_root=project_root)
        system = load_prompt(project_root, env.config['vlm']['roles']['verifier']['prompt_id'])
        _, record = asyncio.run(client.call('verifier', system, 'data'))
        assert transport.body['messages'][0]['content'].encode() == system
        return transport.body, record
    before, _ = call()
    set_key(env.config, 'vlm.roles.verifier.' + key, value)
    if key == 'prompt_id':
        (project_root / 'prompts/brief_alt.txt').write_bytes(b'Alternate verifier\n')
    validate_validation_reads(env.config, project_root=project_root, require_verifier=True)
    after, record = call()
    if key == 'prompt_id':
        assert after['messages'][0]['content'] == 'Alternate verifier\n'
        assert after['messages'][0] != before['messages'][0]
    else:
        record['warnings'] = [list(entry) for entry in record['warnings']]
        assert_inference_override(key, value, before, after, record)


@pytest.mark.parametrize('key,value', list(BACKEND_OVERRIDES.items()))
def test_verifier_backend_override_and_refusal(model_test_profile, tmp_path, project_root, key, value):
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    path = 'vlm.backends.check.' + key
    set_key(env.config, path, value)
    validate_validation_reads(env.config, project_root=project_root, require_verifier=True)
    for failure in ('missing', 'sentinel'):
        broken = deepcopy(env.config)
        set_key(broken, path, 'fit_from_gt', remove=failure == 'missing')
        with pytest.raises(ConfigurationError):
            validate_validation_reads(broken, project_root=project_root, require_verifier=True)


def test_seed_override_changes_monitoring_decisions(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['validation']['verifier']['sampled_fraction_for_rest'] = 0.5
    calibrate(env)
    choices = set()
    for seed in range(16):
        env.config['resolution']['random_seed'] = seed
        choices.add(run(env)['monitoring_selected'])
    assert choices == {False, True}


@pytest.mark.parametrize('key', ['vlm.default_backend', 'vlm.roles.verifier.backend'])
def test_verifier_backend_binding_override_and_missing(model_test_profile, tmp_path, project_root, key):
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    env.config['vlm']['backends']['alternate'] = deepcopy(env.config['vlm']['backends']['check'])
    set_key(env.config, key, 'alternate')
    validate_validation_reads(env.config, project_root=project_root, require_verifier=True)
    set_key(env.config, key, None, remove=True)
    with pytest.raises(ConfigurationError):
        validate_validation_reads(env.config, project_root=project_root, require_verifier=True)


@pytest.mark.parametrize('key', ['writer_models', 'verifier_models', 'writer_settings_hash', 'verifier_settings_hash',
    'writer_prompt_hash', 'verifier_prompt_hash', 'system_prompt_hash', 'addendum_hash', 'ledger_schema_hash',
    'evidence_ref', 'evidence_hash', 'human_checked'])
def test_each_calibration_binding_is_required(model_test_profile, tmp_path, project_root, key):
    env = environment(model_test_profile, tmp_path, project_root)
    path = calibrate(env)
    value = json.loads(path.read_text('utf-8'))
    del value[key]
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)


def test_calibration_ref_override_selects_evidence_file(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    path = calibrate(env)
    moved = path.with_name('different-calibration.json')
    moved.write_bytes(path.read_bytes())
    path.unlink()
    env.config['validation']['verifier']['calibration_ref'] = moved.name
    assert require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)['calibration_hash']
    env.config['validation']['verifier']['calibration_ref'] = path.name
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)


@pytest.mark.parametrize('key', ['provider', 'unsupported_param_policy'])
@pytest.mark.parametrize('failure', ['missing', 'sentinel'])
def test_verifier_backend_protocol_fields_required(model_test_profile, tmp_path, project_root, key, failure):
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    set_key(env.config, 'vlm.backends.check.' + key, 'fit_from_gt', remove=failure == 'missing')
    with pytest.raises(ConfigurationError):
        validate_validation_reads(env.config, project_root=project_root, require_verifier=True)


def test_verifier_engine_extras_change_calibration_and_cannot_override_messages(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    calibrate(env)
    env.config['vlm']['backends']['check']['extra_body'] = {'explicit_test_extension': 17}
    with pytest.raises(ConfigurationError):
        require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    calibrate(env)
    assert require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    for field in ('messages', 'system', 'systemInstruction', 'contents', 'model'):
        env.config['vlm']['backends']['check']['extra_body'] = {field: 'unsafe override'}
        with pytest.raises(ConfigurationError):
            validate_validation_reads(env.config, project_root=project_root, require_verifier=True)


def test_legacy_commands_load_without_injecting_new_settings(model_test_profile, tmp_path, project_root):
    import yaml
    from prompt_enhancer.config import load_config
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['schema_version'] = 1
    del env.config['validation']['verifier']['calibration_ref']
    del env.config['validation']['verifier']['require_calibration']
    for key in ('request_deadline_s', 'trace', 'monitoring'):
        del env.config['runtime'][key]
    target = tmp_path / 'legacy.yaml'
    target.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    loaded = load_config(target, 'kb status', project_root=project_root)
    assert loaded['schema_version'] == 1 and 'calibration_ref' not in loaded['validation']['verifier']
    assert not {'request_deadline_s', 'trace', 'monitoring'} & loaded['runtime'].keys()
    with pytest.raises(ConfigurationError, match='migrate'):
        load_config(target, 'enhance once', project_root=project_root)


@pytest.mark.parametrize('command', ['enhance once', 'enhance serve', 'eval run'])
@pytest.mark.parametrize('missing_legacy_field', [False, True])
def test_each_v2_command_refuses_legacy_before_versioned_reads(model_test_profile, tmp_path, project_root,
                                                              command, missing_legacy_field):
    import yaml
    from prompt_enhancer.config import load_config
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['schema_version'] = 1
    del env.config['validation']['verifier']['calibration_ref']
    del env.config['validation']['verifier']['require_calibration']
    for key in ('request_deadline_s', 'trace', 'monitoring'):
        del env.config['runtime'][key]
    if missing_legacy_field:
        del env.config['profiles']
    target = tmp_path / 'legacy-request.yaml'
    target.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='schema_version: migrate S5 configuration to version 2'):
        load_config(target, command, project_root=project_root)


def test_verifier_prompt_override_reaches_system_and_invalidates_calibration(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    env.config['vlm']['roles']['verifier']['prompt_id'] = 'verifier_alt'
    system = b'Alternate synthetic verifier\r\n'
    (project_root / 'prompts/verifier_alt.txt').write_bytes(system)
    assert run(env)['status'] == 'degraded' and env.verifier.calls == []
    calibrate(env)
    assert run(env)['status'] == 'success' and env.verifier.calls[0][0] == system


def test_writer_budget_fallback_can_be_the_verifier_model(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    cloud = deepcopy(env.config['vlm']['backends']['main'])
    cloud.update(provider='anthropic', engine={'name': 'anthropic'},
                 base_urls=['https://example.invalid'], endpoints={'chat': 'messages', 'health': 'health'},
                 call_budget=1, on_budget_exhausted='fallback_to_default_backend', allow_images=False,
                 api_key_file='.secrets/synthetic.key', anthropic_version='2025-01-01', anthropic_workspace_id='none')
    cloud['model']['id'] = 'synthetic-cloud-writer'
    env.config['vlm']['backends']['cloud-writer'] = cloud
    env.config['vlm']['roles']['writer'].update(backend='cloud-writer', image='none')
    env.config['vlm']['roles']['verifier']['backend'] = 'main'
    calibrate(env)
    binding = require_calibrated_verifier(env.config, env.s0.profile, project_root=project_root)
    assert binding['writer_models'][-1] == binding['verifier_models'][0]


@pytest.mark.parametrize('value', [None, 'false', 0, 'deploy_required', False, True])
def test_require_calibration_direct_and_loader(model_test_profile, tmp_path, project_root, value):
    import yaml
    from prompt_enhancer.config import load_config
    env = environment(model_test_profile, tmp_path, project_root)
    set_key(env.config, 'validation.verifier.require_calibration', value, remove=value is None)
    target = tmp_path / 'calibration-policy.yaml'
    target.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    readers = (lambda: validate_validation_reads(env.config, project_root=project_root),
               lambda: load_config(target, 'enhance once', project_root=project_root)['validation'])
    for read in readers:
        if type(value) is bool:
            assert read()['verifier']['require_calibration'] is value
        else:
            with pytest.raises(ConfigurationError, match='require_calibration'):
                read()
