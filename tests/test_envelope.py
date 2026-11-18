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


def test_failed_retrieval_survives_empty_pack_and_repair():
    result = build_envelope(intake_state(rag=True), {'state': 'ready', 'warnings': []},
        {'retrieval_state': 'no_match', 'pack': {'images': []}},
        {'status': 'repaired', 'output': 'kept', 'warnings': []},
        shortlist={'state': 'failed', 'warnings': []})
    assert result['status'] == 'degraded' and result['retrieval']['state'] == 'failed'


@given(st.sampled_from(['ask', 'best_effort']), st.sampled_from(['success', 'repaired', 'degraded', 'failed']),
       st.sampled_from(['skipped_no_knowledge_needed', 'no_match', 'retrieved', 'failed']), st.booleans())
def test_ambiguity_retrieval_and_repair_precedence(mode, local_status, retrieval_state, access_failed):
    s0 = intake_state(rag=True)
    s0.payload['effective_config']['clarification']['mode'] = mode
    s2 = {'state': 'needs_clarification', 'warnings': []}
    s3 = {'retrieval_state': retrieval_state, 'pack': {'images': []}}
    s5 = {'status': local_status, 'output': None if local_status == 'failed' else 'kept', 'warnings': []}
    result = build_envelope(s0, s2, s3, s5, shortlist={'state': 'failed' if access_failed else 'ready'})
    expected = 'needs_clarification' if mode == 'ask' else 'failed' if local_status == 'failed' else 'degraded'
    assert result['status'] == expected
    assert result['retrieval']['state'] == ('failed' if access_failed else retrieval_state)
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
