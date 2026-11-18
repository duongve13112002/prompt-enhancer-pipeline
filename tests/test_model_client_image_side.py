"""Decision 144 (per-backend image cap) and role image sources, with a labelled in-memory transport double."""

import asyncio
import base64
from copy import deepcopy
from io import BytesIO
from pathlib import Path

from hypothesis import given, strategies as st
import httpx
from PIL import Image
import pytest
import yaml

from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.model_client import (IMAGE_SOURCE_ROLES, ModelCallError, RoleClient, image_max_side,
                                          semantic_role_settings, _digest)
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_schemas import SchemaRegistry

SCHEMAS = Path(__file__).resolve().parents[1] / 'contracts' / 'schemas'


class TransportDouble:
    """Labelled double: records the last body; answers a plain text completion."""

    def __init__(self):
        self.body = None

    async def post(self, route, body, headers):
        self.body = body
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]},
                              request=httpx.Request('POST', 'http://127.0.0.1/chat'))


def png(width, height):
    stream = BytesIO()
    Image.new('RGB', (width, height), 'green').save(stream, format='PNG')
    return stream.getvalue()


def client(*, role_side='from_vision', vision_side='none', backend_side='none', source='source_image'):
    role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'sampling': {},
            'image': {'source': source, 'max_side': role_side, 'format': 'png', 'quality': 90},
            'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
            'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
    backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'v'},
               'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'},
               'image': {'max_decode_pixels': 10_000_000, 'max_side': backend_side}}
    config = {'vision': {'max_image_side': vision_side, 'format': 'png', 'alpha_policy': {'mode': 'refuse'}},
              'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
    double = TransportDouble()
    probe = ProbeResult('complete', {'image': True, 'multiple_images': True}, {})
    return RoleClient(config, {'main': double}, {'main': probe}, SchemaRegistry(SCHEMAS)), double


def sent_sizes(double):
    sizes = []
    for part in double.body['messages'][1]['content'][1:]:
        with Image.open(BytesIO(base64.b64decode(part['image_url']['url'].split(',', 1)[1]))) as image:
            sizes.append(image.size)
    return sizes


@pytest.mark.parametrize(('role_side', 'vision_side', 'backend_side', 'size', 'expected'), [
    ('from_vision', 1024, 512, (2000, 1000), (512, 256)),   # backend smaller than the role's inherited side
    (300, 'none', 512, (2000, 1000), (300, 150)),           # role smaller than the backend
    ('none', 'none', 'none', (2000, 1000), (2000, 1000)),   # no limit anywhere
    ('from_vision', 1024, 'none', (100, 80), (100, 80)),    # never upscaled
    ('none', 'none', 64, (100, 80), (64, 51)),              # aspect ratio kept
])
def test_call_uses_the_smaller_side(role_side, vision_side, backend_side, size, expected):
    role_client, double = client(role_side=role_side, vision_side=vision_side, backend_side=backend_side)
    value, record = asyncio.run(role_client.call('planner', b'system', 'data', images=[('source_image', png(*size))]))
    assert value == 'ok' and sent_sizes(double) == [expected]


def test_request_hash_includes_the_backend_cap():
    hashes = []
    for backend_side in ('none', 512, 256, 4096):  # 4096 leaves the used side at 1024, like none
        role_client, _ = client(backend_side=backend_side, vision_side=1024)
        _, record = asyncio.run(role_client.call('planner', b's', 'd', images=[('source_image', png(4, 4))]))
        hashes.append(record['request_hash'])
    assert len(set(hashes)) == 4
    role_client, _ = client(backend_side=512, vision_side=1024)
    _, again = asyncio.run(role_client.call('planner', b's', 'd', images=[('source_image', png(4, 4))]))
    assert again['request_hash'] == hashes[1]


ints = st.integers(min_value=1, max_value=10_000)
sides = st.one_of(st.just('none'), ints)


@given(role=st.one_of(st.just('from_vision'), sides), vision=sides, backend=sides)
def test_image_max_side_is_the_smaller_limit(role, vision, backend):
    used = image_max_side({'vision': {'max_image_side': vision}}, {'max_side': role}, {'image': {'max_side': backend}})
    resolved = vision if role == 'from_vision' else role
    limits = [value for value in (resolved, backend) if value != 'none']
    assert used == (min(limits) if limits else 'none')
    assert all(used <= value for value in limits) if limits else used == 'none'


@pytest.mark.parametrize('bad', [0, -3, True, 1.5, 'deploy_required'])
def test_image_max_side_refuses_a_non_limit(bad):
    with pytest.raises(ConfigurationError):
        image_max_side({'vision': {'max_image_side': 'none'}}, {'max_side': 'none'}, {'image': {'max_side': bad}})


@pytest.mark.parametrize(('source', 'roles', 'ok'), [
    ('edit_source_only', ['source_image'], True),
    ('edit_source_only', ['knowledge_reference'], False),
    ('edit_source_only', ['source_image', 'user_reference'], False),
    ('edit_source_and_live_look', ['source_image', 'knowledge_reference'], True),
    ('edit_source_and_live_look', ['user_reference'], False),
    ('knowledge_reference', ['knowledge_reference'], True),
    ('knowledge_reference', ['source_image'], False),
    ('unknown_source', ['source_image'], False),
])
def test_role_receives_only_its_configured_image_sources(source, roles, ok):
    role_client, double = client(source=source)
    images = [(name, png(4, 4)) for name in roles]
    if ok:
        value, record = asyncio.run(role_client.call('planner', b's', 'd', images=images))
        assert value == 'ok' and record['image_roles'] == roles
    else:
        with pytest.raises(ModelCallError, match='not permitted by vlm.roles.planner.image.source'):
            asyncio.run(role_client.call('planner', b's', 'd', images=images))
        assert double.body is None  # refused before any request


def test_schema_enum_matches_the_client_map():
    import json

    role = json.loads((SCHEMAS / 'vlm_role.v0.schema.json').read_bytes())
    enum = role['properties']['image']['anyOf'][1]['properties']['source']['enum']
    assert set(enum) == set(IMAGE_SOURCE_ROLES)


OFFLINE_ROLES = ('observer', 'folder_namer', 'info_assessor', 'card_builder', 'alias_generator')


# Test-only oracle: `_digest(semantic_role_settings(config, role))` of the base revision 637611f for the five offline
# roles of both example profiles (the base ignores the `image.max_side` key). Computed once, read only, with
#   git show 637611f:src/prompt_enhancer/model_client.py > <scratch>/old_model_client.py
# and the profile built by `example_profile` below. The full K2/K3/K4 checkpoint keys also hash tool source revisions
# and change when a hashed module changes (R-017); this pins only the model-settings component of the key.
# EKS-5 changed the observer's reply schema id to `observer_reply.v1` on purpose (old observation checkpoints must not be
# reused), so the two observer digests are recomputed from the EKS-5 profiles; the other roles keep the base digests.
BASE_DIGESTS = {
    ('enhancer.example.yaml', 'observer'): 'a36e55ed7904968ee2b2922ce53ce7a5fee96506754fb52f3ef236a04841a32f',
    ('enhancer.example.yaml', 'folder_namer'): '9baaea7e8e14dd3e36efda80b4b529edabbe7e16b9f6c1f4053b3801c4602009',
    ('enhancer.example.yaml', 'info_assessor'): 'afdcd0ee4ee0f93719bb2481087fdd8684063a539800c7025bbbe00e51241c09',
    ('enhancer.example.yaml', 'card_builder'): 'c897839be6e522c78d7cd014c14c8a509486a75a4b9dc2b61620eb46d371f81b',
    ('enhancer.example.yaml', 'alias_generator'): '814ce074d4162e072565225af6714c7a1f29917d2f946f4abf7aae8f37ec8778',
    ('enhancer.dev.example.yaml', 'observer'): '34722491f3ef6bcbb0671d34c4fc7b3b395e697cce1343befd5cbc9c517c0388',
    ('enhancer.dev.example.yaml', 'folder_namer'): 'f89eea79fecc66be4727f7ff0dc60b979c7e8db7858a07e9b1000c95c4d75843',
    ('enhancer.dev.example.yaml', 'info_assessor'): 'e6921e00e6dd05ca7d8bbf8ef67e55c9002dc7c43593e8086475157d315a46f1',
    ('enhancer.dev.example.yaml', 'card_builder'): '939b80fb51af261b39c3a9331cdb7bedd081939fe6b885baaa4aea63954ed847',
    ('enhancer.dev.example.yaml', 'alias_generator'): '816f49919858b1fefa6b4b618999d6fedf6204d2cd9deca4ef9989cf41ca780f',
}


def example_profile(name):
    """An example profile with its `extends` merged and the unset engine and API version pinned (test-only values)."""
    configs = Path(__file__).resolve().parents[1] / 'configs'

    def merge(base, over):
        for key, value in over.items():
            base[key] = merge(base.get(key, {}), value) if isinstance(value, dict) and isinstance(base.get(key), dict) \
                else value
        return base

    config = yaml.safe_load((configs / name).read_bytes())
    if 'extends' in config:
        config = merge(yaml.safe_load((configs / config['extends']).read_bytes()),
                       {key: value for key, value in config.items() if key != 'extends'})
    for backend in config['vlm']['backends'].values():
        if backend['provider'] == 'openai_compatible':
            backend['engine'] = {'name': 'llama_cpp', 'version': 'probe'}
        if backend['provider'] == 'anthropic':
            backend['anthropic_version'] = 'probe'
    return config


@pytest.mark.parametrize(('profile', 'role'), sorted(BASE_DIGESTS))
def test_offline_reply_identity_equals_the_base_digest_while_the_cap_is_none(profile, role):
    """K2/K3/K4 checkpoint keys digest semantic_role_settings: with every cap `none` the digest is the one the base
    revision 637611f produced, so this component of existing checkpoint keys is unchanged."""
    config = example_profile(profile)
    assert all(backend['image']['max_side'] == 'none' for backend in config['vlm']['backends'].values())
    assert _digest(semantic_role_settings(config, role)) == BASE_DIGESTS[(profile, role)]


@pytest.mark.parametrize('role', OFFLINE_ROLES)
def test_offline_reply_identity_changes_with_a_real_cap(model_test_profile, role):
    """A real cap changes the pixels sent, so it enters the reply identity of a role that sends images."""
    config = deepcopy(model_test_profile)
    settings = semantic_role_settings(config, role)
    assert set(settings['backend']) == {'provider', 'engine', 'model', 'extra_body'}
    before = _digest(settings)
    config['vlm']['backends']['main']['image']['max_side'] = 512
    capped = semantic_role_settings(config, role)
    if isinstance(config['vlm']['roles'][role]['image'], dict):
        assert capped['backend']['image_max_side'] == 512 and _digest(capped) != before
    else:
        assert _digest(capped) == before  # a text-only role sends no image, so the cap is irrelevant
