"""Rewrite note files for an agy workspace so the runner path lint passes.

The runner reads only `.tmp/agy-work`; `scripts/agy-runner/agy-lib.ps1` (`Get-AgyPathRule`, `Test-AgyTraversal`) refuses
any prompt or `notes/*.md` line that names another location. This rewrites such text into inert placeholders: project and
drive-letter paths become `ROOT/` and `DRIVE/`, an absolute `/src/...` gets a `SYS` prefix, `.tmp/` becomes `<scratch>/`,
`.secrets` and `.venv` become `<secrets-dir>` and `<venv>`, UNC, `~/`, environment-variable and `..` paths are neutralised
(a Markdown link target `](../x)` and `.tmp/agy-work` are kept: the lint accepts them in notes/).
The reviewer cannot read any of them anyway. `tests/test_agy_sanitize.py` runs the real lint over the output.

Usage: python -B -I agy_sanitize.py <file> [<file> ...]   (rewrites in place, UTF-8, LF; used by `gemini-ws.ps1 -Sanitize`)
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

Replacement = str | Callable[[re.Match[str]], str]

# The checkout's directory name (the same on the PM's machine and in the runner's view); derived, not configured,
# so the tooling needs no edit when copied to another repository.
PROJECT_DIR_NAME = Path(__file__).resolve().parents[2].name

# Order matters: environment variables and the project root are rewritten before the generic drive and .tmp rules.
RULES: tuple[tuple[re.Pattern[str], Replacement], ...] = (
    (re.compile(r"%([A-Za-z_][\w()]*)%(?=[/\\])"), r"<\1>"),
    (re.compile(r"\$env:([\w()]+)(?=[/\\])", re.I), r"<env:\1>"),
    (re.compile(r"\$\{env:([^}\s]+)\}", re.I), r"<env:\1>"),
    (
        re.compile(
            r"(?i)\b[a-z]:[\\/]+(?:[^\s\"'`<>|]*?[\\/])?" + re.escape(PROJECT_DIR_NAME) + r"[\\/]"
        ),
        "ROOT/",
    ),
    (re.compile(r"(?i)\b[a-z]:[\\/]+"), "DRIVE/"),
    (re.compile(r"(?<![\w\\])\\\\(?=[\w.$-])"), "UNC/"),
    (re.compile(r"<root>(?:SYS)?/"), "ROOT/"),
    (re.compile(r"(?<!\w)~(?=[\w.-]*[/\\])"), "<home>"),
    # `.tmp/agy-work` is the one location the runner reads, so it stays.
    (
        re.compile(r"(?i)(?<!\w)\.tmp[/\\]+(?!agy-work(?![\w-]|\.[\w-]))"),
        "<scratch>/",
    ),
    (re.compile(r"(?i)\.secrets"), "<secrets-dir>"),
    (re.compile(r"(?i)\.venv"), "<venv>"),
    # The runner lint reads "/" + a top-level directory as an absolute path, also after a placeholder such as
    # `<venv>/Scripts/`, so this rule runs after every placeholder above; the "SYS" prefix reads as relative.
    (
        re.compile(
            r"(?i)(?<![\w.:/\\~-])/(?=(?:home|Users|tmp|mnt|etc|var|usr|opt|root|proc|media|srv|bin|sbin|boot|private"
            r"|Volumes|Library|Applications|src|tests|docs|tasks|configs|scripts|tools|contracts|prompts|notes|out)[/\\])"
        ),
        "SYS/",
    ),
    # A ".." segment of a path-like token. The lint allows one leading ".." of a Markdown link target "](../x)" in notes/,
    # so that link form is kept and every other ".." (also a second one in the same target) is neutralised.
    (re.compile(r"(?<![\w.])(?<!\]\()\.\.(?=[/\\])|(?<=[/\\])\.\.(?![\w.])"), "UP"),
)


MAX_PASSES = (
    8  # bound on the fixpoint loop (safety-bound: a rewrite that never settles must fail, not hang)
)


def sanitize_once(text: str) -> str:
    for pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text


def sanitize(text: str) -> str:
    """Apply `RULES` until the text stops changing: a placeholder can sit next to text that makes a new violation
    (JSON-escaped `.venv\\\\lib` becomes `<venv>\\\\lib`, which the lint reads as a UNC path), and the next pass fixes it."""
    for _ in range(MAX_PASSES):
        rewritten = sanitize_once(text)
        if rewritten == text:
            return text
        text = rewritten
    raise RuntimeError(f"agy_sanitize: the rules did not settle within {MAX_PASSES} passes")


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: agy_sanitize.py <file> [<file> ...]\n")
        return 2
    for name in argv:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"agy_sanitize: cannot read {path}: {exc}\n")
            return 1
        try:
            clean = sanitize(text)
        except RuntimeError as exc:
            sys.stderr.write(f"{exc} ({path})\n")
            return 1
        path.write_text(clean, encoding="utf-8", newline="\n")
        sys.stdout.write(f"sanitized {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
