"""Deterministic test-file sharding for the CI matrix (decision 168, R-025, docs/ci.md).

One pytest process runs one shard: the CI job sets `PE_SHARD` (1-based index) and `PE_SHARDS` (count) in its
environment, and the collection hook below keeps only the tests of the files assigned to that shard. Whole files are
assigned, never single tests, so a test belongs to exactly one shard because its file does. With neither variable set
the hook does nothing and the full suite runs. The two variables are CI protocol values, not profile leaves
(AGENTS.md section 5: protocol-bound): a malformed, zero, negative, out-of-range or half-set value is refused.

The assignment is longest-processing-time-first over the committed weight table `ci_shard_weights.json` (seconds of
test time per file, measured once locally; a file missing from the table gets `default_weight_s` and is still
assigned). The partition depends only on the set of files and their weights, never on their order or on the
operating system, so every shard computes the same partition on its own.

Usage in a conftest: `from ci_shard import pytest_collection_modifyitems, pytest_report_collectionfinish`.
CLI: `python ci_shard.py check` exits non-zero unless both variables are set and valid (the CI guard step).
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ENV_SHARD = "PE_SHARD"
ENV_SHARDS = "PE_SHARDS"
WEIGHTS_FILE = Path(__file__).with_name("ci_shard_weights.json")  # artifact-bound: committed beside this module
_WEIGHT_KEYS = {"_comment", "schema_version", "default_weight_s", "weights"}


class ShardError(ValueError):
    """A shard setting or weight table that must be refused."""


def _positive_int(name: str, value: str) -> int:
    # Plain ASCII digits without a leading zero: no sign, no space, no "1.0", no "٣" (str.isdecimal accepts it).
    if not (value.isascii() and value.isdecimal() and not value.startswith("0")):
        raise ShardError(f"{name} must be a positive decimal integer, got {value!r}")
    return int(value)


def parse_shard(environ: Mapping[str, str], *, required: bool = False) -> tuple[int, int] | None:
    """Return (index, count) from the environment, None when neither variable is set and `required` is false."""
    raw_index, raw_count = environ.get(ENV_SHARD), environ.get(ENV_SHARDS)
    if raw_index is None and raw_count is None:
        if required:
            raise ShardError(f"{ENV_SHARD} and {ENV_SHARDS} must both be set")
        return None
    if raw_index is None or raw_count is None:
        raise ShardError(f"{ENV_SHARD} and {ENV_SHARDS} must be set together")
    index, count = _positive_int(ENV_SHARD, raw_index), _positive_int(ENV_SHARDS, raw_count)
    if index > count:
        raise ShardError(f"{ENV_SHARD}={index} is outside 1..{ENV_SHARDS}={count}")
    return index, count


def child_environ(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a child process that is not part of the sharded run, without the shard variables.

    The variables are process environment, so every child inherits them. A child pytest that loads one of the
    conftests would apply the hook to its own few files and could land in an empty shard. Every test that starts a
    child Python or pytest process takes its environment from here (test_ci_shard.py guards the rule).
    """
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k.upper() not in (ENV_SHARD, ENV_SHARDS)}


def load_weights(path: Path = WEIGHTS_FILE) -> tuple[float, dict[str, float]]:
    """Read and validate the weight table: (default weight, weight per posix repo-relative file key)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShardError(f"weight table {path.name} unreadable: {exc}") from exc
    if not isinstance(data, dict) or set(data) - _WEIGHT_KEYS or _WEIGHT_KEYS - {"_comment"} - set(data):
        raise ShardError(f"weight table {path.name}: keys must be {sorted(_WEIGHT_KEYS)}")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise ShardError("weight table schema_version must be 1")
    default = _weight("default_weight_s", data["default_weight_s"])
    table = data["weights"]
    if not isinstance(table, dict):
        raise ShardError("weight table 'weights' must be an object")
    return default, {_key_text(k): _weight(k, v) for k, v in table.items()}


def _weight(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ShardError(f"weight {name!r} must be a finite number above 0, got {value!r}")
    return float(value)


def _key_text(key: str) -> str:
    if normalise(key) != key:
        raise ShardError(f"weight key {key!r} must be a posix path relative to the project root")
    return key


def normalise(path: str) -> str:
    """Spell a repo-relative path the one way the table and the partition use: forward slashes, no leading ./."""
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def assign(files: Sequence[str], weights: Mapping[str, float], default_weight: float, count: int) -> list[list[str]]:
    """Split `files` into `count` shards, balanced by weight, every file in exactly one shard.

    Longest-processing-time first: files sorted by (-weight, name), each placed on the least-loaded shard (lowest index
    on a tie). The result does not depend on the order or spelling of `files`. For any input the heaviest shard is at
    most the mean load plus the heaviest single weight (list-scheduling bound), which the property test checks.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ShardError(f"shard count must be a positive integer, got {count!r}")
    keys = [normalise(f) for f in files]
    if len(set(keys)) != len(keys):
        raise ShardError("duplicate file in the shard input")
    ranked = sorted(keys, key=lambda k: (-weights.get(k, default_weight), k))
    loads = [0.0] * count
    shards: list[list[str]] = [[] for _ in range(count)]
    for key in ranked:
        target = min(range(count), key=lambda i: (loads[i], i))
        shards[target].append(key)
        loads[target] += weights.get(key, default_weight)
    return [sorted(s) for s in shards]


# --- pytest hooks ------------------------------------------------------------------------------------------------


def _state(config) -> dict:  # per session: whether the partition was applied, and its report lines
    import pytest

    key = _state.__dict__.setdefault("key", pytest.StashKey[dict]())
    return config.stash.setdefault(key, {"applied": False, "lines": []})


def pytest_collection_modifyitems(config, items) -> None:
    import pytest

    # Two conftests (tests/ and tools/agent-workflow/tests/) register this hook, so a session over both directories
    # calls it twice; the partition is made once over every collected file, never again over the filtered list.
    state = _state(config)
    if state["applied"]:
        return
    try:
        shard = parse_shard(os.environ)
        if shard is None:
            return
        index, count = shard
        default, table = load_weights()
        root = Path(config.rootpath)
        keyed = []
        for item in items:
            try:
                keyed.append((normalise(Path(item.path).relative_to(root).as_posix()), item))
            except ValueError as exc:
                raise ShardError(f"{item.nodeid} lies outside the project root {root}") from exc
        files = sorted({key for key, _ in keyed})
        mine = set(assign(files, table, default, count)[index - 1])
    except ShardError as exc:
        raise pytest.UsageError(f"shard refused: {exc}") from exc
    kept = [item for key, item in keyed if key in mine]
    dropped = [item for key, item in keyed if key not in mine]
    if not kept:  # a green shard that ran nothing would hide the tests of its files
        raise pytest.UsageError(f"shard {index}/{count} selects no test from {len(files)} collected files")
    state["applied"] = True
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept
    unlisted = sum(1 for f in mine if f not in table)
    state["lines"].append(
        f"shard {index}/{count}: {len(mine)} of {len(files)} files, {len(kept)} tests kept, {len(dropped)} deselected, "
        f"{unlisted} file(s) at the default weight {default:g} s"
    )


def pytest_report_collectionfinish(config) -> list[str]:
    state = _state(config)  # registered by two conftests too: the shard line is reported once
    lines, state["lines"] = list(state["lines"]), []
    return lines


def main(argv: Sequence[str]) -> int:
    if list(argv) != ["check"]:
        print("usage: ci_shard.py check", file=sys.stderr)
        return 2
    try:
        index, count = parse_shard(os.environ, required=True)  # type: ignore[misc]
        load_weights()
    except ShardError as exc:
        print(f"shard refused: {exc}", file=sys.stderr)
        return 1
    print(f"shard {index}/{count} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
