"""`eval run` and `eval import` through the real entry point (T-016 P4; R-001, R-009, R-017, R-024, R-026, R-027).

Labelled substitutes: the loopback model double (`eval_fixtures.VariantDouble`), its recording/gating wrappers, and a tiny
synthetic set. Everything else is production code. Evidence class: `mock_contract`; no live model, GPU or network.
The double's writer output never reaches the envelope here (a shared-profile conflict makes every status `degraded`, with
the original prompt as output), so the two retrieval variants of a scenario have equal outputs; the tests of what a pack
shows under which label use synthetic result rows (`test_eval_judge.py`)."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_fixtures import finish_setup, model_server, pipeline_config, run_cli, text, write_yaml
from eval_fixtures import (SCHEMA, Recording, comparable, evaluation_values, prepared, record, result_rows, run_args,
                           snapshot_of, spy_on_locks, standard_set, start, write_set)
from prompt_enhancer.cli_input import Refusal
from prompt_enhancer.eval_judge import import_scores, swapped
from serve_fixtures import Gate

SEED = 11
FOREIGN = 's20200101T000000000000Z-00000000'  # a well-formed snapshot id that was never published


def sources_for(tmp_path, originals):
    """The `--source-images` directory: each file named by its image-id digest (the off variant of the edit scenario)."""
    directory = tmp_path / 'sources'
    directory.mkdir()
    for image_id, data in originals.items():
        (directory / image_id[7:]).write_bytes(data)
    return directory


def runs_of(config):
    return Path(config['storage']['data_root']) / 'eval' / 'runs'


def only(path):
    (entry,) = list(path.iterdir())
    return entry


def lines_of(path):
    return [json.loads(line) for line in path.read_bytes().decode('utf-8').split('\n') if line]


def test_a_run_writes_one_result_per_scenario_resumes_without_a_call_and_writes_a_blinded_pack(
        k2_env, model_test_profile, tmp_path, project_root):
    recording = Recording()
    with model_server(recording.double()) as (handler, port):
        config, originals = prepared(k2_env, model_test_profile, tmp_path, project_root, port, handler)
        set_path = write_set(tmp_path / 'set.parquet', standard_set(originals))
        extra = ['--source-images', sources_for(tmp_path, originals)]
        path = write_yaml(project_root, tmp_path, config)
        args = run_args(path, project_root, set_path, snapshot_of(k2_env), extra=extra)
        done = run_cli(args, project_root, timeout=600)
        out, err = text(done)
        assert done.returncode == 0, err
        summary = json.loads(out)
        first_calls = list(recording.prompts)

        # one durable result per (variant, scenario); independent counts
        assert summary['units'] == {'total': 7, 'skipped': 0, 'selected': 7, 'completed': 7, 'failed': 0, 'not_run': 0}
        rows = result_rows(config, project_root)
        assert len(rows) == 7 and len({(row['run_id'], row['scenario_id']) for row in rows}) == 7
        by_variant = {variant: sorted(row['scenario_id'] for row in rows if row['metrics']['variant'] == variant)
                      for variant in ('on', 'off')}
        assert by_variant == {'on': ['edit-1', 'gen-1', 'gen-2'], 'off': ['edit-1', 'gen-1', 'gen-2', 'off-1']}
        for row in rows:
            metrics = row['metrics']
            assert metrics['evidence'] == 'structural' and row['status'] == metrics['envelope']['status']
            assert metrics['semantic'] == {'state': 'not_run', 'reason': 'scoring contract not approved'}  # no score, no number
            assert metrics['intervals']['state'] == 'not_run'
            assert metrics['structural']['links'] == {'state': 'not_run', 'reason': 'trace row unavailable'}
            assert metrics['snapshot_id'] == (snapshot_of(k2_env) if metrics['variant'] == 'on' else None)
        gen = next(row for row in rows if row['scenario_id'] == 'gen-1' and row['metrics']['variant'] == 'on')
        assert gen['metrics']['structural']['literal']['items'] == [{'constraint_id': 'c1', 'present': True}]
        assert summary['structural']['literal']['items'] == 2 and summary['structural']['literal']['present'] == 2
        assert summary['semantic'] == {'state': 'not_run', 'reason': 'scoring contract not approved'}
        assert summary['intervals'] == {'state': 'not_run', 'reason': 'interval method not approved'}
        # multilingual text reaches the model unchanged (R-001)
        assert 'Une Name très calme, 静かな Name' in first_calls and 'A Name in the garden "OPEN" sign' in first_calls

        # the blinded pack and its key
        run_directory = only(runs_of(config))
        pack_directory = only(run_directory / 'packs')
        assert sorted(entry.name for entry in pack_directory.iterdir()) == ['key.json', 'pack.jsonl']
        header, *items = lines_of(pack_directory / 'pack.jsonl')
        key = json.loads((pack_directory / 'key.json').read_bytes())
        assert header['items'] == 4 == len(items) and header['pack_id'] == key['pack_id'] == summary['pack']['pack_id']
        assert header['scale'] == {'min': 1, 'max': 5} and header['rubric']['prompt_id'] == 'eval_rubric_v0'
        pack_bytes = (pack_directory / 'pack.jsonl').read_bytes()
        for row in rows:
            assert row['metrics']['envelope']['trace_id'].encode() not in pack_bytes  # no trace id in the pack
        for item in items:
            assert set(item) == {'schema', 'scenario_id', 'kind', 'task', 'prompt', 'source_image', 'key', 'dimensions',
                                 'outputs'}
            assert all(set(output) == {'label', 'output'} for output in item['outputs'])
        edit = next(item for item in items if item['scenario_id'] == 'edit-1')
        assert [entry['dimension'] for entry in edit['dimensions']] == [
            'intent_preserved', 'link_alignment', 'leakage', 'under_editing']
        assert any(entry == {'dimension': 'constraint_survival', 'ref': 'c1'}
                   for entry in next(item for item in items if item['scenario_id'] == 'gen-1')['dimensions'])
        for scenario in ('gen-1', 'gen-2', 'edit-1'):  # the label order follows the seed, a key entry names each variant
            entries = [entry for entry in key['entries'] if entry['scenario_id'] == scenario]
            assert [entry['variant'] for entry in entries] == (['off', 'on'] if swapped(SEED, scenario) else ['on', 'off'])
        assert [entry['label'] for entry in key['entries'] if entry['scenario_id'] == 'off-1'] == ['A']

        # resume: same identity, nothing missing, so no model call and no second result
        again = run_cli(args, project_root, timeout=600)
        assert again.returncode == 0, text(again)[1]
        assert recording.prompts == first_calls and len(result_rows(config, project_root)) == 7

        # another blinding seed changes only the pack: no model call, a run with the same id
        config['evaluation']['judge']['blinding_seed'] = SEED + 1
        reseeded = write_yaml(project_root, tmp_path, config, name='reseeded.yaml')
        third = run_cli(run_args(reseeded, project_root, set_path, snapshot_of(k2_env), extra=extra), project_root,
                        timeout=600)
        assert third.returncode == 0, text(third)[1]
        assert recording.prompts == first_calls and json.loads(text(third)[0])['run_id'] == summary['run_id']
        assert len(result_rows(config, project_root)) == 7 and len(list(runs_of(config).iterdir())) == 1
        key_after = json.loads((only(only(runs_of(config)) / 'packs') / 'key.json').read_bytes())
        assert key_after['blinding_seed'] == SEED + 1  # the key covers the seed

        # a changed manifest under the same run id is refused, never overwritten
        manifest = run_directory / 'manifest.json'
        manifest.write_bytes(manifest.read_bytes().replace(b'"split":"all"', b'"split":"fit"'))
        refused = run_cli(args, project_root, timeout=600)
        assert refused.returncode == 2 and 'another manifest' in text(refused)[1] and text(refused)[0] == ''
        assert recording.prompts == first_calls


def test_an_off_only_run_never_touches_the_knowledge_base(model_test_profile, tmp_path, project_root):
    recording = Recording()
    with model_server(recording.double()) as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        evaluation_values(config)
        finish_setup(config, project_root, handler)
        handler.real_writer, handler.writer_system = handler.writer_system, None
        set_path = write_set(tmp_path / 'off.parquet', [
            record('off-1', 'A plain bowl, no extras', kind='rag_off', rag=False, linked=False),
            record('off-2', 'Une autre', kind='rag_off', rag=False, linked=False)])
        path = write_yaml(project_root, tmp_path, config)
        done = run_cli(run_args(path, project_root, set_path, None, variants='record'), project_root, timeout=300)
        out, err = text(done)
        assert done.returncode == 0, err
        assert json.loads(out)['units']['completed'] == 2 and not (tmp_path / 'kb').exists()  # no store was opened (R-009)
        assert sorted(recording.prompts) == ['A plain bowl, no extras', 'Une autre']
        # a snapshot is refused when nothing runs with retrieval on, before any further effect
        before = len(recording.prompts)
        refused = run_cli(run_args(path, project_root, set_path, FOREIGN, variants='record'), project_root)
        assert refused.returncode == 2 and 'not accepted' in text(refused)[1] and text(refused)[0] == ''
        assert len(recording.prompts) == before and not (tmp_path / 'kb').exists()


def test_a_killed_run_resumes_at_the_first_missing_result_without_repeating_a_successful_call(
        k2_env, model_test_profile, tmp_path, project_root):
    gate = Gate()
    recording = Recording(gate=gate, free=2)  # the first two scenarios run; the third waits inside the model double
    with model_server(recording.double()) as (handler, port):
        config, originals = prepared(k2_env, model_test_profile, tmp_path, project_root, port, handler, concurrency=1)
        set_path = write_set(tmp_path / 'set.parquet', standard_set(originals))
        path = write_yaml(project_root, tmp_path, config)
        args = run_args(path, project_root, set_path, snapshot_of(k2_env), variants='record')
        process = start(args, project_root)
        try:
            gate.wait_for(1)  # the third scenario is inside the double
            assert len(result_rows(config, project_root)) == 2
            process.kill()
            process.wait(timeout=60)
        finally:
            gate.release()
            process.stdout.close()
            process.stderr.close()
        rows = result_rows(config, project_root)  # a hard kill leaves whole results only
        assert sorted(row['scenario_id'] for row in rows) == ['gen-1', 'gen-2']
        done = run_cli(args, project_root, timeout=600)
        out, err = text(done)
        assert done.returncode == 0, err
        summary = json.loads(out)
        assert summary['units']['completed'] == 4 and summary['units']['not_run'] == 0
        counts = {prompt: recording.prompts.count(prompt) for prompt in set(recording.prompts)}
        assert counts['A Name in the garden "OPEN" sign'] == counts['Une Name très calme, 静かな Name'] == 1  # not repeated
        assert counts['Change the Name please'] == 2  # the interrupted attempt and the retry; it had no result
        final = result_rows(config, project_root)
        assert len(final) == 4 and len({(row['run_id'], row['scenario_id']) for row in final}) == 4


REFUSALS = {  # name: (what to change, expected words in the refusal)
    'foreign snapshot': ('snapshot', 'not the current published snapshot'),
    'unknown concept': ('concept', 'concept id'),
    'source image absent from the snapshot': ('image', 'source image id'),
    'a concept in both splits': ('split', 'both splits'),
    'a column the draft layout does not have': ('column', 'columns'),
    'a repeated scenario id': ('repeat', 'repeated'),
    'a kind that is not configured': ('kind', 'scenario_kinds'),
    'an off edit without source images': ('off_edit', '--source-images'),
    'a source file that is not the image': ('bad_source', 'does not hash'),
    'no snapshot with retrieval on': ('nosnapshot', '--snapshot: required'),
    'a draft field of the wrong type': ('draft', 'draft'),
    'a prompt outside its spans': ('span', 'verbatim'),
}


def test_a_set_the_run_cannot_trust_is_refused_before_any_model_call_and_any_file(
        k2_env, model_test_profile, tmp_path, project_root):
    import pyarrow as pa
    recording = Recording()
    with model_server(recording.double()) as (handler, port):
        config, originals = prepared(k2_env, model_test_profile, tmp_path, project_root, port, handler)
        path = write_yaml(project_root, tmp_path, config)
        image = sorted(originals)[0]
        sources = sources_for(tmp_path, originals)
        good = standard_set(originals)
        for name, (case, words) in REFUSALS.items():
            records, snapshot, extra, variants = deepcopy(good), snapshot_of(k2_env), ['--source-images', sources], 'both'
            schema = SCHEMA
            if case == 'snapshot':
                snapshot = FOREIGN
            elif case == 'concept':
                records[0]['expected_concept_links'][0]['concept_id'] = 'concept:' + '0' * 64
            elif case == 'image':
                records[2]['source_image']['image_id'] = 'sha256:' + '1' * 64
                variants, extra = 'record', []  # no off edit: the snapshot is what lacks the image
            elif case == 'split':
                records[1]['split'] = 'test'
            elif case == 'column':
                schema = SCHEMA.append(pa.field('extra', pa.string()))
                records = [dict(item, extra='x') for item in records]
            elif case == 'repeat':
                records[1]['scenario_id'] = records[0]['scenario_id']
            elif case == 'kind':
                records[0]['kind'] = 'not_a_configured_kind'
            elif case == 'off_edit':
                extra = []
            elif case == 'bad_source':
                (sources / image[7:]).write_bytes(b'not the image')
            elif case == 'nosnapshot':
                snapshot = None
            elif case == 'draft':
                records[0]['draft_rag_enabled'] = None
            elif case == 'span':
                records[0]['prompt'] = 'no mention here'
            set_path = write_set(tmp_path / 'case.parquet', records, schema)
            done = run_cli(run_args(path, project_root, set_path, snapshot, variants=variants, extra=extra), project_root,
                       timeout=300)
            out, err = text(done)
            assert done.returncode == 2 and out == '' and err.startswith('refused: ') and words in err, (name, err)
            assert recording.prompts == [] and not (tmp_path / 'data' / 'eval').exists(), name  # no call, no file
            (sources / image[7:]).write_bytes(originals[image])
        # a too small `evaluation.run.max_set_bytes` refuses the set file itself
        config['evaluation']['run']['max_set_bytes'] = 100
        small = write_yaml(project_root, tmp_path, config, name='small.yaml')
        set_path = write_set(tmp_path / 'case.parquet', good)
        done = run_cli(run_args(small, project_root, set_path, snapshot_of(k2_env), extra=['--source-images', sources]),
                       project_root)
        assert done.returncode == 2 and 'evaluation.run.max_set_bytes' in text(done)[1] and recording.prompts == []


def test_a_set_path_through_a_link_is_refused(model_test_profile, tmp_path, project_root):
    from test_storage_anchored import _dir_link, _drop_link
    real, link = tmp_path / 'real', tmp_path / 'linked'
    real.mkdir()
    write_set(real / 'set.parquet', standard_set({'sha256:' + '2' * 64: b''}))
    _dir_link(link, real)
    try:
        config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
        evaluation_values(config)
        path = write_yaml(project_root, tmp_path, config)
        done = run_cli(run_args(path, project_root, link / 'set.parquet', None, variants='record', split='test'),
                       project_root)
        assert done.returncode == 2 and 'link' in text(done)[1] and not (tmp_path / 'data' / 'eval').exists()
    finally:
        _drop_link(link)


def test_concurrency_one_and_three_give_identical_results_and_the_bound_is_reached_and_never_exceeded(
        k2_env, model_test_profile, tmp_path, project_root):
    """Differential: the same set with `max_concurrency` 1 and 3 gives the same rows (but for the run and trace ids) and the
    same summary. The double holds requests until `limit` are inside it at once, so no sleep decides the result."""
    outcomes, originals, snapshot, base, writer, verifier = {}, None, None, None, None, None
    for limit in (1, 3):
        gate = Gate()
        recording = Recording(gate=gate)
        with model_server(recording.double()) as (handler, port):
            if base is None:
                config, originals = prepared(k2_env, model_test_profile, tmp_path, project_root, port, handler,
                                             concurrency=limit)
                snapshot, base, writer, verifier = snapshot_of(k2_env), deepcopy(config), handler.real_writer, handler.verifier_system
            else:
                config = deepcopy(base)
                config['vlm']['backends']['main']['base_urls'] = [f'http://127.0.0.1:{port}']
                config['vlm']['backends']['embedder']['base_urls'] = config['vlm']['backends']['main']['base_urls']
                handler.real_writer, handler.writer_system, handler.verifier_system = writer, None, verifier  # the profile is already active
                evaluation_values(config, concurrency=limit)
            config['storage']['data_root'] = str(tmp_path / f'data{limit}')
            set_path = write_set(tmp_path / 'set.parquet', standard_set(originals))
            path = write_yaml(project_root, tmp_path, config, name=f'config{limit}.yaml')
            process = start(run_args(path, project_root, set_path, snapshot, variants='record'), project_root)
            try:
                gate.wait_for(limit)
                gate.release()
                out, err = process.communicate(timeout=600)
            finally:
                gate.release()
                process.kill()
            assert process.returncode == 0, err.decode()
            outcomes[limit] = (gate.peak, json.loads(out), result_rows(config, project_root))
    (peak_one, summary_one, rows_one), (peak_three, summary_three, rows_three) = outcomes[1], outcomes[3]
    assert peak_one == 1 and peak_three == 3  # reached, never exceeded (the peak over the whole run)
    order = lambda row: (row['scenario_id'], row['metrics']['variant'])
    assert [comparable(row) for row in sorted(rows_one, key=order)] == [comparable(row) for row in sorted(rows_three, key=order)]
    for summary in (summary_one, summary_three):
        summary.pop('run_id'), summary.pop('pack')
        summary['packs'] = len(summary['packs'])
        summary.pop('not_run_reasons')
    assert summary_one == summary_three


def test_a_second_process_of_the_same_run_is_refused_at_once_and_a_rerun_after_the_first_repeats_no_call(
        k2_env, model_test_profile, tmp_path, project_root):
    """Fix round 1, P1: two `eval run` processes of one identity must never both run it. The second starts while the first is
    held inside the model double (so the first surely holds the run lock): it is refused with no model call and no row, an
    `eval import` of the run is refused too, and after the first ends a rerun resumes with no repeated call."""
    gate = Gate()
    recording = Recording(gate=gate)
    with model_server(recording.double()) as (handler, port):
        config, originals = prepared(k2_env, model_test_profile, tmp_path, project_root, port, handler, concurrency=2)
        set_path = write_set(tmp_path / 'set.parquet', standard_set(originals))
        # test-only: the profile's 1 s read timeout would end a held request and let the pipeline degrade and go on
        config['vlm']['backends']['main']['http']['read_timeout_s'] = 120
        path = write_yaml(project_root, tmp_path, config)
        args = run_args(path, project_root, set_path, snapshot_of(k2_env), variants='record')
        first = start(args, project_root)
        try:
            gate.wait_for(2)  # two units are inside the double: the first process is past `prepare` and holds the lock
            held = list(recording.prompts)
            second = run_cli(args, project_root, timeout=300)
            assert second.returncode == 2 and text(second)[0] == '', text(second)[1]
            assert 'in use by another process' in text(second)[1]
            (run_directory,) = list(runs_of(config).iterdir())
            with pytest.raises(Refusal, match='in use by another process'):  # in this process: the lock is the other one's
                import_scores(config, project_root, SimpleNamespace(
                    run_id=run_directory.name, scores=[str(tmp_path / 'a.json'), str(tmp_path / 'b.json')]))
            assert recording.prompts == held and result_rows(config, project_root) == []  # no call, no row from either
            gate.release()
            out, err = first.communicate(timeout=600)
        finally:
            gate.release()
            first.kill()
        assert first.returncode == 0, err.decode()
        calls = list(recording.prompts)
        rows = result_rows(config, project_root)
        assert len(rows) == 4 and len({(row['run_id'], row['scenario_id']) for row in rows}) == 4
        again = run_cli(args, project_root, timeout=600)  # the first has ended: resuming is allowed and calls nothing
        assert again.returncode == 0, text(again)[1]
        assert recording.prompts == calls and len(result_rows(config, project_root)) == 4


def test_a_refusal_after_the_run_has_started_is_an_operational_failure_and_gives_the_lock_back(
        model_test_profile, tmp_path, project_root, monkeypatch, capsys):
    """Fix round 1, P3 (f): exit 2 means no side effect. Once the manifest is written (and the lock held), a `Refusal` raised
    by the work (a source image that changed since `prepare`) is exit 1, and the run can be taken again."""
    from prompt_enhancer import eval_run
    from prompt_enhancer.__main__ import build_parser
    from prompt_enhancer.cli_commands import run_command
    from prompt_enhancer.cli_input import Refusal
    from prompt_enhancer.storage import open_data_store
    recording = Recording()
    with model_server(recording.double()) as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        evaluation_values(config)
        finish_setup(config, project_root, handler)
        handler.real_writer, handler.writer_system = handler.writer_system, None
        set_path = write_set(tmp_path / 'off.parquet', [record('off-1', 'A plain bowl', kind='rag_off', rag=False, linked=False)])
        path = write_yaml(project_root, tmp_path, config)

        def changed(*args, **kwargs):
            raise Refusal('--source-images: changed while the run was going')
        monkeypatch.setattr(eval_run, 'build_request', changed)
        taken = spy_on_locks(monkeypatch, eval_run)
        parsed = build_parser().parse_args([str(item) for item in run_args(path, project_root, set_path, None, variants='record')])
        assert run_command(parsed, project_root) == 1
        error = capsys.readouterr().err
        assert error.startswith('failed: --source-images: changed') and 'refused' not in error
        (run_directory,) = list(runs_of(config).iterdir())
        assert (run_directory / 'manifest.json').is_file()  # a side effect exists, so the status could not be 2
        store = open_data_store(config, 'eval run', project_root=project_root)
        assert [item.released for item in taken] == [True]  # the lock was taken once and released on that exit path
        eval_run.hold_run(store, run_directory.name, create=False).release()  # and the file is free
        assert recording.prompts == []


def test_the_run_lock_is_held_while_the_final_summary_and_the_pack_are_written(
        model_test_profile, tmp_path, project_root, monkeypatch):
    """PM integration (retest N2, mutant O1): the lock covers every durable write of the run, the last summary and the
    judge pack included, and is released only after them."""
    from prompt_enhancer import eval_judge, eval_run
    from prompt_enhancer.__main__ import build_parser
    from prompt_enhancer.cli_commands import run_command
    recording = Recording()
    with model_server(recording.double()) as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        evaluation_values(config)
        finish_setup(config, project_root, handler)
        set_path = write_set(tmp_path / 'off.parquet', [record('off-1', 'A plain bowl', kind='rag_off', rag=False, linked=False)])
        path = write_yaml(project_root, tmp_path, config)
        taken = spy_on_locks(monkeypatch, eval_run)
        seen = []

        def watched(name, real):
            def call(*args, **kwargs):
                seen.append((name, [item.released for item in taken]))
                return real(*args, **kwargs)
            return call
        monkeypatch.setattr(eval_run, 'refresh_summary', watched('summary', eval_run.refresh_summary))
        monkeypatch.setattr(eval_judge, 'write_pack', watched('pack', eval_judge.write_pack))
        parsed = build_parser().parse_args([str(item) for item in run_args(path, project_root, set_path, None, variants='record')])
        assert run_command(parsed, project_root) == 0
    assert [name for name, _ in seen].count('pack') == 1 and 'summary' in [name for name, _ in seen]
    assert all(state == [False] for _, state in seen)  # each write happened while the one lock was still held
    assert [item.released for item in taken] == [True]
