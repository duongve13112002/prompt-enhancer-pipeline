"""Command-line entry point for the knowledge-base pipeline."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from .config import ConfigurationError, load_config
from .k0_sync import sync_config
from .storage import StorageError


def main(argv=None):
    parser = argparse.ArgumentParser(prog='prompt_enhancer')
    commands = parser.add_subparsers(dest='command', required=True)
    kb = commands.add_parser('kb')
    actions = kb.add_subparsers(dest='action', required=True)
    sync = actions.add_parser('sync')
    sync.add_argument('--config', required=True)
    sync.add_argument('--overlay')
    sync.add_argument('--plan', action='store_true')
    sync.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        config = load_config(args.config, 'kb sync', overlay=args.overlay, project_root=root)
        result = sync_config(config, project_root=root, plan=args.plan, force=args.force)
        print(json.dumps(result, ensure_ascii=False, default=_json_default))
        if args.plan and result['report']['guard']['exceeded'] and not args.force:
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
