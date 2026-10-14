"""Additive portable sharing schema; all writes use the Cloud critical transaction lock."""

SCHEMA_VERSION = 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_schema(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS public_varyz(
 owner TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 status TEXT NOT NULL, revision INTEGER NOT NULL,
 created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS public_publications(
 owner TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, PRIMARY KEY(owner,revision));
CREATE TABLE IF NOT EXISTS public_selections(
 owner TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 item TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
 source_version INTEGER NOT NULL, topic TEXT NOT NULL, suppressed DOUBLE PRECISION,
 PRIMARY KEY(owner,item));
CREATE TABLE IF NOT EXISTS follows(
 follower TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 creator TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 created DOUBLE PRECISION NOT NULL, PRIMARY KEY(follower,creator));
CREATE TABLE IF NOT EXISTS follow_picks(
 follower TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, topic TEXT NOT NULL,
 creator TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 updated DOUBLE PRECISION NOT NULL, PRIMARY KEY(follower,topic));
CREATE TABLE IF NOT EXISTS public_uses(
 creator TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, day INTEGER NOT NULL,
 kind TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(creator,day,kind));
CREATE INDEX IF NOT EXISTS follows_creator ON follows(creator);
CREATE INDEX IF NOT EXISTS public_selections_topic ON public_selections(owner,topic);
CREATE TABLE IF NOT EXISTS shared_projects(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 project TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL,
 revision INTEGER NOT NULL, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_context(
 share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 state TEXT NOT NULL, payload TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(share,state));
CREATE TABLE IF NOT EXISTS shared_publications(
 share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, PRIMARY KEY(share,revision));
CREATE TABLE IF NOT EXISTS shared_previews(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 session TEXT NOT NULL, share TEXT, payload TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_selections(
 share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 item TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT, source_version INTEGER NOT NULL,
 source_scope TEXT, origin TEXT NOT NULL, suppressed DOUBLE PRECISION,
 PRIMARY KEY(share,item));
CREATE TABLE IF NOT EXISTS shared_manual_answers(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, payload TEXT NOT NULL, retracted DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS shared_invites(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 email TEXT NOT NULL, email_hash TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
 state TEXT NOT NULL, accepted_account TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 expires DOUBLE PRECISION NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_memberships(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 guest TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 state TEXT NOT NULL, revision INTEGER NOT NULL, created DOUBLE PRECISION NOT NULL,
 ended DOUBLE PRECISION, UNIQUE(share,guest));
CREATE TABLE IF NOT EXISTS shared_invitation_mail(
 invite TEXT PRIMARY KEY REFERENCES shared_invites(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, sent DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS shared_threads(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 membership TEXT NOT NULL REFERENCES shared_memberships(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
 version INTEGER NOT NULL, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_replies(
 id TEXT PRIMARY KEY, thread TEXT NOT NULL REFERENCES shared_threads(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, remembered TEXT, published INTEGER, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_task_bindings(
 guest TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, connection TEXT NOT NULL,
 task TEXT NOT NULL, membership TEXT NOT NULL REFERENCES shared_memberships(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, publication INTEGER NOT NULL, updated DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(guest,connection,task));
CREATE TABLE IF NOT EXISTS shared_connection_checks(
 guest TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 connection TEXT NOT NULL,
 membership TEXT NOT NULL REFERENCES shared_memberships(id) ON DELETE CASCADE,
 publication INTEGER NOT NULL, checked_at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(guest,connection,membership));
CREATE TABLE IF NOT EXISTS shared_idempotency(
 actor TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, operation TEXT NOT NULL,
 key TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL,
 resource TEXT REFERENCES shared_projects(id) ON DELETE CASCADE,
 PRIMARY KEY(actor,operation,key));
CREATE TABLE IF NOT EXISTS shared_events(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 actor TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_reports(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 author TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, side TEXT NOT NULL,
 payload TEXT NOT NULL, hash TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_report_order(
 report TEXT PRIMARY KEY REFERENCES shared_reports(id) ON DELETE CASCADE,
 share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shared_report_reads(
 reader TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 seen DOUBLE PRECISION NOT NULL, PRIMARY KEY(reader,share));
CREATE TABLE IF NOT EXISTS shared_access(
 membership TEXT PRIMARY KEY REFERENCES shared_memberships(id) ON DELETE CASCADE,
 state TEXT NOT NULL, payload TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_suggestions(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 membership TEXT REFERENCES shared_memberships(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_owner_questions(
 id TEXT PRIMARY KEY, share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS shared_notices(
 id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 share TEXT NOT NULL REFERENCES shared_projects(id) ON DELETE CASCADE,
 actor TEXT REFERENCES accounts(id) ON DELETE CASCADE, kind TEXT NOT NULL, detail TEXT,
 created DOUBLE PRECISION NOT NULL, emailed DOUBLE PRECISION, shown DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS shared_notice_settings(
 account TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 email_off INTEGER NOT NULL, last_email DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS shared_notices_account ON shared_notices(account,created);
CREATE INDEX IF NOT EXISTS shared_notices_mail ON shared_notices(emailed,created);
CREATE INDEX IF NOT EXISTS shared_reports_share ON shared_reports(share,created);
CREATE INDEX IF NOT EXISTS shared_suggestions_share ON shared_suggestions(share,state);
CREATE INDEX IF NOT EXISTS shared_owner_questions_share ON shared_owner_questions(share,status);
CREATE INDEX IF NOT EXISTS shared_memberships_guest ON shared_memberships(guest,state);
CREATE INDEX IF NOT EXISTS shared_projects_owner ON shared_projects(owner,status);
CREATE INDEX IF NOT EXISTS shared_threads_share ON shared_threads(share,status,updated,id);
CREATE INDEX IF NOT EXISTS shared_threads_member ON shared_threads(membership,status,fingerprint);
CREATE INDEX IF NOT EXISTS shared_invites_token ON shared_invites(token_hash,state);
"""

# Added after the first release. NULL level/fill_gaps mean Consult without gap filling.
COLUMNS = (
    ("shared_idempotency", "resource", "TEXT REFERENCES shared_projects(id) ON DELETE CASCADE"),
    ("shared_projects", "repo", "TEXT"),
    ("shared_memberships", "level", "TEXT"),
    ("shared_memberships", "fill_gaps", "INTEGER"),
    ("shared_invites", "level", "TEXT"),
    ("shared_invites", "fill_gaps", "INTEGER"),
)


def add_column(store, db, table, column, kind):
    if hasattr(store, "pool"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
    elif column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def migrate(store):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not hasattr(store, "pool"):
            db.execute("PRAGMA foreign_keys=ON")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        for table, column, kind in COLUMNS:
            add_column(store, db, table, column, kind)
        db.execute(
            "INSERT INTO shared_schema VALUES(?) ON CONFLICT(version) DO NOTHING", (SCHEMA_VERSION,)
        )
