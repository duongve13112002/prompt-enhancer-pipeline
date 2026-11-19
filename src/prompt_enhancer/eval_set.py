"""The scenario set `eval run` reads: an explicitly named Parquet file through an explicit, validated adapter (T-016 P4).

The T-003 draft keeps what `eval_scenario.v0` has no field for in five `draft_*` columns. The adapter keeps every one of
them and every string byte for byte (no strip, no normalisation, no re-encoding: R-001). It validates each record
against `eval_scenario.v0`, each `draft_*` field by its type, and the set as a whole, before any model call. Records are
decoded one batch at a time; only ids and counts outlive a record, never the set (R-017). Nothing here reads meaning
from text: every check is a type, a closed structure, an id lookup or an exact string comparison (R-015).
"""
from dataclasses import dataclass, field
import hashlib
import io
import re

from jsonschema import Draft202012Validator
import pyarrow.parquet as pq
import pyarrow as pa

from .cli_input import Refusal, resolve_input
from .k0_scan import LinkRefused, ScanError, SourceTooLarge, read_source_file
from .model_json import strict_json
from .storage import StorageError

DRAFT_FIELDS = ('draft_link_details', 'draft_edit_key', 'draft_rag_enabled', 'draft_language', 'draft_format')  # artifact-bound: the T-003 draft layout
SCENARIO_SCHEMA = 'eval_scenario.v0'  # artifact-bound
VARIANTS = ('on', 'off')  # protocol-bound: retrieval on and off
SPLITS = ('fit', 'test')  # artifact-bound: eval_scenario.v0 `split`
BATCH_ROWS = 16  # safety-bound: records decoded at a time, so the set is never decoded whole
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')  # artifact-bound: the image id form (contracts/README.md, Identity)

_IDS = {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}
_DRAFT = Draft202012Validator({
    'type': 'object', 'additionalProperties': False, 'required': list(DRAFT_FIELDS),
    'properties': {
        'draft_link_details': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['mention', 'negated_by_user', 'family_member_ids', 'family_conflict_ids', 'note'],
            'properties': {'mention': {'type': 'string', 'minLength': 1}, 'negated_by_user': {'type': 'boolean'},
                           'family_member_ids': _IDS, 'family_conflict_ids': _IDS, 'note': {'type': 'string'}}}},
        'draft_edit_key': {'anyOf': [{'type': 'null'}, {'type': 'string'}]},  # a JSON text, parsed below
        'draft_rag_enabled': {'type': 'boolean'},
        'draft_language': {'type': 'string', 'minLength': 1},
        'draft_format': {'type': 'string', 'minLength': 1}}})
_EDIT_KEY = Draft202012Validator({
    'type': 'object', 'additionalProperties': False, 'required': ['targets', 'delta', 'preserve'],
    'properties': {name: {'type': 'array', 'items': {'type': 'string'}} for name in ('targets', 'delta', 'preserve')}})


@dataclass
class Record:
    scenario: dict  # the `eval_scenario.v0` fields as read
    draft: dict  # the five `draft_*` fields as read


@dataclass
class Survey:
    """What the first pass learned; ids and counts only."""
    records: int = 0
    units: int = 0  # every (record, variant) pair the set holds under the chosen variants
    skipped: int = 0  # units outside the chosen split
    selected: dict = field(default_factory=lambda: {'on': 0, 'off': 0})  # units to run, by variant
    concept_ids: set = field(default_factory=set)  # referenced by a selected record that runs with retrieval on
    image_ids: set = field(default_factory=set)  # source images of selected edit records that run with retrieval on
    off_image_ids: set = field(default_factory=set)  # source images of selected edit records that run with retrieval off

    @property
    def any_on(self):
        return self.selected['on'] > 0


def read_limited(root, value, limit, label, key):
    """One file named on the command line, under the project root, at most `limit` bytes, no link on any component."""
    path = resolve_input(root, value, label)
    try:
        return read_source_file(root, path.relative_to(root).parts, max_bytes=limit)
    except SourceTooLarge:
        raise Refusal(f'{label}: larger than {key} ({limit} bytes)') from None
    except LinkRefused:
        raise Refusal(f'{label}: is a link or another spelling of a stored name') from None
    except ScanError:
        raise Refusal(f'{label}: not a regular file, or it changed while it was read') from None
    except (OSError, ValueError) as error:
        raise Refusal(f'{label}: cannot read the file ({type(error).__name__})') from None


def digest_tag(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def read_records(data, catalog):
    """The records of the set bytes, one at a time, in file order; the columns must be exactly the scenario fields plus
    the five draft fields."""
    scenario_fields = tuple(catalog.schema(SCENARIO_SCHEMA)['properties'])
    try:
        parquet = pq.ParquetFile(io.BytesIO(data))
        names = tuple(parquet.schema_arrow.names)
        if set(names) != {*scenario_fields, *DRAFT_FIELDS} or len(set(names)) != len(names):
            raise Refusal('--set: columns are not the eval_scenario.v0 fields plus the five draft_* fields')
        for batch in parquet.iter_batches(batch_size=BATCH_ROWS):
            for row in batch.to_pylist():
                yield Record({name: row[name] for name in scenario_fields}, {name: row[name] for name in DRAFT_FIELDS})
    except (pa.ArrowException, OSError) as error:
        raise Refusal(f'--set: not a readable Parquet file ({type(error).__name__})') from None


def variants_for(record, mode):
    """The retrieval variants a record runs in: the one its own key states, and with `both` also `off` for a record
    that states `on` (an off record is never run with retrieval on: its key is that there is no link)."""
    own = 'on' if record.draft['draft_rag_enabled'] else 'off'
    return ('on', 'off') if mode == 'both' and own == 'on' else (own,)


def check_record(record, index, catalog, kinds):
    """Refuse one record naming its position and field, never a value (a prompt is private, R-026)."""
    where = f'--set: record {index}'
    scenario, draft = record.scenario, record.draft
    path = catalog.document_error(SCENARIO_SCHEMA, scenario)
    if path is not None:
        raise Refusal(f'{where}: {path}: invalid against {SCENARIO_SCHEMA}')
    error = next(iter(_DRAFT.iter_errors(draft)), None)
    if error is not None:
        raise Refusal(f'{where}: {".".join(str(part) for part in error.absolute_path) or "draft"}: invalid draft field')
    if scenario['kind'] not in kinds:
        raise Refusal(f'{where}: kind: not in evaluation.scenario_kinds')
    edit = scenario['task'] == 'edit'
    if edit != (scenario['source_image'] is not None):
        raise Refusal(f'{where}: source_image: present exactly for an edit scenario')
    if edit and IMAGE_ID.fullmatch(scenario['source_image']['image_id']) is None:
        raise Refusal(f'{where}: source_image.image_id: not an image id')
    if edit != (draft['draft_edit_key'] is not None):
        raise Refusal(f'{where}: draft_edit_key: present exactly for an edit scenario')
    if edit:
        try:
            parsed = strict_json(draft['draft_edit_key'])
        except ValueError:
            raise Refusal(f'{where}: draft_edit_key: not JSON') from None
        if next(iter(_EDIT_KEY.iter_errors(parsed)), None) is not None:
            raise Refusal(f'{where}: draft_edit_key: not targets, delta and preserve lists')
    links = scenario['expected_concept_links']
    if [item['mention'] for item in links] != [item['mention'] for item in draft['draft_link_details']]:
        raise Refusal(f'{where}: draft_link_details: not one entry per expected concept link, in order')
    ids = [item['id'] for item in scenario['expected_constraints']['constraints']]
    if len(set(ids)) != len(ids):
        raise Refusal(f'{where}: expected_constraints: a constraint id is repeated')
    prompt = scenario['prompt']
    spans = [item['mention'] for item in links] + [item['span'] for item in scenario['expected_constraints']['constraints']]
    if any(span not in prompt for span in spans):
        raise Refusal(f'{where}: a mention or constraint span is not a verbatim part of the prompt')


def survey_set(data, catalog, *, kinds, mode, split):
    """The first pass: every record valid, the set consistent, and the ids the run will need. Raises Refusal."""
    result, seen, split_of = Survey(), set(), {}
    for index, record in enumerate(read_records(data, catalog)):
        check_record(record, index, catalog, kinds)
        scenario = record.scenario
        if scenario['scenario_id'] in seen:
            raise Refusal(f'--set: record {index}: scenario_id: repeated')
        seen.add(scenario['scenario_id'])
        for link in scenario['expected_concept_links']:
            if link['concept_id'] is not None and split_of.setdefault(link['concept_id'], scenario['split']) != scenario['split']:
                raise Refusal(f'--set: record {index}: split: a concept is in both splits (evaluation.split_by)')
        variants = variants_for(record, mode)
        result.records += 1
        result.units += len(variants)
        if split != 'all' and scenario['split'] != split:
            result.skipped += len(variants)
            continue
        for variant in variants:
            result.selected[variant] += 1
        if 'on' in variants:
            for link, detail in zip(scenario['expected_concept_links'], record.draft['draft_link_details']):
                result.concept_ids.update(item for item in (link['concept_id'], *detail['family_member_ids'],
                                                              *detail['family_conflict_ids']) if item is not None)
            if scenario['source_image'] is not None:
                result.image_ids.add(scenario['source_image']['image_id'])
        if 'off' in variants and scenario['source_image'] is not None:
            result.off_image_ids.add(scenario['source_image']['image_id'])
    return result


def selected(record, split):
    return split == 'all' or record.scenario['split'] == split


def check_snapshot_ids(survey, pinned):
    """Every concept and image id the on-variant records name exists in the named snapshot, and every source image
    has a retained original there (KBS-F3). Raises Refusal."""
    concepts = {row['concept_id'] for row in pinned.read_rows('concepts')}
    if survey.concept_ids - concepts:
        raise Refusal(f'--set: {len(survey.concept_ids - concepts)} concept id(s) are not in the named snapshot')
    images = {row['image_id'] for row in pinned.read_rows('images')}
    if survey.image_ids - images:
        raise Refusal(f'--set: {len(survey.image_ids - images)} source image id(s) are not in the named snapshot')
    for image_id in sorted(survey.image_ids):
        try:
            pinned.retained_image_entry(image_id)
        except StorageError:
            raise Refusal('--set: a source image has no retained original in the named snapshot') from None


def source_file(root, directory, image_id, limit, key):
    """The bytes of a source image for an edit run with retrieval off: file `<directory>/<digest>`, whose SHA-256 must
    be the image id (so the content is the scenario's, whatever the directory holds). Raises Refusal."""
    data = read_limited(root, f'{directory}/{image_id[7:]}', limit, '--source-images', key)
    if digest_tag(data) != image_id:
        raise Refusal('--source-images: a file does not hash to its image id')
    return data
