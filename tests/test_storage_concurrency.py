"""Bounded concurrent readers, publishers and garbage collection across processes and threads (R-014)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from storage_fixtures import make_store, publish, tag_of

from prompt_enhancer.storage import StorageError

CHILD = Path(__file__).with_name('storage_child.py')
SRC = Path(__file__).resolve().parents[1] / 'src'
TIMEOUT_S = 180  # test-only bound per child; a hang fails the test instead of blocking the suite


def spawn(mode, project_root, kb, count):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), str(CHILD.parent)]), PYTHONDONTWRITEBYTECODE='1')
    # Footer rule 7: no console window for the venv redirector's interpreter child.
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.Popen([sys.executable, '-X', 'faulthandler', str(CHILD), mode, str(project_root), str(kb),
                             str(count)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            creationflags=flags)


def finish(process):
    try:
        out, err = process.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        raise AssertionError(f'child hung: {err[-2000:]}')
    assert process.returncode == 0, err[-2000:]
    return json.loads(out.strip().splitlines()[-1])


def test_readers_publishers_and_gc_in_processes(project_root, tmp_path):
    kb = tmp_path / 'kb'
    store = make_store(project_root, kb, keep=0)
    publish(store, 'p-initial')
    children = [spawn('reader', project_root, kb, 25), spawn('reader', project_root, kb, 25),
                spawn('publisher', project_root, kb, 4), spawn('gc', project_root, kb, 4)]
    results = [finish(child) for child in children]
    for result in results:
        assert result['errors'] == [], result
    current = store.current()  # CURRENT parses and names a complete, published snapshot
    with store.pin() as pinned:
        assert pinned.snapshot_id == current[0] and len(tag_of(pinned)) == 1
    # Evidence for a later storage.lock value: wall time of pin() and publish(), an upper bound of the lock hold.
    times = {kind: sorted(t for r in results if r['mode'] == kind for t in r['hold_s']) for kind in ('reader', 'publisher')}
    assert len(times['reader']) == 50 and len(times['publisher']) == 4
    print('lock hold times', json.dumps({kind: {'n': len(v), 'median_s': round(v[len(v) // 2], 4),
                                                'max_s': round(v[-1], 4)} for kind, v in times.items()}))


def test_killed_lease_holder_becomes_stale(project_root, tmp_path):
    kb = tmp_path / 'kb'
    store = make_store(project_root, kb, keep=0)
    old = publish(store, 'a')
    child = spawn('hold_lease', project_root, kb, 1)
    try:
        line = child.stdout.readline()
        assert line.startswith('PINNED ' + old), child.stderr.read()[-2000:] if not line else line
        publish(store, 'b')
        with store.writer() as writer:
            report = writer.gc()
        assert report.leased == [old] and old not in report.collected  # a live reader in another process
    finally:
        child.kill()
        child.communicate(timeout=TIMEOUT_S)
    with store.writer() as writer:
        report = writer.gc()
    assert report.collected == [old] and not report.leased


def test_threads_pin_while_publishing(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    publish(store, 't-initial')
    errors = []

    def reader():
        for _ in range(15):
            try:
                with store.pin() as pinned:
                    if len(tag_of(pinned)) != 1:
                        errors.append('mixed view')
            except StorageError as error:
                errors.append(str(error))

    def publisher():
        try:  # fix round 3: a lease sharing violation in GC used to escape this thread as a PermissionError
            for index in range(3):
                publish(store, f't{index}')
                with store.writer() as writer:
                    writer.gc()
        except Exception as error:
            errors.append(f'publisher: {type(error).__name__}: {error}')

    threads = [threading.Thread(target=reader) for _ in range(4)] + [threading.Thread(target=publisher)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT_S)
        assert not thread.is_alive()
    assert errors == []
