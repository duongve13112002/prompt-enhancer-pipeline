"""P stage: parsing, registration, confirmation, conflict lint, golden check and the read API."""

import asyncio
import errno
import json
import os
import re
import shutil
import time

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from prompt_enhancer import profiles
from prompt_enhancer.config import COMMAND_READS, ConfigurationError, _parse_manifest
from prompt_enhancer.model_client import ModelCallError
from prompt_enhancer.profile_prompt import ProfileError, parse_meta, parse_structure, sha256_tag
from prompt_enhancer.profiles import check_profile, confirm_profile, register_profile, resolve_profile
from prompt_enhancer.storage import _anchored
from prompt_enhancer.storage._anchored import lock_file

from profile_fixtures import (EXAMPLE, ROOT, SECTIONED, DrafterDouble, RunnerDouble, bundle, config_data, draft,
                              example_draft, install_prompts, load, set_manifest, write_golden)


@pytest.fixture(autouse=True)
def prompts(project_root):
    install_prompts(project_root)


def run(coroutine):
    return asyncio.run(coroutine)


def register(tmp_path, project_root, profile_id='p1', prompt=SECTIONED, value=None, data=None):
    data = data or config_data(tmp_path)
    folder = bundle(tmp_path, profile_id, prompt)
    config = load(tmp_path, project_root, data, 'profile register')
    drafter = DrafterDouble(draft(profile_id=profile_id) if value is None else value)
    result = run(register_profile(config, profile_id, drafter, project_root=project_root))
    return folder, result, drafter


def activate(tmp_path, project_root, decision='profile_wins', data=None, **kwargs):
    folder, result, _ = register(tmp_path, project_root, data=data, **kwargs)
    config = load(tmp_path, project_root, data or config_data(tmp_path), 'profile check')
    ids = [conflict['id'] for conflict in result['manifest']['conflicts']]
    confirm_profile(config, 'p1', {cid: decision for cid in ids}, project_root=project_root)
    for cid in ids:
        write_golden(folder, f'g_{cid}', [cid])
    set_manifest(folder, golden_tests=[f'g_{cid}' for cid in ids] or ['g0'])
    if not ids:
        write_golden(folder, 'g0', [])
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    return folder, config


# Parsing ---------------------------------------------------------------------------------------------

def test_sectioned_split_keeps_meta_out_of_system_text():
    prompt = (EXAMPLE / 'system_prompt.txt').read_bytes()
    structure = parse_structure(prompt)
    system = structure.body(prompt, 'SYSTEM')
    assert structure.template_format == 'sectioned'
    assert system.startswith(b'You convert a natural-language user idea')
    assert b'[META]' not in system and b'thinking_mode' not in system and b'[USER]' not in system
    assert b'{{original_prompt}}' in structure.body(prompt, 'USER')
    meta = parse_meta(prompt, structure)
    assert meta['frozen'] is False and meta['thinking_mode'] == 'disabled' and 'description' in meta


def test_plain_prompt_is_whole_system_message(tmp_path, project_root):
    prompt = 'Write tags only.\r\nNo [SYSTEM] header here.\n'.encode('utf-8')
    value = draft(template_format='plain', template_variables=[], template_values={})
    del value['meta']
    folder, result, drafter = register(tmp_path, project_root, prompt=prompt, value=value)
    manifest = result['manifest']
    assert manifest['template_format'] == 'plain' and 'meta' not in manifest and not result['findings']
    assert parse_structure(prompt).template_format == 'plain'


@pytest.mark.parametrize('prompt', [
    b'[SYSTEM]\nA\n[SYSTEM]\nB\n[USER]\n{{x}}\n',
    b'preamble\n[SYSTEM]\nA\n[USER]\n{{x}}\n',
    b'[SYSTEM]\nA only\n',
    b'[SYSTEM]\nA\n[USER]\n{{x\n',
    b'[SYSTEM]\nA\n[USER]\n{{not a name}}\n',
    b'[SYSTEM]\nA\n[USER]\nx}}\n',
    b'[SYSTEM]\nA\n[USER]\n}} {{x}}\n',  # G2: a stray close before a placeholder
    b'[SYSTEM]\nA\n[USER]\n{{x}} }} {{y}}\n',  # G2: a stray close between placeholders
])
def test_malformed_sectioned_prompt_is_refused(tmp_path, project_root, prompt):
    with pytest.raises(ProfileError, match='PROFILE_PROMPT_INVALID'):
        register(tmp_path, project_root, prompt=prompt)
    assert not (tmp_path / 'profiles/p1/manifest.yaml').exists()


@pytest.mark.parametrize('meta', [b'- a\n- b\n', b'key: [1, 2]\n', b'when: !!timestamp 2026-01-01\n',
                                  b'a: 1\na: 2\n', b'a: null\n', b'a:\n', b'a: .inf\n', b'1: a\n', b'true: a\n'])
def test_meta_must_be_a_mapping_of_scalars(tmp_path, project_root, meta):
    prompt = b'[META]\n' + meta + b'[SYSTEM]\nA\n[USER]\n{{x}}\n'
    with pytest.raises(ProfileError, match='PROFILE_PROMPT_INVALID'):
        register(tmp_path, project_root, prompt=prompt)


@pytest.mark.parametrize(('text', 'value'), [
    ('yes', 'yes'), ('no', 'no'), ('on', 'on'), ('off', 'off'), ('True', 'True'), ('2026-01-01', '2026-01-01'),
    ('007', '007'), ('0x1F', '0x1F'), ('1_000', '1_000'), ('12:30', '12:30'), ("'true'", 'true'), ('"5"', '5'),
    ('true', True), ('false', False), ('0', 0), ('-12', -12), ('1.5', 1.5), ('.5', 0.5), ('1e3', 1000.0),
])
def test_meta_scalar_contract(tmp_path, project_root, text, value):
    """F3, PM decision pending user review: only YAML 1.2 core canonical booleans and decimal numbers are typed."""
    prompt = f'[META]\nlabel: {text}\n[SYSTEM]\nA\n[USER]\n{{{{x}}}}\n'.encode()
    meta = parse_meta(prompt, parse_structure(prompt))
    assert meta['label'] == value and type(meta['label']) is type(value)
    _, result, _ = register(tmp_path, project_root, prompt=prompt, value=draft(meta={'label': value},
                                                                              template_variables=['x'],
                                                                              template_values={}))
    stored = _parse_manifest((tmp_path / 'profiles/p1/manifest.yaml').read_bytes(), 'manifest.yaml')['meta']
    assert stored == {'label': value} and type(stored['label']) is type(value)  # the manifest keeps it as parsed
    assert yaml.safe_load('a: yes') == {'a': True}  # the global YAML 1.1 tables are unchanged


def test_non_utf8_prompt_is_refused(tmp_path, project_root):
    with pytest.raises(ProfileError, match='PROFILE_PROMPT_INVALID.*UTF-8'):
        register(tmp_path, project_root, prompt=b'[SYSTEM]\n\xff\n[USER]\n{{x}}\n')


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_byte_exact_round_trip(tmp_path, project_root, newline):
    text = 'Giữ nguyên 東京 Привет café — "q"  '.encode('utf-8')
    prompt = newline.join([b'\xef\xbb\xbf[META]', b'k: v', b'[SYSTEM]', text, b'', b'[USER]',
                           b'{{aspect_ratio}} / {{original_prompt}}', b''])
    folder, config = activate(tmp_path, project_root, prompt=prompt)
    assert (folder / 'system_prompt.txt').read_bytes() == prompt
    view = resolve_profile(config, 'p1', project_root=project_root)
    assert view.system_message == newline.join([text, b'', b''])
    assert view.user_template == b'{{aspect_ratio}} / {{original_prompt}}' + newline
    assert view.system_prompt_sha256 == sha256_tag(prompt)
    assert yaml.safe_load((folder / 'manifest.yaml').read_bytes())['system_prompt_hash'] == sha256_tag(prompt)


# Registration ---------------------------------------------------------------------------------------

def test_register_writes_draft_from_one_call(tmp_path, project_root):
    folder, result, drafter = register(tmp_path, project_root)
    assert len(drafter.calls) == 1
    system, user_text = drafter.calls[0]
    assert system == (ROOT / 'prompts/manifest_drafter_v0.txt').read_bytes()
    data = json.loads(user_text)
    assert data['system_prompt_text'] == SECTIONED.decode('utf-8')  # the prompt is one JSON string (R-026)
    assert data['structure']['template_variables'] == ['aspect_ratio', 'original_prompt']
    assert data['pipeline_policy']['configuration']['creativity.max_new_entities'] == 2
    written = yaml.safe_load((folder / 'manifest.yaml').read_bytes())
    assert written == result['manifest']
    assert written['status'] == 'draft' and written['system_prompt_hash'] == sha256_tag(SECTIONED)
    assert written['template_values'] == {'aspect_ratio': 'auto'}  # decision 81, from the draft
    assert (folder / 'system_prompt.txt').read_bytes() == SECTIONED


def test_code_owned_fields_and_drafted_decisions_are_reset(tmp_path, project_root):
    value = draft(profile_id='other', status='active', template_variables=['x'], golden_tests=['g'],
                  conflicts=[{'id': 'c1', 'profile_rule': 'r', 'conflicts_with': 'w', 'decision': 'config_wins',
                              'decided_by_admin': True}])
    _, result, _ = register(tmp_path, project_root, value=value)
    manifest = result['manifest']
    assert manifest['profile_id'] == 'p1' and manifest['status'] == 'draft' and manifest['golden_tests'] == []
    assert manifest['template_variables'] == ['aspect_ratio', 'original_prompt']
    assert manifest['conflicts'][0]['decision'] == 'undecided' and not manifest['conflicts'][0]['decided_by_admin']
    assert {'profile_id', 'status', 'golden_tests', 'template_variables', 'conflicts.c1.decision'} <= set(result['overrides'])


@pytest.mark.parametrize('value', [draft(output_kind=''), {'not': 'a manifest'}, ['list']])
def test_invalid_draft_is_refused(tmp_path, project_root, value):
    with pytest.raises(ProfileError, match='PROFILE_DRAFT_INVALID'):
        register(tmp_path, project_root, value=value)
    assert not (tmp_path / 'profiles/p1/manifest.yaml').exists()


@pytest.mark.parametrize(('error', 'record', 'code'), [
    (ModelCallError('BUDGET_EXHAUSTED: model request budget exhausted'), None, 'BUDGET_EXHAUSTED'),
    (ModelCallError('CAPABILITY_UNAVAILABLE: required capability is unproven'), None, 'CAPABILITY_UNAVAILABLE'),
    (None, {'status': 'SCHEMA_INVALID'}, 'SCHEMA_INVALID'),
    (None, {'status': 'HTTP_ERROR', 'http_status': 503}, 'HTTP_ERROR'),
])
def test_model_error_passes_through_its_code(tmp_path, project_root, error, record, code):
    bundle(tmp_path, 'p1', SECTIONED)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile register')
    drafter = DrafterDouble(None, record=record, error=error)
    with pytest.raises(ProfileError, match=f'PROFILE_DRAFT_FAILED: {code}') as raised:
        run(register_profile(config, 'p1', drafter, project_root=project_root))
    assert raised.value.context['model_code'] == code
    assert not (tmp_path / 'profiles/p1/manifest.yaml').exists()


def test_register_refuses_existing_manifest(tmp_path, project_root):
    folder, _, _ = register(tmp_path, project_root)
    before = (folder / 'manifest.yaml').read_bytes()
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile register')
    with pytest.raises(ProfileError, match='PROFILE_EXISTS'):
        run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))
    assert (folder / 'manifest.yaml').read_bytes() == before


def test_example_profile_registers_with_known_conflicts(tmp_path, project_root):
    data = config_data(tmp_path)
    data['profiles']['exclude'] = []
    folder = bundle(tmp_path, '_example', (EXAMPLE / 'system_prompt.txt').read_bytes())
    config = load(tmp_path, project_root, data, 'profile register')
    run(register_profile(config, '_example', DrafterDouble(example_draft()), project_root=project_root))
    manifest = yaml.safe_load((folder / 'manifest.yaml').read_bytes())
    assert [c['id'] for c in manifest['conflicts']] == ['c1', 'c2', 'c3', 'c4']
    assert manifest['conflicts'][0]['config_keys'] == ['creativity.allowed_additions', 'creativity.max_new_entities']
    assert manifest['template_variables'] == ['aspect_ratio', 'original_prompt']
    assert manifest['template_values'] == {'aspect_ratio': 'auto'}
    assert manifest['edit_output_semantics'] == 'whole_edited_image' and manifest['output_language'] == 'English'
    assert manifest['meta']['thinking_mode'] == 'disabled'
    check = load(tmp_path, project_root, data, 'profile check')
    confirm_profile(check, '_example', {'c1': 'config_wins', 'c2': 'config_wins', 'c3': 'profile_wins',
                                        'c4': 'profile_wins'}, project_root=project_root)
    write_golden(folder, 'g1', ['c1', 'c2', 'c3', 'c4'], prompt='một quán phở')
    write_golden(folder, 'g2', [], task='edit')
    set_manifest(folder, golden_tests=['g1', 'g2'])
    # T-015: the example's prose-only key-order/word-cap/bbox names have no confirmed parameters.
    runner = RunnerDouble()
    with pytest.raises(ProfileError, match='PROFILE_VALIDATOR_INVALID'):
        run(check_profile(check, '_example', runner, project_root=project_root))
    assert runner.calls == []
    # Explicit test-only, admin-confirmed subset for the existing template/addendum assertions below.
    set_manifest(folder, validator={'checks': ['single_line_minified_json']})
    raw = {'status': 'success', 'output': '{"text":"fixture"}', 'attachments': [], 'source_binding': None,
           'retrieval': {'state': 'disabled', 'snapshot_id': None}, 'warnings': [], 'trace_id': 'test'}
    run(check_profile(check, '_example', RunnerDouble(raw=raw), project_root=project_root))
    view = resolve_profile(check, '_example', project_root=project_root)
    assert view.input_variable == 'original_prompt' and view.template_values == {'aspect_ratio': 'auto'}
    assert b'[META]' not in view.system_message and view.has_known_conflict
    view.require_task('edit')
    entries = json.loads(view.addendum['text'].splitlines()[-1])
    assert [entry['conflict'] for entry in entries] == ['c1', 'c2']
    assert entries[1]['configuration'] == {'creativity.max_added_text_blocks': 0}
    assert (folder / 'system_prompt.txt').read_bytes() == (EXAMPLE / 'system_prompt.txt').read_bytes()


# Conflict lint, check and activation -----------------------------------------------------------------

@pytest.mark.parametrize(('decision', 'code'), [('profile_wins', None), ('config_wins', None),
                                                ('block_activation', 'PROFILE_CONFLICT_BLOCKS'),
                                                ('undecided', 'PROFILE_CONFLICT_UNDECIDED')])
def test_each_conflict_decision(tmp_path, project_root, decision, code):
    if code is None:
        folder, config = activate(tmp_path, project_root, decision)
        view = resolve_profile(config, 'p1', project_root=project_root)
        assert (view.addendum is None) == (decision == 'profile_wins')
        assert json.loads((folder / 'activation.json').read_bytes())['runner'] == RunnerDouble.identity
        return
    with pytest.raises(ProfileError, match=code):
        activate(tmp_path, project_root, decision)
    assert not (tmp_path / 'profiles/p1/activation.json').exists()


def test_unconfirmed_hand_decision_is_invalid(tmp_path, project_root):
    folder, result, _ = register(tmp_path, project_root)
    conflicts = result['manifest']['conflicts']
    conflicts[0]['decision'] = 'profile_wins'  # decided_by_admin stays false
    set_manifest(folder, status='confirmed', conflicts=conflicts, golden_tests=['g1'])
    write_golden(folder, 'g1', ['c1'])
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    with pytest.raises(ProfileError, match='PROFILE_CONFLICT_INVALID'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


def test_config_wins_requires_a_cited_key(tmp_path, project_root):
    value = draft(conflicts=[{'id': 'c1', 'profile_rule': 'r', 'conflicts_with': 'w', 'config_keys': [],
                              'decision': 'undecided', 'decided_by_admin': False}])
    register(tmp_path, project_root, value=value)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    with pytest.raises(ProfileError, match='PROFILE_CONFIRM_INVALID.*CONFLICT_KEY_REQUIRED'):
        confirm_profile(config, 'p1', {'c1': 'config_wins'}, project_root=project_root)


def test_config_wins_addendum(tmp_path, project_root):
    _, config = activate(tmp_path, project_root, 'config_wins')
    addendum = resolve_profile(config, 'p1', project_root=project_root).addendum
    template = (ROOT / 'prompts/runtime_addendum_v0.txt').read_bytes()
    assert addendum['id'] == 'runtime_addendum_v0' and addendum['text'].startswith(template)
    assert addendum['sha256'] == sha256_tag(addendum['text'])
    assert json.loads(addendum['text'][len(template):]) == [
        {'conflict': 'c1', 'profile_rule': 'Populate scenes', 'configuration': {'creativity.max_new_entities': 2}}]
    data = config_data(tmp_path)
    data['creativity']['max_new_entities'] = 5
    changed = load(tmp_path, project_root, data, 'profile check')
    with pytest.raises(ProfileError, match='PROFILE_NOT_CHECKED'):  # the cited-value fingerprint refuses first
        resolve_profile(changed, 'p1', project_root=project_root)


def test_addendum_template_drift_needs_new_check(tmp_path, project_root):
    _, config = activate(tmp_path, project_root, 'config_wins')
    path = project_root / 'prompts/runtime_addendum_v0.txt'  # session-shared: restored below
    original = path.read_bytes()
    path.write_bytes(original + b'changed\n')
    try:
        with pytest.raises(ProfileError, match='PROFILE_NOT_CHECKED.*addendum'):
            resolve_profile(config, 'p1', project_root=project_root)
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize('target', ['system_prompt.txt', 'manifest.yaml'])
def test_change_after_activation_needs_new_check(tmp_path, project_root, target):
    folder, config = activate(tmp_path, project_root)
    resolve_profile(config, 'p1', project_root=project_root)
    path = folder / target
    path.write_bytes(path.read_bytes() + b'\n# edited\n')
    with pytest.raises(ProfileError, match='PROFILE_NOT_CHECKED'):
        resolve_profile(config, 'p1', project_root=project_root)


def test_hash_drift_blocks_activation(tmp_path, project_root):
    folder, config = activate(tmp_path, project_root)
    (folder / 'system_prompt.txt').write_bytes(SECTIONED.replace(b'caption', b'captions'))
    with pytest.raises(ProfileError, match='PROFILE_HASH_MISMATCH'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    assert not (folder / 'activation.json').exists()
    with pytest.raises(ProfileError, match='PROFILE_NOT_ACTIVE'):
        resolve_profile(config, 'p1', project_root=project_root)


def test_edit_refused_on_generate_only_profile(tmp_path, project_root):
    _, config = activate(tmp_path, project_root)
    view = resolve_profile(config, 'p1', project_root=project_root)
    view.require_task('generate')
    with pytest.raises(ProfileError, match='TASK_UNSUPPORTED'):
        view.require_task('edit')
    with pytest.raises(ProfileError, match='INPUT_INVALID'):
        view.require_task('Edit')


def test_draft_is_not_checked(tmp_path, project_root):
    folder, _, _ = register(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    with pytest.raises(ProfileError, match='PROFILE_NOT_CONFIRMED'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    with pytest.raises(ProfileError, match='PROFILE_NOT_ACTIVE'):
        resolve_profile(config, 'p1', project_root=project_root)


def _confirmed(tmp_path, project_root):
    folder, _, _ = register(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    return folder, config


@pytest.mark.parametrize(('runner', 'code'), [
    (RunnerDouble(default='failed'), 'PROFILE_GOLDEN_FAILED'),
    (RunnerDouble(raw={'status': 'success'}), 'PROFILE_GOLDEN_FAILED'),
    (RunnerDouble(error=RuntimeError('boom')), 'PROFILE_GOLDEN_FAILED'),
])
def test_failing_golden_test_blocks_activation(tmp_path, project_root, runner, code):
    folder, config = _confirmed(tmp_path, project_root)
    write_golden(folder, 'g1', ['c1'])
    set_manifest(folder, golden_tests=['g1'])
    with pytest.raises(ProfileError, match=code):
        run(check_profile(config, 'p1', runner, project_root=project_root))
    results = json.loads((folder / 'golden_results.json').read_bytes())
    assert results['cases'][0]['passed'] is False and not (folder / 'activation.json').exists()


def test_failed_recheck_removes_previous_activation(tmp_path, project_root):
    folder, config = activate(tmp_path, project_root)
    with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
        run(check_profile(config, 'p1', RunnerDouble(default='degraded'), project_root=project_root))
    with pytest.raises(ProfileError, match='PROFILE_NOT_ACTIVE'):
        resolve_profile(config, 'p1', project_root=project_root)


@pytest.mark.parametrize(('setup', 'code'), [
    ('none', 'PROFILE_GOLDEN_MISSING'),
    ('uncovered', 'PROFILE_GOLDEN_UNCOVERED'),
    ('missing_file', 'PROFILE_GOLDEN_INVALID'),
    ('unknown_cover', 'PROFILE_GOLDEN_INVALID'),
    ('wrong_id', 'PROFILE_GOLDEN_INVALID'),
    ('id_path', 'PROFILE_GOLDEN_INVALID'),  # G3: the manifest schema accepts any non-empty id
    ('id_dots', 'PROFILE_GOLDEN_INVALID'),
    ('image_path', 'PROFILE_GOLDEN_INVALID'),  # G3: the golden schema accepts any non-empty image name
])
def test_golden_set_shape(tmp_path, project_root, setup, code):
    folder, config = _confirmed(tmp_path, project_root)
    if setup in ('id_path', 'id_dots'):
        set_manifest(folder, golden_tests=['a/b' if setup == 'id_path' else '..'])
    elif setup != 'none':
        set_manifest(folder, golden_tests=['g1'])
    if setup == 'image_path':
        write_golden(folder, 'g1', ['c1'], task='edit', source='../x.png')
    if setup == 'uncovered':
        write_golden(folder, 'g1', [])
    elif setup == 'unknown_cover':
        write_golden(folder, 'g1', ['c9'])
    elif setup == 'wrong_id':
        write_golden(folder, 'g2', ['c1'])
        shutil.move(folder / 'golden/g2.yaml', folder / 'golden/g1.yaml')
    with pytest.raises(ProfileError, match=code):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


def test_golden_source_image_reaches_runner(tmp_path, project_root):
    folder, config = _confirmed(tmp_path, project_root)
    write_golden(folder, 'g1', ['c1'], task='edit', source='src.png', expect=('failed',))
    (folder / 'golden/src.png').write_bytes(b'image-bytes')
    set_manifest(folder, golden_tests=['g1'])
    runner = RunnerDouble(default='failed')
    run(check_profile(config, 'p1', runner, project_root=project_root))
    view, request = runner.calls[0]
    assert request == {'task': 'edit', 'prompt': 'prompt for g1', 'source_image': b'image-bytes'}
    assert view.profile_id == 'p1' and view.activation is None


def test_template_must_leave_one_input_variable(tmp_path, project_root):
    value = draft(template_values={})
    _, result, _ = register(tmp_path, project_root, value=value)
    assert any(item[0] == 'TEMPLATE_INVALID' for item in result['findings'])
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    with pytest.raises(ProfileError, match='PROFILE_CONFIRM_INVALID.*exactly one variable'):
        confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)


# Configuration: one override and one refusal per key P reads -----------------------------------------

def test_read_sets():
    assert COMMAND_READS['profile register'] == ('profiles.root', 'profiles.exclude', 'creativity',
                                                 'vlm.roles.manifest_drafter', 'storage.lock')
    assert COMMAND_READS['profile check'] == ('profiles.root', 'profiles.exclude', 'storage.lock')


def test_profiles_root_override(tmp_path, project_root):
    data = config_data(tmp_path)
    data['profiles']['root'] = str(tmp_path / 'other_root')
    (tmp_path / 'other_root' / 'p1').mkdir(parents=True)
    (tmp_path / 'other_root' / 'p1' / 'system_prompt.txt').write_bytes(SECTIONED)
    config = load(tmp_path, project_root, data, 'profile register')
    run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))
    assert (tmp_path / 'other_root/p1/manifest.yaml').exists()
    assert not (tmp_path / 'profiles/p1/manifest.yaml').exists()


@pytest.mark.parametrize('command', ['profile register', 'profile check'])
@pytest.mark.parametrize('failure', ['missing', 'sentinel'])
@pytest.mark.parametrize('key', ['root', 'exclude'])
def test_profiles_key_refusal(tmp_path, project_root, command, failure, key):
    data = config_data(tmp_path)
    if failure == 'missing':
        del data['profiles'][key]
    else:
        data['profiles'][key] = 'deploy_required' if key == 'root' else ['deploy_required']
    with pytest.raises(ConfigurationError, match=f'profiles.{key}'):
        load(tmp_path, project_root, data, command)


def test_profiles_exclude_override_and_refusal(tmp_path, project_root):
    data = config_data(tmp_path)
    bundle(tmp_path, '_example', SECTIONED)
    config = load(tmp_path, project_root, data, 'profile register')
    with pytest.raises(ProfileError, match='PROFILE_EXCLUDED'):
        run(register_profile(config, '_example', DrafterDouble(draft()), project_root=project_root))
    data['profiles']['exclude'] = ['other']
    config = load(tmp_path, project_root, data, 'profile register')
    run(register_profile(config, '_example', DrafterDouble(draft()), project_root=project_root))
    assert (tmp_path / 'profiles/_example/manifest.yaml').exists()


def test_drafter_prompt_id_override_and_refusal(tmp_path, project_root):
    (project_root / 'prompts' / 'manifest_drafter_v9.txt').write_bytes(b'alternate drafter prompt\n')
    data = config_data(tmp_path)
    data['vlm']['roles']['manifest_drafter']['prompt_id'] = 'manifest_drafter_v9'
    _, _, drafter = register(tmp_path, project_root, data=data)
    assert drafter.calls[0][0] == b'alternate drafter prompt\n'
    data = config_data(tmp_path / 'second')
    data['vlm']['roles']['manifest_drafter']['prompt_id'] = 'manifest_drafter_missing'
    with pytest.raises(ConfigurationError, match='prompt_id'):
        register(tmp_path / 'second', project_root, data=data)


def test_drafter_role_refusal(tmp_path, project_root):
    data = config_data(tmp_path)
    data['vlm']['roles']['manifest_drafter']['max_tokens'] = 'design_required'
    with pytest.raises(ConfigurationError, match='vlm.roles.manifest_drafter.max_tokens'):
        load(tmp_path, project_root, data, 'profile register')
    data = config_data(tmp_path)
    data['vlm']['roles']['manifest_drafter']['output']['schema_id'] = 'intent_ir.v0'
    bundle(tmp_path, 'p1', SECTIONED)
    config = load(tmp_path, project_root, data, 'profile register')
    with pytest.raises(ConfigurationError, match='output.schema_id'):
        run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))


def test_creativity_override_reaches_drafter(tmp_path, project_root):
    data = config_data(tmp_path)
    data['creativity']['max_new_entities'] = 5
    _, _, drafter = register(tmp_path, project_root, data=data)
    policy = json.loads(drafter.calls[0][1])['pipeline_policy']['configuration']
    assert policy['creativity.max_new_entities'] == 5


@pytest.mark.parametrize('failure', ['missing', 'sentinel'])
def test_creativity_refusal(tmp_path, project_root, failure):
    data = config_data(tmp_path)
    if failure == 'missing':
        del data['creativity']
    else:
        data['creativity']['mode'] = 'design_required'
    with pytest.raises(ConfigurationError, match='creativity'):
        load(tmp_path, project_root, data, 'profile register')


@pytest.mark.parametrize(('key', 'detail'), [('creativity.no_such_key', 'missing key'),
                                             ('validation.verifier.sampled_fraction_for_rest', 'unresolved sentinel')])
def test_cited_key_refusal(tmp_path, project_root, key, detail):
    value = draft(conflicts=[{'id': 'c1', 'profile_rule': 'r', 'conflicts_with': 'w', 'config_keys': [key],
                              'decision': 'undecided', 'decided_by_admin': False}])
    folder, result, _ = register(tmp_path, project_root, value=value)
    assert any(item[0].startswith('CONFLICT_KEY_') for item in result['findings'])
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    confirm_profile(config, 'p1', {'c1': 'config_wins'}, project_root=project_root)
    with pytest.raises(ConfigurationError, match=f'conflicts.c1.config_keys: {key}: {detail}'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


def test_default_profile_id_override_and_refusal(tmp_path, project_root):
    data = config_data(tmp_path)
    activate(tmp_path, project_root, data=data)
    config = load(tmp_path, project_root, data, 'profile check')
    assert resolve_profile(config, project_root=project_root).profile_id == 'p1'
    data['profiles']['default_profile_id'] = 'p2'
    config = load(tmp_path, project_root, data, 'profile check')
    with pytest.raises(ProfileError, match="PROFILE_NOT_ACTIVE: 'p2'"):
        resolve_profile(config, project_root=project_root)
    data['profiles']['default_profile_id'] = 'deploy_required'
    config = load(tmp_path, project_root, data, 'profile check')
    with pytest.raises(ConfigurationError, match='profiles.default_profile_id'):
        resolve_profile(config, project_root=project_root)
    assert resolve_profile(config, 'p1', project_root=project_root).profile_id == 'p1'


@pytest.mark.parametrize('profile_id', ['..', 'a/b', 'CON', ''])
def test_invalid_profile_id_is_refused(tmp_path, project_root, profile_id):
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    with pytest.raises(ProfileError, match='PROFILE_ID_INVALID'):
        resolve_profile(config, profile_id, project_root=project_root)


# Fix round 1: publication, integrity and the check lock ----------------------------------------------

class PausedRunner(RunnerDouble):
    """Labelled test double: a runner that waits for the test before it answers."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started, self.release = asyncio.Event(), asyncio.Event()

    async def run(self, view, request):
        self.started.set()
        await self.release.wait()
        return await super().run(view, request)


def _checkable(tmp_path, project_root, decision='profile_wins', data=None):
    folder, _, _ = register(tmp_path, project_root, data=data)
    config = load(tmp_path, project_root, data or config_data(tmp_path), 'profile check')
    confirm_profile(config, 'p1', {'c1': decision}, project_root=project_root)
    write_golden(folder, 'g1', ['c1'])
    set_manifest(folder, golden_tests=['g1'])
    return folder, config


def _state(config, project_root):
    try:
        resolve_profile(config, 'p1', project_root=project_root)
        return 'ACTIVE'
    except ProfileError as error:
        return error.code


def test_newer_failed_check_leaves_profile_inactive(tmp_path, project_root):
    """F1: the reviewer's interleaving; the newer check waits for the older one, then its failure deactivates."""
    folder, config = _checkable(tmp_path, project_root)

    async def race():
        older, newer = PausedRunner(), PausedRunner(default='failed')
        first = asyncio.create_task(check_profile(config, 'p1', older, project_root=project_root))
        await older.started.wait()
        second = asyncio.create_task(check_profile(config, 'p1', newer, project_root=project_root))
        await asyncio.sleep(0.3)  # six lock polls; without the lock the newer runner starts at once
        assert not newer.started.is_set()
        older.release.set()
        await first
        assert _state(config, project_root) == 'ACTIVE'
        newer.release.set()
        with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
            await second
        assert newer.calls
    run(race())
    assert _state(config, project_root) == 'PROFILE_NOT_ACTIVE'
    assert json.loads((folder / 'golden_results.json').read_bytes())['cases'][0]['passed'] is False


def test_check_lock_wait_follows_storage_lock(tmp_path, project_root):
    """Override test for storage.lock under `profile check`: a held lock refuses after the configured wait."""
    data = config_data(tmp_path)
    data['storage']['lock'] = {'timeout_s': 0.4, 'poll_s': 0.1}
    folder, config = _checkable(tmp_path, project_root, data=data)
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    before = (folder / 'activation.json').read_bytes()
    holder = lock_file(folder.resolve(), 'check.lock', 1, 0.1)  # another process's check, by its OS lock
    assert holder.try_acquire()
    try:
        clock = time.monotonic()
        with pytest.raises(ProfileError, match='PROFILE_BUSY.*timeout_s=0.4'):
            run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
        assert time.monotonic() - clock >= 0.4
    finally:
        holder.release()
    assert (folder / 'activation.json').read_bytes() == before  # a refused check changes nothing


@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'poll_above_timeout'])
def test_check_lock_refusal(tmp_path, project_root, failure):
    data = config_data(tmp_path)
    if failure == 'missing':
        del data['storage']['lock']
    elif failure == 'sentinel':
        data['storage']['lock']['timeout_s'] = 'deploy_required'
    else:
        data['storage']['lock'] = {'timeout_s': 0.1, 'poll_s': 0.5}
    if failure != 'poll_above_timeout':
        with pytest.raises(ConfigurationError, match='storage.lock'):
            load(tmp_path, project_root, data, 'profile check')
        return
    config = load(tmp_path, project_root, data, 'profile check')
    with pytest.raises(ConfigurationError, match='storage.lock.poll_s must not exceed'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


_POSIX_LINKAT = pytest.mark.skipif(os.name == 'nt', reason='Windows publishes by a no-replace rename on the '
                                   "temporary's handle; the hard-link fallback is POSIX only (Linux CI)")


def _no_hard_links(calls):
    """Test double: `linkat` fails as on a file system without hard links."""
    def link(*args, **kwargs):
        calls.append(args)
        raise OSError(errno.EPERM, 'hard links unsupported (test double)')
    return link


@pytest.mark.parametrize('link', ['supported', pytest.param('unsupported', marks=_POSIX_LINKAT)])
def test_register_never_replaces_an_interposed_manifest(tmp_path, project_root, monkeypatch, link):
    """F2: an admin write between the early guard and publication is kept byte for byte."""
    admin = b'# the admin wrote this meanwhile\nstatus: confirmed\n'
    folder = bundle(tmp_path, 'p1', SECTIONED)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile register')
    real_publish, calls = _anchored._publish, []

    def interposed(*args):  # scheduling double: the admin writes after the temporary exists, before its commit
        (folder / 'manifest.yaml').write_bytes(admin)
        if link == 'unsupported':
            monkeypatch.setattr(_anchored.os, 'link', _no_hard_links(calls))
        return real_publish(*args)
    monkeypatch.setattr(_anchored, '_publish', interposed)
    with pytest.raises(ProfileError, match='PROFILE_EXISTS.*kept as written'):
        run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))
    assert (folder / 'manifest.yaml').read_bytes() == admin
    assert len(calls) == (link == 'unsupported')  # the fallback ran, and refused the existing name too
    # No temporary file is left; `check.lock` is the bundle lock register now holds (fix round 2).
    assert sorted(path.name for path in folder.iterdir()) == ['check.lock', 'manifest.yaml', 'system_prompt.txt']


@_POSIX_LINKAT
def test_register_publishes_without_hard_links(tmp_path, project_root, monkeypatch):
    """F2 fallback: anchored exclusive creation publishes the same bytes where `linkat` fails (forced)."""
    calls = []
    monkeypatch.setattr(_anchored.os, 'link', _no_hard_links(calls))
    folder, result, _ = register(tmp_path, project_root)
    assert calls  # the forced linkat failure was reached, so the bytes come from the fallback
    assert yaml.safe_load((folder / 'manifest.yaml').read_bytes()) == result['manifest']
    assert sorted(path.name for path in folder.iterdir()) == ['check.lock', 'manifest.yaml', 'system_prompt.txt']


def test_confirm_refuses_an_interposed_edit(tmp_path, project_root, monkeypatch):
    """Audit A7: a hand edit made while confirm works is refused, not overwritten."""
    folder, _, _ = register(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    edited = (folder / 'manifest.yaml').read_bytes() + b'# the admin edited this meanwhile\n'
    real = profiles.template_findings

    def interposed(*args):
        (folder / 'manifest.yaml').write_bytes(edited)
        return real(*args)
    monkeypatch.setattr(profiles, 'template_findings', interposed)
    with pytest.raises(ProfileError, match='PROFILE_CHANGED'):
        confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    assert (folder / 'manifest.yaml').read_bytes() == edited


@pytest.mark.parametrize('decision', ['profile_wins', 'config_wins'])
@pytest.mark.parametrize(('key', 'state'), [('max_new_entities', 'PROFILE_NOT_CHECKED'),
                                            ('max_added_text_blocks', 'ACTIVE')])
def test_cited_value_drift_needs_new_check(tmp_path, project_root, decision, key, state):
    """F4: a cited value change refuses for every decision; an uncited key leaves the read set narrow."""
    _, config = _checkable(tmp_path, project_root, decision)
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    data = config_data(tmp_path)
    data['creativity'][key] = 5
    assert _state(load(tmp_path, project_root, data, 'profile check'), project_root) == state
    assert _state(config, project_root) == 'ACTIVE'


def test_fingerprint_comes_from_one_snapshot(tmp_path, project_root):
    """F4: a configuration change made while the check runs does not enter the activation record."""
    _, config = _checkable(tmp_path, project_root)

    class Mutating(RunnerDouble):
        async def run(self, view, request):
            config['creativity']['max_new_entities'] = 9
            return await super().run(view, request)
    run(check_profile(config, 'p1', Mutating(), project_root=project_root))
    assert _state(config, project_root) == 'PROFILE_NOT_CHECKED'
    config['creativity']['max_new_entities'] = 2
    assert _state(config, project_root) == 'ACTIVE'


@pytest.mark.parametrize('tamper', ['case_changed', 'case_deleted', 'case_added', 'image_changed', 'image_deleted',
                                    'unrecorded'])
def test_golden_inputs_are_verified(tmp_path, project_root, tamper):
    """Part b (F5 class): a changed, deleted or added golden case or image after activation needs a new check."""
    folder, config = _checkable(tmp_path, project_root)
    write_golden(folder, 'g2', [], task='edit', source='src.png')
    (folder / 'golden/src.png').write_bytes(b'image-bytes')
    set_manifest(folder, golden_tests=['g1', 'g2'])
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    assert _state(config, project_root) == 'ACTIVE'
    activation = folder / 'activation.json'
    if tamper == 'case_changed':
        write_golden(folder, 'g1', ['c1'], expect=('success', 'degraded'))
    elif tamper == 'case_deleted':
        (folder / 'golden/g1.yaml').unlink()
    elif tamper == 'image_changed':
        (folder / 'golden/src.png').write_bytes(b'other-image-bytes')
    elif tamper == 'image_deleted':
        (folder / 'golden/src.png').unlink()
    elif tamper == 'case_added':
        write_golden(folder, 'g3', [])
        set_manifest(folder, golden_tests=['g1', 'g2', 'g3'])
        # Test-only forgery: re-record the new manifest hash, so only the golden fingerprint can refuse.
        record = json.loads(activation.read_bytes())
        record['manifest_sha256'] = sha256_tag((folder / 'manifest.yaml').read_bytes())
        activation.write_text(json.dumps(record), encoding='utf-8')
    else:
        record = json.loads(activation.read_bytes())
        del record['golden_inputs_sha256']
        activation.write_text(json.dumps(record), encoding='utf-8')
    assert _state(config, project_root) == 'PROFILE_NOT_CHECKED'


@pytest.mark.parametrize('tamper', ['replaced', 'deleted', 'hash_unrecorded', 'fingerprint_unrecorded'])
def test_activation_evidence_is_verified(tmp_path, project_root, tamper):
    """F5 and F4: missing or changed golden results, or a record without its hashes, are refused."""
    folder, config = _checkable(tmp_path, project_root)
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    results, activation = folder / 'golden_results.json', folder / 'activation.json'
    if tamper == 'replaced':
        results.write_bytes(results.read_bytes().replace(b'"passed": true', b'"passed": false'))
    elif tamper == 'deleted':
        results.unlink()
    else:
        record = json.loads(activation.read_bytes())
        del record['golden_results_sha256' if tamper == 'hash_unrecorded' else 'config_keys_sha256']
        activation.write_text(json.dumps(record), encoding='utf-8')
    assert _state(config, project_root) == 'PROFILE_NOT_CHECKED'


# Fix round 2: the three classes (activation dependencies, manifest fidelity, guarded writes) -----------

def _own_project(tmp_path):
    """A project root of this test alone, so a schema edit cannot reach another test (test-only)."""
    shutil.copytree(ROOT / 'contracts' / 'schemas', tmp_path / 'contracts' / 'schemas')
    install_prompts(tmp_path)
    return tmp_path


@pytest.mark.parametrize('schema', ['response_envelope.v0', 'golden_case.v0', 'profile_manifest.v0'])
def test_check_schema_drift_needs_new_check(tmp_path, schema):
    """RT4 class: every schema that decides the verdict is bound; changing one needs a new check."""
    root = _own_project(tmp_path)
    _, config = _checkable(tmp_path, root)
    run(check_profile(config, 'p1', RunnerDouble(), project_root=root))
    assert _state(config, root) == 'ACTIVE'
    path = root / 'contracts' / 'schemas' / f'{schema}.schema.json'
    value = json.loads(path.read_bytes())
    value['$comment'] = value.get('$comment', '') + ' (edited)'  # any byte change, even one no case notices
    path.write_text(json.dumps(value), encoding='utf-8')
    assert _state(config, root) == 'PROFILE_NOT_CHECKED'


def test_check_code_drift_needs_new_check(tmp_path, project_root, monkeypatch):
    """RT4 class: the check code and its libraries are bound by a fingerprint; another one needs a new check."""
    _, config = _checkable(tmp_path, project_root)
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    assert _state(config, project_root) == 'ACTIVE'
    monkeypatch.setattr(profiles, 'CHECK_CODE_SHA256', 'sha256:' + '0' * 64)  # test double: other code
    assert _state(config, project_root) == 'PROFILE_NOT_CHECKED'


@pytest.mark.parametrize('target', ['golden', 'schema'])
def test_input_changed_during_check_is_refused(tmp_path, target):
    """Guarded writes: the final comparison before activation covers golden inputs and schemas too."""
    root = _own_project(tmp_path)
    folder, config = _checkable(tmp_path, root)

    class Editing(RunnerDouble):
        async def run(self, view, request):
            if target == 'golden':
                write_golden(folder, 'g1', ['c1'], expect=('success', 'failed'))
            else:
                path = root / 'contracts/schemas/response_envelope.v0.schema.json'
                path.write_bytes(path.read_bytes() + b'\n')
            return await super().run(view, request)
    with pytest.raises(ProfileError, match='PROFILE_CHANGED'):
        run(check_profile(config, 'p1', Editing(), project_root=root))
    assert not (folder / 'activation.json').exists()


@pytest.mark.parametrize('order', ['meta_first', 'alias_first'])
def test_meta_alias_keeps_other_fields(order):
    """RT1: an alias that shares the META node keeps both fields, each read under its own contract."""
    if order == 'meta_first':
        raw = b'meta: &m {aspect_ratio: auto, n: 1e3}\ntemplate_values: *m\nlast: x\n'
    else:
        raw = b'template_values: &m {aspect_ratio: auto, n: 1e3}\nmeta: *m\nlast: x\n'
    parsed = _parse_manifest(raw, 'm')
    assert parsed['template_values'] == {'aspect_ratio': 'auto', 'n': '1e3'}  # YAML 1.1: no dot, a string
    assert parsed['meta'] == {'aspect_ratio': 'auto', 'n': 1000.0}  # META contract: a float
    expected = ['meta', 'template_values', 'last'] if order == 'meta_first' else ['template_values', 'meta', 'last']
    assert list(parsed) == expected


def test_aliased_manifest_activates(tmp_path, project_root):
    """RT1, the reviewer's full-profile probe: `template_values: *m` keeps its value through check."""
    folder, config = _checkable(tmp_path, project_root)
    manifest = yaml.safe_load((folder / 'manifest.yaml').read_bytes())
    manifest['meta'] = {'aspect_ratio': 'auto'}
    raw = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode('utf-8')
    raw = raw.replace(b'meta:\n  aspect_ratio: auto\n', b'meta: &m\n  aspect_ratio: auto\n')
    raw = raw.replace(b'template_values:\n  aspect_ratio: auto\n', b'template_values: *m\n')
    assert b'template_values: *m' in raw
    (folder / 'manifest.yaml').write_bytes(raw)
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    assert resolve_profile(config, 'p1', project_root=project_root).template_values == {'aspect_ratio': 'auto'}


@pytest.mark.parametrize('escape', ['\\N', '\\L', '\\P'])
def test_meta_line_break_escapes_survive_the_writer(escape):
    """RT2: an escaped NEL, LS or PS in [META] keys and values is written and read back unchanged."""
    raw = ('[META]\n"key' + escape + 'tail": "value' + escape + 'tail"\n[SYSTEM]\nS\n[USER]\n{{x}}\n').encode()
    meta = parse_meta(raw, parse_structure(raw))
    written = profiles._dump_manifest({'meta': meta})
    assert _parse_manifest(written, 'm')['meta'] == meta
    assert not any(ch in written.decode('utf-8') for ch in '\x85\u2028\u2029')  # escaped, never raw


@pytest.mark.parametrize('char', ['\x85', '\u2028', '\u2029'])
@pytest.mark.parametrize('where', ['manifest_value', 'manifest_comment', 'meta_section', 'golden_case'])
def test_raw_yaml_line_breaks_are_refused(tmp_path, project_root, char, where):
    """YAML 1.1 reads a raw NEL, LS or PS as a line break, YAML 1.2 as text: refused with its position."""
    if where == 'meta_section':
        raw = f'[META]\nlabel: a{char}b\n[SYSTEM]\nS\n[USER]\n{{{{x}}}}\n'.encode()
        with pytest.raises(ProfileError, match=rf'PROFILE_PROMPT_INVALID.*U\+{ord(char):04X} at line 1'):
            parse_meta(raw, parse_structure(raw))
        return
    if where == 'golden_case':
        folder, config = _checkable(tmp_path, project_root)
        path = folder / 'golden/g1.yaml'
        path.write_bytes(path.read_bytes() + f'# note{char}x\n'.encode())
        with pytest.raises(ProfileError, match=rf'PROFILE_GOLDEN_INVALID.*U\+{ord(char):04X}'):
            run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
        return
    text = f'a: 1\nb: "x{char}y"\n' if where == 'manifest_value' else f'a: 1\n# c{char}b: 1\n'
    with pytest.raises(ConfigurationError, match=rf'U\+{ord(char):04X} at line 2'):
        _parse_manifest(text.encode(), 'm')


@pytest.mark.parametrize(('raw', 'detail'), [
    (b'a: &t [*t]\n', 'recursive alias'),
    (b'meta: &t {a: *t}\n', 'recursive alias'),
    (b'a: "\\UFFFFFFFF"\n', 'escape'),
    (b'meta: {a: "\\U00110000"}\n', 'escape'),
    (b'a: !!int "1x"\n', 'invalid literal'),
    (b'a: ' + b'[' * 5000 + b']' * 5000 + b'\n', 'nested'),
])
def test_manifest_parser_refuses_precisely(raw, detail):
    """Inputs PyYAML accepts but cannot represent are a precise ConfigurationError, never another type."""
    with pytest.raises(ConfigurationError, match=f'm: file: .*{detail}'):
        _parse_manifest(raw, 'm')


def test_golden_case_parser_refuses_a_recursive_alias(tmp_path, project_root):
    folder, config = _checkable(tmp_path, project_root)
    path = folder / 'golden/g1.yaml'
    path.write_bytes(path.read_bytes() + b'extra: &t [*t]\n')
    with pytest.raises(ProfileError, match='PROFILE_GOLDEN_INVALID.*recursive alias'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


def test_confirm_refuses_an_edit_during_serialization(tmp_path, project_root, monkeypatch):
    """RT3: an edit made while confirm serializes is refused and kept; the bytes are computed before the guard."""
    folder, _, _ = register(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    path = folder / 'manifest.yaml'
    edited = path.read_bytes() + b'# admin edit during serialization\n'
    real, calls = profiles._dump_manifest, []

    def interposed(manifest):
        calls.append(manifest.get('status'))
        if manifest.get('status') == 'confirmed':  # the serialization of the confirmed manifest
            path.write_bytes(edited)
        return real(manifest)
    monkeypatch.setattr(profiles, '_dump_manifest', interposed)
    with pytest.raises(ProfileError, match='PROFILE_CHANGED'):
        confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    assert 'confirmed' in calls and path.read_bytes() == edited


def test_confirm_refuses_a_manifest_it_cannot_rewrite(tmp_path, project_root):
    """Fidelity: a hand-edited manifest (a comment here) is never rewritten, so nothing the admin wrote is lost."""
    folder, _, _ = register(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    path = folder / 'manifest.yaml'
    lines = path.read_bytes().split(b'\n')
    lines.insert(3, b'# the admin note')
    path.write_bytes(b'\n'.join(lines))
    before = path.read_bytes()
    with pytest.raises(ProfileError, match='PROFILE_CONFIRM_INVALID.*line 4'):
        confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    assert path.read_bytes() == before


def test_confirmed_manifest_can_be_confirmed_again(tmp_path, project_root):
    """The writer form is stable: what confirm writes, confirm accepts again (only the decision changes)."""
    folder, _, _ = register(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    first = (folder / 'manifest.yaml').read_bytes()
    confirm_profile(config, 'p1', {'c1': 'config_wins'}, project_root=project_root)
    second = (folder / 'manifest.yaml').read_bytes()
    assert first.replace(b'decision: profile_wins', b'decision: config_wins') == second


def test_writer_refuses_a_value_it_would_change(monkeypatch):
    """Fidelity guard: the writer reads its own bytes back and refuses any difference, typed values included."""
    monkeypatch.setattr(profiles, '_parse_manifest', lambda raw, path: {'a': 1})  # test double: a lossy reader
    with pytest.raises(ProfileError, match='PROFILE_MANIFEST_UNWRITABLE'):
        profiles._dump_manifest({'a': True})


def test_bundle_read_compares_types(tmp_path, project_root, monkeypatch):
    """`_read_bundle` refuses when the hashed bytes and the validated value differ, even only by type."""
    _, config = _checkable(tmp_path, project_root)
    real = profiles.load_profile

    def retyped(path, *, project_root):
        manifest, prompt = real(path, project_root=project_root)
        manifest['renderer']['accepts_reference_images'] = 0  # test double: a reread that changed false to 0
        return manifest, prompt
    monkeypatch.setattr(profiles, 'load_profile', retyped)
    with pytest.raises(ProfileError, match='PROFILE_CHANGED'):
        run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


@pytest.mark.parametrize('writer', ['register', 'confirm'])
def test_bundle_lock_serializes_writers(tmp_path, project_root, writer):
    """Override test for storage.lock under register and confirm: a held lock refuses after the configured wait."""
    data = config_data(tmp_path)
    data['storage']['lock'] = {'timeout_s': 0.4, 'poll_s': 0.1}
    folder = bundle(tmp_path, 'p1', SECTIONED)
    if writer == 'confirm':
        register(tmp_path, project_root, data=data)
    before = sorted(path.name for path in folder.iterdir())
    manifest = (folder / 'manifest.yaml').read_bytes() if writer == 'confirm' else None
    holder = lock_file(folder.resolve(), 'check.lock', 1, 0.1)
    assert holder.try_acquire()
    try:
        clock = time.monotonic()
        with pytest.raises(ProfileError, match='PROFILE_BUSY.*timeout_s=0.4'):
            if writer == 'register':
                config = load(tmp_path, project_root, data, 'profile register')
                run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))
            else:
                config = load(tmp_path, project_root, data, 'profile check')
                confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
        assert time.monotonic() - clock >= 0.4
    finally:
        holder.release()
    assert sorted(path.name for path in folder.iterdir()) == sorted(set(before) | {'check.lock'})
    if manifest is not None:
        assert (folder / 'manifest.yaml').read_bytes() == manifest


@pytest.mark.parametrize('writer', ['register', 'confirm'])
def test_bundle_lock_refusal(tmp_path, project_root, writer):
    """Refusal test for storage.lock under register and confirm: a sentinel is refused before any write."""
    data = config_data(tmp_path)
    if writer == 'confirm':
        folder, _, _ = register(tmp_path, project_root)
        before = (folder / 'manifest.yaml').read_bytes()
        config = load(tmp_path, project_root, data, 'profile check')
        config['storage']['lock']['timeout_s'] = 'deploy_required'  # as loaded for a command that does not read it
        with pytest.raises(ConfigurationError, match='storage.lock.timeout_s: unresolved sentinel'):
            confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
        assert (folder / 'manifest.yaml').read_bytes() == before
        return
    data['storage']['lock']['timeout_s'] = 'deploy_required'
    with pytest.raises(ConfigurationError, match='storage.lock'):
        load(tmp_path, project_root, data, 'profile register')


# --- T-012-fp (decision 124): the fingerprint hashes the normalized AST of config.py ---

_SYNTHETIC = '''"""Module doc."""
import os


class Box:
    """Class doc."""

    def get(self, value):
        """Function doc."""
        return value + 1  # trailing comment


def top(x):
    return x * 2
'''


def _config_source():
    from prompt_enhancer import config
    return config.__spec__.loader.get_data(config.__file__)


def _digest(text):
    return profiles._ast_digest(text.encode('utf-8'))


def test_fingerprint_hashes_config_ast_digest(monkeypatch):
    """The call site feeds `config.py` through the AST digest, not through a byte hash."""
    seen = []
    monkeypatch.setattr(profiles, '_ast_digest', lambda source: seen.append(source) or 'sha256:x')
    profiles._code_fingerprint()
    assert seen == [_config_source()]


def test_ast_digest_ignores_docstring_edit_of_config():
    source = _config_source().decode('utf-8').replace('\r\n', '\n')
    assert source.startswith('"""')
    edited = '"""A different module docstring."""' + source[source.index('"""', 3) + 3:]
    assert edited != source
    assert _digest(edited) == _digest(source)


def test_ast_digest_ignores_comment_edit_of_config():
    source = _config_source().decode('utf-8')
    assert _digest('# a new comment\n' + source + '\n# another comment\n') == _digest(source)


def test_ast_digest_ignores_whitespace_edit_of_config():
    source = _config_source().decode('utf-8').replace('\r\n', '\n')
    assert _digest('\n\n' + source.replace('\n', '\n\n\n', 1) + '\n\n') == _digest(source)


def test_ast_digest_sees_one_token_code_edit_of_config():
    source = _config_source().decode('utf-8')
    body = source.index('\nimport')  # past the module docstring
    match = re.search(r'\b\d+\b', source[body:])  # first integer literal in code
    edited = source[:body + match.start()] + str(int(match.group()) + 1) + source[body + match.end():]
    assert _digest(edited) != _digest(source)


@pytest.mark.parametrize('old, new', [('value + 1', 'value - 1'), ('x * 2', 'x * 3'), ('def top(', 'def top2('),
                                      ('import os', 'import sys')])
def test_ast_digest_sees_code_edits(old, new):
    assert _digest(_SYNTHETIC.replace(old, new, 1)) != _digest(_SYNTHETIC)


def test_ast_digest_ignores_class_and_function_docstring_edits():
    edited = _SYNTHETIC.replace('Class doc.', 'Other class doc.').replace('Function doc.', 'Other function doc.')
    assert _digest(edited) == _digest(_SYNTHETIC)


def test_ast_digest_keeps_non_string_leading_expression():
    """Only a string constant is a docstring; a leading number is code."""
    source = 'def f():\n    {}\n    return 1\n'
    assert _digest(source.format('5')) != _digest(source.format('6'))


def test_ast_digest_sees_docstring_replaced_by_code():
    assert _digest(_SYNTHETIC.replace('"""Function doc."""', 'print("Function doc.")')) != _digest(_SYNTHETIC)


@settings(max_examples=60, deadline=None)
@given(doc=st.text(alphabet=st.characters(blacklist_categories=('Cs', 'Cc'), blacklist_characters='"\\'), max_size=30),
       comment=st.text(alphabet=st.characters(blacklist_categories=('Cs', 'Cc')), max_size=20),
       blanks=st.integers(0, 3), literal=st.integers(1, 99), operator=st.sampled_from(['+', '-', '*']))
def test_ast_digest_property(doc, comment, blanks, literal, operator):
    """Comment, docstring and blank-line edits keep the digest; a changed literal or operator moves it."""
    base = f'"""d"""\n\ndef f(a):\n    """x"""\n    return a {operator} {literal}\n'
    noisy = (f'"""{doc}"""\n' + '\n' * blanks + f'# {comment}\ndef f(a):\n    """{doc}"""\n'
             f'    return a {operator} {literal}  # {comment}\n')
    assert _digest(noisy) == _digest(base)
    assert _digest(base.replace(f'{operator} {literal}', f'{operator} {literal + 100}')) != _digest(base)
    other = {'+': '-', '-': '*', '*': '+'}[operator]
    assert _digest(base.replace(f'a {operator} ', f'a {other} ')) != _digest(base)


# Decision 177: attaching knowledge images needs a renderer that accepts reference images ------------------

def edit_data(tmp_path, attach):
    data = config_data(tmp_path)
    data['edit'].update(attach_kb_reference_images=attach, max_kb_reference_images=2)  # test-only count
    return data


def renderer_draft(tasks, accepts):
    return draft(supported_tasks=tasks, edit_output_semantics='whole_edited_image' if 'edit' in tasks else None,
                 renderer={'accepts_reference_images': accepts, 'image_reference_syntax': 'none'})


@pytest.mark.parametrize('tasks', [['generate'], ['edit'], ['generate', 'edit']])
@pytest.mark.parametrize('accepts', [True, False])
@pytest.mark.parametrize('attach', [True, False])
def test_attaching_needs_a_renderer_that_accepts_reference_images(tmp_path, project_root, tasks, accepts, attach):
    refused = 'edit' in tasks and attach and not accepts
    data = edit_data(tmp_path, attach)
    if not refused:
        folder, config = activate(tmp_path, project_root, data=data, value=renderer_draft(tasks, accepts))
        assert resolve_profile(config, 'p1', project_root=project_root).supported_tasks == tuple(tasks)
        return
    with pytest.raises(profiles.AttachmentRendererMismatch) as error:
        activate(tmp_path, project_root, data=data, value=renderer_draft(tasks, accepts))
    message = str(error.value)
    assert isinstance(error.value, ConfigurationError) and message == profiles.AttachmentRendererMismatch.MESSAGE
    assert all(part in message for part in ('renderer.accepts_reference_images is false',
                                            'edit.attach_kb_reference_images is true', 'set '))
    assert str(project_root) not in message and str(tmp_path) not in message  # no file path, no profile id
    assert 'p1' not in message
    assert not (tmp_path / 'profiles' / 'p1' / 'activation.json').exists()  # a refused check activates nothing


def test_resolving_refuses_a_request_that_turns_attachments_on_for_such_a_profile(tmp_path, project_root):
    """The request's effective configuration (here a deep copy with the override applied) is checked on every resolve."""
    folder, config = activate(tmp_path, project_root, data=edit_data(tmp_path, False),
                              value=renderer_draft(['generate', 'edit'], False))
    assert resolve_profile(config, 'p1', project_root=project_root).profile_id == 'p1'
    effective = json.loads(json.dumps(config))
    effective['edit']['attach_kb_reference_images'] = True
    with pytest.raises(profiles.AttachmentRendererMismatch, match='renderer.accepts_reference_images is false but '
                                                                   'edit.attach_kb_reference_images is true'):
        resolve_profile(effective, 'p1', project_root=project_root)


def test_resolving_accepts_the_override_for_a_generate_only_profile(tmp_path, project_root):
    folder, config = activate(tmp_path, project_root, data=edit_data(tmp_path, False),
                              value=renderer_draft(['generate'], False))
    effective = json.loads(json.dumps(config))
    effective['edit']['attach_kb_reference_images'] = True
    assert resolve_profile(effective, 'p1', project_root=project_root).supported_tasks == ('generate',)


def test_the_example_profile_and_the_example_configuration_are_a_valid_pair():
    """The shipped example renderer accepts no images and the shipped configuration attaches none."""
    manifest = yaml.safe_load((EXAMPLE / 'manifest.yaml').read_bytes())
    config = yaml.safe_load((ROOT / 'configs' / 'enhancer.example.yaml').read_bytes())
    profiles._require_attachable_renderer(manifest, config)
    assert manifest['renderer']['accepts_reference_images'] is False
    assert config['edit']['attach_kb_reference_images'] is False
