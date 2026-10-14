"""Tell people about Shared Varyz activity by email and on their desktop.

A notice says who did what in which project, never what was asked, answered or
shared. Email groups pending activity and carries a one-click unsubscribe. Local
Varyz polls the same notices for desktop notifications and marks them shown.
"""

import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlencode

# What each notice says, and the Cloud page that shows it.
KINDS = {
    "invitation": ("{actor} invited you to {project}", "/account/shared"),
    "question": ("{actor} has a question about {project}", "/account/questions"),
    "decide": ("{actor} needs your decision on {project}", "/account/shared"),
    "answer": ("{actor} answered your question about {project}", "/account/shared"),
    "decided": ("{actor} decided a question about {project}", "/account/shares"),
    "access": ("{actor} asked for GitHub access to {project}", "/account/shares"),
    "report": ("{actor} reported work on {project}", "/account/shares"),
}
KEEP = 30 * 86400.0
DESKTOP_WINDOW = 7 * 86400.0
PER_RUN = 50
INVITATION_LEASE = 300.0


def mailable(settings):
    return bool(settings.email_sender or (settings.smtp_host and settings.smtp_from))


def send(settings, message):
    if settings.email_sender:
        settings.email_sender(message)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def plain(value, fallback):
    return " ".join(str(value or "").split())[:80] or fallback


def add(db, account, share, kind, actor, detail=None):
    from .sharing_service import identifier

    if account and account != actor:
        db.execute(
            "INSERT INTO shared_notices(id,account,share,actor,kind,detail,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (identifier(), account, share, actor, kind, detail, time.time()),
        )


def line(row):
    text = KINDS[row["kind"]][0].format(
        actor=plain(row["actor_name"], "Someone"), project=plain(row["label"], "a project")
    )
    if row["kind"] == "invitation" and row["detail"]:
        text += " at " + row["detail"].capitalize()
    return text


def rows(db, where, params):
    return db.execute(
        "SELECT n.id,n.account,n.kind,n.detail,n.created,n.share,p.label,"
        "a.username AS actor_name FROM shared_notices n JOIN shared_projects p ON p.id=n.share "
        "LEFT JOIN accounts a ON a.id=n.actor WHERE p.status='active' AND " + where,
        params,
    ).fetchall()


def link(settings, kind):
    return settings.public_url + KINDS[kind][1]


def unsubscribe_url(service, account):
    token = service.mac("notices-off:" + account)
    return service.settings.public_url + "/notices/email?" + urlencode({"a": account, "t": token})


def compose(service, account, items):
    settings = service.settings
    lines = [line(row) for row in items]
    message = EmailMessage()
    message["From"] = settings.smtp_from or "test@example.test"
    message["To"] = account["email"]
    message["Subject"] = (
        lines[0] if len(lines) == 1 else str(len(lines)) + " updates in your shared Varyz projects"
    )
    off = unsubscribe_url(service, account["id"])
    message["List-Unsubscribe"] = "<" + off + ">"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    pages = {link(settings, row["kind"]) for row in items}
    where = pages.pop() if len(pages) == 1 else settings.public_url + "/account"
    message.set_content(
        "Hi "
        + plain(account["username"], "there")
        + ",\n\n"
        + "\n".join("- " + text + "." for text in lines)
        + "\n\nAsk your AI agent about your shared projects (it handles invitations and "
        "questions for you), or open "
        + where
        + "\n\nVaryz never includes what was asked or shared.\nStop these emails: " + off + "\n"
    )
    return message


def collect(service):
    """Pick the emails due now and mark their notices sent, in one transaction."""
    now = time.time()
    due = []
    with service.transaction() as db:
        db.execute("DELETE FROM shared_notices WHERE created<?", (now - KEEP,))
        accounts = db.execute(
            "SELECT DISTINCT n.account FROM shared_notices n "
            "WHERE n.emailed IS NULL LIMIT ?",
            (PER_RUN,),
        ).fetchall()
        for row in accounts:
            account = db.execute(
                "SELECT a.id,a.email,a.username,a.email_verified,s.email_off "
                "FROM accounts a LEFT JOIN shared_notice_settings s ON s.account=a.id "
                "WHERE a.id=?",
                (row["account"],),
            ).fetchone()
            items = rows(db, "n.account=? AND n.emailed IS NULL", (row["account"],))
            db.execute(
                "UPDATE shared_notices SET emailed=? WHERE account=? AND emailed IS NULL",
                (now, row["account"]),
            )
            if not account or account["email_off"] or not account["email_verified"] or not items:
                continue
            db.execute(
                "INSERT INTO shared_notice_settings(account,email_off,last_email) VALUES(?,0,?) "
                "ON CONFLICT(account) DO UPDATE SET last_email=excluded.last_email",
                (account["id"], now),
            )
            due.append(compose(service, dict(account), items))
    return due


def deliver(service):
    """Send due notice emails. Returns how many went out; never raises for delivery."""
    settings = service.settings
    if not settings.shared_varyz_enabled or not mailable(settings):
        return 0
    sent = deliver_invitations(service)
    for message in collect(service):
        try:
            send(settings, message)
            sent += 1
        except (OSError, smtplib.SMTPException):
            # Desktop and the agent still show the notice; never log the address.
            continue
    return sent


def deliver_invitations(service):
    """Deliver invitations for people without accounts; retain failures for retry."""
    sent = 0
    with service.store.connection() as db:
        pending = db.execute(
            "SELECT q.invite,q.payload,i.email,p.label,a.username AS actor_name "
            "FROM shared_invitation_mail q JOIN shared_invites i ON i.id=q.invite "
            "JOIN shared_projects p ON p.id=i.share JOIN accounts a ON a.id=p.owner "
            "WHERE (q.sent IS NULL OR (q.sent<0 AND q.sent>=?)) "
            "AND i.state='pending' AND i.expires>? "
            "AND p.status='active' LIMIT ?",
            (-time.time(), time.time(), PER_RUN),
        ).fetchall()
    for row in pending:
        # A negative deadline reserves this outbox row across workers. An expired
        # lease is retryable after a process crash; a positive value means sent.
        now = time.time()
        lease = -(now + INVITATION_LEASE)
        with service.transaction() as db:
            claimed = db.execute(
                "UPDATE shared_invitation_mail SET sent=? WHERE invite=? "
                "AND (sent IS NULL OR (sent<0 AND sent>=?)) "
                "AND EXISTS(SELECT 1 FROM shared_invites i JOIN shared_projects p ON p.id=i.share "
                "WHERE i.id=invite AND i.state='pending' AND i.expires>? AND p.status='active')",
                (lease, row["invite"], -now, now),
            ).rowcount
        if not claimed:
            continue

        def release(invite=row["invite"], claim=lease):
            with service.transaction() as db:
                db.execute(
                    "UPDATE shared_invitation_mail SET sent=NULL WHERE invite=? AND sent=?",
                    (invite, claim),
                )

        email = service.open(row["email"])
        message = EmailMessage()
        message["From"] = service.settings.smtp_from or "test@example.test"
        message["To"] = email
        message["Subject"] = (
            plain(row["actor_name"], "Someone")
            + " invited you to "
            + plain(row["label"], "a project")
        )
        message.set_content(
            message["Subject"] + ".\n\nCreate a free Varyz account with this email address "
            "to join the project:\n"
            + service.open(row["payload"])["url"]
            + "\n\nThis invitation expires in seven days. "
            "If you weren't expecting it, you can ignore it.\n"
        )
        try:
            send(service.settings, message)
        except (OSError, smtplib.SMTPException):
            release()
            continue
        with service.transaction() as db:
            db.execute(
                "UPDATE shared_invitation_mail SET sent=? WHERE invite=? AND sent=?",
                (time.time(), row["invite"], lease),
            )
        sent += 1
    return sent


def desktop(service, account):
    with service.store.connection() as db:
        items = rows(
            db,
            "n.account=? AND n.shown IS NULL AND n.created>? ORDER BY n.created LIMIT 20",
            (account, time.time() - DESKTOP_WINDOW),
        )
        setting = db.execute(
            "SELECT email_off FROM shared_notice_settings WHERE account=?", (account,)
        ).fetchone()
    return {
        "notices": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "text": line(row),
                "url": link(service.settings, row["kind"]),
                "created": row["created"],
            }
            for row in items
        ],
        "email": not (setting and setting["email_off"]),
    }


def shown(service, account, ids):
    with service.transaction() as db:
        for notice in ids[:50]:
            db.execute(
                "UPDATE shared_notices SET shown=? WHERE id=? AND account=? AND shown IS NULL",
                (time.time(), notice, account),
            )
    return {"shown": len(ids[:50])}


def set_email(service, account, on):
    with service.transaction() as db:
        db.execute(
            "INSERT INTO shared_notice_settings(account,email_off) VALUES(?,?) "
            "ON CONFLICT(account) DO UPDATE SET email_off=excluded.email_off",
            (account, 0 if on else 1),
        )
    return {"email": bool(on)}


def test_email(service, account):
    """Send the account one sample email now, so its owner can see delivery works."""
    settings = service.settings
    if not mailable(settings):
        return {"email": "not_configured"}
    with service.store.connection() as db:
        row = db.execute(
            "SELECT id,email,username,email_verified FROM accounts WHERE id=?", (account,)
        ).fetchone()
    if not row or not row["email_verified"]:
        return {"email": "unverified"}
    sample = {
        "kind": "question",
        "actor_name": "Varyz",
        "label": "this test",
        "detail": None,
    }
    message = compose(service, dict(row), [sample])
    message.replace_header("Subject", "Varyz notifications work")
    try:
        send(settings, message)
    except (OSError, smtplib.SMTPException):
        return {"email": "failed"}
    return {"email": "sent"}
