"""Tests for audit_extract.py (user-words digest) and the audit_brief.md template."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_t_audit", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_t_audit"] = module
    spec.loader.exec_module(module)
    return module


ax = load("audit_extract")
bl = load("brief_lint")


def user(text: object, when: str, **extra: object) -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": when,
        "sessionId": "abcdef123456",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
        **extra,
    }


STAMP = "2026-10-29T07:14:00.000Z"
OLD = "2026-10-20T07:14:00.000Z"
RECORDS = [
    user("Tui nghĩ làm cả 4 nha", STAMP),
    user([{"type": "text", "text": "hai dong\nnua"}, {"type": "image", "source": {}}], STAMP),
    user([{"type": "tool_result", "content": "SECRET TOOL OUTPUT"}], STAMP),
    user(
        "<task-notification>done</task-notification>", STAMP, origin={"kind": "task-notification"}
    ),
    user("peer says hi", STAMP, origin={"kind": "peer", "from": "x"}),
    user("meta text", STAMP, isMeta=True),
    user("This session is being continued from a summary", STAMP, isCompactSummary=True),
    user("sidechain text", STAMP, isSidechain=True),
    user("too old", OLD),
    user("legacy plain text without origin", STAMP, origin=None),
    user("<system-reminder>x</system-reminder>", STAMP, origin=None),
    {
        "type": "assistant",
        "timestamp": STAMP,
        "message": {"role": "assistant", "content": "ASSISTANT TEXT"},
    },
]


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    (root / ".tmp" / "pm-state" / "audit").mkdir(parents=True)
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    lines = [json.dumps(r) for r in RECORDS] + ["{broken json", ""]
    (transcripts / "s1.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return root, transcripts


def run(root: Path, transcripts: Path, *args: str) -> int:
    return ax.main(["--project-root", str(root), "--transcripts-dir", str(transcripts), *args])


def digest(root: Path) -> str:
    return next((root / ".tmp" / "pm-state" / "audit").glob("user-words-*.md")).read_text(
        encoding="utf-8"
    )


def test_only_what_the_user_typed_reaches_the_digest(project, capsys) -> None:
    root, transcripts = project
    assert run(root, transcripts, "--since", "2026-10-25") == 0
    text = digest(root)
    assert "Tui nghĩ làm cả 4 nha" in text
    assert "hai dong\n  nua" in text
    assert "legacy plain text without origin" in text
    for banned in (
        "SECRET TOOL OUTPUT",
        "task-notification",
        "peer says hi",
        "meta text",
        "This session is being",
        "sidechain text",
        "too old",
        "ASSISTANT TEXT",
        "system-reminder",
    ):
        assert banned not in text, banned
    assert "(abcdef12)" in text and "3 messages" in capsys.readouterr().out


def test_since_defaults_to_the_newest_audit_file(project) -> None:
    root, transcripts = project
    audit = root / ".tmp" / "pm-state" / "audit"
    (audit / "audit-2026-10-23.md").write_text("old audit", encoding="utf-8")
    (audit / "audit-A.md").write_text("not dated", encoding="utf-8")
    assert ax.last_audit_date(audit) == dt.date(2026, 10, 23)
    assert run(root, transcripts) == 0
    assert "too old" not in digest(root) and "Tui nghĩ" in digest(root)
    assert run(root, transcripts, "--since", "2026-10-01") == 0
    assert "too old" in digest(root)


def test_secrets_are_masked_and_secrets_paths_dropped() -> None:
    text = ax.redact(
        "key sk-ant-abcdefghijklmnop1234 and AIzaSyA1234567890abcdefghij and .secrets/gemini.key ok"
    )
    assert "sk-ant" not in text and "AIza" not in text and ".secrets/gemini" not in text
    assert text.count("[secret]") == 2 and "[secrets-path]" in text
    assert "Bearer" not in ax.redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")


def test_long_messages_are_cut_and_old_ones_dropped_loudly(project) -> None:
    root, transcripts = project
    big = [user(f"message {n} " + "x" * 300, f"2026-10-29T1{n}:00:00.000Z") for n in range(5)]
    (transcripts / "s2.jsonl").write_text("\n".join(json.dumps(r) for r in big), encoding="utf-8")
    args = ("--since", "2026-10-28", "--max-message-chars", "100", "--max-total-chars", "700")
    assert run(root, transcripts, *args) == 0
    text = digest(root)
    assert "[cut:" in text and "OLDER MESSAGES OMITTED" in text.split("\n")[3]
    assert "message 4" in text and "message 0 " not in text  # the newest are kept


def test_output_and_missing_directory_are_handled(project, tmp_path: Path, capsys) -> None:
    root, transcripts = project
    with pytest.raises(SystemExit):
        run(root, transcripts, "--out", str(tmp_path / "outside.md"))
    assert run(root, tmp_path / "nowhere") == 2
    assert "not found" in capsys.readouterr().err


def test_default_transcripts_directory_matches_claude_code_naming() -> None:
    name = ax.NON_ALNUM.sub("-", "D:\\Project_github\\Enhance_Diffusion_model_pipeline")
    assert name == "D--Project-github-Enhance-Diffusion-model-pipeline"
    assert ax.default_transcripts_dir(REPO).parent == Path.home() / ".claude" / "projects"


def test_the_audit_brief_is_a_valid_scout_brief_with_the_agreed_contract() -> None:
    template = (TOOLS / "audit_brief.md").read_text(encoding="utf-8")
    filled = template.replace("<DATE>", "2026-10-29").replace("<SINCE>", "2026-10-28")
    footer = (REPO / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md").read_text(encoding="utf-8")
    findings = bl.lint_message(filled + "\n\n" + footer, full=True, footer=footer)
    assert findings == []
    for needle in (
        "report only",
        "PM verifies every",
        "met / not met / cannot tell",
        "user-words-",
        "Never open a raw transcript",
        "never open `.secrets/`",
        "Write only the one output file",
        "remembered only",
    ):
        assert needle in template or needle.lower() in template.lower(), needle


# ---- property tests (decision 130) ----
JSONISH = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
    lambda kids: st.one_of(
        st.lists(kids, max_size=3), st.dictionaries(st.text(max_size=5), kids, max_size=4)
    ),
    max_leaves=10,
)


@given(JSONISH)
def test_property_is_human_never_raises(record: object) -> None:
    assert ax.is_human(record) in (True, False)


@given(st.text(max_size=60), st.sampled_from(["tool_result", "tool_use", "thinking"]))
def test_property_a_user_record_with_a_non_text_block_is_never_typed_text(
    text: str, kind: str
) -> None:
    record = user([{"type": "text", "text": text}, {"type": kind}], STAMP)
    assert ax.message_text(record) is None


@given(
    st.text(max_size=80),
    st.sampled_from(["sk-ant-", "AIza", "hf_", "ghp_"]),
    st.text("abcDEF0123456789", min_size=24, max_size=30),
)
def test_property_known_key_shapes_never_survive(prefix: str, shape: str, tail: str) -> None:
    secret = shape + tail
    assert secret not in ax.redact(f"{prefix} {secret} end")


@given(st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=15), st.integers(150, 600))
def test_property_the_digest_body_respects_the_total_cap(texts: list[str], cap: int) -> None:
    base = dt.datetime(2026, 10, 29, 8, 0).astimezone()
    messages = [ax.Message(base + dt.timedelta(minutes=i), "s", t) for i, t in enumerate(texts)]
    out = ax.render(messages, base, 40, cap, 1)
    entries_block = out.split("\n\n", 2)[2]
    assert len(entries_block) <= cap
