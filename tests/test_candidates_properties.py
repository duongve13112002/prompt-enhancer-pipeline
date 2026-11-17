"""Structural S1 properties: Unicode completeness, JSON provenance, rank determinism."""

import json
import unicodedata

from hypothesis import example, given, strategies as st

from prompt_enhancer.candidates import (
    _span, _structural_segments, exact_width, normalization_expansion, prompt_windows,
    reciprocal_rank_fusion, structural_segments, window_count,
)
from prompt_enhancer.storage import normalise_name


TEXT = st.text(st.characters(blacklist_categories=('Cs',)), max_size=20)


@given(TEXT, st.integers(min_value=0, max_value=30))
def test_windows_cover_every_span_once(text, width):
    windows = list(prompt_windows(text, width))
    expected = [(start, start + size, text[start:start + size])
                for size in range(1, min(len(text), width) + 1) for start in range(len(text) - size + 1)]
    assert windows == expected
    assert len(windows) == window_count(len(text), width) == len(set(windows))


@given(st.text(st.sampled_from(list('Aa\u0301\u0308\u0327\u1100\u1161\u11a8\ufdfa\ufb03\u0130\u00df猫')), min_size=1, max_size=15))
def test_width_bound_never_skips_a_normalized_key(text):
    # All substrings are independent keys, including combining contractions, Hangul and compatibility expansions.
    spans = list(prompt_windows(text, len(text)))
    keys = {normalise_name(value) for _, _, value in spans}
    width = exact_width(keys)
    found = set(prompt_windows(text, width))
    for span in spans:
        assert span in found
    # Exercise the bound against each individual key rather than only the longest whole prompt key.
    for start, end, value in spans:
        bound = exact_width([normalise_name(value)])
        assert end - start <= bound


def test_width_bound_all_unicode_mappings_are_nonempty():
    # The cached runtime scan exhaustively checks all code points, not just a language-specific sample.
    assert normalization_expansion() >= len(unicodedata.normalize('NFKD', '\ufdfa'))
    assert exact_width(['é']) >= len('e\u0301')
    assert exact_width(['각']) >= len('\u1100\u1161\u11a8')
    assert exact_width([]) == 0


@given(TEXT)
def test_candidate_normalization_is_nfkc_casefold_only(text):
    assert normalise_name(text) == unicodedata.normalize('NFKC', text).casefold()
    assert normalise_name('é') != normalise_name('e')


@given(st.lists(st.lists(st.sampled_from(['a', 'b', 'c', 'd']), max_size=5), max_size=5),
       st.integers(min_value=0, max_value=100))
def test_rrf_is_input_order_independent(rankings, constant):
    assert reciprocal_rank_fusion(rankings, constant) == reciprocal_rank_fusion(list(reversed(rankings)), constant)
    dedup = [list(dict.fromkeys(ranking)) for ranking in rankings]
    assert reciprocal_rank_fusion(rankings, constant) == reciprocal_rank_fusion(dedup, constant)


def test_rrf_reference_ties_and_constant_override():
    assert reciprocal_rank_fusion([['a'], ['b', 'b']], 10) == ['a', 'b']
    assert reciprocal_rank_fusion([['b', 'a'], ['a', 'b']], 0) == ['a', 'b']
    ranks = [['a', 'c', 'd', 'b'], ['e', 'f', 'g', 'b']]
    assert reciprocal_rank_fusion(ranks, 0)[0] == 'a'
    assert reciprocal_rank_fusion(ranks, 10)[0] == 'b'


@given(st.lists(TEXT, min_size=1, max_size=5), st.booleans())
def test_json_values_keep_raw_offsets(values, ensure_ascii):
    prompt = json.dumps({'key': [values, {'duplicate': values}]}, ensure_ascii=ensure_ascii)
    segments = structural_segments(prompt)
    decoded = [value for _, _, value in segments]
    assert sorted(decoded) == sorted(value for value in values * 2 if value)
    for start, end, value in segments:
        assert json.loads(prompt[start:end]) == value


def test_json_keys_never_become_value_queries():
    assert structural_segments('{"only-key": [1, true, null, "a,b", {"nested": "x"}]}') == [
        (14, 15, '1'), (17, 21, 'true'), (23, 27, 'null'), (29, 34, 'a,b'), (47, 50, 'x')]
    assert structural_segments('a,b\nc') == [(0, 1, 'a'), (2, 3, 'b'), (4, 5, 'c')]


@given(st.one_of(TEXT, TEXT.map(lambda value: json.dumps({'value': value})),
                 TEXT.map(lambda value: json.dumps(value) + ', other')))
@example('"Kimono", other')
@example('{"value": "\\u00e9"} trailing')
@example('{"value": "\\u00e9"}')
@example('[NaN]')
def test_json_provenance_requires_whole_prompt_parse(prompt):
    def reject_constant(value):
        raise ValueError('not a JSON number')

    try:
        json.loads(prompt, parse_constant=reject_constant)
    except ValueError:
        expected_json = False
    else:
        expected_json = True
    segments, is_json = _structural_segments(prompt)
    assert is_json is expected_json
    assert structural_segments(prompt) == segments
    for start, end, value in segments:
        raw = prompt[start:end]
        if not is_json:
            assert value == raw
        else:
            decoded = json.loads(raw)
            assert value == (decoded if isinstance(decoded, str) else raw)
        span = _span(prompt, (start, end, value))
        assert 0 <= span['start'] < span['end'] <= len(prompt)
        assert span['text'] == raw
