"""Override and refusal tests for every key S3 reads (T-014 P2, R-015)."""

from copy import deepcopy

import pytest

from assembly_fixtures import assembly_config
from prompt_enhancer.config import ConfigurationError, effective_request_config, validate_assembly_reads


def setting(config, key, value):
    node = config
    parts = key.split('.')
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = deepcopy(value)


CASES = [  # key, a non-default accepted value, refused values
    ('rag.evidence_mode', 'cards', ['live', 'cards_then_live', 'other', 'design_required']),
    ('assembly.scope_matrix.subject_identity', ['identity', 'text_in_image'],
     [['identity', 'identity'], ['hair'], 'design_required', ['design_required'], 'identity']),
    ('assembly.scope_matrix.other', ['incidental'], [['Identity'], [1]]),
    ('assembly.scope_matrix', {'subject_identity': [], 'worn_garment': [], 'location': [], 'object': [],
                               'style_reference': [], 'other': []},
     [{'subject_identity': []}, 'fit_from_gt']),
    ('assembly.default_outfit', 'include', ['wear', 'design_required']),
    ('assembly.max_facts_per_concept', 0, [-1, True, 1.0, 'fit_from_gt', 2 ** 63]),
    ('assembly.max_facts_per_request', 7, [-1, False, 2.5, 'fit_from_gt', 2 ** 63]),
    ('assembly.fact_order', 'need_then_support', ['support_then_need', 'design_required']),
    ('resolution.on_variant_unspecified', 'omit', ['random', 'design_required']),
    ('resolution.random_seed', 5, [-1, 'fresh', 'design_required']),
    ('resolution.trace_choice', False, ['no', 'design_required']),
    ('edit.attach_kb_reference_images', True, ['yes', 1, 'design_required']),
]


@pytest.mark.parametrize(('key', 'accepted', 'refused'), CASES, ids=[case[0] for case in CASES])
def test_assembly_reads_override_and_refusal(model_test_profile, tmp_path, project_root, key, accepted, refused):
    config = assembly_config(model_test_profile, tmp_path)
    setting(config, key, accepted)
    assert validate_assembly_reads(config, project_root=project_root) is config['assembly']
    for value in refused:
        changed = deepcopy(config)
        setting(changed, key, value)
        with pytest.raises(ConfigurationError, match=key.split('.')[0]):
            validate_assembly_reads(changed, project_root=project_root)


@pytest.mark.parametrize('mode', ['live', 'cards_then_live'])
def test_live_evidence_modes_are_refused_with_the_deferral(model_test_profile, tmp_path, project_root, mode):
    config = assembly_config(model_test_profile, tmp_path)
    config['rag']['evidence_mode'] = mode
    with pytest.raises(ConfigurationError, match=(
            rf'rag\.evidence_mode: {mode} is not implemented yet: live reading of knowledge-base images and the '
            r'on-demand observer question are deferred \(T-014\); use cards')):
        validate_assembly_reads(config, project_root=project_root)


@pytest.mark.parametrize('key', ['assembly.max_images_per_concept', 'edit.max_kb_reference_images'])
def test_image_caps_are_read_only_when_attach_is_on(model_test_profile, tmp_path, project_root, key):
    """PM answer 1: the caps are S3 reads only with attach on (request preflight still checks the whole block)."""
    config = assembly_config(model_test_profile, tmp_path)
    setting(config, key, 'fit_from_gt')
    validate_assembly_reads(config, project_root=project_root)  # attach off: not read
    config['edit']['attach_kb_reference_images'] = True
    with pytest.raises(ConfigurationError, match=rf'{key}: unresolved sentinel fit_from_gt'):
        validate_assembly_reads(config, project_root=project_root)
    for value, ok in ((0, True), (4, True), (-1, False), (True, False), (1.0, False), (2 ** 63, False)):
        setting(config, key, value)
        if ok:
            validate_assembly_reads(config, project_root=project_root)
        else:
            with pytest.raises(ConfigurationError, match=key):
                validate_assembly_reads(config, project_root=project_root)


def test_request_preflight_runs_the_assembly_gate_only_with_retrieval_on(model_test_profile, tmp_path,
                                                                         project_root):
    config = assembly_config(model_test_profile, tmp_path)
    config['rag']['evidence_mode'] = 'live'
    with pytest.raises(ConfigurationError, match='request configuration validation failed'):
        effective_request_config(config, {}, project_root=project_root)
    config['rag']['enabled'] = False  # S3 is absent: rag.evidence_mode is not read
    effective_request_config(config, {}, project_root=project_root)


def test_request_preflight_accepts_the_assembly_gate(model_test_profile, tmp_path, project_root):
    config = assembly_config(model_test_profile, tmp_path)
    (tmp_path / 'kb').mkdir(exist_ok=True)
    effective = effective_request_config(config, {}, project_root=project_root)
    assert effective['assembly']['fact_order'] == 'need_then_support'
