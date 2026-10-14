"""Portable isolation/concurrency gate; disposable fixtures, no account impersonation."""

import json
import os
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from cryptography.fernet import Fernet

from varyz.cloud import sharing_models as models
from varyz.cloud.app import Settings
from varyz.cloud.archive import Archive
from varyz.cloud.sharing_service import Sharing, SharingError, identifier
from varyz.cloud.store import Store


def gate(store):
    service = Sharing(
        store,
        Archive(store, "https://fixture.test", encryption_key=Fernet.generate_key().decode()),
        Settings(shared_varyz_enabled=True, public_url="https://fixture.test"),
    )
    with store.connection() as db:
        for name, state in [("owner", "active"), ("guest", "inactive"), ("outsider", "inactive")]:
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified,username) "
                "VALUES(?,?,?, ?,1,?)",
                (name, name + "@example.test", "unused", state, name),
            )
    store.push(
        "owner",
        [
            dict(
                kind="rule",
                id="public-rule",
                base_version=0,
                payload={"statement": "Synthetic shared preference", "scope": "demo"},
            ),
            dict(
                kind="rule",
                id="private-rule",
                base_version=0,
                payload={"statement": "Unselected private fixture", "scope": "demo"},
            ),
        ],
    )
    preview = service.preview(
        "owner",
        "session",
        models.Preview(
            project="demo",
            label="Synthetic collaboration",
            selection=[models.SourceRef(kind="rule", id="public-rule", version=1)],
        ).model_dump(),
    )
    share = service.create(
        "owner",
        "session",
        models.Publish(
            preview_id=preview["preview_id"],
            invited_email="guest@example.test",
            source_fingerprint=preview["source_fingerprint"],
        ).model_dump(),
        identifier(),
    )
    token = share["invite_url"].rsplit("/", 1)[-1]
    try:
        service.accept("outsider", token, identifier())
        raise AssertionError("Wrong account accepted")
    except SharingError as error:
        assert error.status == 403
    barrier = Barrier(2)

    def accept(_):
        barrier.wait()
        return service.accept("guest", token, identifier())["membership_id"]

    with ThreadPoolExecutor(2) as pool:
        memberships = list(pool.map(accept, range(2)))
    assert memberships[0] == memberships[1]
    membership = memberships[0]
    brief = service.brief(
        "guest",
        "native",
        models.Brief(
            membership_id=membership, conversation_id="task", expected_binding_revision=0
        ).model_dump(),
    )
    assert len(brief["items"]) == 1 and "Unselected private" not in json.dumps(brief)
    bound = dict(
        membership_id=membership, conversation_id="task", binding_revision=brief["binding_revision"]
    )

    def ask(_):
        barrier.wait()
        return service.ask(
            "guest",
            "native",
            models.Ask(
                **bound,
                idempotency_key=identifier(),
                requests=[models.Question(question="Synthetic missing question?")],
            ).model_dump(),
        )["results"][0]["question_id"]

    with ThreadPoolExecutor(2) as pool:
        questions = list(pool.map(ask, range(2)))
    assert questions[0] == questions[1]
    reply = service.reply(
        "owner",
        questions[0],
        models.Reply(
            expected_thread_version=1,
            answer="Synthetic owner reply.",
            remember_project=True,
            publish_to_share=True,
            expected_publication_revision=1,
        ).model_dump(),
        identifier(),
    )
    assert reply["remembered_record_id"] and reply["published_revision"] == 2
    status = service.question_status(
        "guest", "native", models.QuestionStatus(**bound, thread_ids=[questions[0]]).model_dump()
    )
    assert (
        status["questions"][0]["reply"]["answer"] == "Synthetic owner reply."
        and status["brief_changed"]
    )
    store.push("owner", [dict(kind="rule", id="public-rule", base_version=1, payload=None)])
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT revision FROM shared_projects WHERE id=?", (share["share_id"],)
            ).fetchone()["revision"]
            == 3
        )
        assert (
            db.execute(
                "SELECT payload FROM shared_publications WHERE share=? AND revision=3",
                (share["share_id"],),
            )
            .fetchone()["payload"]
            .startswith("encrypted:")
        )
    service.end_member("owner", membership, {"expected_revision": 1}, identifier())
    try:
        service.question_status(
            "guest",
            "native",
            models.QuestionStatus(**bound, thread_ids=[questions[0]]).model_dump(),
        )
        raise AssertionError("Revoked guest read reply")
    except SharingError as error:
        assert error.status == 404
    service.lifecycle(
        "owner",
        share["share_id"],
        {"status": "deleted", "expected_publication_revision": 3},
        identifier(),
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shared_publications").fetchone()["n"] == 0
        assert db.execute("SELECT COUNT(*) AS n FROM shared_idempotency").fetchone()["n"] == 0
    service.purge()
    return {
        "private_isolation": True,
        "free_guest": True,
        "concurrent_accept": True,
        "concurrent_dedup": True,
        "remember_and_publish_atomic": True,
        "source_suppression_atomic": True,
        "revoke_denies_replies": True,
        "erasure": True,
    }


def collaboration(store):
    """Levels, partner answers, work reports, GitHub handshake and erasure on one backend."""
    service = Sharing(
        store,
        Archive(store, "https://fixture.test", encryption_key=Fernet.generate_key().decode()),
        Settings(shared_varyz_enabled=True, public_url="https://fixture.test"),
    )
    store.push(
        "owner",
        [
            dict(
                kind="rule",
                id="collab-rule",
                base_version=0,
                payload={"statement": "Synthetic calm layout", "scope": "collab"},
            )
        ],
    )
    preview = service.preview(
        "owner",
        "session",
        models.Preview(
            project="collab",
            label="Synthetic levels",
            selection=[models.SourceRef(kind="rule", id="collab-rule", version=1)],
        ).model_dump(),
    )
    share = service.create(
        "owner",
        "session",
        models.Publish(
            preview_id=preview["preview_id"],
            invited_email="guest@example.test",
            source_fingerprint=preview["source_fingerprint"],
            level="decide",
        ).model_dump(),
        identifier(),
    )
    share_id = share["share_id"]
    invite = service.invite(
        "owner",
        share_id,
        models.Invite(invited_email="outsider@example.test", level="consult").model_dump(),
        identifier(),
    )

    def join(actor, url, task):
        member = service.accept(actor, url.rsplit("/", 1)[-1], identifier())["membership_id"]
        brief = service.brief(
            actor,
            "native",
            models.Brief(
                membership_id=member, conversation_id=task, expected_binding_revision=0
            ).model_dump(),
        )
        return member, dict(
            membership_id=member, conversation_id=task, binding_revision=brief["binding_revision"]
        )

    _, partner = join("guest", share["invite_url"], "collab-partner")
    member, helper = join("outsider", invite["invite_url"], "collab-helper")
    question = service.ask(
        "outsider",
        "native",
        models.Ask(
            **helper,
            idempotency_key=identifier(),
            requests=[models.Question(question="Synthetic accent colour?")],
        ).model_dump(),
    )["results"][0]["question_id"]
    barrier = Barrier(2)

    def decide(_):
        barrier.wait()
        data = models.PartnerAnswer(**partner, question_id=question, answer="Synthetic teal.")
        try:
            return service.partner_answer("guest", "native", data.model_dump())["authority"]
        except SharingError as error:
            return error.status

    with ThreadPoolExecutor(2) as pool:
        outcomes = sorted(map(str, pool.map(decide, range(2))))
    assert outcomes == ["409", "partner_decision"], outcomes
    again = service.ask(
        "outsider",
        "native",
        models.Ask(
            **helper,
            idempotency_key=identifier(),
            requests=[models.Question(question="Synthetic accent colour?")],
        ).model_dump(),
    )["results"][0]
    assert again["authority"] == "partner_decision" and again["permission_grant"] is False
    service.collaborate(
        "owner",
        models.Collaboration(
            action="set_level", membership_id=member, level="contribute"
        ).model_dump(),
    )
    work = models.Work(
        summary="Synthetic section.",
        branch="probe/section",
        commit="abc1234",
        provisional=[{"question": "Synthetic radius?", "answer": "8px"}],
        disagreements=[{"preference": "Synthetic calm layout", "suggestion": "One bold CTA."}],
    )

    def report(_):
        barrier.wait()
        data = models.Finish(**helper, work=work).model_dump()
        return service.finish("outsider", "native", data)["report"]

    with ThreadPoolExecutor(2) as pool:
        reports = list(pool.map(report, range(2)))
    assert sorted(bool(r.get("duplicate")) for r in reports) == [False, True], reports
    digest = service.owner_digest("owner", "collab")
    assert digest and digest["shares"][0]["to_review"] == 3, digest
    service.collaborate(
        "owner",
        models.Collaboration(
            action="link_repo", share_id=share_id, repo="https://github.com/example/probe.git"
        ).model_dump(),
    )
    requested = service.github(
        "outsider",
        "native",
        models.GitHub(
            **helper,
            git_installed=True,
            gh_installed=True,
            gh_signed_in=True,
            github_username="probe-user",
        ).model_dump(),
    )
    assert requested["state"] == "requested"
    status = service.collaborate("owner", models.Collaboration(action="status").model_dump())
    detail = [s for s in status["shares"] if s["share_id"] == share_id][0]
    command = detail["access_requests"][0]["command"]
    assert command.endswith("collaborators/probe-user -f permission=push")
    service.collaborate(
        "owner",
        models.Collaboration(action="access_granted", membership_id=member).model_dump(),
    )
    for item in detail["review"]:
        decision = "keep" if item["kind"] != "disagreement" else "dismiss"
        service.collaborate(
            "owner",
            models.Collaboration(
                action="review", review_id=item["review_id"], decision=decision
            ).model_dump(),
        )
    assert service.owner_digest("owner", "collab") is None
    counts = service.metrics()
    assert isinstance(counts, dict) and "@" not in json.dumps(counts)
    with store.connection() as db:
        revision = db.execute(
            "SELECT revision FROM shared_projects WHERE id=?", (share_id,)
        ).fetchone()["revision"]
    service.lifecycle(
        "owner",
        share_id,
        {"status": "deleted", "expected_publication_revision": revision},
        identifier(),
    )
    with store.connection() as db:
        tables = ("shared_reports", "shared_suggestions", "shared_access", "shared_memberships")
        for table in tables:
            assert db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0, table
    return {
        "levels": True,
        "concurrent_partner_answer": True,
        "concurrent_report_dedup": True,
        "owner_review": True,
        "github_handshake": True,
        "collaboration_erasure": True,
    }


def by_username(store):
    """The owner's agent adds and removes people by username; they join from their agent."""
    service = Sharing(
        store,
        Archive(store, "https://fixture.test", encryption_key=Fernet.generate_key().decode()),
        Settings(shared_varyz_enabled=True, public_url="https://fixture.test"),
    )
    store.push(
        "owner",
        [
            dict(
                kind="rule",
                id="named-rule",
                base_version=0,
                payload={"statement": "Synthetic named rule", "scope": "named"},
            )
        ],
    )

    def collaborate(**fields):
        return service.collaborate("owner", models.Collaboration(**fields).model_dump())

    added = collaborate(action="add", username="@Guest", project="named", level="decide")
    assert added["status"] == "invited" and added["shared_preferences"] == 1, added
    pending = service.projects("guest", {})["invitations"]
    assert [i["invite_id"] for i in pending] == [added["invite_id"]], pending
    member = service.join("guest", {"invite_id": added["invite_id"]})["membership_id"]
    changed = collaborate(action="add", username="guest", project="named", level="consult")
    assert changed["status"] == "level_changed" and changed["membership_id"] == member
    collaborate(action="add", username="outsider", project="named")
    assert collaborate(action="remove", username="outsider", project="named")
    collaborate(action="remove", username="guest", project="named")
    status = collaborate(action="status", project="named")["shares"][0]
    assert status["members"] == [] and status["invited"] == [], status
    service.lifecycle(
        "owner",
        added["share_id"],
        {"status": "deleted", "expected_publication_revision": 1},
        identifier(),
    )
    return {"add_by_username": True, "join_from_agent": True, "remove_by_username": True}


def notices(store):
    """Existing accounts get notices; new recipients get a signup invitation."""
    from varyz.cloud import sharing_notify

    sent = []
    service = Sharing(
        store,
        Archive(store, "https://fixture.test", encryption_key=Fernet.generate_key().decode()),
        Settings(
            shared_varyz_enabled=True, public_url="https://fixture.test", email_sender=sent.append
        ),
    )
    added = service.collaborate(
        "owner",
        models.Collaboration(
            action="add", username="guest", project="named", level="contribute"
        ).model_dump(),
    )
    with service.transaction() as db:
        invited = service.invitation(db, "owner", added["share_id"], "nobody@example.test")
    found = sharing_notify.desktop(service, "guest")["notices"]
    assert [n["kind"] for n in found] == ["invitation"], found
    with store.connection() as db:
        db.execute("UPDATE shared_notices SET created=created-120")
    assert service.deliver_notices() == 2, sent
    messages = {m["To"]: m for m in sent}
    assert invited["invite_url"] in messages["nobody@example.test"].get_content()
    assert "List-Unsubscribe" in messages["guest@example.test"]
    sharing_notify.shown(service, "guest", [n["id"] for n in found])
    assert sharing_notify.desktop(service, "guest")["notices"] == []
    service.lifecycle(
        "owner",
        added["share_id"],
        {"status": "deleted", "expected_publication_revision": 1},
        identifier(),
    )
    return {"invite_without_account": True, "notice_email": True, "notice_desktop": True}


def main():
    dsn = os.environ.get("VARYZ_PROBE_POSTGRES")
    if dsn:
        import psycopg
        from psycopg import sql

        from varyz.cloud.postgres import PostgresStore

        schema = "shared_test_" + secrets.token_hex(8)
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        store = None
        try:
            store = PostgresStore(dsn, schema=schema)
            result = {**gate(store), **collaboration(store), **by_username(store), **notices(store)}
        finally:
            if store:
                store.close()
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        print(json.dumps({"backend": "postgresql", "checks": result}))
    else:
        with tempfile.TemporaryDirectory(
            dir=os.environ.get("VARYZ_PROBE_TMPDIR", str(Path.cwd()))
        ) as directory:
            store = Store(Path(directory) / "fixture.sqlite")
            checks = {**gate(store), **collaboration(store), **by_username(store), **notices(store)}
            print(json.dumps({"backend": "sqlite", "checks": checks}))


if __name__ == "__main__":
    main()
