"""Prepare or inspect a synthetic sandbox purchase inside the cloud container."""

import argparse
import json
import secrets

import httpx

from varyz.cloud.app import LOCAL_CLIENT, Settings, password_hash
from varyz.cloud.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["prepare", "status", "cancel", "delete", "repair"]
    )
    parser.add_argument("--account")
    parser.add_argument("--event-id")
    parser.add_argument("--annual", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    assert settings.paddle_sandbox, "Sandbox only"
    store = Store(settings.database)
    owner = args.account
    if args.action == "prepare":
        owner = "payment_probe_" + secrets.token_hex(8)
        with store.connection() as db:
            db.execute(
                "INSERT INTO accounts(id,email,password,email_verified) VALUES(?,?,?,1)",
                (owner, owner + "@example.test", password_hash(secrets.token_urlsafe(32))),
            )
    assert owner and owner.startswith("payment_probe_"), "Synthetic account required"
    if args.action == "delete":
        with store.connection() as db:
            row = db.execute("SELECT subscription FROM accounts WHERE id=?", (owner,)).fetchone()
        assert row and row["subscription"]
        with httpx.Client(
            base_url="https://sandbox-api.paddle.com",
            headers={"Authorization": "Bearer " + settings.paddle_api_key},
            timeout=30,
        ) as provider:
            subscription = provider.get("/subscriptions/" + row["subscription"])
            subscription.raise_for_status()
            assert subscription.json()["data"]["custom_data"]["varyz_account_id"] == owner
            # Simulations change the receiver's state, not Paddle's subscription.
            # Restore the actual provider status before testing account deletion.
            with store.connection() as db:
                db.execute(
                    "UPDATE accounts SET status=? WHERE id=?",
                    (subscription.json()["data"]["status"], owner),
                )
    if args.action == "repair":
        assert args.event_id and args.event_id.startswith("evt_")
        with store.connection() as db:
            payments = db.execute(
                "SELECT * FROM billing_payments WHERE account=?", (owner,)
            ).fetchall()
        with httpx.Client(
            base_url="https://sandbox-api.paddle.com",
            headers={"Authorization": "Bearer " + settings.paddle_api_key},
            timeout=30,
        ) as provider:
            for payment in payments:
                response = provider.get("/transactions/" + payment["id"])
                response.raise_for_status()
                data = response.json()["data"]
                assert data["custom_data"]["varyz_account_id"] == owner
                assert data["subscription_id"] == payment["subscription"]
                with store.connection() as db:
                    db.execute(
                        "UPDATE billing_payments SET grand_total=?,currency=? "
                        "WHERE id=? AND account=?",
                        (
                            int(data["details"]["totals"]["grand_total"]),
                            data["currency_code"],
                            payment["id"],
                            owner,
                        ),
                    )
        with store.connection() as db:
            db.execute("DELETE FROM events WHERE id=?", (args.event_id,))
        print("PASS synthetic payment total repaired; provider event ready for replay")
        return
    token = store.credential(owner, "access", 600, LOCAL_CLIENT)
    with httpx.Client(
        base_url=settings.public_url, headers={"Authorization": "Bearer " + token}, timeout=45
    ) as client:
        if args.action == "prepare":
            response = client.post(
                "/api/billing/checkout",
                json={"price_id": settings.paddle_prices[int(args.annual)]},
            )
        elif args.action == "cancel":
            response = client.post("/api/billing/cancel")
        elif args.action == "delete":
            response = client.delete("/api/account")
        else:
            response = client.get("/api/account")
        print(
            json.dumps(
                {"account": owner, "status": response.status_code, "result": response.json()}
            )
        )
    if args.action == "delete":
        assert response.status_code == 200
        with httpx.Client(
            base_url="https://sandbox-api.paddle.com",
            headers={"Authorization": "Bearer " + settings.paddle_api_key},
            timeout=30,
        ) as provider:
            final = provider.get("/subscriptions/" + row["subscription"])
            final.raise_for_status()
            assert final.json()["data"]["status"] == "canceled"
        with store.connection() as db:
            assert not db.execute("SELECT 1 FROM accounts WHERE id=?", (owner,)).fetchone()
        print("PASS synthetic account removed and actual sandbox subscription canceled")
    if args.action == "status":
        with store.connection() as db:
            row = db.execute("SELECT subscription FROM accounts WHERE id=?", (owner,)).fetchone()
            payments = db.execute(
                "SELECT id FROM billing_payments WHERE account=?", (owner,)
            ).fetchall()
            refunds = db.execute(
                "SELECT r.id FROM billing_refunds r JOIN billing_payments p "
                "ON p.id=r.transaction_id WHERE p.account=?",
                (owner,),
            ).fetchall()
            print(
                json.dumps(
                    {
                        "subscription": row["subscription"] if row else None,
                        "payments": [r["id"] for r in payments],
                        "refunds": [r["id"] for r in refunds],
                    }
                )
            )
    with store.connection() as db:
        from varyz.cloud.store import digest

        db.execute("DELETE FROM credentials WHERE hash=?", (digest(token),))


if __name__ == "__main__":
    main()
