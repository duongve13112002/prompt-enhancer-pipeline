"""Load explicitly configured pipeline and admin profiles."""

from copy import deepcopy
import codecs
import errno
from functools import lru_cache
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re

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
    'kb sync': ('knowledge', 'sync', 'embedding', 'observe', 'cards', 'aliases', 'index', 'storage.kb_store', 'storage.data_root',
                'storage.lock', 'storage.table_format', 'runtime.cpu_workers',
                'vlm.roles.observer', 'vlm.roles.info_assessor', 'vlm.roles.folder_namer',
                'vlm.roles.card_builder', 'vlm.roles.alias_generator', 'vision'),
    'kb rollback': ('storage.kb_store', 'storage.lock', 'storage.table_format'),
    'kb gc': ('storage.kb_store', 'storage.lock', 'storage.table_format', 'sync.gc.retention', 'sync.snapshots.keep'),
    'kb audit': ('storage.kb_store', 'storage.lock'),
    'kb show': ('storage.kb_store', 'storage.lock'),
    # P (T-012): register sends `creativity` to the drafter as policy data; check also reads the keys the
    # manifest's conflicts cite, which are known only after the manifest is read (profiles.py), and
    # `storage.lock` bounds the wait for the bundle lock that check (fix round 1, F1) and register's
    # publication (fix round 2) hold.
    'profile register': ('profiles.root', 'profiles.exclude', 'creativity', 'vlm.roles.manifest_drafter',
                         'storage.lock'),
    'profile check': ('profiles.root', 'profiles.exclude', 'storage.lock'),
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


class _MetaLoader(_StrictSafeLoader):
    """The one loader for admin-written profile `meta` ([META] and the manifest field; T-012 F3).

    A subset of the YAML 1.2 core schema. Typed: `true` and `false`; decimal integers and floats without a
    leading zero; null (refused by its callers). Everything else stays the string as written: `yes`, `no`, `on`,
    `off`, `True`, dates, `007`, `0x1F`, `1_000`, `12:30`. The class owns its resolver table, so SafeLoader's
    YAML 1.1 table and the global one stay unchanged; duplicate and non-string keys are still refused.
    """

    yaml_implicit_resolvers = {}  # own table: `add_implicit_resolver` appends here, not to SafeLoader's


# Protocol-bound YAML 1.2 core syntax (spec 1.2.2, section 10.3.2), narrowed as the docstring says.
_MetaLoader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$'), list('tf'))
_MetaLoader.add_implicit_resolver('tag:yaml.org,2002:int', re.compile(r'^[-+]?(?:0|[1-9][0-9]*)$'),
                                  list('-+0123456789'))
_MetaLoader.add_implicit_resolver(
    'tag:yaml.org,2002:float',
    re.compile(r'^(?:[-+]?(?:\.[0-9]+|(?:0|[1-9][0-9]*)(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?'
               r'|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$'),
    list('-+0123456789.'))
_MetaLoader.add_implicit_resolver('tag:yaml.org,2002:null', re.compile(r'^(?:~|null|Null|NULL|)$'),
                                  ['~', 'n', 'N', ''])


# Protocol-bound: line breaks for YAML 1.1 (PyYAML: installed yaml/scanner.py scan_line_break folds a raw NEL
# into LF) but ordinary text for YAML 1.2, so their meaning depends on the reader (T-012 fix round 2).
_AMBIGUOUS_BREAKS = '\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}'  # NEL, LS, PS
_AMBIGUOUS_BREAK = re.compile('[' + _AMBIGUOUS_BREAKS + ']')
_ESCAPES = dict(zip(_AMBIGUOUS_BREAKS, ('\\N', '\\L', '\\P')))  # the double-quoted YAML escapes
# What PyYAML raises besides YAMLError on an input it cannot represent: `int('1x')` under `!!int`, `chr()` of an
# escape above U+10FFFF (ValueError, OverflowError), and composition deeper than the interpreter's stack.
_YAML_FAILURES = (yaml.YAMLError, ValueError, OverflowError, RecursionError)


def _yaml_text(loader):
    """Refuse a raw NEL, LS or PS anywhere in the text; the escapes `\\N`, `\\L` and `\\P` stay accepted.

    A bytes or str input is decoded whole when the loader is built (installed yaml/reader.py, `__init__` and
    `determine_encoding`), so `loader.buffer` is the full text plus a final NUL.
    """
    match = _AMBIGUOUS_BREAK.search(loader.buffer)
    if match:
        index = match.start()
        line, column = loader.buffer.count('\n', 0, index) + 1, index - loader.buffer.rfind('\n', 0, index)
        raise yaml.YAMLError(f'raw U+{ord(match.group()):04X} at line {line} column {column}: YAML 1.1 reads it as '
                             f'a line break and YAML 1.2 as text; write the escape {_ESCAPES[match.group()]} inside '
                             'double quotes')


def _acyclic(node):
    """Refuse a recursive alias (a collection inside itself has no JSON value); iterative, so depth is no limit."""
    if not isinstance(node, yaml.CollectionNode):
        return
    def children(item):
        return iter([part for pair in item.value for part in pair] if isinstance(item, yaml.MappingNode)
                    else item.value)
    active, done, stack = {id(node)}, set(), [(node, children(node))]
    while stack:
        current, pending = stack[-1]
        for child in pending:
            if not isinstance(child, yaml.CollectionNode) or id(child) in done:
                continue
            if id(child) in active:
                raise yaml.constructor.ConstructorError(None, None, 'recursive alias: a collection contains itself',
                                                        child.start_mark)
            active.add(id(child))
            stack.append((child, children(child)))
            break
        else:
            stack.pop()
            active.discard(id(current))
            done.add(id(current))


def _yaml_failure(error, path):
    if isinstance(error, yaml.YAMLError):
        detail = str(error)
    elif isinstance(error, RecursionError):
        detail = 'nested too deeply to read'
    else:
        detail = f'cannot construct a value (an escape or a tagged literal is out of range): {error}'
    return ConfigurationError(f'{path}: file: {detail}')


def _load_yaml(raw, loader_class, path):
    """Load one YAML document with `loader_class`, refusing input it would not read faithfully (fix round 2).

    The admin-owned P files ([META], golden cases) read through here; the manifest through `_parse_manifest`.
    """
    try:
        loader = loader_class(raw)
        try:
            _yaml_text(loader)
            node = loader.get_single_node()
            _acyclic(node)
            return None if node is None else loader.construct_document(node)
        finally:
            loader.dispose()
    except _YAML_FAILURES as error:
        raise _yaml_failure(error, path) from error


def _parse_manifest(raw, path):
    """Parse manifest bytes: the schema-typed fields as before, `meta` under the META scalar contract.

    The top-level `meta` value node is constructed only by `_MetaLoader` and every other node only by
    `_StrictSafeLoader`, so no other field changes meaning and a META string such as `<<` stays a string.
    The `meta` pair is split off by its position, never by node identity: an alias elsewhere may share the
    node, and that field keeps its value under its own reading (fix round 2, RT1).
    """
    def split(loader):
        node = loader.get_single_node()
        if not isinstance(node, yaml.MappingNode):
            return node, [], None, []
        _acyclic(node)
        pairs = node.value
        found = [index for index, (key, _) in enumerate(pairs) if isinstance(key, yaml.ScalarNode)
                 and key.value == 'meta' and key.tag == 'tag:yaml.org,2002:str']
        if len(found) != 1:
            return node, found, None, []
        index = found[0]
        node.value = pairs[:index] + pairs[index + 1:]
        return node, found, pairs[index][1], [key.value for key, _ in pairs[:index] if isinstance(key, yaml.ScalarNode)]

    try:
        strict, typed = _StrictSafeLoader(raw), _MetaLoader(raw)
        try:
            _yaml_text(strict)
            node, found, _, before = split(strict)
            if len(found) <= 1:
                result = None if node is None else strict.construct_document(node)
                _, _, meta_node, _ = split(typed)
                meta = None if meta_node is None else typed.construct_document(meta_node)
        finally:
            strict.dispose()
            typed.dispose()
    except _YAML_FAILURES as error:
        raise _yaml_failure(error, path) from error
    if len(found) > 1:
        raise ConfigurationError(f'{path}: meta: duplicate key')
    metas = found
    if not isinstance(result, dict):
        raise ConfigurationError(f'{path}: root: expected a mapping')
    if 'meta' in result:
        raise ConfigurationError(f'{path}: meta: expected a top-level meta mapping, not a merged one')
    if metas:
        if isinstance(meta, dict) and any(isinstance(v, float) and not math.isfinite(v) for v in meta.values()):
            raise ConfigurationError(f'{path}: meta: expected finite numbers')
        # Keep `meta` where the admin wrote it, so a rewrite by `confirm_profile` does not move it.
        head = [key for key in result if key in before]
        result = {**{key: result[key] for key in head}, 'meta': meta,
                  **{key: value for key, value in result.items() if key not in head}}
    return result


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


def _schema_path(root, schema_id):
    """The validated, contained file of one schema id (shared with P, which hashes the file's bytes)."""
    try:
        _validate_segments(schema_id, root, 'schema_id', allow_navigation=False)
    except ConfigurationError as error:
        raise ConfigurationError(str(error).removeprefix(f'{root}: schema_id: ')) from error
    if not isinstance(schema_id, str) or not schema_id or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_.' for c in schema_id):
        raise ConfigurationError(f'invalid schema id {schema_id!r}')
    try:
        return _inside(root / 'contracts' / 'schemas' / (schema_id + '.schema.json'), root)
    except ConfigurationError as error:
        raise ConfigurationError(str(error).partition(': path: ')[2] or str(error)) from error


def _schema(root, schema_id):
    path = _schema_path(root, schema_id)
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
        error = max(candidates, key=lambda item: (len(item.absolute_path), item.validator == 'required'))
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
        invalid = set(backend) - {'provider', 'engine', 'base_urls', 'model', 'extra_body', 'max_concurrency', 'endpoints',
                                  'http', 'probe', 'image', 'anthropic_version', 'anthropic_workspace_id'}
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


def _gemini_api_version(route, path):
    """Read the version component of an explicitly configured relative route."""
    _sentinel(route, path, 'endpoints.chat', {})
    version, separator, remainder = route.partition('/')
    if (not separator or not remainder or len(version) < 2 or version[0] != 'v'
            or version[1] not in '0123456789' or not version.isascii() or not version.isalnum()):
        raise ConfigurationError(f'{path}: endpoints.chat: expected a versioned Gemini route')
    return version


def resolve_backend_engine(backend, path):
    """Resolve the configured engine identity without consulting a server."""
    engine = backend['engine']
    _sentinel(engine, path, 'engine', {})
    provider, name = backend['provider'], engine['name']
    if provider == 'openai_compatible':
        if name not in ('vllm', 'llama_cpp'):
            raise ConfigurationError(f'{path}: engine.name: incompatible with provider')
        version = engine['version']
    elif provider in ('anthropic', 'gemini') and name == provider:
        if provider == 'anthropic':
            version = backend['anthropic_version']
            _sentinel(version, path, 'anthropic_version', {})
        else:
            version = _gemini_api_version(backend['endpoints']['chat'], path)
    else:
        raise ConfigurationError(f'{path}: engine.name: incompatible with provider')
    return {'name': name, 'version': version}


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


def _validate_model_bindings(config, path):
    """Reject known provider and encoder incompatibilities before model calls."""
    from PIL import Image
    from .model_transport import endpoint_url

    vlm = config['vlm']
    backends = vlm['backends']
    default = vlm['default_backend']
    if default not in SENTINELS:
        if default not in backends:
            raise ConfigurationError(f'{path}: vlm.default_backend: expected an existing local backend')
        local = backends[default]
        if isinstance(local, dict) and local['provider'] not in SENTINELS and local['provider'] != 'openai_compatible':
            raise ConfigurationError(f'{path}: vlm.default_backend: expected an existing local backend')
    for name, backend in backends.items():
        if isinstance(backend, dict):
            provider = backend['provider']
            engine_name = backend['engine']['name']
            if provider not in SENTINELS and engine_name not in SENTINELS:
                if ((provider == 'openai_compatible' and engine_name not in ('vllm', 'llama_cpp'))
                        or (provider in ('anthropic', 'gemini') and engine_name != provider)):
                    raise ConfigurationError(f'{path}: vlm.backends.{name}.engine.name: incompatible with provider')
            endpoints = backend['endpoints']
            if provider == 'gemini' and isinstance(endpoints, dict) and endpoints['chat'] not in SENTINELS:
                _gemini_api_version(endpoints['chat'], f'{path}: vlm.backends.{name}')
        if isinstance(backend, dict) and backend['provider'] in ('anthropic', 'gemini'):
            if isinstance(backend['base_urls'], list) and 'provider_default' in backend['base_urls']:
                raise ConfigurationError(f'{path}: vlm.backends.{name}.base_urls: provider_default is not documented; configure a URL')
        if not isinstance(backend, dict):
            continue
        bases, endpoints = backend['base_urls'], backend['endpoints']
        if isinstance(bases, list) and len(bases) > 1:
            raise ConfigurationError(f'{path}: vlm.backends.{name}.base_urls: multiple URLs are not supported')
        route = endpoints['chat'] if isinstance(endpoints, dict) else 'chat'
        if route in SENTINELS:
            route = 'chat'
        if isinstance(bases, list):
            for base in bases:
                if base in SENTINELS:
                    continue
                try:
                    endpoint_url(base, route)
                except ValueError as error:
                    raise ConfigurationError(f'{path}: vlm.backends.{name}.base_urls/endpoints.chat: {error}') from error
        if isinstance(endpoints, dict) and endpoints['chat'] not in SENTINELS:
            try:
                endpoint_url('https://localhost', endpoints['chat'])
            except ValueError as error:
                raise ConfigurationError(f'{path}: vlm.backends.{name}.endpoints.chat: {error}') from error
    Image.init()
    for name, role in vlm['roles'].items():
        if not isinstance(role, dict):
            continue
        key = f'vlm.roles.{name}'
        sampling = role['sampling']
        if isinstance(sampling, dict):
            for field, value in sampling.items():
                if isinstance(value, float) and not math.isfinite(value):
                    raise ConfigurationError(f'{path}: {key}.sampling.{field}: expected finite number')
        bound = role['backend']
        if bound in SENTINELS:
            continue
        if bound not in backends:
            raise ConfigurationError(f'{path}: {key}.backend: unknown backend {bound!r}')
        backend = backends[bound]
        if not isinstance(backend, dict) or backend['provider'] in SENTINELS:
            continue
        provider = backend['provider']
        reasoning = role['reasoning']
        maximum = role['max_tokens']
        if isinstance(reasoning, dict) and provider == 'anthropic' and reasoning['mode'] == 'on':
            budget = reasoning['budget_tokens']
            if budget not in SENTINELS and maximum not in SENTINELS:
                if type(budget) is not int or budget < 1024 or budget >= maximum:
                    raise ConfigurationError(f'{path}: {key}.reasoning.budget_tokens: Anthropic requires >= 1024 and < max_tokens')
        output = role['output']
        if isinstance(output, dict) and output['format'] == 'json_schema' and output['schema_id'] in ('none', 'from_profile_manifest'):
            raise ConfigurationError(f'{path}: {key}.output.schema_id: json_schema requires a concrete schema')
        if isinstance(output, dict) and provider in ('anthropic', 'gemini'):
            fmt = output['format']
            if fmt not in SENTINELS and fmt != 'none':
                raise ConfigurationError(f'{path}: {key}.output.format: cloud structured output mapping is not verified')
        image = role['image']
        if isinstance(image, dict):
            if provider == 'gemini' and backend['allow_images'] is True:
                raise ConfigurationError(f'{path}: {key}.image: Gemini image mapping is not verified')
            fmt = image['format']
            if fmt == 'from_vision':
                vision = config['vision']
                fmt = vision.get('format') if isinstance(vision, dict) else vision
            if fmt not in SENTINELS and fmt.upper() not in Image.SAVE:
                raise ConfigurationError(f'{path}: {key}.image.format: no installed encoder for {fmt!r}')


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
    _validate_model_bindings(config, path)
    for name, backend in config['vlm']['backends'].items():
        if not isinstance(backend, dict):
            continue
        http = backend['http']
        for section in ('http', 'probe'):
            for field, value in backend[section].items():
                if isinstance(value, float) and not math.isfinite(value):
                    raise ConfigurationError(f'{path}: vlm.backends.{name}.{section}.{field}: expected finite number')
        initial, maximum = http['backoff_initial_s'], http['backoff_max_s']
        if isinstance(initial, (int, float)) and isinstance(maximum, (int, float)) and initial > maximum:
            raise ConfigurationError(f'{path}: vlm.backends.{name}.http.backoff_max_s: less than initial backoff')
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
    reads = _command_reads(command, config)
    for key in reads:
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
    if any(key.startswith('vlm.roles.') for key in reads):
        default = config['vlm']['default_backend']
        _sentinel(default, path, 'vlm.default_backend', sources)
        default_key = 'vlm.backends.' + default
        _sentinel(_get(config, default_key, path), path, default_key, sources)
    if command == 'kb sync':
        source = _resolve_within(_lexical_path(config['knowledge']['root'], root, path, 'knowledge.root'), root,
                                 {os.path.normcase('.secrets'), os.path.normcase('models')}, path, 'knowledge.root')
        for key in ('storage.kb_store', 'storage.data_root'):
            target = _resolve_within(_lexical_path(_get(config, key, path), root, path, key), root,
                                     {os.path.normcase('.secrets'), os.path.normcase('models')}, path, key)
            if source == target or source in target.parents or target in source.parents:
                raise ConfigurationError(f'{path}: {key} overlaps knowledge.root')
        key = 'knowledge.info_file.encoding'
        try:
            codecs.lookup(config['knowledge']['info_file']['encoding'])
        except LookupError as error:
            raise ConfigurationError(f'{_source(sources, key, path)}: {key}: unknown codec') from error
        workers = config['runtime']['cpu_workers']
        if workers != 'auto' and (type(workers) is not int or workers <= 0):
            raise ConfigurationError(f'{path}: runtime.cpu_workers: expected auto or a positive integer')
        for extension in config['knowledge']['image_extensions']:
            if not isinstance(extension, str) or not extension.startswith('.') or len(extension) < 2:
                raise ConfigurationError(f'{path}: knowledge.image_extensions: invalid extension {extension!r}')
            _filename(extension, path, 'knowledge.image_extensions')
    return config


def load_profile(path, *, project_root=None):
    """Load an admin profile; a relative input path starts at the project root."""
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    path = _inside(path, root)
    manifest_path = _inside(path / 'manifest.yaml', root)
    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f'{manifest_path}: file: {error}') from error
    manifest = _parse_manifest(raw, manifest_path)
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
