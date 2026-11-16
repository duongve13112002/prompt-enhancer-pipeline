"""Diagnostic channels exclude model content; valid data artifacts are deliberately excluded."""

import json
import traceback

import pytest

from k2_fixtures import folder
from kb_sync_fixtures import SyncServer, invoke
from prompt_enhancer.storage import StorageError


@pytest.mark.parametrize(('role', 'stage'), [('info_assessor', 'k2_naming'), ('folder_namer', 'k2_naming'),
    ('observer', 'k2_observation'), ('card_builder', 'k3'), ('alias_generator', 'k4')])
def test_model_failures_have_no_text_in_reports_state_or_cli(k2_env, monkeypatch, capsys, role, stage):
    from prompt_enhancer import __main__ as cli
    marker = 'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS'
    folder(k2_env, 'f', None if role == 'folder_namer' else 'Name\ndescription')
    server = SyncServer()
    server.fail_role = role
    with pytest.raises(StorageError, match=stage + ': STAGE_FAILED') as caught:
        invoke(k2_env, server)
    assert marker not in ''.join(traceback.format_exception(caught.value))
    with k2_env.store.writer() as writer:
        failures = [row for row in writer._state_rows().values() if row['state'] == 'failed']
        assert failures and all(row['error'] == 'SCHEMA_INVALID' for row in failures)
        assert marker not in json.dumps(failures, default=str)
        for row in writer._state_rows().values():
            if row['stage'] in ('k2_call_failure', 'k3_call_failure', 'k4_call_failure'):
                assert marker not in writer.checkpoints(row['stage']).load(row['input_key']).decode()
    monkeypatch.setattr(cli, 'load_config', lambda *a, **k: k2_env.config)
    monkeypatch.setattr(cli, 'sync_config', lambda *a, **k: invoke(k2_env, server))
    assert cli.main(['kb', 'sync', '--config', 'labelled-double.yaml']) == 1
    assert marker not in capsys.readouterr().err


@pytest.mark.parametrize('stage', ['k2_assessment', 'k2_naming', 'k2_observation', 'k3_grouping', 'k4_generation'])
def test_cached_model_text_never_enters_diagnostics(k2_env, stage):
    folder(k2_env, 'f', None if stage == 'k2_naming' else 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    marker = 'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS'
    # Labelled corrupt-cache double at the verified load seam; stored referenced bytes stay intact.
    from prompt_enhancer.storage.store import Checkpoints
    original = Checkpoints.load
    from unittest.mock import patch

    def malformed(checkpoints, key):
        raw = original(checkpoints, key)
        if checkpoints.stage != stage:
            return raw
        bundle = json.loads(raw)
        bundle['reply'] = {marker: marker}
        return json.dumps(bundle).encode()

    k2_env.config['runtime']['cpu_workers'] = 1  # changes completion identity, preserves semantic item keys
    with patch.object(Checkpoints, 'load', malformed):
        try:
            invoke(k2_env, server)
        except StorageError as error:
            rendered = ''.join(traceback.format_exception(error))
            assert marker not in rendered
            assert 'CACHE_INVALID' in rendered or 'STAGE_FAILED' in rendered
        else:
            pytest.fail('malformed cached model reply accepted')
