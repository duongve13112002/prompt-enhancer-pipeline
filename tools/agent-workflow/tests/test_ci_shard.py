"""ci_shard.py (decision 168, R-025): every test file lands in exactly one CI shard, and bad shard values are refused.

The proof that "every test runs exactly once" is the always-on check of this file, run in every CI job:
- a unit test over the real collection of `tests/` and `tools/agent-workflow/tests/` (collected once by a child pytest
  with no shard variables): the shards' files are disjoint and their union is the collected file set;
- a Hypothesis property test over file lists, weights and shard counts of the pure `assign` function;
- child-pytest runs of the collection hook over a tiny synthetic project: the node ids of all shards, joined, equal the
  unsharded ids, none twice; an empty shard is refused; malformed values are refused before any test runs.
No test double: the children are real pytest processes, the weight table is the committed one.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import ci_shard as cs
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[1]
SUITES = {"pipeline": "tests", "tooling": "tools/agent-workflow/tests"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # AGENTS.md: no console window for child processes on Windows


def _env(**extra: str) -> dict[str, str]:
    env = cs.child_environ()
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    env.update(extra)
    return env


def _pytest(cwd: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=_env(**env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        creationflags=NO_WINDOW,
        check=False,
    )


def _ids(done: subprocess.CompletedProcess[str]) -> list[str]:
    assert done.returncode == 0, done.stdout + done.stderr
    return [line for line in done.stdout.splitlines() if "::" in line]


# --- shard variables: override and refusal (docs/test-plan.md, Configuration) -----------------------------------------


def test_unset_variables_mean_the_full_suite() -> None:
    assert cs.parse_shard({}) is None


@pytest.mark.parametrize(("index", "count"), [("1", "1"), ("1", "4"), ("4", "4"), ("2", "10"), ("10", "10")])
def test_valid_values_are_read(index: str, count: str) -> None:
    assert cs.parse_shard({cs.ENV_SHARD: index, cs.ENV_SHARDS: count}) == (int(index), int(count))


@pytest.mark.parametrize(
    "bad", ["", "0", "-1", "+1", "1.0", " 1", "1 ", "01", "one", "1e1", "٣", "0x1", "1_0", "١"]
)
def test_malformed_values_are_refused(bad: str) -> None:
    with pytest.raises(cs.ShardError):
        cs.parse_shard({cs.ENV_SHARD: bad, cs.ENV_SHARDS: "4"})
    with pytest.raises(cs.ShardError):
        cs.parse_shard({cs.ENV_SHARD: "1", cs.ENV_SHARDS: bad})


@pytest.mark.parametrize(("index", "count"), [("5", "4"), ("2", "1"), ("11", "10")])
def test_an_index_beyond_the_count_is_refused(index: str, count: str) -> None:
    with pytest.raises(cs.ShardError, match="outside"):
        cs.parse_shard({cs.ENV_SHARD: index, cs.ENV_SHARDS: count})


@pytest.mark.parametrize("only", [cs.ENV_SHARD, cs.ENV_SHARDS])
def test_one_variable_alone_is_refused(only: str) -> None:
    with pytest.raises(cs.ShardError, match="together"):
        cs.parse_shard({only: "1"})


def test_required_refuses_when_both_are_unset() -> None:
    with pytest.raises(cs.ShardError, match="both be set"):
        cs.parse_shard({}, required=True)


def test_the_ci_guard_command_accepts_and_refuses_through_the_environment() -> None:
    def check(**env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(TOOLS / "ci_shard.py"), "check"],
            env=_env(**env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            creationflags=NO_WINDOW,
            check=False,
        )

    ok = check(PE_SHARD="2", PE_SHARDS="3")
    assert (ok.returncode, ok.stdout.strip()) == (0, "shard 2/3 ok")
    for env in ({}, {"PE_SHARD": "4", "PE_SHARDS": "3"}, {"PE_SHARD": "0", "PE_SHARDS": "3"}, {"PE_SHARD": "1"}):
        bad = check(**env)
        assert bad.returncode == 1 and "shard refused" in bad.stderr, env


# --- the weight table --------------------------------------------------------------------------------------------------


def _real_files(suite_dir: str) -> list[str]:
    base = ROOT / suite_dir
    found = {p.relative_to(ROOT).as_posix() for pattern in ("test_*.py", "*_test.py") for p in base.rglob(pattern)}
    return sorted(found)


def test_the_committed_table_is_valid_and_names_only_real_files_with_their_exact_spelling() -> None:
    default, table = cs.load_weights()
    assert default > 0 and table
    real = {f for d in SUITES.values() for f in _real_files(d)}
    # A stale or case-wrong key (a rename, or a Windows spelling) would silently fall back to the default weight.
    assert sorted(set(table) - real) == []


def _table(tmp_path: Path, **override: object) -> Path:
    data: dict[str, object] = {"schema_version": 1, "default_weight_s": 1.0, "weights": {"tests/test_a.py": 2.5}}
    data.update(override)
    path = tmp_path / "w.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_valid_table_is_read(tmp_path: Path) -> None:
    assert cs.load_weights(_table(tmp_path)) == (1.0, {"tests/test_a.py": 2.5})


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"default_weight_s": 0},
        {"default_weight_s": "1"},
        {"default_weight_s": True},
        {"default_weight_s": float("inf")},
        {"weights": []},
        {"weights": {"tests/test_a.py": -1}},
        {"weights": {"tests/test_a.py": None}},
        {"weights": {"tests\\test_a.py": 1}},
        {"weights": {"./tests/test_a.py": 1}},
        {"unknown": 1},
    ],
)
def test_a_malformed_table_is_refused(tmp_path: Path, override: dict[str, object]) -> None:
    with pytest.raises(cs.ShardError):
        cs.load_weights(_table(tmp_path, **override))


def test_a_missing_key_or_file_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["weights"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(cs.ShardError):
        cs.load_weights(path)
    with pytest.raises(cs.ShardError):
        cs.load_weights(tmp_path / "absent.json")


# --- the partition: pure function ---------------------------------------------------------------------------------------


def test_assign_balances_known_weights_and_gives_unlisted_files_the_default() -> None:
    shards = cs.assign(["a.py", "b.py", "c.py", "d.py"], {"a.py": 10.0, "b.py": 6.0, "c.py": 4.0}, 5.0, 2)
    # Heaviest first: a(10) -> shard 1, b(6) -> shard 2, the unlisted d at the default 5 -> shard 2 (the lighter, 6),
    # c(4) -> shard 1 (10 against 11).
    assert shards == [["a.py", "c.py"], ["b.py", "d.py"]]


def test_assign_with_more_shards_than_files_leaves_empty_shards_but_loses_nothing() -> None:
    shards = cs.assign(["a.py", "b.py"], {}, 1.0, 5)
    assert len(shards) == 5 and sorted(f for s in shards for f in s) == ["a.py", "b.py"]


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_assign_refuses_a_bad_count(count: object) -> None:
    with pytest.raises(cs.ShardError):
        cs.assign(["a.py"], {}, 1.0, count)  # type: ignore[arg-type]


def test_assign_refuses_a_duplicate_even_in_another_spelling() -> None:
    with pytest.raises(cs.ShardError, match="duplicate"):
        cs.assign(["t/a.py", "t\\a.py"], {}, 1.0, 2)


_NAME = st.from_regex(r"[a-z_]{1,6}(/[a-z_]{1,6}){0,2}\.py", fullmatch=True)


@st.composite
def _scenario(draw: st.DrawFn) -> tuple[list[str], dict[str, float], float, int]:
    files = draw(st.lists(_NAME, unique=True, max_size=40))
    weights = {}
    for name in files:
        if draw(st.booleans()):  # about half the files are missing from the table and take the default
            weights[name] = draw(st.floats(0.01, 1000.0))
    default = draw(st.floats(0.01, 1000.0))
    return files, weights, default, draw(st.integers(1, 9))


@given(_scenario(), st.randoms(use_true_random=False))
def test_property_every_file_in_exactly_one_shard_deterministic_and_balanced(scenario, rnd) -> None:
    files, weights, default, count = scenario
    shards = cs.assign(files, weights, default, count)
    assert len(shards) == count
    flat = [f for s in shards for f in s]
    assert sorted(flat) == sorted(files) and len(flat) == len(set(flat))  # union equals the input, nothing twice
    shuffled = list(files)
    rnd.shuffle(shuffled)
    assert cs.assign(shuffled, weights, default, count) == shards  # order does not matter
    backslashed = [f.replace("/", "\\") for f in files]
    assert cs.assign(backslashed, weights, default, count) == shards  # the separator spelling does not matter
    assert cs.assign(["./" + f for f in files], weights, default, count) == shards
    loads = [sum(weights.get(f, default) for f in s) for s in shards]
    total, heaviest = sum(loads), max((weights.get(f, default) for f in files), default=0.0)
    # List-scheduling bound: the fullest shard exceeds the mean by at most the heaviest single file.
    assert max(loads) <= total / count + heaviest + 1e-9 * (total + 1)


# --- the real collection -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_real_collection_is_partitioned_exactly_once_for_every_shard_count(suite: str) -> None:
    ids = _ids(_pytest(ROOT, "--collect-only", "-q", SUITES[suite]))
    files = sorted({i.split("::", 1)[0] for i in ids})
    assert files, "the suite collected nothing"
    assert sorted(set(files) - set(_real_files(SUITES[suite]))) == [], "a collected file the glob does not know"
    default, table = cs.load_weights()
    for count in range(1, 9):
        shards = cs.assign(files, table, default, count)
        flat = [f for s in shards for f in s]
        assert sorted(flat) == files and len(flat) == len(set(flat)), f"{suite}, {count} shards"
        # Each node id belongs to the shard of its file, so every test runs in exactly one shard.
        per_shard = [[i for i in ids if i.split("::", 1)[0] in set(s)] for s in shards]
        joined = [i for part in per_shard for i in part]
        assert sorted(joined) == sorted(ids) and len(joined) == len(set(joined)), f"{suite}, {count} shards"


# --- the collection hook over a synthetic project ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Five files with 1..5 tests, and a conftest that loads the real hooks."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(TOOLS)!r})\n"
        "from ci_shard import pytest_collection_modifyitems, pytest_report_collectionfinish  # noqa: F401\n",
        encoding="utf-8",
    )
    for n in range(1, 6):
        body = "".join(f"def test_{k}():\n    pass\n\n" for k in range(n))
        (tmp_path / "t" / f"test_f{n}.py").write_text(body, encoding="utf-8")
    return tmp_path


def _collect(project: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return _pytest(project, "--collect-only", "-q", "t", **env)


def test_hook_is_inert_without_variables(project: Path) -> None:
    assert len(_ids(_collect(project))) == 15


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_hook_shards_join_to_the_full_collection_with_nothing_twice(project: Path, count: int) -> None:
    full = _ids(_collect(project))
    joined: list[str] = []
    for index in range(1, count + 1):
        done = _collect(project, PE_SHARD=str(index), PE_SHARDS=str(count))
        ids = _ids(done)
        assert f"shard {index}/{count}:" in done.stdout and "default weight" in done.stdout
        assert len({i.split("::", 1)[0] for i in ids}) >= 1
        joined += ids
    assert sorted(joined) == sorted(full) and len(joined) == len(set(joined)) == 15


def test_hook_runs_only_its_shard_and_reports_the_deselected_count(project: Path) -> None:
    done = _pytest(project, "-q", "t", PE_SHARD="1", PE_SHARDS="2")
    assert done.returncode == 0, done.stdout
    kept = int(done.stdout.split(" passed")[0].split()[-1])
    deselected = int(done.stdout.split(" deselected")[0].split()[-1])
    assert kept + deselected == 15 and 0 < kept < 15


def test_an_empty_shard_fails_instead_of_passing_green(project: Path) -> None:
    done = _collect(project, PE_SHARD="6", PE_SHARDS="6")  # five files, six shards: the last one is empty
    assert done.returncode == 4 and "selects no test" in done.stderr


@pytest.mark.parametrize(
    "env",
    [
        {"PE_SHARD": "0", "PE_SHARDS": "2"},
        {"PE_SHARD": "3", "PE_SHARDS": "2"},
        {"PE_SHARD": "-1", "PE_SHARDS": "2"},
        {"PE_SHARD": "x", "PE_SHARDS": "2"},
        {"PE_SHARD": "1", "PE_SHARDS": ""},
        {"PE_SHARD": "1"},
        {"PE_SHARDS": "2"},
    ],
)
def test_a_bad_shard_value_stops_pytest_before_any_test_runs(project: Path, env: dict[str, str]) -> None:
    done = _pytest(project, "-q", "t", **env)
    assert done.returncode == 4 and "shard refused" in done.stderr
    assert "passed" not in done.stdout


def _second_directory(project: Path) -> None:
    """A second test directory with its own conftest that imports the same hooks, as tools/agent-workflow/tests does."""
    (project / "u").mkdir()
    (project / "u" / "conftest.py").write_text((project / "t" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    for n in range(1, 4):
        body = "".join(f"def test_{k}():\n    pass\n\n" for k in range(n + 1))
        (project / "u" / f"test_g{n}.py").write_text(body, encoding="utf-8")


@pytest.mark.parametrize("count", [2, 3, 4])
def test_two_conftests_in_one_session_partition_once(project: Path, count: int) -> None:
    """Fix round 1, P2: both conftests register the hook; the partition must not be applied a second time."""
    _second_directory(project)
    full = _ids(_pytest(project, "--collect-only", "-q", "t", "u"))
    assert len(full) == 15 + 9
    joined: list[str] = []
    for index in range(1, count + 1):
        done = _pytest(project, "--collect-only", "-q", "t", "u", PE_SHARD=str(index), PE_SHARDS=str(count))
        assert done.stdout.count(f"shard {index}/{count}:") == 1, done.stdout  # reported once, applied once
        joined += _ids(done)
    assert sorted(joined) == sorted(full) and len(joined) == len(set(joined))


def test_a_child_pytest_with_the_variables_set_runs_its_own_tests_only_through_the_shared_helper(
    project: Path,
) -> None:
    """Fix round 1, P1: a child that loads the conftest must not inherit the job's shard (it has one file only)."""
    child = project / "child"
    child.mkdir()
    (child / "conftest.py").write_text((project / "t" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    (child / "test_only.py").write_text("def test_only():\n    pass\n", encoding="utf-8")
    job = {**os.environ, cs.ENV_SHARD: "2", cs.ENV_SHARDS: "2"}  # what a shard-2-of-2 job hands its children

    def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", "-X", "faulthandler", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir",
             str(child), str(child)],
            cwd=child, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
            creationflags=NO_WINDOW, check=False,
        )

    leaked = run(job)  # the defect: the inherited variables put the child's only file in shard 1, shard 2 is empty
    assert leaked.returncode == 4 and "selects no test" in leaked.stderr, leaked.stdout + leaked.stderr
    scrubbed = run(cs.child_environ(job))
    assert scrubbed.returncode == 0 and "1 passed" in scrubbed.stdout, scrubbed.stdout + scrubbed.stderr


def test_child_environ_drops_both_variables_and_keeps_the_rest() -> None:
    env = {"PE_SHARD": "2", "PE_SHARDS": "3", "pe_shard": "1", "PATH": "x", "PE_OTHER": "y"}
    assert cs.child_environ(env) == {"PATH": "x", "PE_OTHER": "y"}


_SPAWN_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})  # `subprocess.<name>` or the bare import
_SPAWN_BARE = frozenset({"Popen", "check_call", "check_output", "call"})  # `run` alone is too common a helper name
# Spawns that run a child pytest on purpose with the raw environment, by (repo-relative file, enclosing function).
DELIBERATE_RAW_ENV = {"tools/agent-workflow/tests/test_ci_shard.py": frozenset({"run"})}  # shows the leak: exit 4


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _calls_child_environ(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and _callee(n) == "child_environ" for n in ast.walk(node))


def _is_pytest_command(node: ast.AST | None) -> bool:
    """A list or tuple literal that runs pytest: `... "-m", "pytest" ...` or `"pytest", ...` as the first word."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return False
    words = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None for e in node.elts]
    return any(word == "pytest" and (i == 0 or words[i - 1] == "-m") for i, word in enumerate(words))


def _is_spawn(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return isinstance(func.value, ast.Name) and func.value.id == "subprocess" and func.attr in _SPAWN_CALLS
    return isinstance(func, ast.Name) and func.id in _SPAWN_BARE


def _runs_pytest(call: ast.Call, scope: ast.AST) -> bool:
    command = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "args"), None)
    if isinstance(command, ast.Name):  # `cmd = [sys.executable, "-m", "pytest"]` then `subprocess.run(cmd, ...)`
        return any(
            isinstance(n, ast.Assign) and _is_pytest_command(n.value)
            and any(isinstance(t, ast.Name) and t.id == command.id for t in n.targets)
            for n in ast.walk(scope)
        )
    return _is_pytest_command(command)


def _scrubbed(call: ast.Call, scope: ast.AST, functions: dict[str, ast.AST]) -> bool:
    """The `env=` of this one call is built by `child_environ()`: directly, through a variable assigned in the same
    function, or through a helper of the same file whose body calls it. No `env=` means the parent's environment."""
    env = next((k.value for k in call.keywords if k.arg == "env"), None)
    if env is None:
        return False
    if _calls_child_environ(env):
        return True
    if isinstance(env, ast.Name):
        return any(
            isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and _calls_child_environ(n.value)
            and any(isinstance(t, ast.Name) and t.id == env.id
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
            for n in ast.walk(scope)
        )
    if isinstance(env, ast.Call) and (helper := functions.get(_callee(env) or "")) is not None:
        return _calls_child_environ(helper)
    return False


def unscrubbed_pytest_spawns(source: str, exempt: frozenset[str] = frozenset()) -> list[int]:
    """Line of every child pytest spawn in `source` whose own `env=` is not built by `child_environ()` (decision 168).

    The check is per spawn, not per file: a file with one scrubbed spawn and one that inherits the job's shard variables
    reports the second. `exempt` names functions whose spawns run with the raw environment on purpose."""
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found: list[int] = []

    def visit(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            if isinstance(child, ast.Call) and _is_spawn(child) and _runs_pytest(child, inner):
                if getattr(inner, "name", None) not in exempt and not _scrubbed(child, inner, functions):
                    found.append(child.lineno)
            visit(child, inner)

    visit(tree, tree)
    return sorted(found)


def test_every_child_pytest_spawn_takes_its_own_environment_from_the_shared_helper() -> None:
    """Class guard (fix round 1, per spawn since P3-SWEEP-1): each spawn of `python -m pytest` in either suite scrubs
    the shard variables itself; a scrubbed spawn elsewhere in the same file does not cover it."""
    offenders = [
        f"{p.relative_to(ROOT).as_posix()}:{line}"
        for d in SUITES.values()
        for p in sorted((ROOT / d).rglob("*.py"))
        for line in unscrubbed_pytest_spawns(
            p.read_text(encoding="utf-8"), DELIBERATE_RAW_ENV.get(p.relative_to(ROOT).as_posix(), frozenset())
        )
    ]
    assert offenders == [], f"child pytest started with the parent environment: {offenders}"


def test_the_deliberate_raw_environment_spawn_is_the_only_exemption_and_still_exists() -> None:
    for name, functions in DELIBERATE_RAW_ENV.items():
        found = unscrubbed_pytest_spawns((ROOT / name).read_text(encoding="utf-8"))
        assert len(found) == len(functions) == 1, f"{name}: the exemption names a spawn that no longer exists: {found}"


_TWO_SPAWNS = '''
import os
import subprocess
import sys
from ci_shard import child_environ

def scrubbed():
    return subprocess.run([sys.executable, "-m", "pytest", "-q"], env=child_environ(), check=False)

def inherits():
    return subprocess.run([sys.executable, "-m", "pytest", "-q"], check=False)
'''


def test_a_file_with_two_pytest_spawns_and_one_scrubbed_is_reported_at_the_other() -> None:
    """The guard is per spawn: the old per-file text check passed this file because one call used the helper."""
    assert "child_environ(" in _TWO_SPAWNS  # what the per-file check looked for
    found = unscrubbed_pytest_spawns(_TWO_SPAWNS)
    assert found == [_TWO_SPAWNS.splitlines().index("    return subprocess.run([sys.executable, \"-m\", \"pytest\", \"-q\"], check=False)") + 1]
    same_function = _TWO_SPAWNS.replace("def inherits():\n    return", "def inherits():\n    child_environ()\n    return")
    assert len(unscrubbed_pytest_spawns(same_function)) == 1  # a helper call nearby is not the spawn's own env


@pytest.mark.parametrize(
    ("body", "reported"),
    [
        ('subprocess.run([sys.executable, "-m", "pytest"], env=child_environ())', 0),
        ('subprocess.Popen([sys.executable, "-m", "pytest"], env=child_environ())', 0),
        ('subprocess.run([sys.executable, "-m", "pytest"], env=os.environ)', 1),
        ('subprocess.run([sys.executable, "-m", "pytest"], env=dict(os.environ, X="1"))', 1),
        ('subprocess.run([sys.executable, "-m", "pytest"], env=None)', 1),
        ('subprocess.Popen([sys.executable, "-m", "pytest"])', 1),
        ('subprocess.check_call(("pytest", "-q"))', 1),
        ('subprocess.run(["pytest", "-q"], env=child_environ({}))', 0),
        ('cmd = [sys.executable, "-m", "pytest"]\n    subprocess.run(cmd)', 1),
        ('cmd = [sys.executable, "-m", "pytest"]\n    subprocess.run(cmd, env=child_environ())', 0),
        ('env = {k: v for k, v in child_environ().items()}\n    subprocess.run([sys.executable, "-m", "pytest"], env=env)', 0),
        ('env = dict(os.environ)\n    subprocess.run([sys.executable, "-m", "pytest"], env=env)', 1),
        ('subprocess.run([sys.executable, "-c", "print(1)"])', 0),  # a child that is not pytest
        ('subprocess.run([sys.executable, "-m", "pytest"], env=build())', 1),  # an unknown helper proves nothing
    ],
)
def test_the_spawn_guard_recognizes_each_way_a_child_pytest_is_started(body: str, reported: int) -> None:
    source = f"import os, subprocess, sys\nfrom ci_shard import child_environ\n\ndef test_x():\n    {body}\n"
    assert len(unscrubbed_pytest_spawns(source)) == reported


def test_the_spawn_guard_follows_a_same_file_helper_that_builds_the_environment() -> None:
    source = (
        "import subprocess, sys\nfrom ci_shard import child_environ\n\n"
        "def _env():\n    return child_environ()\n\n"
        "def _raw():\n    return {}\n\n"
        "def good():\n    subprocess.run([sys.executable, '-m', 'pytest'], env=_env())\n\n"
        "def bad():\n    subprocess.run([sys.executable, '-m', 'pytest'], env=_raw())\n"
    )
    assert unscrubbed_pytest_spawns(source) == [14]
    assert unscrubbed_pytest_spawns(source, frozenset({"bad"})) == []


# --- the workflow file -----------------------------------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_the_matrix_lists_every_shard_of_every_suite_once() -> None:
    job = _workflow()["jobs"]["test"]
    entries = job["strategy"]["matrix"]["include"]
    seen = [(e["os"], e["suite"], e["shard"]) for e in entries]
    assert len(seen) == len(set(seen))  # no job twice
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        groups.setdefault((e["os"], e["suite"]), []).append(e)
    assert set(groups) == {(o, s) for o in ("ubuntu-latest", "windows-latest") for s in ("pipeline", "tooling")}
    for (_os, _suite), members in groups.items():
        counts = {e["shards"] for e in members}
        assert len(counts) == 1, "one shard count per (os, suite)"
        # Shards 1..N all present: a missing one would leave its files untested while the rest stay green.
        assert sorted(e["shard"] for e in members) == list(range(1, counts.pop() + 1))
    assert job["strategy"]["fail-fast"] is False


def test_suite_steps_take_the_shard_from_the_matrix_environment_and_a_guard_refuses_bad_values() -> None:
    job = _workflow()["jobs"]["test"]
    steps = job["steps"]
    for step in steps:
        if step.get("name") in ("Pipeline suite", "Tooling suite"):
            assert step["env"]["PE_SHARD"] == "${{ matrix.shard }}" and step["env"]["PE_SHARDS"] == "${{ matrix.shards }}"
            assert "${{" not in step["run"], "matrix values enter through the environment, never script text"
        elif step.get("name") == "Exit stress after an in-memory Parquet read (Linux)":
            assert "PE_SHARD" not in step["env"] and "PE_SHARDS" not in step["env"]  # one named test, never sharded
            # Exactly once: one Linux pipeline shard runs it, and that shard exists in the matrix.
            assert step["if"] == "${{ matrix.suite == 'pipeline' && runner.os == 'Linux' && matrix.shard == 1 }}"
    (guard,) = [s for s in steps if s.get("name") == "Refuse a malformed shard"]
    assert guard["env"] == {"PE_SHARD": "${{ matrix.shard }}", "PE_SHARDS": "${{ matrix.shards }}"}
    assert guard["run"] == "python tools/agent-workflow/ci_shard.py check"
    names = [s.get("name") for s in steps]
    assert names.index("Refuse a malformed shard") < names.index("Pipeline suite")
    assert sum(1 for e in job["strategy"]["matrix"]["include"]
               if e["suite"] == "pipeline" and e["os"] == "ubuntu-latest" and e["shard"] == 1) == 1


def test_the_expected_slowest_shard_stays_near_an_even_split() -> None:
    """Weight-table forecast of the slowest job per (os, suite): within 15 % of a perfect split (decision 168)."""
    default, table = cs.load_weights()
    entries = _workflow()["jobs"]["test"]["strategy"]["matrix"]["include"]
    for suite, suite_dir in SUITES.items():
        files = _real_files(suite_dir)
        for count in {e["shards"] for e in entries if e["suite"] == suite}:
            loads = [sum(table.get(f, default) for f in s) for s in cs.assign(files, table, default, count)]
            assert max(loads) <= 1.15 * sum(loads) / count, (suite, count, loads)
