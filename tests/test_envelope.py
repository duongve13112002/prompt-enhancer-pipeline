"""S6 payload and status invariants over generated stage outcomes."""
from copy import deepcopy
from types import SimpleNamespace

from hypothesis import given, strategies as st
import pytest

from prompt_enhancer.envelope import build_envelope


def intake_state(outcome='ready', rag=False):
    return SimpleNamespace(payload={'outcome': outcome, 'request_id': 'r1', 'profile_id': 'p1',
        'effective_config': {'rag': {'enabled': rag}, 'clarification': {'mode': 'ask'}},
        'source_image': None, 'warnings': [], 'error': None,
        'retrieval': {'state': 'pinned' if rag else 'disabled', 'snapshot_id': 's1' if rag else None}},
        profile=None)


@given(st.sampled_from(['ready', 'degraded', 'refused']),
       st.sampled_from(['ready', 'degraded', 'failed', 'needs_clarification']),
       st.sampled_from(['success', 'repaired', 'degraded', 'failed']), st.booleans(), st.booleans())
def test_envelope_status_precedence_property(upstream, planned, checked, retrieval, expired):
    s0 = intake_state(upstream, retrieval)
    s2 = {'state': planned, 'warnings': [], 'intent_ir': None}
    s5 = {'status': checked, 'output': None if checked == 'failed' else {'prompt': 'quoted 名'}, 'warnings': []}
    s3 = {'retrieval_state': 'no_match', 'pack': {'images': []}, 'error': None} if retrieval else None
    shortlist = {'state': 'ready', 'warnings': [{'code': 'DENSE_QUERY_TOO_LONG',
                                                 'span': {'start': 0, 'end': 4, 'text': 'long'}}]}
    if upstream == 'refused':
        s0.payload['error'] = {'code': 'INPUT_INVALID', 'message': 'intake refused (test)'}
    result = build_envelope(s0, s2, s3, s5, shortlist=shortlist if retrieval else None, fatal=expired)
    expected = 'failed' if expired or upstream == 'refused' else 'needs_clarification' if planned == 'needs_clarification' else (
        'failed' if checked == 'failed' else 'degraded' if upstream == 'degraded' or planned != 'ready' else checked)
    assert result['status'] == expected
    assert result['output'] == (None if expected in ('failed', 'needs_clarification') else s5['output'])
    assert result['retrieval']['state'] != 'pinned'
    assert set(result) == {'status', 'output', 'attachments', 'source_binding', 'retrieval', 'warnings', 'trace_id'}


def test_stage_diagnostics_stay_outside_payload():
    s0 = intake_state()
    payload = {'prompt': 'debug warnings citations are user evidence'}
    s5 = {'status': 'success', 'output': deepcopy(payload), 'warnings': ['PARAM_UNSUPPORTED', 'STAGE_DETAIL']}
    result = build_envelope(s0, {'state': 'ready', 'warnings': []}, None, s5)
    assert result['output'] == payload and result['warnings'] == ['PARAM_UNSUPPORTED']


def s1(state='ready', code=None):
    """A candidate_shortlist.v0-shaped S1 result (only the fields S6 reads)."""
    return {'state': state, 'warnings': [], 'error': None if code is None else {'code': code, 'message': 'S1: test'}}


def s3(state, code=None):
    return {'retrieval_state': state, 'pack': None if state == 'failed' else {'images': []},
            'error': None if code is None else {'code': code, 'message': 'S3: test'}}


def test_failed_retrieval_survives_empty_pack_and_repair():
    result = build_envelope(intake_state(rag=True), {'state': 'ready', 'warnings': []},
        {'retrieval_state': 'no_match', 'pack': {'images': []}},
        {'status': 'repaired', 'output': 'kept', 'warnings': []},
        shortlist=s1('failed', 'WINDOW_BUDGET_EXCEEDED'))
    assert result['status'] == 'degraded'
    # Decision 198: the S1 code itself, not only the planner's RETRIEVAL_FAILED.
    assert result['retrieval'] == {'state': 'failed', 'snapshot_id': 's1', 'error': 'WINDOW_BUDGET_EXCEEDED'}


@given(st.sampled_from(['ask', 'best_effort']), st.sampled_from(['success', 'repaired', 'degraded', 'failed']),
       st.sampled_from(['skipped_no_knowledge_needed', 'no_match', 'retrieved', 'failed']), st.booleans())
def test_ambiguity_retrieval_and_repair_precedence(mode, local_status, retrieval_state, access_failed):
    s0 = intake_state(rag=True)
    s0.payload['effective_config']['clarification']['mode'] = mode
    s2 = {'state': 'needs_clarification', 'warnings': []}
    s3_result = s3(retrieval_state, 'KNOWLEDGE_READ_FAILED' if retrieval_state == 'failed' else None)
    s5 = {'status': local_status, 'output': None if local_status == 'failed' else 'kept', 'warnings': []}
    result = build_envelope(s0, s2, s3_result, s5,
                            shortlist=s1('failed', 'SNAPSHOT_MISSING') if access_failed else s1())
    expected = 'needs_clarification' if mode == 'ask' else 'failed' if local_status == 'failed' else 'degraded'
    assert result['status'] == expected
    assert result['retrieval']['state'] == ('failed' if access_failed else retrieval_state)
    assert result['retrieval']['error'] == ('SNAPSHOT_MISSING' if access_failed else
                                            'KNOWLEDGE_READ_FAILED' if retrieval_state == 'failed' else None)
    assert result['output'] == (None if expected in ('failed', 'needs_clarification') else 'kept')


def test_attachment_requires_retained_bytes():
    s0 = intake_state(rag=True)
    s3 = {'retrieval_state': 'retrieved', 'pack': {'images': [{'image_id': 'i1', 'channel': 'attach',
          'bound_entity': 'e1', 'facets': ['identity.hair']}]}}
    result = build_envelope(s0, {'state': 'ready', 'warnings': []}, s3,
                            {'status': 'success', 'output': 'kept', 'warnings': []})
    assert result['status'] == 'degraded' and result['attachments'] == []


@given(st.integers(min_value=0, max_value=100))
def test_native_shortlist_warning_preserves_span(start):
    from prompt_enhancer.envelope import build_trace_row
    s0 = intake_state()
    s0.payload.update(prompt='original', task='generate', config_hash='test')
    s0.payload['effective_config']['resolution'] = {'random_seed': 1}
    warning = {'code': 'DENSE_QUERY_TOO_LONG', 'span': {'start': start, 'end': start + 1}}
    shortlist = {'state': 'ready', 'warnings': [warning, warning]}
    result = build_envelope(s0, shortlist=shortlist)
    row = build_trace_row(s0, result, shortlist=shortlist)
    assert row['warnings'] == ['DENSE_QUERY_TOO_LONG'] and result['warnings'] == []
    assert row['body']['shortlist']['warnings'] == [warning, warning]


def test_trace_separates_configured_models_from_actual_fallback():
    from prompt_enhancer.envelope import build_trace_row
    s0 = intake_state()
    s0.payload.update(prompt='original', task='generate', config_hash='test')
    config = s0.payload['effective_config']
    config['resolution'] = {'random_seed': 1}
    config['vlm'] = {'roles': {'writer': {'backend': 'selected'}, 'verifier': {'backend': 'default'}},
                     'backends': {'selected': {'model': {'id': 'chosen-writer'}},
                                  'default': {'model': {'id': 'actual-fallback'}}}}
    written = {'calls': [{'status': 'OK', 'record_json': '{"model_id":"actual-fallback"}'}]}
    row = build_trace_row(s0, build_envelope(s0), written=written)
    assert row['body']['configured_model_ids'] == {'writer': 'chosen-writer', 'verifier': 'actual-fallback'}
    assert row['calls'] == written['calls']


# T017-FIX3 (decisions 198, 200): property over generated pipeline outcomes. The pipeline's own order holds in every
# draw: a refused intake runs nothing; S1 runs first with retrieval on; S3 runs only after S1 returned.
S1_CODES = ('WINDOW_BUDGET_EXCEEDED', 'SNAPSHOT_MISSING', 'RETRIEVAL_FAILED')
STATES = {'disabled', 'skipped_no_knowledge_needed', 'retrieved', 'no_match', 'failed', 'not_reached'}


@st.composite
def outcomes(draw):
    rag = draw(st.booleans())
    outcome = draw(st.sampled_from(['ready', 'degraded', 'refused']))
    s0 = intake_state(outcome, rag)
    if outcome == 'degraded' and rag:
        s0.payload['warnings'] = ['SNAPSHOT_MISSING']
    shortlist = planned = assembled = None
    if outcome == 'refused':
        s0.payload['error'] = {'code': draw(st.sampled_from(['KNOWLEDGE_FAILED', 'PROFILE_NOT_ACTIVE', 'INTAKE_DEADLINE'])),
                               'message': 'intake refused (test)'}
    else:
        if rag:
            shortlist = draw(st.one_of(st.none(), st.just(s1()),
                                       st.sampled_from(S1_CODES).map(lambda code: s1('failed', code))))
        if not rag or shortlist is not None:
            planned = draw(st.one_of(st.none(), st.fixed_dictionaries({
                'state': st.sampled_from(['ready', 'degraded', 'failed', 'needs_clarification']),
                'warnings': st.lists(st.sampled_from(['RETRIEVAL_FAILED', 'SCHEMA_INVALID']), unique=True)})))
        if rag and shortlist is not None and planned is not None:
            assembled = draw(st.one_of(
                st.none(), st.sampled_from(['skipped_no_knowledge_needed', 'no_match', 'retrieved']).map(s3),
                st.sampled_from(['SNAPSHOT_MISSING', 'KNOWLEDGE_READ_FAILED']).map(lambda code: s3('failed', code))))
    return s0, shortlist, planned, assembled, draw(st.booleans())


def expected_retrieval(s0, shortlist, planned, assembled):
    """Independent oracle: the earliest failing stage wins; no failure and no S3 result is not_reached."""
    payload = s0.payload
    if payload['retrieval']['state'] == 'disabled':
        return 'disabled', None
    if 'SNAPSHOT_MISSING' in payload['warnings']:
        return 'failed', 'SNAPSHOT_MISSING'
    if payload['outcome'] == 'refused':
        knowledge = payload['error']['code'] == 'KNOWLEDGE_FAILED'
        return ('failed', 'KNOWLEDGE_FAILED') if knowledge else ('not_reached', None)
    if shortlist is None:
        return 'failed', 'RETRIEVAL_FAILED'
    if shortlist['state'] == 'failed':
        return 'failed', shortlist['error']['code']
    if planned and 'RETRIEVAL_FAILED' in planned['warnings']:
        return 'failed', 'RETRIEVAL_FAILED'
    if assembled is None:
        return 'not_reached', None
    return assembled['retrieval_state'], None if assembled['error'] is None else assembled['error']['code']


@given(outcomes())
def test_retrieval_state_is_one_enum_value_and_not_reached_only_without_s3(case):
    s0, shortlist, planned, assembled, fatal = case
    result = build_envelope(s0, planned, assembled, None, shortlist=shortlist, fatal=fatal)  # validated against v1
    retrieval = result['retrieval']
    assert retrieval['state'] in STATES
    assert (retrieval['state'], retrieval['error']) == expected_retrieval(s0, shortlist, planned, assembled)
    if retrieval['state'] == 'not_reached':
        assert assembled is None and s0.payload['effective_config']['rag']['enabled']
    assert (retrieval['error'] is None) == (retrieval['state'] != 'failed')


def test_failed_plan_after_ready_s1_is_not_reached_not_failed():
    """J4a4: a refused planner reply left assembly null and the envelope said failed although S1 was ready."""
    s0 = intake_state(rag=True)
    result = build_envelope(s0, {'state': 'degraded', 'warnings': ['SCHEMA_INVALID'], 'intent_ir': None}, None,
                            {'status': 'failed', 'output': None, 'warnings': []}, shortlist=s1())
    assert result['retrieval'] == {'state': 'not_reached', 'snapshot_id': 's1', 'error': None}
    assert result['status'] == 'failed'


def test_retrieval_error_is_a_closed_code_never_stage_text():
    """R-026: S1's message never reaches the envelope; only its closed code does."""
    s0 = intake_state(rag=True)
    shortlist = {'state': 'failed', 'warnings': [], 'error': {'code': 'WINDOW_BUDGET_EXCEEDED',
                                                              'message': 'MARKER-prompt-text 猫'}}
    result = build_envelope(s0, {'state': 'ready', 'warnings': []}, None, None, shortlist=shortlist)
    assert result['retrieval']['error'] == 'WINDOW_BUDGET_EXCEEDED' and 'MARKER' not in repr(result)
