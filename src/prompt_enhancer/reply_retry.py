"""Retry a refused model reply with another seed (decisions 183 and 188).

The request is re-sent identical except `sampling.seed`: no validator message, no earlier reply and nothing else goes
back to the model (R-026), and nothing about the reply is repaired or relaxed. Only a reply that arrived and failed
validation is retried; transport, budget, configuration and cancellation failures propagate at once.

`call_until_valid` drives K2 observation and K3 cards. `attempt_seed` is also used by the plain image probe
(`model_client.RoleClient._retry_plain_image`), the S2 planner retry (`planner.plan`) and the S5 writer repair
(`writer.attempt_seed_of`), each with its own loop (T017-FIX2). The planner refuses a retry the backend would send
without its seed only when the retry is due (its attempt 0 is not a configured attempt count). The writer repair is
never refused for that: its brief carries the draft and the failures, so it never repeats the earlier request.
"""

from .k2_naming import _digest, failure_code
from .model_client import ModelCallError
from .planner import SEED_BITS
from .storage import StorageError


# Structural: the exception types a `parse_*` validator raises for a reply that arrived (not a content rule).
REFUSED = (ValueError, TypeError, UnicodeError, OverflowError)
SEED_WARNING = ('PARAM_UNSUPPORTED', 'sampling.seed')


def attempt_seed(base, attempt):
    """Seed of attempt `attempt` (>= 1): pairwise distinct for attempts below 2**SEED_BITS."""
    return (base + attempt) % (1 << SEED_BITS)


class Attempts:
    """The attempt entries and call records of one item, filled in place so a failure keeps what happened."""

    def __init__(self):
        self.entries = []
        self.records = []


def reply_digest(value):
    """Digest of a reply that arrived, never its text; None when it has no canonical form."""
    try:
        return _digest(value)
    except (StorageError, ValueError, TypeError, UnicodeError, OverflowError):
        return None


async def call_until_valid(call, parse, *, attempts, base_seed, log, persist=None):
    """Run `call(seed)` up to `attempts` times until `parse(reply)` accepts.

    `seed` is None for attempt 0 (the request as the profile defines it) and `attempt_seed(base_seed, k)` after.
    Returns `(parsed, reply, record)`. A refused reply is logged and retried; the last refusal is raised unchanged,
    so exhaustion fails the item exactly as a single attempt does. `persist(log)` runs after every refused attempt,
    so a crash between attempts keeps the earlier ones.
    """
    if type(attempts) is not int or attempts < 1:
        raise ValueError('attempts must be a positive integer')
    for attempt in range(attempts):
        seed = attempt_seed(base_seed, attempt) if attempt else None
        value, record = await call(seed)
        entry = {'attempt': attempt, 'seed': seed if attempt else (base_seed if type(base_seed) is int else None),
                 'code': None, 'reply_digest': None}
        log.entries.append(entry)
        log.records.append(record)
        if attempts > 1 and any(tuple(item) == SEED_WARNING for item in record.get('warnings', ())):
            entry['code'] = 'CONFIGURATION_INVALID'
            raise ModelCallError('CONFIGURATION_INVALID: the backend dropped sampling.seed, so a retry would repeat the request')
        last = attempt + 1 == attempts
        if value is None:
            entry['code'] = record['status']
            if persist:
                persist(log)
            if record['status'] != 'SCHEMA_INVALID' or last:
                raise ValueError(record['status'])
            continue
        entry['reply_digest'] = reply_digest(value)
        try:
            return parse(value), value, record
        except REFUSED as error:
            entry['code'] = failure_code(error)
            if persist:
                persist(log)
            if last:
                raise
    raise AssertionError('unreachable')
