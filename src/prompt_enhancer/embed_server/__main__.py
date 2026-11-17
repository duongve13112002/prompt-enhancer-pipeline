"""Command line: `python -m prompt_enhancer.embed_server serve --config <profile.yaml> [--overlay <file>]`, and
`reference-check reference|compare` (reference.py, T-018 P2) and the DEV-ONLY `dev-compare` (dev_compare.py, T-018
P3; smoke-only output, never a threshold, R-020).

The server reads everything from the profile (`embed serve` read set); a refusal prints one line on stderr and exits 2.
SIGINT, SIGTERM and (Windows) SIGBREAK stop it cleanly and free the GPU. Once the profile is read, the last line (a
refusal, an incomplete stop or an unexpected error) is written within `server.read_timeout_s`, so a full stderr pipe
cannot hold the exit, and no exception reaches Python's own traceback printing (fix round 5).

Before the profile is read nothing is owned (no thread, no listener, no engine) and no window exists: `argparse`
output and the configuration refusal are written directly, the one accepted unbounded write (PM decision pending user
review).
"""

import argparse
import json
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
MISMATCH = 4  # protocol-bound: exit status of `reference-check compare` when an item failed its verdict


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
    check = commands.add_parser('reference-check', help='compare the server with its model\'s documented recipe '
                                '(reference.py); a smoke check, never a quality claim')
    steps = check.add_subparsers(dest='step', required=True)
    for step, text in (('reference', 'embed the inputs with the card recipe (GPU environment); write reference.json '
                        'and the fitted min_cosine (fit.json)'),
                       ('compare', 'send the same inputs to the running server through K1; judge each item against '
                        'server.reference_check.min_cosine; write check.json')):
        part = steps.add_parser(step, help=text)
        part.add_argument('--config', required=True, help='profile path, relative paths start at the project root')
        part.add_argument('--overlay', default=None, help='optional deployment overlay of the profile')
        part.add_argument('--project-root', default=None, help='project root (default: the root of this checkout)')
        part.add_argument('--image', action='append', default=[], help='an input image inside the project (repeatable)')
        part.add_argument('--texts', default=None, help='a JSON list of input texts inside the project')
        part.add_argument('--out', required=True, help='output directory inside the project (runs/...)')
        if step == 'reference':
            part.add_argument('--batch-size', type=int, action='append', required=True,
                              help='a batch size of the recipe runs (repeatable; 1 is required, the baseline)')
        else:
            part.add_argument('--reference', required=True, help='the reference.json the reference step wrote')
    dev = commands.add_parser('dev-compare', help='DEV-ONLY: K0 and K1 over the knowledge base through the profile\'s '
                              'embedder, then leave-one-out and name-to-image ranking metrics (dev_compare.py); '
                              'smoke-only output, never a threshold or a quality claim (R-020)')
    dev.add_argument('--config', required=True, help='profile path, relative paths start at the project root')
    dev.add_argument('--overlay', default=None, help='optional deployment overlay of the profile')
    dev.add_argument('--project-root', default=None, help='project root (default: the root of this checkout)')
    dev.add_argument('--out', required=True, help='output directory inside the project (runs/...)')
    dev.add_argument('--top-k', type=int, required=True, help='k of the top-k metrics (recorded in compare.json)')
    args = parser.parse_args(argv)
    if args.command == 'reference-check':
        return _reference_check(args)
    if args.command == 'dev-compare':
        return _dev_compare(args)
    try:
        settings = load_server_settings(args.config, args.overlay, _project_root(args.project_root))
    except ConfigurationError as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    try:
        return _serve(settings)
    except (Exception, KeyboardInterrupt) as error:  # the type only: a message may carry a path or request content
        return _last_line(f'failed: {type(error).__name__}', settings, FAILED)


def _reference_check(args):
    """`reference-check reference|compare`: the profile is read through `embed serve` (reference: the tolerances are
    being measured, not read) or `embed reference-check` (compare: `server.reference_check.min_cosine` and
    `.max_norm_deviation` are required and refused while a sentinel). Exit 0 when written or passed, MISMATCH when an
    item failed, REFUSED on a refusal (a K1 `EmbeddingError` included: its code and structural detail only, never a
    response body). Once the profile is read an unexpected error is one `failed: <type>` line and FAILED, as `serve`
    does (P1 convention; P2 fix round 1, PM answer 5)."""
    from ..config import load_config
    from ..k1_embed import EmbeddingError
    from . import reference as ref
    from .settings import server_settings

    refusals = (ConfigurationError, ManifestError, ref.ReferenceCheckError, EmbeddingError)
    root = Path(args.project_root or Path(__file__).resolve().parents[3]).resolve()
    command = 'embed serve' if args.step == 'reference' else 'embed reference-check'
    try:
        config = load_config(args.config, command, overlay=args.overlay, project_root=root)
        settings = server_settings(config, root, str(args.config))
    except ConfigurationError as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    try:
        return _reference_step(args, root, config, settings)
    except refusals as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    except OSError as error:  # its text holds a file name: the type only (P3 fix round 1, F4 class)
        print(f'refused: {type(error).__name__}', file=sys.stderr)
        return REFUSED
    except (Exception, KeyboardInterrupt) as error:  # the type only: a message may carry a path or request content
        return _last_line(f'failed: {type(error).__name__}', settings, FAILED)


def _reference_step(args, root, config, settings):
    from ..k1_embed import embedder_settings
    from . import reference as ref

    k1_settings = embedder_settings(config, str(args.config))
    out = ref.check_output_dir(root, args.out)
    inputs = ref.prepare_inputs(root, args.image, args.texts, k1_settings)
    if args.step == 'reference':
        ref.check_batch_sizes(args.batch_size)
        fit = ref.run_reference(settings, verify_model(root, settings.model_id, settings.revision), inputs,
                                args.batch_size, out)
        print(json.dumps({'step': 'reference', **{key: fit[key] for key in (
            'min_cosine', 'raw_min_cosine', 'max_norm_deviation', 'raw_max_norm_deviation')},
            'out': out.relative_to(root).as_posix()}))
        return 0
    check = config['vlm']['backends'][settings.backend_name]['server']['reference_check']
    reference = ref.load_reference(ref._inside(root, args.reference, '--reference'), settings, inputs)
    verdict = ref.run_compare(config, settings, k1_settings, inputs, reference, check['min_cosine'],
                              check['max_norm_deviation'], out)
    print(json.dumps({'step': 'compare', 'passed': verdict['passed'], 'min_cosine': check['min_cosine'],
                      'max_norm_deviation': check['max_norm_deviation'],
                      'cosines': [item['cosine'] for item in verdict['items']],
                      'norms': [item['norm'] for item in verdict['items']]}))
    return 0 if verdict['passed'] else MISMATCH


def _dev_compare(args):
    """DEV-ONLY `dev-compare` (dev_compare.py): exit 0 when compare.json is written with every item embedded, REFUSED
    on a refusal (profile, inputs, store, or K1 items left unembedded, recorded in compare.json first), FAILED with the
    type only for anything unexpected. No server settings are read, so the lines are written directly."""
    from ..config import load_config
    from ..storage import StorageError
    from . import dev_compare
    from . import reference as ref

    root = Path(args.project_root or Path(__file__).resolve().parents[3]).resolve()
    try:
        dev_compare.check_top_k(args.top_k)
        config = load_config(args.config, dev_compare.COMMAND, overlay=args.overlay, project_root=root)
        out = ref._inside(root, args.out, '--out')
        report = dev_compare.run_dev_compare(config, root, out, args.top_k)
    except (ConfigurationError, dev_compare.DevCompareError, ref.ReferenceCheckError, StorageError) as error:
        print(f'refused: {error}', file=sys.stderr)  # store errors name configured store paths, as `kb sync` does
        return REFUSED
    except OSError as error:  # its text holds a file name: the type only (P3 fix round 1, F4)
        print(f'refused: {type(error).__name__}', file=sys.stderr)
        return REFUSED
    except (Exception, KeyboardInterrupt) as error:  # the type only: a message may carry a path or content
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED
    print(json.dumps({'step': 'dev-compare', 'label': report['label'], 'out': out.relative_to(root).as_posix(),
                      **{key: report[key] for key in ('counts', 'seconds', 'leave_one_out', 'name_to_image')}}))
    return 0


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
