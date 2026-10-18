from __future__ import annotations

import base64
import contextlib
import itertools
import json
import logging
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from varyz.connectors.base import SkipItem, get_connector
from varyz.connectors.calendar_ics import CalendarICSConnector
from varyz.connectors.documents import DocumentsConnector, _walk_documents
from varyz.connectors.gmail_imap import GmailImapConnector
from varyz.connectors.personal_archive import PersonalArchiveConnector
from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, RawItem
from varyz.pipeline.ingest import sync_connector


def _link_to_ancestor(link: Path, target: Path) -> None:
    """A directory that points back up the tree, however this platform spells it.

    On Windows `os.symlink` needs Developer Mode or an elevated shell, so it
    fails on most machines — but `mklink /J` needs neither, which is why
    junctions are the loop that actually turns up in a user's folders.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        pytest.skip("this platform will not create a directory link here")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, check=False,
    )
    if not link.exists():
        pytest.skip(f"could not create a junction: {result.stderr!r}")


def test_documents_require_approval_and_extract_text(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    (root / "plan.txt").write_text("Move to Lisbon in September", encoding="utf-8")
    connector = DocumentsConnector()
    try:
        list(connector.discover({"paths": [str(root)]}, None))
    except ValueError as exc:
        assert "approval" in str(exc)
    else:
        raise AssertionError("unapproved folders must fail closed")
    item, _ = next(connector.discover({"approved": True, "paths": [str(root)]}, None))
    norm = connector.normalize(item)
    assert norm.title == "plan"
    assert "Lisbon" in norm.body
    assert norm.significance == 0.55


def test_a_file_that_is_not_the_format_its_name_claims_is_skipped(tmp_path, caplog):
    """One bad download must not print a pypdf traceback and mark the source failed.

    A .pdf holding anything but a PDF -- a truncated download, a rename, an
    HTML error page saved under the wrong extension -- reached pypdf, which
    raised.  ingest caught it as an unexpected error: a twenty-line
    third-party traceback in the terminal on every sync, `connect status`
    reading "1 errors" for as long as the file stays in the folder, and the
    connector cursor held back because the run had failed.  The file is
    unreadable, which is a property of the file, not a failure of the sync.
    """
    root = tmp_path / "Documents"
    root.mkdir()
    (root / "invoice.pdf").write_bytes(b"<html>404 Not Found</html>" * 40)
    (root / "notes.md").write_text("Move to Lisbon in September", encoding="utf-8")
    connector = DocumentsConnector()
    options = {"approved": True, "paths": [str(root)]}

    kept = []
    with caplog.at_level(logging.WARNING, logger="varyz.connectors.documents"):
        for item, _ in connector.discover(options, None):
            with contextlib.suppress(SkipItem):
                kept.append(connector.normalize(item))

    assert [entry.title for entry in kept] == ["notes"]
    # Named, once, in a line -- not a traceback, and not silence either.
    assert "invoice.pdf" in caplog.text
    assert "Traceback" not in caplog.text
    # And pypdf's own narration of the same file does not land beside it.
    assert "invalid pdf header" not in caplog.text


def test_ics_calendar_import_normalizes_event(tmp_path):
    path = tmp_path / "life.ics"
    path.write_bytes(
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        b"UID:wedding-1\r\nDTSTART:20260912T140000Z\r\nDTEND:20260912T220000Z\r\n"
        b"SUMMARY:Anna and Jo's wedding\r\nLOCATION:Lisbon\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    connector = CalendarICSConnector()
    item, _ = next(
        connector.discover({"approved": True, "paths": [str(path)]}, None)
    )
    norm = connector.normalize(item)
    assert norm.title == "Anna and Jo's wedding"
    assert norm.occurred_start.isoformat() == "2026-09-12T14:00:00+00:00"
    assert "Lisbon" in norm.body


def test_a_bad_member_of_an_export_archive_is_skipped_not_raised(tmp_path, caplog):
    """A Takeout ZIP is assembled by somebody else's exporter.

    One member that is not the format its name claims — an HTML error page
    saved as .pdf, a truncated .docx — used to raise out of normalize: a
    third-party traceback per sync and an error count that never cleared.
    """

    archive_path = tmp_path / "takeout.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Takeout/Docs/notes.md", "Moving to Lisbon in September")
        archive.writestr("Takeout/Docs/invoice.pdf", "<html>error page</html>")
        archive.writestr("Takeout/Docs/report.docx", "not a docx either")

    connector = PersonalArchiveConnector()
    items = list(connector.discover({"approved": True, "paths": [str(archive_path)]}, None))
    kept = []
    with caplog.at_level(logging.WARNING):
        for item, _ in items:
            with contextlib.suppress(SkipItem):
                kept.append(connector.normalize(item).title)

    assert "notes" in kept
    assert "invoice" not in kept and "report" not in kept
    assert "invoice.pdf" in caplog.text and "report.docx" in caplog.text
    assert "Traceback" not in caplog.text


def test_personal_archive_compacts_health_spotify_and_location(tmp_path):
    archive_path = tmp_path / "takeout.zip"
    health = b"""<?xml version="1.0"?><HealthData>
      <Record type="HKQuantityTypeIdentifierStepCount" value="500"
       startDate="2026-07-01 09:00:00 +0000"/>
      <Record type="HKQuantityTypeIdentifierStepCount" value="700"
       startDate="2026-07-01 18:00:00 +0000"/>
      <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
       startDate="2026-07-01 07:00:00 +0000"/>
    </HealthData>"""
    spotify = [
        {
            "ts": "2026-07-01T10:00:00Z",
            "master_metadata_track_name": "Teardrop",
            "master_metadata_album_artist_name": "Massive Attack",
            "ms_played": 240000,
        }
    ]
    location = {
        "timelineObjects": [
            {
                "placeVisit": {
                    "location": {
                        "name": "Gulbenkian Museum",
                        "address": "Lisbon",
                        "latitudeE7": 387369000,
                        "longitudeE7": -91499000,
                    },
                    "duration": {
                        "startTimestamp": "2026-07-01T12:00:00Z",
                        "endTimestamp": "2026-07-01T14:00:00Z",
                    },
                }
            }
        ]
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("apple_health_export/export.xml", health)
        archive.writestr("Spotify/Streaming_History_Audio_2026.json", json.dumps(spotify))
        archive.writestr(
            "Location History/Semantic Location History/2026.json",
            json.dumps(location),
        )

    connector = PersonalArchiveConnector()
    discovered = list(
        connector.discover({"approved": True, "paths": [str(archive_path)]}, None)
    )
    normalized = [connector.normalize(item) for item, _ in discovered]
    assert any(entry.title == "Health — 2026-07-01" for entry in normalized)
    health_entry = next(entry for entry in normalized if entry.title.startswith("Health"))
    assert "StepCount" in health_entry.body
    assert "Running: 30" in health_entry.body
    activity = next(entry for entry in normalized if entry.title.startswith("Spotify"))
    assert "Massive Attack" in activity.body
    place = next(entry for entry in normalized if entry.title.startswith("Visited"))
    assert place.lat == 38.7369
    assert place.lon == -9.1499


def test_gmail_google_api_resumes_and_keeps_message_id(monkeypatch, cfg, conn, vault):
    raw = (
        b"From: anna@example.com\r\nTo: me@example.com\r\n"
        b"Message-ID: <stable@example.com>\r\n"
        b"Date: Wed, 01 Jul 2026 10:00:00 +0000\r\n"
        b"Subject: Plans\r\n\r\nLisbon in September"
    )

    class FakeGoogleAPI:
        def __init__(self, account, **kwargs):
            self.account = account

        def json(self, url, params=None):
            if url.endswith("/profile"):
                return {"historyId": "900"}
            if url.endswith("/messages"):
                return {"messages": [{"id": "g-1"}]}
            if url.endswith("/g-1"):
                return {
                    "id": "g-1",
                    "historyId": "899",
                    "raw": base64.urlsafe_b64encode(raw).decode().rstrip("="),
                }
            if url.endswith("/history"):
                return {"historyId": "901"}
            raise AssertionError(url)

    monkeypatch.setattr("varyz.connectors.google_gmail.GoogleAPI", FakeGoogleAPI)
    cfg.connectors["gmail"] = {"auth": "google", "account": "me@example.com"}
    first = sync_connector(conn, vault, GmailImapConnector(), cfg)
    assert first.new == 1
    assert dao.get_cursor(conn, "gmail", "me@example.com").data == {
        "phase": "incremental",
        "history_id": "900",
    }
    row = conn.execute("SELECT external_id FROM entries").fetchone()
    assert row["external_id"] == "<stable@example.com>"
    second = sync_connector(conn, vault, GmailImapConnector(), cfg)
    assert second.new == 0
    assert dao.count_entries(conn) == 1


def test_google_workspace_bundle_syncs_calendar_and_drive(
    monkeypatch, cfg, conn, vault
):
    class FakeGoogleAPI:
        def __init__(self, account, **kwargs):
            self.account = account

        def json(self, url, params=None):
            params = params or {}
            if url.endswith("/calendarList"):
                return {"items": [{"id": "primary", "summary": "Life"}]}
            if url.endswith("/calendars/primary/events"):
                if params.get("syncToken"):
                    return {"items": [], "nextSyncToken": "calendar-2"}
                return {
                    "items": [
                        {
                            "id": "event-1",
                            "summary": "Dinner with Anna",
                            "location": "Lisbon",
                            "start": {"dateTime": "2026-07-20T19:00:00Z"},
                            "end": {"dateTime": "2026-07-20T21:00:00Z"},
                        }
                    ],
                    "nextSyncToken": "calendar-1",
                }
            if url.endswith("/changes/startPageToken"):
                return {"startPageToken": "drive-start"}
            if url.endswith("/changes"):
                return {"changes": [], "newStartPageToken": "drive-next"}
            if url.endswith("/files"):
                return {
                    "files": [
                        {
                            "id": "doc-1",
                            "name": "Life plan.txt",
                            "mimeType": "text/plain",
                            "size": "30",
                            "modifiedTime": "2026-07-20T10:00:00Z",
                            "createdTime": "2026-07-19T10:00:00Z",
                        }
                    ]
                }
            raise AssertionError((url, params))

        def bytes(self, url, params=None):
            assert url.endswith("/files/doc-1")
            return b"Move to Lisbon in September"

    monkeypatch.setattr("varyz.connectors.google_workspace.GoogleAPI", FakeGoogleAPI)
    for source in ("google_calendar", "google_drive"):
        cfg.connectors[source] = {"account": "me@example.com"}
        first = sync_connector(conn, vault, get_connector(source), cfg)
        assert first.new == 1
        second = sync_connector(conn, vault, get_connector(source), cfg)
        assert second.new == 0
    rows = conn.execute("SELECT source, title, body FROM entries ORDER BY source").fetchall()
    assert {row["source"] for row in rows} == {"google_calendar", "google_drive"}
    assert any("Lisbon" in row["body"] for row in rows)


def test_a_google_setting_that_is_not_what_it_claims_names_itself():
    """fromisoformat and float() raise about strings and literals.

    These settings are typed into config.toml by hand, so the message has to
    say which line of it to fix -- not what a parser was expecting.
    """
    from varyz.connectors.google_workspace import _max_bytes, _since_iso

    with pytest.raises(ValueError, match="since"):
        _since_iso("01/01/2026")
    with pytest.raises(ValueError, match="max_size_mb"):
        _max_bytes({"max_size_mb": "25 MB"})
    with pytest.raises(ValueError, match="max_size_mb"):
        _max_bytes({"max_size_mb": 0})
    assert _max_bytes({}) == 25 * 1024 * 1024


def test_documents_prunes_dependency_and_dot_directories(tmp_path):
    """A developer folder is mostly vendored files by volume; they must not
    bury real documents, and the walk must not pay to descend into them."""
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real note", encoding="utf-8")
    for noisy in ("node_modules/pkg", ".venv/lib", "dist", "build", "__pycache__"):
        directory = tmp_path / noisy
        directory.mkdir(parents=True)
        (directory / "README.md").write_text("vendored", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    names = sorted(Path(item.metadata["path"]).name for item, _ in items)
    assert names == ["notes.md"]


def test_documents_still_reads_nested_real_folders(tmp_path):
    from varyz.connectors.documents import DocumentsConnector

    nested = tmp_path / "docs" / "design"
    nested.mkdir(parents=True)
    (nested / "architecture.md").write_text("decision table", encoding="utf-8")
    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert [Path(i.metadata["path"]).name for i, _ in items] == ["architecture.md"]


def _init_repo(path, gitignore: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
    (path / ".gitignore").write_text(gitignore, encoding="utf-8")


def test_documents_refuses_gitignored_paths(tmp_path):
    """A developer's own .gitignore is the most accurate statement of what is
    generated or private; local tool databases must not enter the index."""
    from varyz.connectors.documents import DocumentsConnector

    _init_repo(tmp_path, "data/\nsecret-notes.md\n")
    (tmp_path / "README.md").write_text("public", encoding="utf-8")
    (tmp_path / "secret-notes.md").write_text("private", encoding="utf-8")
    store = tmp_path / "data" / "vault"
    store.mkdir(parents=True)
    (store / "message.txt").write_text("someone's private message", encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    names = sorted(Path(item.metadata["path"]).name for item, _ in items)
    assert names == ["README.md"]


def test_documents_deny_list_holds_without_gitignore(tmp_path):
    """Fail-closed: known personal stores are refused even in a plain folder
    with no git repository and no ignore rules."""
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real", encoding="utf-8")
    for forbidden in (
        "whatsapp_out",
        "whatsapp_out_2y_lead",
        "whatsapp_bridge",
        "vault",
        "_preview",
        "secrets",
    ):
        directory = tmp_path / forbidden
        directory.mkdir()
        (directory / "leak.md").write_text("personal", encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["notes.md"]


def test_documents_never_indexes_its_own_data_dir(tmp_path):
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real", encoding="utf-8")
    own = tmp_path / "varyz-state"
    own.mkdir()
    (own / "copy.md").write_text("would recurse", encoding="utf-8")

    items = list(
        DocumentsConnector().discover(
            {"approved": True, "paths": [str(tmp_path)], "_data_dir": str(own)}, None
        )
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["notes.md"]


def test_documents_refuses_browser_profiles_and_caches(tmp_path):
    """Browser profiles hold cookies, session tokens and browsing history,
    which varyz does not read; caches are regenerable noise."""
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real", encoding="utf-8")
    for generated in (
        "runtime/default/chrome-profile/Sync Data",
        "multiturn_cache",
        "some_project/.cache",
        "build",
        "playwright",
    ):
        directory = tmp_path / generated
        directory.mkdir(parents=True)
        (directory / "junk.json").write_text("{}", encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["notes.md"]


def test_documents_skips_model_weight_directories(tmp_path):
    """A folder holding tensors is a model. Its JSON describes the tensors,
    not the user — and a tokenizer vocabulary alone is thousands of chunks."""
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real", encoding="utf-8")
    model = tmp_path / "models" / "qwen2.5-7b"
    model.mkdir(parents=True)
    (model / "model.safetensors").write_bytes(b"\x00\x01")
    (model / "vocab.json").write_text('{"a": 1}', encoding="utf-8")
    (model / "README.md").write_text("model card", encoding="utf-8")
    checkpoint = tmp_path / "adapters" / "run" / "checkpoint-424"
    checkpoint.mkdir(parents=True)
    (checkpoint / "tokenizer.json").write_text('{"a": 1}', encoding="utf-8")
    (checkpoint / "notes.md").write_text("inside a checkpoint", encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["notes.md"]


def test_documents_folder_named_like_weights_is_still_read(tmp_path):
    """A `$RECYCLE.BIN` folder from a Windows drive is not a model file."""
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real", encoding="utf-8")
    (tmp_path / "$RECYCLE.BIN").mkdir()

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert [Path(i.metadata["path"]).name for i, _ in items] == ["notes.md"]


def test_documents_skips_ml_artifacts_outside_weight_directories(tmp_path):
    from varyz.connectors.documents import DocumentsConnector

    (tmp_path / "notes.md").write_text("real", encoding="utf-8")
    (tmp_path / "tokenizer_config.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "merges.txt").write_text("a b\n", encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["notes.md"]


def test_documents_skips_numeric_datasets_but_keeps_written_csv(tmp_path):
    """A price series answers no question a person would ask; a contact
    export does. Both are .csv, so the split has to be made on content."""
    from varyz.connectors.documents import DocumentsConnector

    prices = "date,open,close\n" + "".join(
        f"2024-01-{d:02d},1.5,2.5\n" for d in range(1, 29)
    )
    (tmp_path / "equity_curve.csv").write_text(prices, encoding="utf-8")
    people = "name,role,note\n" + "".join(
        f"Person {d},engineer,joined the platform team\n" for d in range(1, 29)
    )
    (tmp_path / "contacts.csv").write_text(people, encoding="utf-8")

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["contacts.csv"]


def test_a_telemetry_log_is_a_table_even_though_its_rows_start_with_words(tmp_path):
    """The numeric test read a GPU capture log as prose, because every row
    opens with the application, the card and the CPU before its eighty numeric
    columns. It then retrieved beautifully for "what GPU is this machine
    running" and outranked the file that answers the question. What gives it
    away is the shape: one header and hundreds of rows cut to the same width.
    """
    from varyz.connectors.documents import DocumentsConnector

    header = "Application,GPU,CPU,Resolution,Runtime,MsBetweenPresents,GPU0Temp\n"
    row = "chrome.exe,NVIDIA GeForce RTX 5070,AMD Ryzen 7 7800X3D,1447x864,Other,39.048,47\n"
    (tmp_path / "FrameView_chrome.exe_Log.csv").write_text(header + row * 40, encoding="utf-8")
    # Same width, but too few columns to be anything but somebody's list.
    (tmp_path / "reading.csv").write_text(
        "title,author,note\n"
        + "".join(f"Book {n},Someone,worth rereading\n" for n in range(40)),
        encoding="utf-8",
    )

    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert sorted(Path(i.metadata["path"]).name for i, _ in items) == ["reading.csv"]


def test_a_requirements_file_is_regular_without_being_a_table(tmp_path):
    """Every line the same shape is not enough on its own: a requirements file
    is one field per line, hundreds of times, and it is a document.
    """
    from varyz.connectors.documents import looks_tabular

    assert not looks_tabular("\n".join(f"package-{n}==1.{n}.0" for n in range(40)))


def test_configured_paths_expand_the_way_people_write_them(tmp_path, monkeypatch):
    """`~/notes` and `%USERPROFILE%\\notes` do not exist as written, so they
    came back as "none of the configured paths exist" — an error naming a path
    while the user looks at the folder sitting plainly there.
    """
    from varyz.connectors.base import resolve_paths

    home = tmp_path / "home"
    (home / "notes").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    spellings = ["~/notes", "$HOME/notes" if os.name != "nt" else "%USERPROFILE%/notes"]
    resolved = resolve_paths({"paths": spellings}, "documents")
    assert [p.resolve() for p in resolved] == [(home / "notes").resolve()] * 2


def test_a_path_that_really_is_missing_is_named_as_it_was_written(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from varyz.connectors.base import resolve_paths

    with pytest.raises(ValueError, match=r"~/nowhere"):
        resolve_paths({"paths": ["~/nowhere"]}, "documents")


def test_a_relative_path_is_read_against_the_config_not_the_shell(tmp_path, monkeypatch):
    """`paths = ["./notes"]` means the folder beside config.toml.

    It used to resolve against the process's working directory, so the same
    line found the folder when someone ran `varyz sync` from the archive and
    reported it gone when autosync ran the identical config at login.
    """
    from varyz.connectors.base import resolve_paths

    archive = tmp_path / "archive"
    (archive / "notes").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = resolve_paths(
        {"paths": ["./notes"], "_base_dir": str(archive)}, "documents"
    )

    assert [p.resolve() for p in resolved] == [(archive / "notes").resolve()]


def test_a_relative_path_with_no_archive_to_anchor_to_is_left_alone(tmp_path, monkeypatch):
    # Callers that build options by hand -- tests, and connectors driven
    # directly -- pass no _base_dir, and the old meaning is the only one left.
    from varyz.connectors.base import resolve_paths

    (tmp_path / "notes").mkdir()
    monkeypatch.chdir(tmp_path)

    resolved = resolve_paths({"paths": ["./notes"]}, "documents")

    assert [p.resolve() for p in resolved] == [(tmp_path / "notes").resolve()]


def test_documents_walks_a_directory_cycle_once(tmp_path):
    """A junction or symlink pointing at an ancestor turns the tree into a
    cycle, and `is_dir()` follows both. Measured before the fix: the same two
    files came back 64 times each, deeper every round, and the walk stopped
    only because Windows refused the path length — with long paths enabled,
    not at all.
    """
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("kept for 30 days", encoding="utf-8")
    (tmp_path / "notes" / "deep").mkdir()
    (tmp_path / "notes" / "deep" / "b.md").write_text("signing key", encoding="utf-8")
    _link_to_ancestor(tmp_path / "notes" / "deep" / "loopback", tmp_path)

    # islice, not list: before the fix this returned 128 and the only reason it
    # returned at all was MAX_PATH.
    found = list(itertools.islice(_walk_documents(tmp_path), 200))
    assert sorted(p.name for p in found) == ["a.md", "b.md"]


def test_documents_survives_a_file_that_vanishes_mid_walk(tmp_path, monkeypatch):
    """The mtime used to be read outside the guard that covers the size check
    and the read, so a file deleted in between raised out of the generator —
    and `sync_connector` wraps the ingest, not the iteration, so one build
    artefact disappearing ended the whole sync.
    """
    (tmp_path / "keep.md").write_text("backups are kept 30 days", encoding="utf-8")
    (tmp_path / "gone.md").write_text("about to disappear", encoding="utf-8")

    real_stat = Path.stat
    seen = {"gone.md": 0}

    def flaky(self, *args, **kwargs):
        if self.name == "gone.md":
            seen["gone.md"] += 1
            # Everything up to and including the read succeeds; the file is
            # gone by the time anything asks about it again.
            if seen["gone.md"] > 5:
                raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    items = list(
        DocumentsConnector().discover({"approved": True, "paths": [str(tmp_path)]}, None)
    )
    assert "keep.md" in [Path(i.metadata["path"]).name for i, _ in items]


def test_a_connector_that_fails_part_way_still_keeps_what_it_found(cfg, conn, vault):
    """A mailbox that drops its connection, a repository that turns unreadable,
    a folder someone is building in. The alternative is that the sync ends, the
    cursor write at the bottom never runs, and the next run redoes work already
    ingested — every time, because the condition has not gone away.
    """

    class Halfway:
        connector_id = "documents"

        def discover(self, options, cursor):
            yield (
                RawItem(external_id="a", payload=b"backups are kept 30 days",
                        payload_ext=".txt", metadata={"path": "a.txt", "mtime": 0.0}),
                None,
            )
            raise OSError("the drive went away")

        def normalize(self, item):
            return NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=item.external_id,
                title="a",
                body=item.payload_bytes().decode(),
            )

    stats = sync_connector(conn, vault, Halfway(), cfg)
    assert stats.new == 1
    assert stats.errors == 1


def test_a_connector_that_cannot_be_configured_still_says_so(cfg, conn, vault):
    """The other direction. `no git repositories found under the configured
    paths` is the user's to fix, and counting it as one error among many would
    return the clean-sync-of-nothing this replaced.
    """

    class Misconfigured:
        connector_id = "git"

        def discover(self, options, cursor):
            raise ValueError("no git repositories found under the configured paths")
            yield  # pragma: no cover - makes this a generator

        def normalize(self, item):  # pragma: no cover - never reached
            raise AssertionError

    with pytest.raises(ValueError, match="no git repositories"):
        sync_connector(conn, vault, Misconfigured(), cfg)


def _discovered_paths(folder):
    """The files `documents` would actually ingest from `folder`."""
    items = DocumentsConnector().discover(
        {"approved": True, "paths": [str(folder)]}, None
    )
    return [Path(item.metadata["path"]) for item, _ in items]


def _documents_cfg(cfg, folder):
    cfg.connectors["documents"] = {"approved": True, "paths": [str(folder)]}
    return cfg


def test_a_deleted_file_stops_being_evidence(tmp_path, cfg, conn, vault):
    """The index used to only ever grow. A note whose decision was reversed
    stayed answerable forever, and `recall` quoted it with a reference to a
    file that was not there — which is the one failure the caller cannot catch,
    because checking the citation is exactly what it would do."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "keep.md").write_text("Logs are kept 90 days.", encoding="utf-8")
    (folder / "reversed.md").write_text("We are migrating to Postgres.", encoding="utf-8")
    connector = get_connector("documents")
    _documents_cfg(cfg, folder)

    assert sync_connector(conn, vault, connector, cfg).new == 2
    (folder / "reversed.md").unlink()
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.deleted == 1
    bodies = [r[0] for r in conn.execute("SELECT body FROM entries")]
    assert bodies == ["Logs are kept 90 days."]
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_a_file_with_no_text_in_it_is_not_an_entry(tmp_path, cfg, conn, vault):
    """Emptiness that only shows after extraction: a note nobody wrote in, a
    page that is all markup. The entry got made anyway and chunked to its own
    filename, so the index held a searchable `index` and `Guidelines` that
    could never be quoted — an empty excerpt is dropped — but could still take
    a rank from something that could.
    """
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "real.md").write_text("Oncall rotates every Monday.", encoding="utf-8")
    (folder / "Guidelines.md").write_text("   \n\n  \n", encoding="utf-8")
    (folder / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
    connector = get_connector("documents")
    _documents_cfg(cfg, folder)

    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.new == 1
    # Not an error either: nothing here failed, and a run of errors stops the
    # cursor from advancing.
    assert stats.errors == 0
    titles = sorted(r[0] for r in conn.execute("SELECT title FROM entries"))
    assert titles == ["real"]


def test_an_entry_for_an_empty_file_leaves_on_the_next_sync(tmp_path, cfg, conn, vault):
    """The check arrived after the entries did, so it has to clear them out
    rather than only stop making more."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "real.md").write_text("Oncall rotates every Monday.", encoding="utf-8")
    stub = folder / "stdout.txt"
    stub.write_text("captured output", encoding="utf-8")
    connector = get_connector("documents")
    _documents_cfg(cfg, folder)
    assert sync_connector(conn, vault, connector, cfg).new == 2

    stub.write_text("\n", encoding="utf-8")
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.deleted == 1
    titles = sorted(r[0] for r in conn.execute("SELECT title FROM entries"))
    assert titles == ["real"]


def test_a_renamed_file_does_not_become_two_notes(tmp_path, cfg, conn, vault):
    """The path is the identity, so without a prune the old entry lingers
    beside the new one and the same text comes back twice — spending two of the
    eight capsules an agent asked for on one note."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "oncall.md").write_text("Oncall rotates every Monday.", encoding="utf-8")
    connector = get_connector("documents")
    _documents_cfg(cfg, folder)

    sync_connector(conn, vault, connector, cfg)
    (folder / "oncall.md").rename(folder / "rotation.md")
    stats = sync_connector(conn, vault, connector, cfg)

    assert (stats.new, stats.deleted) == (1, 1)
    names = [Path(r[0]).name for r in conn.execute("SELECT external_id FROM entries")]
    assert names == ["rotation.md"]


def test_a_scan_that_stopped_early_never_prunes(cfg, conn, vault):
    """A discovery that broke part-way has not seen the rest of the corpus.
    Reading that silence as deletion would empty the index on a transient
    failure — the same failure the part-way handling exists to survive."""

    class Halfway:
        connector_id = "documents"
        enumerates_corpus = True
        fail = False

        def discover(self, options, cursor):
            yield (
                RawItem(external_id="a", payload=b"first", payload_ext=".txt",
                        metadata={"path": "a.txt", "mtime": 0.0}),
                None,
            )
            if self.fail:
                raise OSError("the drive went away")
            yield (
                RawItem(external_id="b", payload=b"second", payload_ext=".txt",
                        metadata={"path": "b.txt", "mtime": 0.0}),
                None,
            )

        def normalize(self, item):
            return NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=item.external_id,
                title=item.external_id,
                body=item.payload_bytes().decode(),
            )

    connector = Halfway()
    assert sync_connector(conn, vault, connector, cfg).new == 2

    connector.fail = True
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.errors == 1
    assert stats.deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2


def test_a_scan_that_found_nothing_never_prunes(cfg, conn, vault):
    """An unmounted drive and a user who deleted every note look identical from
    in here, and only one of them is recoverable without re-embedding
    everything."""

    class Vanishing:
        connector_id = "documents"
        enumerates_corpus = True
        empty = False

        def discover(self, options, cursor):
            if self.empty:
                return
            yield (
                RawItem(external_id="a", payload=b"first", payload_ext=".txt",
                        metadata={"path": "a.txt", "mtime": 0.0}),
                None,
            )

        def normalize(self, item):
            return NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=item.external_id,
                title="a",
                body=item.payload_bytes().decode(),
            )

    connector = Vanishing()
    sync_connector(conn, vault, connector, cfg)

    connector.empty = True
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1


def test_a_cursor_based_connector_is_never_pruned(cfg, conn, vault):
    """For a source that yields only what is new since its checkpoint, absence
    from a run means "already ingested" — the opposite conclusion from the same
    evidence, which is why pruning is opt-in."""

    class Incremental:
        connector_id = "git"
        # no enumerates_corpus: the default is the safe one

        def __init__(self):
            self.batch = 0

        def discover(self, options, cursor):
            self.batch += 1
            yield (
                RawItem(external_id=f"commit{self.batch}", payload=b"a change",
                        payload_ext=".txt", metadata={"path": "x", "mtime": 0.0}),
                None,
            )

        def normalize(self, item):
            return NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=item.external_id,
                title=item.external_id,
                body=item.payload_bytes().decode(),
            )

    connector = Incremental()
    sync_connector(conn, vault, connector, cfg)
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2


def test_content_that_became_excluded_actually_leaves_the_index(
    tmp_path, cfg, conn, vault
):
    """"Stop indexing this" has to remove what is already indexed, or the
    setting is decorative — and for a deny rule that is the wrong direction to
    be wrong in. The file is still sitting on disk, so an existence check would
    keep it forever."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "keep.md").write_text("Logs are kept 90 days.", encoding="utf-8")
    excluded = folder / "notes.md"
    excluded.write_text("Once a real note.", encoding="utf-8")
    connector = get_connector("documents")
    _documents_cfg(cfg, folder)
    assert sync_connector(conn, vault, connector, cfg).new == 2

    # The path is unchanged and perfectly readable; it simply no longer
    # qualifies. A deny rule, a new .gitignore line, and a file that stopped
    # being text all arrive here the same way.
    excluded.write_bytes(bytes(range(32)) * 200)
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.deleted == 1
    assert excluded.exists()  # still on disk, and out of the index
    bodies = [r[0] for r in conn.execute("SELECT body FROM entries")]
    assert bodies == ["Logs are kept 90 days."]


def test_a_file_the_walk_could_not_read_keeps_its_entry(
    tmp_path, cfg, conn, vault, monkeypatch
):
    """On Windows "cannot read" routinely means "open in Word right now".
    Pruning it would delete the entry and re-embed it the moment the document
    is closed, on every sync in between."""
    folder = tmp_path / "notes"
    folder.mkdir()
    locked = folder / "budget.md"
    locked.write_text("The Q3 budget is 40k.", encoding="utf-8")
    connector = get_connector("documents")
    _documents_cfg(cfg, folder)
    sync_connector(conn, vault, connector, cfg)

    (folder / "other.md").write_text("Something else entirely.", encoding="utf-8")
    real_open = Path.open

    # Patched at open, which is where Windows actually refuses a locked file,
    # and which `read_bytes` goes through — so the walk and the prune's second
    # look both meet the same lock, as they would in life.
    def refuse_the_locked_file(self, *args, **kwargs):
        if self.name == "budget.md":
            raise PermissionError("used by another process")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_the_locked_file)
    stats = sync_connector(conn, vault, connector, cfg)
    monkeypatch.undo()

    assert stats.deleted == 0
    names = sorted(Path(r[0]).name for r in conn.execute("SELECT external_id FROM entries"))
    assert names == ["budget.md", "other.md"]


def test_appledouble_sidecars_are_not_documents(tmp_path):
    """macOS writes a `._name` resource fork beside every real file on a
    non-native volume, so they arrive by the hundred from any shared drive or
    USB stick — each a binary carrying the real file's extension. Five were
    found in the live index, contributing 21 embedded chunks of structure bytes
    that could come back as evidence."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "README.md").write_text("# Readme\n\nThe real document.", encoding="utf-8")
    # The header of an actual AppleDouble file, magic number and all.
    (folder / "._README.md").write_bytes(
        b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02\x00\x00\x00\t"
        b"\x00\x00\x002\x00\x00\x0e\xb0\x00\x00\x00\x02\x00\x00\x0e\xe2" + bytes(200)
    )

    found = sorted(p.name for p in _discovered_paths(folder))

    assert found == ["README.md"]


def test_a_binary_file_wearing_a_text_extension_is_refused(tmp_path):
    """The name check catches AppleDouble; this catches the general case, since
    any binary saved under a text extension decodes to *something*."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "real.md").write_text("# Real\n\nWritten by a person.", encoding="utf-8")
    (folder / "sneaky.md").write_bytes(bytes(range(32)) * 200)
    (folder / "tabs.md").write_text(
        "# Table\n\ncol\tcol\tcol\n" + "a\tb\tc\n" * 50, encoding="utf-8"
    )

    found = sorted(p.name for p in _discovered_paths(folder))

    # Tabs are not control characters for this purpose: plenty of real notes
    # are mostly tab-separated.
    assert found == ["real.md", "tabs.md"]


def test_a_utf16_document_is_not_mistaken_for_binary(tmp_path):
    """UTF-16 text is half NUL bytes on disk. Probing the raw bytes would refuse
    exactly the files the BOM handling exists to rescue, so the probe decodes
    first — and decodes the whole buffer, since a byte prefix can end
    mid-character."""
    folder = tmp_path / "notes"
    folder.mkdir()
    long_text = "# Notes\n\n" + ("Backups are kept for 30 days. " * 4000)
    (folder / "wide.md").write_bytes(long_text.encode("utf-16"))

    found = sorted(p.name for p in _discovered_paths(folder))

    assert found == ["wide.md"]


def test_a_file_saved_as_ansi_keeps_its_punctuation():
    """The bytes Notepad writes when the encoding box says ANSI. An em-dash is
    0x97 there, which is not valid UTF-8, so decoding with errors="replace"
    turned every one into U+FFFD — in the embedded chunk and in the excerpt
    quoted back to the caller, with the original recoverable the whole time."""
    from varyz.connectors.documents import extract_document_text

    raw = "Deploys — Tuesdays — after the “smoke” suite.".encode("cp1252")
    text = extract_document_text(raw, ".md")

    assert text == "Deploys — Tuesdays — after the “smoke” suite."
    assert "�" not in text


def test_a_file_saved_as_unicode_is_not_read_as_bytes():
    """Notepad's "Unicode" is UTF-16 LE with a BOM. Believing the BOM costs
    nothing and is the file stating its own encoding."""
    from varyz.connectors.documents import extract_document_text

    for encoding in ("utf-16", "utf-8-sig", "utf-32"):
        raw = "Backups are kept for 30 days.".encode(encoding)
        assert extract_document_text(raw, ".txt") == "Backups are kept for 30 days."


def test_valid_utf8_is_never_guessed_at():
    """cp1252 decodes almost any byte sequence, so it must only ever be reached
    after UTF-8 has been tried and failed — otherwise every non-ASCII UTF-8
    document would come back as mojibake."""
    from varyz.connectors.base import decode_text

    for text in ("Привет, как дела?", "日本語のメモ", "café — naïve", "emoji 🎯 ok"):
        assert decode_text(text.encode("utf-8")) == text


def test_documents_truncates_oversized_text():
    from varyz.connectors.documents import MAX_DOCUMENT_CHARS, extract_document_text

    body = ("word " * 400_000).encode("utf-8")
    text = extract_document_text(body, ".txt")
    assert len(text) < MAX_DOCUMENT_CHARS + 100
    assert text.endswith("… truncated by varyz …")
