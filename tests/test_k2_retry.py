"""Decision 188 (a): an observer reply that fails validation is retried with another seed (labelled HTTP double).

The scripted first reply repeats one facet of one concept (the J1b-L1 call 10 shape, `duplicate facet for one
concept`); the second is valid. Synthetic folder and image; no knowledge-base text or image is copied.
"""

import json

import httpx
import pytest

from k2_fixtures import folder
from k2_observe_fixtures import ObserverServer, invoke_observer, observation, states
from prompt_enhancer.config import ConfigurationError, validate_observation_reads
from prompt_enhancer.storage import StorageError

BASE_SEED = 777
MARKER = 'MODEL_TEXT_MUST_NEVER_ENTER_THE_NEXT_REQUEST'


def configure(env, attempts, seed=BASE_SEED):
    env.config['observe']['reply_attempts'] = attempts
    role = env.config['vlm']['roles']['observer']
    role['sampling'] = {**role['sampling'], 'seed': seed}


def duplicate_facet():
    reply = observation((0,), text=MARKER)
    reply['observations'].append(dict(reply['observations'][0]))  # same concept, same facet twice: refused
    return reply


def scripted(*kinds):
    seen = []

    def reply(data, body):
        kind = kinds[min(len(seen), len(kinds) - 1)]
        seen.append(body)
        return duplicate_facet() if kind == 'bad' else observation(range(len(data['concept_names'])))
    return reply, seen


def only_seed_differs(requests):
    stripped = [{key: value for key, value in body.items() if key != 'seed'} for body in requests]
    return all(item == stripped[0] for item in stripped)


def test_observation_stored_on_attempt_two_with_both_attempts_recorded(k2_env):
    configure(k2_env, 3)
    folder(k2_env, 'f', 'Name\ndescription')
    server = ObserverServer(scripted('bad', 'good')[0])
    result = invoke_observer(k2_env, server)
    assert result['report']['observed'] == 1 and result['report']['failed'] == {}
    assert [body['seed'] for body in server.requests] == [BASE_SEED, BASE_SEED + 1] and only_seed_differs(server.requests)
    (row,) = states(k2_env)
    assert row['state'] == 'done' and len(row['call_records']) == 2
    assert result['report']['attempts'] == {'allowed': 3, 'used': {row['input_key']: 2}}
    with k2_env.store.writer() as writer:
        bundle = json.loads(writer.checkpoints('k2_observation').load(row['input_key']))
    assert [(a['attempt'], a['seed'], a['code']) for a in bundle['attempts']] == [
        (0, BASE_SEED, 'SCHEMA_INVALID'), (1, BASE_SEED + 1, None)]
    assert bundle['attempts'][0]['reply_digest'] != bundle['attempts'][1]['reply_digest']
    # the refused attempt was written to its own checkpoint before the retry
    with k2_env.store.writer() as writer:
        (failure,) = [r for r in writer._state_rows().values() if r['stage'] == 'k2_call_failure']
    assert len(failure['call_records']) == 1


def test_observation_stored_on_attempt_two_is_found_by_the_next_run_with_zero_model_calls(k2_env):
    configure(k2_env, 3)
    folder(k2_env, 'f', 'Name\ndescription')
    invoke_observer(k2_env, ObserverServer(scripted('bad', 'good')[0]))
    again = ObserverServer(lambda data, body: pytest.fail('a stored observation must not be asked again'))
    result = invoke_observer(k2_env, again)
    assert again.requests == [] and result['report']['skipped_done'] == 1


def test_every_attempt_bad_fails_the_image_stores_nothing_and_keeps_every_record(k2_env):
    configure(k2_env, 3)
    folder(k2_env, 'f', 'Name\ndescription')
    server = ObserverServer(scripted('bad')[0])
    result = invoke_observer(k2_env, server)
    (code,) = result['report']['failed'].values()
    assert code == 'SCHEMA_INVALID' and result['rows']['observations'] == []
    assert len(server.requests) == 3 and len({body['seed'] for body in server.requests}) == 3
    (row,) = states(k2_env)
    assert row['state'] == 'failed'
    with k2_env.store.writer() as writer:
        (failure,) = [r for r in writer._state_rows().values() if r['stage'] == 'k2_call_failure']
        document = json.loads(writer.checkpoints('k2_call_failure').load(failure['input_key']))
    assert len(failure['call_records']) == 3
    assert [a['seed'] for a in document['attempts']] == [BASE_SEED, BASE_SEED + 1, BASE_SEED + 2]
    assert MARKER not in json.dumps(document)  # digests only, never reply text (R-026)


def test_attempts_are_per_image_not_shared(k2_env):
    configure(k2_env, 2)
    folder(k2_env, 'a', 'A\ndescription', count=2)
    folder(k2_env, 'b', 'B\ndescription')
    k2_env.config['observe']['representatives_per_folder'] = 2
    server = ObserverServer(scripted('bad')[0])
    result = invoke_observer(k2_env, server)
    assert len(result['report']['failed']) == 3 and len(server.requests) == 6  # 3 images x 2 attempts, not 2 in all


@pytest.mark.parametrize('failure', ['http_error', 'incomplete'])
def test_failures_that_are_not_a_refused_reply_are_never_retried(k2_env, failure):
    configure(k2_env, 4)
    folder(k2_env, 'f', 'Name\ndescription')
    server = ObserverServer()

    def handler(request):
        server.requests.append(json.loads(request.content))
        if failure == 'http_error':
            return httpx.Response(500, json={})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]})
    server.handler = handler
    result = invoke_observer(k2_env, server)
    assert list(result['report']['failed'].values()) == [{'http_error': 'HTTP_ERROR',
                                                          'incomplete': 'INCOMPLETE_OR_REFUSED'}[failure]]
    assert len(server.requests) == 1


def test_a_failure_between_attempts_keeps_the_earlier_attempt_record(k2_env):
    """Attempt 0 is refused, attempt 1 hits a transport error: the refused attempt is already on disk."""
    configure(k2_env, 4)
    folder(k2_env, 'f', 'Name\ndescription')
    server = ObserverServer(scripted('bad')[0])
    original = server.handler

    def handler(request):
        if not server.requests:
            return original(request)
        server.requests.append(json.loads(request.content))
        return httpx.Response(500, json={})
    server.handler = handler
    result = invoke_observer(k2_env, server)
    assert list(result['report']['failed'].values()) == ['HTTP_ERROR'] and len(server.requests) == 2
    with k2_env.store.writer() as writer:
        (failure,) = [r for r in writer._state_rows().values() if r['stage'] == 'k2_call_failure']
        document = json.loads(writer.checkpoints('k2_call_failure').load(failure['input_key']))
    assert (document['attempts'][0]['attempt'], document['attempts'][0]['code']) == (0, 'SCHEMA_INVALID')
    assert result['rows']['observations'] == []


def test_a_retry_carries_neither_validator_text_nor_the_previous_reply(k2_env):
    configure(k2_env, 3)
    folder(k2_env, 'f', 'Name\ndescription')
    server = ObserverServer(scripted('bad', 'bad', 'good')[0])
    invoke_observer(k2_env, server)
    assert len(server.requests) == 3 and only_seed_differs(server.requests)
    for body in server.requests:
        assert MARKER not in json.dumps(body) and 'duplicate' not in json.dumps(body)


def test_one_attempt_is_the_unchanged_single_request(k2_env):
    configure(k2_env, 1, seed='none')
    folder(k2_env, 'f', 'Name\ndescription')
    server = ObserverServer(scripted('bad')[0])
    result = invoke_observer(k2_env, server)
    assert len(server.requests) == 1 and 'seed' not in server.requests[0]
    assert list(result['report']['failed'].values()) == ['SCHEMA_INVALID']


@pytest.mark.parametrize('value', ['fit_from_bench', 'fit_from_pilot'])
def test_a_sentinel_attempts_key_is_refused_by_the_observation_read_set(k2_env, value):
    k2_env.config['observe']['reply_attempts'] = value
    with pytest.raises(ConfigurationError, match=f'observe.reply_attempts: unresolved sentinel {value}'):
        validate_observation_reads(k2_env.config, project_root=k2_env.root)


def test_a_missing_or_invalid_attempts_key_is_refused(k2_env):
    for value in (0, -2, True, 2.5, '2'):
        k2_env.config['observe']['reply_attempts'] = value
        with pytest.raises(ConfigurationError, match='reply_attempts'):
            validate_observation_reads(k2_env.config, project_root=k2_env.root)
    del k2_env.config['observe']['reply_attempts']
    with pytest.raises(ConfigurationError, match='reply_attempts'):
        validate_observation_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize('seed', ['none', None, -1, 2 ** 63])  # a boolean is refused earlier, by the role schema
def test_attempts_above_one_need_an_integer_seed_in_range(k2_env, seed):
    configure(k2_env, 2, seed=seed)
    with pytest.raises(ConfigurationError, match=r'observe\.reply_attempts.*observer\.sampling\.seed'):
        validate_observation_reads(k2_env.config, project_root=k2_env.root)


def test_a_provider_that_cannot_take_a_seed_is_refused_for_retries(k2_env):
    configure(k2_env, 2)
    from test_model_production_probes import synthetic_provider_backend  # a whole, schema-valid anthropic backend
    k2_env.config['vlm']['backends']['main'] = synthetic_provider_backend(k2_env, 'anthropic')
    with pytest.raises(ConfigurationError, match="observe.reply_attempts.*'anthropic' does not map sampling.seed"):
        validate_observation_reads(k2_env.config, project_root=k2_env.root)


def test_a_larger_attempt_count_is_a_valid_override(k2_env):
    configure(k2_env, 9)
    validate_observation_reads(k2_env.config, project_root=k2_env.root)
