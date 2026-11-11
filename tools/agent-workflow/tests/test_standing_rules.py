"""Tests for hooks/standing_rules.py, .agents/policy/USER_STANDING_RULES.md and the pinned .agents/policy/AGENT_PEER_RULES.md."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parents[1]
HOOK = TOOLS / "hooks" / "standing_rules.py"
GOOD = "S01 | rule | source | check\nS02 | other rule | decision 9 | remembered only\n"


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load(HOOK, "standing_rules")
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agents" / "policy").mkdir(parents=True)
    (tmp_path / ".agents" / "policy" / "USER_STANDING_RULES.md").write_text(GOOD, encoding="utf-8")
    (tmp_path / ".agents" / "policy" / "AGENT_PEER_RULES.md").write_text(
        "# Peer\nrule\n", encoding="utf-8"
    )
    budgets = tmp_path / "doc_budgets.json"
    budgets.write_text(
        json.dumps(
            {
                "project": {
                    ".agents/policy/USER_STANDING_RULES.md": 500,
                    ".agents/policy/AGENT_PEER_RULES.md": 500,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "BUDGETS", budgets)
    monkeypatch.delenv(module.WORKER_ENV_FLAG, raising=False)
    return module


def run(hook: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys, event: object) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert hook.main() == 0
    out = capsys.readouterr().out
    if not out:
        return ""
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "SessionStart"
    return str(data["additionalContext"])


def test_injects_peer_rules_then_the_whole_list(hook, monkeypatch, capsys) -> None:
    text = run(hook, monkeypatch, capsys, {"session_id": "pm"})
    assert text.index("Peer rules") < text.index("User standing rules")
    assert "S01 | rule | source | check" in text and "S02 | other rule" in text
    assert "2 rules, 1 remembered only" in text
    assert "[standing_rules]" not in text


def test_over_budget_is_loud_and_not_cut(hook, monkeypatch, capsys, tmp_path: Path) -> None:
    lines = [f"S{n:02d} | rule {'x' * 40} | source | remembered only" for n in range(1, 21)]
    (tmp_path / ".agents" / "policy" / "USER_STANDING_RULES.md").write_text("\n".join(lines), encoding="utf-8")
    text = run(hook, monkeypatch, capsys, {})
    assert text.startswith("[standing_rules] OVER BUDGET: .agents/policy/USER_STANDING_RULES.md")
    assert all(line in text for line in lines)  # uncut, down to the last rule
    assert "[cut" not in text


def test_total_over_the_harness_cap_is_flagged_first(
    hook, monkeypatch, capsys, tmp_path: Path
) -> None:
    big = "\n".join(f"S{n:03d} | {'y' * 60} | source | check" for n in range(200))
    (tmp_path / ".agents" / "policy" / "USER_STANDING_RULES.md").write_text(big, encoding="utf-8")
    text = run(hook, monkeypatch, capsys, {})
    first = text.split("\n")[0]
    assert first.startswith("[standing_rules] TOTAL ") and "the harness drops the tail" in first


def test_missing_files_warn_and_do_not_crash(hook, monkeypatch, capsys, tmp_path: Path) -> None:
    (tmp_path / ".agents" / "policy" / "USER_STANDING_RULES.md").unlink()
    text = run(hook, monkeypatch, capsys, {})
    assert "MISSING: .agents/policy/USER_STANDING_RULES.md" in text
    assert "Peer rules" in text  # the other file still goes out
    (tmp_path / ".agents" / "policy" / "AGENT_PEER_RULES.md").unlink()
    text = run(hook, monkeypatch, capsys, {})
    assert "MISSING: .agents/policy/AGENT_PEER_RULES.md" in text and "MISSING: .agents/policy/USER_STANDING_RULES.md" in text


def test_unreadable_budget_file_warns_but_still_injects(
    hook, monkeypatch, capsys, tmp_path: Path
) -> None:
    (tmp_path / "doc_budgets.json").write_text("{not json", encoding="utf-8")
    text = run(hook, monkeypatch, capsys, {})
    assert "no budget for .agents/policy/USER_STANDING_RULES.md" in text and "S01 | rule" in text


def test_malformed_and_repeated_rule_lines_are_named(
    hook, monkeypatch, capsys, tmp_path: Path
) -> None:
    (tmp_path / ".agents" / "policy" / "USER_STANDING_RULES.md").write_text(
        "S01 | a | b | c\nS02 | only three | fields\nS01 | dup | b | c\nS03 |  | empty | c\n",
        encoding="utf-8",
    )
    text = run(hook, monkeypatch, capsys, {})
    assert "MALFORMED rule line S02: needs 4 non-empty fields" in text
    assert "MALFORMED rule line S01: repeated id" in text
    assert "MALFORMED rule line S03" in text


def test_worker_sessions_get_nothing(hook, monkeypatch, capsys, tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "cli-workers.json").write_text(
        json.dumps({"workers": {"w": {"session_id": "s-w"}}}), encoding="utf-8"
    )
    assert run(hook, monkeypatch, capsys, {"session_id": "s-w"}) == ""
    monkeypatch.setenv(hook.WORKER_ENV_FLAG, "1")
    assert run(hook, monkeypatch, capsys, {"session_id": "pm"}) == ""


def test_bad_stdin_and_internal_failure_fail_open(hook, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main() == 0
    assert (
        "Peer rules"
        in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    )

    def boom() -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(hook, "build_context", boom)
    text = run(hook, monkeypatch, capsys, {})
    assert text.startswith("[standing_rules] hook failed (RuntimeError)")


def test_crlf_and_bom_do_not_change_the_text(hook, monkeypatch, capsys, tmp_path: Path) -> None:
    (tmp_path / ".agents" / "policy" / "USER_STANDING_RULES.md").write_bytes(
        b"\xef\xbb\xbf" + GOOD.replace("\n", "\r\n").encode()
    )
    text = run(hook, monkeypatch, capsys, {})
    assert "\r" not in text and "2 rules" in text and "﻿" not in text


RULE_FIELD = st.text(
    st.characters(
        blacklist_characters="|\n\r\x85\u2028\u2029\x0b\x0c\x1c\x1d\x1e",
        blacklist_categories=("Cs",),
    ),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")


@given(st.lists(st.tuples(RULE_FIELD, RULE_FIELD, RULE_FIELD), min_size=1, max_size=20))
def test_property_well_formed_lines_always_parse(rows) -> None:
    module = load(HOOK, "standing_rules_prop")
    text = "\n".join(f"S{i:02d} | {a} | {b} | {c}" for i, (a, b, c) in enumerate(rows))
    ids, unchecked, problems = module.parse_rules(text)
    assert problems == [] and ids == [f"S{i:02d}" for i in range(len(rows))]
    assert len(unchecked) == sum(1 for _, _, c in rows if c.strip().strip("`") == "remembered only")


@given(st.text(max_size=300))
def test_property_parse_never_raises_and_ids_are_unique(text: str) -> None:
    module = load(HOOK, "standing_rules_prop2")
    ids, unchecked, problems = module.parse_rules(text)
    assert len(ids) == len(set(ids)) and set(unchecked) <= set(ids)
    assert all(isinstance(problem, str) for problem in problems)


@given(st.integers(min_value=2, max_value=8))  # one field has no `|`, so it is free text
def test_property_only_exactly_four_fields_is_a_valid_rule(count: int) -> None:
    module = load(HOOK, "standing_rules_prop3")
    ids, _, problems = module.parse_rules(" | ".join(["S01"] + ["a"] * (count - 1)))
    assert (problems == []) == (count == 4) and (ids == ["S01"]) == (count == 4)


def test_the_real_list_is_well_formed_within_budget() -> None:
    """The checked-in list parses, fits its budget and points at the decisions index (no project wording pinned)."""
    module = load(HOOK, "standing_rules_real")
    text = (REPO / module.RULES_REL).read_text(encoding="utf-8")
    ids, _unchecked, problems = module.parse_rules(text.strip())
    budget = json.loads((TOOLS / "doc_budgets.json").read_text(encoding="utf-8"))["project"]
    assert problems == [] and ids
    assert len(ids) == len(set(ids))
    assert len(text) <= budget[module.RULES_REL]
    assert "tasks/DECISIONS_INDEX.md" in text


def test_the_real_peer_rules_exist_within_budget() -> None:
    module = load(HOOK, "standing_rules_peer")
    text = (REPO / module.PEER_REL).read_text(encoding="utf-8")
    budget = json.loads((TOOLS / "doc_budgets.json").read_text(encoding="utf-8"))["project"]
    assert text.strip() and len(text) <= budget[module.PEER_REL]


def test_budgets_cover_both_injected_files_and_fit_the_harness_cap() -> None:
    module = load(HOOK, "standing_rules_budget")
    config = load(TOOLS / "memory_hooks_config.py", "memory_hooks_config")
    budgets = json.loads((TOOLS / "doc_budgets.json").read_text(encoding="utf-8"))["project"]
    assert module.HARD_INJECT_CAP == config.HARD_INJECT_CAP
    parts = [budgets[module.RULES_REL], budgets[module.PEER_REL]]
    assert sum(parts) < module.HARD_INJECT_CAP  # leaves room for the warning lines
