"""End-to-end K0 tests with synthetic source and downstream snapshot doubles."""

from threading import Barrier

import pytest

from prompt_enhancer import k0_scan
from prompt_enhancer import __main__ as cli
from prompt_enhancer.k0_sync import sync
from prompt_enhancer.storage.store import Checkpoints, PinnedSnapshot
from storage_fixtures import make_store
from test_k0_checkpoint import publish_synthetic_snapshot


@pytest.fixture
def fixture(project_root, tmp_path):
    source = tmp_path / 'source'
    folder = source / 'folder'
    folder.mkdir(parents=True)
    (folder / 'info.txt').write_text('Kimono\ndescription', encoding='utf-8')
    (folder / 'image.jpg').write_bytes(b'synthetic image')
    store = make_store(project_root, tmp_path / 'kb')
    return store, source, folder


def run(store, source, **options):
    args = dict(info_names=['info.txt'], encoding='utf-8', extensions=['.jpg'],
                separator='|', workers=2, threshold=0.5)
    args.update(options)
    return sync(store, source, **args)


def tree(root):
    if not root.exists():
        return {}
    return {path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob('*')}


@pytest.mark.parametrize('fail', [False, True])
def test_plan_leaves_store_tree_unchanged_even_on_snapshot_read_error(fixture, monkeypatch, fail):
    store, source, _ = fixture
    publish_synthetic_snapshot(store, source)
    before = tree(store.root)
    if fail:
        def fail_read(*args):  # labelled snapshot read failure double
            raise RuntimeError('injected read failure')
        monkeypatch.setattr(PinnedSnapshot, 'read_rows', fail_read)
        with pytest.raises(RuntimeError, match='injected'):
            run(store, source, plan=True)
    else:
        result = run(store, source, plan=True)
        assert result['plan']['folders']['removed'] == []
    assert tree(store.root) == before


def test_plan_new_store_does_not_create_it(fixture):
    store, source, _ = fixture
    result = run(store, source, plan=True)
    assert not store.root.exists()
    assert result['plan']['folders']['added'] == ['folder']
    assert result['handoff']['k1']['images'] == result['rows']['images']
    assert result['handoff']['k2']['folders'] == result['rows']['folders']


def test_plan_empty_existing_store_preserves_tree(fixture):
    store, source, _ = fixture
    store.root.mkdir()
    before = tree(store.root)
    run(store, source, plan=True)
    assert tree(store.root) == before


def test_plan_only_creates_transient_lease_while_snapshot_is_pinned(fixture, monkeypatch):
    store, source, _ = fixture
    publish_synthetic_snapshot(store, source)
    before = tree(store.root)
    original = PinnedSnapshot.read_rows
    observed = []

    def inspect(self, name):  # labelled observation double
        during = {path.relative_to(store.root).as_posix():
                  None if path.is_dir() else b'<locked lease>' if path.suffix == '.lease' else path.read_bytes()
                  for path in store.root.rglob('*')}
        added = set(during) - set(before)
        assert all(path == 'leases' or path == f'leases/{self.snapshot_id}' or
                   path.startswith(f'leases/{self.snapshot_id}/') and path.endswith('.lease')
                   for path in added)
        assert any(path.endswith('.lease') for path in added)
        assert all(during[path] == value for path, value in before.items())
        observed.append(added)
        return original(self, name)

    monkeypatch.setattr(PinnedSnapshot, 'read_rows', inspect)
    run(store, source, plan=True)
    assert observed
    assert tree(store.root) == before


@pytest.mark.parametrize('fail', [False, True])
def test_cli_plan_preserves_store_tree(fixture, monkeypatch, capsys, fail):
    store, source, _ = fixture
    publish_synthetic_snapshot(store, source)
    before = tree(store.root)
    monkeypatch.setattr(cli, 'load_config', lambda *args, **kwargs: {})

    def command(config, *, project_root, plan, force):  # labelled configuration-loader double
        assert plan and not force
        return run(store, source, plan=plan, force=force)

    monkeypatch.setattr(cli, 'sync_config', command)
    if fail:
        def fail_read(*args):
            raise OSError('injected snapshot failure')
        monkeypatch.setattr(PinnedSnapshot, 'read_rows', fail_read)
    assert cli.main(['kb', 'sync', '--config', 'fixture.yaml', '--plan']) == int(fail)
    output = capsys.readouterr()
    assert ('injected snapshot failure' in output.err) if fail else ('"plan"' in output.out)
    assert tree(store.root) == before


def test_guard_force_and_current_are_preserved(fixture):
    store, source, folder = fixture
    snapshot = publish_synthetic_snapshot(store, source)
    (folder / 'info.txt').unlink()
    (folder / 'image.jpg').unlink()
    folder.rmdir()
    report = run(store, source, plan=True)['report']['guard']
    assert (report['removed'], report['previous'], report['fraction'], report['exceeded']) == (1, 1, 1, True)
    with pytest.raises(ValueError, match='mass deletion guard'):
        run(store, source)
    forced = run(store, source, force=True)
    assert forced['report']['guard']['forced']
    assert store.current()[0] == snapshot


def test_interruption_converges(fixture, monkeypatch):
    store, source, _ = fixture
    original = Checkpoints.commit
    calls = []

    def interrupt(self, *args, **kwargs):  # labelled interruption double
        result = original(self, *args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('interrupted')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', interrupt)
        with pytest.raises(RuntimeError, match='interrupted'):
            run(store, source)
    first = run(store, source)
    assert run(store, source) == first
    assert store.current() is None


@pytest.mark.parametrize('plan', [True, False])
def test_configured_pool_runs_two_reads_concurrently(fixture, monkeypatch, plan):
    store, source, _ = fixture
    barrier = Barrier(2, timeout=5)
    original = k0_scan._read

    def observed(*args, **kwargs):  # labelled concurrency observation double
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(k0_scan, '_read', observed)
    result = run(store, source, plan=plan, workers=2)
    assert result['report']['workers'] == 2


def test_source_configuration_overrides_change_rows(fixture, tmp_path):
    store, source, folder = fixture
    (folder / 'alternate.txt').write_bytes('Café;Tea\nfirst;second'.encode('latin-1'))
    (folder / 'new.PNG').write_bytes(b'alternative image')
    result = run(store, source, plan=True, info_names=['alternate.txt', 'info.txt'],
                 encoding='latin-1', extensions=['.png'], separator=';', workers=1)
    assert result['rows']['folders'][0]['declared_names'] == ['Café', 'Tea']
    assert [row['relative_path'] for row in result['rows']['folder_images']] == ['new.PNG']
    assert result['report']['workers'] == 1
    other = tmp_path / 'other-source'
    (other / 'different').mkdir(parents=True)
    assert run(store, other, plan=True)['plan']['folders']['added'] == ['different']


@pytest.mark.parametrize('path_form', ['native', 'forward_slashes'])
def test_source_path_forms_preserve_folder_id(fixture, path_form):
    store, source, _ = fixture
    (source / 'Áo dài').mkdir()
    root = str(source) if path_form == 'native' else source.as_posix()
    result = run(store, root, plan=True)
    assert 'Áo dài' in [row['folder_id'] for row in result['rows']['folders']]


def test_sync_reports_nested_refused_and_description_mismatch(fixture):
    store, source, folder = fixture
    (folder / 'nested').mkdir()
    (folder / 'info.txt').write_text('A|B\nshared description', encoding='utf-8')
    duplicate = source / 'duplicate'
    duplicate.mkdir()
    (duplicate / 'info.txt').write_text('A|a\ntext', encoding='utf-8')
    report = run(store, source, plan=True)['report']
    assert report['nested'] == ['folder/nested']
    assert report['refused_folders'] == ['duplicate']
    assert report['description_mismatch'] == ['folder']


def test_ambiguous_moves_are_reported_and_count_as_removals(fixture):
    store, source, folder = fixture
    duplicate = source / 'duplicate'
    duplicate.mkdir()
    (duplicate / 'info.txt').write_bytes((folder / 'info.txt').read_bytes())
    (duplicate / 'image.jpg').write_bytes((folder / 'image.jpg').read_bytes())
    publish_synthetic_snapshot(store, source)
    folder.rename(source / 'new-one')
    duplicate.rename(source / 'new-two')
    result = run(store, source, plan=True)
    assert result['report']['ambiguous_matches']['folders'] == ['duplicate', 'folder']
    assert len(result['report']['ambiguous_matches']['images']) == 1
    assert result['report']['guard']['removed'] == 2
    assert result['report']['guard']['fraction'] == 1


@pytest.mark.parametrize('workers', [0, -1, True, 'unknown'])
def test_workers_refused_before_scanning(fixture, workers):
    store, source, _ = fixture
    with pytest.raises(ValueError, match='runtime.cpu_workers'):
        run(store, source, plan=True, workers=workers)


@pytest.mark.parametrize('threshold', [-1, 1.1, float('nan'), True])
def test_guard_threshold_refused(fixture, threshold):
    store, source, _ = fixture
    with pytest.raises(ValueError, match='guard fraction'):
        run(store, source, plan=True, threshold=threshold)
