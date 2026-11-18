"""Command-line entry point: `python -m prompt_enhancer <group> <command> --config <profile.yaml> ...` (docs/execution.md).

`kb sync` and `kb audit` keep their original behaviour and exit statuses (configuration and store errors exit 1). The
commands added by T-016 exit 0 on success, 1 on an operational failure and 2 on a refusal made before any side effect.
The separate embedding server is `python -m prompt_enhancer.embed_server`.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from .config import ConfigurationError, load_config
from .k0_sync import sync_config
from .kb_audit import audit_config
from .storage import StorageError


def _common(parser, *, overlay=True):
    parser.add_argument('--config', required=True, help='profile path, relative paths start at the project root')
    if overlay:
        parser.add_argument('--overlay', help='optional deployment overlay of the profile')
    parser.add_argument('--project-root', default=None, help='project root (default: the root of this checkout)')
    return parser


def build_parser():
    parser = argparse.ArgumentParser(prog='prompt_enhancer')
    commands = parser.add_subparsers(dest='command', required=True)
    kb = commands.add_parser('kb', help='knowledge base: sync, status, rollback, gc, audit, show')
    actions = kb.add_subparsers(dest='action', required=True)
    sync = _common(actions.add_parser('sync'))
    sync.add_argument('--plan', action='store_true')
    sync.add_argument('--force', action='store_true')
    sync.add_argument('--only', help='refused: a scoped sync is not available')
    audit = _common(actions.add_parser('audit'))
    audit.add_argument('--folder-count', type=int, required=True)
    audit.add_argument('--card-count', type=int, required=True)
    audit.add_argument('--seed', type=int, required=True)
    _common(actions.add_parser('status', help='stored snapshot facts; live source changes are not checked'))
    rollback = _common(actions.add_parser('rollback', help='move the current pointer to a retained snapshot'))
    rollback.add_argument('snapshot')
    _common(actions.add_parser('gc', help='remove unreferenced snapshots and artifacts past retention'))
    show = _common(actions.add_parser('show', help='a concept\'s card, aliases and folders from the current snapshot'))
    show.add_argument('concept', help='a concept id or a concept name')
    profile = commands.add_parser('profile', help='admin profile: register, check')
    profile_actions = profile.add_subparsers(dest='action', required=True)
    for name, text in (('register', 'draft the manifest of a profile folder (activates nothing)'),
                       ('check', 'run conflict lint and golden tests through the pipeline; activates on success')):
        _common(profile_actions.add_parser(name, help=text)).add_argument('--profile-id', required=True)
    enhance = commands.add_parser('enhance', help='run S0 to S6: once, serve')
    enhance_actions = enhance.add_subparsers(dest='action', required=True)
    once = _common(enhance_actions.add_parser('once', help='one request file in, one envelope on stdout'))
    once.add_argument('--request', required=True, help='request.v0 JSON file inside the project root')
    _common(enhance_actions.add_parser('serve', help='not yet implemented in this build'))
    evaluation = commands.add_parser('eval', help='evaluation: build, run, import, fit')
    eval_actions = evaluation.add_subparsers(dest='action', required=True)
    for name in ('build', 'run', 'import', 'fit'):
        _common(eval_actions.add_parser(name))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    given = getattr(args, 'project_root', None)
    root = Path(given).resolve() if given else Path(__file__).resolve().parents[2]
    if args.command == 'enhance' and args.action == 'once':
        from .cli_enhance import run_once
        return run_once(args, root)
    if args.command == 'kb' and args.action == 'sync' and args.only is not None:
        from .cli_commands import run_command
        return run_command(args, root)
    if args.command == 'kb' and args.action in ('sync', 'audit'):
        return _legacy(args, root)
    from .cli_commands import run_command
    return run_command(args, root)


def _legacy(args, root):
    """`kb sync` and `kb audit`, unchanged."""
    try:
        config = load_config(args.config, 'kb ' + args.action, overlay=args.overlay, project_root=root)
        if args.action == 'audit':
            result = audit_config(config, project_root=root, folder_count=args.folder_count,
                                  card_count=args.card_count, seed=args.seed)
        else:
            result = sync_config(config, project_root=root, plan=args.plan, force=args.force)
        print(json.dumps(result, ensure_ascii=False, default=_json_default))
        if (args.action == 'sync' and args.plan and result.get('status') != 'skipped'
                and result['report']['guard']['exceeded'] and not args.force):
            return 1
    except (ConfigurationError, StorageError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'cannot encode {type(value).__name__}')


if __name__ == '__main__':
    sys.exit(main())
