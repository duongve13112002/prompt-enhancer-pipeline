"""S6: one honest status and an untouched profile payload (R-024)."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .model_schemas import cached_registry

ROOT = Path(__file__).resolve().parents[2]
WARNINGS = frozenset(('PARAM_UNSUPPORTED', 'SCHEMA_INVALID', 'TRUNCATED', 'SNAPSHOT_MISSING',
                      'PROFILE_CONFLICT', 'BUDGET_EXHAUSTED', 'VERIFIER_UNCALIBRATED'))


def _warning_codes(*stages):
    for stage in stages:
        if stage:
            for warning in stage.get('warnings', ()):
                yield warning['code'] if isinstance(warning, dict) else warning


def build_envelope(intake_result, planned=None, assembled=None, checked=None, *, shortlist=None,
                   attachments_ready=False, fatal=False, project_root=None):
    payload = intake_result.payload
    config = payload['effective_config']
    warnings = dict.fromkeys(_warning_codes(payload, shortlist, planned, assembled, checked))
    retrieval = deepcopy(payload['retrieval'])
    if not config or not config['rag']['enabled']:
        retrieval = {'state': 'disabled' if retrieval['state'] == 'disabled' else 'failed', 'snapshot_id': None}
    else:
        retrieval['state'] = assembled['retrieval_state'] if assembled else 'failed'
        if shortlist and shortlist['state'] == 'failed' or planned and 'RETRIEVAL_FAILED' in planned.get('warnings', []):
            retrieval['state'] = 'failed'
    status, output = 'failed', None
    if not fatal and payload['outcome'] != 'refused':
        asks = config['clarification']['mode'] == 'ask' or config.get('resolution', {}).get('on_family_match') == 'ask'
        if planned and planned['state'] == 'needs_clarification' and asks:
            status = 'needs_clarification'
        elif checked and checked['output'] is not None and checked['status'] != 'failed':
            status, output = checked['status'], deepcopy(checked['output'])
            if payload['outcome'] != 'ready' or planned and planned['state'] != 'ready' or retrieval['state'] == 'failed':
                status = 'degraded'
    attachments = []
    pack = None if assembled is None else assembled['pack']
    images = [] if pack is None else [item for item in pack['images'] if item['channel'] == 'attach']
    if output is not None and images:
        if attachments_ready:
            attachments = [{'image_id': item['image_id'], 'role': 'knowledge_reference',
                            'bind_to': item['bound_entity'], 'use_for': list(item['facets'])} for item in images]
        else:
            status = 'degraded'
    source = payload['source_image']
    result = {'status': status, 'output': output, 'attachments': attachments,
              'source_binding': None if source is None else {'source_image_id': source['image_id']},
              'retrieval': retrieval, 'warnings': [item for item in warnings if item in WARNINGS],
              'trace_id': payload['request_id']}
    cached_registry(Path(project_root or ROOT) / 'contracts/schemas').validate('response_envelope.v0', result)
    return result


def build_trace_row(s0, envelope, *, shortlist=None, planned=None, assembled=None, written=None, checked=None,
                    latency_ms=None, diagnostics=(), created_at=None, model_config=None):
    """Store stage diagnostics outside output; never serialize credentials/effective config or transport headers."""
    p = s0.payload
    warnings = list(dict.fromkeys((*_warning_codes(p, shortlist, planned, assembled, written, checked), *diagnostics)))
    calls = [call for stage in (planned, checked or written) if stage for call in stage.get('calls', [])]
    profile = s0.profile
    identity = None if profile is None else {
        'manifest': deepcopy(profile.manifest), 'system_prompt_hash': getattr(profile, 'system_prompt_sha256', None),
        'addendum': None if profile.addendum is None else profile.addendum['id'],
        'validators': dict(profile.validator_hashes)}
    vlm = (model_config or p['effective_config'] or {}).get('vlm', {})
    configured_models = {}
    for role in ('writer', 'verifier'):
        settings = vlm.get('roles', {}).get(role)
        backend = vlm.get('backends', {}).get(settings['backend']) if isinstance(settings, dict) else None
        model = backend.get('model') if isinstance(backend, dict) else None
        configured_models[role] = model.get('id') if isinstance(model, dict) else None
    return {'request_id': p['request_id'], 'profile_id': p['profile_id'] or 'unresolved',
            'snapshot_id': envelope['retrieval']['snapshot_id'], 'effective_config_hash': p['config_hash'] or 'unresolved',
            'status': envelope['status'], 'retrieval_state': envelope['retrieval']['state'],
            'seeds': {} if not p['effective_config'] else {'resolution': p['effective_config']['resolution']['random_seed']},
            'warnings': warnings, 'calls': calls, 'latency_ms': latency_ms,
            'body': {'prompt': p['prompt'], 'task': p['task'], 'profile': identity, 'shortlist': shortlist,
                     'plan': planned, 'assembly': assembled, 'writer': written, 'validation': checked,
                     'configured_model_ids': configured_models,
                     'intake_error': p['error']}, 'created_at': created_at or datetime.now(timezone.utc)}
