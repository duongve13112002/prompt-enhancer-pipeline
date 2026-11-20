"""S6: one honest status and an untouched profile payload (R-024)."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .model_schemas import cached_registry

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'response_envelope.v1'  # artifact-bound stage contract (T017-FIX3)
WARNINGS = frozenset(('PARAM_UNSUPPORTED', 'SCHEMA_INVALID', 'TRUNCATED', 'SNAPSHOT_MISSING',
                      'PROFILE_CONFLICT', 'BUDGET_EXHAUSTED', 'VERIFIER_UNCALIBRATED'))
# Protocol-bound: the intake refusal code of a failure at the knowledge boundary (intake.py StorageError branch and
# refuse_attachments); any other refusal stops the request before retrieval starts.
INTAKE_KNOWLEDGE_CODE = 'KNOWLEDGE_FAILED'
SNAPSHOT_MISSING = 'SNAPSHOT_MISSING'  # protocol-bound: intake's degraded warning, also an S1 code
# Protocol-bound: the planner warning for a lost grounding read (planner.py), which carries no stage code of its own.
PLANNER_RETRIEVAL_CODE = 'RETRIEVAL_FAILED'


def intake_retrieval(payload):
    """The envelope retrieval of a refused intake (decision 200): `disabled` when retrieval is off, `failed` with
    `KNOWLEDGE_FAILED` for a refusal at the knowledge boundary, otherwise `not_reached`. Only closed codes (R-026)."""
    if payload['retrieval']['state'] == 'disabled':
        return {'state': 'disabled', 'snapshot_id': None, 'error': None}
    if SNAPSHOT_MISSING in payload['warnings']:  # intake already found no published snapshot to pin
        return {'state': 'failed', 'snapshot_id': None, 'error': SNAPSHOT_MISSING}
    error = payload.get('error')
    if isinstance(error, dict) and error.get('code') == INTAKE_KNOWLEDGE_CODE:
        return {'state': 'failed', 'snapshot_id': None, 'error': INTAKE_KNOWLEDGE_CODE}
    return {'state': 'not_reached', 'snapshot_id': None, 'error': None}


def _retrieval(payload, shortlist, planned, assembled):
    """One honest retrieval state with its stage code (decisions 198 and 200).

    `failed` names the earliest failing stage: S1's own code, `RETRIEVAL_FAILED` when S1 started but returned nothing
    (the pipeline runs S1 first after an accepted intake with retrieval on) or the planner lost its grounding read, else
    S3's code. Without a failure and without an S3 result the state is `not_reached`; `disabled` is retrieval off."""
    config = payload['effective_config']
    if payload['outcome'] == 'refused' or not config:
        return intake_retrieval(payload)
    if not config['rag']['enabled']:
        return {'state': 'disabled', 'snapshot_id': None, 'error': None}
    code = None
    if SNAPSHOT_MISSING in payload['warnings']:  # the degraded intake: S1 can only fail on it (and says so)
        code = SNAPSHOT_MISSING
    elif shortlist is None:
        code = PLANNER_RETRIEVAL_CODE
    elif shortlist['state'] != 'ready':
        code = shortlist['error']['code']
    elif PLANNER_RETRIEVAL_CODE in _warning_codes(planned):
        code = PLANNER_RETRIEVAL_CODE
    elif assembled and assembled['retrieval_state'] == 'failed':
        code = assembled['error']['code']
    snapshot_id = payload['retrieval']['snapshot_id']
    if code is not None:
        return {'state': 'failed', 'snapshot_id': snapshot_id, 'error': code}
    state = assembled['retrieval_state'] if assembled else 'not_reached'
    return {'state': state, 'snapshot_id': snapshot_id, 'error': None}


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
    retrieval = _retrieval(payload, shortlist, planned, assembled)
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
    if 'source_images' in payload:
        # intake_result.v2 (several sources): response_envelope.v1 holds one binding, so only a refusal can be built here;
        # the multi-source success envelope is MS-5's (response_envelope.v2). Never bind it to image 1.
        if payload['outcome'] != 'refused':
            raise NotImplementedError('several source images need response_envelope.v2 (MS-5)')
        source = None
    else:
        source = payload['source_image']
    result = {'status': status, 'output': output, 'attachments': attachments,
              'source_binding': None if source is None else {'source_image_id': source['image_id']},
              'retrieval': retrieval, 'warnings': [item for item in warnings if item in WARNINGS],
              'trace_id': payload['request_id']}
    cached_registry(Path(project_root or ROOT) / 'contracts/schemas').validate(SCHEMA, result)
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
