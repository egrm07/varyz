"""Synthetic disk-backed fixture for the constrained HTTP capacity experiment."""

import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet

from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import MODEL, READ_SCOPE, Archive, CachedEmbedder, Change, Chunk, Document
from varyz.cloud.sharing_service import identifier
from varyz.cloud.store import digest

DATA = Path(os.environ.get("CAPACITY_DATA", "/probe"))
DATA.mkdir(exist_ok=True)
key = Fernet.generate_key().decode()
embedder = CachedEmbedder("/opt/varyz/models")
app = create_app(
    Settings(
        database=str(DATA / "cloud.db"),
        public_url="http://127.0.0.1:8081",
        shared_varyz_enabled=True,
        archive_encryption_key=key,
        storage_limit=1048576,
        change_limit=10000,
        archive_limit=134217728,
        archive_embedding_cache="/opt/varyz/models",
        archive_embedder=embedder,
    )
)
store, following = app.state.store, app.state.sharing.following
population = int(os.environ.get("CAPACITY_FOLLOWERS", "10000"))
creators = 50
with store.connection() as db:
    db.executemany(
        "INSERT INTO accounts(id,email,password,status,email_verified,username) "
        "VALUES(?,?,?,'active',1,?)",
        [
            (name, name + "@example.test", "unused", name)
            for name in ["history", "syncer"] + [f"creator{i:03}" for i in range(creators)]
        ],
    )
for n in range(creators):
    owner = f"creator{n:03}"
    records = [
        dict(
            kind="rule",
            id=f"rule{i}",
            base_version=0,
            payload=dict(
                scope="*",
                statement=f"Code review standard {i}: "
                "Read the complete diff and explain the impact.",
            ),
        )
        for i in range(20)
    ]
    records.append(
        dict(
            kind="answer",
            id="answer",
            base_version=0,
            payload=dict(
                scope="*",
                question="Should pull requests include tests?",
                answer="Include tests for changed behavior.",
            ),
        )
    )
    store.push(owner, records)
    preview = following.preview(
        owner,
        "capacity",
        "code review",
        [dict(kind=r["kind"], id=r["id"], version=1) for r in records],
    )
    following.publish(owner, "capacity", preview["preview_id"], identifier())
tokens = [secrets.token_urlsafe(48) for _ in range(population)]
with store.connection() as db:
    db.executemany(
        "INSERT INTO accounts(id,email,password,status,email_verified,username) "
        "VALUES(?,?,?,'inactive',1,?)",
        [
            (f"follower{i:06}", f"follower{i:06}@example.test", "unused", f"follower{i:06}")
            for i in range(population)
        ],
    )
    db.executemany(
        "INSERT INTO follows VALUES(?,?,?)",
        (
            (f"follower{i:06}", f"creator{n:03}", float(creators - n))
            for i in range(population)
            for n in range(creators)
        ),
    )
    db.executemany(
        "INSERT INTO credentials VALUES(?,?,?,?,?,?,?)",
        [
            (
                digest(token),
                f"follower{i:06}",
                "access",
                time.time() + 7200,
                "capacity",
                "",
                str(time.time()),
            )
            for i, token in enumerate(tokens)
        ],
    )
archive = Archive(store, "http://127.0.0.1:8081", encryption_key=key)
for start in range(0, 1000, 25):
    changes = []
    texts = [
        f"SQLite capacity bucket{i % 100} decision {i}. "
        + ("We chose offline project memory with reviewed synthetic decisions. " * 8)
        for i in range(start, start + 25)
    ]
    vectors = embedder.embed_documents(texts)
    for i in range(start, start + 25):
        text = texts[i - start]
        payload = Document(
            id=f"note{i}",
            project="capacity",
            source="documents",
            external_id=f"notes/{i}.txt",
            title=f"Capacity decision {i}",
            text=text,
            occurred_at="2026-10-01T12:00:00+00:00",
            sensitivity="personal",
            model=MODEL,
            chunks=[Chunk(text=text, vector=vectors[i - start])],
        )
        changes.append(Change(id=f"note{i}", base_version=0, payload=payload))
    archive.push("history", changes)
store.push(
    "syncer",
    [
        dict(
            kind="rule",
            id="seed",
            base_version=0,
            payload=dict(scope="capacity", statement="Synthetic sync baseline."),
        )
    ],
)
auth = dict(
    followers=tokens,
    history=store.credential("history", "access", 7200, "capacity", scope=READ_SCOPE),
    syncer=store.credential("syncer", "access", 7200, "varyz-local"),
)
(DATA / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
with store.connection() as db:
    fixture = dict(
        followers=population,
        creators=creators,
        follows=db.execute("SELECT COUNT(*) FROM follows").fetchone()[0],
        archive_documents=1000,
        search_mode="hybrid",
        database_bytes=(
            db.execute("PRAGMA page_count").fetchone()[0]
            * db.execute("PRAGMA page_size").fetchone()[0]
        ),
    )
(DATA / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
