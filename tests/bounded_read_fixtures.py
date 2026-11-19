"""A labelled test double for bounded reads: a file that grows while it is being read, with no sleep and no thread."""
import os


def grow_after_first_read(monkeypatch, path, extra):
    """The first `os.read` on `path` returns normally, then the file grows by `extra` bytes (test double, hook-driven).

    Returns the list of block sizes the reader received, so a test can show how far past its bound the reader went."""
    real, received = os.read, []

    def read(fd, count):
        block = real(fd, count)
        try:
            ours = os.path.samestat(os.fstat(fd), os.stat(path))
        except OSError:
            ours = False
        if ours:
            received.append(len(block))
            if len(received) == 1:
                with open(path, 'ab') as handle:
                    handle.write(b'g' * extra)
        return block

    monkeypatch.setattr(os, 'read', read)
    return received
