"""Git ledger and turn snapshot, T-024 final round package C (F09-F13, F18-F21; R-021, R-025).

Each reproduction runs on a scratch repository (pytest `tmp_path`, never the project repository) with a linked
worktree `w`, through the public functions `cli_worker.py` and `tool_guard.py` call (`snapshot`, `unexplained`,
`entries`, `begin`, `finish`, `pending`, `settle`, `main`), so the same test also runs against the round 6 module.

Test doubles (labelled): `FakeTime` stands in for the module's clock in the deadline test; the orchestrator's
events are dicts in place of the Claude Code hook input; ledger entries written by hand stand for forged entries.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import git_ledger as gl
import pytest
from conftest import git, make_link
from hypothesis import given
from hypothesis import strategies as st


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    tmp_path = Path(tmp_path).resolve()
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "commit", "-q", "--allow-empty", "-m", "a")
    for name in ("same", "other"):
        git(main, "branch", name)
    git(main, "symbolic-ref", "refs/heads/link", "refs/heads/same")
    worker = tmp_path / "wt" / "w"
    worker.parent.mkdir()
    git(main, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    oid = git(main, "rev-parse", "HEAD").strip()
    return SimpleNamespace(root=tmp_path, main=main, worker=worker, gitdir=main / ".git", oid=oid)


def ledger(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A ledger as `entries()` returns it, holding `found` (a plain list on the round 6 module)."""
    made = getattr(gl, "Ledger", list)()
    made.extend(found)
    return made


def judged(r: SimpleNamespace, before: dict[str, Any], after: dict[str, Any], found: list | None = None,
           window: tuple[float, float] = (1.0, 9.0)) -> list[str]:
    return gl.unexplained(before, after, window, ledger(found or []), "sid")


def snap(where: Path) -> dict[str, Any]:
    got = gl.snapshot(where, 10)
    assert got is not None and "problem" not in got, got
    return got


# F09: the reflog value is a fingerprint of its bytes, not its line count.
def test_f09_a_reflog_rewritten_with_the_same_line_count_is_flagged(repo: SimpleNamespace) -> None:
    git(repo.main, "commit", "-q", "--allow-empty", "-m", "b")
    before = snap(repo.worker)
    log = repo.gitdir / "logs" / "HEAD"
    lines = log.read_bytes().split(b"\n")
    fields = lines[1].split(b" ")
    fields[0], fields[1] = fields[1], fields[0]  # the entry now claims the reverse move: same length, same count
    log.write_bytes(b"\n".join([lines[0], b" ".join(fields), *lines[2:]]))
    after = snap(repo.worker)
    assert before["state"] != after["state"]
    assert any(c.startswith("reflog logs/HEAD") for c in judged(repo, before, after)), judged(repo, before, after)


# F10: a symref's target is compared; a ref Git leaves out of its listing is not silently dropped.
def test_f10_a_symref_moved_to_a_ref_with_the_same_object_is_flagged(repo: SimpleNamespace) -> None:
    before = snap(repo.worker)
    (repo.gitdir / "refs" / "heads" / "link").write_bytes(b"ref: refs/heads/other\n")
    after = snap(repo.worker)
    assert any(c.startswith("refs/heads/link:") for c in judged(repo, before, after)), after["state"]


def test_f10_an_unreadable_ref_fails_the_reading_and_a_dangling_symref_is_kept(repo: SimpleNamespace) -> None:
    before = snap(repo.worker)
    git(repo.main, "symbolic-ref", "refs/heads/dangle", "refs/heads/nothere")
    after = snap(repo.worker)
    assert "refs/heads/nothere" in after["state"].get("ref refs/heads/dangle", "")  # for-each-ref drops it silently
    assert any(c.startswith("refs/heads/dangle:") for c in judged(repo, before, after))
    (repo.gitdir / "refs" / "heads" / "broken").write_bytes(b"garbage\n")
    got = gl.snapshot(repo.worker, 10)
    assert got is not None and "refs/heads/broken" in got.get("problem", ""), got
    assert gl.unexplained(before, got, (0, 1), [], None) == [got["problem"]]


# F11: the root refs of every Git directory are compared, the worker's own ones are never explained.
@pytest.mark.parametrize("where", ["", "worktrees/w/"])
@pytest.mark.parametrize("name", ["ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "BISECT_EXPECTED_REV"])
def test_f11_a_root_ref_written_during_the_turn_is_flagged(repo: SimpleNamespace, where: str, name: str) -> None:
    before = snap(repo.worker)
    (repo.gitdir / where / name).write_text(f"{repo.oid}\n", encoding="ascii")
    after = snap(repo.worker)
    assert before["state"] != after["state"]
    changes = judged(repo, before, after)
    assert any(name in c for c in changes), changes


def test_f11_the_workers_own_root_ref_is_not_excused_by_a_forged_entry(repo: SimpleNamespace) -> None:
    before = snap(repo.worker)
    (repo.gitdir / "worktrees" / "w" / "ORIG_HEAD").write_text(f"{repo.oid}\n", encoding="ascii")
    (repo.gitdir / "ORIG_HEAD").write_text(f"{repo.oid}\n", encoding="ascii")
    after = snap(repo.worker)
    steps = [s for s in gl.transitions(before["state"], after["state"]) if "ORIG_HEAD" in s[0]]
    assert len(steps) == 2
    forged = [{"start": 2, "end": 3, "session": "pm", "transitions": steps}]
    changes = judged(repo, before, after, forged)
    assert [c for c in changes if "ORIG_HEAD" in c] == [c for c in changes if "worktrees/w/ORIG_HEAD" in c] != []


# F12: one deadline bounds the reading; exceeding it is a reported problem, not an omission.
class FakeTime:
    """Test double of the module's `time`: the monotonic clock advances 0.25 s per reading."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.time, self.sleep = time.time, time.sleep

    def monotonic(self) -> float:
        self.now += 0.25
        return self.now


def test_f12_a_reading_past_the_deadline_is_a_problem_that_flags_the_turn(repo: SimpleNamespace,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    before = snap(repo.worker)
    log = repo.gitdir / "logs" / "refs" / "heads" / "big"
    log.write_bytes(b"x" * 4000 + b"\n")
    monkeypatch.setattr(gl, "CHUNK", 8, raising=False)
    monkeypatch.setattr(gl, "time", FakeTime())
    after = gl.snapshot(repo.worker, 10)
    assert after is not None and "git_timeout_seconds" in after.get("problem", ""), after
    assert gl.unexplained(before, after, (0, 1), [], None) == [after["problem"]]


@pytest.mark.parametrize("kind", ["directory link", "special file"])
def test_f12_a_link_or_special_file_among_the_reflogs_is_a_problem(repo: SimpleNamespace, kind: str) -> None:
    logs = repo.gitdir / "logs" / "refs" / "heads"
    if kind == "directory link":
        if not make_link(logs / "hidden", repo.root):
            pytest.skip("the platform refused the link")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFO on this platform")
        os.mkfifo(logs / "pipe")  # a read of it would block forever
    got = gl.snapshot(repo.worker, 10)
    assert got is not None and "problem" in got, got


# F13: one key per worktree directory, however Git spelled its path.
def test_f13_worktree_keys_are_canonical(repo: SimpleNamespace, tmp_path: Path) -> None:
    spelled = [str(repo.worker), repo.worker.as_posix()]
    if os.name == "nt":
        text = str(repo.worker)
        spelled += [text[0].swapcase() + text[1:], text.upper(), text.lower().replace("\\", "/")]
    alias = tmp_path / "alias"
    if make_link(alias, repo.worker.parent):
        spelled.append(str(alias / repo.worker.name))
    assert len({gl._path_key(s) for s in spelled}) == 1, {s: gl._path_key(s) for s in spelled}


def test_f13_a_worktree_registered_under_another_spelling_keeps_its_own_head(repo: SimpleNamespace,
                                                                             tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    if not make_link(alias, repo.worker.parent):
        pytest.skip("the platform refused the link")
    spelled = alias / "w2"
    if os.name == "nt":
        spelled = Path(str(spelled).swapcase())
    git(repo.main, "worktree", "add", "-q", "--detach", str(spelled), "HEAD")
    real = repo.worker.parent / "w2"
    before = snap(real)
    assert before["own"][0] in before["state"], (before["own"][0], sorted(k for k in before["state"] if "head" in k))
    git(real, "commit", "-q", "--allow-empty", "-m", "w")
    after = snap(real)
    steps = [s for s in gl.transitions(before["state"], after["state"]) if s[0].startswith(("head ", "reflog "))]
    forged = [{"start": 2, "end": 3, "session": "pm", "transitions": steps}]
    assert any(c.startswith("HEAD of ") for c in judged(repo, before, after, forged))


# F18: undecodable, truncated or schema-less records never crash the hook and explain nothing.
def test_f18_an_undecodable_ledger_line_is_skipped(repo: SimpleNamespace) -> None:
    folder = gl.ledger_dir(repo.main)
    folder.mkdir(parents=True)
    good = {"start": 1, "end": 2, "session": "pm", "transitions": [["ref refs/heads/main", "A", "B"]]}
    (folder / gl.LEDGER_NAME).write_bytes(b'{"start": 1, "end": 2, "x": "\xc3"}\n' + json.dumps(good).encode()
                                          + b"\n" + b'{"start": 1, "end"')
    assert list(gl.entries(repo.main)) == [good]


def _event(session: str, call: str, cwd: Path, command: str = "git commit -m x") -> dict[str, Any]:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd), "session_id": session,
            "tool_use_id": call}


def _records(r: SimpleNamespace) -> list[Path]:
    return sorted((gl.ledger_dir(r.main) / gl.PENDING_NAME).glob("*.json"))


@pytest.mark.parametrize("damage", ["no cwd and before", "truncated", "not utf-8", "a list"])
def test_f18_a_bad_pending_record_never_crashes_finish_or_the_hook(repo: SimpleNamespace,
                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                   damage: str) -> None:
    event = _event("pm", "toolu_bad", repo.main)
    gl.begin(repo.main, event, repo.main, 10)
    [path] = _records(repo)
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_bytes({
        "no cwd and before": json.dumps({k: v for k, v in record.items() if k not in ("cwd", "before")}).encode(),
        "truncated": json.dumps(record).encode()[:40],
        "not utf-8": b'{"start": "\xff"}',
        "a list": b"[1, 2]",
    }[damage])
    git(repo.main, "commit", "-q", "--allow-empty", "-m", "x")
    assert gl.finish(repo.main, event, 10) is False
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert gl.main([], root=repo.main) == 0
    assert gl.pending(repo.main, 0, time.time() + 1) == []  # never waited for: no entry is coming
    assert [n for n in gl.incomplete(repo.main, 0, time.time() + 1) if "malformed" in n], err.getvalue()


# F19: records are keyed by session and call, created exclusively, and consumed by their owner only.
def test_f19_records_of_two_sessions_or_colliding_ids_both_survive(repo: SimpleNamespace) -> None:
    events = [_event("pm-a", "same-call", repo.main), _event("pm-b", "same-call", repo.main),
              _event("pm-a", "x/y", repo.main), _event("pm-a", "x:y", repo.main)]
    for event in events:
        gl.begin(repo.main, event, repo.main, 10)
    assert len(_records(repo)) == 4
    git(repo.main, "commit", "-q", "--allow-empty", "-m", "x")
    assert gl.finish(repo.main, {**events[0], "session_id": "intruder"}, 10) is False
    assert len(_records(repo)) == 4  # a call of another session consumes nothing
    for left, event in zip((3, 2, 1, 0), events):
        assert gl.finish(repo.main, event, 10) is True
        assert len(_records(repo)) == left
    assert sorted(e["session"] for e in gl.entries(repo.main)) == ["pm-a", "pm-a", "pm-a", "pm-b"]


def test_f19_a_record_moved_under_another_calls_key_is_not_consumed(repo: SimpleNamespace) -> None:
    """The owner check: a record copied or moved under the key of another session's call (a forgery, or a clash of
    the shortened key) is not taken for that call."""
    for session, call in (("pm-a", "same-call"), ("pm-a", "toolu_a")):
        gl.begin(repo.main, _event(session, call, repo.main), repo.main, 10)
        [path] = _records(repo)
        moved = path.with_name(f"{gl._record_key('pm-b', 'same-call')}.json")
        path.rename(moved)
        git(repo.main, "commit", "-q", "--allow-empty", "-m", "x")
        assert gl.finish(repo.main, _event("pm-b", "same-call", repo.main), 10) is False
        assert moved.exists() and list(gl.entries(repo.main)) == []
        moved.unlink()


@pytest.mark.parametrize("where", ["logs", "worktrees/w"])
def test_f12_a_git_folder_replaced_by_a_link_is_a_problem(repo: SimpleNamespace, where: str) -> None:
    folder = repo.gitdir / where
    copy = repo.root / "copy"
    folder.rename(copy)
    if not make_link(folder, copy):
        pytest.skip("the platform refused the link")
    got = gl.snapshot(repo.main, 10)
    assert got is not None and "link" in got.get("problem", ""), got


def test_f19_a_second_begin_for_the_same_call_keeps_the_first_record(repo: SimpleNamespace) -> None:
    event = _event("pm", "toolu_twice", repo.main)
    gl.begin(repo.main, event, repo.main, 10)
    first = _records(repo)[0].read_bytes()
    git(repo.main, "commit", "-q", "--allow-empty", "-m", "x")
    with contextlib.redirect_stderr(io.StringIO()):
        gl.begin(repo.main, event, repo.main, 10)
    assert [p.read_bytes() for p in _records(repo)] == [first]


# F20: a record past its command's bound ends the wait at once and is named.
def test_f20_an_orphan_record_past_its_bound_stops_the_wait_and_is_named(repo: SimpleNamespace) -> None:
    gl.begin(repo.main, _event("pm", "toolu_orphan", repo.main), repo.main, 10)
    [path] = _records(repo)
    record = json.loads(path.read_text(encoding="utf-8"))
    now = time.time()
    record.update(start=now - 900, deadline=now - 700)  # began 15 min ago, its 200 s bound has passed
    path.write_text(json.dumps(record), encoding="utf-8")
    started = time.monotonic()
    assert gl.settle(repo.main, now - 1000, now, 5) == []
    assert time.monotonic() - started < 2
    assert [n for n in gl.incomplete(repo.main, now - 1000, now) if n.startswith("toolu_orphan")]
    before = snap(repo.worker)
    git(repo.main, "branch", "-f", "other", "HEAD")
    git(repo.main, "commit", "-q", "--allow-empty", "-m", "x")
    after = snap(repo.worker)
    changes = gl.unexplained(before, after, (now - 1000, now), gl.entries(repo.main), "sid")
    assert "toolu_orphan" in changes[0] and any(c.startswith("refs/heads/main:") for c in changes), changes


def test_f20_a_record_within_its_bound_is_still_waited_for(repo: SimpleNamespace) -> None:
    gl.begin(repo.main, _event("pm", "toolu_live", repo.main), repo.main, 10)
    now = time.time()
    assert gl.pending(repo.main, now - 60, now + 1) == ["toolu_live"]
    assert gl.incomplete(repo.main, now - 60, now + 1) == []


# F21: an entry whose end precedes its start explains nothing.
def test_f21_an_entry_ending_before_it_starts_is_unusable(repo: SimpleNamespace) -> None:
    before = snap(repo.worker)
    git(repo.main, "commit", "-q", "--allow-empty", "-m", "x")
    after = snap(repo.worker)
    steps = [s for s in gl.transitions(before["state"], after["state"]) if s[0] != getattr(gl, "LEDGER_KEY", "")]
    sound = {"start": 3, "end": 5, "session": "pm", "transitions": steps}
    assert judged(repo, before, after, [sound]) == []
    assert any(c.startswith("refs/heads/main:") for c in judged(repo, before, after, [{**sound, "end": 2}]))
    folder = gl.ledger_dir(repo.main)
    folder.mkdir(parents=True)
    (folder / gl.LEDGER_NAME).write_text(json.dumps({**sound, "end": 2}) + "\n", encoding="utf-8")
    assert list(gl.entries(repo.main)) == []


# Appends: concurrent hooks never interleave, and a torn tail cannot swallow the next entry.
APPENDER = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import git_ledger as gl
for i in range(25):
    entry = {"start": 1, "end": 2, "session": sys.argv[3], "transitions": [["ref x", str(i), "p" * 3000]]}
    gl._append(Path(sys.argv[2]), (json.dumps(entry) + "\\n").encode(), 30)
"""


def test_parallel_appends_keep_every_line_whole(repo: SimpleNamespace) -> None:
    folder = gl.ledger_dir(repo.main)
    folder.mkdir(parents=True)
    script = repo.root / "appender.py"
    script.write_text(APPENDER, encoding="utf-8")
    hooks = str(Path(gl.__file__).parent)
    procs = [subprocess.Popen([sys.executable, "-X", "faulthandler", "-B", str(script), hooks, str(folder), f"s{n}"])
             for n in range(4)]
    try:
        assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    found = gl.entries(repo.main)
    assert len(found) == 100 and found.malformed == []


def test_a_torn_tail_is_closed_before_the_next_entry(repo: SimpleNamespace) -> None:
    folder = gl.ledger_dir(repo.main)
    folder.mkdir(parents=True)
    (folder / gl.LEDGER_NAME).write_bytes(b'{"start": 1, "en')  # a hook killed mid-write
    entry = {"start": 1, "end": 2, "session": "pm", "transitions": []}
    gl._append(folder, (json.dumps(entry) + "\n").encode(), 10)
    found = gl.entries(repo.main)
    assert list(found) == [entry] and found.malformed == [0]


# Properties of the parsers and validators.
LINE = st.one_of(
    st.binary(max_size=40),
    st.builds(lambda s, e, k: json.dumps({"start": s, "end": e, "session": "pm", "transitions": [[k, None, "v"]]})
              .encode(), st.floats(allow_nan=True), st.floats(allow_nan=True), st.text(max_size=5)),
)


@given(st.lists(LINE, max_size=12), st.binary(max_size=20))
def test_property_entries_never_raise_and_return_only_usable_entries(lines: list[bytes], tail: bytes) -> None:
    import tempfile

    with tempfile.TemporaryDirectory(dir=Path(gl.__file__).resolve().parents[3] / ".tmp") as tmp:
        folder = Path(tmp) / "git-ledger"
        folder.mkdir()
        data = b"".join(line.replace(b"\n", b"") + b"\n" for line in lines) + tail.replace(b"\n", b"")
        (folder / gl.LEDGER_NAME).write_bytes(data)
        old = os.environ.get("AGENT_WORKFLOW_STATE_DIR")
        os.environ["AGENT_WORKFLOW_STATE_DIR"] = tmp
        try:
            found = gl.entries(Path(tmp))
        finally:
            if old is None:
                os.environ.pop("AGENT_WORKFLOW_STATE_DIR")
            else:
                os.environ["AGENT_WORKFLOW_STATE_DIR"] = old
    assert all(gl.entry_ok(e) and e["end"] >= e["start"] for e in found)
    assert all(found.prefixes[k] == hashlib.sha256(data[:k]).hexdigest() for k in found.prefixes)
    assert max(found.prefixes) == data.rfind(b"\n") + 1
    assert len(found) + len(found.malformed) <= len(lines)


JSON = st.recursive(
    st.none() | st.booleans() | st.floats() | st.integers() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=5),
    max_leaves=12,
)


@given(st.one_of(JSON.map(lambda v: json.dumps(v).encode()), st.binary(max_size=60)))
def test_property_pending_records_are_validated_without_raising(raw: bytes) -> None:
    record = gl._pending_record(raw)
    assert isinstance(record, (dict, str))
    if isinstance(record, dict):
        assert record["deadline"] >= record["start"] and isinstance(record["before"], dict)


@given(JSON)
def test_property_entry_ok_never_raises(value: Any) -> None:
    assert gl.entry_ok(value) in (True, False)


@given(st.binary(max_size=200), st.integers(min_value=1, max_value=17))
def test_property_fingerprint_matches_a_direct_reading(data: bytes, chunk: int) -> None:
    import tempfile

    old = gl.CHUNK
    gl.CHUNK = chunk
    try:
        with tempfile.TemporaryDirectory(dir=Path(gl.__file__).resolve().parents[3] / ".tmp") as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(data)
            got = gl._fingerprint(path, time.monotonic() + 30, 30)
    finally:
        gl.CHUNK = old
    cut = data.rfind(b"\n") + 1
    assert got == (len(data), data.count(b"\n"), hashlib.sha256(data).hexdigest(), cut,
                   hashlib.sha256(data[:cut]).hexdigest())


def test_command_seconds_take_the_tool_timeout_else_the_documented_maximum() -> None:
    assert gl._command_seconds(30000) == 30.0
    for value in (None, 0, -5, True, float("nan"), float("inf"), "30000", 10**400, -(10**400)):
        assert gl._command_seconds(value) == gl.COMMAND_TIMEOUT_MAX_MS / 1000


# Review fix round 1, CX5 (Codex): numbers a JSON decoder accepts but a float cannot hold.
def _ledger_with(root: Path, lines: list[bytes]) -> Any:
    folder = gl.ledger_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / gl.LEDGER_NAME).write_bytes(b"".join(line + b"\n" for line in lines))
    return gl.entries(root)


def test_cx5_an_out_of_range_integer_time_is_one_unusable_line(repo: SimpleNamespace) -> None:
    """Codex's reproduction: a complete entry with start=end=10**400 (valid JSON) is classified, not raised."""
    good = {"start": 1, "end": 2, "session": "pm", "transitions": []}
    huge = json.dumps({"start": 10**400, "end": 10**400, "session": "pm", "transitions": []}).encode()
    assert gl.entry_ok({"start": 10**400, "end": 10**400, "transitions": []}) is False
    found = _ledger_with(repo.main, [json.dumps(good).encode(), huge, json.dumps(good).encode()])
    first = len(json.dumps(good)) + 1
    assert list(found) == [good, good] and found.malformed == [first]
    assert max(found.prefixes) == 2 * first + len(huge) + 1  # the integrity evidence covers every line
    record = json.dumps({"start": 10**400, "deadline": 10**400, "session": "s", "tool_use_id": "t", "cwd": ".",
                         "command": "", "before": {}}).encode()
    assert isinstance(gl._pending_record(record), str)
    deep = b"[" * 200_000 + b"]" * 200_000  # experiment: the 3.12 C decoder raises RecursionError here, not ValueError
    found = _ledger_with(repo.main, [json.dumps(good).encode(), deep])
    assert list(found) == [good] and found.malformed == [first]
    assert isinstance(gl._pending_record(deep), str)


def test_cx5_an_unusable_pending_time_is_named_not_raised(repo: SimpleNamespace) -> None:
    gl.begin(repo.main, _event("pm", "toolu_huge", repo.main), repo.main, 10)
    [path] = _records(repo)
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "deadline": 10**400}), encoding="utf-8")
    now = time.time()
    assert gl.settle(repo.main, 0, now + 10, 5) == []
    assert gl.incomplete(repo.main, 0, now + 10) and gl.entries(repo.main).incomplete


# Boundary numbers, as text a JSON decoder accepts: huge integers, the float limits, NaN and infinities (Python's
# json writes and reads them), booleans and non-numbers.
BOUNDARY = st.one_of(
    st.integers(),
    st.sampled_from([2**1023, 2**1024, 10**308, 10**309, -(10**400), 10**400, 0, -1]),
    st.floats(allow_nan=True, allow_infinity=True),
    st.sampled_from([1.7976931348623157e308, 5e-324, -0.0]),
    st.booleans(),
    st.none(),
    st.text(max_size=4),
)


@given(st.lists(st.tuples(BOUNDARY, BOUNDARY), min_size=1, max_size=6))
def test_property_boundary_times_through_json_never_raise(times: list[tuple[Any, Any]]) -> None:
    import tempfile

    lines = [json.dumps({"start": s, "end": e, "session": "pm", "transitions": [["ref x", None, "v"]]}).encode()
             for s, e in times]
    with tempfile.TemporaryDirectory(dir=Path(gl.__file__).resolve().parents[3] / ".tmp") as tmp:
        old = os.environ.get("AGENT_WORKFLOW_STATE_DIR")
        os.environ["AGENT_WORKFLOW_STATE_DIR"] = tmp
        try:
            found = _ledger_with(Path(tmp), lines)
        finally:
            if old is None:
                os.environ.pop("AGENT_WORKFLOW_STATE_DIR")
            else:
                os.environ["AGENT_WORKFLOW_STATE_DIR"] = old
    decoded = [json.loads(line) for line in lines]  # the very objects the reader saw
    usable = [d for d in decoded if gl._real(d["start"]) is not None and gl._real(d["end"]) is not None
              and d["end"] >= d["start"]]
    assert list(found) == usable and len(found) + len(found.malformed) == len(lines)
    assert max(found.prefixes) == sum(len(line) + 1 for line in lines)
    for start, end in times:
        record = json.dumps({"start": start, "deadline": end, "session": "s", "tool_use_id": "t", "cwd": ".",
                             "command": "", "before": {}}).encode()
        assert isinstance(gl._pending_record(record), (dict, str))
        seconds = gl._command_seconds(json.loads(json.dumps(start)))  # a sub-ms timeout may underflow to 0.0 s
        assert isinstance(seconds, float) and seconds >= 0 and seconds == seconds and seconds != float("inf")
