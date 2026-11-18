"""S4/S5 library wiring through real clients and labelled in-memory HTTP transports."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

from prompt_enhancer.model_client import BoundModelClient, CAPABILITIES, RoleClient
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_schemas import cached_registry
from prompt_enhancer.validation import validate
from prompt_enhancer.writer import write
from test_model_client import DoubleTransport
from validation_fixtures import calibrate, environment


def test_real_clients_repair_with_separate_addendum_and_full_ledger(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    env.s0.profile.addendum = {'text': b'Admin-confirmed addition\r\n'}
    calibrate(env)
    events, bodies = [], []
    class ScriptedTransport(DoubleTransport):
        def __init__(self, role):
            super().__init__('kept')
            self.role, self.count = role, 0
        async def post(self, route, body, headers):
            self.count += 1
            events.append(self.role)
            bodies.append((self.role, deepcopy(body)))
            if self.role == 'verifier':
                self.content = json.dumps({'results': [{'constraint_id': 'c',
                    'verdict': 'fail' if self.count == 1 else 'pass',
                    'evidence_span': None if self.count == 1 else 'kept', 'note': ''}]})
            return await super().post(route, body, headers)
    transports = {'main': ScriptedTransport('writer'), 'check': ScriptedTransport('verifier')}
    probes = {name: ProbeResult('complete', dict.fromkeys(CAPABILITIES, True), {}) for name in transports}
    client = RoleClient(env.config, transports, probes, cached_registry(project_root / 'contracts/schemas'),
                        project_root=project_root)
    run = SimpleNamespace(client=client)
    writer, verifier = BoundModelClient(run, 'writer'), BoundModelClient(run, 'verifier')
    async def pipeline():
        draft = await write(env.s0, env.s2, None, writer, project_root=project_root)
        return await validate(env.s0, env.s2, None, draft, writer, verifier, project_root=project_root)
    result = asyncio.run(pipeline())
    assert result['status'] == 'repaired' and result['repair_count'] == 1 and result['output'] == 'kept'
    assert events == ['writer', 'verifier', 'writer', 'verifier'] and len(result['calls']) == 4
    for role, body in bodies:
        if role == 'writer':
            assert body['messages'][0]['content'].encode() == env.s0.profile.system_message
            assert body['messages'][1] == {'role': 'system', 'content': 'Admin-confirmed addition\r\n'}
    repair = json.loads(bodies[2][1]['messages'][2]['content'].split('\n', 1)[1])
    assert repair['repair']['draft'] == 'kept' and repair['repair']['failures'][0]['id'] == 'c'
