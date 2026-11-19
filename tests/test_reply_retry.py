"""Decisions 183 and 188: the retry loop, over scripted outcomes (labelled doubles; no model, no network)."""

import asyncio

from hypothesis import given, settings, strategies as st
import pytest

from prompt_enhancer.model_client import ModelCallError
from prompt_enhancer.planner import SEED_BITS
from prompt_enhancer.reply_retry import Attempts, attempt_seed, call_until_valid, reply_digest

OUTCOMES = ('pass', 'refuse', 'client_schema_invalid', 'http_error', 'incomplete', 'model_call_error', 'cancel')
RETRYABLE = {'refuse', 'client_schema_invalid'}


def run_script(outcomes, attempts, base=10):
    """Drive call_until_valid with one scripted outcome per request; return (result or error, seeds, log, persisted)."""
    seeds, persisted = [], []
    log = Attempts()

    async def call(seed):
        index = len(seeds)
        seeds.append(seed)
        kind = outcomes[index] if index < len(outcomes) else 'pass'
        record = {'status': 'OK', 'warnings': [], 'index': index}
        if kind == 'model_call_error':
            raise ModelCallError('BUDGET_EXHAUSTED: scripted')
        if kind == 'cancel':
            raise asyncio.CancelledError()
        if kind == 'http_error':
            return None, dict(record, status='HTTP_ERROR')
        if kind == 'incomplete':
            return None, dict(record, status='INCOMPLETE_OR_REFUSED')
        if kind == 'client_schema_invalid':
            return None, dict(record, status='SCHEMA_INVALID')
        return {'kind': kind, 'index': index}, record

    def parse(value):
        if value['kind'] == 'refuse':
            raise ValueError('labelled refusal')
        return ('parsed', value['index'])

    async def go():
        return await call_until_valid(call, parse, attempts=attempts, base_seed=base, log=log,
                                      persist=lambda current: persisted.append(len(current.entries)))
    try:
        return asyncio.run(go()), seeds, log, persisted
    except BaseException as error:  # the loop must hand every failure through unchanged
        return error, seeds, log, persisted


def expected(outcomes, attempts):
    """The specification of the loop: how many requests are made and how it ends."""
    for index in range(attempts):
        kind = outcomes[index] if index < len(outcomes) else 'pass'
        if kind == 'pass':
            return index + 1, 'parsed'
        if kind not in RETRYABLE:
            return index + 1, kind
    return attempts, 'exhausted'


@given(st.lists(st.sampled_from(OUTCOMES), max_size=7), st.integers(1, 6))
@settings(max_examples=200, deadline=None)
def test_loop_over_generated_outcome_sequences(outcomes, attempts):
    result, seeds, log, persisted = run_script(outcomes, attempts)
    made, ending = expected(outcomes, attempts)
    # attempts used never exceed the configured count, and the loop stops at the first accepted or non-retryable one
    assert len(seeds) == made <= attempts
    # a request that raised (budget, cancellation) never produced a reply, so it has no entry; its code is the error
    logged = made - (1 if ending in ('model_call_error', 'cancel') else 0)
    assert len(log.entries) == len(log.records) == logged
    # attempt 0 sends no override; later attempts send pairwise distinct seeds base + k
    assert seeds == [None] + [attempt_seed(10, k) for k in range(1, made)]
    assert len(set(seeds[1:])) == len(seeds[1:]) and 10 not in seeds[1:]
    assert [entry['attempt'] for entry in log.entries] == list(range(logged))
    assert [entry['seed'] for entry in log.entries] == ([10] + seeds[1:])[:logged]
    if ending == 'parsed':
        assert result[0] == ('parsed', made - 1)
        assert [entry['code'] for entry in log.entries][:-1] == ['SCHEMA_INVALID'] * (made - 1)
        assert log.entries[-1]['code'] is None and log.entries[-1]['reply_digest']
    else:
        assert isinstance(result, BaseException)
        if ending == 'exhausted':
            assert isinstance(result, (ValueError, ))
            assert all(entry['code'] == 'SCHEMA_INVALID' for entry in log.entries)
        elif ending == 'model_call_error':
            assert isinstance(result, ModelCallError) and result.code == 'BUDGET_EXHAUSTED'
        elif ending == 'cancel':
            assert isinstance(result, asyncio.CancelledError)
        else:  # http_error, incomplete: raised as the status, never retried
            assert isinstance(result, ValueError) and str(result) in ('HTTP_ERROR', 'INCOMPLETE_OR_REFUSED')
    # every refused attempt was persisted before the next request
    refused = [index + 1 for index, entry in enumerate(log.entries) if entry['code']]
    assert persisted == refused


def test_attempt_seeds_are_distinct_and_wrap_inside_the_seed_range():
    top = (1 << SEED_BITS) - 1
    seeds = {attempt_seed(top, k) for k in range(1, 6)}
    assert len(seeds) == 5 and top not in seeds and all(0 <= seed <= top for seed in seeds)


@pytest.mark.parametrize('attempts', [0, -1, True, 1.0, '2', None])
def test_attempts_must_be_a_positive_integer(attempts):
    async def call(seed):
        return {}, {'status': 'OK', 'warnings': []}
    with pytest.raises(ValueError, match='attempts'):
        asyncio.run(call_until_valid(call, lambda value: value, attempts=attempts, base_seed=1, log=Attempts()))


def test_a_dropped_seed_is_a_configuration_error_not_a_retry():
    seen = []

    async def call(seed):
        seen.append(seed)
        return {'x': 1}, {'status': 'OK', 'warnings': [('PARAM_UNSUPPORTED', 'sampling.seed')]}

    log = Attempts()
    with pytest.raises(ModelCallError) as caught:
        asyncio.run(call_until_valid(call, lambda value: (_ for _ in ()).throw(ValueError('refused')),
                                     attempts=3, base_seed=5, log=log))
    assert caught.value.code == 'CONFIGURATION_INVALID' and len(seen) == 1


def test_one_attempt_is_the_unchanged_single_request():
    seen = []

    async def call(seed):
        seen.append(seed)
        return {'x': 1}, {'status': 'OK', 'warnings': [('PARAM_UNSUPPORTED', 'sampling.seed')]}  # tolerated at 1
    log = Attempts()
    value, _, _ = asyncio.run(call_until_valid(call, lambda reply: reply, attempts=1, base_seed='none', log=log))
    assert value == {'x': 1} and seen == [None] and log.entries[0]['seed'] is None


def test_the_digest_is_of_the_reply_and_never_its_text():
    marker = 'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS'
    log = Attempts()

    async def call(seed):
        return {'text': marker}, {'status': 'OK', 'warnings': []}
    with pytest.raises(ValueError):
        asyncio.run(call_until_valid(call, lambda reply: (_ for _ in ()).throw(ValueError(marker)), attempts=2,
                                     base_seed=1, log=log))
    assert marker not in repr(log.entries) and all(len(entry['reply_digest']) == 64 for entry in log.entries)
    assert reply_digest({'a': float('nan')}) is None
