"""P: register, confirm and check admin profiles, and the read API for S0, S4 and S5.

Bundle `<profiles.root>/<profile_id>/`: `system_prompt.txt` (the admin's, never edited), `manifest.yaml`,
`golden/<id>.yaml`, `golden_results.json`, `activation.json` and `check.lock` (contracts/README.md, Profile
manifest).

Publication rules (fix rounds 1 and 2): register, confirm and check write only while holding the bundle's
`check.lock`, so cooperating writers of one profile never overlap (F1, RT3); registration publishes the manifest
without replacing one (F2); a writer computes its bytes before its last comparison with the disk (RT3); the
manifest writer reads its bytes back and refuses any change of value (RT2); activation binds every file, schema,
value and code fingerprint the verdict depends on, and resolve verifies each (F4, F5, RT4).

Known limit: an external editor takes no lock, so an edit of `manifest.yaml` that lands between confirm's last
comparison and its atomic replace is lost; a check in the same window fails closed (resolve refuses).
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys
import time

from jsonschema import Draft202012Validator, exceptions as schema_exceptions
import yaml

from .config import (_AMBIGUOUS_BREAKS, ConfigurationError, _MetaLoader, _StrictSafeLoader, _filename, _inside,
                     _load_yaml, _parse_manifest, _schema, _schema_path, _sentinel, load_profile)
from .model_client import ModelCallError
from .profile_lint import (ADDENDUM_ID, DECISIONS, conflict_findings, input_variable, key_findings, render_addendum,
                           resolve_key, template_findings)
from .profile_prompt import (ProfileError, load_prompt, parse_meta, parse_structure, placeholders, sha256_tag,
                             system_message, user_template)
from .storage._fs import FileLock, StorageError, _temporary, atomic_write_bytes, fsync_dir
from .storage.store import _settings as _storage_settings


# Artifact-bound bundle file names (contracts/README.md, Profile manifest).
MANIFEST_FILE = 'manifest.yaml'
PROMPT_FILE = 'system_prompt.txt'
GOLDEN_DIR = 'golden'
GOLDEN_RESULTS_FILE = 'golden_results.json'
ACTIVATION_FILE = 'activation.json'
# OS lock file of the bundle (name kept from fix round 1, where only check held it); never deleted, because
# deleting a lock file races.
CHECK_LOCK_FILE = 'check.lock'
MANIFEST_SCHEMA = 'profile_manifest.v0'  # protocol-bound: the drafter's output contract
GOLDEN_SCHEMA = 'golden_case.v0'
ENVELOPE_SCHEMA = 'response_envelope.v0'
CHECK_SCHEMAS = (MANIFEST_SCHEMA, GOLDEN_SCHEMA, ENVELOPE_SCHEMA)  # every schema a check verdict depends on (RT4)
# Protocol-bound: the distributions whose behaviour the verdict depends on (YAML reading, schema validation).
CHECK_LIBRARIES = ('PyYAML', 'jsonschema')
HASH_PLACEHOLDER = 'computed_at_registration'  # contracts/README.md: retained for registration
CHECKED_STATUSES = ('confirmed', 'active')  # `active` is accepted when the admin keeps it; P never writes it
TASKS = ('generate', 'edit')  # protocol-bound: profile_manifest.v0 supported_tasks


def _root(project_root):
    return Path(project_root).resolve()


def _bundle(config, profile_id, project_root):
    """Resolve and validate the bundle folder of one profile."""
    root = _root(project_root)
    try:
        if not isinstance(profile_id, str):
            raise ConfigurationError('profile_id: expected a profile id')
        _filename(profile_id, 'profile_id', 'profile_id')
    except ConfigurationError as error:
        raise ProfileError('PROFILE_ID_INVALID', str(error)) from error
    profiles = config['profiles']
    _sentinel(profiles['root'], 'config', 'profiles.root', {})
    _sentinel(profiles['exclude'], 'config', 'profiles.exclude', {})
    if profile_id in profiles['exclude']:
        raise ProfileError('PROFILE_EXCLUDED', f'{profile_id!r} is listed in profiles.exclude')
    base = _inside(profiles['root'], root)
    return _inside(base / profile_id, root)


def _errors(schema, value):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.absolute_path))
    return [f"{'.'.join(map(str, error.absolute_path)) or 'root'}: {error.message}" for error in errors]


def _schema_bytes(root, schema_id):
    path = _schema_path(root, schema_id)
    try:
        return path, path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f'cannot load schema {path}: {error.strerror or error}') from error


def _schema_snapshot(root):
    """Read each check schema once: the hash the record binds and the value the check validates with (RT4)."""
    snapshot = {}
    for schema_id in CHECK_SCHEMAS:
        path, raw = _schema_bytes(root, schema_id)
        try:
            value = json.loads(raw)
            Draft202012Validator.check_schema(value)
        except (ValueError, schema_exceptions.SchemaError) as error:
            raise ConfigurationError(f'invalid schema {path}: {error}') from error
        snapshot[schema_id] = (sha256_tag(raw), value)
    return snapshot


def _schema_hashes(root):
    return {schema_id: sha256_tag(_schema_bytes(root, schema_id)[1]) for schema_id in CHECK_SCHEMAS}


def _code_fingerprint():
    """Hash of the code a verdict depends on: the bytes of the modules that run the check (as imported) and the
    versions of its libraries and of Python, whose Unicode tables decide `str.isidentifier` (RT4 class)."""
    from . import config, profile_lint, profile_prompt
    entries = [[module.__name__, sha256_tag(module.__spec__.loader.get_data(module.__file__))]
               for module in (config, profile_prompt, profile_lint, sys.modules[__name__])]
    entries += [[name, metadata.version(name)] for name in CHECK_LIBRARIES]
    entries.append(['python', platform.python_version()])
    return sha256_tag(json.dumps(entries, separators=(',', ':')).encode('utf-8'))


def _flatten(value, prefix):
    if isinstance(value, dict) and value:
        result = {}
        for key, item in value.items():
            result.update(_flatten(item, f'{prefix}.{key}'))
        return result
    return {prefix: value}


class _ManifestDumper(yaml.SafeDumper):
    """Quotes a string whenever either manifest reading would retype it: YAML 1.1 for the schema-typed fields,
    the META contract for `meta` (`'1e3'` is a string under 1.1 but a float under the contract)."""


_ManifestDumper.yaml_implicit_resolvers = {first: list(entries)
                                           for first, entries in yaml.SafeDumper.yaml_implicit_resolvers.items()}
for _first, _entries in _MetaLoader.yaml_implicit_resolvers.items():
    _ManifestDumper.yaml_implicit_resolvers.setdefault(_first, []).extend(_entries)


def _represent_str(dumper, data):
    # PyYAML writes a raw NEL, LS or PS as a line break, and reads a raw NEL back as a space (RT2); the reader
    # refuses all three raw. Double quotes make the emitter write the escapes \N, \L and \P.
    style = '"' if any(char in data for char in _AMBIGUOUS_BREAKS) else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)


_ManifestDumper.add_representer(str, _represent_str)
_MANIFEST_HEAD = (b'# Profile manifest drafted by `profile register` (P). The admin confirms it: set status to confirmed\n'
                  b'# and one decision per conflict with decided_by_admin: true. The system prompt is never edited.\n')


def _same(left, right):
    """Equal values of equal types, in equal order: `True` is not `1`, `1.0` is not `1`, `-0.0` is not `0.0`.

    Iterative, so a deep value cannot exhaust the stack."""
    stack = [(left, right)]
    while stack:
        a, b = stack.pop()
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            if list(a) != list(b):
                return False
            stack.extend((a[key], b[key]) for key in a)
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif (repr(a) != repr(b)) if isinstance(a, float) else (a != b):
            return False
    return True


def _dump_manifest(manifest):
    """The manifest bytes, read back with the one manifest reader before they are returned (fix round 2).

    Any difference of value, type or order is refused, so a write never changes the admin's data silently.
    """
    data = _MANIFEST_HEAD + yaml.dump(manifest, Dumper=_ManifestDumper, sort_keys=False,
                                      allow_unicode=True).encode('utf-8')
    try:
        back = _parse_manifest(data, MANIFEST_FILE)
    except ConfigurationError as error:
        raise ProfileError('PROFILE_MANIFEST_UNWRITABLE', f'the written manifest would not read back: {error}') from error
    if not _same(back, manifest):
        raise ProfileError('PROFILE_MANIFEST_UNWRITABLE', 'the manifest writer would change a value; nothing was written')
    return data


def _first_difference(left, right):
    """The 1-based number of the first line where two byte strings differ."""
    a, b = left.split(b'\n'), right.split(b'\n')
    return next((index + 1 for index, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)) + 1)


def _read_bytes(path, code):
    try:
        return path.read_bytes()
    except OSError as error:
        raise ProfileError(code, f'cannot read {path.name}: {error.strerror or error}') from error


def _structure(prompt):
    structure = parse_structure(prompt)
    try:
        prompt.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ProfileError('PROFILE_PROMPT_INVALID', f'prompt is not UTF-8 at byte {error.start}') from error
    return structure


def _publish_new(path, data):
    """Publish a complete file at `path` only if none exists there (F2); raise FileExistsError otherwise.

    A hard link of a fsynced temporary sibling is atomic and never replaces its target on NTFS and POSIX file
    systems. Where the file system has no hard links, exclusive creation keeps the no-replace guarantee, but a
    reader may then see a partial file while it is written.
    """
    tmp = _temporary(path)
    try:
        with open(tmp, 'xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise
        except OSError:
            with open(path, 'xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


async def register_profile(config, profile_id, drafter, *, project_root):
    """Draft the manifest once through the `manifest_drafter` role and write it with status `draft`.

    `drafter` is a bound T-009 client (`await drafter.call(system_prompt, user_text)` -> `(value, record)`).
    """
    root = _root(project_root)
    role = config['vlm']['roles']['manifest_drafter']
    _sentinel(role, 'config', 'vlm.roles.manifest_drafter', {})
    _sentinel(config['creativity'], 'config', 'creativity', {})
    if role['output']['schema_id'] != MANIFEST_SCHEMA:
        raise ConfigurationError(f'vlm.roles.manifest_drafter.output.schema_id: expected {MANIFEST_SCHEMA}')
    bundle = _bundle(config, profile_id, root)
    lock = _bundle_lock(config, bundle, root, 'profile register')  # refused here, before the paid call
    if (bundle / MANIFEST_FILE).exists():
        raise ProfileError('PROFILE_EXISTS', f'{profile_id}: {MANIFEST_FILE} exists; the admin removes it to redraft')
    prompt = _read_bytes(_inside(bundle / PROMPT_FILE, root), 'PROFILE_PROMPT_INVALID')
    structure = _structure(prompt)
    meta = parse_meta(prompt, structure)
    variables = placeholders(structure.body(prompt, 'USER')) if structure.template_format == 'sectioned' else []
    drafter_prompt = load_prompt(root, role['prompt_id'])
    code_owned = {'schema_version': 1, 'profile_id': profile_id, 'status': 'draft',
                  'system_prompt_hash': sha256_tag(prompt), 'template_format': structure.template_format,
                  'template_variables': variables, 'golden_tests': []}
    if meta is not None:
        code_owned['meta'] = meta
    data = {'profile_id': profile_id, 'system_prompt_text': prompt.decode('utf-8'),
            'structure': {'template_format': structure.template_format, 'meta': meta or {},
                          'template_variables': variables},
            'pipeline_policy': {'configuration': _flatten(config['creativity'], 'creativity')}}
    user_text = json.dumps(data, ensure_ascii=False, allow_nan=False)
    try:
        value, record = await drafter.call(drafter_prompt, user_text)
    except ModelCallError as error:
        code = str(error).partition(':')[0]
        raise ProfileError('PROFILE_DRAFT_FAILED', code, model_code=code) from error
    if record.get('status') != 'OK':
        raise ProfileError('PROFILE_DRAFT_FAILED', str(record.get('status')), model_code=record.get('status'),
                           record=record)
    schema = _schema(root, MANIFEST_SCHEMA)
    errors = _errors(schema, value) if isinstance(value, dict) else ['root: expected an object']
    if errors:
        raise ProfileError('PROFILE_DRAFT_INVALID', errors[0], errors=errors, record=record)
    manifest = deepcopy(value)
    overrides = sorted(key for key, item in code_owned.items() if manifest.get(key) != item)
    if 'meta' in manifest and meta is None:
        overrides.append('meta')
        del manifest['meta']
    manifest.update(deepcopy(code_owned))
    for conflict in manifest['conflicts']:
        # Decisions belong to the admin (decision 6); a drafted decision is reset and reported.
        if conflict['decision'] != 'undecided' or conflict['decided_by_admin'] is not False:
            overrides.append(f"conflicts.{conflict['id']}.decision")
        conflict['decision'], conflict['decided_by_admin'] = 'undecided', False
    errors = _errors(schema, manifest)
    if errors:
        raise ProfileError('PROFILE_DRAFT_INVALID', errors[0], errors=errors, record=record)
    findings = [finding for finding in conflict_findings(manifest['conflicts']) if finding[0] != 'CONFLICT_UNDECIDED']
    findings += key_findings(manifest['conflicts'], config)
    findings += template_findings(manifest, structure, user_template(prompt, manifest['template_format'], structure))
    data = _dump_manifest(manifest)
    await _acquire(lock, profile_id)
    try:  # the no-replace link is the last comparison: it publishes only where no manifest exists
        _publish_new(_inside(bundle / MANIFEST_FILE, root), data)
    except FileExistsError as error:
        raise ProfileError('PROFILE_EXISTS', f'{profile_id}: {MANIFEST_FILE} appeared during registration; '
                           'it is kept as written') from error
    finally:
        lock.release()
    return {'manifest': manifest, 'record': record, 'overrides': overrides, 'findings': findings}


def confirm_profile(config, profile_id, decisions, *, project_root):
    """Write the admin's decisions and status `confirmed`; editing `manifest.yaml` by hand is equivalent.

    Confirm rewrites only a manifest in the form it writes itself, so a hand edit it cannot reproduce (a comment,
    an anchor, a merge key, a tag, other quoting) is refused rather than lost; the admin then confirms by hand.
    `storage.lock` bounds the wait for the bundle lock and is validated as for `profile check`.
    """
    root = _root(project_root)
    bundle = _bundle(config, profile_id, root)
    lock = _bundle_lock(config, bundle, root, 'profile check')
    deadline = time.monotonic() + lock.timeout_s
    while not lock.try_acquire():
        if time.monotonic() >= deadline:
            raise _busy(lock, profile_id)
        time.sleep(lock.poll_s)
    try:
        return _confirm_locked(bundle, root, decisions)
    finally:
        lock.release()


def _confirm_locked(bundle, root, decisions):
    manifest, manifest_raw, prompt = _read_bundle(bundle, root)
    if manifest['status'] != 'draft' and manifest['status'] != 'confirmed':
        raise ProfileError('PROFILE_CONFIRM_INVALID', f"status {manifest['status']} cannot be confirmed")
    written = _dump_manifest(manifest)
    if written != manifest_raw:
        line = _first_difference(written, manifest_raw)
        raise ProfileError('PROFILE_CONFIRM_INVALID', f'{MANIFEST_FILE} line {line} is not in the form confirm writes; '
                           'a rewrite would drop hand edits (comments, anchors, merge keys, tags, quoting), so '
                           'confirm it by hand')
    ids = [conflict['id'] for conflict in manifest['conflicts']]
    unknown = sorted(set(decisions) - set(ids))
    if unknown:
        raise ProfileError('PROFILE_CONFIRM_INVALID', f'unknown conflict ids {unknown}')
    for cid, decision in decisions.items():
        if decision not in DECISIONS:
            raise ProfileError('PROFILE_CONFIRM_INVALID', f'{cid}: unknown decision {decision!r}')
    for conflict in manifest['conflicts']:
        if conflict['id'] in decisions:
            conflict['decision'] = decisions[conflict['id']]
            conflict['decided_by_admin'] = decisions[conflict['id']] != 'undecided'
    structure = _structure(prompt)
    findings = [finding for finding in conflict_findings(manifest['conflicts'])
                if finding[0] not in ('CONFLICT_UNDECIDED', 'CONFLICT_BLOCKS')]
    findings += template_findings(manifest, structure, user_template(prompt, manifest['template_format'], structure))
    if findings:
        raise ProfileError('PROFILE_CONFIRM_INVALID', str(findings[0]), findings=findings)
    manifest['status'] = 'confirmed'
    data = _dump_manifest(manifest)  # the bytes exist before the last comparison (RT3)
    path = _inside(bundle / MANIFEST_FILE, root)
    # A hand edit made since the read is refused, not overwritten (A7, RT3). Known limit: an editor takes no
    # lock, so an edit between this comparison and the atomic replace is still lost.
    if _read_bytes(path, 'PROFILE_CHANGED') != manifest_raw:
        raise ProfileError('PROFILE_CHANGED', f'{MANIFEST_FILE} changed during confirmation; it is kept as written')
    atomic_write_bytes(path, data)
    return manifest


@dataclass(frozen=True)
class ProfileView:
    """What S0, S4 and S5 read from one checked profile; bytes are the admin's, verbatim."""

    profile_id: str
    manifest: dict
    system_prompt_sha256: str
    manifest_sha256: str
    system_message: bytes
    user_template: bytes | None
    template_variables: tuple
    template_values: dict
    input_variable: str | None
    addendum: dict | None
    validator_checks: tuple
    supported_tasks: tuple
    has_known_conflict: bool
    activation: dict | None

    def require_task(self, task):
        """Refuse a task the profile does not support; never reformat it (decision 5)."""
        if task not in TASKS:
            raise ProfileError('INPUT_INVALID', f'unknown task {task!r}')
        if task not in self.supported_tasks:
            raise ProfileError('TASK_UNSUPPORTED', f'profile {self.profile_id!r} supports only '
                               f'{list(self.supported_tasks)}; a {task} request is refused, never reformatted')


def _read_bundle(bundle, root):
    """Read the manifest bytes once and validate them like `load_profile`, so hashes match what is parsed."""
    manifest_raw = _read_bytes(_inside(bundle / MANIFEST_FILE, root), 'PROFILE_NOT_REGISTERED')
    manifest, prompt = load_profile(bundle, project_root=root)
    if not _same(_parse_manifest(manifest_raw, MANIFEST_FILE), manifest):  # by type too: `false` is not `0`
        raise ProfileError('PROFILE_CHANGED', 'manifest changed while it was read')
    return manifest, manifest_raw, prompt


def _require_keys(manifest, config, source):
    for code, cid, key in key_findings(manifest['conflicts'], config):
        detail = 'missing key' if code == 'CONFLICT_KEY_UNKNOWN' else 'unresolved sentinel'
        raise ConfigurationError(f'{source}: conflicts.{cid}.config_keys: {key}: {detail}')


def _config_fingerprint(manifest, config):
    """Hash of every cited dotted key and its resolved value, whatever the decision (F4).

    Only cited keys enter, so the approved narrow read sets stay narrow; an uncited key never invalidates.
    """
    keys = sorted({key for conflict in manifest['conflicts'] for key in conflict.get('config_keys', [])})
    try:
        data = json.dumps([[key, resolve_key(config, key)] for key in keys], ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f'conflicts.config_keys: a cited value is not JSON data: {error}') from error
    return sha256_tag(data.encode('utf-8'))


def _bundle_lock(config, bundle, root, command):
    """The bundle's OS lock, its wait bounded by `storage.lock` (the per-machine OS-lock wait, F1)."""
    try:
        _, _, settings = _storage_settings(config, command, root)
    except StorageError as error:
        raise ConfigurationError(f'config: {error}') from error
    return FileLock(_inside(bundle / CHECK_LOCK_FILE, root), settings['timeout_s'], settings['poll_s'])


def _busy(lock, profile_id):
    return ProfileError('PROFILE_BUSY', f'{profile_id!r}: another profile register, confirm or check holds '
                        f'{CHECK_LOCK_FILE} beyond storage.lock.timeout_s={lock.timeout_s}; nothing was changed')


async def _acquire(lock, profile_id):
    """Poll the OS lock without blocking the event loop; refuse after `storage.lock.timeout_s`."""
    deadline = time.monotonic() + lock.timeout_s
    while not lock.try_acquire():
        if time.monotonic() >= deadline:
            raise _busy(lock, profile_id)
        await asyncio.sleep(lock.poll_s)


def _view(profile_id, manifest, manifest_raw, prompt, config, root, activation):
    structure = _structure(prompt)
    fmt = manifest['template_format']
    template = user_template(prompt, fmt, structure)
    findings = template_findings(manifest, structure, template)
    if findings:
        raise ProfileError('PROFILE_TEMPLATE_INVALID', findings[0][1], findings=findings)
    addendum = render_addendum(manifest['conflicts'], config, load_prompt(root, ADDENDUM_ID))
    return ProfileView(
        profile_id=profile_id, manifest=deepcopy(manifest), system_prompt_sha256=sha256_tag(prompt),
        manifest_sha256=sha256_tag(manifest_raw), system_message=system_message(prompt, fmt, structure),
        user_template=template, template_variables=tuple(manifest['template_variables']),
        template_values=dict(manifest.get('template_values', {})), input_variable=input_variable(manifest),
        addendum=addendum, validator_checks=tuple(manifest['validator']['checks']),
        supported_tasks=tuple(manifest['supported_tasks']), has_known_conflict=bool(manifest['conflicts']),
        activation=activation)


def _golden_cases(manifest, bundle, root, schema):
    ids = manifest['golden_tests']
    if not ids:
        raise ProfileError('PROFILE_GOLDEN_MISSING', 'a profile needs at least one golden test before activation')
    if len(set(ids)) != len(ids):
        raise ProfileError('PROFILE_GOLDEN_INVALID', 'golden_tests lists an id more than once')
    conflict_ids = {conflict['id'] for conflict in manifest['conflicts']}
    cases, covered = [], set()
    inputs = _golden_inputs(ids, bundle, root, 'PROFILE_GOLDEN_INVALID')
    for case_id, _, case, image in inputs:
        errors = _errors(schema, case)
        if errors:
            raise ProfileError('PROFILE_GOLDEN_INVALID', f'{case_id}: {errors[0]}', errors=errors)
        if case['id'] != case_id:
            raise ProfileError('PROFILE_GOLDEN_INVALID', f'{case_id}: file holds id {case["id"]!r}')
        unknown = sorted(set(case['covers']) - conflict_ids)
        if unknown:
            raise ProfileError('PROFILE_GOLDEN_INVALID', f'{case_id}: covers unknown conflicts {unknown}')
        covered.update(case['covers'])
        cases.append((case, image))
    uncovered = sorted(conflict_ids - covered)
    if uncovered:
        raise ProfileError('PROFILE_GOLDEN_UNCOVERED', f'no golden test covers conflicts {uncovered}')
    return cases, _golden_fingerprint(inputs)


def _golden_inputs(ids, bundle, root, code):
    """Read each golden case and the image it names: [(id, case bytes, parsed case, image bytes or None)].

    The one reader of golden inputs, for the check and for resolve, so both hash the same bytes. Any failure
    raises `code`: `PROFILE_GOLDEN_INVALID` at check, `PROFILE_NOT_CHECKED` at resolve.
    """
    inputs = []
    for case_id in ids:
        try:  # the schema allows any non-empty string, so a bad id is a golden refusal, not a configuration one (G3)
            _filename(case_id, MANIFEST_FILE, f'golden_tests.{case_id}')
            path = _inside(bundle / GOLDEN_DIR / f'{case_id}.yaml', root)
        except ConfigurationError as error:
            raise ProfileError(code, f'golden test id {case_id!r}: {error}') from error
        raw = _read_bytes(path, code)
        try:
            case = _load_yaml(raw, _StrictSafeLoader, f'golden/{case_id}.yaml')
        except ConfigurationError as error:
            raise ProfileError(code, f'{case_id}: {error}') from error
        request = case.get('request') if isinstance(case, dict) else None
        source = request.get('source_image', 'none') if isinstance(request, dict) else 'none'
        image = None
        if source != 'none':
            try:
                _filename(source, f'golden/{case_id}.yaml', 'request.source_image')
                path = _inside(bundle / GOLDEN_DIR / source, root)
            except ConfigurationError as error:
                raise ProfileError(code, f'{case_id}: source image {source!r}: {error}') from error
            image = _read_bytes(path, code)
        inputs.append((case_id, raw, case, image))
    return inputs


def _golden_fingerprint(inputs):
    """Hash of every golden case file and the image it names, in id order (the F5 class, part b).

    A changed, deleted or added case or image gives another hash; the case bytes carry the image name.
    """
    entries = [[case_id, sha256_tag(raw), None if image is None else sha256_tag(image)]
               for case_id, raw, _, image in sorted(inputs, key=lambda item: item[0])]
    return sha256_tag(json.dumps(entries, separators=(',', ':')).encode('utf-8'))


async def check_profile(config, profile_id, runner, *, project_root):
    """Run conflict lint and golden tests; write `activation.json` only when everything passes.

    `runner.identity` names the pipeline runner; `await runner.run(view, request)` returns an envelope.
    Checks of one profile are serialized across processes by `check.lock`, held from invalidation through
    publication, so a newer check that fails always leaves the profile inactive (F1).
    """
    root = _root(project_root)
    bundle = _bundle(config, profile_id, root)
    lock = _bundle_lock(config, bundle, root, 'profile check')
    config = deepcopy(config)  # one configuration snapshot for the view, the addendum and the fingerprint (F4)
    await _acquire(lock, profile_id)
    try:
        return await _check_locked(config, profile_id, runner, bundle, root)
    finally:
        lock.release()


async def _check_locked(config, profile_id, runner, bundle, root):
    # A previous activation never survives a new check, whatever its outcome.
    _inside(bundle / ACTIVATION_FILE, root).unlink(missing_ok=True)
    schemas = _schema_snapshot(root)  # read before the manifest, so the final comparison covers its validation
    manifest, manifest_raw, prompt = _read_bundle(bundle, root)
    if manifest['profile_id'] != profile_id:
        raise ProfileError('PROFILE_INVALID', f"manifest profile_id {manifest['profile_id']!r} is not the folder name")
    _require_keys(manifest, config, bundle / MANIFEST_FILE)
    if manifest['status'] not in CHECKED_STATUSES:
        raise ProfileError('PROFILE_NOT_CONFIRMED', f"status is {manifest['status']}; the admin confirms first")
    if manifest['system_prompt_hash'] != sha256_tag(prompt):
        raise ProfileError('PROFILE_HASH_MISMATCH', 'system_prompt.txt differs from the hash the manifest records')
    findings = conflict_findings(manifest['conflicts'])
    for code in (None, 'CONFLICT_UNDECIDED', 'CONFLICT_BLOCKS'):
        selected = [item for item in findings if (item[0] not in ('CONFLICT_UNDECIDED', 'CONFLICT_BLOCKS')
                                                  if code is None else item[0] == code)]
        if selected:
            name = {None: 'PROFILE_CONFLICT_INVALID', 'CONFLICT_UNDECIDED': 'PROFILE_CONFLICT_UNDECIDED',
                    'CONFLICT_BLOCKS': 'PROFILE_CONFLICT_BLOCKS'}[code]
            raise ProfileError(name, ', '.join(f'{cid}: {detail}' for _, cid, detail in selected), findings=selected)
    view = _view(profile_id, manifest, manifest_raw, prompt, config, root, None)
    cases, golden_inputs_sha256 = _golden_cases(manifest, bundle, root, schemas[GOLDEN_SCHEMA][1])
    results = []
    for case, image in cases:
        request = {'task': case['request']['task'], 'prompt': case['request']['prompt'], 'source_image': image}
        entry = {'id': case['id'], 'status': None, 'passed': False, 'envelope_sha256': None}
        try:
            envelope = await runner.run(view, request)
        except Exception as error:  # a runner failure fails the case; it never activates the profile
            entry['error'] = type(error).__name__
            results.append(entry)
            continue
        errors = (_errors(schemas[ENVELOPE_SCHEMA][1], envelope) if isinstance(envelope, dict)
                  else ['root: not an object'])
        if errors:
            entry['error'] = 'ENVELOPE_INVALID'
        else:
            entry['status'] = envelope['status']
            entry['passed'] = envelope['status'] in case['expect']['status']
            entry['envelope_sha256'] = sha256_tag(json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                                                             allow_nan=False).encode('utf-8'))
        results.append(entry)
    report = {'profile_id': profile_id, 'runner': str(runner.identity), 'cases': results}
    report_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    atomic_write_bytes(_inside(bundle / GOLDEN_RESULTS_FILE, root), report_bytes)
    failed = [entry['id'] for entry in results if not entry['passed']]
    if failed:
        raise ProfileError('PROFILE_GOLDEN_FAILED', f'golden tests failed: {failed}', results=results)
    addendum = None if view.addendum is None else {'id': view.addendum['id'], 'sha256': view.addendum['sha256']}
    activation = {'profile_id': profile_id, 'system_prompt_sha256': view.system_prompt_sha256,
                  'manifest_sha256': view.manifest_sha256, 'addendum': addendum,
                  'config_keys_sha256': _config_fingerprint(manifest, config),
                  'golden_inputs_sha256': golden_inputs_sha256,  # the bytes the runner was given
                  'golden_results_sha256': sha256_tag(report_bytes),
                  'schemas_sha256': {schema_id: sha for schema_id, (sha, _) in schemas.items()},
                  'check_code_sha256': CHECK_CODE_SHA256, 'runner': str(runner.identity),
                  'checked_at': datetime.now(timezone.utc).isoformat()}
    record = json.dumps(activation, indent=2).encode('utf-8') + b'\n'  # the bytes exist before the comparison
    # Every input must still be the one tested. A change after this comparison and before publication leaves
    # a record whose hashes differ from the files, which resolve refuses (fails closed).
    _, current_raw, current_prompt = _read_bundle(bundle, root)
    current_inputs = _golden_inputs(manifest['golden_tests'], bundle, root, 'PROFILE_CHANGED')
    current_addendum = render_addendum(manifest['conflicts'], config, load_prompt(root, ADDENDUM_ID))
    if current_raw != manifest_raw or current_prompt != prompt \
            or _golden_fingerprint(current_inputs) != golden_inputs_sha256 \
            or _schema_hashes(root) != activation['schemas_sha256'] \
            or (current_addendum and current_addendum['sha256']) != (addendum and addendum['sha256']):
        raise ProfileError('PROFILE_CHANGED', 'the profile or an input of its check changed during the check; '
                           'run it again')
    atomic_write_bytes(_inside(bundle / ACTIVATION_FILE, root), record)
    return {'activation': activation, 'results': results}


def resolve_profile(config, profile_id=None, *, project_root):
    """S0: the request's profile, else `profiles.default_profile_id`; refused unless active and unchanged."""
    root = _root(project_root)
    if profile_id is None:
        profile_id = config['profiles']['default_profile_id']
        _sentinel(profile_id, 'config', 'profiles.default_profile_id', {})
    bundle = _bundle(config, profile_id, root)
    path = bundle / ACTIVATION_FILE
    if not path.exists():
        raise ProfileError('PROFILE_NOT_ACTIVE', f'{profile_id!r} has not passed profile check')
    try:
        activation = json.loads(_read_bytes(_inside(path, root), 'PROFILE_NOT_ACTIVE'))
    except ValueError as error:
        raise ProfileError('PROFILE_NOT_ACTIVE', f'{ACTIVATION_FILE} is not valid JSON') from error
    manifest, manifest_raw, prompt = _read_bundle(bundle, root)
    if manifest['status'] not in CHECKED_STATUSES or not isinstance(activation, dict) \
            or activation.get('profile_id') != profile_id:
        raise ProfileError('PROFILE_NOT_ACTIVE', f'{profile_id!r} is not active')
    if activation.get('system_prompt_sha256') != sha256_tag(prompt) \
            or activation.get('manifest_sha256') != sha256_tag(manifest_raw):
        raise ProfileError('PROFILE_NOT_CHECKED', f'{profile_id!r} changed after profile check; run it again')
    if activation.get('check_code_sha256') != CHECK_CODE_SHA256:
        raise ProfileError('PROFILE_NOT_CHECKED', 'the check code or its libraries differ from the checked ones; '
                           'run profile check')
    if activation.get('schemas_sha256') != _schema_hashes(root):
        raise ProfileError('PROFILE_NOT_CHECKED', f'a schema of the check ({", ".join(CHECK_SCHEMAS)}) differs from '
                           'the checked one; run profile check')
    _require_keys(manifest, config, bundle / MANIFEST_FILE)
    if activation.get('config_keys_sha256') != _config_fingerprint(manifest, config):
        raise ProfileError('PROFILE_NOT_CHECKED', 'a configuration value the conflicts cite differs from the checked '
                           'one; run profile check')
    # The evidence must be the results the check wrote: missing hash, missing file or other bytes refuse (F5).
    recorded = activation.get('golden_results_sha256')
    results = _read_bytes(_inside(bundle / GOLDEN_RESULTS_FILE, root), 'PROFILE_NOT_CHECKED')
    if not isinstance(recorded, str) or recorded != sha256_tag(results):
        raise ProfileError('PROFILE_NOT_CHECKED', f'{GOLDEN_RESULTS_FILE} differs from the checked results; '
                           'run profile check')
    inputs = _golden_inputs(manifest['golden_tests'], bundle, root, 'PROFILE_NOT_CHECKED')
    if activation.get('golden_inputs_sha256') != _golden_fingerprint(inputs):
        raise ProfileError('PROFILE_NOT_CHECKED', 'a golden case file or image differs from the checked ones; '
                           'run profile check')
    view = _view(profile_id, manifest, manifest_raw, prompt, config, root, activation)
    expected = None if view.addendum is None else {'id': view.addendum['id'], 'sha256': view.addendum['sha256']}
    if activation.get('addendum') != expected:
        raise ProfileError('PROFILE_NOT_CHECKED', 'the runtime addendum differs from the checked one; run profile check')
    return view


# Computed once at import: it fingerprints the code this process runs, whatever changes on disk later.
CHECK_CODE_SHA256 = _code_fingerprint()
