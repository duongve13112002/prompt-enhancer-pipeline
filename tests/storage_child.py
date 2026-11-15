"""Child process for the storage concurrency tests (test-only). Usage: storage_child.py <mode> <project> <kb> <n>."""

import faulthandler
import json
import sys
import time

faulthandler.enable()
from storage_fixtures import build_snapshot, make_store, tag_of  # noqa: E402

from prompt_enhancer.storage import StorageError  # noqa: E402


def main():
    mode, project, kb, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    store = make_store(project, kb, timeout_s=60, poll_s=0.005, keep=0)
    result = {'mode': mode, 'errors': [], 'hold_s': []}
    if mode == 'reader':
        for _ in range(count):
            started = time.perf_counter()
            pinned = store.pin()
            result['hold_s'].append(time.perf_counter() - started)  # upper bound of the store-lock hold
            try:
                tags = tag_of(pinned)
                if len(tags) != 1:
                    result['errors'].append(f'mixed view {sorted(tags)} in {pinned.snapshot_id}')
            except StorageError as error:
                result['errors'].append(str(error))
            finally:
                pinned.release()
    elif mode == 'publisher':
        for index in range(count):
            with store.writer() as writer:
                sid = build_snapshot(writer, f'p{index}')
                started = time.perf_counter()
                writer.publish(sid)
                result['hold_s'].append(time.perf_counter() - started)
    elif mode == 'gc':
        for _ in range(count):
            with store.writer() as writer:
                report = writer.gc()
                result.setdefault('collected', []).extend(report.collected)
    elif mode == 'hold_lease':
        pinned = store.pin()
        print('PINNED ' + pinned.snapshot_id, flush=True)
        time.sleep(120)  # killed by the parent
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
