"""The runtime model manifest `models/manifest.json`: load it and verify one model's files before they are loaded.

The manifest is the record docs/model-storage.md describes (format `model_manifest.v0`, schema
contracts/schemas/model_manifest.v0.schema.json). A model is used only when every file its entries list is present
with the recorded size and SHA-256, sits at the recorded layout path, is reached through no link, and no file the
manifest does not list lies in the model directory: a loader reads files by name, so an unlisted file would be
unverified input (R-017).
"""

import hashlib
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator

from ..config import ConfigurationError, _schema

MANIFEST_PATH = ('models', 'manifest.json')  # artifact-bound: where the runtime record lives (docs/model-storage.md)
UNRESOLVED = 'pin_required'  # protocol-bound: the sentinel an example manifest holds for a value not yet read
HASH_BLOCK = 1 << 20  # implementation buffer of the streaming hash; not an operational value


class ManifestError(RuntimeError):
    """The manifest is unreadable or invalid, or a model's files do not match it."""


def load_manifest(root):
    """The parsed, schema-valid manifest of the project at `root`."""
    path = Path(root, *MANIFEST_PATH)
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ManifestError(f'{path}: cannot read the model manifest: {type(error).__name__}') from error
    try:
        schema = _schema(Path(root), 'model_manifest.v0')
    except ConfigurationError as error:
        raise ManifestError(str(error)) from error
    problems = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda item: list(item.absolute_path))
    if problems:
        where = '.'.join(str(part) for part in problems[0].absolute_path) or '<root>'
        raise ManifestError(f'{path}: {where}: {problems[0].message}')
    return manifest


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _same_place(real, expected):
    return os.path.normcase(str(real)) == os.path.normcase(os.path.abspath(expected))


def verify_model(root, repository, revision):
    """Verify every file of one model and return its directory; raise ManifestError on the first mismatch."""
    manifest = load_manifest(root)
    entries = [entry for entry in manifest['entries']
               if entry['repository'] == repository and entry['revision'] == revision]
    if not entries:
        raise ManifestError(f'no manifest entry for {repository} at revision {revision}')
    base = Path(os.path.realpath(root))
    prefix = f"models/{repository.replace('/', '--')}/{revision}"
    listed = set()
    for entry in entries:
        file = entry['file']
        if entry['path'] != f'{prefix}/{file}':
            raise ManifestError(f'{file}: path {entry["path"]!r} is not the layout path {prefix}/{file}')
        if UNRESOLVED in (entry['size'], entry['sha256']):
            raise ManifestError(f'{file}: size or sha256 is still {UNRESOLVED}; the manifest entry is unresolved')
        if file in listed:
            raise ManifestError(f'{file}: listed twice')
        listed.add(file)
        expected = base / entry['path']
        real = Path(os.path.realpath(expected))
        if not _same_place(real, expected):
            raise ManifestError(f'{file}: reached through a link or outside the project')
        if not real.is_file():
            raise ManifestError(f'{file}: missing')
        if real.stat().st_size != entry['size']:
            raise ManifestError(f'{file}: size {real.stat().st_size} differs from the manifest size {entry["size"]}')
        if _sha256(real) != entry['sha256']:
            raise ManifestError(f'{file}: sha256 differs from the manifest')
    directory = base / prefix
    for current, directories, names in os.walk(directory):
        for name in directories + names:
            relative = (Path(current) / name).relative_to(directory).as_posix()
            if not (directory / relative).is_dir() and relative not in listed:
                raise ManifestError(f'{relative}: file in the model directory is not listed in the manifest')
            if os.path.islink(Path(current) / name):
                raise ManifestError(f'{relative}: link inside the model directory')
    return directory
