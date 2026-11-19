"""Configuration of `eval run` and `eval import` (T-016 P4, R-015, R-017, R-020): every consumed `evaluation` leaf has a
validated value, an override test (a non-default value is the value the command uses) and a refusal test (missing,
unresolved or malformed: refused before anything starts, naming the key). `evaluation.min_items_per_class` and the other
leaves a command does not read are not consumed. Evidence class: `mock_contract` (no model call)."""
from pathlib import Path
import sys

import pytest
import yaml

from cli_fixtures import ROOT, pipeline_config, write_yaml
from eval_fixtures import evaluation_values
from prompt_enhancer.config import (COMMAND_READS, ConfigurationError, EVALUATION_LEAVES, load_config,
                                    validate_evaluation_settings)

RUN_LEAVES = EVALUATION_LEAVES['eval run']
IMPORT_LEAVES = EVALUATION_LEAVES['eval import']
LEAVES = [*RUN_LEAVES, *(leaf for leaf in IMPORT_LEAVES if leaf not in RUN_LEAVES)]
OVERRIDES = {'scenario_kinds': [['only_kind'], ['kind_a', 'kind_b']], 'split_by': ['concept'],
             'run.max_set_bytes': [1, 4096, 99_999_999], 'run.max_source_image_bytes': [1, 5000, 99_999_999],
             'run.max_concurrency': [1, 3, 7], 'run.max_sidecar_bytes': [1, 4096, 99_999_999],
             'judge.blinding_seed': [0, 7, 2 ** 63 - 1],
             'judge.score_min': [-3, 0, 2], 'judge.score_max': [3, 10, 100],
             'judge.rubric_prompt_id': ['eval_rubric_v0', 'another_rubric'], 'judge.min_scorers': [2, 3, 9],
             'judge.under_test_families': [['one'], ['one', 'two']], 'judge.max_score_file_bytes': [1, 5000, 99_999_999]}
POSITIVE = ('run.max_set_bytes', 'run.max_source_image_bytes', 'run.max_concurrency', 'run.max_sidecar_bytes',
            'judge.min_scorers', 'judge.max_score_file_bytes')
BAD = {'scenario_kinds': [[], ['a', 'a'], [''], [1], 'a', None, {'a': 1}], 'split_by': ['', 'file', 'scenario', 1, None],
       'judge.rubric_prompt_id': ['', 1, None, ['x']]}
for leaf in POSITIVE:
    BAD[leaf] = [0, -1, True, 1.5, '3', None, sys.maxsize + 1]
for leaf in ('judge.score_min', 'judge.score_max'):
    BAD[leaf] = [True, 1.5, '3', None, sys.maxsize + 1]
BAD['judge.blinding_seed'] = [-1, True, 1.5, '3', None, 2 ** 63]
BAD['judge.under_test_families'] = [[], ['a', 'a'], [''], [1], 'a', None]
BAD['judge.min_scorers'] += [1]  # two scorers of different families are the least a comparison needs


def command_of(leaf):
    return 'eval run' if leaf in RUN_LEAVES else 'eval import'


def base(model_test_profile, tmp_path, project_root):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1, retrieval=True)
    evaluation_values(config)
    config['runtime']['trace']['queue_limit'] = 64  # room for every override below
    config['evaluation'].setdefault('scenario_kinds', ['single_concept'])
    config['evaluation'].setdefault('split_by', 'concept')
    return config


def put(config, leaf, value):
    node = config['evaluation']
    *parents, name = leaf.split('.')
    for part in parents:
        node = node[part]
    node[name] = value


def drop(config, leaf):
    node = config['evaluation']
    *parents, name = leaf.split('.')
    for part in parents:
        node = node[part]
    del node[name]


def key(leaf):
    return f'evaluation.{leaf}'


def test_the_consumed_leaves_are_the_read_set_of_each_command():
    for command, leaves in EVALUATION_LEAVES.items():
        reads = COMMAND_READS[command]
        for leaf in leaves:
            assert any(key(leaf) == read or key(leaf).startswith(read + '.') for read in reads), (command, leaf)
    assert 'evaluation.min_items_per_class' not in COMMAND_READS['eval run']  # a leaf nothing here consumes is not read


@pytest.mark.parametrize('leaf, value', [(leaf, value) for leaf in LEAVES for value in OVERRIDES[leaf]])
def test_every_evaluation_leaf_override_is_the_value_the_command_uses(model_test_profile, tmp_path, project_root, leaf, value):
    config = base(model_test_profile, tmp_path, project_root)
    if leaf == 'judge.score_min':
        config['evaluation']['judge']['score_max'] = 200
    if leaf == 'judge.score_max':
        config['evaluation']['judge']['score_min'] = -4
    put(config, leaf, value)
    settings = validate_evaluation_settings(config, command_of(leaf), project_root=project_root)
    assert settings[leaf] == value and type(settings[leaf]) is type(value)


@pytest.mark.parametrize('leaf', [leaf for leaf in LEAVES if leaf != 'split_by'])  # `concept` is the one rule it names
def test_an_override_changes_only_its_own_value(model_test_profile, tmp_path, project_root, leaf):
    config = base(model_test_profile, tmp_path, project_root)
    if leaf == 'judge.score_min':
        config['evaluation']['judge']['score_max'] = 200
    reference = validate_evaluation_settings(config, command_of(leaf), project_root=project_root)
    put(config, leaf, OVERRIDES[leaf][-1] if leaf != 'judge.score_min' else 2)
    changed = validate_evaluation_settings(config, command_of(leaf), project_root=project_root)
    differing = {other for other in reference if reference[other] != changed[other]}
    assert differing == {leaf}


@pytest.mark.parametrize('leaf, value', [(leaf, value) for leaf in LEAVES for value in BAD[leaf]],
                         ids=lambda item: repr(item)[:24])
def test_every_evaluation_leaf_refuses_a_malformed_value_naming_its_key(model_test_profile, tmp_path, project_root, leaf, value):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, value)
    with pytest.raises(ConfigurationError) as caught:
        validate_evaluation_settings(config, command_of(leaf), project_root=project_root)
    assert key(leaf) in str(caught.value)


@pytest.mark.parametrize('leaf', LEAVES)
@pytest.mark.parametrize('sentinel', ['deploy_required', 'fit_from_bench', 'design_required', 'ask_user'])
def test_every_evaluation_leaf_refuses_an_unresolved_sentinel_at_load(model_test_profile, tmp_path, project_root, leaf, sentinel):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, sentinel)
    with pytest.raises(ConfigurationError) as caught:
        load_config(write_yaml(project_root, tmp_path, config), command_of(leaf), project_root=project_root)
    assert key(leaf) in str(caught.value) and sentinel in str(caught.value)


@pytest.mark.parametrize('leaf', LEAVES)
def test_every_evaluation_leaf_refuses_a_missing_key(model_test_profile, tmp_path, project_root, leaf):
    config = base(model_test_profile, tmp_path, project_root)
    drop(config, leaf)
    with pytest.raises(ConfigurationError) as caught:
        load_config(write_yaml(project_root, tmp_path, config), command_of(leaf), project_root=project_root)
    assert key(leaf) in str(caught.value), str(caught.value)  # the exact key, not just the block


def test_the_loader_accepts_a_resolved_profile_for_both_commands(model_test_profile, tmp_path, project_root):
    config = base(model_test_profile, tmp_path, project_root)
    for command in ('eval run', 'eval import'):
        loaded = load_config(write_yaml(project_root, tmp_path, config), command, project_root=project_root)
        assert validate_evaluation_settings(loaded, command, project_root=project_root)['judge.score_min'] == 1


def test_the_scale_needs_its_lowest_value_below_its_highest(model_test_profile, tmp_path, project_root):
    config = base(model_test_profile, tmp_path, project_root)
    config['evaluation']['judge'].update(score_min=3, score_max=4)
    validate_evaluation_settings(config, 'eval import', project_root=project_root)  # one step apart is a scale
    config['evaluation']['judge'].update(score_min=3, score_max=3)
    with pytest.raises(ConfigurationError, match=r'evaluation\.judge\.score_min: must be below'):
        validate_evaluation_settings(config, 'eval import', project_root=project_root)


@pytest.mark.parametrize('command', ['eval run', 'eval import'])
@pytest.mark.parametrize('leaf', ['run.max_sidecar_bytes', 'judge.score_min', 'judge.score_max'])
def test_a_leaf_both_commands_read_is_refused_by_each_when_unresolved(model_test_profile, tmp_path, project_root, command, leaf):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, 'deploy_required')
    with pytest.raises(ConfigurationError, match=key(leaf)):
        load_config(write_yaml(project_root, tmp_path, config), command, project_root=project_root)


@pytest.mark.parametrize('command', ['eval run', 'eval import'])
@pytest.mark.parametrize('value', ['deploy_required', {'timeout_s': 'deploy_required', 'poll_s': 1}, {'timeout_s': 1}])
def test_the_run_lock_needs_a_resolved_storage_lock_in_both_commands(model_test_profile, tmp_path, project_root, command, value):
    config = base(model_test_profile, tmp_path, project_root)
    config['storage']['lock'] = value
    with pytest.raises(ConfigurationError, match='storage.lock'):
        load_config(write_yaml(project_root, tmp_path, config), command, project_root=project_root)
    config['storage']['lock'] = {'timeout_s': 2.5, 'poll_s': 0.5}  # an override: the validated values reach the lock
    loaded = load_config(write_yaml(project_root, tmp_path, config), command, project_root=project_root)
    from prompt_enhancer.storage import open_data_store
    store = open_data_store(loaded, command, project_root=project_root)
    assert (store.timeout_s, store.poll_s) == (2.5, 0.5)


def test_no_setting_claims_to_guarantee_trace_capacity(model_test_profile, tmp_path, project_root):
    """Fix round 1, P3 (a): a trace slot is freed only after its trace write finishes, so `max_concurrency` is not tied
    to `runtime.trace.queue_limit`; a unit refused for capacity is recorded `not_run` instead (`test_eval_concurrency`)."""
    config = base(model_test_profile, tmp_path, project_root)
    config['runtime']['trace']['queue_limit'] = 3
    config['evaluation']['run']['max_concurrency'] = 9
    assert validate_evaluation_settings(config, 'eval run', project_root=project_root)['run.max_concurrency'] == 9


def test_one_scenario_at_a_time_is_not_a_hidden_default(model_test_profile, tmp_path, project_root):
    """Decision 149 (S46): the bound is a profile value; nothing supplies 1 when it is absent."""
    config = base(model_test_profile, tmp_path, project_root)
    drop(config, 'run.max_concurrency')
    with pytest.raises(ConfigurationError, match=r'evaluation\.run\.max_concurrency'):
        validate_evaluation_settings(config, 'eval run', project_root=project_root)


def test_the_blinding_seed_is_validated_but_the_run_identity_never_reads_it():
    from prompt_enhancer import eval_run
    source = Path(eval_run.__file__).read_text(encoding='utf-8')
    assert 'blinding_seed' not in source  # the seed picks a pack's label order only (PM amendment 2)


def test_the_shipped_examples_hold_sentinels_for_the_deployment_and_a_dev_only_set_for_the_dev_profile():
    base_profile = yaml.safe_load((ROOT / 'configs' / 'enhancer.example.yaml').read_bytes())['evaluation']
    dev_profile = yaml.safe_load((ROOT / 'configs' / 'enhancer.dev.example.yaml').read_bytes())['evaluation']
    assert base_profile['run']['max_concurrency'] == 'deploy_required'  # a deployment value is never defaulted
    assert base_profile['judge']['blinding_seed'] == 'deploy_required'
    assert isinstance(dev_profile['run']['max_concurrency'], int) and dev_profile['run']['max_concurrency'] >= 1
    assert dev_profile['judge']['blinding_seed'] != base_profile['judge']['blinding_seed']
