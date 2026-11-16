"""Identities and artifact keys (contracts/README.md Identity; docs/execution.md artifact keys, R-017)."""

import hashlib
import json
import re
import unicodedata

from ._fs import StorageError, check_segment

PHYSICAL_VERSION = 1  # artifact-bound: physical table layout version, part of every artifact key
_HEX64 = re.compile(r'[0-9a-f]{64}')
_KIND = re.compile(r'[a-z0-9_]+')
REVISION_FIELDS = ('model', 'prompt', 'schema')


def normalise_name(name):
    """Unicode NFKC then case folding, nothing else (contracts/README.md:22, R-015)."""
    if not isinstance(name, str):
        raise StorageError(f'concept name must be a string, got {type(name).__name__}')
    return unicodedata.normalize('NFKC', name).casefold()


def concept_id(name):
    return 'concept:' + hashlib.sha256(normalise_name(name).encode('utf-8')).hexdigest()


def image_id(data):
    return 'sha256:' + hashlib.sha256(bytes(data)).hexdigest()


def is_concept_id(value):
    return isinstance(value, str) and value.startswith('concept:') and bool(_HEX64.fullmatch(value[8:]))


def is_image_id(value):
    return isinstance(value, str) and value.startswith('sha256:') and bool(_HEX64.fullmatch(value[7:]))


def check_folder_id(value):
    """Relative path, forward slashes, kept byte for byte: refuse anything that is not such a path."""
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value or value.startswith('/'):
        raise StorageError(f'invalid folder_id {value!r}')
    if any(part in ('', '.', '..') for part in value.split('/')):
        raise StorageError(f'invalid folder_id {value!r}')
    return value


def canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise StorageError(f'value is not canonical JSON: {error}') from error


def artifact_key(kind, input_hash, revisions):
    """Input content hash plus model, prompt and schema revisions; paths never enter the key (rename-invariant)."""
    check_kind(kind)
    if not isinstance(input_hash, str) or not input_hash:
        raise StorageError('artifact input hash must be a non-empty string')
    if not isinstance(revisions, dict) or set(revisions) != set(REVISION_FIELDS):
        raise StorageError(f'artifact revisions must name exactly {REVISION_FIELDS}')
    for field in REVISION_FIELDS:
        if not isinstance(revisions[field], str) or not revisions[field]:
            raise StorageError(f'artifact revision {field!r} must be a non-empty string')
    body = canonical_json({'kind': kind, 'input': input_hash, 'revisions': revisions, 'physical': PHYSICAL_VERSION})
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def check_artifact_key(value):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise StorageError(f'invalid artifact key {value!r}')
    return value


def check_kind(value):
    if not isinstance(value, str) or not _KIND.fullmatch(value):
        raise StorageError(f'invalid artifact kind {value!r}')
    return value  # a device-name kind ('nul') fits the pattern; `inside` refuses it before any file is touched


# ----------------------------------------------------------------------------- the identity rule (fix round 3)
#
# A file system may map several spellings to one file (case on Windows and macOS, a final dot or space and 8.3
# short names on Windows, NFD and NFC on macOS). The store therefore admits exactly one spelling per identity and
# refuses the others at admission; it never rewrites a spelling it was given:
# - names the store generates (artifacts, segments, leases, registry files, hold files) use an alphabet without
#   letters of two cases (lowercase hex, digits, fixed punctuation) or a pattern whose letters are fixed per
#   position (snapshot ids), so two distinct admitted names are two distinct files on every file system;
# - names a caller chooses (snapshot files, request ids) pass `check_segment` and must not share
#   `identity_key` with another name in the same namespace.

_ARTIFACT_PATH = re.compile(r'artifacts/([a-z0-9_]+)/([0-9a-f]{2})/([0-9a-f]{64})\.(parquet|arrow|bin)')


def artifact_path(kind, key, suffix):
    """The one spelling of an artifact's store path (content-addressed: kind, key prefix, key)."""
    return check_artifact_path(f'artifacts/{check_kind(kind)}/{check_artifact_key(key)[:2]}/{key}{suffix}')


def check_artifact_path(value):
    """Refuse every spelling of an artifact path except the exact generated one."""
    match = _ARTIFACT_PATH.fullmatch(value) if isinstance(value, str) else None
    if match is None or match.group(2) != match.group(3)[:2]:
        raise StorageError(f'{value!r} is not a stored artifact path in its generated form')
    check_kind(match.group(1))
    # Artifact-bound: persisted naming draws are JSON blobs, never table artifacts.
    if match.group(1) == 'k2_draw' and match.group(4) != 'bin':
        raise StorageError(f'{value!r}: k2_draw requires the .bin suffix')
    return value


def check_relative_name(value):
    """A caller-chosen relative POSIX path: every segment passes the one segment rule."""
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise StorageError(f'invalid relative path {value!r}')
    try:
        for segment in value.split('/'):
            check_segment(segment)
    except StorageError as error:
        raise StorageError(f'invalid relative path {value!r}: {error}') from error
    return value


def identity_key(value):
    """What two spellings share when a case-insensitive file system maps them to one file.

    Full Unicode case folding on NFC text: two names with one key may collide on some file system, so a
    namespace admits only one of them. Measured on NTFS here (.tmp/T-006/review-r3/case-facts-r3.json): every
    pair NTFS maps to one file shares a key; the key also joins pairs NTFS keeps apart ('ß'/'SS', 'k'/KELVIN
    SIGN), an over-refusal that is the safe side."""
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', value).casefold())
