"""The HTTP/1.1 wire format of `enhance serve` (T-016 P3), standard library only.

One request per connection (`Connection: close`). This module holds the parts that need no event loop: the request head
parser, the router, the body length rule and the response builder. Every function is total over arbitrary bytes and
strings: it returns a result or raises `WireError`, whose message is fixed text and never a value from the request
(R-026). The limits named here guard the parser itself and are safety-bound; every deployment-dependent bound
(connections, body bytes, timeouts) is a validated `serving` value that the server passes in.
"""
import asyncio
from http import HTTPStatus
import json
from typing import NamedTuple
from urllib.parse import unquote

MAX_HEAD_BYTES = 16384  # safety-bound: request line and headers together (the stream reader's limit)
MAX_HEADERS = 64  # safety-bound: header lines per request
_TOKEN = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")  # RFC 9110 tchar
_SINGLE = frozenset(('content-length', 'content-type', 'transfer-encoding', 'expect'))  # a repeat is ambiguous: refused
_VERSIONS = frozenset(('HTTP/1.1', 'HTTP/1.0'))
_MAX_LENGTH_DIGITS = 19  # protocol-bound: digits of sys.maxsize; a longer Content-Length exceeds every possible cap

# protocol-bound: the routes and their methods (docs/execution.md)
HEALTH, ENHANCE, ATTACHMENT = 'health', 'enhance', 'attachment'
_METHODS = {HEALTH: 'GET', ENHANCE: 'POST', ATTACHMENT: 'GET'}
# protocol-bound: an envelope is delivered with 200 whatever its own status says; the status field is the outcome
# (R-024, R-027). A status outside the schema's enumeration is a defect and never reaches the client as a 200.
ENVELOPE_STATUSES = frozenset(('success', 'repaired', 'degraded', 'needs_clarification', 'failed'))


class WireError(Exception):
    """A refusal the client receives: an HTTP status, a stable code and a fixed message."""

    def __init__(self, status, code, message, allow=None):
        super().__init__(message)
        self.status, self.code, self.message, self.allow = status, code, message, allow


class Head(NamedTuple):
    method: str
    target: str
    headers: dict


class Route(NamedTuple):
    name: str
    params: tuple


def _bad(message='malformed request'):
    return WireError(400, 'bad_request', message)


def parse_head(raw):
    """The bytes of a request head (up to and including the blank line) -> `Head`, or a `WireError`."""
    if len(raw) > MAX_HEAD_BYTES:
        raise WireError(431, 'head_too_large', 'request head too large')
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError:
        raise _bad('request head is not ASCII') from None
    if not text.endswith('\r\n\r\n'):
        raise _bad()
    lines = text[:-4].split('\r\n')
    if any(ord(character) < 32 and character != '\t' or ord(character) == 127 for line in lines for character in line):
        raise _bad('control character in the request head')
    parts = lines[0].split(' ')
    if len(parts) != 3 or not parts[0] or not set(parts[0]) <= _TOKEN or not parts[1] or parts[2] not in _VERSIONS:
        raise _bad('malformed request line')
    if len(lines) - 1 > MAX_HEADERS:
        raise WireError(431, 'head_too_large', 'too many headers')
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(':')
        if not separator or not name or not set(name) <= _TOKEN:  # a folded line starts with a space: no token
            raise _bad('malformed header')
        name = name.lower()
        if name in _SINGLE and name in headers:
            raise _bad('repeated header')
        headers[name] = value.strip(' \t')
    return Head(parts[0], parts[1], headers)


def route(method, target):
    """Method and request target -> `Route`; the target is exact (no query, no fragment, no dot segments).

    An attachment path names a trace id and an image id and nothing else: whether they exist is decided from the
    response's published index, never from a path built here."""
    if not isinstance(target, str) or not target.startswith('/') or '?' in target or '#' in target:
        raise WireError(404, 'not_found', 'no such route')
    parts = target.split('/')
    if target == '/health':
        found = Route(HEALTH, ())
    elif target == '/v1/enhance':
        found = Route(ENHANCE, ())
    elif len(parts) == 5 and parts[1:3] == ['v1', 'attachments']:
        try:
            ids = tuple(unquote(part, errors='strict') for part in parts[3:])
        except UnicodeDecodeError:
            raise WireError(404, 'not_found', 'no such route') from None
        if any('/' in item or '\\' in item or not item for item in ids):
            raise WireError(404, 'not_found', 'no such route')
        found = Route(ATTACHMENT, ids)
    else:
        raise WireError(404, 'not_found', 'no such route')
    if method != _METHODS[found.name]:
        raise WireError(405, 'method_not_allowed', 'method not allowed', allow=_METHODS[found.name])
    return found


def require_json(headers):
    """`Content-Type: application/json`, with a charset only when it is UTF-8."""
    value = headers.get('content-type')
    if value is None:
        raise WireError(415, 'unsupported_media_type', 'expected application/json')
    media, *parameters = value.split(';')
    if media.strip().lower() != 'application/json':
        raise WireError(415, 'unsupported_media_type', 'expected application/json')
    for parameter in parameters:
        name, _, setting = parameter.partition('=')
        if name.strip().lower() == 'charset' and setting.strip().strip('"').lower() != 'utf-8':
            raise WireError(415, 'unsupported_media_type', 'expected UTF-8')


def body_length(headers, limit, *, required):
    """The declared body size, checked against `limit` before a byte is read; None when no body is declared.

    A chunked body is refused: a size the server must trust cannot be known before it reads."""
    if 'transfer-encoding' in headers:
        raise WireError(501, 'not_implemented', 'transfer encodings are not supported')
    value = headers.get('content-length')
    if value is None:
        if required:
            raise WireError(411, 'length_required', 'Content-Length is required')
        return None
    if not (value.isascii() and value.isdigit()):
        raise _bad('malformed Content-Length')
    digits = value.lstrip('0') or '0'
    if len(digits) > _MAX_LENGTH_DIGITS or int(digits) > limit:
        raise WireError(413, 'payload_too_large', 'request body too large')
    return int(digits)


async def read_head(reader):
    """Read one request head; raises `WireError`, or `ConnectionError` when the client closed before it was complete."""
    try:
        return parse_head(await reader.readuntil(b'\r\n\r\n'))
    except asyncio.LimitOverrunError:
        raise WireError(431, 'head_too_large', 'request head too large') from None
    except asyncio.IncompleteReadError:
        raise ConnectionError('closed before the request head was complete') from None


def http_status_of(envelope):
    """The HTTP status that carries an S6 envelope: 200 for each status of the schema's enumeration."""
    status = envelope.get('status') if isinstance(envelope, dict) else None
    if status not in ENVELOPE_STATUSES:
        raise ValueError('envelope has no known status')
    return 200


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def build_response(status, body, content_type='application/json; charset=utf-8', *, allow=None):
    head = [f'HTTP/1.1 {status} {HTTPStatus(status).phrase}', f'Content-Type: {content_type}',
            f'Content-Length: {len(body)}', 'Connection: close', 'Cache-Control: no-store']
    if allow is not None:
        head.append(f'Allow: {allow}')
    return '\r\n'.join(head).encode('ascii') + b'\r\n\r\n' + body


def error_response(error):
    """The transport error body, distinct from the S6 envelope: it never carries a trace id of its own."""
    return build_response(error.status, json_bytes({'error': {'code': error.code, 'message': error.message}}),
                          allow=error.allow)
