"""Load explicitly configured pipeline and admin profiles."""

from copy import deepcopy
import errno
from functools import lru_cache
import json
import os
from pathlib import Path, PureWindowsPath

from jsonschema import Draft202012Validator, exceptions
import yaml


SENTINELS = frozenset(('fit_from_gt', 'fit_from_bench', 'fit_from_pilot', 'pin_required',
                       'deploy_required', 'design_required', 'ask_user'))
SCHEMA_CACHE_CAPACITY = 128  # Safety-bound cap for in-process schema cache entries.

# Microsoft, "Naming Files, Paths, and Namespaces": "Do not use the following
# reserved names for the name of a file:" (protocol-bound Windows names).
# "Do not end a file or directory name with a space or a period."
# "Use two consecutive periods (..) as a directory component in a path to
# represent the parent of the current directory"; separators delimit components.
# https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
WINDOWS_DEVICE_NAMES = frozenset(
    ('CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$')
    + tuple(f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in '0123456789\u00b9\u00b2\u00b3')
)

# Only documented reads are listed; later stage tasks complete their read sets.
COMMAND_READS = {
    'kb status': ('storage.kb_store', 'storage.lock'),
    'kb sync': ('knowledge', 'sync', 'embedding', 'observe', 'cards', 'aliases', 'index', 'storage.kb_store',
                'storage.lock', 'storage.table_format',
                'vlm.roles.observer', 'vlm.roles.info_assessor', 'vlm.roles.folder_namer',
                'vlm.roles.card_builder', 'vlm.roles.alias_generator', 'vision'),
    'kb rollback': ('storage.kb_store', 'storage.lock', 'storage.table_format'),
    'kb gc': ('storage.kb_store', 'storage.lock', 'storage.table_format', 'sync.gc.retention', 'sync.snapshots.keep'),
    'kb audit': ('storage.kb_store', 'storage.lock'),
    'kb show': ('storage.kb_store', 'storage.lock'),
    'profile register': ('profiles.root', 'vlm.roles.manifest_drafter'),
    'profile check': ('profiles.root',),
    'enhance once': ('profiles', 'rag', 'vision', 'candidates', 'embedding', 'planner', 'resolution',
                     'clarification', 'creativity', 'edit', 'assembly', 'writer', 'validation', 'runtime',
                     'storage.data_root', 'storage.table_format',
                     'vlm.roles.planner', 'vlm.roles.writer', 'vlm.roles.verifier'),
    'enhance serve': ('profiles', 'rag', 'vision', 'candidates', 'embedding', 'planner', 'resolution',
                      'clarification', 'creativity', 'edit', 'assembly', 'writer', 'validation', 'runtime',
                      'storage.data_root', 'storage.table_format',
                      'vlm.roles.planner', 'vlm.roles.writer', 'vlm.roles.verifier', 'serving'),
    'eval build': ('evaluation', 'storage.kb_store', 'storage.lock', 'storage.data_root', 'storage.table_format',
                   'vlm.roles.scenario_generator'),
    'eval run': ('evaluation', 'profiles', 'rag', 'vision', 'candidates', 'embedding', 'planner', 'resolution',
                 'clarification', 'creativity', 'edit', 'assembly', 'writer', 'validation', 'runtime',
                 'storage.data_root', 'storage.table_format',
                 'vlm.roles.planner', 'vlm.roles.writer', 'vlm.roles.verifier'),
    'eval fit': ('evaluation', 'storage.data_root'),
}


class ConfigurationError(ValueError):
    """Invalid configuration with its source and key."""


def _validate_segments(value, path, key, *, allow_navigation=True):
    """Enforce a portable spelling before any platform normalization."""
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ConfigurationError(f'{path}: {key}: expected a non-empty path')
    parts = value.replace('\\', '/').split('/')
    for index, part in enumerate(parts):
        if not part:
            continue
        if part in ('.', '..'):
            if allow_navigation:
                continue
            raise ConfigurationError(f'{path}: {key}: invalid path segment {part!r}')
        if index == 0 and len(part) == 2 and part[0].isalpha() and part[1] == ':':
            continue  # Drive prefix; containment is checked separately.
        stem = part.split('.', 1)[0].rstrip(' ').upper()
        if part != part.strip() or part.endswith('.') or stem in WINDOWS_DEVICE_NAMES:
            raise ConfigurationError(f'{path}: {key}: invalid path segment {part!r}')


def _resolve_within(candidate, root, forbidden, path, key):
    """Resolve links component by component without touching protected targets."""
    pending = list(candidate.relative_to(root).parts)
    current = root
    visited = set()
    while pending:
        part = pending.pop(0)
        if current == root and os.path.normcase(part) in forbidden:
            raise ConfigurationError(f'{path}: {key}: protected project directory')
        target = current / part
        try:
            link = os.readlink(target)
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENOENT, errno.ENOTDIR):
                current = Path(os.path.realpath(target))
                if not current.is_relative_to(root):
                    raise ConfigurationError(f'{path}: {key}: resolved outside project root')
                parts = current.relative_to(root).parts
                if parts and os.path.normcase(parts[0]) in forbidden:
                    raise ConfigurationError(f'{path}: {key}: protected project directory')
                continue
            raise ConfigurationError(f'{path}: {key}: cannot inspect link: {error}') from error
        if target in visited:
            raise ConfigurationError(f'{path}: {key}: link cycle')
        visited.add(target)
        if os.name == 'nt' and link.startswith('\\\\?\\UNC\\'):
            link = '\\\\' + link[8:]
        elif os.name == 'nt' and link.startswith('\\\\?\\'):
            link = link[4:]
        resolved = Path(os.path.abspath(target.parent / link))
        if not resolved.is_relative_to(root):
            raise ConfigurationError(f'{path}: {key}: resolved outside project root')
        pending = list(resolved.relative_to(root).parts) + pending
        current = root
    return current


def _inside(path, root):
    path = _lexical_path(str(path), root, path, 'path')
    resolved = _resolve_within(path, root, {os.path.normcase('.secrets')}, path, 'path')
    if resolved == root:
        raise ConfigurationError(f'{path}: path: must not be the project root')
    return resolved


class _StrictSafeLoader(yaml.SafeLoader):
    """Reject duplicate explicit keys before YAML merge keys are flattened."""

    def __init__(self, stream):
        super().__init__(stream)
        self._checked_mappings = set()

    def flatten_mapping(self, node):
        if id(node) not in self._checked_mappings:
            self._checked_mappings.add(id(node))
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == 'tag:yaml.org,2002:merge':
                    continue
                key = self.construct_object(key_node, deep=True)
                if not isinstance(key, str):
                    raise yaml.constructor.ConstructorError(
                        'while constructing a mapping', node.start_mark,
                        f'non-string mapping key {key_node.value!r}', key_node.start_mark)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        'while constructing a mapping', node.start_mark,
                        f'duplicate key {key!r}', key_node.start_mark)
                seen.add(key)
        return super().flatten_mapping(node)


def _read_yaml(path):
    try:
        result = yaml.load(path.read_bytes(), Loader=_StrictSafeLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigurationError(f'{path}: file: {error}') from error
    if not isinstance(result, dict):
        raise ConfigurationError(f'{path}: root: expected a mapping')
    return result


@lru_cache(maxsize=SCHEMA_CACHE_CAPACITY)
def _cached_schema(path, mtime_ns, size):
    del mtime_ns, size
    result = json.loads(path.read_bytes())
    Draft202012Validator.check_schema(result)
    return result


def _schema(root, schema_id):
    try:
        _validate_segments(schema_id, root, 'schema_id', allow_navigation=False)
    except ConfigurationError as error:
        raise ConfigurationError(str(error).removeprefix(f'{root}: schema_id: ')) from error
    if not isinstance(schema_id, str) or not schema_id or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_.' for c in schema_id):
        raise ConfigurationError(f'invalid schema id {schema_id!r}')
    try:
        path = _inside(root / 'contracts' / 'schemas' / (schema_id + '.schema.json'), root)
    except ConfigurationError as error:
        raise ConfigurationError(str(error).partition(': path: ')[2] or str(error)) from error
    try:
        stat = path.stat()
        return _cached_schema(path, stat.st_mtime_ns, stat.st_size)
    except OSError as error:
        raise ConfigurationError(f'cannot load schema {path}: {error.strerror or str(error)}') from error
    except (ValueError, exceptions.SchemaError) as error:
        raise ConfigurationError(f'invalid schema {path}: {error}') from error


def _source(sources, key, fallback):
    while key:
        if key in sources:
            return sources[key]
        key = key.rpartition('.')[0]
    return fallback


def _lexical_path(value, root, path, key):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ConfigurationError(f'{path}: {key}: expected a non-empty path')
    if PureWindowsPath(value).drive and os.name != 'nt':
        raise ConfigurationError(f'{path}: {key}: outside project root')
    supplied = Path(value.replace('\\', '/'))
    if supplied.is_absolute():
        try:
            spelling = str(supplied.relative_to(root))
        except ValueError as error:
            raise ConfigurationError(f'{path}: {key}: outside project root') from error
    else:
        spelling = value
    _validate_segments(spelling, path, key)
    candidate = Path(os.path.abspath(root / supplied))
    if not candidate.is_relative_to(root):
        raise ConfigurationError(f'{path}: {key}: outside project root')
    return candidate


def _configured_path(value, root, path, key, *, protected=True, secret=False):
    if isinstance(value, str) and value in SENTINELS:
        return
    if secret and value == 'none':
        return
    candidate = _lexical_path(value, root, path, key)
    if candidate == root:
        raise ConfigurationError(f'{path}: {key}: must not be the project root')
    if secret:
        parts = candidate.relative_to(root).parts
        if len(parts) < 2 or parts[0] != '.secrets':
            raise ConfigurationError(f'{path}: {key}: expected a file under .secrets/')
        return  # Never resolve, open, list or stat a secret path.
    forbidden = {os.path.normcase(name) for name in ('.secrets', '.git', '.venv')}
    if protected:
        forbidden.add(os.path.normcase('models'))
    resolved = _resolve_within(candidate, root, forbidden, path, key)
    if resolved == root:
        raise ConfigurationError(f'{path}: {key}: must not be the project root')
    parts = resolved.relative_to(root).parts
    if parts and os.path.normcase(parts[0]) in forbidden:
        raise ConfigurationError(f'{path}: {key}: protected project directory')


def _filename(value, path, key):
    if isinstance(value, str) and value in SENTINELS:
        return
    _validate_segments(value, path, key, allow_navigation=False)
    if value in ('.', '..') or any(c in value for c in ('/', '\\', ':')):
        raise ConfigurationError(f'{path}: {key}: expected a filename')


def _validate_configured_paths(config, root, path, sources):
    for key in ('knowledge.root', 'storage.data_root', 'storage.kb_store', 'profiles.root'):
        try:
            value = _get(config, key, path)
        except ConfigurationError:
            continue
        _configured_path(value, root, _source(sources, key, path), key)
    knowledge = config.get('knowledge')
    if isinstance(knowledge, dict):
        key = 'knowledge.overrides_file'
        if 'overrides_file' in knowledge:
            value = knowledge['overrides_file']
            if value != 'none':
                _filename(value, _source(sources, key, path), key)
        info = knowledge.get('info_file')
        if isinstance(info, dict) and isinstance(info.get('names'), list):
            for index, value in enumerate(info['names']):
                key = f'knowledge.info_file.names.{index}'
                _filename(value, _source(sources, key, path), key)
    profiles = config.get('profiles')
    if isinstance(profiles, dict):
        key = 'profiles.default_profile_id'
        if 'default_profile_id' in profiles:
            _filename(profiles['default_profile_id'], _source(sources, key, path), key)
        if isinstance(profiles.get('exclude'), list):
            for index, value in enumerate(profiles['exclude']):
                key = f'profiles.exclude.{index}'
                _filename(value, _source(sources, key, path), key)
    vlm = config.get('vlm')
    backends = vlm.get('backends') if isinstance(vlm, dict) else None
    if isinstance(backends, dict):
        for name, backend in backends.items():
            if not isinstance(backend, dict):
                continue
            key = f'vlm.backends.{name}.api_key_file'
            if 'api_key_file' in backend:
                _configured_path(backend['api_key_file'], root, _source(sources, key, path), key, secret=True)
            model = backend.get('model')
            if isinstance(model, dict):
                for field in ('file', 'mmproj_file'):
                    key = f'vlm.backends.{name}.model.{field}'
                    if field in model and model[field] != 'none':
                        _configured_path(model[field], root, _source(sources, key, path), key, protected=False)


def _mark_sources(data, path, sources, prefix=''):
    for name, value in data.items():
        key = '.'.join(filter(None, (prefix, str(name))))
        sources[key] = path
        if isinstance(value, dict):
            _mark_sources(value, path, sources, key)


def _validate(value, schema, path, prefix='', sources=None):
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        def leaves(error):
            if not error.context:
                return [error]
            return [child for entry in error.context for child in leaves(entry)]

        candidates = [item for error in errors for item in leaves(error)]
        candidates = [item for item in candidates if not (
            item.validator == 'enum' and set(item.validator_value) == SENTINELS
        )] or candidates
        error = max(candidates, key=lambda item: len(item.absolute_path))
        parts = [str(p) for p in error.absolute_path]
        if error.validator == 'required' and isinstance(error.instance, dict):
            missing = sorted(set(error.validator_value) - set(error.instance))
            if missing:
                parts.append(missing[0])
        if error.validator == 'additionalProperties' and isinstance(error.instance, dict):
            extra = sorted(set(error.instance) - set(error.schema.get('properties', {})))
            if extra:
                parts.append(extra[0])
        key = '.'.join(parts)
        key = '.'.join(part for part in (prefix, key) if part) or 'root'
        path = _source(sources or {}, key, path)
        raise ConfigurationError(f'{path}: {key}: {error.message}')


def _merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _load_chain(path, root, stack, sources):
    path = _inside(path, root)
    if path in stack:
        raise ConfigurationError(f'{path}: extends: cycle')
    data = _read_yaml(path)
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ConfigurationError(f'{path}: schema_version: expected 1')
    if 'extends' in data and 'overlay_of' in data:
        raise ConfigurationError(f'{path}: extends: cannot combine with overlay_of')
    if 'overlay_of' in data:
        _overlay(data, path)
    ref_key = 'extends' if 'extends' in data else 'overlay_of'
    ref = data.get(ref_key)
    if ref is not None:
        if not isinstance(ref, str) or not ref:
            raise ConfigurationError(f'{path}: {ref_key}: expected a path')
        _validate_segments(ref, path, ref_key)
        own = {k: v for k, v in data.items() if k not in ('extends', 'overlay_of')}
        try:
            base = _load_chain(path.parent / ref, root, (*stack, path), sources)
        except ConfigurationError as error:
            raise ConfigurationError(f'{path}: {ref_key}: {error}') from error
        data = _merge(base, own)
        _mark_sources(own, path, sources)
    else:
        _mark_sources(data, path, sources)
    return data


def _overlay(overlay, path):
    allowed = {'schema_version', 'overlay_of', 'status', 'storage', 'runtime', 'serving', 'vlm'}
    unknown = set(overlay) - allowed
    if unknown:
        raise ConfigurationError(f'{path}: {sorted(unknown)[0]}: not a deployment overlay key')
    vlm = overlay.get('vlm', {})
    if not isinstance(vlm, dict) or set(vlm) - {'backends'}:
        raise ConfigurationError(f'{path}: vlm: only backends may be overridden')
    backends = vlm.get('backends', {})
    if not isinstance(backends, dict):
        raise ConfigurationError(f'{path}: vlm.backends: expected a mapping')
    for name, backend in backends.items():
        if not isinstance(backend, dict):
            raise ConfigurationError(f'{path}: vlm.backends.{name}: expected a mapping')
        invalid = set(backend) - {'provider', 'base_urls', 'model', 'extra_body', 'max_concurrency', 'endpoints'}
        if invalid:
            raise ConfigurationError(f'{path}: vlm.backends.{name}.{sorted(invalid)[0]}: not a deployment value')


def _lineage(path, root):
    paths = []
    while True:
        path = _inside(path, root)
        if path in paths:
            raise ConfigurationError(f'{path}: extends: cycle')
        paths.append(path)
        data = _read_yaml(path)
        ref_key = 'extends' if 'extends' in data else 'overlay_of'
        ref = data.get(ref_key)
        if ref is None:
            return paths
        if not isinstance(ref, str) or not ref:
            raise ConfigurationError(f'{path}: {ref_key}: expected a path')
        _validate_segments(ref, path, ref_key)
        path = path.parent / ref


def _sentinel(value, path, key, sources):
    if isinstance(value, str) and value in SENTINELS:
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: unresolved sentinel {value}')
    if isinstance(value, dict):
        for name, child in value.items():
            _sentinel(child, path, f'{key}.{name}', sources)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _sentinel(child, path, f'{key}.{index}', sources)


def _get(config, key, path):
    value = config
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            raise ConfigurationError(f'{path}: {key}: missing key')
        value = value[part]
    return value


def _command_reads(command, config):
    keys = COMMAND_READS[command]
    if command not in ('enhance once', 'enhance serve', 'eval run'):
        return keys
    if not isinstance(config['rag'], dict):
        return keys
    if config['rag']['enabled']:
        return (*keys, 'storage.kb_store', 'storage.lock')
    return tuple('rag.enabled' if key == 'rag' else key for key in keys
                 if key not in ('candidates', 'embedding', 'assembly'))


def load_config(path, command, *, overlay=None, project_root=None):
    """Load configuration; relative input and overlay paths start at the project root."""
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    path = _inside(path, root)
    if command not in COMMAND_READS:
        raise ConfigurationError(f'{path}: command: unknown command {command!r}')
    sources = {}
    config = _load_chain(path, root, (), sources)
    if overlay is not None:
        overlay_path = _inside(overlay, root)
        data = _read_yaml(overlay_path)
        if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
            raise ConfigurationError(f'{overlay_path}: schema_version: expected 1')
        _overlay(data, overlay_path)
        ref = data.get('overlay_of')
        if not isinstance(ref, str) or not ref:
            raise ConfigurationError(f'{overlay_path}: overlay_of: expected a path')
        _validate_segments(ref, overlay_path, 'overlay_of')
        try:
            referenced = _inside(overlay_path.parent / ref, root)
            if referenced not in _lineage(path, root):
                raise ConfigurationError(f'{overlay_path}: overlay_of: base does not match selected configuration')
        except ConfigurationError as error:
            if str(error).startswith(f'{overlay_path}: overlay_of:'):
                raise
            raise ConfigurationError(f'{overlay_path}: overlay_of: {error}') from error
        own = {k: v for k, v in data.items() if k != 'overlay_of'}
        config = _merge(config, own)
        _mark_sources(own, overlay_path, sources)
    try:
        config_schema = _schema(root, 'enhancer_config.v0')
    except ConfigurationError as error:
        raise ConfigurationError(f'{path}: schema_version: {error}') from error
    _validate(config, config_schema, path, sources=sources)
    _validate_configured_paths(config, root, path, sources)
    for name, role in config['vlm']['roles'].items():
        if not isinstance(role, dict) or not isinstance(role.get('output'), dict):
            continue
        schema_id = role['output'].get('schema_id')
        if schema_id not in SENTINELS and schema_id not in ('none', 'from_profile_manifest'):
            try:
                _schema(root, schema_id)
            except ConfigurationError as error:
                key = f'vlm.roles.{name}.output.schema_id'
                raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {error}') from error
    for key in _command_reads(command, config):
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        if key.startswith('vlm.roles.'):
            try:
                role_schema = _schema(root, 'vlm_role.v0')
            except ConfigurationError as error:
                raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {error}') from error
            _validate(value, role_schema, path, key, sources)
            backend_key = 'vlm.backends.' + value['backend']
            _sentinel(_get(config, backend_key, path), path, backend_key, sources)
    return config


def load_profile(path, *, project_root=None):
    """Load an admin profile; a relative input path starts at the project root."""
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    path = _inside(path, root)
    manifest_path = _inside(path / 'manifest.yaml', root)
    manifest = _read_yaml(manifest_path)
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ConfigurationError(f'{manifest_path}: schema_version: expected 1')
    try:
        manifest_schema = _schema(root, 'profile_manifest.v0')
    except ConfigurationError as error:
        raise ConfigurationError(f'{manifest_path}: schema_version: {error}') from error
    _validate(manifest, manifest_schema, manifest_path)
    _filename(manifest['profile_id'], manifest_path, 'profile_id')
    try:
        prompt_path = _inside(path / 'system_prompt.txt', root)
        prompt = prompt_path.read_bytes()
    except (OSError, ConfigurationError) as error:
        raise ConfigurationError(f'{manifest_path}: system_prompt.txt: {error}') from error
    return manifest, prompt


def resolve_writer_format(role, manifest, *, project_root=None, role_source='config.yaml', manifest_source='manifest.yaml'):
    """Resolve writer references, attributing refusals to the caller's source files."""
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    result = deepcopy(role)
    output = result.get('output', {})
    if output.get('format') != 'from_profile_manifest' or output.get('schema_id') != 'from_profile_manifest':
        raise ConfigurationError(f'{role_source}: writer.output: expected paired from_profile_manifest references')
    schema_id = manifest.get('output_schema_id')
    if not isinstance(schema_id, str) or not schema_id:
        raise ConfigurationError(f'{manifest_source}: output_schema_id: missing or invalid')
    if schema_id != 'none':
        try:
            _schema(root, schema_id)
        except ConfigurationError as error:
            raise ConfigurationError(f'{manifest_source}: output_schema_id: {error}') from error
    output['schema_id'] = schema_id
    output['format'] = 'none' if schema_id == 'none' else 'json_schema'
    return result
