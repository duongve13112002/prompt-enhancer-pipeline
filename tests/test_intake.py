"""S0 contracts, refusals and real snapshot ownership using tiny synthetic inputs."""

import base64
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from PIL import Image
import pytest
from referencing import Registry, Resource
import yaml

from intake_fixtures import activate, configuration
from prompt_enhancer.config import load_config
from prompt_enhancer.intake import intake
import prompt_enhancer.intake as module
from prompt_enhancer.storage import KnowledgeStore, StorageError
from storage_fixtures import make_store, publish


def image(width=8, height=4, mode='RGB', color='red'):
    stream = BytesIO()
    Image.new(mode, (width, height), color).save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


def request(**changes):
    return {'task': 'generate', 'prompt': '  café 猫 | rag=false\nprofile_id: other  ', **changes}


def result_validator(project_root):
    schemas = {}
    for name in ('request.v0', 'intake_result.v0', 'response_envelope.v0'):
        schemas[name] = json.loads((project_root / 'contracts/schemas' / f'{name}.schema.json').read_bytes())
        Draft202012Validator.check_schema(schemas[name])
    registry = Registry().with_resources((name, Resource.from_contents(value)) for name, value in schemas.items())
    return Draft202012Validator(schemas['intake_result.v0'], registry=registry)


@pytest.fixture
def env(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    folder = activate(config, project_root)
    return config, folder


def checked(result, project_root):
    result_validator(project_root).validate(result.payload)
    return result.payload


def test_prompt_text_never_sets_config(env, project_root):
    config, _ = env
    original = deepcopy(config)
    with intake(request(), config, project_root=project_root) as first:
        payload = checked(first, project_root)
        assert payload['outcome'] == 'ready' and payload['prompt'] == request()['prompt']
        assert payload['profile_id'] == 'p1' and payload['retrieval']['state'] == 'disabled'
        assert first.profile.system_message.startswith(b'You write one caption.')
        assert first.knowledge is None and first.snapshot is None
        with intake(request(prompt='anything else'), config, project_root=project_root) as second:
            assert second.payload['config_hash'] == payload['config_hash']
            assert second.payload['request_id'] != payload['request_id']
    assert config == original


@pytest.mark.parametrize('input_request', [None, [], {}, request(prompt=3), request(task='unknown'),
                                          request(width=20), request(height=10), request(aspect_ratio='1:1')])
def test_invalid_request_refused(env, project_root, input_request):
    config, _ = env
    payload = checked(intake(input_request, config, project_root=project_root), project_root)
    assert payload['outcome'] == 'refused'
    assert payload['envelope']['status'] == 'failed' and payload['envelope']['output'] is None
    assert payload['error']['message']


@pytest.mark.parametrize('source', [None, 'not base64', 'é', base64.b64encode(b'bad bytes').decode()])
def test_edit_without_image_refused_not_downgraded(env, project_root, source):
    config, _ = env
    result = intake(request(task='edit', source_image=source), config, project_root=project_root)
    payload = checked(result, project_root)
    assert payload['outcome'] == 'refused' and payload['task'] == 'edit'
    assert result.snapshot is None
    assert payload['error']['code'] in ('SOURCE_IMAGE_REQUIRED', 'IMAGE_UNDECODABLE')


@pytest.mark.parametrize('change,code', [('inactive', 'PROFILE_NOT_ACTIVE'), ('stale', 'PROFILE_NOT_CHECKED'),
                                       ('excluded', 'PROFILE_EXCLUDED'), ('unsupported', 'TASK_UNSUPPORTED')])
def test_profile_refusals(env, project_root, change, code):
    config, folder = env
    if change == 'inactive':
        (folder / 'activation.json').unlink()
    elif change == 'stale':
        (folder / 'system_prompt.txt').write_bytes(b'changed by admin')
    elif change == 'excluded':
        config['profiles']['exclude'].append('p1')
    else:
        # A labelled unsupported-task double isolates the real require_task gate after resolution.
        from dataclasses import replace
        from prompt_enhancer.profiles import resolve_profile
        view = resolve_profile(config, project_root=project_root)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, 'resolve_profile', lambda *a, **k: replace(view, supported_tasks=('generate',)))
            payload = checked(intake(request(task='edit', source_image=image()), config,
                                     project_root=project_root), project_root)
        assert payload['error']['code'] == code and payload['task'] == 'edit'
        return
    payload = checked(intake(request(), config, project_root=project_root), project_root)
    assert payload['error']['code'] == code


def test_override_invalidates_cited_profile_config(env, project_root):
    config, _ = env
    config['request_overrides']['allowed_keys'] = ['creativity.max_new_entities']
    result = intake(request(overrides={'creativity.max_new_entities': 4}), config, project_root=project_root)
    assert checked(result, project_root)['error']['code'] == 'PROFILE_NOT_CHECKED'


def test_override_hash_equals_direct_edit(env, project_root):
    config, _ = env
    config['request_overrides']['allowed_keys'] = ['vision.max_image_side']
    original = deepcopy(config)
    req = request(overrides={'vision.max_image_side': 3})
    with intake(request(), config, project_root=project_root) as base:
        with intake(req, config, project_root=project_root) as overridden:
            direct = deepcopy(config)
            direct['vision']['max_image_side'] = 3
            with intake(request(), direct, project_root=project_root) as edited:
                for result in (base, overridden, edited):
                    assert checked(result, project_root)['outcome'] == 'ready'
                assert overridden.payload['effective_config'] == edited.payload['effective_config'] == direct
                assert overridden.payload['config_hash'] != base.payload['config_hash']
                assert overridden.payload['config_hash'] == edited.payload['config_hash']
    assert config == original and req['overrides'] == {'vision.max_image_side': 3}


def test_explicit_profile_selection_and_activation(env, project_root):
    config, _ = env
    second = activate(config, project_root, profile_id='p2')
    for selected in (None, 'p2'):
        with intake(request(profile_id=selected), config, project_root=project_root) as result:
            expected = 'p1' if selected is None else selected
            assert checked(result, project_root)['outcome'] == 'ready'
            assert result.payload['profile_id'] == result.profile.profile_id == expected
    (second / 'activation.json').unlink()
    for selected in ('p2', 'unknown'):
        payload = checked(intake(request(profile_id=selected), config, project_root=project_root), project_root)
        assert payload['error']['code'] == 'PROFILE_NOT_ACTIVE'
        assert payload['profile_id'] is None


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('invalid', ['request', 'override'])
def test_early_refusal_uses_base_retrieval(env, project_root, enabled, invalid):
    config, _ = env
    config['rag']['enabled'] = enabled
    req = request(task='invalid') if invalid == 'request' else request(overrides={'rag.enabled': not enabled})
    payload = checked(intake(req, config, project_root=project_root), project_root)
    expected = {'state': 'failed' if enabled else 'disabled', 'snapshot_id': None}
    assert payload['outcome'] == 'refused'
    assert payload['retrieval'] == payload['envelope']['retrieval'] == expected
    assert payload['effective_config'] is payload['config_hash'] is payload['profile_id'] is None
    assert payload['source_image'] is None and payload['warnings'] == []


@pytest.mark.parametrize('enabled', [False, True])
def test_late_refusal_uses_effective_retrieval(env, project_root, enabled):
    config, folder = env
    config['rag']['enabled'] = not enabled
    config['request_overrides']['allowed_keys'] = ['rag.enabled']
    (folder / 'activation.json').unlink()
    payload = checked(intake(request(overrides={'rag.enabled': enabled}), config,
                             project_root=project_root), project_root)
    assert payload['error']['code'] == 'PROFILE_NOT_ACTIVE'
    assert payload['effective_config']['rag']['enabled'] is enabled
    expected = {'state': 'failed' if enabled else 'disabled', 'snapshot_id': None}
    assert payload['retrieval'] == payload['envelope']['retrieval'] == expected


@pytest.mark.parametrize('changes', [
    {'task': 'SECRET PROMPT TEXT'}, {'profile_id': 'SECRET_PROMPT_TEXT'},
    {'overrides': {'SECRET PROMPT TEXT': 'private/value'}},
    {'overrides': {'vision.max_image_side': 'SECRET PROMPT TEXT'}},
    {'overrides': {'profiles.root': '../private/value'}},
])
def test_validation_refusal_diagnostics_are_structural(env, project_root, changes):
    config, _ = env
    config['request_overrides']['allowed_keys'] = ['vision.max_image_side', 'profiles.root']
    payload = checked(intake(request(**changes), config, project_root=project_root), project_root)
    assert payload['outcome'] == 'refused'
    assert payload['error']['message'] in ('intake refused (ConfigurationError)', 'intake refused (ProfileError)')


def test_request_validator_diagnostics_are_structural(project_root):
    with pytest.raises(module.ConfigurationError) as caught:
        module.validate_request(request(task='SECRET PROMPT TEXT'), project_root=project_root)
    assert str(caught.value) == 'request validation failed (ConfigurationError)'


@pytest.mark.parametrize('config', [None, {}, {'rag': {}}, {'rag': 'SECRET PROMPT TEXT'}])
def test_invalid_base_config_still_returns_refusal(project_root, config):
    payload = checked(intake(request(), config, project_root=project_root), project_root)
    assert payload['error'] == {'code': 'CONFIGURATION_INVALID', 'message': 'intake refused (ConfigurationError)'}
    assert payload['retrieval'] == payload['envelope']['retrieval'] == {'state': 'failed', 'snapshot_id': None}


@pytest.mark.parametrize('error,code', [
    (module._Refusal('INTAKE_DEADLINE', 'private/value'), 'INTAKE_DEADLINE'),
    (module.ProfileError('PROFILE_NOT_ACTIVE', 'private/value'), 'PROFILE_NOT_ACTIVE'),
    (module.ConfigurationError('private/value'), 'CONFIGURATION_INVALID'),
    (module.ImageRejected('private/value', 'test-only', {}), 'IMAGE_REJECTED'),
    (module.UndecodableImage('private/value'), 'IMAGE_UNDECODABLE'),
    (Image.DecompressionBombError('private/value'), 'IMAGE_UNDECODABLE'),
    (StorageError('private/value'), 'KNOWLEDGE_FAILED'),
    (ValueError('private/value'), 'INTAKE_FAILED'),
    (OSError(2, 'test-only failure', 'private/value'), 'INTAKE_FAILED'),
    (RuntimeError('private/value'), 'INTAKE_FAILED'),
])
def test_refusal_exception_classes_only(env, project_root, monkeypatch, error, code):
    config, _ = env

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(module, 'resolve_profile', fail)  # labelled failing profile-boundary double
    payload = checked(intake(request(), config, project_root=project_root), project_root)
    assert payload['error'] == {'code': code, 'message': f'intake refused ({type(error).__name__})'}
    assert payload['envelope']['status'] == 'failed'


@pytest.mark.parametrize('error_type', [OSError, RuntimeError])
@pytest.mark.parametrize('cleanup_error', [None, StorageError, RuntimeError])
def test_unexpected_failure_releases_pin(env, project_root, monkeypatch, error_type, cleanup_error):
    config, _ = env
    config['rag']['enabled'] = True
    store = make_store(project_root, Path(config['storage']['kb_store']))
    publish(store)
    pin, close = module.KnowledgeGateway.pin, module.KnowledgeGateway.close

    def fail_after_pin(self):
        pin(self)
        raise error_type('private/value')

    def fail_after_close(self):
        close(self)
        raise cleanup_error('private/cleanup')

    monkeypatch.setattr(module.KnowledgeGateway, 'pin', fail_after_pin)
    if cleanup_error:
        monkeypatch.setattr(module.KnowledgeGateway, 'close', fail_after_close)
    result = intake(request(), config, project_root=project_root)
    payload = checked(result, project_root)
    expected = f'intake refused ({error_type.__name__})'
    if cleanup_error:
        expected += f'; lease cleanup failed ({cleanup_error.__name__})'
    assert payload['error'] == {'code': 'INTAKE_FAILED', 'message': expected}
    assert payload['retrieval'] == payload['envelope']['retrieval'] == {'state': 'failed', 'snapshot_id': None}
    assert result.knowledge is None and result.snapshot is None
    assert not list((store.root / 'leases').glob('*/*.lease'))


def test_context_cleanup_note_is_structural():
    def close():
        raise StorageError('private/cleanup')

    result = module.IntakeResult({}, knowledge=SimpleNamespace(close=close))
    with pytest.raises(RuntimeError) as caught:
        with result:
            raise RuntimeError('test-only body failure')
    assert caught.value.__notes__ == ['intake lease cleanup failed (StorageError)']
    assert result.knowledge is None


@pytest.mark.parametrize('source', ['not base64', image()], ids=['undecodable', 'valid'])
def test_generate_image_refused_before_decode(env, project_root, monkeypatch, source):
    config, _ = env

    def forbidden(*args, **kwargs):
        pytest.fail('generate reached image decoding')

    for name in ('_image', 'configure_decode_backstop', 'prepare_vision_image_with_info'):
        monkeypatch.setattr(module, name, forbidden)
    payload = checked(intake(request(source_image=source), config, project_root=project_root), project_root)
    assert payload['error'] == {'code': 'SOURCE_IMAGE_UNEXPECTED', 'message': 'intake refused (_Refusal)'}
    assert payload['task'] == 'generate' and payload['source_image'] is None
    assert payload['effective_config'] is None


@pytest.mark.parametrize('with_source', [False, True])
def test_refusal_envelope_identity(env, project_root, monkeypatch, with_source):
    config, _ = env
    config['rag']['enabled'] = True
    config['runtime']['intake_deadline_s'] = 1
    clock = [0.0]  # labelled clock and UUID doubles identify the actual protocol inputs.
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex='generated-request-id'))

    def missing_snapshot(self):
        clock[0] = 2.0
        return None

    monkeypatch.setattr(module.KnowledgeGateway, 'pin', missing_snapshot)
    source = image()
    req = request(task='edit' if with_source else 'generate', source_image=source if with_source else None)
    payload = checked(intake(req, config, project_root=project_root), project_root)
    image_id = 'sha256:' + hashlib.sha256(base64.b64decode(source)).hexdigest()
    assert payload['request_id'] == 'generated-request-id'
    assert payload['prompt'] == req['prompt'] and payload['task'] == req['task']
    assert payload['profile_id'] == 'p1' and payload['effective_config'] == config
    assert payload['error']['code'] == 'INTAKE_DEADLINE'
    assert payload['envelope'] == {
        'status': 'failed', 'output': None, 'attachments': [],
        'source_binding': {'source_image_id': image_id} if with_source else None,
        'retrieval': {'state': 'failed', 'snapshot_id': None},
        'warnings': ['SNAPSHOT_MISSING'], 'trace_id': 'generated-request-id',
    }
    assert payload['warnings'] == ['SNAPSHOT_MISSING']
    if with_source:
        assert payload['source_image']['image_id'] == image_id
        assert payload['source_image']['role'] == 'source_image'
    else:
        assert payload['source_image'] is None


def test_image_resize_identity_and_encoding(env, project_root):
    config, _ = env
    config['request_overrides']['allowed_keys'] = ['vision.max_image_side', 'vision.format', 'vision.quality']
    original = image()
    with intake(request(task='edit', source_image=original,
                        overrides={'vision.max_image_side': 3, 'vision.format': 'jpeg', 'vision.quality': 40}),
                config, project_root=project_root) as result:
        payload = checked(result, project_root)
        assert payload['outcome'] == 'ready'
        prepared = payload['source_image']
        with Image.open(BytesIO(base64.b64decode(prepared['data']))) as decoded:
            assert decoded.size == (3, 1) and decoded.format == 'JPEG'
        with intake(request(task='edit', source_image=original), config, project_root=project_root) as second:
            assert second.payload['source_image']['image_id'] == prepared['image_id']
            assert second.payload['source_image']['data'] != prepared['data']


@pytest.mark.parametrize('policy', [{'mode': 'refuse'}, {'mode': 'flatten', 'background': '#123456'}])
def test_image_alpha_override(env, project_root, policy):
    config, _ = env
    config['vision']['alpha_policy'] = policy
    result = intake(request(task='edit', source_image=image(mode='RGBA', color=(0, 0, 0, 0))),
                    config, project_root=project_root)
    payload = checked(result, project_root)
    if policy['mode'] == 'refuse':
        assert payload['error']['code'] == 'IMAGE_REJECTED'
    else:
        with Image.open(BytesIO(base64.b64decode(payload['source_image']['data']))) as decoded:
            assert decoded.getpixel((0, 0)) == (18, 52, 86)


def test_decode_bound_changes_acceptance(env, project_root):
    config, _ = env
    config['request_overrides']['allowed_keys'] = ['vision.max_decode_pixels']
    result = intake(request(task='edit', source_image=image(), overrides={'vision.max_decode_pixels': 31}),
                    config, project_root=project_root)
    assert checked(result, project_root)['error']['code'] == 'IMAGE_REJECTED'
    result = intake(request(task='edit', source_image=image(), overrides={'vision.max_decode_pixels': 32}),
                    config, project_root=project_root)
    assert checked(result, project_root)['outcome'] == 'ready'


@pytest.mark.parametrize('edit,override', [(False, False), (True, False), (False, True), (True, True)])
def test_enhance_once_rag_off_is_structural(env, project_root, tmp_path, monkeypatch, edit, override):
    import prompt_enhancer.config as loader
    import prompt_enhancer.storage as storage

    config, _ = env
    config['knowledge']['root'] = str(tmp_path / 'absent-source')
    config['storage']['kb_store'] = str(tmp_path / 'absent-store')
    config['request_overrides']['allowed_keys'] = ['rag.enabled']
    path = tmp_path / 'off.yaml'
    # Loader off-path is audited too; the override variant starts from an in-memory on configuration.
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    absent = [str(tmp_path / name) for name in ('absent-source', 'absent-store')]
    events, active = [], [True]

    def audit(event, args):
        if active[0] and event in ('open', 'os.listdir', 'os.mkdir', 'os.scandir') and args:
            if any(name in str(args[0]) for name in absent):
                events.append((event, str(args[0])))

    def forbidden(*args, **kwargs):
        pytest.fail('knowledge constructor reached with retrieval disabled')

    original = loader._configured_path

    def guard(value, root, source, key, **kwargs):
        assert key not in ('knowledge.root', 'storage.kb_store')
        return original(value, root, source, key, **kwargs)

    monkeypatch.setattr(loader, '_configured_path', guard)
    monkeypatch.setattr(module, 'KnowledgeGateway', forbidden)
    monkeypatch.setattr(module, 'open_knowledge_store', forbidden)
    monkeypatch.setattr(storage, 'open_knowledge_store', forbidden)
    monkeypatch.setattr(KnowledgeStore, 'pin', forbidden)
    monkeypatch.setattr(KnowledgeStore, 'read_table', forbidden, raising=False)
    sys.addaudithook(audit)
    try:
        loaded = load_config(path, 'enhance once', project_root=project_root)
        if override:
            loaded['rag']['enabled'] = True
        req = request(task='edit' if edit else 'generate', source_image=image() if edit else None,
                      overrides={'rag.enabled': False} if override else {})
        with intake(req, loaded, project_root=project_root) as result:
            assert checked(result, project_root)['outcome'] == 'ready'
            assert result.knowledge is None
    finally:
        active[0] = False
    assert events == []
    assert not any(Path(name).exists() for name in absent)


def test_missing_snapshot_is_degraded(env, project_root):
    config, _ = env
    config['rag']['enabled'] = True
    with intake(request(), config, project_root=project_root) as result:
        payload = checked(result, project_root)
        assert payload['outcome'] == 'degraded' and result.snapshot is None
        assert payload['warnings'] == ['SNAPSHOT_MISSING']
        assert payload['retrieval'] == {'state': 'failed', 'snapshot_id': None}


def test_pin_once_survives_publish(env, project_root, monkeypatch):
    config, _ = env
    config['rag']['enabled'] = True
    store = make_store(project_root, Path(config['storage']['kb_store']))
    first_id = publish(store, 'first')
    counts = {'pin': 0, 'pointer': 0}
    pin, read = KnowledgeStore.pin, KnowledgeStore._read_current

    def counted_pin(self):
        counts['pin'] += 1
        return pin(self)

    def counted_read(self):
        counts['pointer'] += 1
        return read(self)

    monkeypatch.setattr(KnowledgeStore, 'pin', counted_pin)
    monkeypatch.setattr(KnowledgeStore, '_read_current', counted_read)
    monkeypatch.setattr(KnowledgeStore, 'current', lambda self: pytest.fail('extra current() read'))
    result = intake(request(), config, project_root=project_root)
    payload = checked(result, project_root)
    assert counts == {'pin': 1, 'pointer': 1}
    assert payload['retrieval'] == {'state': 'pinned', 'snapshot_id': first_id}
    snapshot = result.snapshot
    assert snapshot is not None
    assert publish(store, 'second') != first_id
    assert snapshot.read_file('index/names.txt') == b'index first'
    result.close()
    result.close()
    with pytest.raises(StorageError, match='released'):
        snapshot.read_file('index/names.txt')
    assert not list((store.root / 'leases').glob('*/*.lease'))


@pytest.mark.parametrize('operation', ['profile', 'image', 'pin'])
def test_intake_deadline_cleanup(env, project_root, monkeypatch, operation):
    config, _ = env
    config['rag']['enabled'] = True
    store = make_store(project_root, Path(config['storage']['kb_store']))
    publish(store)
    clock = [0.0]  # labelled monotonic-clock double: no timing assertion depends on machine load.
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    obj, name = {'profile': (module, 'resolve_profile'), 'image': (module, 'prepare_vision_image_with_info'),
                 'pin': (module.KnowledgeGateway, 'pin')}[operation]
    original = getattr(obj, name)

    def expires(*args, **kwargs):
        value = original(*args, **kwargs)
        clock[0] = 2.0
        return value

    config['runtime']['intake_deadline_s'] = 1
    monkeypatch.setattr(obj, name, expires)
    result = intake(request(task='edit', source_image=image()), config, project_root=project_root)
    assert checked(result, project_root)['error']['code'] == 'INTAKE_DEADLINE'
    assert result.snapshot is None
    assert not list((store.root / 'leases').glob('*/*.lease'))
