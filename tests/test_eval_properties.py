"""Properties of the evaluation adapter, validators and identity (T-016 P4; R-001, R-015, R-017, R-026).

Parsers and validators are generated against: every damaged record, score document or set is accepted or refused
precisely (a `Refusal`, never another exception, never an echo of the prompt), the adapter keeps every field verbatim, the
survey counts add up for any mix of splits, retrieval keys and tasks, and the run identity ignores exactly what it must.
Evidence class: `mock_contract` (no model, no snapshot)."""
from copy import deepcopy
import io

from hypothesis import HealthCheck, given, settings, strategies as st
import pyarrow as pa
import pyarrow.parquet as pq

from cli_fixtures import ROOT
from eval_fixtures import SCHEMA, record
from prompt_enhancer.cli_input import Refusal
from prompt_enhancer.eval_run import canonical, config_hash
from prompt_enhancer.eval_set import Record, check_record, read_records, survey_set, variants_for
from prompt_enhancer.storage.tables import Catalog

PROPERTY = settings(deadline=None, max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture])
CATALOG = Catalog(ROOT)
KINDS = ['single_concept', 'edit_local', 'rag_off']
MARKER = 'PRIVATE-MARKER'
IMAGE = 'sha256:' + 'a' * 64
TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)), max_size=60)
SCALAR = st.none() | st.booleans() | st.integers(min_value=-10**6, max_value=10**6) | st.floats(allow_nan=False, allow_infinity=False) | TEXT
VALUE = st.recursive(SCALAR, lambda inner: st.lists(inner, max_size=3) | st.dictionaries(TEXT, inner, max_size=3), max_leaves=6)


def table_bytes(rows):
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(list(rows), schema=SCHEMA), buffer, row_group_size=2)
    return buffer.getvalue()


def pieces(row):
    scenario = {name: row[name] for name in CATALOG.schema('eval_scenario.v0')['properties']}
    return Record(scenario, {name: row[name] for name in row if name.startswith('draft_')})


def valid_rows():
    return [record('gen', f'{MARKER} A Name "OPEN" sign', literal='"OPEN"'),
            record('edit', f'{MARKER} Change the Name', task='edit', kind='edit_local', image=IMAGE),
            record('off', f'{MARKER} A plain bowl', kind='rag_off', rag=False, linked=False)]


def test_the_valid_rows_are_accepted():  # the base of the damaged-record property below
    for index, row in enumerate(valid_rows()):
        check_record(pieces(row), index, CATALOG, KINDS)


def two_constraints(row):
    row['expected_constraints']['constraints'].append(dict(row['expected_constraints']['constraints'][0]))
    return row


def damaged(change):
    def apply(row):
        change(row)
        return row
    return apply


REFUSED_RECORDS = {  # name: (valid row index, change, words the refusal must carry)
    'a repeated constraint id': (0, two_constraints, 'a constraint id is repeated'),
    'a span outside the prompt': (0, damaged(lambda row: row.update(prompt='no sign here')), 'verbatim part'),
    'a mention outside the prompt': (0, damaged(lambda row: row['expected_concept_links'][0].update(mention='Elsewhere')),
                                     'draft_link_details'),
    'an edit without a source image': (1, damaged(lambda row: row.update(source_image=None)), 'source_image'),
    'a generate with a source image': (0, damaged(lambda row: row.update(source_image={'image_id': IMAGE, 'folder_id': 'f'})),
                                       'source_image'),
    'an edit without an edit key': (1, damaged(lambda row: row.update(draft_edit_key=None)), 'draft_edit_key'),
    'an edit key that is not JSON': (1, damaged(lambda row: row.update(draft_edit_key='{not json')), 'not JSON'),
    'an edit key without its lists': (1, damaged(lambda row: row.update(draft_edit_key='{"targets": []}')), 'targets, delta'),
    'a kind the profile does not list': (0, damaged(lambda row: row.update(kind='unlisted')), 'evaluation.scenario_kinds'),
    'link details that do not follow the links': (0, damaged(lambda row: row.update(draft_link_details=[])),
                                                  'one entry per expected concept link'),
    'a source image that is not an image id': (1, damaged(lambda row: row.update(
        source_image={'image_id': 'not-an-id', 'folder_id': 'f'})), 'not an image id'),
}


def test_every_rule_of_the_record_check_refuses_with_its_own_words_and_never_echoes_the_prompt():
    for name, (which, change, words) in REFUSED_RECORDS.items():
        row = change(deepcopy(valid_rows()[which]))
        try:
            check_record(pieces_of(row), which, CATALOG, KINDS)
        except Refusal as refusal:
            assert words in str(refusal) and MARKER not in str(refusal), name
        else:
            raise AssertionError(f'{name}: accepted')


@PROPERTY
@given(which=st.integers(0, 2), key=st.sampled_from(sorted(record('x', 'p')) + ['extra']), value=VALUE, drop=st.booleans())
def test_a_damaged_record_is_refused_precisely_and_never_echoed(which, key, value, drop):
    row = deepcopy(valid_rows()[which])
    if drop:
        row.pop(key, None)
    else:
        row[key] = value
    try:
        check_record(pieces_of(row), which, CATALOG, KINDS)
    except Refusal as refusal:
        message = str(refusal)
        assert message.startswith('--set: record') and MARKER not in message and len(message) < 300


def pieces_of(row):
    """A record from a damaged row; a row missing a field the adapter would never see (the table fixes the columns) is
    completed with None, which the validators then refuse."""
    scenario = {name: row.get(name) for name in CATALOG.schema('eval_scenario.v0')['properties']}
    return Record(scenario, {name: row.get(name) for name in ('draft_link_details', 'draft_edit_key', 'draft_rag_enabled',
                                                              'draft_language', 'draft_format')})


@PROPERTY
@given(document=VALUE)
def test_an_arbitrary_score_document_is_judged_by_the_schema_without_any_other_exception(document):
    problem = CATALOG.document_error('eval_judge_scores.v0', document)
    assert problem is None or isinstance(problem, str)
    if problem is None:  # accepted: exactly the closed shape
        assert set(document) == {'schema', 'pack_id', 'scorer', 'scores'} and set(document['scorer']) == {'id', 'family', 'model'}
        assert all(set(item) == {'scenario_id', 'ratings'} for item in document['scores'])


@PROPERTY
@given(rows=st.lists(st.tuples(st.sampled_from(['fit', 'test']), st.booleans(), st.booleans()), min_size=1, max_size=8),
       mode=st.sampled_from(['record', 'both']), split=st.sampled_from(['fit', 'test', 'all']))
def test_the_survey_counts_add_up_for_any_mix_of_splits_retrieval_keys_and_tasks(rows, mode, split):
    records = [record(f's{number}', f'Prompt {number}', split=part, rag=rag, linked=False,
                      **({'task': 'edit', 'kind': 'edit_local', 'image': IMAGE} if edit else {}))
               for number, (part, rag, edit) in enumerate(rows)]
    survey = survey_set(table_bytes(records), CATALOG, kinds=KINDS, mode=mode, split=split)
    chosen = [(rag, edit) for part, rag, edit in rows if split in ('all', part)]
    assert survey.records == len(rows)
    assert survey.units == sum(len(variants_for(Record({}, {'draft_rag_enabled': rag}), mode)) for _, rag, _ in rows)
    assert survey.units == sum(survey.selected.values()) + survey.skipped
    assert survey.selected['on'] == sum(1 for rag, _ in chosen if rag)
    assert survey.selected['off'] == sum(1 for rag, _ in chosen if not rag or mode == 'both')
    assert (IMAGE in survey.image_ids) == any(rag and edit for rag, edit in chosen)
    assert (IMAGE in survey.off_image_ids) == any(edit and (not rag or mode == 'both') for rag, edit in chosen)
    assert survey.any_on == (survey.selected['on'] > 0)


@PROPERTY
@given(prompt=TEXT, language=TEXT, fmt=TEXT, scenario_id=TEXT.filter(bool))
def test_the_adapter_keeps_every_text_field_verbatim(prompt, language, fmt, scenario_id):
    """R-001 and the multilingual rule: any Unicode text survives the Parquet adapter byte for byte."""
    row = record(scenario_id, prompt, linked=False, language=language, fmt=fmt)
    (read,) = list(read_records(table_bytes([row]), CATALOG))
    assert read.scenario['prompt'] == prompt and read.scenario['scenario_id'] == scenario_id
    assert read.draft['draft_language'] == language and read.draft['draft_format'] == fmt
    assert {**read.scenario, **read.draft} == row


@PROPERTY
@given(extra=st.dictionaries(st.sampled_from(['evaluation', 'rag', 'vlm', 'runtime']), VALUE, max_size=2))
def test_the_run_identity_ignores_the_evaluation_block_and_nothing_else(extra):
    base = {'rag': {'enabled': True}, 'vlm': {'roles': {}}, 'evaluation': {'judge': {'blinding_seed': 1}}}
    other = {**deepcopy(base), **deepcopy(extra)}
    same_outside = {key: value for key, value in other.items() if key != 'evaluation'} == {
        key: value for key, value in base.items() if key != 'evaluation'}
    assert (config_hash(base) == config_hash(other)) is same_outside


@PROPERTY
@given(value=VALUE)
def test_the_canonical_form_is_independent_of_key_order(value):
    def reorder(item):
        if isinstance(item, dict):
            return {key: reorder(item[key]) for key in sorted(item, reverse=True)}
        return [reorder(part) for part in item] if isinstance(item, list) else item
    assert canonical(value) == canonical(reorder(value))
