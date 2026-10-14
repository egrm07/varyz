"""Offline SQL boundary checks and opt-in real PostgreSQL concurrency gates.

Set VARYZ_TEST_POSTGRES_DSN to a disposable PostgreSQL database and run with
`pytest -m live tests/test_cloud_postgres.py`. Each test uses a unique schema;
the fixture never modifies existing application tables.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from varyz.cloud.postgres import PostgresStore, parameter_sql
from varyz.cloud.store import Conflict, QuotaExceeded, Store, digest


def test_postgres_placeholder_translation_preserves_sql_literals():
    statement = (
        "SELECT '?' AS literal, \"?\", 'it''s ?' FROM accounts WHERE email=? AND email LIKE '%?'"
    )
    assert parameter_sql(statement) == (
        "SELECT '?' AS literal, \"?\", 'it''s ?' FROM accounts WHERE email=%s AND email LIKE '%%?'"
    )


def test_postgres_overrides_sqlite_question_and_email_methods():
    assert PostgresStore.queue_question is not Store.queue_question
    assert PostgresStore.allow_email is not Store.allow_email


def test_sqlite_username_migration_and_racing_claims(tmp_path):
    path = tmp_path / "legacy-usernames.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE accounts(id TEXT PRIMARY KEY,email TEXT UNIQUE,"
            "password TEXT NOT NULL,subscription TEXT,status TEXT,billing_at TEXT)"
        )
        db.execute(
            "INSERT INTO accounts(id,email,password) VALUES('legacy','old@example.test','hash')"
        )
    store = Store(path)
    Store(path)  # migration is repeatable; legacy users are not assigned names
    with store.connection() as db:
        assert db.execute("SELECT username FROM accounts WHERE id='legacy'").fetchone()[0] is None
    assert _racing_username_claims(store, Store(path)) == [False, True]


def _racing_username_claims(first, second):
    gate = threading.Barrier(2)

    def claim(store, identifier, username):
        gate.wait(timeout=10)
        try:
            with store.connection() as db:
                db.execute(
                    "INSERT INTO accounts(id,email,password,username) VALUES(?,?,?,?)",
                    (identifier, identifier + "@example.test", "hash", username),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, first, "claim_one", "Egor_07"),
            executor.submit(claim, second, "claim_two", "egor_07"),
        ]
        return sorted(future.result(timeout=15) for future in futures)


@pytest.mark.live
def test_postgres_racing_username_claims(stores):
    assert _racing_username_claims(*stores) == [False, True]


def test_existing_sqlite_billing_ledger_upgrades_without_losing_refunds(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE billing_payments(id TEXT PRIMARY KEY,account TEXT,
              subscription TEXT,period_start REAL,completed_at REAL);
            CREATE TABLE billing_refunds(id TEXT PRIMARY KEY,transaction_id TEXT,subscription TEXT);
            INSERT INTO billing_payments VALUES('txn_old','alice','sub_old',1,1);
            INSERT INTO billing_refunds VALUES('adj_old','txn_old','sub_old');
        """)
    for _ in range(2):
        store = Store(path)
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM billing_payments").fetchone()[0] == 1
            assert db.execute("SELECT refund_type FROM billing_refunds").fetchone()[0] == "full"


@pytest.mark.live
def test_postgres_dashboard_full_amount_refund_before_payment(stores):
    from varyz.cloud.billing import effective_account, record_payment, record_refund

    first, _ = stores
    with first.connection() as db:
        db.execute("UPDATE accounts SET status='active',subscription='sub_paid' WHERE id='alice'")
        record_refund(
            db,
            dict(
                id="adj_paid",
                transaction_id="txn_paid",
                action="refund",
                type="partial",
                status="approved",
                totals={"total": "605"},
                currency_code="USD",
            ),
        )
        record_payment(
            db,
            dict(
                id="txn_paid",
                subscription_id="sub_paid",
                status="completed",
                items=[{"price": {"id": "pri_paid"}}],
                details={"totals": {"grand_total": "605"}},
                currency_code="USD",
                custom_data={"varyz_account_id": "alice"},
            ),
            "2026-10-03T11:00:00Z",
            ("pri_paid",),
        )
        owner = db.execute("SELECT * FROM accounts WHERE id='alice'").fetchone()
        assert effective_account(db, owner)["status"] == "refunded"
        other = db.execute("SELECT * FROM accounts WHERE id='bob'").fetchone()
        assert effective_account(db, other)["status"] == "inactive"


@pytest.fixture
def stores():
    dsn = os.environ.get("VARYZ_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Real PostgreSQL required: set VARYZ_TEST_POSTGRES_DSN")
    psycopg = pytest.importorskip("psycopg")
    schema = "varyz_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
        )
    try:
        first = PostgresStore(dsn, schema=schema, pool_max=2)
        second = PostgresStore(dsn, schema=schema, pool_max=2)
        with first.connection() as db:
            for account in ("alice", "bob"):
                db.execute(
                    "INSERT INTO accounts(id,email,password) VALUES(?,?,?)",
                    (account, account + "@example.test", "test-hash"),
                )
        yield first, second
    finally:
        if "first" in locals():
            first.close()
        if "second" in locals():
            second.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


def rule(identifier="r1", base=0, statement="Use concise prose"):
    return dict(
        kind="rule", id=identifier, base_version=base, payload=dict(statement=statement, scope="*")
    )


@pytest.mark.live
def test_postgres_tenant_retry_tombstone_and_atomic_batch(stores):
    first, second = stores
    saved = first.push("alice", [rule()])[0]
    assert second.pull("bob", 0, 10)["records"] == []
    assert second.push("alice", [rule()])[0] == saved
    with pytest.raises(Conflict):
        second.push("alice", [rule("r2"), rule(base=0, statement="Different")])
    assert first.pull("alice", 0, 10)["records"] == [saved]
    assert first.knowledge("alice", "demo") == [saved]
    assert first.knowledge("bob", "demo") == []
    tombstone = dict(kind="rule", id="r1", base_version=1, payload=None)
    deleted = second.push("alice", [tombstone])[0]
    assert deleted["cursor"] == saved["cursor"] + 1
    assert first.pull("alice", saved["cursor"], 10)["records"] == [deleted]
    assert first.knowledge("alice", "demo") == []


@pytest.mark.live
def test_postgres_concurrent_compare_and_swap_has_one_winner(stores):
    first, second = stores
    first.push("alice", [rule()])
    barrier = threading.Barrier(2)

    def write(store, text):
        barrier.wait(timeout=5)
        try:
            return store.push("alice", [rule(base=1, statement=text)])
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: write(*args), [(first, "A"), (second, "B")]))
    assert results.count("conflict") == 1
    changes = first.pull("alice", 0, 10)["records"]
    assert [r["version"] for r in changes] == [1, 2]
    assert [r["cursor"] for r in changes] == [1, 2]


@pytest.mark.live
def test_postgres_later_cursor_cannot_commit_before_earlier_write(stores):
    first, second = stores
    started = threading.Event()

    def later_write():
        started.set()
        return second.push("bob", [rule("later")])

    with ThreadPoolExecutor(max_workers=1) as executor:
        with first.connection() as db:
            clock = db.execute("SELECT value FROM sync_clock WHERE id=1 FOR UPDATE").fetchone()
            cursor = clock["value"] + 1
            db.execute(
                "INSERT INTO changes(cursor,account,kind,id,version,payload) VALUES(?,?,?,?,?,?)",
                (cursor, "alice", "rule", "earlier", 1, "null"),
            )
            db.execute("UPDATE sync_clock SET value=? WHERE id=1", (cursor,))
            future = executor.submit(later_write)
            assert started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
        later = future.result(timeout=5)[0]
        assert later["cursor"] == cursor + 1
        assert first.pull("alice", 0, 10)["records"][0]["cursor"] == cursor
        assert first.pull("bob", 0, 10)["records"][0] == later


@pytest.mark.live
def test_postgres_single_use_credential_across_connections(stores):
    first, second = stores
    credential = first.credential("alice", "code", 60)
    barrier = threading.Barrier(2)

    def consume(store):
        barrier.wait(timeout=5)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM credentials WHERE hash=?", (digest(credential),)
            ).fetchone()
            if current:
                db.execute("DELETE FROM credentials WHERE hash=?", (digest(credential),))
            return current is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(consume, (first, second))) == [False, True]


@pytest.mark.live
def test_postgres_account_deletion_cannot_leave_orphan_records(stores):
    first, second = stores
    first.push("alice", [rule()])
    with first.connection() as db:
        db.execute("DELETE FROM accounts WHERE id=?", ("alice",))
    assert second.pull("alice", 0, 10)["records"] == []
    with pytest.raises(sqlite3.IntegrityError):
        second.push("alice", [rule("after-delete")])


@pytest.mark.live
def test_postgres_shared_rate_limit_counts_all_instances_and_hashes_keys(stores):
    first, second = stores
    private_key = "ip:192.0.2.123:account:alice"
    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(
            executor.map(
                lambda index: (first if index % 2 else second).allow_request(private_key, 3600, 5),
                range(20),
            )
        )
    assert sum(accepted) == 5
    with first.connection() as db:
        rows = db.execute("SELECT key,count FROM rate_limits").fetchall()
    assert rows == [{"key": digest(private_key), "count": 6}]
    assert second.allow_request("independent", 3600, 5)


@pytest.mark.live
def test_postgres_rate_cleanup_is_bounded_and_keeps_current_keys(stores):
    first, _ = stores
    with first.connection() as db:
        now = db.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now"
        ).fetchone()["now"]
        for index in range(120):
            db.execute(
                "INSERT INTO rate_limits(key,window_start,count) VALUES(?,?,?)",
                (digest("expired" + str(index)), now - 8000, 1),
            )
    assert first.allow_request("current", 60, 1)
    with first.connection() as db:
        assert len(db.execute("SELECT * FROM rate_limits").fetchall()) == 21
    assert not first.allow_request("current", 60, 1)
    with first.connection() as db:
        assert len(db.execute("SELECT * FROM rate_limits").fetchall()) == 1


@pytest.mark.live
def test_postgres_quota_is_atomic_shared_and_retry_does_not_spend_again(stores):
    first, second = stores
    saved = first.push("alice", [rule(statement="Über concise prose")])[0]
    with first.connection() as db:
        before = db.execute("SELECT * FROM account_usage WHERE account=?", ("alice",)).fetchone()
    assert second.push("alice", [rule(statement="Über concise prose")])[0] == saved
    with second.connection() as db:
        assert (
            db.execute("SELECT * FROM account_usage WHERE account=?", ("alice",)).fetchone()
            == before
        )
    second.max_changes = 2
    with pytest.raises(QuotaExceeded):
        second.push("alice", [rule("second"), rule("third")])
    assert first.pull("alice", 0, 10)["records"] == [saved]
    with first.connection() as db:
        assert (
            db.execute("SELECT * FROM account_usage WHERE account=?", ("alice",)).fetchone()
            == before
        )
    second.max_storage_bytes = before["bytes"]
    with pytest.raises(QuotaExceeded):
        second.push("alice", [rule("second")])
    assert first.push("bob", [rule("bob")])[0]["cursor"] == saved["cursor"] + 1


@pytest.mark.live
def test_postgres_question_quota_reuse_and_tenant_isolation(stores):
    first, second = stores
    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(
            executor.map(
                lambda i: (first if i % 2 else second).queue_question(
                    "alice", "Which font?", "project"
                ),
                range(20),
            )
        )
    assert len(set(identifiers)) == 1
    with first.connection() as db:
        before = db.execute("SELECT * FROM account_usage WHERE account=?", ("alice",)).fetchone()
        assert (
            len(db.execute("SELECT * FROM questions WHERE account=?", ("alice",)).fetchall()) == 1
        )
        assert db.execute("SELECT value FROM sync_clock WHERE id=1").fetchone()["value"] == 0
    # Pending questions have their own reclaimable budget, not sync history usage.
    assert before is None
    second.max_storage_bytes = len(b"Which font?") + len(b"project") + 128
    with pytest.raises(QuotaExceeded):
        second.queue_question("alice", "Another question?", "project")
    assert first.queue_question("bob", "Which font?", "project") != identifiers[0]
    with first.connection() as db:
        assert (
            db.execute("SELECT * FROM account_usage WHERE account=?", ("alice",)).fetchone()
            == before
        )


@pytest.mark.live
def test_postgres_daily_email_admission_shared_across_instances(stores):
    first, second = stores
    with ThreadPoolExecutor(max_workers=8) as executor:
        allowed = list(
            executor.map(
                lambda i: (first if i % 2 else second).allow_email("global-daily", 5), range(20)
            )
        )
    assert sum(allowed) == 5
    with first.connection() as db:
        rows = db.execute("SELECT key,count FROM email_limits").fetchall()
    assert rows == [{"key": digest("global-daily"), "count": 6}]
