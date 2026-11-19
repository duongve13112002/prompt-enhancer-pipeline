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


def test_every_invocation_in_the_guide_parses_and_every_command_is_shown():
    shown = set()
    for line, arguments in invocations(GUIDE):
        namespace = parse(arguments)
        assert namespace is not None, f'does not parse with the built command line: {line}'
        shown.add((namespace.command, namespace.action))
    assert shown == leaves(build_parser()), 'the guide shows exactly the commands the parser has'


def test_every_invocation_in_the_execution_document_parses():
    for line, arguments in invocations(EXECUTION):
        assert parse(arguments) is not None, f'does not parse with the built command line: {line}'


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
