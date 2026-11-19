"""Labelled S2 doubles: a scripted planner-bound client (no HTTP, no model) and tiny synthetic replies."""

from copy import deepcopy
from types import SimpleNamespace

from intake_fixtures import configuration
from profile_fixtures import install_prompts
from prompt_enhancer.intake import IntakeResult
from prompt_enhancer.model_client import ModelCallError

MANIFEST = {'quote_semantics': 'test-only: double quotes mark text to render'}  # labelled test-only manifest view


class PlannerClientDouble:
    """Labelled double of a planner-bound client: returns scripted (value, record) pairs and records each call."""

    role_name = 'planner'

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def call(self, system_prompt, user_text, *, manifest=None, images=(), schema_id=None):
        self.calls.append({'system': system_prompt, 'text': user_text, 'images': list(images),
                           'schema_id': schema_id})  # T017-FIX: the per-request reply schema, None = the role's
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            return outcome
        return deepcopy(outcome), record('OK')


def record(status, **extra):
    return {'role': 'planner', 'status': status, 'request_hash': 'test-only-hash', 'image_roles': [],
            'warnings': [('PARAM_UNSUPPORTED', 'sampling.top_k')], **extra}


def failure(status):
    return None, record(status)


def call_error():
    return ModelCallError('BUDGET_EXHAUSTED: model request budget exhausted')


def reply(**changes):
    """A minimal valid planner_reply.v0 for a generate request."""
    value = {'edit_kind': None, 'entities': [], 'scene': {'location': None, 'medium': None, 'attributes': []},
             'links': [], 'knowledge_needs': [], 'constraints': [], 'literal_texts': [], 'passthrough_tokens': [],
             'edit_targets': [], 'delta': [], 'preserve': [], 'dependent_effects': [], 'unresolved': [],
             'needs_clarification': False, 'clarification_candidates': []}
    value.update(changes)
    return value


def entity(entity_id, mention, **changes):
    return {'id': entity_id, 'kind': 'person', 'count': 1, 'mention': mention, 'attributes': [], 'action': None,
            'relations': [], 'source': 'user', **changes}


def link(mention, status, *, position=None, candidates=(), role='subject_identity', entity_id=None,
         needs_lookup=False, variants=()):
    return {'mention': mention, 'entity_id': entity_id, 'status': status,
            'role': None if status == 'no_match' else role, 'candidate_position': position,
            'candidates': [{'position': p, 'context_fit': fit} for p, fit in candidates],
            'needs_lookup': needs_lookup, 'variants': list(variants)}


def shortlist_entry(index, name=None):
    return {'concept_id': f'concept:test-{index}', 'name': name or f'name {index}', 'kind': 'clothing',
            'description': f'description {index}', 'matched_source': 'name',
            'matched_span': {'start': 0, 'end': 1, 'text': 'x'}}


def planner_config(model_test_profile, tmp_path):
    config = configuration(model_test_profile, tmp_path)
    config['resolution']['random_seed'] = 7  # labelled test-only fixed seed
    return config


def intake_view(config, prompt, *, task='generate', source=None, snapshot=None):
    """An S0 result shaped like intake(); `snapshot` is a real pin or None."""
    payload = {'outcome': 'ready', 'request_id': 'test-request', 'prompt': prompt, 'task': task,
               'effective_config': config, 'config_hash': 'test-only-hash', 'profile_id': 'p1',
               'source_image': source, 'warnings': [], 'error': None, 'envelope': None,
               'retrieval': ({'state': 'disabled', 'snapshot_id': None} if snapshot is None
                             else {'state': 'pinned', 'snapshot_id': snapshot.snapshot_id})}
    knowledge = None if snapshot is None else SimpleNamespace(snapshot=snapshot, close=snapshot.release)
    return IntakeResult(payload, SimpleNamespace(manifest=MANIFEST), knowledge)


def prepared_root(project_root):
    install_prompts(project_root)
    return project_root
