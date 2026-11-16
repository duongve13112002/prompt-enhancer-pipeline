"""Load explicitly configured pipeline and admin profiles."""

from copy import deepcopy
import codecs
import errno
from functools import lru_cache
import ipaddress
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import sys
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, exceptions, validators
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

# protocol-bound: the engines an openai_compatible backend may name; `embed_server` is this project's own embedding
# server (T-018, src/prompt_enhancer/embed_server).
LOCAL_ENGINES = ('vllm', 'llama_cpp', 'embed_server')

# Only documented reads are listed; later stage tasks complete their read sets.
COMMAND_READS = {
    'embed serve': ('runtime.gpus', 'runtime.vram_budget_gb', 'embedding.backend'),
    'kb status': ('storage.kb_store', 'storage.lock'),
    'kb sync': ('rag.enabled', 'knowledge', 'sync', 'embedding', 'observe', 'cards', 'aliases', 'index', 'storage.kb_store', 'storage.data_root',
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


def _strip_extended_prefix(value):
    """Drop a Windows extended-length prefix (`\\\\?\\` or `\\\\?\\UNC\\`) from a path string.

    `ntpath.realpath` and `os.readlink` return it when a file vanishes between the two probes of realpath or
    for a link target; without stripping, the containment check reads that spelling as outside the root."""
    value = os.fspath(value)
    if os.name != 'nt':
        return value
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    if value.startswith('\\\\?\\'):
        return value[4:]
    return value


def _is_deleted_name(resolved):
    """True for NTFS's name of a delete-pending file (`<drive>\\$Extend\\$Deleted\\...`), which `realpath`
    returns for a file that is unlinked while it is being resolved (T-006 race fix round 1, native probe)."""
    if os.name != 'nt':
        return False
    parts = [part.lower() for part in Path(resolved).parts]
    return parts[1:3] == ['$extend', '$deleted']


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
                # The readlink error says nothing about the object now in place: the realpath result is what
                # is compared, so a link swapped in since resolves to another spelling and is refused. A file
                # gone at realpath's second probe keeps the first probe's result with its `\\?\` prefix.
                current = Path(_strip_extended_prefix(os.path.realpath(target)))
                if _is_deleted_name(current):
                    # NTFS's name for a delete-pending file; a junction to that spelling gives it too. A name
                    # cannot prove which, so it is a plain refusal; store files that callers delete are removed
                    # under the store lock, where this race cannot happen (T-006 race fix round 2).
                    raise ConfigurationError(f'{path}: {key}: changed while it was resolved')
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
        link = _strip_extended_prefix(link)
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


def _validate(value, schema, path, prefix='', sources=None, validator=Draft202012Validator):
    errors = list(validator(schema).iter_errors(value))
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
        if name not in LOCAL_ENGINES:
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
        if isinstance(local, dict) and isinstance(local['endpoints'], dict) and 'chat' not in local['endpoints']:
            raise ConfigurationError(f'{path}: vlm.default_backend: backend {default!r} has no endpoints.chat')
    for name, backend in backends.items():
        if isinstance(backend, dict):
            provider = backend['provider']
            engine_name = backend['engine']['name']
            if provider not in SENTINELS and engine_name not in SENTINELS:
                if ((provider == 'openai_compatible' and engine_name not in LOCAL_ENGINES)
                        or (provider in ('anthropic', 'gemini') and engine_name != provider)):
                    raise ConfigurationError(f'{path}: vlm.backends.{name}.engine.name: incompatible with provider')
            endpoints = backend['endpoints']
            if (provider == 'gemini' and isinstance(endpoints, dict) and 'chat' in endpoints
                    and endpoints['chat'] not in SENTINELS):
                _gemini_api_version(endpoints['chat'], f'{path}: vlm.backends.{name}')
        if isinstance(backend, dict) and backend['provider'] in ('anthropic', 'gemini'):
            if isinstance(backend['base_urls'], list) and 'provider_default' in backend['base_urls']:
                raise ConfigurationError(f'{path}: vlm.backends.{name}.base_urls: provider_default is not documented; configure a URL')
        if not isinstance(backend, dict):
            continue
        bases, endpoints = backend['base_urls'], backend['endpoints']
        if isinstance(bases, list) and len(bases) > 1:
            raise ConfigurationError(f'{path}: vlm.backends.{name}.base_urls: multiple URLs are not supported')
        # chat and embeddings are both relative routes under the one base URL (embeddings: T-008).
        routes = [field for field in ('chat', 'embeddings') if not isinstance(endpoints, dict) or field in endpoints]
        for field in routes:
            route = endpoints[field] if isinstance(endpoints, dict) else field
            if route in SENTINELS:
                route = field
            if isinstance(bases, list):
                for base in bases:
                    if base in SENTINELS:
                        continue
                    try:
                        endpoint_url(base, route)
                    except ValueError as error:
                        raise ConfigurationError(f'{path}: vlm.backends.{name}.base_urls/endpoints.{field}: {error}') from error
            if isinstance(endpoints, dict) and endpoints[field] not in SENTINELS:
                try:
                    endpoint_url('https://localhost', endpoints[field])
                except ValueError as error:
                    raise ConfigurationError(f'{path}: vlm.backends.{name}.endpoints.{field}: {error}') from error
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
        if isinstance(backend, dict) and isinstance(backend['endpoints'], dict) and 'chat' not in backend['endpoints']:
            # A backend may serve only embeddings (T-008); a role that chats needs the chat route.
            raise ConfigurationError(f'{path}: {key}.backend: backend {bound!r} has no endpoints.chat')
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
            if fmt not in SENTINELS and not installed_encoder(fmt):
                raise ConfigurationError(f'{path}: {key}.image.format: no installed encoder for {fmt!r}')
            if fmt not in SENTINELS and image_media_type(fmt) is None:  # the request header names it (R4F1)
                raise ConfigurationError(f'{path}: {key}.image.format: {NO_MEDIA_TYPE.format(fmt)}')


def installed_encoder(name):
    """Whether Pillow can encode `name`, looked up as `Image.save` looks it up: `SAVE[name.upper()]` once every
    plugin is registered (PIL/Image.py:2564-2573). The installed plugins decide, not a list of format words; an
    empty name is no encoder."""
    from PIL import Image  # imported where needed, as in _validate_model_bindings

    Image.init()
    return isinstance(name, str) and name.upper() in Image.SAVE


def image_media_type(name):
    """The media type Pillow registers for the encoder `name` (`Image.MIME`, keyed as `Image.SAVE`), or None when
    the encoder is not installed or registers no `image/` type. Encoder names and media types are separate
    namespaces (JPEG2000 registers image/jp2, PIL/Jpeg2KImagePlugin.py:442), so a request header is never built
    from the name (T-008 fix round 5, R4F1)."""
    from PIL import Image

    if not installed_encoder(name):  # registers every plugin, so MIME is complete too
        return None
    media_type = Image.MIME.get(name.upper())
    return media_type if isinstance(media_type, str) and media_type.startswith('image/') else None


NO_MEDIA_TYPE = 'the Pillow encoder {!r} registers no image/ media type'


# K1 reads exactly these keys (T-008); the embedder backend contributes only the transport fields it uses. A container
# comes before the leaves K1 indexes in it, so a sentinel container and a missing leaf are each refused by name.
# `candidates` is read to check `chosen` against it (fix round 1, F2).
K1_READS = ('embedding.chosen', 'embedding.candidates', 'embedding.revision', 'embedding.backend', 'embedding.request',
            'embedding.request.shape', 'embedding.request.image_instruction', 'embedding.request.extra_body',
            'embedding.request.text_template', 'embedding.dimension', 'embedding.dimension_mode',
            'embedding.batch_size', 'vision.max_image_side', 'vision.format', 'vision.quality', 'vision.alpha_policy',
            'vision.transport')
EMBEDDER_BACKEND_READS = ('provider', 'engine', 'engine.name', 'engine.version', 'base_urls', 'model.id',
                          'model.revision', 'endpoints.embeddings', 'http', 'http.connect_timeout_s',
                          'http.read_timeout_s', 'http.write_timeout_s', 'http.pool_timeout_s', 'http.max_retries',
                          'http.backoff_initial_s', 'http.backoff_max_s', 'http.retry_statuses', 'max_concurrency',
                          'image.max_decode_pixels')
# K1 also reads these for an `embed_server` backend: `embed_server.families.output_fingerprint` hashes them into the
# embedding-space identity (T-018 fix round 1, R2).
EMBED_SERVER_IDENTITY_READS = ('server', 'server.family', 'server.dtype', 'server.attn_implementation',
                               'server.max_length', 'server.image', 'server.default_instruction')
# protocol-bound: request layout -> the engine whose documentation defines it (T-008 Step 0, A1 and A2).
EMBEDDING_SHAPE_ENGINE = {'openai_input_content': ('llama_cpp', 'embed_server'), 'vllm_chat_messages': ('vllm',)}
TEXT_PLACEHOLDER = '{text}'  # protocol-bound: the one placeholder of embedding.request.text_template
# jsonschema's Draft 2020-12 checker counts an integral float such as 2.0 as an integer (jsonschema/_types.py:195-201).
# K1 slices, counts and passes these values to C-level code, so its read set takes exact ints (never bool or float).
_EXACT_INTEGERS = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
    'integer', lambda checker, instance: type(instance) is int))


def embedder_reads(config, path):
    """K1's read keys, with the backend fields resolved through embedding.backend."""
    name = _get(config, 'embedding.backend', path)
    _sentinel(name, path, 'embedding.backend', {})
    backend = config['vlm']['backends'].get(name) if isinstance(config['vlm']['backends'], dict) else None
    engine = backend.get('engine') if isinstance(backend, dict) else None
    fields = EMBEDDER_BACKEND_READS
    if isinstance(engine, dict) and engine.get('name') == 'embed_server':
        fields += EMBED_SERVER_IDENTITY_READS  # the server's output settings are part of the embedding space
    return K1_READS + tuple(f'vlm.backends.{name}.{field}' for field in fields)


def _schema_at(schema, key):
    """The configuration schema's node for a dotted key: object branches of anyOf/oneOf, then properties or the
    additionalProperties of a mapping such as vlm.backends. A key the schema does not describe is a defect."""
    def objects(nodes):
        for node in nodes:
            if 'properties' in node or isinstance(node.get('additionalProperties'), dict):
                yield node
            for keyword in ('anyOf', 'oneOf'):
                yield from objects(node.get(keyword, ()))

    nodes = [schema]
    for part in key.split('.'):
        nodes = [node['properties'][part] if part in node.get('properties', {}) else node['additionalProperties']
                 for node in objects(nodes)
                 if part in node.get('properties', {}) or isinstance(node.get('additionalProperties'), dict)]
        if not nodes:
            raise KeyError(f'the configuration schema has no node for {key}')
    return nodes[0] if len(nodes) == 1 else {'anyOf': nodes}


def _require_finite(value, path, key, sources):
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: expected a finite number')
    if isinstance(value, dict):
        for name, child in value.items():
            _require_finite(child, path, f'{key}.{name}', sources)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _require_finite(child, path, f'{key}.{index}', sources)


def _schema_types(node):
    """Every JSON type a schema node admits, through its anyOf/oneOf branches (not through properties)."""
    declared = node.get('type', ())
    types = {declared} if isinstance(declared, str) else set(declared)
    for keyword in ('anyOf', 'oneOf'):
        for branch in node.get(keyword, ()):
            types |= _schema_types(branch)
    return types


def _require_float_seconds(value, path, key, sources):
    """A schema `number` of a backend's http or probe settings (all of K1's numbers) is a duration in seconds that a
    float clock adds: AnyIO's deadline for every HTTP timeout (`current_time() + delay`, anyio/_core/_tasks.py:177,
    through httpcore/_backends/anyio.py) and asyncio's for the backoff sleep (`self.time() + delay`,
    asyncio/base_events.py:765) and the probe's wait_for (asyncio/timeouts.py:146). They accept exactly the values
    that convert to a finite float and raise OverflowError on the rest (T-008 fix round 4, R3F2, timer_probe), so
    that is the rule: a large convertible value such as 10**300 stays valid. Floats are already finite
    (`_require_finite`)."""
    if type(value) is int and not _converts_to_float(value):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: the number does not convert to a finite '
                                 'float, which the timer clocks require')


def _converts_to_float(value):
    try:
        float(value)
    except OverflowError:
        return False
    return True


def _check_backoff_order(http, path, key, sources):
    initial, maximum = http['backoff_initial_s'], http['backoff_max_s']
    if type(initial) in (int, float) and type(maximum) in (int, float) and initial > maximum:
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}.backoff_max_s: less than initial backoff')


def validate_embedder_reads(config, path, sources, project_root=None):
    """K1's read set: present, resolved, and inside the domain the configuration schema states for each key.

    One check for both paths: the loader's `kb sync` and K1's standalone entry points call it, so a profile that
    skips the loader meets the same rules (T-008 fix round 3, R2F2). Each value is validated against the schema's
    own node for its key, with exact integers; every float must be finite; the backoff pair keeps its order. The
    schema maximum of embedding.dimension, 2**31 - 1, is Arrow's C-int list size (pyarrow/types.pxi:4959, RT3).
    Fix round 4 (R3F1, R3F2) adds the domains of the consumers: a schema `number` must convert to a finite float
    (the timer clocks), and vision.format must name an installed Pillow encoder; fix round 5 (R4F1): one that
    registers an image/ media type, the type the data URI states.
    """
    keys = embedder_reads(config, path)
    for key in keys:
        _sentinel(_get(config, key, path), path, key, sources)
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _schema(root, 'enhancer_config.v0')
    for key in keys:
        value = _get(config, key, path)
        node = _schema_at(schema, key)
        _validate(value, node, path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        if 'number' in _schema_types(node):
            _require_float_seconds(value, path, key, sources)
    fmt = config['vision']['format']
    if not installed_encoder(fmt):  # K1 encodes every image with it (R3F1); role formats are checked separately
        key = 'vision.format'
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: no installed Pillow encoder for {fmt!r}')
    if image_media_type(fmt) is None:  # the data URI states the registered type of the bytes (R4F1)
        key = 'vision.format'
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {NO_MEDIA_TYPE.format(fmt)}')
    name = config['embedding']['backend']
    if config['vlm']['backends'][name]['engine']['name'] == 'embed_server':
        from .embed_server.protocol import ALLOWED_FORMATS

        if fmt.upper() not in ALLOWED_FORMATS:  # the server decodes only these (T-018 review R6); refuse at start
            key = 'vision.format'
            raise ConfigurationError(f'{_source(sources, key, path)}: {key}: the embedding server decodes only '
                                     f'{", ".join(ALLOWED_FORMATS)}, not {fmt!r}')
    _check_backoff_order(config['vlm']['backends'][name]['http'], path, f'vlm.backends.{name}.http', sources)


SERVER_READS = ('runtime.gpus', 'runtime.vram_budget_gb', 'embedding.backend', 'embedding.batch_size',
                'embedding.dimension', 'embedding.dimension_mode', 'embedding.revision', 'embedding.request.shape',
                'embedding.request.image_instruction')
SERVER_BACKEND_READS = ('provider', 'engine', 'engine.name', 'engine.version', 'base_urls', 'model.id', 'model.revision',
                        'endpoints.embeddings', 'endpoints.health', 'image.max_decode_pixels', 'server.family',
                        'server.dtype', 'server.attn_implementation', 'server.forward_batch', 'server.max_length',
                        'server.image', 'server.default_instruction', 'server.idle_exit_s', 'server.max_request_bytes',
                        'server.max_image_bytes', 'server.max_pending_requests', 'server.max_connections',
                        'server.max_discard_bytes', 'server.read_timeout_s')
NONE = 'none'  # protocol-bound profile value of "not used": no instruction, or an image limit the family lacks


def embed_server_address(base_url):
    """(host, port) of an embedding server's base URL; plain http on an IPv4 loopback host with an explicit port.

    IPv6 is refused: the server binds one IPv4 socket (stdlib `ThreadingHTTPServer`), so an accepted `[::1]` could
    never bind. Narrowing the contract is the reversible choice; binding `AF_INET6` can be added with its own test.
    """
    parts = urlsplit(base_url)
    if parts.scheme != 'http' or not parts.hostname or parts.username or parts.password:
        raise ValueError('expected a plain http URL')
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        loopback = parts.hostname.lower() == 'localhost'
    else:
        if address.version != 4:
            raise ValueError('the embedding server listens on IPv4 only; use 127.0.0.1 or localhost')
        loopback = address.is_loopback
    if not loopback:
        raise ValueError('the embedding server listens on loopback only')
    port = parts.port  # raises ValueError for an out-of-range port
    if port is None or port == 0:
        raise ValueError('an explicit port is required')
    return parts.hostname, port


def validate_embed_server_reads(config, path, sources, project_root=None):
    """The embedding server's read set (T-018): present, resolved, inside the schema's domain, consistent.

    One check for the loader's `embed serve` and the server's own entry, as with `validate_embedder_reads`. The
    backend is `embedding.backend`; it must name an `embed_server` engine and hold a `server` block.
    """
    from .embed_server.families import FAMILIES
    from .model_transport import endpoint_url

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    name = _get(config, 'embedding.backend', path)
    _sentinel(name, path, 'embedding.backend', sources)
    backends = config['vlm']['backends']
    if name not in backends:
        refuse('embedding.backend', f'unknown backend {name!r}')
    backend = backends[name]
    if not isinstance(backend, dict) or backend['provider'] != 'openai_compatible':
        refuse('embedding.backend', 'the embedding server needs a local openai_compatible backend')
    if not isinstance(backend['engine'], dict) or backend['engine']['name'] != 'embed_server':
        refuse(f'vlm.backends.{name}.engine.name', 'expected embed_server')
    keys = SERVER_READS + tuple(f'vlm.backends.{name}.{field}' for field in SERVER_BACKEND_READS)
    for key in keys:
        _sentinel(_get(config, key, path), path, key, sources)
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _schema(root, 'enhancer_config.v0')
    for key in keys:
        value = _get(config, key, path)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
    prefix = f'vlm.backends.{name}'
    gpus = config['runtime']['gpus']
    if not isinstance(gpus, list) or len(gpus) != 1:
        refuse('runtime.gpus', 'the embedding server places its model on exactly one listed GPU')
    budget = config['runtime']['vram_budget_gb']
    if type(budget) not in (int, float) or budget <= 0:
        refuse('runtime.vram_budget_gb', 'expected a positive number of GB')
    bases = backend['base_urls']
    if not isinstance(bases, list) or len(bases) != 1:
        refuse(f'{prefix}.base_urls', 'expected exactly one URL')
    try:
        embed_server_address(bases[0])
        embeddings, health = (urlsplit(endpoint_url(bases[0], backend['endpoints'][route])).path
                              for route in ('embeddings', 'health'))
    except ValueError as error:
        refuse(f'{prefix}.base_urls', str(error))
    if embeddings == health:
        refuse(f'{prefix}.endpoints', 'embeddings and health must be different routes')
    server = backend['server']
    family = FAMILIES[server['family']]
    if server['dtype'] in family.forbidden_dtypes:
        refuse(f'{prefix}.server.dtype', f"{server['dtype']} is refused for {family.name} (model card)")
    image, instruction = server['image'], config['embedding']['request']['image_instruction']
    if family.accepts_instruction:
        fields = {'min_pixels': int, 'max_pixels': int, 'max_soft_tokens': None}
    else:
        fields = {'min_pixels': None, 'max_pixels': None, 'max_soft_tokens': int}
        for key, value in ((f'{prefix}.server.default_instruction', server['default_instruction']),
                           ('embedding.request.image_instruction', instruction)):
            if value != NONE:
                refuse(key, f'{family.name} has no system instruction; expected none')
    for field, kind in fields.items():
        if (kind is None) != (image[field] == NONE):
            refuse(f'{prefix}.server.image.{field}', f'{family.name} {"does not use" if kind is None else "requires"} it')
    if family.accepts_instruction and image['min_pixels'] > image['max_pixels']:
        refuse(f'{prefix}.server.image.min_pixels', 'exceeds max_pixels')
    try:  # the consumer decides what a usable timeout is: the platform's socket (its limit differs per OS)
        with socket.socket() as probe:
            probe.settimeout(server['read_timeout_s'])
    except (OverflowError, ValueError, OSError):
        refuse(f'{prefix}.server.read_timeout_s', 'the platform socket cannot hold a timeout this long')
    if server['forward_batch'] > config['embedding']['batch_size']:
        refuse(f'{prefix}.server.forward_batch', 'exceeds embedding.batch_size')
    if server['max_request_bytes'] < -(-server['max_image_bytes'] // 3) * 4:
        refuse(f'{prefix}.server.max_request_bytes', 'too small for one image of max_image_bytes as base64')
    if server['max_discard_bytes'] < server['max_request_bytes']:
        refuse(f'{prefix}.server.max_discard_bytes', 'below max_request_bytes: a refused body over the limit could not be drained')
    if server['max_connections'] < server['max_pending_requests']:
        refuse(f'{prefix}.server.max_connections', 'below max_pending_requests: admitted requests could not all hold a connection')
    if config['embedding']['request']['shape'] != 'openai_input_content':
        refuse('embedding.request.shape', 'the embedding server answers openai_input_content only')
    if config['embedding']['revision'] != backend['model']['revision']:
        refuse('embedding.revision', f'differs from {prefix}.model.revision')
    if config['embedding']['dimension_mode'] == 'request_field':
        refuse('embedding.dimension_mode', 'request_field is not supported by the embedding server')


# Protocol-bound supported naming policies (K2-observe.md, naming step); no content decisions live here.
NAMING_POLICIES = {'on_no_info_file': 'model_names_from_images',
                   'on_unusable_info_file': 'model_names_from_images',
                   'selection': 'random', 'on_no_images': 'skip'}
NAMING_ROLES = ('info_assessor', 'folder_namer')


def validate_naming_reads(config, path='configuration', sources=None, project_root=None):
    """The same strict K2 preflight at load and standalone entry, before source reads or model calls."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _schema(root, 'enhancer_config.v0')

    def checked(key, node=None):
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, node or _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        return value

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    naming = checked('knowledge.naming')
    for field, supported in NAMING_POLICIES.items():
        if naming[field] != supported:
            refuse('knowledge.naming.' + field, f'expected {supported!r}')
    if naming['sample_size'] == 0:
        refuse('knowledge.naming.sample_size', 'must be positive for naming')
    # The share is compared as an exact ratio; it need not be converted to a float.
    for key in ('knowledge.root', 'knowledge.info_file', 'knowledge.concept_separator', 'knowledge.overrides_file'):
        checked(key)
    override = config['knowledge']['overrides_file']
    if override != 'none':
        _filename(override, path, 'knowledge.overrides_file')
    for filename in config['knowledge']['info_file']['names']:
        _filename(filename, path, 'knowledge.info_file.names')
    try:
        codecs.lookup(config['knowledge']['info_file']['encoding'])
    except LookupError as error:
        refuse('knowledge.info_file.encoding', f'unknown codec: {error}')
    checked('vision')
    default = checked('vlm.default_backend')
    backends = {default}
    for name in NAMING_ROLES:
        key = f'vlm.roles.{name}'
        role = checked(key, _schema(root, 'vlm_role.v0'))
        backends.add(role['backend'])
        output = role['output']
        if output['format'] not in ('json_schema', 'json_object'):
            refuse(key + '.output.format', 'naming requires structured JSON')
        if output['validate_response'] is not True:
            refuse(key + '.output.validate_response', 'naming requires response validation')
        if output['transport'] != 'response_format':
            refuse(key + '.output.transport', 'only response_format is implemented by the naming client')
        try:
            _schema(root, output['schema_id'])
        except ConfigurationError as error:
            refuse(key + '.output.schema_id', str(error))
        if name == 'info_assessor':
            if role['image'] != 'none':
                refuse(key + '.image', 'info_assessor is text-only')
        else:
            image = role['image']
            if not isinstance(image, dict):
                refuse(key + '.image', 'folder_namer requires knowledge_reference images')
            if image['source'] != 'knowledge_reference':
                refuse(key + '.image.source', 'expected knowledge_reference')
            if image['transport'] != 'data_uri':
                refuse(key + '.image.transport', 'expected data_uri')
            _get(config, key + '.image.max_side', path)  # no implicit inherited-size fallback at the K2 boundary
            fmt = config['vision']['format'] if image['format'] == 'from_vision' else image['format']
            if not installed_encoder(fmt) or image_media_type(fmt) is None:
                refuse(key + '.image.format', 'expected an installed encoder with an image/ media type')
    for name in sorted(backends):
        key = 'vlm.backends.' + name
        backend = checked(key)
        resolve_backend_engine(backend, key)
        _check_backoff_order(backend['http'], path, key + '.http', sources)
        for section in ('http', 'probe'):
            for field, value in backend[section].items():
                dotted = f'{key}.{section}.{field}'
                if 'number' in _schema_types(_schema_at(schema, dotted)):
                    _require_float_seconds(value, path, dotted, sources)
    _validate_model_bindings(config, path)
    _validate_configured_paths(config, root, path, sources)


def validate_observation_reads(config, path='configuration', sources=None, project_root=None):
    """Validate the standalone/command observer read set before reads, checkpoints or model activity."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _schema(root, 'enhancer_config.v0')

    def checked(key, node=None):
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, node or _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        return value

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    observe = checked('observe')
    # Protocol-bound single-behavior policies: false/unknown values cannot silently select another mode.
    for field, supported in {'one_image_per_call': True, 'lazy_observation_of_rest': True,
                             'region_output': 'optional', 'facet_schema_version': 'image_observation.v0'}.items():
        if observe[field] != supported:
            refuse('observe.' + field, f'expected {supported!r}')
    if observe['representatives_per_folder'] == 0:
        refuse('observe.representatives_per_folder', 'must be positive for observation')
    checked('vision')
    checked('knowledge.root')
    checked('storage.kb_store')
    checked('storage.data_root')
    default = checked('vlm.default_backend')
    key = 'vlm.roles.observer'
    role = checked(key, _schema(root, 'vlm_role.v0'))
    image, output = role['image'], role['output']
    if not isinstance(image, dict) or image['source'] != 'knowledge_reference':
        refuse(key + '.image.source', 'observer requires knowledge_reference images')
    if image['transport'] != 'data_uri':
        refuse(key + '.image.transport', 'expected data_uri')
    _get(config, key + '.image.max_side', path)
    fmt = config['vision']['format'] if image['format'] == 'from_vision' else image['format']
    if not installed_encoder(fmt) or image_media_type(fmt) is None:
        refuse(key + '.image.format', 'expected an installed encoder with an image/ media type')
    if output['format'] not in ('json_schema', 'json_object'):
        refuse(key + '.output.format', 'observer requires structured JSON')
    if output['transport'] != 'response_format':
        refuse(key + '.output.transport', 'expected response_format')
    if output['validate_response'] is not True:
        refuse(key + '.output.validate_response', 'observer requires response validation')
    try:
        _schema(root, output['schema_id'])
    except ConfigurationError as error:
        refuse(key + '.output.schema_id', str(error))
    backends = {}
    for name in sorted({role['backend'], default}):
        backend_key = 'vlm.backends.' + name
        backend = checked(backend_key)
        backends[name] = backend
        resolve_backend_engine(backend, backend_key)
        _check_backoff_order(backend['http'], path, backend_key + '.http', sources)
        for section in ('http', 'probe'):
            for field in backend[section]:
                dotted = f'{backend_key}.{section}.{field}'
                if 'number' in _schema_types(_schema_at(schema, dotted)):
                    _require_float_seconds(backend[section][field], path, dotted, sources)
    scoped = {'vlm': {'roles': {'observer': role}, 'backends': backends, 'default_backend': default},
              'vision': config['vision'], 'knowledge': {'root': config['knowledge']['root']},
              'storage': {field: config['storage'][field] for field in ('kb_store', 'data_root')}}
    _validate_model_bindings(scoped, path)
    _validate_configured_paths(scoped, root, path, sources)


# Protocol/artifact-bound policy vocabulary; semantic facet/value vocabularies remain open.
CARD_LABELS = ('curated', 'stable', 'stable_low_diversity', 'varies', 'single_observation')
CARD_POLICY_VERSION = 'invariance_v0'


def validate_card_reads(config, path='configuration', sources=None, project_root=None):
    """Validate K3's complete read set before opening a writer or composing a model client."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _schema(root, 'enhancer_config.v0')

    def checked(key, node=None):
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, node or _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        return value

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    cards = checked('cards')
    labels = cards['stability_policy']['labels']
    if sorted(labels) != sorted(CARD_LABELS):
        refuse('cards.stability_policy.labels', 'expected each supported stability label exactly once')
    if cards['stability_policy']['version'] != CARD_POLICY_VERSION:
        refuse('cards.stability_policy.version', f'expected {CARD_POLICY_VERSION}')
    if cards['min_visible_images'] <= 0:
        refuse('cards.min_visible_images', 'must be positive')
    if cards['low_diversity'] != 'mark_stable_low_diversity':
        refuse('cards.low_diversity', 'expected mark_stable_low_diversity')
    if checked('observe.facet_schema_version') != 'image_observation.v0':
        refuse('observe.facet_schema_version', 'expected image_observation.v0')
    for key in ('storage.kb_store', 'storage.data_root', 'storage.lock', 'storage.table_format',
                'sync.resume', 'knowledge.root'):
        checked(key)
    default = checked('vlm.default_backend')
    key = 'vlm.roles.card_builder'
    role = checked(key, _schema(root, 'vlm_role.v0'))
    for field, supported in {'mode': 'generate', 'image': 'none'}.items():
        if role[field] != supported:
            refuse(key + '.' + field, f'expected {supported}')
    load_id = role['prompt_id']
    # Match pipeline prompt IDs structurally, without reading the prompt before preflight completes.
    if (not load_id or not set(load_id) <= set('abcdefghijklmnopqrstuvwxyz0123456789_.')
            or load_id.strip('.') != load_id or '..' in load_id):
        refuse(key + '.prompt_id', 'invalid prompt id')
    output = role['output']
    if output['format'] not in ('json_schema', 'json_object'):
        refuse(key + '.output.format', 'card builder requires structured JSON')
    if output['transport'] != 'response_format':
        refuse(key + '.output.transport', 'expected response_format')
    if output['validate_response'] is not True:
        refuse(key + '.output.validate_response', 'expected true')
    configured_schema = _schema(root, output['schema_id'])
    reply_schema = _schema(root, 'card_builder_reply.v0')
    metadata = ('$id', 'title', '$comment')
    if ({key: value for key, value in configured_schema.items() if key not in metadata}
            != {key: value for key, value in reply_schema.items() if key not in metadata}):
        refuse(key + '.output.schema_id', 'expected card_builder_reply.v0 or an equivalent local schema')
    backends = {}
    for name in sorted({role['backend'], default}):
        backend_key = 'vlm.backends.' + name
        backend = checked(backend_key)
        backends[name] = backend
        resolve_backend_engine(backend, backend_key)
        _check_backoff_order(backend['http'], path, backend_key + '.http', sources)
        for section in ('http', 'probe'):
            for field in backend[section]:
                dotted = f'{backend_key}.{section}.{field}'
                if 'number' in _schema_types(_schema_at(schema, dotted)):
                    _require_float_seconds(backend[section][field], path, dotted, sources)
    scoped = {'vlm': {'roles': {'card_builder': role}, 'backends': backends, 'default_backend': default},
              'knowledge': {'root': config['knowledge']['root']},
              'storage': {field: config['storage'][field] for field in ('kb_store', 'data_root')}}
    _validate_model_bindings(scoped, path)
    _validate_configured_paths(scoped, root, path, sources)


def validate_alias_reads(config, path='configuration', sources=None, project_root=None):
    """Validate K4's complete read set before constructing a writer or model client."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _schema(root, 'enhancer_config.v0')

    def checked(key, node=None):
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, node or _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        return value

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    aliases = checked('aliases')
    # Safety/protocol invariants from K4: no alternate linking or collision policy is implemented.
    for field, supported in {'collision_policy': 'drop_and_queue_for_review',
                             'review_queue': True, 'only_add_candidates': True}.items():
        if aliases[field] != supported:
            refuse('aliases.' + field, f'expected {supported}')
    for key in ('storage.kb_store', 'storage.data_root', 'storage.lock', 'storage.table_format',
                'sync.resume', 'knowledge.root'):
        checked(key)
    default = checked('vlm.default_backend')
    key = 'vlm.roles.alias_generator'
    role = checked(key, _schema(root, 'vlm_role.v0'))
    for field, supported in {'mode': 'generate', 'image': 'none'}.items():
        if role[field] != supported:
            refuse(key + '.' + field, f'expected {supported}')
    prompt_id = role['prompt_id']
    if (not prompt_id or not set(prompt_id) <= set('abcdefghijklmnopqrstuvwxyz0123456789_.')
            or prompt_id.strip('.') != prompt_id or '..' in prompt_id):
        refuse(key + '.prompt_id', 'invalid prompt id')
    output = role['output']
    if output['format'] not in ('json_schema', 'json_object'):
        refuse(key + '.output.format', 'alias generator requires structured JSON')
    if output['transport'] != 'response_format':
        refuse(key + '.output.transport', 'expected response_format')
    if output['validate_response'] is not True:
        refuse(key + '.output.validate_response', 'expected true')
    configured = _schema(root, output['schema_id'])
    expected = _schema(root, 'alias_generator_reply.v0')
    metadata = ('$id', 'title', '$comment')
    if ({name: value for name, value in configured.items() if name not in metadata}
            != {name: value for name, value in expected.items() if name not in metadata}):
        refuse(key + '.output.schema_id', 'expected alias_generator_reply.v0 or an equivalent local schema')
    backends = {}
    for name in sorted({role['backend'], default}):
        backend_key = 'vlm.backends.' + name
        backend = checked(backend_key)
        backends[name] = backend
        resolve_backend_engine(backend, backend_key)
        _check_backoff_order(backend['http'], path, backend_key + '.http', sources)
        for section in ('http', 'probe'):
            for field in backend[section]:
                dotted = f'{backend_key}.{section}.{field}'
                if 'number' in _schema_types(_schema_at(schema, dotted)):
                    _require_float_seconds(backend[section][field], path, dotted, sources)
    scoped = {'vlm': {'roles': {'alias_generator': role}, 'backends': backends, 'default_backend': default},
              'knowledge': {'root': config['knowledge']['root']},
              'storage': {field: config['storage'][field] for field in ('kb_store', 'data_root')}}
    _validate_model_bindings(scoped, path)
    _validate_configured_paths(scoped, root, path, sources)


def _index_migration(config, path):
    index = config.get('index')
    if isinstance(index, dict):
        for key in ('ann', 'exact_search_below_count'):
            if key in index:
                raise ConfigurationError(f'{path}: index.{key}: obsolete index configuration; migrate to '
                                         'index.engine, index.metric and index.build.block_rows')


def validate_index_reads(config, path='configuration', sources=None, project_root=None):
    """K5's entire profile read set; refuse before reading or attaching snapshot files."""
    sources = sources or {}
    _index_migration(config, path)
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    value = _get(config, 'index', path)
    _sentinel(value, path, 'index', sources)
    node = _schema_at(_schema(root, 'enhancer_config.v0'), 'index')
    _validate(value, node, path, 'index', sources, _EXACT_INTEGERS)
    # Safety-bound: counts cross Python/numpy/Arrow native-size boundaries.
    for key in ('index.build.block_rows', 'index.lexical.ngram_size'):
        if _get(config, key, path) > sys.maxsize:
            raise ConfigurationError(f'{path}: {key}: exceeds native integer size')
    return deepcopy(value)


def _validate_embedding(config, path, sources):
    """Cross-field rules of the embedder settings; sentinel values are checked when a command reads them."""
    embedding = config['embedding']
    if not isinstance(embedding, dict):
        return

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    chosen, candidates = embedding['chosen'], embedding['candidates']
    if chosen not in SENTINELS and isinstance(candidates, list):
        if chosen not in [item['id'] for item in candidates if isinstance(item, dict)]:
            refuse('embedding.chosen', 'not one of embedding.candidates')
    request = embedding['request']
    shape = request['shape'] if isinstance(request, dict) else request
    if isinstance(request, dict):
        template, extra = request['text_template'], request['extra_body']
        if template not in SENTINELS and template.count(TEXT_PLACEHOLDER) != 1:
            refuse('embedding.request.text_template', f'expected exactly one {TEXT_PLACEHOLDER} placeholder')
        if shape == 'openai_input_content' and isinstance(extra, dict) and extra:
            refuse('embedding.request.extra_body', 'only vllm_chat_messages sends extra body fields')
    batch = embedding['batch_size']
    if shape == 'vllm_chat_messages' and type(batch) is int and batch > 1:
        refuse('embedding.batch_size', 'vllm_chat_messages carries one item per request')
    name = embedding['backend']
    if name in SENTINELS:
        return
    vlm = config['vlm']
    backends = vlm['backends'] if isinstance(vlm, dict) else None
    if not isinstance(backends, dict):
        return
    if name not in backends:
        refuse('embedding.backend', f'unknown backend {name!r}')
    backend = backends[name]
    if not isinstance(backend, dict):
        return
    if backend['provider'] not in SENTINELS and backend['provider'] != 'openai_compatible':
        refuse('embedding.backend', 'the embedder must be a local openai_compatible backend (R-018)')
    if isinstance(backend['endpoints'], dict) and 'embeddings' not in backend['endpoints']:
        refuse('embedding.backend', f'backend {name!r} has no endpoints.embeddings')
    model = backend['model']
    revision = model.get('revision') if isinstance(model, dict) else model
    if (embedding['revision'] not in SENTINELS and revision not in SENTINELS
            and embedding['revision'] != revision):
        refuse('embedding.revision', f'differs from vlm.backends.{name}.model.revision')
    engine = backend['engine']['name']
    if engine not in SENTINELS:
        if shape in EMBEDDING_SHAPE_ENGINE and engine not in EMBEDDING_SHAPE_ENGINE[shape]:
            refuse('embedding.request.shape', f'{shape} is not documented for engine {engine}')
        if embedding['dimension_mode'] == 'request_field' and engine != 'vllm':
            refuse('embedding.dimension_mode', f'request_field is not documented for engine {engine}')


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
    return _validate_loaded_config(config, command, root, path, sources)


def validate_sync_config(config, *, project_root):
    """Apply the command preflight to a supplied mapping before any stage/store/source activity."""
    return _validate_loaded_config(config, 'kb sync', Path(project_root).resolve(), 'configuration', {})


def _validate_loaded_config(config, command, root, path, sources):
    try:
        config_schema = _schema(root, 'enhancer_config.v0')
    except ConfigurationError as error:
        raise ConfigurationError(f'{path}: schema_version: {error}') from error
    _index_migration(config, path)
    _validate(config, config_schema, path, sources=sources)
    if command == 'kb sync':
        gate = _get(config, 'rag.enabled', path)
        if type(gate) is not bool:
            raise ConfigurationError(f'{path}: rag.enabled: expected boolean')
        if not gate:
            return config
    _validate_model_bindings(config, path)
    _validate_embedding(config, path, sources)
    for name, backend in config['vlm']['backends'].items():
        if not isinstance(backend, dict):
            continue
        for section in ('http', 'probe'):
            for field, value in backend[section].items():
                if isinstance(value, float) and not math.isfinite(value):
                    raise ConfigurationError(f'{path}: vlm.backends.{name}.{section}.{field}: expected finite number')
                key = f'vlm.backends.{name}.{section}.{field}'
                if 'number' in _schema_types(_schema_at(config_schema, key)):
                    # The same timer rule K1's preflight applies (fix round 4, R3F2), before the order check, so an
                    # unconvertible value is refused under its own key on every backend.
                    _require_float_seconds(value, path, key, {})
        _check_backoff_order(backend['http'], path, f'vlm.backends.{name}.http', {})
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
        if command == 'kb sync':
            _validate(value, _schema_at(config_schema, key), path, key, sources, _EXACT_INTEGERS)
            _require_finite(value, path, key, sources)
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
    if command == 'embed serve':
        validate_embed_server_reads(config, path, sources, root)
    if command == 'kb sync':
        for key in ('storage.lock.timeout_s', 'storage.lock.poll_s'):
            _require_float_seconds(_get(config, key, path), path, key, sources)
        # Validate store policy interactions without constructing a store or starting K0.
        from .storage.store import _settings
        from .storage import StorageError
        try:
            _settings(config, command, root)
        except StorageError as error:
            raise ConfigurationError(f'{path}: {error}') from None
        validate_index_reads(config, path, sources, root)
        validate_embedder_reads(config, path, sources, root)  # K1 is part of kb sync; the same check K1 runs
        validate_naming_reads(config, path, sources, root)
        validate_observation_reads(config, path, sources, root)
        validate_card_reads(config, path, sources, root)
        validate_alias_reads(config, path, sources, root)
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
