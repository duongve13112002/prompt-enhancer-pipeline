"""Read an admin system prompt as bytes and split it structurally (P, decision 33).

The file is never edited or normalised: sections are exact byte slices, so LF, CRLF and every
non-ASCII byte survive unchanged (R-004).
"""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path

from .config import ConfigurationError, _MetaLoader, _inside, _load_yaml


# Protocol-bound section headers of the sectioned format (decision 33, docs/stages/P-profiles.md).
SECTION_NAMES = ('META', 'SYSTEM', 'USER')
_HEADERS = {f'[{name}]'.encode('ascii'): name for name in SECTION_NAMES}
_BOM = b'\xef\xbb\xbf'  # protocol-bound UTF-8 byte order mark; an encoding marker, not content
# Artifact-bound placeholder delimiters of the admin's [USER] template (decision 81: `{{aspect_ratio}}`).
_OPEN, _CLOSE = b'{{', b'}}'
_PROMPT_ID_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789_.')


class ProfileError(ValueError):
    """A profile refusal; the message starts with a stable code such as `PROFILE_NOT_ACTIVE`."""

    def __init__(self, code, detail, **context):
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.context = context


def sha256_tag(data):
    """Content hash in the `sha256:<hex>` form stored in manifests and activation records."""
    return 'sha256:' + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PromptStructure:
    """Structural facts of a prompt file; `spans` maps a section name to its (start, end) body slice."""

    template_format: str
    spans: dict
    header_spans: dict

    def body(self, data, name):
        start, end = self.spans[name]
        return data[start:end]


def _lines(data):
    """Yield (start, content_end, next_start) per line; content excludes the LF or CRLF terminator."""
    start = 0
    while start < len(data):
        newline = data.find(b'\n', start)
        end = len(data) if newline < 0 else newline
        content_end = end - 1 if end > start and data[end - 1:end] == b'\r' else end
        yield start, content_end, (len(data) if newline < 0 else newline + 1)
        start = len(data) if newline < 0 else newline + 1


def parse_structure(data):
    """Find the sections of a prompt; a file without any header line is `plain`."""
    if not isinstance(data, bytes):
        raise TypeError('prompt must be bytes')
    headers = []
    for start, content_end, next_start in _lines(data):
        line = data[start:content_end]
        if start == 0 and line.startswith(_BOM):
            line, start = line[len(_BOM):], len(_BOM)
        if line in _HEADERS:
            headers.append((_HEADERS[line], start, next_start))
    if not headers:
        return PromptStructure('plain', {}, {})
    first = headers[0][1]
    if data[:first] not in (b'', _BOM):
        raise ProfileError('PROFILE_PROMPT_INVALID', 'content before the first section header')
    spans, header_spans = {}, {}
    for index, (name, start, body_start) in enumerate(headers):
        if name in spans:
            raise ProfileError('PROFILE_PROMPT_INVALID', f'section [{name}] appears more than once')
        body_end = headers[index + 1][1] if index + 1 < len(headers) else len(data)
        spans[name] = (body_start, body_end)
        header_spans[name] = (start, body_start)
    for name in ('SYSTEM', 'USER'):
        if name not in spans:
            raise ProfileError('PROFILE_PROMPT_INVALID', f'sectioned prompt has no [{name}] section')
    return PromptStructure('sectioned', spans, header_spans)


def parse_meta(data, structure):
    """Read [META] as a YAML mapping of scalars; it is recorded in the manifest, never sent as text.

    The scalar contract is `config._MetaLoader`, the same loader the manifest's `meta` field is read with.
    """
    if 'META' not in structure.spans:
        return None
    try:
        value = _load_yaml(structure.body(data, 'META'), _MetaLoader, '[META]')
    except ConfigurationError as error:
        raise ProfileError('PROFILE_PROMPT_INVALID', f'[META] is not a YAML mapping: {error}') from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileError('PROFILE_PROMPT_INVALID', '[META] is not a YAML mapping')
    for key, item in value.items():
        # The manifest schema allows string, boolean and number; nulls, explicit tags such as `!!timestamp`
        # and nesting are refused.
        if not isinstance(item, (str, bool, int, float)) or (isinstance(item, float) and not math.isfinite(item)):
            raise ProfileError('PROFILE_PROMPT_INVALID', f'[META] {key}: expected a string, boolean or finite number')
    return value


def placeholders(template):
    """Return the `{{name}}` placeholder names of a [USER] template in first-appearance order.

    Every `}}` must close a `{{`: one before the next `{{`, or after the last placeholder, is refused (G2).
    """
    names = []
    position = 0
    while True:
        start = template.find(_OPEN, position)
        stray = template.find(_CLOSE, position, len(template) if start < 0 else start)
        if stray >= 0:
            raise ProfileError('PROFILE_PROMPT_INVALID', f'unmatched placeholder close at byte {stray}')
        if start < 0:
            return names
        end = template.find(_CLOSE, start + len(_OPEN))
        if end < 0:
            raise ProfileError('PROFILE_PROMPT_INVALID', f'unclosed placeholder at byte {start}')
        raw = template[start + len(_OPEN):end]
        try:
            name = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            raise ProfileError('PROFILE_PROMPT_INVALID', f'placeholder at byte {start} is not UTF-8') from error
        if not name.isidentifier():
            raise ProfileError('PROFILE_PROMPT_INVALID', f'placeholder at byte {start} is not a name: {name!r}')
        if name not in names:
            names.append(name)
        position = end + len(_CLOSE)


def system_message(data, template_format, structure):
    """The system message verbatim: the [SYSTEM] slice, or the whole file when `plain`."""
    if template_format == 'plain':
        return data
    if structure.template_format != 'sectioned':
        raise ProfileError('PROFILE_TEMPLATE_INVALID', 'manifest says sectioned but the prompt has no sections')
    return structure.body(data, 'SYSTEM')


def user_template(data, template_format, structure):
    """The [USER] template bytes, or None for `plain` (the brief is the whole user message)."""
    if template_format == 'plain':
        return None
    if structure.template_format != 'sectioned':
        raise ProfileError('PROFILE_TEMPLATE_INVALID', 'manifest says sectioned but the prompt has no sections')
    return structure.body(data, 'USER')


def load_prompt(project_root, prompt_id):
    """Read a pipeline-owned prompt `prompts/<prompt_id>.txt` as bytes."""
    root = Path(project_root).resolve()
    if not isinstance(prompt_id, str) or not prompt_id or not set(prompt_id) <= _PROMPT_ID_CHARS \
            or prompt_id.strip('.') != prompt_id or '..' in prompt_id:
        raise ConfigurationError(f'prompt_id: invalid prompt id {prompt_id!r}')
    try:
        path = _inside(root / 'prompts' / f'{prompt_id}.txt', root)
        return path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f'prompt_id: cannot read prompt {prompt_id!r}: {error.strerror or error}') from error
