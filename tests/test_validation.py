"""S5 bounded state transitions with labelled writer/verifier doubles."""
from copy import deepcopy
import json

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from prompt_enhancer.model_client import ModelCallError
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.validation import validate_verifier_reply
from validation_fixtures import calibrate, draft, environment, run


def test_structure_precedes_verifier(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c1', 'style', 'kept')])
    calibrate(env)
    env.s0.profile.validator_checks = ('exact',)
    env.s0.profile.validator_artifacts = ({'id': 'exact', 'operator': 'exact_text',
                                         'parameters': {'pointer': '', 'value': 'kept'}},)
    env.writer.replies = ['kept']
    result = run(env, 'bad structure')
    assert env.events == ['writer', 'verifier']
    assert result['status'] == 'repaired' and result['repair_count'] == 1
    data = json.loads(env.writer.calls[0][1].split('\n', 1)[1])
    assert data['repair']['draft'] == 'bad structure'
    assert any('"operator": "exact_text"' in item['note'] and '"value": "kept"' in item['note']
               for item in data['repair']['failures'])
    assert env.writer.calls[0][0] == env.s0.profile.system_message


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=0, max_value=4))
def test_repair_bound_property(model_test_profile, tmp_path, project_root, rounds):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c1', 'style', 'kept')])
    calibrate(env)
    env.config['validation']['max_repairs'] = rounds
    env.writer.replies = ['kept'] * rounds
    failure = {'results': [{'constraint_id': 'c1', 'verdict': 'fail', 'evidence_span': None, 'note': 'missing'}]}
    env.verifier.replies = [deepcopy(failure) for _ in range(rounds + 1)]
    result = run(env)
    assert len(env.writer.calls) == rounds and len(env.verifier.calls) == rounds + 1
    assert result['repair_count'] == rounds and result['status'] == 'degraded'
    assert result['output'] == 'original prompt'


def test_repair_reruns_whole_ledger(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root,
                      constraints=[('c1', 'style', 'first'), ('c2', 'relation', 'second')])
    calibrate(env)
    def replies(first_pass):
        return {'results': [{'constraint_id': name, 'verdict': 'pass' if passing else 'fail',
                             'evidence_span': 'kept' if passing else None, 'note': ''}
                            for name, passing in [('c1', first_pass), ('c2', not first_pass)]]}
    env.verifier.replies = [replies(True), replies(False)]
    env.writer.replies = ['kept']
    result = run(env)
    assert result['status'] == 'degraded'
    assert [item['id'] for item in json.loads(env.verifier.calls[1][1])['obligations']] == ['c1', 'c2']


@pytest.mark.parametrize('status', ['INCOMPLETE_OR_REFUSED', 'HTTP_ERROR', 'MODEL_CALL_FAILED'])
def test_failed_initial_draft_is_not_repaired_or_accepted(model_test_profile, tmp_path, project_root, status):
    env = environment(model_test_profile, tmp_path, project_root)
    result = run(env, result=draft(env, status=status))
    assert result['status'] == 'degraded' and env.events == [] and result['repair_count'] == 0


def test_schema_invalid_draft_can_consume_one_repair(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.writer.replies = ['kept']
    result = run(env, result=draft(env, status='SCHEMA_INVALID'))
    assert result['status'] == 'repaired' and result['repair_count'] == 1
    assert json.loads(env.writer.calls[0][1].split('\n', 1)[1])['repair']['draft'] is None


@pytest.mark.parametrize('mode', ['missing', 'prompt_drift', 'evidence_drift', 'wrong_route'])
def test_verifier_identity_and_calibration_fail_closed(model_test_profile, tmp_path, project_root, mode):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    target = calibrate(env)
    if mode == 'missing':
        env.config['validation']['verifier']['calibration_ref'] = 'fit_from_gt'
    elif mode == 'prompt_drift':
        (project_root / 'prompts/verifier_v0.txt').write_text('changed', encoding='utf-8')
    elif mode == 'evidence_drift':
        (project_root / 'synthetic-human-evidence.txt').write_text('changed', encoding='utf-8')
    else:
        value = json.loads(target.read_text('utf-8'))
        value['verifier_models'][0]['model']['revision'] = 'wrong'
        target.write_text(json.dumps(value), encoding='utf-8')
    result = run(env)
    assert result['status'] in ('degraded', 'failed') and env.events == []
    assert 'VERIFIER_UNAVAILABLE' in result['warnings']


@pytest.mark.parametrize('mode', ['calibrated', 'uncalibrated', 'repair', 'wrong_provenance', 'invalid_reply'])
def test_shared_model_and_optional_inline_calibration(model_test_profile, tmp_path, project_root, mode):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    env.config['vlm']['roles']['verifier']['backend'] = 'main'
    env.config['validation']['verifier']['require_calibration'] = mode == 'calibrated'
    if mode == 'calibrated':
        calibrate(env)
    elif mode == 'repair':
        env.verifier.replies = [{'results': [{'constraint_id': 'c', 'verdict': 'fail',
                                            'evidence_span': None, 'note': ''}]}]
        env.writer.replies = ['kept']
    elif mode == 'invalid_reply':
        env.verifier.replies = [{'results': []}]
    written = draft(env)
    if mode == 'wrong_provenance':
        record = json.loads(written['calls'][0]['record_json'])
        record['model_revision'] = 'unexpected'
        written['calls'][0]['record_json'] = json.dumps(record)
    result = run(env, result=written)
    expected = {'repair': 'repaired', 'wrong_provenance': 'degraded', 'invalid_reply': 'degraded'}
    assert result['status'] == expected.get(mode, 'success')
    assert ('VERIFIER_UNCALIBRATED' in result['warnings']) == (mode in ('uncalibrated', 'repair', 'invalid_reply'))
    if mode == 'wrong_provenance':
        assert env.events == []


def test_optional_inline_calibration_does_not_relax_monitoring(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['validation']['verifier'].update(require_calibration=False, sampled_fraction_for_rest=1)
    result = run(env)
    assert result['status'] == 'success' and not result['monitoring_selected']
    assert 'MONITORING_UNAVAILABLE' in result['warnings']
    calibrate(env)
    assert run(env)['monitoring_selected']
    assert env.events == []


@pytest.mark.parametrize('reply', [{}, {'results': []}, {'results': [
    {'constraint_id': 'foreign', 'verdict': 'pass', 'evidence_span': 'kept', 'note': ''}]}])
def test_invalid_verifier_reply_never_starts_repairs(model_test_profile, tmp_path, project_root, reply):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    env.verifier.replies = [reply]
    result = run(env)
    assert result['status'] == 'degraded' and len(env.verifier.calls) == 1 and env.writer.calls == []


def test_verifier_transport_failure_is_unverified_not_content_repair(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    env.verifier.replies = [ModelCallError('HTTP_ERROR: synthetic')]
    assert run(env)['status'] == 'degraded' and env.writer.calls == []


def test_fallback_order_and_no_invented_minimal(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    result = run(env, result=draft(env, status='HTTP_ERROR'))
    assert result['fallback'] == 'user_prompt_with_status_degraded'
    assert result['fallback_attempts'][0] == {'name': 'minimal_output_valid_for_contract', 'available': False}
    env.config['validation']['fallbacks'] = ['failed', 'user_prompt_with_status_degraded']
    assert run(env, result=draft(env, status='HTTP_ERROR'))['status'] == 'failed'


def test_minimal_template_preserves_user_prompt_without_extra_generation(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s0.payload['prompt'] = '{{original_prompt}} café\n'
    env.s0.profile.validator_checks = ('single',)
    env.s0.profile.validator_artifacts = ({'id': 'single', 'operator': 'json', 'parameters': {},
        'fallback': {'template': {'prompt': '{{original_prompt}}'}, 'input_variable': 'original_prompt'}},)
    env.s0.profile.validator_hashes = {'single': 'sha256:' + '0' * 64}  # labelled checked-view double
    result = run(env, result=draft(env, status='HTTP_ERROR'))
    assert result['status'] == 'degraded' and result['output'] == {'prompt': '{{original_prompt}} café\n'}
    assert result['fallback'] == 'minimal_output_valid_for_contract' and env.events == []


def test_original_prompt_fallback_respects_json_only_contract(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s0.profile.manifest['output_schema_id'] = 'verifier_result.v0'
    assert run(env, result=draft(env, status='HTTP_ERROR'))['status'] == 'failed'


@settings(max_examples=30, deadline=None)
@given(st.lists(st.sampled_from(['c1', 'c2', 'foreign']), max_size=4))
def test_verifier_reply_coverage_property(ids):
    reply = {'results': [{'constraint_id': name, 'verdict': 'pass', 'evidence_span': 'kept', 'note': ''}
                         for name in ids]}
    if len(ids) == 2 and set(ids) == {'c1', 'c2'}:
        assert len(validate_verifier_reply(reply, ['c1', 'c2'], 'kept')) == 2
    else:
        with pytest.raises(ValueError):
            validate_verifier_reply(reply, ['c1', 'c2'], 'kept')


@pytest.mark.parametrize('span', [None, '', ' ', 'fabricated', 'cafÉ'])
def test_evidence_span_must_match_decoded_output(span):
    reply = {'results': [{'constraint_id': 'c', 'verdict': 'pass', 'evidence_span': span, 'note': ''}]}
    with pytest.raises(ValueError):
        validate_verifier_reply(reply, ['c'], {'text': 'café kept'})


def test_no_risk_success_and_monitoring_selection(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    assert run(env)['status'] == 'success'
    env.config['validation']['verifier']['sampled_fraction_for_rest'] = 1
    calibrate(env)
    result = run(env)
    assert result['status'] == 'success' and result['monitoring_selected'] is True and env.events == []


def test_upstream_degradation_is_preserved(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s2['state'] = 'degraded'
    assert run(env)['status'] == 'degraded'


def test_declared_json_literals_are_compared_after_decoding(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    literal = '名 "café"\\\n'
    env.s2['intent_ir']['literal_texts'] = [{'id': 'l1', 'text': literal, 'span': literal}]
    env.s0.profile.validator_checks = ('json', 'literal')
    env.s0.profile.validator_artifacts = ({'id': 'json', 'operator': 'json', 'parameters': {}},
                                        {'id': 'literal', 'operator': 'exact_text',
                                         'parameters': {'pointer': '/text', 'value': literal}})
    result = run(env, json.dumps({'text': literal}, ensure_ascii=False))
    assert result['status'] == 'success' and env.events == []


def test_unmapped_format_constraint_needs_meaning(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('format', 'format', 'user format')])
    assert run(env)['status'] == 'degraded' and 'VERIFIER_UNAVAILABLE' in run(env)['warnings']


@pytest.mark.parametrize('output,operator,parameters', [
    ('{"text":"kept"}', 'keys', {'pointer': '', 'keys': ['text'], 'ordered': True}),
    ('12', 'number_range', {'pointer': '', 'minimum': 10, 'maximum': 15})])
def test_json_text_root_selectors_reach_s5(model_test_profile, tmp_path, project_root, output, operator, parameters):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s0.profile.validator_checks = ('json', 'root')
    env.s0.profile.validator_artifacts = ({'id': 'json', 'operator': 'json', 'parameters': {}},
                                         {'id': 'root', 'operator': operator, 'parameters': parameters})
    result = run(env, output)
    assert result['status'] == 'success' and result['output'] == output and env.events == []


def test_missing_ir_uses_fallback_without_fabricating_plan(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s2.update(state='failed', intent_ir=None, error={'code': 'PLANNER_CALL_FAILED', 'message': 'synthetic'})
    result = run(env, result=draft(env, status='MODEL_CALL_FAILED'))
    assert result['status'] == 'degraded' and result['output'] == 'original prompt' and env.events == []


def test_unbound_or_conflicting_fallback_refuses_before_calls(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root)
    artifact = {'id': 'fallback', 'operator': 'json', 'parameters': {},
                'fallback': {'template': {'prompt': '{{original_prompt}}'}, 'input_variable': 'original_prompt'}}
    env.s0.profile.validator_checks = ('fallback',)
    env.s0.profile.validator_artifacts = (artifact,)
    with pytest.raises(ProfileError, match='hash-bound'):
        run(env)
    other = deepcopy(artifact)
    other.update(id='different')
    other['fallback']['template'] = ['{{original_prompt}}']
    env.s0.profile.validator_checks = ('fallback', 'different')
    env.s0.profile.validator_artifacts = (artifact, other)
    with pytest.raises(ProfileError, match='conflicting'):
        run(env)
    assert env.events == []


def test_verifier_provenance_cannot_claim_a_different_model(model_test_profile, tmp_path, project_root):
    from validation_fixtures import record
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    class WrongModel:
        role_name = 'verifier'
        async def call(self, system, user):
            env.events.append('wrong-model')
            return {'results': [{'constraint_id': 'c', 'verdict': 'pass', 'evidence_span': 'kept', 'note': ''}]}, record(env.config, 'writer')
    env.verifier = WrongModel()
    assert run(env)['status'] == 'degraded' and env.events == ['wrong-model']


def test_cancellation_propagates_without_fallback_or_extra_calls(model_test_profile, tmp_path, project_root):
    import asyncio
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    calibrate(env)
    env.verifier.replies = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        run(env)
    assert env.events == ['verifier']


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.sampled_from(['OK', 'INCOMPLETE_OR_REFUSED', 'HTTP_ERROR', 'SCHEMA_INVALID']),
       st.integers(min_value=0, max_value=2))
def test_no_failed_output_accepted_and_status_is_single(model_test_profile, tmp_path, project_root, status, rounds):
    env = environment(model_test_profile, tmp_path, project_root)
    env.config['validation']['max_repairs'] = rounds
    env.writer.replies = [('discarded', 'INCOMPLETE_OR_REFUSED')] * rounds
    result = run(env, output='kept', result=draft(env, 'kept', status))
    assert result['status'] in ('success', 'repaired', 'degraded', 'failed')
    assert sum(key == 'status' for key in result) == 1
    if status != 'OK':
        assert result['status'] in ('degraded', 'failed') and result['output'] != 'discarded'
    assert result['repair_count'] <= rounds


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',)), min_size=1))
def test_verbatim_literals_property(model_test_profile, tmp_path, project_root, text):
    env = environment(model_test_profile, tmp_path, project_root)
    env.s2['intent_ir']['literal_texts'] = [{'id': 'l1', 'text': text, 'span': text}]
    env.s0.profile.validator_checks = ('json', 'text')
    env.s0.profile.validator_artifacts = ({'id': 'json', 'operator': 'json', 'parameters': {}},
                                        {'id': 'text', 'operator': 'exact_text',
                                         'parameters': {'pointer': '/literal', 'value': text}})
    output = json.dumps({'literal': text}, ensure_ascii=False)
    result = run(env, output)
    assert result['status'] == 'success' and result['output'] == output and env.events == []


def test_lazy_verifier_binding_occurs_only_after_eligibility(model_test_profile, tmp_path, project_root):
    import asyncio
    from prompt_enhancer.validation import validate
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c', 'style', 'kept')])
    async def bind(role):
        assert role == 'verifier'
        env.events.append('bind-verifier')
        return env.verifier
    env.writer.for_role = bind
    def call():
        return asyncio.run(validate(env.s0, env.s2, None, draft(env), env.writer, project_root=project_root))
    assert call()['status'] == 'degraded' and env.events == []
    calibrate(env)
    assert call()['status'] == 'success' and env.events == ['bind-verifier', 'verifier']


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',)), min_size=1))
def test_named_concepts_survive_verbatim_with_semantic_check(model_test_profile, tmp_path, project_root, text):
    from assembly_fixtures import link
    env = environment(model_test_profile, tmp_path, project_root)
    env.s2['intent_ir']['concept_links'] = [link(None, None, status='no_match', mention=text)]
    calibrate(env)
    output = {'concept': text, 'description': 'scene ' + text}
    def witness(data):
        return {'results': [{'constraint_id': item['id'], 'verdict': 'pass',
                             'evidence_span': 'scene ' + text, 'note': ''} for item in data['obligations']]}
    env.verifier.replies = [witness]
    result = run(env, output)
    assert result['status'] == 'success' and result['output'] == output and env.events == ['verifier']
