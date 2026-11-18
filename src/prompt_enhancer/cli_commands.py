"""Command handlers behind `python -m prompt_enhancer` for everything except `kb sync`, `kb audit` and `enhance once` (T-016 P1).

Each handler loads the configuration with its own command read set first (a sentinel or missing key refuses before any
store, model or file is touched), then calls the production API; none keeps a private copy of store or profile logic.
Exit status: 0 done, 1 operational failure, 2 refusal (argument, configuration, unconfirmed or invalid input, or a command
this build deliberately does not serve). JSON goes to stdout only on success; diagnostics go to stderr.
"""
import asyncio
from dataclasses import asdict
import sys

from .cli_input import Refusal, emit_json, refuse_linked_configuration
from .config import ConfigurationError, load_config
from .kb_audit import _audit_snapshot
from .model_client import ModelCallError, compose_model_client
from .pipeline import GoldenAdapter, Pipeline
from .profile_prompt import ProfileError
from .profiles import check_profile, register_profile
from .storage import StorageError, normalise_name, open_knowledge_store
from .storage.store import check_snapshot_id

REFUSED = 2  # protocol-bound exit status: refusal before any side effect
FAILED = 1  # protocol-bound exit status: operational failure
OPERATIONAL_PROFILE_CODES = frozenset(('PROFILE_DRAFT_FAILED', 'PROFILE_GOLDEN_FAILED'))  # a model or golden failure, not an input refusal
STATUS_TABLES = ('folders', 'concepts', 'images', 'cards', 'aliases')  # counted by `kb status`
# Owners of the commands this tool recognises and refuses (plan rows T-003, T-030): named so the refusal is precise.
NOT_SERVED = {
    'eval build': 'eval build is owned by T-003 (scenario build) and is not part of this tool',
    'eval fit': 'eval fit is owned by T-030 (experiments that fit the fit_from_* values) and is not part of this tool',
    'enhance serve': 'enhance serve is not yet implemented in this build (T-016 phase 3)',
    'eval run': 'eval run is not yet implemented in this build (T-016 phase 4)',
    'eval import': 'eval import is not yet implemented in this build (T-016 phase 4)',
}


def _config(args, root, command):
    refuse_linked_configuration(args, root)
    return load_config(args.config, command, overlay=args.overlay, project_root=root)


def not_served(args, root):
    raise Refusal(NOT_SERVED[f'{args.command} {args.action}'])


def kb_only(args, root):
    raise Refusal('kb sync --only is not available: a scoped sync would treat unselected folders as removed; '
                  'run kb sync for the whole knowledge base')


def kb_status(args, root):
    store = open_knowledge_store(_config(args, root, 'kb status'), 'kb status', project_root=root)
    snapshots = [dict(row) for row in store.snapshots()]
    result = {'command': 'kb status', 'store_present': store.root.exists(), 'current': None, 'tables': None,
              'snapshots': snapshots, 'live_source_changes': 'not_checked'}
    with _audit_snapshot(store) as pinned:
        if pinned is not None:
            row = next((item for item in snapshots if item['snapshot_id'] == pinned.snapshot_id), None)
            result['current'] = {'snapshot_id': pinned.snapshot_id,
                                 'manifest_sha256': None if row is None else row['manifest_sha256'],
                                 'source_scan_summary': None if row is None else row['source_scan_summary']}
            result['tables'] = {name: len(pinned.read_rows(name)) for name in STATUS_TABLES}
            result['folders_with_error'] = sum(1 for item in pinned.read_rows('folders') if item['error'])
    emit_json(result)
    return 0


def kb_show(args, root):
    store = open_knowledge_store(_config(args, root, 'kb show'), 'kb show', project_root=root)
    with _audit_snapshot(store) as pinned:
        if pinned is None:
            raise Refusal('kb show: no published snapshot')
        wanted = normalise_name(args.concept)
        concept = next((row for row in pinned.read_rows('concepts')
                        if row['concept_id'] == args.concept or row['normalised_name'] == wanted), None)
        if concept is None:
            raise Refusal('kb show: unknown concept in the published snapshot')
        identity = concept['concept_id']
        bindings = [row for row in pinned.read_rows('folder_concepts') if row['concept_id'] == identity]
        folders = {row['folder_id'] for row in bindings}
        result = {'command': 'kb show', 'snapshot_id': pinned.snapshot_id, 'concept': concept,
                  'card': next((row for row in pinned.read_rows('cards') if row['concept_id'] == identity), None),
                  'aliases': [row for row in pinned.read_rows('aliases') if row['concept_id'] == identity],
                  'folder_bindings': bindings,
                  'folders': [row for row in pinned.read_rows('folders') if row['folder_id'] in folders]}
    emit_json(result)
    return 0


def _writable_store(args, root, command):
    """The knowledge store of a writer command; a missing store is refused, never created by the command."""
    store = open_knowledge_store(_config(args, root, command), command, project_root=root)
    if not store.root.exists():
        raise Refusal(f'{command}: no knowledge store at the configured location')
    return store


def kb_rollback(args, root):
    store = _writable_store(args, root, 'kb rollback')
    try:
        target = check_snapshot_id(args.snapshot)
    except StorageError:
        raise Refusal('kb rollback: not a snapshot id') from None
    row = next((item for item in store.snapshots() if item['snapshot_id'] == target), None)
    if row is None or row['state'] != 'retired':
        raise Refusal(f"kb rollback: {target} is not a retained snapshot ({'unknown' if row is None else row['state']})")
    with store.writer() as writer:
        writer.rollback(target)
    emit_json({'command': 'kb rollback', 'current': target})
    return 0


def kb_gc(args, root):
    store = _writable_store(args, root, 'kb gc')
    with store.writer() as writer:
        report = writer.gc()
    emit_json({'command': 'kb gc', **asdict(report)})
    return 0


def profile_register(args, root):
    config = _config(args, root, 'profile register')

    async def run():
        drafter = await compose_model_client(config, 'manifest_drafter', project_root=root)
        try:
            return await register_profile(config, args.profile_id, drafter, project_root=root)
        finally:
            await drafter.close()

    result = asyncio.run(run())
    emit_json({'command': 'profile register', 'profile_id': args.profile_id, 'status': result['manifest']['status'],
               'overrides': result['overrides'], 'findings': [list(item) for item in result['findings']],
               'call_status': result['record'].get('status')})
    return 0


def profile_check(args, root):
    _config(args, root, 'profile check')
    # The golden runner is the production pipeline, so its own (request-command) read set is checked first: a
    # missing or unresolved value refuses here, before a pipeline, a model client or a store is built.
    config = _config(args, root, 'enhance once')

    async def run():
        async with Pipeline(config, project_root=root) as pipeline:
            return await check_profile(config, args.profile_id, GoldenAdapter(pipeline), project_root=root)

    result = asyncio.run(run())
    emit_json({'command': 'profile check', 'profile_id': args.profile_id, 'activated': True,
               'activation': result['activation'], 'results': result['results']})
    return 0


HANDLERS = {
    'kb status': kb_status, 'kb show': kb_show, 'kb rollback': kb_rollback, 'kb gc': kb_gc,
    'profile register': profile_register, 'profile check': profile_check,
    'enhance serve': not_served, 'eval run': not_served, 'eval import': not_served,
    'eval build': not_served, 'eval fit': not_served, 'kb only': kb_only,
}


def run_command(args, root):
    """Run one handler and map every failure to an exit status without echoing request or model content."""
    # `kb sync` reaches this function only with --only, which this build refuses.
    handler = HANDLERS['kb only' if (args.command, args.action) == ('kb', 'sync') else f'{args.command} {args.action}']
    try:
        return handler(args, root)
    except (Refusal, ConfigurationError) as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    except ProfileError as error:
        print(f'{"failed" if error.code in OPERATIONAL_PROFILE_CODES else "refused"}: {error}', file=sys.stderr)
        return FAILED if error.code in OPERATIONAL_PROFILE_CODES else REFUSED
    except StorageError as error:  # store errors name configured store paths, as `kb sync` does
        print(str(error), file=sys.stderr)
        return FAILED
    except ModelCallError as error:
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return FAILED
    except Exception as error:  # the type only: a message may carry a path or content
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED
