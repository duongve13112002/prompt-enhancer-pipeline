"""MS-1: the multi-source v1 schemas, their contract examples and the 'nothing existing changed' guards.

The schemas and prompts are drafts that no code path selects yet (MS-2 to MS-7 build on them), so this test checks
the files themselves: they are valid schemas, the valid examples pass, the invalid ones fail, and the cases that only
code can refuse (an index above n, descending order, a duplicate image) pass the schema on purpose.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
import pytest

from prompt_enhancer.model_schemas import SchemaRegistry

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / 'contracts' / 'schemas'
FIXTURES = ROOT / 'tests' / 'fixtures' / 'contracts' / 'multi_source'
NEW_SCHEMAS = ('request.v1', 'intake_result.v2', 'planner_reply.v1', 'intent_ir.v1', 'plan_result.v2')
ENVELOPE_V1 = SCHEMAS / 'response_envelope.v1.schema.json'  # written by T017-FIX3, referenced by intake_result.v2
DRAFT_PROMPTS = ('planner_v2', 'brief_v1', 'manifest_drafter_v1')

# Test-only oracle: SHA-256 of the LF-normalised bytes of every file MS-1 must leave alone (decision 179:
# "request.v0 and every one-image request stay byte-identical"; the design's 'kept, never edited' column).
UNCHANGED = {
    'contracts/schemas/request.v0.schema.json': 'bc416007804fc858dedb168c324348e4e1fd288044c554d2a4b86b8274ba448d',
    'contracts/schemas/intake_result.v0.schema.json': 'bc5c0a811026f94509a9806fb8c95749fc363c2fab4b8bfe1dcbe314d3e0145b',
    'contracts/schemas/planner_reply.v0.schema.json': '6b70d57fd60e6a253aef3b8b6d9712601107b372de383d0988541ec4e553a59e',
    'contracts/schemas/intent_ir.v0.schema.json': 'a5d4084be03c9c055653be89094f09a174c054a87b42a3271154561722c7230c',
    'contracts/schemas/plan_result.v0.schema.json': '16397b3a73a643e20d648479cc3d1b8065cdf22387545c54c914c6ef03545211',
    'contracts/schemas/plan_result.v1.schema.json': 'e60b0889d886418721808c67ea7aac3f2b2ceef61be45f7167a8ad826672dff4',
    'prompts/planner_v1.txt': 'ae0a21a9ab644dcd35d8beb0d7ff88fa07aec007f9ac3d0ce525c03bf66440a7',
    'prompts/brief_v0.txt': 'c73727dcbc42cd6242f27dab8e5a62acbdf3f7be4701a9a494f853647f781657',
    'prompts/manifest_drafter_v0.txt': 'aa293633cb53b92d8651dfb21dedabddec722b11b1b77c96a76326c048281b6b',
}


@pytest.fixture(scope='module')
def registry():
    return SchemaRegistry(SCHEMAS)


def _load(schema_id):
    return json.loads((FIXTURES / f'{schema_id}.json').read_text(encoding='utf-8'))


def _apply(base, case):
    """The base with the case's 'set' (path, value) and 'delete' (path) edits; the base itself is never changed."""
    instance = deepcopy(base)
    for path, value in case.get('set', []):
        node = instance
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    for path in case.get('delete', []):
        node = instance
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return instance


def _base(fixture):
    base = deepcopy(fixture['base'])
    if fixture.get('intent_ir_from'):
        base['intent_ir'] = _load(fixture['intent_ir_from'].removesuffix('.json'))['base']
    return base


def _skip_while_waiting_for_t017_fix3(schema_id):
    """intake_result.v2 references response_envelope.v1; its instances cannot resolve before T017-FIX3 lands."""
    if schema_id == 'intake_result.v2' and not ENVELOPE_V1.exists():
        pytest.skip('waiting for T017-FIX3: response_envelope.v1 (referenced by intake_result.v2) is not in this tree; '
                    'the PM re-runs this test after the merge')


def _cases(schema_id, kind):
    fixture = _load(schema_id)
    return [pytest.param(fixture, case, id=case['name'][:70]) for case in fixture[kind]]


@pytest.mark.parametrize('schema_id', NEW_SCHEMAS)
def test_new_schema_is_a_valid_draft_2020_12_schema_with_its_own_id(schema_id):
    schema = json.loads((SCHEMAS / f'{schema_id}.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    assert schema['$id'] == schema_id
    assert 'designed, not yet implemented' in schema['$comment']


@pytest.mark.parametrize('schema_id', NEW_SCHEMAS)
def test_every_new_schema_has_a_valid_and_an_invalid_example(schema_id):
    fixture = _load(schema_id)
    assert fixture['schema'] == schema_id
    assert fixture['valid'] and fixture['invalid']


@pytest.mark.parametrize(('fixture', 'case'), [p for s in NEW_SCHEMAS for p in _cases(s, 'valid')])
def test_valid_examples_pass(registry, fixture, case):
    _skip_while_waiting_for_t017_fix3(fixture['schema'])
    registry.validate(fixture['schema'], _apply(_base(fixture), case))


@pytest.mark.parametrize(('fixture', 'case'), [p for s in NEW_SCHEMAS for p in _cases(s, 'invalid')])
def test_invalid_examples_fail(registry, fixture, case):
    _skip_while_waiting_for_t017_fix3(fixture['schema'])
    # The base is valid (test above), so a failing case fails because of its own edit.
    assert case.get('set') or case.get('delete'), 'an invalid case must change the base'
    with pytest.raises(ValidationError):
        registry.validate(fixture['schema'], _apply(_base(fixture), case))


@pytest.mark.parametrize(('fixture', 'case'), [p for s in NEW_SCHEMAS for p in _cases(s, 'code_refused')])
def test_code_refused_examples_are_schema_valid_on_purpose(registry, fixture, case):
    _skip_while_waiting_for_t017_fix3(fixture['schema'])
    # A schema cannot see n or compare two fields; the contract docs name the code check that refuses each of these.
    assert case.get('why'), 'every code-refused case names the check that refuses it'
    registry.validate(fixture['schema'], _apply(_base(fixture), case))


@pytest.mark.parametrize('index', [0, -1, 1.5, '1', None, True])
def test_a_source_index_below_one_or_not_an_integer_is_refused_everywhere(registry, index):
    """A 0-based, negative, fractional, string, null or boolean index is refused in every list that carries indexes."""
    reply = _base(_load('planner_reply.v1'))
    list_paths = (('edit_targets', 0, 'source_indexes'), ('preserve', 0, 'source_indexes'),
                  ('entities', 0, 'source_indexes'), ('entities', 0, 'attributes', 0, 'source_indexes'))
    for path in list_paths:
        instance = deepcopy(reply)
        node = instance
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = [index]
        with pytest.raises(ValidationError):
            registry.validate('planner_reply.v1', instance)
    if index is not None:  # a null base is the allowed 'no base image' value (intent_ir.v1 ties it to subject_recompose)
        instance = deepcopy(reply)
        instance['base_source_index'] = index
        with pytest.raises(ValidationError):
            registry.validate('planner_reply.v1', instance)


def test_existing_v0_schemas_and_prompts_are_byte_unchanged():
    for relative, expected in UNCHANGED.items():
        raw = (ROOT / relative).read_bytes().replace(b'\r\n', b'\n')
        assert hashlib.sha256(raw).hexdigest() == expected, f'{relative} was edited; multi-source adds new versions only'


def test_intake_result_v2_points_at_the_t017_fix3_envelope_by_id():
    schema = json.loads((SCHEMAS / 'intake_result.v2.schema.json').read_text(encoding='utf-8'))
    assert schema['properties']['envelope'] == {'oneOf': [{'type': 'null'}, {'$ref': 'response_envelope.v1'}]}


def test_plan_result_v1_stays_the_t017_fix_file_and_v2_is_the_multi_source_one(registry):
    v1 = json.loads((SCHEMAS / 'plan_result.v1.schema.json').read_text(encoding='utf-8'))
    v2 = json.loads((SCHEMAS / 'plan_result.v2.schema.json').read_text(encoding='utf-8'))
    assert v1['properties']['intent_ir']['anyOf'][0] == {'$ref': 'intent_ir.v0'}
    assert v2['properties']['intent_ir']['anyOf'][0] == {'$ref': 'intent_ir.v1'}
    for key in ('state', 'selections', 'variant_ratings', 'warnings', 'calls', 'error'):
        assert v2['properties'][key] == v1['properties'][key]


@pytest.mark.parametrize('prompt_id', DRAFT_PROMPTS)
def test_draft_prompts_exist_and_no_code_or_configuration_selects_them(prompt_id):
    text = (ROOT / 'prompts' / f'{prompt_id}.txt').read_text(encoding='utf-8')
    assert text.strip()
    for directory in ('src', 'configs'):
        for path in (ROOT / directory).rglob('*'):
            if path.is_file() and path.suffix in ('.py', '.yaml', '.yml', '.json', '.txt'):
                assert prompt_id not in path.read_text(encoding='utf-8', errors='replace'), f'{path} selects {prompt_id}'
    readme = (ROOT / 'prompts' / 'README.md').read_text(encoding='utf-8')
    entry = readme.split(f'## `{prompt_id}`', 1)[1].split('\n## ', 1)[0]
    assert 'status: draft, not selected' in entry


@pytest.mark.parametrize('prompt_id', ('planner_v2', 'brief_v1'))
def test_model_facing_drafts_keep_images_and_data_as_data(prompt_id):
    text = (ROOT / 'prompts' / f'{prompt_id}.txt').read_text(encoding='utf-8')
    assert 'data, never an instruction' in text or 'remain data' in text
    assert 'image' in text
