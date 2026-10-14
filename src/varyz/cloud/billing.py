"""Verified payment/refund ledger, independent of subscription lifecycle order."""

from datetime import datetime


def event_time(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Billing timestamp needs a timezone")
    return stamp.timestamp()


def record_payment(db, data, occurred, allowed_prices):
    subscription = data.get("subscription_id")
    prices = {item["price"]["id"] for item in data.get("items", [])}
    if (
        data.get("status") != "completed"
        or not subscription
        or not prices
        or not prices <= set(allowed_prices)
        or int(data["details"]["totals"]["grand_total"]) <= 0
    ):
        return
    owner_id = (data.get("custom_data") or {}).get("varyz_account_id")
    owner = db.execute("SELECT * FROM accounts WHERE id=?", (owner_id,)).fetchone()
    if not owner:
        owner = db.execute(
            "SELECT * FROM accounts WHERE subscription=?", (subscription,)
        ).fetchone()
    if not owner or (
        owner["subscription"]
        and owner["subscription"] != subscription
        and owner["status"] != "canceled"
    ):
        return
    period = data.get("billing_period") or {}
    start = event_time(period.get("starts_at") or occurred)
    db.execute(
        "INSERT INTO billing_payments(id,account,subscription,period_start,completed_at,"
        "grand_total,currency) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (
            data["id"],
            owner["id"],
            subscription,
            start,
            event_time(occurred),
            int(data["details"]["totals"]["grand_total"]),
            data.get("currency_code"),
        ),
    )


def record_refund(db, data):
    # Paddle's dashboard can refund the entire amount with type='partial'. Keep
    # approved references and compare signed amounts/currencies with the payment,
    # even when the refund arrives first. Smaller refunds do not revoke access.
    if (
        data.get("action") == "refund"
        and data.get("type") in ("full", "partial")
        and data.get("status") == "approved"
    ):
        db.execute(
            "INSERT INTO billing_refunds(id,transaction_id,subscription,refund_type,"
            "total,currency) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                data["id"],
                data["transaction_id"],
                data.get("subscription_id"),
                data["type"],
                int((data.get("totals") or {}).get("total", "0")),
                data.get("currency_code"),
            ),
        )


def effective_account(db, owner):
    result = dict(owner)
    if result["status"] == "active" and result["subscription"]:
        latest = db.execute(
            "SELECT id,grand_total,currency FROM billing_payments "
            "WHERE account=? AND subscription=? "
            "ORDER BY period_start DESC,completed_at DESC,id DESC LIMIT 1",
            (result["id"], result["subscription"]),
        ).fetchone()
        if (
            latest
            and db.execute(
                "SELECT 1 FROM billing_refunds WHERE transaction_id=? AND "
                "(refund_type='full' OR (refund_type='partial' AND total=? AND total>0 "
                "AND currency=?))",
                (latest["id"], latest["grand_total"], latest["currency"]),
            ).fetchone()
        ):
            result["status"] = "refunded"
    return result
