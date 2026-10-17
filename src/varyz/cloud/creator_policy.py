"""Creator program rules: commission, refund hold, free Cloud months and monthly payouts.

Pure functions, no database and no money movement. Amounts are integer minor units
(cents) in the payment's own currency; currencies are never converted or mixed.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

COMMISSION_PERCENT = 30
HOLD_DAYS = 30
CREATOR_MONTHS = 3
AUDIENCE_MONTHS = 1
ATTRIBUTION_DAYS = 30
TERMS_VERSION = "2026-10-15"

# Every method is paid by hand from the operator's own accounts. Card numbers and Apple Pay
# are deliberately absent: pushing money to a card needs a payout processor, and storing
# card numbers would bring card-industry security rules with it.
PAYOUT_METHODS = {
    "paypal": "PayPal",
    "wise": "Wise",
    "payoneer": "Payoneer",
    "revolut": "Revolut",
    "bank": "Bank transfer",
}
EMAIL_METHODS = ("paypal", "wise", "payoneer")
EMAIL = re.compile(r"[^\s@]{1,100}@[^\s@]{1,100}\.[^\s@]{1,40}")
REVTAG = re.compile(r"@?([a-z0-9_.-]{3,32})")
IBAN = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}")
BIC = re.compile(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?")


def _amount(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Amounts must be nonnegative integer minor units")
    return value


def _instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return value.astimezone(UTC)


def commission_minor(net_revenue: int, approved_refund: int = 0) -> int:
    """30% of what remains after refunds, rounded half up, never negative."""
    net, refund = _amount(net_revenue), _amount(approved_refund)
    remaining = max(net - refund, 0)
    return (remaining * COMMISSION_PERCENT + 50) // 100


def hold_until(completed_at: datetime) -> datetime:
    return _instant(completed_at) + timedelta(days=HOLD_DAYS)


def add_months(start: datetime, months: int) -> datetime:
    """Calendar months, clamped to the last day of a shorter month."""
    start = _instant(start)
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def grant_end(role: str, started_at: datetime) -> datetime:
    if role not in ("creator", "audience"):
        raise ValueError("Grant role must be creator or audience")
    return add_months(started_at, CREATOR_MONTHS if role == "creator" else AUDIENCE_MONTHS)


def payout_cutoff(now: datetime) -> datetime:
    """The latest payout cutoff: the first day of this month at 00:00 UTC."""
    now = _instant(now)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


@dataclass(frozen=True)
class LedgerRow:
    transaction_id: str
    creator: str
    currency: str
    completed_at: datetime
    net_revenue: int
    approved_refund: int = 0
    paid_commission: int = 0
    disputed: bool = False


def payout_preview(rows: Iterable[LedgerRow], as_of: datetime) -> dict:
    """What each creator is owed per currency at a monthly cutoff.

    A commission is payable once its payment has cleared the hold before the cutoff.
    Refunds after a payout become debts that offset later commissions. Any open
    dispute or pending refund holds that creator's whole batch in that currency,
    because it can still turn into a debt.
    """
    cutoff = _instant(as_of)
    if (cutoff.day, cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (
        1, 0, 0, 0, 0,
    ):
        raise ValueError("Use the first day of the month at 00:00:00 UTC")
    seen: set[str] = set()
    totals: dict[tuple[str, str], dict] = {}
    blocked: set[tuple[str, str]] = set()
    held: list[str] = []
    debts: list[dict] = []
    for row in rows:
        if not row.transaction_id or not row.creator or not (
            len(row.currency) == 3 and row.currency.isalpha() and row.currency.isupper()
        ):
            raise ValueError("Incomplete ledger row")
        if row.transaction_id in seen:
            raise ValueError("Duplicate transaction")
        seen.add(row.transaction_id)
        amount = commission_minor(row.net_revenue, row.approved_refund)
        paid = _amount(row.paid_commission)
        key = (row.creator, row.currency)
        if row.disputed:
            blocked.add(key)
        if row.disputed or hold_until(row.completed_at) > cutoff:
            held.append(row.transaction_id)
            continue
        if paid > amount:
            debts.append({
                "transaction_id": row.transaction_id, "creator": row.creator,
                "currency": row.currency, "amount": paid - amount,
            })
        total = totals.setdefault(key, {
            "creator": row.creator, "currency": row.currency, "amount": 0, "lines": {},
        })
        total["amount"] += amount - paid
        if amount != paid:
            total["lines"][row.transaction_id] = amount - paid
    return {
        "as_of": cutoff,
        "payouts": [
            total for key, total in totals.items() if total["amount"] > 0 and key not in blocked
        ],
        "held": held,
        "blocked": [
            {"creator": creator, "currency": currency} for creator, currency in sorted(blocked)
        ],
        "debts": debts,
    }


def iban_ok(value: str) -> bool:
    """ISO 13616 check digits: move the first four characters to the end, then mod 97."""
    if not IBAN.fullmatch(value):
        return False
    digits = "".join(str(int(char, 36)) for char in value[4:] + value[:4])
    return int(digits) % 97 == 1


def payout_details(method: str, fields: dict) -> dict:
    """Validate what a creator typed for one payout method; ValueError explains a problem."""

    def field(name: str) -> str:
        value = str(fields.get(name) or "").strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("Payout details cannot contain control characters")
        return value

    if method in EMAIL_METHODS:
        email = field("address")
        if not EMAIL.fullmatch(email):
            raise ValueError(f"Enter the email of your {PAYOUT_METHODS[method]} account")
        return {"email": email}
    if method == "revolut":
        tag = REVTAG.fullmatch(field("address").lower())
        if not tag:
            raise ValueError("Enter your Revolut Revtag, for example @alexsmith")
        return {"revtag": "@" + tag.group(1)}
    if method != "bank":
        raise ValueError("Choose a payout method")
    holder = " ".join(field("holder").split())
    if not 2 <= len(holder) <= 140:
        raise ValueError("Enter the account holder's full name")
    account = re.sub(r"[\s-]", "", field("account")).upper()
    code = re.sub(r"[\s-]", "", field("bank_code")).upper()
    country = field("country").upper()
    if IBAN.fullmatch(account):
        if not iban_ok(account):
            raise ValueError("That IBAN does not check out; compare it with your bank app")
        country = account[:2]
    elif not re.fullmatch(r"[A-Z0-9]{4,34}", account):
        raise ValueError("Enter your IBAN, or your account number")
    elif not code:
        raise ValueError("An account number needs a SWIFT/BIC, routing number or sort code")
    if code and not (BIC.fullmatch(code) or re.fullmatch(r"[0-9]{6,11}", code)):
        raise ValueError("Enter a SWIFT/BIC, a routing number or a sort code")
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Enter the bank's two-letter country code, for example US")
    return {"holder": holder, "account": account, "bank_code": code, "country": country}


def payout_lines(details: dict) -> list[tuple[str, str]]:
    """Every saved detail, labelled, for the operator who sends the payment."""
    labels = (("email", "Email"), ("revtag", "Revtag"), ("holder", "Account holder"),
              ("account", "Account"), ("bank_code", "Bank code"), ("country", "Country"),
              ("note", "Note"))
    return [(label, str(details[key])) for key, label in labels if details.get(key)]


def payout_hint(details: dict) -> str:
    """A short description that never shows a full bank account number."""
    if details.get("account"):
        return f"{details.get('holder', '')}, account ending {details['account'][-4:]}"
    return str(details.get("email") or details.get("revtag") or details.get("note") or "")
