"""Varyz Creator Program: referral links, free Cloud months, commission ledger, portal.

Money facts come only from signed Paddle webhooks. A referral binds once, when a new
account is created, and then follows that account's paid subscription and renewals.
Nothing here moves money: the owner pays creators by hand and records each payout.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
import smtplib
import ssl
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse

from .browser import delete_cookie, read_cookie, set_cookie
from .creator_policy import (
    ATTRIBUTION_DAYS,
    PAYOUT_METHODS,
    TERMS_VERSION,
    LedgerRow,
    commission_minor,
    grant_end,
    hold_until,
    payout_details,
    payout_hint,
)
from .store import digest

DAY = 86400
CODE = re.compile(r"[a-z0-9_]{3,30}")
AUDIENCE_GRANTS_PER_DAY = 100
ZERO_DECIMAL = {"JPY", "KRW"}
RETURN_PATHS = ("/creators", "/admin")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS creator_code_aliases(code TEXT PRIMARY KEY,"
    "account TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS creator_profiles(account TEXT PRIMARY KEY,"
    "code TEXT UNIQUE NOT NULL,status TEXT NOT NULL,name TEXT,channel TEXT,audience TEXT,"
    "plan TEXT,terms_version TEXT,payout_method TEXT,payout_address TEXT,"
    "applied_at DOUBLE PRECISION,decided_at DOUBLE PRECISION)",
    "CREATE TABLE IF NOT EXISTS creator_clicks(code TEXT NOT NULL,day TEXT NOT NULL,"
    "clicks BIGINT NOT NULL,PRIMARY KEY(code,day))",
    "CREATE TABLE IF NOT EXISTS creator_referrals(account TEXT PRIMARY KEY,"
    "creator TEXT NOT NULL,attributed_at DOUBLE PRECISION NOT NULL)",
    "CREATE INDEX IF NOT EXISTS creator_referrals_creator ON creator_referrals(creator)",
    "CREATE TABLE IF NOT EXISTS creator_pending(email TEXT PRIMARY KEY,code TEXT NOT NULL,"
    "created DOUBLE PRECISION NOT NULL)",
    "CREATE TABLE IF NOT EXISTS account_created(account TEXT PRIMARY KEY,"
    "at DOUBLE PRECISION NOT NULL)",
    "CREATE TABLE IF NOT EXISTS billing_revenue(id TEXT PRIMARY KEY,account TEXT,"
    "currency TEXT NOT NULL,completed_at DOUBLE PRECISION NOT NULL,net BIGINT NOT NULL,"
    "tax BIGINT NOT NULL,total BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS creator_commissions(transaction_id TEXT PRIMARY KEY,"
    "creator TEXT NOT NULL,account TEXT,currency TEXT NOT NULL,"
    "completed_at DOUBLE PRECISION NOT NULL,net BIGINT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS creator_commissions_creator ON creator_commissions(creator)",
    "CREATE TABLE IF NOT EXISTS billing_adjustments(id TEXT PRIMARY KEY,"
    "transaction_id TEXT NOT NULL,action TEXT NOT NULL,status TEXT NOT NULL,"
    "amount BIGINT NOT NULL,currency TEXT,resolved INTEGER NOT NULL DEFAULT 0,"
    "updated DOUBLE PRECISION NOT NULL)",
    "CREATE INDEX IF NOT EXISTS billing_adjustments_transaction "
    "ON billing_adjustments(transaction_id)",
    "CREATE TABLE IF NOT EXISTS creator_payouts(id TEXT PRIMARY KEY,creator TEXT NOT NULL,"
    "currency TEXT NOT NULL,as_of DOUBLE PRECISION NOT NULL,amount BIGINT NOT NULL,"
    "method TEXT,address TEXT,reference TEXT NOT NULL,"
    "recorded_at DOUBLE PRECISION NOT NULL)",
    "CREATE TABLE IF NOT EXISTS creator_payout_lines(payout TEXT NOT NULL,"
    "transaction_id TEXT NOT NULL,amount BIGINT NOT NULL,PRIMARY KEY(payout,transaction_id))",
    "CREATE TABLE IF NOT EXISTS program_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS creator_invitations(email TEXT PRIMARY KEY,"
    "code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,"
    "account TEXT,accepted_at DOUBLE PRECISION)",
)


class PayoutVault:
    """Seals payout details with the Cloud encryption key before they reach the database.

    Without a key, email and Revtag details are stored as before, but bank details are
    refused: they are only ever kept encrypted.
    """

    def __init__(self, key: str) -> None:
        self.cipher: Any = None
        if key:
            from cryptography.fernet import Fernet

            self.cipher = Fernet(key.encode())

    def seal(self, details: dict) -> str:
        if details.get("account") and self.cipher is None:
            raise HTTPException(503, "Bank transfer is not available yet; choose another method")
        text = json.dumps(details, sort_keys=True)
        return "sealed:" + self.cipher.encrypt(text.encode()).decode() if self.cipher else text

    def open(self, stored: str | None) -> dict:
        """Saved details as a dict; an older plain email reads as {"email": ...}."""
        if not stored:
            return {}
        if stored.startswith("sealed:"):
            if self.cipher is None:
                return {"note": "Encrypted; the Cloud encryption key is unavailable"}
            stored = self.cipher.decrypt(stored.removeprefix("sealed:").encode()).decode()
        if stored.startswith("{"):
            value = json.loads(stored)
            return value if isinstance(value, dict) else {}
        return {"email": stored}


def ensure_tables(db: Any) -> None:
    for statement in SCHEMA:
        db.execute(statement)


def stamp(value: str) -> float:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Timestamp needs a timezone")
    return moment.timestamp()


def instant(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def day_label(seconds: float) -> str:
    moment = instant(seconds)
    return f"{moment.day} {moment:%b %Y}"


def money(amount: int, currency: str) -> str:
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if currency in ZERO_DECIMAL:
        return f"{sign}{amount:,} {currency}"
    return f"{sign}{amount // 100:,}.{amount % 100:02d} {currency}"


def setting(db: Any, key: str, default: str) -> str:
    row = db.execute("SELECT value FROM program_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def put_setting(db: Any, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO program_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def applications_open(db: Any, settings: Any) -> bool:
    default = "open" if settings.creators_open else "closed"
    return setting(db, "applications", default) == "open"


def invite_creator(db: Any, email: str, code: str, name: str = "") -> None:
    """Reserve an owner-approved code for one email; public codes are not proof."""
    email = email.strip().lower()
    code = code.strip().lower()
    name = " ".join(name.split())
    if db.execute("SELECT 1 FROM creator_code_aliases WHERE code=?", (code,)).fetchone():
        raise HTTPException(409, "That creator code is already assigned")
    if (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
            or not CODE.fullmatch(code) or len(name) > 80):
        raise HTTPException(422, "Enter a valid email, creator code and name")
    profile = db.execute(
        "SELECT p.status FROM creator_profiles p JOIN accounts a ON a.id=p.account "
        "WHERE lower(a.email)=?", (email,),
    ).fetchone()
    if profile and profile["status"] != "applied":
        raise HTTPException(409, "This account already has a creator decision")
    if db.execute(
        "SELECT 1 FROM creator_profiles p LEFT JOIN accounts a ON a.id=p.account "
        "WHERE p.code=? AND (a.email IS NULL OR lower(a.email)<>?)", (code, email),
    ).fetchone():
        raise HTTPException(409, "That creator code is already assigned")
    existing = db.execute(
        "SELECT email,code FROM creator_invitations WHERE email=? OR code=?", (email, code),
    ).fetchone()
    if existing:
        if existing["email"] == email and existing["code"] == code:
            return
        raise HTTPException(409, "That email or code already has an invitation")
    db.execute(
        "INSERT INTO creator_invitations(email,code,name,created) VALUES(?,?,?,?)",
        (email, code, name, time.time()),
    )


def creator_invitation(db: Any, owner: dict) -> Any:
    if not owner.get("email_verified"):
        return None
    return db.execute(
        "SELECT * FROM creator_invitations WHERE email=? AND account IS NULL",
        (owner.get("email", "").strip().lower(),),
    ).fetchone()


# Account lifecycle hooks, called inside the caller's transaction.


def remember_signup_referral(db: Any, email: str, code: str) -> None:
    """Keep a password signup's referral until its email link creates the account."""
    if not code or not CODE.fullmatch(code):
        return
    db.execute("DELETE FROM creator_pending WHERE created<?", (time.time() - 2 * DAY,))
    db.execute(
        "INSERT INTO creator_pending(email,code,created) VALUES(?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET code=excluded.code,created=excluded.created",
        (digest(email), code, time.time()),
    )


def account_created(db: Any, account: str, *, email: str = "", code: str = "") -> None:
    """Note a new account and bind its referral, with the audience's free month."""
    now = time.time()
    db.execute(
        "INSERT INTO account_created(account,at) VALUES(?,?) ON CONFLICT(account) DO NOTHING",
        (account, now),
    )
    if email:
        pending = db.execute(
            "SELECT code FROM creator_pending WHERE email=? AND created>?",
            (digest(email), now - 2 * DAY),
        ).fetchone()
        db.execute("DELETE FROM creator_pending WHERE email=?", (digest(email),))
        code = code or (pending["code"] if pending else "")
    if not code or not CODE.fullmatch(code):
        return
    creator = db.execute(
        "SELECT account FROM creator_profiles WHERE status='approved' AND "
        "(code=? OR account IN (SELECT account FROM creator_code_aliases WHERE code=?))",
        (code, code),
    ).fetchone()
    if not creator or creator["account"] == account:
        return
    db.execute(
        "INSERT INTO creator_referrals(account,creator,attributed_at) VALUES(?,?,?) "
        "ON CONFLICT(account) DO NOTHING",
        (account, creator["account"], now),
    )
    recent = db.execute(
        "SELECT COUNT(*) AS n FROM cloud_grants WHERE creator=? AND role='audience' "
        "AND started_at>?",
        (creator["account"], now - DAY),
    ).fetchone()["n"]
    if recent < AUDIENCE_GRANTS_PER_DAY:
        give_grant(db, account, "audience", creator["account"], now)


def give_grant(db: Any, account: str, role: str, creator: str, now: float) -> bool:
    """One free-access grant per account, ever; creator and audience offers never stack."""
    ends = grant_end(role, instant(now)).timestamp()
    cursor = db.execute(
        "INSERT INTO cloud_grants(account,role,creator,started_at,ends_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(account) DO NOTHING",
        (account, role, creator, now, ends),
    )
    return cursor.rowcount > 0


def forget_account(db: Any, account: str) -> None:
    """Account deletion: drop personal program data, keep the money ledger anonymous."""
    db.execute(
        "DELETE FROM creator_invitations WHERE account=? OR email IN "
        "(SELECT lower(email) FROM accounts WHERE id=?)", (account, account),
    )
    for table in ("creator_referrals", "account_created", "cloud_grants"):
        db.execute(f"DELETE FROM {table} WHERE account=?", (account,))
    db.execute("UPDATE billing_revenue SET account=NULL WHERE account=?", (account,))
    db.execute("UPDATE creator_commissions SET account=NULL WHERE account=?", (account,))
    db.execute(
        "UPDATE creator_profiles SET status='closed',name='',channel='',audience='',plan='',"
        "payout_method='',payout_address='' WHERE account=?",
        (account,),
    )


# Signed Paddle events, called inside the webhook's transaction after the core ledger.


def _minor(value: Any) -> int:
    amount = int(str(value))
    if amount < 0:
        raise ValueError("Negative amount")
    return amount


def record_transaction(db: Any, data: dict, occurred: str, prices: tuple[str, ...]) -> None:
    payment = db.execute(
        "SELECT account,completed_at,currency FROM billing_payments WHERE id=?", (data["id"],)
    ).fetchone()
    if not payment:
        return  # Not a verified Cloud payment the core ledger accepted.
    details = data.get("details") or {}
    totals = details.get("totals") or {}
    lines = [
        line for line in details.get("line_items") or []
        if isinstance(line, dict) and line.get("price_id") in prices
    ]
    if lines:
        net = sum(
            _minor(line["totals"]["subtotal"]) - _minor(line["totals"].get("discount", 0))
            for line in lines
        )
        tax = sum(_minor(line["totals"].get("tax", 0)) for line in lines)
    else:
        net = _minor(totals.get("subtotal", 0)) - _minor(totals.get("discount", 0))
        tax = _minor(totals.get("tax", 0))
    net = max(net, 0)
    currency = str(data.get("currency_code") or payment["currency"] or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        return  # Paddle always sends one; never block the access ledger over reporting.
    completed = payment["completed_at"] or stamp(occurred)
    db.execute(
        "INSERT INTO billing_revenue(id,account,currency,completed_at,net,tax,total) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (data["id"], payment["account"], currency, completed, net,
         tax, _minor(totals.get("grand_total", 0))),
    )
    referral = db.execute(
        "SELECT r.creator FROM creator_referrals r JOIN creator_profiles p "
        "ON p.account=r.creator WHERE r.account=? AND p.status IN ('approved','suspended')",
        (payment["account"],),
    ).fetchone()
    if referral and net > 0 and referral["creator"] != payment["account"]:
        db.execute(
            "INSERT INTO creator_commissions(transaction_id,creator,account,currency,"
            "completed_at,net) VALUES(?,?,?,?,?,?) ON CONFLICT(transaction_id) DO NOTHING",
            (data["id"], referral["creator"], payment["account"], currency, completed, net),
        )


ADJUSTMENT_ACTIONS = ("refund", "chargeback", "chargeback_reverse", "chargeback_warning",
                      "credit")


def record_adjustment(db: Any, data: dict) -> None:
    """Keep every refund and chargeback with its latest status; amounts exclude tax."""
    action = data.get("action")
    if action not in ADJUSTMENT_ACTIONS or not data.get("transaction_id"):
        return
    totals = data.get("totals") or {}
    if "subtotal" in totals:
        amount = _minor(totals["subtotal"])
    else:
        amount = max(_minor(totals.get("total", 0)) - _minor(totals.get("tax", 0)), 0)
    db.execute(
        "INSERT INTO billing_adjustments(id,transaction_id,action,status,amount,currency,"
        "updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "status=excluded.status,amount=excluded.amount,updated=excluded.updated",
        (data["id"], data["transaction_id"], action, str(data.get("status", "")),
         amount, data.get("currency_code"), time.time()),
    )


# Ledger views.


def adjustment_state(db: Any, transaction_ids: list[str]) -> dict[str, tuple[int, bool]]:
    """Per transaction: approved refunded amount and whether anything is still open."""
    state: dict[str, tuple[int, bool]] = {}
    for start in range(0, len(transaction_ids), 500):
        chunk = transaction_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in db.execute(
            f"SELECT transaction_id,action,status,amount,resolved FROM billing_adjustments "
            f"WHERE transaction_id IN ({marks})",
            chunk,
        ):
            refunded, open_ = state.get(row["transaction_id"], (0, False))
            approved = row["status"] == "approved"
            if row["action"] in ("refund", "chargeback", "credit") and approved:
                refunded += row["amount"]
            elif row["action"] == "chargeback_reverse" and approved:
                refunded -= row["amount"]
            if (row["action"] in ("refund", "credit") and row["status"] == "pending_approval") or (
                row["action"] == "chargeback_warning" and not row["resolved"]
            ):
                open_ = True
            state[row["transaction_id"]] = (refunded, open_)
    return state


def ledger_rows(db: Any, creator: str | None = None) -> list[dict]:
    query = (
        "SELECT c.*,p.status AS creator_status FROM creator_commissions c "
        "LEFT JOIN creator_profiles p ON p.account=c.creator"
    )
    rows = [
        dict(r) for r in db.execute(
            query + (" WHERE c.creator=?" if creator else "") + " ORDER BY c.completed_at DESC",
            (creator,) if creator else (),
        )
    ]
    ids = [r["transaction_id"] for r in rows]
    state = adjustment_state(db, ids)
    paid: dict[str, int] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for line in db.execute(
            f"SELECT transaction_id,SUM(amount) AS amount FROM creator_payout_lines "
            f"WHERE transaction_id IN ({marks}) GROUP BY transaction_id",
            chunk,
        ):
            paid[line["transaction_id"]] = int(line["amount"])
    for row in rows:
        refunded, open_ = state.get(row["transaction_id"], (0, False))
        row["refunded"] = min(max(refunded, 0), row["net"])
        row["open"] = open_
        row["paid"] = paid.get(row["transaction_id"], 0)
        row["commission"] = commission_minor(row["net"], row["refunded"])
        row["hold_until"] = hold_until(instant(row["completed_at"])).timestamp()
    return rows


def policy_rows(rows: list[dict]) -> list[LedgerRow]:
    # A suspended or closed creator's batch waits for the owner's decision.
    return [
        LedgerRow(
            transaction_id=r["transaction_id"], creator=r["creator"], currency=r["currency"],
            completed_at=instant(r["completed_at"]), net_revenue=r["net"],
            approved_refund=r["refunded"], paid_commission=r["paid"],
            disputed=r["open"] or r["creator_status"] != "approved",
        )
        for r in rows
    ]


def balances(rows: list[dict], now: float) -> dict[str, dict[str, int]]:
    """Per currency: earned, held, ready for the next payout, and paid so far."""
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        total = result.setdefault(
            row["currency"], {"earned": 0, "held": 0, "ready": 0, "paid": 0}
        )
        owed = row["commission"] - row["paid"]
        total["earned"] += row["commission"]
        total["paid"] += row["paid"]
        if row["open"] or row["hold_until"] > now:
            total["held"] += owed
        else:
            total["ready"] += owed
    return result


def share_link(settings: Any, code: str) -> str:
    if urlparse(settings.public_url).hostname == "cloud.varyzai.com":
        return "https://varyzai.com/r/" + code
    return settings.public_url.rstrip("/") + "/r/" + code


def send_mail(settings: Any, store: Any, to: str, subject: str, body: str) -> None:
    """Best-effort program email within the service's daily email budget."""
    if not to or (not settings.email_sender and (not settings.smtp_host or not settings.smtp_from)):
        return
    if not store.allow_email("global", settings.email_daily_limit):
        return
    message = EmailMessage()
    message["From"] = settings.smtp_from or "test@example.test"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        if settings.email_sender:
            settings.email_sender(message)
            return
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        return


APPROVAL_SUBJECT = "You're in the Varyz Creator Program"
APPROVAL_BODY = """You're in the Varyz Creator Program.

Your creator dashboard has your referral link:
{portal}

Activate your three free months of Cloud, then share the link to give your audience
one month free.

You earn 30% on their paid subscriptions and every renewal. Payouts are monthly,
after each payment clears its 30-day refund hold.
"""

PROGRAM_CSS = """
.cp{max-width:860px}.cp h1{font-family:var(--serif);font-weight:400;font-size:40px;margin:8px 0 6px}
.cp h1 em{color:var(--signal);font-style:italic}
.cp h2{font-family:var(--serif);font-weight:400;font-size:24px;margin:0 0 12px}
.cp-lead{color:var(--muted);font-size:17px;max-width:620px}
.cp-card{border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:18px 0;
  background:var(--glass)}
.cp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;
  margin:14px 0}
.cp-stat{border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.cp-stat b{display:block;font-family:var(--serif);font-weight:400;font-size:28px;color:var(--ink)}
.cp-stat span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.cp-link{display:block;font-family:ui-monospace,Consolas,monospace;font-size:16px;
  padding:12px 14px;border:1px dashed var(--signal);border-radius:10px;color:var(--signal);
  word-break:break-all;user-select:all}
.cp table{width:100%;border-collapse:collapse;font-size:14px}
.cp th,.cp td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
.cp th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;
  letter-spacing:.06em}
.cp form label{display:block;margin:12px 0 4px}.cp textarea{width:100%;min-height:84px}
.cp .cp-check{display:flex;gap:10px;align-items:flex-start}.cp .cp-check input{margin-top:4px}
.cp-note{color:var(--muted);font-size:14px}
.cp-tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;
  border:1px solid var(--line);color:var(--muted)}
.cp-tag.ok{border-color:var(--signal);color:var(--signal)}
.cp-btn{display:inline-block;padding:11px 18px;border-radius:999px;background:var(--signal);
  color:var(--night);font-weight:600;text-decoration:none;margin-right:12px}
.cp-btn:hover{background:#ffd16c}
.cp blockquote{margin:12px 0;padding:10px 16px;border-left:3px solid var(--signal);color:var(--ink)}
.cp td form,.cp .cp-inline{display:inline}
.cp td button,.cp .cp-inline button{width:auto;margin:0 6px 0 0;padding:6px 12px;font-size:13px}
.cp .cp-scroll{overflow-x:auto}
.cp fieldset{border:1px solid var(--line);border-radius:12px;padding:4px 16px 14px;margin:14px 0}
.cp legend{padding:0 6px;color:var(--muted);font-size:14px}
.cp-link-row{display:flex;gap:10px;align-items:center}.cp-link-row .cp-link,
.cp-link-row input{flex:1;min-width:0}.cp-link-row button{width:auto;white-space:nowrap;margin:0}
.cp .cp-link-editor{display:flex;flex-wrap:wrap;align-items:center;flex:1;min-width:0;
 gap:2px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;
 font-family:ui-monospace,Consolas,monospace;font-size:14px;color:var(--muted)}
.cp .cp-link-editor:focus-within{border-color:var(--signal)}
.cp .cp-link-editor input{flex:1;width:10ch;min-width:6ch;max-width:100%;padding:4px 6px;
 margin:0;border:1px solid transparent;border-radius:5px;background:#ffc23d0a;
 font:inherit;color:var(--signal);box-shadow:none}
.cp .cp-link-editor input:hover{background:#ffc23d16;border-color:#ffc23d40}
.cp .cp-link-editor input:focus{outline:none;background:#ffc23d16}
.cp .cp-methods{border:0;padding:0;margin:18px 0;display:flex;flex-wrap:wrap;gap:8px}
.cp .cp-methods legend{padding:0;margin-bottom:10px;color:var(--ink)}
.cp form .cp-choice{display:flex;align-items:center;gap:8px;margin:0;padding:11px 14px;
 border:1px solid var(--line);border-radius:10px;cursor:pointer;font-size:14px}
.cp-choice:has(input:checked){border-color:var(--signal);background:#ffc23d12}
.cp-choice:has(input:focus-visible){outline:2px solid var(--signal);outline-offset:3px}
.cp .cp-choice input{width:16px;height:16px;margin:0;accent-color:var(--signal)}
.cp [hidden]{display:none!important}
.cp-earnings-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.cp .cp-earnings-head h2{margin:0}
.cp-earnings-total{text-align:right;min-width:0}
.cp-earnings-total span{display:block;color:var(--muted);font-size:11px;
 letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px}
.cp-earnings-total strong{display:block;font-family:var(--serif);font-weight:400;
 font-size:30px;line-height:1.25;color:var(--signal);overflow-wrap:anywhere}
.cp-breakdown{margin-top:18px;border-top:1px solid var(--line)}
.cp-breakdown summary{display:flex;justify-content:space-between;align-items:center;
 padding:14px 0 0;cursor:pointer;list-style:none;font-size:14px;color:var(--muted)}
.cp-breakdown summary::-webkit-details-marker{display:none}
.cp-breakdown summary::after{content:'+';font-size:20px;color:var(--signal)}
.cp-breakdown[open] summary::after{content:'−'}
.cp-breakdown summary:hover{color:var(--ink)}
.cp-breakdown summary:focus-visible{outline:2px solid var(--signal);outline-offset:4px}
.cp-history{max-height:420px;overflow:auto;margin-top:16px}
.cp-payment-id{display:block;font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.cp-history th{position:sticky;top:0;background:var(--night)}
@media(max-width:600px){.cp h1{font-size:30px}.cp-card{padding:18px 16px}}
@media(max-width:600px){.cp-referral-form .cp-link-row{flex-wrap:wrap}
 .cp .cp-link-editor{flex-basis:100%;font-size:12px}.cp-referral-form button{margin-left:auto}}
"""

PROGRAM_JS = """
const copy = document.getElementById('cp-copy');
const code = document.getElementById('cp-code-edit');
if (copy && code) {
  const savedCode = code.value, save = document.getElementById('cp-save-code');
  const status = document.getElementById('cp-copy-status');
  function edit() {
    const changed = code.value !== savedCode;
    code.style.minWidth = 'min(100%, ' + Math.max(6, code.value.length + 1) + 'ch)';
    save.hidden = !changed; copy.hidden = changed;
    status.textContent = changed ? 'Save your code to use the new link' : '';
  }
  code.addEventListener('input', edit); edit();
}
if (copy) copy.addEventListener('click', async () => {
  const link = document.getElementById('cp-referral-prefix').textContent + code.value;
  const status = document.getElementById('cp-copy-status');
  try {
    await navigator.clipboard.writeText(link);
    status.textContent = 'Link copied';
  } catch (_) {
    const field = document.createElement('textarea'); field.value = link;
    field.style.position = 'fixed'; field.style.opacity = '0'; document.body.appendChild(field);
    field.select();
    const copied = document.execCommand('copy'); field.remove();
    status.textContent = copied ? 'Link copied' : 'Copy this link: ' + link;
  }
});
const methods = document.querySelector('.cp-methods');
if (methods) {
  const labels = {
    paypal: ['PayPal account email',
      'Use the email linked to your PayPal account. Your account must be able to ' +
      'receive commercial payments in the payout currency.'],
    wise: ['Wise account email',
      'Enable email discoverability in Wise so we can find you. If your account ' +
      'cannot receive this currency, we will ask for your Wise receiving account ' +
      'details.'],
    payoneer: ['Payoneer account email',
      'Email alone does not guarantee a Payoneer transfer. We will confirm your ' +
      'receiving eligibility before the first payout and may need a payment request ' +
      'or receiving account details.'],
    revolut: ['Revolut Revtag',
      'Find your Revtag in Revolut, for example @alexsmith. Receiving availability ' +
      'depends on your account and the payout currency.'],
    bank: ['',
      'Use bank details for an account that can receive the payout currency. ' +
      'International transfers may also require a BIC, recipient address or other ' +
      'bank details; we will contact you if needed.']
  };
  const address = document.getElementById('cp-address');
  let previous;
  function update() {
    const method = methods.querySelector(':checked').value, bank = method === 'bank';
    if (previous && previous !== method) address.value = '';
    previous = method;
    document.querySelector('.cp-handle').hidden = bank;
    document.querySelector('.cp-bank').hidden = !bank;
    address.disabled = bank; address.required = !bank;
    address.type = method === 'revolut' ? 'text' : 'email';
    address.autocomplete = method === 'revolut' ? 'off' : 'email';
    document.getElementById('cp-address-label').textContent = labels[method][0];
    document.getElementById('cp-method-help').textContent = labels[method][1];
    document.querySelectorAll('.cp-bank input').forEach(input => {
      input.disabled = !bank;
      input.required = bank && ['cp-holder', 'cp-account'].includes(input.id);
    });
  }
  methods.addEventListener('change', update); update();
}
"""


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def register_creators(app: Any, settings: Any, store: Any, account: Any, csrf: Any,
                      form: Any, page: Any) -> None:
    secure = settings.public_url.startswith("https://")
    vault = PayoutVault(getattr(settings, "archive_encryption_key", ""))
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")  # Replicas starting together create tables once.
        ensure_tables(db)

    def with_csrf(content: str, request: Request):
        token = read_cookie(request, "csrf")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        request.state.oauth_close_nonce = nonce
        response = page("<style>" + PROGRAM_CSS + "</style>" + content.replace("@CSRF@", token)
                        + f'<script nonce="{nonce}">' + PROGRAM_JS + '</script>')
        set_cookie(response, "csrf", token, httponly=True, secure=secure, samesite="lax",
                   max_age=3600)
        return response

    @app.get("/r/{code}")
    def referral(request: Request, code: str):
        code = code.lower()
        with store.connection() as db:
            creator = db.execute(
                "SELECT name FROM creator_profiles WHERE status='approved' AND "
                "(code=? OR account IN (SELECT account FROM creator_code_aliases WHERE code=?))",
                (code, code),
            ).fetchone() if CODE.fullmatch(code) else None
            if creator and read_cookie(request, "ref") != code:
                db.execute(
                    "INSERT INTO creator_clicks(code,day,clicks) VALUES(?,?,1) "
                    "ON CONFLICT(code,day) DO UPDATE SET clicks=creator_clicks.clicks+1",
                    (code, datetime.now(UTC).date().isoformat()),
                )
        if not creator:
            return RedirectResponse("/signup", status_code=303)
        response = page(
            "<style>" + PROGRAM_CSS + "</style><section class=\"cp\">"
            f"<p class=\"cp-tag ok\">Invited by {esc(creator['name'])}</p>"
            "<h1>Your first month of Varyz Cloud is <em>free</em>.</h1>"
            "<p class=\"cp-lead\">Varyz teaches your AI agents how you work: your rules, "
            "decisions and standards, ready in every tool. Cloud keeps them in sync across "
            "your computers and AI apps.</p>"
            "<div class=\"cp-card\"><p>Create a Varyz account and your free month starts "
            "right away. No card needed, and nothing renews automatically.</p>"
            "<p><a class=\"cp-btn\" href=\"/signup\">Create your account</a></p>"
            "<p class=\"cp-note\">For new accounts only. Already have one? "
            "<a href=\"/login\">Sign in</a>. Learn more at "
            "<a href=\"https://varyzai.com/\">varyzai.com</a>.</p></div></section>",
            title="Varyz Cloud: one month free",
            description="An invitation to try Varyz Cloud free for one month.",
        )
        # Last click wins, for 30 days, in this browser only.
        set_cookie(response, "ref", code, httponly=True, secure=secure, samesite="lax",
                   max_age=ATTRIBUTION_DAYS * DAY)
        return response

    def signed_in(request: Request) -> dict | None:
        try:
            return account(request, browser=True)
        except HTTPException:
            return None

    @app.get("/creators")
    def portal(request: Request):
        owner = signed_in(request)
        if not owner:
            response = page(
                "<style>" + PROGRAM_CSS + "</style><section class=\"cp\">"
                "<h1>Varyz Creator Program</h1><p class=\"cp-lead\">Earn 30% on every paid "
                "Varyz Cloud subscription you refer, including every renewal. Get three months "
                "of Cloud free, and give your audience one month free.</p>"
                "<div class=\"cp-card\"><p>Sign in or create a free Varyz account to apply.</p>"
                "<p><a class=\"cp-btn\" href=\"/signup\">Create an account</a> "
                "<a href=\"/login\">Sign in</a></p></div></section>",
                title="Varyz Creator Program",
            )
            set_cookie(response, "return_to", "/creators", httponly=True, secure=secure,
                       samesite="lax", max_age=3600)
            return response
        now = time.time()
        with store.connection() as db:
            profile = db.execute(
                "SELECT * FROM creator_profiles WHERE account=?", (owner["id"],)
            ).fetchone()
            is_open = applications_open(db, settings)
            invitation = creator_invitation(db, owner)
            if profile and profile["status"] in ("approved", "suspended"):
                return with_csrf(dashboard(db, owner, dict(profile), now, request), request)
        if invitation and (not profile or profile["status"] == "applied"):
            body = (
                '<h1>You are invited to the Varyz Creator Program</h1>'
                f'<p class="cp-lead">Your creator code is <strong>{esc(invitation["code"])}'
                '</strong>. You are already approved. Accept the program terms to join, '
                'then activate your three free months and add your payout details.</p>'
                '<form class="cp-card" method="post" action="/creators/action">'
                '<input type="hidden" name="csrf" value="@CSRF@">'
                '<input type="hidden" name="action" value="accept_invitation">'
                '<label class="cp-check"><input type="checkbox" name="terms" value="yes" '
                'required><span>I accept the <a href="https://varyzai.com/creators#program-terms">'
                'program terms</a> and <a href="https://varyzai.com/privacy">Privacy notice</a>, '
                'and I will disclose my commission and free access when I promote Varyz.'
                '</span></label><p><button>Join the Creator Program</button></p></form>'
            )
        elif profile and profile["status"] == "applied":
            body = (
                "<h1>Application received</h1><p class=\"cp-lead\">Thanks. We review every "
                "application by hand and email you when it is decided.</p>"
            )
        elif profile:
            body = (
                "<h1>Varyz Creator Program</h1><p class=\"cp-lead\">Your application was not "
                "approved this time. Questions? Write to the contact on "
                "<a href=\"https://varyzai.com/contact\">varyzai.com/contact</a>.</p>"
            )
        elif not is_open:
            body = (
                "<h1>Varyz Creator Program</h1><p class=\"cp-lead\">Applications are not open "
                "right now. Check back soon.</p>"
            )
        else:
            body = application_form()
        return with_csrf("<section class=\"cp\">" + body + "</section>", request)

    def application_form() -> str:
        return (
            "<h1>Share Varyz. Earn <em>30%</em> on every renewal.</h1>"
            "<p class=\"cp-lead\">Earn 30% on paid Varyz Cloud subscriptions you refer, "
            "including every renewal. Get three months of Cloud free when you join, and give "
            "your audience one month free.</p>"
            "<form class=\"cp-card\" method=\"post\" action=\"/creators/action\">"
            "<h2>Apply to join</h2>"
            "<input type=\"hidden\" name=\"csrf\" value=\"@CSRF@\">"
            "<input type=\"hidden\" name=\"action\" value=\"apply\">"
            "<label for=\"cp-name\">Your name or channel name</label>"
            "<input id=\"cp-name\" name=\"name\" maxlength=\"80\" required>"
            "<label for=\"cp-channel\">Channel or profile link</label>"
            "<input id=\"cp-channel\" name=\"channel\" type=\"url\" maxlength=\"300\" "
            "placeholder=\"https://\" required>"
            "<label for=\"cp-audience\">Your audience and topics</label>"
            "<textarea id=\"cp-audience\" name=\"audience\" maxlength=\"1000\" required>"
            "</textarea>"
            "<label for=\"cp-plan\">How you plan to share Varyz</label>"
            "<textarea id=\"cp-plan\" name=\"plan\" maxlength=\"1000\" required></textarea>"
            "<label class=\"cp-check\"><input type=\"checkbox\" name=\"terms\" value=\"yes\" "
            "required><span>I accept the <a href=\"https://varyzai.com/creators\">program "
            "terms</a> and the <a href=\"https://varyzai.com/privacy\">Privacy notice</a>, "
            "and I will disclose my commission and free access when I promote Varyz.</span>"
            "</label><p><button>Send application</button></p>"
            "<p class=\"cp-note\">We ask for payout details only after approval, in this "
            "portal.</p></form>"
        )

    def dashboard(db: Any, owner: dict, profile: dict, now: float, request: Request) -> str:
        code = profile["code"]
        since = datetime.fromtimestamp(now - 30 * DAY, UTC).date().isoformat()
        clicks = db.execute(
            "SELECT COALESCE(SUM(clicks),0) AS n FROM creator_clicks WHERE "
            "(code=? OR code IN (SELECT code FROM creator_code_aliases WHERE account=?)) "
            "AND day>=?", (code, owner["id"], since),
        ).fetchone()["n"]
        signups = db.execute(
            "SELECT COUNT(*) AS n FROM creator_referrals WHERE creator=?", (owner["id"],)
        ).fetchone()["n"]
        rows = ledger_rows(db, owner["id"])
        paying = len({r["account"] or r["transaction_id"] for r in rows})
        totals = balances(rows, now)
        grant = db.execute(
            "SELECT role,ends_at FROM cloud_grants WHERE account=?", (owner["id"],)
        ).fetchone()
        suspended = profile["status"] == "suspended"
        out = (
            "<section class=\"cp\"><p class=\"cp-tag ok\">Creator</p>"
            f"<h1>Welcome, {esc(profile['name'])}</h1>"
        )
        if suspended:
            out += (
                "<div class=\"cp-card\" role=\"status\"><p>Your creator account is paused. "
                "Your link does not add new referrals, and payouts wait for review. "
                "Contact us through <a href=\"https://varyzai.com/contact\">varyzai.com/"
                "contact</a>.</p></div>"
            )
        out += (
            "<div class=\"cp-card\"><h2>Your referral link</h2>"
            '<form class="cp-referral-form" method="post" action="/creators/action">'
            '<input type="hidden" name="csrf" value="@CSRF@">'
            '<input type="hidden" name="action" value="code">'
            '<div class="cp-link-row"><div class="cp-link-editor">'
            f'<span id="cp-referral-prefix">{esc(share_link(settings, ""))}</span>'
            f'<input id="cp-code-edit" name="code" value="{esc(code)}" '
            'aria-label="Your referral code" title="Click to edit your referral code" '
            'pattern="[a-z0-9_]{3,30}" minlength="3" maxlength="30" required '
            'autocapitalize="none" spellcheck="false" autocomplete="off">'
            '</div><button type="button" id="cp-copy" hidden>Copy link</button>'
            '<button id="cp-save-code">Save link</button></div>'
            '<span id="cp-copy-status" class="cp-note" role="status"></span>'
            '<p class="cp-note">Click your code to edit it. Previous links keep working.</p>'
            '</form>'
            "<p class=\"cp-note\">People who open your link and create a new account within "
            "30 days get one month of Cloud free. You earn 30% of their paid subscription and "
            "every renewal, excluding tax and refunds.</p></div>"
        )
        out += "<div class=\"cp-card\"><h2>Your free Cloud</h2>"
        if grant and grant["role"] == "creator":
            state = "until" if grant["ends_at"] > now else "ended"
            out += (
                f"<p>Your three free months {'run' if state == 'until' else 'ended'} "
                f"{'until' if state == 'until' else 'on'} {day_label(grant['ends_at'])}. "
                "They do not renew into a paid plan.</p>"
            )
        elif grant:
            out += "<p>This account already used its one free Cloud offer.</p>"
        elif owner.get("status") == "active" and not owner.get("free_access"):
            out += "<p>You already have a paid subscription, so there is nothing to activate.</p>"
        elif suspended:
            out += "<p>Available once your creator account is active again.</p>"
        else:
            out += (
                "<p>Three months of Varyz Cloud, free. No card, and it does not renew "
                "into a paid plan.</p>"
                "<form method=\"post\" action=\"/creators/action\">"
                "<input type=\"hidden\" name=\"csrf\" value=\"@CSRF@\">"
                "<input type=\"hidden\" name=\"action\" value=\"activate\">"
                "<button>Activate three free months</button></form>"
            )
        out += "</div>"
        out += (
            "<div class=\"cp-grid\">"
            f"<div class=\"cp-stat\"><b>{clicks}</b><span>Link visits, 30 days</span></div>"
            f"<div class=\"cp-stat\"><b>{signups}</b><span>Sign-ups</span></div>"
            f"<div class=\"cp-stat\"><b>{paying}</b><span>Paying customers</span></div></div>"
        )
        out += (
            '<div class="cp-card" id="cp-earnings"><div class="cp-earnings-head">'
            '<h2>Earnings</h2><div class="cp-earnings-total"><span>Total earned</span>'
            + ("".join(
                f'<strong>{money(total["earned"], currency)}</strong>'
                for currency, total in sorted(totals.items())
            ) if totals else '<strong>0</strong>')
            + '</div></div>'
        )
        if rows:
            out += '<details class="cp-breakdown"><summary>View breakdown</summary>'
        if totals:
            out += (
                '<div class="cp-scroll">'
                "<table><thead><tr><th>Currency</th><th>On hold</th><th>Next payout</th>"
                "<th>Paid</th></tr></thead><tbody>"
            )
            for currency, total in sorted(totals.items()):
                out += (
                    f"<tr><td>{esc(currency)}</td>"
                    f"<td>{money(total['held'], currency).removesuffix(' ' + currency)}</td>"
                    f"<td>{money(total['ready'], currency).removesuffix(' ' + currency)}</td>"
                    f"<td>{money(total['paid'], currency).removesuffix(' ' + currency)}</td></tr>"
                )
            out += "</tbody></table></div>"
        else:
            out += "<p class=\"cp-note\">No commissions yet.</p>"
        if rows:
            out += (
                '<p class="cp-note">Lifetime commissions after refunds, including amounts '
                'already paid, on hold and due for payout.</p>'
            )
        out += (
            "<p class=\"cp-note\">Each payment is held for 30 days in case of a refund, then "
            "paid on the next monthly run. Refunds and chargebacks reduce earnings, including "
            "after a payout. Each currency is paid separately.</p>"
        )
        if rows:
            out += (
                f'<p class="cp-note">All {len(rows)} commission payments</p>'
                '<div class="cp-history cp-scroll">'
                "<table><thead><tr><th>Payment</th><th>Commission</th><th>Status</th>"
                "</tr></thead><tbody>"
            )
            for row in rows:
                if row["refunded"] >= row["net"]:
                    status = "Refunded"
                elif row["open"]:
                    status = "In review"
                elif row["paid"] >= row["commission"]:
                    status = "Paid"
                elif row["hold_until"] > now:
                    status = "On hold until " + day_label(row["hold_until"])
                else:
                    status = "Next payout"
                if 0 < row["refunded"] < row["net"]:
                    status += " · Partially refunded"
                out += (
                    f"<tr><td>{day_label(row['completed_at'])}"
                    f'<span class="cp-payment-id">{esc(row["transaction_id"])}</span></td>'
                    f"<td>{money(row['commission'], row['currency'])}</td>"
                    f"<td>{esc(status)}</td></tr>"
                )
            out += "</tbody></table></div>"
            out += "</details>"
        out += "</div>"
        method = profile.get("payout_method") or ""
        saved = vault.open(profile.get("payout_address"))
        handle = "" if saved.get("account") else payout_hint(saved)
        current = (
            f"<p class=\"cp-note\">Saved: {esc(PAYOUT_METHODS[method])}, "
            f"{esc(payout_hint(saved))}.</p>"
            if method in PAYOUT_METHODS and saved else ""
        )
        out += (
            "<form class=\"cp-card\" method=\"post\" action=\"/creators/action\">"
            "<h2>Payout details</h2><input type=\"hidden\" name=\"csrf\" value=\"@CSRF@\">"
            "<input type=\"hidden\" name=\"action\" value=\"payout\">" + current
            + '<fieldset class="cp-methods"><legend>Payout method</legend>'
            + "".join(
                '<label class="cp-choice"><input type="radio" name="method" '
                f'value="{key}"{ " checked" if key == (method or "paypal") else ""}>'
                f'<span>{label}</span></label>'
                for key, label in PAYOUT_METHODS.items()
            )
            + '</fieldset><p class="cp-note" id="cp-method-help" aria-live="polite">'
            'Choose a method and enter its receiving details below.</p>'
            '<div class="cp-handle"><label for="cp-address" id="cp-address-label">'
            'Account email or Revolut Revtag</label>'
            "<input id=\"cp-address\" name=\"address\" maxlength=\"200\" "
            f'autocomplete="email" aria-describedby="cp-method-help" value="{esc(handle)}"></div>'
            "<fieldset class=\"cp-bank\"><legend>Bank transfer</legend>"
            "<label for=\"cp-holder\">Account holder name</label>"
            "<input id=\"cp-holder\" name=\"holder\" maxlength=\"140\" autocomplete=\"name\">"
            "<label for=\"cp-account\">IBAN, or account number</label>"
            "<input id=\"cp-account\" name=\"account\" maxlength=\"48\" autocomplete=\"off\">"
            "<label for=\"cp-code\">SWIFT/BIC, routing number or sort code "
            "(not needed with an IBAN)</label>"
            "<input id=\"cp-code\" name=\"bank_code\" maxlength=\"20\" autocomplete=\"off\">"
            "<label for=\"cp-country\">Bank country code (not needed with an IBAN)</label>"
            "<input id=\"cp-country\" name=\"country\" maxlength=\"2\" placeholder=\"US\" "
            "autocomplete=\"off\"></fieldset>"
            "<p><button>Save payout details</button></p>"
            '<p class="cp-note">Saving details does not connect or verify your account. '
            'We will contact you at your Varyz account email if anything else is needed '
            'before your first payout.</p>'
            "<p class=\"cp-note\">We pay monthly, by hand, in the currency your referrals paid. "
            "Payout details are stored encrypted and only the program operator can read them. "
            "We never ask for card numbers.</p>"
            "</form>"
        )
        out += (
            '<p class="cp-note">Varyz is free to use locally. Cloud is an optional paid '
            'service, and commissions apply only to paid Cloud subscriptions. '
            'When promoting Varyz, disclose your affiliate relationship and any free access. '
            '<a href="https://varyzai.com/creators">Program terms</a></p></section>'
        )
        return out

    @app.post("/creators/action")
    async def creator_action(request: Request, background_tasks: BackgroundTasks):
        data = await form(request)
        csrf(request, data)
        owner = account(request, browser=True)
        action = data.get("action", "")
        if not await asyncio.to_thread(
            store.allow_request, digest("creator-action:" + owner["id"]), 3600, 20
        ):
            raise HTTPException(429, "Too many requests; try again later")
        now = time.time()
        if action == "accept_invitation":
            if data.get("terms") != "yes":
                raise HTTPException(422, "Accept the program terms to join")
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                invitation = creator_invitation(db, owner)
                profile = db.execute(
                    "SELECT status FROM creator_profiles WHERE account=?", (owner["id"],),
                ).fetchone()
                if not invitation or (profile and profile["status"] != "applied"):
                    raise HTTPException(403, "No pending creator invitation for this account")
                db.execute(
                    "INSERT INTO creator_profiles(account,code,status,name,terms_version,"
                    "applied_at,decided_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account) "
                    "DO UPDATE SET code=excluded.code,status=excluded.status,"
                    "terms_version=excluded.terms_version,decided_at=excluded.decided_at",
                    (owner["id"], invitation["code"], "approved", invitation["name"] or
                     owner.get("username") or "Creator", TERMS_VERSION, now, now),
                )
                db.execute(
                    "UPDATE creator_invitations SET account=?,accepted_at=? WHERE email=?",
                    (owner["id"], now, invitation["email"]),
                )
        elif action == "apply":
            name = " ".join(data.get("name", "").split())
            channel = data.get("channel", "").strip()
            audience = data.get("audience", "").strip()
            plan = data.get("plan", "").strip()
            parsed = urlparse(channel)
            if (
                not 1 <= len(name) <= 80
                or parsed.scheme not in ("http", "https")
                or not parsed.netloc
                or len(channel) > 300
                or not 1 <= len(audience) <= 1000
                or not 1 <= len(plan) <= 1000
                or data.get("terms") != "yes"
            ):
                raise HTTPException(422, "Complete every field and accept the program terms")
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not applications_open(db, settings):
                    raise HTTPException(409, "Applications are not open right now")
                if creator_invitation(db, owner):
                    return RedirectResponse("/creators", status_code=303)
                if db.execute(
                    "SELECT 1 FROM creator_profiles WHERE account=?", (owner["id"],)
                ).fetchone():
                    return RedirectResponse("/creators", status_code=303)
                db.execute(
                    "INSERT INTO creator_profiles(account,code,status,name,channel,audience,"
                    "plan,terms_version,applied_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (owner["id"], new_code(db, owner), "applied", name, channel, audience,
                     plan, TERMS_VERSION, now),
                )
            recipients = (
                (settings.creator_notification_email,)
                if settings.creator_notification_email else settings.admin_emails
            )
            for admin in recipients:
                background_tasks.add_task(
                    send_mail, settings, store, admin, "New Varyz creator application",
                    f"{name} applied to the Varyz Creator Program.\n\nReview it at "
                    f"{settings.public_url.rstrip('/')}/admin#creators\n",
                )
        elif action == "activate":
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                profile = db.execute(
                    "SELECT status FROM creator_profiles WHERE account=?", (owner["id"],)
                ).fetchone()
                if not profile or profile["status"] != "approved":
                    raise HTTPException(403, "Only approved creators can activate free Cloud")
                if owner.get("status") == "active" and not owner.get("free_access"):
                    raise HTTPException(409, "You already have a paid Cloud subscription")
                if not give_grant(db, owner["id"], "creator", owner["id"], now):
                    raise HTTPException(409, "This account already used its free Cloud offer")
        elif action == "code":
            code = data.get("code", "").strip().lower()
            if not CODE.fullmatch(code):
                raise HTTPException(422, "Use 3–30 letters, numbers or underscores")
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                profile = db.execute(
                    "SELECT code FROM creator_profiles WHERE account=? AND status='approved'",
                    (owner["id"],),
                ).fetchone()
                if not profile:
                    raise HTTPException(403, "Only active creators can change their code")
                if db.execute(
                    "SELECT 1 FROM creator_profiles WHERE code=? AND account<>?",
                    (code, owner["id"]),
                ).fetchone() or db.execute(
                    "SELECT 1 FROM creator_invitations WHERE code=? AND "
                    "(account IS NULL OR account<>?)", (code, owner["id"]),
                ).fetchone():
                    raise HTTPException(409, "That code is already taken; choose another")
                # Reserve before changing; a unique key prevents competing claims.
                db.execute(
                    "INSERT INTO creator_code_aliases(code,account) VALUES(?,?) "
                    "ON CONFLICT(code) DO NOTHING", (code, owner["id"]),
                )
                reserved = db.execute(
                    "SELECT account FROM creator_code_aliases WHERE code=?", (code,),
                ).fetchone()
                if reserved["account"] != owner["id"]:
                    raise HTTPException(409, "That code is already taken; choose another")
                db.execute(
                    "INSERT INTO creator_code_aliases(code,account) VALUES(?,?) "
                    "ON CONFLICT(code) DO NOTHING", (profile["code"], owner["id"]),
                )
                db.execute("UPDATE creator_profiles SET code=? WHERE account=?",
                           (code, owner["id"]))
        elif action == "payout":
            method = data.get("method", "")
            try:
                details = payout_details(method, data)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            with store.connection() as db:
                db.execute(
                    "UPDATE creator_profiles SET payout_method=?,payout_address=? "
                    "WHERE account=? AND status IN ('approved','suspended')",
                    (method, vault.seal(details), owner["id"]),
                )
        else:
            raise HTTPException(422, "Unknown action")
        return RedirectResponse("/creators", status_code=303)

    def new_code(db: Any, owner: dict) -> str:
        base = re.sub(r"[^a-z0-9_]", "", (owner.get("username") or "").lower())[:24]
        if len(base) < 3:
            base = "v" + secrets.token_hex(3)
        code = base
        while (db.execute("SELECT 1 FROM creator_profiles WHERE code=?", (code,)).fetchone()
               or db.execute("SELECT 1 FROM creator_invitations WHERE code=?", (code,)).fetchone()
               or db.execute("SELECT 1 FROM creator_code_aliases WHERE code=?",
                             (code,)).fetchone()):
            code = base + str(secrets.randbelow(900) + 100)
        return code


def take_return(request: Request, response: Any, secure: bool) -> str | None:
    """A whitelisted page to land on after sign-in, used once."""
    target = read_cookie(request, "return_to")
    if target in RETURN_PATHS:
        delete_cookie(response, "return_to", secure=secure)
        return target
    return None
