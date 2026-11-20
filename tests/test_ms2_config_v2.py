"""MS-2 (decision 179): `enhancer_config.v2` (configuration schema_version 3), the three source-image limits and the
two multi-source role bindings, loaded and refused by key.

Labelled CPU tests: synthetic configurations built from the example profile (`model_test_profile`), no server,
network or model call, and no knowledge-base access.
"""

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import sys

from hypothesis import given, strategies as st
import pytest
import yaml

from prompt_enhancer import config as config_module
from prompt_enhancer.config import (
    REQUEST_COMMANDS, SENTINELS, ConfigurationError, _configuration_schema, _is_source_limit_key, _load_chain,
    _require_source_limits_version, _require_validation_version, _validate, _validate_loaded_config,
    _validate_override_policy, check_limit, validate_planner_reads, validate_source_limits, validate_writer_reads,
)
from intake_fixtures import configuration
from profile_fixtures import install_prompts

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ('enhancer.example.yaml', 'enhancer.dev.example.yaml', 'enhancer.dev-qwen3vl2b.example.yaml')

# Guard: the files that must never change in place (versioning rules, contracts/README.md). SHA-256 of the committed
# bytes with line endings normalized to LF.
GUARDED = {
    'alias_generator_reply.v0.schema.json': '8ab9e31c41644bd3fb81bf7c14e855872879f77eff0f7fe35d63657a1f40962d',
    'alias_set.v0.schema.json': '13357709b2eb34031f8c5a0045065b71e2403a0ea44d39eb00b5e9ac63d1bc48',
    'assembly_result.v0.schema.json': 'b31c8d4fafafc6c38ae6a5d2797bce99bfd7cd807a239d6255d4a680c972c2e6',
    'candidate_shortlist.v0.schema.json': '2de388b064e4ccdad3b2c68d3671517c2611d4c03f6e615133c64720d547b5a0',
    'card_builder_reply.v0.schema.json': '4f7a7ea286c179556e23ce01ace7418b8dcc0c1fe7f7aa3341cd4412caac40ae',
    'concept_card.v0.schema.json': '2eeec941fbb879a30db60f7ce40f339c47ff01e37df8a3317f126c41a806b6b6',
    'constraint_ledger.v0.schema.json': 'd98bd98f0774d1d1a6d91734e84d0abeba8c7b6711f15b149fcb29457fd2a52a',
    'enhancer_config.v0.schema.json': '1a4811a861e289e31e218299242202c7c50841b568605d01b8b7c44ed4946ddb',
    'enhancer_config.v1.schema.json': '0b6e7b858a25ab52ba9bfe8092131c1e938c300b18d7f609654692e40ea5f517',
    'eval_judge_pack.v0.schema.json': 'b036aa686e21692b709092377f34daf19de72a6e4cec5a8e4cf5f4d890950f9e',
    'eval_judge_scores.v0.schema.json': '9092537596e05b8a3c5c69bff1725094676de48d718529caf015ffc854b0c79e',
    'eval_result_metrics.v0.schema.json': 'de783cba21959c77db92b53a5172defe6dbcd4f26db0ebd1f94ff1b372b03ab6',
    'eval_scenario.v0.schema.json': '6e147fe0bb6f99af733655c03120f7bfbf0bd9cbde1c3dd9e285d92cc7d5f7e6',
    'folder_naming.v0.schema.json': '2063f7984d0280f13783ce19aebb0ac1b4a85dbd7647014310577b541ff64832',
    'golden_case.v0.schema.json': '92898c7b335c7bf7d059628a506c9d39187ac95dd7fd4c9adc38a5295ee8cd52',
    'image_observation.v0.schema.json': '135d87751733e8f315a40d2e2ea203f5cf7c2e0c2075ae051edf6caad8ceba0b',
    'info_assessment.v0.schema.json': '9f0b04ed60263626ffdd03ff11fd7095d6eb0bca48071079be145ec234bd2a96',
    'intake_result.v0.schema.json': 'bc5c0a811026f94509a9806fb8c95749fc363c2fab4b8bfe1dcbe314d3e0145b',
    'intent_ir.v0.schema.json': 'a5d4084be03c9c055653be89094f09a174c054a87b42a3271154561722c7230c',
    'kb_overrides.v0.schema.json': 'ffa17263b7812226325d79c7242ec7a46ca4470c19445334774b78353839c761',
    'knowledge_pack.v0.schema.json': '85ee4fb213f5ef4a922ca5710b9a80ee832da0320e2b2d3cb0925198e9e3cc83',
    'model_manifest.v0.schema.json': '670b77225f6315818b9fbf6ad681bb67f43c1f2898e444447a14e094a358d4d2',
    'observer_reply.v0.schema.json': 'd820327cb0c5a8f1ccabf22824116ff9a413dea480e4fd36f58172141c0e3ad6',
    'pipeline_runtime.v0.schema.json': '55300bbe1e4452859264011f31e40e67e1d9aa551f309363b24fe0b5a59372c3',
    'plan_result.v0.schema.json': '16397b3a73a643e20d648479cc3d1b8065cdf22387545c54c914c6ef03545211',
    'planner_reply.v0.schema.json': '6b70d57fd60e6a253aef3b8b6d9712601107b372de383d0988541ec4e553a59e',
    'planner_reply_generate.v0.schema.json': '480404b4816b5f95d364744c92deb493a9b0b7ef0b1d59e29e4848cb0f7c1ac8',
    'profile_manifest.v0.schema.json': '07b42c05ce5900c0d64e2aba0557053834d33cf8ddcd0126fee17f4537f7a5f2',
    'profile_validator.v0.schema.json': 'b2b89a8be6045998f016d3cea829e28225b074ab77902fce3a2f2a65739dbceb',
    'request.v0.schema.json': 'bc416007804fc858dedb168c324348e4e1fd288044c554d2a4b86b8274ba448d',
    'response_envelope.v0.schema.json': '7ff98b7c43cea346bd8bd339b79aae928535cc0a2e73c54bb86ca68f8ded41a1',
    'serving_index.v0.schema.json': '42ca7df06b3893e4fec96f38df794428082528d08779906bc1d8881b65d51866',
    'validation_result.v0.schema.json': '5601870cf1197576d50d527b4e3a67cab2d2496bf3a295596d8fce9a07d07c48',
    'verifier_calibration.v0.schema.json': '1f64ee71481f9053a40ab78b91ca3074871501d0559d03257262d04f0041d88a',
    'verifier_result.v0.schema.json': '263adc2e727ef35676205fc021d9c8e81db0edb19aa3bc6254e97e85649f3371',
    'vlm_role.v0.schema.json': 'b755864e3e2202d413d0bf7ad488cbc64cbec37295f313562a27b1fa5f5b4173',
    'writer_result.v0.schema.json': '904b6c296b34a0eb0a206d284755fd006c3b197b08b773697bc720de6113a533',
}


@pytest.fixture
def project_root(project_root):
    """The session project root plus the pipeline-owned prompt files (test-only copy)."""
    install_prompts(project_root)
    return project_root


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


@pytest.mark.parametrize('name', sorted(GUARDED))
def test_existing_schema_files_are_never_edited_in_place(name):
    assert digest(ROOT / 'contracts' / 'schemas' / name) == GUARDED[name]


def test_the_guard_lists_every_v0_schema_and_enhancer_config_v1():
    present = {path.name for path in (ROOT / 'contracts' / 'schemas').glob('*.v0.schema.json')}
    present.add('enhancer_config.v1.schema.json')
    assert present <= set(GUARDED)


def v3(model_test_profile, tmp_path):
    """A fully resolved version-3 request configuration (labelled test-only values)."""
    config = configuration(model_test_profile, tmp_path)
    assert config['schema_version'] == 3
    return config


def add_backend(config, name, limit):
    backend = deepcopy(config['vlm']['backends']['main'])
    backend['image']['max_images_per_call'] = limit
    config['vlm']['backends'][name] = backend
    return name


# ---- schema composition ------------------------------------------------------------------------------------

def test_version_3_is_version_2_plus_exactly_the_four_new_keys():
    v2 = _configuration_schema(ROOT, {'schema_version': 2})
    composed = _configuration_schema(ROOT, {'schema_version': 3})
    assert composed['properties']['schema_version'] == {'const': 3}
    rest = deepcopy(composed)
    rest['properties']['schema_version'] = v2['properties']['schema_version']
    edit = rest['properties']['edit']['anyOf'][0]
    assert 'max_source_images' in edit['required'] and edit['properties'].pop('max_source_images')
    edit['required'].remove('max_source_images')
    old_edit = v2['properties']['edit']['anyOf'][0]
    assert edit['properties'].keys() == old_edit['properties'].keys() and set(edit['required']) == set(old_edit['required'])
    rest['properties']['edit'] = v2['properties']['edit']
    vlm, old_vlm = rest['properties']['vlm']['anyOf'][0]['properties'], v2['properties']['vlm']['anyOf'][0]['properties']
    image = vlm['backends']['anyOf'][0]['additionalProperties']['anyOf'][0]['properties']['image']
    assert image['properties'].pop('max_images_per_call') and image['required'].pop() == 'max_images_per_call'
    roles = vlm['roles']['anyOf'][0]
    assert set(roles.pop('properties')) == {'planner', 'writer'}
    rest_vlm = rest['properties']['vlm']['anyOf'][0]['properties']
    assert rest_vlm == old_vlm


def test_version_2_and_1_schemas_are_what_they_were():
    assert _configuration_schema(ROOT, {'schema_version': 1})['properties'].keys() == \
        _configuration_schema(ROOT, {'schema_version': 2})['properties'].keys()
    image = _configuration_schema(ROOT, {'schema_version': 2})['properties']['vlm']['anyOf'][0]['properties'][
        'backends']['anyOf'][0]['additionalProperties']['anyOf'][0]['properties']['image']
    assert 'max_images_per_call' not in image['properties']


@pytest.mark.parametrize('name', EXAMPLES)
def test_every_tracked_example_is_version_3_and_valid(name):
    config = _load_chain(ROOT / 'configs' / name, ROOT, (), {})
    assert config['schema_version'] == 3
    _validate(config, _configuration_schema(ROOT, config), name)
    assert config['request_overrides']['allowed_keys'] == []  # a request can raise no limit


def test_example_values_follow_the_design():
    base = _load_chain(ROOT / 'configs' / 'enhancer.example.yaml', ROOT, (), {})
    dev = _load_chain(ROOT / 'configs' / 'enhancer.dev.example.yaml', ROOT, (), {})
    assert base['edit']['max_source_images'] == 'deploy_required' and dev['edit']['max_source_images'] == 1
    assert {b['image']['max_images_per_call'] for b in base['vlm']['backends'].values()} == {'deploy_required'}
    dev_limits = [b['image']['max_images_per_call'] for b in dev['vlm']['backends'].values()]
    assert dev_limits.count(1) == 3 and set(dev_limits) <= {1, 'deploy_required'}  # inherited backends keep the sentinel
    for config in (base, dev):
        assert config['vlm']['roles']['planner']['multi_source'] == {'prompt_id': 'planner_v2', 'schema_id': 'planner_reply.v1'}
        assert config['vlm']['roles']['writer']['multi_source'] == {'prompt_id': 'brief_v1'}


# ---- the limits and bindings ---------------------------------------------------------------------------------

def test_the_synthetic_profile_passes(model_test_profile, tmp_path, project_root):
    config = v3(model_test_profile, tmp_path)
    validate_planner_reads(config, project_root=project_root)
    validate_writer_reads(config, project_root=project_root)
    assert validate_source_limits(config, project_root=project_root) == 1


def test_override_two_images_with_a_higher_backend_limit_and_other_prompts(model_test_profile, tmp_path, project_root):
    """Non-default values: a cap of 2, a backend that carries 4 images, another admin prompt id for n >= 2."""
    (project_root / 'prompts' / 'brief_ms2_two.txt').write_bytes(b'Two images\n{{data}}')
    (project_root / 'prompts' / 'planner_ms2_two.txt').write_bytes(b'Plan two images')
    config = v3(model_test_profile, tmp_path)
    config['edit']['max_source_images'] = 2
    config['vlm']['backends']['main']['image']['max_images_per_call'] = 4
    config['vlm']['roles']['planner']['multi_source'] = {'prompt_id': 'planner_ms2_two', 'schema_id': 'planner_reply.v1'}
    config['vlm']['roles']['writer']['multi_source'] = {'prompt_id': 'brief_ms2_two'}
    assert validate_source_limits(config, project_root=project_root) == 2


@pytest.mark.parametrize('value, message', [
    ('deploy_required', 'edit.max_source_images: unresolved sentinel deploy_required'),
    ('fit_from_gt', 'unresolved sentinel fit_from_gt'),
    (0, 'edit.max_source_images: expected a positive integer'),
    (-1, 'expected a positive integer'),
    (True, 'expected a positive integer'),
    (2.0, 'expected a positive integer'),
    ('2', 'expected a positive integer'),
    (None, 'expected a positive integer'),
])
def test_edit_max_source_images_is_refused_by_key(model_test_profile, tmp_path, project_root, value, message):
    config = v3(model_test_profile, tmp_path)
    config['edit']['max_source_images'] = value
    with pytest.raises(ConfigurationError, match=message):
        validate_source_limits(config, project_root=project_root)


def test_a_missing_limit_is_refused_not_defaulted(model_test_profile, tmp_path, project_root):
    config = v3(model_test_profile, tmp_path)
    del config['edit']['max_source_images']
    with pytest.raises(ConfigurationError, match='edit.max_source_images'):
        validate_source_limits(config, project_root=project_root)
    with pytest.raises(ConfigurationError, match='edit.max_source_images'):
        _validate(config, _configuration_schema(project_root, config), 'configuration')
    config = v3(model_test_profile, tmp_path)
    del config['vlm']['backends']['main']['image']['max_images_per_call']
    with pytest.raises(ConfigurationError, match='vlm.backends.main.image.max_images_per_call'):
        _validate(config, _configuration_schema(project_root, config), 'configuration')


@pytest.mark.parametrize('role', ['planner', 'writer'])
def test_a_missing_multi_source_block_is_refused(model_test_profile, tmp_path, project_root, role):
    config = v3(model_test_profile, tmp_path)
    del config['vlm']['roles'][role]['multi_source']
    with pytest.raises(ConfigurationError, match=f'vlm.roles.{role}.multi_source'):
        _validate(config, _configuration_schema(project_root, config), 'configuration')


def test_the_multi_source_block_is_allowed_on_the_planner_and_writer_only(model_test_profile, tmp_path, project_root):
    config = v3(model_test_profile, tmp_path)
    config['vlm']['roles']['verifier']['multi_source'] = {'prompt_id': 'brief_v1'}
    with pytest.raises(ConfigurationError, match='vlm.roles.verifier.multi_source'):
        _validate(config, _configuration_schema(project_root, config), 'configuration')


@pytest.mark.parametrize('role', ['planner', 'writer'])
def test_a_backend_limit_below_the_cap_is_refused_for_the_role_backend(model_test_profile, tmp_path, project_root, role):
    config = v3(model_test_profile, tmp_path)
    config['edit']['max_source_images'] = 3
    config['vlm']['backends']['main']['image']['max_images_per_call'] = 3
    other = add_backend(config, 'other', 2)
    config['vlm']['roles'][role]['backend'] = other
    with pytest.raises(ConfigurationError, match=rf'vlm.backends.other.image.max_images_per_call: 2 is below edit.max_source_images \(3\)'):
        validate_source_limits(config, project_root=project_root)


def test_a_backend_limit_equal_to_the_cap_passes_and_a_sentinel_backend_limit_is_refused(model_test_profile, tmp_path,
                                                                                          project_root):
    config = v3(model_test_profile, tmp_path)
    config['edit']['max_source_images'] = 5
    config['vlm']['backends']['main']['image']['max_images_per_call'] = 5
    assert validate_source_limits(config, project_root=project_root) == 5
    config['vlm']['backends']['main']['image']['max_images_per_call'] = 'deploy_required'
    with pytest.raises(ConfigurationError, match='max_images_per_call: unresolved sentinel deploy_required'):
        validate_source_limits(config, project_root=project_root)


@pytest.mark.parametrize('prompt_id', ['no_such_prompt', '../escape', '', 'UPPER'])
@pytest.mark.parametrize('role', ['planner', 'writer'])
def test_an_unknown_multi_source_prompt_id_is_refused(model_test_profile, tmp_path, project_root, role, prompt_id):
    config = v3(model_test_profile, tmp_path)
    config['vlm']['roles'][role]['multi_source']['prompt_id'] = prompt_id
    with pytest.raises(ConfigurationError, match=f'vlm.roles.{role}.multi_source.prompt_id: unknown prompt id'):
        validate_source_limits(config, project_root=project_root)


@pytest.mark.parametrize('schema_id', ['planner_reply.v0', 'planner_reply_generate.v0', 'plan_result.v2', 'nothing'])
def test_the_planner_schema_id_must_be_planner_reply_v1(model_test_profile, tmp_path, project_root, schema_id):
    config = v3(model_test_profile, tmp_path)
    config['vlm']['roles']['planner']['multi_source']['schema_id'] = schema_id
    with pytest.raises(ConfigurationError, match='vlm.roles.planner.multi_source.schema_id: expected planner_reply.v1'):
        validate_source_limits(config, project_root=project_root)


@pytest.mark.parametrize('template', [b'no variable here', b'{{data}} and {{data}}', b'{{data}} {{other}}', b'{{data}} }}',
                                      b'{{aspect_ratio}}'])
def test_a_writer_template_needs_exactly_one_data_variable(model_test_profile, tmp_path, project_root, template):
    (project_root / 'prompts' / 'brief_ms2_bad.txt').write_bytes(template)
    config = v3(model_test_profile, tmp_path)
    config['vlm']['roles']['writer']['multi_source']['prompt_id'] = 'brief_ms2_bad'
    with pytest.raises(ConfigurationError, match='vlm.roles.writer.multi_source.prompt_id: the brief template requires exactly one'):
        validate_source_limits(config, project_root=project_root)


def test_sentinels_in_the_multi_source_blocks_are_refused(model_test_profile, tmp_path, project_root):
    config = v3(model_test_profile, tmp_path)
    config['vlm']['roles']['planner']['multi_source']['prompt_id'] = 'deploy_required'
    with pytest.raises(ConfigurationError, match='vlm.roles.planner.multi_source.prompt_id: unresolved sentinel'):
        validate_source_limits(config, project_root=project_root)


def test_retrieval_off_reads_no_knowledge_base_for_the_limits(model_test_profile, tmp_path, project_root, monkeypatch):
    """R-009: no new code path opens the knowledge base."""
    config = v3(model_test_profile, tmp_path)
    assert config['rag']['enabled'] is False

    def forbidden(*args, **kwargs):
        raise AssertionError('the knowledge base was touched')

    for name in ('resolve_knowledge_root', 'same_physical_path'):
        monkeypatch.setattr(config_module, name, forbidden)
    assert validate_source_limits(config, project_root=project_root) == 1


# ---- versions ------------------------------------------------------------------------------------------------

def as_v2(config):
    config = deepcopy(config)
    config['schema_version'] = 2
    del config['edit']['max_source_images']
    for backend in config['vlm']['backends'].values():
        backend.get('image', {}).pop('max_images_per_call', None)
    for role in ('planner', 'writer'):
        del config['vlm']['roles'][role]['multi_source']
    return config


@pytest.mark.parametrize('command', REQUEST_COMMANDS)
def test_every_request_command_gives_the_migrate_message_for_version_2(model_test_profile, tmp_path, command):
    config = as_v2(v3(model_test_profile, tmp_path))
    _require_validation_version(config, 'configuration')  # still a valid S5 version
    with pytest.raises(ConfigurationError, match='schema_version: migrate the configuration to version 3'):
        _require_source_limits_version(config, 'configuration')
    with pytest.raises(ConfigurationError, match=r'edit.max_source_images, vlm.backends.<name>.image.max_images_per_call'):
        _validate_loaded_config(config, command, ROOT, 'configuration', {})


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('command', REQUEST_COMMANDS)
def test_a_request_config_below_version_3_gets_one_message_naming_version_3(model_test_profile, tmp_path, command, version):
    """F5: a version-1 file is not told to go to version 2 and then to 3; every version below 3 names the target 3."""
    config = v3(model_test_profile, tmp_path)
    config['schema_version'] = version  # only the version is read before the first refusal
    with pytest.raises(ConfigurationError) as error:
        _validate_loaded_config(config, command, ROOT, 'configuration', {})
    assert 'schema_version: migrate the configuration to version 3 (enhancer_config.v2)' in str(error.value)
    assert 'version 2' not in str(error.value)


@pytest.mark.parametrize('command', REQUEST_COMMANDS)
def test_no_request_command_refuses_version_3(model_test_profile, tmp_path, command):
    config = v3(model_test_profile, tmp_path)
    _require_validation_version(config, 'configuration')
    _require_source_limits_version(config, 'configuration')


def test_a_version_3_request_load_passes_end_to_end_and_injects_nothing(model_test_profile, tmp_path, project_root):
    config = v3(model_test_profile, tmp_path)
    before = deepcopy(config)
    assert _validate_loaded_config(config, 'enhance once', project_root, 'configuration', {}) is config
    assert config == before


# The fixture cannot make `embed serve` and `eval import` load (it states no embed_server engine and an unresolved
# judge sentinel), so for those two the case asserts the exact unrelated refusal and that version 3 gives the same one:
# the version plays no part in it. `kb status` and `profile register` load.
NO_KEY_COMMANDS = [('kb status', None), ('profile register', None),
                   ('embed serve', 'vlm.backends.embedder.engine.name: expected embed_server'),
                   ('eval import', 'evaluation.judge.score_min: unresolved sentinel design_required')]


@pytest.mark.parametrize('command, reason', NO_KEY_COMMANDS)
def test_a_version_2_configuration_still_loads_for_commands_that_read_none_of_the_keys(model_test_profile, tmp_path,
                                                                                        project_root, command, reason):
    config = as_v2(v3(model_test_profile, tmp_path))
    before = deepcopy(config)
    if reason is None:
        _validate_loaded_config(config, command, project_root, 'configuration', {})
    else:
        with pytest.raises(ConfigurationError) as version_2:
            _validate_loaded_config(config, command, project_root, 'configuration', {})
        assert reason in str(version_2.value) and 'migrate' not in str(version_2.value)
        with pytest.raises(ConfigurationError) as version_3:
            _validate_loaded_config(v3(model_test_profile, tmp_path), command, project_root, 'configuration', {})
        assert str(version_3.value) == str(version_2.value)  # the same refusal at version 3: unrelated to the version
    assert config == before  # a version-2 file is never rewritten or given a default


def test_the_commands_that_read_the_keys_are_exactly_the_request_commands():
    reads = {command: keys for command, keys in config_module.COMMAND_READS.items()
             if any(key == 'edit' or key.startswith('vlm.roles.planner') for key in keys)}
    assert set(reads) == set(REQUEST_COMMANDS)


@pytest.mark.parametrize('child, base, merged', [(1, 3, 3), (2, 3, 3), (3, 3, 3), (1, 2, 2), (2, 2, 2), (1, 1, 1)])
def test_a_child_below_its_base_takes_the_base_version(tmp_path, child, base, merged):
    (tmp_path / 'base.yaml').write_text(yaml.safe_dump({'schema_version': base, 'x': {'a': 1, 'b': 2}}), encoding='utf-8')
    (tmp_path / 'child.yaml').write_text(yaml.safe_dump({'schema_version': child, 'extends': 'base.yaml', 'x': {'b': 3}}),
                                         encoding='utf-8')
    config = _load_chain(tmp_path / 'child.yaml', tmp_path, (), {})
    assert config['schema_version'] == merged and config['x'] == {'a': 1, 'b': 3}


@pytest.mark.parametrize('child', [1, 2])
def test_a_pilot_style_child_of_the_dev_example_becomes_version_3(tmp_path, child):
    """The pilot profile states schema_version 1 and extends the dev example: it inherits the dev example's contract."""
    shutil.copytree(ROOT / 'configs', tmp_path / 'configs', ignore=shutil.ignore_patterns('profiles'))
    path = tmp_path / 'child.yaml'
    path.write_text(yaml.safe_dump({'schema_version': child, 'extends': 'configs/enhancer.dev.example.yaml',
                                    'status': 'template', 'purpose': 'smoke'}), encoding='utf-8')
    config = _load_chain(path, tmp_path, (), {})
    assert config['schema_version'] == 3 and config['edit']['max_source_images'] == 1
    assert config['vlm']['backends']['main']['image']['max_images_per_call'] == 1


def test_an_unknown_schema_version_is_refused(tmp_path):
    (tmp_path / 'bad.yaml').write_text('schema_version: 4\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match=r'schema_version: expected one of \[1, 2, 3\]'):
        _load_chain(tmp_path / 'bad.yaml', tmp_path, (), {})


# ---- a request can raise no limit ----------------------------------------------------------------------------

@pytest.mark.parametrize('key', ['edit.max_source_images', 'vlm.backends.main.image.max_images_per_call',
                                 'vlm.backends.other.image.max_images_per_call', 'renderer.max_source_images'])
def test_a_limit_in_allowed_keys_is_refused_at_load_naming_the_key(model_test_profile, tmp_path, project_root, key):
    config = v3(model_test_profile, tmp_path)
    config['request_overrides']['allowed_keys'] = [key]
    schema = _configuration_schema(project_root, config)
    with pytest.raises(ConfigurationError, match=f'request_overrides.allowed_keys: {key}: a request cannot raise a source image limit'):
        _validate_override_policy(config, schema, 'configuration')
    with pytest.raises(ConfigurationError, match='request_overrides.allowed_keys'):
        _validate_loaded_config(config, 'enhance once', project_root, 'configuration', {})


def test_a_per_request_validation_reads_no_prompt_file_unless_an_override_names_a_multi_source_key(
        model_test_profile, tmp_path, project_root, monkeypatch):
    from prompt_enhancer import profile_prompt
    from prompt_enhancer.config import effective_request_config

    def forbidden(*args, **kwargs):
        raise AssertionError('a prompt file was read')

    config = v3(model_test_profile, tmp_path)
    monkeypatch.setattr(profile_prompt, 'load_prompt', forbidden)
    effective_request_config(config, {}, project_root=project_root)  # the base was validated at load
    with pytest.raises(AssertionError):  # the same validation with prompts on does read them
        validate_source_limits(config, project_root=project_root)


def test_other_keys_in_allowed_keys_still_pass_and_the_matcher_is_exact(model_test_profile, tmp_path, project_root):
    config = v3(model_test_profile, tmp_path)
    config['request_overrides']['allowed_keys'] = ['edit.max_kb_reference_images']
    assert _validate_override_policy(config, _configuration_schema(project_root, config), 'c') == ['edit.max_kb_reference_images']
    assert not _is_source_limit_key('edit.max_kb_reference_images')
    assert not _is_source_limit_key('vlm.backends.main.image.max_side')
    assert not _is_source_limit_key('vlm.backends.main.image.max_images_per_call.x')


def test_a_request_override_naming_a_limit_is_refused_with_the_default_policy(model_test_profile, tmp_path, project_root):
    from prompt_enhancer.config import effective_request_config
    config = v3(model_test_profile, tmp_path)
    for key in ('edit.max_source_images', 'vlm.backends.main.image.max_images_per_call'):
        with pytest.raises(ConfigurationError, match='request configuration validation failed'):
            effective_request_config(config, {key: 99}, project_root=project_root)


# ---- property tests ------------------------------------------------------------------------------------------

LIMIT_VALUES = st.one_of(st.integers(min_value=-5, max_value=2 ** 63 + 5), st.booleans(), st.floats(allow_nan=True),
                         st.text(max_size=6), st.none(), st.lists(st.integers(), max_size=2),
                         st.sampled_from(sorted(SENTINELS)))


@given(LIMIT_VALUES)
def test_the_limit_validator_accepts_exactly_positive_non_bool_integers(value):
    expected = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= sys.maxsize
    try:
        check_limit(value, 'configuration', 'edit.max_source_images')
        accepted = True
    except ConfigurationError as error:
        accepted = False
        assert 'edit.max_source_images' in str(error)  # every refusal names the key
    assert accepted == expected
