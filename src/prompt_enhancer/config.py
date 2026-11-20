"""Load explicitly configured pipeline and admin profiles."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import codecs
import errno
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

from .model_mapping import SAMPLING, anthropic_reasoning_refusal, provider_maps_seed
from .model_schemas import SnapshotCache


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

# S0 owns its decode limit; model stages use their backend's independent limit.
MODEL_VISION_READS = tuple('vision.' + field for field in
                           ('max_image_side', 'alpha_policy', 'format', 'quality', 'transport'))
REQUEST_COMMANDS = ('enhance once', 'enhance serve', 'eval run')  # protocol-bound command names
CONFIG_VERSIONS = (1, 2, 3)  # artifact-bound: the schema_version values of enhancer_config.v0, .v1 and .v2
SOURCE_LIMITS_VERSION = 3  # artifact-bound: the first version that carries the multi-source keys (decision 179)
MANIFEST_VERSIONS = (1, 2)  # artifact-bound: manifest schema_version of profile_manifest.v0 and .v1

# Only documented reads are listed; later stage tasks complete their read sets.
COMMAND_READS = {
    'embed serve': ('runtime.gpus', 'runtime.vram_budget_gb', 'embedding.backend'),
    'embed reference-check': ('runtime.gpus', 'runtime.vram_budget_gb', 'embedding.backend'),  # + the backend's server.reference_check
    # DEV-ONLY (T-018 P3, embed_server/dev_compare.py): K0's scan and K1's two parts, exactly what they read; the
    # embedder backend is checked by validate_embedder_reads. Smoke-only outputs, never thresholds (R-020).
    'embed dev-compare': ('knowledge.root', 'knowledge.info_file.names', 'knowledge.info_file.encoding',
                          'knowledge.image_extensions', 'knowledge.concept_separator', 'runtime.cpu_workers',
                          'sync.mass_deletion_guard.max_removed_fraction', 'sync.resume', 'storage.kb_store', 'storage.lock',
                          'storage.table_format', 'embedding.candidates', 'embedding.chosen', 'embedding.revision', 'embedding.backend',
                          'embedding.request', 'embedding.dimension', 'embedding.dimension_mode',
                          'embedding.batch_size', 'vision'),
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
                      'vlm.roles.planner', 'vlm.roles.writer', 'vlm.roles.verifier',
                      # `serving.prefix_cache` is an external-backend setup choice and is not read here (T-016 Q2);
                      # the keys below are read leaf by leaf, then checked together by `validate_serving`.
                      'serving.host', 'serving.port', 'serving.max_concurrency', 'serving.queue_limit',
                      'serving.total_deadline_s', 'serving.http'),
    'eval build': ('evaluation', 'storage.kb_store', 'storage.lock', 'storage.data_root', 'storage.table_format',
                   'vlm.roles.scenario_generator'),
    # `eval run` consumes these `evaluation` leaves only (T-016 P4): `min_items_per_class`, `languages` and the import-only
    # judge leaves are not read, so an unresolved value of theirs is not a refusal here.
    'eval run': ('evaluation.scenario_kinds', 'evaluation.split_by', 'evaluation.run',
                 'evaluation.judge.blinding_seed', 'evaluation.judge.score_min', 'evaluation.judge.score_max',
                 'evaluation.judge.rubric_prompt_id',
                 'profiles', 'rag', 'vision', 'candidates', 'embedding', 'planner', 'resolution',
                 'clarification', 'creativity', 'edit', 'assembly', 'writer', 'validation', 'runtime',
                 'storage.data_root', 'storage.table_format', 'storage.lock',
                 'vlm.roles.planner', 'vlm.roles.writer', 'vlm.roles.verifier'),
    # `storage.lock` builds the run lock that one `eval run` (or one `eval import`) holds on a run (fix round 1, P1)
    'eval import': ('evaluation.judge.score_min', 'evaluation.judge.score_max', 'evaluation.judge.min_scorers',
                    'evaluation.judge.under_test_families', 'evaluation.judge.max_score_file_bytes',
                    'evaluation.run.max_sidecar_bytes', 'storage.data_root', 'storage.table_format', 'storage.lock'),
    'eval fit': ('evaluation', 'storage.data_root'),
}


class ConfigurationError(ValueError):
    """Invalid configuration with its source and key."""


class LinkedPath(ConfigurationError):
    """A path the project owns or writes has a link (symlink, junction) or another spelling of a stored name on its way."""


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


def _resolve_within(candidate, root, forbidden, path, key, *, links=False):
    """Resolve a path under the project root component by component without touching protected targets.

    A component that is a link (symlink, junction) or another spelling of a stored name is refused, so a path the project
    owns or writes never reaches its target through one (AGENTS.md section 6, P3-SWEEP-2). `links=True` follows links
    that stay inside the root; only the store-relative `storage._fs.inside` takes it. The knowledge root, which may lie
    outside the project (decision 175), is not resolved here but by `resolve_knowledge_root`."""
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
                if not links and os.path.normcase(str(current)) != os.path.normcase(str(target)):
                    raise LinkedPath(f'{path}: {key}: is a link or another spelling of a stored name')
                continue
            raise ConfigurationError(f'{path}: {key}: cannot inspect link: {error}') from error
        if target in visited:
            raise ConfigurationError(f'{path}: {key}: link cycle')
        visited.add(target)
        link = _strip_extended_prefix(link)
        resolved = Path(os.path.abspath(target.parent / link))
        if not resolved.is_relative_to(root):
            raise ConfigurationError(f'{path}: {key}: resolved outside project root')  # the more specific fact first
        if not links:
            target_parts = resolved.relative_to(root).parts
            if target_parts and os.path.normcase(target_parts[0]) in forbidden:
                raise ConfigurationError(f'{path}: {key}: protected project directory')
            raise LinkedPath(f'{path}: {key}: is a link or another spelling of a stored name')
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


# Keyed by the digest of the bytes parsed, never by a separate stat (RES-1: one snapshot for key and value).
_SCHEMA_CACHE = SnapshotCache(SCHEMA_CACHE_CAPACITY)


def _parsed_schema(raw):
    result = json.loads(raw)
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
        return _SCHEMA_CACHE.fetch(path.read_bytes, _parsed_schema)
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


# safety-bound: the project's protected top-level names and the prefix of every environment directory (`.venv`,
# `.venv-gpu`, ...). `resolve_knowledge_root` is their one reader for the knowledge root (decision 175).
PROTECTED_NAMES = ('.secrets', '.git', 'models')
PROTECTED_VENV_PREFIX = '.venv'


def _folded(path):
    return tuple(os.path.normcase(part) for part in Path(path).parts)


def _overlaps(first, second):
    """True when the paths are equal or one lies inside the other, compared by components (so `data` and `database`
    differ) and case-insensitively where the platform is (`os.path.normcase`)."""
    first, second = _folded(first), _folded(second)
    size = min(len(first), len(second))
    return first[:size] == second[:size]


def _contains(outer, inner):
    outer, inner = _folded(outer), _folded(inner)
    return inner[:len(outer)] == outer


def _physical(path):
    return Path(_strip_extended_prefix(os.path.realpath(path)))


def same_physical_path(first, second):
    """True when both paths name the same directory after links (case-insensitive where the platform is)."""
    return _folded(_physical(first)) == _folded(_physical(second))


def _protected_entries(root):
    """`(name, paths)` of the protected directories, each as spelled under the root and, except `.secrets`, as its
    target after links (a `models/` junction to another drive protects that drive's directory). `.secrets` is compared
    lexically and never resolved, stat-ed, opened or listed: a `.secrets` that is itself a link is a known limit."""
    try:
        with os.scandir(root) as listing:
            names = [entry.name for entry in listing]
    except OSError as error:
        raise ConfigurationError(f'{root}: cannot inspect the project root: {error.strerror or error}') from error
    fold_prefix = os.path.normcase(PROTECTED_VENV_PREFIX)
    entries = [('.secrets', [root / '.secrets'])]
    wanted = [name for name in PROTECTED_NAMES if name != '.secrets']
    # `.venv` is protected whether or not it exists yet; every other environment directory by its prefix.
    wanted += sorted({PROTECTED_VENV_PREFIX, *(name for name in names if os.path.normcase(name).startswith(fold_prefix))})
    for name in wanted:
        entries.append((name, [root / name, _physical(root / name)]))
    return entries


def resolve_knowledge_root(config, project_root, path='configuration', *, require_dir=False, sources=None):
    """The one physical knowledge root every stage reads (decision 175; R-014 read only).

    The root may lie anywhere (another drive, a link pointing out of the project). It is resolved once with
    `os.path.realpath`, so a link whose target holds `..` is followed as the OS reads it, and then refused when it equals
    or contains the project root, or equals, lies inside or contains `models/`, `.secrets/`, `.git`, any `.venv*`,
    `storage.data_root` or `storage.kb_store`, each side resolved first (a root inside the project outside those stays
    valid). `require_dir` also demands an existing directory. Callers ask only with retrieval on (R-009); a sentinel
    value is refused here, so a caller that tolerates one skips this call. Paths the project owns stay strict."""
    sources = sources or {}
    key = 'knowledge.root'
    root = Path(project_root).resolve()
    where = _source(sources, key, path)

    def refuse(message):
        raise ConfigurationError(f'{where}: {key}: {message}')

    value = _get(config, key, path)
    if not isinstance(value, str) or not value or '\x00' in value:
        refuse('expected a non-empty path')
    if value in SENTINELS:
        refuse(f'unresolved sentinel {value}')
    if PureWindowsPath(value).drive and os.name != 'nt':
        refuse('a Windows drive path on this platform')
    _validate_segments(value, where, key)
    supplied = Path(value.replace('\\', '/'))
    candidate = supplied if supplied.is_absolute() else root / supplied
    # Lexical pre-check: the spelled path never reaches `.secrets` or the project root, so `realpath` is not asked to
    # touch `.secrets` for the direct spellings. The physical check below is the authority.
    spelled = Path(_strip_extended_prefix(os.path.abspath(candidate)))  # a `\\?\` spelling compares like its plain form
    if _contains(spelled, root) or _overlaps(spelled, root / '.secrets'):
        refuse('must not be the project root, contain it, or lie in a protected project directory')
    try:
        physical = _physical(candidate)
    except (OSError, ValueError) as error:
        refuse(f'cannot be resolved: {type(error).__name__}')
    if _is_deleted_name(physical):
        refuse('changed while it was resolved')
    if not physical.is_absolute():
        refuse('resolves to a path form that is not absolute (a volume mount point?): use a drive letter or a directory path')
    if _contains(physical, root):
        refuse('must not be the project root or contain it')
    for name, paths in _protected_entries(root):
        if any(_overlaps(physical, protected) for protected in paths):
            refuse(f'must not be, lie inside or contain the protected project directory {name}')
    physical_parts, root_parts = _folded(physical), _folded(root)
    if (physical_parts[:len(root_parts)] == root_parts and len(physical_parts) > len(root_parts)
            and physical_parts[len(root_parts)].startswith(os.path.normcase(PROTECTED_VENV_PREFIX))):
        refuse('must not lie in an environment directory (.venv*) of the project')  # also one not created yet
    forbidden = {os.path.normcase(name) for name in (*PROTECTED_NAMES, '.venv')}
    for store_key in ('storage.data_root', 'storage.kb_store'):
        try:
            raw = _get(config, store_key, path)
        except ConfigurationError:
            continue
        if isinstance(raw, str) and raw in SENTINELS:
            continue
        store_where = _source(sources, store_key, path)
        target = _resolve_within(_lexical_path(raw, root, store_where, store_key), root, forbidden, store_where, store_key)
        if _overlaps(physical, target):
            raise ConfigurationError(f'{store_where}: {store_key} overlaps knowledge.root')
    if require_dir and not physical.is_dir():
        refuse('not an existing directory')
    return physical


def _filename(value, path, key):
    if isinstance(value, str) and value in SENTINELS:
        return
    _validate_segments(value, path, key, allow_navigation=False)
    if value in ('.', '..') or any(c in value for c in ('/', '\\', ':')):
        raise ConfigurationError(f'{path}: {key}: expected a filename')


def _validate_configured_paths(config, root, path, sources, *, knowledge_access=True):
    for key in ('storage.data_root', 'storage.kb_store', 'profiles.root'):
        if not knowledge_access and key == 'storage.kb_store':
            continue
        try:
            value = _get(config, key, path)
        except ConfigurationError:
            continue
        _configured_path(value, root, _source(sources, key, path), key)  # the project's own: strict, no link
    if knowledge_access:
        try:
            value = _get(config, 'knowledge.root', path)
        except ConfigurationError:
            value = None
        # The knowledge root is the user's read-only input: it may lie outside the project (decision 175). It is
        # resolved after the owned paths, which it is compared with; a sentinel is left to the command's own check.
        if value is not None and not (isinstance(value, str) and value in SENTINELS):
            resolve_knowledge_root(config, root, path, sources=sources)
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
    if type(data.get('schema_version')) is not int or data['schema_version'] not in CONFIG_VERSIONS:
        raise ConfigurationError(f'{path}: schema_version: expected one of {list(CONFIG_VERSIONS)}')
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
        if base['schema_version'] >= 2 and own['schema_version'] < base['schema_version']:
            # A legacy partial child inherits the base's explicit new contract (a version-1 or 2 child of a version-3
            # base becomes 3); no values are injected.
            own['schema_version'] = base['schema_version']
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
                                  'http', 'probe', 'image', 'anthropic_version', 'anthropic_workspace_id',
                                  'addendum_layout', 'addendum_separator'}
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
    if command not in REQUEST_COMMANDS:
        # Query templates belong to S1, even when offline commands validate the other embedding leaves.
        def expand(key):
            if key == 'vision':
                return MODEL_VISION_READS
            if key == 'embedding' and isinstance(config.get('embedding'), dict):
                return tuple('embedding.' + name for name in config['embedding'] if name != 'query_text_template')
            return (key,)
        return tuple(part for key in keys for part in expand(key))
    keys = tuple(part for key in (*keys, 'request_overrides')
                 for part in (VALIDATION_READS if key == 'validation' else _request_runtime_reads(config)
                              if key == 'runtime' else (key,))
                 if part != 'vlm.roles.verifier')
    if not isinstance(config['rag'], dict):
        return keys
    if config['rag']['enabled']:
        return (*keys, 'index', 'storage.kb_store', 'storage.lock')
    return tuple('rag.enabled' if key == 'rag' else key for key in keys
                 if key not in ('candidates', 'embedding', 'assembly'))


ADDENDUM_LAYOUTS = ('separate_system_message', 'joined_system_message')  # protocol-bound: model_mapping.system_messages
NO_SEPARATOR = 'none'  # protocol-bound profile value (decision 193): the separate layout sends no separator


def _validate_addendum_layout(name, backend, path):
    """T017-FIX: the addendum keys exist exactly where a request builder reads them.

    An OpenAI-compatible backend with a chat route needs `addendum_layout` (a chat template may refuse a second system
    message, J4a2 call 18); cloud backends send native system blocks and chat-less backends never chat, so the keys
    would be settings nothing reads and are refused. The separate layout takes no separator or the reserved `none`
    (decision 193: a child profile cannot delete an inherited separator, so it states `none`); any other separator
    there would be ignored and is refused. The joined layout needs a real separator, so `none` is refused there. A
    sentinel layout or separator passes here and is refused by the start gate (`_sentinel`), never defaulted.
    """
    key = f'{path}: vlm.backends.{name}'
    provider, endpoints = backend['provider'], backend['endpoints']
    if provider in SENTINELS:
        return
    chats = provider == 'openai_compatible' and (not isinstance(endpoints, dict) or 'chat' in endpoints)
    present = [field for field in ('addendum_layout', 'addendum_separator') if field in backend]
    if not chats:
        if present:
            reason = (f'{provider} sends native system blocks' if provider != 'openai_compatible'
                      else 'the backend has no endpoints.chat, so it never receives an addendum')
            raise ConfigurationError(f'{key}.{present[0]}: refused: {reason}')
        return
    if 'addendum_layout' not in backend:
        raise ConfigurationError(f'{key}.addendum_layout: missing key: an openai_compatible chat backend must state '
                                 f'how it receives the runtime addendum ({" or ".join(ADDENDUM_LAYOUTS)})')
    layout = backend['addendum_layout']
    if layout == 'joined_system_message' and 'addendum_separator' not in backend:
        raise ConfigurationError(f'{key}.addendum_separator: missing key: required by addendum_layout '
                                 'joined_system_message')
    if layout == 'joined_system_message' and backend['addendum_separator'] == NO_SEPARATOR:
        raise ConfigurationError(f'{key}.addendum_separator: refused: none sends no separator, and addendum_layout '
                                 'joined_system_message needs one between the admin prompt and the addendum')
    if layout == 'separate_system_message' and backend.get('addendum_separator', NO_SEPARATOR) != NO_SEPARATOR:
        raise ConfigurationError(f'{key}.addendum_separator: refused: addendum_layout separate_system_message '
                                 f'sends no separator (state {NO_SEPARATOR} or leave the key out)')


def image_probe_backend(vlm, role):
    """The backend whose plain `image` probe a role's calls run, or None when it cannot be named from the profile.

    The condition of `RoleClient._production_probe` and `_call`: a backend that is not `openai_compatible` and does not
    allow images never probes or serves an image, so the call (and its probe) moves to the default local backend.
    """
    backends = vlm['backends']
    name = role.get('backend')
    backend = backends.get(name) if isinstance(backends, dict) else None
    if not isinstance(backend, dict) or backend.get('provider') in SENTINELS:
        return None
    if backend.get('provider') == 'openai_compatible' or backend.get('allow_images') is True:
        return name
    default = vlm.get('default_backend')
    return default if isinstance(default, str) and default in backends else None


def attempt_seed_refusal(vlm, role_name, role=None, backend_name=None):
    """Why a role cannot take retry seeds (decisions 183, 188), or None; the caller names the attempts key.

    Attempt k sends sampling.seed + k on a backend whose protocol has a seed field (`model_mapping.SAMPLING`), so the
    role needs an integer base seed when any backend the call can reach (`backend_name`, default the role's own, and
    the default one when a budget fallback may serve it) maps `seed`. A role whose every reachable backend has no seed
    field (Anthropic) needs none: its retry is a fresh sample, recorded as such (CLOUD-1). The probe passes the
    backend its image probe really runs on.
    """
    from .planner import SEED_BITS  # lazy: planner imports this module

    role = vlm['roles'][role_name] if role is None else role
    seed = role['sampling']['seed']
    key = f'vlm.roles.{role_name}.sampling.seed'
    backends = vlm['backends']
    start = role['backend'] if backend_name is None else backend_name
    names = [start]
    first = backends.get(start) if isinstance(backends, dict) else None
    if (isinstance(first, dict) and first.get('on_budget_exhausted') == 'fallback_to_default_backend'
            and vlm.get('default_backend') not in names):
        names.append(vlm['default_backend'])
    providers = []
    for name in names:
        backend = backends.get(name) if isinstance(backends, dict) else None
        provider = backend.get('provider') if isinstance(backend, dict) else None
        if isinstance(provider, str) and provider not in SENTINELS:
            providers.append(provider)
    if providers and not any(provider_maps_seed(provider) for provider in providers):
        return None
    if type(seed) is not int or not 0 <= seed < 2 ** SEED_BITS:
        return f'{key}: an integer in [0, 2^{SEED_BITS}) is required, got {seed!r}'
    return None


def _refuse_attempts_seed(config, role_name, attempts_key, attempts, path, sources):
    if type(attempts) is int and attempts > 1:
        role = config['vlm']['roles'][role_name]
        # a role that sends images is served (and probed) by the backend `image_probe_backend` names
        served = image_probe_backend(config['vlm'], role) if isinstance(role.get('image'), dict) else None
        problem = attempt_seed_refusal(config['vlm'], role_name, backend_name=served)
        if problem:
            raise ConfigurationError(f'{_source(sources, attempts_key, path)}: {attempts_key}: {attempts} attempts need '
                                     f'distinct seeds; {problem}')


def _validate_probe_attempts(vlm, path, project_root=None):
    """`probe.image_attempts` above 1: every role with an image needs a seed base on the backend its image probe really
    runs on (`image_probe_backend`; a backend that never image-probes is not asked), and the plain `image` probe,
    retried, must fit `probe.max_requests` (a floor: the role's own challenges plus the retries; other roles on the
    same backend add more at run time and stay a runtime PROBE_INCOMPLETE). The probe bundle is read from the loader's
    project root (the runtime reads the same one); when it cannot be read the profile is refused, never skipped."""
    from .model_probe import BUNDLE_PATH, probe_image_challenges

    backends = vlm['backends']
    root = Path(__file__).resolve().parents[2] if project_root is None else Path(project_root)
    for name, role in vlm['roles'].items():
        if not isinstance(role, dict) or not isinstance(role.get('image'), dict):
            continue
        probed = image_probe_backend(vlm, role)
        if probed is None:
            continue
        backend = backends[probed]
        probe = backend.get('probe') if isinstance(backend, dict) else None
        attempts = probe.get('image_attempts') if isinstance(probe, dict) else None
        if type(attempts) is not int or attempts <= 1:
            continue
        key = f'vlm.backends.{probed}.probe.image_attempts'
        problem = attempt_seed_refusal(vlm, name, backend_name=probed)
        if problem:
            raise ConfigurationError(f'{path}: {key}: {attempts} attempts need distinct seeds; {problem}')
        limit = probe.get('max_requests')
        challenges = probe_image_challenges(root)
        output = role['output']['format'] if isinstance(role.get('output'), dict) else None
        if challenges is None:
            raise ConfigurationError(
                f'{path}: {key}: {attempts} attempts need the probe bundle {BUNDLE_PATH.as_posix()} under {root} to '
                f'count the plain image challenges against probe.max_requests, but it cannot be read there')
        if type(limit) is not int or backend.get('provider') in SENTINELS:
            continue  # not computable from the profile: the runtime keeps PROBE_INCOMPLETE
        structured = 1 if output in ('json_object', 'json_schema') else 0
        # CLOUD-2: on Anthropic a json_schema role also sends its real schema once (`schema` challenge), or, forced
        # through a tool, replaces its structured probes by one tool request each (text-level and image-level).
        forced = isinstance(role['output'], dict) and role['output'].get('transport') == 'tool_forced'
        real = 1 if (backend.get('provider') == 'anthropic' and output == 'json_schema' and not forced) else 0
        structured_images = 1 if forced else challenges * structured
        base = 1 + structured + real + challenges + structured_images
        retries = challenges * (attempts - 1)
        if base + retries > limit:
            raise ConfigurationError(
                f'{path}: {key}: {attempts} attempts make the plain image probe of role {name!r} need up to '
                f'{base + retries} requests ({base} for its challenges: 1 text + {structured + real} structured + '
                f'{challenges + structured_images} image; plus {challenges} challenges x {attempts - 1} retries = '
                f'{retries}), but vlm.backends.{probed}.probe.max_requests is {limit}; raise it to at least '
                f'{base + retries}')


def _validate_model_bindings(config, path, project_root=None):
    """Reject known provider and encoder incompatibilities before model calls."""
    from PIL import Image
    from .model_transport import endpoint_url

    vlm = config['vlm']
    backends = vlm['backends']
    for name, backend in backends.items():
        if isinstance(backend, dict):
            _validate_addendum_layout(name, backend, path)
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
        model = backend.get('model')
        model_id = model.get('id') if isinstance(model, dict) else None
        if (isinstance(reasoning, dict) and provider == 'anthropic' and model_id not in SENTINELS
                and not any(reasoning[field] in SENTINELS for field in ('mode', 'effort', 'budget_tokens'))):
            problem = anthropic_reasoning_refusal(model_id, reasoning)  # documented API errors of the bound model
            if problem:
                raise ConfigurationError(f'{path}: {key}.reasoning: {problem}')
        if isinstance(reasoning, dict) and provider == 'anthropic' and reasoning['mode'] == 'on':
            budget = reasoning['budget_tokens']
            if budget not in SENTINELS and maximum not in SENTINELS:
                if type(budget) is not int or budget < 1024 or budget >= maximum:
                    raise ConfigurationError(f'{path}: {key}.reasoning.budget_tokens: Anthropic requires >= 1024 and < max_tokens')
        output = role['output']
        if isinstance(output, dict) and output['format'] == 'json_schema' and output['schema_id'] in ('none', 'from_profile_manifest'):
            raise ConfigurationError(f'{path}: {key}.output.schema_id: json_schema requires a concrete schema')
        if isinstance(output, dict) and output['transport'] == 'tool_forced':
            _validate_tool_forced(name, role, provider, path, key, backend)
        if isinstance(output, dict) and provider in ('anthropic', 'gemini'):
            fmt = output['format']
            manifest_bound = (name == 'writer' and fmt == 'from_profile_manifest'
                              and output['schema_id'] == 'from_profile_manifest')
            if provider == 'anthropic' and fmt == 'json_schema' and output['transport'] == 'tool_forced':
                pass  # a forced non-strict tool carries the schema unchanged: nothing is stripped or compiled
            elif provider == 'anthropic' and fmt == 'json_schema':
                _validate_anthropic_schemas(name, output['schema_id'], path, key, project_root)
            elif fmt not in SENTINELS and fmt != 'none' and not manifest_bound:
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
    _validate_probe_attempts(vlm, path, project_root)


TOOL_FORCED_ROLES = frozenset(('planner',))  # artifact-bound: the roles whose reply the client reads from a tool block


def _validate_tool_forced(role_name, role, provider, path, key, backend=None):
    """CLOUD-2: `output.transport: tool_forced` is implemented only for the planner on an Anthropic backend, with a
    concrete schema, `validate_response: true` and no manual thinking (the API refuses a forced tool with it). With
    `backend` given it also refuses the two routes that would send the call to the local default backend, which cannot
    read a tool reply: a budget fallback and an image the cloud backend does not accept (`allow_images: false`)."""
    where = f'{path}: {key}.output.transport'
    allowed = 'tool_forced is allowed only for vlm.roles.planner on an Anthropic backend with format json_schema'
    if role_name not in TOOL_FORCED_ROLES:
        raise ConfigurationError(f'{where}: {allowed}; this role uses response_format')
    if provider != 'anthropic':
        raise ConfigurationError(f'{where}: {allowed}; backend provider is {provider!r}')
    output = role['output']
    if output['format'] != 'json_schema':
        raise ConfigurationError(f'{where}: {allowed}; format is {output["format"]!r}')
    if output['validate_response'] is not True:
        raise ConfigurationError(f'{where}: tool_forced requires {key}.output.validate_response: true')
    reasoning = role['reasoning']
    if isinstance(reasoning, dict) and reasoning['mode'] == 'on':
        raise ConfigurationError(f'{where}: tool_forced is refused with {key}.reasoning.mode on '
                                 '(a forced tool call is an API error with manual extended thinking)')
    if isinstance(backend, dict):
        backend_key = f'vlm.backends.{role["backend"]}'
        if backend.get('on_budget_exhausted') == 'fallback_to_default_backend':
            raise ConfigurationError(
                f'{where}: tool_forced cannot fall back to the local default backend, but {backend_key}.'
                'on_budget_exhausted is fallback_to_default_backend; set it to stop')
        if isinstance(role['image'], dict) and backend.get('allow_images') is False:
            raise ConfigurationError(
                f'{where}: tool_forced with an image role ({key}.image) would fall back to the local default backend '
                f'because {backend_key}.allow_images is false; set it to true or use response_format')


def _validate_anthropic_schemas(role_name, schema_id, path, key, project_root):
    """A role that sends `json_schema` to Anthropic: every schema it can bind must be sendable as it is (CLOUD-1).

    The registered schema, and for the planner the generate variant S2 chooses at run time, go through
    `anthropic_output_schema`; a schema it refuses is a start-up refusal, never a repaired one.
    """
    from .model_cloud import anthropic_output_schema
    from .model_schemas import cached_registry
    from .planner import GENERATE_REPLY_SCHEMA

    if schema_id in SENTINELS:
        return
    root = Path(__file__).resolve().parents[2] if project_root is None else Path(project_root)
    registry = cached_registry(root / 'contracts' / 'schemas')
    for candidate in (schema_id, GENERATE_REPLY_SCHEMA) if role_name == 'planner' else (schema_id,):
        try:
            schema, _ = registry.get(candidate)
        except KeyError:
            raise ConfigurationError(f'{path}: {key}.output.schema_id: unknown schema {candidate!r}') from None
        try:
            anthropic_output_schema(schema)
        except ValueError as error:
            raise ConfigurationError(f'{path}: {key}.output.schema_id: {candidate} cannot be sent to Anthropic '
                                     f'structured output: {error}') from error


def validate_anthropic_writer_schema(schema, source):
    """T017-FIX4: the writer's resolved schema (the admin bundle's or a project contract) must be sendable to Anthropic.

    The writer's schema is unknown at load (`from_profile_manifest`), so the check runs where it becomes known: a
    profile load (`source` names `output_schema.json`) and the request path (`writer.prepare_write`). It is the same
    `anthropic_output_schema` check a registered schema gets at load (CLOUD-1); a refusal names `source`, never repairs."""
    from .model_cloud import anthropic_output_schema

    try:
        anthropic_output_schema(schema)
    except ValueError as error:
        raise ConfigurationError(f'{source}: cannot be sent to Anthropic structured output: {error}') from error


def writer_provider(config):
    """The provider of the writer role's backend, or None while the role or backend is a sentinel or absent."""
    vlm = config.get('vlm') if isinstance(config, dict) else None
    roles = vlm.get('roles') if isinstance(vlm, dict) else None
    role = roles.get('writer') if isinstance(roles, dict) else None
    name = role.get('backend') if isinstance(role, dict) else None
    backends = vlm.get('backends') if isinstance(vlm, dict) else None
    backend = backends.get(name) if isinstance(backends, dict) and isinstance(name, str) else None
    return backend.get('provider') if isinstance(backend, dict) else None


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
# Containers K1 reads only some children of: checked for a sentinel in place of the container and for the schema's
# shape, while only the listed leaves are sentinel-checked (the serving limits and `reference_check` are read by
# `embed serve` / `embed reference-check`, not by K1; T-018 P3 fix round 1, F7).
_PARTLY_READ_CONTAINERS = ('server',)
# Read, not hashed: the client's request concurrency must fit the server's admission slots (RES-1 Q4).
EMBED_SERVER_CLIENT_READS = ('server.max_pending_requests',)
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
        fields += EMBED_SERVER_CLIENT_READS
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
    partial = {f'vlm.backends.{_get(config, "embedding.backend", path)}.{field}' for field in _PARTLY_READ_CONTAINERS}
    for key in keys:
        value = _get(config, key, path)
        _sentinel(None if key in partial and isinstance(value, dict) else value, path, key, sources)
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)
    for key in keys:
        value = _get(config, key, path)
        node = _schema_at(schema, key)
        _validate(value, node, path, key, sources, _EXACT_INTEGERS)
        if key not in partial:  # the container's read leaves are keys of their own
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
        # RES-1 Q4: requests beyond the server's admission slots are refused as overloaded by design, so a client that
        # sends more would only retry against a refusal it caused itself.
        key = f'vlm.backends.{name}.max_concurrency'
        if _get(config, key, path) > _get(config, f'vlm.backends.{name}.server.max_pending_requests', path):
            raise ConfigurationError(f'{_source(sources, key, path)}: {key}: exceeds server.max_pending_requests; the '
                                     'extra requests would be refused as overloaded')
    _check_backoff_order(config['vlm']['backends'][name]['http'], path, f'vlm.backends.{name}.http', sources)
    # Decision 144 applies to chat roles only so far: K1's embedding-space identity does not include a backend cap,
    # so a cap here would be silently ignored or would change stored vectors without changing their key (T-014 Q5).
    key = f'vlm.backends.{name}.image.max_side'
    if _get(config, key, path) != NONE:
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: an embedder backend must keep none until '
                                 "K1's cache identity includes the backend image cap")


SERVER_READS =('runtime.gpus', 'runtime.vram_budget_gb', 'embedding.backend', 'embedding.batch_size',
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
    schema = _reads_schema(root, config)
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
    if server['attn_implementation'] not in family.attn_implementations:  # before any hash, bind or load (F6)
        refuse(f'{prefix}.server.attn_implementation', f"{server['attn_implementation']} is not supported by "
               f"{family.name} in the pinned transformers; expected one of {', '.join(family.attn_implementations)}")
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


REFERENCE_CHECK_READS = ('server.reference_check', 'server.reference_check.min_cosine',
                         'server.reference_check.max_norm_deviation')


def validate_reference_check_reads(config, path, sources, project_root=None):
    """The reads of `embed reference-check` beyond the server's own (T-018 P2): the tolerances the check compares
    against, `min_cosine` in the schema's (0, 1] and `max_norm_deviation` in [0, 1) (P2 fix round 1, N1). Present and
    resolved (a sentinel such as `fit_from_bench` is refused). The server itself never reads them; `reference-check
    reference` measures them and does not read them either."""
    validate_embed_server_reads(config, path, sources, project_root)
    name = config['embedding']['backend']
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)
    for field in REFERENCE_CHECK_READS:
        key = f'vlm.backends.{name}.{field}'
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)  # the schema's number excludes bool (jsonschema is_number)


# Protocol-bound supported naming policies (K2-observe.md, naming step); no content decisions live here.
NAMING_POLICIES = {'on_no_info_file': 'model_names_from_images',
                   'on_unusable_info_file': 'model_names_from_images',
                   'selection': 'random', 'on_no_images': 'skip'}
NAMING_ROLES = ('info_assessor', 'folder_namer')


def validate_naming_reads(config, path='configuration', sources=None, project_root=None):
    """The same strict K2 preflight at load and standalone entry, before source reads or model calls."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)

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
    for key in MODEL_VISION_READS:
        checked(key)
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
    _validate_model_bindings(config, path, project_root)
    _validate_configured_paths(config, root, path, sources)


def validate_observation_reads(config, path='configuration', sources=None, project_root=None):
    """Validate the standalone/command observer read set before reads, checkpoints or model activity."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)

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
    for key in MODEL_VISION_READS:
        checked(key)
    checked('knowledge.root')
    checked('storage.kb_store')
    checked('storage.data_root')
    default = checked('vlm.default_backend')
    key = 'vlm.roles.observer'
    role = checked(key, _schema(root, 'vlm_role.v0'))
    if role['prompt_id'] != 'observer_v1':
        refuse(key + '.prompt_id', 'expected observer_v1')
    if role['output']['schema_id'] != 'observer_reply.v1':
        refuse(key + '.output.schema_id', 'expected observer_reply.v1')
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
    _refuse_attempts_seed({'vlm': {'roles': {'observer': role}, 'backends': backends, 'default_backend': default}},
                          'observer', 'observe.reply_attempts', observe['reply_attempts'], path, sources)
    scoped = {'vlm': {'roles': {'observer': role}, 'backends': backends, 'default_backend': default},
              'vision': config['vision'], 'knowledge': {'root': config['knowledge']['root']},
              'storage': {field: config['storage'][field] for field in ('kb_store', 'data_root')}}
    _validate_model_bindings(scoped, path, project_root)
    _validate_configured_paths(scoped, root, path, sources)


# Protocol/artifact-bound policy vocabulary; semantic facet/value vocabularies remain open.
CARD_LABELS = ('curated', 'stable', 'stable_low_diversity', 'varies', 'single_observation')
CARD_POLICY_VERSION = 'invariance_v0'


def validate_card_reads(config, path='configuration', sources=None, project_root=None):
    """Validate K3's complete read set before opening a writer or composing a model client."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)

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
    _refuse_attempts_seed({'vlm': {'roles': {'card_builder': role}, 'backends': backends, 'default_backend': default}},
                          'card_builder', 'cards.reply_attempts', cards['reply_attempts'], path, sources)
    scoped = {'vlm': {'roles': {'card_builder': role}, 'backends': backends, 'default_backend': default},
              'knowledge': {'root': config['knowledge']['root']},
              'storage': {field: config['storage'][field] for field in ('kb_store', 'data_root')}}
    _validate_model_bindings(scoped, path, project_root)
    _validate_configured_paths(scoped, root, path, sources)


def validate_alias_reads(config, path='configuration', sources=None, project_root=None):
    """Validate K4's complete read set before constructing a writer or model client."""
    sources = sources or {}
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)

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
    _validate_model_bindings(scoped, path, project_root)
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
        if type(data.get('schema_version')) is not int or data['schema_version'] not in CONFIG_VERSIONS:
            raise ConfigurationError(f'{overlay_path}: schema_version: expected one of {list(CONFIG_VERSIONS)}')
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
        if config['schema_version'] >= 2 and own['schema_version'] < config['schema_version']:
            own['schema_version'] = config['schema_version']
        config = _merge(config, own)
        _mark_sources(own, overlay_path, sources)
    return _validate_loaded_config(config, command, root, path, sources)


def validate_sync_config(config, *, project_root):
    """Apply the command preflight to a supplied mapping before any stage/store/source activity."""
    root = Path(project_root).resolve()
    validated = _validate_loaded_config(config, 'kb sync', root, 'configuration', {})
    # The run itself is about to read the knowledge root, so its resolved target must be a directory; a configuration that is
    # only loaded for its shape (`load_config`) may name a knowledge base that does not exist yet. With retrieval off the
    # run reads nothing of the knowledge base, so nothing of it is resolved either (R-009).
    if config['rag']['enabled']:
        resolve_knowledge_root(config, root, 'configuration', require_dir=True)
    return validated


def _is_source_limit_key(key):
    """A dotted path naming `edit.max_source_images`, a backend's `image.max_images_per_call` or a `renderer` limit."""
    parts = key.split('.')
    return (parts == ['edit', 'max_source_images']
            or (len(parts) == 5 and parts[:2] == ['vlm', 'backends'] and parts[3:] == ['image', 'max_images_per_call'])
            or (len(parts) == 2 and parts[0] == 'renderer' and parts[1] == 'max_source_images'))


def _validate_override_policy(config, schema, path):
    allowed = _get(config, 'request_overrides.allowed_keys', path)
    _validate(allowed, _schema_at(schema, 'request_overrides.allowed_keys'), path,
              'request_overrides.allowed_keys', validator=_EXACT_INTEGERS)
    for key in allowed:
        if key.split('.')[0] == 'request_overrides':
            raise ConfigurationError(f'{path}: request_overrides.allowed_keys: policy is not overridable')
        if _is_source_limit_key(key):
            raise ConfigurationError(f'{path}: request_overrides.allowed_keys: {key}: a request cannot raise a source '
                                     'image limit (decision 179); remove it from the policy')
        try:
            _schema_at(schema, key)
            value = _get(config, key, path)
        except (KeyError, ConfigurationError):
            raise ConfigurationError(f'{path}: request_overrides.allowed_keys: unknown path {key!r}') from None
        if isinstance(value, dict):
            raise ConfigurationError(f'{path}: request_overrides.allowed_keys: expected leaf path {key!r}')
    return allowed


CANDIDATE_READS = tuple(f'candidates.sources.{name}.{field}'
                        for name in ('alias', 'lexical', 'dense') for field in ('enabled', 'depth')) + (
    'candidates.segment_prompt', 'candidates.fusion', 'candidates.shortlist_size',
    'candidates.rrf_constant', 'candidates.max_prompt_windows',
    'embedding.query_text_template', 'embedding.query_text_limit')


def validate_candidates_reads(config, path='configuration', sources=None, project_root=None):
    """S1's direct-entry and request-startup gate; query-only values never enter K1's offline reads."""
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)
    for key in CANDIDATE_READS:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        if key.endswith('.depth') or key in ('candidates.max_prompt_windows', 'embedding.query_text_limit'):
            if type(value) is not int or not 0 < value <= sys.maxsize:
                raise ConfigurationError(f'{path}: {key}: expected a positive native integer')
    if not _converts_to_float(config['candidates']['rrf_constant']):
        raise ConfigurationError(f'{path}: candidates.rrf_constant: expected a finite representable number')
    if config['embedding']['query_text_template'].count(TEXT_PLACEHOLDER) != 1:
        raise ConfigurationError(f'{path}: embedding.query_text_template: expected exactly one {TEXT_PLACEHOLDER}')
    if config['candidates']['sources']['dense']['enabled']:
        validate_embedder_reads(config, path, sources, root)
        _validate_embedding(config, path, sources)
    return config['candidates']


PLANNER_READS = ('planner.second_chance_rounds', 'planner.catalogue_in_context', 'resolution.on_family_match',
                 'resolution.random_seed', 'resolution.trace_choice', 'clarification.mode', 'creativity',
                 'edit.default_preservation', 'edit.source_entity_linking', 'vlm.roles.planner')
def _reads_schema(root, config):
    """The schema a stage's read check validates leaves with: the version-3 composition when the file is version 3 (its
    backend `image` and role objects gained keys), else `enhancer_config.v0` exactly as before."""
    if config.get('schema_version') == SOURCE_LIMITS_VERSION:
        return _configuration_schema(root, config)
    return _schema(root, 'enhancer_config.v0')


def _role_core(role):
    """The role without the version-3 `multi_source` block, which `vlm_role.v0` (byte-identical) does not know."""
    if isinstance(role, dict) and 'multi_source' in role:
        return {key: value for key, value in role.items() if key != 'multi_source'}
    return role


def _planner_role_schema_id(config):
    """CLOUD-2: the planner of a version-3 file is checked against `vlm_role.v1` (v0 plus `tool_forced`); every other
    role, and every file below version 3, keeps `vlm_role.v0`, so `tool_forced` is refused there."""
    return 'vlm_role.v1' if isinstance(config, dict) and config.get('schema_version') == SOURCE_LIMITS_VERSION else (
        'vlm_role.v0')


def _require_tool_forced_version(config, path):
    """CLOUD-2: `output.transport: tool_forced` exists only in configuration version 3 (`enhancer_config.v2`); a file
    below it that names it is refused with the key and the version, before the generic enum message."""
    roles = config.get('vlm', {}).get('roles') if isinstance(config.get('vlm'), dict) else None
    if config.get('schema_version') == SOURCE_LIMITS_VERSION or not isinstance(roles, dict):
        return
    for name, role in roles.items():
        output = role.get('output') if isinstance(role, dict) else None
        if isinstance(output, dict) and output.get('transport') == 'tool_forced':
            raise ConfigurationError(
                f'{path}: vlm.roles.{name}.output.transport: tool_forced needs schema_version {SOURCE_LIMITS_VERSION} '
                f'(enhancer_config.v2); migrate the configuration or use response_format')


PLANNER_REPLY_SCHEMA = 'planner_reply.v0'  # artifact-bound: S2 builds intent_ir.v0 from this reply shape only
PLANNER_IMAGE_SOURCE = 'edit_source_only'  # protocol-bound: the only image S2 can send is the request's source image


def validate_planner_reads(config, path='configuration', sources=None, project_root=None):
    """S2's request-startup and direct-entry gate (T-014); retrieval on or off, the planner always runs."""
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    for key in PLANNER_READS:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
    role = config['vlm']['roles']['planner']
    _validate(_role_core(role), _schema(root, _planner_role_schema_id(config)), path, 'vlm.roles.planner', sources)
    # Decision 144: the cap of every backend a planner call may reach, as an exact integer or none.
    for name in sorted({role['backend'], _get(config, 'vlm.default_backend', path)}):
        key = f'vlm.backends.{name}.image.max_side'
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
    if config['planner']['second_chance_rounds'] > sys.maxsize:
        refuse('planner.second_chance_rounds', 'expected a native integer')
    if config['planner']['catalogue_in_context'] is not False:
        refuse('planner.catalogue_in_context', 'true is not implemented: the planner sees the shortlist only (N120)')
    if config['edit']['source_entity_linking'] is not False:
        refuse('edit.source_entity_linking', 'true is not implemented: source-image entity linking is later work')
    if config['resolution']['on_family_match'] == 'ask' and config['clarification']['mode'] != 'ask':
        refuse('resolution.on_family_match', 'ask requires clarification.mode ask (decision 68)')
    output = role['output']
    if output['format'] != 'json_schema' or output['schema_id'] != PLANNER_REPLY_SCHEMA:
        refuse('vlm.roles.planner.output', f'expected format json_schema with schema_id {PLANNER_REPLY_SCHEMA}')
    if output['transport'] not in ('response_format', 'tool_forced'):
        refuse('vlm.roles.planner.output.transport', 'expected response_format, or tool_forced on an Anthropic backend')
    if role['image'] != 'none' and role['image']['source'] != PLANNER_IMAGE_SOURCE:
        refuse('vlm.roles.planner.image.source', f'S2 can send only the request source image ({PLANNER_IMAGE_SOURCE}) '
               'or no image (image: none)')
    return config['planner']


PLANNER_MULTI_SOURCE_SCHEMA = 'planner_reply.v1'  # artifact-bound: the reply shape S2 builds intent_ir.v1 from (MS-4)


def validate_source_limits(config, path='configuration', sources=None, project_root=None, *, prompts=True):
    """Decision 179: the three required limits and the two multi-source prompt bindings, each refused by key.

    Runs after the planner and writer reads (roles and backends are validated by then). The backend of the planner and
    of the writer must carry at least `edit.max_source_images` images per call, because a request over any limit is
    refused and never truncated. Nothing here reads the knowledge base, and no value is defaulted."""
    from .profile_prompt import ProfileError, load_prompt, placeholders

    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    _require_source_limits_version(config, path)

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    cap = check_limit(_get(config, 'edit.max_source_images', path), path, 'edit.max_source_images', sources)
    for role_name in ('planner', 'writer'):
        role = config['vlm']['roles'][role_name]
        key = f"vlm.backends.{role['backend']}.image.max_images_per_call"
        limit = check_limit(_get(config, key, path), path, key, sources)
        if limit < cap:
            refuse(key, f'{limit} is below edit.max_source_images ({cap}); a call of the {role_name} role could not carry '
                        'every source image')
        multi = _get(config, f'vlm.roles.{role_name}.multi_source', path)
        _sentinel(multi, path, f'vlm.roles.{role_name}.multi_source', sources)
        key = f'vlm.roles.{role_name}.multi_source.prompt_id'
        if role_name == 'planner' and multi['schema_id'] != PLANNER_MULTI_SOURCE_SCHEMA:
            refuse('vlm.roles.planner.multi_source.schema_id', f'expected {PLANNER_MULTI_SOURCE_SCHEMA}')
        if not prompts:
            continue  # a per-request re-validation reads no prompt file unless an override names a multi_source key
        try:
            template = load_prompt(root, multi['prompt_id'])
        except ConfigurationError:
            refuse(key, 'unknown prompt id: no readable prompts/<id>.txt')
        if role_name == 'writer':
            try:
                names = placeholders(template)
            except ProfileError:
                names = None
            if names != ['data'] or template.count(b'{{data}}') != 1:  # placeholders() lists each name once
                refuse(key, 'the brief template requires exactly one {{data}} variable and no other')
    return cap


WRITER_READS = ('writer', 'creativity', 'rag.enabled', 'vlm.roles.writer', 'vlm.default_backend')


def validate_writer_reads(config, path='configuration', sources=None, project_root=None):
    """S4's startup/direct-entry gate; no inferred policy or unresolved role setting."""
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)
    for key in WRITER_READS:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
    for key, expected in (('writer.system_prompt_verbatim', True), ('writer.knowledge_block', 'delimited_data')):
        if _get(config, key, path) != expected:
            raise ConfigurationError(f'{path}: {key}: expected {expected}')
    role = config['vlm']['roles']['writer']
    _validate(_role_core(role), _schema(root, 'vlm_role.v0'), path, 'vlm.roles.writer', sources)
    if role['output']['validate_response'] is not True:
        raise ConfigurationError(f'{path}: vlm.roles.writer.output.validate_response: expected true')
    if role['output']['transport'] != 'response_format':
        raise ConfigurationError(f'{path}: vlm.roles.writer.output.transport: only response_format is implemented')
    if role['output']['format'] != 'from_profile_manifest' or role['output']['schema_id'] != 'from_profile_manifest':
        raise ConfigurationError(f'{path}: vlm.roles.writer.output: expected paired from_profile_manifest')
    reads = []
    if config['rag']['enabled']:
        reads.append('rag.evidence_mode')
    if isinstance(role['image'], dict):
        for field in ('source', 'max_side', 'format', 'quality', 'transport'):
            _get(config, 'vlm.roles.writer.image.' + field, path)
        if role['image']['transport'] != 'data_uri':
            raise ConfigurationError(f'{path}: vlm.roles.writer.image.transport: expected data_uri')
        if role['image']['source'] not in ('source_image', 'edit_source_only', 'edit_source_and_live_look'):
            raise ConfigurationError(f'{path}: vlm.roles.writer.image.source: expected the request source image')
        reads += ['vision.alpha_policy']
        if role['image']['max_side'] == 'from_vision':
            reads.append('vision.max_image_side')
        if role['image']['format'] == 'from_vision':
            reads.append('vision.format')
    for name in sorted({role['backend'], config['vlm']['default_backend']}):
        reads.append(f'vlm.backends.{name}')
    for key in reads:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        if key.startswith('vlm.backends.'):
            _configured_path(value['api_key_file'], root, path, key + '.api_key_file', secret=True)
            # Safety-bound protocol fields: engine extras cannot replace R-004 bytes or R-018 identity.
            for field in ('messages', 'system', 'systemInstruction', 'contents', 'model'):
                if field in value.get('extra_body', {}):
                    raise ConfigurationError(f'{path}: {key}.extra_body.{field}: cannot replace writer identity')
    if config['rag']['enabled'] and config['rag']['evidence_mode'] != 'cards':
        raise ConfigurationError(f'{path}: rag.evidence_mode: live modes are not implemented')
    _validate_model_bindings(config, path, project_root)
    return role


VALIDATION_READS = ('validation.structural_checks', 'validation.verifier.gating_rules',
                    'validation.verifier.inline_for', 'validation.verifier.sampled_fraction_for_rest',
                    'validation.verifier.require_calibration',
                    'validation.max_repairs', 'validation.fallbacks', 'resolution.random_seed')
# Protocol-bound policy tags, never rules over natural-language input.
VERIFIER_GATES = frozenset(('task_is_edit', 'identity_facts_supplied', 'profile_has_known_conflict',
                            'ledger_entry_needs_meaning'))
VERIFIER_INLINE = frozenset(('edit', 'identity_facts'))
VALIDATION_FALLBACKS = frozenset(('minimal_output_valid_for_contract', 'user_prompt_with_status_degraded', 'failed'))


def _inline_defs(node, defs):
    """Replace each `{"$ref": "#/$defs/<name>"}` of a fragment by a copy of that definition (no other reference form)."""
    if isinstance(node, dict):
        if set(node) == {'$ref'}:
            prefix = '#/$defs/'
            if not isinstance(node['$ref'], str) or not node['$ref'].startswith(prefix) or node['$ref'][len(prefix):] not in defs:
                raise ConfigurationError('configuration: invalid local configuration schema reference')
            return _inline_defs(deepcopy(defs[node['$ref'][len(prefix):]]), defs)
        return {key: _inline_defs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_defs(value, defs) for value in node]
    return node


def _extend_source_limits(root, schema):
    """Compose `enhancer_config.v2` (version 3) over the expanded version-2 schema: the schema's own `edit` node, the
    constant `schema_version`, and the three `vlm` fragments, inserted where `enhancer_config.v0` defines the backend
    `image` object and the role object. No schema text is copied from v0 or v1 (the pattern of `pipeline_runtime.v0`)."""
    extension = _schema(root, 'enhancer_config.v2')
    defs = extension['$defs']
    for key, node in extension['properties'].items():
        if key in ('schema_version', 'edit'):
            schema['properties'][key] = _inline_defs(node, defs)
        elif node != {'$ref': 'enhancer_config.v1#/properties/' + key} or key not in schema['properties']:
            raise ConfigurationError('configuration: invalid local configuration schema reference')
    try:
        vlm = schema['properties']['vlm']['anyOf'][0]['properties']
        image = vlm['backends']['anyOf'][0]['additionalProperties']['anyOf'][0]['properties']['image']
        roles = vlm['roles']['anyOf'][0]
        alternatives = roles['additionalProperties']['anyOf']
        image['properties']['max_images_per_call'] = _inline_defs(defs['backend_image_limit'], defs)
        image['required'] = [*image['required'], 'max_images_per_call']
        roles['properties'] = {}
        # CLOUD-2: only the planner may name `tool_forced`; its transport values are those of `vlm_role.v1`.
        planner_transports = _schema(root, 'vlm_role.v1')['properties']['output']['properties']['transport']['enum']
        for name, fragment in (('planner', 'planner_multi_source'), ('writer', 'writer_multi_source')):
            variants = deepcopy(alternatives)
            role = variants[0]
            role['properties']['multi_source'] = _inline_defs(defs[fragment], defs)
            role['required'] = [*role['required'], 'multi_source']
            if name == 'planner':
                role['properties']['output']['anyOf'][0]['properties']['transport']['anyOf'][0]['enum'] = list(
                    planner_transports)
            roles['properties'][name] = {'anyOf': variants}
    except (KeyError, IndexError, TypeError) as error:
        raise ConfigurationError('configuration: invalid local configuration schema reference') from error
    return schema


def _configuration_schema(root, config):
    """Expand the versioned config's local top-level references; `_schema` caching stays untouched."""
    if not isinstance(config, dict):
        raise ConfigurationError('configuration: expected an object')
    version = config.get('schema_version')
    if type(version) is not int or version not in CONFIG_VERSIONS:
        raise ConfigurationError(f'configuration: schema_version: expected one of {list(CONFIG_VERSIONS)}')
    base = _schema(root, 'enhancer_config.v0')
    if version == 1:
        return base
    schema = deepcopy(_schema(root, 'enhancer_config.v1'))
    for key, node in schema['properties'].items():
        if '$ref' in node:
            if node != {'$ref': 'enhancer_config.v0#/properties/' + key} or key not in base['properties']:
                raise ConfigurationError('configuration: invalid local configuration schema reference')
            schema['properties'][key] = deepcopy(base['properties'][key])
    runtime = schema['properties']['runtime']['anyOf'][0]
    extension = _schema(root, 'pipeline_runtime.v0')
    runtime['properties'].update(deepcopy(extension['properties']))
    runtime['required'].extend(extension['required'])
    return _extend_source_limits(root, schema) if version == SOURCE_LIMITS_VERSION else schema


PIPELINE_READS = ('runtime.request_deadline_s', 'runtime.trace.retention_s', 'runtime.trace.queue_limit',
                  'runtime.trace.workers', 'runtime.trace.drain_timeout_s')
MONITOR_READS = ('runtime.monitoring.queue_limit', 'runtime.monitoring.workers', 'runtime.monitoring.drain_timeout_s')


def _require_attachment_support(config, path, sources):
    key = 'edit.attach_kb_reference_images'
    value = _get(config, key, path)
    _sentinel(value, path, key, sources)
    if type(value) is not bool:
        raise ConfigurationError(f'{path}: {key}: expected boolean')


def _request_runtime_reads(config):
    runtime = config.get('runtime')
    if not isinstance(runtime, dict):
        return ('runtime',)
    fraction = config.get('validation', {}).get('verifier', {}).get('sampled_fraction_for_rest')
    monitoring = MONITOR_READS if type(fraction) in (int, float) and fraction > 0 else ()
    return (*('runtime.' + key for key in runtime if key not in ('request_deadline_s', 'trace', 'monitoring')),
            *PIPELINE_READS, *monitoring)


def validate_pipeline_reads(config, path='configuration', sources=None, project_root=None):
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    _require_validation_version(config, path)
    _require_attachment_support(config, path, sources)
    validate_validation_reads(config, path, sources, root)
    schema = _schema(root, 'pipeline_runtime.v0')
    fraction = config['validation']['verifier']['sampled_fraction_for_rest']
    reads = (*PIPELINE_READS, *(MONITOR_READS if fraction > 0 else ()))
    result = {}
    for key in reads:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _require_finite(value, path, key, sources)
        relative = key.removeprefix('runtime.')
        _validate(value, _schema_at(schema, relative), path, key, sources, _EXACT_INTEGERS)
        if isinstance(value, bool):
            raise ConfigurationError(f'{path}: {key}: expected a positive number')
        if relative.endswith(('_deadline_s', '_timeout_s', 'retention_s')):
            _require_float_seconds(value, path, key, sources)
        if relative == 'trace.retention_s':
            try:
                datetime.now(timezone.utc) + timedelta(seconds=value)
            except OverflowError as error:
                raise ConfigurationError(f'{path}: {key}: expiry is outside the datetime range') from error
        parts = relative.split('.')
        target = result if len(parts) == 1 else result.setdefault(parts[0], {})
        target[parts[-1]] = deepcopy(value)
    return result


REQUEST_LIMIT_KEY = 'serving.http.max_request_bytes'  # protocol-bound name of the request byte cap (T-016, PM answer Q2)


def validate_request_limit(config, path='configuration', sources=None, project_root=None):
    """The byte cap of one request (`enhance once` request file, T-016 P3 HTTP body): a validated profile value.

    Read by the command line, not by the per-request pipeline reads, so a library caller or a golden check that
    never receives a request file needs no value here."""
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema, block_key = _configuration_schema(root, config), REQUEST_LIMIT_KEY.rpartition('.')[0]
    block = _get(config, block_key, path)
    _sentinel(block, path, block_key, sources)  # also refuses a sentinel at any depth, naming the leaf
    _validate(block, _schema_at(schema, block_key), path, block_key, sources, _EXACT_INTEGERS)  # shape, unknown keys
    value = _get(config, REQUEST_LIMIT_KEY, path)
    _validate(value, _schema_at(schema, REQUEST_LIMIT_KEY), path, REQUEST_LIMIT_KEY, sources, _EXACT_INTEGERS)
    if type(value) is not int or value <= 0 or value > sys.maxsize:
        raise ConfigurationError(f'{_source(sources, REQUEST_LIMIT_KEY, path)}: {REQUEST_LIMIT_KEY}: '
                                 'expected a positive native integer')
    return value


# protocol-bound: RFC 1123 limits of a host name (253 characters in all, 63 in a label)
_HOST_NAME_LIMIT, _HOST_LABEL_LIMIT = 253, 63
_HOST_LABEL_CHARACTERS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-')
SERVING_TIMERS = ('read_timeout_s', 'write_timeout_s', 'shutdown_timeout_s')  # protocol-bound `serving.http` leaf names


def _listen_host(value, path, key, sources):
    """An IP address, or a host name in RFC 1123 syntax; whether it can be bound is left to the bind itself."""
    where = f'{_source(sources, key, path)}: {key}'
    if type(value) is not str or not value:
        raise ConfigurationError(f'{where}: expected a non-empty host name or IP address')
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    labels = value.rstrip('.').split('.') if len(value) <= _HOST_NAME_LIMIT else ()
    if not labels or any(not label or len(label) > _HOST_LABEL_LIMIT or label[0] == '-' or label[-1] == '-'
                         or not set(label) <= _HOST_LABEL_CHARACTERS for label in labels):
        raise ConfigurationError(f'{where}: not an IP address or a valid host name')
    return value


def _serving_number(config, name, path, sources, *, minimum, integer):
    """One `serving` leaf: a native integer (never a bool) or a finite number, at or above `minimum`."""
    key = f'serving.{name}'
    value = _get(config, key, path)
    where = f'{_source(sources, key, path)}: {key}'
    if type(value) is int:
        # a count is held in a native integer; a duration only has to convert to the float the timer clocks add
        if (integer and value > sys.maxsize) or not _converts_to_float(value):
            raise ConfigurationError(f'{where}: the number is outside the range a timer or a count can hold')
    elif integer or type(value) is not float or not math.isfinite(value):
        raise ConfigurationError(f'{where}: expected {"a native integer" if integer else "a finite number"}')
    if value < minimum or (not integer and value <= 0):
        raise ConfigurationError(f'{where}: expected {"a positive" if minimum or not integer else "a non-negative"} '
                                 'value')
    return value


def validate_serving(config, path='configuration', sources=None, project_root=None):
    """Every value `enhance serve` consumes, after the loader's per-leaf sentinel and schema checks, as one mapping.

    `serving.prefix_cache` is not read: it describes the external model server (T-016 Q2). A deployment value is never
    defaulted: a missing or unresolved leaf is refused naming its key. Cross-field rules: the connection limit must
    hold every active and waiting request, and the service deadline must exceed the pipeline's own
    `runtime.request_deadline_s`, which it wraps and never replaces."""
    sources = {} if sources is None else sources
    values = {'host': _listen_host(_get(config, 'serving.host', path), path, 'serving.host', sources)}
    port = _get(config, 'serving.port', path)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigurationError(f'{_source(sources, "serving.port", path)}: serving.port: expected an integer '
                                 'from 1 to 65535')
    values['port'] = port
    values['max_concurrency'] = _serving_number(config, 'max_concurrency', path, sources, minimum=1, integer=True)
    values['queue_limit'] = _serving_number(config, 'queue_limit', path, sources, minimum=0, integer=True)
    values['total_deadline_s'] = _serving_number(config, 'total_deadline_s', path, sources, minimum=0, integer=False)
    values['max_request_bytes'] = validate_request_limit(config, path, sources, project_root)
    values['max_connections'] = _serving_number(config, 'http.max_connections', path, sources, minimum=1, integer=True)
    for name in SERVING_TIMERS:
        values[name] = _serving_number(config, f'http.{name}', path, sources, minimum=0, integer=False)
    needed = values['max_concurrency'] + values['queue_limit']
    if values['max_connections'] < needed:
        raise ConfigurationError(f'{_source(sources, "serving.http.max_connections", path)}: serving.http.'
                                 f'max_connections: below serving.max_concurrency + serving.queue_limit ({needed})')
    if values['total_deadline_s'] <= config['runtime']['request_deadline_s']:
        raise ConfigurationError(f'{_source(sources, "serving.total_deadline_s", path)}: serving.total_deadline_s: '
                                 'must exceed runtime.request_deadline_s, which it wraps')
    return values


EVALUATION_LEAVES = {  # protocol-bound: the `evaluation` leaves each evaluation command consumes (T-016 P4)
    'eval run': ('scenario_kinds', 'split_by', 'run.max_set_bytes', 'run.max_source_image_bytes', 'run.max_concurrency',
                 'run.max_sidecar_bytes', 'judge.blinding_seed', 'judge.score_min', 'judge.score_max',
                 'judge.rubric_prompt_id'),
    'eval import': ('judge.score_min', 'judge.score_max', 'judge.min_scorers', 'judge.under_test_families',
                    'judge.max_score_file_bytes', 'run.max_sidecar_bytes'),
}
SPLIT_RULES = ('concept',)  # protocol-bound: the one split rule the runner can check (a concept is in one split only)


def validate_evaluation_settings(config, command, path='configuration', sources=None, project_root=None):
    """The `evaluation` values `eval run` or `eval import` consume, after the loader's per-leaf checks, as one mapping
    keyed by the leaf name below `evaluation`. A missing, unresolved or malformed leaf is refused naming its key; no
    value is defaulted. Cross-field rule: the scale must have a lowest value below its highest. No rule ties
    `run.max_concurrency` to `runtime.trace.queue_limit`: a trace slot is freed only after its trace write (and monitor)
    finishes, so no setting guarantees capacity; a unit the pipeline refuses for capacity is recorded `not_run`
    (reason `trace_capacity`) and retried on the next invocation (fix round 1, P3)."""
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _configuration_schema(root, config)
    values = {}
    for leaf in EVALUATION_LEAVES[command]:
        key = f'evaluation.{leaf}'
        value = _get(config, key, path)
        where = f'{_source(sources, key, path)}: {key}'
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        if leaf in ('scenario_kinds', 'judge.under_test_families'):
            if (type(value) is not list or not value or len(set(value)) != len(value)
                    or any(type(item) is not str or not item for item in value)):
                raise ConfigurationError(f'{where}: expected a non-empty list of distinct non-empty strings')
        elif leaf == 'split_by':
            if value not in SPLIT_RULES:
                raise ConfigurationError(f'{where}: expected one of {list(SPLIT_RULES)}')
        elif leaf == 'judge.rubric_prompt_id':
            if type(value) is not str or not value:
                raise ConfigurationError(f'{where}: expected a prompt id')
        else:
            low = 0 if leaf == 'judge.blinding_seed' else None
            limit = 2 ** 63 - 1 if leaf == 'judge.blinding_seed' else sys.maxsize
            if type(value) is not int or value > limit or (low is not None and value < low):
                raise ConfigurationError(f'{where}: expected a native integer in range')
            if leaf not in ('judge.score_min', 'judge.score_max', 'judge.blinding_seed') and value < 1:
                raise ConfigurationError(f'{where}: expected a positive integer')
        values[leaf] = deepcopy(value)
    if 'judge.score_min' in values and values['judge.score_min'] >= values['judge.score_max']:
        raise ConfigurationError(f'{_source(sources, "evaluation.judge.score_min", path)}: evaluation.judge.score_min: '
                                 'must be below evaluation.judge.score_max')
    if 'judge.min_scorers' in values and values['judge.min_scorers'] < 2:
        raise ConfigurationError(f'{_source(sources, "evaluation.judge.min_scorers", path)}: '
                                 'evaluation.judge.min_scorers: two scorers of different families are required')
    return values


def _require_validation_version(config, path):
    """Refuse legacy requests before any version-dependent field or schema read (versions 2 and 3 carry the policy)."""
    if not isinstance(config, dict) or type(config.get('schema_version')) is not int or config['schema_version'] not in (2, 3):
        raise ConfigurationError(f'{path}: schema_version: migrate S5 configuration to version 2 with verifier calibration policy')


def _require_source_limits_version(config, path):
    """A request command reads the multi-source keys, so a version-2 file is refused with the keys to add (decision 179);
    no cap is assumed for it. Commands that read none of the keys keep loading version 2."""
    if not isinstance(config, dict) or config.get('schema_version') != SOURCE_LIMITS_VERSION:
        raise ConfigurationError(
            f'{path}: schema_version: migrate the configuration to version {SOURCE_LIMITS_VERSION} (enhancer_config.v2): add '
            'edit.max_source_images, vlm.backends.<name>.image.max_images_per_call, vlm.roles.planner.multi_source '
            '(prompt_id, schema_id) and vlm.roles.writer.multi_source (prompt_id)')


def check_limit(value, path, key, sources=None):
    """A source-image limit: a positive native integer (a bool, float, string, null or sentinel is refused, naming `key`)."""
    sources = {} if sources is None else sources
    _sentinel(value, path, key, sources)
    if type(value) is not int or not 1 <= value <= sys.maxsize:
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: expected a positive integer')
    return value


IMAGE_SYNTAX_FIELD = '{n}'  # protocol-bound: the one placeholder of a renderer image syntax (decision 179)


def syntax_error(value):
    """None when `value` is a usable image syntax, else the reason: `none`, or a string with exactly one `{n}` and no
    other brace (so `{N}`, `{ n }`, `{{n}}` and `{n}{n}` are refused). A structural count, never a word list (R-015)."""
    if not isinstance(value, str):
        return 'expected none or a string with exactly one {n}'
    if value == NONE:
        return None
    rest = value.replace(IMAGE_SYNTAX_FIELD, '')
    if value.count(IMAGE_SYNTAX_FIELD) != 1 or '{' in rest or '}' in rest:
        return 'expected none or a string with exactly one {n} and no other brace'
    return None


def render_syntax(value, count):
    """The strings an image syntax gives for the image numbers 1..count (none for `none`)."""
    if value == NONE:
        return []
    return [value.replace(IMAGE_SYNTAX_FIELD, str(number)) for number in range(1, count + 1)]


DIGITS = '0123456789'  # protocol-bound: str(n) of a positive int has only these characters and no leading zero


def _cells(prefix, suffix, digit_count):
    """The positions of a rendering with `digit_count` digits: ('l', character) literals and ('d', digit index)."""
    return ([('l', char) for char in prefix] + [('d', index) for index in range(digit_count)]
            + [('l', char) for char in suffix])


def _step(tight, digit, bound, leading):
    """One digit against the upper bound digit: None when it exceeds the bound, else whether it still equals the bound's
    prefix. A leading zero is refused (it never occurs in `str(n)`); the lower bound 10**(d-1) is exactly that rule."""
    if leading and digit == '0':
        return None
    if not tight:
        return False
    return None if digit > bound else digit == bound


def _can_occur(inner, outer, inner_digits, outer_digits, offset):
    """Whether some n (inner_digits digits, at most the inner count) and m (outer_digits digits, at most the outer count)
    make `inner` rendered with n occur in `outer` rendered with m, starting at `offset`.

    Each position of the inner rendering is aligned with one position of the outer rendering: two literals must be equal,
    a literal over a digit fixes that digit (and must be a decimal digit), two digits are tied. Every digit of n is then
    fixed or equal to a digit of m, so m alone decides; a small walk over m's digits (most significant first) tracks
    whether m and n stay within their upper bounds. The cost depends on the digit counts and template lengths only."""
    (inner_prefix, inner_suffix, inner_count), (outer_prefix, outer_suffix, outer_count) = inner, outer
    x = _cells(inner_prefix, inner_suffix, inner_digits)
    y = _cells(outer_prefix, outer_suffix, outer_digits)
    if offset + len(x) > len(y):
        return False
    fixed_m, fixed_n, tied = {}, {}, {}  # digit index -> character; n digit index -> m digit index
    for position, (kind, value) in enumerate(x):
        other_kind, other = y[offset + position]
        if kind == 'l' and other_kind == 'l':
            if value != other:
                return False
        elif kind == 'l':
            if value not in DIGITS or fixed_m.setdefault(other, value) != value:
                return False
        elif other_kind == 'l':
            if other not in DIGITS:
                return False
            fixed_n[value] = other
        else:
            tied[value] = other
    bound_n = str(inner_count) if len(str(inner_count)) == inner_digits else DIGITS[-1] * inner_digits
    bound_m = str(outer_count) if len(str(outer_count)) == outer_digits else DIGITS[-1] * outer_digits
    first_tied = min(tied, default=inner_digits)
    tight_n = True
    for index in sorted(i for i in fixed_n if i < first_tied):  # constants before the digits taken from m
        tight_n = _step(tight_n, fixed_n[index], bound_n[index], index == 0)
        if tight_n is None:
            return False
    from_m = {digit: index for index, digit in tied.items()}  # m digit index -> n digit index
    states = {(True, tight_n)}
    for j in range(outer_digits):
        following = set()
        for tight_m, tight_n in states:
            for digit in ((fixed_m[j],) if j in fixed_m else DIGITS):
                now_m = _step(tight_m, digit, bound_m[j], j == 0)
                if now_m is None:
                    continue
                now_n = tight_n
                if j in from_m:
                    now_n = _step(tight_n, digit, bound_n[from_m[j]], from_m[j] == 0)
                    if now_n is None:
                        continue
                following.add((now_m, now_n))
        states = following
    for _, tight_n in states:
        for index in sorted(i for i in fixed_n if i >= first_tied):  # constants after them
            tight_n = _step(tight_n, fixed_n[index], bound_n[index], index == 0)
            if tight_n is None:
                break
        else:
            return True
    return False


def _occurs_within(inner, outer, same_group):
    """Whether a rendering of `inner` (n in 1..count) equals or lies inside a rendering of `outer`; within one group only
    another image number counts (a rendering equals itself). Equal digit counts in one group force n = m, so only a
    shorter number can differ."""
    for inner_digits in range(1, len(str(inner[2])) + 1):
        for outer_digits in range(1, len(str(outer[2])) + 1):
            if same_group and inner_digits >= outer_digits:
                continue
            room = (len(outer[0]) + outer_digits + len(outer[1])) - (len(inner[0]) + inner_digits + len(inner[1]))
            if any(_can_occur(inner, outer, inner_digits, outer_digits, offset) for offset in range(room + 1)):
                return True
    return False


def overlapping_syntaxes(groups):
    """The first pair `(container, contained)` of names where a rendering of the contained group equals or lies inside a
    rendering of the container group, within a group (the name twice) or across groups; None when every pair is
    substring-free.

    `groups` maps a name to (syntax, count); a group's renderings are its syntax with `{n}` replaced by the decimal n for
    n in 1..count (`render_syntax`). A syntax is `none` or has exactly one `{n}` (`syntax_error`). The decision is made
    from the prefix, the suffix and the digit structure, never by rendering, so its cost is bounded by the template
    lengths and the number of digits of the counts and not by the counts (the property test checks it against the
    rendering oracle)."""
    live = {}
    for name, (syntax, count) in groups.items():
        if syntax != NONE and count >= 1:
            prefix, _, suffix = syntax.partition(IMAGE_SYNTAX_FIELD)
            live[name] = (prefix, suffix, count)
    for container, outer in live.items():
        for contained, inner in live.items():
            if _occurs_within(inner, outer, container == contained):
                return container, contained
    return None


def validate_renderer(renderer, path, *, reference_count=None):
    """Manifest v1: the loader's refusals for the renderer block, each naming its key (decisions 177 and 179).

    `reference_count` widens the reference group's range for the overlap check (the paired configuration's
    `edit.max_kb_reference_images`); the manifest alone checks both groups up to `max_source_images`."""
    prefix = 'renderer.'
    cap = renderer['max_source_images']
    if cap is None:
        raise ConfigurationError(f'{path}: {prefix}max_source_images: null; the drafter found no statement in the prompt, '
                                 'so the admin states the number of source images the image model accepts')
    check_limit(cap, path, prefix + 'max_source_images')
    for name in ('source_image_syntax', 'reference_image_syntax'):
        if renderer[name] is None:
            raise ConfigurationError(f'{path}: {prefix}{name}: null; the drafter found no statement in the prompt, so the '
                                     'admin states the syntax or none')
        reason = syntax_error(renderer[name])
        if reason:
            raise ConfigurationError(f'{path}: {prefix}{name}: {reason}')
    source, reference = renderer['source_image_syntax'], renderer['reference_image_syntax']
    if source == NONE and cap > 1:
        raise ConfigurationError(f'{path}: {prefix}source_image_syntax: none is allowed only when '
                                 f'{prefix}max_source_images is 1 (it is {cap}); state the syntax the prompt uses')
    if reference != NONE and renderer['accepts_reference_images'] is not True:
        raise ConfigurationError(f'{path}: {prefix}reference_image_syntax: a syntax is set but '
                                 f'{prefix}accepts_reference_images is false; set it to none or accept reference images')
    if source != NONE and source == reference:
        raise ConfigurationError(f'{path}: {prefix}source_image_syntax: equal to {prefix}reference_image_syntax; the two '
                                 'groups need different syntaxes')
    references = cap if reference_count is None else max(cap, reference_count)
    clash = overlapping_syntaxes({prefix + 'source_image_syntax': (source, cap),
                                  prefix + 'reference_image_syntax': (reference, references)})
    if clash:
        raise ConfigurationError(f'{path}: {clash[0]}: a rendering for an image number up to the counted range equals or '
                                 f'contains a rendering of {clash[1]} (counted: {prefix}max_source_images, and '
                                 'edit.max_kb_reference_images for references)')


def validate_validation_reads(config, path='configuration', sources=None, project_root=None, *,
                              require_verifier=False, require_calibration=True):
    """S5 policy first; consume calibrated model bindings only on an eligible verification path."""
    _require_validation_version(config, path)
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _configuration_schema(root, config)
    _validate(_get(config, 'validation', path), _schema_at(schema, 'validation'), path, 'validation', sources,
              _EXACT_INTEGERS)
    for key in VALIDATION_READS:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
    validation = config['validation']
    if validation['structural_checks'] is not True:
        raise ConfigurationError(f'{path}: validation.structural_checks: expected true')
    if type(validation['max_repairs']) is not int or validation['max_repairs'] < 0:
        raise ConfigurationError(f'{path}: validation.max_repairs: expected a nonnegative integer')
    fraction = validation['verifier']['sampled_fraction_for_rest']
    if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
        raise ConfigurationError(f'{path}: validation.verifier.sampled_fraction_for_rest: expected fraction in [0,1]')
    for key, tags in (('validation.verifier.gating_rules', VERIFIER_GATES),
                      ('validation.verifier.inline_for', VERIFIER_INLINE), ('validation.fallbacks', VALIDATION_FALLBACKS)):
        value = _get(config, key, path)
        if len(value) != len(set(value)) or not set(value) <= tags:
            raise ConfigurationError(f'{path}: {key}: expected unique recognized policy names')
    if not require_verifier:
        return validation
    reads = ['vlm.roles.verifier', 'vlm.default_backend']
    if require_calibration:
        reads.append('validation.verifier.calibration_ref')
    role = _get(config, 'vlm.roles.verifier', path)
    _validate(role, _schema(root, 'vlm_role.v0'), path, 'vlm.roles.verifier', sources, _EXACT_INTEGERS)
    _sentinel(role, path, 'vlm.roles.verifier', sources)
    default = _get(config, 'vlm.default_backend', path)
    _sentinel(default, path, 'vlm.default_backend', sources)
    _validate(default, _schema_at(schema, 'vlm.default_backend'), path, 'vlm.default_backend', sources)
    reads += [f'vlm.backends.{name}' for name in sorted({role['backend'], default})]
    for key in reads:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        if key.startswith('vlm.backends.'):
            _configured_path(value['api_key_file'], root, path, key + '.api_key_file', secret=True)
            if set(value.get('extra_body', {})) & {'messages', 'system', 'systemInstruction', 'contents', 'model'}:
                raise ConfigurationError(f'{path}: {key}.extra_body: cannot replace verifier identity')
    if require_calibration:
        _configured_path(validation['verifier']['calibration_ref'], root, path, 'validation.verifier.calibration_ref')
    if role['mode'] != 'generate' or role['image'] != 'none':
        raise ConfigurationError(f'{path}: vlm.roles.verifier: expected a text-only generate role')
    for key, expected in (('format', 'json_schema'), ('schema_id', 'verifier_result.v0'), ('strict', True),
                          ('transport', 'response_format'), ('validate_response', True)):
        if role['output'][key] != expected:
            raise ConfigurationError(f'{path}: vlm.roles.verifier.output.{key}: expected {expected}')
    load_id = role['prompt_id']
    _filename(load_id, path, 'vlm.roles.verifier.prompt_id')
    _validate_model_bindings(config, path, root)
    return validation


ASSEMBLY_READS = ('rag.evidence_mode', 'assembly.scope_matrix', 'assembly.default_outfit',
                  'assembly.max_facts_per_concept', 'assembly.max_facts_per_request', 'assembly.fact_order',
                  'resolution.on_variant_unspecified', 'resolution.random_seed', 'resolution.trace_choice',
                  'edit.attach_kb_reference_images')
# Read only when knowledge images may be attached (T-014 P2, PM answer 1).
ASSEMBLY_ATTACH_READS = ('assembly.max_images_per_concept', 'edit.max_kb_reference_images')
ASSEMBLY_EVIDENCE_MODE = 'cards'  # protocol-bound: the only evidence mode S3 implements (T-014 PM answer 3)


def validate_assembly_reads(config, path='configuration', sources=None, project_root=None):
    """S3's request-startup and direct-entry gate (T-014 P2); S3 runs only with retrieval on."""
    sources = {} if sources is None else sources
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    schema = _reads_schema(root, config)

    def refuse(key, message):
        raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {message}')

    attach = _get(config, 'edit.attach_kb_reference_images', path)
    reads = ASSEMBLY_READS + (ASSEMBLY_ATTACH_READS if attach is True else ())
    for key in reads:
        value = _get(config, key, path)
        _sentinel(value, path, key, sources)
        _validate(value, _schema_at(schema, key), path, key, sources, _EXACT_INTEGERS)
        _require_finite(value, path, key, sources)
        if key.startswith(('assembly.max_', 'edit.max_')) and value > sys.maxsize:
            refuse(key, 'expected a native integer')
    mode = config['rag']['evidence_mode']
    if mode != ASSEMBLY_EVIDENCE_MODE:
        refuse('rag.evidence_mode', f'{mode} is not implemented yet: live reading of knowledge-base images and the '
               f'on-demand observer question are deferred (T-014); use {ASSEMBLY_EVIDENCE_MODE}')
    return config['assembly']


def _validate_request_base(config, schema):
    """Administrator diagnostics name schema-owned fields, without echoing values or arbitrary map keys."""
    try:
        _validate(config, schema, 'configuration')
    except ConfigurationError:
        def leaves(error):
            return [leaf for child in error.context for leaf in leaves(child)] if error.context else [error]
        errors = [leaf for error in Draft202012Validator(schema).iter_errors(config) for leaf in leaves(error)]
        error = max(errors, key=lambda item: len(item.absolute_path))
        path = list(error.absolute_schema_path)
        fields = [path[i + 1] for i, part in enumerate(path[:-1]) if part == 'properties']
        if error.validator == 'required' and isinstance(error.instance, dict):
            fields += [key for key in error.validator_value if key not in error.instance][:1]
        key = '.'.join(fields) or 'schema_version'
        raise ConfigurationError(f'configuration: {key}: invalid base configuration') from None
    try:
        return _validate_override_policy(config, schema, 'configuration')
    except ConfigurationError:
        raise ConfigurationError('configuration: request_overrides.allowed_keys: invalid override policy') from None


def effective_request_config(config, overrides, *, project_root):
    """Apply only administrator-permitted, strictly typed overrides, then run request preflight."""
    root = Path(project_root).resolve()
    try:
        schema = _configuration_schema(root, config)
    except ConfigurationError:
        raise ConfigurationError('configuration: invalid base configuration') from None
    allowed = _validate_request_base(config, schema)
    try:
        if not isinstance(overrides, dict):
            raise ConfigurationError('request.overrides: expected an object')
        effective = deepcopy(config)
        for key, value in overrides.items():
            if not isinstance(key, str) or key not in allowed:
                raise ConfigurationError('request.overrides: override not allowed')
            node = _schema_at(schema, key)
            _validate(value, node, 'request.overrides', key, validator=_EXACT_INTEGERS)
            _sentinel(value, 'request.overrides', key, {})
            _require_finite(value, 'request.overrides', key, {})
            parent = effective
            parts = key.split('.')
            for part in parts[:-1]:
                parent = parent[part]
            parent[parts[-1]] = deepcopy(value)
        # The base was validated at load; a prompt file is read again only for an override of a multi_source key.
        named = any('.multi_source' in key for key in overrides)
        return _validate_loaded_config(effective, 'enhance once', root, 'effective configuration', {}, prompts=named)
    except ConfigurationError as error:
        # The request boundary must not expose validator values, permission keys or filesystem paths.
        raise ConfigurationError(f'request configuration validation failed ({type(error).__name__})') from None


def _validate_loaded_config(config, command, root, path, sources, *, prompts=True):
    if command in REQUEST_COMMANDS:
        _require_source_limits_version(config, path)  # first: one migrate message (target 3) for every version below 3
        _require_validation_version(config, path)
        _require_attachment_support(config, path, sources)
    _require_tool_forced_version(config, path)
    try:
        config_schema = _configuration_schema(root, config)
    except ConfigurationError as error:
        raise ConfigurationError(f'{path}: schema_version: {error}') from error
    _index_migration(config, path)
    _validate(config, config_schema, path, sources=sources)
    _validate_override_policy(config, config_schema, path)
    if command == 'kb sync':
        gate = _get(config, 'rag.enabled', path)
        if type(gate) is not bool:
            raise ConfigurationError(f'{path}: rag.enabled: expected boolean')
        if not gate:
            return config
    _validate_model_bindings(config, path, root)
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
    knowledge_access = (command not in REQUEST_COMMANDS or not isinstance(config['rag'], dict)
                        or config['rag']['enabled'] is not False)
    _validate_configured_paths(config, root, path, sources, knowledge_access=knowledge_access)
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
        if command in ('kb sync', 'embed dev-compare') or command in REQUEST_COMMANDS:
            _validate(value, _schema_at(config_schema, key), path, key, sources, _EXACT_INTEGERS)
            _require_finite(value, path, key, sources)
        if key.startswith('vlm.roles.'):
            try:
                role_schema = _schema(root, _planner_role_schema_id(config) if key == 'vlm.roles.planner'
                                      else 'vlm_role.v0')
            except ConfigurationError as error:
                raise ConfigurationError(f'{_source(sources, key, path)}: {key}: {error}') from error
            _validate(_role_core(value), role_schema, path, key, sources)
            backend_key = 'vlm.backends.' + value['backend']
            _sentinel(_get(config, backend_key, path), path, backend_key, sources)
    if any(key.startswith('vlm.roles.') for key in reads):
        default = config['vlm']['default_backend']
        _sentinel(default, path, 'vlm.default_backend', sources)
        default_key = 'vlm.backends.' + default
        _sentinel(_get(config, default_key, path), path, default_key, sources)
    if command == 'embed serve':
        validate_embed_server_reads(config, path, sources, root)
    if command == 'embed reference-check':
        validate_reference_check_reads(config, path, sources, root)
    if command in REQUEST_COMMANDS:
        _require_float_seconds(config['runtime']['intake_deadline_s'], path, 'runtime.intake_deadline_s', sources)
        validate_planner_reads(config, path, sources, root)
        validate_writer_reads(config, path, sources, root)
        validate_source_limits(config, path, sources, root, prompts=prompts)
        validate_validation_reads(config, path, sources, root)
        validate_pipeline_reads(config, path, sources, root)
        if command == 'enhance serve':
            validate_serving(config, path, sources, root)
        if command == 'eval run':
            validate_evaluation_settings(config, command, path, sources, root)
        if config['rag']['enabled']:
            validate_index_reads(config, path, sources, root)
            validate_candidates_reads(config, path, sources, root)
            validate_assembly_reads(config, path, sources, root)
    if command == 'eval import':
        validate_evaluation_settings(config, command, path, sources, root)
    if command == 'kb sync':
        _validate_store_reads(config, command, root, path, sources)
        for key in ('sync.retained_images.max_image_bytes', 'sync.retained_images.max_total_bytes'):
            value = _get(config, key, path)
            _sentinel(value, path, key, sources)
            _validate(value, _schema_at(config_schema, key), path, key, sources, _EXACT_INTEGERS)
            if type(value) is not int or value <= 0 or value > sys.maxsize:
                raise ConfigurationError(f'{path}: {key}: expected positive native integer')
        validate_index_reads(config, path, sources, root)
        validate_embedder_reads(config, path, sources, root)  # K1 is part of kb sync; the same check K1 runs
        validate_naming_reads(config, path, sources, root)
        validate_observation_reads(config, path, sources, root)
        validate_card_reads(config, path, sources, root)
        validate_alias_reads(config, path, sources, root)
        _validate_k0_reads(config, root, path, sources)
    if command == 'embed dev-compare':  # DEV-ONLY (T-018 P3): K0's and K1's own checks, nothing of K2 to K5
        _validate_store_reads(config, command, root, path, sources)
        validate_embedder_reads(config, path, sources, root)
        _validate_k0_reads(config, root, path, sources)
    return config


def _validate_store_reads(config, command, root, path, sources):
    """Store lock timers and store policy of a command that opens the knowledge store, without constructing it."""
    for key in ('storage.lock.timeout_s', 'storage.lock.poll_s'):
        _require_float_seconds(_get(config, key, path), path, key, sources)
    from .storage.store import _settings
    from .storage import StorageError
    try:
        _settings(config, command, root)
    except StorageError as error:
        raise ConfigurationError(f'{path}: {error}') from None


def _validate_k0_reads(config, root, path, sources):
    """K0's scan settings (`kb sync`, `embed dev-compare`): the knowledge root through `resolve_knowledge_root` (it may lie
    outside the project; apart from the protected directories and both stores), the info-file codec, the worker
    count, the extensions."""
    resolve_knowledge_root(config, root, path, sources=sources)
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


def manifest_schema_id(version):
    """The schema of a manifest `schema_version`: 1 is profile_manifest.v0 (kept as it was), 2 is profile_manifest.v1."""
    return 'profile_manifest.v' + str(MANIFEST_VERSIONS.index(version))


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
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] not in MANIFEST_VERSIONS:
        raise ConfigurationError(f'{manifest_path}: schema_version: expected one of {list(MANIFEST_VERSIONS)}')
    try:
        manifest_schema = _schema(root, manifest_schema_id(manifest['schema_version']))
    except ConfigurationError as error:
        raise ConfigurationError(f'{manifest_path}: schema_version: {error}') from error
    _validate(manifest, manifest_schema, manifest_path)
    if manifest['schema_version'] == 2:
        validate_renderer(manifest['renderer'], manifest_path)
    _filename(manifest['profile_id'], manifest_path, 'profile_id')
    try:
        prompt_path = _inside(path / 'system_prompt.txt', root)
        prompt = prompt_path.read_bytes()
    except (OSError, ConfigurationError) as error:
        raise ConfigurationError(f'{manifest_path}: system_prompt.txt: {error}') from error
    return manifest, prompt


def resolve_writer_format(role, manifest, *, project_root=None, role_source='config.yaml', manifest_source='manifest.yaml',
                          profile_schema_id=None):
    """Resolve writer references, attributing refusals to the caller's source files.

    `profile_schema_id` is the `$id` of the admin bundle's `output_schema.json` that the profile loader checked
    (T017-FIX3, `profiles.output_schema`); the manifest may name it instead of a schema under `contracts/schemas`.
    The caller that passes it also supplies that schema to whoever sends and validates the reply."""
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    result = deepcopy(role)
    output = result.get('output', {})
    if output.get('format') != 'from_profile_manifest' or output.get('schema_id') != 'from_profile_manifest':
        raise ConfigurationError(f'{role_source}: writer.output: expected paired from_profile_manifest references')
    schema_id = manifest.get('output_schema_id')
    if not isinstance(schema_id, str) or not schema_id:
        raise ConfigurationError(f'{manifest_source}: output_schema_id: missing or invalid')
    if schema_id != 'none' and schema_id != profile_schema_id:
        try:
            _schema(root, schema_id)
        except ConfigurationError as error:
            raise ConfigurationError(f'{manifest_source}: output_schema_id: {error}') from error
    output['schema_id'] = schema_id
    output['format'] = 'none' if schema_id == 'none' else 'json_schema'
    return result
