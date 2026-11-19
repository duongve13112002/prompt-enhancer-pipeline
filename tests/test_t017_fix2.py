"""T017-FIX2: the planner retry and the S5 writer repair change only `sampling.seed` (G4), and the planner is sent a
reply schema whose positions are bounded to the lists the request sends (G3).

Labelled CPU tests: synthetic replies, in-memory HTTP doubles and scripted role doubles; no model call. The replay
fixtures are synthetic shapes of the J4a3 captures (an empty-shortlist generate reply with an invented knowledge need;
a byte-identical planner retry); no admin prompt, knowledge-base text or image is copied from `runs/`.
"""

import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
from jsonschema import ValidationError
import pytest

from planner_fixtures import PlannerClientDouble, entity, failure, link, record, reply, shortlist_entry
from prompt_enhancer.model_client import CAPABILITIES, BoundModelClient, RoleClient, _digest
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_resources import BudgetExhausted
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.planner import (GENERATE_REPLY_SCHEMA, REPLY_SCHEMA, SEED_BITS, _facet_groups,
                                     bounded_reply_schema, check_reply, drops_seed)
from prompt_enhancer.reply_retry import attempt_seed
from test_planner import kb, literal_reply, png, run as planner_run, shortlist_for  # noqa: F401 - kb is a fixture
from test_planner_generate_schema import SequenceTransport, env, run, sent_schema  # noqa: F401 - env is a fixture
from validation_fixtures import ModelDouble, calibrate, draft, environment, run as validate_run

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / 'contracts' / 'schemas'
REGISTRY = SchemaRegistry(SCHEMAS)
BASE_SEED = 20261119  # labelled test-only seed (the shape of the pilot's sampling.seed)
DROPPED = ('PARAM_UNSUPPORTED', 'sampling.seed')
SOURCE = {'image_id': 'sha256:test', 'role': 'source_image', 'data': base64.b64encode(png()).decode('ascii')}


def seeded(env, seed=BASE_SEED):
    env.config['vlm']['roles']['planner']['sampling']['seed'] = seed
    return env


def real_client(env, transports, *, seed_capability=True):
    capabilities = dict.fromkeys(CAPABILITIES, True)
    capabilities['sampling.seed'] = seed_capability  # False: the mapper drops the seed with PARAM_UNSUPPORTED
    probes = {name: ProbeResult('complete', capabilities, {}) for name in transports}
    client = RoleClient(env.config, transports, probes, SchemaRegistry(SCHEMAS), project_root=env.root)
    return BoundModelClient(SimpleNamespace(client=client), 'planner'), client


# -- (B) the derived schema: tightening only, every position field bounded ------------------------------------------
def groups_of(*counts):
    return [{'group_id': f'group:{index}', 'facet': 'apparel.color',
             'variants': [{'variant_id': f'variant:{index}:{k}', 'value': f'value {k}'} for k in range(count)]}
            for index, count in enumerate(counts)]


def tightened_only(base, derived, path=''):
    """Every difference adds `maximum`/`maxItems` (not above an existing bound) or removes an anyOf branch."""
    if isinstance(base, dict):
        assert isinstance(derived, dict), path
        assert set(derived) - set(base) <= {'maximum', 'maxItems'}, path
        assert set(base) <= set(derived), path
        for key in ('maximum', 'maxItems'):
            if key in derived:
                assert derived[key] >= 0 and derived[key] <= base.get(key, derived[key]), path
        for key, value in base.items():
            if key == 'anyOf' and len(derived[key]) < len(value):
                assert derived[key] and all(branch in value for branch in derived[key]), path
                continue
            tightened_only(value, derived[key], f'{path}/{key}')
    elif isinstance(base, list):
        assert isinstance(derived, list) and len(derived) == len(base), path
        for index, (left, right) in enumerate(zip(base, derived)):
            tightened_only(left, right, f'{path}/{index}')
    else:
        assert base == derived, path


def position_nodes(schema, path=()):
    """Every integer property named `*position` (the class sweep of the reply schema, by structure)."""
    found = []
    if isinstance(schema, dict):
        for key, value in schema.get('properties', {}).items():
            if key.endswith('position'):
                found.append(path + ('properties', key))
            found += position_nodes(value, path + ('properties', key))
        if 'items' in schema:
            found += position_nodes(schema['items'], path + ('items',))
    return found


def at(schema, path):
    for key in path:
        schema = schema[key]
    return schema


@pytest.mark.parametrize('schema_id', [REPLY_SCHEMA, GENERATE_REPLY_SCHEMA])
@pytest.mark.parametrize('counts', [(), ((),), ((2, 3),), ((1,), (), (4, 1, 2))])
def test_derived_schema_only_tightens_and_bounds_every_position(schema_id, counts):
    entries = [shortlist_entry(index) for index in range(len(counts))]
    variants = {entry['concept_id']: groups_of(*group) for entry, group in zip(entries, counts)}
    base, _ = REGISTRY.get(schema_id)
    derived = bounded_reply_schema(REGISTRY, schema_id, entries, variants)
    tightened_only(base, derived)
    assert REGISTRY.get(schema_id)[0] == base  # the registered schema is never changed in place
    names = {path[-1] for path in position_nodes(base)}
    assert names == {'candidate_position', 'position', 'group_position', 'named_variant_position', 'variant_position'}
    for path in position_nodes(base):  # each is bounded, or sits in an array that can hold no item
        node = at(derived, path)
        parents = [at(derived, path[:index]) for index in range(len(path))]
        empty = any(parent.get('maxItems') == 0 for parent in parents if isinstance(parent, dict))
        integer = node if 'anyOf' not in node else next(
            (branch for branch in node['anyOf'] if branch.get('type') == 'integer'), None)
        assert empty or integer is None or 'maximum' in integer, path


def test_empty_shortlist_closes_every_position_but_keeps_no_match_links():
    derived = bounded_reply_schema(REGISTRY, GENERATE_REPLY_SCHEMA, [], {})
    assert derived['properties']['knowledge_needs']['maxItems'] == 0
    link_schema = derived['properties']['links']
    assert 'maxItems' not in link_schema  # links themselves stay open: a no_match link names no position
    assert link_schema['items']['properties']['candidate_position'] == {'anyOf': [{'type': 'null'}]}
    assert link_schema['items']['properties']['candidates']['maxItems'] == 0
    assert link_schema['items']['properties']['variants']['maxItems'] == 0


def test_unexpected_base_shape_is_refused_not_guessed():
    broken = SimpleNamespace(get=lambda schema_id: (_broken_schema(), 'x'))
    with pytest.raises(ValueError):
        bounded_reply_schema(broken, REPLY_SCHEMA, [shortlist_entry(0)], {})


def _broken_schema():
    schema, _ = REGISTRY.get(REPLY_SCHEMA)
    schema['properties']['knowledge_needs']['items']['properties']['candidate_position'] = {'type': 'string'}
    return schema


PROMPT = 'm0 m1 m2 m3'  # synthetic mentions
FACET = sorted(_facet_groups(REGISTRY))[0] + '.synthetic'


@st.composite
def accepted_case(draw):
    """A shortlist, its variant groups and a reply that check_reply accepts (built by construction)."""
    count = draw(st.integers(0, 3))
    entries = [shortlist_entry(index) for index in range(count)]
    variants = {entry['concept_id']: groups_of(*draw(st.lists(st.integers(0, 3), max_size=3))) for entry in entries}
    links, linked = [], set()
    for index in range(draw(st.integers(0, 3))):
        status = draw(st.sampled_from(['no_match', 'resolved', 'family'] if count else ['no_match']))
        positions = draw(st.lists(st.integers(0, count - 1), unique=True, min_size=1 if status == 'family' else 0))\
            if count else []
        own = draw(st.integers(0, count - 1)) if status == 'resolved' else None
        usable = {own} if status == 'resolved' else set(positions) if status == 'family' else set()
        items = []
        for position in sorted(usable):
            for group_position, group in enumerate(variants[entries[position]['concept_id']]):
                size = len(group['variants'])
                if not draw(st.booleans()):
                    continue
                named = draw(st.one_of(st.none(), st.integers(0, size - 1))) if size else None
                if named is None and not size:
                    continue  # an empty group can be neither named nor fully rated by a valid reply
                rated = [] if named is not None and draw(st.booleans()) else draw(st.permutations(range(size)))
                items.append({'candidate_position': position, 'group_position': group_position,
                              'named_variant_position': named,
                              'ratings': [{'variant_position': v, 'context_fit': 'fits'} for v in rated]})
        links.append(link(f'm{index}', status, position=own, candidates=[(p, 'neutral') for p in positions],
                          variants=items))
        linked |= usable
    needs = [{'candidate_position': position, 'facets': [FACET]}
             for position in draw(st.lists(st.sampled_from(sorted(linked)), unique=True)) ] if linked else []
    return entries, variants, reply(links=links, knowledge_needs=needs)


@settings(max_examples=150, deadline=None)
@given(accepted_case(), st.sampled_from([REPLY_SCHEMA, GENERATE_REPLY_SCHEMA]))
def test_property_derived_schema_accepts_every_reply_the_base_and_check_reply_accept(case, schema_id):
    entries, variants, value = case
    REGISTRY.validate(schema_id, value)
    check_reply(value, PROMPT, 'generate', entries, variants, _facet_groups(REGISTRY))
    REGISTRY.validate_with(bounded_reply_schema(REGISTRY, schema_id, entries, variants), value)


def out_of_range(kind, count, widest, largest, beyond):
    """One reply naming a position just outside the lists sent; the base schema accepts each."""
    variant = {'candidate_position': 0, 'group_position': 0, 'named_variant_position': 0, 'ratings': []}
    if kind == 'need':
        return reply(knowledge_needs=[{'candidate_position': count + beyond, 'facets': [FACET]}])
    if kind == 'resolved':
        return reply(links=[link('m0', 'resolved', position=count + beyond)])
    if kind == 'candidate':
        return reply(links=[link('m0', 'no_match', candidates=[(count + beyond, 'fits')])])
    changed = {'variant_candidate': {'candidate_position': count + beyond},
               'group': {'group_position': widest + beyond},
               'named': {'named_variant_position': largest + beyond},
               'rating': {'named_variant_position': None,
                          'ratings': [{'variant_position': largest + beyond, 'context_fit': 'fits'}]}}[kind]
    return reply(links=[link('m0', 'resolved', position=0, variants=[dict(variant, **changed)])])


@settings(max_examples=150, deadline=None)
@given(st.lists(st.lists(st.integers(0, 3), max_size=3), max_size=3), st.integers(0, 5),
       st.sampled_from(['need', 'resolved', 'candidate', 'variant_candidate', 'group', 'named', 'rating']),
       st.sampled_from([REPLY_SCHEMA, GENERATE_REPLY_SCHEMA]))
def test_property_derived_schema_rejects_every_out_of_range_position(counts, beyond, kind, schema_id):
    entries = [shortlist_entry(index) for index in range(len(counts))]
    variants = {entry['concept_id']: groups_of(*group) for entry, group in zip(entries, counts)}
    widest = max((len(group) for group in counts), default=0)
    largest = max((size for group in counts for size in group), default=0)
    value = out_of_range(kind, len(entries), widest, largest, beyond)
    REGISTRY.validate(schema_id, value)  # the registered contract alone lets it through
    with pytest.raises(ValidationError):
        REGISTRY.validate_with(bounded_reply_schema(REGISTRY, schema_id, entries, variants), value)


# -- (B) through the one client path ----------------------------------------------------------------------------------
INVENTED_NEED = reply(knowledge_needs=[{'candidate_position': 0, 'facets': [FACET]}])  # the J4a3 nomatch shape


def test_replay_g3_invented_need_is_refused_by_the_sent_schema_client_side(env):
    """Replay G3 (synthetic): empty shortlist, the model invents knowledge_needs[0].candidate_position 0 twice. The
    base schema accepts the reply; the sent schema refuses it in the client (SCHEMA_INVALID, not the invariant)."""
    REGISTRY.validate(GENERATE_REPLY_SCHEMA, INVENTED_NEED)
    transport = SequenceTransport(json.dumps(INVENTED_NEED))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound, 'Kimono')
    assert len(transport.bodies) == 2
    assert all(sent_schema(body)['properties']['knowledge_needs']['maxItems'] == 0 for body in transport.bodies)
    assert [call['status'] for call in result['calls']] == ['SCHEMA_INVALID', 'SCHEMA_INVALID']
    assert all('reply_check' not in call for call in result['calls'])
    assert result['state'] == 'degraded' and result['error']['code'] == 'PLANNER_REPLY_INVALID'


def test_sent_schema_hash_is_over_the_object_in_the_posted_body_and_the_record_names_the_base(env):
    transport = SequenceTransport(json.dumps(literal_reply()))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound)
    call = result['calls'][0]
    assert call['schema_id'] == GENERATE_REPLY_SCHEMA
    assert call['schema_hash'] == REGISTRY.get(GENERATE_REPLY_SCHEMA)[1]  # the file hash of the base contract
    assert call['sent_schema_hash'] == _digest(sent_schema(transport.bodies[0]))
    assert call['sent_schema_hash'] != _digest(REGISTRY.get(GENERATE_REPLY_SCHEMA)[0])


def test_edit_reply_with_valid_links_and_needs_is_still_accepted(env):
    env.config['rag']['enabled'] = True
    entries = [dict(shortlist_entry(0, 'Kimono'), concept_id='concept:' + 'a' * 64,
                    matched_span={'start': 0, 'end': 6, 'text': 'Kimono'})]
    shortlist = {'snapshot_id': 'snap:test', 'state': 'ready', 'candidates': entries, 'warnings': [], 'error': None}
    rows = [{'concept_id': entries[0]['concept_id'], 'variant_groups': groups_of(2)}]
    snapshot = SimpleNamespace(snapshot_id='snap:test', read_rows=lambda table: rows, release=lambda: None)
    edit = reply(edit_kind='local_edit', entities=[entity('e1', 'Kimono')],
                 links=[link('Kimono', 'resolved', position=0, entity_id='e1', candidates=[(0, 'fits')], variants=[
                     {'candidate_position': 0, 'group_position': 0, 'named_variant_position': None,
                      'ratings': [{'variant_position': 1, 'context_fit': 'fits'},
                                  {'variant_position': 0, 'context_fit': 'neutral'}]}])],
                 knowledge_needs=[{'candidate_position': 0, 'facets': [FACET]}],
                 edit_targets=[{'id': 't1', 'entity_id': 'e1', 'locator': 'the kimono'}],
                 delta=[{'id': 'd1', 'target_id': 't1', 'operation': 'recolor', 'description': 'synthetic'}])
    transport = SequenceTransport(json.dumps(edit))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound, 'Kimono', task='edit', source=SOURCE, shortlist=shortlist, snapshot=snapshot)
    assert result['state'] == 'ready' and len(transport.bodies) == 1, result['calls']
    assert result['intent_ir']['knowledge_needs'][0]['concept_id'] == entries[0]['concept_id']
    assert sent_schema(transport.bodies[0])['properties']['links']['items']['properties']['candidates']['items'][
        'properties']['position']['maximum'] == 0


def test_no_match_link_with_an_empty_shortlist_is_still_accepted(env):
    """The edit case's link shape (`no_match`, no position, no candidates) stays valid; synthetic mention."""
    prompt = '把珍珠挂链换成金色'  # synthetic Chinese prompt, not the captured one
    edit = reply(edit_kind='local_edit', links=[link('珍珠挂链', 'no_match')],
                 edit_targets=[{'id': 't1', 'entity_id': None, 'locator': '珍珠挂链'}],
                 delta=[{'id': 'd1', 'target_id': 't1', 'operation': 'recolor', 'description': '金色'}])
    transport = SequenceTransport(json.dumps(edit, ensure_ascii=False))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound, prompt, task='edit', source=SOURCE)
    assert result['state'] == 'ready' and len(transport.bodies) == 1, result['calls']
    assert result['intent_ir']['concept_links'][0]['status'] == 'no_match'
    assert result['calls'][0]['schema_id'] == REPLY_SCHEMA


def test_budget_fallback_and_retry_send_the_derived_schema(env):
    class Exhausted:
        bodies = []

        async def post(self, route, body, headers):
            self.bodies.append(body)
            raise BudgetExhausted('main')

    backends = env.config['vlm']['backends']
    backends['spare'] = deepcopy(backends['main'])
    backends['spare']['probe'] = {'max_requests': len(CAPABILITIES), 'timeout_s': 1}  # test-only probe allowance
    backends['main']['on_budget_exhausted'] = 'fallback_to_default_backend'
    env.config['vlm']['default_backend'] = 'spare'
    # the spare answers every request (its labelled probes included) with the invented need
    first, spare = Exhausted(), SequenceTransport(json.dumps(INVENTED_NEED))
    bound, client = real_client(env, {'main': first, 'spare': spare})
    client.probes.pop('spare')
    client.probe_cases['spare'] = {name: ({'probe': True}, lambda response: True)  # labelled double probes
                                   for name in CAPABILITIES}
    result = run(env, bound)
    planned = [body for body in spare.bodies if 'response_format' in body]
    assert len(first.bodies) == 2 and len(planned) == 2  # attempt 0 and the retry, each falling back
    assert all(sent_schema(body)['properties']['knowledge_needs']['maxItems'] == 0
               for body in (*first.bodies, *planned))
    # the fallback validated each reply against the sent schema (the base one accepts it)
    assert [call['status'] for call in result['calls']] == ['SCHEMA_INVALID', 'SCHEMA_INVALID']
    assert result['state'] == 'degraded'


@pytest.mark.parametrize('value', [[], {'type': 'nonsense-type'}])
def test_invalid_sent_schema_is_refused_before_http(env, value):
    transport = SequenceTransport(json.dumps(reply()))
    _, client = real_client(env, {'main': transport})
    with pytest.raises(Exception):
        asyncio.run(client.call('planner', b'admin', 'data', schema=value))
    assert transport.bodies == []


# -- (A) the planner retry changes only sampling.seed -----------------------------------------------------------------
#  schema-valid, but its literal span is not prompt text, so check_reply refuses it (the J4a3 edit-case rule)
NOT_VERBATIM = reply(literal_texts=[{'id': 'l1', 'text': 'CLOSED', 'span': '"CLOSED"'}])


def without_seed(body):
    return {key: value for key, value in body.items() if key != 'seed'}


def test_replay_g4_planner_retry_differs_from_attempt_0_only_in_seed(env):
    """Replay G4 (synthetic): the first reply breaks an invariant; the retry must not be byte-identical."""
    seeded(env)
    transport = SequenceTransport(json.dumps(NOT_VERBATIM), json.dumps(literal_reply()))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound)
    first, second = transport.bodies
    assert first['seed'] == BASE_SEED and second['seed'] == attempt_seed(BASE_SEED, 1) != BASE_SEED
    assert without_seed(first) == without_seed(second) and first != second
    assert result['state'] == 'ready'
    assert [call['attempt'] for call in result['calls']] == [
        {'attempt': 0, 'seed': BASE_SEED, 'code': 'INVARIANT_INVALID', 'reply_digest': result['calls'][0][
            'attempt']['reply_digest']},
        {'attempt': 1, 'seed': attempt_seed(BASE_SEED, 1), 'code': None,
         'reply_digest': result['calls'][1]['attempt']['reply_digest']}]
    assert all(len(call['attempt']['reply_digest']) == 64 for call in result['calls'])
    assert result['calls'][0]['request_hash'] != result['calls'][1]['request_hash']


def test_attempt_0_is_the_request_the_old_path_builds(env):
    """Pre-mortem: attempt 0 changed. Its body equals the body of a direct call without seed or retry logic, once the
    (B) schema is put back to the registered file, and the double sees no `seed` keyword on attempt 0."""
    seeded(env)
    transport = SequenceTransport(json.dumps(literal_reply()))
    bound, client = real_client(env, {'main': transport})
    run(env, bound)
    planned = transport.bodies[0]
    system = planned['messages'][0]['content'].encode('utf-8')
    asyncio.run(client.call('planner', system, planned['messages'][1]['content'], schema_id=GENERATE_REPLY_SCHEMA))
    old = transport.bodies[1]
    restored = deepcopy(planned)
    restored['response_format']['json_schema']['schema'] = REGISTRY.get(GENERATE_REPLY_SCHEMA)[0]
    assert restored == old and planned['seed'] == BASE_SEED
    double = PlannerClientDouble(failure('SCHEMA_INVALID'), literal_reply())
    run(seeded(env), double)
    assert 'seed' not in double.calls[0] and double.calls[1]['seed'] == attempt_seed(BASE_SEED, 1)


def test_unseeded_planner_retry_stays_a_plain_resend(env):
    env.config['vlm']['roles']['planner']['sampling']['seed'] = 'none'  # the test profile's own value
    transport = SequenceTransport(json.dumps(NOT_VERBATIM), json.dumps(literal_reply()))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound)
    assert transport.bodies[0] == transport.bodies[1] and 'seed' not in transport.bodies[0]
    assert [call['attempt']['seed'] for call in result['calls']] == [None, None]
    double = PlannerClientDouble(failure('SCHEMA_INVALID'), literal_reply())
    run(env, double)
    assert all('seed' not in call for call in double.calls)  # never a seed for a role that had none


def dropped(status='SCHEMA_INVALID'):
    return None, record(status, warnings=[DROPPED])


def test_dropped_seed_refuses_the_retry_instead_of_repeating_it(env):
    seeded(env)
    client = PlannerClientDouble(dropped(), literal_reply())
    result = run(env, client)
    assert len(client.calls) == 1  # no repeat is sent
    assert result['state'] == 'failed' and result['error'] == {
        'code': 'PLANNER_CALL_FAILED', 'message': 'S2: the planner call failed', 'cause': 'CONFIGURATION_INVALID'}


def test_dropped_seed_does_not_touch_a_valid_attempt_0(env):
    seeded(env)
    client = PlannerClientDouble((literal_reply(), record('OK', warnings=[DROPPED])))
    assert run(env, client)['state'] == 'ready' and len(client.calls) == 1


def test_retry_record_that_dropped_the_seed_is_refused(env):
    seeded(env)
    client = PlannerClientDouble(failure('SCHEMA_INVALID'), (literal_reply(), record('OK', warnings=[DROPPED])))
    result = run(env, client)
    assert len(client.calls) == 2 and result['state'] == 'failed'
    assert result['error']['cause'] == 'CONFIGURATION_INVALID'
    assert result['calls'][1]['attempt']['code'] == 'CONFIGURATION_INVALID'


def test_real_backend_without_seed_mapping_refuses_the_retry(env):
    seeded(env)
    transport = SequenceTransport(json.dumps(NOT_VERBATIM))
    bound, _ = real_client(env, {'main': transport}, seed_capability=False)
    result = run(env, bound)
    assert len(transport.bodies) == 1 and 'seed' not in transport.bodies[0]
    assert drops_seed(result['calls'][0]) and result['error']['cause'] == 'CONFIGURATION_INVALID'


@settings(max_examples=200, deadline=None)
@given(st.integers(0, (1 << SEED_BITS) - 1), st.integers(2, 64))
def test_property_attempt_seeds_are_pairwise_distinct(base, attempts):
    seeds = [base] + [attempt_seed(base, attempt) for attempt in range(1, attempts)]
    assert len(set(seeds)) == attempts and all(0 <= seed < 1 << SEED_BITS for seed in seeds)


# -- (A) the S5 writer repair: round k sends attempt_seed(base, k) ----------------------------------------------------
@pytest.fixture
def s5(model_test_profile, tmp_path, project_root):
    env = environment(model_test_profile, tmp_path, project_root, constraints=[('c1', 'style', 'kept')])
    env.config['vlm']['roles']['writer']['sampling']['seed'] = BASE_SEED
    calibrate(env)  # after the seed: the calibration binds the writer settings, sampling included
    return env


FAIL = {'results': [{'constraint_id': 'c1', 'verdict': 'fail', 'evidence_span': None, 'note': 'missing'}]}


def test_writer_repair_rounds_send_distinct_seeds_and_record_them(s5):
    s5.config['validation']['max_repairs'] = 3
    s5.writer.replies = ['kept'] * 3
    s5.verifier.replies = [deepcopy(FAIL) for _ in range(4)]
    result = validate_run(s5)
    seeds = [kwargs.get('seed') for _, _, kwargs in s5.writer.calls]
    assert seeds == [attempt_seed(BASE_SEED, k) for k in (1, 2, 3)] and len(set(seeds + [BASE_SEED])) == 4
    attempts = [json.loads(call['record_json']).get('attempt') for call in result['calls']]
    assert [entry['seed'] for entry in attempts if entry] == seeds
    assert [entry['attempt'] for entry in attempts if entry] == [1, 2, 3]
    data = [json.loads(user.split('\n', 1)[1]) for _, user, _ in s5.writer.calls]
    assert all(set(item['repair']) == {'draft', 'failures'} for item in data)  # repair data as today, nothing more


def test_writer_attempt_0_sends_no_seed_keyword_and_records_the_base(s5):
    from prompt_enhancer.writer import write

    asyncio.run(write(s5.s0, s5.s2, None, s5.writer, project_root=s5.root))
    assert 'seed' not in s5.writer.calls[0][2]


def test_unseeded_writer_repair_is_a_plain_resend(s5):
    s5.config['vlm']['roles']['writer']['sampling']['seed'] = 'none'
    calibrate(s5)
    s5.config['validation']['max_repairs'] = 2
    s5.writer.replies = ['kept'] * 2
    s5.verifier.replies = [deepcopy(FAIL) for _ in range(3)]
    validate_run(s5)
    assert len(s5.writer.calls) == 2 and all('seed' not in kwargs for _, _, kwargs in s5.writer.calls)


def test_writer_repair_still_runs_when_the_draft_request_dropped_the_seed(s5):
    """Fix round 1, F1: a repair carries the draft and the failures, so it never repeats the earlier request."""
    s5.config['validation']['max_repairs'] = 1
    s5.writer.replies = ['kept']
    s5.verifier.replies = [deepcopy(FAIL) for _ in range(2)]
    initial = draft(s5)
    initial['calls'][0]['record_json'] = json.dumps(dict(json.loads(initial['calls'][0]['record_json']),
                                                         warnings=[list(DROPPED)]))
    result = validate_run(s5, result=initial)
    assert len(s5.writer.calls) == 1 and result['repair_count'] == 1
    assert s5.writer.calls[0][2]['seed'] == attempt_seed(BASE_SEED, 1)
    assert 'CONFIGURATION_INVALID' not in [call['status'] for call in result['calls']]


class DroppingWriter(ModelDouble):
    """Labelled double of a seedless backend: every writer record says the backend dropped sampling.seed."""

    async def call(self, system, user, **kwargs):
        value, found = await super().call(system, user, **kwargs)
        return value, dict(found, warnings=[DROPPED])


def test_seeded_writer_on_a_seedless_backend_still_repairs_and_records_no_sent_seed(s5):
    s5.config['validation']['max_repairs'] = 2
    s5.writer = DroppingWriter(s5.config, 'writer', replies=['kept'] * 2, events=s5.events)
    s5.verifier.replies = [deepcopy(FAIL) for _ in range(3)]
    result = validate_run(s5)
    assert len(s5.writer.calls) == 2 and result['repair_count'] == 2
    assert 'CONFIGURATION_INVALID' not in [call['status'] for call in result['calls']]
    records = [json.loads(call['record_json']) for call in result['calls'] if 'attempt' in json.loads(
        call['record_json'])]
    assert [entry['attempt'] for entry in records] == [{'attempt': k, 'seed': None, 'code': None,
                                                         'reply_digest': entry['attempt']['reply_digest']}
                                                        for k, entry in zip((1, 2), records)]
    assert all(list(DROPPED) in entry['warnings'] for entry in records)  # the warning is kept as today


# -- fix round 1: N1 (hash names where the schema went) and N4 (the second chance widens the bound) ------------------
def test_schema_only_checked_client_side_is_recorded_as_client_schema_hash(env):
    env.config['vlm']['roles']['planner']['output']['format'] = 'json_object'  # no schema is placed in the body
    derived = bounded_reply_schema(REGISTRY, GENERATE_REPLY_SCHEMA, [], {})
    transport = SequenceTransport(json.dumps(literal_reply()), json.dumps(INVENTED_NEED))
    _, client = real_client(env, {'main': transport})
    value, found = asyncio.run(client.call('planner', b'admin', 'data', schema_id=GENERATE_REPLY_SCHEMA,
                                           schema=derived))
    assert value is not None and transport.bodies[0]['response_format'] == {'type': 'json_object'}
    assert found['client_schema_hash'] == _digest(derived) and 'sent_schema_hash' not in found
    value, found = asyncio.run(client.call('planner', b'admin', 'data', schema_id=GENERATE_REPLY_SCHEMA,
                                           schema=derived))
    assert value is None and found['status'] == 'SCHEMA_INVALID'  # still checked against the derived schema


def test_schema_placed_in_the_body_is_recorded_as_sent_schema_hash_only(env):
    transport = SequenceTransport(json.dumps(literal_reply()))
    bound, _ = real_client(env, {'main': transport})
    call = run(env, bound)['calls'][0]
    assert call['sent_schema_hash'] == _digest(sent_schema(transport.bodies[0])) and 'client_schema_hash' not in call


def test_second_chance_ask_widens_the_bound_to_the_larger_shortlist(kb):
    kb.config['planner']['second_chance_rounds'] = 1
    kb.config['candidates']['shortlist_size'] = 1
    prompt = 'Kimono with a Donut'
    first = reply(links=[link('Kimono', 'resolved', position=0), link('Donut', 'no_match', needs_lookup=True)])
    second = reply(links=[link('Kimono', 'resolved', position=0), link('Donut', 'resolved', position=1, role='object')])
    client = PlannerClientDouble(first, second)
    result = planner_run(kb, client, prompt, shortlist=shortlist_for(kb, prompt), snapshot=kb.snapshot)
    assert [len(json.loads(call['text'])['shortlist']) for call in client.calls] == [1, 2]
    properties = [call['schema']['properties'] for call in client.calls]
    links = [next(branch for branch in item['links']['items']['properties']['candidate_position']['anyOf']
                  if branch.get('type') == 'integer')['maximum'] for item in properties]
    needs = [item['knowledge_needs']['items']['properties']['candidate_position']['maximum'] for item in properties]
    assert links == [0, 1] and needs == [0, 1]
    assert result['intent_ir']['concept_links'][1]['status'] == 'resolved'
