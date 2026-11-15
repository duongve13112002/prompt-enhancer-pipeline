"""Property tests of the P section parser and the structural conflict lint."""

import json
import math

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from prompt_enhancer.config import _AMBIGUOUS_BREAKS, ConfigurationError, _parse_manifest
from prompt_enhancer.profiles import _dump_manifest, _same
from prompt_enhancer.profile_lint import (DECISIONS, conflict_findings, key_findings, render_addendum,
                                          template_findings)
from prompt_enhancer.profile_prompt import ProfileError, parse_meta, parse_structure, placeholders


HEADERS = (b'[META]', b'[SYSTEM]', b'[USER]')
_line_text = st.text(st.characters(blacklist_characters='\r\n', blacklist_categories=('Cs',)), max_size=12)
_line = _line_text.map(lambda text: text.encode('utf-8')).filter(lambda line: line not in HEADERS
                                                                 and not line.startswith(b'\xef\xbb\xbf'))
_body_lines = st.lists(st.one_of(_line, st.sampled_from([b'', b' [SYSTEM]', b'[USER] ', b'[system]'])), max_size=6)


@settings(max_examples=150, deadline=None)
@given(meta=st.one_of(st.none(), _body_lines), system=_body_lines, user=_body_lines,
       newline=st.sampled_from([b'\n', b'\r\n']), bom=st.booleans(), order=st.permutations(['SYSTEM', 'USER']))
def test_sections_are_exact_byte_slices(meta, system, user, newline, bom, order):
    bodies = {'SYSTEM': newline.join(system + [b'']), 'USER': newline.join(user + [b''])}
    names = (['META'] if meta is not None else []) + list(order)
    if meta is not None:
        bodies['META'] = newline.join(meta + [b''])
    data = (b'\xef\xbb\xbf' if bom else b'') + b''.join(f'[{name}]'.encode() + newline + bodies[name]
                                                         for name in names)
    structure = parse_structure(data)
    assert structure.template_format == 'sectioned'
    for name in names:
        assert structure.body(data, name) == bodies[name]
    pieces = sorted([*structure.header_spans.values(), *structure.spans.values()])
    prefix = data[:pieces[0][0]]
    assert prefix in (b'', b'\xef\xbb\xbf')
    assert prefix + b''.join(data[start:end] for start, end in pieces) == data  # nothing lost or added


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=80) | st.lists(st.sampled_from(HEADERS + (b'x', b'\r', b'\n', b'{{a}}')), max_size=12)
       .map(b''.join))
def test_parser_is_total_and_lossless(data):
    try:
        structure = parse_structure(data)
    except ProfileError as error:
        assert error.code == 'PROFILE_PROMPT_INVALID'
        return
    if structure.template_format == 'plain':
        assert not structure.spans
        return
    spans = sorted([*structure.header_spans.values(), *structure.spans.values()])
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    assert spans[-1][1] == len(data)


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=40))
def test_placeholder_parser_is_total(data):
    try:
        names = placeholders(data)
    except ProfileError as error:
        assert error.code == 'PROFILE_PROMPT_INVALID'
        return
    assert len(set(names)) == len(names) and all(name.isidentifier() for name in names)
    assert all(b'{{' + name.encode() + b'}}' in data for name in names)


def _reference_placeholders(data):
    """Test-only reference: a byte-by-byte scan where every `}}` must close the `{{` it follows (G2)."""
    names, index = [], 0
    while index < len(data):
        if data.startswith(b'}}', index):
            return None
        if data.startswith(b'{{', index):
            end = data.find(b'}}', index + 2)
            try:
                name = data[index + 2:end].decode('utf-8') if end >= 0 else ''
            except UnicodeDecodeError:
                return None
            if end < 0 or not name.isidentifier():
                return None
            names += [] if name in names else [name]
            index = end + 2
        else:
            index += 1
    return names


@settings(max_examples=400, deadline=None)
@given(st.lists(st.sampled_from([b'{{a}}', b'{{b}}', b'{{', b'}}', b'{', b'}', b'x', b' ', b'\n', b'\xff']),
                max_size=10).map(b''.join))
def test_placeholder_delimiters_match_reference(data):
    try:
        names = placeholders(data)
    except ProfileError as error:
        assert error.code == 'PROFILE_PROMPT_INVALID'
        names = None
    assert names == _reference_placeholders(data)


# [META] scalar contract (fix round 1, F3) -------------------------------------------------------------

_YAML11_ONLY = ['yes', 'Yes', 'YES', 'no', 'No', 'NO', 'on', 'On', 'ON', 'off', 'Off', 'OFF', 'y', 'n', 'True',
                'TRUE', 'False', 'FALSE', '007', '-01', '00.5', '0o7', '0b101', '0x1F', '1_000', '12:30', '190:20:30',
                '2026-01-01', '2026-01-01 10:00:00', '2026-1-1T1:00:00Z', '<<', '=']
_tokens = st.one_of(st.sampled_from(_YAML11_ONLY + ['true', 'false', '0', '-0', '+3', '1.', '.5', '1e3', '-2.5E-3']),
                    st.integers().map(str), st.floats(allow_nan=False, allow_infinity=False).map(repr),
                    st.dates().map(str), st.text('abcxyz019_-.:', min_size=1, max_size=8))


def _meta(token):
    prompt = f'[META]\nlabel: {token}\n[SYSTEM]\nS\n[USER]\n{{{{x}}}}\n'.encode()
    return parse_meta(prompt, parse_structure(prompt))['label']


@settings(max_examples=400, deadline=None)
@given(_tokens)
def test_meta_types_only_canonical_core_scalars(token):
    try:
        value = _meta(token)
    except ProfileError as error:
        assert error.code == 'PROFILE_PROMPT_INVALID'
        return
    if isinstance(value, bool):
        assert token in ('true', 'false') and value == (token == 'true')
    elif isinstance(value, int):
        assert int(token) == value and token.lstrip('+-') == str(abs(value))  # decimal, no leading zero
    elif isinstance(value, float):
        assert math.isfinite(value) and float(token) == value and not token.lstrip('+-').startswith(('00', '01'))
    else:
        assert value == token  # every other plain scalar is the string exactly as written
    if token in _YAML11_ONLY:
        assert value == token
    # The manifest writer and reader keep META: same value, same type.
    stored = _parse_manifest(_dump_manifest({'meta': {'label': value}}), 'm')['meta']['label']
    assert stored == value and type(stored) is type(value)
    # A hand-written manifest `meta` value follows the same contract as [META] (one shared loader, part b).
    assert _parse_manifest(f'meta:\n  label: {token}\n'.encode(), 'm')['meta']['label'] == value
    assert type(_parse_manifest(f'meta:\n  label: {token}\n'.encode(), 'm')['meta']['label']) is type(value)


# Every code point, surrogates, controls, NEL, LS and PS included: the parser accepts each through an escape,
# so nothing is excluded (fix round 2, RT2).
_ANY = st.characters(codec=None, exclude_categories=())
# Characters random generation rarely reaches, weighted in: NEL, LS, PS, surrogates, NUL, a control, BOM, the
# last code point, and the quote and backslash of the double-quoted style.
_RARE = _AMBIGUOUS_BREAKS + ''.join(map(chr, (0xD800, 0xDFFF, 0x00, 0x1F, 0xFEFF, 0x10FFFF))) + '"\\ a'
_TEXT = st.one_of(st.text(_ANY, max_size=8), st.text(st.sampled_from(_RARE), min_size=1, max_size=6))
_KEY = st.one_of(st.text(_ANY, max_size=5), st.text(st.sampled_from(_RARE), min_size=1, max_size=4))
_FINITE = st.floats(allow_nan=False, allow_infinity=False)
_META_SCALAR = st.one_of(_TEXT, st.booleans(), st.integers(), _FINITE)
_VALUE = st.recursive(st.one_of(_META_SCALAR, st.none()),
                      lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(_KEY, inner, max_size=3)),
                      max_leaves=10)


@st.composite
def _manifests(draw):
    fields = draw(st.dictionaries(_KEY.filter(lambda key: key != 'meta'), _VALUE, max_size=4))
    if not draw(st.booleans()):
        return fields
    items = list(fields.items())
    items.insert(draw(st.integers(0, len(items))), ('meta', draw(st.dictionaries(_KEY, _META_SCALAR, max_size=4))))
    return dict(items)


@settings(max_examples=400, deadline=None)
@given(_manifests())
def test_manifest_writer_round_trips(manifest):
    """Write then read gives the same value, type and order for any value and character; the form is stable."""
    written = _dump_manifest(manifest)  # refuses with PROFILE_MANIFEST_UNWRITABLE on any change
    assert _same(_parse_manifest(written, 'm'), manifest)
    assert not any(char in written.decode('utf-8') for char in _AMBIGUOUS_BREAKS)  # always escaped
    assert _dump_manifest(_parse_manifest(written, 'm')) == written  # confirm's writer-form gate relies on it


_REFUSED = object()
_NAMED = {'"': '\\"', '\\': '\\\\', '\x00': '\\0', '\t': '\\t', '\n': '\\n', '\r': '\\r', '\xa0': '\\_',
          **dict(zip(_AMBIGUOUS_BREAKS, ('\\N', '\\L', '\\P')))}


def _raw_ok(char):
    """Characters the reader accepts raw inside a double-quoted scalar and keeps as written."""
    code = ord(char)
    return char not in '"\\' + _AMBIGUOUS_BREAKS and (0x20 <= code <= 0x7E or 0xA0 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD
                                  and code != 0xFEFF or code >= 0x10000)


def _escape(char, form):
    code = ord(char)
    if form == 0 and char in _NAMED:
        return _NAMED[char]
    return f'\\x{code:02X}' if code < 0x100 else f'\\u{code:04X}' if code < 0x10000 else f'\\U{code:08X}'


@st.composite
def _yaml_sources(draw):
    """Manifest text with anchors, aliases (into and out of `meta`), merge keys, tags, escapes and raw
    characters, and the value a faithful reader gives: `_REFUSED` when a raw NEL, LS or PS is in the text."""
    anchors, state = [], {'refused': False}
    raw_breaks = draw(st.integers(0, 3)) == 0  # a quarter of the texts may hold a raw NEL, LS or PS

    def string(text):
        out = []
        for char in text:
            form = draw(st.integers(0, 2))
            if char in _AMBIGUOUS_BREAKS and form == 2 and raw_breaks:
                out.append(char)  # raw: the reader must refuse the whole text
                state['refused'] = True
            elif _raw_ok(char) and form == 1:
                out.append(char)
            else:
                out.append(_escape(char, form))
        return ('!!str ' if draw(st.booleans()) else '') + '"' + ''.join(out) + '"'

    def scalar(value):
        if isinstance(value, str):
            return string(value)
        if isinstance(value, bool):
            return draw(st.sampled_from(['', '!!bool '])) + ('true' if value else 'false')
        if isinstance(value, int):
            return draw(st.sampled_from(['', '!!int '])) + str(value)
        if isinstance(value, float):
            return '!!float ' + repr(value)
        return draw(st.sampled_from(['null', '~', '!!null ""']))

    # Each node returns (text, value, plain); `plain` means it holds no merge key, so the META reading and the
    # schema-typed reading give it the same value and `meta` may share it.
    def anchor(text, value, plain):
        if not draw(st.booleans()):
            return text
        name = f'a{len(anchors)}'
        anchors.append((name, value, plain))
        return f'&{name} {text}'

    def node(depth, plain_only=False):
        usable = [item for item in anchors if item[2] or not plain_only]
        if usable and draw(st.integers(0, 3)) == 0:
            name, value, plain = draw(st.sampled_from(usable))
            return f'*{name}', value, plain
        if depth == 0 or draw(st.booleans()):
            value = draw(st.one_of(_META_SCALAR, st.none()))
            return anchor(scalar(value), value, True), value, True
        if draw(st.booleans()):
            items = [node(depth - 1, plain_only) for _ in range(draw(st.integers(0, 3)))]
            value, plain = [item[1] for item in items], all(item[2] for item in items)
            text = ('!!seq ' if draw(st.booleans()) else '') + '[' + ', '.join(item[0] for item in items) + ']'
            return anchor(text, value, plain), value, plain
        return mapping(depth - 1, plain_only)

    def mapping(depth, plain_only):
        parts, value, plain = [], {}, True
        sources = [item for item in anchors if isinstance(item[1], dict)]
        if not plain_only and sources and draw(st.integers(0, 2)) > 0:
            name, merged, _ = draw(st.sampled_from(sources))
            parts.append(f'<<: *{name}')
            value.update(merged)
            plain = False
        for key in draw(st.lists(_KEY, unique=True, max_size=3)):
            text, item, item_plain = node(depth, plain_only)
            parts.append(f'{string(key)}: {text}')
            value[key] = item
            plain = plain and item_plain
        text = ('!!map ' if draw(st.booleans()) else '') + '{' + ', '.join(parts) + '}'
        return anchor(text, value, plain), value, plain

    lines, expected = [], {}
    keys = draw(st.lists(_KEY.filter(lambda key: key != 'meta'), unique=True, min_size=1,
                         max_size=4))  # an empty document is no mapping and is refused as such
    meta_at = draw(st.integers(0, len(keys))) if draw(st.booleans()) else None
    for index in range(len(keys) + 1):
        if index == meta_at:
            # `meta` is a mapping of its own or an alias to an earlier one (RT1); either way it has no merge
            # key, because `<<` is a plain string under the META contract but a merge for the other fields.
            plain_maps = [item for item in anchors if item[2] and isinstance(item[1], dict)]
            if plain_maps and draw(st.booleans()):
                name, value, _ = draw(st.sampled_from(plain_maps))
                text = f'*{name}'
            else:
                text, value, _ = mapping(1, plain_only=True)
            lines.append(f'"meta": {text}')
            expected['meta'] = value
        if index < len(keys):
            text, value, _ = node(2)
            lines.append(f'{string(keys[index])}: {text}')
            expected[keys[index]] = value
    return '\n'.join(lines) + '\n', _REFUSED if state['refused'] else expected


@settings(max_examples=400, deadline=None)
@given(_yaml_sources())
def test_manifest_parse_write_parse(source):
    """Parse keeps every value (aliases into and out of `meta`, merges, tags, escapes); write then parse keeps it."""
    text, expected = source
    raw = text.encode('utf-8')
    if expected is _REFUSED:
        with pytest.raises(ConfigurationError, match=r'raw U\+(0085|2028|2029) at line \d+ column \d+'):
            _parse_manifest(raw, 'm')
        return
    first = _parse_manifest(raw, 'm')
    assert _same(first, expected)
    assert _same(_parse_manifest(_dump_manifest(first), 'm'), first)


@pytest.mark.parametrize(('text', 'value'), [('yes', 'yes'), ('off', 'off'), ('2026-01-01', '2026-01-01'),
                                             ('007', '007'), ('true', True), ('1e3', 1000.0), ("'1e3'", '1e3')])
def test_hand_written_manifest_meta_contract(text, value):
    parsed = _parse_manifest(f'decided: yes\nmeta:\n  label: {text}\n'.encode(), 'm')
    assert parsed['meta']['label'] == value and type(parsed['meta']['label']) is type(value)
    assert parsed['decided'] is True  # every other field keeps its reading


@pytest.mark.parametrize('raw', [b'meta:\n  label: .inf\n', b'base: &b {label: x}\n<<: {meta: *b}\n',
                                 b'meta:\n  a: 1\n  a: 2\n'])
def test_hand_written_manifest_meta_refusals(raw):
    with pytest.raises(ConfigurationError, match='m: '):
        _parse_manifest(raw, 'm')


@settings(max_examples=200, deadline=None)
@given(st.text(st.characters(blacklist_categories=('Cs', 'Cc', 'Cn', 'Zl', 'Zp')), max_size=12))
def test_quoted_meta_is_the_string(text):
    # A JSON string is a YAML double-quoted scalar; YAML reads raw line separators as breaks, hence excluded.
    assert _meta(json.dumps(text, ensure_ascii=False)) == text
    assert yaml.safe_load('a: yes') == {'a': True}  # global YAML 1.1 tables unchanged


_ids = st.sampled_from(['c1', 'c2', 'c3', '', None, 7])
_conflict = st.one_of(
    st.fixed_dictionaries({'id': _ids, 'profile_rule': st.just('r'), 'conflicts_with': st.just('w'),
                           'decision': st.sampled_from(DECISIONS + ('other',)),
                           'decided_by_admin': st.sampled_from([True, False, 'yes'])},
                          optional={'config_keys': st.one_of(st.lists(st.sampled_from(['a.b', 'a.c', '', 'z']),
                                                                      max_size=3), st.just('a.b'))}),
    st.integers(), st.none())


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.lists(_conflict, max_size=5), st.just('not a list')))
def test_lint_is_total_and_complete(conflicts):
    findings = conflict_findings(conflicts)
    if not isinstance(conflicts, list):
        assert findings[0][0] == 'CONFLICT_SHAPE'
        return
    valid = [item for item in conflicts if isinstance(item, dict) and isinstance(item['id'], str) and item['id']]
    # Every conflict that is not admin-decided, valid and activatable yields at least one finding.
    flagged = {cid for _, cid, _ in findings}
    for item in valid:
        keys = item.get('config_keys', [])
        good_keys = isinstance(keys, list) and all(isinstance(k, str) and k for k in keys) and len(set(keys)) == len(keys)
        activatable = (item['decided_by_admin'] is True and good_keys
                       and (item['decision'] == 'profile_wins' or (item['decision'] == 'config_wins' and keys)))
        if not activatable:
            assert item['id'] in flagged
    ids = [item['id'] for item in valid]
    if len(set(ids)) == len(ids) and len(valid) == len(conflicts) and not flagged:
        assert all(item['decided_by_admin'] is True for item in valid)
    assert len(findings) >= len(conflicts) - len(valid)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.sampled_from(['a.b', 'a.c', 'a.s', 'a', 'x.y', 'a.b.c']), max_size=4, unique=True))
def test_key_findings_match_resolution(keys):
    config = {'a': {'b': 2, 'c': [1], 's': ['fit_from_pilot']}}
    findings = key_findings([{'id': 'c1', 'config_keys': keys}], config)
    expected = [('CONFLICT_KEY_UNKNOWN' if key in ('x.y', 'a.b.c') else 'CONFLICT_KEY_SENTINEL', 'c1', key)
                for key in keys if key in ('x.y', 'a.b.c', 'a.s', 'a')]
    assert findings == expected


def _conflict(cid, decision, by_admin, keys=('a.b',)):
    return {'id': cid, 'profile_rule': f'rule {cid}', 'conflicts_with': 'w', 'config_keys': list(keys),
            'decision': decision, 'decided_by_admin': by_admin}


def test_duplicate_conflict_id_is_a_finding():
    findings = conflict_findings([_conflict('c1', 'profile_wins', True), _conflict('c1', 'profile_wins', True)])
    assert findings == [('CONFLICT_DUPLICATE_ID', 'c1', 'id appears more than once')]


@pytest.mark.parametrize(('values', 'inputs'), [({'a': '1', 'b': '2'}, []), ({}, ['a', 'b']), ({'a': '1'}, ['b'])])
def test_exactly_one_input_variable(values, inputs):
    structure = parse_structure(b'[SYSTEM]\ns\n[USER]\n{{a}} {{b}}\n')
    manifest = {'template_format': 'sectioned', 'template_variables': ['a', 'b'], 'template_values': values}
    findings = template_findings(manifest, structure, structure.body(b'[SYSTEM]\ns\n[USER]\n{{a}} {{b}}\n', 'USER'))
    assert bool(findings) == (len(inputs) != 1)


def test_addendum_lists_only_admin_decided_config_wins():
    conflicts = [_conflict('c1', 'config_wins', True), _conflict('c2', 'block_activation', True),
                 _conflict('c3', 'undecided', False), _conflict('c4', 'config_wins', False),
                 _conflict('c5', 'profile_wins', True)]
    addendum = render_addendum(conflicts, {'a': {'b': 3}}, b'header\n')
    assert addendum['text'] == b'header\n[{"conflict": "c1", "profile_rule": "rule c1", "configuration": {"a.b": 3}}]\n'
    assert render_addendum(conflicts[1:], {'a': {'b': 3}}, b'header\n') is None


@pytest.mark.parametrize('decision', ['profile_wins', 'config_wins'])
def test_decided_conflict_has_no_finding(decision):
    conflict = {'id': 'c1', 'profile_rule': 'r', 'conflicts_with': 'w', 'config_keys': ['a.b'],
                'decision': decision, 'decided_by_admin': True}
    assert conflict_findings([conflict]) == []
