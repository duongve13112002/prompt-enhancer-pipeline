"""Input paths, `--overlay`, the byte cap and attachment housekeeping of the command line (T-016 fix round 1; R-014, R-015,
R-024, R-025). Rules under test: a link or junction on a user-supplied path is refused (never followed, also inside the
root), `--overlay` reaches every new command and a bad one is refused, a validated cap up to `sys.maxsize` is usable, and
expiry housekeeping that fails is counted, never fatal. Labelled doubles: the loopback model server only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from cli_fixtures import (NO_WINDOW, DraftingServer, finish_setup, model_server, pipeline_config, write_request,
                          write_yaml)
from prompt_enhancer import attachments
from prompt_enhancer.__main__ import main
from prompt_enhancer.attachments import attachment_path, expire_attachments, publish_attachments
from prompt_enhancer.cli_input import Refusal, read_bounded, resolve_input
from prompt_enhancer.storage import StorageError, open_data_store
from profile_fixtures import bundle, install_prompts, SECTIONED

GOOD = {'prompt': 'hello', 'task': 'generate'}
COMMANDS = {
    'kb status': ['kb', 'status'], 'kb show': ['kb', 'show', 'Name'],
    'kb rollback': ['kb', 'rollback', 's20200101T000000000000Z-00000000'], 'kb gc': ['kb', 'gc'],
    'profile register': ['profile', 'register', '--profile-id', 'p1'],
    'profile check': ['profile', 'check', '--profile-id', 'p1'], 'enhance once': ['enhance', 'once'],
}


def make_link(link, target):
    """A directory link `link` -> `target`: a junction on Windows (needs no privilege), a symlink elsewhere."""
    if os.name == 'nt':
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True,
                              creationflags=NO_WINDOW)
        made = done.returncode == 0
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
            made = True
        except OSError:
            made = False
    if not made:
        pytest.skip('a directory link cannot be created here')


def drop_link(link):
    if os.path.lexists(link):
        # Removes the link itself, never its target: a junction is removed with rmdir, a POSIX symlink with unlink
        # (rmdir on a symlink is ENOTDIR there: CI window 25 on Ubuntu).
        if os.name == 'nt':
            os.rmdir(link)
        else:
            os.unlink(link)


def run_main(capsys, argv, project_root):
    code = main([*map(str, argv), '--project-root', str(project_root)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree(root):
    return sorted(str(item.relative_to(root)) for item in Path(root).rglob('*'))


def set_path(config, dotted, value):
    *parents, leaf = dotted.split('.')
    node = config
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


def get_path(config, dotted):
    for part in dotted.split('.'):
        config = config[part]
    return config


# ---- links (F2): the request, the configuration and the overlay -------------------------------------------------------

def test_a_request_below_a_linked_directory_is_refused_before_anything_starts(capsys, model_test_profile, tmp_path,
                                                                              project_root):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    path = write_yaml(project_root, tmp_path, config)
    real = tmp_path / 'real'
    real.mkdir()
    write_request(real, GOOD)
    link = tmp_path / 'link'
    make_link(link, real)
    try:
        before = tree(tmp_path)
        code, out, err = run_main(capsys, ['enhance', 'once', '--config', path, '--request', link / 'request.json'],
                                  project_root)
        assert code == 2 and out == '' and err.startswith('refused: --request: is a link'), err
        assert tree(tmp_path) == before and not (tmp_path / 'data').exists()
        assert resolve_input(project_root, real / 'request.json', '--request') == real / 'request.json'  # no link: accepted
    finally:
        drop_link(link)


def test_the_reader_itself_never_follows_a_link_even_when_the_path_rule_is_bypassed(tmp_path, project_root):
    """No gap between checking and opening: `read_bounded` refuses a linked component on its own."""
    real = tmp_path / 'real'
    real.mkdir()
    write_request(real, GOOD)
    link = tmp_path / 'link'
    make_link(link, real)
    try:
        assert json.loads(read_bounded(project_root, real / 'request.json', 1000, '--request')) == GOOD
        with pytest.raises(Refusal, match='--request: is a link'):
            read_bounded(project_root, link / 'request.json', 1000, '--request')
        with pytest.raises(Refusal, match='--request: is a link'):
            resolve_input(project_root, link / 'request.json', '--request')
    finally:
        drop_link(link)


@pytest.mark.skipif(os.name == 'nt', reason='a file symlink needs a privilege that Windows does not grant by default')
def test_a_request_that_is_itself_a_symlink_is_refused(capsys, model_test_profile, tmp_path, project_root):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    path = write_yaml(project_root, tmp_path, config)
    target = write_request(tmp_path, GOOD)
    link = tmp_path / 'req-link.json'
    os.symlink(target, link)
    code, out, err = run_main(capsys, ['enhance', 'once', '--config', path, '--request', link], project_root)
    assert code == 2 and out == '' and err.startswith('refused: --request: is a link')


@pytest.mark.parametrize('command', sorted(COMMANDS))
@pytest.mark.parametrize('which', ['config', 'overlay'])
def test_every_new_command_refuses_a_linked_configuration_or_overlay(capsys, model_test_profile, tmp_path, project_root,
                                                                     command, which):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    real = tmp_path / 'real'
    real.mkdir()
    write_yaml(project_root, real, config)
    (real / 'overlay.yaml').write_text(yaml.safe_dump({'schema_version': 2, 'overlay_of': 'config.yaml'}), encoding='utf-8')
    plain = write_yaml(project_root, tmp_path, config)
    link = tmp_path / 'link'
    make_link(link, real)
    try:
        extra = ['--request', write_request(tmp_path, GOOD)] if command == 'enhance once' else []
        if which == 'config':
            options = ['--config', link / 'config.yaml']
        else:
            options = ['--config', plain, '--overlay', link / 'overlay.yaml']
        before = tree(tmp_path)
        code, out, err = run_main(capsys, [*COMMANDS[command], *options, *extra], project_root)
        assert code == 2 and out == '' and err.startswith(f'refused: --{which}: is a link'), err
        assert tree(tmp_path) == before
    finally:
        drop_link(link)


@pytest.mark.parametrize('failure', [RuntimeError('labelled failure'), MemoryError(), RecursionError()])
def test_an_unexpected_failure_before_the_pipeline_is_exit_one_with_the_type_only(
        monkeypatch, capsys, model_test_profile, tmp_path, project_root, failure):
    """No exception of the request path ends in a traceback: it maps to the documented operational status."""
    from prompt_enhancer import cli_enhance

    def broken(*args):
        raise failure

    monkeypatch.setattr(cli_enhance, 'read_request', broken)
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    path = write_yaml(project_root, tmp_path, config)
    code, out, err = run_main(capsys, ['enhance', 'once', '--config', path, '--request', write_request(tmp_path, GOOD)],
                              project_root)
    assert code == 1 and out == '' and err == f'failed: {type(failure).__name__}\n'


def test_an_interrupt_before_the_pipeline_is_exit_one_without_a_traceback(
        monkeypatch, capsys, model_test_profile, tmp_path, project_root):
    """Ctrl-C while the request is read ends like an interrupt in the pipeline phase (review N3)."""
    from prompt_enhancer import cli_enhance

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_enhance, 'read_request', interrupted)
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    path = write_yaml(project_root, tmp_path, config)
    code, out, err = run_main(capsys, ['enhance', 'once', '--config', path, '--request', write_request(tmp_path, GOOD)],
                              project_root)
    assert code == 1 and out == '' and err == 'interrupted\n'


# ---- the cap (F1): a validated value is a usable value -----------------------------------------------------------------

def test_a_request_cap_of_sys_maxsize_reads_a_normal_request_and_still_delivers(capsys, model_test_profile, tmp_path,
                                                                                 project_root):
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        config['serving']['http']['max_request_bytes'] = sys.maxsize
        finish_setup(config, project_root, handler)
        path = write_yaml(project_root, tmp_path, config)
        code, out, err = run_main(capsys, ['enhance', 'once', '--config', path, '--request',
                                           write_request(tmp_path, GOOD)], project_root)
    assert code == 0, err
    assert json.loads(out)['output'] == 'hello' and 'Error' not in err


@pytest.mark.parametrize('limit', [1, 2 ** 31, 2 ** 62, sys.maxsize])
def test_read_bounded_allocates_nothing_for_a_large_limit_and_refuses_an_oversize_file(tmp_path, project_root, limit):
    path = tmp_path / 'f.bin'
    path.write_bytes(b'x' * 10)
    if limit >= 10:
        assert read_bounded(project_root, path, limit, '--request') == b'x' * 10
    else:
        with pytest.raises(Refusal, match='larger than serving.http.max_request_bytes'):
            read_bounded(project_root, path, limit, '--request')


def test_read_bounded_refuses_a_directory_and_a_missing_file_by_argument_name(tmp_path, project_root):
    for target in (tmp_path, tmp_path / 'absent.json'):
        with pytest.raises(Refusal, match='^--request: '):
            read_bounded(project_root, target, 100, '--request')


# ---- --overlay (F4): it reaches each new command, and a bad one is refused --------------------------------------------

OVERLAY_KEYS = {'kb status': 'storage.kb_store', 'kb show': 'storage.kb_store', 'kb rollback': 'storage.kb_store',
                'kb gc': 'storage.kb_store', 'profile register': 'storage.lock.timeout_s',
                'profile check': 'storage.lock.timeout_s', 'enhance once': 'runtime.request_deadline_s'}


def overlay_file(tmp_path, config, key, value):
    body = {'schema_version': config['schema_version'], 'overlay_of': 'config.yaml'}
    set_path(body, key, value)
    path = tmp_path / 'overlay.yaml'
    path.write_text(yaml.safe_dump(body), encoding='utf-8')
    return path


@pytest.mark.parametrize('command', sorted(COMMANDS))
def test_overlay_supplies_a_value_the_base_profile_leaves_unresolved(capsys, model_test_profile, tmp_path, project_root,
                                                                     command):
    key = OVERLAY_KEYS[command]
    with model_server(DraftingServer) as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        config['storage']['kb_store'] = (tmp_path / 'absent-store').relative_to(project_root).as_posix()
        config['sync']['gc']['retention'] = '1d'  # test-only: the profile's own sentinels must not hide the key under test
        config['sync']['snapshots']['keep'] = 3
        install_prompts(project_root)
        bundle(tmp_path, 'p1', SECTIONED)
        wanted = deepcopy(get_path(config, key))
        set_path(config, key, 'deploy_required')
        path = write_yaml(project_root, tmp_path, config)
        extra = ['--request', write_request(tmp_path, GOOD)] if command == 'enhance once' else []
        argv = [*COMMANDS[command], '--config', path, *extra]
        code, out, err = run_main(capsys, argv, project_root)
        assert code == 2 and 'deploy_required' in err and key.split('.')[-1] in err, err  # refused without the overlay
        overlay = overlay_file(tmp_path, config, key, wanted)
        code, out, err = run_main(capsys, [*argv, '--overlay', overlay], project_root)
    assert 'deploy_required' not in err and 'Traceback' not in err, err  # the overlay's value reached the command
    if command == 'kb status':
        assert code == 0 and json.loads(out)['store_present'] is False
    elif command in ('kb rollback', 'kb gc'):
        assert code == 2 and 'no knowledge store' in err  # past configuration: the overlay's store path was the one used
    elif command == 'kb show':
        assert code == 2 and 'no published snapshot' in err
    elif command == 'profile register':
        assert code == 0 and json.loads(out)['command'] == 'profile register'
    elif command == 'enhance once':
        assert code == 0 and json.loads(out)['status'] == 'failed'  # no active profile: an honest failed envelope


@pytest.mark.parametrize('command', sorted(COMMANDS))
@pytest.mark.parametrize('flaw', ['not_an_overlay_key', 'wrong_base', 'missing_file'])
def test_a_bad_overlay_is_refused_by_every_new_command_before_any_side_effect(capsys, model_test_profile, tmp_path,
                                                                              project_root, command, flaw):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    path = write_yaml(project_root, tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    if flaw == 'not_an_overlay_key':
        overlay.write_text(yaml.safe_dump({'schema_version': 2, 'overlay_of': 'config.yaml', 'knowledge': {}}),
                           encoding='utf-8')
    elif flaw == 'wrong_base':
        overlay.write_text(yaml.safe_dump({'schema_version': 2, 'overlay_of': 'other.yaml'}), encoding='utf-8')
    extra = ['--request', write_request(tmp_path, GOOD)] if command == 'enhance once' else []
    before = tree(tmp_path)
    code, out, err = run_main(capsys, [*COMMANDS[command], '--config', path, '--overlay', overlay, *extra], project_root)
    assert code == 2 and out == '' and err.startswith('refused: ') and 'overlay' in err, err
    assert tree(tmp_path) == before


# ---- attachment housekeeping (F3, F4) -----------------------------------------------------------------------------------

def store_of(model_test_profile, tmp_path, project_root):
    return open_data_store(pipeline_config(model_test_profile, tmp_path, project_root, 1), 'enhance once',
                           project_root=project_root)


def test_an_unreadable_attachments_listing_is_counted_and_never_stops_the_request(
        monkeypatch, capsys, model_test_profile, tmp_path, project_root):
    base = tmp_path / 'data' / 'attachments'
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        finish_setup(config, project_root, handler)
        store = open_data_store(config, 'enhance once', project_root=project_root)
        data = b'labelled bytes'
        image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
        publish_attachments(store, 'a' * 32, {image_id: data}, datetime.now(timezone.utc) + timedelta(seconds=60))
        real_listdir = os.listdir

        def listdir(path='.'):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(str(base)):
                raise PermissionError('labelled unreadable directory')
            return real_listdir(path)

        monkeypatch.setattr(attachments.os, 'listdir', listdir)
        assert expire_attachments(store) == (0, 1)
        path = write_yaml(project_root, tmp_path, config)
        code, out, err = run_main(capsys, ['enhance', 'once', '--config', path, '--request',
                                           write_request(tmp_path, GOOD)], project_root)
    assert code == 0, err
    assert 'warning: expired attachment cleanup failed for 1 entry' in err and json.loads(out)['output'] == 'hello'


def test_expiry_counts_each_outcome_exactly(model_test_profile, tmp_path, project_root):
    store = store_of(model_test_profile, tmp_path, project_root)
    base = tmp_path / 'data' / 'attachments'
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    data = b'labelled bytes'
    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
    publish_attachments(store, 'live' + 'a' * 28, {image_id: data}, soon)
    stray = base / ('b' * 32)
    stray.mkdir()
    (stray / 'stray').write_bytes(b'x')
    assert expire_attachments(store) == (0, 0)  # a live set and a non-empty unindexed directory: nothing removed, nothing failed
    assert (stray / 'stray').is_file()
    (stray / 'stray').unlink()
    assert expire_attachments(store) == (1, 0) and not stray.exists()  # an empty unindexed directory is removed
    broken = base / ('c' * 32)
    broken.mkdir()
    (broken / 'index.json').write_bytes(b'{not json')
    assert expire_attachments(store) == (0, 1) and broken.is_dir()  # an unreadable index is a failure and is left alone
    assert expire_attachments(store, now=soon + timedelta(seconds=1)) == (1, 1)  # the live set expired; the broken one stays
    assert not (base / ('live' + 'a' * 28)).exists() and broken.is_dir()


@pytest.mark.parametrize('trace_id', ['', '../x', 'a/b', 'a\\b', 'a:b', 'a.b', 'x' * 129, 'sp ace', 'é'])
def test_the_trace_id_is_checked_before_a_path_is_built(trace_id):
    image_id = 'sha256:' + 'a' * 64
    with pytest.raises(StorageError, match='invalid request id'):
        attachment_path(trace_id, image_id)
    assert attachment_path('t' * 32, image_id) == f"attachments/{'t' * 32}/{'a' * 64}"
