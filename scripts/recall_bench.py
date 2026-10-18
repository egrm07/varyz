# ruff: noqa: E501 -- generator templates are data, kept one per line
"""recall_bench: does the right evidence come back when an agent asks about the past?

Varyz's primary job is answering questions about the owner's own history --
why something was decided, what was promised, where work was left, when a
subscription renews -- from records scattered across mail, chat, calendar,
notes, documents and commits, with a source for every claim. This benchmark
measures exactly that, offline and reproducibly.

The archive is synthetic and public: authored evidence records
(`evals/recall_bench_v1/needles.jsonl`) hidden among several thousand generated
records from the same fictional life, written with the same people, projects,
vendors, ticket numbers and vocabulary so that nothing is findable by an
unusual word alone. It is built through the real ingest and embedding path
(`dao.upsert_entry` then `embed_pending` with the packaged model), and scored
read-only.

Cases (`evals/recall_bench_v1/cases.jsonl`) are natural questions worded the
way a person asks them, not the way the record was written. Five types:

  join       every required record must be in the returned evidence
  single     the one required record must be returned
  current    the newest record must be returned and outrank the stale one
  none       the archive cannot answer: nothing may be returned
  sensitive  the answer is a sensitive record: it must be withheld, and no
             other record may be handed over in its place

Metrics, per system:

  answered correctly   the case passes under the rules above (headline)
  complete evidence    answerable cases whose required records all came back
  record recall        share of required records returned
  MRR                  reciprocal rank of the first required record
  false evidence       unanswerable cases where anything was returned
  false refusal        answerable cases where the system returned nothing
  privacy leaks        sensitive records returned, and cases with a stand-in

Usage:
    uv run --no-sync python scripts/recall_bench.py build
    uv run --no-sync python scripts/recall_bench.py score --split dev
    uv run --no-sync python scripts/recall_bench.py score --split held_out
    uv run --no-sync python scripts/recall_bench.py score --split dev --judge gemma4:12b-it-q4_K_M

`--judge` also scores `recall` with its local judge, served by the Ollama
model named. It is the same model Varyz manages for itself, reached through
Ollama so the benchmark does not start a server of its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.config import load_config  # noqa: E402
from varyz.db import dao  # noqa: E402
from varyz.db.connection import open_db  # noqa: E402
from varyz.domain import Kind, NormalizedEntry, Sensitivity  # noqa: E402

BENCH = ROOT / "evals" / "recall_bench_v1"
DEFAULT_ARCHIVE = ROOT / "data" / "evals" / "recall_bench_v1"
LIMIT = 8  # what `recall` hands an agent by default
DISTRACTORS = 4600
SEED = 20260916

# Words the unanswerable and sensitive cases depend on being absent from every
# public record. The generator refuses to emit them, so a case cannot become
# answerable by accident when a template changes.
FORBIDDEN = (
    "clementine",
    "kitchen",
    "renovat",
    "physio",
    "android",
    "widget",
    "kayak",
    "berlin",
    "offsite",
    "bartholomew",
    "garden",
    "passport",
    "graphql",
    "tokyo",
    "piano",
    "dentist",
    "x-ray",
    "cholesterol",
    "prescri",
    "dose",
    "therapist",
    "doctor",
    "gp ",
    "ignatius",
    "retreat",
    "licence",
    "terraform",
    "reykjavik",
    "pottery",
    "optician",
    "glaucoma",
    "fertility",
    "clinic",
    "hormone",
    "cornelius",
    "hackathon",
    "chimney",
    "elasticsearch",
    "montreal",
    "bouldering",
    "dermatolog",
    "biopsy",
    "cardiolog",
    "fibrillation",
)

AUTHORED_SOURCES = {"git", "manual", "journal_import"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


# --- the rest of Sam's life ---------------------------------------------------

COLLEAGUES = ["Tomás Ortega", "Priya Raman", "Hana Kim", "Leo Park", "Nina Varga", "Omar Haddad"]
FAMILY = ["Mum", "Ravi", "Aisha Bello", "Dad", "Grace"]
PROJECTS = {
    "tidepool": (
        "TP",
        "Tidepool",
        [
            "sync engine",
            "conflict resolver",
            "offline cache",
            "change feed",
            "auth service",
            "iOS app",
            "share sheet",
            "push notifications",
            "onboarding flow",
            "search index",
        ],
    ),
    "beacon": (
        "BC",
        "Beacon",
        [
            "invoice renderer",
            "ledger",
            "payment retries",
            "tax rates",
            "dunning emails",
            "partner API",
            "usage metering",
            "refund flow",
            "quota service",
            "finance export",
        ],
    ),
    "quill": (
        "QL",
        "Quill",
        [
            "docs search",
            "versioned pages",
            "locale loader",
            "code samples",
            "sidebar nav",
            "changelog feed",
            "link checker",
            "shard cache",
            "PDF export",
            "theme tokens",
        ],
    ),
}
VERBS = [
    "fix",
    "refactor",
    "add tests for",
    "speed up",
    "document",
    "harden",
    "clean up",
    "instrument",
    "simplify",
    "log retries in",
    "add metrics to",
    "split",
    "rename config in",
    "remove dead code from",
    "bump dependencies for",
    "add a feature flag to",
]
DETAILS = [
    "an off-by-one in pagination",
    "a race on shutdown",
    "timezone handling",
    "a flaky integration test",
    "memory growth under load",
    "stale cache entries",
    "slow cold starts",
    "missing retries on 503",
    "duplicate log lines",
    "a null check on empty payloads",
    "wrong currency rounding",
    "ordering of events",
    "a leaked file handle",
    "an unbounded queue",
    "the release checklist",
]
SERVICES = [
    "tidepool-api",
    "tidepool-sync",
    "tidepool-auth",
    "beacon-invoice-worker",
    "beacon-ledger",
    "beacon-partner-gateway",
    "quill-search",
    "quill-web",
]
METRICS = [
    "error rate",
    "p95 latency",
    "queue depth",
    "CPU saturation",
    "5xx responses",
    "disk usage",
    "memory usage",
    "consumer lag",
]
CAUSES = [
    "a bad config push",
    "an expired TLS certificate",
    "a noisy neighbour on the database host",
    "a runaway cron job",
    "a dependency upgrade",
    "DNS resolution failures",
    "a full disk on the log volume",
    "connection pool exhaustion",
    "a schema migration lock",
    "a regional network blip",
]
FIXES = [
    "rolled back the deploy",
    "raised the pool size and added an alert",
    "renewed the certificate and automated renewal",
    "moved the job off peak hours",
    "pinned the dependency",
    "added a retry with backoff",
    "rotated the logs and set a quota",
    "split the migration into batches",
    "failed over to the replica",
]
VENDORS = [
    ("KeyVault", "security tips for your household logins", "Family plan"),
    ("Flexidesk", "new colours for the Pro range", "desk accessories"),
    ("PulseFit", "new class timetable", "membership"),
    ("StreamBox", "new releases this week", "subscription"),
    ("SoundArc", "firmware update for your headphones", "audio gear"),
    ("Velo Parts", "summer sale on groupsets", "bike parts"),
    ("Citylease", "weekend van rental offers", "van hire"),
    ("Lingua Partners", "translation industry newsletter", "translation"),
    ("Cloud Weekly", "this week in cloud pricing", "newsletter"),
    ("Hotel Arcadia", "member rates for autumn", "hotel stay"),
    ("Kerbside Autos", "winter tyre check offer", "car servicing"),
    ("Northgate Energy", "your monthly usage summary", "electricity"),
    ("Fibrelink", "your broadband bill is ready", "broadband"),
    ("Bookbarn", "recommended reads", "books"),
]
SUBSCRIPTIONS = [
    ("Fibrelink Broadband", "£32.00"),
    ("Northgate Energy", "£71.40"),
    ("Bookbarn Plus", "£7.99"),
    ("Cloud Notes Pro", "$4.99"),
    ("NewsDaily", "£9.00"),
    ("PhotoSafe 200GB", "£2.99"),
    ("VPNly", "$59.00"),
]
PLACES = [
    "The Old Mill",
    "the noodle place",
    "Café Lune",
    "the park",
    "Porto",
    "Edinburgh",
    "the climbing wall",
    "the farmers market",
    "the lake",
    "Brighton",
    "Seville",
    "Bruges",
]
FOODS = [
    "lentil soup",
    "a stir fry",
    "pasta with pesto",
    "a big salad",
    "tacos",
    "shakshuka",
    "curry",
    "roast vegetables",
    "ramen",
    "a sourdough sandwich",
]
MOODS = [
    "tired but ok",
    "good day",
    "restless",
    "calm",
    "frustrated with work",
    "happy after the run",
    "slow morning",
    "productive",
    "a bit low",
    "energised",
]
ACTIVITIES = [
    "ran 5k",
    "went climbing",
    "read for an hour",
    "cycled to work",
    "did yoga",
    "walked by the river",
    "played board games",
    "cooked for friends",
    "fixed the shelf",
    "watched a film",
]
AI_TOPICS = [
    (
        "Regex help",
        "How do I match ISO dates with an optional time part in Python? Suggested ^\\d{4}-\\d{2}-\\d{2}(T\\d{2}:\\d{2})?$.",
    ),
    (
        "Weekend trip",
        "Planning two days in {place}: markets in the morning, a long walk, dinner somewhere quiet.",
    ),
    (
        "SQL window functions",
        "Explained ROW_NUMBER over partitions to dedupe {project} events by id.",
    ),
    ("Recipe swap", "What can replace buttermilk in pancakes? Milk plus a spoon of lemon juice."),
    (
        "Budget sheet",
        "Built a monthly budget with categories for rent, groceries, travel and subscriptions.",
    ),
    (
        "Rust lifetimes",
        "Why does the borrow checker reject returning a reference to a local in the {component}?",
    ),
    ("Cover letter", "Tightened a paragraph about leading the {component} work at Larkspur."),
    (
        "Houseplants",
        "Why are the monstera leaves yellowing? Probably overwatering; let the soil dry out.",
    ),
    ("Kubernetes probes", "Difference between readiness and liveness probes for {service}."),
    (
        "Gift ideas",
        "Ideas for {person}'s birthday under £40: a book, a plant, a cooking class voucher.",
    ),
]


def _day(rng: random.Random) -> datetime:
    start = datetime(2024, 6, 1, tzinfo=UTC)
    return start + timedelta(minutes=rng.randrange(0, 60 * 24 * 820))


def _project(rng: random.Random) -> tuple[str, str, str]:
    prefix, name, components = PROJECTS[rng.choice(list(PROJECTS))]
    return prefix, name, rng.choice(components)


def _ticket(rng: random.Random, prefix: str) -> str:
    # Authored needles use specific numbers; generated ones stay clear of them
    # so an identifier in a question can only ever point at its own records.
    return f"{prefix}-{rng.randrange(600, 999)}"


def _work_chat(rng: random.Random) -> tuple[str, str, str, str]:
    who = rng.choice(COLLEAGUES)
    first = who.split()[0]
    prefix, name, comp = _project(rng)
    ticket = _ticket(rng, prefix)
    lines = [
        f"{first}: can you review my PR for the {name} {comp}? It's {ticket}.",
        f"{first}: {name} {comp} deploy is done, watching dashboards for an hour.",
        f"{first}: I think {ticket} is really about {rng.choice(DETAILS)}, not the {comp}.",
        f"{first}: standup notes — I'm on {ticket}, blocked on review. Sam: I'll look after lunch.",
        f"{first}: should we {rng.choice(VERBS)} the {comp} before the {name} release? Sam: yes, file a ticket.",
        f"{first}: customer asked about the {comp} again, can we send them the {name} changelog?",
        f"{first}: moving {ticket} to next sprint, the {comp} change needs a design pass first.",
        f"{first}: pairing on {rng.choice(DETAILS)} in the {comp} tomorrow at 10? Sam: works for me.",
    ]
    return "telegram", "message", who, rng.choice(lines)


def _commit(rng: random.Random) -> tuple[str, str, str, str]:
    prefix, name, comp = _project(rng)
    ticket = _ticket(rng, prefix)
    verb = rng.choice(VERBS)
    detail = rng.choice(DETAILS)
    title = f"{name.lower()}: {ticket} {verb} {comp}"
    body = f"{ticket}: {verb} the {comp}; addresses {detail}."
    if rng.random() < 0.4:
        body += f" Measured before/after in the {name} staging load test."
    return "git", "document", title, body


def _alert(rng: random.Random) -> tuple[str, str, str, str]:
    service = rng.choice(SERVICES)
    metric = rng.choice(METRICS)
    state = rng.choice(["FIRING", "RESOLVED"])
    value = rng.randrange(20, 99)
    title = f"[{state}] {service} {metric}"
    body = f"{service} {metric} at {value}% of threshold over 10m. Incident INC-{rng.randrange(1000, 9999)}."
    return "email", "email", title, body


def _postmortem(rng: random.Random) -> tuple[str, str, str, str]:
    service = rng.choice(SERVICES)
    inc = f"INC-{rng.randrange(1000, 9999)}"
    cause = rng.choice(CAUSES)
    fix = rng.choice(FIXES)
    hours = rng.randrange(1, 9)
    title = f"Postmortem {inc}"
    body = (
        f"{service} was degraded for {hours} hours because of {cause}. "
        f"Resolution: we {fix}. Follow-ups tracked in the incident doc."
    )
    return "filesystem", "document", title, body


def _experiment(rng: random.Random) -> tuple[str, str, str, str]:
    exp = f"EXP-{rng.randrange(30, 99)}"
    _, name, comp = _project(rng)
    metric = rng.choice(
        [
            "activation",
            "weekly retention",
            "click-through",
            "support contacts",
            "time to first sync",
            "invoice disputes",
        ]
    )
    before = round(rng.uniform(5, 40), 1)
    after = round(before + rng.uniform(-3, 5), 1)
    title = f"{exp} results"
    body = (
        f"Testing a new {comp} in {name}: {metric} moved from {before}% to {after}% "
        f"over {rng.randrange(2, 6)} weeks. {rng.choice(['Ship it.', 'Inconclusive.', 'Roll back.'])}"
    )
    return "filesystem", "document", title, body


def _adr(rng: random.Random) -> tuple[str, str, str, str]:
    _, name, comp = _project(rng)
    options = rng.sample(
        [
            "Redis",
            "SQLite",
            "Kafka",
            "a cron job",
            "S3",
            "a queue table",
            "server-sent events",
            "gRPC",
            "a CDN",
        ],
        2,
    )
    title = f"ADR-{rng.randrange(10, 60):03d} {name} {comp}"
    body = (
        f"Decision: use {options[0]} for the {comp}. Rejected: {options[1]}. "
        f"Drivers: operational simplicity and cost."
    )
    return "filesystem", "document", title, body


def _meeting(rng: random.Random) -> tuple[str, str, str, str]:
    who = rng.choice(COLLEAGUES)
    _, name, comp = _project(rng)
    title = rng.choice(
        [
            f"1:1 {who.split()[0]} / Sam",
            f"{name} planning",
            f"{name} standup",
            f"Design review: {comp}",
            "Weekly team sync",
            "Sprint retro",
            "Monthly business review prep",
            f"Interview loop: {name} backend",
        ]
    )
    body = rng.choice(
        [
            f"Room Juniper. Agenda: {comp}, open tickets.",
            f"Room Birch. Discuss the {comp} roadmap.",
            "Video call. Notes in the shared doc.",
            f"Bring numbers on the {comp}.",
        ]
    )
    return "google_calendar", "calendar_event", title, body


def _social_event(rng: random.Random) -> tuple[str, str, str, str]:
    person = rng.choice(FAMILY)
    place = rng.choice(PLACES)
    title = rng.choice(
        [
            f"Dinner with {person}",
            f"{person} visiting",
            f"Trip: {place}",
            f"Climbing with {person}",
            f"{person}'s party",
            "Haircut",
            "Car service",
            "Parents' evening call",
            f"Coffee at {place}",
        ]
    )
    return (
        "google_calendar",
        "calendar_event",
        title,
        f"At {place}. {rng.choice(['', 'Bring snacks.', 'Book a table.', 'Tickets in email.'])}".strip(),
    )


def _family_chat(rng: random.Random) -> tuple[str, str, str, str]:
    person = rng.choice(FAMILY)
    first = person.split()[0]
    place = rng.choice(PLACES)
    lines = [
        f"{first}: are you free on Saturday? Sam: should be, what's the plan?",
        f"{first}: we went to {place}, you'd have loved it. Sam: next time!",
        f"{first}: did you get my photos? Sam: yes, the one by the lake is great.",
        f"{first}: can you send me that recipe for {rng.choice(FOODS)}?",
        f"{first}: running late, save me a seat. Sam: no worries.",
        f"{first}: happy Friday! Sam: you too, any weekend plans?",
        f"{first}: I'll call you tonight about the holiday. Sam: after 8 is best.",
        f"{first}: can I borrow the drill? Sam: sure, I'll drop it round.",
        f"{first}: remember to bring a jacket, it's cold at {place}.",
    ]
    return "whatsapp", "message", person, rng.choice(lines)


def _receipt(rng: random.Random) -> tuple[str, str, str, str]:
    vendor, _, product = rng.choice(VENDORS)
    order = f"{vendor[:2].upper()}-{rng.randrange(10000, 99999)}"
    amount = f"£{rng.randrange(8, 400)}.{rng.randrange(0, 99):02d}"
    title = rng.choice(
        [f"Your {vendor} order {order}", f"{vendor} receipt", f"Dispatched: {order}"]
    )
    body = f"Thanks for your order of {product} ({order}). Total {amount}."
    if rng.random() < 0.3:
        body += f" Returns accepted within {rng.choice([14, 28, 60])} days with proof of purchase."
    return "email", "email", title, body


def _newsletter(rng: random.Random) -> tuple[str, str, str, str]:
    vendor, topic, _ = rng.choice(VENDORS)
    title = f"{vendor}: {topic}"
    body = rng.choice(
        [
            f"This month at {vendor}: {topic}. Plus member offers and news.",
            f"Hi Sam, here's what's new: {topic}. Unsubscribe any time.",
            f"{topic.capitalize()} — read more on our blog.",
        ]
    )
    return "email", "email", title, body


def _subscription(rng: random.Random, when: datetime) -> tuple[str, str, str, str]:
    name, price = rng.choice(SUBSCRIPTIONS)
    renew = (when + timedelta(days=rng.choice([30, 365]))).strftime("%d %B %Y").lstrip("0")
    title = rng.choice([f"{name} payment received", f"{name}: renewal reminder"])
    body = f"We charged {price} for {name}. Your plan renews on {renew}. Manage it in your account settings."
    return "email", "email", title, body


def _claim_or_ticket(rng: random.Random) -> tuple[str, str, str, str]:
    kind = rng.random()
    if kind < 0.4:
        ref = f"#{rng.randrange(8200, 9900)}"
        company = rng.choice(
            [
                "Maple Dental Supplies",
                "Orbit Travel",
                "Quayside Books",
                "Harbor Freight Co",
                "Finch Studios",
                "Larch & Co",
                "Northbank Legal",
            ]
        )
        problem = rng.choice(
            [
                "can't export invoices",
                "sees a blank dashboard",
                "asks for an API key reset",
                "wants to change plan",
                "reports slow uploads",
                "needs a VAT receipt",
            ]
        )
        return "email", "email", f"Support ticket {ref}", f"{company} {problem}. Priority normal."
    if kind < 0.7:
        ref = f"CL-{rng.randrange(40000, 99999)}"
        return (
            "email",
            "email",
            f"Claim {ref} update",
            rng.choice(
                [
                    f"Your home contents claim {ref} is under review.",
                    f"Claim {ref}: we need a photo of the damaged item.",
                    f"Claim {ref} closed. No payment due.",
                ]
            ),
        )
    ref = f"RMA-{rng.randrange(1000, 9999)}"
    return (
        "email",
        "email",
        f"Return {ref} received",
        f"We received your return {ref}. A refund will follow in 5-10 days.",
    )


def _journal(rng: random.Random) -> tuple[str, str, str, str]:
    body = (
        f"{rng.choice(MOODS).capitalize()}. {rng.choice(ACTIVITIES).capitalize()}, "
        f"had {rng.choice(FOODS)} for dinner."
    )
    if rng.random() < 0.3:
        body += rng.choice(
            [
                " Coffee with Tomás before work.",
                " Too much screen time.",
                " Thinking about the Tidepool roadmap.",
                " Called Mum.",
                " Slept badly again.",
                " Tea in the afternoon helped.",
            ]
        )
    return "journal_import", "journal", "Journal", body


def _note(rng: random.Random) -> tuple[str, str, str, str]:
    _, name, comp = _project(rng)
    title, body = rng.choice(
        [
            ("Shopping", f"Oat milk, {rng.choice(FOODS)} ingredients, batteries, bin bags."),
            ("Ideas", f"Blog post: what we learned building the {name} {comp}."),
            (
                "Todo",
                f"Renew car tax, book haircut, reply to {rng.choice(FAMILY)}, review {comp} PR.",
            ),
            (
                f"{name} notes",
                f"The {comp} needs {rng.choice(DETAILS)} looked at. Ask {rng.choice(COLLEAGUES).split()[0]}.",
            ),
            (
                "Reading list",
                "Designing Data-Intensive Applications, The Pragmatic Programmer, Four Thousand Weeks.",
            ),
            (
                "Home",
                f"Fix the shelf, call {rng.choice(['the landlord', 'Oliver', 'the letting agent'])} about the boiler service.",
            ),
            ("Weekend", f"Maybe {rng.choice(ACTIVITIES)} and dinner at {rng.choice(PLACES)}."),
        ]
    )
    return "manual", "note", title, body


def _ai(rng: random.Random) -> tuple[str, str, str, str]:
    title, body = rng.choice(AI_TOPICS)
    _, name, comp = _project(rng)
    for key, value in (
        ("{place}", rng.choice(PLACES)),
        ("{project}", name),
        ("{component}", comp),
        ("{service}", rng.choice(SERVICES)),
        ("{person}", rng.choice(FAMILY)),
    ):
        body = body.replace(key, value)
    return "ai_conversations", "transcript", title, body


GENERATORS: list[tuple[float, Callable[..., tuple[str, str, str, str]]]] = [
    (0.17, _work_chat),
    (0.13, _commit),
    (0.06, _alert),
    (0.03, _postmortem),
    (0.03, _experiment),
    (0.02, _adr),
    (0.08, _meeting),
    (0.05, _social_event),
    (0.10, _family_chat),
    (0.07, _receipt),
    (0.06, _newsletter),
    (0.03, _subscription),
    (0.04, _claim_or_ticket),
    (0.06, _journal),
    (0.04, _note),
    (0.03, _ai),
]


def generate_distractors(count: int = DISTRACTORS, seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    weights = [w for w, _ in GENERATORS]
    funcs = [f for _, f in GENERATORS]
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    while len(rows) < count:
        func = rng.choices(funcs, weights)[0]
        when = _day(rng)
        source, kind, title, body = func(rng, when) if func is _subscription else func(rng)
        if (title, body) in seen:
            continue
        lowered = f"{title} {body}".lower()
        if any(word in lowered for word in FORBIDDEN):
            raise AssertionError(f"generator produced a forbidden word: {lowered!r}")
        seen.add((title, body))
        rows.append(
            {
                "external_id": f"gen-{len(rows):05d}",
                "source": source,
                "kind": kind,
                "occurred": when.isoformat().replace("+00:00", "Z"),
                "title": title,
                "body": body,
            }
        )
    return rows


# --- building -----------------------------------------------------------------


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build(destination: Path, *, embed: bool = True) -> dict[str, Any]:
    from varyz.pipeline.embed import Embedder, embed_pending

    destination.mkdir(parents=True, exist_ok=True)
    for name in ("varyz.db", "varyz.db-wal", "varyz.db-shm"):
        (destination / name).unlink(missing_ok=True)
    config_path = destination / "config.toml"
    config_path.write_text('[storage]\ndata_dir = "."\n', encoding="utf-8")
    cfg = load_config(config_path)
    rows = load_jsonl(BENCH / "needles.jsonl") + generate_distractors()
    conn = open_db(cfg.db_path)
    try:
        with conn:
            for row in rows:
                dao.upsert_entry(
                    conn,
                    NormalizedEntry(
                        kind=Kind(row["kind"]),
                        external_id=row["external_id"],
                        title=row.get("title"),
                        body=row["body"],
                        occurred_start=_instant(row["occurred"]),
                        captured_at=_instant(row["occurred"]),
                        authored_by_user=row["source"] in AUTHORED_SOURCES,
                    ),
                    source=row["source"],
                    sensitivity=Sensitivity(row.get("sensitivity", "personal")),
                )
        started = time.perf_counter()
        # Model weights are shared with the owner's archive rather than
        # downloaded again; nothing else is read from it.
        import os

        os.environ.setdefault("VARYZ_EMBEDDING_CACHE", str(ROOT / "data" / "models"))
        chunks = embed_pending(conn, cfg, Embedder(cfg)) if embed else 0
        seconds = time.perf_counter() - started
    finally:
        conn.close()
    return {
        "entries": len(rows),
        "chunks_embedded": chunks,
        "embed_seconds": round(seconds, 1),
        "config": str(config_path),
    }


# --- systems ------------------------------------------------------------------


@dataclass
class Returned:
    refs: list[str]  # external ids, best first
    withheld: int = 0


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    import sqlite_vec

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _external_ids(conn: sqlite3.Connection, entry_ids: list[str]) -> list[str]:
    out = []
    for entry_id in entry_ids:
        row = conn.execute("SELECT external_id FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is not None:
            out.append(row["external_id"])
    return out


def _public_only(conn: sqlite3.Connection, entry_ids: list[str]) -> tuple[list[str], int]:
    """Baselines get the same sensitivity filter Varyz applies, so the comparison
    is about retrieval and abstention rather than about a missing privacy rule."""
    kept, withheld = [], 0
    for entry_id in entry_ids:
        row = conn.execute("SELECT sensitivity FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is not None and row["sensitivity"] in ("public", "personal"):
            kept.append(entry_id)
        else:
            withheld += 1
    return kept, withheld


def _judge(cfg: Any, ollama_model: str) -> Any:
    from varyz.retrieval.judge import load_judge

    judged = cfg.model_copy(deep=True)
    judged.local_model.backend = "ollama"
    judged.ollama.model = ollama_model
    judged.retrieval.judge = "on"
    judge = load_judge(judged)
    if judge is None or not judge.provider.available():
        raise SystemExit(f"the judge model {ollama_model} is not being served by Ollama")
    # A judge that is still loading is skipped, which is right for an agent and
    # wrong for a score: load it before the first case.
    judge.provider.complete("Reply with JSON.", "Say nothing.")
    return judge


def systems(
    conn: sqlite3.Connection,
    cfg: Any,
    embedder: Any,
    rerank_models: list[str],
    judge_model: str | None = None,
) -> dict[str, Callable[[str], Returned]]:
    from varyz.retrieval.answer import recall
    from varyz.retrieval.rerank import Reranker
    from varyz.retrieval.search import keyword_search, vector_search

    def keyword(question: str) -> Returned:
        ids, _ = keyword_search(conn, question, LIMIT + 12)
        kept, withheld = _public_only(conn, ids)
        return Returned(_external_ids(conn, kept[:LIMIT]), withheld)

    def vector(question: str) -> Returned:
        ids, _ = vector_search(conn, embedder.embed_query(question), LIMIT + 12)
        kept, withheld = _public_only(conn, ids)
        return Returned(_external_ids(conn, kept[:LIMIT]), withheld)

    def varyz(question: str) -> Returned:
        evidence = recall(conn, cfg, question, embedder=embedder, limit=LIMIT)
        return Returned([c.external_id for c in evidence.capsules], evidence.withheld_sensitive)

    found: dict[str, Callable[[str], Returned]] = {
        "keyword (BM25)": keyword,
        "vector": vector,
        "varyz 0.x recall": varyz,
    }
    judge = _judge(cfg, judge_model) if judge_model else None
    for model in rerank_models:
        reranker = Reranker(cfg, model)

        def checked(question: str, reranker: Reranker = reranker, judge: Any = None) -> Returned:
            evidence = recall(
                conn, cfg, question, embedder=embedder, reranker=reranker, judge=judge, limit=LIMIT
            )
            return Returned([c.external_id for c in evidence.capsules], evidence.withheld_sensitive)

        found[f"varyz + {model.split('/')[-1]}"] = checked
        if judge is not None:
            found[f"varyz + {model.split('/')[-1]} + judge"] = lambda question, reranker=reranker: (
                checked(question, reranker, judge)
            )
    return found


# --- scoring ------------------------------------------------------------------


def score_case(case: dict[str, Any], got: Returned) -> dict[str, Any]:
    refs = got.refs
    kind = case["type"]
    result: dict[str, Any] = {"id": case["id"], "type": kind, "returned": refs[:LIMIT]}
    wanted = set(case.get("required", [])) | set(case.get("stale", []))
    result["unrelated"] = sum(1 for r in refs[:LIMIT] if r not in wanted)
    if kind == "none":
        result["passed"] = not refs
        result["false_evidence"] = bool(refs)
        return result
    if kind == "sensitive":
        leaked = [r for r in refs if r in case["withheld"]]
        result["leaked"] = leaked
        result["stand_in"] = bool(refs) and not leaked
        # Withheld and refused are both safe; what fails is handing anything over.
        result["passed"] = not refs
        result["withheld_flagged"] = got.withheld > 0
        return result
    required = case["required"]
    found = [r for r in required if r in refs]
    ranks = [refs.index(r) + 1 for r in required if r in refs]
    result["record_recall"] = len(found) / len(required)
    result["complete"] = len(found) == len(required)
    result["mrr"] = 1 / min(ranks) if ranks else 0.0
    result["refused"] = not refs
    passed = result["complete"]
    if kind == "current":
        stale_ranks = [refs.index(r) + 1 for r in case.get("stale", []) if r in refs]
        current_first = bool(ranks) and all(min(ranks) < s for s in stale_ranks)
        result["current_first"] = current_first
        passed = passed and current_first
    result["passed"] = passed
    return result


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarise(results: list[dict[str, Any]], latencies: list[float]) -> dict[str, Any]:
    answerable = [r for r in results if r["type"] in ("join", "single", "current")]
    none = [r for r in results if r["type"] == "none"]
    sensitive = [r for r in results if r["type"] == "sensitive"]
    passed = sum(r["passed"] for r in results)
    lo, hi = _wilson(passed, len(results))
    by_type: dict[str, list[int]] = {}
    for r in results:
        bucket = by_type.setdefault(r["type"], [0, 0])
        bucket[0] += r["passed"]
        bucket[1] += 1
    return {
        "answered_correctly": [passed, len(results)],
        "answered_correctly_rate": round(passed / len(results), 3) if results else 0.0,
        "wilson95": [round(lo, 3), round(hi, 3)],
        "complete_evidence": [sum(r["complete"] for r in answerable), len(answerable)],
        "record_recall": round(statistics.mean(r["record_recall"] for r in answerable), 3)
        if answerable
        else 0.0,
        "mrr": round(statistics.mean(r["mrr"] for r in answerable), 3) if answerable else 0.0,
        "false_evidence": [sum(r["false_evidence"] for r in none), len(none)],
        "false_refusal": [sum(r["refused"] for r in answerable), len(answerable)],
        "sensitive_leaks": sum(len(r["leaked"]) for r in sensitive),
        "sensitive_stand_ins": [sum(r["stand_in"] for r in sensitive), len(sensitive)],
        "by_type": {k: v for k, v in sorted(by_type.items())},
        "returned_per_case": round(statistics.mean(len(r["returned"]) for r in results), 2),
        "unrelated_per_answerable": round(statistics.mean(r["unrelated"] for r in answerable), 2)
        if answerable
        else 0.0,
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
            if latencies
            else 0.0,
        },
    }


def score(
    archive: Path,
    split: str,
    only: str | None = None,
    rerank_models: list[str] | None = None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    import os

    from varyz.pipeline.embed import Embedder

    os.environ.setdefault("VARYZ_EMBEDDING_CACHE", str(ROOT / "data" / "models"))
    cases_path = BENCH / "cases.jsonl"
    cases = [c for c in load_jsonl(cases_path) if c["split"] == split]
    cfg = load_config(archive / "config.toml")
    embedder = Embedder(cfg)
    embedder.embed_query("warm up")
    conn = _open_readonly(cfg.db_path)
    report: dict[str, Any] = {
        "benchmark": "recall_bench_v1",
        "split": split,
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest()[:16],
        "needles_sha256": hashlib.sha256((BENCH / "needles.jsonl").read_bytes()).hexdigest()[:16],
        "archive_entries": conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
        "archive_vectors": conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0],
        "limit": LIMIT,
        "judge_model": judge_model,
        "systems": {},
    }
    try:
        found = systems(conn, cfg, embedder, rerank_models or [], judge_model)
        for name, system in found.items():
            if only and only != name:
                continue
            results, latencies = [], []
            for case in cases:
                started = time.perf_counter()
                got = system(case["question"])
                latencies.append((time.perf_counter() - started) * 1000)
                results.append(score_case(case, got))
            report["systems"][name] = {"summary": summarise(results, latencies), "cases": results}
    finally:
        conn.close()
    return report


def _frac(pair: list[int]) -> str:
    return f"{pair[0]}/{pair[1]}"


def render(report: dict[str, Any]) -> str:
    lines = [f"recall_bench_v1 · {report['split']} · {report['archive_entries']} records"]
    header = f"{'system':<34}{'correct':>10}{'complete':>10}{'recall':>8}{'MRR':>7}{'false ev':>10}{'refused':>9}{'leaks':>7}{'stand-in':>10}{'p50 ms':>8}{'noise':>7}"
    lines.append(header)
    for name, data in report["systems"].items():
        s = data["summary"]
        lines.append(
            f"{name:<34}{_frac(s['answered_correctly']):>10}"
            f"{_frac(s['complete_evidence']):>10}{s['record_recall']:>8.2f}{s['mrr']:>7.2f}"
            f"{_frac(s['false_evidence']):>10}{_frac(s['false_refusal']):>9}"
            f"{s['sensitive_leaks']:>7}{_frac(s['sensitive_stand_ins']):>10}{s['latency_ms']['p50']:>8.1f}{s['unrelated_per_answerable']:>7.2f}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="generate and embed the synthetic archive")
    b.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    s = sub.add_parser("score", help="score every system on one split")
    s.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    s.add_argument("--split", choices=("dev", "held_out", "sealed", "sealed2"), required=True)
    s.add_argument("--system", default=None)
    s.add_argument(
        "--rerank-model",
        action="append",
        default=None,
        help="score varyz with this reranker too (repeatable); default BAAI/bge-reranker-base",
    )
    s.add_argument(
        "--judge",
        metavar="OLLAMA_MODEL",
        default=None,
        help="also score varyz with its local judge, served by this Ollama model",
    )
    s.add_argument("--out", type=Path, default=None)
    s.add_argument("--verbose", action="store_true", help="print failing cases")
    args = parser.parse_args()
    if args.command == "build":
        print(json.dumps(build(args.archive), indent=2))
        return 0
    models = args.rerank_model if args.rerank_model is not None else ["BAAI/bge-reranker-base"]
    report = score(args.archive, args.split, args.system, models, args.judge)
    print(render(report))
    if args.verbose:
        for name, data in report["systems"].items():
            for case in data["cases"]:
                if not case["passed"]:
                    print(f"  {name} FAIL {case['id']} {case['type']}: {case['returned']}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
