"""The judge pack of `eval run` and the score import of `eval import` (T-016 P4, decision 154).

The pack is what two subscription agents of different families score outside the product runtime: per scenario the
prompt, the answer key, the rubric dimensions that apply and the outputs under blind labels. It says nothing else about an
output (no status, warning, retrieval state, attachment, trace id or variant), so a scorer cannot tell which output used
retrieval; the unblinding key stays in the run directory. `eval import` validates every score file against a closed schema
and against the pack, unblinds, records both scorers, flags every disagreement and labels every value `agent_judged`:
pilot evidence only. Imported scores are written beside the run, never into `eval_results`, so no threshold, calibration or
`eval fit` reader can meet them (R-020).
"""
import hashlib
import json
import re

from .cli_input import Refusal
from .config import validate_evaluation_settings
from .eval_run import canonical, hold_run, refresh_summary, scan_results, Run
from .eval_set import digest_tag, read_limited, read_records, selected, variants_for
from .model_json import strict_json
from .storage import StorageError, open_data_store

PACK_SCHEMA = 'eval_judge_pack.v0'  # artifact-bound
SCORES_SCHEMA = 'eval_judge_scores.v0'  # artifact-bound
LABELS = 'AB'  # protocol-bound: blind labels, at most one per retrieval variant
EVIDENCE = 'agent_judged'  # protocol-bound: the label every imported score carries (decision 154)
RUN_ID = re.compile(r'[0-9a-f]{16}')


def swapped(seed, scenario_id):
    """Whether a two-output scenario shows its outputs in reverse variant order: one bit of a hash of the seed and the
    scenario id, so the order is repeatable, differs between seeds and scenarios, and is a bijection on the outputs."""
    return hashlib.sha256(f'{seed}:{scenario_id}'.encode('utf-8')).digest()[0] & 1 == 1


def dimensions_of(scenario, draft):
    """The rubric dimensions that apply, from the structure of the answer key only (types and statuses)."""
    constraints, links = scenario['expected_constraints']['constraints'], scenario['expected_concept_links']
    result = [('intent_preserved', None), *(('constraint_survival', item['id']) for item in constraints)]
    if links:
        result.append(('link_alignment', None))
    if any(link['status'] == 'family' for link in links):
        result.append(('family_conflict', None))
    if any(item['negated_by_user'] for item in draft['draft_link_details']) or any(item['type'] == 'negation' for item in constraints):
        result.append(('negation', None))
    if scenario['task'] == 'edit':
        result += [('leakage', None), ('under_editing', None)]
    return [{'dimension': name, 'ref': ref} for name, ref in result]


def _line(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _result_row(run, keys, variant, scenario_id):
    name = run.run_id(variant)
    for row in run.store.read_segment('eval_results', keys[(name, scenario_id)]):
        if (row['run_id'], row['scenario_id']) == (name, scenario_id):
            return row
    raise StorageError('eval_results: a result is not in its segment')


def iter_items(run, keys):
    """Each pack item with its key entries, in file order, one scenario at a time."""
    seed = run.settings['judge.blinding_seed']
    for record in read_records(run.data, run.catalog):
        if not selected(record, run.split):
            continue
        scenario, draft = record.scenario, record.draft
        found = []
        for variant in variants_for(record, run.mode):
            envelope = _result_row(run, keys, variant, scenario['scenario_id'])['metrics']['envelope']
            found.append((variant, envelope['output'], envelope['trace_id']))
        if len(found) == 2 and swapped(seed, scenario['scenario_id']):
            found.reverse()
        item = {'schema': 'eval_judge_pack.item.v0', 'scenario_id': scenario['scenario_id'], 'kind': scenario['kind'],
                'task': scenario['task'], 'prompt': scenario['prompt'], 'source_image': scenario['source_image'],
                'key': {'expected_concept_links': scenario['expected_concept_links'],
                        'expected_constraints': scenario['expected_constraints'],
                        'draft_edit_key': draft['draft_edit_key'], 'draft_link_details': draft['draft_link_details']},
                'dimensions': dimensions_of(scenario, draft),
                'outputs': [{'label': LABELS[index], 'output': output} for index, (_, output, _) in enumerate(found)]}
        problem = run.catalog.document_error(PACK_SCHEMA, item, '/$defs/item')
        if problem is not None:
            raise StorageError(f'judge pack: an item is invalid ({problem})')
        yield item, [{'scenario_id': scenario['scenario_id'], 'label': LABELS[index], 'variant': variant, 'trace_id': trace}
                     for index, (variant, _, trace) in enumerate(found)]


def write_pack(run, keys):
    """Write the blinded pack and its key under `packs/<pack id>/` (the run is complete). Returns what was written."""
    settings = run.settings
    rubric = {'prompt_id': settings['judge.rubric_prompt_id'], 'sha256': digest_tag(run.rubric),
              'text': run.rubric.decode('utf-8')}
    scale = {'min': settings['judge.score_min'], 'max': settings['judge.score_max']}
    cap = run.cap  # evaluation.run.max_sidecar_bytes: nothing is written that `eval import` could not read back
    hashes, entries, size = [], [], 0
    for item, keyed in iter_items(run, keys):
        line = _line(item)
        hashes.append(hashlib.sha256(line).hexdigest())
        size += len(line) + 1
        entries += keyed
    pack_id = digest_tag(canonical({'run_id': run.rid, 'rubric': rubric['sha256'], 'scale': scale, 'items': hashes}))
    header = {'schema': PACK_SCHEMA, 'pack_id': pack_id, 'run_id': run.rid, 'rubric': rubric, 'scale': scale,
              'items': len(hashes)}
    directory = f'packs/{pack_id[7:23]}'
    key = {'schema': 'eval_judge_key.v0', 'pack_id': pack_id, 'run_id': run.rid,
           'blinding_seed': settings['judge.blinding_seed'], 'entries': entries}
    key_bytes = _line(key)
    if size + len(_line(header)) + 1 > cap or len(key_bytes) > cap:  # before the first byte is written
        raise StorageError('judge pack: the pack or its key is larger than evaluation.run.max_sidecar_bytes; '
                           'nothing was written')

    def produce(handle):
        handle.write(_line(header) + b'\n')
        for index, (item, _) in enumerate(iter_items(run, keys)):
            line = _line(item)
            if hashlib.sha256(line).hexdigest() != hashes[index]:
                raise StorageError('judge pack: the results changed while the pack was written')
            handle.write(line + b'\n')

    run.store.write_sidecar(run.relative(f'{directory}/key.json'), key_bytes)  # the key first: a pack without a key is unusable
    run.store.write_sidecar_stream(run.relative(f'{directory}/pack.jsonl'), produce)
    return {'pack_id': pack_id, 'items': len(hashes), 'directory': f'{run.relative(directory)}'}


# ----------------------------------------------------------------------------- import


def _score_file(root, value, settings, catalog):
    data = read_limited(root, value, settings['judge.max_score_file_bytes'], '--scores',
                        'evaluation.judge.max_score_file_bytes')
    try:
        document = strict_json(data.decode('utf-8'))
    except UnicodeDecodeError:
        raise Refusal('--scores: not UTF-8') from None
    except ValueError:
        raise Refusal('--scores: not strict JSON') from None
    problem = catalog.document_error(SCORES_SCHEMA, document)
    if problem is not None:
        raise Refusal(f'--scores: {problem}: invalid against {SCORES_SCHEMA}')
    return document, digest_tag(data)


def _load_pack(store, rid, pack_id, cap):
    """The pack's dimensions per scenario and its key, from the run directory; Refusal when either is missing or differs.
    The pack is read line by line, as it was written, so its size is bounded by `cap` (`evaluation.run.max_sidecar_bytes`)
    and by nothing else; only the small per-scenario answer sets are kept."""
    directory = f'eval/runs/{rid}/packs/{pack_id[7:23]}'
    try:
        key = strict_json(store.read_sidecar(f'{directory}/key.json', cap).decode('utf-8'))
    except FileNotFoundError:
        raise Refusal('--scores: no pack of this id in the run') from None
    except ValueError:  # includes UnicodeDecodeError
        raise Refusal('the pack or its key is damaged or belongs to another run') from None
    lines = store.iter_sidecar_lines(f'{directory}/pack.jsonl', cap)
    try:
        if (store.catalog.document_error(PACK_SCHEMA, key, '/$defs/key') is not None or key['pack_id'] != pack_id
                or key['run_id'] != rid):
            raise ValueError
        header = strict_json(next(lines).decode('utf-8'))
        if (store.catalog.document_error(PACK_SCHEMA, header, '/$defs/header') is not None
                or header['pack_id'] != pack_id or header['run_id'] != rid):
            raise ValueError
        expected = {}
        for line in lines:
            if line:
                item = strict_json(line.decode('utf-8'))
                if store.catalog.document_error(PACK_SCHEMA, item, '/$defs/item') is not None:
                    raise ValueError
                expected[item['scenario_id']] = {(output['label'], entry['dimension'], entry['ref'])
                                                 for output in item['outputs'] for entry in item['dimensions']}
        if len(expected) != header['items']:
            raise ValueError
    except FileNotFoundError:
        raise Refusal('--scores: no pack of this id in the run') from None
    except (ValueError, StopIteration):  # includes UnicodeDecodeError; an empty file has no header
        raise Refusal('the pack or its key is damaged or belongs to another run') from None
    finally:
        lines.close()
    return header, key, expected


def import_scores(config, root, args):
    """Validate every score file before one byte is written; then unblind, record, flag disagreements. Returns the report."""
    settings = validate_evaluation_settings(config, 'eval import', project_root=root)
    store = open_data_store(config, 'eval import', project_root=root)
    rid = args.run_id
    if RUN_ID.fullmatch(rid) is None:
        raise Refusal('--run-id: not a run id')
    lock = hold_run(store, rid, create=False)  # one writer of a run at a time: refused at once while an eval run holds it
    try:
        return _import_locked(settings, store, root, args, rid)
    finally:
        lock.release()


def _import_locked(settings, store, root, args, rid):
    if len(args.scores) < settings['judge.min_scorers']:
        raise Refusal(f'--scores: at least {settings["judge.min_scorers"]} score files are required '
                      '(evaluation.judge.min_scorers)')
    documents = [_score_file(root, value, settings, store.catalog) for value in args.scores]
    pack_ids = {document['pack_id'] for document, _ in documents}
    if len(pack_ids) != 1:
        raise Refusal('--scores: the files do not score one pack')
    (pack_id,) = pack_ids
    scorers = [document['scorer'] for document, _ in documents]
    if len({item['id'] for item in scorers}) != len(scorers):
        raise Refusal('--scores: two files name the same scorer')
    if len({item['family'] for item in scorers}) != len(scorers):
        raise Refusal('--scores: the scorers must be of different families')
    if any(item['family'] in settings['judge.under_test_families'] for item in scorers):
        raise Refusal('--scores: a scorer is of a family under test (evaluation.judge.under_test_families)')
    header, key, expected = _load_pack(store, rid, pack_id, settings['run.max_sidecar_bytes'])
    low, high = settings['judge.score_min'], settings['judge.score_max']
    if header['scale'] != {'min': low, 'max': high}:
        raise Refusal('the pack was written for another scale than evaluation.judge.score_min and score_max')
    where = {(entry['scenario_id'], entry['label']): entry for entry in key['entries']}
    ratings = []  # per scorer: {(scenario_id, variant, dimension, ref): value}
    for document, _ in documents:
        seen_ids = [item['scenario_id'] for item in document['scores']]
        if len(set(seen_ids)) != len(seen_ids):
            raise Refusal('--scores: a scenario is scored twice')
        if set(seen_ids) != set(expected):
            raise Refusal('--scores: the scenarios are not exactly those of the pack (unknown or missing)')
        table = {}
        for item in document['scores']:
            given = [(rating['label'], rating['dimension'], rating['ref']) for rating in item['ratings']]
            if len(set(given)) != len(given) or set(given) != expected[item['scenario_id']]:
                raise Refusal('--scores: the ratings are not exactly the pack\'s dimensions, once each')
            for rating in item['ratings']:
                if type(rating['value']) is not int or not low <= rating['value'] <= high:
                    raise Refusal('--scores: a rating is outside the scale')
                entry = where[(item['scenario_id'], rating['label'])]
                table[(item['scenario_id'], entry['variant'], rating['dimension'], rating['ref'])] = rating['value']
        ratings.append(table)
    names = [item['id'] for item in scorers]
    disagreements = [{'scenario_id': cell[0], 'variant': cell[1], 'dimension': cell[2], 'ref': cell[3],
                      'values': {name: table[cell] for name, table in zip(names, ratings)}}
                     for cell in sorted(ratings[0], key=lambda cell: (cell[0], cell[1], cell[2], cell[3] or ''))
                     if len({table[cell] for table in ratings}) > 1]
    aggregates = {}
    for name, table in zip(names, ratings):
        for (_, variant, dimension, _), value in table.items():
            cell = aggregates.setdefault(name, {}).setdefault(variant, {}).setdefault(dimension, {'n': 0, 'sum': 0})
            cell['n'] += 1
            cell['sum'] += value
    import_id = digest_tag(canonical({'pack_id': pack_id, 'files': sorted(digest for _, digest in documents)}))
    summary = {'import_id': import_id, 'pack_id': pack_id, 'evidence': EVIDENCE, 'pilot_only': True,
               'scorers': scorers, 'disagreements': len(disagreements), 'needs_user_review': bool(disagreements),
               'aggregates': aggregates}
    record = {'schema': 'eval_judge_import.v0', 'run_id': rid, 'summary': summary, 'disagreement_list': disagreements,
              'ratings': [{'scorer': name, 'evidence': EVIDENCE,
                           'values': [{'scenario_id': cell[0], 'variant': cell[1], 'dimension': cell[2], 'ref': cell[3],
                                       'value': value} for cell, value in sorted(
                                           table.items(), key=lambda pair: (pair[0][0], pair[0][1], pair[0][2], pair[0][3] or ''))]}
                          for name, table in zip(names, ratings)]}
    data = _line(record)
    if len(data) > settings['run.max_sidecar_bytes']:
        raise StorageError('the import record is larger than evaluation.run.max_sidecar_bytes; nothing was written')
    run = _summary_run(settings, store, rid)
    keys, stats = scan_results(run)  # a repeated or malformed result is refused here, before the import is written
    store.write_sidecar(f'eval/runs/{rid}/imports/{import_id[7:23]}.json', data)
    refresh_summary(run, keys, stats)
    return {'command': 'eval import', 'run_id': rid, **summary, 'disagreement_list': disagreements}


def _summary_run(settings, store, rid):
    """The part of a run the summary needs, rebuilt from its manifest (the set itself is not needed)."""
    from types import SimpleNamespace
    manifest = strict_json(store.read_sidecar(f'eval/runs/{rid}/manifest.json',
                                              settings['run.max_sidecar_bytes']).decode('utf-8'))
    selected_units = manifest['units']['selected']
    survey = SimpleNamespace(selected=selected_units, units=manifest['units']['total'], skipped=manifest['units']['skipped'])
    run = Run.__new__(Run)
    run.store, run.rid, run.survey, run.settings = store, rid, survey, settings
    return run
