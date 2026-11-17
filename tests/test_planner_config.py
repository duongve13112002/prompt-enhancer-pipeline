"""Override and refusal tests for every key S2 reads, decision 144 and the role image sources (T-014)."""

from copy import deepcopy

import pytest

from planner_fixtures import planner_config
from prompt_enhancer.config import ConfigurationError, effective_request_config, validate_embedder_reads, \
    validate_planner_reads


def setting(config, key, value):
    node = config
    parts = key.split('.')
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = deepcopy(value)


ROLE = 'vlm.roles.planner'
CASES = [  # key, a non-default accepted value, refused values
    ('planner.second_chance_rounds', 3, [-1, True, 1.0, 'design_required']),
    ('planner.catalogue_in_context', False, [True, 'no', 'design_required']),
    ('resolution.on_family_match', 'omit', ['ask', 'context', 'design_required']),
    ('resolution.random_seed', 11, [-1, True, 'fresh', 1.5, 2 ** 63, 'design_required']),
    ('resolution.trace_choice', False, ['yes', 'design_required']),
    ('clarification.mode', 'ask', ['never', 'design_required']),
    ('edit.source_entity_linking', False, [True, 'design_required']),
    ('creativity.max_new_entities', 5, ['design_required']),
    ('edit.default_preservation.local_edit', 'preserve_subject_and_layout', ['design_required']),
    (ROLE + '.image', 'none', [{'source': 'source_image', 'max_side': 'from_vision', 'format': 'from_vision',
                                'quality': 80, 'transport': 'data_uri'}, 'design_required']),
    (ROLE + '.image.source', 'edit_source_only', ['knowledge_reference', 'edit_source_and_live_look', 'other']),
    (ROLE + '.output.schema_id', 'planner_reply.v0', ['intent_ir.v0']),
    (ROLE + '.output.format', 'json_schema', ['json_object']),
    (ROLE + '.output.transport', 'response_format', ['engine_native']),
    (ROLE + '.max_tokens', 64, ['design_required', 0]),
]


@pytest.mark.parametrize(('key', 'accepted', 'refused'), CASES, ids=[case[0] for case in CASES])
def test_planner_reads_override_and_refusal(model_test_profile, tmp_path, project_root, key, accepted, refused):
    config = planner_config(model_test_profile, tmp_path)
    setting(config, key, accepted)
    validate_planner_reads(config, project_root=project_root)
    for value in refused:
        changed = deepcopy(config)
        setting(changed, key, value)
        with pytest.raises(ConfigurationError, match=key.rsplit('.', 1)[0].split('.')[0]):
            validate_planner_reads(changed, project_root=project_root)


def test_family_ask_requires_clarification_ask(model_test_profile, tmp_path, project_root):
    config = planner_config(model_test_profile, tmp_path)
    config['resolution']['on_family_match'] = 'ask'
    with pytest.raises(ConfigurationError, match='decision 68'):
        validate_planner_reads(config, project_root=project_root)
    config['clarification']['mode'] = 'ask'
    validate_planner_reads(config, project_root=project_root)


def test_request_preflight_runs_the_planner_gate(model_test_profile, tmp_path, project_root):
    config = planner_config(model_test_profile, tmp_path)
    effective_request_config(config, {}, project_root=project_root)
    config['vlm']['roles']['planner']['image']['source'] = 'source_image'
    with pytest.raises(ConfigurationError, match='request configuration validation failed'):
        effective_request_config(config, {}, project_root=project_root)


@pytest.mark.parametrize(('value', 'ok'), [(512, True), ('none', True), (0, False), (True, False), ('big', False),
                                           (512.0, False), ('deploy_required', False)])
def test_backend_max_side_override_and_refusal(model_test_profile, tmp_path, project_root, value, ok):
    """Decision 144: an integer or none; a sentinel is refused where a request reads the backend."""
    config = planner_config(model_test_profile, tmp_path)
    config['vlm']['backends']['main']['image']['max_side'] = value
    if ok:
        assert effective_request_config(config, {}, project_root=project_root)['vlm']['backends']['main']['image'][
            'max_side'] == value
    else:
        with pytest.raises(ConfigurationError):
            effective_request_config(config, {}, project_root=project_root)


def test_backend_max_side_is_required(model_test_profile, tmp_path, project_root):
    config = planner_config(model_test_profile, tmp_path)
    del config['vlm']['backends']['main']['image']['max_side']
    with pytest.raises(ConfigurationError):
        effective_request_config(config, {}, project_root=project_root)


def test_embedder_backend_cap_is_refused_until_k1_includes_it(model_test_profile, project_root):
    config = deepcopy(model_test_profile)
    validate_embedder_reads(config, 'configuration', {}, project_root)
    config['vlm']['backends']['embedder']['image']['max_side'] = 512
    with pytest.raises(ConfigurationError, match=r"embedder\.image\.max_side: an embedder backend must keep none"):
        validate_embedder_reads(config, 'configuration', {}, project_root)
