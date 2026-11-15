"""hooks/question_guard.py (PreToolUse on ArtifactData, decisions 503, T-AUDIT-GATES): a question is checked before it is asked.

The input shape comes from PM session transcripts: `tool_input` = {action, url, collection, doc_id, data, if_version};
`action: "batch"` carries `writes: [{op, collection, doc_id, data, if_version}]`.

Invariant (an allowlist): a question with options passes only when it carries evidence, names existing decisions and
standing rules it was checked against, and repeats no option of an answered question unless the user reopened that
question in quoted words. Anything the guard cannot read is a finding, never a pass.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest
import question_guard as qg
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

HOOK = Path(qg.__file__).resolve()
URL = "https://claude.ai/artifact/x"
OPTIONS = [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]
CHECKED = ["decision 148, decision 503 and S03"]

# The 07:59 "smoke first" re-offer of 2026-10-30: option B of the 06:49 question, asked again.
SMOKE_OPTION = {
    "label": "Option B",
    "description": "Smoke first: run one job on the laptop, then stop for the PM verdict",
}
# "Smoke first" in full-width Latin letters (built, so the source holds no ambiguous characters).
FULLWIDTH_SMOKE = "".join(
    chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in "Smoke first"
)
ANSWERED_0649 = {
    "title": "How do we start the round?",
    "status": "answered",
    "answer": "B",
    "ref": "q-0649",
    "options": [
        {"label": "Option A", "description": "Run the whole batch at once"},
        SMOKE_OPTION,
    ],
}
DECISIONS = "| Row | Date | Rule |\n| 1 | 2026-09-25 | x |\n| 148 | 2026-10-09 | y |\n| 503 | 2026-10-28 | z |\n"
ARCHIVE = "| 188a | 2026-10-10 | x |\n| 60 | 2026-09-26 | y |\n"
RULES = "S01 | Never assume | x\nS03 | Never re-ask | x\nS06 | Questions | x\nS08 | Verdict | x\n"


def question(**data: Any) -> dict[str, Any]:
    return {"title": "t", "order": 1, "options": OPTIONS, "checked_against": CHECKED, **data}


def single(
    action: str, data: dict[str, Any], collection: str = "questions", doc_id: str = "q-1"
) -> dict[str, Any]:
    return {
        "tool_name": "ArtifactData",
        "tool_input": {
            "action": action,
            "url": URL,
            "collection": collection,
            "doc_id": doc_id,
            "data": data,
        },
    }


def batch(*writes: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": "ArtifactData",
        "tool_input": {"action": "batch", "url": URL, "writes": list(writes)},
    }


def item(op: str, data: dict[str, Any], collection: str = "questions") -> dict[str, Any]:
    return {"op": op, "collection": collection, "doc_id": "q-2", "data": data}


def _copy_tree(
    tmp: Path,
    cfg: dict[str, Any],
    answered: dict[str, Any] | None = None,
    *,
    id_files: bool = True,
    mirror: bool = True,
) -> Path:
    """Copy the hook under `tmp` with `cfg` as its guard config, the id files and the answered mirror."""
    tools = tmp / "tools" / "agent-workflow"
    (tools / "hooks").mkdir(parents=True)
    for name in ("guard_config.py", "question_guard.py"):
        (tools / "hooks" / name).write_bytes((HOOK.parent / name).read_bytes())
    (tools / "guard_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if id_files:
        (tmp / "tasks" / "archive").mkdir(parents=True)
        (tmp / "tasks" / "DECISIONS_INDEX.md").write_text(DECISIONS, encoding="utf-8")
        (tmp / "tasks" / "archive" / "DECISIONS_INDEX-001-200.md").write_text(
            ARCHIVE, encoding="utf-8"
        )
        (tmp / ".agents" / "policy").mkdir(parents=True)
        (tmp / ".agents" / "policy" / "USER_STANDING_RULES.md").write_text(RULES, encoding="utf-8")
    if mirror:
        board = tmp / cfg["question_guard"]["answered_dir"]
        board.mkdir(parents=True)
        for stem, document in (
            answered if answered is not None else {"q-0649": ANSWERED_0649}
        ).items():
            text = document if isinstance(document, str) else json.dumps(document)
            (board / f"{stem}.json").write_text(text, encoding="utf-8")
    return tools / "hooks" / "question_guard.py"


def block_config() -> dict[str, Any]:
    cfg = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    cfg["question_guard"]["mode"] = "block"
    return cfg


BLOCK_SCRIPT: Path | None = None


@pytest.fixture(scope="module", autouse=True)
def _block_tree(tmp_path_factory: pytest.TempPathFactory) -> None:
    # The committed mode is a deployment choice (decision 509 runs it as `warn`); the default
    # tests pin the blocking behaviour on a copy so they do not depend on that choice.
    global BLOCK_SCRIPT
    BLOCK_SCRIPT = _copy_tree(tmp_path_factory.mktemp("block"), block_config())


def run(
    event: Any,
    cfg: dict[str, Any] | None = None,
    tmp: Path | None = None,
    **tree: Any,
) -> tuple[int, str, str]:
    """Run the real hook process; `cfg` (or tree options) build a replacement tree under `tmp`."""
    assert BLOCK_SCRIPT is not None
    script = BLOCK_SCRIPT
    if cfg is not None or tree:
        assert tmp is not None
        script = _copy_tree(tmp, cfg if cfg is not None else block_config(), **tree)
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(script)],
        input=event if isinstance(event, str) else json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",  # as Claude Code sends it; the locale code page (cp1252 on CI) cannot encode a BOM
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


GOOD = [
    ["tools/agent-workflow/hooks/tool_guard.py:123", "decision 148"],  # one per option
    ["user words: 'ok 3 hook'"],  # one for the whole question
]


@pytest.mark.parametrize("verified", GOOD)
def test_set_with_evidence_passes(verified: list[str]) -> None:
    assert run(single("set", question(verified=verified))) == (0, "", "")


@pytest.mark.parametrize(
    "data",
    [
        question(),
        question(verified=[]),
        question(verified=[""]),
        question(verified=["   "]),
        question(verified="decision 148"),
        question(verified=None),
        question(verified=[1]),
        question(verified=["a", ""]),
        question(verified=["a", "b", "c"]),  # three entries for two options
    ],
)
def test_set_without_valid_evidence_is_blocked(data: dict[str, Any]) -> None:
    code, out, err = run(single("set", data))
    assert code == 2, (out, err)
    assert "BLOCKED by question_guard" in err
    assert "q-1" in err


def test_batch_set_is_checked_and_names_the_bad_item() -> None:
    good = item("set", question(verified=["decision 1"]))
    bad = item("set", question())
    assert run(batch(good, good))[0] == 0
    code, _out, err = run(batch(good, bad))
    assert code == 2
    assert "q-2" in err


def test_status_answer_updates_pass() -> None:
    update = {"status": "answered", "answer": "A", "pm_status": "noted"}
    assert run(single("update", update))[0] == 0
    assert run(batch(item("update", update)))[0] == 0


def test_update_that_sets_options_needs_evidence_too() -> None:
    assert run(single("update", {"options": OPTIONS}))[0] == 2
    assert run(single("update", {"options": OPTIONS, "verified": ["decision 1"]}))[0] == 2
    full = {"options": OPTIONS, "verified": ["decision 1"], "checked_against": CHECKED}
    assert run(single("update", full))[0] == 0


def test_set_without_options_passes() -> None:
    assert run(single("set", {"title": "free text question"}))[0] == 0


@pytest.mark.parametrize(
    "event",
    [
        single("set", question(), collection="notes"),  # another collection
        single("list", {}),
        single("get", {}),
        {"tool_name": "ArtifactData", "tool_input": {"action": "list", "collection": "questions"}},
        {"tool_name": "Bash", "tool_input": {"command": "x"}},
        {"tool_name": "ArtifactData", "tool_input": "not a dict"},
        {"tool_name": "ArtifactData", "tool_input": {"action": "batch", "writes": "no"}},
        {"tool_name": "ArtifactData", "tool_input": {"action": "batch", "writes": [3, None]}},
        {
            "tool_name": "ArtifactData",
            "tool_input": {"action": "set", "collection": "questions", "data": "x"},
        },
        {"tool_name": "ArtifactData"},
        [],
    ],
)
def test_other_calls_pass(event: Any) -> None:
    assert run(event) == (0, "", "")


@pytest.mark.parametrize("raw", ["", "not json", "{", "null", "﻿{}"])
def test_unreadable_input_passes_silently(raw: str) -> None:
    assert run(raw) == (0, "", "")


def test_batch_inherits_the_top_level_collection() -> None:
    event = batch({"op": "set", "doc_id": "q-3", "data": question()})
    event["tool_input"]["collection"] = "questions"
    assert run(event)[0] == 2


def test_override_collection_and_field(tmp_path: Path) -> None:
    cfg = block_config()
    cfg["question_guard"].update({"collection": "asks", "verified_field": "evidence"})
    assert run(single("set", question(), "questions"), cfg, tmp_path)[0] == 0
    code = run(single("set", question(verified=["x"]), "asks"), cfg, tmp_path / "b")[0]
    assert code == 2  # the field is now `evidence`
    assert run(single("set", question(evidence=["x"]), "asks"), cfg, tmp_path / "c")[0] == 0


def test_mode_warn_and_off(tmp_path: Path) -> None:
    cfg = block_config()
    cfg["question_guard"]["mode"] = "warn"
    code, out, err = run(single("set", question()), cfg, tmp_path / "w")
    assert code == 0 and err == ""
    assert "question_guard" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    cfg = copy.deepcopy(cfg)
    cfg["question_guard"]["mode"] = "off"
    assert run(single("set", question()), cfg, tmp_path / "o") == (0, "", "")


def test_invalid_config_fails_open_with_one_line(tmp_path: Path) -> None:
    cfg = block_config()
    del cfg["question_guard"]["verified_field"]
    code, out, err = run(single("set", question()), cfg, tmp_path)
    assert code == 0 and err == ""
    assert (
        "question_guard config invalid"
        in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    )


GUARD_FIELDS = sorted(json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))["question_guard"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "maybe"),
        ("collection", ""),
        ("verified_field", 3),
        ("checked_against_field", ""),
        ("decision_pattern", "("),
        ("decision_pattern", "decision \\d+"),  # no capture group
        ("rule_pattern", "(S)(\\d+)"),  # two groups
        ("decision_files", []),
        ("decision_files", ["../outside.md"]),
        ("decision_row_pattern", "x"),
        ("rules_file", "/etc/rules.md"),
        ("rule_row_pattern", "["),
        ("answered_dir", "a\\b"),
        ("status_field", None),
        ("answered_status", ""),
        ("reopens_field", 1),
        ("reopen_evidence_pattern", "("),
        ("min_option_chars", 0),
        ("min_option_chars", True),
    ],
)
def test_invalid_question_guard_config_is_refused(field: str, value: Any) -> None:
    data = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    data["question_guard"][field] = value
    with pytest.raises(gc.ConfigError):
        gc.validate(data)


@pytest.mark.parametrize("field", GUARD_FIELDS)
def test_every_question_guard_field_is_required(field: str) -> None:
    data = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["question_guard"][field]
    with pytest.raises(gc.ConfigError, match=field):
        gc.validate(data)


def test_missing_question_guard_section_is_refused() -> None:
    data = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["question_guard"]
    with pytest.raises(gc.ConfigError, match="question_guard"):
        gc.validate(data)


def test_the_committed_mode_is_print_only() -> None:
    # Decision 503: the guard prints; whether it blocks is the user's call, not this task's.
    assert (
        json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))["question_guard"]["mode"] == "warn"
    )


# --- S03: the "checked against" line ----------------------------------------------------------------


@pytest.mark.parametrize(
    "checked",
    [
        ["decision 148 and S03"],
        "Decision 148, s03",  # one string, any case
        ["decision 503", "S06 S08"],
        ["decision 188a and S01"],  # an id with a letter, found in an archive file
        ["decision #60 and S03"],
    ],
)
def test_a_question_that_names_existing_decisions_and_rules_passes(checked: Any) -> None:
    assert run(single("set", question(verified=["x"], checked_against=checked)))[0] == 0


@pytest.mark.parametrize(
    "checked",
    [
        None,
        "",
        "   ",
        [],
        [3],
        ["decision 148"],  # no standing rule
        ["S03"],  # no decision
        ["I checked everything"],
        ["decision 9999 and S03"],  # no such decision
        ["decision 148 and S99"],  # no such rule
        ["decision 148 and S03 and decision 7777"],  # one unknown id is enough
    ],
)
def test_a_question_without_a_valid_checked_against_line_is_blocked(checked: Any) -> None:
    data = question(verified=["x"])
    if checked is None:
        del data["checked_against"]
    else:
        data["checked_against"] = checked
    code, _out, err = run(single("set", data))
    assert code == 2 and "checked_against" in err


def test_unknown_ids_are_named(tmp_path: Path) -> None:
    code, _out, err = run(
        single("set", question(verified=["x"], checked_against=["decision 9999 S77"]))
    )
    assert code == 2 and "9999" in err and "S77" in err


def test_an_unreadable_id_file_is_a_finding_not_a_pass(tmp_path: Path) -> None:
    code, _out, err = run(single("set", question(verified=["x"])), None, tmp_path, id_files=False)
    assert code == 2
    assert "cannot read the id files" in err and "tasks/DECISIONS_INDEX.md" in err


def test_the_checked_against_field_name_is_configuration(tmp_path: Path) -> None:
    cfg = block_config()
    cfg["question_guard"]["checked_against_field"] = "geprueft"
    data = question(verified=["x"], geprueft=CHECKED)
    del data["checked_against"]
    assert run(single("set", data), cfg, tmp_path)[0] == 0
    assert run(single("set", question(verified=["x"])), cfg, tmp_path / "b")[0] == 2


# --- S06: no option repeats an answered one -------------------------------------------------------------


def smoke_question(option: Any, **extra: Any) -> dict[str, Any]:
    return question(
        options=[{"label": "Option A", "description": "Wait for the user"}, option],
        verified=["user words: 'ok'"],
        **extra,
    )


def test_the_0759_smoke_first_reoffer_is_blocked_and_names_the_answered_question() -> None:
    code, _out, err = run(single("set", smoke_question(dict(SMOKE_OPTION)), doc_id="q-0759"))
    assert code == 2
    assert "q-0649" in err and "option 2" in err


@pytest.mark.parametrize(
    "option",
    [
        # case and spacing
        {
            "label": "OPTION B",
            "description": "smoke   first:\trun one job on the laptop,\nthen stop for the PM verdict",
        },
        # punctuation
        {
            "label": "Option B.",
            "description": "Smoke first -- run one job on the laptop; then stop for the PM verdict!",
        },
        # width (NFKC) and accents (diacritics)
        {
            "label": "Option B",
            "description": (
                FULLWIDTH_SMOKE + ": run one job on the laptop, then stop for the PM verdict"
            ),
        },
        {
            "label": "Optión B",
            "description": "Smóke first: rún one job on the laptop, then stop for the PM verdict",
        },
        # only the description, only the label+description string, or a plain string option
        {"label": "Something else", "description": SMOKE_OPTION["description"]},
        SMOKE_OPTION["description"].upper(),
        "Option B Smoke first: run one job on the laptop, then stop for the PM verdict",
    ],
)
def test_a_reworded_copy_of_an_answered_option_is_blocked(option: Any) -> None:
    code, _out, err = run(single("set", smoke_question(option), doc_id="q-0759"))
    assert code == 2 and "q-0649" in err


def test_a_different_option_passes() -> None:
    option = {"label": "Option C", "description": "Ask the user which image to open first"}
    assert run(single("set", smoke_question(option), doc_id="q-0759")) == (0, "", "")


def test_short_labels_are_exempt(tmp_path: Path) -> None:
    answered = {"q-old": {**ANSWERED_0649, "options": [{"label": "A", "description": "x"}, "Yes"]}}
    short = question(options=[{"label": "A", "description": "x"}, "Yes"], verified=["x"])
    assert run(single("set", short), None, tmp_path, answered=answered)[0] == 0


def test_the_min_option_chars_value_is_configuration(tmp_path: Path) -> None:
    answered = {"q-old": {**ANSWERED_0649, "options": [{"label": "Yes", "description": "Yes"}]}}
    short = question(options=[{"label": "Yes", "description": "Yes"}], verified=["x"])
    assert run(single("set", short), None, tmp_path / "default", answered=answered)[0] == 0
    cfg = block_config()
    cfg["question_guard"]["min_option_chars"] = 3
    assert run(single("set", short), cfg, tmp_path / "low", answered=answered)[0] == 2


def test_the_question_s_own_earlier_text_is_not_a_repeat() -> None:
    event = single("set", smoke_question(dict(SMOKE_OPTION)), doc_id="q-0649")
    assert run(event)[0] == 0


def test_an_unanswered_question_is_not_a_repeat_source(tmp_path: Path) -> None:
    answered = {"q-0649": {**ANSWERED_0649, "status": "open"}}
    event = single("set", smoke_question(dict(SMOKE_OPTION)), doc_id="q-0759")
    assert run(event, None, tmp_path, answered=answered)[0] == 0


def test_the_answered_status_value_is_configuration(tmp_path: Path) -> None:
    answered = {"q-0649": {**ANSWERED_0649, "status": "closed"}}
    event = single("set", smoke_question(dict(SMOKE_OPTION)), doc_id="q-0759")
    cfg = block_config()
    cfg["question_guard"]["answered_status"] = "closed"
    assert run(event, cfg, tmp_path, answered=answered)[0] == 2


def test_the_match_may_use_the_ref_of_the_answered_question(tmp_path: Path) -> None:
    answered = {"stem-differs": ANSWERED_0649}
    event = single("set", smoke_question(dict(SMOKE_OPTION), reopens=["q-0649"]), doc_id="q-0759")
    # The stem and the ref both name the answered question: the ref is accepted for reopening.
    event["tool_input"]["data"]["verified"] = ['user words: "smoke first is still wanted"']
    assert run(event, None, tmp_path, answered=answered)[0] == 0


def test_reopening_needs_the_listed_id_and_the_users_quoted_words() -> None:
    reopen = {"reopens": ["q-0649"], "verified": ['user words: "làm smoke trước đi"']}
    base = smoke_question(dict(SMOKE_OPTION))
    assert run(single("set", {**base, **reopen}, doc_id="q-0759"))[0] == 0
    # The id without the user's words, the words without the id, another id, a non-quote.
    for broken in (
        {"reopens": ["q-0649"], "verified": ["decision 148"]},
        {"reopens": ["q-0649"], "verified": ["user words: none"]},
        {"verified": ['user words: "làm smoke trước đi"']},
        {"reopens": ["q-9999"], "verified": ['user words: "làm smoke trước đi"']},
        {"reopens": "q-0649", "verified": ['user words: "x"']},
    ):
        code, _out, err = run(single("set", {**base, **broken}, doc_id="q-0759"))
        assert code == 2 and "q-0649" in err, broken


def test_reopening_one_question_does_not_cover_another(tmp_path: Path) -> None:
    other = {
        "title": "t",
        "status": "answered",
        "options": [{"label": "Option D", "description": "Open the original input first, always"}],
    }
    event = single(
        "set",
        question(
            options=[
                dict(SMOKE_OPTION),
                {"label": "Option D", "description": "Open the original input first, always"},
            ],
            reopens=["q-0649"],
            verified=['user words: "smoke first again"'],
        ),
        doc_id="q-0759",
    )
    code, _out, err = run(
        event, None, tmp_path, answered={"q-0649": ANSWERED_0649, "q-other": other}
    )
    assert code == 2 and "q-other" in err and "q-0649" not in err


def test_an_unreadable_or_missing_mirror_is_a_finding_not_a_pass(tmp_path: Path) -> None:
    event = single(
        "set", smoke_question({"label": "Option C", "description": "A new idea entirely"})
    )
    code, _out, err = run(event, None, tmp_path / "bad", answered={"q-broken": "{not json"})
    assert code == 2 and "q-broken.json" in err
    code, _out, err = run(event, None, tmp_path / "none", mirror=False)
    assert code == 2 and "cannot compare" in err and ".tmp/pm-state/board/questions" in err


@pytest.mark.parametrize("raw", ["[]", "null", "42", '"text"', "true", '[{"status": "answered"}]'])
def test_a_json_mirror_file_that_is_not_an_object_is_a_finding_not_a_skip(
    tmp_path: Path, raw: str
) -> None:
    event = single(
        "set", smoke_question({"label": "Option C", "description": "A new idea entirely"})
    )
    code, _out, err = run(event, None, tmp_path, answered={"q-array": raw})
    assert code == 2 and "q-array.json" in err and "cannot compare" in err


@pytest.mark.parametrize("options", ['"text"', "{}", "7", "true"])
def test_an_answered_question_whose_options_are_not_a_list_is_a_finding(
    tmp_path: Path, options: str
) -> None:
    event = single(
        "set", smoke_question({"label": "Option C", "description": "A new idea entirely"})
    )
    raw = '{"status": "answered", "options": ' + options + "}"
    code, _out, err = run(event, None, tmp_path, answered={"q-odd": raw})
    assert code == 2 and "q-odd.json" in err
    ok = '{"status": "answered", "answer": "free text"}'
    assert run(event, None, tmp_path / "ok", answered={"q-free": ok})[0] == 0
    assert run(event, None, tmp_path / "open", answered={"q-open": "[]"})[0] == 2


def test_a_repeat_inside_a_batch_is_found(tmp_path: Path) -> None:
    bad = item("set", smoke_question(dict(SMOKE_OPTION)))
    good = item("set", smoke_question({"label": "Option C", "description": "A new idea entirely"}))
    assert run(batch(good, good))[0] == 0
    code, _out, err = run(batch(good, bad))
    assert code == 2 and "q-2" in err


def test_warn_mode_prints_the_repeat_and_still_allows(tmp_path: Path) -> None:
    cfg = block_config()
    cfg["question_guard"]["mode"] = "warn"
    event = single("set", smoke_question(dict(SMOKE_OPTION)), doc_id="q-0759")
    code, out, err = run(event, cfg, tmp_path)
    assert code == 0 and err == ""
    assert "q-0649" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


# --- the normaliser and the matcher as properties ---------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=80))
def test_property_normalise_is_idempotent_and_collapses_spacing(text: str) -> None:
    once = qg.normalise(text)
    assert qg.normalise(once) == once
    assert once == once.strip() and "  " not in once


WORD = st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8)


@settings(max_examples=120, deadline=None)
@given(
    words=st.lists(WORD, min_size=1, max_size=8),
    glue=st.sampled_from([" ", "  ", ", ", " - ", "\n", "\t;"]),
    tail=st.sampled_from(["", ".", "!", " ?"]),
)
def test_property_case_punctuation_and_spacing_do_not_change_the_text(
    words: list[str], glue: str, tail: str
) -> None:
    plain = " ".join(words)
    varied = glue.join(w.upper() for w in words) + tail
    assert qg.normalise(plain) == qg.normalise(varied)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    words=st.lists(WORD, min_size=3, max_size=8),
    glue=st.sampled_from([" ", ", ", " - "]),
    tail=st.sampled_from(["", ".", "!"]),
)
def test_property_a_varied_copy_of_an_answered_option_is_always_found(
    words: list[str], glue: str, tail: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    text = " ".join(words)
    if len(qg.normalise(text)) < 12:
        return
    root = tmp_path_factory.mktemp("prop")
    cfg = block_config()["question_guard"]
    board = root / cfg["answered_dir"]
    board.mkdir(parents=True)
    old = {"status": "answered", "options": [{"label": "x", "description": text}]}
    (board / "q-old.json").write_text(json.dumps(old), encoding="utf-8")
    option = {"label": "y", "description": glue.join(w.upper() for w in words) + tail}
    found = qg.repeat_problem("q-new", {"options": [option]}, cfg, root)
    assert found is not None and "q-old" in found
