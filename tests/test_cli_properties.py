"""Properties of the command-line input layer (T-016, R-001, R-004, R-015, R-026): generated argv, Unicode prompts,
malformed JSON and hostile paths. Each property states one rule: the prompt survives exactly, every refusal is a
precise `Refusal` / `ConfigurationError` / usage error and never another exception, and a refusal leaves no trace."""
import json
import sys

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from cli_fixtures import pipeline_config, write_request, write_yaml
from prompt_enhancer.__main__ import build_parser, main
from prompt_enhancer.cli_input import Refusal, parse_request, read_bounded, resolve_input
from prompt_enhancer.config import ConfigurationError, validate_request_limit

PROPERTY = settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)), max_size=200)  # any scalar value, no lone surrogate


@PROPERTY
@given(prompt=TEXT, ascii_only=st.booleans(), task=st.sampled_from(['generate']))
def test_a_prompt_survives_the_request_reader_exactly(project_root, prompt, ascii_only, task):
    """R-001: whatever the text, in whichever JSON spelling, the parsed prompt equals the file's prompt."""
    data = json.dumps({'prompt': prompt, 'task': task}, ensure_ascii=ascii_only).encode('utf-8')
    assert parse_request(data, project_root) == {'prompt': prompt, 'task': task}


@PROPERTY
@given(data=st.binary(max_size=300))
def test_arbitrary_bytes_are_parsed_or_refused_precisely(project_root, data):
    try:
        request = parse_request(data, project_root)
    except Refusal as refusal:
        assert str(refusal).startswith('request') and len(str(refusal)) < 200
    else:
        assert isinstance(request, dict) and set(request) >= {'prompt', 'task'}


@PROPERTY
@given(prompt=TEXT.filter(bool), cut=st.integers(min_value=0, max_value=400), marker=st.just('PRIVATE-MARKER'))
def test_a_truncated_request_is_refused_without_echoing_its_content(project_root, prompt, cut, marker):
    data = json.dumps({'prompt': marker + prompt, 'task': 'generate'}, ensure_ascii=False).encode('utf-8')
    cut = min(cut, len(data) - 1)  # a strict prefix of a JSON object is never a complete document
    try:
        parse_request(data[:cut], project_root)
    except Refusal as refusal:
        assert marker not in str(refusal) and str(refusal).startswith(('request: not valid JSON', 'request: not UTF-8'))  # the cut may split a character
    else:
        pytest.fail('a truncated JSON object was accepted')


@PROPERTY
@given(value=st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | TEXT,
                          lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3),
                          max_leaves=8))
def test_a_wrongly_shaped_document_is_refused_by_field_and_keyword_only(project_root, value):
    try:
        data = json.dumps(value, allow_nan=True).encode('utf-8')
    except (TypeError, ValueError):
        return
    try:
        parse_request(data, project_root)
    except Refusal as refusal:
        assert str(refusal).startswith('request') and len(str(refusal)) < 200  # a path and a keyword, never the value
    else:
        assert isinstance(value, dict) and 'prompt' in value and value.get('task') in ('generate', 'edit')


@PROPERTY
@given(path=st.one_of(st.text(max_size=60), st.sampled_from([
    '..', '../x', '..\\x', 'a/../../b', 'C:\\x', 'C:/Windows', '/', '\\\\server\\share\\f', 'a\x00b', '.', 'CON', 'aux.json',
    'nul', 'a ', ' a', 'a.', 'x' * 400, '\u202eevil.json', 'a/./b', 'a//b'])))
def test_a_hostile_request_path_is_refused_or_stays_inside_the_root(project_root, path):
    try:
        resolved = resolve_input(project_root, path, '--request')
    except Refusal as refusal:
        assert str(refusal).startswith('--request: ')
    else:
        resolved.resolve().relative_to(project_root.resolve())  # never outside the root, links followed


@PROPERTY
@given(content=st.binary(max_size=3000),
       limit=st.one_of(st.integers(min_value=1, max_value=3500), st.integers(min_value=2 ** 31, max_value=sys.maxsize),
                       st.sampled_from([1, 2 ** 31 - 1, 2 ** 62, sys.maxsize - 1, sys.maxsize])))
def test_read_bounded_returns_the_exact_bytes_or_a_precise_refusal_for_any_validated_limit(
        tmp_path, project_root, content, limit):
    """F1: every limit `validate_request_limit` accepts (up to sys.maxsize) is usable; nothing is allocated from it."""
    path = tmp_path / 'sized.bin'
    path.write_bytes(content)
    try:
        data = read_bounded(project_root, path, limit, '--request')
    except Refusal as refusal:
        assert len(content) > limit and str(refusal).startswith('--request: larger than serving.http.max_request_bytes')
    else:
        assert data == content and len(content) <= limit


@PROPERTY
@given(path=st.one_of(st.text(max_size=40), st.sampled_from(['..', '../x', 'C:\\x', 'a\x00b', '.', 'CON', 'x' * 300])))
def test_a_refused_request_path_leaves_nothing_behind(capsys, model_test_profile, tmp_path_factory, project_root, path):
    work = tmp_path_factory.mktemp('prop', numbered=True)
    config = pipeline_config(model_test_profile, work, project_root, 1)
    config_path = write_yaml(project_root, work, config)
    before = sorted(str(item.relative_to(work)) for item in work.rglob('*'))
    code = main(['enhance', 'once', '--config', str(config_path), f'--request={path}', '--project-root', str(project_root)])  # `=` form: a value that starts with '-' is not an option
    captured = capsys.readouterr()
    assert code == 2 and captured.out == '' and captured.err.startswith('refused: ')
    assert sorted(str(item.relative_to(work)) for item in work.rglob('*')) == before  # no data root, store or trace


TOKENS = ['kb', 'profile', 'enhance', 'eval', 'sync', 'audit', 'status', 'rollback', 'gc', 'show', 'register', 'check',
          'once', 'serve', 'build', 'run', 'import', 'fit', '--config', '--overlay', '--project-root', '--request', '--only',
          '--plan', '--force', '--profile-id', '--seed', '--folder-count', '--card-count', 'x', '1', '-1', '', '--', '\u65e5']


@PROPERTY
@given(argv=st.lists(st.sampled_from(TOKENS) | TEXT, max_size=8))
def test_generated_argv_is_parsed_or_rejected_by_the_usage_error_only(capsys, argv):
    """The parser never raises anything but its own usage exit (status 2); a parse never starts work."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        assert stop.code == 2 or stop.code == 0  # usage error, or --help
    else:
        assert args.command in ('kb', 'profile', 'enhance', 'eval')
    capsys.readouterr()


@PROPERTY
@given(value=st.one_of(st.integers(min_value=1, max_value=sys.maxsize)))
def test_every_positive_limit_is_returned_unchanged(model_test_profile, tmp_path, project_root, value):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    config['serving']['http']['max_request_bytes'] = value
    assert validate_request_limit(config, project_root=project_root) == value


@PROPERTY
@given(value=st.one_of(st.integers(max_value=0), st.integers(min_value=sys.maxsize + 1), st.floats(), st.booleans(), TEXT,
                       st.none(), st.lists(st.integers(), max_size=2)))
def test_every_other_limit_is_a_configuration_error_naming_the_key(model_test_profile, tmp_path, project_root, value):
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    config['serving']['http']['max_request_bytes'] = value
    with pytest.raises(ConfigurationError, match='serving.http.max_request_bytes'):
        validate_request_limit(config, project_root=project_root)
