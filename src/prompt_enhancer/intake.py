"""S0: fix a request's configuration, checked profile, source image and snapshot (R-009)."""

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from PIL import Image

from .config import ConfigurationError, _schema, _validate, effective_request_config
from .envelope import intake_retrieval
from .model_image import (
    ImageRejected, UndecodableImage, configure_decode_backstop, prepare_vision_image_with_info,
)
from .profile_prompt import ProfileError, sha256_tag
from .profiles import AttachmentRendererMismatch, resolve_profile
from .storage import StorageError, open_knowledge_store
from .storage.store import RetainedFileError


# protocol-bound operator diagnostics. `kb sync` cannot repair a damaged current snapshot (it fails on the listed file),
# so only the legacy-layout case, which a sync does republish, carries the remedy (P3-SWEEP-2).
RETENTION_MESSAGE = 'image copies unavailable in the current snapshot'
REPUBLISH_MESSAGE = 'image copies unavailable; run kb sync to republish'  # a snapshot built before copies were kept
INDEX_DAMAGE_MESSAGE = 'knowledge index file missing or changed in the current snapshot'  # protocol-bound operator diagnostic (no remedy: kb sync does not repair a damaged current snapshot)
_INDEX_PREFIX = 'index/'  # artifact-bound: where K5 keeps its files inside a snapshot (`k5_index.MANIFEST`)


class AttachmentRetentionUnavailable(StorageError):
    """A fixed request diagnostic (store state, not configuration) with no values or paths."""


class KnowledgeIndexDamaged(StorageError):
    """A fixed request diagnostic: a K5 index file the manifest lists is missing or has the wrong size."""


def _damaged_file_error(store):
    """`pin` raised `RetainedFileError` without saying which listed file is damaged. Walk the current manifest in the
    order `pin` checks it and name the cause by the file's place in the snapshot: a K5 index file is its own failure, any
    other file (an image copy or the layout marker) is the retention case. A manifest that changed or cannot be read
    since keeps the retention message this refusal always gave. Only fixed messages leave here, never a path (R-014)."""
    try:
        current = store.current()
        if current is not None:
            directory = store._snapshot_dir(current[0])
            for row in store._manifest(directory, current[1]):
                if row['kind'] != 'file':
                    continue
                try:
                    size = store._entry_path(directory, row).stat().st_size
                except OSError:
                    size = None
                if size != row['size_bytes']:
                    if row['path'].startswith(_INDEX_PREFIX):
                        return KnowledgeIndexDamaged(INDEX_DAMAGE_MESSAGE)
                    break
    except StorageError:
        pass
    return AttachmentRetentionUnavailable(RETENTION_MESSAGE)


class KnowledgeGateway:
    """Request-owned knowledge boundary; construction and its single pin are enabled-only."""

    def __init__(self, config, project_root):
        self.snapshot = None
        self.store = open_knowledge_store(config, 'enhance once', project_root=project_root)

    def pin(self):
        self.snapshot = self.store.pin()
        return self.snapshot

    def close(self):
        snapshot, self.snapshot = self.snapshot, None
        if snapshot is not None:
            snapshot.release()


@dataclass
class IntakeResult:
    """JSON payload plus live resources; close only after downstream stages finish."""

    payload: dict
    profile: object = None
    knowledge: KnowledgeGateway | None = None

    @property
    def snapshot(self):
        return None if self.knowledge is None else self.knowledge.snapshot

    def close(self):
        knowledge, self.knowledge = self.knowledge, None
        if knowledge is not None:
            knowledge.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except StorageError as error:
            if exc is None:
                raise
            exc.add_note(f'intake lease cleanup failed ({type(error).__name__})')
        return False


class _Refusal(ValueError):
    def __init__(self, code, message, *, stage=None):
        super().__init__(message)
        self.code = code
        self.stage = stage


def _image(data):
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise _Refusal('IMAGE_UNDECODABLE', 'source_image must contain valid base64 image bytes') from error


def validate_request(request, *, project_root):
    """Validate structure only: arbitrary prompt strings are never interpreted or normalized."""
    try:
        _validate(request, _schema(project_root, 'request.v0'), 'request')
    except ConfigurationError as error:
        raise ConfigurationError(f'request validation failed ({type(error).__name__})') from None
    if request['task'] == 'edit' and request.get('source_image') is None:
        raise _Refusal('SOURCE_IMAGE_REQUIRED', 'edit requires a source_image')
    if request['task'] == 'generate' and request.get('source_image') is not None:
        raise _Refusal('SOURCE_IMAGE_UNEXPECTED', 'generate does not accept a source_image')
    return request


def _refuse(payload, code, error):
    payload['outcome'] = 'refused'
    # Diagnostics describe structure only; exception text can contain request values or local paths.
    label = type(error).__name__
    if isinstance(error, _Refusal):
        label = code
        # Protocol-bound stages originate only at the controlled boundaries below.
        if error.stage in ('validation', 'profile start', 'profile end', 'image start', 'image end',
                           'snapshot start', 'snapshot end'):
            label += f' at {error.stage}'
    fixed = isinstance(error, (AttachmentRetentionUnavailable, KnowledgeIndexDamaged, AttachmentRendererMismatch))
    message = str(error) if fixed else f'intake refused ({label})'
    payload['error'] = {'code': code, 'message': message}
    # These are schema-bound refusal fields, not model output or inferred retrieval success.
    state = 'disabled' if payload['retrieval']['state'] == 'disabled' else 'failed'
    payload['retrieval'] = {'state': state, 'snapshot_id': None}
    source = payload['source_image']
    payload['envelope'] = {
        'status': 'failed', 'output': None, 'attachments': [],
        'source_binding': None if source is None else {'source_image_id': source['image_id']},
        'retrieval': intake_retrieval(payload), 'warnings': list(payload['warnings']),
        'trace_id': payload['request_id'],
    }
    return IntakeResult(payload)


def refuse_attachments(result, error):
    """Turn a ready S0 result into the same refusal intake gives, for a failure found after S3 and before S4."""
    _refuse(result.payload, 'KNOWLEDGE_FAILED', error)


def intake(request, config, *, project_root, profile_view=None):
    """Prepare S0 without model calls; every ordinary refusal returns a failed envelope.

    The supplied config may contain unresolved values repaired by permitted typed overrides.
    The monotonic budget includes validation but checks occur at stage boundaries: filesystem
    and Pillow calls are synchronous and cannot be preempted by this deadline.
    The caller owns a successful result's lease, preferably through a `with intake(...)` block.
    """
    started = time.monotonic()
    base_disabled = (isinstance(config, dict) and isinstance(config.get('rag'), dict)
                     and config['rag'].get('enabled') is False)
    payload = {
        'outcome': 'ready', 'request_id': uuid.uuid4().hex,
        'prompt': request.get('prompt') if isinstance(request, dict) and isinstance(request.get('prompt'), str) else None,
        'task': request.get('task') if isinstance(request, dict) and request.get('task') in ('generate', 'edit') else None,
        'profile_id': None, 'effective_config': None, 'config_hash': None, 'source_image': None,
        'retrieval': {'state': 'disabled' if base_disabled else 'failed',
                      'snapshot_id': None}, 'warnings': [], 'error': None, 'envelope': None,
    }
    gateway = None

    def boundary(stage):
        if time.monotonic() - started >= effective['runtime']['intake_deadline_s']:
            raise _Refusal('INTAKE_DEADLINE', 'intake deadline exceeded', stage=stage)

    try:
        validate_request(request, project_root=project_root)
        effective = effective_request_config(config, request.get('overrides', {}), project_root=project_root)
        payload['effective_config'] = effective
        payload['retrieval']['state'] = 'failed' if effective['rag']['enabled'] else 'disabled'
        encoded = json.dumps(effective, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
        payload['config_hash'] = 'sha256:' + hashlib.sha256(encoded).hexdigest()
        boundary('validation')

        boundary('profile start')
        if profile_view is None:
            profile = resolve_profile(effective, request.get('profile_id'), project_root=project_root)
        else:
            # Only the profile-check adapter supplies the checked, not-yet-activated view.
            from .profiles import ProfileView
            if not isinstance(profile_view, ProfileView) or request.get('profile_id') != profile_view.profile_id:
                raise ProfileError('PROFILE_INVALID', 'golden profile binding differs')
            profile = profile_view
        payload['profile_id'] = profile.profile_id
        profile.require_task(request['task'])
        boundary('profile end')

        boundary('image start')
        if request.get('source_image') is not None:
            raw = _image(request['source_image'])
            vision = effective['vision']
            configure_decode_backstop([vision['max_decode_pixels']])
            prepared, _ = prepare_vision_image_with_info(raw, vision, vision['max_decode_pixels'])
            payload['source_image'] = {'image_id': sha256_tag(raw), 'role': 'source_image',
                                       'data': base64.b64encode(prepared).decode('ascii')}
        boundary('image end')

        boundary('snapshot start')
        if effective['rag']['enabled']:
            gateway = KnowledgeGateway(effective, project_root)
            try:
                snapshot = gateway.pin()
            except RetainedFileError:
                if request['task'] != 'edit' or not effective['edit']['attach_kb_reference_images']:
                    raise
                raise _damaged_file_error(gateway.store) from None
            if snapshot is not None and request['task'] == 'edit' and effective['edit']['attach_kb_reference_images']:
                # Manifest-only check (no image bytes read); no snapshot at all stays the degraded SNAPSHOT_MISSING path.
                try:
                    snapshot.check_retained_images(snapshot.read_rows('images'))
                except (StorageError, OSError):
                    raise AttachmentRetentionUnavailable(
                        RETENTION_MESSAGE if snapshot.has_retained_image_layout() else REPUBLISH_MESSAGE) from None
            if snapshot is None:
                payload['outcome'] = 'degraded'
                payload['warnings'].append('SNAPSHOT_MISSING')
            else:
                payload['retrieval'] = {'state': 'pinned', 'snapshot_id': snapshot.snapshot_id}
        boundary('snapshot end')
        result = IntakeResult(payload, profile, gateway)
        gateway = None  # transfer ownership only after every refusal boundary
        return result
    except (_Refusal, ProfileError) as error:
        return _refuse(payload, error.code, error)
    except ConfigurationError as error:
        return _refuse(payload, 'CONFIGURATION_INVALID', error)
    except ImageRejected as error:
        return _refuse(payload, 'IMAGE_REJECTED', error)
    except (UndecodableImage, Image.DecompressionBombError) as error:
        return _refuse(payload, 'IMAGE_UNDECODABLE', error)
    except StorageError as error:
        return _refuse(payload, 'KNOWLEDGE_FAILED', error)
    except Exception as error:
        return _refuse(payload, 'INTAKE_FAILED', error)
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception as error:
                # Preserve the refusal and report cleanup failure without exposing exception text.
                if payload['error'] is not None:
                    payload['error']['message'] += f'; lease cleanup failed ({type(error).__name__})'
                else:
                    raise
