"""The gate that decides what a private archive's repository may publish.

It had no tests, and it shipped two defects that a test would have caught at
once: it suppressed a real quotation because three files repeated it, and it
reported 957 digit runs of which 812 were the date at the top of a report --
noise deep enough that two real Telegram ids sat in a committed test file
underneath it.  Both are pinned here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    source = ROOT / "scripts" / "audit_publication.py"
    spec = importlib.util.spec_from_file_location("audit", source)
    module = importlib.util.module_from_spec(spec)
    # The module defines a dataclass, which resolves its annotations by name
    # against sys.modules; without this the import fails on FileFindings.
    sys.modules["audit"] = module
    spec.loader.exec_module(module)
    return module


audit = _load()


def _git_checkout() -> bool:
    import subprocess

    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip() == "true"
    except OSError:
        return False


# A release copy (the test Mac's) is not a git checkout: there is nothing to audit.
needs_git = pytest.mark.skipif(not _git_checkout(),
                               reason="not a git checkout: git ls-files has nothing to list")


def test_empty_candidate_index_cannot_pass_publication_gate(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["audit_publication", "--without-archive", "--gate"])
    monkeypatch.setattr(audit, "tracked_files", lambda paths: [])
    with pytest.raises(SystemExit) as error:
        audit.main()
    assert error.value.code == 2
    assert "refusing an empty publication audit" in capsys.readouterr().err


@pytest.mark.parametrize(
    "line",
    [
        "generated 2026-08-30 by the compile",
        "# Alpha acceptance - 2026-08-13",
        "stamped 20260830 in the report header",
        "2026/08/30 and 2026.08.30 both",
    ],
)
def test_a_date_is_not_a_digit_leak(line):
    kinds = {mark["kind"] for mark in audit.scan_marks("report.md", line)}
    assert "long_digits" not in kinds


@pytest.mark.parametrize(
    "line",
    [
        "phone +34600123456 in a fixture",
        "chat tg:-4000000456 in a test",
        "wa:79161234567@s.whatsapp.net",
    ],
)
def test_a_number_that_is_not_a_date_is_still_reported(line):
    kinds = {mark["kind"] for mark in audit.scan_marks("fixture.py", line)}
    assert "long_digits" in kinds


def test_a_digit_run_naming_an_archive_entry_is_promoted():
    """The failure that hid two real Telegram ids: a leak made of digits."""

    findings = {"tests/t.py": audit.FileFindings("tests/t.py")}
    findings["tests/t.py"].marks.append(
        {"kind": "long_digits", "line": 4, "shape": "10 digits ending 56", "value": "-4000000456"}
    )
    promoted = audit.promote_archive_identifiers(findings, lambda digits: 2114)

    assert promoted == 1
    mark = findings["tests/t.py"].marks[0]
    assert mark["kind"] == "archive_identifier"
    assert mark["entries"] == 2114
    # The shape says enough to find it and does not restate the identifier.
    assert "4000000456" not in mark["shape"]


def test_a_digit_run_the_archive_does_not_know_stays_an_ordinary_mark():
    findings = {"a.py": audit.FileFindings("a.py")}
    findings["a.py"].marks.append(
        {"kind": "long_digits", "line": 1, "shape": "10 digits ending 23", "value": "7000000123"}
    )

    assert audit.promote_archive_identifiers(findings, lambda digits: 0) == 0
    assert findings["a.py"].marks[0]["kind"] == "long_digits"


def test_a_short_run_is_not_matched_against_the_archive():
    """Below the threshold a match is a coincidence, and asking is a false alarm."""

    findings = {"a.py": audit.FileFindings("a.py")}
    findings["a.py"].marks.append({"kind": "long_digits", "line": 1, "shape": "", "value": "12345"})

    def refuse(digits):  # pragma: no cover - proving it is never consulted
        raise AssertionError("the archive was asked about a run too short to identify anything")

    assert audit.promote_archive_identifiers(findings, refuse) == 0


def test_repetition_does_not_excuse_quoting_a_person():
    """The defect that hid five quotations behind the rule meant to hide boilerplate."""

    idioms = {"i decided to go to leiden university this"}
    for source in sorted(audit.CONVERSATIONAL):
        assert not audit.suppress_as_idiom(
            "i decided to go to leiden university this", idioms, source
        )


def test_repetition_still_excuses_the_codebase_repeating_itself():
    run = "payload dict str any field default factory"
    assert audit.suppress_as_idiom(run, {run}, "documents")


def test_a_run_the_repository_does_not_repeat_is_never_suppressed():
    assert not audit.suppress_as_idiom("something said once", set(), "documents")


def test_the_archives_copy_of_this_repository_is_not_a_leak():
    assert audit.is_repository_copy(str(ROOT / "src" / "varyz" / "db" / "dao.py"), ROOT)
    assert audit.is_repository_copy(f"{ROOT}@deadbeef", ROOT)


def test_the_declared_seed_tree_is_source_lineage_not_a_private_document():
    seed = ROOT.parent / "varys"
    assert audit.is_seed_repository_copy(str(seed / "src" / "varys" / "config.py"), ROOT)
    assert audit.is_seed_repository_copy(f"{seed}@deadbeef", ROOT)
    assert not audit.is_seed_repository_copy(str(ROOT.parent / "memoir" / "README.md"), ROOT)
    # This repository is not its own seed: the rename once pointed the seed at it.
    assert not audit.is_seed_repository_copy(str(ROOT / "README.md"), ROOT)


@pytest.mark.parametrize("external_id", ["79161234567@s.whatsapp.net", "tg:-4000000456:812", None])
def test_a_message_identifier_is_never_read_as_a_path_into_this_repository(external_id):
    """Resolving these relatively lands them here and skips every private message."""

    assert not audit.is_repository_copy(external_id, ROOT)


@pytest.mark.parametrize(
    "external_id",
    (
        "codex:019e5062-63ba-7050-ae35-adc3dbafaa61:line155",
        "claude_code:017c9cbf-2ff0-4526-a50e-922dc42eef3c:message-7",
    ),
)
def test_coding_agent_sessions_are_repository_work_provenance(external_id):
    assert audit.is_repository_work_session("ai_conversations", external_id)


@pytest.mark.parametrize(
    ("source", "external_id"),
    (
        ("ai_conversations", "chatgpt:personal-chat:message-7"),
        ("whatsapp", "codex:mentioned-in-a-message"),
        ("ai_conversations", None),
    ),
)
def test_ordinary_conversations_are_never_exempted_as_repository_work(source, external_id):
    assert not audit.is_repository_work_session(source, external_id)


def _quote(source="whatsapp", text="he said a thing that was his to say"):
    return {"kind": "archive_quotation", "line": 3, "entry_source": source, "text": text}


def test_a_finding_with_a_written_reason_is_accounted_for():
    findings = {"a.py": audit.FileFindings("a.py")}
    findings["a.py"].quoted.append(_quote())
    key = audit.finding_key("a.py", findings["a.py"].quoted[0])

    assert audit.unaccepted(findings, {key: "checked: it is the vendor's UI copy"}) == []


def test_a_blank_reason_does_not_accept_anything():
    """A baseline fillable without saying why is a list of things nobody looked at."""

    findings = {"a.py": audit.FileFindings("a.py")}
    findings["a.py"].quoted.append(_quote())
    key = audit.finding_key("a.py", findings["a.py"].quoted[0])

    for blank in ("", "   ", "\n"):
        assert len(audit.unaccepted(findings, {key: blank})) == 1


def test_accepting_a_finding_in_one_file_does_not_accept_it_in_another():
    findings = {"a.py": audit.FileFindings("a.py"), "b.py": audit.FileFindings("b.py")}
    findings["a.py"].quoted.append(_quote())
    findings["b.py"].quoted.append(_quote())
    key = audit.finding_key("a.py", findings["a.py"].quoted[0])

    assert [path for path, _ in audit.unaccepted(findings, {key: "reviewed"})] == ["b.py"]


def test_a_key_names_a_finding_without_restating_it():
    key = audit.finding_key("a.py", _quote(text="a sentence nobody agreed to publish"))
    assert "sentence" not in "".join(key)
    assert "nobody" not in "".join(key)


@pytest.mark.parametrize(
    ("item", "gated"),
    [
        (_quote("whatsapp"), True),
        (_quote("telegram"), True),
        (_quote("ai_conversations"), True),
        # Tens of thousands of these, summarised by project on purpose.  One
        # baseline entry each is a list nobody can read, which is the failure
        # this gate has already made once.
        (_quote("documents"), False),
        ({"kind": "archive_identifier", "line": 1}, True),
        ({"kind": "private_key", "line": 1}, True),
        ({"kind": "long_digits", "line": 1}, False),
        ({"kind": "home_path", "line": 1}, False),
    ],
)
def test_only_the_classes_a_person_can_review_are_gated(item, gated):
    assert audit.is_gated(item, "reports/whatever.md") is gated


@pytest.mark.parametrize(
    "path", ["src/varyz/graph/semantic.py", r"src\varyz\graph\semantic.py"]
)
def test_shipping_source_is_reviewed_whatever_the_words_came_out_of(path):
    """A document sentence in a prompt ships to every user of this program.

    The gate read the source of the entry and stopped there, so two sentences
    out of the owner's documents sat in an extraction prompt accounted for by
    nobody.  Under `src/` there are fourteen quotations in the whole tree; that
    is a list a person can read.
    """

    assert audit.is_gated(_quote("documents"), path) is True


def test_the_bulk_of_the_tree_is_still_summarised_rather_than_listed():
    for path in ("docs/ARCHITECTURE.md", "reports/RESOURCE_COST_2026-08-05.md", "LICENSE"):
        assert audit.is_gated(_quote("documents"), path) is False


def test_the_committed_baseline_carries_none_of_what_it_accepts():
    """It is a tracked file in a repository about to be public."""

    import json

    payload = json.loads((ROOT / "publication_accepted.json").read_text(encoding="utf-8"))
    for row in payload["accepted"]:
        assert set(row) <= {"path", "kind", "digest", "entry_source", "reason"}
        assert len(row["digest"]) == 16
        int(row["digest"], 16)  # a hash, not a fragment of anything


def _run_without_archive(tmp_path, extra=()):
    import subprocess

    out = tmp_path / "marks.json"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            str(ROOT / "scripts" / "audit_publication.py"),
            "--without-archive",
            "--json-out",
            str(out),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    return done, out


@needs_git
def test_the_marks_half_runs_without_the_archive(tmp_path):
    """The half a clean machine can run.

    Continuous integration has no copy of the archive and never will, so the
    quotation pass cannot run there.  The marks pass can, and this is the exact
    invocation the workflow uses.
    """

    import json

    done, out = _run_without_archive(tmp_path)
    assert done.returncode == 0, done.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["archive_compared"] is False
    assert payload["entries_scanned"] == 0
    quoted = [
        item for file in payload["findings"].values() for item in file["quoted"]
    ]
    assert quoted == []
    assert any(file["marks"] for file in payload["findings"].values())


@needs_git
def test_the_run_says_it_did_not_look_at_the_archive(tmp_path):
    """A clean exit here must not read as 'the repository quotes nothing'."""

    done, _ = _run_without_archive(tmp_path)
    assert "The archive was not opened" in done.stdout
    assert "somebody's words" not in done.stdout


def test_the_baseline_cannot_be_rewritten_from_marks_alone(tmp_path):
    """Otherwise one run on a machine without the archive erases every reason."""

    done, _ = _run_without_archive(tmp_path, extra=("--accept",))
    assert done.returncode != 0
    assert "cannot be built from marks alone" in done.stderr
