"""The blinded judge pack and `eval import` (T-016 P4, decision 154; R-001, R-020, R-026).

Labelled substitutes: synthetic result rows (`eval_fixtures.synthetic_run`: valid envelopes written through the production
row builder and data store, no model, no snapshot) and hand-written score files. Evidence class: `mock_contract`."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from eval_fixtures import SEED, pack_lines, record, result_row, score_document, spy_on_locks, synthetic_run
from prompt_enhancer.cli_input import Refusal
from prompt_enhancer.eval_judge import import_scores, swapped, write_pack
from prompt_enhancer.eval_run import hold_run, scan_results
from prompt_enhancer.eval_set import read_records
from prompt_enhancer.storage import StorageError

IMAGE = 'sha256:' + 'a' * 64
FAMILIES = ({'id': 'agent-one', 'family': 'family-a', 'model': 'model-a'},
            {'id': 'agent-two', 'family': 'family-b', 'model': 'model-b'})


def records():
    return [record('gen-1', 'A Name "OPEN" sign', literal='"OPEN"', split='fit'),
            record('edit-1', 'Change the Name', task='edit', kind='edit_local', image=IMAGE, split='fit'),
            record('off-1', 'A plain bowl', kind='rag_off', rag=False, linked=False)]


def pack_of(run):
    keys, _ = scan_results(run)
    return write_pack(run, keys)


def key_of(run, pack):
    data = run.store.read_sidecar(f'{pack["directory"]}/key.json', 1 << 20)
    return {(entry['scenario_id'], entry['label']): entry['variant'] for entry in json.loads(data)['entries']}


def scores_path(tmp_path, name, document):
    path = tmp_path / f'{name}.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def tree(run):
    """Every file under the data root with its bytes, but for the run lock (empty, and unreadable while it is held)."""
    return {str(path.relative_to(run.store.root)): path.read_bytes() for path in run.store.root.rglob('*')
            if path.is_file() and path.name != 'run.lock'}


def import_args(run, *paths):
    return SimpleNamespace(run_id=run.rid, scores=[str(path) for path in paths])


def pair(tmp_path, run, pack, disagree=None, *, second=None):
    header, *items = pack_lines(run, pack['pack_id'])
    first = score_document(header, items, FAMILIES[0])
    other = score_document(header, items, second or FAMILIES[1], disagree or (lambda *cell: 3))
    return header, items, first, other, [scores_path(tmp_path, 'first', first), scores_path(tmp_path, 'second', other)]


@pytest.fixture
def made(tmp_path, project_root, model_test_profile):
    run, config = synthetic_run(tmp_path, project_root, model_test_profile, records())
    return run, config, pack_of(run)


def test_the_pack_shows_outputs_under_blind_labels_and_nothing_about_how_they_were_made(made):
    run, _, pack = made
    header, *items = pack_lines(run, pack['pack_id'])
    assert pack['items'] == 3 and header['items'] == 3 and header['scale'] == {'min': 1, 'max': 5}
    assert [item['scenario_id'] for item in items] == ['gen-1', 'edit-1', 'off-1']  # file order of the set
    variants = key_of(run, pack)
    for item in items:
        # a pack item states the prompt, the answer key, the applicable dimensions and the outputs: nothing else
        assert set(item) == {'schema', 'scenario_id', 'kind', 'task', 'prompt', 'source_image', 'key', 'dimensions', 'outputs'}
        assert all(set(output) == {'label', 'output'} for output in item['outputs'])
        for output in item['outputs']:  # the label maps to the output of the variant the key says
            assert output['output'] == f'{variants[(item["scenario_id"], output["label"])]} output of {item["scenario_id"]}'
    assert [len(item['outputs']) for item in items] == [2, 2, 1]  # a rag-off scenario has one output
    text = (run.store.root / 'eval' / 'runs' / run.rid / 'packs' / pack['pack_id'][7:23] / 'pack.jsonl').read_text('utf-8')
    for leaked in ('trace-', 'warnings', 'retrieval', 'attachments', '"variant"', '"envelope'):  # `status` is a link status of the key
        assert leaked not in text


def test_the_dimensions_follow_the_structure_of_the_answer_key(made):
    run, _, pack = made
    _, *items = pack_lines(run, pack['pack_id'])
    by_id = {item['scenario_id']: [(entry['dimension'], entry['ref']) for entry in item['dimensions']] for item in items}
    assert by_id['gen-1'] == [('intent_preserved', None), ('constraint_survival', 'c1'), ('link_alignment', None)]
    assert by_id['edit-1'] == [('intent_preserved', None), ('link_alignment', None), ('leakage', None),
                               ('under_editing', None)]
    assert by_id['off-1'] == [('intent_preserved', None)]


def test_the_same_seed_writes_the_same_pack_and_another_seed_a_pack_with_another_id_and_no_new_result(made):
    run, config, pack = made
    assert pack_of(run) == pack  # repeatable
    rows = run.store.read_rows('eval_results')
    other = next(seed for seed in range(SEED + 1, SEED + 200)
                 if any(swapped(seed, name) != swapped(SEED, name) for name in ('gen-1', 'edit-1')))
    run.settings = {**run.settings, 'judge.blinding_seed': other}
    again = pack_of(run)
    assert again['pack_id'] != pack['pack_id'] and again['directory'] != pack['directory']
    assert run.store.read_rows('eval_results') == rows  # a new seed never reruns a model call and never adds a result
    assert key_of(run, again) != key_of(run, pack)
    for name in ('gen-1', 'edit-1'):  # the order differs exactly where the seed's bit differs
        flipped = swapped(other, name) != swapped(SEED, name)
        assert (key_of(run, again)[(name, 'A')] != key_of(run, pack)[(name, 'A')]) is flipped


def test_the_swap_is_one_bit_of_the_seed_and_scenario_and_covers_both_orders():
    assert swapped(SEED, 'x') == swapped(SEED, 'x')
    assert {swapped(SEED, f'scenario-{number}') for number in range(40)} == {True, False}
    assert {swapped(seed, 'x') for seed in range(40)} == {True, False}


def test_an_import_unblinds_flags_every_disagreement_and_never_touches_the_results(made, tmp_path, project_root):
    run, config, pack = made
    variants = key_of(run, pack)
    rows = run.store.read_rows('eval_results')

    def disagree(scenario_id, label, dimension, ref):
        return 4 if (scenario_id, variants[(scenario_id, label)], dimension) == ('gen-1', 'on', 'intent_preserved') else 3

    _, _, _, _, paths = pair(tmp_path, run, pack, disagree)
    report = import_scores(config, project_root, import_args(run, *paths))
    assert report['evidence'] == 'agent_judged' and report['pilot_only'] is True
    assert report['disagreements'] == 1 and report['needs_user_review'] is True
    assert report['disagreement_list'] == [{'scenario_id': 'gen-1', 'variant': 'on', 'dimension': 'intent_preserved',
                                            'ref': None, 'values': {'agent-one': 3, 'agent-two': 4}}]
    assert [item['id'] for item in report['scorers']] == ['agent-one', 'agent-two']
    record_path = f'eval/runs/{run.rid}/imports/{report["import_id"][7:23]}.json'
    stored = json.loads(run.store.read_sidecar(record_path, 1 << 20))
    assert {item['evidence'] for item in stored['ratings']} == {'agent_judged'}
    assert run.store.read_rows('eval_results') == rows  # imported scores are never results
    summary = json.loads(run.store.read_sidecar(run.relative('summary.json'), 1 << 20))
    assert summary['semantic']['state'] == 'agent_judged' and summary['semantic']['pilot_only'] is True
    assert summary['intervals']['state'] == 'not_run'  # an import approves no interval method
    again = import_scores(config, project_root, import_args(run, *paths))  # the same files again: the same import
    assert again['import_id'] == report['import_id'] and len(run.store.list_sidecars(run.relative('imports'))) == 1


def test_the_lowest_and_the_highest_value_of_the_scale_are_accepted(made, tmp_path, project_root):
    run, config, pack = made
    header, items = pack_lines(run, pack['pack_id'])[0], pack_lines(run, pack['pack_id'])[1:]
    low, high = (score_document(header, items, scorer, lambda *cell, value=value: value)
                 for scorer, value in zip(FAMILIES, (1, 5)))  # the scale of the test profile is 1 to 5
    paths = [scores_path(tmp_path, 'low', low), scores_path(tmp_path, 'high', high)]
    report = import_scores(config, project_root, import_args(run, *paths))
    assert report['disagreements'] == len(report['disagreement_list']) > 0
    assert {value for item in report['disagreement_list'] for value in item['values'].values()} == {1, 5}


def test_two_agreeing_scorers_flag_nothing(made, tmp_path, project_root):
    run, config, pack = made
    *_, paths = pair(tmp_path, run, pack)
    report = import_scores(config, project_root, import_args(run, *paths))
    assert report['disagreements'] == 0 and report['needs_user_review'] is False and report['disagreement_list'] == []


def broken(kind, first, other, header):
    """One damaged pair of score files per refusal case (a copy; the originals stay valid)."""
    first, other = deepcopy(first), deepcopy(other)
    if kind == 'unknown scenario':
        other['scores'][0]['scenario_id'] = 'not-in-the-pack'
    elif kind == 'missing scenario':
        other['scores'].pop()
    elif kind == 'duplicate scenario':
        other['scores'].append(deepcopy(other['scores'][0]))
    elif kind == 'missing rating':
        other['scores'][0]['ratings'].pop()
    elif kind == 'duplicate rating':
        other['scores'][0]['ratings'].append(deepcopy(other['scores'][0]['ratings'][0]))
    elif kind == 'above the scale':
        other['scores'][0]['ratings'][0]['value'] = 6
    elif kind == 'below the scale':
        other['scores'][0]['ratings'][0]['value'] = 0
    elif kind == 'boolean value':
        other['scores'][0]['ratings'][0]['value'] = True
    elif kind == 'float value':
        other['scores'][0]['ratings'][0]['value'] = 3.0
    elif kind == 'extra field':
        other['scores'][0]['ratings'][0]['comment'] = 'free text'
    elif kind == 'extra top-level field':
        other['notes'] = 'free text'
    elif kind == 'unknown dimension':
        other['scores'][0]['ratings'][0]['dimension'] = 'charm'
    elif kind == 'unknown label':
        other['scores'][0]['ratings'][0]['label'] = 'C'
    elif kind == 'another pack':
        other['pack_id'] = 'sha256:' + '0' * 64
    elif kind == 'files of different packs':
        first['pack_id'] = 'sha256:' + '1' * 64
    elif kind == 'same family':
        other['scorer']['family'] = first['scorer']['family']
    elif kind == 'same scorer id':
        other['scorer']['id'] = first['scorer']['id']
    elif kind == 'family under test':
        other['scorer']['family'] = 'family-under-test'
    return first, other


REFUSED = ['unknown scenario', 'missing scenario', 'duplicate scenario', 'missing rating', 'duplicate rating',
           'above the scale', 'below the scale', 'boolean value', 'float value', 'extra field', 'extra top-level field',
           'unknown dimension', 'unknown label', 'another pack', 'files of different packs', 'same family',
           'same scorer id', 'family under test']


@pytest.mark.parametrize('kind', REFUSED)
def test_a_damaged_score_pair_is_refused_and_nothing_is_written(made, tmp_path, project_root, kind):
    run, config, pack = made
    header, _, first, other, _ = pair(tmp_path, run, pack)
    first, other = broken(kind, first, other, header)
    paths = [scores_path(tmp_path, 'first', first), scores_path(tmp_path, 'second', other)]
    before = tree(run)
    with pytest.raises(Refusal):
        import_scores(config, project_root, import_args(run, *paths))
    assert run.store.list_sidecars(run.relative('imports')) == []
    assert tree(run) == before  # not one byte of the data root changed


def test_other_refused_imports(made, tmp_path, project_root):
    run, config, pack = made
    _, _, first, other, paths = pair(tmp_path, run, pack)
    with pytest.raises(Refusal, match='at least 2'):
        import_scores(config, project_root, import_args(run, paths[0]))  # one scorer is not enough
    with pytest.raises(Refusal, match='--run-id'):
        import_scores(config, project_root, SimpleNamespace(run_id='../x', scores=[str(path) for path in paths]))
    with pytest.raises(Refusal, match='no such run'):  # a run that never started has no lock file, so nothing is created
        import_scores(config, project_root, SimpleNamespace(run_id='0' * 16, scores=[str(path) for path in paths]))
    assert not (run.store.root / 'eval' / 'runs' / ('0' * 16)).exists()
    huge = tmp_path / 'huge.json'
    huge.write_bytes(b' ' * (config['evaluation']['judge']['max_score_file_bytes'] + 1))
    with pytest.raises(Refusal, match='max_score_file_bytes'):
        import_scores(config, project_root, import_args(run, paths[0], huge))
    not_json = tmp_path / 'nan.json'
    not_json.write_text('{"schema": NaN}', encoding='utf-8')
    with pytest.raises(Refusal, match='strict JSON'):
        import_scores(config, project_root, import_args(run, paths[0], not_json))
    scaled = deepcopy(config)
    scaled['evaluation']['judge']['score_max'] = 7  # a profile with another scale than the pack was written for
    with pytest.raises(Refusal, match='another scale'):
        import_scores(scaled, project_root, import_args(run, *paths))
    assert run.store.list_sidecars(run.relative('imports')) == []


def test_a_repeated_result_is_refused_by_the_summary_the_pack_writer_and_the_import_and_never_counted_twice(
        made, tmp_path, project_root):
    """Fix round 1, P2: one `(run_id, scenario_id)` in two segments must stop every reader that counts rows."""
    run, config, pack = made
    *_, paths = pair(tmp_path, run, pack)
    first = next(read_records(run.data, run.catalog))
    run.store.append('eval_results', [result_row(run, first, 'on', 'a repeat', 'trace-repeat')])
    before = tree(run)
    with pytest.raises(StorageError, match='stored twice'):
        scan_results(run)
    with pytest.raises(StorageError, match='stored twice'):
        pack_of(run)  # the pack writer takes its keys from the same scan
    with pytest.raises(StorageError, match='stored twice'):
        import_scores(config, project_root, import_args(run, *paths))
    assert tree(run) == before  # the import wrote nothing: the scan comes before the write
    with pytest.raises(StorageError, match='duplicate key'):
        run.store.read('eval_results')  # and the table itself refuses every reader


def test_the_pack_is_imported_line_by_line_and_never_read_whole(made, tmp_path, project_root, monkeypatch):
    run, config, pack = made
    *_, paths = pair(tmp_path, run, pack)
    whole = type(run.store).read_sidecar

    def guarded(self, relative, max_bytes):
        assert not relative.endswith('pack.jsonl'), 'the pack was read whole'
        return whole(self, relative, max_bytes)
    monkeypatch.setattr(type(run.store), 'read_sidecar', guarded)
    assert import_scores(config, project_root, import_args(run, *paths))['disagreements'] == 0


def test_the_pack_size_limit_is_the_configured_one_on_both_sides(made, tmp_path, project_root):
    """No fixed 64 MiB: `evaluation.run.max_sidecar_bytes` bounds the pack when it is written and when it is imported."""
    run, config, pack = made
    *_, paths = pair(tmp_path, run, pack)
    size = (run.store.root / 'eval' / 'runs' / run.rid / 'packs' / pack['pack_id'][7:23] / 'pack.jsonl').stat().st_size
    exact, short = deepcopy(config), deepcopy(config)
    exact['evaluation']['run']['max_sidecar_bytes'] = size
    short['evaluation']['run']['max_sidecar_bytes'] = size - 1
    with pytest.raises(StorageError, match='SourceTooLarge'):  # one byte over the configured limit: not importable
        import_scores(short, project_root, import_args(run, *paths))
    assert run.store.list_sidecars(run.relative('imports')) == []
    assert import_scores(exact, project_root, import_args(run, *paths))['disagreements'] == 0  # exactly at the limit
    # the writer refuses what the reader could not read back, before it writes a byte
    run.settings = {**run.settings, 'run.max_sidecar_bytes': size - 1, 'judge.blinding_seed': SEED + 99}
    before = tree(run)
    with pytest.raises(StorageError, match='larger than evaluation.run.max_sidecar_bytes'):
        pack_of(run)
    assert tree(run) == before


def test_an_import_is_refused_at_once_while_a_run_holds_the_lock_and_works_after(made, tmp_path, project_root):
    run, config, pack = made
    *_, paths = pair(tmp_path, run, pack)
    lock = hold_run(run.store, run.rid, create=True)  # what a running `eval run` holds
    before = tree(run)
    try:
        with pytest.raises(Refusal, match='in use by another process'):
            import_scores(config, project_root, import_args(run, *paths))
        assert tree(run) == before  # refused before anything was read or written
    finally:
        lock.release()
    assert import_scores(config, project_root, import_args(run, *paths))['disagreements'] == 0
    hold_run(run.store, run.rid, create=False).release()  # the import gave its lock back


def test_an_import_gives_its_lock_back_on_every_exit_path(made, tmp_path, project_root, monkeypatch):
    from prompt_enhancer import eval_judge
    run, config, pack = made
    taken = spy_on_locks(monkeypatch, eval_judge)
    _, _, first, other, good = pair(tmp_path, run, pack)
    other['scorer']['family'] = first['scorer']['family']
    bad = [scores_path(tmp_path, 'a', first), scores_path(tmp_path, 'b', other)]
    with pytest.raises(Refusal, match='different families'):  # a refusal
        import_scores(config, project_root, import_args(run, *bad))
    run.store.append('eval_results', [result_row(run, next(read_records(run.data, run.catalog)), 'on', 'x', 'trace-x')])
    with pytest.raises(StorageError, match='stored twice'):  # an exception from the storage layer
        import_scores(config, project_root, import_args(run, *good))
    assert [item.released for item in taken] == [True, True]
    hold_run(run.store, run.rid, create=False).release()  # and the file really is free


def test_a_two_output_item_carries_nothing_that_tells_the_labels_apart(made):
    """Fix round 1, P3 (c): blinding covers the comparison of one scenario's two outputs. Whatever the seed, such an
    item differs only in the order of its two outputs; a single-output item cannot hide its variant from its own kind."""
    run, _, pack = made
    other = next(seed for seed in range(SEED + 1, SEED + 200)
                 if all(swapped(seed, name) != swapped(SEED, name) for name in ('gen-1', 'edit-1')))
    _, *before = pack_lines(run, pack['pack_id'])
    run.settings = {**run.settings, 'judge.blinding_seed': other}
    again = pack_of(run)
    _, *after = pack_lines(run, again['pack_id'])
    for one, two in zip(before, after):
        assert {key: value for key, value in one.items() if key != 'outputs'} == {
            key: value for key, value in two.items() if key != 'outputs'}
        if len(one['outputs']) == 2:
            assert [item['output'] for item in one['outputs']] == [item['output'] for item in reversed(two['outputs'])]
    assert [len(item['outputs']) for item in before] == [2, 2, 1]


def test_a_pack_is_not_written_for_a_run_that_has_a_missing_result(tmp_path, project_root, model_test_profile):
    """The pack needs every selected result: a run missing one is not packed (the run command refuses a partial pack)."""
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records())
    keys, _ = scan_results(run)
    keys.pop(next(iter(keys)))
    with pytest.raises(Exception):
        write_pack(run, keys)
    assert run.store.list_sidecars(run.relative('packs')) == [] or all(
        not name.endswith('pack.jsonl') for name in run.store.list_sidecars(run.relative('packs')))
