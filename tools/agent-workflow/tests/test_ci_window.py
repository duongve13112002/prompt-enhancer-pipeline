"""ci_window.py (T-022, docs/ci.md): preconditions refuse, and every exit path returns the repository as found.

Labelled test doubles, no network:
- `FakeGh`, an in-process stand-in for the `gh` CLI (visibility, dispatch, run list/view/cancel), injected as the
  runner; `tests/fake_gh.py` stands in for the `gh` executable where the real child-process path is tested;
- the secret scanner double, a tiny script at `<tmp>/scan.py` run by this interpreter;
- the remote, a temporary bare repository reached through `url.<bare>.insteadOf` for a github-shaped URL.
Clocks and sleeps are injected, so time limits are reached without waiting.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest
from conftest import git
from hypothesis import assume, given
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_window as cw

HERE = Path(__file__).resolve().parent
GUARD = gc.load()
URL = "https://github.com/owner/repo.git"
SLUG = "github.com/owner/repo"
PIN = "0123456789abcdef0123456789abcdef01234567"
WORKFLOW = f"on:\n  workflow_dispatch:\njobs:\n  t:\n    steps:\n      - uses: actions/checkout@{PIN} # v0\n"


def commit(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"add {rel}")


SECRET = "T022-FAKE-SECRET-" + "q7Zx9wVb"  # assembled, so no scanner flags this test file itself
FINDING_COMMIT = "c0ffee" + "0" * 34
BASELINE = "gitleaks-baseline.json"
EMPTY_BASELINE = b"[]\n"


def finding(rule: str = "generic-api-key", line: int = 3) -> dict[str, Any]:
    """A gitleaks-shaped raw finding holding the fake secret in the fields the tool must never show."""
    return {"RuleID": rule, "File": "cfg/app.env", "StartLine": line, "Commit": FINDING_COMMIT,
            "Date": "2026-11-15T00:00:00Z", "Secret": SECRET, "Match": f"key={SECRET}", "Line": f"key={SECRET}"}  # fmt: skip


def entry(row: dict[str, Any]) -> dict[str, Any]:
    keep = {k: row[k] for k in ("RuleID", "File", "StartLine", "Commit")}
    return {**keep, "Fingerprint": cw.fingerprint(row), "Reason": "test data", "Decision": "test"}


def scanner(tmp: Path, code: int | None = None, rows: list[dict[str, Any]] | None = None,
            broken: bool = False, version: str = "double-1", prints: bytes | None = None,
            version_code: int = 0, version_sleep: float = 0) -> dict[str, Any]:  # fmt: skip
    """Labelled double of gitleaks: records the commits it was given, writes `rows` as the JSON report after
    --report-path, echoes the fake secret on stdout and stderr, and exits 1 when it reports a row (or `code`).
    `broken` drops StartLine from every row. Asked `version`, it prints `prints` (default: `version` and a line end)
    after `version_sleep` seconds, echoes the fake secret on stderr, and exits `version_code`; the entry is configured
    with `version`."""
    rows = [dict(r) for r in rows or []]
    for r in rows:
        if broken:
            del r["StartLine"]
    out = (version + "\n").encode() if prints is None else prints
    key = (code, json.dumps(rows), broken, out, version_code, version_sleep)
    script = tmp / f"scan-{abs(hash(key))}.py"  # one script per behaviour
    script.write_text(
        "import json, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "if args == ['version']:\n"
        f"    time.sleep({version_sleep!r}); sys.stdout.buffer.write({out!r}); sys.stderr.write({SECRET!r})\n"
        f"    raise SystemExit({version_code!r})\n"
        f"pathlib.Path({str(tmp / 'scanned.txt')!r}).write_text(args[0].removeprefix('--revs='))\n"
        "assert '--redact' in args and args[args.index('--report-format') + 1] == 'json'\n"
        f"rows = {rows!r}\n"
        "open(args[args.index('--report-path') + 1], 'w').write(json.dumps(rows))\n"
        f"print('log ' + {SECRET!r}); sys.stderr.write({SECRET!r})\n"
        f"raise SystemExit({code!r} if {code!r} is not None else (1 if rows else 0))\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    exe = Path(sys.executable).name  # relative to PYROOT, the "project root" of the double
    # `--revs={revs}` exercises the token inside an argument, as the shipped `--log-opts={revs}` does.
    return {"executable": exe, "version": version, "version_args": [str(script), "version"], "sha256": digest,
            "args": [str(script), "--revs={revs}"]}  # fmt: skip


PYROOT = Path(sys.executable).parent


def config(tmp: Path, baseline: bytes = EMPTY_BASELINE, **over: Any) -> dict[str, Any]:
    raw = json.loads((HERE.parent / "ci_window_config.json").read_text(encoding="utf-8"))
    raw["secret_scanner"] = {sys.platform: scanner(tmp)}
    raw["scanner_baseline"] = {"path": BASELINE, "sha256": hashlib.sha256(baseline).hexdigest()}
    raw.update(over)
    cfg = cw.validate(raw, PYROOT)
    # The raw reports of the doubles and the run ledger stay in the test's tmp, which is their project root here.
    cfg["root"] = tmp.resolve()
    cfg["paths"]["findings_raw_dir"] = cfg["root"] / "raw"
    cfg["paths"]["run_ledger"] = cfg["root"] / "ledger.json"
    return cfg


# What a clean repository answers to the inventory reads (fix round 2): every flag off, nothing listed.
CLEAN = {"repo": {"has_wiki": False, "has_discussions": False, "has_pages": False}, "releases": [], "issues": [],
         "runs": [], "artifacts": []}  # fmt: skip


def set_baseline(repo: Path, entries: list[dict[str, Any]]) -> bytes:
    data = (json.dumps(entries, indent=2) + "\n").encode()
    (repo / BASELINE).write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    push(repo)
    return data


@pytest.fixture
def world(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "remote", "add", "origin", URL)
    git(repo, "config", f"url.{bare.as_uri()}.insteadOf", URL)
    commit(repo, "README.md", "base\n")
    commit(repo, ".github/workflows/ci.yml", WORKFLOW)
    commit(repo, ".secrets/README.md", "placeholder\n")
    commit(repo, BASELINE, EMPTY_BASELINE.decode())
    git(repo, "push", "-q", "origin", "main")
    return repo


def push(repo: Path) -> None:
    git(repo, "push", "-q", "origin", "main")


class FakeGh:
    """Labelled test double of the `gh` CLI, in process. `script` maps an event name to a callable or value."""

    def __init__(self, visibility: str = "private", **script: Any) -> None:
        self.visibility = visibility
        self.script = script
        self.calls: list[list[str]] = []
        self.views = list(script.get("views", [("completed", "success")]))
        self.listed = 0
        self.head = script.get("head")

    def done(self, out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, out, err)

    def __call__(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        args = argv[1:]
        self.calls.append(args)
        if args[0] == "api":
            hook = self.script.get("api")  # callable(self, path) -> CompletedProcess | None
            result = hook(self, args[3]) if callable(hook) else None
            return result if result is not None else self.api(args[3])
        hook = self.script.get(" ".join(args[:2]))
        if callable(hook):
            result = hook(self, args)
            if result is not None:
                return result  # type: ignore[no-any-return]
        if args[:2] == ["repo", "view"]:
            return self.done(json.dumps({"visibility": self.visibility.upper()}))
        if args[:2] == ["repo", "edit"]:
            if not self.script.get("sticky"):
                self.visibility = args[args.index("--visibility") + 1]
            return self.done()
        if args[:2] == ["workflow", "run"]:
            return self.done(self.script.get("printed", "https://github.com/owner/repo/actions/runs/77\n"))
        if args[:2] == ["run", "list"]:
            self.listed += 1
            ids = self.script.get("lists", [[]])
            return self.done(json.dumps([{"databaseId": i} for i in ids[min(self.listed - 1, len(ids) - 1)]]))
        if args[:2] == ["run", "view"]:
            status, conclusion = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            head = self.head or self.script["sha"]
            event = self.script.get("event", "workflow_dispatch")
            data = {"headSha": head, "event": event, "status": status, "conclusion": conclusion, "url": "u"}
            return self.done(json.dumps(data))
        if args[:2] == ["run", "cancel"]:
            return self.done()
        raise AssertionError(f"unexpected gh call {args}")

    def api(self, path: str) -> subprocess.CompletedProcess[str]:
        """`gh api --hostname <host> repos/<owner>/<repo><tail>`: the inventory reads, from `script["inventory"]`
        over CLEAN; the runs and artifacts lists are served page by page, `<key>_total` overrides the total."""
        from urllib.parse import parse_qsl

        assert path.startswith("repos/owner/repo"), path
        inv = {**CLEAN, **self.script.get("inventory", {})}
        base, _, query = path.removeprefix("repos/owner/repo").lstrip("/").partition("?")
        params = dict(parse_qsl(query))
        if base == "":
            return self.done(json.dumps(inv["repo"]))
        if base in ("releases", "issues"):
            return self.done(json.dumps(inv[base]))
        key = {"actions/runs": "runs", "actions/artifacts": "artifacts"}[base]
        items, per, page = inv[key], int(params["per_page"]), int(params["page"])
        field = "workflow_runs" if key == "runs" else "artifacts"
        body = {"total_count": inv.get(f"{key}_total", len(items)), field: items[(page - 1) * per : page * per]}
        return self.done(json.dumps(body))

    def edits(self) -> list[str]:
        return [c[c.index("--visibility") + 1] for c in self.calls if c[:2] == ["repo", "edit"]]

    def names(self) -> list[str]:
        return [" ".join(c[:2]) for c in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def window(repo: Path, cfg: dict[str, Any], fake: FakeGh, clock: Clock | None = None) -> tuple[int, dict[str, Any]]:
    clock = clock or Clock()
    fake.script.setdefault("sha", git(repo, "rev-parse", "HEAD").strip())
    return cw.open_window(repo, cfg, GUARD, fake, clock, clock.sleep)


# --- green, red and every abnormal exit return to private ------------------------------------------------------


def test_green_run_opens_dispatches_and_returns_private(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(views=[("queued", None), ("in_progress", None), ("completed", "success")])
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_OK and report["result"] == "success"
    assert fake.edits() == ["public", "private"] and fake.visibility == "private"
    assert report["initial_visibility"] == "private" and report["final_visibility"] == "private"
    assert report["run"]["id"] == 77 and report["repository"] == SLUG
    order = fake.names()
    assert order.index("repo edit") < order.index("workflow run") < order.index("run view")
    dispatch = next(c for c in fake.calls if c[:2] == ["workflow", "run"])
    assert dispatch == ["workflow", "run", "ci.yml", "--ref", "main", "-R", SLUG]
    assert "--accept-visibility-change-consequences" in next(c for c in fake.calls if c[:2] == ["repo", "edit"])
    scanned = (tmp_path / "scanned.txt").read_text()
    assert report["commit"] in scanned.split()


def test_red_run_returns_private(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(views=[("completed", "failure")])
    code, report = window(world, config(tmp_path), fake)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "failure", "private")


def test_timeout_returns_private_then_cancels(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(views=[("in_progress", None)])
    clock = Clock()
    code, report = window(world, config(tmp_path, run_timeout_seconds=60, poll_interval_seconds=7), fake, clock)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "timeout", "private")
    order = fake.names()
    assert order[-3:] == ["repo edit", "repo view", "run cancel"] and fake.edits()[-1] == "private"
    assert report["run"]["cancelled"] is True
    assert set(clock.sleeps) == {7} and clock.now >= 60  # override of the poll interval and the time limit


def test_dispatch_failure_returns_private(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(**{"workflow run": lambda self, a: self.done(code=1, err="HTTP 422")})
    code, report = window(world, config(tmp_path), fake)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "error", "private")
    assert "HTTP 422" in report["errors"][0]


def test_raised_exception_returns_private(world: Path, tmp_path: Path) -> None:
    def boom(self: FakeGh, args: list[str]) -> None:
        raise RuntimeError("runner broke")

    fake = FakeGh(**{"run view": boom})
    code, report = window(world, config(tmp_path), fake)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "error", "private")
    assert "RuntimeError" in report["errors"][0]


def test_keyboard_interrupt_returns_private(world: Path, tmp_path: Path) -> None:
    clock = Clock()

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    fake = FakeGh(views=[("in_progress", None)], sha=git(world, "rev-parse", "HEAD").strip())
    code, report = cw.open_window(world, config(tmp_path), GUARD, fake, clock, interrupt)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "interrupted", "private")


def test_sigterm_is_recorded_and_returns_private_then_cancels(world: Path, tmp_path: Path) -> None:
    clock = Clock()

    def terminate(seconds: float) -> None:
        clock.sleep(seconds)  # time passes, so code that ignores the signal ends at the time limit, not in a hang
        signal.raise_signal(signal.SIGTERM)  # a real signal; the window's handler only records it

    fake = FakeGh(views=[("in_progress", None)], sha=git(world, "rev-parse", "HEAD").strip())
    before = signal.getsignal(signal.SIGTERM)
    code, report = cw.open_window(world, config(tmp_path), GUARD, fake, clock, terminate)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "interrupted", "private")
    assert report["signals"] == ["SIGTERM"] and "stopped before the next poll" in report["errors"][0]
    assert fake.names()[-3:] == ["repo edit", "repo view", "run cancel"] and report["run"]["cancelled"] is True
    assert signal.getsignal(signal.SIGTERM) is before  # handlers are put back


def test_a_failed_cancel_after_an_interrupt_is_reported_and_keeps_the_exit(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(views=[("in_progress", None)], sha=git(world, "rev-parse", "HEAD").strip(),
                  **{"run cancel": lambda self, a: self.done(code=1, err="HTTP 500")})  # fmt: skip
    clock = Clock()
    code, report = cw.open_window(world, config(tmp_path), GUARD, fake, clock,
                                  lambda s: (clock.sleep(s), signal.raise_signal(signal.SIGINT)))  # fmt: skip
    assert (code, fake.visibility) == (cw.EXIT_FAILED, "private")
    assert any(e.startswith("cancel after interrupted:") and "HTTP 500" in e for e in report["errors"])


def test_no_cancel_before_the_return_is_verified(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(sticky=True, views=[("in_progress", None)], sha=git(world, "rev-parse", "HEAD").strip())
    fake.script["repo edit"] = lambda self, a: setattr(self, "visibility", "public")
    clock = Clock()
    code, report = cw.open_window(world, config(tmp_path, restore_attempts=1), GUARD, fake, clock,
                                  lambda s: (clock.sleep(s), signal.raise_signal(signal.SIGINT)))  # fmt: skip
    assert code == cw.EXIT_EXPOSED and "run cancel" not in fake.names()


def test_signals_are_recorded_while_returning(world: Path, tmp_path: Path) -> None:
    seen: list[Any] = []

    def edit(self: FakeGh, args: list[str]) -> None:
        if "private" in args:
            seen.append(signal.getsignal(signal.SIGINT))
            signal.raise_signal(signal.SIGINT)  # pressed during the return: recorded, the return goes on

    fake = FakeGh(**{"repo edit": edit})
    code, report = window(world, config(tmp_path), fake)
    assert isinstance(getattr(seen[0], "__self__", None), cw.Deferred)
    assert (code, fake.visibility, report["signals"]) == (cw.EXIT_OK, "private", ["SIGINT"])


def test_switch_to_public_that_times_out_still_returns(world: Path, tmp_path: Path) -> None:
    def edit(self: FakeGh, args: list[str]) -> None:
        if "public" in args:
            self.visibility = "public"  # the change happened, but the answer never came
            raise subprocess.TimeoutExpired(args, 60)

    fake = FakeGh(**{"repo edit": edit})
    code, report = window(world, config(tmp_path), fake)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "error", "private")
    assert report["made_public"] is False and fake.edits()[-1] == "private"


def test_failed_final_verification_is_loud(
    world: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # main() resolves the scanner against its root (tmp_path here), so the scan itself is a labelled double.
    monkeypatch.setattr(cw, "check_secrets", lambda *a, **k: ("scan double: clean", []))
    cfg_path = tmp_path / "ci.json"
    raw = json.loads((HERE.parent / "ci_window_config.json").read_text(encoding="utf-8"))
    raw.update(report_dir="out/reports", restore_attempts=2)
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    fake = FakeGh(sticky=True, sha=git(world, "rev-parse", "HEAD").strip())

    def edit(self: FakeGh, args: list[str]) -> None:
        self.visibility = "public"  # the switch to public works; the return never takes effect

    fake.script["repo edit"] = edit
    code = cw.main(["run", "--repo", str(world), "--config", str(cfg_path)], root=tmp_path, runner=fake)
    err = capsys.readouterr().err
    assert code == cw.EXIT_EXPOSED and fake.visibility == "public"
    assert fake.edits() == ["public", "private", "private"]  # restore_attempts = 2 (non-default)
    assert "REPOSITORY MAY BE PUBLIC" in err and "--visibility private" in err
    reports = list((tmp_path / "out" / "reports").glob("*.json"))  # report_dir override
    assert len(reports) == 1 and json.loads(reports[0].read_text())["final_visibility"] == "public"


def test_unreadable_final_visibility_counts_as_exposed(world: Path, tmp_path: Path) -> None:
    def view(self: FakeGh, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        if self.edits():
            return self.done("not json")
        return None

    code, report = window(world, config(tmp_path, restore_attempts=1), FakeGh(**{"repo view": view}))
    assert code == cw.EXIT_EXPOSED and report["final_visibility"] is None


def test_already_public_start_is_left_public_and_warned(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(visibility="public")
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_OK and fake.edits() == [] and report["final_visibility"] == "public"
    assert "ALREADY PUBLIC" in report["warnings"][0] and "S35" in report["warnings"][0]


def test_internal_repository_is_not_touched(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(visibility="internal")
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and fake.edits() == [] and "workflow run" not in fake.names()


def test_unparsable_visibility_never_opens(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(**{"repo view": lambda self, a: self.done("{}")})
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and fake.edits() == [] and "no known visibility" in report["errors"][0]


# --- tying the run to the commit -------------------------------------------------------------------------------


def test_run_found_by_list_when_no_url_is_printed(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(printed="", lists=[[5], [5], [5, 9]])
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_OK and report["run"]["id"] == 9
    listing = next(c for c in fake.calls if c[:2] == ["run", "list"])
    assert listing[listing.index("--commit") + 1] == report["commit"]
    assert listing[listing.index("--event") + 1] == "workflow_dispatch"


def test_two_new_runs_are_not_guessed(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(printed="", lists=[[], [3, 4]])
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and "cannot tell" in report["errors"][0] and fake.visibility == "private"


def test_no_run_appearing_times_out(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(printed="")
    code, report = window(world, config(tmp_path, dispatch_lookup_timeout_seconds=5), fake)
    assert code == cw.EXIT_FAILED and "within 5 s" in report["errors"][0] and fake.visibility == "private"


NOT_FOUND_ERR = "failed to get run: HTTP 404: Not Found (https://api.github.com/repos/owner/repo/actions/runs/77)"


def view_failures(errors: list[str]) -> Any:
    """A `run view` hook that fails with each stderr in `errors` in turn, then serves the scripted views."""
    pending = list(errors)

    def hook(fake: FakeGh, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        return fake.done(code=1, err=pending.pop(0)) if pending else None

    return hook


def test_a_run_unknown_right_after_dispatch_is_viewed_again(world: Path, tmp_path: Path) -> None:
    """CI 35: the first `gh run view` after dispatch answered HTTP 404; the window now waits within the dispatch
    lookup window and watches the run to its end."""
    clock = Clock()
    fake = FakeGh(views=[("in_progress", None), ("completed", "success")],
                  **{"run view": view_failures([NOT_FOUND_ERR, NOT_FOUND_ERR])})  # fmt: skip
    code, report = window(world, config(tmp_path), fake, clock)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_OK, "success", "private")
    assert fake.names().count("run view") == 4 and report["errors"] == []


def test_a_run_unknown_past_the_lookup_window_fails_and_returns_private(world: Path, tmp_path: Path) -> None:
    fake = FakeGh(**{"run view": view_failures([NOT_FOUND_ERR] * 1000)})
    code, report = window(world, config(tmp_path, dispatch_lookup_timeout_seconds=5), fake)
    assert code == cw.EXIT_FAILED and "HTTP 404" in report["errors"][0] and fake.visibility == "private"
    assert fake.names().count("run view") < 1000  # bounded by the window, not by the script


@pytest.mark.parametrize("err", ["HTTP 500: Internal Server Error", "HTTP 403: Forbidden", "could not resolve host"])
def test_any_other_view_failure_is_not_retried(world: Path, tmp_path: Path, err: str) -> None:
    fake = FakeGh(**{"run view": view_failures([err])})
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and err in report["errors"][0] and fake.visibility == "private"
    assert fake.names().count("run view") == 1


def test_a_404_after_the_run_was_seen_is_not_retried(world: Path, tmp_path: Path) -> None:
    served: list[str] = []

    def hook(fake: FakeGh, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        served.append("view")
        return fake.done(code=1, err=NOT_FOUND_ERR) if len(served) == 2 else None

    fake = FakeGh(views=[("in_progress", None), ("in_progress", None)], **{"run view": hook})
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and "HTTP 404" in report["errors"][0] and fake.visibility == "private"
    assert len(served) == 2


@pytest.mark.parametrize(("err", "status"), [(NOT_FOUND_ERR, 404), ("HTTP 502: Bad Gateway", 502), ("boom", None)])
def test_gh_failures_carry_the_printed_http_status(err: str, status: int | None) -> None:
    gh = cw.Gh(["gh"], SLUG, 5, lambda argv, timeout: subprocess.CompletedProcess(argv, 1, "", err))
    with pytest.raises(cw.WindowError) as caught:
        gh.run_view(77)
    assert caught.value.http_status == status


@pytest.mark.parametrize("field", ["head", "event"])
def test_run_on_another_commit_or_event_fails(world: Path, tmp_path: Path, field: str) -> None:
    fake = FakeGh(head="f" * 40) if field == "head" else FakeGh(event="push")
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and "not workflow_dispatch on" in report["errors"][0]
    assert fake.visibility == "private"


def test_remote_moving_before_dispatch_fails(world: Path, tmp_path: Path) -> None:
    def edit(self: FakeGh, args: list[str]) -> None:
        if "public" in args:
            commit(world, "late.txt")
            push(world)

    fake = FakeGh(**{"repo edit": edit})
    code, report = window(world, config(tmp_path), fake)
    assert code == cw.EXIT_FAILED and "moved" in report["errors"][0] and "workflow run" not in fake.names()


def test_workflow_file_override_reaches_gh(world: Path, tmp_path: Path) -> None:
    commit(world, ".github/workflows/other.yml", WORKFLOW)
    push(world)
    fake = FakeGh()
    code, _ = window(world, config(tmp_path, workflow_file="other.yml"), fake)
    assert code == cw.EXIT_OK
    assert next(c for c in fake.calls if c[:2] == ["workflow", "run"])[2] == "other.yml"


# --- preconditions refuse with nothing changed ------------------------------------------------------------------


def refused(repo: Path, cfg: dict[str, Any]) -> str:
    fake = FakeGh()
    code, report = window(repo, cfg, fake)
    assert code == cw.EXIT_REFUSED and fake.calls == [] and report["result"] == "refused"
    return str(report["errors"][0])


def test_dirty_tree_is_refused(world: Path, tmp_path: Path) -> None:
    (world / "README.md").write_text("changed\n", encoding="utf-8")
    assert "not clean" in refused(world, config(tmp_path))


def test_untracked_file_is_refused(world: Path, tmp_path: Path) -> None:
    (world / "new.txt").write_text("x\n", encoding="utf-8")
    assert "new.txt" in refused(world, config(tmp_path))


def test_unpushed_head_is_refused(world: Path, tmp_path: Path) -> None:
    commit(world, "local.txt")
    assert "push it first" in refused(world, config(tmp_path))


def test_head_on_another_branch_is_refused(world: Path, tmp_path: Path) -> None:
    git(world, "checkout", "-q", "-b", "topic")
    assert "not refs/heads/main" in refused(world, config(tmp_path))


def test_forbidden_path_anywhere_in_history_is_refused(world: Path, tmp_path: Path) -> None:
    commit(world, "models/weights.txt")
    git(world, "rm", "-q", "models/weights.txt")
    git(world, "commit", "-q", "-m", "drop")
    push(world)
    assert "models/weights.txt" in refused(world, config(tmp_path))


@pytest.mark.parametrize("rel", ["runs/r1/report.json", "data/x.txt", "tools/w.gguf", "Models/x.txt"])
def test_forbidden_prefixes_and_suffixes_are_refused(world: Path, tmp_path: Path, rel: str) -> None:
    commit(world, rel)
    push(world)
    assert rel in refused(world, config(tmp_path))


def test_extra_prefix_override(world: Path, tmp_path: Path) -> None:
    commit(world, "evidence/a.txt")
    push(world)
    assert "evidence/a.txt" in refused(world, config(tmp_path, extra_forbidden_prefixes=["evidence"]))
    assert window(world, config(tmp_path), FakeGh())[0] == cw.EXIT_OK


def test_allowlist_is_exact(world: Path, tmp_path: Path) -> None:
    assert window(world, config(tmp_path), FakeGh())[0] == cw.EXIT_OK  # .secrets/README.md is allowlisted
    assert ".secrets/README.md" in refused(world, config(tmp_path, allowed_tracked_paths=[]))


def test_remote_ref_without_local_commit_is_refused(world: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(other))
    commit(other, "elsewhere.txt")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/elsewhere")
    assert "not local" in refused(world, config(tmp_path))


def test_every_remote_ref_is_scanned(world: Path, tmp_path: Path) -> None:
    git(world, "push", "-q", "origin", "HEAD~1:refs/heads/old")
    window(world, config(tmp_path), FakeGh())
    scanned = (tmp_path / "scanned.txt").read_text().split()
    assert git(world, "rev-parse", "HEAD~1").strip() in scanned and len(scanned) == 2


def test_secret_scan_hit_is_refused(world: Path, tmp_path: Path) -> None:
    message = refused(world, config(tmp_path, secret_scanner={sys.platform: scanner(tmp_path, rows=[finding()])}))
    assert "1 finding(s) outside the baseline" in message and SECRET not in message


def test_scanner_exit_that_contradicts_its_report_is_refused(world: Path, tmp_path: Path) -> None:
    assert "does not match" in refused(world, config(tmp_path, secret_scanner={sys.platform: scanner(tmp_path, code=1)}))


# --- the baseline (PM decision pending user review, decision 94) -----------------------------------------------


def baseline_cfg(world: Path, tmp_path: Path, rows: list[dict[str, Any]],
                 entries: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    data = set_baseline(world, entries)
    return config(tmp_path, baseline=data, secret_scanner={sys.platform: scanner(tmp_path, rows=rows)})


def test_findings_in_the_baseline_pass(world: Path, tmp_path: Path) -> None:
    rows = [finding("generic-api-key", 3), finding("private-key", 9)]
    code, report = window(world, baseline_cfg(world, tmp_path, rows, [entry(r) for r in rows]), FakeGh())
    assert code == cw.EXIT_OK and "2 finding(s) all in the baseline of 2" in report["preconditions"]["scan"]
    assert report["warnings"] == []


def test_a_new_finding_outside_the_baseline_refuses(world: Path, tmp_path: Path) -> None:
    rows = [finding("generic-api-key", 3), finding("private-key", 9)]
    message = refused(world, baseline_cfg(world, tmp_path, rows, [entry(rows[0])]))
    assert "1 finding(s) outside the baseline" in message and cw.fingerprint(rows[1]) in message


def test_a_changed_baseline_file_refuses_on_its_hash(world: Path, tmp_path: Path) -> None:
    rows = [finding()]
    pinned = set_baseline(world, [entry(rows[0])])
    widened = [entry(rows[0]), entry(finding("private-key", 9))]
    set_baseline(world, widened)  # one more entry, committed and pushed, but the pin still names the old file
    cfg = config(tmp_path, baseline=pinned, secret_scanner={sys.platform: scanner(tmp_path, rows=rows)})
    assert "not the pinned" in refused(world, cfg)


def test_an_uncommitted_baseline_edit_refuses(world: Path, tmp_path: Path) -> None:
    rows = [finding()]
    cfg = baseline_cfg(world, tmp_path, rows, [entry(rows[0])])
    (world / BASELINE).write_text("[]\n", encoding="utf-8")
    with pytest.raises(cw.Refusal, match="differs from HEAD"):
        cw.scan(cfg["secret_scanner"], cfg["scanner_baseline"], world, ["a" * 40], 60, tmp_path / "raw",
                root=tmp_path)  # fmt: skip


def test_a_stale_baseline_entry_warns_and_passes(world: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = [finding()]
    gone = entry(finding("private-key", 9))
    cfg = baseline_cfg(world, tmp_path, rows, [entry(rows[0]), gone])
    code, report = window(world, cfg, FakeGh())
    assert code == cw.EXIT_OK and report["warnings"] == [
        f"scanner baseline entry no longer found (review and remove it): {gone['Fingerprint']}"
    ]
    assert cw.findings_main(world, None, cfg, GUARD, tmp_path) == cw.EXIT_OK
    assert gone["Fingerprint"] in capsys.readouterr().err


def test_a_baseline_entry_matches_its_exact_fingerprint_only(world: Path, tmp_path: Path) -> None:
    near = [finding("generic-api-key", 4)]  # same commit, file and rule, another line
    message = refused(world, baseline_cfg(world, tmp_path, near, [entry(finding("generic-api-key", 3))]))
    assert cw.fingerprint(near[0]) in message


def test_the_scanner_is_never_given_a_baseline_path(world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    real = cw.subprocess.run

    def spy(argv: list[str], **kw: Any) -> Any:
        seen.append(list(argv))
        return real(argv, **kw)

    monkeypatch.setattr(cw.subprocess, "run", spy)
    rows = [finding()]
    window(world, baseline_cfg(world, tmp_path, rows, [entry(rows[0])]), FakeGh())
    scans = [a for a in seen if "--report-path" in a]
    assert len(scans) == 1 and "--baseline-path" not in scans[0]


@pytest.mark.parametrize(
    "bad",
    [
        {"Reason": ""},
        {"Decision": None},
        {"Fingerprint": "x:y:z:1"},
        {"Commit": "abc"},
        {"Extra": 1},
    ],
)
def test_malformed_baseline_entries_refuse(world: Path, tmp_path: Path, bad: dict[str, Any]) -> None:
    rows = [finding()]
    item = {**entry(rows[0]), **bad}
    cfg = baseline_cfg(world, tmp_path, rows, [item])
    with pytest.raises(cw.Refusal, match="baseline entry 0"):
        cw.scan(cfg["secret_scanner"], cfg["scanner_baseline"], world, ["a" * 40], 60, tmp_path / "raw",
                root=tmp_path)  # fmt: skip


def test_shipped_baseline_holds_exactly_the_reviewed_findings() -> None:
    cfg = cw.load(cw.DEFAULT_CONFIG)
    root = gc.ROOT
    data = json.loads((root / cfg["scanner_baseline"]["path"]).read_text(encoding="utf-8"))
    assert sorted(e["Fingerprint"] for e in data) == [
        "0c93c6d4a8f206117679607432a041f65e849486:tests/test_k2_observe_shared_keys.py:generic-api-key:84",
        "1adb1eda51b41666aa4d5e17ba9ad127c8adccca:tools/agent-workflow/tests/test_recall.py:generic-api-key:169",
        "1adb1eda51b41666aa4d5e17ba9ad127c8adccca:tools/agent-workflow/tests/test_recall.py:private-key:180",
        "4f370d1e002664e20ee5b2c3c0cd153a700d64da:tasks/STATUS.md:generic-api-key:39",
        "4f370d1e002664e20ee5b2c3c0cd153a700d64da:tools/agent-workflow/gitleaks-baseline.json:generic-api-key:35",
        "e9b2781a19fffcc6a7c53c3fab298d572e4ff5d3:docs/ci.md:generic-api-key:197",
    ]
    assert all(", not a secret:" in e["Reason"] and "decision 94" in e["Decision"] for e in data)
    raw = (root / cfg["scanner_baseline"]["path"]).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == cfg["scanner_baseline"]["sha256"]  # the pin matches the LF blob


def test_scanner_sentinel_for_this_platform_is_refused_with_the_step(world: Path, tmp_path: Path) -> None:
    message = refused(world, config(tmp_path, secret_scanner={sys.platform: "pin_required", "other": scanner(tmp_path)}))
    assert f"secret_scanner.{sys.platform} is pin_required" in message and "release checksums" in message


def test_platform_without_an_entry_is_refused(world: Path, tmp_path: Path) -> None:
    message = refused(world, config(tmp_path, secret_scanner={"otheros": scanner(tmp_path)}))
    assert f"secret_scanner.{sys.platform} has no entry" in message


def test_scanner_entry_is_chosen_by_platform(world: Path, tmp_path: Path) -> None:
    cfg = config(tmp_path, secret_scanner={"plan9": scanner(tmp_path), sys.platform: "pin_required"})
    args = (cfg["secret_scanner"], cfg["scanner_baseline"], world, ["a" * 40], 60, tmp_path / "raw")
    assert "clean" in cw.check_secrets(*args, platform="plan9", root=tmp_path)[0]
    with pytest.raises(cw.Refusal, match="pin_required"):
        cw.check_secrets(*args, root=tmp_path)


def test_scanner_with_wrong_hash_is_refused(world: Path, tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg["secret_scanner"][sys.platform]["sha256"] = "0" * 64
    assert "SHA-256" in refused(world, cfg)


def test_uppercase_hash_is_accepted(world: Path, tmp_path: Path) -> None:
    entry = scanner(tmp_path)
    entry["sha256"] = entry["sha256"].upper()  # the release checksum files print either case
    assert window(world, config(tmp_path, secret_scanner={sys.platform: entry}), FakeGh())[0] == cw.EXIT_OK


def test_missing_scanner_is_refused(world: Path, tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg["secret_scanner"][sys.platform]["path"] = str(tmp_path / "absent-scanner.exe")
    assert "cannot be read" in refused(world, cfg)


@pytest.mark.parametrize("name", [".gitleaks.toml", ".gitleaksignore"])
def test_tracked_scanner_override_file_is_refused(world: Path, tmp_path: Path, name: str) -> None:
    commit(world, name, "[extend]\n")
    push(world)
    message = refused(world, config(tmp_path, allowed_tracked_paths=[".secrets/README.md"]))
    assert name in message and "would change" in message


@pytest.mark.parametrize("var", ["GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"])
def test_scanner_override_env_is_refused(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, "x")
    assert var in refused(world, config(tmp_path))


def test_findings_keep_only_the_five_fields_and_never_the_secret(
    world: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(tmp_path, secret_scanner={sys.platform: scanner(tmp_path, rows=[finding()])})
    code = cw.findings_main(world, None, cfg, GUARD, tmp_path)
    out = capsys.readouterr()
    assert code == cw.EXIT_FAILED
    assert SECRET not in out.out and SECRET not in out.err
    assert "generic-api-key" in out.out and "cfg/app.env" in out.out and FINDING_COMMIT[:12] in out.out
    (kept,) = (tmp_path / cfg["findings_dir"]).glob("findings-*.json")
    text = kept.read_text(encoding="utf-8")
    assert SECRET not in text
    body = json.loads(text)
    assert [set(f) for f in body["findings"]] == [{"RuleID", "File", "StartLine", "Commit", "Date", "InBaseline"}]
    assert body["findings"][0]["Commit"] == FINDING_COMMIT and body["count"] == 1
    assert body["outside_baseline"] == [cw.fingerprint(finding())]
    assert list((tmp_path / cfg["findings_raw_dir"]).iterdir()) == []  # both raw reports are deleted


def test_findings_with_a_malformed_report_refuse_and_delete_it(
    world: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(tmp_path, secret_scanner={sys.platform: scanner(tmp_path, rows=[finding()], broken=True)})
    code = cw.findings_main(world, None, cfg, GUARD, tmp_path)
    err = capsys.readouterr().err
    assert code == cw.EXIT_REFUSED and "no valid StartLine" in err and SECRET not in err
    assert list((tmp_path / cfg["findings_raw_dir"]).iterdir()) == []
    assert not (tmp_path / cfg["findings_dir"]).exists()


def test_findings_on_named_commits_and_bad_ids(world: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = config(tmp_path, secret_scanner={sys.platform: scanner(tmp_path, rows=[finding()])})
    head = git(world, "rev-parse", "HEAD").strip()
    assert cw.findings_main(world, [head.upper()], cfg, GUARD, tmp_path) == cw.EXIT_FAILED
    assert cw.findings_main(world, ["abc123"], cfg, GUARD, tmp_path) == cw.EXIT_REFUSED
    assert "full local commit ids" in capsys.readouterr().err


def test_unpinned_action_is_refused(world: Path, tmp_path: Path) -> None:
    commit(world, ".github/workflows/ci.yml", WORKFLOW.replace(PIN, "pin_required"))
    push(world)
    assert "not pinned" in refused(world, config(tmp_path))


def test_unparsable_remote_url_is_refused(world: Path, tmp_path: Path) -> None:
    git(world, "remote", "set-url", "origin", str(tmp_path / "remote.git"))
    assert "remote URL" in refused(world, config(tmp_path))


def test_unparsable_ls_remote_is_refused(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = cw._git

    def fake_git(repo: Path, timeout: int, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess([], 0, b"garbage line\n", b"")
        return real(repo, timeout, *args)

    monkeypatch.setattr(cw, "_git", fake_git)
    with pytest.raises(cw.Refusal, match="cannot parse"):
        cw.remote_refs(world, 30, "origin")


# --- settings ---------------------------------------------------------------------------------------------------


def raw_config() -> dict[str, Any]:
    return dict(json.loads((HERE.parent / "ci_window_config.json").read_text(encoding="utf-8")))


# A valid entry; each refusal case below changes exactly one field, so it refuses for that field's reason.
GOOD_ENTRY = {"executable": "g", "version": "1", "version_args": ["version"], "sha256": "0" * 64, "args": ["{revs}"]}


def test_shipped_config_pins_gitleaks_for_windows_only() -> None:
    cfg = cw.load(cw.DEFAULT_CONFIG)
    win = cfg["secret_scanner"]["win32"]
    assert cfg["secret_scanner"]["linux"] == "pin_required" and cfg["run_timeout_seconds"] == 4200
    assert win["version"] == "8.30.1" and win["sha256"].startswith("17157e2e")
    assert Path(win["path"]).relative_to(gc.ROOT).as_posix() == ".tmp/tools/gitleaks/8.30.1/bin/gitleaks.exe"


@pytest.mark.parametrize("key", sorted(cw.KEYS))
def test_each_missing_key_is_refused(key: str) -> None:
    raw = raw_config()
    del raw[key]
    with pytest.raises(cw.ConfigError, match=key):
        cw.validate(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run_timeout_seconds", 0),
        ("run_timeout_seconds", True),
        ("poll_interval_seconds", 2400),
        ("gh_timeout_seconds", "60"),
        ("restore_attempts", 0),
        ("gh_argv", []),
        ("report_dir", "../outside"),
        ("report_dir", "/abs"),
        ("workflow_file", "../ci.yml"),
        ("workflow_file", "ci.txt"),
        ("allowed_tracked_paths", [""]),
        ("secret_scanner", {"argv": ["g"], "version": "1", "sha256": "0" * 64}),
        ("secret_scanner", "none"),
        ("secret_scanner", "pin_required"),
        ("secret_scanner", {}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "args": ["x"]}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "executable": "../g"}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "executable": "/g"}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version": ""}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version": " 1"}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version": "1\n"}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "sha256": "0" * 63}}),
        ("secret_scanner", {"win32": {k: v for k, v in GOOD_ENTRY.items() if k != "args"}}),
        ("secret_scanner", {"win32": {k: v for k, v in GOOD_ENTRY.items() if k != "version_args"}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version_args": []}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version_args": [""]}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version_args": "version"}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version_args": ["--log-opts={revs}"]}}),
        ("secret_scanner", {"win32": {**GOOD_ENTRY, "version_args": ["{repo}"]}}),
        ("schema_version", 2),
        ("schema_version", 1.0),  # fix round 3: equal to 1, but not the integer 1
        ("schema_version", True),
        ("ledger_lock_timeout_seconds", 0),
        ("ledger_lock_timeout_seconds", 121),
        ("ledger_lock_timeout_seconds", True),
        ("ledger_lock_timeout_seconds", 1.5),
        ("ledger_lock_timeout_seconds", "10"),
        ("surprise", 1),
        ("run_ledger", "../ledger.json"),
        ("run_ledger", "/abs/ledger.json"),
        ("run_ledger", ""),
        ("run_ledger", 7),
    ],
)
def test_bad_values_are_refused(key: str, value: Any) -> None:
    raw = raw_config()
    raw[key] = value
    with pytest.raises(cw.ConfigError):
        cw.validate(raw)


def test_the_good_scanner_entry_of_the_refusal_table_is_valid() -> None:
    raw = raw_config()
    raw["secret_scanner"] = {"win32": GOOD_ENTRY}
    entry = cw.validate(raw)["secret_scanner"]["win32"]
    assert entry["version_args"] == ["version"] and entry["version"] == "1"


def test_scanner_behind_a_link_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "tools"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # a directory junction needs no privilege on Windows

        _winapi.CreateJunction(str(outside), str(link))
    try:
        raw = raw_config()
        raw["secret_scanner"] = {"win32": {**GOOD_ENTRY, "executable": "tools/g.exe"}}
        with pytest.raises(cw.ConfigError, match="outside the project root"):
            cw.validate(raw, root)
    finally:
        os.unlink(link) if sys.platform != "win32" else os.rmdir(link)  # remove the link as a link


def test_missing_config_file_refuses_from_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cw.main(["run", "--repo", str(tmp_path), "--config", str(tmp_path / "absent.json")], root=tmp_path)
    assert code == cw.EXIT_REFUSED and "cannot read" in capsys.readouterr().err


# --- remote URL parsing -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://github.com/owner/repo.git", "https://github.com/owner/repo", "git@github.com:owner/repo.git",
     "ssh://git@github.com/owner/repo.git", "https://GitHub.com/owner/repo/"],
)  # fmt: skip
def test_remote_urls_give_the_slug(url: str) -> None:
    assert cw.repo_slug(url) == SLUG


@pytest.mark.parametrize(
    "url", ["", "C:/x/remote.git", "file:///x/r.git", "https://github.com/owner", "https://github.com/a/b/c",
            "https://github.com/o w/r", "http://github.com/owner/repo"],
)  # fmt: skip
def test_other_remote_urls_are_refused(url: str) -> None:
    with pytest.raises(cw.Refusal):
        cw.repo_slug(url)


NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABC0123456789_.-", min_size=1, max_size=20).filter(
    lambda s: s not in (".", "..") and not s.endswith(".git")
)


@given(NAMES, NAMES)
def test_repo_slug_property(owner: str, name: str) -> None:
    for url in (f"https://github.com/{owner}/{name}.git", f"git@github.com:{owner}/{name}"):
        assert cw.repo_slug(url) == f"github.com/{owner}/{name}"


# --- the real child-process path, against the gh executable double ------------------------------------------


def test_gh_child_process_uses_the_configured_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_VISIBILITY", "PUBLIC")
    gh = cw.Gh([sys.executable, str(HERE / "fake_gh.py")], SLUG, 30)
    assert gh.visibility() == "public"
    assert json.loads(log.read_text().splitlines()[0]) == ["repo", "view", SLUG, "--json", "visibility"]


def test_gh_child_process_timeout_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_SLEEP", "10")
    gh = cw.Gh([sys.executable, str(HERE / "fake_gh.py")], SLUG, 1)
    with pytest.raises(cw.WindowError, match="TimeoutExpired"):
        gh.visibility()


def test_child_processes_open_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, bool]] = []

    def spy(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append((kw["creationflags"], kw["start_new_session"]))
        return subprocess.CompletedProcess(argv, 0, b"1\n", b"")

    monkeypatch.setattr(cw.subprocess, "run", spy)
    monkeypatch.setattr(cw.sys, "platform", "win32")
    cw.subprocess_runner(["gh"], 5)
    cw._git(Path("."), 5, "status")
    cw.scanner_version(Path("g"), ["version"], 5)
    # No window, and its own session, so a terminal Ctrl+C or hangup cannot kill a `gh` call of the return.
    assert seen == [(0x08000000, True)] * 3


# --- the workflow file -------------------------------------------------------------------------------------------


def test_workflow_is_dispatch_only_least_privilege_and_runs_both_suites() -> None:
    yaml = pytest.importorskip("yaml")  # PyYAML is a project dependency (pyproject.toml)
    text = (HERE.parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    triggers = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert set(triggers) == {"workflow_dispatch"}
    assert data["permissions"] == {"contents": "read"}
    (job,) = data["jobs"].values()
    assert job["strategy"]["fail-fast"] is False
    # Decision 168: one matrix entry per job (os, suite, shard, shards); test_ci_shard.py checks the shard lists.
    entries = job["strategy"]["matrix"]["include"]
    assert {e["os"] for e in entries} == {"ubuntu-latest", "windows-latest"}
    assert {e["suite"] for e in entries} == {"pipeline", "tooling"}
    assert job["timeout-minutes"] * 60 < cw.load(cw.DEFAULT_CONFIG)["run_timeout_seconds"]
    assert "continue-on-error" not in text and "--deselect" not in text and " -k " not in text
    runs = [s.get("run", "") for s in job["steps"]]
    assert any(r.endswith("pytest -q -p no:cacheprovider tests") for r in runs)
    assert any(r.endswith("tools/agent-workflow/tests") for r in runs)
    # Each suite step runs in exactly the job its matrix entry names, so the four jobs run both suites on both systems.
    pytest_steps = {s["run"].rsplit(" ", 1)[-1]: s.get("if") for s in job["steps"] if "pytest" in s.get("run", "")}
    assert pytest_steps == {
        "tests": "${{ matrix.suite == 'pipeline' }}",
        "tools/agent-workflow/tests": "${{ matrix.suite == 'tooling' }}",
        # S42-ci7: the opt-in exit stress check runs one named test (no -k), only in the Linux pipeline job.
        "tests/test_storage_concurrency.py::test_child_reading_a_manifest_exits_cleanly_many_times": (
            "${{ matrix.suite == 'pipeline' && runner.os == 'Linux' && matrix.shard == 1 }}"
        ),
    }
    (stress,) = [s for s in job["steps"] if s.get("name") == "Exit stress after an in-memory Parquet read (Linux)"]
    assert stress["env"] == {"PYTHONPATH": "src", "PE_EXIT_STRESS": "1000"}
    # Any other suite value fails its job before the suite steps (S42-ci5 review F1): the guard's condition is the
    # exact complement of the two suite conditions, and it exits non-zero.
    guards = [s for s in job["steps"] if s.get("name") == "Refuse an unknown suite"]
    assert len(guards) == 1
    (guard,) = guards
    assert guard["if"] == "${{ matrix.suite != 'pipeline' && matrix.suite != 'tooling' }}"
    assert guard["env"] == {"SUITE": "${{ matrix.suite }}"} and "${{" not in guard["run"]
    program, flag, code = guard["run"].split(" ", 2)
    assert (program, flag) == ("python", "-c") and code[0] == code[-1] == '"'
    done = subprocess.run(  # the guard's own program, run here with a misspelled suite
        [sys.executable, "-I", "-c", code[1:-1]],
        env={**os.environ, "SUITE": "toolng"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 1 and "unknown matrix suite toolng" in done.stderr
    names = [s.get("name") for s in job["steps"]]
    assert names.index("Refuse an unknown suite") < names.index("Pipeline suite") < names.index("Tooling suite")
    assert any('pip install ".[test]"' in r for r in runs)
    step_env = [s.get("env", {}) for s in job["steps"]]
    assert any(env.get("PYTHONPATH") == "src" for env in step_env)
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses and all(u.split("@")[0] in ("actions/checkout", "actions/setup-python") for u in uses)
    assert all(cw.PINNED.search(u) for u in uses)  # full commit SHAs (docs/ci.md records their source)
    assert "# v7.0.1" in text and "# v7.1.0" in text
    assert "secrets." not in text


# --- review fix round 1: signals (F1), the scanned snapshot (F2), the scanner's identity (F3) --------------------

SHA = "a" * 40
# The required table (brief: Windows SIGINT, SIGBREAK; Linux SIGINT, SIGTERM, SIGHUP; plus SIGQUIT and Windows SIGTERM),
# stated here so the tables below also run against code that lacks it.
REQUIRED = ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP", "SIGQUIT")
TABLE = [getattr(signal, n) for n in REQUIRED if hasattr(signal, n)]  # the platform's catchable stop requests


def test_the_signal_table_is_the_required_one() -> None:
    assert set(cw.SIGNALS) == set(REQUIRED) and cw.SIGNALS[0] == "SIGINT"  # SIGINT first, so it is put back last


class Outside(BaseException):
    """What a raising handler outside the window throws (the worst case a caller can leave installed)."""


@pytest.fixture
def raising_outside() -> Any:
    """Every table signal raises outside the window (SIGINT: Python's default KeyboardInterrupt), so a signal that
    reaches code with no recorder installed shows up as an escape instead of ending the test process."""

    def raise_outside(signum: int, frame: Any) -> None:
        raise Outside(signum)

    saved = {s: signal.getsignal(s) for s in TABLE}
    for s in TABLE:
        signal.signal(s, signal.default_int_handler if s == signal.SIGINT else raise_outside)
    yield
    for s, old in saved.items():
        signal.signal(s, old)


def fast_pre(monkeypatch: pytest.MonkeyPatch, scanned: dict[str, str] | None = None,
             later: dict[str, str] | None = None, head: Any = SHA) -> Any:  # fmt: skip
    """Labelled precondition double: the scan passed over `scanned`; the later `ls-remote` reads give `later` (the same
    by default) and the local HEAD read gives `head` (a callable may raise). No Git and no child process."""
    scanned = scanned or {"refs/heads/main": SHA}
    monkeypatch.setattr(cw, "remote_refs", lambda *a: dict(later if later is not None else scanned))
    monkeypatch.setattr(cw, "local_head", head if callable(head) else (lambda *a: head))

    def pre(repo: Path, cfg: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
        return {"commit": SHA, "repository": SLUG, "remote": "origin", "branch": "main", "refs": dict(scanned),
                "scan": "scan double: clean", "warnings": []}  # fmt: skip

    return pre


def traced(run: Any, hit: int | None, sig: int | None) -> tuple[Any, int]:
    """Run `run()` counting the line events of ci_window frames; at event number `hit`, send `sig` for real
    (`signal.raise_signal`), so the installed handler runs at exactly that point. Returns (result or the escaped
    exception, events counted)."""
    count = 0

    def tracer(frame: Any, event: str, arg: Any) -> Any:
        nonlocal count
        if frame.f_code.co_filename != cw.__file__:
            return None
        if event == "line":
            count += 1
            if count == hit:
                signal.raise_signal(sig)  # type: ignore[arg-type]
        return tracer

    prior = sys.gettrace()
    sys.settrace(tracer)
    try:
        outcome: Any = run()
    except BaseException as exc:  # noqa: BLE001 - the escape is what the table checks
        outcome = exc
    finally:
        sys.settrace(prior)
    return outcome, count


# Once something changed, a signal may escape only while the saved handlers are put back (the visibility is then
# settled); before any change it may escape from code that runs before the recorder is installed. It never escapes
# from a function that runs with the recorder installed.
ESCAPE_FRAMES = {"open_window", "main", "__exit__", "_restore"}
INSIDE = {"_window", "restore", "find_run", "watch", "recheck", "snapshot_diff", "call", "_json", "visibility",
          "set_visibility", "run_ids", "dispatch", "run_view", "cancel", "check", "names", "_record", "write_report",
          "_say"}  # fmt: skip


def may_escape(exc: BaseException, changed: bool) -> bool:
    frame = escaped_from(exc)
    return frame not in INSIDE and (not changed or frame in ESCAPE_FRAMES)


def escaped_from(exc: BaseException) -> str:
    import traceback

    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == cw.__file__]
    return frames[-1].name if frames else "<outside ci_window>"


def test_interrupt_at_every_transition_of_the_window_leaves_it_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raising_outside: Any
) -> None:
    """Review F1, inverted: a real signal at every line the window executes, for every table signal."""
    cfg = config(tmp_path)
    pre = fast_pre(monkeypatch)

    def run(fake: FakeGh) -> Any:
        clock = Clock()
        return lambda: cw.open_window(Path("unused"), cfg, GUARD, fake, clock, clock.sleep, pre=pre)

    dry = FakeGh(sha=SHA, views=[("in_progress", None), ("completed", "success")])
    outcome, events = traced(run(dry), None, None)
    assert outcome[0] == cw.EXIT_OK and events > 50
    print(f"open_window transitions: {events} line events x {len(TABLE)} signals")
    bad = []
    for sig in TABLE:
        for hit in range(1, events + 1):
            fake = FakeGh(sha=SHA, views=[("in_progress", None), ("completed", "success")])
            outcome, _ = traced(run(fake), hit, sig)
            settled = fake.visibility == "private" and (not fake.edits() or fake.edits()[-1] == "private")
            if isinstance(outcome, BaseException):
                ok = settled and may_escape(outcome, bool(fake.edits()))
            else:
                code, report = outcome
                ok = settled and code in (cw.EXIT_OK, cw.EXIT_FAILED)
                ok = ok and (report["final_visibility"] == "private" if fake.edits() else True)
            if not ok:
                where = escaped_from(outcome) if isinstance(outcome, BaseException) else "returned"
                bad.append((signal.Signals(sig).name, hit, where, fake.edits(), fake.visibility))
    assert bad == []


def test_interrupt_at_every_transition_of_main_keeps_the_exit_3_message(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    raising_outside: Any,
) -> None:  # fmt: skip
    """The return never takes effect (sticky double): wherever a signal lands after the switch to public, main still
    exits 3 and prints the fix command."""
    import functools

    pre = fast_pre(monkeypatch)
    monkeypatch.setattr(cw, "open_window", functools.partial(cw.open_window, pre=pre))
    raw = raw_config()
    raw.update(report_dir="out/reports", restore_attempts=1)
    cfg_path = tmp_path / "ci.json"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")

    def run(fake: FakeGh) -> Any:
        return lambda: cw.main(["run", "--repo", str(world), "--config", str(cfg_path)], root=tmp_path, runner=fake)

    def sticky() -> FakeGh:
        fake = FakeGh(sticky=True, sha=SHA)
        fake.script["repo edit"] = lambda self, a: setattr(self, "visibility", "public")
        return fake

    outcome, events = traced(run(sticky()), None, None)
    assert outcome == cw.EXIT_EXPOSED and events > 50
    with capsys.disabled():
        print(f"main transitions: {events} line events x {len(TABLE)} signals")
    capsys.readouterr()
    bad = []
    for sig in TABLE:
        for hit in range(1, events + 1):
            fake = sticky()
            outcome, _ = traced(run(fake), hit, sig)
            err = capsys.readouterr().err
            if not fake.edits():
                continue  # nothing changed
            told = "REPOSITORY MAY BE PUBLIC. Make it private now" in err
            if isinstance(outcome, BaseException):
                ok = told and may_escape(outcome, True)
            else:
                ok = told and outcome == cw.EXIT_EXPOSED
            if not ok:
                bad.append((signal.Signals(sig).name, hit, outcome if not isinstance(outcome, int) else "exit"))
    assert bad == []


def test_a_handler_install_that_raises_puts_back_what_it_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {s: signal.getsignal(s) for s in TABLE}
    real, calls = cw.signal.signal, []

    def flaky(num: int, handler: Any) -> Any:
        calls.append(num)
        if len(calls) == 2:
            raise KeyboardInterrupt  # a signal while the second handler is installed
        return real(num, handler)

    monkeypatch.setattr(cw.signal, "signal", flaky)
    with pytest.raises(KeyboardInterrupt):
        cw.Deferred().__enter__()
    monkeypatch.setattr(cw.signal, "signal", real)
    assert {s: signal.getsignal(s) for s in TABLE} == before


def test_a_runner_bug_in_the_return_still_makes_every_attempt(world: Path, tmp_path: Path) -> None:
    def edit(self: FakeGh, args: list[str]) -> None:
        if "private" in args:
            raise RuntimeError("runner bug")  # not a WindowError

    fake = FakeGh(**{"repo edit": edit})
    code, report = window(world, config(tmp_path, restore_attempts=3), fake)
    assert code == cw.EXIT_EXPOSED and fake.edits() == ["public", "private", "private", "private"]
    assert sum("RuntimeError runner bug" in e for e in report["errors"]) == 3


def test_a_runner_bug_in_the_verify_read_still_makes_every_attempt(world: Path, tmp_path: Path) -> None:
    def view(self: FakeGh, args: list[str]) -> None:
        if self.edits():
            raise RuntimeError("reader bug")

    fake = FakeGh(**{"repo view": view})
    code, report = window(world, config(tmp_path, restore_attempts=2), fake)
    assert code == cw.EXIT_EXPOSED and fake.edits() == ["public", "private", "private"]
    assert sum(e.startswith("verify attempt") and "RuntimeError reader bug" in e for e in report["errors"]) == 2


def test_a_signal_before_the_switch_stops_with_nothing_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def view(self: FakeGh, args: list[str]) -> None:
        signal.raise_signal(signal.SIGINT)  # pressed while the visibility is read

    fake = FakeGh(sha=SHA, **{"repo view": view})
    clock = Clock()
    code, report = cw.open_window(Path("unused"), config(tmp_path), GUARD, fake, clock, clock.sleep,
                                  pre=fast_pre(monkeypatch))  # fmt: skip
    assert (code, report["result"], fake.edits()) == (cw.EXIT_FAILED, "interrupted", [])
    assert "before the switch to public" in report["errors"][0]


def test_a_signal_during_the_run_lookup_stops_and_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGh(sha=SHA, printed="")  # no URL printed, and no run ever appears
    clock = Clock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        signal.raise_signal(signal.SIGINT)

    code, report = cw.open_window(Path("unused"), config(tmp_path), GUARD, fake, clock, sleep,
                                  pre=fast_pre(monkeypatch))  # fmt: skip
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "interrupted", "private")
    assert "before the run lookup" in report["errors"][0] and clock.now < 60


def test_a_closed_terminal_keeps_exit_3(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Dead:
        def write(self, text: str) -> int:
            raise OSError(5, "Input/output error")  # stderr after a hangup

    import functools

    monkeypatch.setattr(cw, "open_window", functools.partial(cw.open_window, pre=fast_pre(monkeypatch)))
    raw = raw_config()
    raw.update(report_dir="out/reports", restore_attempts=1)
    cfg_path = tmp_path / "ci.json"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    fake = FakeGh(sticky=True, sha=SHA)
    fake.script["repo edit"] = lambda self, a: setattr(self, "visibility", "public")
    monkeypatch.setattr(cw.sys, "stderr", Dead())
    monkeypatch.setattr(cw.sys, "stdout", Dead())
    code = cw.main(["run", "--repo", str(world), "--config", str(cfg_path)], root=tmp_path, runner=fake)
    assert code == cw.EXIT_EXPOSED and len(list((tmp_path / "out" / "reports").glob("*.json"))) == 1


SNAPSHOT = {"refs/heads/main": SHA, "refs/heads/other": "b" * 40}


@pytest.mark.parametrize(
    ("case", "later", "head", "words"),
    [
        ("new branch", {**SNAPSHOT, "refs/heads/unscanned": "d" * 40}, SHA, "added refs/heads/unscanned"),
        ("new tag", {**SNAPSHOT, "refs/tags/v9": "d" * 40}, SHA, "added refs/tags/v9"),
        ("moved non-main", {**SNAPSHOT, "refs/heads/other": "c" * 40}, SHA, "moved refs/heads/other"),
        ("removed ref", {"refs/heads/main": SHA}, SHA, "removed refs/heads/other"),
        ("moved main", {**SNAPSHOT, "refs/heads/main": "e" * 40}, SHA, "moved refs/heads/main"),
        ("local HEAD moved", SNAPSHOT, "f" * 40, "local HEAD moved"),
        ("HEAD on another branch", SNAPSHOT, "raise", "not refs/heads/main"),
    ],
)
def test_snapshot_refusal_table_no_public_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, later: dict[str, str], head: str, words: str
) -> None:
    """Review F2, inverted: any change of what was scanned refuses while still private."""

    def other_branch(*a: Any) -> str:
        raise cw.Refusal("HEAD is refs/heads/topic, not refs/heads/main")

    pre = fast_pre(monkeypatch, scanned=SNAPSHOT, later=later, head=other_branch if head == "raise" else head)
    fake = FakeGh(sha=SHA)
    clock = Clock()
    code, report = cw.open_window(Path("unused"), config(tmp_path), GUARD, fake, clock, clock.sleep, pre=pre)
    assert (code, report["result"], fake.edits()) == (cw.EXIT_REFUSED, "refused", [])
    assert fake.names() == ["repo view"] and words in report["errors"][0]


@pytest.mark.parametrize("late", ["new branch", "moved main"])
def test_a_real_push_after_the_scan_refuses_before_publishing(world: Path, tmp_path: Path, late: str) -> None:
    def pre(repo: Path, cfg: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
        found = cw.preconditions(repo, cfg, guard)
        if late == "new branch":
            git(repo, "push", "-q", "origin", "HEAD~1:refs/heads/late")  # unscanned name, after the scan
        else:
            commit(repo, "late.txt")
            push(repo)
        return found

    fake = FakeGh()
    fake.script["sha"] = git(world, "rev-parse", "HEAD").strip()
    clock = Clock()
    code, report = cw.open_window(world, config(tmp_path), GUARD, fake, clock, clock.sleep, pre=pre)
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "nothing was made public" in report["errors"][0]


def test_the_baseline_is_read_from_the_scanned_commit(world: Path, tmp_path: Path) -> None:
    rows = [finding()]
    pinned = set_baseline(world, [entry(rows[0])])
    scanned = git(world, "rev-parse", "HEAD").strip()
    commit(world, BASELINE, "[]\n")  # HEAD moves on; the scanned commit still holds the pinned blob
    cfg = config(tmp_path, baseline=pinned, secret_scanner={sys.platform: scanner(tmp_path, rows=rows)})
    args = (cfg["secret_scanner"], cfg["scanner_baseline"], world, ["a" * 40], 60, tmp_path / "raw")
    git(world, "checkout", "-q", scanned, "--", BASELINE)  # the working file parses like the scanned blob
    assert not cw.scan(*args, rev=scanned, root=tmp_path)["new"]
    with pytest.raises(cw.Refusal, match="not the pinned"):
        cw.scan(*args, root=tmp_path)  # HEAD's blob is another file


REFS = st.dictionaries(st.text(alphabet="abcdefghij/", min_size=1, max_size=6),
                       st.sampled_from(["a" * 40, "b" * 40, "c" * 40]), max_size=6)  # fmt: skip


@given(REFS, REFS)
def test_snapshot_diff_property(before: dict[str, str], after: dict[str, str]) -> None:
    diff = cw.snapshot_diff(before, after)
    assert (diff == "") == (before == after)
    assert cw.snapshot_diff(before, dict(before)) == ""
    for ref in set(before) | set(after):
        if before.get(ref) != after.get(ref):
            assert ref in diff


@pytest.mark.parametrize(
    ("over", "words"),
    [
        ({"prints": b"double-2\n"}, "reports version 'double-2', not the configured 'double-1'"),
        ({"prints": b""}, "not one version line"),
        ({"prints": b"double-1\nextra\n"}, "not one version line"),
        ({"prints": b" double-1\n"}, "not one version line"),
        ({"prints": b"\xff\xfe\n"}, "not UTF-8"),
        ({"version_code": 3}, "version command exit 3"),
        ({"version_sleep": 60}, "TimeoutExpired"),
    ],
)
def test_scanner_identity_refusal_table(tmp_path: Path, over: dict[str, Any], words: str) -> None:
    """Review F3, inverted: the version comes from the binary; a mismatch or an unreadable answer refuses."""
    cfg = config(tmp_path, secret_scanner={sys.platform: scanner(tmp_path, **over)})
    timeout = 3 if "version_sleep" in over else 60  # the check timeout bounds the version command too
    with pytest.raises(cw.Refusal) as caught:
        cw.scan(cfg["secret_scanner"], cfg["scanner_baseline"], tmp_path, ["a" * 40], timeout, tmp_path / "raw",
                root=tmp_path)  # fmt: skip
    assert words in str(caught.value) and SECRET not in str(caught.value)
    assert not (tmp_path / "scanned.txt").exists()  # refused before the scan ran


def test_a_wrong_scanner_version_refuses_before_any_gh_call(world: Path, tmp_path: Path) -> None:
    entry_ = scanner(tmp_path, prints=b"double-2\n")
    assert "reports version" in refused(world, config(tmp_path, secret_scanner={sys.platform: entry_}))


@pytest.mark.parametrize("prints", [b"double-7\n", b"double-7\r\n", b"double-7"])
def test_a_matching_scanner_version_passes_and_is_reported(world: Path, tmp_path: Path, prints: bytes) -> None:
    entry_ = scanner(tmp_path, version="double-7", prints=prints)  # a non-default configured version
    code, report = window(world, config(tmp_path, secret_scanner={sys.platform: entry_}), FakeGh())
    assert code == cw.EXIT_OK and report["preconditions"]["scan"].startswith("secret scanner double-7 ")


def test_a_scanner_changed_during_the_scan_refuses(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe_bytes, real, seen = Path(sys.executable).read_bytes(), hashlib.sha256, []

    def hasher(data: bytes = b"") -> Any:  # labelled double: the scanner file differs on its second read
        if data == exe_bytes:
            seen.append(1)
            if len(seen) == 2:
                return real(b"swapped while it ran")
        return real(data)

    cfg = config(tmp_path)
    monkeypatch.setattr(cw.hashlib, "sha256", hasher)
    with pytest.raises(cw.Refusal, match="not the configured"):
        cw.scan(cfg["secret_scanner"], cfg["scanner_baseline"], world, ["a" * 40], 30, tmp_path / "raw",
                root=tmp_path)  # fmt: skip
    assert len(seen) == 2 and (tmp_path / "scanned.txt").exists()  # the second check is the one after the scan


@given(st.text(min_size=1, max_size=30))
def test_parse_version_property(text: str) -> None:
    assume(not text.endswith(("\n", "\r")))  # else `text` plus a line end is another, valid, line
    line_like = text.isprintable() and text == text.strip()
    for raw in (text.encode(), (text + "\n").encode(), (text + "\r\n").encode()):
        if line_like:
            assert cw.parse_version(raw) == text
        else:
            with pytest.raises(cw.Refusal):
                cw.parse_version(raw)


def _real_gitleaks_root() -> Path | None:
    """The tree holding the approved binary: this checkout, else the main working tree of a linked worktree."""
    rel = Path(".tmp/tools/gitleaks/8.30.1/bin/gitleaks.exe")
    roots = [gc.ROOT]
    proc = subprocess.run(["git", "-C", str(gc.ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                          capture_output=True, text=True, timeout=30, check=False,
                          creationflags=0x08000000 if sys.platform == "win32" else 0)  # fmt: skip
    if proc.returncode == 0:
        roots.append(Path(proc.stdout.strip()).parent)
    return next((r for r in roots if (r / rel).is_file()), None)


@pytest.mark.skipif(sys.platform != "win32" or _real_gitleaks_root() is None, reason="approved gitleaks.exe absent")
def test_real_gitleaks_8_30_1_reports_its_version_and_a_wrong_pin_refuses(tmp_path: Path) -> None:
    """Live CPU check of the approved binary (read-only: only its `version` command runs)."""
    root = _real_gitleaks_root()
    assert root is not None
    cfg = cw.load(cw.DEFAULT_CONFIG, root)
    entry_ = cfg["secret_scanner"]["win32"]
    assert cw.scanner_version(Path(entry_["path"]), entry_["version_args"], 30) == "8.30.1"
    argv, _ = cw.scanner_argv(cfg["secret_scanner"], tmp_path, [SHA], 30, "win32")
    assert argv[0] == entry_["path"]
    wrong = {"win32": {**entry_, "version": "0.0.0"}}
    with pytest.raises(cw.Refusal, match="reports version '8.30.1', not the configured '0.0.0'"):
        cw.scanner_argv(wrong, tmp_path, [SHA], 30, "win32")


# --- fix round 2: the non-Git surfaces (RT1) and the check after the last private read (RT2) --------------------


def fast_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeGh, ledger: list[int] | None = None,
                **pre: Any) -> tuple[int, dict[str, Any]]:  # fmt: skip
    cfg = config(tmp_path)
    if ledger is not None:
        cfg["paths"]["run_ledger"].write_text(json.dumps({"schema_version": 1, "runs": {SLUG: ledger}}),
                                              encoding="utf-8")  # fmt: skip
    clock = Clock()
    return cw.open_window(Path("unused"), cfg, GUARD, fake, clock, clock.sleep, pre=fast_pre(monkeypatch, **pre))


def runs(*ids: int) -> list[dict[str, Any]]:
    return [{"id": i} for i in ids]


def artifacts(*pairs: tuple[int, Any]) -> list[dict[str, Any]]:
    return [{"id": a, "expired": False, "workflow_run": r if r is None or isinstance(r, dict) else {"id": r}}
            for a, r in pairs]  # fmt: skip


SURFACES = {
    "wiki": {"repo": {**CLEAN["repo"], "has_wiki": True}},
    "discussions": {"repo": {**CLEAN["repo"], "has_discussions": True}},
    "pages": {"repo": {**CLEAN["repo"], "has_pages": True}},
    "release": {"releases": [{"id": 9, "draft": False}]},
    "draft release": {"releases": [{"id": 9, "draft": True}]},
    "issue or pull request": {"issues": [{"id": 4}]},
    "retained run": {"runs": runs(8)},
    "artifact": {"artifacts": artifacts((7, 8))},
}


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_a_non_git_surface_refuses_before_publishing(world: Path, tmp_path: Path, surface: str) -> None:
    """Retest RT1, inverted: real preconditions, then a surface that would become public refuses with no edit."""
    fake = FakeGh(inventory=SURFACES[surface])
    code, report = window(world, config(tmp_path), fake)
    assert (code, report["result"], fake.edits()) == (cw.EXIT_REFUSED, "refused", [])
    assert any(c[0] == "api" for c in fake.calls) and "workflow run" not in fake.names()


def test_a_clean_inventory_publishes_and_records_the_run(world: Path, tmp_path: Path) -> None:
    cfg = config(tmp_path)
    fake = FakeGh()
    code, report = window(world, cfg, fake)
    assert code == cw.EXIT_OK and report["inventory"].startswith("no wiki")
    paths = [c[3] for c in fake.calls if c[0] == "api"]
    assert paths == ["repos/owner/repo", "repos/owner/repo/releases?per_page=1",
                     "repos/owner/repo/issues?state=all&per_page=1",
                     "repos/owner/repo/actions/runs?per_page=100&page=1",
                     "repos/owner/repo/actions/artifacts?per_page=100&page=1"]  # fmt: skip
    assert all(c[1:3] == ["--hostname", "github.com"] for c in fake.calls if c[0] == "api")
    assert fake.names().index("api --hostname") < fake.names().index("repo edit")  # read while private
    assert json.loads(cfg["paths"]["run_ledger"].read_text()) == {"schema_version": 1, "runs": {SLUG: [77]}}


def test_the_next_window_accepts_the_tools_own_runs_and_artifacts(world: Path, tmp_path: Path) -> None:
    cfg = config(tmp_path)
    assert window(world, cfg, FakeGh())[0] == cw.EXIT_OK  # run 77 is recorded
    own = {"runs": runs(77), "artifacts": artifacts((5, 77))}
    fake = FakeGh(inventory=own, printed="https://github.com/owner/repo/actions/runs/78\n")
    assert window(world, cfg, fake)[0] == cw.EXIT_OK
    assert json.loads(cfg["paths"]["run_ledger"].read_text())["runs"][SLUG] == [77, 78]


def test_ledger_runs_over_several_pages_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = list(range(1, 251))  # three pages of 100
    fake = FakeGh(sha=SHA, inventory={"runs": runs(*ids), "artifacts": artifacts(*[(1000 + i, i) for i in ids])})
    code, report = fast_window(tmp_path, monkeypatch, fake, ledger=ids)
    assert code == cw.EXIT_OK and "250 run(s) and 250 artifact(s)" in report["inventory"]
    assert sum("actions/runs" in c[3] for c in fake.calls if c[0] == "api") == 3


def bad(**inv: Any) -> dict[str, Any]:
    return {"inventory": inv}


@pytest.mark.parametrize(
    ("case", "script", "words"),
    [
        ("repo not an object", bad(repo=[]), "no boolean"),
        ("flag missing", bad(repo={"has_wiki": False, "has_pages": False}), "no boolean"),
        ("flag not a boolean", bad(repo={**CLEAN["repo"], "has_wiki": "false"}), "no boolean"),
        ("releases not a list", bad(releases={}), "not a JSON list"),
        ("unknown run", bad(runs=runs(3)), "not dispatched by this tool"),
        ("unknown artifact run", bad(runs=runs(1), artifacts=artifacts((5, 9))), "did not dispatch"),
        ("artifact without a run", bad(runs=runs(1), artifacts=artifacts((5, None))), "names no workflow run"),
        ("artifact run without id", bad(artifacts=artifacts((5, {"name": "x"}))), "names no workflow run"),
        ("truncated runs", bad(runs=runs(1), runs_total=2), "truncated"),
        ("count below the list", bad(runs=runs(1, 2), runs_total=1), "truncated"),
        ("negative total", bad(runs_total=-1), "no valid total_count"),
        ("total not an integer", bad(runs_total=True), "no valid total_count"),
        ("run without id", bad(runs=[{"name": "x"}]), "no integer id"),
        ("run id as text", bad(runs=[{"id": "1"}]), "no integer id"),
        ("duplicate run", bad(runs=runs(1, 1)), "listed twice"),
    ],
)
def test_inventory_refusal_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, script: dict[str, Any],
                                 words: str) -> None:  # fmt: skip
    fake = FakeGh(sha=SHA, **script)
    code, report = fast_window(tmp_path, monkeypatch, fake, ledger=[1, 2])
    assert (code, report["result"], fake.edits()) == (cw.EXIT_REFUSED, "refused", []), case
    assert words in report["errors"][0]


@pytest.mark.parametrize("reply", ["exit 1", "not json", "timeout"])
@pytest.mark.parametrize("which", ["repos/owner/repo", "releases", "issues", "actions/runs", "actions/artifacts"])
def test_a_failed_inventory_read_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str,
                                         reply: str) -> None:  # fmt: skip
    def api(self: FakeGh, path: str) -> Any:
        if path == which if which.startswith("repos/") else f"/{which}" in path:
            if reply == "timeout":
                raise subprocess.TimeoutExpired(["gh"], 60)
            return self.done("{not json" if reply == "not json" else "", 0 if reply == "not json" else 1, "HTTP 403")
        return None

    fake = FakeGh(sha=SHA, api=api)
    code, report = fast_window(tmp_path, monkeypatch, fake)
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "cannot prove" in report["errors"][0]


def test_a_total_count_that_moves_between_pages_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[int] = []

    def api(self: FakeGh, path: str) -> Any:
        if "actions/runs" in path:
            served.append(1)
            body = {"total_count": 150 + len(served), "workflow_runs": runs(*range(len(served) * 100, len(served) * 100 + 100))}
            return self.done(json.dumps(body))
        return None

    fake = FakeGh(sha=SHA, api=api)
    code, report = fast_window(tmp_path, monkeypatch, fake, ledger=list(range(100, 400)))
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "changed from 151 to 152" in report["errors"][0]


@pytest.mark.parametrize("ledger", ['{"runs": {}}', "[]", "not json", '{"schema_version": 1, "runs": {"x": ["1"]}}'])
def test_a_malformed_ledger_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: str) -> None:
    cfg = config(tmp_path)
    cfg["paths"]["run_ledger"].write_text(ledger, encoding="utf-8")
    fake = FakeGh(sha=SHA)
    clock = Clock()
    code, report = cw.open_window(Path("unused"), cfg, GUARD, fake, clock, clock.sleep, pre=fast_pre(monkeypatch))
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "run ledger" in report["errors"][0]


def test_runs_recorded_for_another_repository_do_not_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config(tmp_path)
    cfg["paths"]["run_ledger"].write_text(json.dumps({"schema_version": 1, "runs": {"github.com/other/repo": [8]}}),
                                          encoding="utf-8")  # fmt: skip
    fake = FakeGh(sha=SHA, inventory={"runs": runs(8)})
    clock = Clock()
    code, report = cw.open_window(Path("unused"), cfg, GUARD, fake, clock, clock.sleep, pre=fast_pre(monkeypatch))
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "not dispatched by this tool" in report["errors"][0]


def test_a_failed_ledger_write_is_a_window_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(handle: Any, data: bytes) -> None:  # labelled double: the in-place ledger write fails
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cw.ts, "rewrite", full)
    fake = FakeGh(sha=SHA)
    code, report = fast_window(tmp_path, monkeypatch, fake)
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "error", "private")
    assert "could not be recorded in the run ledger" in report["errors"][0] and "run view" not in fake.names()


def test_run_ledger_override_is_written_through_main(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cw, "check_secrets", lambda *a, **k: ("scan double: clean", []))
    raw = raw_config()
    raw.update(run_ledger="out/state/ledger.json")  # a non-default path, resolved inside the root
    cfg_path = tmp_path / "ci.json"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    fake = FakeGh(sha=git(world, "rev-parse", "HEAD").strip())
    assert cw.main(["run", "--repo", str(world), "--config", str(cfg_path)], root=tmp_path, runner=fake) == 0
    assert json.loads((tmp_path / "out/state/ledger.json").read_text())["runs"] == {SLUG: [77]}


def test_the_shipped_run_ledger_is_under_runs() -> None:
    assert cw.load(cw.DEFAULT_CONFIG)["run_ledger"] == "runs/ci-window/ledger.json"


# --- fix round 3: the trusted ledger state (retest 2, R2F1-R2F3) -----------------------------------------------


def dir_link(target: Path, link: Path) -> None:
    """A directory link: a symbolic link, or on Windows without that privilege a junction (no privilege needed)."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def remove_link(link: Path) -> None:
    """Remove the link as a link, never what it points to (AGENTS.md section 6)."""
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        os.rmdir(link)  # a junction: rmdir removes the reparse point only


def project_cfg(root: Path, **over: Any) -> dict[str, Any]:
    raw = raw_config()
    raw.update({"run_ledger": "state/ledger.json", **over})
    return cw.validate(raw, root)


def ledger_window(monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any], fake: FakeGh) -> tuple[int, dict[str, Any]]:
    clock = Clock()
    return cw.open_window(Path("unused"), cfg, GUARD, fake, clock, clock.sleep, pre=fast_pre(monkeypatch))


def write_ledger(path: Path, ids: list[Any], schema: Any = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": schema, "runs": {SLUG: ids}}), encoding="utf-8")


def test_a_link_made_after_loading_refuses_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retest 2 R2F1, inverted: the ledger folder becomes a link out of the project after the configuration was
    validated; the window must not trust the outside ledger (run 8) nor write there."""
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    cfg = project_cfg(root)
    write_ledger(outside / "ledger.json", [8])
    dir_link(outside, root / "state")
    try:
        fake = FakeGh(sha=SHA, inventory={"runs": runs(8)})
        code, report = ledger_window(monkeypatch, cfg, fake)
    finally:
        remove_link(root / "state")
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "is a link" in report["errors"][0]
    assert json.loads((outside / "ledger.json").read_text())["runs"][SLUG] == [8]


def test_a_link_made_after_publication_is_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2F1 at the write: the link appears after the inventory read, before the run is recorded. The ledger sits one
    folder deeper, so even creating its missing folder through the link would leave a trace outside."""
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    cfg = project_cfg(root, run_ledger="state/sub/ledger.json")
    real = cw.find_run

    def find_then_link(*args: Any) -> int:
        run_id = real(*args)
        dir_link(outside, root / "state")
        return run_id

    monkeypatch.setattr(cw, "find_run", find_then_link)
    fake = FakeGh(sha=SHA)
    try:
        code, report = ledger_window(monkeypatch, cfg, fake)
    finally:
        remove_link(root / "state")
    assert (code, report["result"], fake.visibility) == (cw.EXIT_FAILED, "error", "private")
    assert "could not be recorded" in report["errors"][0] and "is a link" in report["errors"][0]
    assert list(outside.iterdir()) == []  # nothing, not even a temp or lock file, was written outside


def test_a_link_inside_the_project_is_refused_at_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stronger rule chosen for R2F1: no link below the project root, even one that stays inside it."""
    root = tmp_path / "project"
    (root / "real-state").mkdir(parents=True)
    dir_link(root / "real-state", root / "state")
    try:
        cfg = project_cfg(root)  # the load-time check resolves inside the root and passes
        fake = FakeGh(sha=SHA)
        code, report = ledger_window(monkeypatch, cfg, fake)
    finally:
        remove_link(root / "state")
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "is a link" in report["errors"][0]


@pytest.mark.parametrize("schema", [True, 1.0])
def test_a_non_integer_ledger_schema_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: Any) -> None:
    """Retest 2 R2F3, inverted: `True == 1` and `1.0 == 1` in Python, but neither is the integer schema 1."""
    cfg = config(tmp_path)
    write_ledger(cfg["paths"]["run_ledger"], [8], schema)
    fake = FakeGh(sha=SHA, inventory={"runs": runs(8)})
    code, report = ledger_window(monkeypatch, cfg, fake)
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "run ledger" in report["errors"][0]


def test_concurrent_recorders_keep_both_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retest 2 R2F2, inverted: window A has read the ledger (it is parsing it) when window B records 79. A waits
    (at most 1 s) for B to read too, the interleaving the reviewer forced: unlocked, both read the old file and one
    id is lost; under the lock, B cannot read before A has written. Both calls must succeed and both ids stay."""
    root = tmp_path.resolve()
    path = root / "ledger.json"
    write_ledger(path, [])
    real_parse = cw.parse_ledger
    a_read, b_read = threading.Event(), threading.Event()
    ok: list[int] = []
    errors: list[str] = []

    def parse(text: str) -> dict[str, Any]:
        if threading.current_thread().name == "A":
            a_read.set()
            b_read.wait(1.0)  # a locked B never reads while A holds the lock
        else:
            b_read.set()
        return real_parse(text)

    def worker(run_id: int) -> None:
        try:
            cw.record_run(root, path, SLUG, run_id, 10)
            ok.append(run_id)
        except Exception as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(f"{type(exc).__name__}: {exc}")

    monkeypatch.setattr(cw, "parse_ledger", parse)
    a = threading.Thread(target=worker, args=(78,), name="A")
    b = threading.Thread(target=worker, args=(79,), name="B")
    a.start()
    assert a_read.wait(10)
    b.start()
    a.join(20)
    b.join(20)
    assert not a.is_alive() and not b.is_alive()
    monkeypatch.undo()
    assert (sorted(ok), errors) == ([78, 79], [])
    assert sorted(json.loads(path.read_text())["runs"][SLUG]) == [78, 79]
    assert sorted(p.name for p in root.iterdir()) == ["ledger.json", "ledger.json.lock"]  # no temp file left


LEDGER_VALUES = st.one_of(st.integers(-3, 3), st.booleans(), st.floats(allow_nan=False, width=16), st.none(),
                          st.text(max_size=3))  # fmt: skip


@given(LEDGER_VALUES, st.lists(LEDGER_VALUES, max_size=4))
def test_parse_ledger_property(schema: Any, ids: list[Any]) -> None:
    """Property: a ledger is accepted exactly when its schema is the integer 1 and every run id is an int."""
    text = json.dumps({"schema_version": schema, "runs": {SLUG: ids}})
    valid = type(schema) is int and schema == 1 and all(type(i) is int for i in ids)
    if valid:
        assert cw.parse_ledger(text)["runs"][SLUG] == ids
    else:
        with pytest.raises(cw.Refusal, match="run ledger"):
            cw.parse_ledger(text)


def hold_lock(path: Path) -> Any:
    """Hold `<ledger>.lock` from another handle, as a second window would."""
    handle = path.with_name(path.name + cw.LEDGER_LOCK_SUFFIX).open("a+b")
    cw.ts.gl._lock(handle, time.monotonic() + 5)
    return handle


def release_lock(handle: Any) -> None:
    cw.ts.gl._unlock(handle)
    handle.close()


def test_ledger_lock_timeout_override_bounds_the_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration override: a non-default 1 s (the example is 10) is the wait for a held lock, then a refusal."""
    cfg = config(tmp_path, ledger_lock_timeout_seconds=1)
    assert cfg["ledger_lock_timeout_seconds"] == 1
    write_ledger(cfg["paths"]["run_ledger"], [])
    handle = hold_lock(cfg["paths"]["run_ledger"])
    try:
        fake = FakeGh(sha=SHA)
        start = time.monotonic()
        code, report = ledger_window(monkeypatch, cfg, fake)
        took = time.monotonic() - start
    finally:
        release_lock(handle)
    assert (code, fake.edits()) == (cw.EXIT_REFUSED, []) and "stayed busy for 1 s" in report["errors"][0]
    assert 0.9 <= took < 5


def test_a_busy_ledger_lock_at_the_write_is_a_window_error(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    path = root / "ledger.json"
    write_ledger(path, [5])
    handle = hold_lock(path)
    try:
        with pytest.raises(cw.WindowError, match="stayed busy for 1 s"):
            cw.record_run(root, path, SLUG, 6, 1)
    finally:
        release_lock(handle)
    assert json.loads(path.read_text())["runs"][SLUG] == [5]


def test_a_linked_raw_report_folder_refuses_before_the_scanner_runs(world: Path, tmp_path: Path) -> None:
    """The class audit: the raw scanner report of `run` was checked at load only; now at each use."""
    cfg = config(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    dir_link(tmp_path / "elsewhere", tmp_path / "raw")
    try:
        message = refused(world, cfg)
    finally:
        remove_link(tmp_path / "raw")
    assert "is a link" in message and not (tmp_path / "scanned.txt").exists()


def test_a_linked_report_folder_is_refused_at_write(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "real").mkdir()
    dir_link(root / "real", root / "runs")
    try:
        with pytest.raises(ValueError, match="is a link"):  # Escape, a ValueError, which main reports
            cw.write_report(root, {"report_dir": "runs/ci-window"}, {"commit": None})
    finally:
        remove_link(root / "runs")
    assert list((root / "real").iterdir()) == []


# --- fix round 4: trusted state I/O bound to the opened object (retest 3, R3F1) -----------------------------------


def files_below(folder: Path) -> list[str]:
    """Every regular file under `folder`, without following links (an absent folder has none)."""
    return sorted(str(Path(d) / f) for d, _, names in os.walk(folder) for f in names) if folder.is_dir() else []


@pytest.mark.parametrize("when", ["after_scan", "at_read", "never"])
def test_a_raw_report_folder_swapped_after_the_scan_is_never_read_nor_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """Retest 3 R3F1, inverted, read and cleanup. A labelled scanner double writes an inside finding. Then, right
    after the scan or right before the report is opened (the reviewer's probe point), the test does what the probe
    did: it renames the raw folder away and puts a junction to an outside folder holding a forged report at the same
    name. The window must never read the outside report and must leave no scanner file anywhere in the project.
    `never` is the positive control: a normal inside report is read and removed. On Windows the held folder refuses
    the rename. On POSIX the rename succeeds, and the held descriptors still read and remove the real report
    (Linux CI)."""
    root, outside = tmp_path / "project", tmp_path / "outside"
    rawdir, saved = root / "raw", root / "saved-raw"
    root.mkdir()
    outside.mkdir()
    real_run, swapped, reports = subprocess.run, [], []

    def swap() -> None:
        if swapped or not reports:
            return
        rel = reports[0].relative_to(rawdir)
        try:
            rawdir.rename(saved)
        except OSError:
            swapped.append(False)  # the held folder cannot be renamed
            return
        dir_link(outside, rawdir)
        (outside / rel).parent.mkdir(parents=True, exist_ok=True)
        (outside / rel).write_text(json.dumps([finding(rule="outside_fixture")]), encoding="utf-8")
        swapped.append(True)

    def scanner_double(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "scanner-double":
            return real_run(argv, **kwargs)
        reports.append(Path(argv[-1]))
        reports[0].write_text(json.dumps([finding(rule="inside_fixture")]), encoding="utf-8")
        if when == "after_scan":
            swap()
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    if when == "at_read":  # the read of either implementation: round 3 `Path.read_text`, round 4 `Folder.read_bytes`
        real_read_text = Path.read_text

        def read_text(self: Path, *a: Any, **k: Any) -> str:
            if reports and self == reports[0]:
                swap()
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", read_text)
        if hasattr(cw, "ts"):
            real_read_bytes = cw.ts.Folder.read_bytes

            def read_bytes(self: Any, name: str) -> bytes | None:
                swap()
                return real_read_bytes(self, name)

            monkeypatch.setattr(cw.ts.Folder, "read_bytes", read_bytes)
    monkeypatch.setattr(cw.subprocess, "run", scanner_double)
    swap_wanted = when != "never"
    try:
        try:
            outcome = cw._report_rows(["scanner-double"], root, 5, rawdir, root)[1][0]["RuleID"]
        except cw.Refusal as exc:
            outcome = f"refused: {exc}"
        linked = rawdir.is_symlink() or rawdir.is_junction()
        left = files_below(saved) + ([] if linked else files_below(rawdir))
    finally:
        if rawdir.is_symlink() or rawdir.is_junction():
            remove_link(rawdir)
        if saved.exists():
            saved.rename(rawdir)
    print(f"R3F1 when={when} swapped={swapped} outcome={outcome[:60]} left={left}")
    assert outcome != "outside_fixture" and left == []
    assert bool(swapped) == swap_wanted  # the swap was attempted where asked
    if not swap_wanted:
        assert outcome == "inside_fixture" and list(rawdir.iterdir()) == []


def test_a_raw_report_whose_handle_is_outside_refuses_and_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handle check itself: a labelled double makes the opened report's final path name an outside folder (on
    Windows `GetFinalPathNameByHandleW`, on POSIX the `O_NOFOLLOW` open reports a link). The read refuses and the
    owned folder is still removed."""
    root = tmp_path.resolve()
    rawdir = root / "raw"
    real_run = subprocess.run

    def scanner_double(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "scanner-double":
            return real_run(argv, **kwargs)
        Path(argv[-1]).write_text(json.dumps([finding(rule="inside_fixture")]), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(cw.subprocess, "run", scanner_double)
    if cw.ts.WINDOWS:
        real_final = cw.ts._final_path

        def final_path(fd: int) -> str:
            text = real_final(fd)
            return text.replace("\\raw\\", "\\elsewhere\\") if text.endswith(cw.RAW_NAME) else text

        monkeypatch.setattr(cw.ts, "_final_path", final_path)
    else:
        real_open = cw.ts.os.open

        def no_follow(path: Any, flags: int, *a: Any, **k: Any) -> int:
            if path == cw.RAW_NAME and flags & os.O_NOFOLLOW:
                raise OSError(40, "Too many levels of symbolic links")  # labelled double: ELOOP
            return real_open(path, flags, *a, **k)

        monkeypatch.setattr(cw.ts.os, "open", no_follow)
    with pytest.raises(cw.Refusal, match="outside its folder|is a link"):
        cw._report_rows(["scanner-double"], root, 5, rawdir, root)
    assert list(rawdir.iterdir()) == []


def test_a_raw_report_that_cannot_be_removed_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owned cleanup reports what it leaves: the scanner double also makes a folder the cleanup does not recurse."""
    root = tmp_path.resolve()
    rawdir = root / "raw"
    real_run = subprocess.run

    def scanner_double(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "scanner-double":
            return real_run(argv, **kwargs)
        report = Path(argv[-1])
        report.write_text("[]", encoding="utf-8")
        (report.parent / "extra").mkdir()
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(cw.subprocess, "run", scanner_double)
    with pytest.raises(cw.Refusal, match="could not be deleted") as caught:
        cw._report_rows(["scanner-double"], root, 5, rawdir, root)
    assert "extra" in str(caught.value)
    (owned,) = rawdir.iterdir()
    assert sorted(p.name for p in owned.iterdir()) == ["extra"]  # the report itself is gone


def test_a_signal_after_the_final_check_is_returned_by_the_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted cancellation residual (docs/ci.md Known limits): a real SIGINT after the last check, on the line
    of the public edit, cannot stop that edit; the always-run restore returns the repository to private."""
    import inspect

    source, start = inspect.getsourcelines(cw._window)
    line = start + next(i for i, text in enumerate(source) if 'gh.set_visibility("public")' in text)
    hits: list[int] = []
    prior = sys.gettrace()

    def trace(frame: Any, event: str, arg: Any) -> Any:
        if event == "line" and frame.f_code is cw._window.__code__ and frame.f_lineno == line and not hits:
            hits.append(line)
            signal.raise_signal(signal.SIGINT)
        return trace

    fake = FakeGh(sha=SHA)
    sys.settrace(trace)
    try:
        code, report = ledger_window(monkeypatch, config(tmp_path), fake)
    finally:
        sys.settrace(prior)
    assert hits == [line] and report["signals"] == ["SIGINT"]
    assert (code, report["result"], fake.edits(), fake.visibility) == (cw.EXIT_FAILED, "interrupted",
                                                                      ["public", "private"], "private")  # fmt: skip


def test_signal_in_private_recheck_refuses_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retest RT2, inverted: a real SIGINT during the private remote read stops the window with no edit."""
    pre = fast_pre(monkeypatch)
    fake = FakeGh(sha=SHA)

    def refs(*args: Any) -> dict[str, str]:
        if not fake.edits():
            signal.raise_signal(signal.SIGINT)
        return {"refs/heads/main": SHA}

    monkeypatch.setattr(cw, "remote_refs", refs)
    clock = Clock()
    code, report = cw.open_window(Path("unused"), config(tmp_path), GUARD, fake, clock, clock.sleep, pre=pre)
    assert (code, report["result"], report["signals"], fake.edits()) == (cw.EXIT_FAILED, "interrupted", ["SIGINT"], [])


PRIVATE_READS = ["remote refs", "local HEAD", "repos/owner/repo", "releases", "issues", "actions/runs",
                 "actions/artifacts"]  # fmt: skip


@pytest.mark.parametrize("read", PRIVATE_READS)
def test_a_signal_during_any_private_read_leaves_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    pre = fast_pre(monkeypatch)
    fake = FakeGh(sha=SHA)
    if read == "remote refs":
        monkeypatch.setattr(cw, "remote_refs", lambda *a: (signal.raise_signal(signal.SIGINT), {"refs/heads/main": SHA})[1])
    elif read == "local HEAD":
        monkeypatch.setattr(cw, "local_head", lambda *a: (signal.raise_signal(signal.SIGINT), SHA)[1])
    else:
        def api(self: FakeGh, path: str) -> None:
            if path == read if read.startswith("repos/") else f"/{read}" in path:
                signal.raise_signal(signal.SIGINT)

        fake.script["api"] = api
    clock = Clock()
    code, report = cw.open_window(Path("unused"), config(tmp_path), GUARD, fake, clock, clock.sleep, pre=pre)
    assert (code, report["result"], fake.edits()) == (cw.EXIT_FAILED, "interrupted", [])
    assert "before the switch to public" in report["errors"][0]


class PagedGh:
    """Labelled double of `Gh.rest` for one counted list, serving `pages` (a list of response bodies) in order."""

    def __init__(self, pages: list[Any]) -> None:
        self.pages, self.asked = pages, 0

    def rest(self, tail: str) -> Any:
        self.asked += 1
        return self.pages[self.asked - 1] if self.asked <= len(self.pages) else {"total_count": 0, "workflow_runs": []}


PER_PAGE = 100  # the REST maximum per_page ([WR], [AR]); stated here so the tables also load against older code


def test_the_page_size_is_the_rest_maximum() -> None:
    assert cw.PAGE == PER_PAGE


def serve(ids: list[int], total: int, per: int = PER_PAGE) -> list[Any]:
    pages = [{"total_count": total, "workflow_runs": runs(*ids[i : i + per])} for i in range(0, len(ids), per)]
    return pages + [{"total_count": total, "workflow_runs": []}]


IDS = st.lists(st.integers(min_value=0, max_value=10**12), unique=True, max_size=260)


@given(IDS)
def test_counted_property_well_formed_pages_pass(ids: list[int]) -> None:
    got = cw.counted(PagedGh(serve(ids, len(ids))), "/actions/runs", "workflow_runs", "runs")  # type: ignore[arg-type]
    assert [i["id"] for i in got] == ids


@given(IDS.filter(bool), st.data())
def test_counted_property_truncated_or_moved_lists_refuse(ids: list[int], data: st.DataObject) -> None:
    kind = data.draw(st.sampled_from(["truncated", "overcounted", "duplicate", "moved total", "malformed"]))
    if kind == "truncated":
        pages = serve(ids[:-1], len(ids))
    elif kind == "overcounted":
        # More items than total_count; seen only when the excess lands on a page that is read (a count that ends
        # exactly on a page boundary hides the rest, and no client can tell).
        assume(len(ids) == 1 or (len(ids) - 1) % PER_PAGE != 0)
        pages = serve(ids, len(ids) - 1)
    elif kind == "duplicate":
        pages = serve([*ids, ids[0]], len(ids) + 1)
    elif kind == "moved total":
        pages = [{"total_count": len(ids) + 1, "workflow_runs": runs(*ids)},
                 {"total_count": len(ids), "workflow_runs": []}]  # fmt: skip
    else:
        broken = data.draw(st.sampled_from([None, [], {"total_count": "1"}, {"workflow_runs": []},
                                            {"total_count": 1, "workflow_runs": [{"id": None}]}]))  # fmt: skip
        pages = [broken]
    with pytest.raises(cw.Refusal):
        cw.counted(PagedGh(pages), "/actions/runs", "workflow_runs", "runs")  # type: ignore[arg-type]
