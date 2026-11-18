"""Admin-confirmed structural checks; no interpretation of prompt prose (R-015)."""

from copy import deepcopy
import json
import math
from pathlib import Path

from jsonschema import ValidationError

from .config import ConfigurationError, _EXACT_INTEGERS, _schema, _schema_path
from .model_json import check_json_depth, strict_json
from .profile_prompt import ProfileError, sha256_tag
from .storage._fs import StorageError, check_segment, real_inside


SCHEMA = 'profile_validator.v0'  # artifact-bound validator metadata contract
# Protocol-bound parameter-free checks already named by profile_manifest.v0 bundles.
BUILTINS = {'single_line': 'single_line', 'single_line_minified_json': 'minified_json'}
VALIDATOR_DIR = 'validators'  # artifact-bound profile bundle layout


def _pointer_parts(pointer):
    if pointer == '':
        return []
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('expected a JSON Pointer')
    parts = []
    for part in pointer[1:].split('/'):
        index = 0
        while index < len(part):
            if part[index] == '~':
                if index + 1 == len(part) or part[index + 1] not in ('0', '1'):
                    raise ValueError('invalid JSON Pointer escape')
                index += 1
            index += 1
        parts.append(part.replace('~1', '/').replace('~0', '~'))
    return parts


def validate_artifact(value, *, project_root):
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        _EXACT_INTEGERS(_schema(root, SCHEMA)).validate(value)
        check_segment(value['id'])
        params = value['parameters']
        if 'pointer' in params:
            _pointer_parts(params['pointer'])
        if value['operator'] in ('string_length', 'number_range'):
            if params['minimum'] > params['maximum']:
                raise ValueError('minimum exceeds maximum')
            if any(isinstance(n, float) and not math.isfinite(n) for n in (params['minimum'], params['maximum'])):
                raise ValueError('nonfinite bound')
        if value['operator'] == 'schema':
            _schema(root, params['schema_id'])
    except (ValidationError, StorageError, ConfigurationError, ValueError, TypeError) as error:
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'invalid operator, parameter or selector') from error
    return deepcopy(value)


def load_validators(bundle, names, *, project_root):
    """Read an ordered artifact snapshot; builtins have no file or inferred parameters."""
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'checks must be names')
    if len(set(names)) != len(names):
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'duplicate check name')
    root, bundle = Path(project_root).resolve(), Path(bundle)
    validators, hashes = [], {}
    for name in names:
        try:
            check_segment(name)
            relative = (bundle / VALIDATOR_DIR / (name + '.json')).relative_to(root).as_posix()
            path = real_inside(root, relative)
            if path.exists():
                raw = path.read_bytes()
                value = strict_json(raw)
            elif name in BUILTINS:
                value = {'id': name, 'operator': BUILTINS[name], 'parameters': {}}
                raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
            else:
                raise ValueError('missing validator artifact')
            value = validate_artifact(value, project_root=root)
            if value['id'] != name:
                raise ValueError('validator id differs from name')
        except (ValueError, OSError, StorageError) as error:
            raise ProfileError('PROFILE_VALIDATOR_INVALID', f'unresolved check {name!r}') from error
        validators.append(value)
        hashes[name] = sha256_tag(raw)
        if value['operator'] == 'schema':
            schema_id = value['parameters']['schema_id']
            hashes['schema:' + schema_id] = sha256_tag(_schema_path(root, schema_id).read_bytes())
    return tuple(validators), hashes


def _select(value, pointer):
    for part in _pointer_parts(pointer):
        if isinstance(value, list):
            if not part.isascii() or not part.isdecimal() or (len(part) > 1 and part.startswith('0')):
                raise ValueError('invalid array index')
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError('selector traverses a scalar')
    return value


def _has_outer_space(text):
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in ' \t\r\n':  # protocol-bound JSON whitespace
            return True
    return False


def output_format(manifest, validators):
    """The client decodes schema outputs; explicit JSON checks declare schema-free JSON text."""
    if manifest['output_schema_id'] != 'none':
        return 'json'
    return 'json_text' if any(item['operator'] in ('json', 'minified_json') for item in validators) else 'none'


def decode_output(output, format):
    if format not in ('none', 'json', 'json_text'):
        raise ValueError('unknown validator output format')
    if format == 'json_text' and isinstance(output, str):
        return strict_json(output)
    check_json_depth(output)
    return output


def check_output(output, validators, *, project_root, output_format='none'):
    """Return failed check IDs; absent selectors and wrong types are failures, never skipped."""
    failures = []
    try:
        decoded = decode_output(output, output_format)
    except ValueError:
        return [item['id'] for item in validators]
    for artifact in validators:
        artifact = validate_artifact(artifact, project_root=project_root)
        operator, params = artifact['operator'], artifact['parameters']
        try:
            value = output
            if operator in ('json', 'minified_json'):
                if output_format == 'json_text':
                    pass  # Already parsed once above, independently of selector/operator order.
                elif output_format == 'json':
                    strict_json(json.dumps(value, allow_nan=False))
                elif isinstance(value, str):
                    strict_json(value)
                else:
                    strict_json(json.dumps(value, allow_nan=False))
                passed = operator == 'json' or (output_format != 'json' and isinstance(value, str) and not _has_outer_space(value))
            elif operator == 'single_line':
                passed = isinstance(value, str) and (value == '' or value.splitlines() == [value])
            else:
                value = _select(decoded, params.get('pointer', ''))
                if operator == 'keys':
                    passed = isinstance(value, dict) and (list(value) == params['keys'] if params['ordered']
                                                         else set(value) == set(params['keys']))
                elif operator == 'string_length':
                    passed = isinstance(value, str) and params['minimum'] <= len(value) <= params['maximum']
                elif operator == 'number_range':
                    passed = type(value) in (int, float) and (not isinstance(value, float) or math.isfinite(value)) \
                        and params['minimum'] <= value <= params['maximum']
                elif operator == 'exact_text':
                    passed = isinstance(value, str) and value == params['value']
                else:  # schema operator: its local identity was checked above
                    from .model_schemas import cached_registry
                    cached_registry(Path(project_root) / 'contracts' / 'schemas').validate(params['schema_id'], value)
                    passed = True
            if not passed:
                failures.append(artifact['id'])
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            failures.append(artifact['id'])
    return failures
