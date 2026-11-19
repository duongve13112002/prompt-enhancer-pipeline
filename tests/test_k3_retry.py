"""Decision 183: a card reply that fails validation is retried with another seed (labelled HTTP double, synthetic data).

The fixture reproduces the shape of the T-017 J1-L2 call 12: three observations, positions 0 and 1 visible and one
`not_shown`; the scripted first reply omits the `not_shown` position from `view_groups`, as the 4B model did. No
knowledge-base text or image is copied; all values are synthetic.
"""

from copy import deepcopy
import json

import httpx
import pytest

from k3_fixtures import CardServer, checkpoint_rows, inputs, invoke_cards, reply_for
from prompt_enhancer.config import ConfigurationError, validate_card_reads
from prompt_enhancer.storage import StorageError

BASE_SEED = 4242
MARKER = 'MODEL_TEXT_MUST_NEVER_ENTER_THE_NEXT_REQUEST'


def configure(env, attempts, seed=BASE_SEED):
    env.config['cards']['reply_attempts'] = attempts
    role = env.config['vlm']['roles']['card_builder']
    role['sampling'] = {**role['sampling'], 'seed': seed}


def j1_inputs(env):
    """Three observations: two visible, the last `not_shown` (J1-L2 call 12 shape)."""
    data = inputs(env, values=('A', 'A', 'B'))
    data['observations'][2]['visibility'] = 'not_shown'
    return data


def omit_hidden_view(payload, marker=None):
    """A reply the stage must refuse: `view_groups` drop the `not_shown` position."""
    reply = reply_for(payload)
    hidden = {row['observation_position'] for row in payload['observations'] if row['visibility'] == 'not_shown'}
    reply['view_groups'] = [dict(group, observation_positions=[p for p in group['observation_positions']
                                                              if p not in hidden]) for group in reply['view_groups']]
    reply['view_groups'] = [group for group in reply['view_groups'] if group['observation_positions']]
    if marker:
        reply['summary'] = marker
    return reply


def scripted(*kinds, marker=None):
    """One reply per request: 'bad' (refused by parse_card_reply) or 'good'; repeats the last kind."""
    def reply(payload, body):
        kind = kinds[min(len(seen), len(kinds) - 1)]
        seen.append(body)
        return omit_hidden_view(payload, marker) if kind == 'bad' else reply_for(payload)
    seen = []
    return reply, seen


def only_seed_differs(requests):
    stripped = [{key: value for key, value in body.items() if key != 'seed'} for body in requests]
    return all(item == stripped[0] for item in stripped)


def test_card_stored_on_attempt_two_with_both_attempts_recorded(k2_env):
    configure(k2_env, 3)
    data = j1_inputs(k2_env)
    reply, _ = scripted('bad', 'good')
    server = CardServer(reply)
    result = invoke_cards(k2_env, data, server)
    assert len(result['rows']['cards']) == 1 and result['report']['failed'] == {}
    assert [body['seed'] for body in server.requests] == [BASE_SEED, BASE_SEED + 1]  # the seeds differ
    assert only_seed_differs(server.requests)  # prompt, schema, max_tokens, sampling: identical
    concept = result['rows']['cards'][0]['concept_id']
    assert result['report']['attempts'] == {'allowed': 3, 'used': {concept: 2}}
    (row,) = checkpoint_rows(k2_env, 'k3_grouping')
    assert len(row['call_records']) == 2
    with k2_env.store.writer() as writer:
        bundle = json.loads(writer.checkpoints('k3_grouping').load(row['input_key']))
    assert [(a['attempt'], a['seed'], a['code']) for a in bundle['attempts']] == [
        (0, BASE_SEED, 'SCHEMA_INVALID'), (1, BASE_SEED + 1, None)]
    assert all(len(a['reply_digest']) == 64 for a in bundle['attempts'])
    assert bundle['attempts'][0]['reply_digest'] != bundle['attempts'][1]['reply_digest']
    assert bundle['records'] == row['call_records'] and bundle['records'][-1] == bundle['record']
    # the refused attempt was written before the next one (crash between attempts keeps it)
    (failure,) = checkpoint_rows(k2_env, 'k3_call_failure')
    assert len(failure['call_records']) == 1


def test_card_stored_on_attempt_two_is_found_by_the_next_run_with_zero_model_calls(k2_env):
    configure(k2_env, 3)
    data = j1_inputs(k2_env)
    reply, _ = scripted('bad', 'good')
    invoke_cards(k2_env, data, CardServer(reply))
    again = CardServer(lambda payload, body: pytest.fail('a stored card must not be asked again'))
    result = invoke_cards(k2_env, data, again)
    assert again.requests == [] and result['report']['reused_grouping'] == 1 and len(result['rows']['cards']) == 1


def test_artifact_keys_ignore_the_attempt_seed(k2_env):
    """The key of a card stored on attempt 2 equals the key of a card stored on attempt 1 (profile settings only)."""
    configure(k2_env, 3)
    data = j1_inputs(k2_env)
    invoke_cards(k2_env, data, CardServer(scripted('good')[0]))
    (first,) = [row['input_key'] for row in checkpoint_rows(k2_env, 'k3_grouping')]
    second = CardServer(lambda payload, body: pytest.fail('same key expected'))
    invoke_cards(k2_env, data, second)  # attempt-1 card is reused by key: no request
    assert second.requests == [] and [row['input_key'] for row in checkpoint_rows(k2_env, 'k3_grouping')] == [first]


def test_every_attempt_bad_fails_the_concept_stores_nothing_and_keeps_every_record(k2_env):
    configure(k2_env, 3)
    data = j1_inputs(k2_env)
    reply, _ = scripted('bad')
    server = CardServer(reply)
    result = invoke_cards(k2_env, data, server)
    concept = data['handoff']['concepts'][0]['concept_id']
    assert result['report']['failed'] == {concept: 'SCHEMA_INVALID'}  # exactly as one refused reply failed before
    assert result['rows']['cards'] == [] and checkpoint_rows(k2_env, 'k3_card') == []
    assert checkpoint_rows(k2_env, 'k3_grouping')[0]['state'] == 'failed'
    assert len(server.requests) == 3  # never more than configured
    assert len({body['seed'] for body in server.requests}) == 3
    (failure,) = checkpoint_rows(k2_env, 'k3_call_failure')
    assert len(failure['call_records']) == 3
    with k2_env.store.writer() as writer:
        document = json.loads(writer.checkpoints('k3_call_failure').load(failure['input_key']))
    assert document['error'] == 'SCHEMA_INVALID'
    assert [a['seed'] for a in document['attempts']] == [BASE_SEED, BASE_SEED + 1, BASE_SEED + 2]
    assert result['report']['attempts']['used'] == {concept: 3}


def test_exhaustion_fails_the_sync_stage_so_no_snapshot_lacks_the_concept(k2_env):
    from k2_fixtures import folder
    from kb_sync_fixtures import SyncServer, invoke

    configure(k2_env, 3)
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    server.fail_role = 'card_builder'  # every card reply is refused by the schema, on every attempt
    with pytest.raises(StorageError, match='^k3: STAGE_FAILED: count=1; codes=SCHEMA_INVALID=1$'):
        invoke(k2_env, server)
    assert server.roles.count('card_builder') == 3  # all configured attempts, not one more
    assert k2_env.store.current() is None  # nothing published: no snapshot without the concept
    assert checkpoint_rows(k2_env, 'k3_card') == []


@pytest.mark.parametrize('failure', ['http_error', 'incomplete', 'capability'])
def test_failures_that_are_not_a_refused_reply_are_never_retried(k2_env, failure):
    configure(k2_env, 4)
    data = j1_inputs(k2_env)

    def respond(request):
        if failure == 'http_error':
            return httpx.Response(500, json={})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]})

    class Server(CardServer):
        def run(self, env):
            run = super().run(env)
            if failure == 'capability':  # the proof says json_schema is unavailable: refused before any request
                run.probes['main'].capabilities['json_schema'] = False
            return run
    server = Server()
    server.handler = lambda request: (server.requests.append(json.loads(request.content)), respond(request))[1]
    result = invoke_cards(k2_env, data, server)
    codes = {'http_error': 'HTTP_ERROR', 'incomplete': 'INCOMPLETE_OR_REFUSED', 'capability': 'CAPABILITY_UNAVAILABLE'}
    assert list(result['report']['failed'].values()) == [codes[failure]]
    assert len(server.requests) == (0 if failure == 'capability' else 1)
    assert result['rows']['cards'] == []


def test_a_failure_between_attempts_keeps_the_earlier_attempt_record(k2_env):
    """Attempt 0 is refused, attempt 1 hits a transport error: the refused attempt is already on disk."""
    configure(k2_env, 4)
    data = j1_inputs(k2_env)
    server = CardServer(scripted('bad')[0])
    original = server.handler

    def handler(request):
        if not server.requests:
            return original(request)
        server.requests.append(json.loads(request.content))
        return httpx.Response(500, json={})
    server.handler = handler
    result = invoke_cards(k2_env, data, server)
    assert list(result['report']['failed'].values()) == ['HTTP_ERROR'] and len(server.requests) == 2
    (failure,) = checkpoint_rows(k2_env, 'k3_call_failure')
    with k2_env.store.writer() as writer:
        document = json.loads(writer.checkpoints('k3_call_failure').load(failure['input_key']))
    assert (document['attempts'][0]['attempt'], document['attempts'][0]['code']) == (0, 'SCHEMA_INVALID')
    assert len(failure['call_records']) >= 1 and checkpoint_rows(k2_env, 'k3_card') == []


def test_a_retry_carries_neither_validator_text_nor_the_previous_reply(k2_env):
    configure(k2_env, 3)
    data = j1_inputs(k2_env)
    reply, _ = scripted('bad', 'bad', 'good', marker=MARKER)
    server = CardServer(reply)
    result = invoke_cards(k2_env, data, server)
    assert len(server.requests) == 3 and len(result['rows']['cards']) == 1
    assert only_seed_differs(server.requests)
    # attempts 1 and 2 equal attempt 0 apart from the seed (above), so no validator text can have been added; the
    # marker of the refused replies is the proof that no earlier reply is echoed either
    assert all(MARKER not in json.dumps(body) for body in server.requests)
    rows = checkpoint_rows(k2_env, 'k3_call_failure')
    with k2_env.store.writer() as writer:  # the evidence holds digests, never the text of a refused reply (R-026)
        for row in rows:
            assert MARKER not in writer.checkpoints('k3_call_failure').load(row['input_key']).decode()


def test_one_attempt_is_the_unchanged_single_request(k2_env):
    configure(k2_env, 1, seed='none')
    data = j1_inputs(k2_env)
    server = CardServer(scripted('bad')[0])
    result = invoke_cards(k2_env, data, server)
    assert len(server.requests) == 1 and 'seed' not in server.requests[0]
    assert list(result['report']['failed'].values()) == ['SCHEMA_INVALID']


def test_retries_do_not_serialise_the_stage(k2_env):
    """Structural check only: ONE concept (this fixture builds one) whose first reply is refused makes 2 requests, and
    the stage source still schedules through the single `flow(` call with no `asyncio.gather`. It does not run two
    concepts concurrently, so it proves no overlap; the source assertion is what guards against a stage barrier."""
    configure(k2_env, 2)
    data = j1_inputs(k2_env)
    assert len({row['concept_id'] for row in data['observations']}) == 1
    reply, seen = scripted('bad', 'good')
    server = CardServer(reply)
    invoke_cards(k2_env, data, server)
    assert len(seen) == 2  # the retry loop runs inside the item's own worker: attempt 0, then attempt 1
    import inspect
    from prompt_enhancer import k3_cards
    source = inspect.getsource(k3_cards.build_cards)
    assert source.count('await flow(') == 1 and 'asyncio.gather' not in source


@pytest.mark.parametrize('attempts', [2, 5])
def test_attempts_above_one_need_an_integer_seed(k2_env, attempts):
    configure(k2_env, attempts, seed='none')
    with pytest.raises(ConfigurationError, match=r'cards\.reply_attempts.*card_builder\.sampling\.seed'):
        validate_card_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize('value', ['fit_from_bench', 'fit_from_pilot', 'design_required'])
def test_a_sentinel_attempts_key_is_refused_by_the_card_read_set(k2_env, value):
    k2_env.config['cards']['reply_attempts'] = value
    with pytest.raises(ConfigurationError, match=f'cards.reply_attempts: unresolved sentinel {value}'):
        validate_card_reads(k2_env.config, project_root=k2_env.root)


def test_a_missing_attempts_key_is_refused(k2_env):
    del k2_env.config['cards']['reply_attempts']
    with pytest.raises(ConfigurationError, match='reply_attempts'):
        validate_card_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '3'])
def test_an_invalid_attempts_key_is_refused(k2_env, value):
    k2_env.config['cards']['reply_attempts'] = value
    with pytest.raises(ConfigurationError, match='reply_attempts'):
        validate_card_reads(k2_env.config, project_root=k2_env.root)


def test_a_provider_that_cannot_take_a_seed_is_refused_for_retries(k2_env):
    configure(k2_env, 2)
    from test_model_production_probes import synthetic_provider_backend  # a whole, schema-valid anthropic backend
    k2_env.config['vlm']['backends']['main'] = synthetic_provider_backend(k2_env, 'anthropic')
    with pytest.raises(ConfigurationError, match="cards.reply_attempts.*'anthropic' does not map sampling.seed"):
        validate_card_reads(k2_env.config, project_root=k2_env.root)


def test_a_larger_attempt_count_is_a_valid_override(k2_env):
    configure(k2_env, 7)
    validate_card_reads(k2_env.config, project_root=k2_env.root)
    assert deepcopy(k2_env.config)['cards']['reply_attempts'] == 7
