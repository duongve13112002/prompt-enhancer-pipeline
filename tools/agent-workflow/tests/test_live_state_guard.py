"""Self-tests of the live PM state guard (S42-statedir): scratch-only, nothing here reads or writes the live directory.

The access guard refuses a path before the operating system sees it, so a test may aim at the live path and still
touch nothing; such a test removes its own recorded violation so the autouse teardown does not fail it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import live_state_guard as lsg
from ci_shard import child_environ  # the conftest puts tools/agent-workflow on sys.path
from conftest import make_link

TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent
HOOKS = TOOLS / "hooks"
LIVE = lsg.ROOT / ".tmp" / "pm-state"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _swallow_recorded(seen: int) -> None:
    """Drop the violations this test provoked on purpose (the autouse teardown fails a test that leaves any)."""
    del lsg._violations[seen:]


# ---- the access guard ----


def test_a_live_path_is_refused_and_recorded_even_when_the_code_swallows_the_error() -> None:
    seen = len(lsg.violations())
    for action in (
        lambda: open(LIVE / "cli-workers.json", "rb"),
        lambda: os.mkdir(LIVE / "scratch-dir"),
        lambda: os.listdir(LIVE),
        lambda: os.scandir(LIVE),
    ):
        with pytest.raises(lsg.LiveStateAccess):
            action()
    try:
        open(LIVE / "git-ledger" / "x.json", "w")  # noqa: SIM115 - refused before any file is created
    except OSError:
        pass  # swallowed, like the code under test might
    recorded = lsg.violations()[seen:]
    assert len(recorded) == 5 and all(str(LIVE) in line for line in recorded)
    _swallow_recorded(seen)


def test_only_the_limiter_subtree_of_the_live_directory_is_exempt() -> None:
    assert lsg.is_protected(LIVE) and lsg.is_protected(LIVE / "inbox" / "pm.events.md")
    assert not lsg.is_protected(LIVE / lsg.SLOTS_NAME)
    assert not lsg.is_protected(LIVE / lsg.SLOTS_NAME / "slot-1.lock")
    assert lsg.is_protected(LIVE / (lsg.SLOTS_NAME + "-other")), "a sibling that only shares the prefix is not exempt"
    seen = len(lsg.violations())
    lsg._hook("open", (str(LIVE / lsg.SLOTS_NAME / "max"),))  # the hook function itself: no I/O, must not raise
    assert lsg.violations()[seen:] == []


def test_a_scratch_path_is_allowed(tmp_path: Path) -> None:
    seen = len(lsg.violations())
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x"
    assert not lsg.is_protected(tmp_path / ".tmp" / "pm-state" / "cli-workers.json"), "a scratch root's default is free"
    assert lsg.violations()[seen:] == []


def test_the_ambient_override_is_protected_too(tmp_path: Path) -> None:
    ambient = tmp_path / "outer-state"
    roots = lsg.live_roots(lsg.ROOT, str(ambient))
    keys = tuple(lsg._key(p) for p in roots)
    assert lsg.is_protected(ambient / "x.json", keys=keys, exempt=())
    assert not lsg.live_roots(lsg.ROOT, None)[1:], "no ambient override: only the project default"
    assert not lsg.is_protected(tmp_path / "other" / "x.json", keys=keys, exempt=())


# ---- link aliases (fix round 1): the target decides, never the spelling of the path ----


def _scratch_state(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic protected tree (a ledger and a `test-slots` directory) beside an unprotected `outside` directory."""
    state = tmp_path / "state"
    (state / lsg.SLOTS_NAME).mkdir(parents=True)
    (state / "ledger.jsonl").write_text("synthetic ledger", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    return state, outside


def _link(link: Path, target: Path) -> None:
    if not make_link(link, target):
        pytest.skip("this platform refused to create a directory link")


def test_an_alias_into_protected_state_is_refused_and_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _scratch_state(tmp_path)
    alias = tmp_path / "alias"
    _link(alias, state)
    monkeypatch.setattr(lsg, "_ROOTS", (state,))  # the hook reads the roots when it is called
    seen = len(lsg.violations())
    with pytest.raises(lsg.LiveStateAccess):
        (alias / "ledger.jsonl").read_text(encoding="utf-8")
    with pytest.raises(lsg.LiveStateAccess):
        os.listdir(alias)
    assert len(lsg.violations()[seen:]) == 2
    _swallow_recorded(seen)


def test_a_chain_of_aliases_and_a_relative_alias_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _scratch_state(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    _link(first, state)
    _link(second, first)
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    assert lsg.is_protected(second / "ledger.jsonl")
    monkeypatch.chdir(tmp_path)
    assert lsg.is_protected(Path("second") / "ledger.jsonl")
    assert not lsg.is_protected(Path("outside") / "outside.txt")


def test_a_protected_root_that_is_itself_a_link_is_refused_by_either_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _ = _scratch_state(tmp_path)
    root = tmp_path / "root-link"
    _link(root, state)
    monkeypatch.setattr(lsg, "_ROOTS", (root,))
    assert lsg.is_protected(root / "ledger.jsonl") and lsg.is_protected(state / "ledger.jsonl")
    assert not lsg.is_protected(root / lsg.SLOTS_NAME / "slot-1.lock")


def test_a_link_inside_the_limiter_subtree_does_not_extend_the_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, outside = _scratch_state(tmp_path)
    _link(state / lsg.SLOTS_NAME / "to-ledger-dir", state)  # redirects out of test-slots into protected state
    _link(state / lsg.SLOTS_NAME / "to-outside", outside)  # redirects out of the protected tree altogether
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    assert lsg.is_protected(state / lsg.SLOTS_NAME / "to-ledger-dir" / "ledger.jsonl"), "the target is not exempt"
    assert not lsg.is_protected(state / lsg.SLOTS_NAME / "to-outside" / "outside.txt"), "the target is unprotected"
    assert not lsg.is_protected(state / lsg.SLOTS_NAME / "slot-1.lock"), "a plain limiter path stays exempt"


def test_a_limiter_directory_that_is_itself_a_link_exempts_nothing_it_points_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    (state / "other").mkdir(parents=True)
    _link(state / lsg.SLOTS_NAME, state / "other")
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    assert lsg.is_protected(state / "other" / "ledger.jsonl"), "the link's target is protected state"
    assert lsg.is_protected(state / lsg.SLOTS_NAME / "ledger.jsonl"), "reached through the link: judged by the target"


def _refusing_resolver(monkeypatch: pytest.MonkeyPatch, failing: Path, error: Exception) -> pytest.MonkeyPatch:
    """A labelled test double: the resolver raises `error` for the one path `failing` (compared by spelling) and is
    the real one for every other path, so the surrounding audit events keep working."""
    real = os.path.realpath
    target = os.path.normcase(os.path.abspath(failing))

    def resolver(path: object, *args: object, **kwargs: object) -> str:
        if os.path.normcase(os.path.abspath(os.fspath(path))) == target:  # type: ignore[arg-type]
            raise error
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lsg.os.path, "realpath", resolver)
    return monkeypatch


@pytest.mark.parametrize("error", [OSError("refused"), ValueError("embedded null byte")])
def test_a_candidate_the_resolver_refuses_is_refused_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    state, outside = _scratch_state(tmp_path)
    alias = tmp_path / "alias"
    _link(alias, state)  # a real alias into protected state: the lexical spelling alone would pass
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    seen = len(lsg.violations())
    with monkeypatch.context() as scoped:
        _refusing_resolver(scoped, alias / "ledger.jsonl", error)
        with pytest.raises(lsg.Unresolvable):
            lsg.is_protected(alias / "ledger.jsonl")
        with pytest.raises(lsg.LiveStateAccess):
            lsg._hook("open", (str(alias / "ledger.jsonl"),))
    recorded = lsg.violations()[seen:]
    assert len(recorded) == 1 and "cannot be classified" in recorded[0]
    _swallow_recorded(seen)


def test_a_root_the_resolver_refuses_refuses_every_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _scratch_state(tmp_path)
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    seen = len(lsg.violations())
    with monkeypatch.context() as scoped:
        _refusing_resolver(scoped, state, OSError("refused"))
        with pytest.raises(lsg.Unresolvable):
            lsg.protected_keys()
        with pytest.raises(lsg.Unresolvable):
            lsg.exempt_keys()  # the exemption goes through the same key
        with pytest.raises(lsg.LiveStateAccess):
            lsg._hook("open", (str(tmp_path / "outside" / "outside.txt"),))  # even an unrelated path: no verdict
    assert len(lsg.violations()[seen:]) == 1
    _swallow_recorded(seen)


def test_a_missing_tail_is_a_normal_answer_not_a_resolution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _scratch_state(tmp_path)
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    assert lsg.is_protected(state / "no-such-dir" / "x.json")
    assert not lsg.is_protected(tmp_path / "no-such-dir" / "x.json")


# ---- Windows spellings of one path (extended-length, UNC, device): one comparison key ----

# Win32 path normalization treats these as one path (learn.microsoft.com/dotnet/standard/io/file-path-formats, "Path
# normalization": canonicalize separators, evaluate `.` and `..`, trim trailing periods and spaces of the last segment).
TRAILING = ["", ".", " ", ". ", " .", "  ", "..", "..."]
INSERTED = ["", "\\.", "\\zz\\..", "\\\\", "\\.\\zz\\.."]  # between the parent folder and the last component


@st.composite
def win32_spellings(draw: st.DrawFn, base: str, *, suffixes: list[str] = TRAILING) -> str:
    """An equivalent Win32 spelling of the absolute path `base` (no trailing separator): mixed separators, case
    variants, `.`/`..`/doubled-separator segments before the last component, trailing periods and spaces."""
    parent, last = base.rsplit("\\", 1)
    text = parent + draw(st.sampled_from(INSERTED)) + "\\" + last + draw(st.sampled_from(suffixes))
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    cased = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(
        ("/" if flip and ch == "\\" else ch.swapcase() if upper else ch) for ch, flip, upper in zip(text, flips, cased)
    )


@pytest.fixture(scope="module")
def win_roots(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    existing = tmp_path_factory.mktemp("existing-root")
    absent = tmp_path_factory.mktemp("absent-parent") / "pm-state"  # never created
    return str(existing), str(absent)


WIN = pytest.mark.skipif(os.name != "nt", reason="Win32 path normalization")
PROPERTY = settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


@WIN
@PROPERTY
@given(data=st.data())
def test_every_win32_spelling_of_an_absent_root_has_one_key(data: st.DataObject, win_roots: tuple[str, str]) -> None:
    _, absent = win_roots
    assert not os.path.exists(absent)
    assert lsg._key(data.draw(win32_spellings(absent))) == lsg._key(absent)


@WIN
@PROPERTY
@given(data=st.data())
def test_every_win32_spelling_of_an_existing_root_has_one_key(data: st.DataObject, win_roots: tuple[str, str]) -> None:
    existing, _ = win_roots
    spelling = data.draw(win32_spellings(existing))
    assert lsg._key(spelling) == lsg._key(existing)
    assert lsg._key("\\\\?\\" + existing) == lsg._key(existing), "an extended-length path of an existing directory"


@WIN
@PROPERTY
@given(data=st.data())
def test_a_child_of_an_absent_root_keeps_the_root_s_key_under_every_spelling(
    data: st.DataObject, win_roots: tuple[str, str]
) -> None:
    _, absent = win_roots
    root = data.draw(win32_spellings(absent, suffixes=["", "."]))  # a middle segment loses only a single period
    assert lsg._key(root + "\\ledger.jsonl") == lsg._key(absent + "\\ledger.jsonl")


@WIN
@pytest.mark.parametrize("suffix", [".", " ", ". ", "..."])
def test_creating_an_absent_protected_root_through_a_trimmed_spelling_is_refused_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    root = tmp_path / "pm-state"  # absent: the guard still knows it
    monkeypatch.setattr(lsg, "_ROOTS", (root,))
    seen = len(lsg.violations())
    with pytest.raises(lsg.LiveStateAccess):
        os.mkdir(str(root) + suffix)  # Win32 would create `pm-state`
    with pytest.raises(lsg.LiveStateAccess):
        os.mkdir(str(root).replace("\\", "/") + suffix)
    assert not root.exists(), "refused before the operating system created it"
    assert len(lsg.violations()[seen:]) == 2
    _swallow_recorded(seen)


def test_the_windows_prefixes_reduce_to_the_ordinary_spelling() -> None:
    """Pure text, no network: the UNC conversion and the prefixes are structural and run on every platform."""
    plain = lsg._plain_spelling
    assert plain("\\\\?\\C:\\a\\b") == "C:\\a\\b"
    assert plain("\\\\.\\C:\\a\\b") == "C:\\a\\b"
    assert plain("\\\\?\\UNC\\server\\share\\a") == "\\\\server\\share\\a"
    assert plain("\\\\?\\unc\\server\\share\\a") == "\\\\server\\share\\a", "the prefix is case-insensitive"
    for unchanged in ("C:\\a\\b", "\\\\server\\share\\a", "/a/b", "relative\\x", ""):
        assert plain(unchanged) == unchanged


@pytest.mark.skipif(os.name != "nt", reason="a case-insensitive file system")
def test_a_case_variant_of_the_candidate_and_of_the_limiter_is_judged_like_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()  # no `test-slots` yet: its missing tail keeps the spelling it was given, so only normcase equates
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    assert lsg.is_protected(str(state / "no-such.json").upper())
    assert not lsg.is_protected(str(state / lsg.SLOTS_NAME / "slot-1.lock").upper())
    assert not lsg.is_protected(state / lsg.SLOTS_NAME.upper() / "slot-1.lock")


@pytest.mark.skipif(os.name != "nt", reason="extended-length paths are a Windows spelling")
def test_an_extended_length_candidate_is_judged_like_the_ordinary_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _ = _scratch_state(tmp_path)
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    extended = "\\\\?\\" + str(state / "ledger.jsonl")
    assert lsg.is_protected(extended) and lsg.is_protected(state / "ledger.jsonl")
    assert lsg._key(extended) == lsg._key(state / "ledger.jsonl")
    assert not lsg.is_protected("\\\\?\\" + str(state / lsg.SLOTS_NAME / "slot-1.lock")), "the exemption sees it too"
    assert not lsg.is_protected("\\\\?\\" + str(tmp_path / "outside" / "outside.txt"))
    seen = len(lsg.violations())
    with pytest.raises(lsg.LiveStateAccess):
        open(extended, "rb")  # the real I/O call keeps its spelling; only the comparison form is shared
    _swallow_recorded(seen)


@pytest.mark.skipif(os.name != "nt", reason="extended-length paths are a Windows spelling")
def test_an_extended_length_root_protects_the_ordinary_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _scratch_state(tmp_path)
    monkeypatch.setattr(lsg, "_ROOTS", (Path("\\\\?\\" + str(state)),))
    assert lsg.is_protected(state / "ledger.jsonl")
    assert not lsg.is_protected(state / lsg.SLOTS_NAME / "slot-1.lock"), "the exemption of an extended root works"
    assert not lsg.is_protected(tmp_path / "outside" / "outside.txt")


def test_a_link_cycle_does_not_crash_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _scratch_state(tmp_path)
    (tmp_path / "loop-a").mkdir()
    (tmp_path / "loop-b").mkdir()
    _link(tmp_path / "loop-a" / "next", tmp_path / "loop-b")
    _link(tmp_path / "loop-b" / "next", tmp_path / "loop-a")
    monkeypatch.setattr(lsg, "_ROOTS", (state,))
    deep = tmp_path / "loop-a" / "next" / "next" / "next" / "x.txt"
    assert not lsg.is_protected(deep), "a cycle that never reaches the state tree is an answer, not protected"


def test_a_non_path_argument_is_ignored() -> None:
    lsg._hook("open", (3, "rb"))  # a file descriptor
    lsg._hook("open", (None,))
    lsg._hook("os.listdir", ())
    lsg._hook("socket.connect", (str(LIVE),))  # an event the guard does not audit


# ---- the metadata snapshot ----


def test_snapshot_and_changes_see_a_write_and_skip_the_limiter_subtree(tmp_path: Path) -> None:
    base = tmp_path / "state"
    (base / "inbox").mkdir(parents=True)
    (base / lsg.SLOTS_NAME).mkdir()
    (base / "a.json").write_text("one", encoding="utf-8")
    before = lsg.snapshot(base)
    assert set(before) == {"inbox", "a.json"}, "test-slots is excluded"
    (base / lsg.SLOTS_NAME / "slot-1.lock").write_text("pid", encoding="utf-8")
    assert lsg.changes(before, lsg.snapshot(base)) == []
    (base / "a.json").write_text("longer text", encoding="utf-8")
    (base / "inbox" / "pm.events.md").write_text("e", encoding="utf-8")
    (base / "gone").mkdir()
    after = lsg.snapshot(base)
    assert lsg.changes(before, after) == ["added gone", "added inbox/pm.events.md", "changed a.json"]
    (base / "gone").rmdir()
    assert "removed gone" in lsg.changes(after, lsg.snapshot(base))


def test_a_directory_s_own_times_are_no_change_on_any_platform(tmp_path: Path) -> None:
    """CI window 4: a directory's mtime moves on Linux when a child is added and not on Windows, so a directory
    records its mode and identity, not its times; setting its times explicitly makes the check the same on both
    platforms."""
    base = tmp_path / "state"
    (base / "inbox").mkdir(parents=True)
    before = lsg.snapshot(base)
    os.utime(base / "inbox", ns=(1_000_000_000, 1_000_000_000))
    assert lsg.changes(before, lsg.snapshot(base)) == [], "a directory's own times are not a change"
    (base / "inbox" / "a.json").write_text("one", encoding="utf-8")
    assert lsg.changes(before, lsg.snapshot(base)) == ["added inbox/a.json"], "its children are"
    (base / "file.txt").write_text("x", encoding="utf-8")
    seen = lsg.snapshot(base)
    os.utime(base / "file.txt", ns=(1_000_000_000, 1_000_000_000))
    assert lsg.changes(seen, lsg.snapshot(base)) == ["changed file.txt"], "a file keeps its full metadata"


def test_an_empty_directory_replaced_by_another_is_a_change(tmp_path: Path) -> None:
    """S42-ci4 review F1: a directory records its identity, so an empty one removed and recreated with the same mode
    and times is still reported. The new one is made while the old still exists, so no inode can be reused."""
    base = tmp_path / "state"
    (base / "empty").mkdir(parents=True)
    os.utime(base / "empty", ns=(1_000_000_000, 1_000_000_000))
    before = lsg.snapshot(base)
    (base / "empty").rename(base / "old")
    (base / "empty").mkdir()
    os.utime(base / "empty", ns=(1_000_000_000, 1_000_000_000))
    (base / "old").rmdir()
    assert lsg.changes(before, lsg.snapshot(base)) == ["changed empty"]


def test_snapshot_keeps_a_link_s_own_metadata_and_never_descends_into_it(tmp_path: Path) -> None:
    state, outside = _scratch_state(tmp_path)
    _link(state / "out-link", outside)
    _link(state / "loop", state)  # a cycle back to the root: a followed link would never end
    found = lsg.snapshot(state)
    assert set(found) == {"ledger.jsonl", "out-link", "loop"}, "the links are listed, their targets are not walked"
    (outside / "new.txt").write_text("n", encoding="utf-8")
    assert lsg.changes(found, lsg.snapshot(state)) == [], "a change behind a link is not this tree's change"


@pytest.mark.skipif(os.name != "nt", reason="a case-insensitive file system")
def test_snapshot_skips_the_limiter_directory_in_any_case_on_windows(tmp_path: Path) -> None:
    base = tmp_path / "state"
    (base / "TEST-SLOTS").mkdir(parents=True)
    (base / "TEST-SLOTS" / "slot-1.lock").write_text("pid", encoding="utf-8")
    (base / "a.json").write_text("x", encoding="utf-8")
    assert set(lsg.snapshot(base)) == {"a.json"}


@pytest.mark.skipif(os.name == "nt", reason="a case-sensitive file system")
def test_snapshot_keeps_a_differently_cased_name_on_a_case_sensitive_system(tmp_path: Path) -> None:
    base = tmp_path / "state"
    (base / "TEST-SLOTS").mkdir(parents=True)
    assert set(lsg.snapshot(base)) == {"TEST-SLOTS"}, "another directory, not the limiter's"


def test_snapshot_of_a_link_to_a_state_root_walks_the_root(tmp_path: Path) -> None:
    state, _ = _scratch_state(tmp_path)
    root = tmp_path / "root-link"
    _link(root, state)  # the protected root itself may be a link; only links below it are not followed
    assert set(lsg.snapshot(root)) == {"ledger.jsonl"}


def test_snapshot_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert lsg.snapshot(tmp_path / "absent") == {}


# ---- the per-test routing ----


def test_each_test_gets_its_own_empty_state_directory(pm_state_dir: Path) -> None:
    assert os.environ[lsg.STATE_ENV] == str(pm_state_dir)
    assert pm_state_dir.is_dir() and list(pm_state_dir.iterdir()) == []
    assert not lsg.is_protected(pm_state_dir), "the per-test directory is scratch, never the live one"


def test_a_child_started_with_the_default_environment_sees_the_per_test_directory(pm_state_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-B", "-c", f"import os; print(os.environ.get({lsg.STATE_ENV!r}))"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(pm_state_dir)


# Readers that resolve the state directory when they are loaded (one process per hook event): module, attribute,
# the file name appended to the state directory ("" when the attribute is the directory itself).
IMPORT_TIME_READERS = [
    ("supervisor_events.py", "STATE", ""),
    ("recall.py", "STATE", ""),
    ("hooks/session_context.py", "STATE", ""),
    ("hooks/standing_rules.py", "STATE", ""),
    ("hooks/precompact_snapshot.py", "STATE", ""),
    ("hooks/precompact_snapshot.py", "NOTE", "pm-continuation.md"),
    ("hooks/ctx_nudge.py", "STATE_DIR", ""),
    ("hooks/process_guard.py", "STATE_DIR", ""),
    ("hooks/process_guard.py", "LOG", "process-guard.log"),
    ("hooks/tool_monitor.py", "CRASH_LOG", "guard-crash.log"),
]
LOAD = textwrap.dedent(
    """
    import importlib.util, sys
    sys.path[:0] = [sys.argv[1], sys.argv[2]]
    spec = importlib.util.spec_from_file_location("probe_mod", sys.argv[3])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print(getattr(mod, sys.argv[4]))
    """
)


def _read_attr(rel: str, attr: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-B", "-c", LOAD, str(TOOLS), str(HOOKS), str(TOOLS / rel), attr],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize(("rel", "attr", "leaf"), IMPORT_TIME_READERS)
def test_a_reader_follows_the_override_and_uses_the_default_when_it_is_unset(
    rel: str, attr: str, leaf: str, tmp_path: Path
) -> None:
    base = {k: v for k, v in os.environ.items() if k != lsg.STATE_ENV}
    chosen = tmp_path / "chosen-state"
    got = _read_attr(rel, attr, {**base, lsg.STATE_ENV: str(chosen)})
    assert os.path.normcase(got) == os.path.normcase(str(chosen / leaf if leaf else chosen))
    other = tmp_path / "other-state"
    got = _read_attr(rel, attr, {**base, lsg.STATE_ENV: str(other)})
    assert os.path.normcase(got) == os.path.normcase(str(other / leaf if leaf else other))
    default = _read_attr(rel, attr, {**base, lsg.STATE_ENV: ""})  # empty counts as unset (the hooks' rule)
    assert os.path.normcase(default) == os.path.normcase(str(LIVE / leaf if leaf else LIVE))


# ---- the whole guard, end to end, in a child pytest run through the real conftest ----

CHILD_TESTS = textwrap.dedent(
    """
    import os
    import live_state_guard as lsg

    def test_control_scratch_state_is_fine(pm_state_dir):
        (pm_state_dir / "ok.json").write_text("{}", encoding="utf-8")

    def test_swallowed_live_access_still_fails():
        try:
            open(os.path.join(str(lsg.live_roots()[0]), "cli-workers.json"), "rb")
        except OSError:
            pass
    """
)


def test_a_test_that_touches_the_live_directory_fails_even_when_it_swallows_the_error(tmp_path: Path) -> None:
    scratch = tmp_path / "child"
    scratch.mkdir()
    (scratch / "test_child.py").write_text(CHILD_TESTS, encoding="utf-8")
    # The child loads the real conftest (so the CI shard hook): without PE_SHARD/PE_SHARDS it runs its own files, whatever
    # shard this job has (decision 168; the child would otherwise land in an empty shard).
    env = {k: v for k, v in child_environ().items() if k != lsg.STATE_ENV}
    env["PYTHONPATH"] = os.pathsep.join([str(TESTS), str(HOOKS)])
    proc = subprocess.run(
        [
            sys.executable, "-B", "-X", "faulthandler", "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-p", "conftest", f"--basetemp={tmp_path / 'bt'}", "--rootdir", str(scratch), str(scratch),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
        cwd=scratch,
        creationflags=CREATE_NO_WINDOW,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    # The swallowing test's body passes; its teardown (the autouse guard check) is the one error.
    assert "2 passed, 1 error" in out and "ERROR test_child.py::test_swallowed_live_access_still_fails" in out, out
    assert "test touched the live PM state directory" in out, out
