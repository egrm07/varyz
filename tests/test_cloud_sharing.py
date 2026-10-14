"""Shared Varyz behavior, tenant boundaries, races and the actual HTTP/MCP contract."""

import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from varyz.cloud import sharing_models as models
from varyz.cloud.app import Settings, create_app
from varyz.cloud.sharing_service import SCOPE, SharingError, identifier


def test_context_draft_is_private_until_owner_browser_review(sharing):
    import re

    app, store, service = sharing
    created = create(service)
    member = accept(service, created)
    bound(service, member)
    context = {
        "explanation": "A website explaining the product and its pricing.",
        "story": [
            {
                "text": "Dropped the crowded pricing layout.",
                "at": "2026-10-10",
                "by": "egr",
                "source": "Owner's reviewed project notes",
            }
        ],
    }
    staged = collaborate(service, action="context", share_id=created["share_id"], context=context)
    assert staged["status"] == "needs_owner_review"
    brief_args = models.Brief(membership_id=member, conversation_id="task-1").model_dump()
    before = service.brief("joemama", "native-guest", brief_args)
    assert context["explanation"] not in json.dumps(before)
    assert "Dropped the crowded" not in json.dumps(before)
    url = "/account/shares/" + created["share_id"]
    with TestClient(app, base_url="https://cloud.example.test") as client:
        client.cookies.set("__Host-session", store.credential("egr", "session", 3600))
        page = client.get(url)
        fingerprint = re.search(r'name="fingerprint" value="([^"]+)"', page.text).group(1)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        saved = client.post(
            url + "/context",
            data={
                "csrf": csrf,
                "fingerprint": fingerprint,
                "explanation": context["explanation"],
                "include_story": "yes",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        client.cookies.clear()
        client.cookies.set("__Host-session", store.credential("joemama", "session", 3600))
        guest = client.get("/account/shared/" + member)
        assert context["explanation"] in guest.text
        assert "Dropped the crowded" not in guest.text
    after = service.brief("joemama", "native-guest", brief_args)
    shared = after["collaboration"]["project_context"]
    assert shared["explanation_origin"] == "owner_reviewed"
    assert shared["agent_context"]["story"] == context["story"]
    assert "never owner instructions" in shared["agent_context"]["how"]
    assert after["publication_revision"] > before["publication_revision"]
    assert "PRIVATE_GENERAL" not in json.dumps(after)


def test_shared_overview_replaces_old_pending_work_and_preserves_dated_story(sharing, monkeypatch):
    _, _, service = sharing
    created = create(service)
    member = accept(service, created)
    monkeypatch.setattr("varyz.cloud.sharing_service.time.time", lambda: 1791630000.0)
    collaborate(
        service,
        action="report",
        share_id=created["share_id"],
        work={
            "summary": "Built the pricing section.",
            "open_work": ["Fix mobile overflow"],
            "next_steps": ["Check phone layout"],
            "next_milestone": "Pricing page ready",
            "decisions": ["Use one pricing column"],
            "checks": ["Desktop layout passed"],
        },
    )
    collaborate(
        service,
        action="report",
        share_id=created["share_id"],
        work={
            "summary": "Fixed mobile overflow.",
            "open_work": [],
            "next_steps": ["Review copy"],
        },
    )
    brief = service.brief(
        "joemama",
        "native-guest",
        models.Brief(
            membership_id=member,
            conversation_id="overview",
            expected_binding_revision=0,
        ).model_dump(),
    )
    value = brief["collaboration"]["project_context"]
    assert value["latest_work"]["summary"] == "Fixed mobile overflow."
    assert value["latest_work"]["by"] == "egr"
    assert value["open_work"] == []
    assert value["next_steps"] == ["Review copy"]
    history = value["agent_context"]["work_history"]
    assert len(history) == 2 and history[1]["open_work"] == ["Fix mobile overflow"]
    assert history[1]["source"] and history[1]["at"]
    assert "vision" not in value and "links" not in value


def test_context_review_rejects_nonowner_stale_drafts_and_revoked_access(sharing):
    from varyz.cloud.sharing_context import approve, read
    from varyz.cloud.sharing_service import encoded
    from varyz.cloud.store import digest

    _, store, service = sharing
    created = create(service)
    member = accept(service, created)
    binding = bound(service, member)
    sid = created["share_id"]
    collaborate(service, action="context", share_id=sid, context={"explanation": "First draft"})
    with store.connection() as db:
        fingerprint = digest(encoded(read(service, db, sid, "draft")))
    denied(404, lambda: approve(service, "joemama", sid, "Forged", fingerprint, True))
    collaborate(service, action="context", share_id=sid, context={"explanation": "Second draft"})
    denied(409, lambda: approve(service, "egr", sid, "First draft", fingerprint, True))
    collaborate(service, action="remove", username="joemama", project="website")
    denied(
        404,
        lambda: service.brief(
            "joemama",
            "native-guest",
            models.Brief(
                membership_id=member,
                conversation_id=binding["conversation_id"],
            ).model_dump(),
        ),
    )


def test_invitation_owner_explanation_reaches_guest_and_context_contract_rejects_vision(sharing):
    from pydantic import ValidationError

    _, _, service = sharing
    result = service.invite_project(
        "egr",
        models.ProjectInvitation(
            project="website",
            recipient="@joemama",
            explanation="A product website.",
        ).model_dump(),
        identifier(),
    )
    member = accept(service, result)
    brief = service.brief(
        "joemama",
        "native-guest",
        models.Brief(
            membership_id=member,
            conversation_id="explained",
            expected_binding_revision=0,
        ).model_dump(),
    )
    assert brief["collaboration"]["project_context"]["explanation"] == "A product website."
    with pytest.raises(ValidationError):
        models.ProjectContext.model_validate({"vision": "Invented ambition"})


def test_context_story_removal_storage_and_project_deletion(sharing):
    from varyz.cloud.sharing_context import approve, read
    from varyz.cloud.sharing_service import encoded
    from varyz.cloud.store import digest

    _, store, service = sharing
    staged = collaborate(
        service,
        action="context",
        project="website",
        context={
            "explanation": "A product website.",
            "story": [
                {
                    "text": "Private draft background",
                    "at": "2026-10-10",
                    "by": "egr",
                    "source": "Owner-reviewed notes",
                }
            ],
        },
    )
    sid = staged["share_id"]
    with store.connection() as db:
        fingerprint = digest(encoded(read(service, db, sid, "draft")))
        assert not db.execute("SELECT 1 FROM shared_invites WHERE share=?", (sid,)).fetchone()
        assert (
            "Private draft background"
            not in db.execute(
                "SELECT payload FROM shared_context WHERE share=?",
                (sid,),
            ).fetchone()["payload"]
        )
    approve(service, "egr", sid, "A product website.", fingerprint, True)
    with store.connection() as db:
        current = read(service, db, sid)
        assert len(current["story"]) == 1
    approve(service, "egr", sid, "A product website.", digest(encoded(current)), False)
    with store.connection() as db:
        assert read(service, db, sid)["story"] == []
        revision = db.execute("SELECT revision FROM shared_projects WHERE id=?", (sid,)).fetchone()[
            "revision"
        ]
    service.lifecycle(
        "egr", sid, {"status": "deleted", "expected_publication_revision": revision}, identifier()
    )
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_context WHERE share=?", (sid,)).fetchone()


@pytest.fixture
def sharing(tmp_path):
    app = create_app(
        Settings(
            database=str(tmp_path / "sharing.sqlite"),
            public_url="https://cloud.example.test",
            shared_varyz_enabled=True,
            archive_encryption_key=Fernet.generate_key().decode(),
        )
    )
    store, service = app.state.store, app.state.sharing
    with store.connection() as db:
        for name in ("egr", "joemama", "outsider", "another_owner"):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified,username) "
                "VALUES(?,?,?,'active',1,?)",
                (name, name + "@example.test", "unused", name),
            )
        db.execute("UPDATE accounts SET status='inactive' WHERE id IN ('joemama','outsider')")
    for kind, record_id, payload in [
        (
            "rule",
            "selected",
            {"statement": "Keep the page calm.", "scope": "website", "rationale": "PRIVATE_REASON"},
        ),
        ("rule", "secret", {"statement": "PRIVATE_UNSELECTED", "scope": "website"}),
        ("rule", "general", {"statement": "PRIVATE_GENERAL", "scope": "*"}),
        ("answer", "known", {"question": "Include pricing?", "answer": "Yes.", "scope": "website"}),
    ]:
        store.push("egr", [{"kind": kind, "id": record_id, "base_version": 0, "payload": payload}])
    return app, store, service


def preview(service, selection=None, share=None):
    data = models.Preview(
        project="website",
        label="Public website",
        selection=selection
        or [
            models.SourceRef(kind="rule", id="selected", version=1),
            models.SourceRef(kind="answer", id="known", version=1),
        ],
        expected_publication_revision=share["revision"] if share else None,
    ).model_dump()
    return service.preview("egr", "session-owner", data, share["id"] if share else None)


def create(service, email="joemama@example.test"):
    receipt = preview(service)
    result = service.create(
        "egr",
        "session-owner",
        models.Publish(
            preview_id=receipt["preview_id"],
            invited_email=email,
            source_fingerprint=receipt["source_fingerprint"],
        ).model_dump(),
        identifier(),
    )
    return result


def accept(service, result, guest="joemama"):
    return service.accept(guest, result["invite_url"].rsplit("/", 1)[-1], identifier())[
        "membership_id"
    ]


def test_combined_shared_page_separates_owner_projects_and_recipient_invites(sharing):
    app, store, service = sharing
    created = create(service)
    with TestClient(app, base_url="https://cloud.example.test") as client:
        client.cookies.set("__Host-session", store.credential("egr", "session", 3600))
        for route in ("/account/shared", "/account/shares"):
            response = client.get(route)
            assert response.status_code == 200
            assert '<h1 tabindex="-1">Shared</h1>' in response.text
            assert 'id="shared-panel-by-me"' in response.text
            assert 'id="shared-panel-with-me"' in response.text
            assert 'aria-labelledby="shared-tab-with-me" hidden' in response.text
            assert "/account/shares/" + created["share_id"] in response.text
            assert "Nothing shared with you yet." in response.text
        client.cookies.clear()
        client.cookies.set("__Host-session", store.credential("joemama", "session", 3600))
        response = client.get("/account/shared")
        assert response.status_code == 200
        assert "No projects shared yet" in response.text
        assert "Join" in response.text and "Public website" in response.text
        assert "/account/shares/" + created["share_id"] not in response.text
        client.cookies.clear()
        client.cookies.set("__Host-session", store.credential("outsider", "session", 3600))
        response = client.get("/account/shared")
        assert response.status_code == 200
        assert "Public website" not in response.text


def test_shared_tabs_support_direct_selection_without_javascript(sharing):
    app, store, service = sharing
    create(service)
    with TestClient(app, base_url="https://cloud.example.test") as client:
        client.cookies.set("__Host-session", store.credential("joemama", "session", 3600))
        for route in ("/account/shared?tab=with-me", "/account/shares?tab=with-me"):
            response = client.get(route)
            assert response.status_code == 200
            assert 'aria-labelledby="shared-tab-by-me" hidden' in response.text
            assert 'aria-labelledby="shared-tab-with-me">' in response.text
            assert 'aria-selected="true" tabindex="0" href="?tab=with-me#with-me"' in response.text
            assert "Public website" in response.text
        response = client.get("/account/shared?tab=invalid")
        assert 'aria-labelledby="shared-tab-with-me" hidden' in response.text


def test_combined_shared_keeps_owner_management_available_when_sharing_is_paused(sharing):
    app, store, service = sharing
    created = create(service)
    app.state.settings.shared_varyz_enabled = False
    with TestClient(app, base_url="https://cloud.example.test") as client:
        client.cookies.set("__Host-session", store.credential("egr", "session", 3600))
        response = client.get("/account/shares")
        assert response.status_code == 200
        assert "/account/shares/" + created["share_id"] in response.text
        assert "Shared projects are currently unavailable" in response.text
        assert "Nothing shared with you yet." not in response.text


def bound(service, member, guest="joemama", task="task-1", connection="native-guest"):
    value = service.brief(
        guest,
        connection,
        models.Brief(
            membership_id=member, conversation_id=task, expected_binding_revision=0
        ).model_dump(),
    )
    return dict(
        membership_id=member, conversation_id=task, binding_revision=value["binding_revision"]
    )


def ask(
    service, binding, question="Use orange?", guest="joemama", key=None, connection="native-guest"
):
    return service.ask(
        guest,
        connection,
        models.Ask(
            **binding,
            idempotency_key=key or identifier(),
            requests=[models.Question(question=question, reason="Synthetic test")],
        ).model_dump(),
    )["results"][0]


def denied(code, operation):
    with pytest.raises(SharingError) as failure:
        operation()
    assert failure.value.status == code


def test_real_owner_guest_roundtrip_and_one_time_reply(sharing):
    _, store, service = sharing
    result = create(service)
    member = accept(service, result)
    binding = bound(service, member)
    brief = service.brief(
        "joemama",
        "native-guest",
        models.Brief(membership_id=member, conversation_id="task-1").model_dump(),
    )
    text = json.dumps(brief)
    assert "Keep the page calm." in text
    assert all(
        private not in text for private in ("PRIVATE_", "selected", "rationale", "@example.test")
    )
    assert brief["coverage"].startswith("Selected published")
    assert ask(service, binding, "  INCLUDE pricing? ")["answer"] == "Yes."
    queued = ask(service, binding)
    assert queued["permission_grant"] is False
    duplicate = ask(service, binding, "USE   ORANGE?")
    assert duplicate["question_id"] == queued["question_id"] and duplicate["already_pending"]
    reply = models.Reply(expected_thread_version=1, answer="Use marigold.").model_dump()
    service.reply("egr", queued["question_id"], reply, identifier())
    status = service.question_status(
        "joemama",
        "native-guest",
        models.QuestionStatus(**binding, thread_ids=[queued["question_id"]]).model_dump(),
    )
    assert status["questions"][0]["reply"]["answer"] == "Use marigold."
    assert status["questions"][0]["state"] == "answered"
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM records WHERE account='egr'").fetchone()["n"] == 4
        )
    assert ask(service, binding)["status"] == "queued"  # one-time reply is not published knowledge


@pytest.mark.parametrize("remember,publish", [(False, True), (True, False), (True, True)])
def test_remember_and_share_are_independent_atomic_choices(sharing, remember, publish):
    _, store, service = sharing
    result = create(service)
    binding = bound(service, accept(service, result))
    queued = ask(service, binding)
    result = service.reply(
        "egr",
        queued["question_id"],
        models.Reply(
            expected_thread_version=1,
            answer="Use marigold.",
            remember_project=remember,
            publish_to_share=publish,
            expected_publication_revision=1 if publish else None,
        ).model_dump(),
        identifier(),
    )
    assert bool(result["remembered_record_id"]) == remember
    assert bool(result["published_revision"]) == publish
    with store.connection() as db:
        rows = db.execute(
            "SELECT payload FROM records WHERE account='egr' AND kind='answer'"
        ).fetchall()
        assert any(json.loads(r["payload"])["answer"] == "Use marigold." for r in rows) == remember
    assert (ask(service, binding)["status"] == "answered") == publish
    if remember:
        pulled = store.pull("egr", 0, 20)["records"]
        assert any(r["id"] == result["remembered_record_id"] for r in pulled)


def test_pending_private_update_and_explicit_manual_answer_retraction(sharing):
    _, store, service = sharing
    share = create(service)
    binding = bound(service, accept(service, share))
    thread = ask(service, binding)["question_id"]
    service.reply(
        "egr",
        thread,
        models.Reply(
            expected_thread_version=1,
            answer="Synthetic shared answer",
            publish_to_share=True,
            expected_publication_revision=1,
        ).model_dump(),
        identifier(),
    )
    detail = service.owner_detail("egr", share["share_id"])
    manual = next(item for item in detail["items"] if item["origin"] == "owner_shared_answer")
    assert ask(service, binding)["status"] == "answered"
    store.push(
        "egr",
        [
            {
                "kind": "rule",
                "id": "selected",
                "base_version": 1,
                "payload": {"statement": "Private update awaiting review", "scope": "website"},
            }
        ],
    )
    assert service.owner_detail("egr", share["share_id"])["pending_updates"] == 1
    key = identifier()
    data = models.RetractAnswer(
        item_id=manual["item_id"], expected_publication_revision=2
    ).model_dump()
    result = service.retract_answer("egr", share["share_id"], data, key)
    assert service.retract_answer("egr", share["share_id"], data, key) == result
    assert result["publication_revision"] == 3
    assert ask(service, binding)["status"] == "queued"
    assert all(
        item["item_id"] != manual["item_id"]
        for item in service.owner_detail("egr", share["share_id"])["items"]
    )


def test_sharing_storage_limit_rolls_back_but_does_not_block_revocation(sharing):
    app, store, service = sharing
    share = create(service)
    member = accept(service, share)
    binding = bound(service, member)
    app.state.settings.storage_limit = 1
    denied(429, lambda: ask(service, binding))
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_threads").fetchone()["n"] == 0
    service.end_member("egr", member, {"expected_revision": 1}, identifier())
    denied(404, lambda: bound(service, member))


def test_invite_is_verified_email_bound_single_use_and_revoke_blocks_replay(sharing):
    _, store, service = sharing
    result = create(service)
    token = result["invite_url"].rsplit("/", 1)[-1]
    denied(403, lambda: service.accept("outsider", token, identifier()))
    with store.connection() as db:
        db.execute("UPDATE accounts SET email_verified=0 WHERE id='joemama'")
    denied(404, lambda: service.accept("joemama", token, identifier()))
    with store.connection() as db:
        db.execute("UPDATE accounts SET email_verified=1 WHERE id='joemama'")
    key = identifier()
    result = service.accept("joemama", token, key)
    assert service.accept("joemama", token, key) == result
    member = result["membership_id"]
    service.end_member("egr", member, {"expected_revision": 1}, identifier())
    denied(410, lambda: service.accept("joemama", token, key))
    denied(404, lambda: bound(service, member))


def test_recipient_thread_audience_and_owner_authority(sharing):
    _, _, service = sharing
    share = create(service)
    m1 = accept(service, share)
    invite = service.invite(
        "egr", share["share_id"], {"invited_email": "outsider@example.test"}, identifier()
    )
    m2 = accept(service, invite, "outsider")
    b1, b2 = bound(service, m1), bound(service, m2, "outsider")
    q1, q2 = ask(service, b1), ask(service, b2, guest="outsider")
    assert q1["question_id"] != q2["question_id"]
    denied(
        404,
        lambda: service.question_status(
            "outsider",
            "native-guest",
            models.QuestionStatus(
                **b2, thread_ids=[q1["question_id"], q2["question_id"]]
            ).model_dump(),
        ),
    )
    denied(
        404,
        lambda: service.reply(
            "joemama",
            q1["question_id"],
            models.Reply(expected_thread_version=1, answer="Fake").model_dump(),
            identifier(),
        ),
    )
    denied(404, lambda: service.owner_detail("joemama", share["share_id"]))
    denied(404, lambda: service.end_member("outsider", m1, {"expected_revision": 1}, identifier()))


def test_private_edits_do_not_leak_preview_cas_and_retraction_suppresses(sharing):
    _, store, service = sharing
    share = create(service)
    member = accept(service, share)
    binding = bound(service, member)
    receipt = preview(service)
    store.push(
        "egr",
        [
            {
                "kind": "rule",
                "id": "selected",
                "base_version": 1,
                "payload": {"statement": "PRIVATE_EDIT", "scope": "website"},
            }
        ],
    )
    denied(
        409,
        lambda: service.create(
            "egr",
            "session-owner",
            models.Publish(
                preview_id=receipt["preview_id"],
                invited_email="outsider@example.test",
                source_fingerprint=receipt["source_fingerprint"],
            ).model_dump(),
            identifier(),
        ),
    )
    data = models.Brief(membership_id=member, conversation_id="task-1").model_dump()
    brief = service.brief("joemama", "native-guest", data)
    assert "Keep the page calm." in json.dumps(brief) and "PRIVATE_EDIT" not in json.dumps(brief)
    details = service.owner_detail("egr", share["share_id"])
    receipt = preview(service, [models.SourceRef(kind="rule", id="selected", version=2)], details)
    service.update(
        "egr",
        "session-owner",
        details["id"],
        models.Update(
            preview_id=receipt["preview_id"], expected_publication_revision=1
        ).model_dump(),
        identifier(),
    )
    assert "PRIVATE_EDIT" in json.dumps(service.brief("joemama", "native-guest", data))
    store.push("egr", [{"kind": "rule", "id": "selected", "base_version": 2, "payload": None}])
    brief = service.brief("joemama", "native-guest", data)
    assert brief["items"] == [] and brief["publication_revision"] == 3
    assert service.finish("joemama", "native-guest", binding)["pending_question_ids"] == []


def test_stale_task_switch_and_connection_isolation(sharing):
    _, _, service = sharing
    m1, m2 = accept(service, create(service)), accept(service, create(service))
    b1 = bound(service, m1)
    denied(409, lambda: ask(service, b1, connection="different-native"))
    denied(
        409,
        lambda: service.brief(
            "joemama",
            "native-guest",
            models.Brief(membership_id=m2, conversation_id="task-1").model_dump(),
        ),
    )
    switched = service.brief(
        "joemama",
        "native-guest",
        models.Brief(
            membership_id=m2, conversation_id="task-1", expected_binding_revision=1
        ).model_dump(),
    )
    assert switched["binding_revision"] == 2
    denied(409, lambda: ask(service, b1))


def test_paused_inactive_owner_and_kill_switch_stop_reads_and_replies(sharing):
    app, store, service = sharing
    result = create(service)
    binding = bound(service, accept(service, result))
    q = ask(service, binding)
    service.lifecycle(
        "egr",
        result["share_id"],
        {"status": "paused", "expected_publication_revision": 1},
        identifier(),
    )
    denied(503, lambda: ask(service, binding))
    denied(
        503,
        lambda: service.reply(
            "egr",
            q["question_id"],
            models.Reply(expected_thread_version=1, answer="No").model_dump(),
            identifier(),
        ),
    )
    service.lifecycle(
        "egr",
        result["share_id"],
        {"status": "active", "expected_publication_revision": 1},
        identifier(),
    )
    with store.connection() as db:
        db.execute("UPDATE accounts SET status='inactive' WHERE id='egr'")
    denied(
        503,
        lambda: service.question_status(
            "joemama",
            "native-guest",
            models.QuestionStatus(**binding, thread_ids=[q["question_id"]]).model_dump(),
        ),
    )
    app.state.settings.shared_varyz_enabled = False
    denied(503, lambda: ask(service, binding))
    # Revocation remains possible when disabled and inactive.
    service.end_member("egr", binding["membership_id"], {"expected_revision": 1}, identifier())


def test_idempotency_body_mismatch_and_quota_rollback(sharing):
    _, store, service = sharing
    binding = bound(service, accept(service, create(service)))
    key = identifier()
    first = ask(service, binding, key=key)
    assert ask(service, binding, key=key) == first
    denied(409, lambda: ask(service, binding, "Different?", key=key))
    for i in range(4):
        ask(service, binding, f"Other {i}?")
    denied(
        429,
        lambda: service.ask(
            "joemama",
            "native-guest",
            models.Ask(
                **binding,
                idempotency_key=identifier(),
                requests=[
                    models.Question(question="Over budget 1?"),
                    models.Question(question="Over budget 2?"),
                ],
            ).model_dump(),
        ),
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_threads").fetchone()["n"] == 5


def test_reply_conflict_rolls_back_private_write_and_thread(sharing):
    _, store, service = sharing
    share = create(service)
    binding = bound(service, accept(service, share))
    queued = ask(service, binding)
    denied(
        409,
        lambda: service.reply(
            "egr",
            queued["question_id"],
            models.Reply(
                expected_thread_version=1,
                answer="Use marigold.",
                remember_project=True,
                publish_to_share=True,
                expected_publication_revision=0,
            ).model_dump(),
            identifier(),
        ),
    )
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM records WHERE account='egr'").fetchone()["n"] == 4
        )
        assert db.execute("SELECT COUNT(*) AS n FROM shared_replies").fetchone()["n"] == 0
        assert db.execute("SELECT status FROM shared_threads").fetchone()["status"] == "open"


def test_concurrent_accept_duplicate_questions_and_reply_vs_revoke(sharing):
    _, store, service = sharing
    result = create(service)
    token = result["invite_url"].rsplit("/", 1)[-1]
    barrier = Barrier(2)

    def accept_race(_):
        barrier.wait()
        return service.accept("joemama", token, identifier())

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(accept_race, range(2)))
    assert results[0]["membership_id"] == results[1]["membership_id"]
    binding = bound(service, results[0]["membership_id"])

    def ask_race(_):
        barrier.wait()
        return ask(service, binding)

    with ThreadPoolExecutor(2) as pool:
        questions = list(pool.map(ask_race, range(2)))
    assert questions[0]["question_id"] == questions[1]["question_id"]

    def final_race(operation):
        barrier.wait()
        try:
            if operation == "reply":
                return service.reply(
                    "egr",
                    questions[0]["question_id"],
                    models.Reply(expected_thread_version=1, answer="Synthetic reply").model_dump(),
                    identifier(),
                )
            return service.end_member(
                "egr", binding["membership_id"], {"expected_revision": 1}, identifier()
            )
        except SharingError as error:
            return error.status

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(final_race, ["reply", "revoke"]))
    denied(
        404,
        lambda: service.question_status(
            "joemama",
            "native-guest",
            models.QuestionStatus(**binding, thread_ids=[questions[0]["question_id"]]).model_dump(),
        ),
    )
    with store.connection() as db:
        assert db.execute("SELECT state FROM shared_memberships").fetchone()["state"] == "revoked"


def browser(app, store, name):
    client = TestClient(app, base_url="https://cloud.example.test")
    client.cookies.set(
        "__Host-session", store.credential(name, "session", 3600), domain="cloud.example.test"
    )
    client.cookies.set("__Host-csrf", "test-browser-csrf", domain="cloud.example.test")
    return client


@pytest.mark.parametrize("age", [1000, 20 * 86400])
def test_persistent_session_invites_after_browser_and_app_restart(sharing, age):
    import re

    app, store, service = sharing
    authenticated = str(time.time() - age)
    token = store.credential("egr", "session", 10 * 86400, challenge=authenticated)
    restarted_app = create_app(app.state.settings)
    client = browser(restarted_app, restarted_app.state.store, "egr")
    client.cookies.set("__Host-session", token, domain="cloud.example.test")
    page = client.get("/account/shares/new?project=website")
    assert page.status_code == 200
    assert any(
        "Max-Age=2592000" in cookie and cookie.startswith("__Host-csrf=")
        for cookie in page.headers.get_list("set-cookie")
    )
    data = invitation_draft()
    data["csrf"] = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post("/account/shares/new", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert "Invitation sent" in client.get(response.headers["location"]).text
    assert not client.cookies.get("__Host-sharing_draft")
    assert store.lookup(token, "session")["challenge"] == authenticated
    assert store.lookup(token, "session")["expires"] > time.time() + 29 * 86400
    cookies = dict(client.cookies)
    client.close()
    again = TestClient(create_app(app.state.settings), base_url="https://cloud.example.test")
    for name, value in cookies.items():
        again.cookies.set(name, value, domain="cloud.example.test")
    assert again.get("/account/shared").status_code == 200
    data["recipient"] = "outsider"
    data["idempotency_key"] = identifier()
    assert again.post("/account/shares/new", data=data, follow_redirects=False).status_code == 303


def test_persistent_session_routine_actions_and_deletion_boundary(sharing):
    app, store, service = sharing
    result = create(service)
    membership = accept(service, result)
    thread = ask(service, bound(service, membership))["question_id"]
    client = browser(app, store, "egr")
    token = store.credential("egr", "session", 20 * 86400, challenge=str(time.time() - 20 * 86400))
    client.cookies.set("__Host-session", token, domain="cloud.example.test")
    headers = {"X-CSRF-Token": "test-browser-csrf", "Idempotency-Key": identifier()}
    api = client.post(
        "/api/sharing/v1/shares/" + result["share_id"] + "/invites",
        json={"invited_email": "outsider@example.test"},
        headers=headers,
    )
    assert api.status_code == 200
    reply = client.post(
        "/account/questions/" + thread + "/reply",
        data={
            "csrf": "test-browser-csrf",
            "answer": "Reply from my persistent session",
            "expected_thread_version": "1",
            "idempotency_key": identifier(),
        },
        follow_redirects=False,
    )
    assert reply.status_code == 303 and reply.headers["location"] == "/account/questions"
    action = {
        "csrf": "test-browser-csrf",
        "operation": "state",
        "share_id": result["share_id"],
        "status": "paused",
        "expected_publication_revision": "1",
        "idempotency_key": identifier(),
    }
    paused = client.post("/account/sharing/action", data=action, follow_redirects=False)
    assert paused.status_code == 303 and paused.headers["location"] == "/account/shares"
    api = client.post(
        "/api/sharing/v1/shares/" + result["share_id"] + "/state",
        json={"status": "deleted", "expected_publication_revision": 1},
        headers={**headers, "Idempotency-Key": identifier()},
    )
    assert api.status_code == 403 and "Sign in again" in api.text
    action.update(status="deleted", confirm="on", idempotency_key=identifier())
    deletion = client.post("/account/sharing/action", data=action, follow_redirects=False)
    assert deletion.status_code == 303 and deletion.headers["location"] == "/login?reauth=1"
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT status FROM shared_projects WHERE id=?", (result["share_id"],)
            ).fetchone()["status"]
            == "paused"
        )


def test_simple_invitation_accepts_username_and_reuses_project(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    page = client.get("/account/shares/new?project=website")
    assert "Share website" in page.text and "Email or username" in page.text
    for removed in (
        "Choose what to share",
        "Collaboration name",
        "Review selected",
        "PRIVATE_GENERAL",
        "PRIVATE_UNSELECTED",
    ):
        assert removed not in page.text
    data = {
        "csrf": "test-browser-csrf",
        "project": "website",
        "recipient": "@joemama",
        "level": "consult",
        "idempotency_key": identifier(),
    }
    sent = client.post("/account/shares/new", data=data)
    assert sent.status_code == 200 and "Invitation sent" in sent.text
    assert "Copy invitation" not in sent.text
    with store.connection() as db:
        share = db.execute("SELECT * FROM shared_projects WHERE owner='egr'").fetchone()
        publication = service.publication(db, dict(share))
    assert share["label"] == "website"
    assert "PRIVATE_GENERAL" not in json.dumps(publication)
    assert "PRIVATE_REASON" not in json.dumps(publication)
    assert client.post("/account/shares/new", data=data).status_code == 200
    service.invite_project(
        "egr", {"project": "website", "recipient": "outsider", "level": "consult"}, identifier()
    )
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM shared_projects WHERE owner='egr'").fetchone()[
                "n"
            ]
            == 1
        )


def test_new_email_invitation_delivers_and_requires_verified_matching_account(sharing):
    app, store, service = sharing
    sent = []
    service.settings.email_sender = sent.append
    result = service.invite_project(
        "egr",
        {"project": "website", "recipient": "new@example.test", "level": "decide"},
        identifier(),
    )
    token = result["invite_url"].rsplit("/", 1)[-1]
    denied(403, lambda: service.accept("outsider", token, identifier()))
    assert service.deliver_notices() == 1
    assert sent[0]["To"] == "new@example.test" and result["invite_url"] in sent[0].get_content()
    assert "PRIVATE" not in sent[0].as_string()
    assert service.deliver_notices() == 0
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified,username) "
            "VALUES('new','new@example.test','unused','inactive',0,'new_person')"
        )
    denied(404, lambda: service.accept("new", token, identifier()))
    with store.connection() as db:
        db.execute("UPDATE accounts SET email_verified=1 WHERE id='new'")
    assert service.accept("new", token, identifier())["membership_id"]


def test_simple_invitation_errors_preserve_draft_and_do_not_create_shares(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    data = {
        "csrf": "test-browser-csrf",
        "project": "website",
        "recipient": "@missing",
        "level": "decide",
        "idempotency_key": identifier(),
    }
    response = client.post("/account/shares/new", data=data)
    assert response.status_code == 404 and 'value="@missing"' in response.text
    assert 'value="decide" selected' in response.text
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_projects").fetchone()
    data["csrf"] = "wrong"
    assert client.post("/account/shares/new", data=data).status_code == 403


def test_invitation_from_existing_share_targets_that_share_and_checks_owner(sharing):
    app, store, service = sharing
    first, second = create(service), create(service)
    client = browser(app, store, "egr")
    response = client.get("/account/shares/new?share_id=" + second["share_id"])
    assert response.status_code == 200
    assert 'name="share_id" value="' + second["share_id"] + '"' in response.text
    result = service.invite_project(
        "egr",
        {
            "project": "website",
            "share_id": second["share_id"],
            "recipient": "outsider",
            "level": "consult",
        },
        identifier(),
    )
    assert result["share_id"] == second["share_id"] != first["share_id"]
    stranger = browser(app, store, "another_owner")
    assert stranger.get("/account/shares/new?share_id=" + second["share_id"]).status_code == 404


@pytest.mark.parametrize(
    "operation,fields",
    [
        ("invite", {"invited_email": "new@example.test"}),
        ("revoke_invite", {}),
        ("retract_answer", {"confirm": "on"}),
        ("revoke", {"confirm": "on"}),
        ("leave", {"confirm": "on"}),
        ("state", {"status": "paused"}),
        ("withdraw", {}),
        (
            "state",
            {"share_id": "x" * 32, "status": "paused", "expected_publication_revision": "oops"},
        ),
        ("revoke", {"membership_id": "x" * 32, "confirm": "on", "expected_revision": "oops"}),
    ],
)
def test_browser_malformed_actions_are_readable_and_never_mutate(sharing, operation, fields):
    app, store, service = sharing
    client = browser(app, store, "egr")
    response = client.post(
        "/account/sharing/action",
        data={
            "csrf": "test-browser-csrf",
            "operation": operation,
            "idempotency_key": identifier(),
            **fields,
        },
    )
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_projects").fetchone()


def test_browser_publish_bad_revision_is_readable(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    response = client.post(
        "/account/shares/" + "x" * 32 + "/publish",
        data={
            "csrf": "test-browser-csrf",
            "reviewed": "yes",
            "expected_publication_revision": "oops",
            "preview_id": "x" * 32,
            "idempotency_key": identifier(),
        },
    )
    assert response.status_code == 422 and "text/html" in response.headers["content-type"]


def test_paused_project_invite_does_not_create_another_active_share(sharing):
    app, store, service = sharing
    result = create(service)
    service.lifecycle(
        "egr",
        result["share_id"],
        {"status": "paused", "expected_publication_revision": 1},
        identifier(),
    )
    client = browser(app, store, "egr")
    response = client.post("/account/shares/new", data=invitation_draft())
    assert response.status_code == 409 and "paused" in response.text.lower()
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_projects").fetchone()["n"] == 1
        assert db.execute("SELECT status FROM shared_projects").fetchone()["status"] == "paused"


def test_full_pending_invite_limit_allows_reissue_only(sharing):
    app, store, service = sharing
    first = service.invite_project(
        "egr",
        {"project": "website", "recipient": "p0@example.test", "level": "consult"},
        identifier(),
    )
    for i in range(1, 10):
        service.invite(
            "egr", first["share_id"], {"invited_email": f"p{i}@example.test"}, identifier()
        )
    denied(
        429,
        lambda: service.invite(
            "egr", first["share_id"], {"invited_email": "overflow@example.test"}, identifier()
        ),
    )
    reissued = service.invite(
        "egr", first["share_id"], {"invited_email": "p0@example.test"}, identifier()
    )
    assert reissued["invite_id"] != first["invite_id"]
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM shared_invites WHERE state='pending'").fetchone()[
                "n"
            ]
            == 10
        )
        assert (
            db.execute(
                "SELECT state FROM shared_invites WHERE id=?", (first["invite_id"],)
            ).fetchone()["state"]
            == "revoked"
        )


def test_expired_session_reply_restores_private_answer(sharing):
    import re

    app, store, service = sharing
    membership = accept(service, create(service))
    binding = bound(service, membership)
    thread = ask(service, binding)["question_id"]
    client = browser(app, store, "egr")
    page = client.get("/account/questions/" + thread)
    hint = re.search(r'name="sharing_owner" value="([^"]+)"', page.text).group(1)
    stale_browser(store, client, expired=True)
    data = {
        "csrf": "test-browser-csrf",
        "sharing_owner": hint,
        "answer": "Private saved draft",
        "expected_thread_version": "1",
        "idempotency_key": identifier(),
    }
    assert (
        client.post(
            "/account/questions/" + thread + "/reply", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    fresh_browser(store, client)
    resumed = client.get("/account/sharing/resume")
    assert resumed.status_code == 200 and "Private saved draft" in resumed.text
    with store.connection() as db:
        assert (
            db.execute("SELECT status FROM shared_threads WHERE id=?", (thread,)).fetchone()[
                "status"
            ]
            == "open"
        )


def test_invitation_mail_parallel_delivery_claims_once(sharing):
    from threading import Event

    app, store, service = sharing
    service.invite_project(
        "egr",
        {"project": "website", "recipient": "new@example.test", "level": "consult"},
        identifier(),
    )
    started, release = Event(), Event()
    sent = []

    def send(message):
        sent.append(message)
        started.set()
        assert release.wait(10)

    service.settings.email_sender = send
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.deliver_notices)
        assert started.wait(5)
        try:
            assert pool.submit(service.deliver_notices).result(timeout=3) == 0
        finally:
            release.set()
        assert first.result(timeout=5) == 1
    assert len(sent) == 1


def test_invitation_mail_failed_send_retries_and_revoked_links_are_skipped(sharing):
    import smtplib

    app, store, service = sharing
    data = {"project": "website", "recipient": "new@example.test", "level": "consult"}
    first = service.invite_project("egr", data, identifier())
    second = service.invite_project("egr", data, identifier())

    def unavailable(message):
        raise smtplib.SMTPException("Synthetic mail outage")

    service.settings.email_sender = unavailable
    assert service.deliver_notices() == 0
    sent = []
    service.settings.email_sender = sent.append
    assert service.deliver_notices() == 1 and len(sent) == 1
    assert second["invite_url"] in sent[0].get_content()
    assert first["invite_url"] not in sent[0].get_content()
    assert service.deliver_notices() == 0


def test_invitation_mail_recovers_expired_worker_lease(sharing):
    app, store, service = sharing
    result = service.invite_project(
        "egr",
        {"project": "website", "recipient": "new@example.test", "level": "consult"},
        identifier(),
    )
    sent = []
    service.settings.email_sender = sent.append
    with store.connection() as db:
        db.execute(
            "UPDATE shared_invitation_mail SET sent=? WHERE invite=?",
            (-(time.time() + 60), result["invite_id"]),
        )
    assert service.deliver_notices() == 0
    with store.connection() as db:
        db.execute(
            "UPDATE shared_invitation_mail SET sent=? WHERE invite=?",
            (-(time.time() - 1), result["invite_id"]),
        )
    assert service.deliver_notices() == 1 and len(sent) == 1


def test_expired_session_preview_restores_selection_without_publishing(sharing):
    import re

    app, store, service = sharing
    share = create(service)["share_id"]
    client = browser(app, store, "egr")
    page = client.get("/account/shares/" + share + "/edit")
    hint = re.search(r'name="sharing_owner" value="([^"]+)"', page.text).group(1)
    stale_browser(store, client, expired=True)
    data = {
        "csrf": "test-browser-csrf",
        "sharing_owner": hint,
        "project": "website",
        "label": "Public website",
        "selected": ["rule:selected:1"],
        "expected_publication_revision": "1",
    }
    assert (
        client.post(
            "/account/shares/" + share + "/preview", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    fresh_browser(store, client)
    resumed = client.get("/account/sharing/resume")
    assert resumed.status_code == 200 and 'value="rule:selected:1" checked' in resumed.text
    assert client.post("/account/shares/" + share + "/preview", data=data).status_code == 200
    with store.connection() as db:
        assert (
            db.execute("SELECT revision FROM shared_projects WHERE id=?", (share,)).fetchone()[
                "revision"
            ]
            == 1
        )


def test_expired_session_acceptance_returns_to_confirmation_without_joining(sharing):
    import re

    app, store, service = sharing
    invitation = create(service)
    client = browser(app, store, "joemama")
    page = client.get(invitation["invite_url"])
    path = page.request.url.path
    assert path.startswith("/account/shared/invitations/")
    hint = re.search(r'name="sharing_owner" value="([^"]+)"', page.text).group(1)
    with store.connection() as db:
        db.execute(
            "UPDATE credentials SET expires=? WHERE account='joemama' AND kind='session'",
            (time.time() - 1,),
        )
    data = {"csrf": "test-browser-csrf", "sharing_owner": hint, "idempotency_key": identifier()}
    assert client.post(path + "/accept", data=data, follow_redirects=False).status_code == 303
    fresh_browser(store, client, "joemama")
    resumed = client.get("/account/sharing/resume")
    assert resumed.status_code == 200 and "Accept invitation" in resumed.text
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_memberships").fetchone()
    assert client.post(path + "/accept", data=data, follow_redirects=False).status_code == 303


def test_sharing_preview_rejects_file_fields_without_server_error(sharing):
    app, store, service = sharing
    share = create(service)["share_id"]
    client = browser(app, store, "egr")
    response = client.post(
        "/account/shares/" + share + "/preview",
        data={"csrf": "test-browser-csrf"},
        files={"selected": ("unexpected.txt", b"bad")},
    )
    assert response.status_code == 422 and "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/account/shares/new/publish", "/account/sharing/action"])
def test_unbound_expired_post_never_redirects_to_post_only_endpoint(sharing, path):
    app, store, service = sharing
    client = browser(app, store, "egr")
    stale_browser(store, client, expired=True)
    data = {"csrf": "test-browser-csrf", "private_text": "Must not attach to another account"}
    assert client.post(path, data=data, follow_redirects=False).status_code == 303
    fresh_browser(store, client)
    response = client.get("/account/sharing/resume")
    assert response.status_code == 410 and "could not be restored safely" in response.text
    assert "Must not attach" not in response.text
    assert not client.cookies.get("__Host-sharing_draft")


def test_shared_oauth_pkce_refresh_cannot_widen_scope(sharing):
    app, store, service = sharing
    member = accept(service, create(service))
    client = browser(app, store, "joemama")
    verifier = "s" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    redirect = "https://connector.example.test/callback"
    registered = client.post("/oauth/register", json={"redirect_uris": [redirect]}).json()
    params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": redirect,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": "guest-test",
        "resource": "https://cloud.example.test/mcp/shared",
        "scope": "varyz varyz.shared",
    }
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200
    assert "Consult projects people have shared" in consent.text
    response = client.post(
        "/oauth/authorize",
        data={**params, "csrf": client.cookies.get("__Host-csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    exchange = {
        "grant_type": "authorization_code",
        "client_id": registered["client_id"],
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    }
    assert (
        client.post("/oauth/token", data={**exchange, "code_verifier": "x" * 64}).status_code == 400
    )
    # A failed PKCE attempt does not grant credentials; use a fresh consent code.
    response = client.post(
        "/oauth/authorize",
        data={**params, "csrf": client.cookies.get("__Host-csrf")},
        follow_redirects=False,
    )
    exchange["code"] = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    tokens = client.post("/oauth/token", data=exchange).json()
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    tool = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "shared_brief",
            "arguments": {
                "membership_id": member,
                "conversation_id": "oauth-task",
                "expected_binding_revision": 0,
            },
        },
    }
    first = client.post("/mcp/shared", json=tool, headers=headers).json()["result"][
        "structuredContent"
    ]
    assert first["binding_revision"] == 1
    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": tokens["refresh_token"],
            "scope": "varyz",
        },
    ).json()
    new_headers = {"Authorization": "Bearer " + refreshed["access_token"]}
    assert client.post("/mcp", json=tool, headers=new_headers).status_code == 403
    tool["params"] = {
        "name": "shared_finish",
        "arguments": {
            "membership_id": member,
            "conversation_id": "oauth-task",
            "binding_revision": 1,
        },
    }
    assert (
        not client.post("/mcp/shared", json=tool, headers=new_headers)
        .json()["result"]
        .get("isError")
    )


def test_reply_draft_survives_session_expiry(sharing):
    app, store, service = sharing
    binding = bound(service, accept(service, create(service)))
    thread = ask(service, binding)["question_id"]
    client = browser(app, store, "egr")
    hint = expire_browser(store, client, "/account/questions/" + thread)
    request_key = identifier()
    response = client.post(
        "/account/questions/" + thread + "/reply",
        data={
            "csrf": "test-browser-csrf",
            "answer": "Preserved owner draft",
            "sharing_owner": hint,
            "remember_project": "1",
            "expected_thread_version": "1",
            "publication_revision": "1",
            "idempotency_key": request_key,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/login?reauth=1"
    assert client.cookies.get("__Host-sharing_draft")
    fresh_browser(store, client)
    resumed = client.get("/account/sharing/resume")
    assert resumed.status_code == 200
    assert "Preserved owner draft" in resumed.text and request_key in resumed.text
    assert 'name="remember_project" value="1" checked' in resumed.text
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_replies").fetchone()["n"] == 0


def stale_browser(store, client, *, expired=False):
    with store.connection() as db:
        db.execute(
            "UPDATE credentials SET challenge=? WHERE account='egr' AND kind='session'",
            (str(time.time() - 1000),),
        )
        if expired:
            db.execute(
                "UPDATE credentials SET expires=? WHERE account='egr' AND kind='session'",
                (time.time() - 1,),
            )


def expire_browser(store, client, path="/account/shares/new?project=website"):
    import re

    page = client.get(path)
    assert page.status_code == 200
    hint = re.search(r'name="sharing_owner" value="([^"]+)"', page.text).group(1)
    stale_browser(store, client, expired=True)
    return hint


def invitation_draft(**changes):
    return {
        "csrf": "test-browser-csrf",
        "project": "website",
        "recipient": "@joemama",
        "level": "decide",
        "idempotency_key": identifier(),
        **changes,
    }


def fresh_browser(store, client, name="egr"):
    client.cookies.set(
        "__Host-session", store.credential(name, "session", 3600), domain="cloud.example.test"
    )


@pytest.mark.parametrize("expired", [True])
def test_invitation_reauth_preserves_private_draft_and_requires_resubmit(sharing, expired):
    import re

    app, store, service = sharing
    client = browser(app, store, "egr")
    page = client.get("/account/shares/new?project=website")
    hint = re.search(r'name="sharing_owner" value="([^"]+)"', page.text).group(1)
    stale_browser(store, client, expired=expired)
    data = invitation_draft(sharing_owner=hint)
    response = client.post("/account/shares/new", data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login?reauth=1"
    assert client.cookies.get("__Host-sharing_draft")
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_projects").fetchone()
        encrypted = db.execute("SELECT params FROM oauth_flows").fetchone()["params"]
        assert "@joemama" not in encrypted and data["idempotency_key"] not in encrypted
    fresh_browser(store, client)
    response = client.get("/account/sharing/resume")
    assert response.status_code == 200 and 'value="@joemama"' in response.text
    assert 'value="decide" selected' in response.text and data["idempotency_key"] in response.text
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_invites").fetchone()
    assert client.post("/account/shares/new", data=data).status_code == 200
    assert client.post("/account/shares/new", data=data).status_code == 200
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_invites").fetchone()["n"] == 1


@pytest.mark.parametrize("kind", ["plaintext", "tampered", "expired"])
def test_expired_session_recovery_rejects_forged_owner_hint(sharing, kind):
    import json

    app, store, service = sharing
    client = browser(app, store, "egr")
    hint = {"phase": "sharing-form-owner", "actor": "egr", "expires": time.time() + 3600}
    if kind == "expired":
        hint["expires"] = time.time() - 1
    value = json.dumps(hint) if kind == "plaintext" else service.seal(hint)
    if kind == "tampered":
        value = value[:-4] + "abcd"
    stale_browser(store, client, expired=True)
    response = client.post(
        "/account/shares/new",
        data=invitation_draft(recipient="private@example.test", sharing_owner=value),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with store.connection() as db:
        saved = service.open(db.execute("SELECT params FROM oauth_flows").fetchone()["params"])
        assert saved["actor"] is None and saved["draft"] == {}
        assert not db.execute("SELECT 1 FROM shared_invites").fetchone()


def test_reauth_real_login_rotates_session_and_returns_to_saved_invitation(sharing):
    import re

    from varyz.cloud.app import password_hash
    from varyz.cloud.store import digest

    app, store, service = sharing
    password = "Synthetic-password-for-reauth"
    with store.connection() as db:
        db.execute("UPDATE accounts SET password=? WHERE id='egr'", (password_hash(password),))
    client = browser(app, store, "egr")
    original = client.cookies.get("__Host-session")
    hint = expire_browser(store, client)
    data = invitation_draft(sharing_owner=hint)
    assert client.post("/account/shares/new", data=data, follow_redirects=False).status_code == 303
    login = client.get("/login?reauth=1")
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text).group(1)
    failed = client.post(
        "/login",
        data={"email": "egr@example.test", "password": "wrong", "csrf": csrf},
        follow_redirects=False,
    )
    assert failed.status_code == 401 and "text/html" in failed.headers["content-type"]
    assert "Try signing in again" in failed.text and client.cookies.get("__Host-sharing_draft")
    success = client.post(
        "/login",
        data={"email": "egr@example.test", "password": password, "csrf": csrf},
        follow_redirects=False,
    )
    assert success.status_code == 303
    assert success.headers["location"] == "/account/sharing/resume"
    with store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM credentials WHERE hash=?", (digest(original),)
        ).fetchone()
    resumed = client.get(success.headers["location"])
    assert resumed.status_code == 200 and "@joemama" in resumed.text
    data["csrf"] = csrf
    assert client.post("/account/shares/new", data=data).status_code == 200


def test_publication_reauth_rebuilds_preview_for_new_session(sharing):
    import re

    from varyz.cloud.store import digest

    app, store, service = sharing
    client = browser(app, store, "egr")
    original = client.cookies.get("__Host-session")
    receipt = service.preview(
        "egr",
        digest(original),
        models.Preview(
            project="website",
            label="Public website",
            selection=[models.SourceRef(kind="rule", id="selected", version=1)],
        ).model_dump(),
    )
    hint = expire_browser(store, client)
    data = {
        "csrf": "test-browser-csrf",
        "preview_id": receipt["preview_id"],
        "sharing_owner": hint,
        "source_fingerprint": receipt["source_fingerprint"],
        "reviewed": "yes",
        "invited_email": "joemama@example.test",
        "idempotency_key": identifier(),
    }
    assert (
        client.post("/account/shares/new/publish", data=data, follow_redirects=False).status_code
        == 303
    )
    fresh_browser(store, client)
    resumed = client.get("/account/sharing/resume")
    assert resumed.status_code == 200 and "Keep the page calm." in resumed.text
    new_preview = re.search(r'name="preview_id" value="([^"]+)"', resumed.text).group(1)
    assert new_preview != receipt["preview_id"]
    with store.connection() as db:
        row = db.execute(
            "SELECT session FROM shared_previews WHERE id=?", (new_preview,)
        ).fetchone()
        assert row["session"] == digest(client.cookies.get("__Host-session"))
        assert not db.execute("SELECT 1 FROM shared_projects").fetchone()
    data["preview_id"] = new_preview
    data["source_fingerprint"] = re.search(
        r'name="source_fingerprint" value="([^"]+)"', resumed.text
    ).group(1)
    assert client.post("/account/shares/new/publish", data=data).status_code == 200


def test_reauth_wrong_account_never_discloses_or_consumes_draft(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    hint = expire_browser(store, client)
    client.post(
        "/account/shares/new",
        data=invitation_draft(recipient="private@example.test", sharing_owner=hint),
        follow_redirects=False,
    )
    receipt = client.cookies.get("__Host-sharing_draft")
    fresh_browser(store, client, "another_owner")
    wrong = client.get("/account/sharing/resume")
    assert wrong.status_code == 403 and "Use the same account" in wrong.text
    assert "private@example.test" not in wrong.text
    assert client.cookies.get("__Host-sharing_draft") == receipt
    fresh_browser(store, client)
    assert "private@example.test" in client.get("/account/sharing/resume").text


def test_reauth_expired_draft_is_readable_cleared_and_never_sent(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    hint = expire_browser(store, client)
    client.post(
        "/account/shares/new", data=invitation_draft(sharing_owner=hint), follow_redirects=False
    )
    fresh_browser(store, client)
    with store.connection() as db:
        db.execute("UPDATE oauth_flows SET expires=?", (time.time() - 1,))
    response = client.get("/account/sharing/resume")
    assert response.status_code == 410 and "Saved action expired" in response.text
    assert "text/html" in response.headers["content-type"]
    assert not client.cookies.get("__Host-sharing_draft")
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM shared_invites").fetchone()


def test_reauth_repeat_without_login_keeps_original_draft(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    hint = expire_browser(store, client)
    client.post(
        "/account/shares/new", data=invitation_draft(sharing_owner=hint), follow_redirects=False
    )
    receipt = client.cookies.get("__Host-sharing_draft")
    again = client.get("/account/sharing/resume", follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/login?reauth=1"
    assert client.cookies.get("__Host-sharing_draft") == receipt
    fresh_browser(store, client)
    assert "@joemama" in client.get("/account/sharing/resume").text


def test_sharing_csrf_and_api_auth_stay_closed(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    stale_browser(store, client)
    bad = client.post("/account/shares/new", data=invitation_draft(csrf="wrong"))
    assert bad.status_code == 403 and "text/html" in bad.headers["content-type"]
    assert not client.cookies.get("__Host-sharing_draft")
    api = client.post(
        "/api/sharing/v1/shares/" + identifier() + "/invites",
        json={"invited_email": "joemama@example.test"},
        headers={"X-CSRF-Token": "test-browser-csrf", "Idempotency-Key": identifier()},
    )
    assert api.status_code == 404 and "application/json" in api.headers["content-type"]
    assert "Sign in again" not in api.text
    stale_browser(store, client, expired=True)
    api = client.post(
        "/api/sharing/v1/shares/" + identifier() + "/invites",
        json={"invited_email": "joemama@example.test"},
        headers={"X-CSRF-Token": "test-browser-csrf", "Idempotency-Key": identifier()},
    )
    assert api.status_code == 401 and "application/json" in api.headers["content-type"]


def test_access_removal_reauth_waits_for_explicit_confirmation(sharing):
    app, store, service = sharing
    member = accept(service, create(service))
    client = browser(app, store, "egr")
    hint = expire_browser(store, client)
    data = {
        "csrf": "test-browser-csrf",
        "operation": "revoke",
        "membership_id": member,
        "sharing_owner": hint,
        "confirm": "on",
        "expected_revision": "1",
        "idempotency_key": identifier(),
    }
    response = client.post("/account/sharing/action", data=data, follow_redirects=False)
    assert response.status_code == 303
    fresh_browser(store, client)
    resumed = client.get("/account/sharing/resume")
    assert resumed.status_code == 200 and "Remove access" in resumed.text
    with store.connection() as db:
        assert (
            db.execute("SELECT state FROM shared_memberships WHERE id=?", (member,)).fetchone()[
                "state"
            ]
            == "active"
        )
    assert (
        client.post("/account/sharing/action", data=data, follow_redirects=False).status_code == 303
    )
    with store.connection() as db:
        assert (
            db.execute("SELECT state FROM shared_memberships WHERE id=?", (member,)).fetchone()[
                "state"
            ]
            != "active"
        )


def test_anonymous_sharing_get_returns_after_login_without_external_redirect(sharing):
    app, store, service = sharing
    client = browser(app, store, "egr")
    client.cookies.delete("__Host-session", domain="cloud.example.test")
    response = client.get(
        "/account/shares/new?project=website&next=https://evil.test", follow_redirects=False
    )
    assert response.status_code == 303
    fresh_browser(store, client)
    resumed = client.get("/account/sharing/resume", follow_redirects=False)
    assert resumed.status_code == 303
    assert resumed.headers["location"].startswith("/account/shares/new?")


def test_http_mcp_scope_firewall_free_guest_and_csrf(sharing):
    app, store, service = sharing
    member = accept(service, create(service))
    owner_browser = browser(app, store, "egr")
    guest_browser = browser(app, store, "joemama")
    assert guest_browser.get("/account/shared").status_code == 200
    assert guest_browser.get("/account/shared/" + member).status_code == 200
    guest_access = {
        "Authorization": "Bearer "
        + store.credential("joemama", "access", 3600, "native-host", scope=SCOPE)
    }
    own_access = {
        "Authorization": "Bearer " + store.credential("egr", "access", 3600, "native-host")
    }
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = guest_browser.post("/mcp/shared", json=message, headers=guest_access)
    assert response.status_code == 200
    assert {t["name"] for t in response.json()["result"]["tools"]} == set(models.TOOLS)
    assert owner_browser.post("/mcp/shared", json=message, headers=own_access).status_code == 403
    for path in ("/mcp", "/mcp/plugin", "/mcp/context", "/mcp/archive"):
        assert guest_browser.post(path, json=message, headers=guest_access).status_code == 403
    assert guest_browser.get("/api/export", headers=guest_access).status_code == 403
    tool = {
        **message,
        "method": "tools/call",
        "params": {"name": "remember_owner_rule", "arguments": {}},
    }
    assert guest_browser.post("/mcp/shared", json=tool, headers=guest_access).json()["result"][
        "isError"
    ]
    tool["params"] = {
        "name": "shared_brief",
        "arguments": {
            "membership_id": member,
            "conversation_id": "native-task",
            "expected_binding_revision": 0,
        },
    }
    response = guest_browser.post("/mcp/shared", json=tool, headers=guest_access)
    assert response.status_code == 200 and "items" in response.json()["result"]["structuredContent"]
    assert owner_browser.post("/api/sharing/v1/shares/preview", json={}).status_code == 403
    assert owner_browser.get("/account/shares/new?project=website").status_code == 200
    assert "/mcp/shared" in guest_browser.get("/account/shared/" + member).text


def test_encryption_erasure_and_retention(sharing):
    _, store, service = sharing
    result = create(service)
    binding = bound(service, accept(service, result))
    ask(service, binding)
    with store.connection() as db:
        for table in (
            "shared_publications",
            "shared_previews",
            "shared_threads",
            "shared_idempotency",
        ):
            rows = db.execute(f"SELECT payload FROM {table}").fetchall()
            assert all(r["payload"].startswith("encrypted:") for r in rows)
        assert "joemama@" not in db.execute("SELECT email FROM shared_invites").fetchone()["email"]
    service.lifecycle(
        "egr",
        result["share_id"],
        {"status": "deleted", "expected_publication_revision": 1},
        identifier(),
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_threads").fetchone()["n"] == 0
        assert db.execute("SELECT COUNT(*) AS n FROM shared_publications").fetchone()["n"] == 0
    denied(404, lambda: ask(service, binding))


def test_disabled_by_default_and_requires_encryption(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "disabled.sqlite")))
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    assert client.post("/mcp/shared", json={}).status_code == 503
    with pytest.raises(ValueError, match="encryption key"):
        create_app(Settings(database=str(tmp_path / "no-key.sqlite"), shared_varyz_enabled=True))


def test_pagination_is_actor_publication_and_task_bound(sharing):
    _, store, service = sharing
    sources = []
    for i in range(25):
        record_id = "page-" + str(i)
        store.push(
            "egr",
            [
                {
                    "kind": "rule",
                    "id": record_id,
                    "base_version": 0,
                    "payload": {"statement": "Synthetic preference " + str(i), "scope": "website"},
                }
            ],
        )
        sources.append(models.SourceRef(kind="rule", id=record_id, version=1))
    p = preview(service, sources)
    share = service.create(
        "egr",
        "session-owner",
        models.Publish(
            preview_id=p["preview_id"],
            source_fingerprint=p["source_fingerprint"],
            invited_email="joemama@example.test",
        ).model_dump(),
        identifier(),
    )
    member = accept(service, share)
    first = service.brief(
        "joemama",
        "native",
        models.Brief(
            membership_id=member, conversation_id="page-task", expected_binding_revision=0
        ).model_dump(),
    )
    assert len(first["items"]) == 20 and first["truncated"] and first["next_cursor"]
    with store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM shared_connection_checks WHERE membership=?", (member,)
        ).fetchone()
    last = service.brief(
        "joemama",
        "native",
        models.Brief(
            membership_id=member, conversation_id="page-task", cursor=first["next_cursor"]
        ).model_dump(),
    )
    assert len(last["items"]) == 5 and not last["truncated"]
    with store.connection() as db:
        assert db.execute(
            "SELECT 1 FROM shared_connection_checks WHERE membership=?", (member,)
        ).fetchone()
    denied(
        404,
        lambda: service.brief(
            "outsider",
            "native",
            models.Brief(
                membership_id=member, conversation_id="page-task", cursor=first["next_cursor"]
            ).model_dump(),
        ),
    )
    store.push("egr", [{"kind": "rule", "id": sources[0].id, "base_version": 1, "payload": None}])
    # Suppression happens in the sync transaction, without waiting for a guest read.
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT revision FROM shared_projects WHERE id=?", (share["share_id"],)
            ).fetchone()["revision"]
            == 2
        )
    denied(
        409,
        lambda: service.brief(
            "joemama",
            "native",
            models.Brief(
                membership_id=member, conversation_id="page-task", cursor=first["next_cursor"]
            ).model_dump(),
        ),
    )


def test_decline_withdraw_secret_scan_preview_actor_and_schema(sharing):
    app, store, service = sharing
    p = preview(service)
    denied(
        409,
        lambda: service.create(
            "egr",
            "wrong-session",
            models.Publish(
                preview_id=p["preview_id"],
                source_fingerprint=p["source_fingerprint"],
                invited_email="joemama@example.test",
            ).model_dump(),
            identifier(),
        ),
    )
    share = create(service)
    binding = bound(service, accept(service, share))
    queued = ask(service, binding)
    service.reply(
        "egr",
        queued["question_id"],
        models.Decline(
            expected_thread_version=1, explanation="Please ask a more specific question."
        ).model_dump(),
        identifier(),
        declined=True,
    )
    status = service.question_status(
        "joemama",
        "native-guest",
        models.QuestionStatus(**binding, thread_ids=[queued["question_id"]]).model_dump(),
    )
    assert status["questions"][0]["state"] == "declined"
    q2 = ask(service, binding, "Use dark orange?")
    service.withdraw(
        "joemama",
        binding["membership_id"],
        q2["question_id"],
        {"expected_thread_version": 1},
        identifier(),
    )
    denied(
        409,
        lambda: service.reply(
            "egr",
            q2["question_id"],
            models.Reply(expected_thread_version=1, answer="No").model_dump(),
            identifier(),
        ),
    )
    secret = "ghp_" + "a" * 36
    denied(422, lambda: ask(service, binding, "Use " + secret + "?"))
    client = browser(app, store, "joemama")
    token = store.credential("joemama", "access", 600, "native", scope=SCOPE)
    data = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "shared_brief",
            "arguments": {
                "membership_id": binding["membership_id"],
                "conversation_id": "test",
                "expected_binding_revision": 0,
                "owner_id": "egr",
            },
        },
    }
    result = client.post(
        "/mcp/shared", headers={"Authorization": "Bearer " + token}, json=data
    ).json()
    assert result["result"]["isError"]


def test_browser_acceptance_no_referrer_token_continuation(sharing):
    app, store, service = sharing
    share = create(service)
    guest = browser(app, store, "joemama")
    initial = guest.get(share["invite_url"], follow_redirects=False)
    assert initial.status_code == 303 and initial.headers["referrer-policy"] == "no-referrer"
    receipt_url = initial.headers["location"]
    token = share["invite_url"].rsplit("/", 1)[-1]
    page = guest.get(receipt_url)
    assert page.status_code == 200 and token not in page.text
    assert page.headers["referrer-policy"] == "same-origin"
    assert browser(app, store, "outsider").get(receipt_url).status_code == 404
    response = guest.post(
        receipt_url + "/accept",
        headers={"Origin": "https://cloud.example.test"},
        data={"csrf": guest.cookies.get("__Host-csrf"), "idempotency_key": identifier()},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "/account/shared/" in response.headers["location"]


def test_operator_pilot_allowlist_does_not_change_personal_access(sharing):
    app, store, service = sharing
    app.state.settings.shared_varyz_accounts = ("egr", "joemama")
    member = accept(service, create(service))
    assert bound(service, member)
    denied(503, lambda: service.projects("outsider", {"cursor": None}))
    outsider = browser(app, store, "outsider")
    assert outsider.get("/").status_code == 200


def test_setup_requires_current_complete_brief_and_live_shared_grant(sharing):
    app, store, service = sharing
    member = accept(service, create(service))
    guest = browser(app, store, "joemama")
    url = "/api/sharing/v1/memberships/" + member + "/connection"
    assert guest.get(url).json()["stage"] == "connect"
    store.credential("joemama", "access", 600, "personal", scope="varyz")
    assert guest.get(url).json()["stage"] == "connect"
    token = store.credential("joemama", "access", 600, "native", scope=SCOPE)
    assert guest.get(url).json()["stage"] == "verify"
    page = guest.get("/account/shared/" + member + "?ready=true")
    assert "Project added" in page.text
    assert "Your AI connection is saved" in page.text
    assert "Connect ChatGPT" not in page.text
    assert "I’ve connected" not in page.text and "Check setup" not in page.text
    assert "Your questions" not in page.text
    result = guest.post(
        "/mcp/shared",
        headers={"Authorization": "Bearer " + token},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "shared_brief",
                "arguments": {
                    "membership_id": member,
                    "conversation_id": "setup-test",
                    "expected_binding_revision": 0,
                },
            },
        },
    ).json()
    assert not result["result"].get("isError")
    assert guest.get(url).json()["stage"] == "ready"
    assert browser(app, store, "outsider").get(url).status_code == 404
    # A published revision invalidates readiness until the new brief is read.
    with store.connection() as db:
        db.execute(
            "UPDATE shared_projects SET revision=revision+1 WHERE id=("
            "SELECT share FROM shared_memberships WHERE id=?)",
            (member,),
        )
    assert guest.get(url).json()["stage"] == "verify"
    with store.connection() as db:
        db.execute("UPDATE credentials SET expires=0 WHERE account='joemama' AND kind='access'")
    assert guest.get(url).json()["stage"] == "connect"


def leveled(service, level, email="joemama@example.test", fill_gaps=None):
    receipt = preview(service)
    return service.create(
        "egr",
        "session-owner",
        models.Publish(
            preview_id=receipt["preview_id"],
            invited_email=email,
            source_fingerprint=receipt["source_fingerprint"],
            level=level,
            fill_gaps=fill_gaps,
        ).model_dump(),
        identifier(),
    )


def invite(service, share_id, level, email="outsider@example.test"):
    data = models.Invite(invited_email=email, level=level).model_dump()
    return {**service.invite("egr", share_id, data, identifier()), "share_id": share_id}


def collaborate(service, **fields):
    return service.collaborate("egr", models.Collaboration(**fields).model_dump())


def finish(service, binding, guest="joemama", **work):
    data = models.Finish(**binding, work=models.Work(**work) if work else None).model_dump()
    return service.finish(guest, "native-guest", data)


def github(service, binding, **checks):
    data = models.GitHub(**binding, **checks).model_dump()
    return service.github("joemama", "native-guest", data)


def rebrief(service, binding, guest="joemama"):
    data = models.Brief(
        membership_id=binding["membership_id"], conversation_id=binding["conversation_id"]
    ).model_dump()
    return service.brief(guest, "native-guest", data)


def events(store):
    with store.connection() as db:
        return {row["kind"] for row in db.execute("SELECT kind FROM shared_events").fetchall()}


def test_decide_partner_settles_guest_questions_and_owner_can_overrule(sharing):
    _, store, service = sharing
    share = leveled(service, "decide")
    partner = bound(service, accept(service, share))
    outsider = accept(service, invite(service, share["share_id"], "consult"), "outsider")
    guest = bound(service, outsider, guest="outsider", task="task-2")
    queued = ask(service, guest, guest="outsider")
    collaboration = rebrief(service, partner)["collaboration"]
    assert collaboration["level"] == "decide" and collaboration["fill_gaps"] is True
    assert [q["question_id"] for q in collaboration["to_decide"]] == [queued["question_id"]]
    assert "to_decide" not in rebrief(service, guest, "outsider")["collaboration"]
    sneaky = models.PartnerAnswer(**guest, question_id=queued["question_id"], answer="No")
    denied(403, lambda: service.partner_answer("outsider", "native-guest", sneaky.model_dump()))
    answer = models.PartnerAnswer(**partner, question_id=queued["question_id"], answer="Use teal.")
    result = service.partner_answer("joemama", "native-guest", answer.model_dump())
    assert result["authority"] == "partner_decision" and result["permission_grant"] is False
    denied(409, lambda: service.partner_answer("joemama", "native-guest", answer.model_dump()))
    status = service.question_status(
        "outsider",
        "native-guest",
        models.QuestionStatus(**guest, thread_ids=[queued["question_id"]]).model_dump(),
    )
    assert status["questions"][0]["reply"]["decided_by"] == "joemama"
    again = ask(service, guest, guest="outsider")
    assert again["answer"] == "Use teal." and again["authority"] == "partner_decision"
    owner_pref = ask(service, guest, "Include pricing?", guest="outsider")
    assert owner_pref["authority"] == "owner_confirmed"
    review = collaborate(service, action="status")["shares"][0]["review"]
    assert review[0]["kind"] == "partner_decision" and review[0]["by"] == "joemama"
    collaborate(
        service,
        action="review",
        review_id=review[0]["review_id"],
        decision="overrule",
        answer="Use marigold.",
    )
    overruled = ask(service, guest, guest="outsider")
    assert overruled["answer"] == "Use marigold." and overruled["authority"] == "owner_confirmed"
    assert {"invited_decide", "answer_partner_decided", "partner_decision_overruled"} <= events(
        store
    )


def test_work_reports_provisional_gaps_and_disagreements_reach_owner_review(sharing):
    _, store, service = sharing
    consult = bound(service, accept(service, create(service)))
    gap = {"question": "Button radius?", "answer": "8px"}
    denied(403, lambda: finish(service, consult, summary="Hero", provisional=[gap]))
    share = leveled(service, "contribute", "outsider@example.test")
    guest = bound(service, accept(service, share, "outsider"), guest="outsider", task="task-2")
    work = dict(
        summary="Built the pricing section.",
        branch="joe/pricing",
        commit="abc1234",
        run="npm run dev",
        provisional=[gap],
        disagreements=[{"preference": "Keep the page calm.", "suggestion": "Add one bold CTA."}],
    )
    assert finish(service, guest, "outsider", **work)["report"]["for_owner_review"] == 2
    assert finish(service, guest, "outsider", **work)["report"]["duplicate"] is True
    digest = service.owner_digest("egr", "website")
    assert [s["share_id"] for s in digest["shares"]] == [share["share_id"]]
    assert digest["shares"][0]["new_reports"] == 1 and digest["shares"][0]["to_review"] == 2
    shares = collaborate(service, action="status")["shares"]
    detail = [s for s in shares if s["share_id"] == share["share_id"]][0]
    assert detail["reports"][0]["branch"] == "joe/pricing" and detail["reports"][0]["new"]
    assert service.owner_digest("egr", "website") is not None  # review items still wait
    kinds = {item["kind"]: item["review_id"] for item in detail["review"]}
    denied(
        422,
        lambda: collaborate(
            service, action="review", review_id=kinds["provisional"], decision="adopt"
        ),
    )
    collaborate(service, action="review", review_id=kinds["provisional"], decision="keep")
    adopted = collaborate(
        service, action="review", review_id=kinds["disagreement"], decision="adopt"
    )
    assert "remember_owner_rule" in adopted["next"]
    assert service.owner_digest("egr", "website") is None
    collaborate(
        service,
        action="report",
        share_id=share["share_id"],
        work={"summary": "Merged pricing.", "branch": "main"},
    )
    first = rebrief(service, guest, "outsider")["collaboration"]["reports"][0]
    assert first["summary"] == "Merged pricing." and first["new"] is True
    assert rebrief(service, guest, "outsider")["collaboration"]["reports"][0]["new"] is False
    assert {
        "report_posted_guest",
        "report_posted_owner",
        "answer_guest_varyz",
        "disagreement_raised",
        "provisional_kept",
        "disagreement_adopted",
        "report_picked_up",
    } <= events(store)


def test_github_handshake_routes_access_through_both_agents(sharing):
    _, store, service = sharing
    share = create(service)
    binding = bound(service, accept(service, share))
    tools = dict(git_installed=True, gh_installed=True)
    assert "not linked a repository" in github(service, binding, **tools)["next_step"]
    collaborate(
        service,
        action="link_repo",
        share_id=share["share_id"],
        repo="https://github.com/egr/site.git",
    )
    missing = github(service, binding, git_installed=False, gh_installed=False)
    assert "winget install --id Git.Git" in missing["next_step"]
    assert "gh auth login" in github(service, binding, **tools)["next_step"]
    requested = github(service, binding, **tools, gh_signed_in=True, github_username="joe-gh")
    assert requested["state"] == "requested" and "owner's agent adds you" in requested["next_step"]
    access = collaborate(service, action="status")["shares"][0]["access_requests"]
    assert access[0]["command"] == (
        "gh api -X PUT repos/egr/site/collaborators/joe-gh -f permission=push"
    )
    collaborate(service, action="access_granted", membership_id=access[0]["membership_id"])
    denied(
        409,
        lambda: collaborate(
            service, action="access_granted", membership_id=access[0]["membership_id"]
        ),
    )
    step = rebrief(service, binding)["collaboration"]["github"]["next_step"]
    assert "repository_invitations" in step
    ready = github(service, binding, **tools, gh_signed_in=True, has_access=True, cloned=True)
    assert ready["state"] == "accepted" and "git worktree add" in ready["next_step"]
    assert {
        "guest_git_missing_git",
        "guest_access_requested",
        "guest_access_granted",
        "guest_access_accepted",
        "guest_first_clone",
    } <= events(store)
    assert service.metrics()["github_stall_rate"] == 0.0


def test_away_owner_questions_route_to_decide_partner(sharing):
    _, _, service = sharing
    share = create(service)
    member = accept(service, share)
    route = dict(share_id=share["share_id"], question="Which hosting?", reason="Owner is away")
    denied(409, lambda: collaborate(service, action="ask_partner", **route))
    collaborate(service, action="set_level", membership_id=member, level="decide")
    routed = collaborate(service, action="ask_partner", **route)
    assert routed["partners"] == ["joemama"]
    assert (
        collaborate(service, action="ask_partner", **route)["question_id"]
        == (routed["question_id"])
    )
    binding = bound(service, member)
    asked = rebrief(service, binding)["collaboration"]["to_decide"][0]
    assert asked["question_id"] == routed["question_id"] and "away" in asked["asked_by"]
    answer = models.PartnerAnswer(
        **binding, question_id=routed["question_id"], answer="Cloudflare Pages."
    )
    service.partner_answer("joemama", "native-guest", answer.model_dump())
    detail = collaborate(service, action="status")["shares"][0]
    assert detail["routed_to_partners"] == 0
    assert detail["review"][0]["while_away"] is True


def test_owner_collaboration_surfaces_and_share_page(sharing):
    app, store, service = sharing
    share = create(service)
    accept(service, share)
    own_access = {
        "Authorization": "Bearer " + store.credential("egr", "access", 3600, "native-host")
    }
    client = browser(app, store, "egr")
    listed = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=own_access
    ).json()["result"]["tools"]
    assert "collaboration" in {tool["name"] for tool in listed}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "collaboration", "arguments": {"action": "status"}},
    }
    result = client.post("/mcp", json=call, headers=own_access).json()["result"]
    assert not result.get("isError")
    assert share["share_id"] in result["content"][0]["text"]
    rest = client.post(
        "/api/sharing/v1/owner/collaboration", json={"action": "digest"}, headers=own_access
    )
    assert rest.status_code == 200 and rest.json() == {"shares": []}
    page = client.get("/account/shares/" + share["share_id"]).text
    assert "Link repository" in page and 'name="level"' in page


def test_local_collaboration_digest_is_quiet_and_cached(tmp_path, monkeypatch):
    from varyz.cloud import collaboration

    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    assert not collaboration.connected(config) and collaboration.digest(config, "site") is None
    (tmp_path / "config.cloud-sync.json").write_text(
        json.dumps({"url": "https://cloud.example.test"}), encoding="utf-8"
    )
    assert collaboration.connected(config)
    calls = []

    def unreachable(*args, **kwargs):
        calls.append(args)
        raise collaboration.CloudError("Cloud could not be reached")

    monkeypatch.setattr(collaboration, "call", unreachable)
    assert collaboration.digest(config, "site") is None
    assert collaboration.digest(config, "site") is None and len(calls) == 1
    monkeypatch.setattr(collaboration, "call", lambda *a, **k: {"shares": [{"share_id": "s"}]})
    assert collaboration.digest(config, "other")["shares"][0]["share_id"] == "s"


def test_owner_agent_adds_people_by_username_and_they_join_from_their_agent(sharing):
    app, store, service = sharing
    added = collaborate(
        service, action="add", username="@JoeMama", project="Website", level="decide"
    )
    assert added["status"] == "invited" and added["username"] == "joemama"
    # Project preferences are shared; General ones and private rationale never are.
    assert added["shared_preferences"] == 3 and added["level"] == "decide"
    listed = service.projects("joemama", {})
    assert listed["memberships"] == []
    assert [(i["invite_id"], i["owner_label"], i["level"]) for i in listed["invitations"]] == [
        (added["invite_id"], "egr", "decide")
    ]
    assert service.projects("outsider", {})["invitations"] == []
    denied(404, lambda: service.join("outsider", {"invite_id": added["invite_id"]}))
    member = service.join("joemama", {"invite_id": added["invite_id"]})["membership_id"]
    assert service.join("joemama", {"invite_id": added["invite_id"]})["membership_id"] == member
    assert service.projects("joemama", {})["invitations"] == []
    binding = bound(service, member)
    brief = rebrief(service, binding)
    text = json.dumps(brief)
    assert len(brief["items"]) == 3 and brief["collaboration"]["level"] == "decide"
    assert "PRIVATE_GENERAL" not in text and "PRIVATE_REASON" not in text
    changed = collaborate(
        service, action="add", username="joemama", project="website", level="consult"
    )
    assert changed["status"] == "level_changed" and changed["share_id"] == added["share_id"]
    denied(404, lambda: collaborate(service, action="add", username="nobody", project="website"))
    denied(422, lambda: collaborate(service, action="add", username="egr", project="website"))
    denied(422, lambda: collaborate(service, action="add", username="joemama", project="*"))
    own_access = {
        "Authorization": "Bearer " + store.credential("egr", "access", 3600, "native-host")
    }
    rest = browser(app, store, "egr").post(
        "/api/sharing/v1/owner/collaboration",
        json={"action": "add", "username": "outsider", "project": "website"},
        headers=own_access,
    )
    assert rest.status_code == 200 and rest.json()["level"] == "consult"
    status = collaborate(service, action="status")["shares"]
    assert len(status) == 1
    assert [(m["name"], m["level"]) for m in status[0]["members"]] == [("joemama", "consult")]
    assert [i["name"] for i in status[0]["invited"]] == ["outsider"]
    assert collaborate(service, action="remove", username="outsider", project="website")
    assert collaborate(service, action="status")["shares"][0]["invited"] == []
    denied(410, lambda: service.join("outsider", {"invite_id": rest.json()["invite_id"]}))
    collaborate(service, action="remove", username="joemama", project="website")
    denied(404, lambda: rebrief(service, binding))
    denied(
        404, lambda: collaborate(service, action="remove", username="joemama", project="website")
    )
    assert {"shared_by_agent", "invited_by_username", "revoked"} <= events(store)


def test_share_cli_sends_username_level_and_this_folders_project(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from varyz.cloud import collaboration
    from varyz.cloud.cli import app as cloud_app

    calls = []
    monkeypatch.setattr(
        collaboration, "call", lambda config, arguments: calls.append(arguments) or {"ok": 1}
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cloud_app, ["share", "add", "egr", "--level", "decide"]).exit_code == 0
    assert runner.invoke(cloud_app, ["share", "status"]).exit_code == 0
    assert runner.invoke(cloud_app, ["share", "add", "egr", "-l", "owner"]).exit_code == 1
    assert calls == [
        {"action": "add", "username": "egr", "level": "decide", "project": tmp_path.name},
        {"action": "status"},
    ]


def test_local_collaboration_tool_matches_the_cloud_contract():
    from typing import get_args

    from varyz.cloud import collaboration

    schema = collaboration.TOOL["inputSchema"]
    assert set(schema["properties"]) == set(models.Collaboration.model_fields)
    actions = get_args(models.Collaboration.model_fields["action"].annotation)
    assert set(collaboration.ACTIONS) | {"digest"} == set(actions)


def settle(store, seconds=120):
    with store.connection() as db:
        db.execute("UPDATE shared_notices SET created=created-?", (seconds,))


def test_sharing_notifications_for_existing_accounts(sharing):
    from varyz.cloud import sharing_notify as notices

    app, store, service = sharing
    sent = []
    service.settings.email_sender = sent.append
    denied(422, lambda: create(service, "egr@example.test"))

    created = create(service)
    assert created["invitation_sent"] is True
    seen = notices.desktop(service, "joemama")["notices"]
    assert [n["text"] for n in seen] == ["egr invited you to Public website at Consult"]
    assert seen[0]["url"] == "https://cloud.example.test/account/shared"
    # Joining from the Shared with me page needs no link.
    guest_browser = browser(app, store, "joemama")
    page = guest_browser.get("/account/shared").text
    assert "egr invited you to Public website" in page and "/account/shared/join/" in page
    invite_id = page.split("/account/shared/join/", 1)[1].split('"', 1)[0]
    joined = guest_browser.post(
        "/account/shared/join/" + invite_id,
        data={"csrf": "test-browser-csrf"},
        follow_redirects=False,
    )
    assert joined.status_code == 303
    member = joined.headers["location"].rsplit("/", 1)[-1]
    binding = bound(service, member)
    queued = ask(service, binding, question="PRIVATE_QUESTION orange?")
    assert [n["kind"] for n in notices.desktop(service, "egr")["notices"]] == ["question"]
    reply = models.Reply(expected_thread_version=1, answer="Use marigold.").model_dump()
    service.reply("egr", queued["question_id"], reply, identifier())
    assert [n["text"] for n in notices.desktop(service, "joemama")["notices"]][-1] == (
        "egr answered your question about Public website"
    )

    # Pending notices reach each person without including private content.
    assert service.deliver_notices() == 2
    by = {m["To"]: m for m in sent}
    owner, guest = by["egr@example.test"], by["joemama@example.test"]
    assert owner["Subject"] == "joemama has a question about Public website"
    assert "PRIVATE_QUESTION" not in owner.as_string()
    assert owner["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/notices/email?" in guest.get_content()

    # A second question can reach the same recipient immediately.
    ask(service, binding, question="Use teal?")
    assert service.deliver_notices() == 1

    # One click stops them; a forged link does nothing.
    off = urlparse(guest["List-Unsubscribe"].strip("<>"))
    client = TestClient(app, base_url="https://cloud.example.test")
    assert client.get("/notices/email?a=joemama&t=forged").status_code == 404
    assert "Stop these emails" in client.get(off.path + "?" + off.query).text
    response = client.post(off.path + "?" + off.query, content="List-Unsubscribe=One-Click")
    assert response.status_code == 200
    assert notices.desktop(service, "joemama")["email"] is False
    sent.clear()
    with store.connection() as db:
        share = db.execute("SELECT id FROM shared_projects").fetchone()["id"]
        notices.add(db, "joemama", share, "answer", "egr")
        db.execute("UPDATE shared_notice_settings SET last_email=NULL")
    settle(store)
    assert service.deliver_notices() == 0 and not sent


def test_sharing_email_delivery_has_no_time_period_quota(sharing, monkeypatch):
    from varyz.cloud import sharing_notify as notices

    app, store, service = sharing
    sent = []
    service.settings.email_sender = sent.append
    service.settings.email_daily_limit = 0
    monkeypatch.setattr(store, "allow_email", lambda *args: False)
    created = create(service)
    assert service.deliver_notices() == 1
    for _ in range(30):
        with store.connection() as db:
            notices.add(db, "joemama", created["share_id"], "answer", "egr")
        assert service.deliver_notices() == 1
    for _ in range(5):
        assert notices.test_email(service, "joemama") == {"email": "sent"}
    service.invite_project(
        "egr",
        {"project": "website", "recipient": "new@example.test", "level": "decide"},
        identifier(),
    )
    assert service.deliver_notices() == 1
    assert sent[-1]["To"] == "new@example.test"
    assert service.deliver_notices() == 0
    assert all("at most" not in message.get_content() for message in sent)


def test_desktop_notices_api_lists_marks_seen_and_switches_email(sharing):
    app, store, service = sharing
    create(service)
    client = TestClient(app, base_url="https://cloud.example.test")
    access = {"Authorization": "Bearer " + store.credential("joemama", "access", 3600, "x")}
    listed = client.get("/api/sharing/v1/notices", headers=access).json()
    assert listed["email"] is True and len(listed["notices"]) == 1
    ids = [n["id"] for n in listed["notices"]]
    assert (
        client.post("/api/sharing/v1/notices/seen", json={"ids": ids}, headers=access).status_code
        == 200
    )
    assert client.get("/api/sharing/v1/notices", headers=access).json()["notices"] == []
    assert client.post(
        "/api/sharing/v1/notices/settings", json={"email": False}, headers=access
    ).json() == {"email": False}
    assert (
        client.post(
            "/api/sharing/v1/notices/settings", json={"email": "no"}, headers=access
        ).status_code
        == 422
    )
    assert client.post("/api/sharing/v1/notices/test", headers=access).json() == {
        "email": "not_configured"
    }
    assert client.get("/api/sharing/v1/notices").status_code == 401


def test_desktop_check_shows_notices_without_a_window_and_marks_them(tmp_path, monkeypatch):
    from varyz import desktop_notify

    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    shown, posted = [], []

    class Cloud:
        def request(self, method, path, **kwargs):
            if method == "GET":
                return {
                    "notices": [
                        {
                            "id": "n1",
                            "text": "egr invited you to Site",
                            "url": "https://cloud.example.test/account/shared",
                        }
                    ]
                }
            posted.append((path, kwargs["json"]))
            return {}

    def runner(command, env):
        shown.append((command, env))

    monkeypatch.setattr(desktop_notify.sys, "platform", "win32")
    # Never touch the real registry from a test.
    monkeypatch.setattr(desktop_notify, "register_app", lambda: desktop_notify.APP_ID)
    assert desktop_notify.check(config, runner=runner, client=Cloud()) == 1
    command, env = shown[0]
    assert command[0] == "powershell.exe" and "-EncodedCommand" in command
    # A Varyz toast: its own name and logo, on screen 10 seconds, then kept in the
    # notification centre without popping up again.
    assert env["VARYZ_NOTICE_APP"] == "VaryzAI.Varyz"
    assert env["VARYZ_NOTICE_LOGO"].startswith("file:///")
    assert desktop_notify.LOGO.is_file()
    assert env["VARYZ_NOTICE_SECONDS"] == "10"
    assert 'duration="long"' in desktop_notify._TOAST
    assert "$q.SuppressPopup = $true" in desktop_notify._TOAST
    # Other people's text reaches PowerShell only as data, never as script source.
    assert env["VARYZ_NOTICE_BODY"] == "egr invited you to Site"
    assert "egr" not in " ".join(command)
    assert posted == [("/api/sharing/v1/notices/seen", {"ids": ["n1"]})]
    desktop_notify.set_enabled(config, False)
    assert desktop_notify.check(config, runner=runner, client=Cloud()) == 0
    desktop_notify.set_enabled(config, True)
    assert desktop_notify.enabled(config)


def test_shared_card_people_management_is_owner_only(sharing):
    app, store, service = sharing
    result = create(service)
    member = accept(service, result)
    owner = browser(app, store, "egr")
    path = "/account/shares/" + result["share_id"] + "/people"
    listing = owner.get("/account/shared")
    assert path in listing.text and "Manage people</a>" in listing.text
    page = owner.get(path)
    assert page.status_code == 200 and "joemama" in page.text
    assert "Change permissions" in page.text and "Remove access" in page.text
    assert "Invite someone else" in page.text
    assert "Review updates" not in page.text and "Erase collaboration" not in page.text
    for name in ("outsider", "joemama"):
        assert browser(app, store, name).get(path).status_code == 404
    assert member


@pytest.mark.parametrize("paused", [False, True])
def test_shared_card_leave_requires_confirmation_and_ends_only_own_access(sharing, paused):
    import re

    app, store, service = sharing
    result = create(service)
    member = accept(service, result)
    guest = browser(app, store, "joemama")
    stale_browser(store, guest)
    path = "/account/shared/" + member + "/leave"
    listing = guest.get("/account/shared?tab=with-me")
    assert path in listing.text and "Leave project</a>" in listing.text
    if paused:
        with store.connection() as db:
            db.execute("UPDATE shared_projects SET status='paused'")
    page = guest.get(path)
    assert page.status_code == 200 and "Leave Public website?" in page.text
    assert "Cancel</a>" in page.text and "Leave project</button>" in page.text
    with store.connection() as db:
        assert (
            db.execute("SELECT state FROM shared_memberships WHERE id=?", (member,)).fetchone()[
                "state"
            ]
            == "active"
        )
    data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page.text))
    denied_csrf = guest.post(
        "/account/sharing/action", data={**data, "csrf": "bad"}, follow_redirects=False
    )
    assert denied_csrf.status_code == 403
    missing = {k: v for k, v in data.items() if k != "confirm"}
    assert guest.post("/account/sharing/action", data=missing).status_code == 422
    outsider = browser(app, store, "outsider")
    assert outsider.get(path).status_code == 404
    assert (
        outsider.post(
            "/account/sharing/action", data={**data, "csrf": "test-browser-csrf"}
        ).status_code
        == 404
    )
    left = guest.post("/account/sharing/action", data=data, follow_redirects=False)
    assert left.status_code == 303 and left.headers["location"] == "/account/shared?tab=with-me"
    assert (
        guest.post("/account/sharing/action", data=data, follow_redirects=False).status_code == 303
    )
    assert guest.get(path).status_code == 404
    assert path not in guest.get("/account/shared?tab=with-me").text
    with store.connection() as db:
        assert (
            db.execute("SELECT state FROM shared_memberships WHERE id=?", (member,)).fetchone()[
                "state"
            ]
            == "left"
        )
        assert db.execute(
            "SELECT status FROM shared_projects WHERE id=?", (result["share_id"],)
        ).fetchone()["status"] == ("paused" if paused else "active")


def test_shared_overview_labels_preferences_and_prioritizes_people(sharing):
    app, store, service = sharing
    result = create(service)
    owner = browser(app, store, "egr")
    page = owner.get("/account/shares/" + result["share_id"])
    assert page.status_code == 200
    assert "Invitation sent. Once they join" in page.text
    assert page.text.index("People with access") < page.text.index("Shared preferences")
    assert '<details class="shared-disclosure shared-preferences">' in page.text
    assert "They are not a description of the project." in page.text
    assert "PRIVATE_UNSELECTED" not in page.text
    assert '<details class="shared-disclosure shared-settings"' in page.text
    assert "Published version" not in page.text
    assert page.text.count("Invite someone else</a>") == 1
    accept(service, result)
    page = owner.get("/account/shares/" + result["share_id"])
    assert "Manage who can use this project" in page.text
    assert "joemama" in page.text


def test_invitation_confirmation_is_on_project_and_consumed_once(sharing):
    app, store, service = sharing
    owner = browser(app, store, "egr")
    sent = owner.post("/account/shares/new", data=invitation_draft(), follow_redirects=False)
    assert sent.status_code == 303
    path = sent.headers["location"]
    assert path.startswith("/account/shares/")
    cookie = owner.cookies.get("__Host-sharing_sent")
    assert cookie and "joemama" not in cookie
    page = owner.get(path)
    assert 'class="shared-sent-dialog"' in page.text
    assert '<h1 tabindex="-1">website</h1>' in page.text
    assert "People with access" in page.text and "Shared preferences" in page.text
    assert not owner.cookies.get("__Host-sharing_sent")
    assert 'class="shared-sent-dialog"' not in owner.get(path).text
    for payload in (
        "tampered",
        service.seal(
            {"actor": "outsider", "share_id": path.rsplit("/", 1)[-1], "expires": time.time() + 120}
        ),
        service.seal(
            {"actor": "egr", "share_id": path.rsplit("/", 1)[-1], "expires": time.time() - 1}
        ),
    ):
        owner.cookies.set("__Host-sharing_sent", payload, domain="cloud.example.test")
        page = owner.get(path)
        assert page.status_code == 200 and 'class="shared-sent-dialog"' not in page.text
