"""Every card/role leaf has an observable override or an explicit singleton refusal."""

from copy import deepcopy
import json

import pytest
import yaml

from k3_fixtures import CardServer, checkpoint_rows, inputs, invoke_cards
from test_k2_config import ROLE_OVERRIDES, set_key
from test_k2_observe_shared_config import B, OVERRIDES as SHARED
from prompt_enhancer.config import ConfigurationError, load_config, validate_card_reads

PREFIX = 'vlm.roles.card_builder.'
CARD_KEYS = ['cards.stability_policy.labels', 'cards.stability_policy.version', 'cards.min_visible_images',
             'cards.low_diversity', 'cards.low_diversity_max_group_ratio', 'cards.variant_groups', 'cards.lint_report']
ROLE_KEYS = [PREFIX + field for field in [*ROLE_OVERRIDES, 'mode', 'image',
                                         'output.transport', 'output.validate_response']]
SHARED_OVERRIDES = {key: value for key, value in SHARED.items() if not key.startswith('vision.')}
SHARED_KEYS = [*SHARED_OVERRIDES, B + 'provider', B + 'api_key_file', B + 'unsupported_param_policy',
               'vlm.default_backend', 'storage.kb_store', 'storage.data_root', 'storage.lock.timeout_s',
               'storage.lock.poll_s', 'storage.table_format', 'sync.resume', 'knowledge.root', 'observe.facet_schema_version']
READ_KEYS = CARD_KEYS + ROLE_KEYS + SHARED_KEYS


@pytest.mark.parametrize('key', READ_KEYS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'wrong_type'])
def test_each_card_read_leaf_refuses_before_activity(k2_env, key, failure):
    changed = deepcopy(k2_env.config)
    set_key(changed, key, 'design_required' if failure == 'sentinel' else {'invalid': True}, remove=failure == 'missing')
    with pytest.raises(ConfigurationError):
        validate_card_reads(changed, project_root=k2_env.root)
    assert not k2_env.store.root.exists()


@pytest.mark.parametrize('key', CARD_KEYS + ROLE_KEYS)
def test_loader_card_leaves_refuse_missing_and_sentinel(k2_env, key):
    for missing in (False, True):
        changed = deepcopy(k2_env.config)
        set_key(changed, key, 'design_required', remove=missing)
        path = k2_env.root / 'card-test-profile.yaml'
        path.write_text(yaml.safe_dump(changed), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb sync', project_root=k2_env.root)


@pytest.mark.parametrize(('field', 'value'), ROLE_OVERRIDES.items())
def test_card_role_override_observed(k2_env, field, value):
    data = inputs(k2_env)
    server = CardServer()
    invoke_cards(k2_env, data, server)
    original = checkpoint_rows(k2_env, 'k3_grouping')[0]['call_records'][0]
    if field == 'backend':
        k2_env.config['vlm']['backends']['alternate'] = deepcopy(k2_env.config['vlm']['backends']['main'])
        k2_env.config['vlm']['backends']['alternate']['model']['id'] = 'alternate-card-model'
    elif field == 'prompt_id':
        (k2_env.root / 'prompts/alternate_prompt.txt').write_bytes(
            (k2_env.root / 'prompts/card_builder_v0.txt').read_bytes() + b'\nTest-only prompt revision.\n')
    elif field == 'output.schema_id':
        schema = json.loads((k2_env.root / 'contracts/schemas/card_builder_reply.v0.schema.json').read_bytes())
        schema['$id'] = value
        (k2_env.root / f'contracts/schemas/{value}.schema.json').write_text(json.dumps(schema), encoding='utf-8')
    set_key(k2_env.config, PREFIX + field, value)
    result = invoke_cards(k2_env, data, server)
    assert not result['report']['failed'] and len(server.requests) == 2
    record = max(checkpoint_rows(k2_env, 'k3_grouping'), key=lambda row: row['recorded_at'])['call_records'][0]
    assert record['request_hash'] != original['request_hash']
    body = server.requests[-1]
    if field in ('backend', 'prompt_id'):
        assert record[field] == value
    elif field == 'max_tokens':
        assert body['max_tokens'] == value
    elif field.startswith('sampling.'):
        name = field.split('.')[1]
        if name == 'repetition_penalty':
            assert ['PARAM_UNSUPPORTED', field] in record['warnings']
        else:
            assert body[name] == value
    elif field == 'reasoning.mode':
        assert body['chat_template_kwargs']['enable_thinking'] is True
    elif field == 'reasoning.effort':
        assert body['reasoning_effort'] == value
    elif field == 'reasoning.budget_tokens':
        assert ['PARAM_UNSUPPORTED', field] in record['warnings']
    elif field == 'reasoning.template_kwargs_key':
        assert body['chat_template_kwargs'] == {'test_thinking': False}
    elif field == 'output.format':
        assert body['response_format']['type'] == 'json_object'
    elif field == 'output.schema_id':
        assert record['schema_id'] == value
    elif field == 'output.strict':
        assert body['response_format']['json_schema']['strict'] is False


@pytest.mark.parametrize(('key', 'value'), SHARED_OVERRIDES.items())
def test_card_backend_override_changes_only_semantic_cache_record(k2_env, key, value):
    data = inputs(k2_env)
    server = CardServer()
    before = invoke_cards(k2_env, data, server)
    set_key(k2_env.config, key, value)
    if key == B + 'http.backoff_initial_s':
        set_key(k2_env.config, B + 'http.backoff_max_s', 0.5)
    after = invoke_cards(k2_env, data, server)
    semantic = key.startswith((B + 'engine.', B + 'model.'))
    assert not after['report']['failed'] and len(server.requests) == (2 if semantic else 1)
    assert (before['artifacts'] != after['artifacts']) == semantic


@pytest.mark.parametrize(('key', 'value', 'message'), [
    ('cards.min_visible_images', 0, 'positive'),
    ('cards.low_diversity_max_group_ratio', -0.1, 'low_diversity_max_group_ratio'),
    ('cards.low_diversity_max_group_ratio', 1.1, 'low_diversity_max_group_ratio'),
    ('cards.low_diversity_max_group_ratio', True, 'low_diversity_max_group_ratio'),
    ('cards.low_diversity_max_group_ratio', float('nan'), 'low_diversity_max_group_ratio'),
    ('cards.low_diversity_max_group_ratio', float('inf'), 'low_diversity_max_group_ratio'),
    ('cards.stability_policy.version', 'invented', 'version'),
    ('cards.stability_policy.labels', ['stable'], 'labels'),
    ('cards.low_diversity', 'typo', 'low_diversity'),
    (PREFIX + 'image', {'source': 'knowledge_reference'}, 'image'),
    (PREFIX + 'output.transport', 'engine_native', 'transport'),
    (PREFIX + 'output.validate_response', False, 'validate_response'),
    (PREFIX + 'prompt_id', '../unsafe', 'prompt_id'),
    (PREFIX + 'output.schema_id', 'concept_card.v0', 'schema_id'),
])
def test_invalid_card_policies_refused(k2_env, key, value, message):
    set_key(k2_env.config, key, value)
    with pytest.raises(ConfigurationError, match=message):
        validate_card_reads(k2_env.config, project_root=k2_env.root)


def test_card_policy_overrides_change_labels_and_output(k2_env):
    data = inputs(k2_env, ('A', 'A', 'A'))
    server = CardServer()
    assert invoke_cards(k2_env, data, server)['rows']['cards'][0]['facets'][0]['stability'] == 'stable'
    k2_env.config['cards']['min_visible_images'] = 4
    assert invoke_cards(k2_env, data, server)['rows']['cards'][0]['facets'][0]['stability'] == 'stable_low_diversity'
    k2_env.config['cards']['min_visible_images'] = 2
    k2_env.config['cards']['low_diversity_max_group_ratio'] = 1.0
    assert invoke_cards(k2_env, data, server)['rows']['cards'][0]['facets'][0]['stability'] == 'stable_low_diversity'
    k2_env.config['cards']['lint_report'] = False
    result = invoke_cards(k2_env, data, server)
    assert result['report']['lint_findings'] == [] and result['rows']['cards'][0]['lint_findings']
    data['observations'][0]['value'] = 'minority'
    k2_env.config['cards']['variant_groups'] = False
    card = invoke_cards(k2_env, data, server)['rows']['cards'][0]
    assert card['variant_groups'] == [] and len(card['facets'][0]['values']) == 2
    assert card['facets'][0]['stability'] == 'varies'


def test_default_backend_explicit_and_overlays_refuse_card_fit(k2_env):
    data = inputs(k2_env)
    server = CardServer()
    first = invoke_cards(k2_env, data, server)
    k2_env.config['vlm']['backends']['alternate'] = deepcopy(k2_env.config['vlm']['backends']['main'])
    k2_env.config['vlm']['default_backend'] = 'alternate'
    second = invoke_cards(k2_env, data, server)
    assert len(server.requests) == 1 and first['artifacts'] == second['artifacts']
    base = k2_env.root / 'card-base.yaml'
    base.write_text(yaml.safe_dump(k2_env.config), encoding='utf-8')
    for key, value in (('min_visible_images', 3), ('low_diversity_max_group_ratio', 0.2)):
        overlay = k2_env.root / 'card-overlay.yaml'
        overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': 'card-base.yaml',
                                           'cards': {key: value}}), encoding='utf-8')
        for path, selected in ((base, overlay), (overlay, None)):
            with pytest.raises(ConfigurationError, match='cards'):
                load_config(path, 'kb sync', overlay=selected, project_root=k2_env.root)
