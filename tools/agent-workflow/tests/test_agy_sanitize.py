"""agy_sanitize.py against the runner's real path lint (scripts/agy-runner/agy-lib.ps1, run through its test driver)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[1]
spec = importlib.util.spec_from_file_location("agy_sanitize_under_test", TOOLS / "agy_sanitize.py")
assert spec is not None and spec.loader is not None
san = importlib.util.module_from_spec(spec)
spec.loader.exec_module(san)

# One line per lint rule family, in the shapes task files and reviews really contain.
OFFENDING = [
    f"Read D:/work/{san.PROJECT_DIR_NAME}/src/a.py and D:\\work\\{san.PROJECT_DIR_NAME}\\tests\\b.py",
    "see c:\\Users\\x\\file.py and E:/data/y.bin and C:\\\\escaped\\\\path",
    "worktree .tmp/worktrees/r3/src/a.py, .tmp\\worktrees\\r3\\src\\a.py and .tmp/cli-jobs/w1/001.result.md",
    "write .tmp/agy-work.bak/x and .tmp/agy-workshop/x and .tmp/agy-work-2/x",
    "read /etc/passwd, /home/user/a, /Users/x/b, /tmp/c/d, /mnt/e/f and (/docs/x.md) and `/src/pkg/mod.py`",
    "also /tasks/X.md /configs/a.yaml /scripts/run.ps1 /tools/t.py /contracts/c.json /prompts/p.md /notes/n.md /out/o",
    "use the .venv/Scripts/python.exe, ./.venv\\Scripts\\python.exe and D:/p/.venv/bin/python",
    "never read .secrets/key, ../.secrets/key or .secrets\\key; the .secrets folder",
    "cat ~/.gemini/settings.json, ~user/x/y and ~\\z",
    "see \\\\server\\share\\a and \\\\10.0.0.5\\c$\\x",
    r"read %USERPROFILE%\x, %temp%/x, $env:TEMP\x, $ENV:Home/x and ${env:TEMP}",
    "read ../x, ..\\x, notes/../../x, ..\\..\\y, .tmp/agy-work/../x and a/..",
    "see [doc](../../docs/a.md), [x](../docs/../../a.md), [y](./../a.md) and plain ../docs/a.md and `../docs/b.md`",
    "<root>/src/a.py and <root>SYS/tests/b.py",
    # JSON-escaped separators, upper case names, and glued text that only becomes a violation after a rewrite (F2).
    r"D:\\proj\\.venv\\Scripts\\python.exe and path .secrets\\key and .venv\\lib and [.venv]\\\\lib",
    r".VENV/x and .SECRETS\y and .SECRETS\\y and .Venv/z and .venv~user/ and .venvc:/ and .secrets~x/",
]
# Text the lint accepts in notes/ (depth 1) and that the sanitizer must leave alone.
HARMLESS = [
    "Review notes/delta.diff and write .tmp/agy-work/ws1/REVIEW.md.",
    "Write only below (.tmp/agy-work), `.tmp/agy-work`, or .tmp/agy-work: done",
    "Read src/pkg/mod.py and tests/test_mod.py; and/or tasks/X.md.",
    "POST /knowledge-v1/concept-pool and GET /service-info; /ready, /health/live, /v1/drain",
    "see https://example.com/a/b and 2026/10/22 about 1/2 of TCP/IP; takes ~5 minutes",
    "git diff Base..Head and a ... b and 1..5 and wait.. then go",
    "fine, see [doc](../docs/a.md#x) and [note](../docs/note/a.md#top) and ([doc](../tools/b.md))",
    "ROOT/src/a.py, DRIVE/x, SYS/etc/y, UP/z, <scratch>/a, <venv>, <secrets-dir>, <home>/x",
    "naïve café - 日本語 text stays as it is",
]


AGY_LIB = ROOT / "scripts" / "agy-runner" / "agy-lib.ps1"
# The Windows runner scripts are not part of every checkout (`setup_project.py check` reports them missing); the
# tests that compare against the runner's own lint skip without them, the pure rewrite tests still run.
needs_runner = pytest.mark.skipif(not AGY_LIB.exists(), reason="scripts/agy-runner/agy-lib.ps1 is not in this checkout")


def lint_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """The runner's path rules, parsed from agy-lib.ps1 itself (PowerShell single-quoted strings), so no copy drifts."""
    if not AGY_LIB.exists():
        return []
    lib = AGY_LIB.read_text(encoding="utf-8")
    found = re.findall(r"@\{ Name = '([^']+)'; Pattern = '((?:[^']|'')*)' \}", lib)
    assert len(found) >= 8
    return [(name, re.compile(pattern.replace("''", "'"))) for name, pattern in found]


LINT = lint_patterns()


def traverses(line: str, depth: int = 1) -> bool:
    """Python port of Test-AgyTraversal."""
    for m in re.finditer(r"(?P<link>\]\()?(?P<tok>[^\s()\[\]`'\",;<>|*]+)", line):
        token = m.group("tok")
        if "://" in token:
            continue
        segments = re.split(r"[/\\]", token.split("#")[0])
        if len(segments) < 2 or ".." not in segments:
            continue
        lead = 0
        while lead < len(segments) and segments[lead] == "..":
            lead += 1
        if m.group("link") and lead <= depth and ".." not in segments[lead:]:
            continue
        return True
    return False


def lint_hits(line: str) -> list[str]:
    names = [name for name, pattern in LINT if pattern.search(line)]
    return [*names, "parent-directory path"] if traverses(line) else names


# -- the rewrite itself (platform-neutral) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"D:/work/{san.PROJECT_DIR_NAME}/src/a.py", "ROOT/src/a.py"),
        (f"c:\\work\\{san.PROJECT_DIR_NAME}\\tests\\b.py", "ROOT/tests\\b.py"),
        ("E:/data/y.bin", "DRIVE/data/y.bin"),
        ("<root>/src/a.py", "ROOT/src/a.py"),
        ("see /src/a.py now", "see SYS/src/a.py now"),
        (".tmp/cli-jobs/w1/001.result.md", "<scratch>/cli-jobs/w1/001.result.md"),
        (".tmp\\worktrees\\r3", "<scratch>/worktrees\\r3"),
        (".tmp/agy-work/ws1/REVIEW.md", ".tmp/agy-work/ws1/REVIEW.md"),
        ("the .secrets folder", "the <secrets-dir> folder"),
        (".venv/lib/python.exe", "<venv>/lib/python.exe"),
        # A placeholder before "/<top-level dir>/" would read as an absolute path to the lint.
        (".venv/Scripts/python.exe", "<venv>SYS/Scripts/python.exe"),
        (".tmp/tests/x.py", "<scratch>SYS/tests/x.py"),
        ("~/src/x", "<home>SYS/src/x"),
        ("~/.gemini/s.json", "<home>/.gemini/s.json"),
        ("\\\\server\\share\\a", "UNC/server\\share\\a"),
        (
            "%TEMP%\\x and $env:TEMP\\y and ${env:TEMP}",
            "<TEMP>\\x and <env:TEMP>\\y and <env:TEMP>",
        ),
        ("read ../x and a/../b", "read UP/x and a/UP/b"),
        ("[a](../docs/x.md) and [b](../../x)", "[a](../docs/x.md) and [b](../UP/x)"),
    ],
)
def test_rewrites(raw: str, expected: str) -> None:
    assert san.sanitize(raw) == expected


@pytest.mark.parametrize("line", HARMLESS)
def test_harmless_text_is_left_alone(line: str) -> None:
    assert san.sanitize(line) == line


@pytest.mark.parametrize("line", OFFENDING)
def test_every_offending_line_changes_and_the_result_is_stable(line: str) -> None:
    once = san.sanitize(line)
    assert once != line
    assert san.sanitize(once) == once


PATHISH = st.lists(
    st.sampled_from(
        [
            *"abcSYsr:.~/\\ %$_{}<>()[]-",
            "..",
            ".tmp",
            ".venv",
            ".secrets",
            "src",
            "env:",
            "agy-work",
            "D:",
            "ENV",
        ]
    ),
    max_size=30,
).map("".join)


@given(PATHISH)
def test_property_sanitizing_is_idempotent_and_removes_the_hidden_names(text: str) -> None:
    once = san.sanitize(text)
    assert san.sanitize(once) == once
    assert ".secrets" not in once and ".venv" not in once
    assert not re.search(r"(?<!\w)[A-Za-z]:[/\\]", once)


LINT_PARTS = st.sampled_from(
    [
        ".venv",
        ".VENV",
        ".Secrets",
        ".secrets",
        ".tmp",
        ".TMP",
        "agy-work",
        "src",
        "tests",
        "home",
        "user",
        "x",
        "c",
        "D",
        "\\",
        "\\\\",
        "/",
        "//",
        ":",
        "~",
        "%",
        "$",
        "env",
        "{",
        "}",
        "<",
        ">",
        "..",
        ".",
        "-",
        " ",
        "](",
        ")",
        "[",
    ]
)
PATHLIKE = st.lists(LINT_PARTS, max_size=14).map("".join)


@needs_runner
@given(PATHLIKE)
def test_property_sanitized_text_passes_the_runner_lint_and_is_stable(text: str) -> None:
    once = san.sanitize(text)
    assert [hit for line in once.split("\n") for hit in lint_hits(line)] == [], (text, once)
    assert san.sanitize(once) == once


@needs_runner
@pytest.mark.parametrize("line", OFFENDING)
def test_the_port_of_the_lint_sees_the_offending_lines_and_not_their_sanitized_form(
    line: str,
) -> None:
    assert lint_hits(line)
    assert lint_hits(san.sanitize(line)) == []


@needs_runner
@pytest.mark.parametrize("line", HARMLESS)
def test_the_port_of_the_lint_accepts_the_harmless_lines(line: str) -> None:
    assert lint_hits(line) == []


def test_a_rewrite_that_never_settles_fails_instead_of_looping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(san, "RULES", ((re.compile(r"a"), "aa"),))
    with pytest.raises(RuntimeError, match="did not settle"):
        san.sanitize("a")
    note = tmp_path / "n.md"
    note.write_text("a\n", encoding="utf-8")
    assert san.main([str(note)]) == 1
    assert note.read_text(encoding="utf-8") == "a\n"
    assert "did not settle" in capsys.readouterr().err


def test_cli_rewrites_in_place_with_lf_and_keeps_unicode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    note = tmp_path / "n.md"
    note.write_bytes("see .tmp/cli-jobs/a\r\nnaïve D:/x/y\r\n".encode())
    assert san.main([str(note)]) == 0
    assert note.read_bytes() == "see <scratch>/cli-jobs/a\nnaïve DRIVE/x/y\n".encode()
    assert f"sanitized {note}" in capsys.readouterr().out


def test_cli_refuses_missing_files_and_no_arguments(tmp_path: Path) -> None:
    assert san.main([]) == 2
    assert san.main([str(tmp_path / "absent.md")]) == 1


# -- against the runner's real lint (Windows PowerShell 5.1 only) ---------------------------------------------------

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 tooling")


def lint_rule_names(items: list[str]) -> set[str]:
    return {item.split(": ")[1] for item in items}


@needs_runner
@win_only
def test_sanitized_offending_text_passes_the_real_path_lint(tmp_path: Path) -> None:
    from test_agy_lib import violations

    raw = "\n".join(OFFENDING) + "\n"
    assert violations(tmp_path, raw, depth=1), "the corpus must trip the lint before sanitizing"
    left = violations(tmp_path, san.sanitize(raw), depth=1)
    assert left == []


@needs_runner
@win_only
def test_corpus_covers_every_rule_the_runner_lint_defines(tmp_path: Path) -> None:
    """A rule added to agy-lib.ps1 without a corpus line (and so without a sanitizer rule) fails here."""
    from test_agy_lib import violations

    lib = (ROOT / "scripts" / "agy-runner" / "agy-lib.ps1").read_text(encoding="utf-8")
    defined = set(re.findall(r"@\{ Name = '([^']+)'", lib)) | {"parent-directory path"}
    assert len(defined) >= 9
    seen = lint_rule_names(violations(tmp_path, "\n".join(OFFENDING) + "\n", depth=1))
    assert defined <= seen, sorted(defined - seen)


@needs_runner
@win_only
def test_the_python_port_of_the_lint_agrees_with_the_real_lint_line_by_line(tmp_path: Path) -> None:
    from test_agy_lib import violations

    lines = [*OFFENDING, *HARMLESS, *(san.sanitize(line) for line in OFFENDING)]
    found = violations(tmp_path, "\n".join(lines), depth=1)
    real = {int(item.split(": ")[0].rsplit(":", 1)[1]) - 1 for item in found}
    assert real == {k for k, line in enumerate(lines) if lint_hits(line)}


@needs_runner
@win_only
def test_harmless_text_passes_the_real_lint_before_and_after(tmp_path: Path) -> None:
    from test_agy_lib import violations

    text = "\n".join(HARMLESS) + "\n"
    assert violations(tmp_path, text, depth=1) == []
    assert violations(tmp_path, san.sanitize(text), depth=1) == []


@needs_runner
@win_only
def test_cli_output_passes_the_workspace_lint(tmp_path: Path) -> None:
    """The file route end to end: write notes, sanitize through main(), then the runner's Assert-AgyPathLint."""
    from test_agy_lib import run

    notes = tmp_path / ".tmp" / "agy-work" / "ws" / "notes"
    notes.mkdir(parents=True)
    note = notes / "task.md"
    note.write_text("\n".join(OFFENDING) + "\n", encoding="utf-8")
    refused = run(
        tmp_path, case="lint_ws", root=str(tmp_path), workspace="ws", prompt="Review notes/task.md"
    )
    assert not refused["ok"]
    assert san.main([str(note)]) == 0
    clean = run(
        tmp_path, case="lint_ws", root=str(tmp_path), workspace="ws", prompt="Review notes/task.md"
    )
    assert clean["ok"], clean
