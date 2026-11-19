"""T017-FIX F1: `vlm.backends.<name>.addendum_layout` and `addendum_separator` as validated profile values.

Labelled CPU tests with synthetic configurations and in-memory transports; no server, network or model call.
"""

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from prompt_enhancer.config import ADDENDUM_LAYOUTS, ConfigurationError, validate_writer_reads
from prompt_enhancer.model_client import RoleClient
from prompt_enhancer.model_probe import ProbeResult
from test_model_client import DoubleTransport
from test_writer import setup

ROOT = Path(__file__).resolve().parents[1]


def main(config):
    return config['vlm']['backends']['main']


def writer_call(config, addendum=b'ADDENDUM'):
    """Send one request with the writer role's settings through a labelled double transport; return the posted body.

    The role is registered under a plain name so the client's manifest gate (writer-only) does not apply; the backend
    and its layout are the validated configuration's.
    """
    role = deepcopy(config['vlm']['roles']['writer'])
    role['output'] = dict(role['output'], format='none', schema_id='none')
    role['image'] = 'none'
    client_config = {'vlm': {'roles': {'layout_probe': role}, 'backends': deepcopy(config['vlm']['backends']),
                             'default_backend': config['vlm']['default_backend']}, 'vision': config['vision']}
    transport = DoubleTransport('draft')
    client = RoleClient(client_config, {name: transport for name in client_config['vlm']['backends']},
                        {name: ProbeResult('complete', {}, {}) for name in client_config['vlm']['backends']}, None)
    asyncio.run(client.call('layout_probe', b'ADMIN', 'data', addendum=addendum))
    return transport.body


def test_default_test_layout_sends_two_system_messages(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    validate_writer_reads(config, project_root=project_root)
    assert main(config)['addendum_layout'] == 'separate_system_message'
    assert writer_call(config)['messages'][:2] == [{'role': 'system', 'content': 'ADMIN'},
                                                   {'role': 'system', 'content': 'ADDENDUM'}]


@pytest.mark.parametrize('separator', ['\n\n', '\r\n---\r\n'])
def test_joined_layout_override_reaches_the_request(model_test_profile, tmp_path, project_root, separator):
    """Non-default override: joined layout with a non-default separator changes the posted system text."""
    config = setup(model_test_profile, tmp_path, project_root)
    main(config).update(addendum_layout='joined_system_message', addendum_separator=separator)
    validate_writer_reads(config, project_root=project_root)
    messages = writer_call(config)['messages']
    assert messages[0] == {'role': 'system', 'content': 'ADMIN' + separator + 'ADDENDUM'}
    assert [message['role'] for message in messages] == ['system', 'user']


def test_missing_layout_refuses_naming_key_backend_and_reason(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    del main(config)['addendum_layout']
    with pytest.raises(ConfigurationError) as caught:
        validate_writer_reads(config, project_root=project_root)
    message = str(caught.value)
    assert 'vlm.backends.main.addendum_layout: missing key' in message
    assert 'openai_compatible chat backend' in message


@pytest.mark.parametrize('key, value', [
    ('addendum_layout', 'deploy_required'),
    ('addendum_separator', 'deploy_required'),
    ('addendum_separator', 'pin_required'),
])
def test_sentinel_layout_or_separator_refuses_at_start(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    main(config).update(addendum_layout='joined_system_message', addendum_separator='\n\n')
    main(config)[key] = value
    with pytest.raises(ConfigurationError, match=f'vlm.backends.main.*{value}'):
        validate_writer_reads(config, project_root=project_root)


def test_joined_layout_without_separator_refuses(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    main(config)['addendum_layout'] = 'joined_system_message'
    with pytest.raises(ConfigurationError) as caught:
        validate_writer_reads(config, project_root=project_root)
    assert str(caught.value).endswith('vlm.backends.main.addendum_separator: missing key: required by '
                                      'addendum_layout joined_system_message')


def test_sentinel_separator_with_separate_layout_refuses(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    main(config)['addendum_separator'] = 'deploy_required'
    with pytest.raises(ConfigurationError, match='vlm.backends.main.addendum_separator: unresolved sentinel'):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('separator', ['\n\n', ' ', 'None', 'NONE', ' none', 'none\n'])
def test_separator_with_separate_layout_refuses(model_test_profile, tmp_path, project_root, separator):
    """Decision 193: under the separate layout only the exact reserved `none` is accepted; a real separator (or a near
    spelling of `none`) would be a setting nothing reads, so it is refused, never ignored."""
    config = setup(model_test_profile, tmp_path, project_root)
    main(config)['addendum_separator'] = separator
    with pytest.raises(ConfigurationError) as caught:
        validate_writer_reads(config, project_root=project_root)
    assert str(caught.value).endswith('vlm.backends.main.addendum_separator: refused: addendum_layout '
                                      'separate_system_message sends no separator (state none or leave the key out)')


def test_none_separator_with_joined_layout_refuses(model_test_profile, tmp_path, project_root):
    """Decision 193: `none` belongs to the separate layout; the joined layout needs a real separator."""
    config = setup(model_test_profile, tmp_path, project_root)
    main(config).update(addendum_layout='joined_system_message', addendum_separator='none')
    with pytest.raises(ConfigurationError) as caught:
        validate_writer_reads(config, project_root=project_root)
    assert str(caught.value).endswith('vlm.backends.main.addendum_separator: refused: none sends no separator, and '
                                      'addendum_layout joined_system_message needs one between the admin prompt and '
                                      'the addendum')


def test_none_separator_with_separate_layout_sends_two_system_messages(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    main(config)['addendum_separator'] = 'none'
    validate_writer_reads(config, project_root=project_root)
    body = writer_call(config)
    assert body['messages'][:2] == [{'role': 'system', 'content': 'ADMIN'}, {'role': 'system', 'content': 'ADDENDUM'}]
    assert not any('none' in json.dumps(message['content']) for message in body['messages'] if message['role'] == 'system')


EXAMPLE_PROFILES = ('configs/enhancer.example.yaml', 'configs/enhancer.dev.example.yaml')


def profile_tree(tmp_path):
    """A project root under `tmp_path` holding copies of the two example profiles only (about 45 KB), so the loader's
    inside-the-root check holds wherever pytest puts its basetemp (CI: outside the checkout, another drive on
    Windows)."""
    tree = tmp_path / 'tree'
    for relative in EXAMPLE_PROFILES:
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    return tree


def child_main(tmp_path, project_root, parent, own):
    """Merge a child profile file over `parent` with the real loader chain (`config._load_chain`, `extends`) and
    return the child's resolved `vlm.backends.main`. A relative `parent` names an example profile, copied into the
    tree; an absolute one is a file the test wrote inside `profile_tree(tmp_path)`."""
    from prompt_enhancer.config import _load_chain

    tree = profile_tree(tmp_path)
    parent_path = Path(parent) if Path(parent).is_absolute() else tree / parent
    child = tree / 'child.yaml'
    child.write_text(yaml.safe_dump({'schema_version': yaml.safe_load(parent_path.read_bytes())['schema_version'],
                                     'extends': os.path.relpath(parent_path, tree).replace(os.sep, '/'),
                                     'vlm': {'backends': {'main': own}}}), encoding='utf-8')
    return main(_load_chain(child, tree, (), {}))


def apply_layout(config, resolved):
    for key in ('addendum_layout', 'addendum_separator'):
        main(config).pop(key, None)
        if key in resolved:
            main(config)[key] = resolved[key]


def test_a_child_of_the_dev_example_switches_to_separate_with_none(model_test_profile, tmp_path, project_root):
    """Decision 193 override: the dev example is joined with "\\n\\n" (decision 190); a child cannot delete the inherited
    separator, so it states `none`, and the request carries two system messages, admin first and unchanged."""
    resolved = child_main(tmp_path, project_root, 'configs/enhancer.dev.example.yaml',
                          {'addendum_layout': 'separate_system_message', 'addendum_separator': 'none'})
    assert resolved['addendum_layout'] == 'separate_system_message' and resolved['addendum_separator'] == 'none'
    config = setup(model_test_profile, tmp_path, project_root)
    apply_layout(config, resolved)
    validate_writer_reads(config, project_root=project_root)
    admin = 'ADMIN é\r\n﻿'.encode('utf-8')
    role = deepcopy(config['vlm']['roles']['writer'])
    role['output'] = dict(role['output'], format='none', schema_id='none')
    role['image'] = 'none'
    client_config = {'vlm': {'roles': {'layout_probe': role}, 'backends': deepcopy(config['vlm']['backends']),
                             'default_backend': config['vlm']['default_backend']}, 'vision': config['vision']}
    transport = DoubleTransport('draft')
    client = RoleClient(client_config, {name: transport for name in client_config['vlm']['backends']},
                        {name: ProbeResult('complete', {}, {}) for name in client_config['vlm']['backends']}, None)
    asyncio.run(client.call('layout_probe', admin, 'data', addendum=b'ADDENDUM'))
    messages = transport.body['messages']
    assert messages[0] == {'role': 'system', 'content': admin.decode('utf-8')}
    assert messages[1] == {'role': 'system', 'content': 'ADDENDUM'}
    assert [message['role'] for message in messages] == ['system', 'system', 'user']


def test_a_child_of_the_dev_example_that_keeps_the_separator_cannot_pick_separate(model_test_profile, tmp_path,
                                                                                  project_root):
    """The inherited "\\n\\n" is refused under separate: the switch must state `none`, it is never assumed."""
    resolved = child_main(tmp_path, project_root, 'configs/enhancer.dev.example.yaml',
                          {'addendum_layout': 'separate_system_message'})
    config = setup(model_test_profile, tmp_path, project_root)
    apply_layout(config, resolved)
    with pytest.raises(ConfigurationError, match='addendum_separator: refused: addendum_layout separate_system_message'):
        validate_writer_reads(config, project_root=project_root)


def test_a_child_of_a_separate_profile_switches_back_to_joined(model_test_profile, tmp_path, project_root):
    parent = profile_tree(tmp_path) / 'separate.yaml'
    parent.write_text(yaml.safe_dump({'schema_version': 2, 'extends': 'configs/enhancer.dev.example.yaml',
                                      'vlm': {'backends': {'main': {
            'addendum_layout': 'separate_system_message', 'addendum_separator': 'none'}}}}), encoding='utf-8')
    resolved = child_main(tmp_path, project_root, parent,
                          {'addendum_layout': 'joined_system_message', 'addendum_separator': ' | '})
    config = setup(model_test_profile, tmp_path, project_root)
    apply_layout(config, resolved)
    validate_writer_reads(config, project_root=project_root)
    assert writer_call(config)['messages'][0] == {'role': 'system', 'content': 'ADMIN | ADDENDUM'}


@pytest.mark.parametrize('separator', ['deploy_required', 'none', '\n\n'])
def test_a_sentinel_layout_is_refused_at_start_whatever_the_separator(model_test_profile, tmp_path, project_root,
                                                                     separator):
    """The base example's `deploy_required` layout (with its `deploy_required` separator, or any separator) is refused
    by the start gate, never defaulted to either layout."""
    config = setup(model_test_profile, tmp_path, project_root)
    main(config).update(addendum_layout='deploy_required', addendum_separator=separator)
    with pytest.raises(ConfigurationError, match='vlm.backends.main.addendum_(layout|separator): unresolved sentinel '
                                                 'deploy_required'):
        validate_writer_reads(config, project_root=project_root)


def test_the_base_example_addendum_keys_are_refused_at_start(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    apply_layout(config, main(load_yaml('configs/enhancer.example.yaml')))
    with pytest.raises(ConfigurationError, match='vlm.backends.main.addendum_(layout|separator): unresolved sentinel '
                                                 'deploy_required'):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('key, value', [
    ('addendum_layout', 'two_system_messages'),
    ('addendum_layout', ''),
    ('addendum_separator', ''),
    ('addendum_separator', 2),
])
def test_malformed_values_refuse_by_schema(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    main(config).update(addendum_layout='joined_system_message', addendum_separator='\n\n')
    main(config)[key] = value
    with pytest.raises(ConfigurationError, match=f'vlm.backends.main'):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('provider, extra', [
    ('anthropic', {'anthropic_version': '2023-06-01'}),
    ('gemini', {}),
])
@pytest.mark.parametrize('key', ['addendum_layout', 'addendum_separator'])
def test_cloud_backend_refuses_both_keys(model_test_profile, tmp_path, project_root, provider, extra, key):
    """A cloud backend sends native system blocks, so the keys would be settings nothing reads."""
    config = setup(model_test_profile, tmp_path, project_root)
    cloud = deepcopy(main(config))
    cloud.pop('addendum_layout')
    cloud.update(provider=provider, **extra)
    cloud[key] = 'joined_system_message' if key == 'addendum_layout' else '\n\n'
    config['vlm']['backends']['cloud'] = cloud
    with pytest.raises(ConfigurationError) as caught:
        validate_writer_reads(config, project_root=project_root)
    assert f'vlm.backends.cloud.{key}: refused: {provider} sends native system blocks' in str(caught.value)


@pytest.mark.parametrize('key', ['addendum_layout', 'addendum_separator'])
def test_chatless_backend_refuses_both_keys(model_test_profile, tmp_path, project_root, key):
    config = setup(model_test_profile, tmp_path, project_root)
    embedder = config['vlm']['backends']['embedder']
    embedder[key] = 'separate_system_message' if key == 'addendum_layout' else '\n\n'
    with pytest.raises(ConfigurationError) as caught:
        validate_writer_reads(config, project_root=project_root)
    assert (f'vlm.backends.embedder.{key}: refused: the backend has no endpoints.chat, so it never receives an '
            'addendum') in str(caught.value)


@pytest.mark.parametrize('layout', [{'addendum_layout': 'separate_system_message', 'addendum_separator': 'none'},
                                    {'addendum_layout': 'joined_system_message', 'addendum_separator': ' | '}])
def test_a_child_of_the_base_example_can_choose_either_layout(model_test_profile, tmp_path, project_root, layout):
    """Lineage through the real loader chain: a child of the base example (both keys `deploy_required`) that states
    both keys is valid for either layout."""
    resolved = child_main(tmp_path, project_root, 'configs/enhancer.example.yaml', layout)
    assert {key: resolved[key] for key in layout} == layout
    config = setup(model_test_profile, tmp_path, project_root)
    apply_layout(config, resolved)
    validate_writer_reads(config, project_root=project_root)


def test_a_child_of_the_base_example_that_states_only_the_layout_is_refused(model_test_profile, tmp_path,
                                                                           project_root):
    """The inherited `deploy_required` separator is never defaulted: the start gate refuses it for either layout, and
    the binding rule alone (which also runs where no start gate applies) refuses it under separate."""
    from prompt_enhancer.config import _validate_addendum_layout

    for layout in ADDENDUM_LAYOUTS:
        resolved = child_main(tmp_path, project_root, 'configs/enhancer.example.yaml', {'addendum_layout': layout})
        config = setup(model_test_profile, tmp_path, project_root)
        apply_layout(config, resolved)
        with pytest.raises(ConfigurationError, match='addendum_separator: unresolved sentinel deploy_required'):
            validate_writer_reads(config, project_root=project_root)
        if layout == 'separate_system_message':
            with pytest.raises(ConfigurationError, match='addendum_separator: refused: addendum_layout separate'):
                _validate_addendum_layout('main', main(config), 'configuration')


def load_yaml(relative):
    return yaml.safe_load((ROOT / relative).read_bytes())


def test_example_profiles_state_the_layout():
    """Base and overlay examples hold deploy_required for both keys (decision 193: every key appears in the example);
    the dev example is joined with "\\n\\n" (decision 190, J4a2 evidence)."""
    base = main(load_yaml('configs/enhancer.example.yaml'))
    assert base['addendum_layout'] == base['addendum_separator'] == 'deploy_required'
    overlay = load_yaml('configs/overlays/deploy-vllm.example.yaml')['vlm']['backends']['main']
    assert overlay['addendum_layout'] == overlay['addendum_separator'] == 'deploy_required'
    dev = main(load_yaml('configs/enhancer.dev.example.yaml'))
    assert dev['addendum_layout'] == 'joined_system_message' and dev['addendum_separator'] == '\n\n'
    text = (ROOT / 'configs/enhancer.dev.example.yaml').read_text(encoding='utf-8')
    assert 'decision 190' in text.split('addendum_separator:', 1)[1].splitlines()[0]


@pytest.mark.parametrize('own', [{'addendum_layout': 'separate_system_message', 'addendum_separator': 'none'},
                                 {'addendum_layout': 'joined_system_message', 'addendum_separator': '\n\n'}])
def test_the_deploy_overlay_resolves_and_accepts_either_layout(tmp_path, project_root, own):
    """The deploy overlay still applies over the base example (both keys stay deploy-time values), and an overlay may
    state either resolved layout, including `none` for separate."""
    from prompt_enhancer.config import _load_chain, _overlay

    resolved = main(_load_chain(ROOT / 'configs/overlays/deploy-vllm.example.yaml', ROOT, (), {}))
    assert resolved['addendum_layout'] == resolved['addendum_separator'] == 'deploy_required'
    _overlay({'schema_version': 1, 'overlay_of': '../enhancer.example.yaml',
              'vlm': {'backends': {'main': dict(own)}}}, tmp_path / 'overlay.yaml')
