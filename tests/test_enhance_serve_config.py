"""Configuration of `enhance serve` (T-016 P3, R-015, R-025): every consumed `serving` leaf has a validated value, an
override test (a non-default value is the value the command uses) and a refusal test (missing, unresolved or malformed:
refused before anything starts, naming the key). `serving.prefix_cache` is not consumed (PM answer Q2)."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest
import yaml

from cli_fixtures import write_yaml
from prompt_enhancer.__main__ import main
from prompt_enhancer.config import COMMAND_READS, ConfigurationError, load_config, validate_serving
from serve_fixtures import free_port, serve_config

LEAVES = ('host', 'port', 'max_concurrency', 'queue_limit', 'total_deadline_s', 'http.max_connections',
          'http.read_timeout_s', 'http.write_timeout_s', 'http.shutdown_timeout_s', 'http.max_request_bytes')
OVERRIDES = {'host': ['localhost', '::1', '0.0.0.0', 'model-gateway.example.internal'], 'port': [1, 8081, 65535],
             'max_concurrency': [1, 3, 8], 'queue_limit': [0, 2, 9], 'total_deadline_s': [25, 90.5, 3600],
             'http.max_connections': [20, 64], 'http.read_timeout_s': [0.5, 2, 30.5],
             'http.write_timeout_s': [0.5, 2, 30.5], 'http.shutdown_timeout_s': [0.25, 3, 120],
             'http.max_request_bytes': [1, 4096, 99_999_999]}


def base(model_test_profile, tmp_path, project_root):
    config = serve_config(model_test_profile, tmp_path, project_root, 1, free_port())
    config['serving']['http']['max_connections'] = 4000  # room for every override below
    return config


def put(config, leaf, value):
    node = config['serving']
    *parents, name = leaf.split('.')
    for part in parents:
        node = node[part]
    node[name] = value


def drop(config, leaf):
    node = config['serving']
    *parents, name = leaf.split('.')
    for part in parents:
        node = node[part]
    del node[name]


def key(leaf):
    return f'serving.{leaf}'


@pytest.mark.parametrize('leaf, value', [(leaf, value) for leaf in LEAVES for value in OVERRIDES[leaf]])
def test_every_serving_leaf_override_is_the_value_the_command_uses(model_test_profile, tmp_path, project_root, leaf, value):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, value)
    if leaf == 'http.max_connections':
        config['serving']['max_concurrency'], config['serving']['queue_limit'] = 1, 0
    loaded = load_config(write_yaml(project_root, tmp_path, config), 'enhance serve', project_root=project_root)
    settings = validate_serving(loaded, project_root=project_root)
    name = leaf.removeprefix('http.')
    assert settings[name] == value and type(settings[name]) is type(value)


@pytest.mark.parametrize('leaf', LEAVES)
def test_an_override_changes_only_its_own_value(model_test_profile, tmp_path, project_root, leaf):
    config = base(model_test_profile, tmp_path, project_root)
    reference = validate_serving(config, project_root=project_root)
    put(config, leaf, OVERRIDES[leaf][-1] if leaf != 'http.max_connections' else 5000)
    changed = validate_serving(config, project_root=project_root)
    name = leaf.removeprefix('http.')
    assert {other for other in reference if reference[other] != changed[other]} == {name}


BAD = {
    'host': ['', ' ', 'a b', 'a/b', 'http://x', 'x' * 300, '-bad.example', 'bad-.example', 'a..b', 'a_b', 'école.example',
             '[::1]', 'host:80', 123, None, True, ['127.0.0.1']],
    'port': [0, -1, 65536, 10 ** 9, True, False, 80.0, '8080', None, [80]],
    'max_concurrency': [0, -1, True, 1.0, 2.5, '2', None, 2 ** 63],
    'queue_limit': [-1, True, 1.0, '1', None, 2 ** 63],
    'total_deadline_s': [0, -1, 0.0, True, math.inf, math.nan, '30', None, 10 ** 400],
    'http.max_connections': [0, -1, True, 1.0, '4', None, 2 ** 63],
    'http.read_timeout_s': [0, -0.5, True, math.inf, math.nan, '5', None, 10 ** 400],
    'http.write_timeout_s': [0, -0.5, True, math.inf, math.nan, '5', None, 10 ** 400],
    'http.shutdown_timeout_s': [0, -0.5, True, math.inf, math.nan, '5', None, 10 ** 400],
    'http.max_request_bytes': [0, -1, True, 1.5, '10', None, 2 ** 63],
}


@pytest.mark.parametrize('leaf, value', [(leaf, value) for leaf in LEAVES for value in BAD[leaf]],
                         ids=lambda item: repr(item)[:24])
def test_every_serving_leaf_refuses_a_malformed_value_naming_its_key(model_test_profile, tmp_path, project_root, leaf, value):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, value)
    with pytest.raises(ConfigurationError) as caught:
        validate_serving(config, project_root=project_root)
    assert key(leaf) in str(caught.value)


@pytest.mark.parametrize('leaf', LEAVES)
@pytest.mark.parametrize('sentinel', ['deploy_required', 'fit_from_bench', 'design_required', 'ask_user'])
def test_every_serving_leaf_refuses_an_unresolved_sentinel_at_load(model_test_profile, tmp_path, project_root, leaf, sentinel):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, sentinel)
    with pytest.raises(ConfigurationError) as caught:
        load_config(write_yaml(project_root, tmp_path, config), 'enhance serve', project_root=project_root)
    assert key(leaf) in str(caught.value) and sentinel in str(caught.value)


@pytest.mark.parametrize('leaf', ['host', 'port', 'http.max_connections', 'http.read_timeout_s', 'http.write_timeout_s',
                                  'http.shutdown_timeout_s', 'http.max_request_bytes', 'max_concurrency', 'queue_limit',
                                  'total_deadline_s'])
def test_every_serving_leaf_refuses_a_missing_key(model_test_profile, tmp_path, project_root, leaf):
    config = base(model_test_profile, tmp_path, project_root)
    drop(config, leaf)
    with pytest.raises(ConfigurationError) as caught:
        load_config(write_yaml(project_root, tmp_path, config), 'enhance serve', project_root=project_root)
    assert key(leaf) in str(caught.value), str(caught.value)  # the exact key, not just the block


@pytest.mark.parametrize('leaf, value', [('host', ''), ('port', 0), ('port', 65536), ('http.max_connections', 0),
                                         ('max_concurrency', 0), ('total_deadline_s', 0), ('http.read_timeout_s', 0)])
def test_the_schema_refuses_what_the_validator_refuses_at_load(model_test_profile, tmp_path, project_root, leaf, value):
    config = base(model_test_profile, tmp_path, project_root)
    put(config, leaf, value)
    with pytest.raises(ConfigurationError) as caught:
        load_config(write_yaml(project_root, tmp_path, config), 'enhance serve', project_root=project_root)
    assert key(leaf) in str(caught.value)


def test_the_connection_limit_must_hold_every_active_and_waiting_request(model_test_profile, tmp_path, project_root):
    config = base(model_test_profile, tmp_path, project_root)
    config['serving'].update(max_concurrency=3, queue_limit=2)
    config['serving']['http']['max_connections'] = 5
    assert validate_serving(config, project_root=project_root)['max_connections'] == 5  # exactly enough
    config['serving']['http']['max_connections'] = 4
    with pytest.raises(ConfigurationError, match=r'serving\.http\.max_connections: below serving\.max_concurrency \+ '
                                                 r'serving\.queue_limit \(5\)'):
        validate_serving(config, project_root=project_root)


def test_the_service_deadline_must_exceed_the_pipelines_own(model_test_profile, tmp_path, project_root):
    config = base(model_test_profile, tmp_path, project_root)
    deadline = config['runtime']['request_deadline_s']
    config['serving']['total_deadline_s'] = deadline + 0.5
    assert validate_serving(config, project_root=project_root)['total_deadline_s'] == deadline + 0.5
    for value in (deadline, deadline - 1):
        config['serving']['total_deadline_s'] = value
        with pytest.raises(ConfigurationError, match=r'serving\.total_deadline_s: must exceed runtime\.request_deadline_s'):
            validate_serving(config, project_root=project_root)


def test_prefix_cache_is_not_consumed_by_the_wrapper(model_test_profile, tmp_path, project_root):
    """It is an external-backend setup choice (docs/serving.md): the command neither reads nor checks it."""
    assert not any(read == 'serving' or read.endswith('prefix_cache') for read in COMMAND_READS['enhance serve'])
    config = base(model_test_profile, tmp_path, project_root)
    for value in (True, False, 'deploy_required'):
        config['serving']['prefix_cache'] = value
        loaded = load_config(write_yaml(project_root, tmp_path, config), 'enhance serve', project_root=project_root)
        assert 'prefix_cache' not in validate_serving(loaded, project_root=project_root)


def test_the_loader_reads_each_serving_leaf_for_serve_and_none_for_once(model_test_profile, tmp_path, project_root):
    reads = {read for read in COMMAND_READS['enhance serve'] if read.startswith('serving')}
    assert reads == {'serving.host', 'serving.port', 'serving.max_concurrency', 'serving.queue_limit',
                     'serving.total_deadline_s', 'serving.http'}
    assert not any(read.startswith('serving') for read in COMMAND_READS['enhance once'])


def set_path(config, dotted, value):
    node = config
    *parents, name = dotted.split('.')
    for part in parents:
        node = node[part]
    node[name] = value


SERVE_READS = [read for read in COMMAND_READS['enhance serve'] if read.startswith('serving')]


@pytest.mark.parametrize('read', SERVE_READS)
def test_serve_refuses_an_unresolved_leaf_before_any_side_effect(capsys, model_test_profile, tmp_path, project_root, read):
    """Through the entry point's own dispatch: exit 2, empty stdout, nothing created, no listener, no model client."""
    config = base(model_test_profile, tmp_path, project_root)
    set_path(config, read, 'deploy_required')
    path = write_yaml(project_root, tmp_path, config)
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*'))
    code = main(['enhance', 'serve', '--config', str(path), '--project-root', str(project_root)])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == '' and captured.err.startswith('refused: '), (code, captured.err)
    assert read.split('.', 1)[1].split('.')[0] in captured.err
    assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*')) == before


def test_the_shipped_profiles_leave_the_serving_deployment_values_unresolved_and_dev_holds_derived_ones(project_root):
    repo = Path(__file__).resolve().parents[1]
    names = ('host', 'port')
    http_names = ('max_request_bytes', 'max_connections', 'read_timeout_s', 'write_timeout_s', 'shutdown_timeout_s')
    for name in ('configs/enhancer.example.yaml', 'configs/overlays/deploy-vllm.example.yaml'):
        serving = yaml.safe_load((repo / name).read_bytes())['serving']
        assert [serving[item] for item in names] == ['deploy_required'] * 2, name
        assert [serving['http'][item] for item in http_names] == ['deploy_required'] * 5, name
    dev = yaml.safe_load((repo / 'configs/enhancer.dev.example.yaml').read_bytes())['serving']
    assert (dev['host'], dev['port']) == ('127.0.0.1', 8765)  # loopback and an example port, dev only
    assert dev['http'] == {'max_request_bytes': 23_068_672, 'max_connections': 4, 'read_timeout_s': 10,
                           'write_timeout_s': 10, 'shutdown_timeout_s': 10}
    assert 'max_concurrency' not in dev and 'total_deadline_s' not in dev  # unmeasured: left to the base sentinels


def test_the_base_example_names_prefix_cache_as_an_external_choice(project_root):
    repo = Path(__file__).resolve().parents[1]
    text = (repo / 'configs/enhancer.example.yaml').read_text(encoding='utf-8')
    assert 'prefix_cache: true' in text and 'the wrapper does not read it' in text
    assert isinstance(yaml.safe_load(text)['serving']['prefix_cache'], bool)
