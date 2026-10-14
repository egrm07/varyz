"""Public Varyz: standards a creator chose to publish, followed by username.

A creator publishes reviewed General preferences under topics ("code review").
Anyone with a Varyz account follows them by username, and their agent gets the
standards for tasks about those topics. Deciding which Varyz to use is one rule:

1. The follower's own Varyz always wins; followed standards only fill gaps.
2. One creator per topic: the follower's pick ("use @egr for code review"), else
   the creator they followed most recently that covers the topic.
3. Naming a creator in the task ("@egr" or "egr's varyz") uses them for it.

Routing is plain text matching on the task, so it costs no model call.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from typing import Any

from varyz.resolution.domains import detect_domains, domain_title

from .billing import effective_account
from .payloads import canonical_payload
from .sharing_service import SharingError, clean, encoded, fail, identifier, normalized
from .store import digest

MAX_FOLLOWS = 50
BUDGET = 6500  # Characters of standards per tool page, as in shared_brief.
CACHE = 512
FIELDS = {"rule": ("statement",), "answer": ("question", "answer"), "workflow": ("name", "steps")}
KIND_ORDER = {"rule": 0, "workflow": 1, "answer": 2}
TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
LABEL = (
    "Published standards from creators the owner follows. Use them for this task. The "
    "owner's own rules and answers win where they differ, and nothing here grants "
    "permission to spend, deploy or publish."
)
FOLLOW_HOW = (
    "Each topic uses one creator. When the owner says 'use @name for <topic>', call "
    "follow with action pick, the username and the topic. A task that names @name uses "
    "that creator. When a listed topic fits the task but no standards came back, call "
    "follow with action standards and that topic."
)
WHY = {
    "named": "named in the task",
    "picked": "the owner picked this creator for the topic",
    "latest": "the creator the owner followed most recently for the topic",
}
# Everyday task words for topics creators often publish, beyond the topic's own
# name: "review this PR" is code review work even though it never says so.
ALIASES = {
    slug: re.compile(r"(?<![\w-])(?:" + words + r")(?![\w-])", re.I)
    for slug, words in {
        "code-review": r"code reviews?|reviews? (?:of )?(?:the |this |my |our |a )?"
        r"(?:code|changes|diffs?|prs?|pull requests?|branch|commits?)|pull requests?|prs?",
        "testing": r"tests?|testing|test suites?|unit tests?|e2e|qa",
        "debugging": r"debug(?:ging)?|bugs?|crash(?:es|ing)?|stack traces?|failing",
        "refactoring": r"refactor(?:s|ing)?|clean(?:ing)? up (?:the |this )?code",
        "copywriting": r"copywriting|(?:ad|marketing|website|landing page|product) copy"
        r"|headlines?|taglines?",
        "design": r"designs?|designing|ui|ux|layouts?|typography",
    }.items()
}


def mentions(text: str, topics) -> list[str]:
    """The topics a task is about: by their own words, built-in kinds or aliases."""
    covered = list(dict.fromkeys(topics))
    found = [topic for topic in detect_domains(text, extra=covered) if topic in covered]
    return found + [
        topic
        for topic in covered
        if topic not in found and topic in ALIASES and ALIASES[topic].search(text)
    ]


def topic_slug(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-")
    if not TOPIC.fullmatch(slug) or slug == "general":
        fail(422, "invalid_topic", "Name the topic in a few words, for example code review")
    return slug


def topic_title(slug: str) -> str:
    return domain_title(slug)


def named_in(task: str, username: str) -> bool:
    name = re.escape(username)
    return bool(
        re.search(rf"(?<![\w@])@{name}(?!\w)", task, re.I)
        or re.search(rf"(?<![\w@]){name}(?:'s|’s)\s+varyz\b", task, re.I)
    )


def route(task, creators, picks, topics=()):
    """Which creator answers each topic of this task.

    creators are newest follow first, each with its published topics; picks map a
    topic to the creator the follower chose. Returns the chosen (topic, creator,
    why) entries and, per topic, the other creators that also cover it.
    """
    text = task or ""
    covered = sorted({topic for creator in creators for topic in creator["topics"]})
    if not covered:
        return [], []
    named = [creator for creator in creators if named_in(text, creator["username"])]
    matched = mentions(text, covered)
    matched += [topic for topic in topics if topic in covered and topic not in matched]
    chosen: dict[str, dict[str, Any]] = {}
    for topic in matched:
        covering = [creator for creator in creators if topic in creator["topics"]]
        pick, why = next((c for c in named if topic in c["topics"]), None), "named"
        if pick is None:
            pick, why = next((c for c in covering if c["id"] == picks.get(topic)), None), "picked"
        if pick is None:
            pick, why = covering[0], "latest"
        chosen[topic] = {"topic": topic, "creator": pick, "why": why}
    # A named creator with no matching topic lends all of theirs to this task.
    for creator in named:
        if not any(entry["creator"] is creator for entry in chosen.values()):
            for topic in sorted(creator["topics"]):
                if chosen.get(topic, {}).get("why") != "named":
                    chosen[topic] = {"topic": topic, "creator": creator, "why": "named"}
    also = []
    for topic, entry in chosen.items():
        others = [
            "@" + c["username"]
            for c in creators
            if topic in c["topics"] and c is not entry["creator"]
        ]
        if others:
            also.append({"topic": topic_title(topic), "creators": others})
    return list(chosen.values()), also


def compact(item):
    body = item["body"]
    if item["kind"] == "rule":
        return {"rule": body["statement"]}
    if item["kind"] == "answer":
        return {"question": body["question"], "answer": body["answer"]}
    return {"workflow": body["name"], "steps": body.get("steps", [])}


class Following:
    def __init__(self, sharing):
        self.sharing, self.store, self.settings = sharing, sharing.store, sharing.settings
        self._cache: OrderedDict[tuple[str, int], list[dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    # Reading published standards

    def items(self, db, owner, revision):
        """One publication, decrypted once per process and revision."""
        key = (owner, revision)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        row = db.execute(
            "SELECT payload FROM public_publications WHERE owner=? AND revision=?",
            (owner, revision),
        ).fetchone()
        value = self.sharing.open(row["payload"]) if row else []
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > CACHE:
                self._cache.popitem(last=False)
        return value

    def serving(self, db, creator):
        """A creator's standards reach followers while their Cloud plan is active."""
        row = db.execute("SELECT * FROM accounts WHERE id=?", (creator,)).fetchone()
        allowed = self.settings.shared_varyz_accounts
        if not row or (allowed and row["username"] not in allowed):
            return False
        return effective_account(db, row)["status"] == "active"

    def current(self, db, owner):
        row = db.execute("SELECT * FROM public_varyz WHERE owner=?", (owner,)).fetchone()
        if not row or row["status"] != "active":
            return (dict(row) if row else None), []
        return dict(row), self.items(db, owner, row["revision"])

    def catalog(self, db, follower):
        creators = []
        for row in db.execute(
            "SELECT f.creator,f.created,a.username,p.status,p.revision FROM follows f "
            "JOIN accounts a ON a.id=f.creator LEFT JOIN public_varyz p ON p.owner=f.creator "
            "WHERE f.follower=? ORDER BY f.created DESC,f.creator",
            (follower,),
        ).fetchall():
            published = row["status"] == "active"
            live = published and self.serving(db, row["creator"])
            items = self.items(db, row["creator"], row["revision"]) if live else []
            creators.append(
                {
                    "id": row["creator"],
                    "username": row["username"] or "creator",
                    "followed": row["created"],
                    "items": items,
                    "topics": {item["topic"] for item in items},
                    "paused": published and not live,
                }
            )
        picks = {
            row["topic"]: row["creator"]
            for row in db.execute(
                "SELECT topic,creator FROM follow_picks WHERE follower=?", (follower,)
            ).fetchall()
        }
        return creators, picks

    def pack(self, entries, offset=0, budget=BUDGET):
        """Standards grouped by creator and topic, cut at the page budget, never mid-item."""
        flat = [
            (entry, item)
            for entry in entries
            for item in entry["creator"]["items"]
            if item["topic"] == entry["topic"]
        ]
        groups: list[dict[str, Any]] = []
        delivered: set[str] = set()
        used, index = 0, offset
        for entry, item in flat[offset:]:
            value = compact(item)
            size = len(encoded(value))
            if used + size > budget and index > offset:
                break
            if not groups or groups[-1]["slug"] != entry["topic"]:
                groups.append(
                    {
                        "creator": "@" + entry["creator"]["username"],
                        "topic": topic_title(entry["topic"]),
                        "slug": entry["topic"],
                        "why": WHY[entry["why"]],
                        "standards": [],
                    }
                )
            groups[-1]["standards"].append(value)
            delivered.add(entry["creator"]["id"])
            used, index = used + size, index + 1
        return groups, (index if index < len(flat) else None), delivered

    def count(self, creators, kind):
        if not creators:
            return
        day = int(time.time() // 86400)
        with self.sharing.transaction() as db:
            for creator in sorted(creators):
                db.execute(
                    "INSERT INTO public_uses VALUES(?,?,?,1) ON CONFLICT(creator,day,kind) "
                    "DO UPDATE SET n=public_uses.n+1",
                    (creator, day, kind),
                )

    def summary(self, creators):
        return [
            {
                "creator": "@" + creator["username"],
                "topics": [topic_title(topic) for topic in sorted(creator["topics"])],
                **({"paused": True} if creator["paused"] else {}),
            }
            for creator in creators[:20]
        ]

    def brief(self, follower, task, topics=()):
        """Followed standards for one task; None when the follower follows nobody."""
        self.sharing.enabled()
        explicit = [topic_slug(topic) for topic in topics]
        with self.store.connection() as db:
            self.sharing.admitted(db, follower)
            creators, picks = self.catalog(db, follower)
        if not creators:
            return None
        chosen, also = route(task, creators, picks, explicit)
        groups, more, delivered = self.pack(chosen)
        self.count(delivered, "brief")
        value: dict[str, Any] = {
            "label": LABEL,
            "standards": groups,
            "following": self.summary(creators),
            "how": FOLLOW_HOW,
        }
        if also:
            value["also_available"] = also
        if more is not None:
            value["truncated"] = True
            value["more"] = "Call follow with action standards and a topic for the rest."
        return value

    def standards(self, follower, data):
        """All of one topic's standards (or one creator's), paged."""
        self.sharing.enabled()
        slug = topic_slug(data["topic"]) if data.get("topic") else None
        name = (data.get("username") or "").lstrip("@").lower()
        if not slug and not name:
            fail(422, "invalid_fields", "Pass topic, username or both")
        with self.store.connection() as db:
            self.sharing.admitted(db, follower)
            creators, picks = self.catalog(db, follower)
        creator = None
        if name:
            creator = next((c for c in creators if c["username"].lower() == name), None)
            if creator is None:
                fail(404, "not_following", "Follow @" + name + " first")
        if creator and slug:
            if slug not in creator["topics"]:
                fail(404, "no_topic", self.missing(creator, slug))
            entries = [{"topic": slug, "creator": creator, "why": "named"}]
        elif creator:
            entries = [
                {"topic": topic, "creator": creator, "why": "named"}
                for topic in sorted(creator["topics"])
            ]
        else:
            entries, _ = route("", creators, picks, [slug])
            if not entries:
                fail(404, "no_topic", "Nobody you follow has published " + str(slug) + " standards")
        groups, more, delivered = self.pack(entries, int(data.get("offset") or 0))
        self.count(delivered, "brief")
        return {"label": LABEL, "standards": groups, "next_offset": more}

    def task_brief(self, follower, task):
        """start_task's followed block; never fails the owner's own briefing."""
        try:
            return self.brief(follower, task)
        except SharingError:
            return None

    def catalog_for(self, follower):
        """Followed creators for owner-side lookups; empty when sharing is off for them."""
        try:
            self.sharing.enabled()
            with self.store.connection() as db:
                self.sharing.admitted(db, follower)
                return self.catalog(db, follower)
        except SharingError:
            return [], {}

    def answer(self, catalog, question, task=""):
        """One followed answer for the owner's ask, labelled as someone else's standard."""
        creators, picks = catalog
        found = self.match(creators, picks, question, task) if creators else None
        if not found:
            return None
        self.count({found.pop("creator_id")}, "answer")
        found["basis"] = "followed"
        return found

    @staticmethod
    def missing(creator, slug):
        topics = ", ".join(topic_title(topic) for topic in sorted(creator["topics"]))
        return (
            "@" + creator["username"] + " has not published " + topic_title(slug).lower()
            + " standards" + (". Their topics: " + topics if topics else "")
        )

    def ask(self, follower, questions, task=""):
        """Followed creators' published answers. Unmatched questions return not_found."""
        self.sharing.enabled()
        with self.store.connection() as db:
            self.sharing.admitted(db, follower)
            creators, picks = self.catalog(db, follower)
        results, used = [], set()
        for question in questions:
            found = self.match(creators, picks, question, task)
            if found:
                used.add(found.pop("creator_id"))
                results.append(found)
            else:
                results.append({"status": "not_found", "question": question})
        self.count(used, "answer")
        return {"results": results}

    @staticmethod
    def match(creators, picks, question, task=""):
        from varyz.resolution.knowledge import decision_score

        answers = [
            (creator, item)
            for creator in creators
            for item in creator["items"]
            if item["kind"] == "answer"
        ]
        candidates = [
            pair
            for pair in answers
            if normalized(pair[1]["body"]["question"]) == normalized(question)
        ]
        if not candidates:
            scored = [
                (decision_score(question, i["body"]["question"], i["body"]["answer"]), c, i)
                for c, i in answers
            ]
            ranked = sorted(
                [(score, creator, item) for score, creator, item in scored if score is not None],
                key=lambda entry: entry[0],
                reverse=True,
            )
            if ranked and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.2):
                candidates = [(ranked[0][1], ranked[0][2])]
        if not candidates:
            return None
        # One creator per topic, chosen exactly as for the brief.
        chosen, _ = route(
            (task or "") + "\n" + question,
            creators,
            picks,
            sorted({item["topic"] for _, item in candidates}),
        )
        preferred = {(entry["topic"], entry["creator"]["id"]) for entry in chosen}
        best = [p for p in candidates if (p[1]["topic"], p[0]["id"]) in preferred] or candidates
        if len({item["body"]["answer"] for _, item in best}) != 1:
            return None
        creator, item = best[0]
        return {
            "status": "answered",
            "question": question,
            "answer": item["body"]["answer"],
            "creator": "@" + creator["username"],
            "topic": topic_title(item["topic"]),
            "label": "Published standard from @"
            + creator["username"]
            + ", which the owner follows. Not the owner's own answer.",
            "creator_id": creator["id"],
        }

    # Following

    def creator_named(self, db, follower, username):
        name = (username or "").lstrip("@").lower()
        if not name:
            fail(422, "invalid_fields", "Pass username, the creator's Varyz username")
        row = db.execute("SELECT * FROM accounts WHERE lower(username)=?", (name,)).fetchone()
        if not row or not row["email_verified"]:
            fail(404, "no_account", "No Varyz account has the username " + name)
        if row["id"] == follower:
            fail(422, "own_account", "That is your own Varyz")
        allowed = self.settings.shared_varyz_accounts
        if allowed and row["username"] not in allowed:
            fail(403, "not_in_pilot", row["username"] + " is not in the Shared Varyz pilot yet")
        return dict(row)

    def profile_url(self, username):
        return self.settings.public_url + "/@" + username

    def follow(self, follower, data):
        """The follow tool: follow, unfollow, pick a creator for a topic, list, standards."""
        action = data.get("action") or "list"
        if action == "list":
            return self.following(follower)
        if action == "standards":
            return self.standards(follower, data)
        self.sharing.enabled()
        with self.sharing.transaction() as db:
            self.sharing.admitted(db, follower)
            creator = self.creator_named(db, follower, data.get("username"))
            name = creator["username"]
            if action == "unfollow":
                db.execute(
                    "DELETE FROM follows WHERE follower=? AND creator=?", (follower, creator["id"])
                )
                db.execute(
                    "DELETE FROM follow_picks WHERE follower=? AND creator=?",
                    (follower, creator["id"]),
                )
                return {"status": "unfollowed", "creator": "@" + name}
            if action == "pick" and not data.get("topic"):
                fail(422, "invalid_fields", "Pass topic, the kind of work to use @" + name + " for")
            exists = db.execute(
                "SELECT 1 FROM follows WHERE follower=? AND creator=?", (follower, creator["id"])
            ).fetchone()
            if not exists:
                total = db.execute(
                    "SELECT COUNT(*) AS n FROM follows WHERE follower=?", (follower,)
                ).fetchone()["n"]
                if total >= MAX_FOLLOWS:
                    fail(429, "follow_limit", "You follow 50 creators. Unfollow one first.")
                db.execute(
                    "INSERT INTO follows VALUES(?,?,?)", (follower, creator["id"], time.time())
                )
            _, items = self.current(db, creator["id"])
            topics = sorted({item["topic"] for item in items})
            slug = topic_slug(data["topic"]) if data.get("topic") else None
            if slug:
                if slug not in topics:
                    fail(
                        404,
                        "no_topic",
                        self.missing({"username": name, "topics": set(topics)}, slug),
                    )
                db.execute(
                    "INSERT INTO follow_picks VALUES(?,?,?,?) ON CONFLICT(follower,topic) "
                    "DO UPDATE SET creator=excluded.creator,updated=excluded.updated",
                    (follower, slug, creator["id"], time.time()),
                )
            titles = [topic_title(topic) for topic in topics]
            if slug:
                next_step = (
                    topic_title(slug) + " tasks now use @" + name + "'s standards. "
                    "Your own Varyz rules still win."
                )
            elif titles:
                next_step = (
                    "Tasks about " + ", ".join(t.lower() for t in titles) + " now use @" + name
                    + "'s standards. Your own Varyz rules still win."
                )
            else:
                next_step = (
                    "@" + name + " has not published standards yet. They arrive once published."
                )
            return {
                "status": "following",
                "creator": "@" + name,
                "topics": titles,
                "profile": self.profile_url(name),
                "next": next_step,
                **({"picked": topic_title(slug)} if slug else {}),
            }

    def following(self, follower):
        self.sharing.enabled()
        with self.store.connection() as db:
            self.sharing.admitted(db, follower)
            creators, picks = self.catalog(db, follower)
        return {
            "following": [
                {
                    "creator": "@" + creator["username"],
                    "profile": self.profile_url(creator["username"]),
                    "topics": [topic_title(t) for t in sorted(creator["topics"])],
                    **({"paused": True} if creator["paused"] else {}),
                }
                for creator in creators
            ],
            "choices": self.choices(creators, picks),
            "how": FOLLOW_HOW,
        }

    @staticmethod
    def choices(creators, picks):
        """Topics more than one followed creator covers, and who answers each now."""
        values = []
        for topic in sorted({t for creator in creators for t in creator["topics"]}):
            covering = [creator for creator in creators if topic in creator["topics"]]
            if len(covering) < 2:
                continue
            chosen, _ = route("", covering, picks, [topic])
            using = chosen[0]
            values.append(
                {
                    "topic": topic_title(topic),
                    "slug": topic,
                    "creators": ["@" + creator["username"] for creator in covering],
                    "using": "@" + using["creator"]["username"],
                    "picked": using["why"] == "picked",
                    "why": WHY[using["why"]],
                }
            )
        return values

    def page(self, viewer, username):
        """The public profile: a creator's standards by topic, readable by anyone."""
        name = (username or "").lstrip("@").lower()
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE lower(username)=?", (name,)
            ).fetchone()
            allowed = self.settings.shared_varyz_accounts
            if (
                not self.settings.shared_varyz_enabled
                or not row
                or not row["email_verified"]
                or (allowed and row["username"] not in allowed)
            ):
                fail(404, "no_profile", "No public Varyz has this name")
            public, items = self.current(db, row["id"])
            live = bool(items) and self.serving(db, row["id"])
            followers = db.execute(
                "SELECT COUNT(*) AS n FROM follows WHERE creator=?", (row["id"],)
            ).fetchone()["n"]
            following = bool(
                viewer
                and db.execute(
                    "SELECT 1 FROM follows WHERE follower=? AND creator=?", (viewer, row["id"])
                ).fetchone()
            )
        return {
            "username": row["username"],
            "url": self.profile_url(row["username"]),
            "own": viewer == row["id"],
            "following": following,
            "followers": followers,
            "paused": bool(items) and not live,
            "updated": public["updated"] if public else None,
            "topics": self.grouped(items if live else []),
        }

    @staticmethod
    def grouped(items):
        topics: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            topics.setdefault(item["topic"], []).append(item)
        return [
            {
                "slug": topic,
                "title": topic_title(topic),
                "items": sorted(values, key=lambda i: KIND_ORDER[i["kind"]]),
            }
            for topic, values in sorted(topics.items())
        ]

    # Publishing (creator side)

    def creator(self, db, owner):
        self.sharing.admitted(db, owner)
        row = db.execute("SELECT * FROM accounts WHERE id=?", (owner,)).fetchone()
        if not row or effective_account(db, row)["status"] != "active":
            fail(
                402,
                "plan_required",
                "Publishing standards needs the Cloud plan. Following others stays free.",
            )
        if not row["username"]:
            fail(409, "username_required", "Choose a username in Account before publishing")
        return row

    def candidates(self, owner, topic):
        """General confirmed preferences for the Choose step, suggested ones first."""
        self.sharing.enabled()
        slug = topic_slug(topic)
        with self.store.connection() as db:
            self.creator(db, owner)
            published = {
                (row["kind"], row["source_id"])
                for row in db.execute(
                    "SELECT kind,source_id FROM public_selections WHERE owner=? AND topic=? "
                    "AND suppressed IS NULL",
                    (owner, slug),
                ).fetchall()
            }
            rows = db.execute(
                "SELECT kind,id,version,payload FROM records WHERE account=? "
                "AND kind IN ('rule','answer','workflow') AND payload!='null' ORDER BY kind,id",
                (owner,),
            ).fetchall()
        values = []
        for row in rows:
            try:
                payload = canonical_payload(row["kind"], json.loads(row["payload"]))
            except (ValueError, KeyError, TypeError):
                continue
            if payload["scope"] != "*":
                continue
            body = {key: payload[key] for key in FIELDS[row["kind"]] if payload.get(key)}
            text = json.dumps(body, ensure_ascii=False)
            current = (row["kind"], row["id"]) in published
            # Answers are personal facts more often than standards: never pre-ticked.
            suggested = current or (
                row["kind"] != "answer"
                and (payload.get("domain") == slug or bool(mentions(text, [slug])))
            )
            values.append(
                {
                    "kind": row["kind"],
                    "id": row["id"],
                    "version": row["version"],
                    "body": body,
                    "suggested": suggested,
                    "published": current,
                }
            )
        values.sort(key=lambda v: (not v["suggested"], KIND_ORDER[v["kind"]], v["id"]))
        return {"topic": slug, "title": topic_title(slug), "items": values[:300]}

    def suggested_topics(self, owner, limit=6):
        """Kinds of work the creator's General rules cover and they have not published."""
        with self.store.connection() as db:
            _, items = self.current(db, owner)
            rows = db.execute(
                "SELECT kind,payload FROM records WHERE account=? "
                "AND kind IN ('rule','workflow') AND payload!='null'",
                (owner,),
            ).fetchall()
        published = {item["topic"] for item in items}
        counts: dict[str, int] = {}
        for row in rows:
            try:
                payload = canonical_payload(row["kind"], json.loads(row["payload"]))
            except (ValueError, KeyError, TypeError):
                continue
            if payload["scope"] != "*":
                continue
            text = json.dumps(
                {key: payload[key] for key in FIELDS[row["kind"]] if payload.get(key)},
                ensure_ascii=False,
            )
            found = set(detect_domains(text))
            found.update(slug for slug, pattern in ALIASES.items() if pattern.search(text))
            if payload.get("domain"):
                found.add(payload["domain"])
            for slug in found:
                if TOPIC.fullmatch(slug) and slug != "general" and slug not in published:
                    counts[slug] = counts.get(slug, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
        return [{"slug": slug, "title": topic_title(slug), "count": n} for slug, n in ranked]

    def projection(self, db, owner, topic, source, item_id=None):
        row = db.execute(
            "SELECT * FROM records WHERE account=? AND kind=? AND id=?",
            (owner, source["kind"], source["id"]),
        ).fetchone()
        if not row or row["version"] != source["version"] or row["payload"] == "null":
            fail(409, "source_changed", "A selected standard changed. Choose again.")
        try:
            payload = canonical_payload(source["kind"], json.loads(row["payload"]))
        except (ValueError, KeyError, TypeError):
            fail(422, "invalid_selection", "This preference cannot be published")
        if payload["scope"] != "*":
            fail(422, "invalid_selection", "Only General preferences can be public")
        body = {key: payload[key] for key in FIELDS[source["kind"]] if payload.get(key)}
        clean(body)
        if len(encoded(body)) > 6000:
            fail(422, "item_too_large", "Shorten a standard to fit the page limit")
        return {
            "item_id": item_id or identifier(),
            "kind": source["kind"],
            "topic": topic,
            "body": body,
            "source_id": source["id"],
            "source_version": row["version"],
            "published_at": time.time(),
        }

    def preview(self, owner, session, topic, selection):
        self.sharing.enabled()
        slug = topic_slug(topic)
        if not selection:
            fail(422, "selection_required", "Choose at least one standard to publish")
        if len(selection) > 200:
            fail(422, "brief_too_large", "Publish at most 200 standards per topic")
        with self.sharing.transaction() as db:
            account = self.creator(db, owner)
            existing = {
                (row["kind"], row["source_id"]): row["item"]
                for row in db.execute(
                    "SELECT kind,source_id,item FROM public_selections WHERE owner=? AND topic=?",
                    (owner, slug),
                ).fetchall()
            }
            items, seen = [], set()
            for source in selection:
                identity = (source["kind"], source["id"])
                if identity in seen:
                    fail(422, "duplicate_selection", "Select each standard once")
                seen.add(identity)
                items.append(self.projection(db, owner, slug, source, existing.get(identity)))
            _, current = self.current(db, owner)
            others = [item for item in current if item["topic"] != slug]
            self.limits(others + items)
            preview_id = identifier()
            value = {
                "preview_id": preview_id,
                "expires_at": time.time() + 600,
                "topic": slug,
                "title": topic_title(slug),
                "items": items,
                "replaces": len(current) - len(others),
                "url": self.profile_url(account["username"]),
            }
            db.execute(
                "INSERT INTO shared_previews VALUES(?,?,?,?,?,?)",
                (preview_id, owner, session, "public:" + slug, self.sharing.seal(value),
                 value["expires_at"]),
            )
            self.sharing.storage_budget(db, owner)
            return value

    @staticmethod
    def limits(items):
        if len(items) > 500 or len(encoded(items).encode()) > 262144:
            fail(422, "brief_too_large", "Public standards exceed the publication limit")

    def snapshot(self, db, owner, items):
        self.limits(items)
        row = db.execute("SELECT revision FROM public_varyz WHERE owner=?", (owner,)).fetchone()
        revision = (row["revision"] if row else 0) + 1
        now = time.time()
        status = "active" if items else "off"
        db.execute(
            "INSERT INTO public_publications VALUES(?,?,?,?,?)",
            (owner, revision, self.sharing.seal(items), digest(encoded(items)), now),
        )
        # Followers only read the current revision; keep a short history, not every edit.
        db.execute(
            "DELETE FROM public_publications WHERE owner=? AND revision<=?", (owner, revision - 10)
        )
        if row:
            db.execute(
                "UPDATE public_varyz SET status=?,revision=?,updated=? WHERE owner=?",
                (status, revision, now, owner),
            )
        else:
            db.execute(
                "INSERT INTO public_varyz VALUES(?,?,?,?,?)", (owner, status, revision, now, now)
            )
        return revision

    def publish(self, owner, session, preview_id, key):
        self.sharing.enabled()
        with self.sharing.transaction() as db:
            account = self.creator(db, owner)
            row = db.execute(
                "SELECT * FROM shared_previews WHERE id=? AND owner=? AND session=?",
                (preview_id, owner, session),
            ).fetchone()
            value = self.sharing.open(row["payload"]) if row else None
            if (
                not row
                or row["expires"] < time.time()
                or not str(row["share"]).startswith("public:")
            ):
                value = None

            def action():
                if value is None:
                    fail(409, "preview_expired", "Review the standards again")
                slug = value["topic"]
                for item in value["items"]:
                    source = {
                        "kind": item["kind"],
                        "id": item["source_id"],
                        "version": item["source_version"],
                    }
                    self.projection(db, owner, slug, source)
                _, current = self.current(db, owner)
                self.snapshot(
                    db, owner, [i for i in current if i["topic"] != slug] + value["items"]
                )
                db.execute(
                    "DELETE FROM public_selections WHERE owner=? AND topic=?", (owner, slug)
                )
                for item in value["items"]:
                    db.execute(
                        "INSERT INTO public_selections VALUES(?,?,?,?,?,?,?)",
                        (owner, item["item_id"], item["kind"], item["source_id"],
                         item["source_version"], slug, None),
                    )
                db.execute("DELETE FROM shared_previews WHERE id=?", (preview_id,))
                self.sharing.storage_budget(db, owner)
                return {
                    "status": "published",
                    "topic": value["title"],
                    "published": len(value["items"]),
                    "url": self.profile_url(account["username"]),
                    "post": self.post(account["username"], value["title"]),
                }

            return self.sharing.idempotent(
                db, owner, "public:" + preview_id, key, {"preview_id": preview_id}, action
            )

    def post(self, username, title):
        return (
            "I published the " + title.lower() + " standards I actually apply. Your AI can "
            "use them on your next project: " + self.profile_url(username)
        )

    def remove_topic(self, owner, topic):
        slug = topic_slug(topic)
        with self.sharing.transaction() as db:
            _, items = self.current(db, owner)
            remaining = [item for item in items if item["topic"] != slug]
            if len(remaining) != len(items):
                self.snapshot(db, owner, remaining)
            db.execute("DELETE FROM public_selections WHERE owner=? AND topic=?", (owner, slug))
            return {"status": "removed", "topic": topic_title(slug)}

    def unpublish(self, owner):
        with self.sharing.transaction() as db:
            public, items = self.current(db, owner)
            if public and (items or public["status"] == "active"):
                self.snapshot(db, owner, [])
            db.execute("DELETE FROM public_selections WHERE owner=?", (owner,))
            return {"status": "unpublished"}

    def suppress(self, db, owner):
        """Drop published standards whose source was deleted or left General."""
        dropped = []
        for source in db.execute(
            "SELECT * FROM public_selections WHERE owner=? AND suppressed IS NULL", (owner,)
        ).fetchall():
            row = db.execute(
                "SELECT payload FROM records WHERE account=? AND kind=? AND id=?",
                (owner, source["kind"], source["source_id"]),
            ).fetchone()
            eligible = bool(row and row["payload"] != "null")
            if eligible:
                try:
                    payload = canonical_payload(source["kind"], json.loads(row["payload"]))
                    eligible = payload["scope"] == "*"
                except (ValueError, KeyError, TypeError):
                    eligible = False
            if not eligible:
                dropped.append(source["item"])
                db.execute(
                    "UPDATE public_selections SET suppressed=? WHERE owner=? AND item=?",
                    (time.time(), owner, source["item"]),
                )
        if dropped:
            _, items = self.current(db, owner)
            self.snapshot(db, owner, [item for item in items if item["item_id"] not in dropped])

    def status(self, owner):
        """The creator's view: link, topics, followers, uses this week, a post to share."""
        with self.store.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (owner,)).fetchone()
            public, items = self.current(db, owner)
            followers = db.execute(
                "SELECT COUNT(*) AS n FROM follows WHERE creator=?", (owner,)
            ).fetchone()["n"]
            week = int(time.time() // 86400) - 6
            uses = {
                row["kind"]: row["n"]
                for row in db.execute(
                    "SELECT kind,SUM(n) AS n FROM public_uses WHERE creator=? AND day>=? "
                    "GROUP BY kind",
                    (owner, week),
                ).fetchall()
            }
            serving = bool(items) and self.serving(db, owner)
        username = account["username"] if account else None
        topics = self.grouped(items)
        latest = max(items, key=lambda i: i["published_at"])["topic"] if items else None
        return {
            "published": bool(items),
            "serving": serving,
            "username": username,
            "url": self.profile_url(username) if username else None,
            "topics": [
                {"topic": t["title"], "slug": t["slug"], "standards": len(t["items"])}
                for t in topics
            ],
            "followers": followers,
            "uses_this_week": {
                "tasks": int(uses.get("brief", 0)),
                "answers": int(uses.get("answer", 0)),
            },
            "post": self.post(username, topic_title(latest)) if username and latest else None,
            "updated": public["updated"] if public else None,
        }

    def purge(self, db, now):
        db.execute("DELETE FROM public_uses WHERE day<?", (int(now // 86400) - 90,))
        db.execute(
            "DELETE FROM public_publications WHERE created<? AND revision NOT IN "
            "(SELECT revision FROM public_varyz WHERE owner=public_publications.owner)",
            (now - 90 * 86400,),
        )

    def metrics(self, db):
        week = int(time.time() // 86400) - 6
        return {
            "creators_published": db.execute(
                "SELECT COUNT(*) AS n FROM public_varyz WHERE status='active'"
            ).fetchone()["n"],
            "follows": db.execute("SELECT COUNT(*) AS n FROM follows").fetchone()["n"],
            "followers": db.execute(
                "SELECT COUNT(DISTINCT follower) AS n FROM follows"
            ).fetchone()["n"],
            "uses_this_week": {
                row["kind"]: row["n"]
                for row in db.execute(
                    "SELECT kind,SUM(n) AS n FROM public_uses WHERE day>=? GROUP BY kind",
                    (week,),
                ).fetchall()
            },
        }
