"""Owner-only admin panel: accounts, revenue, the Creator Program and payouts.

Only verified accounts whose email is in VARYZ_CLOUD_ADMIN_EMAILS can see it; everyone else
gets a plain 404. The overview links to account lists and one page per account. Pages work
without JavaScript (charts are inline SVG) so the Cloud's script policy stays strict.
Recording a payout needs a sign-in from the last 15 minutes.
"""

from __future__ import annotations

import asyncio
import csv
import io
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .browser import read_cookie, set_cookie
from .creator_policy import payout_cutoff, payout_hint, payout_lines, payout_preview
from .creators import (
    APPROVAL_BODY,
    APPROVAL_SUBJECT,
    DAY,
    PAYOUT_METHODS,
    PROGRAM_CSS,
    PayoutVault,
    applications_open,
    balances,
    day_label,
    esc,
    instant,
    invite_creator,
    ledger_rows,
    money,
    policy_rows,
    put_setting,
    send_mail,
    share_link,
)
from .store import digest

ADMIN_CSS = """
.cloud-document:has(.cp-admin){max-width:1120px}
.cp-admin h2{margin-top:6px}
.cp-admin nav{display:flex;flex-wrap:wrap;gap:8px 16px;margin:6px 0 4px;font-size:14px}
.cp-chart{width:100%;height:auto;display:block;margin:6px 0}
.cp-chart text{fill:var(--muted);font:11px var(--sans)}.cp-chart .bar{fill:var(--signal)}
.cp-chart .axis{stroke:var(--line)}
.cp-split{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:18px}
.cp-admin td small{color:var(--muted)}.cp-admin details summary{cursor:pointer;color:var(--muted)}
.cp-warn{color:#ff9b7a}
.cp-admin a.cp-stat{color:inherit;text-decoration:none}
.cp-admin a.cp-stat:hover,.cp-admin a.cp-stat:focus-visible{border-color:var(--signal)}
.cp-admin a.cp-stat span::after{content:" \\2192"}
.cp-admin nav a[aria-current]{color:var(--ink);font-weight:600}
.cp-find{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
.cp-find input{flex:1 1 220px;width:auto;margin:0}.cp-find button{width:auto;margin:0}
.cp-facts th{width:34%;text-transform:none;letter-spacing:0;font-size:13px}
"""

ACCOUNT_VIEWS = {
    "all": "All",
    "week": "New, 7 days",
    "month": "New, 30 days",
    "paying": "Paying",
    "free": "Free access",
    "referred": "Referred",
    "creators": "Creators",
}
PAGE_SIZE = 100


def bar_chart(points: list[tuple[str, int]], *, label_every: int = 1, unit: str = "") -> str:
    """A small inline SVG bar chart; labels show every Nth bar."""
    width, height, top, bottom = 640, 180, 14, 26
    peak = max((value for _, value in points), default=0) or 1
    step = width / max(len(points), 1)
    bars = []
    for index, (label, value) in enumerate(points):
        bar = (height - top - bottom) * value / peak
        x = index * step + step * 0.15
        y = height - bottom - bar
        bars.append(
            f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{step * 0.7:.1f}" '
            f'height="{bar:.1f}" rx="2"><title>{esc(label)}: {value}{esc(unit)}</title></rect>'
        )
        if index % label_every == 0:
            bars.append(
                f'<text x="{x + step * 0.35:.1f}" y="{height - 8}" text-anchor="middle">'
                f"{esc(label)}</text>"
            )
        if value and len(points) <= 14:
            bars.append(
                f'<text x="{x + step * 0.35:.1f}" y="{y - 3:.1f}" text-anchor="middle">'
                f"{value}</text>"
            )
    return (
        f'<svg class="cp-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Bar chart">'
        f'<line class="axis" x1="0" x2="{width}" y1="{height - bottom}" '
        f'y2="{height - bottom}"/>{"".join(bars)}'
        f'<text x="0" y="10">max {peak}{esc(unit)}</text></svg>'
    )


def month_key(seconds: float) -> str:
    return f"{instant(seconds):%Y-%m}"


def last_months(now: float, count: int) -> list[str]:
    moment = instant(now)
    year, month = moment.year, moment.month
    keys = []
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if not month:
            year, month = year - 1, 12
    return keys[::-1]


def megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def spreadsheet_cell(value: Any) -> str:
    """Text a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def count(db: Any, query: str, parameters: tuple = ()) -> int:
    try:
        row = db.execute(query, parameters).fetchone()
    except Exception:  # A table owned by an optional module may not exist yet.
        return 0
    return int(row["n"] or 0) if row else 0


def register_admin(app: Any, settings: Any, store: Any, account: Any, csrf: Any,
                   form: Any, page: Any) -> None:
    secure = settings.public_url.startswith("https://")
    admins = {email.lower() for email in settings.admin_emails}
    vault = PayoutVault(getattr(settings, "archive_encryption_key", ""))

    def is_admin(owner: dict | None) -> bool:
        return bool(
            owner
            and owner.get("email_verified")
            and str(owner.get("email") or "").lower() in admins
        )

    def private(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    def owner_page(request: Request, build: Any, title: str = "Varyz admin") -> Any:
        """Signs the owner in, hides the page from everyone else and renders build()."""
        try:
            owner = account(request, browser=True)
        except HTTPException:
            response = RedirectResponse("/login", status_code=303)
            set_cookie(response, "return_to", "/admin", httponly=True, secure=secure,
                       samesite="lax", max_age=900)
            return private(response)
        if not is_admin(owner):
            response = page(
                '<h1>This page is unavailable for this account</h1>'
                '<p><a href="/login?reauth=1">Sign in with a different account</a></p>'
                '<p><a href="/">Back to your account</a></p>',
                title="Page unavailable", owner=owner,
            )
            response.status_code = 404
            return private(response)
        token = read_cookie(request, "csrf")
        if len(token) != 43:
            token = secrets.token_urlsafe(32)
        with store.connection() as db:
            content = build(db, token, time.time())
        response = page(
            "<style>" + PROGRAM_CSS + ADMIN_CSS + "</style>" + content,
            title=title, owner=owner,
        )
        set_cookie(response, "csrf", token, httponly=True, secure=secure, samesite="lax",
                   max_age=3600)
        return private(response)

    @app.get("/admin")
    def admin_home(request: Request):
        return owner_page(request, lambda db, token, now: render(db, token, now, request))

    @app.get("/admin/accounts")
    def admin_accounts(request: Request):
        return owner_page(
            request,
            lambda db, token, now: account_list(db, now, request),
            "Accounts, Varyz admin",
        )

    @app.get("/admin/accounts.csv")
    def admin_accounts_csv(request: Request):
        owner = account(request, browser=True)
        if not is_admin(owner):
            raise HTTPException(404, "Not Found")
        with store.connection() as db:
            where, parameters, _ = account_filter(request, time.time())
            rows = db.execute(ACCOUNT_QUERY + where + ACCOUNT_ORDER, parameters).fetchall()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["email", "username", "joined_utc", "email_verified", "subscription",
                         "referred_by", "free_access_until"])
        for row in rows:
            writer.writerow([spreadsheet_cell(value) for value in (
                row["email"], row["username"],
                f"{instant(row['joined']):%Y-%m-%d %H:%M}" if row["joined"] else "",
                "yes" if row["email_verified"] else "no",
                row["status"] or "inactive", row["referrer"] or "",
                f"{instant(row['grant_ends']):%Y-%m-%d}" if live_grant(row, time.time()) else "",
            )])
        response = Response(
            buffer.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="varyz-accounts.csv"'},
        )
        return private(response)

    @app.get("/admin/accounts/{account_id}")
    def admin_account(request: Request, account_id: str):
        return owner_page(
            request,
            lambda db, token, now: account_detail(db, token, now, account_id),
            "Account, Varyz admin",
        )

    def hidden(token: str, action: str, **values: Any) -> str:
        fields = f'<input type="hidden" name="csrf" value="{token}">'
        fields += f'<input type="hidden" name="action" value="{esc(action)}">'
        for key, value in values.items():
            fields += f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
        return fields

    def button(token: str, action: str, label: str, **values: Any) -> str:
        return (
            '<form class="cp-inline" method="post" action="/admin/action">'
            + hidden(token, action, **values)
            + f"<button>{esc(label)}</button></form>"
        )

    def render(db: Any, token: str, now: float, request: Request) -> str:
        out = (
            '<section class="cp cp-admin"><p class="cp-tag ok">Owner only</p>'
            "<h1>Varyz admin</h1>"
            f'<p class="cp-note">As of {day_label(now)}, {instant(now):%H:%M} UTC.</p>'
            '<nav><a href="#overview">Overview</a><a href="#revenue">Revenue</a>'
            '<a href="#creators">Creators</a><a href="#payouts">Payouts</a>'
            '<a href="#reviews">Reviews</a><a href="#grants">Free access</a>'
            '<a href="#usage">Usage</a></nav>'
        )
        notice = request.query_params.get("done", "")
        if notice in NOTICES:
            out += f'<div class="cp-card" role="status"><p>{esc(NOTICES[notice])}</p></div>'
        out += overview(db, now) + revenue(db, now) + creators(db, token, now)
        out += payouts(db, token, now) + reviews(db, token) + grants(db, token, now)
        out += usage(db, now) + "</section>"
        return out

    def overview(db: Any, now: float) -> str:
        total = count(db, "SELECT COUNT(*) AS n FROM accounts")
        week = count(db, "SELECT COUNT(*) AS n FROM account_created WHERE at>?", (now - 7 * DAY,))
        month = count(
            db, "SELECT COUNT(*) AS n FROM account_created WHERE at>?", (now - 30 * DAY,)
        )
        paying = count(
            db,
            "SELECT COUNT(*) AS n FROM accounts WHERE status='active' AND subscription IS NOT "
            "NULL AND subscription!=''",
        )
        free = count(
            db,
            "SELECT COUNT(*) AS n FROM cloud_grants WHERE revoked_at IS NULL AND ends_at>?",
            (now,),
        )
        statuses = db.execute(
            "SELECT status,COUNT(*) AS n FROM accounts GROUP BY status ORDER BY n DESC"
        ).fetchall()
        start = now - 29 * DAY
        days = {
            (instant(start) + timedelta(days=i)).date().isoformat(): 0 for i in range(30)
        }
        for row in db.execute("SELECT at FROM account_created WHERE at>?", (start - DAY,)):
            key = instant(row["at"]).date().isoformat()
            if key in days:
                days[key] += 1
        points = [(datetime.fromisoformat(k).strftime("%d"), v) for k, v in days.items()]
        out = (
            '<div id="overview" class="cp-grid">'
            f'<a class="cp-stat" href="/admin/accounts"><b>{total}</b><span>Accounts</span></a>'
            f'<a class="cp-stat" href="/admin/accounts?view=week"><b>+{week}</b>'
            "<span>New, 7 days</span></a>"
            f'<a class="cp-stat" href="/admin/accounts?view=month"><b>+{month}</b>'
            "<span>New, 30 days</span></a>"
            f'<a class="cp-stat" href="/admin/accounts?view=paying"><b>{paying}</b>'
            "<span>Paying subscribers</span></a>"
            f'<a class="cp-stat" href="/admin/accounts?view=free"><b>{free}</b>'
            "<span>Free Cloud grants</span></a></div>"
            '<div class="cp-split"><div class="cp-card"><h2>Sign-ups per day</h2>'
            + bar_chart(points, label_every=5)
            + '<p class="cp-note">Counted from this release on.</p></div>'
            '<div class="cp-card"><h2>Subscriptions</h2><table><tbody>'
        )
        for row in statuses:
            status = row["status"] or "inactive"
            out += (
                f'<tr><td><a href="/admin/accounts?{urlencode({"status": status})}">'
                f"{esc(status)}</a></td><td>{row['n']}</td></tr>"
            )
        return out + "</tbody></table></div></div>"

    def revenue(db: Any, now: float) -> str:
        months = last_months(now, 12)
        by_currency: dict[str, dict[str, int]] = {}
        recent: dict[str, list[int]] = {}
        for row in db.execute(
            "SELECT currency,completed_at,net,tax FROM billing_revenue WHERE completed_at>?",
            (now - 370 * DAY,),
        ):
            key = month_key(row["completed_at"])
            if key in months:
                series = by_currency.setdefault(row["currency"], dict.fromkeys(months, 0))
                series[key] += row["net"]
            if row["completed_at"] > now - 30 * DAY:
                total = recent.setdefault(row["currency"], [0, 0, 0])
                total[0] += row["net"]
                total[1] += row["tax"]
                total[2] += 1
        refunds: dict[str, int] = {}
        for row in db.execute(
            "SELECT currency,amount FROM billing_adjustments WHERE action IN "
            "('refund','chargeback','credit') AND status='approved' AND updated>?",
            (now - 30 * DAY,),
        ):
            currency = row["currency"] or "?"
            refunds[currency] = refunds.get(currency, 0) + row["amount"]
        out = '<div id="revenue" class="cp-card"><h2>Revenue</h2>'
        if not by_currency and not recent:
            out += (
                '<p class="cp-note">No Cloud payments recorded since this release. New Paddle '
                "payments appear here as their webhooks arrive.</p>"
            )
        else:
            out += (
                '<div class="cp-scroll">'
                "<table><thead><tr><th>Last 30 days</th><th>Revenue excl. tax</th><th>Tax</th>"
                "<th>Payments</th><th>Refunds</th></tr></thead><tbody>"
            )
            for currency in sorted(set(recent) | set(refunds)):
                net, tax, payments = recent.get(currency, [0, 0, 0])
                out += (
                    f"<tr><td>{esc(currency)}</td><td>{money(net, currency)}</td>"
                    f"<td>{money(tax, currency)}</td><td>{payments}</td>"
                    f"<td>{money(refunds.get(currency, 0), currency)}</td></tr>"
                )
            out += "</tbody></table></div>"
            for currency, series in sorted(by_currency.items()):
                divisor = 1 if currency in ("JPY", "KRW") else 100
                points = [
                    (datetime.strptime(key, "%Y-%m").strftime("%b"), value // divisor)
                    for key, value in series.items()
                ]
                out += (
                    f"<h2 style=\"margin-top:18px\">Monthly revenue, {esc(currency)}</h2>"
                    + bar_chart(points, unit=" " + currency)
                )
        last = db.execute(
            "SELECT MAX(completed_at) AS at FROM billing_payments"
        ).fetchone()["at"]
        out += (
            '<p class="cp-note">Revenue is Cloud line items after discounts, before Paddle fees. '
            + (f"Last payment received {day_label(last)}." if last else "No payments yet.")
            + "</p></div>"
        )
        return out

    def creators(db: Any, token: str, now: float) -> str:
        is_open = applications_open(db, settings)
        out = (
            '<div id="creators" class="cp-card"><h2>Creator Program</h2>'
            f"<p>Applications are <strong>{'open' if is_open else 'closed'}</strong>. "
            + button(token, "applications", "Close applications" if is_open else
                     "Open applications", value="closed" if is_open else "open")
            + "</p>"
        )
        pending = db.execute(
            "SELECT p.*,a.email FROM creator_profiles p LEFT JOIN accounts a "
            "ON a.id=p.account WHERE p.status='applied' ORDER BY p.applied_at"
        ).fetchall()
        out += (
            '<h2>Invite a creator</h2><p class="cp-note">Reserve their code and pre-approve '
            'their verified email. They accept the terms in their creator dashboard. '
            'Saving an invitation does not send an email.</p>'
            '<form method="post" action="/admin/action">'
            f'<input type="hidden" name="csrf" value="{esc(token)}">'
            '<input type="hidden" name="action" value="invite_creator">'
            '<label>Email<input name="email" type="email" maxlength="254" required></label>'
            '<label>Creator code<input name="code" pattern="[a-z0-9_]{3,30}" '
            'maxlength="30" required></label>'
            '<label>Name<input name="name" maxlength="80"></label>'
            '<p><button>Save invitation</button></p></form>'
        )
        invitations = db.execute(
            "SELECT email,code FROM creator_invitations WHERE account IS NULL ORDER BY created"
        ).fetchall()
        if invitations:
            out += '<h2>Pending invitations</h2><ul>'
            for invitation in invitations:
                out += f'<li>{esc(invitation["email"])}: {esc(invitation["code"])}</li>'
            out += '</ul>'
        out += f"<h2>Applications ({len(pending)})</h2>"
        if not pending:
            out += '<p class="cp-note">No applications waiting.</p>'
        for row in pending:
            out += (
                '<div class="cp-card">'
                f"<p><strong>{esc(row['name'])}</strong> <small>{esc(row['email'])}, applied "
                f"{day_label(row['applied_at'])}</small></p>"
                f'<p><a href="{esc(row["channel"])}" rel="nofollow noopener noreferrer" '
                f'target="_blank">{esc(row["channel"])}</a></p>'
                f"<p><small>Audience</small><br>{esc(row['audience'])}</p>"
                f"<p><small>Plan</small><br>{esc(row['plan'])}</p><p>"
                + button(token, "approve", "Approve", account=row["account"])
                + button(token, "reject", "Reject", account=row["account"])
                + "</p></div>"
            )
        since = (instant(now - 30 * DAY)).date().isoformat()
        people = db.execute(
            "SELECT p.*,a.email FROM creator_profiles p LEFT JOIN accounts a ON "
            "a.id=p.account WHERE p.status IN ('approved','suspended') ORDER BY p.decided_at"
        ).fetchall()
        out += f"<h2>Creators ({len(people)})</h2>"
        if not people:
            return out + '<p class="cp-note">No approved creators yet.</p></div>'
        out += (
            '<div class="cp-scroll"><table><thead><tr><th>Creator</th><th>Visits 30d</th>'
            "<th>Sign-ups</th><th>Paying</th><th>Earned</th><th>Payout to</th><th></th>"
            "</tr></thead><tbody>"
        )
        for row in people:
            clicks = count(
                db,
                "SELECT COALESCE(SUM(clicks),0) AS n FROM creator_clicks WHERE code=? AND day>=?",
                (row["code"], since),
            )
            signups = count(
                db, "SELECT COUNT(*) AS n FROM creator_referrals WHERE creator=?",
                (row["account"],),
            )
            rows = ledger_rows(db, row["account"])
            paying = len({r["account"] or r["transaction_id"] for r in rows})
            earned = ", ".join(
                money(total["earned"], currency)
                for currency, total in sorted(balances(rows, now).items())
            ) or "None yet"
            method = PAYOUT_METHODS.get(row["payout_method"] or "", "")
            hint = payout_hint(vault.open(row["payout_address"])) or "Not set"
            status = "" if row["status"] == "approved" else ' <span class="cp-tag">paused</span>'
            link = "/admin/accounts/" + row["account"]
            referred = "/admin/accounts?" + urlencode({"creator": row["account"]})
            out += (
                f'<tr><td><strong><a href="{esc(link)}">{esc(row["name"])}</a></strong>'
                f"{status}<br><small>{esc(share_link(settings, row['code']))}<br>"
                f"{esc(row['email'])}</small></td>"
                f'<td>{clicks}</td><td><a href="{esc(referred)}">{signups}</a></td>'
                f"<td>{paying}</td><td>{esc(earned)}</td>"
                f"<td>{esc(method)}<br><small>{esc(hint)}</small></td><td>"
                + (
                    button(token, "suspend", "Pause", account=row["account"])
                    if row["status"] == "approved"
                    else button(token, "reinstate", "Reinstate", account=row["account"])
                )
                + "</td></tr>"
            )
        return out + "</tbody></table></div></div>"

    def payouts(db: Any, token: str, now: float) -> str:
        cutoff = payout_cutoff(instant(now))
        rows = ledger_rows(db)
        preview = payout_preview(policy_rows(rows), cutoff)
        names = {
            r["account"]: dict(r) for r in db.execute(
                "SELECT account,name,payout_method,payout_address FROM creator_profiles"
            )
        }
        out = (
            '<div id="payouts" class="cp-card"><h2>Payouts due</h2>'
            f'<p class="cp-note">Cutoff {cutoff.day} {cutoff:%b %Y}, 00:00 UTC. '
            "Commissions on payments that cleared their 30-day hold before the cutoff. "
            "Send each one by hand, then record it with the payment reference.</p>"
        )
        if not preview["payouts"]:
            out += '<p class="cp-note">Nothing to pay this month.</p>'
        for item in preview["payouts"]:
            profile = names.get(item["creator"])
            method = PAYOUT_METHODS.get((profile or {}).get("payout_method") or "", "")
            details = vault.open((profile or {}).get("payout_address"))
            out += (
                '<div class="cp-card">'
                f"<p><strong>{money(item['amount'], item['currency'])}</strong> to "
                f"{esc((profile or {}).get('name') or 'closed account')} "
                f"<small>({len(item['lines'])} payments)</small></p>"
            )
            if not method or not details:
                out += (
                    '<p class="cp-warn">No payout details yet. The creator adds them on their '
                    "dashboard.</p></div>"
                )
                continue
            out += (
                f"<p>{esc(method)}</p><table class=\"cp-facts\"><tbody>"
                + "".join(
                    f"<tr><th>{esc(label)}</th><td><code>{esc(value)}</code></td></tr>"
                    for label, value in payout_lines(details)
                )
                + "</tbody></table>"
                '<form method="post" action="/admin/action">'
                + hidden(token, "payout", creator=item["creator"], currency=item["currency"],
                         amount=item["amount"], as_of=int(cutoff.timestamp()))
                + '<label>Payment reference<input name="reference" maxlength="120" required>'
                "</label><button>Record as paid</button></form></div>"
            )
        if preview["blocked"]:
            out += "<h2>Blocked</h2><p class=\"cp-note\">An open refund, chargeback or paused "
            out += "creator holds the whole batch in that currency.</p><ul>"
            for item in preview["blocked"]:
                name = (names.get(item["creator"]) or {}).get("name") or "closed account"
                out += f"<li>{esc(name)}, {esc(item['currency'])}</li>"
            out += "</ul>"
        if preview["debts"]:
            out += "<h2>Refunds after payout</h2><ul>"
            for debt in preview["debts"]:
                name = (names.get(debt["creator"]) or {}).get("name") or "closed account"
                out += (
                    f"<li>{esc(name)}: {money(debt['amount'], debt['currency'])}, offset "
                    "against later commissions</li>"
                )
            out += "</ul>"
        waiting = set(preview["held"])
        held = [r for r in rows if r["transaction_id"] in waiting]
        if held:
            totals: dict[str, int] = {}
            for r in held:
                totals[r["currency"]] = totals.get(r["currency"], 0) + r["commission"] - r["paid"]
            out += '<p class="cp-note">On hold for later months: ' + ", ".join(
                money(v, c) for c, v in sorted(totals.items())
            ) + ".</p>"
        history = db.execute(
            "SELECT * FROM creator_payouts ORDER BY recorded_at DESC LIMIT 25"
        ).fetchall()
        if history:
            out += (
                "<h2>Payout history</h2><div class=\"cp-scroll\"><table><thead><tr><th>Date"
                "</th><th>Creator</th><th>Amount</th><th>Method</th><th>Reference</th></tr>"
                "</thead><tbody>"
            )
            for row in history:
                name = (names.get(row["creator"]) or {}).get("name") or "closed account"
                out += (
                    f"<tr><td>{day_label(row['recorded_at'])}</td><td>{esc(name)}</td>"
                    f"<td>{money(row['amount'], row['currency'])}</td>"
                    f"<td>{esc(PAYOUT_METHODS.get(row['method'] or '', ''))}</td>"
                    f"<td>{esc(row['reference'])}</td></tr>"
                )
            out += "</tbody></table></div>"
        return out + "</div>"

    def reviews(db: Any, token: str) -> str:
        rows = db.execute(
            "SELECT * FROM billing_adjustments WHERE (action='chargeback_warning' AND "
            "resolved=0) OR (action IN ('refund','credit') AND status='pending_approval') "
            "ORDER BY updated DESC"
        ).fetchall()
        out = '<div id="reviews" class="cp-card"><h2>Open refunds and chargebacks</h2>'
        if not rows:
            return out + '<p class="cp-note">Nothing open.</p></div>'
        out += (
            '<div class="cp-scroll">'
            "<table><thead><tr><th>Transaction</th><th>Kind</th><th>Amount</th><th></th>"
            "</tr></thead><tbody>"
        )
        for row in rows:
            out += (
                f"<tr><td><code>{esc(row['transaction_id'])}</code></td>"
                f"<td>{esc(row['action'].replace('_', ' '))}, {esc(row['status'])}</td>"
                f"<td>{money(row['amount'], row['currency'] or '')}</td><td>"
                + (
                    button(token, "resolve", "Mark resolved", id=row["id"])
                    if row["action"] == "chargeback_warning"
                    else "<small>Waits for Paddle</small>"
                )
                + "</td></tr>"
            )
        return out + "</tbody></table></div></div>"

    def grants(db: Any, token: str, now: float) -> str:
        rows = db.execute(
            "SELECT g.*,a.email,p.name AS creator_name FROM cloud_grants g "
            "LEFT JOIN accounts a ON a.id=g.account LEFT JOIN creator_profiles p "
            "ON p.account=g.creator WHERE g.revoked_at IS NULL AND g.ends_at>? "
            "ORDER BY g.started_at DESC LIMIT 100",
            (now,),
        ).fetchall()
        out = f'<div id="grants" class="cp-card"><h2>Free Cloud access ({len(rows)})</h2>'
        if not rows:
            return out + '<p class="cp-note">No active free access.</p></div>'
        out += (
            '<div class="cp-scroll"><table><thead><tr><th>Account</th><th>Offer</th>'
            "<th>Ends</th><th></th></tr></thead><tbody>"
        )
        for row in rows:
            offer = (
                "Creator, 3 months" if row["role"] == "creator"
                else f"Invited by {row['creator_name'] or 'a creator'}, 1 month"
            )
            out += (
                f'<tr><td><a href="/admin/accounts/{esc(row["account"])}">'
                f"{esc(row['email'])}</a></td><td>{esc(offer)}</td>"
                f"<td>{day_label(row['ends_at'])}</td><td>"
                + button(token, "revoke", "Revoke", account=row["account"])
                + "</td></tr>"
            )
        return out + "</tbody></table></div>"

    def usage(db: Any, now: float) -> str:
        items = (
            ("Synced records", count(db, "SELECT COUNT(*) AS n FROM records")),
            ("Stored, MB", count(
                db, "SELECT COALESCE(SUM(bytes),0) AS n FROM account_usage"
            ) // (1024 * 1024)),
            ("Connected devices and apps", count(
                db,
                "SELECT COUNT(*) AS n FROM (SELECT DISTINCT account,client FROM credentials "
                "WHERE kind='refresh' AND expires>?) AS live",
                (now,),
            )),
            ("Public Varyz pages", count(db, "SELECT COUNT(*) AS n FROM public_varyz")),
            ("Follows", count(db, "SELECT COUNT(*) AS n FROM follows")),
            ("Install reports, 30 days", count(
                db, "SELECT COUNT(*) AS n FROM analytics_reports WHERE day>=?",
                ((instant(now - 30 * DAY)).date().isoformat(),),
            )),
        )
        return (
            '<div id="usage" class="cp-card"><h2>Usage</h2><div class="cp-grid">'
            + "".join(
                f'<div class="cp-stat"><b>{value:,}</b><span>{esc(label)}</span></div>'
                for label, value in items
            )
            + "</div></div>"
        )

    def live_grant(row: Any, now: float) -> bool:
        return bool(row["grant_ends"]) and not row["grant_revoked"] and row["grant_ends"] > now

    def account_filter(request: Request, now: float) -> tuple[str, tuple, dict[str, str]]:
        """The WHERE clause for the account list, from the query string."""
        query = request.query_params
        view = query.get("view", "all")
        view = view if view in ACCOUNT_VIEWS else "all"
        clauses: list[str] = []
        parameters: list[Any] = []
        chosen = {"view": view}
        if view == "week":
            clauses.append("c.at>?")
            parameters.append(now - 7 * DAY)
        elif view == "month":
            clauses.append("c.at>?")
            parameters.append(now - 30 * DAY)
        elif view == "paying":
            clauses.append(
                "a.status='active' AND a.subscription IS NOT NULL AND a.subscription!=''"
            )
        elif view == "free":
            clauses.append("g.revoked_at IS NULL AND g.ends_at>?")
            parameters.append(now)
        elif view == "referred":
            clauses.append("r.creator IS NOT NULL")
        elif view == "creators":
            clauses.append(
                "EXISTS (SELECT 1 FROM creator_profiles cp WHERE cp.account=a.id "
                "AND cp.status IN ('approved','suspended'))"
            )
        status = query.get("status", "")[:40]
        if status:
            clauses.append("COALESCE(a.status,'inactive')=?")
            parameters.append(status)
            chosen["status"] = status
        creator = query.get("creator", "")[:64]
        if creator:
            clauses.append("r.creator=?")
            parameters.append(creator)
            chosen["creator"] = creator
        search = " ".join(query.get("q", "").split())[:120].lower()
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace(
                "_", "\\_") + "%"
            clauses.append(
                "(LOWER(a.email) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(a.username,'')) "
                "LIKE ? ESCAPE '\\')"
            )
            parameters += [pattern, pattern]
            chosen["q"] = search
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, tuple(parameters), chosen

    def account_list(db: Any, now: float, request: Request) -> str:
        where, parameters, chosen = account_filter(request, now)
        total = count(db, "SELECT COUNT(*) AS n FROM (" + ACCOUNT_QUERY + where + ") AS m",
                      parameters)
        try:
            number = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            number = 1
        rows = db.execute(
            ACCOUNT_QUERY + where + ACCOUNT_ORDER + " LIMIT ? OFFSET ?",
            (*parameters, PAGE_SIZE, (number - 1) * PAGE_SIZE),
        ).fetchall()
        view = chosen["view"]
        out = (
            '<section class="cp cp-admin"><p><a href="/admin">Back to admin</a></p>'
            "<h1>Accounts</h1><nav>"
            + "".join(
                f'<a href="/admin/accounts?{urlencode({"view": key})}"'
                + (' aria-current="page"' if key == view and len(chosen) == 1 else "")
                + f">{esc(label)}</a>"
                for key, label in ACCOUNT_VIEWS.items()
            )
            + '</nav><form class="cp-find" method="get" action="/admin/accounts">'
            f'<input type="hidden" name="view" value="{esc(view)}">'
            f'<input name="q" type="search" value="{esc(chosen.get("q", ""))}" '
            'placeholder="Search email or username" aria-label="Search email or username">'
            "<button>Search</button></form>"
        )
        label = ACCOUNT_VIEWS[view]
        if "status" in chosen:
            label += f", subscription {chosen['status']}"
        if "creator" in chosen:
            name = db.execute(
                "SELECT name FROM creator_profiles WHERE account=?", (chosen["creator"],)
            ).fetchone()
            label += f", referred by {name['name'] if name else 'an unknown creator'}"
        if "q" in chosen:
            label += f", matching \u201c{chosen['q']}\u201d"
        export = "/admin/accounts.csv?" + urlencode(chosen)
        out += (
            f'<p class="cp-note">{esc(label)}: {total} account{"" if total == 1 else "s"}. '
            f'<a href="{esc(export)}">Download as CSV</a></p>'
        )
        if not rows:
            return out + '<div class="cp-card"><p class="cp-note">No accounts here.</p></div>'
        out += (
            '<div class="cp-card cp-scroll"><table><thead><tr><th>Email</th><th>Joined</th>'
            "<th>Verified</th><th>Subscription</th><th>Referred by</th><th>Free access</th>"
            "</tr></thead><tbody>"
        )
        for row in rows:
            joined = day_label(row["joined"]) if row["joined"] else "Before 15 Oct 2026"
            grant = (
                f"Until {day_label(row['grant_ends'])}" if live_grant(row, now) else ""
            )
            out += (
                f'<tr><td><a href="/admin/accounts/{esc(row["id"])}">{esc(row["email"])}</a>'
                + (f"<br><small>@{esc(row['username'])}</small>" if row["username"] else "")
                + f"</td><td>{esc(joined)}</td>"
                f"<td>{'Yes' if row['email_verified'] else 'No'}</td>"
                f"<td>{esc(row['status'] or 'inactive')}</td>"
                f"<td>{esc(row['referrer'] or '')}</td><td>{esc(grant)}</td></tr>"
            )
        out += "</tbody></table></div>"
        pages = []
        if number > 1:
            pages.append(
                f'<a href="/admin/accounts?{urlencode({**chosen, "page": number - 1})}">'
                "Previous</a>"
            )
        if number * PAGE_SIZE < total:
            pages.append(
                f'<a href="/admin/accounts?{urlencode({**chosen, "page": number + 1})}">'
                "Next</a>"
            )
        if pages:
            out += f'<p class="cp-note">Page {number}. ' + " &middot; ".join(pages) + "</p>"
        return out + "</section>"

    def account_detail(db: Any, token: str, now: float, account_id: str) -> str:
        row = db.execute(
            ACCOUNT_QUERY + " WHERE a.id=?", (account_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Not Found")
        providers = [
            r["provider"] for r in db.execute(
                "SELECT provider FROM social_identities WHERE account=? ORDER BY provider",
                (account_id,),
            )
        ]
        usage_row = db.execute(
            "SELECT bytes,changes FROM account_usage WHERE account=?", (account_id,)
        ).fetchone()
        records = count(db, "SELECT COUNT(*) AS n FROM records WHERE account=?", (account_id,))
        devices = count(
            db,
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT client FROM credentials WHERE "
            "account=? AND kind='refresh' AND expires>?) AS live",
            (account_id, now),
        )
        facts = [
            ("Email", row["email"]),
            ("Username", "@" + row["username"] if row["username"] else "None"),
            ("Joined", f"{day_label(row['joined'])}, {instant(row['joined']):%H:%M} UTC"
             if row["joined"] else "Before 15 Oct 2026 (not recorded)"),
            ("Email verified", "Yes" if row["email_verified"] else "No"),
            ("Signs in with", ", ".join(p.title() for p in providers) or "Email and password"),
            ("Subscription", row["status"] or "inactive"),
        ]
        if row["subscription"]:
            facts.append(("Paddle subscription", row["subscription"]))
        if row["billing_at"]:
            facts.append(("Last billing event", str(row["billing_at"])))
        if row["grant_role"]:
            offer = "Creator, 3 months" if row["grant_role"] == "creator" else "Invited, 1 month"
            state = (
                f"until {day_label(row['grant_ends'])}" if live_grant(row, now)
                else f"revoked {day_label(row['grant_revoked'])}" if row["grant_revoked"]
                else f"ended {day_label(row['grant_ends'])}"
            )
            facts.append(("Free Cloud access", f"{offer}, {state}"))
        facts += [
            ("Synced records", f"{records:,}"),
            ("Stored", megabytes(usage_row["bytes"] or 0) if usage_row else "0.0 MB"),
            ("Connected devices and apps", str(devices)),
        ]
        out = (
            '<section class="cp cp-admin"><p><a href="/admin/accounts">All accounts</a></p>'
            f"<h1>{esc(row['email'])}</h1>"
            '<div class="cp-card"><table class="cp-facts"><tbody>'
            + "".join(
                f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>"
                for label, value in facts
            )
        )
        if row["referrer_account"]:
            out += (
                "<tr><th>Referred by</th><td>"
                f'<a href="/admin/accounts/{esc(row["referrer_account"])}">'
                f"{esc(row['referrer'] or 'a creator')}</a>, "
                f"{day_label(row['attributed_at'])}</td></tr>"
            )
        out += "</tbody></table>"
        if live_grant(row, now):
            out += "<p>" + button(token, "revoke", "Revoke free access", account=account_id)
            out += "</p>"
        out += "</div>"
        payments = db.execute(
            "SELECT id,currency,completed_at,net,tax,total FROM billing_revenue WHERE "
            "account=? ORDER BY completed_at DESC LIMIT 50",
            (account_id,),
        ).fetchall()
        out += '<div class="cp-card"><h2>Payments</h2>'
        if payments:
            out += (
                '<div class="cp-scroll"><table><thead><tr><th>Date</th><th>Excl. tax</th>'
                "<th>Tax</th><th>Total</th><th>Transaction</th></tr></thead><tbody>"
            )
            for payment in payments:
                currency = payment["currency"]
                out += (
                    f"<tr><td>{day_label(payment['completed_at'])}</td>"
                    f"<td>{money(payment['net'], currency)}</td>"
                    f"<td>{money(payment['tax'], currency)}</td>"
                    f"<td>{money(payment['total'], currency)}</td>"
                    f"<td><code>{esc(payment['id'])}</code></td></tr>"
                )
            out += "</tbody></table></div></div>"
        else:
            out += (
                '<p class="cp-note">No payments recorded. Payments before 15 Oct 2026 are '
                "not listed here.</p></div>"
            )
        profile = db.execute(
            "SELECT * FROM creator_profiles WHERE account=?", (account_id,)
        ).fetchone()
        if profile:
            out += creator_detail(db, now, profile)
        return out + "</section>"

    def creator_detail(db: Any, now: float, profile: Any) -> str:
        creator = profile["account"]
        out = (
            '<div class="cp-card"><h2>Creator</h2><table class="cp-facts"><tbody>'
            f"<tr><th>Name</th><td>{esc(profile['name'])}</td></tr>"
            f"<tr><th>Status</th><td>{esc(profile['status'])}</td></tr>"
            f"<tr><th>Link</th><td>{esc(share_link(settings, profile['code']))}</td></tr>"
            f'<tr><th>Channel</th><td><a href="{esc(profile["channel"])}" '
            'rel="nofollow noopener noreferrer" target="_blank">'
            f"{esc(profile['channel'])}</a></td></tr>"
        )
        if profile["payout_method"]:
            out += (
                "<tr><th>Payout</th><td>"
                f"{esc(PAYOUT_METHODS.get(profile['payout_method'], ''))}, "
                f"{esc(payout_hint(vault.open(profile['payout_address'])))}</td></tr>"
            )
        rows = ledger_rows(db, creator)
        earned = ", ".join(
            money(total["earned"], currency)
            for currency, total in sorted(balances(rows, now).items())
        ) or "None yet"
        out += f"<tr><th>Earned</th><td>{esc(earned)}</td></tr></tbody></table>"
        referred = db.execute(
            "SELECT a.id,a.email,a.status,r.attributed_at FROM creator_referrals r "
            "LEFT JOIN accounts a ON a.id=r.account WHERE r.creator=? "
            "ORDER BY r.attributed_at DESC LIMIT 25",
            (creator,),
        ).fetchall()
        total = count(
            db, "SELECT COUNT(*) AS n FROM creator_referrals WHERE creator=?", (creator,)
        )
        out += f"<h2 style=\"margin-top:18px\">Referred sign-ups ({total})</h2>"
        if not referred:
            return out + '<p class="cp-note">None yet.</p></div>'
        out += "<ul>"
        for item in referred:
            name = esc(item["email"] or "deleted account")
            link = (
                f'<a href="/admin/accounts/{esc(item["id"])}">{name}</a>' if item["id"] else name
            )
            out += (
                f"<li>{link} <small>{day_label(item['attributed_at'])}, "
                f"{esc(item['status'] or 'inactive')}</small></li>"
            )
        out += "</ul>"
        if total > len(referred):
            out += (
                f'<p><a href="/admin/accounts?{urlencode({"creator": creator})}">'
                f"All {total} referred accounts</a></p>"
            )
        return out + "</div>"

    @app.post("/admin/action")
    async def admin_action(request: Request):
        data = await form(request)
        csrf(request, data)
        owner = account(request, browser=True)
        if not is_admin(owner):
            raise HTTPException(404, "Not Found")
        if not await asyncio.to_thread(
            store.allow_request, digest("admin-action:" + owner["id"]), 60, 30
        ):
            raise HTTPException(429, "Too many requests; try again in a minute")
        action = data.get("action", "")
        target = data.get("account", "")
        now = time.time()
        approved_email = ""
        if action == "payout":
            account(request, browser=True, recent=True)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if action == "applications":
                put_setting(db, "applications", "open" if data.get("value") == "open"
                            else "closed")
            elif action == "invite_creator":
                invite_creator(db, data.get("email", ""), data.get("code", ""),
                               data.get("name", ""))
            elif action in ("approve", "reject"):
                row = db.execute(
                    "SELECT p.status,a.email FROM creator_profiles p LEFT JOIN accounts a "
                    "ON a.id=p.account WHERE p.account=?", (target,)
                ).fetchone()
                if not row or row["status"] != "applied":
                    raise HTTPException(409, "That application was already decided")
                db.execute(
                    "UPDATE creator_profiles SET status=?,decided_at=? WHERE account=?",
                    ("approved" if action == "approve" else "rejected", now, target),
                )
                if action == "approve":
                    approved_email = row["email"] or ""
            elif action in ("suspend", "reinstate"):
                db.execute(
                    "UPDATE creator_profiles SET status=? WHERE account=? AND status=?",
                    ("suspended", target, "approved") if action == "suspend"
                    else ("approved", target, "suspended"),
                )
            elif action == "resolve":
                db.execute(
                    "UPDATE billing_adjustments SET resolved=1 WHERE id=? AND "
                    "action='chargeback_warning'", (data.get("id", ""),),
                )
            elif action == "revoke":
                db.execute(
                    "UPDATE cloud_grants SET revoked_at=? WHERE account=? AND revoked_at IS NULL",
                    (now, target),
                )
            elif action == "payout":
                record_payout(db, data, now)
            else:
                raise HTTPException(422, "Unknown action")
        if approved_email:
            await asyncio.to_thread(
                send_mail, settings, store, approved_email, APPROVAL_SUBJECT,
                APPROVAL_BODY.format(portal=settings.public_url.rstrip("/") + "/creators"),
            )
        return private(RedirectResponse(f"/admin?done={action}", status_code=303))

    def record_payout(db: Any, data: dict, now: float) -> None:
        reference = " ".join(data.get("reference", "").split())
        if not 1 <= len(reference) <= 120:
            raise HTTPException(422, "Enter the payment reference")
        try:
            as_of = datetime.fromtimestamp(int(data.get("as_of", "")), UTC)
            expected = int(data.get("amount", ""))
        except ValueError:
            raise HTTPException(422, "Invalid payout") from None
        if as_of != payout_cutoff(instant(now)):
            raise HTTPException(409, "A new payout month started; reload the admin page")
        # Recompute under the write lock so a refund that just arrived changes the amount.
        preview = payout_preview(policy_rows(ledger_rows(db)), as_of)
        item = next(
            (
                p for p in preview["payouts"]
                if p["creator"] == data.get("creator") and p["currency"] == data.get("currency")
            ),
            None,
        )
        if not item or item["amount"] != expected:
            raise HTTPException(409, "The amount due changed; reload the admin page")
        profile = db.execute(
            "SELECT payout_method,payout_address FROM creator_profiles WHERE account=?",
            (item["creator"],),
        ).fetchone()
        if not profile or not profile["payout_method"] or not profile["payout_address"]:
            raise HTTPException(409, "This creator has no payout details yet")
        payout = secrets.token_hex(12)
        db.execute(
            "INSERT INTO creator_payouts(id,creator,currency,as_of,amount,method,address,"
            "reference,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (payout, item["creator"], item["currency"], as_of.timestamp(), item["amount"],
             profile["payout_method"], profile["payout_address"], reference, now),
        )
        for transaction_id, amount in item["lines"].items():
            db.execute(
                "INSERT INTO creator_payout_lines(payout,transaction_id,amount) VALUES(?,?,?)",
                (payout, transaction_id, amount),
            )


ACCOUNT_QUERY = (
    "SELECT a.id,a.email,a.username,a.email_verified,a.status,a.subscription,a.billing_at,"
    "c.at AS joined,r.creator AS referrer_account,r.attributed_at,p.name AS referrer,"
    "g.role AS grant_role,g.ends_at AS grant_ends,g.revoked_at AS grant_revoked "
    "FROM accounts a LEFT JOIN account_created c ON c.account=a.id "
    "LEFT JOIN creator_referrals r ON r.account=a.id "
    "LEFT JOIN creator_profiles p ON p.account=r.creator "
    "LEFT JOIN cloud_grants g ON g.account=a.id"
)
ACCOUNT_ORDER = " ORDER BY COALESCE(c.at,0) DESC, a.email"

NOTICES = {
    "applications": "Application setting saved.",
    "invite_creator": "Invitation saved. This creator can join without an application review.",
    "approve": "Approved. The creator was emailed their dashboard link.",
    "reject": "Application rejected.",
    "suspend": "Creator paused. Their link stops adding referrals and payouts wait.",
    "reinstate": "Creator reinstated.",
    "resolve": "Marked resolved.",
    "revoke": "Free access revoked.",
    "payout": "Payout recorded.",
}
