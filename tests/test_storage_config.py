"""Override and refusal per storage setting, read sets, and the structural RAG-off test (R-015, R-009)."""

from pathlib import Path
import sys
import time
import uuid

import pytest
import yaml
from storage_fixtures import data_rows, publish

from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer.config import _command_reads
from prompt_enhancer.storage import StorageError, open_data_store, open_knowledge_store
from prompt_enhancer.storage._fs import FileLock

EXAMPLE = Path(__file__).resolve().parents[1] / 'configs' / 'enhancer.example.yaml'


@pytest.fixture
def config():
    result = yaml.safe_load(EXAMPLE.read_bytes())
    # test-only values for keys the example leaves at deploy_required/design_required
    result['storage'].update(kb_store=f'data/kb-{uuid.uuid4().hex[:8]}', lock={'timeout_s': 5, 'poll_s': 0.01})
    result['sync'].update(snapshots={'keep': 1}, gc={'retention': '0s'})
    return result


def load(project_root, config, command):
    path = project_root / f'config-{uuid.uuid4().hex[:8]}.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return load_config(path, command, project_root=project_root)


def kb(project_root, config, command='kb gc'):
    return open_knowledge_store(load(project_root, config, command), command, project_root=project_root)


def test_kb_store_override_and_refusal(project_root, config):
    config['storage']['kb_store'] = 'data/alternative-kb'
    store = kb(project_root, config)
    assert store.root == (project_root / 'data' / 'alternative-kb').resolve()
    publish(store, 'a')
    assert (store.root / 'CURRENT').exists()
    for bad, message in (('../outside', 'outside project root'), ('.secrets/kb', 'protected'),
                         ('models/kb', 'protected'), ('deploy_required', 'unresolved sentinel')):
        config['storage']['kb_store'] = bad
        with pytest.raises(ConfigurationError, match=message):
            load(project_root, config, 'kb status')
    loaded = load(project_root, dict(config, storage=dict(config['storage'], kb_store='data/kb')), 'kb status')
    loaded['storage']['kb_store'] = 'design_required'  # a dict that bypassed the loader is refused too
    with pytest.raises(StorageError, match='unresolved sentinel'):
        open_knowledge_store(loaded, 'kb status', project_root=project_root)


def test_data_root_override_and_refusal(project_root, config):
    config['rag']['enabled'] = False
    config['storage']['data_root'] = 'data/alternative-data'
    loaded = load(project_root, config, 'profile check')  # validated whole; the store checks the keys it reads
    data = open_data_store(loaded, 'enhance once', project_root=project_root)
    path = data.append('request_traces', data_rows()['request_traces'])
    assert path.is_relative_to((project_root / 'data' / 'alternative-data' / 'traces').resolve())
    assert data.read_rows('request_traces')[0]['body'] == {'prompt': 'vẽ một con mèo 猫'}
    with pytest.raises(StorageError, match='duplicate key'):
        data.append('request_traces', data_rows()['request_traces']) and data.read('request_traces')
    config['storage']['data_root'] = '../elsewhere'
    with pytest.raises(ConfigurationError, match='storage.data_root'):
        load(project_root, config, 'profile check')
    loaded['storage']['data_root'] = 'fit_from_bench'
    with pytest.raises(StorageError, match='unresolved sentinel'):
        open_data_store(loaded, 'enhance once', project_root=project_root)
    with pytest.raises(StorageError, match='does not read storage.data_root'):
        open_data_store(loaded, 'kb gc', project_root=project_root)


def test_table_format_override_and_refusal(project_root, config):
    config['storage']['table_format'] = 'arrow'
    store = kb(project_root, config, 'kb rollback')
    publish(store, 'a')
    assert (store.root / 'registry' / 'REGISTRY').read_text().strip().endswith('.arrow')
    assert (store.root / 'snapshots' / store.current()[0] / 'folders.arrow').exists()
    for bad in ('csv', 'Parquet', 'deploy_required'):
        config['storage']['table_format'] = bad
        with pytest.raises(ConfigurationError, match='storage.table_format'):
            load(project_root, config, 'kb gc')
    status = kb(project_root, dict(config, storage=dict(config['storage'], table_format='parquet')), 'kb status')
    with pytest.raises(StorageError, match='storage.table_format is not in the read set'):
        with status.writer() as writer:
            writer.begin_snapshot()


def test_lock_timeout_override_and_refusal(project_root, config):
    config['storage']['lock'] = {'timeout_s': 0.2, 'poll_s': 0.05}
    store = kb(project_root, config, 'kb status')
    holder = FileLock(store.root / 'LOCK', 1, 0.01)
    holder.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(StorageError, match='timeout_s=0.2'):
            store.snapshots() if store.root.exists() else store.store_lock().acquire()
        assert 0.15 <= time.monotonic() - started < 2
    finally:
        holder.release()
    for bad in (0, -1, 'soon', None):
        config['storage']['lock'] = {'timeout_s': bad, 'poll_s': 0.01}
        with pytest.raises(ConfigurationError, match='storage.lock'):
            load(project_root, config, 'kb status')
    config['storage']['lock'] = {'poll_s': 0.01}
    with pytest.raises(ConfigurationError, match='storage.lock.timeout_s'):
        load(project_root, config, 'kb status')
    config['storage']['lock'] = {'timeout_s': 'deploy_required', 'poll_s': 0.01}
    with pytest.raises(ConfigurationError, match='storage.lock.timeout_s: unresolved sentinel deploy_required'):
        load(project_root, config, 'kb status')


def test_lock_poll_override_and_refusal(project_root, config):
    config['storage']['lock'] = {'timeout_s': 3, 'poll_s': 0.5}
    store = kb(project_root, config, 'kb status')
    assert store.poll_s == 0.5
    holder = FileLock(store.root / 'LOCK', 1, 0.01)
    holder.acquire()
    release = __import__('threading').Timer(0.3, holder.release)
    release.start()
    with store.store_lock():  # acquired on a later poll, after the holder let go
        pass
    release.join()
    for bad in (0, 'deploy_required'):
        config['storage']['lock'] = {'timeout_s': 3, 'poll_s': bad}
        with pytest.raises(ConfigurationError, match='storage.lock.poll_s'):
            load(project_root, config, 'kb status')
    config['storage']['lock'] = {'timeout_s': 1, 'poll_s': 2}
    with pytest.raises(StorageError, match='poll_s must not exceed'):
        kb(project_root, config, 'kb status')


def test_keep_override_and_refusal(project_root, config):
    config['sync']['snapshots'] = {'keep': 2}
    store = kb(project_root, config)
    ids = []
    for tag in 'abcd':
        ids.append(publish(store, tag))
        time.sleep(0.002)  # distinct publish times order the retired snapshots
    with store.writer() as writer:
        report = writer.gc()
    assert report.kept == [ids[2], ids[1]] and report.collected == [ids[0]]
    for bad in (-1, 1.5, 'design_required'):
        config['sync']['snapshots'] = {'keep': bad}
        with pytest.raises(ConfigurationError, match='sync.snapshots.keep'):
            load(project_root, config, 'kb gc')


def test_retention_override_and_refusal(project_root, config):
    config['sync']['gc'] = {'retention': '1d'}
    store = kb(project_root, config)
    assert store._retention_s == 86400
    for bad in ('abc', '5', '-1d', '1w', 'design_required'):
        config['sync']['gc'] = {'retention': bad}
        with pytest.raises(ConfigurationError, match='sync.gc.retention'):
            load(project_root, config, 'kb gc')
    loaded = load(project_root, dict(config, sync=dict(config['sync'], gc={'retention': '0s'})), 'kb gc')
    loaded['sync']['gc']['retention'] = '1d\n'  # the schema pattern's $ accepts a final newline; the store does not
    with pytest.raises(StorageError, match='sync.gc.retention'):
        open_knowledge_store(loaded, 'kb gc', project_root=project_root)


def test_resume_override_and_refusal(project_root, config):
    loaded = load(project_root, config, 'kb gc')
    loaded['sync']['resume'] = False
    store = open_knowledge_store(loaded, 'kb sync', project_root=project_root)
    assert store._resume is False
    loaded['sync']['resume'] = 'design_required'
    with pytest.raises(StorageError, match='sync.resume: unresolved sentinel'):
        open_knowledge_store(loaded, 'kb sync', project_root=project_root)
    loaded['sync']['resume'] = 'yes'
    with pytest.raises(StorageError, match='sync.resume: invalid value'):
        open_knowledge_store(loaded, 'kb sync', project_root=project_root)
    gc_store = open_knowledge_store(load(project_root, config, 'kb gc'), 'kb gc', project_root=project_root)
    with gc_store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        checkpoints.commit('a' * 64, 'x', b'1', revisions={'model': 'm', 'prompt': 'p', 'schema': 's'})
        with pytest.raises(StorageError, match='sync.resume is not in the read set'):
            checkpoints.done('a' * 64)


def test_read_sets_declare_storage_keys(config):
    for command in ('kb status', 'kb sync', 'kb rollback', 'kb gc', 'kb audit', 'kb show', 'eval build'):
        assert 'storage.lock' in _command_reads(command, config)
    for command in ('kb sync', 'kb rollback', 'kb gc', 'enhance once', 'enhance serve', 'eval build', 'eval run'):
        assert 'storage.table_format' in _command_reads(command, config)
    config['rag']['enabled'] = True
    assert {'storage.kb_store', 'storage.lock'} <= set(_command_reads('enhance once', config))
    config['rag']['enabled'] = False
    assert not {'storage.kb_store', 'storage.lock'} & set(_command_reads('enhance once', config))


def test_rag_off_is_structural(project_root, config):
    """With rag.enabled false no knowledge store exists and nothing under kb_store is touched (R-009)."""
    config['rag']['enabled'] = False
    config['storage']['kb_store'] = 'data/absent-kb'
    loaded = load(project_root, config, 'profile check')
    absent = (project_root / 'data' / 'absent-kb').resolve()
    touched = []
    active = [True]

    def audit(event, args):
        if active[0] and event in ('open', 'os.listdir', 'os.mkdir', 'os.scandir') and args and args[0] is not None:
            path = args[0]
            if isinstance(path, (str, bytes, Path)) and str(absent) in str(Path(str(path)).resolve()):
                touched.append((event, path))

    sys.addaudithook(audit)  # cannot be removed; `active` switches it off after the test
    try:
        with pytest.raises(StorageError, match='rag.enabled is false'):
            open_knowledge_store(loaded, 'enhance once', project_root=project_root)
        data = open_data_store(loaded, 'enhance once', project_root=project_root)
        data.append('request_traces', data_rows()['request_traces'])
        assert data.read_rows('request_traces')[0]['retrieval_state'] == 'failed'
    finally:
        active[0] = False
    assert touched == [] and not absent.exists()
