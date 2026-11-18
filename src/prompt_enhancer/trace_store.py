"""Off-response trace append/retention using the existing anchored storage boundary."""
from datetime import datetime, timedelta, timezone
import heapq
import os
from threading import RLock

from .storage import open_data_store
from .storage._anchored import remove_entry
from .storage._fs import read_table_file, real_inside


def trace_expiry(created_at, retention_s):
    """Check datetime representability at the write boundary using the actual timestamp."""
    return created_at + timedelta(seconds=retention_s)


class TraceRepository:
    def __init__(self, config, *, project_root, retention_s, release_hold=None):
        self.store = open_data_store(config, 'enhance once', project_root=project_root)
        self.retention_s, self.release_hold = retention_s, release_hold
        self._lock, self._loaded, self._expiry = RLock(), False, []

    def _load(self):
        if self._loaded:
            return
        relative = self.store._relative('request_traces')
        directory = real_inside(self.store.root, relative)
        if not directory.is_dir():
            self._loaded = True
            return
        entries = []
        for name in sorted(os.listdir(directory)):
            if not name.startswith('.'):
                target = relative + '/' + name
                try:
                    rows = self.store.catalog.rows('request_traces', read_table_file(real_inside(self.store.root, target)))
                    if rows:
                        expiry = trace_expiry(max(row['created_at'] for row in rows), self.retention_s)
                        holds = [(row['snapshot_id'], row['request_id']) for row in rows
                                 if row['snapshot_id'] is not None and row['body'].get('attachments_retained')]
                        entries.append((expiry, target, len(rows), holds))
                except FileNotFoundError:
                    continue
        self._expiry = entries
        heapq.heapify(self._expiry)
        self._loaded = True

    def next_expiry(self):
        with self._lock:
            self._load()
            return self._expiry[0][0] if self._expiry else None

    def expire(self, now=None):
        with self._lock:
            self._load()
            now = now or datetime.now(timezone.utc)
            expired = 0
            while self._expiry and self._expiry[0][0] <= now:
                _, target, count, holds = self._expiry[0]
                try:
                    remove_entry(self.store.root, target)
                except FileNotFoundError:
                    pass
                if self.release_hold is not None:
                    for snapshot, request in holds:
                        self.release_hold(snapshot, request)
                heapq.heappop(self._expiry)
                expired += count
            return expired

    def append(self, row):
        with self._lock:
            expiry = trace_expiry(row['created_at'], self.retention_s)
            holds = [(row['snapshot_id'], row['request_id'])] if row['snapshot_id'] and row['body'].get('attachments_retained') else []
            self._load()  # Read existing segments once per repository lifecycle, never once per request.
            path = self.store.append('request_traces', [row])
            heapq.heappush(self._expiry, (expiry,
                                        path.relative_to(self.store.root).as_posix(), 1, holds))
            return path
