"""Strict input and output helpers of the command line (T-016): a request file in, UTF-8 JSON out.

Nothing here reads meaning from text. A request is decoded as UTF-8, parsed as JSON that refuses duplicate keys,
non-finite numbers and excessive nesting, checked against `request.v0`, and handed on exactly as written (R-001, R-004).
Refusal messages name the argument, the field path and the schema keyword, never a value, so a private prompt or image
does not reach a log (R-026).
"""
import base64
import binascii
from datetime import datetime
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

from .config import REQUEST_LIMIT_KEY, ConfigurationError, LinkedPath, _inside, _lexical_path, _schema
from .k0_scan import LinkRefused, ScanError, SourceTooLarge, read_source_file
from .model_json import strict_json


class Refusal(Exception):
    """An argument, configuration or input refusal: exit status 2, nothing started."""


def _error_path(error):
    parts = ['request', *(str(part) for part in error.absolute_path)]
    return '.'.join(parts)


def resolve_input(root, value, label):
    """A file named on the command line, under the project root, spelled as it is stored.

    The rule of the stores (`storage/_fs.real_inside`): a link or junction on the way is refused, also when it stays
    inside the root, because there it would let one name reach another name's file. `root` is already resolved."""
    try:
        spelled = _lexical_path(str(value), root, value, 'path')
        resolved = _inside(value, root)
    except LinkedPath:
        raise Refusal(f'{label}: is a link or another spelling of a stored name') from None
    except ConfigurationError as error:
        raise Refusal(f'{label}: {error}') from None
    if str(resolved) != str(spelled):
        raise Refusal(f'{label}: is a link or another spelling of a stored name')
    return resolved


def refuse_linked_configuration(args, root):
    """`--config` and `--overlay` of the commands added by T-016 follow the same no-link rule as `--request`."""
    resolve_input(root, args.config, '--config')
    if getattr(args, 'overlay', None) is not None:
        resolve_input(root, args.overlay, '--overlay')


def read_bounded(root, path, limit, label):
    """Read one regular file of at most `limit` bytes, never following a link; larger or special files are refused.

    The file is opened component by component without following links (`k0_scan.read_source_file`, the source reader
    of the knowledge base) and read in fixed chunks, so no allocation is sized by `limit` and no gap remains between
    checking the path and opening it. Every failure is a `Refusal` that names the argument only."""
    try:
        return read_source_file(root, Path(path).relative_to(root).parts, max_bytes=limit)
    except SourceTooLarge:
        raise Refusal(f'{label}: larger than {REQUEST_LIMIT_KEY} ({limit} bytes)') from None
    except LinkRefused:
        raise Refusal(f'{label}: is a link or another spelling of a stored name') from None
    except ScanError:
        raise Refusal(f'{label}: not a regular file, or it changed while it was read') from None
    except (OSError, ValueError) as error:
        raise Refusal(f'{label}: cannot read the file ({type(error).__name__})') from None



def _lone_surrogate(value):
    """True when a string anywhere in the document cannot be written back as UTF-8."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            stack.extend(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def parse_request(data, root):
    """Bytes of a request file -> the request mapping, unchanged, or a Refusal that holds no request value."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise Refusal(f'request: not UTF-8 at byte {error.start}') from None
    try:
        request = strict_json(text)
    except json.JSONDecodeError as error:
        raise Refusal(f'request: not valid JSON (line {error.lineno}, column {error.colno})') from None
    except ValueError as error:
        raise Refusal(f'request: {error}') from None
    if _lone_surrogate(request):
        raise Refusal('request: a string holds a lone surrogate, which UTF-8 cannot carry')
    if not isinstance(request, dict):
        raise Refusal('request: expected a JSON object')
    try:
        errors = sorted(Draft202012Validator(_schema(root, 'request.v0')).iter_errors(request),
                        key=lambda item: ([str(part) for part in item.absolute_path], item.validator))
    except ConfigurationError as error:
        raise Refusal(f'request: {error}') from None
    if errors:
        error = errors[0]
        what = 'unknown field' if error.validator == 'additionalProperties' else f'violates {error.validator}'
        raise Refusal(f'{_error_path(error)}: {what}')
    source = request.get('source_image')
    if request['task'] == 'edit' and source is None:
        raise Refusal('request.source_image: required by task edit')
    if request['task'] == 'generate' and source is not None:
        raise Refusal('request.source_image: not accepted by task generate')
    if source is not None:
        try:
            base64.b64decode(source, validate=True)
        except (ValueError, binascii.Error):
            raise Refusal('request.source_image: not valid base64') from None
    return request


def read_request(root, value, limit):
    """`--request <file>`: the file under the project root, bounded, then parsed strictly."""
    return parse_request(read_bounded(root, resolve_input(root, value, '--request'), limit, '--request'), root)


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'cannot encode {type(value).__name__}')


def emit_json(result):
    """One JSON line on stdout as UTF-8 bytes whatever the console code page is; raises OSError when it cannot be
    written, so the caller never counts an unwritten result as delivered."""
    data = (json.dumps(result, ensure_ascii=False, allow_nan=False, default=json_default) + '\n').encode('utf-8')
    stream = getattr(sys.stdout, 'buffer', None)
    if stream is None:
        sys.stdout.write(data.decode('utf-8'))
        sys.stdout.flush()
    else:
        stream.write(data)
        stream.flush()
