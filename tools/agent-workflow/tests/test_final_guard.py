"""T-024 final round, package A: regression tests of the shell guard (tool_guard.py, shell_targets.py).

Classes (plan.md and the PM's scope additions): F04 the whole state directory is protected in worker mode (PD1);
F06 arguments of programs the guard does not model; F07 archive options that keep absolute member names; F08 git
options and `-c` settings that run configured helper programs; F27 links; Windows path aliases. Every repository and
state directory lives under pytest's tmp_path; nothing here touches the project's own `.tmp/pm-state`.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import git_ledger as gl
import guard_config as gc
import pytest
import shell_targets as st
import tool_guard as tg
from conftest import git, make_link

CFG = gc.load()
WINDOWS = os.name == "nt"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root that is a git repository with one commit, its `.tmp/` scratch and its state directory."""
    monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR", raising=False)
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".tmp/\nruns/\n", encoding="utf-8")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "one")
    for rel in (".tmp/scratch", ".tmp/pm-state/git-ledger", ".tmp/pm-state/prepush-ok", ".tmp/pm-state/patches"):
        (root / rel).mkdir(parents=True)
    (root / ".tmp" / "pm-state" / "cli-workers.json").write_text("{}", encoding="utf-8")
    assert gl.ledger_dir(root).parent == root / ".tmp" / "pm-state"
    return root


def verdict(root: Path, command: str, tool: str = "Bash", mode: str = "worker") -> tuple[str, list[str]]:
    """B when the guard blocks, else A (a read_router warning is no block)."""
    event = {"tool_name": tool, "tool_input": {"command": command}, "session_id": "t-final", "cwd": str(root)}
    blocks, warns = tg.evaluate(tg.Context(event, CFG, argparse.Namespace(config=None, git_mode=mode), root))
    return ("B" if blocks else "A"), blocks + warns


def both_modes(root: Path, command: str, worker: str, tool: str = "Bash", pm: str | None = None) -> None:
    got, messages = verdict(root, command, tool)
    assert got == worker, (command, messages)
    if pm is not None:
        assert verdict(root, command, tool, mode="pm")[0] == pm, command


# F04 (PD1): every entry of the state directory, not only the Git ledger.
F04 = [
    ("echo forged > .tmp/pm-state/prepush-ok/abc", "B"),
    ("echo {} > .tmp/pm-state/cli-workers.json", "B"),
    ("cp a.txt .tmp/pm-state/patches/T.patch", "B"),
    ("rm .tmp/pm-state/prepush-ok/abc", "B"),
    ("echo x > .tmp/pm-state/other.txt", "B"),
    ("rm .tmp/pm-state/*.log", "B"),
    ("cp -r .tmp/scratch .tmp/pm-state", "B"),
    ("echo x > .tmp/scratch/out.txt", "A"),  # the worker's own scratch
    ("cp a.txt .tmp/scratch/", "A"),
    ("mkdir -p .tmp/scratch/sub", "A"),
]


@pytest.mark.parametrize(("command", "expect"), F04)
def test_f04_the_whole_state_directory_is_protected_in_worker_mode(repo: Path, command: str, expect: str) -> None:
    both_modes(repo, command, expect, pm="A")


def test_f04_the_state_directory_follows_its_override(repo: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The protected directory is derived from git_ledger.ledger_dir, so AGENT_WORKFLOW_STATE_DIR moves it."""
    moved = repo / ".tmp" / "elsewhere"
    moved.mkdir()
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(moved))
    assert verdict(repo, "echo x > .tmp/elsewhere/cli-workers.json")[0] == "B"
    assert verdict(repo, "echo x > .tmp/pm-state/x.txt")[0] == "A"  # no longer the state directory


# F06: an argument of a program the guard does not model.
F06 = [
    ("cpio -id -D .tmp/pm-state --no-absolute-filenames < .tmp/scratch/a.cpio", "B"),  # cpio is modelled now
    ("mytool --directory=.tmp/pm-state", "B"),
    ("mytool -D.tmp/pm-state", "B"),
    ("mytool -C .tmp/pm-state/patches", "B"),
    ("mytool of=.tmp/pm-state/x", "B"),
    ("mytool .tmp", "B"),  # above the state directory
    ("python tools/x.py --out .tmp/pm-state/prepush-ok/abc", "B"),
    ("sort -o .tmp/scratch/s.txt -T .tmp/pm-state a.txt", "B"),  # an unmodelled option of a modelled program
    ("mytool -D .tmp/scratch", "A"),
    ("python tools/x.py --out .tmp/scratch/r.json", "A"),
    ("cat .tmp/pm-state/cli-workers.json", "A"),  # programs that only read or print
    ("ls .tmp", "A"),
    ("grep -r x .tmp/pm-state", "A"),
    ("git log -1 -- .tmp", "A"),  # the git guard judges git
]


@pytest.mark.parametrize(("command", "expect"), F06)
def test_f06_arguments_of_unmodelled_programs(repo: Path, command: str, expect: str) -> None:
    both_modes(repo, command, expect, pm="A")


def test_f06_powershell_and_a_run_time_cd(repo: Path) -> None:
    assert verdict(repo, "mytool -Destination:.tmp/pm-state", "PowerShell")[0] == "B"
    assert verdict(repo, "Get-Content .tmp/pm-state/cli-workers.json", "PowerShell")[0] == "A"
    assert verdict(repo, "cd $X && mytool sub")[0] == "B"  # its relative argument cannot be placed
    assert verdict(repo, "cd $X && ls sub")[0] == "A"


def test_f06_a_variable_bound_in_the_command_is_expanded(repo: Path) -> None:
    assert verdict(repo, "d=.tmp/pm-state; mytool -D $d")[0] == "B"
    assert verdict(repo, "d=.tmp/scratch; mytool -D $d")[0] == "A"


# F07: archive options that keep absolute member names, and programs an archiver runs.
F07 = [
    ("tar -xPf .tmp/scratch/a.tar -C .tmp/scratch", "B"),
    ("tar --absolute-names -xf .tmp/scratch/a.tar -C .tmp/scratch", "B"),
    ("tar --abs -xf .tmp/scratch/a.tar -C .tmp/scratch", "B"),
    ("tar xPf .tmp/scratch/a.tar", "B"),
    ("tar -xf .tmp/scratch/a.tar -C .tmp/scratch --to-command=sh", "B"),
    ("tar -xf .tmp/scratch/a.tar -C .tmp/scratch -I zstd", "B"),
    ("tar -cf .tmp/scratch/a.tar --remove-files .tmp/scratch/x", "B"),
    ("unzip -: .tmp/scratch/a.zip -d .tmp/scratch", "B"),
    ("7z x .tmp/scratch/a.7z -o.tmp/scratch -spf", "B"),
    ("cpio -id < .tmp/scratch/a.cpio", "B"),  # keeps absolute names without --no-absolute-filenames
    ("pax -r -f .tmp/scratch/a.pax", "B"),
    ("tar -xf .tmp/scratch/a.tar -C .tmp/scratch", "A"),
    ("tar -xf.tmp/scratch/Final.tar -C .tmp/scratch", "A"),  # `F` inside the -f value is no option
    ("unzip .tmp/scratch/a.zip -d .tmp/scratch", "A"),
    ("7z x .tmp/scratch/a.7z -o.tmp/scratch", "A"),
    ("cpio -id -D .tmp/scratch --no-absolute-filenames < .tmp/scratch/a.cpio", "A"),
]


@pytest.mark.parametrize(("command", "expect"), F07)
def test_f07_archive_member_names_stay_below_the_target(repo: Path, command: str, expect: str) -> None:
    assert verdict(repo, command)[0] == expect, command


# F08: read-only git calls that run a configured helper program (worker mode).
F08 = [
    ("git diff --ext-diff", "B"),
    ("git diff --textconv", "B"),
    ("git log -p --ext-diff", "B"),
    ("git show --textconv HEAD", "B"),
    ("git log --show-signature -1", "B"),
    ("git log --format=%G? -1", "B"),
    ("git cat-file --filters HEAD:a.txt", "B"),
    ("git cat-file --te HEAD:a.txt", "B"),  # git accepts this abbreviation of --textconv
    ("git grep --textc a", "B"),
    ("git help -w log", "B"),
    ("git tag -v v1", "B"),
    ("git remote show origin", "B"),
    ("git -c log.showsignature=true log -1", "B"),
    ("git diff", "A"),
    ("git diff --no-ext-diff --no-textconv", "A"),
    ("git diff --text", "A"),
    ("git log --format=%H%%G -1", "A"),  # `%%` is a literal percent
    ("git -c log.showsignature=false log -1", "A"),
    ("git remote show -n origin", "A"),
    ("git tag -l --sort=-v:refname", "A"),
    ("git status", "A"),
]


@pytest.mark.parametrize(("command", "expect"), F08)
def test_f08_git_helper_programs_in_worker_mode(repo: Path, command: str, expect: str) -> None:
    got, messages = verdict(repo, command)
    assert got == expect, (command, messages)


def test_f08_helper_config_keys_are_refused_even_when_allowlisted(repo: Path) -> None:
    inv = tg.GitInvocation("log", "log", [], [repo], None, None, ("core.fsmonitor=x",))
    assert "core.fsmonitor" in (tg._helper_reason(inv) or "")
    inv = inv._replace(configs=("diff.png.textconv=x",))
    assert tg._helper_reason(inv)
    assert tg._helper_reason(inv._replace(configs=("core.quotepath=false",))) is None


def test_f08_pm_mode_keeps_its_reads(repo: Path) -> None:
    assert verdict(repo, "git diff --textconv", mode="pm")[0] == "A"


# F27: links.
F27 = [
    ("ln -s .tmp/pm-state .tmp/scratch/l", "B"),
    ("ln -s ../pm-state .tmp/scratch/l", "B"),  # relative to the link's directory: .tmp/pm-state
    ("ln -s .. .tmp/scratch/l", "B"),
    ("mklink /J .tmp\\scratch\\l .tmp\\pm-state", "B"),
    ("fsutil hardlink create .tmp/scratch/h .tmp/pm-state/cli-workers.json", "B"),
    ("tar -xf .tmp/scratch/a.tar -C .tmp/scratch/u && echo x > .tmp/scratch/u/l/f", "B"),
    ("ln -s .tmp/scratch/y .tmp/scratch/l2 && echo y > .tmp/scratch/l2", "B"),  # written through the new link
    ("ln -s .tmp/scratch/y .tmp/scratch/l3", "A"),  # a link inside the scratch, nothing written through it
    ("cp -r .tmp/scratch/src .tmp/scratch/dst && echo x > .tmp/scratch/dst/src/f", "A"),  # no link in the source
]


@pytest.mark.parametrize(("command", "expect"), F27)
def test_f27_links_are_judged_at_both_ends(repo: Path, command: str, expect: str) -> None:
    (repo / ".tmp" / "scratch" / "src").mkdir(exist_ok=True)
    got, messages = verdict(repo, command)
    assert got == expect, (command, messages)


def test_f27_a_relative_link_target_is_read_from_the_link_directory(repo: Path) -> None:
    """From cwd `.tmp/scratch/a/b`, `../pm-state` is `.tmp/scratch/a/pm-state` (allowed), but the link
    `.tmp/scratch/l` points at `.tmp/pm-state`: the link-directory reading refuses it."""
    cwd = repo / ".tmp" / "scratch" / "a" / "b"
    cwd.mkdir(parents=True)
    event = {"tool_name": "Bash", "tool_input": {"command": "ln -s ../pm-state ../../l"}, "cwd": str(cwd)}
    ctx = tg.Context(event, CFG, argparse.Namespace(config=None, git_mode="worker"), repo)
    assert tg.check_write(ctx)
    event["tool_input"] = {"command": "ln -s ../ok ../../l"}  # from the link: .tmp/ok, inside the allowlist
    assert not tg.check_write(tg.Context(event, CFG, argparse.Namespace(config=None, git_mode="worker"), repo))


def test_f27_powershell_links(repo: Path) -> None:
    for item_type in ("SymbolicLink", "Junction", "HardLink"):
        command = f"New-Item -ItemType {item_type} -Path .tmp/scratch/l -Value .tmp/pm-state"
        assert verdict(repo, command, "PowerShell")[0] == "B", item_type
    assert verdict(repo, "New-Item -ItemType SymbolicLink -Path .tmp/scratch/l -Value ../pm-state",
                   "PowerShell")[0] == "B"


def test_f27_a_copy_of_a_tree_with_a_link_makes_later_writes_through_it_refused(repo: Path) -> None:
    src = repo / ".tmp" / "scratch" / "linked"
    src.mkdir()
    if not make_link(src / "l", repo / ".tmp" / "scratch"):
        pytest.skip("the platform refuses to create the link")
    assert verdict(repo, "cp -r .tmp/scratch/linked .tmp/scratch/c && echo x > .tmp/scratch/c/linked/l/f")[0] == "B"
    assert verdict(repo, "cp -r .tmp/scratch/linked .tmp/scratch/c")[0] == "A"


def test_f27_an_existing_link_is_resolved_and_an_unresolvable_one_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    link = repo / ".tmp" / "scratch" / "to_state"
    if not make_link(link, repo / ".tmp" / "pm-state"):
        pytest.skip("the platform refuses to create the link")
    assert verdict(repo, "echo x > .tmp/scratch/to_state/cli-workers.json")[0] == "B"
    # Test double (labelled): a resolution that leaves a link in the path (a loop the OS does not resolve).
    monkeypatch.setattr(st, "_link_component", lambda path: path if "stuck" in path else None)
    got, messages = verdict(repo, "echo x > .tmp/scratch/stuck/f")
    assert got == "B" and "cannot resolve" in "\n".join(messages)


def test_f27_link_detection_is_bounded(tmp_path: Path) -> None:
    for k in range(5):
        (tmp_path / f"f{k}").write_text("x", encoding="utf-8")
    assert st._tree_has_link(tmp_path, [100]) is False
    assert st._tree_has_link(tmp_path, [2]) is True  # over the budget counts as a link
    assert st._tree_has_link(tmp_path / "missing", [100]) is False


# Windows path aliases (package B's sibling): the protected match compares paths as the file system resolves them.
ALIASES = [
    "echo x > '.tmp/pm-state./cli-workers.json'",
    "echo x > '.tmp/pm-state /cli-workers.json'",
    "echo x > '.tmp/PM-STATE/cli-workers.json'",
    "echo x > '.tmp/pm-state/cli-workers.json::$DATA'",
    "echo x > '.tmp/pm-state:stream'",
    "mytool '.tmp/pm-state::$INDEX_ALLOCATION/x'",
    "mytool '.tmp/pm-state /x'",
]


@pytest.mark.skipif(not WINDOWS, reason="Win32 decides these aliases; on Linux they are other names")
@pytest.mark.parametrize("command", ALIASES)
def test_windows_aliases_of_the_state_directory_are_refused(repo: Path, command: str) -> None:
    got, messages = verdict(repo, command)
    assert got == "B", (command, messages)


@pytest.mark.skipif(not WINDOWS, reason="8.3 short names exist only on Windows volumes that keep them")
def test_windows_short_name_of_the_state_directory_is_refused(repo: Path) -> None:
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(repo / ".tmp" / "pm-state"), buf, 1024)  # type: ignore[attr-defined]
    if not buf.value or buf.value.lower().endswith("pm-state"):
        pytest.skip("this volume keeps no 8.3 names")
    assert verdict(repo, f"echo x > '{Path(buf.value).as_posix()}/x.txt'")[0] == "B"


@pytest.mark.skipif(not WINDOWS, reason="Win32 aliases")
def test_fs_key_folds_win32_aliases(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    key = st.fs_key(tmp_path / "d")
    for spelling in ("d.", "d ", "D", "d::$INDEX_ALLOCATION", "d:s"):
        assert st.fs_key(f"{tmp_path}\\{spelling}") == key, spelling
    assert st.fs_key(f"\\\\?\\{tmp_path}\\d") == key


def test_fs_key_keeps_names_on_posix(tmp_path: Path) -> None:
    if WINDOWS:
        pytest.skip("POSIX names")
    assert st.fs_key(tmp_path / "d.") != st.fs_key(tmp_path / "d")
    assert st.fs_key(tmp_path / "d:s") != st.fs_key(tmp_path / "d")


def test_probe_words_read_option_values() -> None:
    assert st._probe_words("--directory=D", "bash") == ["--directory=D", "D"]
    words = st._probe_words("-DD", "bash")
    assert "D" in words and "-DD" in words
    assert "x" in st._probe_words("/log:x", "cmd")
    assert st._probe_words("of=y", "bash")[-1] == "y"


def _ctx(command: str, root: Path, tool: str = "Bash") -> Any:
    return tg.Context({"tool_name": tool, "tool_input": {"command": command}, "cwd": str(root)}, CFG,
                      argparse.Namespace(config=None, git_mode="worker"), root)


def test_targets_carry_their_command_and_link_kind(repo: Path) -> None:
    writes = _ctx("ln -s x .tmp/scratch/l; echo y > .tmp/scratch/f", repo).writes()
    groups = {t.word: (t.group, t.links) for t in writes.targets}
    assert groups[".tmp/scratch/l"] == (0, st.LINK_MAKES)
    assert groups[".tmp/scratch/f"] == (1, "")


# Fix round 1, review CX1 and its class: getopt_long reads an unambiguous prefix of a long option as the option.
# One command per deciding option of st.LONG_DECIDING; `{o}` is the option word. The cases below are generated from
# the table: every prefix must give the full option's targets and problems, or be refused as ambiguous.
LONG_TEMPLATES = {
    "tar": {
        **{f: "tar {o} -f .tmp/scratch/a.tar -C .tmp/scratch" for f in ("--extract", "--get")},
        **{f: "tar {o} -f .tmp/scratch/a.tar .tmp/scratch/x"
           for f in ("--create", "--append", "--update", "--concatenate", "--catenate", "--delete")},
        **{f: "tar -x {o} -f .tmp/scratch/a.tar -C .tmp/scratch"
           for f in ("--absolute-names", "--absolute-paths", "--insecure")},
        **{f: "tar -x -f .tmp/scratch/a.tar -C .tmp/scratch {o}=sh" for f in st.TAR_RUNS},
        "--remove-files": "tar -c -f .tmp/scratch/a.tar {o} .tmp/scratch/x",
        "--listed-incremental": "tar -c -f .tmp/scratch/a.tar {o}=.tmp/scratch/i .tmp/scratch/x",
        "--index-file": "tar -c -f .tmp/scratch/a.tar {o}=.tmp/scratch/i .tmp/scratch/x",
        "--directory": "tar -x -f .tmp/scratch/a.tar {o}=.tmp/scratch/d",
        "--file": "tar -c {o}=.tmp/scratch/a.tar .tmp/scratch/x",
    },
    "cp": {
        "--target-directory": "cp {o}=.tmp/scratch/d .tmp/scratch/x",
        **{f: "cp {o} .tmp/scratch/x .tmp/scratch/y"
           for f in ("--no-target-directory", "--parents", "--symbolic-link", "--link", "--recursive", "--archive",
                     "--no-dereference")},
    },
    "mv": {
        "--target-directory": "mv {o}=.tmp/scratch/d .tmp/scratch/x",
        "--no-target-directory": "mv {o} .tmp/scratch/x .tmp/scratch/y",
    },
    "ln": {"--target-directory": "ln -s {o}=.tmp/scratch/d .tmp/scratch/x"},
    "install": {
        "--target-directory": "install {o}=.tmp/scratch/d .tmp/scratch/x",
        "--no-target-directory": "install {o} .tmp/scratch/x .tmp/scratch/y",
        "--directory": "install {o} .tmp/scratch/d",
    },
    "rsync": {"--remove-source-files": "rsync {o} .tmp/scratch/x .tmp/scratch/y"},
    "wget": {
        "--output-document": "wget {o}=.tmp/scratch/o http://h/x",
        "--output-file": "wget {o}=.tmp/scratch/o --spider http://h/x",
        "--append-output": "wget {o}=.tmp/scratch/o --spider http://h/x",
        "--directory-prefix": "wget {o}=.tmp/scratch/p http://h/x",
    },
    "sort": {"--output": "sort {o}=.tmp/scratch/o .tmp/scratch/x"},
    "patch": {"--output": "patch {o}=.tmp/scratch/o"},
    "csplit": {"--prefix": "csplit {o}=.tmp/scratch/p .tmp/scratch/x 1"},
    "cpio": {
        "--extract": "cpio {o} --no-absolute-filenames -D .tmp/scratch",
        "--pass-through": "cpio {o} .tmp/scratch/d",
        "--directory": "cpio -i --no-absolute-filenames {o}=.tmp/scratch/d",
        "--file": "cpio -o {o}=.tmp/scratch/a.cpio",
    },
    "sed": {
        "--in-place": "sed {o} s/a/b/ .tmp/scratch/x",
        "--expression": "sed {o}=w.tmp/scratch/o 1",  # without the option, the script is `1` (an address)
        "--file": "sed {o}=.tmp/scratch/s .tmp/scratch/x",
        "--line-length": "sed {o} 5 w.tmp/scratch/o .tmp/scratch/x",
    },
    **{name: {"--reference": f"{name} {{o}}=.tmp/scratch/r .tmp/scratch/x"} for name in ("chmod", "chown", "chgrp")},
    "env": {"--split-string": "env {o}='touch .tmp/scratch/e'"},
}
NEUTRAL = "--verbose"  # no deciding option starts with it: the command without the deciding option


def _prefix_cases() -> list[tuple[str, str, str]]:
    cases = []
    for program, deciding in st.LONG_DECIDING.items():
        for full in deciding:
            for n in range(3, len(full) + 1):
                if full[:n] not in st.LONG_EXACT_OTHERS:
                    cases.append((program, full, full[:n]))
    return cases


PREFIX_CASES = _prefix_cases()


def _signature(root: Path, command: str) -> tuple[list[tuple[str, ...]], list[str]]:
    writes = _ctx(command, root).writes()
    targets = sorted((t.word, t.verb, str(t.spread), t.links, t.source or "") for t in writes.targets)
    return targets, sorted(writes.problems)


def test_cx1_every_deciding_option_has_a_template() -> None:
    assert {p: set(v) for p, v in LONG_TEMPLATES.items()} == {p: set(v) for p, v in st.LONG_DECIDING.items()}
    assert not any(NEUTRAL.startswith(f) or f.startswith(NEUTRAL) for v in st.LONG_DECIDING.values() for f in v)


@pytest.mark.parametrize(("program", "full"), [(p, f) for p, v in st.LONG_DECIDING.items() for f in v])
def test_cx1_each_deciding_option_changes_what_the_command_writes(repo: Path, program: str, full: str) -> None:
    template = LONG_TEMPLATES[program][full]
    assert _signature(repo, template.format(o=full)) != _signature(repo, template.format(o=NEUTRAL)), full


@pytest.mark.parametrize(("program", "full", "prefix"), PREFIX_CASES)
def test_cx1_a_long_option_prefix_is_read_as_the_option_or_refused(repo: Path, program: str, full: str,
                                                                   prefix: str) -> None:
    deciding = st.LONG_DECIDING[program]
    hits = [f for f in deciding if st.long_matches(prefix, f)]
    targets, problems = _signature(repo, LONG_TEMPLATES[program][full].format(o=prefix))
    if prefix not in deciding and len(hits) >= 2:
        assert any("abbreviates" in p for p in problems), (prefix, hits, problems)
    else:
        assert (targets, problems) == _signature(repo, LONG_TEMPLATES[program][full].format(o=full)), prefix


AMBIGUOUS = [
    "tar --c -f .tmp/scratch/a.tar .tmp/scratch/x",  # --create, --catenate, --concatenate, --checkpoint-action
    "tar -x --absolute- -f .tmp/scratch/a.tar -C .tmp/scratch",  # GNU --absolute-names, bsdtar --absolute-paths
    "tar -x --r=sh -f .tmp/scratch/a.tar -C .tmp/scratch",  # --rsh-command, --rmt-command, --remove-files
    "cp --no .tmp/scratch/x .tmp/scratch/y",  # --no-target-directory, --no-dereference
    "wget --output=.tmp/scratch/o http://h/x",  # --output-document, --output-file
]


@pytest.mark.parametrize("command", AMBIGUOUS)
def test_cx1_an_ambiguous_prefix_of_deciding_options_is_refused(repo: Path, command: str) -> None:
    got, messages = verdict(repo, command)
    assert got == "B" and "abbreviates" in "\n".join(messages), (command, messages)


CX1_REPRO = [
    ("tar --extr -P -f .tmp/payload.tar -C .tmp/safe", "B"),  # the review's reproduction
    ("tar --ext --abs -f .tmp/scratch/a.tar -C .tmp/scratch", "B"),
    ("tar --get -P -f .tmp/scratch/a.tar -C .tmp/scratch", "B"),
    ("tar --to=sh -x -f .tmp/scratch/a.tar -C .tmp/scratch", "B"),
    ("tar --create --rem -f .tmp/scratch/a.tar .tmp/scratch/x", "B"),
    ("tar --dir=.tmp/pm-state -x -f .tmp/scratch/a.tar", "B"),
    ("cp --targ=.tmp/pm-state a.txt", "B"),
    ("sed --e='w .tmp/pm-state/x' a.txt", "B"),  # experiment curlprefix.out: GNU sed reads --e as --expression
    ("sed --l 5 'w .tmp/pm-state/x' a.txt", "B"),  # --l is --line-length: the script is the next operand
    ("chmod --ref=a.txt .tmp/pm-state/cli-workers.json", "B"),
    ("env --s 'touch .tmp/pm-state/x'", "B"),
    ("tar --checkpoint=10 -x -f .tmp/scratch/a.tar -C .tmp/scratch", "A"),  # a real option, not the action
    ("tar --list -f .tmp/scratch/a.tar", "A"),
    ("tar --extract -f .tmp/scratch/a.tar -C .tmp/scratch", "A"),
    ("cp --targ=.tmp/scratch/d a.txt", "A"),
]


@pytest.mark.parametrize(("command", "expect"), CX1_REPRO)
def test_cx1_abbreviated_long_options(repo: Path, command: str, expect: str) -> None:
    got, messages = verdict(repo, command)
    assert got == expect, (command, messages)


# Review CX3: pretty-format modifiers between `%` and the placeholder (pretty-formats.adoc "If you add a `+` ...").
CX3 = [
    ("git log --format=%+GG -1", "B"),
    ("git log --format=%-GS -1", "B"),
    ("git log '--format=% GK' -1", "B"),
    ("git log --pretty=tformat:%+G? -1", "B"),
    ("git show -s --format %-GF HEAD", "B"),
    ("git for-each-ref --f=%(signature)", "B"),  # experiment abbrev.out: for-each-ref reads --f as --format
    ("git tag --v v1", "B"),  # experiment abbrev.out: tag reads --v as --verify
    ("git log --format=%%GG -1", "A"),  # a literal percent, then `GG`
    ("git log --format=%%+GG -1", "A"),
    ("git log --format=%+gs -1", "A"),  # `%gs` is the reflog subject, no signature
    ("git log --format=%+H -1", "A"),
]


@pytest.mark.parametrize(("command", "expect"), CX3)
def test_cx3_signature_placeholders_with_modifiers(repo: Path, command: str, expect: str) -> None:
    got, messages = verdict(repo, command)
    assert got == expect, (command, messages)


# Review CX4: `name:stream` is an NTFS stream to a native program, a provider path only for a provider drive.
@pytest.mark.skipif(not WINDOWS, reason="`state:stream` names an NTFS stream only on Windows")
def test_cx4_a_stream_on_a_letters_only_state_directory_is_refused(repo: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    state = repo / ".tmp" / "state"
    state.mkdir()
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(state))
    assert gl.ledger_dir(repo).parent == state
    for command in ("cd .tmp; mytool state:review-stream", "Set-Location .tmp; mytool state:x",
                    "cd .tmp; Set-Content -Path state:s -Value x"):
        got, messages = verdict(repo, command, "PowerShell")
        assert got == "B", (command, messages)
    assert verdict(repo, "cd .tmp; mytool scratch:review-stream", "PowerShell")[0] == "A"


@pytest.mark.skipif(not WINDOWS, reason="`env:x` names an NTFS stream only on Windows")
def test_cx4_a_provider_drive_name_is_a_stream_to_a_native_program(repo: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A state directory named like a provider drive: a native program opens `env:x` as the stream `x` of `env`."""
    state = repo / ".tmp" / "env"
    state.mkdir()
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(state))
    for command in ("cd .tmp; mytool env:x", "cd .tmp; tar -x -f scratch/a.tar -C env:x",
                    "cd .tmp; [IO.File]::WriteAllText('env:x', 'y')"):
        got, messages = verdict(repo, command, "PowerShell")
        assert got == "B", (command, messages)
    assert verdict(repo, "cd .tmp; Set-Content -Path env:x -Value 1", "PowerShell")[0] == "A"  # the cmdlet: Env:


@pytest.mark.parametrize("command", ["Set-Content -Path Env:X -Value 1", "Set-Content Variable:v 1", "mytool Env:X",
                                     "mytool Function:f", "cd .tmp; mytool function:pm-state"])
def test_cx4_provider_drives_stay_sinks(repo: Path, command: str) -> None:
    """A provider drive is no file path (writing a Function: item is refused by its own rule, not by a path)."""
    got, messages = verdict(repo, command, "PowerShell")
    assert got == "A", (command, messages)
