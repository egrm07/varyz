"""Significance: heuristic scoring, timeline filtering, and LLM refinement."""

import json

from tests.conftest import FakeLLM
from varyz.connectors.manual import ManualConnector
from varyz.domain import Kind
from varyz.pipeline.enrich import pending_chat_days, refine_significance
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.pipeline.significance import score_text


def _msg(text: str) -> str:
    return f"From: Anna\nChat: Anna\n\n{text}"


def test_noise_messages_score_near_zero():
    assert score_text(Kind.MESSAGE, _msg("WOWWW THANKS SO MUCH")) == 0.05
    assert score_text(Kind.MESSAGE, _msg("hahahaha")) == 0.05
    assert score_text(Kind.MESSAGE, _msg("ok")) == 0.05
    assert score_text(Kind.MESSAGE, _msg("🎉🎉🎉")) == 0.05


def test_life_event_message_clears_threshold():
    assert score_text(Kind.MESSAGE, _msg("guess what — I got the job offer and accepted!")) >= 0.55
    assert score_text(Kind.MESSAGE, _msg("we're moving to Berlin in September")) >= 0.55


def test_ordinary_chat_stays_below_threshold():
    assert score_text(Kind.MESSAGE, _msg("see you at the gym at 6 then")) < 0.55


def test_deliberate_captures_score_high():
    assert score_text(Kind.JOURNAL, "Long day at the lake with Anna.") >= 0.85
    assert score_text(Kind.NOTE, "remember: dad's watch is in the blue box") >= 0.75
    assert score_text(Kind.PHOTO, "") >= 0.7


def test_newsletter_email_demoted():
    body = "Weekly digest!\n\nClick here to unsubscribe from this list."
    assert score_text(Kind.EMAIL, body) < 0.3
    assert score_text(Kind.EMAIL, "Hi Egor, are we on for dinner Friday? - Anna") >= 0.55


def test_ingest_stores_significance(cfg, conn, vault):
    gate = SensitivityGate(cfg.sensitivity)
    entry_id, _ = ingest_item(
        conn, vault, ManualConnector(), gate, ManualConnector.make_item("a real thought")
    )
    sig = conn.execute("SELECT significance FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
    assert sig is not None and sig >= 0.75


def _seed_chat(cfg, conn, vault):
    """Three chat-style entries on one day, one of them life-changing."""
    from varyz.db import dao
    from varyz.domain import NormalizedEntry
    from varyz.pipeline.significance import score

    texts = ["morning!", "I'm pregnant!!! we're having a baby", "hahaha ok"]
    for i, text in enumerate(texts):
        norm = NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id=f"chat:{i}",
            body=_msg(text),
            occurred_start=__import__("datetime").datetime(
                2026, 6, 1, 9 + i, tzinfo=__import__("datetime").UTC
            ),
            thread_id="wa:anna",
        )
        entry_id, _ = dao.upsert_entry(
            conn, norm, source="whatsapp",
            sensitivity=cfg.sensitivity.default, significance=score(norm),
        )
        dao.enqueue_job(conn, entry_id, "significance")


def test_refinement_promotes_and_demotes(cfg, conn, vault):
    _seed_chat(cfg, conn, vault)
    days = pending_chat_days(conn)
    assert len(days) == 1 and len(days[0].entries) == 3

    llm = FakeLLM(reply=json.dumps(
        {"important": [{"i": 2, "score": 9, "label": "Anna announces pregnancy"}]}
    ))
    processed, promoted = refine_significance(conn, llm, on_progress=None)
    assert (processed, promoted) == (1, 1)

    scores = dict(conn.execute(
        "SELECT external_id, significance FROM entries ORDER BY external_id"
    ).fetchall())
    assert scores["chat:1"] == 0.9      # promoted
    assert scores["chat:0"] == 0.10     # seen and demoted
    assert scores["chat:2"] == 0.10

    label = conn.execute("SELECT label FROM life_events").fetchone()[0]
    assert "pregnancy" in label
    assert not pending_chat_days(conn)  # jobs marked done


def test_sensitive_chats_never_reach_refinement(cfg, conn, vault):
    from varyz.db import dao
    from varyz.domain import NormalizedEntry, Sensitivity

    norm = NormalizedEntry(kind=Kind.MESSAGE, external_id="s:1", body=_msg("secret"),
                           thread_id="wa:x")
    entry_id, _ = dao.upsert_entry(conn, norm, source="whatsapp",
                                   sensitivity=Sensitivity.SENSITIVE)
    dao.enqueue_job(conn, entry_id, "significance")
    assert pending_chat_days(conn) == []
