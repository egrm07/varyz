"""SQLite development storage. Every record operation is tenant scoped."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager, nullcontext

from varyz.scopes import normalize_scope


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def scoped_payload(payload):
    """Canonical read/equality view; the stored version and audit bytes stay intact."""
    if payload is None:
        return None
    return {**payload, "scope": normalize_scope(payload.get("scope"))}


class Conflict(Exception):
    def __init__(self, record):
        self.record = record


class QuotaExceeded(Exception):
    """A safety ceiling; changing subscriptions does not increase it."""


class Store:
    def __init__(self, path, max_storage_bytes=50 * 1024 * 1024, max_changes=100000):
        self.path = str(path)
        self.max_storage_bytes = max_storage_bytes
        self.max_changes = max_changes
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,email TEXT UNIQUE,
              password TEXT NOT NULL, subscription TEXT, status TEXT DEFAULT 'inactive',
              billing_at TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS credentials(hash TEXT PRIMARY KEY,account TEXT,
              kind TEXT,expires REAL,client TEXT,redirect TEXT,challenge TEXT);
            CREATE TABLE IF NOT EXISTS credential_grants(
              hash TEXT PRIMARY KEY, scope TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending_signups(email TEXT PRIMARY KEY,
              username TEXT NOT NULL,password TEXT NOT NULL,hash TEXT UNIQUE,expires REAL);
            CREATE INDEX IF NOT EXISTS pending_signups_expiry ON pending_signups(expires);
            CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,redirects TEXT);
            CREATE TABLE IF NOT EXISTS records(account TEXT,kind TEXT,id TEXT,version INTEGER,
              payload TEXT,cursor INTEGER,PRIMARY KEY(account,kind,id));
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS changes(cursor INTEGER PRIMARY KEY AUTOINCREMENT,
              account TEXT,kind TEXT,id TEXT,version INTEGER,payload TEXT);
            CREATE TABLE IF NOT EXISTS questions(account TEXT,id TEXT,question TEXT,project TEXT,
              answered INTEGER DEFAULT 0,PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS away_state(account TEXT,scope TEXT,enabled INTEGER,
              PRIMARY KEY(account,scope));
            CREATE TABLE IF NOT EXISTS varyz_task_routes(namespace TEXT NOT NULL,
              project TEXT NOT NULL,conversation_id TEXT NOT NULL,backend TEXT NOT NULL,
              revision INTEGER NOT NULL,PRIMARY KEY(namespace,project,conversation_id));
            CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY,
              window INTEGER,count INTEGER);
            CREATE TABLE IF NOT EXISTS oauth_flows(hash TEXT PRIMARY KEY,params TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS social_identities(provider TEXT,subject TEXT,account TEXT,
              PRIMARY KEY(provider,subject),UNIQUE(account,provider));
            CREATE TABLE IF NOT EXISTS account_usage(account TEXT PRIMARY KEY,
              bytes INTEGER DEFAULT 0,changes INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS billing_checkouts(account TEXT PRIMARY KEY,
              price TEXT,url TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS billing_payments(id TEXT PRIMARY KEY,account TEXT,
              subscription TEXT,period_start REAL,completed_at REAL);
            CREATE INDEX IF NOT EXISTS billing_payments_account
              ON billing_payments(account,subscription,period_start);
            CREATE TABLE IF NOT EXISTS billing_refunds(id TEXT PRIMARY KEY,
              transaction_id TEXT,subscription TEXT);
            CREATE INDEX IF NOT EXISTS billing_refunds_transaction
              ON billing_refunds(transaction_id);
            CREATE TABLE IF NOT EXISTS cloud_grants(account TEXT PRIMARY KEY,role TEXT NOT NULL,
              creator TEXT,started_at REAL NOT NULL,ends_at REAL NOT NULL,revoked_at REAL);
            CREATE INDEX IF NOT EXISTS cloud_grants_creator ON cloud_grants(creator,started_at);
            CREATE TABLE IF NOT EXISTS email_limits(key TEXT PRIMARY KEY,day INTEGER,count INTEGER);
            CREATE INDEX IF NOT EXISTS credentials_expiry ON credentials(expires);
            CREATE INDEX IF NOT EXISTS credentials_account ON credentials(account);
            CREATE INDEX IF NOT EXISTS changes_account_cursor ON changes(account,cursor);
            CREATE INDEX IF NOT EXISTS records_account_cursor ON records(account,cursor);
            CREATE INDEX IF NOT EXISTS records_dashboard_scope ON records(
              account,COALESCE(json_extract(payload,'$.scope'),'*'),cursor)
              WHERE kind IN ('rule','answer','workflow') AND payload!='null';
            """)
            if "email_verified" not in {
                row["name"] for row in db.execute("PRAGMA table_info(accounts)")
            }:
                db.execute("ALTER TABLE accounts ADD COLUMN email_verified INTEGER DEFAULT 0")
            if "username" not in {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN username TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS accounts_username_unique "
                "ON accounts(lower(username))"
            )
            for table, columns in {
                "accounts": {"subscription_ends_at": "REAL"},
                "billing_payments": {"grand_total": "INTEGER", "currency": "TEXT"},
                "billing_refunds": {
                    "refund_type": "TEXT DEFAULT 'full'",
                    "total": "INTEGER",
                    "currency": "TEXT",
                },
            }.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, declaration in columns.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def credential(self, account, kind, ttl, client="", redirect="", challenge="", scope="varyz"):
        token = secrets.token_urlsafe(48)
        # These kinds do not use the PKCE field. Preserve authentication time
        # across refreshes rather than treating a refresh as another sign-in.
        if kind in ("session", "access", "refresh") and not challenge:
            challenge = str(time.time())
        with self.connection() as db:
            db.execute(
                "INSERT INTO credentials VALUES(?,?,?,?,?,?,?)",
                (digest(token), account, kind, time.time() + ttl, client, redirect, challenge),
            )
            if scope != "varyz":
                db.execute("INSERT INTO credential_grants VALUES(?,?)", (digest(token), scope))
        return token

    def lookup(self, token, kind):
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM credentials WHERE hash=? AND kind=? AND expires>?",
                (digest(token), kind, time.time()),
            ).fetchone()

    def allow_request(self, key, window, limit):
        now = int(time.time())
        current = now // window * window
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Absolute window starts keep minute cleanup from erasing hour
            # counters. Retain two hours, covering every supported window.
            db.execute("DELETE FROM rate_limits WHERE window<?", (now - 7200,))
            db.execute("DELETE FROM pending_signups WHERE expires<=?", (time.time(),))
            db.execute("DELETE FROM credentials WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM credential_grants WHERE hash NOT IN "
                       "(SELECT hash FROM credentials)")
            row = db.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
            if row and row["window"] == current:
                if row["count"] >= limit:
                    return False
                db.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", (key,))
            else:
                # Capacity limits new keys; resetting a known counter adds no
                # rows. Blocking that reset would freeze the global limiter.
                if (
                    not row
                    and db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] >= 10000
                ):
                    return False
                db.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?,?)", (key, current, 1))
        return True

    def allow_email(self, key, limit):
        day = int(time.time()) // 86400
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM email_limits WHERE day<?", (day - 1,))
            row = db.execute("SELECT * FROM email_limits WHERE key=?", (key,)).fetchone()
            if row and row["day"] == day:
                if row["count"] >= limit:
                    return False
                db.execute("UPDATE email_limits SET count=count+1 WHERE key=?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO email_limits VALUES(?,?,?)", (key, day, 1))
        return True

    @staticmethod
    def record(row):
        record = {
            k: json.loads(row[k]) if k == "payload" else row[k]
            for k in ("kind", "id", "version", "payload", "cursor")
        }
        record["payload"] = scoped_payload(record["payload"])
        return record

    def push(self, account, records, *, transaction=None):
        result = []
        with (nullcontext(transaction) if transaction is not None else self.connection()) as db:
            if transaction is None:
                db.execute("BEGIN IMMEDIATE")
            usage = db.execute("SELECT * FROM account_usage WHERE account=?", (account,)).fetchone()
            if usage:
                used_bytes, changes = usage["bytes"], usage["changes"]
            else:
                historical = db.execute(
                    "SELECT COALESCE(SUM(length(CAST(payload AS BLOB))*2+512),0) AS bytes,COUNT(*) AS changes FROM changes WHERE account=?",  # noqa: E501
                    (account,),
                ).fetchone()
                used_bytes, changes = historical["bytes"], historical["changes"]
            for item in records:
                current = db.execute(
                    "SELECT * FROM records WHERE account=? AND kind=? AND id=?",
                    (account, item["kind"], item["id"]),
                ).fetchone()
                if item["payload"] is not None and (sync := getattr(self, "project_sync", None)):
                    sync.check(db, account, item["payload"].get("scope", "*"))
                    if current and current["payload"] != "null":
                        sync.check(db, account, json.loads(current["payload"]).get("scope", "*"))
                version = current["version"] if current else 0
                if item["base_version"] != version:
                    if (
                        current
                        and item["base_version"] < version
                        and scoped_payload(json.loads(current["payload"]))
                        == scoped_payload(item["payload"])
                    ):
                        result.append(self.record(current))
                        continue
                    raise Conflict(
                        self.record(current)
                        if current
                        else {
                            "kind": item["kind"],
                            "id": item["id"],
                            "version": 0,
                            "payload": None,
                            "cursor": 0,
                        }
                    )
                payload = json.dumps(item["payload"], ensure_ascii=False)
                used_bytes += len(payload.encode()) * 2 + 512
                changes += 1
                removing = item["payload"] is None and current and current["payload"] != "null"
                if not removing and (
                    used_bytes > self.max_storage_bytes or changes > self.max_changes
                ):
                    raise QuotaExceeded(
                        "Cloud safety capacity reached; export data or contact support"
                    )
                cursor = db.execute(
                    "INSERT INTO changes(account,kind,id,version,payload) VALUES(?,?,?,?,?)",
                    (account, item["kind"], item["id"], version + 1, payload),
                ).lastrowid
                db.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)",
                    (account, item["kind"], item["id"], version + 1, payload, cursor),
                )
                result.append(
                    dict(
                        kind=item["kind"],
                        id=item["id"],
                        version=version + 1,
                        payload=item["payload"],
                        cursor=cursor,
                    )
                )
            db.execute(
                "INSERT OR REPLACE INTO account_usage VALUES(?,?,?)", (account, used_bytes, changes)
            )
            if sharing := getattr(self, "sharing", None):
                sharing.suppress_owner(db, account)
        return result

    def pull(self, account, cursor, limit):
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM changes WHERE account=? AND cursor>? ORDER BY cursor LIMIT ?",
                (account, cursor, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        records = [self.record(row) for row in rows[:limit]]
        return dict(
            records=records, cursor=records[-1]["cursor"] if records else cursor, has_more=more
        )

    def knowledge(self, account, project):
        from .project_identity import project_names

        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM records WHERE account=? AND payload!='null' ORDER BY cursor",
                (account,),
            ).fetchall()
            names = project_names(self, account, db=db)
        scope = names(project)
        records = [self.record(r) for r in rows]
        for record in records:
            record["payload"]["scope"] = names(record["payload"]["scope"])
        return [r for r in records if r["payload"]["scope"] in ("*", scope)]

    def projects(self, account):
        """Every project the owner has a preference for, to tell a rule about one
        project apart from a rule for this one."""
        from .project_identity import project_names

        with self.connection() as db:
            rows = db.execute(
                "SELECT DISTINCT payload FROM records WHERE account=? AND payload!='null'",
                (account,),
            ).fetchall()
            names = project_names(self, account, db=db)
        scopes = {names(scoped_payload(json.loads(r["payload"])).get("scope")) for r in rows}
        return sorted(scope for scope in scopes if isinstance(scope, str) and scope != "*")

    def queue_question(self, account, question, project):
        """Queue an unknown question; pending questions are bounded, never history.

        Answered questions leave the queue (close_question), so the pending budget
        frees up instead of accumulating against the account's sync quota.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM questions WHERE account=? AND question=? AND project=? AND answered=0",  # noqa: E501
                (account, question, project),
            ).fetchone()
            if existing:
                return existing["id"]
            pending = db.execute(
                "SELECT COUNT(*) AS total,COALESCE(SUM(length(CAST(question AS BLOB))"
                "+length(CAST(project AS BLOB))+128),0) AS bytes "
                "FROM questions WHERE account=? AND answered=0",
                (account,),
            ).fetchone()
            if not question_admitted(self, pending, question, project):
                raise QuotaExceeded("Pending owner questions reached cloud safety capacity")
            identifier = secrets.token_hex(16)
            db.execute(
                "INSERT INTO questions(account,id,question,project) VALUES(?,?,?,?)",
                (account, identifier, question, project),
            )
            return identifier

    def close_question(self, account, identifier):
        with self.connection() as db:
            db.execute("DELETE FROM questions WHERE account=? AND id=?", (account, identifier))


MAX_PENDING_QUESTIONS = 1000


def question_admitted(store, pending, question, project):
    size = len(question.encode()) + len(project.encode()) + 128
    return (
        pending["total"] < MAX_PENDING_QUESTIONS
        and pending["bytes"] + size <= store.max_storage_bytes
    )
