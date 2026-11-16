"""Command line: `python -m prompt_enhancer.embed_server serve --config <profile.yaml> [--overlay <file>]`.

The server reads everything from the profile (`embed serve` read set); a refusal prints one line on stderr and exits 2.
SIGINT, SIGTERM and (Windows) SIGBREAK stop it cleanly and free the GPU. Once the profile is read, the last line (a
refusal, an incomplete stop or an unexpected error) is written within `server.read_timeout_s`, so a full stderr pipe
cannot hold the exit, and no exception reaches Python's own traceback printing (fix round 5).

Before the profile is read nothing is owned (no thread, no listener, no engine) and no window exists: `argparse`
output and the configuration refusal are written directly, the one accepted unbounded write (PM decision pending user
review).
"""

import argparse
from pathlib import Path
import signal
import sys
import threading

from ..config import ConfigurationError
from .manifest import ManifestError, verify_model
from .models import ServerStartError, load_engine
from .server import BoundedOutput, ShutdownIncomplete, serve
from .settings import load_server_settings

REFUSED = 2  # protocol-bound: exit status of a configuration, manifest or startup refusal
INCOMPLETE = 3  # protocol-bound: exit status when a stop left a resource live (a handler, a socket, an admitted request, the engine)
FAILED = 1  # protocol-bound: exit status of an unexpected error once the profile is read (Python's own uncaught status)


def _loader(settings):
    directory = verify_model(settings.project_root, settings.model_id, settings.revision)
    return load_engine(settings, directory)


def _project_root(value):
    return None if value is None else Path(value)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m prompt_enhancer.embed_server')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('serve', help='serve /v1/embeddings for the profile\'s embedding backend')
    run.add_argument('--config', required=True, help='profile path, relative paths start at the project root')
    run.add_argument('--overlay', default=None, help='optional deployment overlay of the profile')
    run.add_argument('--project-root', default=None, help='project root (default: the root of this checkout)')
    args = parser.parse_args(argv)
    try:
        settings = load_server_settings(args.config, args.overlay, _project_root(args.project_root))
    except ConfigurationError as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    try:
        return _serve(settings)
    except (Exception, KeyboardInterrupt) as error:  # the type only: a message may carry a path or request content
        return _last_line(f'failed: {type(error).__name__}', settings, FAILED)


def _serve(settings):
    stop = threading.Event()
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):  # SIGBREAK exists on Windows only
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda *_: stop.set())
    try:
        serve(settings, _loader, stop=stop)
    except (ManifestError, ServerStartError, OSError) as error:  # OSError: the port cannot be bound
        return _last_line(f'refused: {error}', settings, REFUSED)
    except ShutdownIncomplete as error:  # the process exit that follows frees what is still live
        return _last_line(f'stopped incompletely: {error}', settings, INCOMPLETE)
    return 0


def _last_line(line, settings, status):
    """Write the command's last line on stderr without letting a reader that does not read hold the exit status: the
    write gets at most `server.read_timeout_s` (fix round 4, RT3F1), then `status` is returned and the exit ends the
    writer thread. Never raises (fix round 5, RT4F2): the line is best effort, the status is the command's."""
    try:
        output = BoundedOutput(sys.stderr)
        output.emit(line)
        output.close(settings.read_timeout_s)
    except Exception:  # a diagnostic that cannot be written does not replace the status
        pass
    return status


if __name__ == '__main__':
    sys.exit(main())
