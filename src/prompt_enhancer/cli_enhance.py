"""`enhance once`: one request file through the production S0-S6 coordinator (T-016 P2).

Order of effects, each after the one before succeeded: configuration, request file, expired attachment copies, the
pipeline, publication of the attachment bytes and index, the envelope on stdout, then the delivery acknowledgement.
A failure before the acknowledgement leaves the ticket unacknowledged, so the lifecycle records `delivery_unacknowledged`
and no monitor runs (R-024). Exit status: 0 delivered (an honest failed or degraded envelope is still delivered), 1 an
operational failure, 2 a refusal (cli_input.Refusal, ConfigurationError) before any model call.
"""
import asyncio
from datetime import datetime, timezone
import sys

from .attachments import expire_attachments, publish_attachments
from .cli_input import Refusal, emit_json, read_request, refuse_linked_configuration
from .config import ConfigurationError, load_config, validate_request_limit
from .pipeline import Pipeline
from .response_lifecycle import LifecycleFull
from .storage import StorageError, open_data_store
from .trace_store import trace_expiry

REFUSED = 2  # protocol-bound exit status: refused before any model call
FAILED = 1  # protocol-bound exit status: operational failure


def run_once(args, root):
    try:
        refuse_linked_configuration(args, root)
        config = load_config(args.config, 'enhance once', overlay=args.overlay, project_root=root)
        limit = validate_request_limit(config, project_root=root)
        request = read_request(root, args.request, limit)
    except (ConfigurationError, Refusal) as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return FAILED
    except Exception as error:  # the type only: nothing before the pipeline may end in a traceback or echo a path or value
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED
    try:
        return asyncio.run(_once(config, root, request))
    except (ConfigurationError, StorageError) as error:
        # Pipeline construction and the deployment-override gate state a setting problem; a store error here names a
        # configured path, as `kb sync` does.
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    except LifecycleFull:
        print('failed: trace capacity unavailable', file=sys.stderr)
        return FAILED
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return FAILED
    except Exception as error:  # the type only: a message may carry a path or request content
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED


async def _once(config, root, request):
    store = open_data_store(config, 'enhance once', project_root=root)
    retention = config['runtime']['trace']['retention_s']
    async with Pipeline(config, project_root=root) as pipeline:
        removed, failed = await pipeline.lifecycle.io(expire_attachments, store)
        if failed:
            print(f'warning: expired attachment cleanup failed for {failed} entr{"y" if failed == 1 else "ies"}',
                  file=sys.stderr)
        response = await pipeline.enhance(request)
        delivered = False
        try:
            if response.attachment_bytes:
                # Bytes and index exist before any metadata names them; a failure here is not a delivery.
                expires_at = trace_expiry(datetime.now(timezone.utc), retention)
                await pipeline.lifecycle.io(publish_attachments, store, response.envelope['trace_id'],
                                            response.attachment_bytes, expires_at)
            emit_json(response.envelope)
            delivered = True
        except (StorageError, OSError, OverflowError, ValueError) as error:
            print(f'failed: delivery ({type(error).__name__})', file=sys.stderr)
        if delivered:
            await response.after_delivery()
    errors = sorted(set(pipeline.lifecycle.errors))
    if errors or not delivered:
        if errors:
            print('operational: ' + ' '.join(errors), file=sys.stderr)
        return FAILED
    return 0
