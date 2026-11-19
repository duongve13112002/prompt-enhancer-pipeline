"""Configuration of the command line (T-016, R-015, R-025): `serving.http.max_request_bytes` and the read set of every
command added or newly consumed. Every key is checked three ways: a valid value is used (override), a missing or
unresolved one is refused before any side effect (refusal), and a command that does not read it ignores it."""
from copy import deepcopy
import json
import sys

import pytest

from cli_fixtures import pipeline_config, write_request, write_yaml
from prompt_enhancer.__main__ import main
from prompt_enhancer.config import (COMMAND_READS, REQUEST_LIMIT_KEY, VALIDATION_READS, ConfigurationError, load_config,
                                    validate_request_limit)


def with_limit(config, value):
    config = deepcopy(config)
    config['serving']['http'] = {'max_request_bytes': value}
    return config


def set_path(config, dotted, value):
    *parents, leaf = dotted.split('.')
    node = config
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


@pytest.mark.parametrize('value', [1, 4096, 23_068_672, sys.maxsize])
def test_limit_accepts_a_positive_integer_and_returns_it(model_test_profile, tmp_path, project_root, value):
    config = with_limit(pipeline_config(model_test_profile, tmp_path, project_root, 1), value)
    assert validate_request_limit(config, project_root=project_root) == value


@pytest.mark.parametrize('value, fragment', [
    ('deploy_required', 'unresolved sentinel deploy_required'), ('ask_user', 'unresolved sentinel ask_user'), (True, REQUEST_LIMIT_KEY), (False, REQUEST_LIMIT_KEY),
    (0, REQUEST_LIMIT_KEY), (-1, REQUEST_LIMIT_KEY), (1.5, REQUEST_LIMIT_KEY), (2.0, REQUEST_LIMIT_KEY),
    ('10', REQUEST_LIMIT_KEY), (None, REQUEST_LIMIT_KEY), (sys.maxsize + 1, REQUEST_LIMIT_KEY), ([5], REQUEST_LIMIT_KEY),
])
def test_limit_refuses_an_unresolved_or_malformed_value_and_names_the_key(model_test_profile, tmp_path, project_root,
                                                                         value, fragment):
    config = with_limit(pipeline_config(model_test_profile, tmp_path, project_root, 1), value)
    with pytest.raises(ConfigurationError) as caught:
        validate_request_limit(config, project_root=project_root)
    assert fragment in str(caught.value) and REQUEST_LIMIT_KEY in str(caught.value)


@pytest.mark.parametrize('shape', ['no_serving', 'no_http', 'http_not_object', 'extra_key', 'no_limit'])
def test_limit_refuses_a_missing_or_misshaped_block(model_test_profile, tmp_path, project_root, shape):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    if shape == 'no_serving':
        del config['serving']
    elif shape == 'no_http':
        del config['serving']['http']
    elif shape == 'http_not_object':
        config['serving']['http'] = 5
    elif shape == 'extra_key':
        config['serving']['http']['unknown'] = 1
    else:
        config['serving']['http'] = {}
    with pytest.raises(ConfigurationError):
        validate_request_limit(config, project_root=project_root)


def test_the_shipped_profiles_leave_the_limit_to_the_deployment_except_the_dev_profile(project_root):
    """The example and the vLLM overlay hold the sentinel; the dev example holds a measured, derived value."""
    import yaml
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for name in ('configs/enhancer.example.yaml', 'configs/overlays/deploy-vllm.example.yaml'):
        assert yaml.safe_load((repo / name).read_bytes())['serving']['http']['max_request_bytes'] == 'deploy_required'
    dev = yaml.safe_load((repo / 'configs/enhancer.dev.example.yaml').read_bytes())['serving']['http']['max_request_bytes']
    assert type(dev) is int and dev == 23_068_672


def test_the_limit_is_read_by_the_command_line_not_by_the_library_read_set(model_test_profile, tmp_path, project_root):
    """A library caller or golden check never receives a request file, so it needs no value (deviation recorded)."""
    assert not any(key.startswith('serving') for key in COMMAND_READS['enhance once'])
    config = with_limit(pipeline_config(model_test_profile, tmp_path, project_root, 1), 'deploy_required')
    path = write_yaml(project_root, tmp_path, config)
    load_config(path, 'enhance once', project_root=project_root)  # loads: the unresolved limit is not in its read set
    with pytest.raises(ConfigurationError):
        validate_request_limit(load_config(path, 'enhance once', project_root=project_root), project_root=project_root)


def run_main(capsys, argv, project_root):
    code = main([*map(str, argv), '--project-root', str(project_root)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


COMMANDS = {
    'kb status': ['kb', 'status'],
    'kb show': ['kb', 'show'],
    'kb rollback': ['kb', 'rollback'],
    'kb gc': ['kb', 'gc'],
    'profile register': ['profile', 'register'],
    'profile check': ['profile', 'check'],
    'enhance once': ['enhance', 'once'],
}
TAIL = {'kb show': ['Name'], 'kb rollback': ['s20200101T000000000000Z-00000000'],
        'profile register': ['--profile-id', 'p1'], 'profile check': ['--profile-id', 'p1']}
# `enhance once` reads its documented set with these narrowings (config._command_reads): the validation block is read
# leaf by leaf, the verifier role at S5/S6 (T-014), the runtime block by its request keys, and the retrieval keys only
# when `rag.enabled`; the matrix therefore runs it with retrieval on and the narrowed leaves.
ONCE_NARROWED = {'validation': VALIDATION_READS, 'runtime': ('runtime.request_deadline_s', 'runtime.trace'),
                 'vlm.roles.verifier': ()}
CASES = [(command, key) for command in COMMANDS for key in (
    [leaf for name in COMMAND_READS[command] for leaf in ONCE_NARROWED.get(name, (name,))]
    if command == 'enhance once' else COMMAND_READS[command])]


@pytest.mark.parametrize('command, key', CASES, ids=[f'{command}:{key}' for command, key in CASES])
def test_every_documented_read_of_a_command_is_refused_when_unresolved(
        capsys, model_test_profile, tmp_path, project_root, command, key):
    """Consumed-leaf matrix: the key set to `deploy_required` refuses with exit 2, nothing created, no output."""
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1, retrieval=command == 'enhance once')
    config['storage']['kb_store'] = (tmp_path / 'kb-store').relative_to(project_root).as_posix()
    config['sync']['gc']['retention'] = '1d'  # test-only: the profile's own sentinel must not hide the key under test
    set_path(config, key, 'deploy_required')
    path = write_yaml(project_root, tmp_path, config)
    extra = ['--request', write_request(tmp_path, {'prompt': 'p', 'task': 'generate'})] if command == 'enhance once' else []
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*'))
    code, out, err = run_main(capsys, [*COMMANDS[command], '--config', path, *TAIL.get(command, []), *extra], project_root)
    assert code == 2 and out == '' and err.startswith('refused: '), (code, err)
    assert key.split('.')[-1] in err or key in err
    assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*')) == before  # no store, data root or trace


def test_a_key_outside_a_commands_read_set_does_not_stop_it(capsys, model_test_profile, tmp_path, project_root):
    """`kb status` reads only storage.kb_store and storage.lock: an unresolved model setting does not refuse it."""
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    config['storage']['kb_store'] = (tmp_path / 'kb-store').relative_to(project_root).as_posix()
    set_path(config, 'vlm.roles.planner', 'deploy_required')
    set_path(config, 'serving.http.max_request_bytes', 'deploy_required')
    code, out, err = run_main(capsys, ['kb', 'status', '--config', write_yaml(project_root, tmp_path, config)], project_root)
    assert code == 0, err
    assert json.loads(out)['live_source_changes'] == 'not_checked'
