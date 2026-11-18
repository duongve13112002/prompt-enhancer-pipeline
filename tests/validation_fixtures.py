"""Synthetic, explicitly calibrated S5 fixtures; no quality/live evidence."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from assembly_fixtures import ir, plan
from intake_fixtures import configuration
from profile_fixtures import install_prompts
from prompt_enhancer.profile_prompt import sha256_tag
from test_writer import view


def record(config, role, status='OK'):
    backend = config['vlm']['backends'][config['vlm']['roles'][role]['backend']]
    return {'status': status, 'provider': backend['provider'], 'model_id': backend['model']['id'],
            'model_revision': backend['model']['revision'], 'backend': config['vlm']['roles'][role]['backend'],
            'warnings': []}


class ModelDouble:
    """Labelled scripted role double, preserving call order and verifier request data."""
    def __init__(self, config, role, replies=(), events=None):
        self.config, self.role_name = config, role
        self.replies, self.calls, self.events = list(replies), [], events if events is not None else []

    async def call(self, system, user, **kwargs):
        self.events.append(self.role_name)
        self.calls.append((system, user, kwargs))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(json.loads(user))
        if reply is None and self.role_name == 'verifier':
            data = json.loads(user)
            reply = {'results': [{'constraint_id': item['id'], 'verdict': 'pass',
                                 'evidence_span': data['candidate'] if isinstance(data['candidate'], str) else 'kept',
                                 'note': ''} for item in data['obligations']]}
        if isinstance(reply, tuple):
            return reply[0], record(self.config, self.role_name, reply[1])
        return reply, record(self.config, self.role_name)


def environment(model_test_profile, tmp_path, project_root, *, task='generate', constraints=()):
    install_prompts(project_root)
    config = configuration(model_test_profile, tmp_path)
    config['schema_version'] = 2
    config['resolution']['random_seed'] = 7  # explicit test-only monitoring seed
    verifier = deepcopy(config['vlm']['backends']['main'])
    verifier['model']['id'] = 'synthetic-independent-verifier'
    config['vlm']['backends']['check'] = verifier
    config['vlm']['roles']['verifier']['backend'] = 'check'
    config['validation']['verifier']['calibration_ref'] = 'fit_from_gt'
    s0 = view(config, 'original prompt', task=task)
    s0.profile.has_known_conflict = False
    intent = ir(task=task)
    intent['constraint_ledger']['constraints'] = [dict(id=name, type=kind, text=text, span=text, bound_entity=None)
                                                   for name, kind, text in constraints]
    s2 = plan(intent)
    events = []
    return SimpleNamespace(config=config, s0=s0, s2=s2, root=project_root,
                           writer=ModelDouble(config, 'writer', events=events),
                           verifier=ModelDouble(config, 'verifier', events=events), events=events)


def draft(env, output='kept', status='OK'):
    okay = status == 'OK'
    return {'state': 'draft' if okay else 'failed', 'output': output if okay else None,
            'calls': [{'status': status, 'record_json': json.dumps(record(env.config, 'writer', status))}],
            'warnings': [], 'error': None if okay else {'code': status, 'message': 'test-only failure'},
            'brief_hash': sha256_tag(b'fixture'), 'system_prompt_hash': sha256_tag(env.s0.profile.system_message),
            'addendum_hash': None}


def calibrate(env):
    """Test-only evidence, built independently from explicitly stated fixture identities."""
    from prompt_enhancer.model_client import semantic_role_settings
    def tagged(value):
        return sha256_tag(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
    def identities(role):
        settings = env.config['vlm']['roles'][role]
        backend = env.config['vlm']['backends'][settings['backend']]
        routes = [{'provider': backend['provider'], 'model': deepcopy(backend['model'])}]
        if backend['provider'] != 'openai_compatible' and (
            backend['on_budget_exhausted'] == 'fallback_to_default_backend'
            or isinstance(settings['image'], dict) and not backend['allow_images']
        ):
            fallback = env.config['vlm']['backends'][env.config['vlm']['default_backend']]
            route = {'provider': fallback['provider'], 'model': deepcopy(fallback['model'])}
            if route not in routes:
                routes.append(route)
        return routes
    report = Path(env.root) / 'synthetic-human-evidence.txt'
    report.write_bytes(b'LABELLED TEST DOUBLE: no human/model calibration claim')
    writer_config = deepcopy(env.config)
    schema_id = env.s0.profile.manifest['output_schema_id']
    writer_config['vlm']['roles']['writer']['output'].update(
        format='none' if schema_id == 'none' else 'json_schema', schema_id=schema_id)
    value = {'human_checked': True, 'writer_models': identities('writer'), 'verifier_models': identities('verifier'),
             'writer_settings_hash': tagged(semantic_role_settings(writer_config, 'writer')),
             'verifier_settings_hash': tagged(semantic_role_settings(env.config, 'verifier')),
             'writer_prompt_hash': sha256_tag((Path(env.root) / 'prompts' /
                                             (env.config['vlm']['roles']['writer']['prompt_id'] + '.txt')).read_bytes()),
             'verifier_prompt_hash': sha256_tag((Path(env.root) / 'prompts' /
                                               (env.config['vlm']['roles']['verifier']['prompt_id'] + '.txt')).read_bytes()),
             'system_prompt_hash': sha256_tag(env.s0.profile.system_message),
             'addendum_hash': None if env.s0.profile.addendum is None else sha256_tag(env.s0.profile.addendum['text']),
             'ledger_schema_hash': sha256_tag((Path(env.root) / 'contracts/schemas/constraint_ledger.v0.schema.json').read_bytes()),
             'evidence_ref': report.name, 'evidence_hash': sha256_tag(report.read_bytes())}
    target = Path(env.root) / 'calibration.json'
    target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    env.config['validation']['verifier']['calibration_ref'] = target.name
    return target


def run(env, output='kept', *, result=None, assembly=None):
    from prompt_enhancer.validation import validate
    return asyncio.run(validate(env.s0, env.s2, assembly, result or draft(env, output),
                                env.writer, env.verifier, project_root=env.root))
