"""Full-flow preflight/publication gates and structural diagnostics; labelled CPU doubles only."""

from copy import deepcopy
import json
import traceback

import pytest
import yaml

from prompt_enhancer.config import (
    ConfigurationError, MODEL_VISION_READS, _schema, _schema_at, load_config, validate_sync_config,
)
from prompt_enhancer.k0_sync import sync_config
from prompt_enhancer.k2_naming import _validate_reply, failure_code
from prompt_enhancer.kb_sync import require_success
from prompt_enhancer.storage import StorageError


@pytest.mark.parametrize('plan', [False, True])
@pytest.mark.parametrize('where', ['inside', 'outside'])
def test_disabled_sync_never_accesses_knowledge(k2_env, monkeypatch, capsys, plan, where):
    from prompt_enhancer import config as module
    from prompt_enhancer import kb_sync
    from prompt_enhancer import __main__ as cli
    cfg = deepcopy(k2_env.config)
    cfg['rag']['enabled'] = False
    cfg['embedding']['revision'] = 'pin_required'
    # decision 175: an external root, spelled absolutely, is never resolved, stat-ed or read with retrieval off either
    cfg['knowledge']['root'] = 'missing-knowledge' if where == 'inside' else str(k2_env.root.parent / 'no-external-kb')
    path = k2_env.root / 'disabled-sync.yaml'
    path.write_text(yaml.safe_dump(cfg), encoding='utf-8')

    def forbidden(*args, **kwargs):
        pytest.fail('disabled sync resolved/opened a knowledge path')

    monkeypatch.setattr(module, '_validate_configured_paths', forbidden)
    for owner in (module, kb_sync):  # labelled spies: the one root resolver and its physical step
        monkeypatch.setattr(owner, 'resolve_knowledge_root', forbidden)
    monkeypatch.setattr(module, '_physical', forbidden)
    resolve = module._resolve_within

    def checked_resolve(candidate, root, protected, path, key, **options):  # labelled spy; `links=` is passed through
        if key in {'knowledge.root', 'storage.kb_store', 'storage.data_root'}:
            forbidden()
        return resolve(candidate, root, protected, path, key, **options)

    monkeypatch.setattr(module, '_resolve_within', checked_resolve)
    import prompt_enhancer.storage as storage
    monkeypatch.setattr(storage, 'open_knowledge_store', forbidden)
    expected = {'status': 'skipped', 'reason': 'retrieval_disabled'}
    assert sync_config(cfg, project_root=k2_env.root, plan=plan) == expected
    loaded = load_config(path, 'kb sync', project_root=k2_env.root)
    assert loaded['embedding']['revision'] == 'pin_required'
    monkeypatch.setattr(cli, 'load_config', lambda *a, **k: loaded)
    assert cli.main(['kb', 'sync', '--config', 'disabled-sync.yaml'] + (['--plan'] if plan else [])) == 0
    assert json.loads(capsys.readouterr().out) == expected


def leaves(value, prefix=''):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from leaves(item, prefix + '.' + key if prefix else key)
    else:
        yield prefix


def schema_properties(node):
    properties = dict(node.get('properties', {}))
    for keyword in ('anyOf', 'oneOf', 'allOf'):
        for branch in node.get(keyword, []):
            properties.update(schema_properties(branch))
    return properties


def schema_leaves(node, prefix):
    properties = schema_properties(node)
    if properties:
        return {leaf for key, child in properties.items() for leaf in schema_leaves(child, prefix + '.' + key)}
    return {prefix}


def model_vision_groups(project_root):
    vision = _schema(project_root, 'enhancer_config.v0')['properties']['vision']
    return tuple('vision.' + key for key in schema_properties(vision) if key != 'max_decode_pixels')


def test_model_vision_inventory_covers_schema(project_root):
    schema = _schema(project_root, 'enhancer_config.v0')
    expected = schema_leaves(schema['properties']['vision'], 'vision') - {'vision.max_decode_pixels'}
    classified = {leaf for key in MODEL_VISION_READS for leaf in schema_leaves(_schema_at(schema, key), key)}
    assert classified == expected
    assert set(model_vision_groups(project_root)) == set(MODEL_VISION_READS)


def set_leaf(config, key, value, remove=False):
    parts = key.split('.')
    parent = config
    for part in parts[:-1]:
        parent = parent[part]
    if remove:
        del parent[parts[-1]]
    else:
        parent[parts[-1]] = value


@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'wrong_type'])
def test_each_sync_read_leaf_refused_before_k0(k2_env, monkeypatch, failure):
    from prompt_enhancer import k0_sync
    import prompt_enhancer.storage as storage
    cfg = deepcopy(k2_env.config)
    validate_sync_config(cfg, project_root=k2_env.root)

    def forbidden(*args, **kwargs):
        pytest.fail('activity before complete preflight')

    monkeypatch.setattr(k0_sync, 'scan_source', forbidden)
    monkeypatch.setattr(storage, 'open_knowledge_store', forbidden)
    import prompt_enhancer.kb_sync as coordinator
    monkeypatch.setattr(coordinator, 'open_knowledge_store', forbidden)
    # Independent schema inventory: S0's decode bound is not a model-stage read.
    groups = ('knowledge', 'sync', 'embedding', 'observe', 'cards', 'aliases', 'index',
              'storage.data_root', 'storage.kb_store', 'storage.table_format', 'storage.lock',
              'rag.enabled', 'runtime.cpu_workers', 'vlm.default_backend')
    groups += model_vision_groups(k2_env.root)
    groups += tuple('vlm.roles.' + role for role in
                    ('info_assessor', 'folder_namer', 'observer', 'card_builder', 'alias_generator'))
    groups += ('vlm.backends.main',)
    # K1 uses ModelTransport directly: no role parameter policy, probe, credential or local model-file reads.
    groups += tuple('vlm.backends.embedder.' + field for field in (
        'provider', 'engine', 'base_urls', 'endpoints.embeddings', 'model.id', 'model.revision',
        'max_concurrency', 'http', 'image.max_decode_pixels'))
    keys = []
    for group in groups:
        value = cfg
        for part in group.split('.'):
            value = value[part]
        keys.extend(leaves(value, group))
    path = k2_env.root / 'sync-refusal.yaml'
    for key in keys:
        changed = deepcopy(cfg)
        original = cfg
        for part in key.split('.'):
            original = original[part]
        invalid_type = [] if isinstance(original, dict) else {'invalid': True}
        set_leaf(changed, key, invalid_type if failure == 'wrong_type' else 'deploy_required', remove=failure == 'missing')
        if key == 'embedding.query_text_template' and failure == 'sentinel':
            # S1 owns the query template (T-013 P2): kb sync never reads it, so its sentinel is accepted,
            # while a missing or mistyped value still fails the whole-file schema check.
            validate_sync_config(changed, project_root=k2_env.root)
            path.write_text(yaml.safe_dump(changed), encoding='utf-8')
            load_config(path, 'kb sync', project_root=k2_env.root)
            continue
        with pytest.raises(ConfigurationError, match='.'):
            try:
                sync_config(changed, project_root=k2_env.root)
            except BaseException as error:
                error.add_note(key)
                raise
        path.write_text(yaml.safe_dump(changed), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='.'):
            load_config(path, 'kb sync', project_root=k2_env.root)


@pytest.mark.parametrize('partial', [False, True])
def test_require_success_refuses_partial_rows(partial):
    # K4 no longer goes through it (decision 165); the other model stages do, partial rows or not.
    result = {'report': {'failed': {'trusted-id': 'SCHEMA_INVALID'}},
              'rows': {'aliases': [{'origin': 'author'}, {'origin': 'generated'}] if partial else []}}
    with pytest.raises(StorageError, match='^k3: STAGE_FAILED: count=1; codes=SCHEMA_INVALID=1$'):
        require_success('k3', result)


@pytest.mark.parametrize('schema', ['info_assessment.v0', 'folder_naming.v0', 'observer_reply.v1',
                                   'card_builder_reply.v0', 'alias_generator_reply.v0'])
def test_model_boundary_errors_are_structural(project_root, schema):
    from k2_fixtures import assessment, naming
    from k2_observe_fixtures import observation
    from k3_fixtures import reply_for
    marker = 'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS'
    replies = {'info_assessment.v0': assessment(), 'folder_naming.v0': naming(),
               'observer_reply.v1': observation(), 'alias_generator_reply.v0': {'aliases': []},
               'card_builder_reply.v0': reply_for({'facets': [], 'observations': [], 'curated_statements': []})}
    value = replies[schema]
    _validate_reply(project_root, schema, value)
    value[marker] = marker  # the only violation must expose the model-provided key in jsonschema's message
    with pytest.raises(ValueError) as caught:
        _validate_reply(project_root, schema, value)
    assert marker not in ''.join(traceback.format_exception(caught.value))
    assert str(caught.value).startswith('SCHEMA_INVALID: ' + schema)
    assert failure_code(ValueError(marker)) == 'SCHEMA_INVALID'


def test_k4_diagnostics_persist_codes_only(k2_env):
    from k4_fixtures import AliasServer, inputs, invoke_aliases, states
    marker = 'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS'
    result = invoke_aliases(k2_env, inputs(), AliasServer({'aliases': marker}))
    assert set(result['report']['failed'].values()) == {'SCHEMA_INVALID'}
    for row in states(k2_env):
        assert marker not in json.dumps(row, default=str)
    with k2_env.store.writer() as writer:
        failures = [row for row in writer._state_rows().values() if row['stage'] == 'k4_call_failure']
        assert failures
        for row in failures:
            assert marker not in writer.checkpoints('k4_call_failure').load(row['input_key']).decode()


@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'wrong_type'])
def test_selected_backend_and_local_fallback_refused_before_k0(k2_env, monkeypatch, failure):
    import prompt_enhancer.kb_sync as coordinator
    cfg = deepcopy(k2_env.config)
    backend = deepcopy(cfg['vlm']['backends']['main'])
    cfg['vlm']['backends']['selected_local'] = backend
    cfg['vlm']['roles']['alias_generator']['backend'] = 'selected_local'
    validate_sync_config(cfg, project_root=k2_env.root)
    monkeypatch.setattr(coordinator, 'open_knowledge_store', lambda *a, **k: pytest.fail('store before preflight'))
    for name in ('selected_local', 'main'):
        for key in leaves(cfg['vlm']['backends'][name], 'vlm.backends.' + name):
            changed = deepcopy(cfg)
            original = cfg
            for part in key.split('.'):
                original = original[part]
            invalid_type = [] if isinstance(original, dict) else {'invalid': True}
            set_leaf(changed, key, invalid_type if failure == 'wrong_type' else 'deploy_required', remove=failure == 'missing')
            with pytest.raises(ConfigurationError):
                sync_config(changed, project_root=k2_env.root)


@pytest.mark.parametrize('provider', ['gemini', 'anthropic'])
def test_unverified_cloud_sync_refused_before_k0(k2_env, monkeypatch, provider):
    import prompt_enhancer.kb_sync as coordinator
    cfg = deepcopy(k2_env.config)
    backend = deepcopy(cfg['vlm']['backends']['main'])
    backend.update(provider=provider, engine={'name': provider}, base_urls=['https://example.invalid'],
                   endpoints={'chat': 'v1beta/models/synthetic:generateContent' if provider == 'gemini' else 'messages',
                              'health': 'health'},
                   api_key_file='.secrets/unused-synthetic.key', call_budget=3,
                   on_budget_exhausted='fallback_to_default_backend', allow_images=False)
    if provider == 'anthropic':
        backend.update(anthropic_version='2025-01-01', anthropic_workspace_id='none')
    cfg['vlm']['backends']['selected_cloud'] = backend
    cfg['vlm']['roles']['alias_generator']['backend'] = 'selected_cloud'
    monkeypatch.setattr(coordinator, 'open_knowledge_store', lambda *a, **k: pytest.fail('store before refusal'))
    with pytest.raises(ConfigurationError, match='cloud structured output mapping is not verified'):
        sync_config(cfg, project_root=k2_env.root)
