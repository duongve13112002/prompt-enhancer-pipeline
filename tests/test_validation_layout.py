"""T017-FIX F1: the S5 repair request uses the same addendum layout as the S4 draft (R-004).

Labelled CPU test: the production writer, S5 and model client run; the HTTP transport is a scripted in-memory double.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from prompt_enhancer.model_client import CAPABILITIES, BoundModelClient, RoleClient
from prompt_enhancer.model_mapping import effective_system_hash
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.validation import validate
from prompt_enhancer.writer import write
from test_planner_generate_schema import SequenceTransport
from validation_fixtures import environment

LAYOUTS = {'separate': {'addendum_layout': 'separate_system_message'},
           'joined': {'addendum_layout': 'joined_system_message', 'addendum_separator': '\n--\n'}}


@pytest.mark.parametrize('name', sorted(LAYOUTS))
def test_s5_repair_sends_the_same_system_texts_as_the_draft(model_test_profile, tmp_path, project_root, name):
    env = environment(model_test_profile, tmp_path, project_root)
    main = env.config['vlm']['backends']['main']
    main.pop('addendum_separator', None)
    main.update(LAYOUTS[name])
    env.config['writer']['runtime_addendum'] = True
    env.s0.profile.addendum = {'id': 'test-addendum', 'text': b'ADDENDUM\r\n'}
    env.s0.profile.validator_checks = ('exact',)
    env.s0.profile.validator_artifacts = ({'id': 'exact', 'operator': 'exact_text',
                                           'parameters': {'pointer': '', 'value': 'kept'}},)
    transport = SequenceTransport('bad structure', 'kept')  # the draft fails the structural check, the repair passes
    probes = {backend: ProbeResult('complete', dict.fromkeys(CAPABILITIES, True), {}) for backend in ('main', 'check')}
    client = RoleClient(env.config, {'main': transport, 'check': transport}, probes,
                        SchemaRegistry(project_root / 'contracts' / 'schemas'), project_root=project_root)
    bound = BoundModelClient(SimpleNamespace(client=client), 'writer')

    async def run():
        written = await write(env.s0, env.s2, None, bound, project_root=project_root)
        return written, await validate(env.s0, env.s2, None, written, bound, env.verifier, project_root=project_root)
    written, checked = asyncio.run(run())
    assert checked['status'] == 'repaired' and checked['repair_count'] == 1 and len(transport.bodies) == 2
    admin = env.s0.profile.system_message.decode('utf-8')
    expected = ([admin, 'ADDENDUM\r\n'] if name == 'separate' else [admin + '\n--\nADDENDUM\r\n'])
    for body in transport.bodies:
        assert [m['content'] for m in body['messages'] if m['role'] == 'system'] == expected
        assert body['messages'][0]['content'].encode('utf-8').startswith(env.s0.profile.system_message)
    hashes = {json.loads(call['record_json'])['effective_system_hash'] for call in checked['calls']}
    assert hashes == {effective_system_hash('openai_compatible', transport.bodies[0])}
    assert written['effective_system_hash'] == effective_system_hash('openai_compatible', transport.bodies[0])
