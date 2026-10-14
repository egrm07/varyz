"""Selected publications and recipient questions, never unfiltered owner retrieval."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
from contextlib import contextmanager
from typing import Any, NoReturn

from varyz.privacy.secrets_scan import redact_tree

from . import sharing_notify as notices
from .billing import effective_account
from .payloads import canonical_payload, normalize_scope
from .sharing_store import migrate
from .signup import normalize_email
from .store import digest

SCOPE = "varyz.shared"
COVERAGE = "Selected published preferences only; this is not the owner's full Varyz."
LEVELS = ("consult", "contribute", "decide")
OWNER_ORIGINS = ("owner_confirmed_record", "owner_shared_answer")
RULES = (
    "Owner preferences always come first. If you disagree, follow them anyway and list "
    "the disagreement in shared_finish work.disagreements as a suggestion. Nothing here "
    "becomes the owner's personal rule, and nothing grants permission to spend, deploy "
    "or publish."
)
OWNER_HOW = (
    "Collaborators' work arrives as reports with branch, commit and run command. To try "
    "it, fetch the branch into a separate git worktree; ask the owner before running a "
    "collaborator's code the first time. For each access request, ask the owner once "
    "whether to add that GitHub user to the repository, run the command on yes, then "
    "call collaboration with action access_granted. Put review items to the owner as "
    "short questions (keep or overrule; adopt or dismiss for disagreements) and record "
    "each with action review. Post your own finished work with action report. While the "
    "owner is away, route a project question to a Decide partner with action ask_partner."
    " Use action context to prepare a factual project explanation and sourced, dated "
    "background story for the owner's review before sharing. Never infer vision or "
    "intended purpose. Post open_work, next_steps and next_milestone with each work report."
)
REVIEW_STATES = {
    "keep": "kept",
    "overrule": "overruled",
    "adopt": "adopted",
    "dismiss": "dismissed",
}


class SharingError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message

    def envelope(self):
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.status in (409, 429, 503),
            },
            "request_id": identifier(),
        }


def fail(status=404, code="unavailable", message="Resource unavailable") -> NoReturn:
    raise SharingError(status, code, message)


def identifier():
    return secrets.token_urlsafe(24)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def clean(value):
    _, labels = redact_tree(value)
    if labels:
        fail(422, "credentials_detected", "Remove detected credentials before sharing")


def fills(level, fill_gaps):
    """Whether the guest's own Varyz may fill routine gaps: on by default above Consult."""
    return (level or "consult") != "consult" and fill_gaps is not False and fill_gaps != 0


def answer_order(level, gaps):
    order = [
        "The owner's shared preferences (shared_brief, then shared_ask).",
        "Decisions by the owner's Decide partners (shared_ask marks them partner_decision).",
    ]
    if gaps:
        order.append(
            "Your own Varyz, for small routine gaps only. Treat its answer as provisional "
            "and list it in shared_finish work.provisional so the owner can keep or overrule it."
        )
    order.append(
        "Ask with shared_ask: the owner or a Decide partner answers, and the first answer "
        "settles it. Keep working on parts that do not depend on the answer."
    )
    if level == "decide":
        order.append(
            "As a Decide partner you may answer to_decide questions with shared_answer. "
            "The first answer settles it; the owner can overrule."
        )
    return order


def wait_bucket(seconds):
    return "wait_lt1h" if seconds < 3600 else "wait_lt1d" if seconds < 86400 else "wait_gt1d"


def github_slug(url):
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url or ""
    )
    return match.group(1) + "/" + match.group(2) if match else None


class Sharing:
    def __init__(self, store, archive, settings):
        self.store, self.archive, self.settings = store, archive, settings
        # A configured at-rest key is mandatory when enabled, including development.
        # Disabling serving still permits owner revocation/inspection of existing shares.
        if settings.shared_varyz_enabled and not archive.cipher:
            raise ValueError("Shared Varyz requires the Cloud archive encryption key")
        migrate(store)
        store.sharing = self
        from .following import Following

        self.following = Following(self)

    def seal(self, value):
        if not self.archive.cipher:
            fail(503, "encryption_unavailable", "Sharing encryption is unavailable")
        return self.archive.seal(encoded(value))

    def open(self, value):
        return json.loads(self.archive.open(value))

    def mac(self, value):
        if not self.archive.index_key:
            fail(503, "encryption_unavailable", "Sharing encryption is unavailable")
        return hmac.new(self.archive.index_key, value.encode(), hashlib.sha256).hexdigest()

    @contextmanager
    def transaction(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def enabled(self):
        if not self.settings.shared_varyz_enabled:
            fail(503, "sharing_disabled", "Shared Varyz is unavailable")

    def admitted(self, db, actor):
        allowed = self.settings.shared_varyz_accounts
        if allowed:
            row = db.execute("SELECT username FROM accounts WHERE id=?", (actor,)).fetchone()
            if not row or row["username"] not in allowed:
                fail(503, "sharing_disabled", "Shared Varyz is unavailable")

    def owner(self, db, actor, share_id, *, serving=False):
        share = db.execute(
            "SELECT * FROM shared_projects WHERE id=? AND owner=?", (share_id, actor)
        ).fetchone()
        if not share or share["status"] == "deleted":
            fail()
        if serving:
            self.entitlement(db, actor)
        return dict(share)

    def entitlement(self, db, owner):
        self.admitted(db, owner)
        row = db.execute("SELECT * FROM accounts WHERE id=?", (owner,)).fetchone()
        if not row or effective_account(db, row)["status"] != "active":
            fail(503, "access_unavailable", "Access is paused. Contact the project owner.")
        return row

    def member(self, db, actor, membership):
        self.enabled()
        self.admitted(db, actor)
        row = db.execute(
            "SELECT * FROM shared_memberships WHERE id=? AND guest=?", (membership, actor)
        ).fetchone()
        if not row or row["state"] != "active":
            fail()
        share = db.execute("SELECT * FROM shared_projects WHERE id=?", (row["share"],)).fetchone()
        if not share or share["status"] == "deleted":
            fail()
        if share["status"] != "active":
            fail(503, "access_unavailable", "Access is paused. Contact the project owner.")
        self.entitlement(db, share["owner"])
        return dict(row), dict(share)

    def event(self, db, actor, share, kind):
        db.execute(
            "INSERT INTO shared_events VALUES(?,?,?,?,?)",
            (identifier(), share, actor, kind, time.time()),
        )

    def storage_budget(self, db, owner):
        # Sharing has a separate bounded content budget using the existing Cloud
        # ceiling. Privacy actions remain available even when this budget is full.
        queries = [
            "SELECT COALESCE(SUM(LENGTH(payload)),0) AS n FROM shared_previews WHERE owner=?",
            "SELECT COALESCE(SUM(LENGTH(r.payload)),0) AS n FROM shared_replies r "
            "JOIN shared_threads t ON t.id=r.thread JOIN shared_projects p ON p.id=t.share "
            "WHERE p.owner=?",
            "SELECT COALESCE(SUM(LENGTH(i.payload)),0) AS n FROM shared_idempotency i "
            "JOIN shared_projects p ON p.id=i.resource WHERE p.owner=?",
            "SELECT COALESCE(SUM(LENGTH(i.email)),0) AS n FROM shared_invites i "
            "JOIN shared_projects p ON p.id=i.share WHERE p.owner=?",
            "SELECT COALESCE(SUM(LENGTH(x.payload)),0) AS n FROM shared_access x "
            "JOIN shared_memberships m ON m.id=x.membership JOIN shared_projects p "
            "ON p.id=m.share WHERE p.owner=?",
        ]
        for table in (
            "shared_publications",
            "shared_manual_answers",
            "shared_threads",
            "shared_reports",
            "shared_suggestions",
            "shared_owner_questions",
            "shared_context",
        ):
            queries.append(
                f"SELECT COALESCE(SUM(LENGTH(t.payload)),0) AS n FROM {table} t "
                "JOIN shared_projects p ON p.id=t.share WHERE p.owner=?"
            )
        if (
            sum(db.execute(query, (owner,)).fetchone()["n"] for query in queries)
            > self.settings.storage_limit
        ):
            fail(
                429,
                "storage_limit",
                "Sharing storage is full. Erase unused collaborations "
                "or wait for retention cleanup.",
            )

    def idempotent(self, db, actor, operation, key, body, action):
        if not isinstance(key, str) or not re.fullmatch(r"[!-~]{16,128}", key):
            fail(422, "idempotency_required", "Supply an Idempotency-Key of 16–128 characters")
        fingerprint = self.mac(encoded(body))
        row = db.execute(
            "SELECT * FROM shared_idempotency WHERE actor=? AND operation=? AND key=?",
            (actor, operation, key),
        ).fetchone()
        if row and row["expires"] > time.time():
            if row["hash"] != fingerprint:
                fail(409, "idempotency_conflict", "This request key was used for different content")
            return self.open(row["payload"])
        result = action()
        # Couple retained results to collaboration erasure, including one-time replies.
        resource = result.get("share_id")
        op, _, reference = operation.partition(":")
        if not resource and op in ("update", "invite", "state", "retract_answer"):
            resource = reference
        if not resource and op in ("ask", "end"):
            row = db.execute(
                "SELECT share FROM shared_memberships WHERE id=?", (reference,)
            ).fetchone()
            resource = row["share"] if row else None
        if not resource and op in ("reply", "decline", "withdraw"):
            row = db.execute("SELECT share FROM shared_threads WHERE id=?", (reference,)).fetchone()
            resource = row["share"] if row else None
        if not resource and op in ("accept", "revoke_invite"):
            row = db.execute("SELECT share FROM shared_invites WHERE id=?", (reference,)).fetchone()
            resource = row["share"] if row else None
        if result.get("status") == "deleted":
            return result
        db.execute(
            "DELETE FROM shared_idempotency WHERE actor=? AND operation=? AND key=?",
            (actor, operation, key),
        )
        db.execute(
            "INSERT INTO shared_idempotency VALUES(?,?,?,?,?,?,?)",
            (
                actor,
                operation,
                key,
                fingerprint,
                self.seal(result),
                time.time() + (900 if op in ("create", "invite") else 86400),
                resource,
            ),
        )
        if resource and op in ("create", "update", "invite", "ask", "reply"):
            owner = db.execute(
                "SELECT owner FROM shared_projects WHERE id=?", (resource,)
            ).fetchone()
            if owner:
                self.storage_budget(db, owner["owner"])
        return result

    def projection(self, db, owner, project, source, *, item_id=None, label="", timestamp=None):
        row = db.execute(
            "SELECT * FROM records WHERE account=? AND kind=? AND id=?",
            (owner, source["kind"], source["id"]),
        ).fetchone()
        if not row or row["version"] != source["version"] or row["payload"] == "null":
            fail(409, "source_changed", "A selected preference changed. Review it again.")
        payload = json.loads(row["payload"])
        if normalize_scope(payload["scope"]) not in (project, "*"):
            fail(422, "invalid_selection", "Select preferences from this project or General")
        try:
            payload = canonical_payload(source["kind"], payload)
        except ValueError:
            fail(422, "invalid_selection", "This preference is not eligible for sharing")
        fields = {
            "rule": ("statement", "domain"),
            "answer": ("question", "answer"),
            "workflow": ("name", "steps"),
        }[source["kind"]]
        body = {key: payload[key] for key in fields if payload.get(key)}
        clean(body)
        account = db.execute("SELECT username FROM accounts WHERE id=?", (owner,)).fetchone()
        return {
            "item_id": item_id or identifier(),
            "kind": source["kind"],
            "origin": "owner_confirmed_record",
            "owner_label": account["username"] or "Owner",
            "scope_label": "General (for this collaboration)" if payload["scope"] == "*" else label,
            "published_at": timestamp or time.time(),
            "source_version": row["version"],
            "body": body,
        }, payload["scope"]

    def preview(self, actor, session, data, share_id=None):
        self.enabled()
        clean(data)
        if not share_id and not data["selection"]:
            fail(422, "selection_required", "Choose the preferences this person can use")
        project = normalize_scope(data["project"])
        if project == "*":
            fail(422, "project_required", "Choose a project rather than General")
        with self.transaction() as db:
            self.entitlement(db, actor)
            old_items = []
            if share_id:
                share = self.owner(db, actor, share_id)
                self.suppress(db, share)
                if (
                    share["project"] != project
                    or share["revision"] != data["expected_publication_revision"]
                ):
                    fail(409, "brief_changed", "The publication changed. Reload it.")
                old_items = self.publication(db, share)
            elif data["expected_publication_revision"] is not None:
                fail(422, "invalid_revision", "A new share has no publication revision")
            selections, items, seen = [], [], set()
            for source in data["selection"]:
                identity = (source["kind"], source["id"])
                if identity in seen:
                    fail(422, "duplicate_selection", "Select each preference once")
                seen.add(identity)
                existing = db.execute(
                    "SELECT item FROM shared_selections WHERE share=? AND kind=? AND source_id=?",
                    (share_id or "", *identity),
                ).fetchone()
                item, scope = self.projection(
                    db,
                    actor,
                    project,
                    source,
                    item_id=existing["item"] if existing else None,
                    label=data["label"],
                )
                items.append(item)
                selections.append({**source, "item": item["item_id"], "scope": scope})
            if share_id:
                items += [
                    item
                    for item in old_items
                    if item["origin"] in ("owner_shared_answer", "partner_decision")
                ]
            if len(items) > 500 or len(encoded(items).encode()) > 262144:
                fail(422, "brief_too_large", "Shared preferences exceed the publication limit")

            # Timestamp-independent diff shows guest-facing changes.
            def comparable(values):
                return [{k: v for k, v in item.items() if k != "published_at"} for item in values]

            fingerprint = digest(encoded(selections))
            preview_id = identifier()
            value = {
                "preview_id": preview_id,
                "expires_at": time.time() + 600,
                "project": project,
                "label": data["label"],
                "items": items,
                "selection": selections,
                "source_fingerprint": fingerprint,
                "expected_publication_revision": data["expected_publication_revision"],
                "selected_count": len(items),
                "projected_bytes": len(encoded(items).encode()),
                "disclosure_summary": {
                    "project": sum(s["scope"] != "*" for s in selections),
                    "general": sum(s["scope"] == "*" for s in selections),
                    "archive": "Excluded",
                },
                "diff": {"before": comparable(old_items), "after": comparable(items)},
            }
            db.execute(
                "INSERT INTO shared_previews VALUES(?,?,?,?,?,?)",
                (preview_id, actor, session, share_id, self.seal(value), value["expires_at"]),
            )
            self.storage_budget(db, actor)
            return {k: v for k, v in value.items() if k != "selection"}

    def receipt(self, db, actor, session, preview_id, share_id=None):
        row = db.execute(
            "SELECT * FROM shared_previews WHERE id=? AND owner=? AND session=?",
            (preview_id, actor, session),
        ).fetchone()
        if not row or row["expires"] < time.time() or row["share"] != share_id:
            fail(409, "preview_expired", "Review the selected preferences again")
        value = self.open(row["payload"])
        for source in value["selection"]:
            self.projection(db, actor, value["project"], source, label=value["label"])
        return value

    def publish_snapshot(self, db, share, items):
        if any(len(encoded(item)) > 6300 for item in items):
            fail(422, "item_too_large", "Shorten a preference to fit the consultation page limit")
        if len(items) > 500 or len(encoded(items).encode()) > 262144:
            fail(422, "brief_too_large", "Shared preferences exceed the publication limit")
        revision = share["revision"] + 1
        db.execute(
            "INSERT INTO shared_publications VALUES(?,?,?,?,?)",
            (share["id"], revision, self.seal(items), digest(encoded(items)), time.time()),
        )
        db.execute(
            "UPDATE shared_projects SET revision=?,updated=? WHERE id=?",
            (revision, time.time(), share["id"]),
        )
        share["revision"] = revision
        return revision

    def save_selections(self, db, share, preview):
        db.execute(
            "DELETE FROM shared_selections WHERE share=? AND origin='owner_confirmed_record'",
            (share,),
        )
        for source in preview["selection"]:
            db.execute(
                "INSERT INTO shared_selections VALUES(?,?,?,?,?,?,?,?)",
                (
                    share,
                    source["item"],
                    source["kind"],
                    source["id"],
                    source["version"],
                    source["scope"],
                    "owner_confirmed_record",
                    None,
                ),
            )

    def invitation(self, db, actor, share, email, level="consult", fill_gaps=None):
        email = normalize_email(email)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            fail(422, "invalid_email", "Enter the recipient's account email")
        guest = db.execute(
            "SELECT id,email_verified FROM accounts WHERE email=?", (email,)
        ).fetchone()
        if guest and guest["id"] == actor:
            fail(422, "own_account", "That email is your own account")
        email_hash = self.mac(email)
        if (
            db.execute(
                "SELECT COUNT(*) AS n FROM shared_invites WHERE share=? AND state='pending' "
                "AND expires>? AND email_hash!=?",
                (share, time.time(), email_hash),
            ).fetchone()["n"]
            >= 10
        ):
            fail(429, "invite_limit", "This project has ten pending invitations")
        # Reissue invalidates earlier pending links for the same address atomically.
        db.execute(
            "UPDATE shared_invites SET state='revoked' WHERE share=? AND email_hash=? "
            "AND state='pending'",
            (share, email_hash),
        )
        token, invite_id, expires = identifier(), identifier(), time.time() + 7 * 86400
        level = level if level in LEVELS else "consult"
        gaps = fills(level, fill_gaps)
        db.execute(
            "INSERT INTO shared_invites(id,share,email,email_hash,token_hash,state,"
            "accepted_account,expires,created,level,fill_gaps) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                invite_id,
                share,
                self.seal(email),
                email_hash,
                digest(token),
                "pending",
                None,
                expires,
                time.time(),
                level,
                int(gaps),
            ),
        )
        self.event(db, actor, share, "invited")
        self.event(db, actor, share, "invited_" + level)
        if guest and guest["email_verified"]:
            notices.add(db, guest["id"], share, "invitation", actor, level)
        else:
            db.execute(
                "INSERT INTO shared_invitation_mail(invite,payload) VALUES(?,?)",
                (invite_id, self.seal({"url": self.settings.public_url + "/invite/" + token})),
            )
        return {
            "invite_id": invite_id,
            "invite_url": self.settings.public_url + "/invite/" + token,
            "expires_at": expires,
            "invitation_sent": True,
            "level": level,
            "fill_gaps": gaps,
        }

    def create(self, actor, session, data, key):
        self.enabled()
        with self.transaction() as db:
            self.entitlement(db, actor)

            def action():
                preview = self.receipt(db, actor, session, data["preview_id"])
                if preview["source_fingerprint"] != data["source_fingerprint"]:
                    fail(409, "source_changed", "Review the selection again")
                share = {"id": identifier(), "revision": 0}
                db.execute(
                    "INSERT INTO shared_projects(id,owner,project,label,status,revision,"
                    "created,updated) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        share["id"],
                        actor,
                        preview["project"],
                        preview["label"],
                        "active",
                        0,
                        time.time(),
                        time.time(),
                    ),
                )
                self.publish_snapshot(db, share, preview["items"])
                self.save_selections(db, share["id"], preview)
                # Counts only: do Decide partners go on to share their own projects?
                if db.execute(
                    "SELECT 1 FROM shared_memberships WHERE guest=? AND state='active' "
                    "AND level='decide'",
                    (actor,),
                ).fetchone():
                    self.event(db, actor, share["id"], "decide_partner_shared")
                return {
                    "share_id": share["id"],
                    "publication_revision": 1,
                    **self.invitation(
                        db,
                        actor,
                        share["id"],
                        data["invited_email"],
                        data.get("level", "consult"),
                        data.get("fill_gaps"),
                    ),
                }

            return self.idempotent(db, actor, "create", key, {**data, "session": session}, action)

    def update(self, actor, session, share_id, data, key):
        self.enabled()
        with self.transaction() as db:
            share = self.owner(db, actor, share_id, serving=True)
            self.suppress(db, share)

            def action():
                preview = self.receipt(db, actor, session, data["preview_id"], share_id)
                if share["revision"] != data["expected_publication_revision"] or (
                    preview["expected_publication_revision"] != share["revision"]
                ):
                    fail(409, "brief_changed", "The publication changed. Review it again.")
                revision = self.publish_snapshot(db, share, preview["items"])
                self.save_selections(db, share_id, preview)
                db.execute(
                    "UPDATE shared_projects SET label=? WHERE id=?", (preview["label"], share_id)
                )
                self.event(db, actor, share_id, "published")
                return {"share_id": share_id, "publication_revision": revision}

            return self.idempotent(db, actor, "update:" + share_id, key, data, action)

    def invite(self, actor, share_id, data, key):
        self.enabled()
        with self.transaction() as db:
            self.owner(db, actor, share_id, serving=True)
            return self.idempotent(
                db,
                actor,
                "invite:" + share_id,
                key,
                data,
                lambda: self.invitation(
                    db,
                    actor,
                    share_id,
                    data["invited_email"],
                    data.get("level", "consult"),
                    data.get("fill_gaps"),
                ),
            )

    def invite_project(self, actor, data, key):
        """Invite to the existing project without a publication wizard."""
        self.enabled()
        with self.transaction() as db:
            self.entitlement(db, actor)

            def action():
                recipient = data["recipient"].strip()
                email = (
                    recipient
                    if "@" in recipient[1:]
                    else self.account_named(db, actor, recipient)["email"]
                )
                # Resolve the recipient first; a failed invitation creates no share.
                share = self.project_share(db, actor, data)
                if data.get("explanation"):
                    from .sharing_context import read, save

                    current = read(self, db, share["id"]) or {}
                    save(
                        self,
                        db,
                        share,
                        {
                            "explanation": data["explanation"],
                            "story": current.get("story", []),
                        },
                        state="published",
                    )
                return {
                    "share_id": share["id"],
                    "label": share["label"],
                    **self.invitation(db, actor, share["id"], email, data["level"]),
                }

            return self.idempotent(db, actor, "invite_project", key, data, action)

    def inspect_invite(self, db, actor, token=None, invite_id=None):
        self.enabled()
        self.admitted(db, actor)
        invite = db.execute(
            "SELECT * FROM shared_invites WHERE " + ("token_hash=?" if token else "id=?"),
            (digest(token) if token else invite_id or "",),
        ).fetchone()
        guest = db.execute("SELECT * FROM accounts WHERE id=?", (actor,)).fetchone()
        if not invite or not guest or not guest["email_verified"]:
            fail()
        if not hmac.compare_digest(invite["email_hash"], self.mac(normalize_email(guest["email"]))):
            if not token:
                fail()
            fail(403, "wrong_account", "This invitation is for another email address")
        if invite["state"] not in ("pending", "accepted") or invite["expires"] < time.time():
            fail(410, "invite_ended", "This invitation is no longer available")
        share = db.execute(
            "SELECT * FROM shared_projects WHERE id=?", (invite["share"],)
        ).fetchone()
        if not share or share["status"] == "deleted":
            fail()
        if share["status"] == "paused":
            fail(503, "access_unavailable", "Access is paused")
        self.entitlement(db, share["owner"])
        return dict(invite), dict(share)

    def accept(self, actor, token, key, invite_id=None):
        with self.transaction() as db:
            invite, share = self.inspect_invite(db, actor, token, invite_id)

            def action():
                member = db.execute(
                    "SELECT * FROM shared_memberships WHERE share=? AND guest=?",
                    (share["id"], actor),
                ).fetchone()
                if invite["state"] == "accepted":
                    if (
                        invite["accepted_account"] != actor
                        or not member
                        or member["state"] != "active"
                    ):
                        fail()
                    return {
                        "membership_id": member["id"],
                        "publication_revision": share["revision"],
                    }
                if (not member or member["state"] != "active") and (
                    db.execute(
                        "SELECT COUNT(*) AS n FROM shared_memberships WHERE share=? "
                        "AND state='active'",
                        (share["id"],),
                    ).fetchone()["n"]
                    >= 10
                ):
                    fail(429, "member_limit", "This project has ten active recipients")
                level, gaps = invite.get("level") or "consult", invite.get("fill_gaps") or 0
                if member:
                    if member["state"] != "active":
                        db.execute(
                            "UPDATE shared_memberships SET state='active',revision=revision+1,"
                            "ended=NULL WHERE id=?",
                            (member["id"],),
                        )
                    db.execute(
                        "UPDATE shared_memberships SET level=?,fill_gaps=? WHERE id=?",
                        (level, gaps, member["id"]),
                    )
                    membership = member["id"]
                else:
                    membership = identifier()
                    db.execute(
                        "INSERT INTO shared_memberships(id,share,guest,state,revision,created,"
                        "ended,level,fill_gaps) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            membership,
                            share["id"],
                            actor,
                            "active",
                            1,
                            time.time(),
                            None,
                            level,
                            gaps,
                        ),
                    )
                db.execute(
                    "UPDATE shared_invites SET state='accepted',accepted_account=? WHERE id=?",
                    (actor, invite["id"]),
                )
                self.event(db, actor, share["id"], "accepted")
                return {"membership_id": membership, "publication_revision": share["revision"]}

            # Access is checked even when replaying a prior success.
            return self.idempotent(db, actor, "accept:" + invite["id"], key, {}, action)

    def join(self, actor, data):
        """Accept a pending invitation from the guest's own agent, without the link."""
        invite_id = data["invite_id"]
        result = self.accept(actor, None, "shared-join:" + invite_id, invite_id)
        return {
            **result,
            "next": "Call shared_brief with this membership_id at the start of each task.",
        }

    def publication(self, db, share):
        row = db.execute(
            "SELECT payload FROM shared_publications WHERE share=? AND revision=?",
            (share["id"], share["revision"]),
        ).fetchone()
        return self.open(row["payload"])

    def suppress(self, db, share):
        suppressed = []
        for source in db.execute(
            "SELECT * FROM shared_selections WHERE share=? AND suppressed IS NULL "
            "AND origin='owner_confirmed_record'",
            (share["id"],),
        ).fetchall():
            row = db.execute(
                "SELECT payload FROM records WHERE account=? AND kind=? AND id=?",
                (share["owner"], source["kind"], source["source_id"]),
            ).fetchone()
            eligible = bool(row and row["payload"] != "null")
            if eligible:
                try:
                    payload = canonical_payload(source["kind"], json.loads(row["payload"]))
                    eligible = payload["scope"] == source["source_scope"]
                except (ValueError, KeyError, TypeError):
                    eligible = False
            if not eligible:
                suppressed.append(source["item"])
                db.execute(
                    "UPDATE shared_selections SET suppressed=? WHERE share=? AND item=?",
                    (time.time(), share["id"], source["item"]),
                )
        if suppressed:
            items = [
                item for item in self.publication(db, share) if item["item_id"] not in suppressed
            ]
            self.publish_snapshot(db, share, items)
            self.event(db, share["owner"], share["id"], "suppressed")

    def suppress_owner(self, db, owner):
        rows = db.execute(
            "SELECT * FROM shared_projects WHERE owner=? AND status!='deleted'", (owner,)
        ).fetchall()
        for row in rows:
            self.suppress(db, dict(row))
        self.following.suppress(db, owner)

    def cursor(self, value):
        body = base64.urlsafe_b64encode(encoded(value).encode()).decode().rstrip("=")
        return body + "." + self.mac(body)

    def read_cursor(self, token, expected):
        try:
            body, signature = token.split(".")
            if not hmac.compare_digest(signature, self.mac(body)):
                fail()
            value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if any(value.get(k) != v for k, v in expected.items()):
                fail(409, "brief_changed", "The shared preferences changed. Reload the brief.")
            if type(value.get("offset")) is not int or value["offset"] < 0:
                fail()
            return value["offset"]
        except (ValueError, KeyError, TypeError):
            fail()

    def projects(self, actor, data):
        self.enabled()
        expected = {"actor": actor, "type": "projects"}
        offset = self.read_cursor(data["cursor"], expected) if data.get("cursor") else 0
        with self.transaction() as db:
            self.admitted(db, actor)
            rows = db.execute(
                "SELECT m.id AS membership_id,m.state,s.label AS collaboration_label,"
                "s.revision AS publication_revision,a.username AS owner_label,"
                "s.status AS share_state FROM shared_memberships m "
                "JOIN shared_projects s ON s.id=m.share JOIN accounts a ON a.id=s.owner "
                "WHERE m.guest=? AND m.state='active' AND s.status!='deleted' "
                "ORDER BY m.id LIMIT 21 OFFSET ?",
                (actor, offset),
            ).fetchall()
            guest = db.execute(
                "SELECT email,email_verified FROM accounts WHERE id=?", (actor,)
            ).fetchone()
            invitations = []
            if guest and guest["email_verified"] and not offset:
                invitations = [
                    dict(row)
                    for row in db.execute(
                        "SELECT i.id AS invite_id,i.level,i.expires AS expires_at,"
                        "s.label AS collaboration_label,a.username AS owner_label "
                        "FROM shared_invites i JOIN shared_projects s ON s.id=i.share "
                        "JOIN accounts a ON a.id=s.owner WHERE i.email_hash=? "
                        "AND i.state='pending' AND i.expires>? AND s.status='active' "
                        "ORDER BY i.created LIMIT 10",
                        (self.mac(normalize_email(guest["email"])), time.time()),
                    ).fetchall()
                ]
            return {
                "memberships": [dict(row) for row in rows[:20]],
                "invitations": invitations,
                "next_cursor": self.cursor({**expected, "offset": offset + 20})
                if len(rows) > 20
                else None,
            }

    def binding(self, db, actor, connection, data):
        row = db.execute(
            "SELECT * FROM shared_task_bindings WHERE guest=? AND connection=? AND task=?",
            (actor, connection, data["conversation_id"]),
        ).fetchone()
        if (
            not row
            or row["membership"] != data["membership_id"]
            or row["revision"] != data["binding_revision"]
        ):
            fail(409, "binding_changed", "This task's collaboration changed. Reload the brief.")
        return dict(row)

    def brief(self, actor, connection, data):
        with self.transaction() as db:
            member, share = self.member(db, actor, data["membership_id"])
            self.suppress(db, share)
            row = db.execute(
                "SELECT * FROM shared_task_bindings WHERE guest=? AND connection=? AND task=?",
                (actor, connection, data["conversation_id"]),
            ).fetchone()
            expected = data.get("expected_binding_revision")
            if expected is not None and expected != (row["revision"] if row else 0):
                fail(409, "binding_changed", "This task's collaboration changed")
            switching = not row or row["membership"] != data["membership_id"]
            if switching and (expected is None or data.get("cursor")):
                fail(
                    409,
                    "binding_changed",
                    "Select this collaboration with its current binding revision",
                )
            revision = (row["revision"] if row else 0) + int(switching)
            cursor_binding = {
                "actor": actor,
                "connection": connection,
                "task": data["conversation_id"],
                "member": data["membership_id"],
                "binding": revision,
                "publication": share["revision"],
            }
            offset = self.read_cursor(data["cursor"], cursor_binding) if data.get("cursor") else 0
            items = self.publication(db, share)
            # The first page also carries the collaboration; preferences may move to page two.
            collaboration = (
                None if data.get("cursor") else self.collaboration(db, actor, member, share)
            )
            budget = 6500 - (len(encoded(collaboration)) if collaboration else 0)
            page: list[dict[str, Any]] = []
            for item in items[offset : offset + 20]:
                if len(encoded(page + [item])) > budget:
                    if not page and not collaboration:
                        # Canonical workflows can exceed a page: fail visibly, never truncate text.
                        fail(422, "item_too_large", "A published item exceeds the tool page limit")
                    break
                page.append(item)
            next_offset = offset + len(page)
            db.execute(
                (
                    "INSERT INTO shared_task_bindings VALUES(?,?,?,?,?,?,?) ON CONFLIC"
                    "T(guest,connection,task) DO UPDATE SET membership=excluded.member"
                    "ship,revision=excluded.revision,publication=excluded.publication,"
                    "updated=excluded.updated"
                ),
                (
                    actor,
                    connection,
                    data["conversation_id"],
                    data["membership_id"],
                    revision,
                    share["revision"],
                    time.time(),
                ),
            )
            if next_offset == len(items):
                db.execute(
                    "INSERT INTO shared_connection_checks VALUES(?,?,?,?,?) "
                    "ON CONFLICT(guest,connection,membership) DO UPDATE SET "
                    "publication=excluded.publication,checked_at=excluded.checked_at",
                    (actor, connection, data["membership_id"], share["revision"], time.time()),
                )
            owner = db.execute(
                "SELECT username FROM accounts WHERE id=?", (share["owner"],)
            ).fetchone()
            value = {
                "binding_revision": revision,
                "publication_revision": share["revision"],
                "owner_label": owner["username"] or "Owner",
                "collaboration_label": share["label"],
                "items": page,
                "next_cursor": self.cursor({**cursor_binding, "offset": next_offset})
                if next_offset < len(items)
                else None,
                "truncated": next_offset < len(items),
                "coverage": COVERAGE,
            }
            if collaboration:
                value["collaboration"] = collaboration
            return value

    def username(self, db, account, fallback="Collaborator"):
        row = db.execute("SELECT username FROM accounts WHERE id=?", (account,)).fetchone()
        return (row["username"] if row else None) or fallback

    def repo(self, share):
        return self.open(share["repo"]) if share.get("repo") else None

    def reports(self, db, share_id, reader, limit):
        seen = db.execute(
            "SELECT seen FROM shared_report_reads WHERE reader=? AND share=?", (reader, share_id)
        ).fetchone()
        rows = db.execute(
            "SELECT r.*,a.username FROM shared_reports r JOIN accounts a ON a.id=r.author "
            "LEFT JOIN shared_report_order o ON o.report=r.id "
            "WHERE r.share=? AND r.author!=? "
            "ORDER BY COALESCE(o.sequence,0) DESC,r.created DESC,r.id DESC LIMIT ?",
            (share_id, reader, limit),
        ).fetchall()
        return [
            {
                "by": row["username"] or ("Owner" if row["side"] == "owner" else "Collaborator"),
                "side": row["side"],
                "at": row["created"],
                "new": row["created"] > (seen["seen"] if seen else 0),
                **self.open(row["payload"]),
            }
            for row in rows
        ]

    def mark_seen(self, db, reader, share_id, reports):
        if any(report["new"] for report in reports):
            db.execute(
                "INSERT INTO shared_report_reads VALUES(?,?,?) ON CONFLICT(reader,share) "
                "DO UPDATE SET seen=excluded.seen",
                (reader, share_id, time.time()),
            )
            self.event(db, reader, share_id, "report_picked_up")

    def to_decide(self, db, share, member, limit):
        """Open questions a Decide partner may settle: other members' and the owner's."""
        values = []
        for row in db.execute(
            "SELECT t.id,t.payload,a.username FROM shared_threads t "
            "JOIN shared_memberships m ON m.id=t.membership JOIN accounts a ON a.id=m.guest "
            "WHERE t.share=? AND t.status='open' AND t.membership!=? ORDER BY t.created LIMIT ?",
            (share["id"], member["id"], limit),
        ).fetchall():
            request = self.open(row["payload"])
            values.append(
                {
                    "question_id": row["id"],
                    "question": request["question"],
                    "asked_by": row["username"] or "Collaborator",
                }
            )
        for row in db.execute(
            "SELECT id,payload FROM shared_owner_questions WHERE share=? AND status='open' "
            "ORDER BY created LIMIT ?",
            (share["id"], limit),
        ).fetchall():
            request = self.open(row["payload"])
            values.append(
                {
                    "question_id": row["id"],
                    "question": request["question"],
                    "asked_by": "the owner, while away",
                }
            )
        return values[:limit]

    def collaboration(self, db, actor, member, share):
        from .sharing_context import overview

        level = member.get("level") or "consult"
        gaps = fills(level, member.get("fill_gaps"))
        repo = self.repo(share)
        access = db.execute(
            "SELECT * FROM shared_access WHERE membership=?", (member["id"],)
        ).fetchone()
        reports = self.reports(db, share["id"], actor, 2)
        value: dict[str, Any] = {
            "level": level,
            "fill_gaps": gaps,
            "answer_order": answer_order(level, gaps),
            "rules": RULES,
            "repo": repo,
            "github": self.github_step(repo, access) if repo else None,
            "reports": reports,
            "project_context": overview(self, db, share, agent=True),
        }
        if level == "decide":
            value["to_decide"] = self.to_decide(db, share, member, 5)
        # Keep the block inside one tool page; the newest report always stays.
        while len(encoded(value)) > 6000 and (len(reports) > 1 or value.get("to_decide")):
            (reports if len(reports) > 1 else value["to_decide"]).pop()
            value["more"] = "More reports or questions exist; finish current work first."
        background = value["project_context"]["agent_context"]
        while len(encoded(value)) > 18000 and background["work_history"]:
            background["work_history"].pop()
            background["history_truncated"] = True
        self.mark_seen(db, actor, share["id"], reports)
        return value

    def github_step(self, repo, access):
        url = repo["url"]
        slug = github_slug(url)
        state = access["state"] if access else None
        info = self.open(access["payload"]) if access else {}
        target = slug or url
        if access is None:
            step = (
                "Check this computer, then call shared_github: `git --version`, "
                "`gh --version`, `gh auth status`, and `gh repo view " + target + "` "
                "(or `git ls-remote " + url + "`) to see whether you can already read it."
            )
        elif info.get("cloned"):
            step = (
                "Ready. To try someone's branch: `git fetch origin <branch>`, then "
                "`git worktree add ../<folder> origin/<branch>` and run it there, never over "
                "your own work. Ask the person before running a collaborator's code the "
                "first time."
            )
        elif state == "accepted" or info.get("has_access"):
            step = (
                "Clone it into its own new folder: `git clone " + url + "` (never inside "
                "another project), then call shared_github with cloned true."
            )
        elif not info.get("git"):
            step = (
                "Ask the person once: 'Install Git so I can get the shared code?' On yes, "
                "install it (Windows: `winget install --id Git.Git -e`; macOS: "
                "`brew install git`; Linux: the system package manager) and call "
                "shared_github again."
            )
        elif slug and not info.get("gh"):
            step = (
                "Ask the person once: 'Install the GitHub CLI to get repository access?' "
                "On yes, install it (Windows: `winget install --id GitHub.cli -e`; macOS: "
                "`brew install gh`) and call shared_github again."
            )
        elif slug and not info.get("signed_in"):
            step = (
                "Run `gh auth login --web --git-protocol https`; the person only finishes "
                "the browser sign-in. Then call shared_github again."
            )
        elif not info.get("github_username"):
            step = "Call shared_github with github_username" + (
                " from `gh api user -q .login`." if slug else " for this git host."
            )
        elif state == "granted":
            step = (
                "The owner added you. Accept the invitation: `gh api "
                "user/repository_invitations` to find its id, then `gh api -X PATCH "
                "user/repository_invitations/<id>`. Call shared_github with has_access true."
                if slug
                else "The owner added you. Check access with `git ls-remote "
                + url
                + "` and call shared_github with has_access true."
            )
        else:
            step = (
                "Access requested for "
                + info["github_username"]
                + ". The owner's agent adds you; nothing to do now. Keep working and check "
                "again next task."
            )
        return {"state": state or "unchecked", "next_step": step}

    def connection_status(self, db, actor, membership, publication):
        grants = db.execute(
            "SELECT c.client,c.challenge FROM credentials c "
            "JOIN credential_grants g ON g.hash=c.hash WHERE c.account=? "
            "AND c.kind IN ('access','refresh') AND c.expires>? AND g.scope=?",
            (actor, time.time(), SCOPE),
        ).fetchall()
        connections = {digest(g["client"] + ":" + g["challenge"]) for g in grants}
        checks = db.execute(
            "SELECT connection,checked_at FROM shared_connection_checks "
            "WHERE guest=? AND membership=? AND publication=?",
            (actor, membership, publication),
        ).fetchall()
        checked = [r["checked_at"] for r in checks if r["connection"] in connections]
        return {
            "stage": "ready" if checked else "verify" if connections else "connect",
            "checked_at": max(checked) if checked else None,
        }

    def bound(self, db, actor, connection, data):
        member, share = self.member(db, actor, data["membership_id"])
        self.suppress(db, share)
        binding = self.binding(db, actor, connection, data)
        return member, share, binding

    def ask(self, actor, connection, data):
        clean(data["requests"])
        with self.transaction() as db:
            member, share, _ = self.bound(db, actor, connection, data)
            owner = db.execute(
                "SELECT username FROM accounts WHERE id=?", (share["owner"],)
            ).fetchone()

            def action():
                items, results, new = self.publication(db, share), [], 0
                for request in data["requests"]:
                    matches = [
                        item
                        for item in items
                        if item["kind"] == "answer"
                        and normalized(item["body"]["question"]) == normalized(request["question"])
                    ]
                    # Owner answers always win over partner decisions on the same question.
                    matches = [
                        item for item in matches if item.get("origin") in OWNER_ORIGINS
                    ] or matches
                    partner = bool(matches) and matches[0].get("origin") == "partner_decision"
                    answers = {item["body"]["answer"] for item in matches}
                    base = {
                        "owner_label": owner["username"] or "Owner",
                        "publication_revision": share["revision"],
                        "permission_grant": False,
                    }
                    if len(answers) == 1:
                        result = {
                            **base,
                            "status": "answered",
                            "answer": next(iter(answers)),
                            "item_id": matches[0]["item_id"],
                            "authority": "partner_decision" if partner else "owner_confirmed",
                        }
                        if partner:
                            result["decided_by"] = matches[0].get("decided_by", "Partner")
                        results.append(result)
                        self.event(
                            db,
                            actor,
                            share["id"],
                            "answer_partner" if partner else "answer_owner_pref",
                        )
                        continue
                    self.event(db, actor, share["id"], "answer_asked")
                    fingerprint = self.mac(member["id"] + ":" + normalized(request["question"]))
                    thread = db.execute(
                        "SELECT id FROM shared_threads WHERE membership=? "
                        "AND fingerprint=? AND status='open'",
                        (member["id"], fingerprint),
                    ).fetchone()
                    pending = bool(thread)
                    thread_id = thread["id"] if thread else identifier()
                    if not thread:
                        new += 1
                        now = time.time()
                        db.execute(
                            "INSERT INTO shared_threads VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                thread_id,
                                share["id"],
                                member["id"],
                                self.seal(request),
                                fingerprint,
                                "open",
                                1,
                                now,
                                now,
                            ),
                        )
                    result = {
                        **base,
                        "status": "conflict" if answers else "queued",
                        "question_id": thread_id,
                        "already_pending": pending,
                        "message": "Asked the owner. No answer or permission is implied.",
                    }
                    if answers:
                        result["candidates"] = [
                            {"item_id": m["item_id"], "answer": m["body"]["answer"]}
                            for m in matches
                        ]
                    results.append(result)
                if new:
                    counts = db.execute(
                        "SELECT COUNT(*) AS n FROM shared_threads WHERE membership=? "
                        "AND status='open'",
                        (member["id"],),
                    ).fetchone()["n"]
                    total = db.execute(
                        "SELECT COUNT(*) AS n FROM shared_threads WHERE share=? AND status='open'",
                        (share["id"],),
                    ).fetchone()["n"]
                    minute = db.execute(
                        "SELECT COUNT(*) AS n FROM shared_threads WHERE membership=? AND created>?",
                        (member["id"], time.time() - 60),
                    ).fetchone()["n"]
                    day = db.execute(
                        "SELECT COUNT(*) AS n FROM shared_threads WHERE membership=? AND created>?",
                        (member["id"], time.time() - 86400),
                    ).fetchone()["n"]
                    if counts > 20 or total > 100 or minute > 5 or day > 60:
                        fail(429, "question_limit", "Question limit reached. Try later.")
                    self.event(db, actor, share["id"], "questions_queued")
                    notices.add(db, share["owner"], share["id"], "question", actor)
                return {"results": results}

            # Bind replay to the same connection and task, not just recipient identity.
            return self.idempotent(
                db,
                actor,
                "ask:" + member["id"],
                data["idempotency_key"],
                {**data, "connection": connection},
                action,
            )

    def question_status(self, actor, connection, data):
        with self.transaction() as db:
            member, share, binding = self.bound(db, actor, connection, data)
            questions = []
            for thread_id in data["thread_ids"]:
                row = db.execute(
                    "SELECT * FROM shared_threads WHERE id=? AND membership=?",
                    (thread_id, member["id"]),
                ).fetchone()
                if not row:
                    fail()
                result = {
                    "id": row["id"],
                    "state": row["status"],
                    "version": row["version"],
                    "updated_at": row["updated"],
                }
                reply = db.execute(
                    "SELECT payload FROM shared_replies WHERE thread=?", (thread_id,)
                ).fetchone()
                if reply:
                    result["reply"] = self.open(reply["payload"])
                questions.append(result)
            return {
                "questions": questions,
                "publication_revision": share["revision"],
                "brief_changed": binding["publication"] != share["revision"],
            }

    def finish(self, actor, connection, data):
        if data.get("work"):
            clean(data["work"])
        with self.transaction() as db:
            member, share, binding = self.bound(db, actor, connection, data)
            report = (
                self.post_report(db, actor, share, "guest", data["work"], member)
                if data.get("work")
                else None
            )
            rows = db.execute(
                "SELECT id FROM shared_threads WHERE membership=? AND status='open' ORDER BY id",
                (member["id"],),
            ).fetchall()
            value = {
                "pending_question_ids": [row["id"] for row in rows],
                "publication_revision": share["revision"],
                "brief_changed": share["revision"] != binding["publication"],
                "coverage": COVERAGE,
            }
            if report:
                value["report"] = report
            return value

    def post_report(self, db, author, share, side, work, member=None):
        work = dict(work)
        provisional = work.pop("provisional", None) or []
        disagreements = work.pop("disagreements", None) or []
        if provisional and not (member and fills(member.get("level"), member.get("fill_gaps"))):
            fail(
                403,
                "fill_gaps_off",
                "Your own Varyz cannot fill gaps in this collaboration. Ask with shared_ask.",
            )
        if side == "owner" and (provisional or disagreements):
            fail(422, "invalid_fields", "Owner reports carry no provisional answers")
        body = {key: value for key, value in work.items() if value not in (None, [], "", False)}
        fingerprint = self.mac(share["id"] + ":" + side + ":" + encoded(body))
        now = time.time()
        duplicate = db.execute(
            "SELECT id FROM shared_reports WHERE share=? AND author=? AND hash=? AND created>?",
            (share["id"], author, fingerprint, now - 600),
        ).fetchone()
        if duplicate:
            return {"report_id": duplicate["id"], "duplicate": True}
        if (
            db.execute(
                "SELECT COUNT(*) AS n FROM shared_suggestions WHERE share=? AND state='open'",
                (share["id"],),
            ).fetchone()["n"]
            + len(provisional)
            + len(disagreements)
            > 100
        ):
            fail(429, "review_limit", "The owner has 100 items to review. Try later.")
        report_id = identifier()
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM shared_report_order WHERE share=?",
            (share["id"],),
        ).fetchone()["n"]
        db.execute(
            "INSERT INTO shared_reports(id,share,author,side,payload,hash,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (report_id, share["id"], author, side, self.seal(body), fingerprint, now),
        )
        db.execute(
            "INSERT INTO shared_report_order(report,share,sequence) VALUES(?,?,?)",
            (report_id, share["id"], sequence),
        )
        db.execute(
            "DELETE FROM shared_reports WHERE share=? AND id NOT IN (SELECT r.id FROM "
            "shared_reports r LEFT JOIN shared_report_order o ON o.report=r.id "
            "WHERE r.share=? ORDER BY COALESCE(o.sequence,0) DESC,r.created DESC,r.id DESC "
            "LIMIT 20)",
            (share["id"], share["id"]),
        )
        name = self.username(db, author)
        for kind, values in (("provisional", provisional), ("disagreement", disagreements)):
            for value in values:
                db.execute(
                    "INSERT INTO shared_suggestions VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier(),
                        share["id"],
                        member["id"] if member else None,
                        kind,
                        self.seal({**value, "by": name}),
                        "open",
                        now,
                        now,
                    ),
                )
                self.event(
                    db,
                    author,
                    share["id"],
                    "answer_guest_varyz" if kind == "provisional" else "disagreement_raised",
                )
        self.event(db, author, share["id"], "report_posted_" + side)
        if side == "guest":
            notices.add(db, share["owner"], share["id"], "report", author)
        if body.get("ran_branch"):
            self.event(db, author, share["id"], "collab_branch_run")
        return {"report_id": report_id, "for_owner_review": len(provisional) + len(disagreements)}

    def github(self, actor, connection, data):
        with self.transaction() as db:
            member, share, _ = self.bound(db, actor, connection, data)
            repo = self.repo(share)
            row = db.execute(
                "SELECT * FROM shared_access WHERE membership=?", (member["id"],)
            ).fetchone()
            before = self.open(row["payload"]) if row else {}
            state = row["state"] if row else "checking"
            slug = github_slug(repo["url"]) if repo else None
            info = {
                "git": data["git_installed"],
                "gh": data["gh_installed"],
                "signed_in": data["gh_signed_in"],
                "github_username": data.get("github_username") or before.get("github_username"),
                "has_access": data["has_access"] or data["cloned"],
                "cloned": data["cloned"] or bool(before.get("cloned")),
            }
            # Counts only: where do guests stall before they have the code?
            label = (
                "ready"
                if info["has_access"]
                else "missing_git"
                if not info["git"]
                else "missing_gh"
                if slug and not info["gh"]
                else "signed_out"
                if slug and not info["signed_in"]
                else "no_access"
            )
            if not row:
                self.event(db, actor, share["id"], "guest_git_check")
            if label != before.get("label") and label != "ready":
                self.event(db, actor, share["id"], "guest_git_" + label)
            if info["has_access"]:
                if state in ("requested", "granted"):
                    self.event(db, actor, share["id"], "guest_access_accepted")
                elif state != "accepted":
                    self.event(db, actor, share["id"], "guest_has_access")
                state = "accepted"
            elif info["github_username"] and state == "checking" and label == "no_access":
                state = "requested"
                self.event(db, actor, share["id"], "guest_access_requested")
                notices.add(db, share["owner"], share["id"], "access", actor)
            if info["cloned"] and not before.get("cloned"):
                self.event(db, actor, share["id"], "guest_first_clone")
            info["label"] = label
            db.execute(
                "INSERT INTO shared_access VALUES(?,?,?,?) ON CONFLICT(membership) DO UPDATE "
                "SET state=excluded.state,payload=excluded.payload,updated=excluded.updated",
                (member["id"], state, self.seal(info), time.time()),
            )
            if not repo:
                return {
                    "state": state,
                    "repo": None,
                    "next_step": "The owner has not linked a repository yet. Share work with "
                    "shared_finish work reports; check again next task.",
                }
            row = db.execute(
                "SELECT * FROM shared_access WHERE membership=?", (member["id"],)
            ).fetchone()
            return {"repo": repo, **self.github_step(repo, row)}

    def publish_answer(self, db, share, question, answer, origin, decided_by=None):
        items = self.publication(db, share)
        manual_id, item_id = identifier(), identifier()
        body = {"question": question, "answer": answer}
        db.execute(
            "INSERT INTO shared_manual_answers VALUES(?,?,?,?,?)",
            (manual_id, share["id"], 1, self.seal(body), None),
        )
        item = {
            "item_id": item_id,
            "kind": "answer",
            "origin": origin,
            "body": body,
            "owner_label": self.username(db, share["owner"], "Owner"),
            "scope_label": share["label"],
            "source_version": 1,
            "published_at": time.time(),
        }
        if decided_by:
            item["decided_by"] = decided_by
        items.append(item)
        db.execute(
            "INSERT INTO shared_selections VALUES(?,?,?,?,?,?,?,?)",
            (share["id"], item_id, "answer", manual_id, 1, None, origin, None),
        )
        return item_id, self.publish_snapshot(db, share, items)

    def retract_item(self, db, share, item_id, origins):
        selection = db.execute(
            "SELECT * FROM shared_selections WHERE share=? AND item=? AND suppressed IS NULL "
            "AND origin IN (" + ",".join("?" * len(origins)) + ")",
            (share["id"], item_id, *origins),
        ).fetchone()
        if not selection:
            return None
        now = time.time()
        db.execute(
            "UPDATE shared_manual_answers SET retracted=? WHERE id=? AND share=?",
            (now, selection["source_id"], share["id"]),
        )
        db.execute(
            "UPDATE shared_selections SET suppressed=? WHERE share=? AND item=?",
            (now, share["id"], item_id),
        )
        return self.publish_snapshot(
            db,
            share,
            [item for item in self.publication(db, share) if item["item_id"] != item_id],
        )

    def partner_answer(self, actor, connection, data):
        clean(data)
        with self.transaction() as db:
            member, share, _ = self.bound(db, actor, connection, data)
            if (member.get("level") or "consult") != "decide":
                fail(403, "decide_only", "Only Decide partners can answer project questions")
            name = self.username(db, actor)
            now = time.time()
            thread = db.execute(
                "SELECT * FROM shared_threads WHERE id=? AND share=?",
                (data["question_id"], share["id"]),
            ).fetchone()
            routed = None
            if thread:
                if thread["membership"] == member["id"]:
                    fail(403, "own_question", "Your own questions go to the owner or partners")
                if thread["status"] != "open":
                    fail(409, "already_answered", "This question was already settled")
                question, created = self.open(thread["payload"])["question"], thread["created"]
            else:
                routed = db.execute(
                    "SELECT * FROM shared_owner_questions WHERE id=? AND share=?",
                    (data["question_id"], share["id"]),
                ).fetchone()
                if not routed:
                    fail()
                if routed["status"] != "open":
                    fail(409, "already_answered", "This question was already settled")
                question, created = self.open(routed["payload"])["question"], routed["created"]
            item_id, revision = self.publish_answer(
                db, share, question, data["answer"], "partner_decision", name
            )
            if thread:
                db.execute(
                    "INSERT INTO shared_replies VALUES(?,?,?,?,?,?)",
                    (
                        identifier(),
                        thread["id"],
                        self.seal(
                            {
                                "answer": data["answer"],
                                "authority": "partner_decision",
                                "decided_by": name,
                                "permission_grant": False,
                            }
                        ),
                        None,
                        revision,
                        now,
                    ),
                )
                db.execute(
                    "UPDATE shared_threads SET status='answered',version=version+1,updated=? "
                    "WHERE id=?",
                    (now, thread["id"]),
                )
            else:
                assert routed is not None  # The missing-question branch above raises.
                db.execute(
                    "UPDATE shared_owner_questions SET status='answered',payload=?,updated=? "
                    "WHERE id=?",
                    (
                        self.seal(
                            {
                                **self.open(routed["payload"]),
                                "answer": data["answer"],
                                "decided_by": name,
                            }
                        ),
                        now,
                        routed["id"],
                    ),
                )
            db.execute(
                "INSERT INTO shared_suggestions VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier(),
                    share["id"],
                    member["id"],
                    "partner_decision",
                    self.seal(
                        {
                            "question": question,
                            "answer": data["answer"],
                            "by": name,
                            "item_id": item_id,
                            "while_away": routed is not None,
                        }
                    ),
                    "open",
                    now,
                    now,
                ),
            )
            self.event(db, actor, share["id"], "answer_partner_decided")
            self.event(db, actor, share["id"], wait_bucket(now - created))
            notices.add(db, share["owner"], share["id"], "decided", actor)
            if thread:
                asker = db.execute(
                    "SELECT guest FROM shared_memberships WHERE id=?", (thread["membership"],)
                ).fetchone()
                notices.add(db, asker and asker["guest"], share["id"], "answer", actor)
            return {
                "status": "answered",
                "question_id": data["question_id"],
                "item_id": item_id,
                "publication_revision": revision,
                "authority": "partner_decision",
                "permission_grant": False,
            }

    def remember(self, db, share, question, answer):
        payload = canonical_payload(
            "answer", {"question": question, "answer": answer, "scope": share["project"]}
        )
        records = db.execute(
            "SELECT * FROM records WHERE account=? AND kind='answer' AND payload!='null'",
            (share["owner"],),
        ).fetchall()
        same = []
        for row in records:
            existing = json.loads(row["payload"])
            if normalize_scope(existing["scope"]) == share["project"] and (
                normalized(existing["question"]) == normalized(question)
            ):
                if existing["answer"] != payload["answer"]:
                    fail(
                        409,
                        "answer_conflict",
                        "A different private answer exists. Correct it explicitly.",
                    )
                same.append(row["id"])
        if same:
            return same[0]
        record_id = identifier()
        self.store.push(
            share["owner"],
            [{"id": record_id, "kind": "answer", "base_version": 0, "payload": payload}],
            transaction=db,
        )
        return record_id

    def reply(self, actor, thread_id, data, key, *, declined=False):
        clean(data)
        with self.transaction() as db:
            thread = db.execute("SELECT * FROM shared_threads WHERE id=?", (thread_id,)).fetchone()
            if not thread:
                fail()
            share = self.owner(db, actor, thread["share"], serving=True)
            self.enabled()
            member = db.execute(
                "SELECT * FROM shared_memberships WHERE id=?", (thread["membership"],)
            ).fetchone()
            if not member or member["state"] != "active" or share["status"] != "active":
                fail(503, "access_unavailable", "Recipient access has ended or is paused")
            self.suppress(db, share)

            def action():
                if (
                    thread["status"] != "open"
                    or thread["version"] != data["expected_thread_version"]
                ):
                    fail(409, "thread_changed", "This question has already changed")
                request = self.open(thread["payload"])
                answer = data.get("explanation", "") if declined else data["answer"]
                remembered, published = None, None
                if not declined and data["remember_project"]:
                    remembered = self.remember(db, share, request["question"], answer)
                if not declined and data["publish_to_share"]:
                    if share["revision"] != data["expected_publication_revision"]:
                        fail(409, "brief_changed", "The publication changed. Reload it.")
                    _, published = self.publish_answer(
                        db, share, request["question"], answer, "owner_shared_answer"
                    )
                reply_id = identifier()
                state = "declined" if declined else "answered"
                db.execute(
                    "INSERT INTO shared_replies VALUES(?,?,?,?,?,?)",
                    (
                        reply_id,
                        thread_id,
                        self.seal(
                            {
                                "answer": answer,
                                "authority": "owner_reply",
                                "permission_grant": False,
                            }
                        ),
                        remembered,
                        published,
                        time.time(),
                    ),
                )
                db.execute(
                    "UPDATE shared_threads SET status=?,version=version+1,updated=? WHERE id=?",
                    (state, time.time(), thread_id),
                )
                self.event(db, actor, share["id"], "question_" + state)
                self.event(db, actor, share["id"], wait_bucket(time.time() - thread["created"]))
                notices.add(db, member["guest"], share["id"], "answer", actor)
                return {
                    "thread_id": thread_id,
                    "thread_version": thread["version"] + 1,
                    "state": state,
                    "reply_id": reply_id,
                    "remembered_record_id": remembered,
                    "published_revision": published,
                }

            return self.idempotent(
                db, actor, ("decline:" if declined else "reply:") + thread_id, key, data, action
            )

    def owner_detail(self, actor, share_id):
        from .sharing_context import overview, read

        with self.transaction() as db:
            share = self.owner(db, actor, share_id)
            self.suppress(db, share)
            members = db.execute(
                "SELECT m.id,m.state,m.revision,m.level,m.fill_gaps,a.username "
                "FROM shared_memberships m "
                "JOIN accounts a ON a.id=m.guest WHERE share=? ORDER BY m.id LIMIT 20",
                (share_id,),
            ).fetchall()
            invites = db.execute(
                "SELECT id,state,expires,email FROM shared_invites WHERE share=? "
                "ORDER BY created DESC LIMIT 20",
                (share_id,),
            ).fetchall()
            pending = db.execute(
                "SELECT COUNT(*) AS n FROM shared_selections s "
                "JOIN shared_projects p ON p.id=s.share "
                "JOIN records r ON r.account=p.owner AND r.kind=s.kind AND r.id=s.source_id "
                "WHERE s.share=? AND s.suppressed IS NULL AND s.origin='owner_confirmed_record' "
                "AND r.version<>s.source_version",
                (share_id,),
            ).fetchone()["n"]
            return {
                **share,
                "repo": self.repo(share),
                "collaboration": self.share_digest(db, actor, share, mark=False),
                "pending_updates": pending,
                "items": self.publication(db, share),
                "project_context": overview(self, db, share),
                "context_draft": read(self, db, share_id, "draft"),
                "context_published": read(self, db, share_id),
                "members": [dict(m) for m in members],
                "invites": [
                    {
                        "id": i["id"],
                        "state": i["state"],
                        "expires": i["expires"],
                        "email": self.open(i["email"]),
                    }
                    for i in invites
                ],
            }

    def retract_answer(self, actor, share_id, data, key):
        with self.transaction() as db:
            share = self.owner(db, actor, share_id)
            self.suppress(db, share)

            def action():
                if share["revision"] != data["expected_publication_revision"]:
                    fail(409, "brief_changed", "The publication changed. Review it again.")
                revision = self.retract_item(
                    db, share, data["item_id"], ("owner_shared_answer", "partner_decision")
                )
                if revision is None:
                    fail()
                self.event(db, actor, share_id, "answer_retracted")
                return {"share_id": share_id, "publication_revision": revision}

            return self.idempotent(db, actor, "retract_answer:" + share_id, key, data, action)

    def lifecycle(self, actor, share_id, data, key):
        with self.transaction() as db:
            share = self.owner(db, actor, share_id)

            def action():
                if share["revision"] != data["expected_publication_revision"]:
                    fail(409, "brief_changed", "The publication changed. Reload it.")
                if data["status"] == "active":
                    self.enabled()
                    self.entitlement(db, actor)
                db.execute(
                    "UPDATE shared_projects SET status=?,updated=? WHERE id=?",
                    (data["status"], time.time(), share_id),
                )
                self.event(db, actor, share_id, data["status"])
                if data["status"] == "deleted":
                    # Explicit erasure; remembered owner knowledge is retained independently.
                    db.execute("DELETE FROM shared_projects WHERE id=?", (share_id,))
                    db.execute("DELETE FROM shared_previews WHERE share=?", (share_id,))
                return {"share_id": share_id, "status": data["status"]}

            return self.idempotent(db, actor, "state:" + share_id, key, data, action)

    def end_member(self, actor, membership, data, key, *, leave=False):
        with self.transaction() as db:
            member = db.execute(
                "SELECT * FROM shared_memberships WHERE id=?", (membership,)
            ).fetchone()
            if not member:
                fail()
            if leave:
                if member["guest"] != actor:
                    fail()
            else:
                self.owner(db, actor, member["share"])

            def action():
                if data["expected_revision"] != member["revision"]:
                    fail(409, "member_changed", "Membership changed. Reload it.")
                return self.end_membership(db, actor, dict(member), "left" if leave else "revoked")

            return self.idempotent(db, actor, "end:" + membership, key, data, action)

    def end_membership(self, db, actor, member, state):
        db.execute(
            "UPDATE shared_memberships SET state=?,revision=revision+1,ended=? WHERE id=?",
            (state, time.time(), member["id"]),
        )
        db.execute(
            "UPDATE shared_threads SET status='access_ended',version=version+1,updated=? "
            "WHERE membership=? AND status='open'",
            (time.time(), member["id"]),
        )
        db.execute("DELETE FROM shared_task_bindings WHERE membership=?", (member["id"],))
        db.execute(
            "UPDATE shared_invites SET state='revoked' WHERE share=? AND accepted_account=?",
            (member["share"], member["guest"]),
        )
        self.event(db, actor, member["share"], state)
        return {"membership_id": member["id"], "state": state, "revision": member["revision"] + 1}

    def revoke_invite(self, actor, invite_id, key):
        with self.transaction() as db:
            invite = db.execute("SELECT * FROM shared_invites WHERE id=?", (invite_id,)).fetchone()
            if not invite:
                fail()
            self.owner(db, actor, invite["share"])

            def action():
                if invite["state"] == "accepted":
                    fail(
                        409,
                        "accepted_invite",
                        "Revoke the member rather than an accepted invitation",
                    )
                db.execute("UPDATE shared_invites SET state='revoked' WHERE id=?", (invite_id,))
                self.event(db, actor, invite["share"], "invite_revoked")
                return {"invite_id": invite_id, "state": "revoked"}

            return self.idempotent(db, actor, "revoke_invite:" + invite_id, key, {}, action)

    def withdraw(self, actor, membership, thread_id, data, key):
        with self.transaction() as db:
            member, share = self.member(db, actor, membership)

            def action():
                row = db.execute(
                    "SELECT * FROM shared_threads WHERE id=? AND membership=?",
                    (thread_id, member["id"]),
                ).fetchone()
                if not row:
                    fail()
                if row["status"] != "open" or row["version"] != data["expected_thread_version"]:
                    fail(409, "thread_changed", "Question changed")
                db.execute(
                    (
                        "UPDATE shared_threads SET status='withdrawn',version=version+1,up"
                        "dated=? WHERE id=?"
                    ),
                    (time.time(), thread_id),
                )
                self.event(db, actor, share["id"], "withdrawn")
                return {"thread_id": thread_id, "state": "withdrawn", "version": row["version"] + 1}

            return self.idempotent(db, actor, "withdraw:" + thread_id, key, data, action)

    def deliver_notices(self):
        return notices.deliver(self)

    def purge(self):
        """Bounded content retention, called by the existing background runner."""
        now = time.time()
        with self.transaction() as db:
            db.execute("DELETE FROM shared_previews WHERE expires<?", (now,))
            db.execute("DELETE FROM shared_idempotency WHERE expires<?", (now,))
            db.execute(
                "DELETE FROM shared_connection_checks WHERE checked_at<?", (now - 90 * 86400,)
            )
            db.execute(
                "DELETE FROM shared_threads WHERE status!='open' AND updated<?", (now - 90 * 86400,)
            )
            db.execute("DELETE FROM shared_events WHERE created<?", (now - 90 * 86400,))
            db.execute("DELETE FROM shared_reports WHERE created<?", (now - 90 * 86400,))
            db.execute(
                "DELETE FROM shared_suggestions WHERE state!='open' AND updated<?",
                (now - 90 * 86400,),
            )
            db.execute(
                "DELETE FROM shared_owner_questions WHERE status!='open' AND updated<?",
                (now - 90 * 86400,),
            )
            db.execute("DELETE FROM shared_invites WHERE expires<?", (now - 30 * 86400,))
            # Historical snapshots removed after retention; never the current publication.
            db.execute(
                "DELETE FROM shared_publications WHERE created<? AND revision NOT IN "
                "(SELECT revision FROM shared_projects WHERE id=shared_publications.share)",
                (now - 90 * 86400,),
            )
            self.following.purge(db, now)

    def share_digest(self, db, owner, share, *, mark=True):
        repo = self.repo(share)
        slug = github_slug(repo["url"]) if repo else None
        members, requests = [], []
        for row in db.execute(
            "SELECT m.id,m.level,m.fill_gaps,a.username,x.state AS access,x.payload "
            "FROM shared_memberships m JOIN accounts a ON a.id=m.guest "
            "LEFT JOIN shared_access x ON x.membership=m.id "
            "WHERE m.share=? AND m.state='active' ORDER BY m.created",
            (share["id"],),
        ).fetchall():
            name = row["username"] or "Collaborator"
            members.append(
                {
                    "membership_id": row["id"],
                    "name": name,
                    "level": row["level"] or "consult",
                    "fill_gaps": fills(row["level"], row["fill_gaps"]),
                    "github": row["access"],
                }
            )
            if row["access"] == "requested" and repo:
                user = self.open(row["payload"]).get("github_username")
                request = {"membership_id": row["id"], "name": name, "github_username": user}
                if slug and user:
                    request["command"] = (
                        "gh api -X PUT repos/"
                        + slug
                        + "/collaborators/"
                        + user
                        + " -f permission=push"
                    )
                requests.append(request)
        review = []
        for row in db.execute(
            "SELECT id,kind,payload FROM shared_suggestions WHERE share=? AND state='open' "
            "ORDER BY created LIMIT 10",
            (share["id"],),
        ).fetchall():
            value = self.open(row["payload"])
            value.pop("item_id", None)
            review.append({"review_id": row["id"], "kind": row["kind"], **value})
        invited = []
        for row in db.execute(
            "SELECT id,email,level,expires FROM shared_invites WHERE share=? "
            "AND state='pending' AND expires>? ORDER BY created",
            (share["id"], time.time()),
        ).fetchall():
            account = db.execute(
                "SELECT username FROM accounts WHERE email=?", (self.open(row["email"]),)
            ).fetchone()
            invited.append(
                {
                    "invite_id": row["id"],
                    "name": (account["username"] if account else None) or "Not signed up yet",
                    "level": row["level"] or "consult",
                    "expires_at": row["expires"],
                }
            )
        reports = self.reports(db, share["id"], owner, 3)
        if mark:
            self.mark_seen(db, owner, share["id"], reports)
        return {
            "share_id": share["id"],
            "label": share["label"],
            "project": share["project"],
            "repo": repo,
            "members": members,
            "invited": invited,
            "reports": reports,
            "access_requests": requests,
            "review": review,
            "open_questions": db.execute(
                "SELECT COUNT(*) AS n FROM shared_threads WHERE share=? AND status='open'",
                (share["id"],),
            ).fetchone()["n"],
            "routed_to_partners": db.execute(
                "SELECT COUNT(*) AS n FROM shared_owner_questions WHERE share=? AND status='open'",
                (share["id"],),
            ).fetchone()["n"],
        }

    def owner_digest(self, owner, project=None):
        """Compact counts for the owner's start_task; None when nothing needs attention."""
        try:
            with self.store.connection() as db:
                return self.digest(db, owner, project)
        except Exception:  # noqa: BLE001 - the briefing never fails because of sharing
            return None

    def digest(self, db, owner, project=None):
        if not self.settings.shared_varyz_enabled:
            return None
        shares = self.owner_shares(db, owner, project) or (
            self.owner_shares(db, owner) if project else []
        )
        values = []
        for share in shares[:10]:
            detail = self.share_digest(db, owner, share, mark=False)
            counts = {
                "new_reports": sum(1 for report in detail["reports"] if report["new"]),
                "to_review": len(detail["review"]),
                "access_requests": len(detail["access_requests"]),
                "open_questions": detail["open_questions"],
            }
            if any(counts.values()):
                values.append({"share_id": share["id"], "label": share["label"], **counts})
        if not values:
            return None
        return {
            "shares": values,
            "next": "Call collaboration (action status) for the reports, access requests and "
            "items to review, then put any decisions to the owner as short questions.",
        }

    def owner_share(self, db, owner, data):
        if data.get("share_id"):
            return self.owner(db, owner, data["share_id"], serving=True)
        rows = self.owner_shares(db, owner, data.get("project"))
        if len(rows) != 1:
            fail(
                409 if rows else 404,
                "share_required",
                "Pass share_id: " + ", ".join(r["id"] + " (" + r["label"] + ")" for r in rows)
                if rows
                else "No shared collaboration for this project",
            )
        self.entitlement(db, owner)
        return rows[0]

    def owner_shares(self, db, owner, project=None):
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM shared_projects WHERE owner=? AND status!='deleted' "
                "ORDER BY created",
                (owner,),
            ).fetchall()
        ]
        if project:
            rows = [row for row in rows if row["project"] == normalize_scope(project)]
        return rows

    def owner_member(self, db, owner, membership):
        row = db.execute(
            "SELECT m.* FROM shared_memberships m JOIN shared_projects p ON p.id=m.share "
            "WHERE m.id=? AND p.owner=? AND m.state='active'",
            (membership or "", owner),
        ).fetchone()
        if not row:
            fail()
        return dict(row)

    def collaborate(self, owner, data):
        """Owner-side collaboration for the owner's own agent (status and small actions)."""
        action = data.get("action") or "status"
        for key in ("work", "answer", "question", "reason", "context"):
            if data.get(key):
                clean(data[key])
        if action in ("publish", "public"):
            return self.public(owner, action, data.get("topic"))
        with self.transaction() as db:
            if action == "digest":
                return self.digest(db, owner, data.get("project")) or {"shares": []}
            if action == "status":
                if not self.settings.shared_varyz_enabled:
                    return {"shares": [], "how": OWNER_HOW}
                shares = self.owner_shares(db, owner, data.get("project"))
                return {
                    "shares": [self.share_digest(db, owner, share) for share in shares],
                    "how": OWNER_HOW,
                }
            self.enabled()
            if action in ("link_repo", "report", "ask_partner"):
                share = self.owner_share(db, owner, data)
            if action == "context":
                from .sharing_context import save

                self.entitlement(db, owner)
                share = self.project_share(db, owner, data)
                if data.get("context") is None:
                    fail(
                        422,
                        "invalid_fields",
                        "Pass context, the draft project explanation and story",
                    )
                save(self, db, share, data["context"], state="draft")
                return {
                    "share_id": share["id"],
                    "status": "needs_owner_review",
                    "review_url": self.settings.public_url + "/account/shares/" + share["id"],
                    "how": "The owner reviews the factual explanation and story before sharing. "
                    "Do not invent a vision or treat this draft as approved context.",
                }
            if action == "link_repo":
                if not data.get("repo"):
                    fail(422, "invalid_fields", "Pass repo, the repository URL")
                repo = {"url": data["repo"], "branch": data.get("branch") or ""}
                db.execute(
                    "UPDATE shared_projects SET repo=?,updated=? WHERE id=?",
                    (self.seal(repo), time.time(), share["id"]),
                )
                self.event(db, owner, share["id"], "collab_repo_linked")
                return {"share_id": share["id"], "repo": repo}
            if action == "report":
                if not data.get("work"):
                    fail(422, "invalid_fields", "Pass work, the report")
                return self.post_report(db, owner, share, "owner", data["work"])
            if action == "ask_partner":
                return self.ask_partner(db, owner, share, data)
            if action == "access_granted":
                member = self.owner_member(db, owner, data.get("membership_id"))
                self.owner(db, owner, member["share"], serving=True)
                row = db.execute(
                    "SELECT state FROM shared_access WHERE membership=?", (member["id"],)
                ).fetchone()
                if not row or row["state"] != "requested":
                    fail(409, "no_request", "This collaborator has not requested access")
                db.execute(
                    "UPDATE shared_access SET state='granted',updated=? WHERE membership=?",
                    (time.time(), member["id"]),
                )
                self.event(db, owner, member["share"], "guest_access_granted")
                return {"membership_id": member["id"], "github": "granted"}
            if action == "set_level":
                member = self.owner_member(db, owner, data.get("membership_id"))
                self.owner(db, owner, member["share"], serving=True)
                level = data.get("level")
                if level not in LEVELS:
                    fail(422, "invalid_fields", "Choose consult, contribute or decide")
                gaps = fills(level, data.get("fill_gaps"))
                db.execute(
                    "UPDATE shared_memberships SET level=?,fill_gaps=? WHERE id=?",
                    (level, int(gaps), member["id"]),
                )
                self.event(db, owner, member["share"], "level_set_" + level)
                return {"membership_id": member["id"], "level": level, "fill_gaps": gaps}
            if action == "review":
                return self.review(db, owner, data)
            if action == "add":
                return self.add(db, owner, data)
            if action == "remove":
                return self.remove(db, owner, data)
            fail(422, "unknown_action", "Unknown collaboration action")

    def public(self, owner, action, topic):
        """The owner's Public Varyz: where to choose standards, or what is published."""
        self.enabled()
        with self.store.connection() as db:
            self.following.creator(db, owner)
        if action == "public":
            return self.following.status(owner)
        page = self.settings.public_url + "/account/public"
        if topic:
            from urllib.parse import quote

            from .following import topic_slug

            page += "/topic?name=" + quote(topic_slug(topic))
        return {
            "status": "needs_owner_review",
            "review_url": page,
            "how": "Give the owner this link. They tick the standards for the topic and "
            "review the exact text before anything is public. Do not publish for them.",
        }

    def account_named(self, db, owner, username):
        name = (username or "").lstrip("@").lower()
        if not name:
            fail(422, "invalid_fields", "Pass username, the person's Varyz username")
        row = db.execute("SELECT * FROM accounts WHERE lower(username)=?", (name,)).fetchone()
        if not row or not row["email_verified"]:
            fail(404, "no_account", "No Varyz account has the username " + name)
        if row["id"] == owner:
            fail(422, "own_account", "That username is your own account")
        allowed = self.settings.shared_varyz_accounts
        if allowed and row["username"] not in allowed:
            fail(403, "not_in_pilot", row["username"] + " is not in the Shared Varyz pilot yet")
        return dict(row)

    def project_share(self, db, owner, data):
        """The owner's share for a project, created from its confirmed preferences if new."""
        if data.get("share_id"):
            return self.owner(db, owner, data["share_id"], serving=True)
        label = " ".join((data.get("project") or "").split())[:80]
        project = normalize_scope(label)
        if not label or project == "*":
            fail(422, "project_required", "Pass project, the project to share (not General)")
        rows = self.owner_shares(db, owner, project)
        if len(rows) > 1:
            fail(
                409,
                "share_required",
                "Pass share_id: " + ", ".join(r["id"] + " (" + r["label"] + ")" for r in rows),
            )
        if rows:
            if rows[0]["status"] != "active":
                fail(
                    409,
                    "access_paused",
                    "Sharing is paused for this project. Resume it before inviting someone.",
                )
            return rows[0]
        # General preferences stay private: only this project's confirmed ones are shared.
        items, selections = [], []
        for row in db.execute(
            "SELECT kind,id,version,payload FROM records WHERE account=? "
            "AND kind IN ('rule','answer','workflow') AND payload!='null' ORDER BY kind,id",
            (owner,),
        ).fetchall():
            try:
                if normalize_scope(json.loads(row["payload"])["scope"]) != project:
                    continue
                source = {"kind": row["kind"], "id": row["id"], "version": row["version"]}
                item, scope = self.projection(db, owner, project, source, label=label)
            except (SharingError, ValueError, KeyError, TypeError):
                continue
            items.append(item)
            selections.append({**source, "item": item["item_id"], "scope": scope})
            if len(items) == 500:
                break
        share = {"id": identifier(), "owner": owner, "project": project, "label": label}
        now = time.time()
        db.execute(
            "INSERT INTO shared_projects(id,owner,project,label,status,revision,"
            "created,updated) VALUES(?,?,?,?,?,?,?,?)",
            (share["id"], owner, project, label, "active", 0, now, now),
        )
        share.update(status="active", revision=0, repo=None)
        self.publish_snapshot(db, share, items)
        self.save_selections(db, share["id"], {"selection": selections})
        self.storage_budget(db, owner)
        self.event(db, owner, share["id"], "shared_by_agent")
        return share

    def add(self, db, owner, data):
        self.entitlement(db, owner)
        guest = self.account_named(db, owner, data.get("username"))
        share = self.project_share(db, owner, data)
        level = data.get("level") or "consult"
        gaps = fills(level, data.get("fill_gaps"))
        result = {
            "username": guest["username"],
            "share_id": share["id"],
            "label": share["label"],
            "level": level,
            "fill_gaps": gaps,
            "shared_preferences": len(self.publication(db, share)),
        }
        member = db.execute(
            "SELECT id FROM shared_memberships WHERE share=? AND guest=? AND state='active'",
            (share["id"], guest["id"]),
        ).fetchone()
        if member:
            db.execute(
                "UPDATE shared_memberships SET level=?,fill_gaps=? WHERE id=?",
                (level, int(gaps), member["id"]),
            )
            self.event(db, owner, share["id"], "level_set_" + level)
            return {**result, "status": "level_changed", "membership_id": member["id"]}
        invite = self.invitation(
            db, owner, share["id"], guest["email"], level, data.get("fill_gaps")
        )
        self.event(db, owner, share["id"], "invited_by_username")
        return {
            **result,
            "status": "invited",
            "invite_id": invite["invite_id"],
            "invite_url": invite["invite_url"],
            "expires_at": invite["expires_at"],
            "next": (
                guest["username"]
                + " gets an email and a desktop notification, and joins by asking their "
                "agent (Shared Varyz connected) or at Shared with me in Varyz Cloud. The "
                "invitation lasts seven days."
            ),
        }

    def remove(self, db, owner, data):
        guest = self.account_named(db, owner, data.get("username"))
        share = self.owner_share(db, owner, data)
        member = db.execute(
            "SELECT * FROM shared_memberships WHERE share=? AND guest=? AND state='active'",
            (share["id"], guest["id"]),
        ).fetchone()
        pending = db.execute(
            "UPDATE shared_invites SET state='revoked' WHERE share=? AND email_hash=? "
            "AND state='pending'",
            (share["id"], self.mac(normalize_email(guest["email"]))),
        ).rowcount
        if member:
            self.end_membership(db, owner, dict(member), "revoked")
        elif not pending:
            fail(404, "not_a_member", guest["username"] + " is not in this collaboration")
        else:
            self.event(db, owner, share["id"], "invite_revoked")
        return {"username": guest["username"], "share_id": share["id"], "status": "removed"}

    def ask_partner(self, db, owner, share, data):
        if not data.get("question"):
            fail(422, "invalid_fields", "Pass question")
        found = db.execute(
            "SELECT a.id,a.username FROM shared_memberships m JOIN accounts a ON a.id=m.guest "
            "WHERE m.share=? AND m.state='active' AND m.level='decide'",
            (share["id"],),
        ).fetchall()
        partners = [row["username"] or "Partner" for row in found]
        if not partners:
            fail(409, "no_partner", "No Decide partner in this collaboration; ask the owner later")
        fingerprint = self.mac(share["id"] + ":" + normalized(data["question"]))
        existing = db.execute(
            "SELECT id FROM shared_owner_questions WHERE share=? AND fingerprint=? "
            "AND status='open'",
            (share["id"], fingerprint),
        ).fetchone()
        if existing:
            return {"status": "routed", "question_id": existing["id"], "partners": partners}
        if (
            db.execute(
                "SELECT COUNT(*) AS n FROM shared_owner_questions WHERE share=? AND status='open'",
                (share["id"],),
            ).fetchone()["n"]
            >= 20
        ):
            fail(429, "question_limit", "Twenty questions already wait for partners")
        question_id, now = identifier(), time.time()
        db.execute(
            "INSERT INTO shared_owner_questions VALUES(?,?,?,?,?,?,?)",
            (
                question_id,
                share["id"],
                self.seal({"question": data["question"], "reason": data.get("reason", "")}),
                fingerprint,
                "open",
                now,
                now,
            ),
        )
        self.event(db, owner, share["id"], "owner_question_routed")
        for row in found:
            notices.add(db, row["id"], share["id"], "decide", owner)
        return {"status": "routed", "question_id": question_id, "partners": partners}

    def review(self, db, owner, data):
        row = db.execute(
            "SELECT s.* FROM shared_suggestions s JOIN shared_projects p ON p.id=s.share "
            "WHERE s.id=? AND p.owner=? AND p.status!='deleted'",
            (data.get("review_id") or "", owner),
        ).fetchone()
        if not row:
            fail()
        share = self.owner(db, owner, row["share"], serving=True)
        decision = data.get("decision")
        allowed = ("adopt", "dismiss") if row["kind"] == "disagreement" else ("keep", "overrule")
        if decision not in allowed:
            fail(422, "invalid_fields", "Choose " + " or ".join(allowed))
        if row["state"] != "open":
            fail(409, "already_reviewed", "Already reviewed")
        value = self.open(row["payload"])
        result: dict[str, Any] = {"review_id": row["id"], "state": REVIEW_STATES[decision]}
        if decision == "overrule":
            if row["kind"] == "partner_decision":
                self.retract_item(db, share, value.get("item_id", ""), ("partner_decision",))
            if data.get("answer"):
                _, result["publication_revision"] = self.publish_answer(
                    db, share, value["question"], data["answer"], "owner_shared_answer"
                )
        elif decision == "adopt":
            result["next"] = (
                "Ask the owner how to word the changed preference, record it with "
                "remember_owner_rule (replaces the old rule), then publish the update from "
                "the share page."
            )
        db.execute(
            "UPDATE shared_suggestions SET state=?,updated=? WHERE id=?",
            (REVIEW_STATES[decision], time.time(), row["id"]),
        )
        self.event(db, owner, share["id"], row["kind"] + "_" + REVIEW_STATES[decision])
        result["publication_revision"] = result.get("publication_revision", share["revision"])
        return result

    def metrics(self):
        """Counts only, for deciding later about Decide pricing and a hosted git remote."""
        with self.store.connection() as db:
            events = {
                row["kind"]: row["n"]
                for row in db.execute(
                    "SELECT kind,COUNT(*) AS n FROM shared_events GROUP BY kind"
                ).fetchall()
            }
            access = {
                row["state"]: row["n"]
                for row in db.execute(
                    "SELECT state,COUNT(*) AS n FROM shared_access GROUP BY state"
                ).fetchall()
            }
            public = self.following.metrics(db)
        checked = sum(access.values())
        stalled = checked - access.get("accepted", 0)
        return {
            "public": public,
            "events": events,
            "github_access": access,
            "github_stall_rate": round(stalled / checked, 2) if checked else None,
            "hosted_remote_signal": bool(checked >= 10 and stalled / checked >= 0.3),
        }
