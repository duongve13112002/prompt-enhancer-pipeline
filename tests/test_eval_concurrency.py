"""The coordinator of `eval run` in process (T-016 P4; decision 149 / S46; R-017).

Labelled substitute: `FakePipeline`, a stand-in for `Pipeline` that returns valid hand-made envelopes and counts the
requests inside it. The coordinator, the data store, the failure markers and the summary are production code. The bound is
proved without a sleep: the fake holds every request until `limit` are inside at once, and then yields to the event loop
many times, so any extra worker would be seen entering. Evidence class: `mock_contract` (no model, no network)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from eval_fixtures import record, result_row, synthetic_run
from prompt_enhancer import eval_run
from prompt_enhancer.cli_input import Refusal
from prompt_enhancer.eval_set import read_records
from prompt_enhancer.storage import StorageError
from prompt_enhancer.eval_run import execute, hold_run, refresh_summary, release, scan_results
from prompt_enhancer.response_lifecycle import LifecycleFull

YIELDS = 200  # event-loop turns an extra worker would need at most to start (a turn runs every ready task)
FAILSAFE_S = 60  # only a guard against a hang when the bound is too low; no result depends on elapsed time


def records(count=6):
    return [record(f's{number}', f'Prompt {number}', linked=False, split='fit' if number % 2 else 'test')
            for number in range(count)]


class State:
    def __init__(self, limit, failing=(), snapshot=None, capacity=()):
        self.limit, self.failing, self.inflight, self.peak, self.snapshot = limit, set(failing), 0, 0, snapshot
        self.capacity = set(capacity)  # prompts the fake refuses with LifecycleFull, as a full trace store does
        self.full, self.calls, self.delivered, self.abandoned = asyncio.Event(), [], 0, 0


def fake_pipeline(state):
    class FakePipeline:
        def __init__(self, config, project_root=None):
            self.variant = 'on' if config['rag']['enabled'] else 'off'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *error):
            return None

        async def enhance(self, request):
            state.calls.append((self.variant, request['prompt']))
            state.inflight += 1
            state.peak = max(state.peak, state.inflight)
            if state.inflight == state.limit:
                state.full.set()
            await state.full.wait()  # every request waits until `limit` are inside at once
            for _ in range(YIELDS):
                await asyncio.sleep(0)
            state.inflight -= 1
            if request['prompt'] in state.failing:
                raise RuntimeError('a model call failed')
            if request['prompt'] in state.capacity:
                raise LifecycleFull('trace capacity unavailable before generation')

            async def delivered():
                state.delivered += 1

            async def abandoned():
                state.abandoned += 1
            envelope = {'status': 'success', 'output': f'{self.variant}: {request["prompt"]}', 'attachments': [],
                        'source_binding': None, 'warnings': [], 'trace_id': f'trace-{len(state.calls)}',
                        'retrieval': {'state': 'retrieved' if self.variant == 'on' else 'disabled', 'snapshot_id': state.snapshot if self.variant == 'on' else None}}
            return SimpleNamespace(envelope=envelope, after_delivery=delivered, abandon=abandoned)
    return FakePipeline


def execute_once(run):
    async def main():
        return await asyncio.wait_for(execute(run), FAILSAFE_S)
    return asyncio.run(main())


@pytest.mark.parametrize('limit', [1, 2, 3, 5])
def test_the_bound_is_reached_and_never_exceeded_and_every_unit_has_one_result(
        monkeypatch, tmp_path, project_root, model_test_profile, limit):
    state = State(limit)
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(state))
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(), concurrency=limit, fill=False)
    counts = execute_once(run)
    assert state.peak == limit  # reached, and never above it
    assert counts['completed'] == 12 and counts['failed'] == 0  # six scenarios x two variants
    keys, stats = scan_results(run)
    assert len(keys) == 12 and len(state.calls) == 12 and len(set(state.calls)) == 12  # one call and one row per unit
    assert state.delivered == 12 and state.abandoned == 0  # every successful response is released after its row is stored
    assert refresh_summary(run, keys, stats)['units'] == {'total': 12, 'skipped': 0, 'selected': 12, 'completed': 12,
                                                          'failed': 0, 'not_run': 0}


def test_a_failed_call_is_counted_apart_marked_and_retried_alone_by_the_next_invocation(
        monkeypatch, tmp_path, project_root, model_test_profile):
    state = State(2, failing={'Prompt 3'})
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(state))
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(), concurrency=2, fill=False)
    counts = execute_once(run)
    assert counts['completed'] == 10 and counts['failed'] == 2  # the scenario fails in both variants; the rest goes on
    keys, stats = scan_results(run)
    summary = refresh_summary(run, keys, stats)
    assert summary['units'] == {'total': 12, 'skipped': 0, 'selected': 12, 'completed': 10, 'failed': 2, 'not_run': 0}
    assert summary['not_run_reasons'] == {}
    assert len(run.store.list_sidecars(run.relative('incomplete'))) == 2
    marker = run.store.read_sidecar(
        run.relative('incomplete/' + run.store.list_sidecars(run.relative('incomplete'))[0]), 1 << 20)
    assert json.loads(marker)['disposition'] == 'failed'
    assert json.loads(marker)['reason'] == 'RuntimeError'  # the class of the failure; never a message or a prompt
    again = State(2)
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(again))
    counts = execute_once(run)  # resumes at the first unfinished item: only the two failed units run
    assert counts['completed'] == 2 and sorted(again.calls) == [('off', 'Prompt 3'), ('on', 'Prompt 3')]  # ('off' sorts before 'on')
    keys, stats = scan_results(run)
    summary = refresh_summary(run, keys, stats)
    assert summary['units'] == {'total': 12, 'skipped': 0, 'selected': 12, 'completed': 12, 'failed': 0, 'not_run': 0}
    assert again.peak <= 2


def test_a_unit_refused_for_trace_capacity_is_not_run_with_its_reason_and_retried_on_resume(
        monkeypatch, tmp_path, project_root, model_test_profile):
    """`LifecycleFull` is the pipeline's own capacity signal (a trace slot is freed only after its write): the unit is
    `not_run` (reason `trace_capacity`), has no result row, no retry loop runs, and the next invocation runs it alone."""
    state = State(2, capacity={'Prompt 3'})
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(state))
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(), concurrency=2, fill=False)
    counts = execute_once(run)
    assert counts['completed'] == 10 and counts['not_run'] == 2 and counts['failed'] == 0
    assert state.calls.count(('on', 'Prompt 3')) == 1  # one attempt, no retry loop
    keys, stats = scan_results(run)
    summary = refresh_summary(run, keys, stats)
    assert summary['units'] == {'total': 12, 'skipped': 0, 'selected': 12, 'completed': 10, 'failed': 0, 'not_run': 2}
    assert summary['not_run_reasons'] == {'trace_capacity': 2}
    assert len(keys) == 10 and state.delivered == 10  # no row for the refused units
    again = State(2)
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(again))
    assert execute_once(run)['completed'] == 2 and sorted(again.calls) == [('off', 'Prompt 3'), ('on', 'Prompt 3')]
    keys, stats = scan_results(run)
    summary = refresh_summary(run, keys, stats)
    assert summary['units']['not_run'] == 0 and summary['not_run_reasons'] == {}  # a result clears its marker


def test_a_later_failure_replaces_an_earlier_not_run_marker_of_the_same_unit(
        monkeypatch, tmp_path, project_root, model_test_profile):
    state = State(2, capacity={'Prompt 3'})
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(state))
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(), concurrency=2, fill=False)
    execute_once(run)
    again = State(2, failing={'Prompt 3'})
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(again))
    execute_once(run)
    summary = refresh_summary(run, *scan_results(run))
    assert summary['units']['failed'] == 2 and summary['units']['not_run'] == 0  # one marker per unit: the latest wins


def test_a_snapshot_that_changed_during_the_run_stops_it_and_stores_no_result_for_that_scenario(
        monkeypatch, tmp_path, project_root, model_test_profile):
    state = State(1, snapshot='s20200101T000000000000Z-00000000')  # not the snapshot the run was prepared with (None here)
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(state))
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(3), concurrency=1, fill=False)
    with pytest.raises(eval_run.RunAborted, match='snapshot changed'):
        execute_once(run)
    assert [row['metrics']['variant'] for row in run.store.read_rows('eval_results')] == []  # the first unit was an on unit
    assert state.abandoned >= 1 and state.delivered == 0  # the response was released without being delivered


def test_the_run_lock_refuses_a_second_holder_at_once_and_is_given_back(tmp_path, project_root, model_test_profile):
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(2), concurrency=1, fill=False)
    first = hold_run(run.store, run.rid, create=True)
    for create in (True, False):  # `eval run` (creates) and `eval import` (never creates) are both refused while it is held
        with pytest.raises(Refusal, match='in use by another process'):
            hold_run(run.store, run.rid, create=create)
    run.lock = first
    release(run)
    release(run)  # safe to call twice
    hold_run(run.store, run.rid, create=False).release()  # free again
    with pytest.raises(Refusal, match='no such run'):
        hold_run(run.store, '0' * 16, create=False)
    assert not (run.store.root / 'eval' / 'runs' / ('0' * 16)).exists()  # asking never creates a run directory


def test_a_result_the_selected_units_cannot_account_for_is_refused_by_the_summary(
        tmp_path, project_root, model_test_profile):
    """`not_run` is never negative: a row of this run for a unit outside the selection is a storage error."""
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(), concurrency=1, split='fit')
    other = next(rec for rec in read_records(run.data, run.catalog) if rec.scenario['split'] == 'test')
    run.store.append('eval_results', [result_row(run, other, 'on', 'stray', 'trace-stray')])
    with pytest.raises(StorageError, match='do not fit the selected units'):
        refresh_summary(run, *scan_results(run))


def test_a_completed_run_makes_no_call_when_executed_again(monkeypatch, tmp_path, project_root, model_test_profile):
    state = State(2)
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(state))
    run, _ = synthetic_run(tmp_path, project_root, model_test_profile, records(4), concurrency=2, fill=False)
    execute_once(run)
    rows = run.store.read_rows('eval_results')
    again = State(2)
    monkeypatch.setattr(eval_run, 'Pipeline', fake_pipeline(again))
    assert execute_once(run)['completed'] == 0 and again.calls == []
    assert run.store.read_rows('eval_results') == rows  # no duplicate row, no new row
