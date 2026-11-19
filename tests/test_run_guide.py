"""The run guide and the commands table of docs/execution.md match the built command line (T-016 P5, R-015, R-020).

Nothing here reads meaning from prose: documents are parsed as Markdown, so the checked facts are structure only (a fenced
code block, the first code span of a table row, a link target) and every command is tested through the real argparse tree
of `python -m prompt_enhancer`. No command is run."""
import argparse
import shlex
from pathlib import Path

from markdown_it import MarkdownIt

from prompt_enhancer.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'docs' / 'run-guide.md'
EXECUTION = ROOT / 'docs' / 'execution.md'
LINKING = (ROOT / 'README.md', EXECUTION, ROOT / 'docs' / 'serving.md')
OPERATORS = frozenset(('|', '||', '&&', ';', '>', '>>', '2>', '2>&1', '&'))  # a shell operator ends one command
MODULE = ('-m', 'prompt_enhancer')  # protocol-bound: the module entry point; `prompt_enhancer.embed_server` is another module


def tokens_of(path):
    return MarkdownIt('commonmark').enable('table').parse(path.read_text(encoding='utf-8'))


def leaves(parser, path=()):
    """Every (group, action) the parser accepts: a parser with no sub-parsers is a command."""
    groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if not groups:
        return {path}
    found = set()
    for group in groups:
        for name, child in group.choices.items():
            found |= leaves(child, (*path, name))
    return found


def command_lines(path):
    """The lines of every fenced code block, a backslash or backtick at the end of a line joining the next one."""
    lines = []
    for token in tokens_of(path):
        if token.type != 'fence':
            continue
        pending = ''
        for line in token.content.splitlines():
            stripped = line.rstrip()
            if stripped.endswith(('\\', '`')):
                pending += stripped[:-1] + ' '
                continue
            lines.append(pending + stripped)
            pending = ''
        if pending:
            lines.append(pending)
    return lines


def invocations(path):
    """The argument lists after `-m prompt_enhancer` in the fenced blocks of a document; a `<placeholder>` becomes `1`,
    which every argument type of the parser accepts (a choice is written literally in the document)."""
    found = []
    for line in command_lines(path):
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        for index in range(len(words) - 1):
            if tuple(words[index:index + 2]) == MODULE:
                arguments = []
                for word in words[index + 2:]:
                    if word in OPERATORS:
                        break
                    arguments.append('1' if word.startswith('<') and word.endswith('>') else word)
                found.append((line, arguments))
                break
    return found


def parse(arguments):
    try:
        return build_parser().parse_args(arguments)
    except SystemExit:  # argparse reports its own error on stderr; the caller names the line
        return None


def misspelled_options(arguments):
    """The long options of one invocation that are not an exact `option_strings` entry of its command. `parse` alone
    accepts an abbreviation (argparse `allow_abbrev`), so `--snap` for `--snapshot` would pass; the runtime parser is not
    changed. A word after an option that takes a value is that value (it may start with `--` once `=` joins it), and
    nothing after a bare `--` is an option."""
    parser = build_parser()
    index = 0
    while index < len(arguments):
        groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if not groups or arguments[index] not in groups[0].choices:
            break
        parser = groups[0].choices[arguments[index]]
        index += 1
    options = {name: action for action in parser._actions for name in action.option_strings}
    wrong, takes_value = [], False
    for word in arguments[index:]:
        if takes_value:
            takes_value = False
        elif word == '--':
            break
        elif word.startswith('--'):
            name, joined, _ = word.partition('=')
            if name not in options:
                wrong.append(word)
            elif not joined and options[name].nargs != 0:
                takes_value = True
    return wrong


def test_every_invocation_in_the_guide_parses_and_every_command_is_shown():
    shown = set()
    for line, arguments in invocations(GUIDE):
        namespace = parse(arguments)
        assert namespace is not None, f'does not parse with the built command line: {line}'
        assert not misspelled_options(arguments), f'an option is not spelled as the parser spells it: {line}'
        shown.add((namespace.command, namespace.action))
    assert shown == leaves(build_parser()), 'the guide shows exactly the commands the parser has'


def test_every_invocation_in_the_execution_document_parses():
    for line, arguments in invocations(EXECUTION):
        assert parse(arguments) is not None, f'does not parse with the built command line: {line}'
        assert not misspelled_options(arguments), f'an option is not spelled as the parser spells it: {line}'


def test_an_abbreviated_or_unknown_long_option_is_caught_although_argparse_accepts_the_abbreviation():
    abbreviated = ['eval', 'run', '--set', '1', '--snap', '1', '--variants', 'both', '--split', 'all', '--config', '1']
    assert parse(abbreviated) is not None  # why a parse check is not enough: allow_abbrev
    assert misspelled_options(abbreviated) == ['--snap']
    assert misspelled_options(['kb', 'sync', '--plans', '--config', '1']) == ['--plans']
    assert misspelled_options(['kb', 'sync', '--forc=1', '--nope']) == ['--forc=1', '--nope']


def test_a_valid_command_with_positionals_values_and_joined_values_has_no_misspelled_option():
    assert misspelled_options(['kb', 'rollback', '1', '--config', 'configs/x.yaml', '--overlay', 'o.yaml']) == []
    assert misspelled_options(['kb', 'audit', '--folder-count', '2', '--card-count=3', '--seed', '4', '--config', '1']) == []
    assert misspelled_options(['kb', 'sync', '--plan', '--force', '--config', '1']) == []  # flags take no value
    # a value after an option that takes one is a value, whatever it looks like; `=` joins a value that starts with `--`
    assert misspelled_options(['enhance', 'once', '--request', '--odd', '--config', '1']) == []
    assert misspelled_options(['enhance', 'once', '--request=--odd', '--config', '1']) == []
    assert misspelled_options(['kb', 'show', '1', '--', '--after-the-separator']) == []
    assert misspelled_options(['kb', 'sync', '-h', '-x']) == []  # only long options are checked


def test_a_flag_is_judged_against_the_options_of_its_own_command():
    assert misspelled_options(['kb', 'status', '--plan']) == ['--plan']  # `--plan` belongs to `kb sync`
    assert misspelled_options(['profile', 'check', '--profile-id', '1', '--config', '1']) == []


def test_only_fenced_blocks_are_read_so_prose_with_an_abbreviation_is_not(tmp_path):
    document = tmp_path / 'doc.md'
    document.write_text('Use `python -m prompt_enhancer kb sync --pla --config c` here, or write --snap in a sentence.\n\n'
                        '```sh\npython -m prompt_enhancer kb sync --plan --config c\n```\n', encoding='utf-8')
    found = invocations(document)
    assert [arguments for _, arguments in found] == [['kb', 'sync', '--plan', '--config', 'c']]
    assert all(not misspelled_options(arguments) for _, arguments in found)
    fenced = tmp_path / 'fenced.md'
    fenced.write_text('```sh\npython -m prompt_enhancer kb sync --pla --config c\n```\n', encoding='utf-8')
    assert [misspelled_options(arguments) for _, arguments in invocations(fenced)] == [['--pla']]


def commands_table(path):
    """The (group, action) of the first code span of each body row of the table that follows the heading `Commands`."""
    found, after_heading, in_table, title, first_cell = [], False, False, False, False
    for token in tokens_of(path):
        if token.type == 'heading_open':
            title = True
        elif token.type == 'inline' and title:
            after_heading = token.content == 'Commands'
            title = False
        elif after_heading and token.type == 'table_open':
            in_table = True
        elif in_table and token.type == 'table_close':
            return found
        elif in_table and token.type == 'tr_open':
            first_cell = True
        elif in_table and token.type == 'inline' and first_cell:
            first_cell = False
            spans = [child.content.split() for child in token.children if child.type == 'code_inline']
            if spans:
                found.append(tuple(spans[0][:2]))
    return found


def test_the_commands_table_names_exactly_the_built_commands():
    documented = commands_table(EXECUTION)
    assert len(documented) == len(set(documented)), 'a command is listed twice'
    assert set(documented) == leaves(build_parser())


def test_the_readme_the_execution_document_and_serving_link_the_guide():
    for document in LINKING:
        targets = {(document.parent / child.attrs['href'].split('#')[0]).resolve()
                   for token in tokens_of(document) if token.type == 'inline' for child in token.children
                   if child.type == 'link_open' and not child.attrs['href'].startswith(('http://', 'https://', '#'))}
        assert GUIDE.resolve() in targets, f'{document.name} does not link docs/run-guide.md'
