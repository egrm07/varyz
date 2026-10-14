"""Optional shared cloud persistence; no local archive tables or inference.

The small connection adapter supports the fixed statements in cloud/app.py.
Sync writes have their own transactional cursor clock, not a PostgreSQL sequence:
sequence allocation is not commit ordered and could make a pull skip a write.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager, nullcontext

from .store import Conflict, QuotaExceeded, Store, digest, question_admitted, scoped_payload

# Serializes schema setup and the app's existing BEGIN IMMEDIATE critical
# sections (single-use credentials and webhook replay). It is transaction scoped.
_CRITICAL_LOCK = 792467503271

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, email TEXT UNIQUE, password TEXT NOT NULL,
 subscription TEXT, status TEXT DEFAULT 'inactive', billing_at TEXT DEFAULT '',
 email_verified INTEGER DEFAULT 0);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email_verified INTEGER DEFAULT 0;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS username TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS accounts_username_unique ON accounts(lower(username));
CREATE TABLE IF NOT EXISTS pending_signups(email TEXT PRIMARY KEY,
 username TEXT NOT NULL,password TEXT NOT NULL,hash TEXT UNIQUE,expires DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS pending_signups_expiry ON pending_signups(expires);
CREATE TABLE IF NOT EXISTS credentials(
 hash TEXT PRIMARY KEY, account TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 kind TEXT, expires DOUBLE PRECISION, client TEXT, redirect TEXT, challenge TEXT);
CREATE TABLE IF NOT EXISTS credential_grants(hash TEXT PRIMARY KEY,scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, redirects TEXT);
CREATE TABLE IF NOT EXISTS oauth_flows(
 hash TEXT PRIMARY KEY, params TEXT, expires DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS social_identities(
 provider TEXT, subject TEXT, account TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 PRIMARY KEY(provider,subject), UNIQUE(account,provider));
CREATE TABLE IF NOT EXISTS records(
 account TEXT REFERENCES accounts(id) ON DELETE CASCADE, kind TEXT, id TEXT,
 version BIGINT, payload TEXT, cursor BIGINT, PRIMARY KEY(account,kind,id));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS questions(
 account TEXT REFERENCES accounts(id) ON DELETE CASCADE, id TEXT,
 question TEXT, project TEXT, answered INTEGER DEFAULT 0, PRIMARY KEY(account,id));
CREATE TABLE IF NOT EXISTS away_state(
 account TEXT REFERENCES accounts(id) ON DELETE CASCADE, scope TEXT,
 enabled INTEGER, PRIMARY KEY(account,scope));
CREATE TABLE IF NOT EXISTS varyz_task_routes(
 namespace TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 project TEXT NOT NULL, conversation_id TEXT NOT NULL, backend TEXT NOT NULL,
 revision INTEGER NOT NULL, PRIMARY KEY(namespace,project,conversation_id));
CREATE TABLE IF NOT EXISTS changes(
 cursor BIGINT PRIMARY KEY, account TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 kind TEXT, id TEXT, version BIGINT, payload TEXT);
CREATE TABLE IF NOT EXISTS sync_clock(id INTEGER PRIMARY KEY CHECK(id=1), value BIGINT);
INSERT INTO sync_clock(id,value) VALUES(1,0) ON CONFLICT(id) DO NOTHING;
CREATE INDEX IF NOT EXISTS changes_account_cursor ON changes(account,cursor);
CREATE INDEX IF NOT EXISTS records_account_cursor ON records(account,cursor);
CREATE INDEX IF NOT EXISTS records_dashboard_scope ON records(
 account,(COALESCE(payload::jsonb->>'scope','*')),cursor)
 WHERE kind IN ('rule','answer','workflow') AND payload!='null';
CREATE INDEX IF NOT EXISTS credentials_account ON credentials(account);
CREATE INDEX IF NOT EXISTS credentials_expiry ON credentials(expires);
CREATE TABLE IF NOT EXISTS account_usage(
 account TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 bytes BIGINT DEFAULT 0, changes BIGINT DEFAULT 0);
CREATE TABLE IF NOT EXISTS billing_checkouts(
 account TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 price TEXT, url TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS billing_payments(
 id TEXT PRIMARY KEY, account TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 subscription TEXT, period_start DOUBLE PRECISION, completed_at DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS billing_payments_account
 ON billing_payments(account,subscription,period_start);
CREATE TABLE IF NOT EXISTS billing_refunds(
 id TEXT PRIMARY KEY, transaction_id TEXT, subscription TEXT);
CREATE INDEX IF NOT EXISTS billing_refunds_transaction ON billing_refunds(transaction_id);
ALTER TABLE billing_payments ADD COLUMN IF NOT EXISTS grand_total BIGINT;
ALTER TABLE billing_payments ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE billing_refunds ADD COLUMN IF NOT EXISTS refund_type TEXT DEFAULT 'full';
ALTER TABLE billing_refunds ADD COLUMN IF NOT EXISTS total BIGINT;
ALTER TABLE billing_refunds ADD COLUMN IF NOT EXISTS currency TEXT;
CREATE TABLE IF NOT EXISTS email_limits(key TEXT PRIMARY KEY, day BIGINT, count INTEGER);
CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY, window_start BIGINT, count INTEGER);
CREATE INDEX IF NOT EXISTS rate_limits_window ON rate_limits(window_start);
"""


def parameter_sql(statement: str) -> str:
    """Convert app-controlled positional SQL, preserving quoted question marks.

    Values are always bound separately; this function never interpolates them.
    The app does not issue dollar-quoted strings or comment-containing SQL.
    """
    result = []
    quote = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if char == "%":
            result.append("%%")
        elif quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    result.append(quote)
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            result.append(char)
        else:
            result.append("%s" if char == "?" else char)
        index += 1
    return "".join(result)


class _Connection:
    def __init__(self, connection, integrity_error):
        self.raw = connection
        self.integrity_error = integrity_error

    def execute(self, statement, parameters=()):
        normalized = " ".join(statement.strip().split()).upper()
        if normalized == "BEGIN IMMEDIATE":
            return self.raw.execute("SELECT pg_advisory_xact_lock(%s)", (_CRITICAL_LOCK,))
        # This is the one SQLite ignore statement the app uses. Do not implement
        # a generic text replacement for arbitrary SQL dialects.
        if normalized.startswith("INSERT OR IGNORE INTO CLIENTS "):
            statement = (
                re.sub(
                    r"INSERT\s+OR\s+IGNORE\s+INTO",
                    "INSERT INTO",
                    statement,
                    count=1,
                    flags=re.IGNORECASE,
                )
                + " ON CONFLICT(id) DO NOTHING"
            )
        try:
            if parameters:
                return self.raw.execute(parameter_sql(statement), parameters)
            return self.raw.execute(statement)
        except self.integrity_error as error:
            # Existing app signup maps sqlite3.IntegrityError to its safe 409.
            # The outer driver transaction rolls back on this exception.
            raise sqlite3.IntegrityError("Cloud database integrity constraint") from error


class PostgresStore(Store):
    """Store API backed by PostgreSQL, with tenant-scoped CAS and commit cursors.

    Bounded per-process pool; aggregate capacity is replicas times pool_max.
    Optional schema is used by isolated integration tests, never from MCP input.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "public",
        pool_max: int | None = None,
        max_storage_bytes: int = 50 * 1024 * 1024,
        max_changes: int = 100000,
    ):
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL cloud storage needs the cloud extra (psycopg)"
            ) from error
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("Invalid internal database schema")
        pool_max = (
            pool_max if pool_max is not None else int(os.getenv("VARYZ_CLOUD_DB_POOL_MAX", "8"))
        )
        if not 1 <= pool_max <= 64:
            raise ValueError("VARYZ_CLOUD_DB_POOL_MAX must be between 1 and 64")
        self.path = dsn
        if (
            type(max_storage_bytes) is not int
            or max_storage_bytes < 1
            or type(max_changes) is not int
            or max_changes < 1
        ):
            raise ValueError("Cloud storage/change limits must be positive integers")
        self.max_storage_bytes = max_storage_bytes
        self.max_changes = max_changes
        self._driver = psycopg
        self._dict_row = dict_row
        self._schema = schema
        self.pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=pool_max,
            timeout=10,
            max_waiting=32,
            max_idle=60,
            max_lifetime=300,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 10,
                "options": (
                    f"-c search_path={self._schema} "
                    "-c statement_timeout=15000 -c lock_timeout=10000 "
                    "-c idle_in_transaction_session_timeout=15000"
                ),
            },
        )
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.pool.close()

    @contextmanager
    def connection(self):
        with (
            self.pool.connection() as raw,
            raw.transaction(),
        ):
            yield _Connection(raw, self._driver.IntegrityError)

    def allow_request(self, key: str, window: int, limit: int) -> bool:
        """Shared fixed-window admission with server time and hashed keys.

        Counter saturation prevents overflow from repeated rejected traffic.
        Old windows cannot overwrite newer ones if requests wait on a row lock.
        Cleanup deletes at most 100 expired keys per operation.
        """
        if not isinstance(key, str) or len(key) > 1024:
            raise ValueError("Invalid internal rate key")
        if type(window) is not int or not 1 <= window <= 3600:
            raise ValueError("Invalid internal rate window")
        if type(limit) is not int or not 1 <= limit <= 1_000_000:
            raise ValueError("Invalid internal rate limit")
        with self.connection() as db:
            now = db.execute(
                "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now"
            ).fetchone()["now"]
            bucket = now // window * window
            row = db.execute(
                "INSERT INTO rate_limits(key,window_start,count) VALUES(?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET "
                "window_start=GREATEST(rate_limits.window_start,excluded.window_start), "
                "count=CASE WHEN rate_limits.window_start>=excluded.window_start "
                "THEN LEAST(rate_limits.count+1,?) ELSE 1 END RETURNING count",
                (digest(key), bucket, limit + 1),
            ).fetchone()
            db.execute(
                "DELETE FROM rate_limits WHERE key IN "
                "(SELECT key FROM rate_limits WHERE window_start<? "
                "ORDER BY window_start LIMIT 100) AND window_start<?",
                (now - 7200, now - 7200),
            )
            # Recheck expiration outside the candidate subquery so a concurrent
            # reset/rotation cannot have its refreshed credential deleted.
            db.execute(
                "DELETE FROM credentials WHERE hash IN "
                "(SELECT hash FROM credentials WHERE expires<? ORDER BY expires LIMIT 100) "
                "AND expires<?",
                (now, now),
            )
            db.execute(
                "DELETE FROM credential_grants WHERE hash IN "
                "(SELECT g.hash FROM credential_grants g LEFT JOIN credentials c "
                "ON c.hash=g.hash WHERE c.hash IS NULL LIMIT 100)"
            )
            return row["count"] <= limit

    def push(self, account, records, *, transaction=None):
        result = []
        with (nullcontext(transaction) if transaction is not None else self.connection()) as db:
            if transaction is None and (
                getattr(self, "sharing", None) or getattr(self, "project_sync", None)
            ):
                db.execute("BEGIN IMMEDIATE")
            # This row lock covers allocation AND commit. A sequence, even with
            # SERIALIZABLE reads, does not prevent cursor N+1 committing first.
            clock = db.execute("SELECT value FROM sync_clock WHERE id=1 FOR UPDATE").fetchone()
            cursor = clock["value"]
            # Prevent account deletion from racing a successfully authenticated
            # request and creating orphaned preference records.
            if not db.execute(
                "SELECT id FROM accounts WHERE id=? FOR SHARE", (account,)
            ).fetchone():
                raise sqlite3.IntegrityError("Cloud account no longer exists")
            usage = db.execute(
                "SELECT bytes,changes FROM account_usage WHERE account=? FOR UPDATE", (account,)
            ).fetchone()
            if usage is None:
                usage = db.execute(
                    "SELECT COALESCE(SUM(octet_length(payload)*2+512),0) AS bytes, "
                    "COUNT(*) AS changes FROM changes WHERE account=?",
                    (account,),
                ).fetchone()
                db.execute(
                    "INSERT INTO account_usage(account,bytes,changes) VALUES(?,?,?)",
                    (account, usage["bytes"], usage["changes"]),
                )
            storage_bytes, changes = usage["bytes"], usage["changes"]
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
                cursor += 1
                payload = json.dumps(item["payload"], ensure_ascii=False)
                storage_bytes += len(payload.encode()) * 2 + 512
                changes += 1
                removing = item["payload"] is None and current and current["payload"] != "null"
                if not removing and (
                    storage_bytes > self.max_storage_bytes or changes > self.max_changes
                ):
                    raise QuotaExceeded("Confirmed preference storage limit reached")
                db.execute(
                    "INSERT INTO changes(cursor,account,kind,id,version,payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (cursor, account, item["kind"], item["id"], version + 1, payload),
                )
                db.execute(
                    "INSERT INTO records(account,kind,id,version,payload,cursor) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(account,kind,id) DO UPDATE SET "
                    "version=excluded.version,payload=excluded.payload,cursor=excluded.cursor",
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
            db.execute("UPDATE sync_clock SET value=? WHERE id=1", (cursor,))
            db.execute(
                "UPDATE account_usage SET bytes=?,changes=? WHERE account=?",
                (storage_bytes, changes, account),
            )
            if sharing := getattr(self, "sharing", None):
                sharing.suppress_owner(db, account)
        return result

    def allow_email(self, key: str, limit: int) -> bool:
        """Reserve a shared daily delivery slot before attempting SMTP."""
        if not isinstance(key, str) or len(key) > 1024 or type(limit) is not int or limit < 1:
            raise ValueError("Invalid email admission key or limit")
        with self.connection() as db:
            day = db.execute(
                "SELECT floor(extract(epoch FROM clock_timestamp())/86400)::bigint AS day"
            ).fetchone()["day"]
            row = db.execute(
                "INSERT INTO email_limits(key,day,count) VALUES(?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET "
                "day=GREATEST(email_limits.day,excluded.day), "
                "count=CASE WHEN email_limits.day>=excluded.day "
                "THEN LEAST(email_limits.count+1,?) ELSE 1 END RETURNING count",
                (digest(key), day, limit + 1),
            ).fetchone()
            db.execute(
                "DELETE FROM email_limits WHERE key IN "
                "(SELECT key FROM email_limits WHERE day<? ORDER BY day LIMIT 100) AND day<?",
                (day - 1, day - 1),
            )
            return row["count"] <= limit

    def queue_question(self, account: str, question: str, project: str) -> str:
        """Queue unknown questions under a pending budget separate from sync history."""
        with self.connection() as db:
            # Lock the account row so concurrent asks cannot both pass the budget
            # check, and deletion cannot race the insert.
            if not db.execute(
                "SELECT id FROM accounts WHERE id=? FOR UPDATE", (account,)
            ).fetchone():
                raise sqlite3.IntegrityError("Cloud account no longer exists")
            existing = db.execute(
                "SELECT id FROM questions WHERE account=? AND question=? "
                "AND project=? AND answered=0",
                (account, question, project),
            ).fetchone()
            if existing:
                return existing["id"]
            pending = db.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(octet_length(question)"
                "+octet_length(project)+128),0) AS bytes "
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
