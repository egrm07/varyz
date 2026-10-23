"""`varyz rules review`: what varyz learned about the owner, as a page to keep or drop.

A reading of the owner's turns (``profile_reader``) gives dozens of items at
once -- each project's vision and its lines never to cross, the owner's taste
and way of working, what "done" means for a kind of work, their workflows. One
at a time in a terminal that is a chore; here they come grouped by project and
by kind, each with the owner's own words behind it, and a group's items can
be kept or dropped together. The groups keep apart what applies to all the
owner's work with agents, their taste in everything they build, and each
project. Kept items are what agents are told from then on;
dropped ones are never suggested again. A kept workflow becomes the owner's
stated order for that kind of work; a kept decision, an answer `ask` gives.

The full collection separates confirmed live rules, all trial rules, pending
suggestions and withdrawn rules. Terminal review retains the trial spot-check.

The page is served the way `varyz setup` serves its own: on 127.0.0.1, with a
token that exists only in the link opened here, dressed like the Taste Card.

After a save that keeps rules, and again on Done, the page shows the owner's
AI type card (``card.work_with``), yellow first. Done leaves the
server up while that card is open, so it can be changed and downloaded; closing
the tab stops it, and so does a few minutes without a word from the page.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from varyz.resolution.knowledge import normalize_question, normalize_scope, stored_scope
from varyz.resolution.principles import APPLIES_TO, GLOBAL_SCOPE
from varyz.resolution.trials import TRIAL_DROPPED

PAGE = Path(__file__).resolve().parent / "review_web.html"
IDLE_LIMIT = 30 * 60.0
# After Done the page only shows the card: it stops pinging, and this much quiet ends it.
DONE_IDLE_LIMIT = 5 * 60.0
_MAX_BODY = 256 * 1024
_TOKEN_HEADER = "X-Varyz-Review"
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "font-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
SLOT_ORDER = ("vision", "never", "decision", "rule", "done", "workflow")
GROUP_ORDER = ("work", "products", "project")


def slot_of(item: dict[str, Any]) -> str:
    """Which part of the profile a candidate belongs to; a mined rule is a rule."""
    slot = (item.get("provenance") or {}).get("slot")
    if slot in SLOT_ORDER:
        return str(slot)
    if item.get("kind") == "vision":
        return "vision"
    return "done" if item.get("domain") else "rule"


def applies_to(item: dict[str, Any]) -> str:
    """What a candidate applies to: all the owner's work with agents ('work'), their
    taste in everything they build ('products'), or its project ('project').

    A rule for every project that says neither is about how they work.
    """
    if stored_scope(item.get("scope")) != GLOBAL_SCOPE:
        return "project"
    said = (item.get("provenance") or {}).get("applies_to")
    return str(said) if said in {"work", "products"} else "work"


def keep_candidate(
    client: Any, caller: Any, device_id: str, item: dict[str, Any],
    *, statement: str | None = None, scope: str | None = None,
    name: str | None = None, steps: list[str] | None = None,
    question: str | None = None, answer: str | None = None,
    applies: str | None = None,
) -> str:
    """Keep a candidate, as the owner worded it; a workflow becomes their stated order,
    a decision their answer to its question.

    The id kept.
    """
    decision = (item.get("provenance") or {}).get("decision")
    if isinstance(decision, dict):
        return _keep_decision(client, caller, device_id, item, decision,
                              question=question, answer=answer, scope=scope)
    workflow = (item.get("provenance") or {}).get("workflow")
    if isinstance(workflow, dict):
        workflow_id = client.add_workflow(
            name=name or str(workflow["name"]),
            steps=steps or list(workflow["steps"]),
            caller=caller,
            device_id=device_id,
            source=str(item.get("source") or "review"),
            scope=scope if scope is not None else item["scope"],
            domain=str(workflow.get("domain") or ""),
            provenance={"candidate_id": item["id"],
                        "quotes": (item.get("provenance") or {}).get("quotes", [])},
            rationale=str(item.get("rationale") or ""),
        )
        client.retract_principle(str(item["id"]), caller=caller, device_id=device_id,
                                 reason="kept as a workflow")
        return str(workflow_id)
    return str(client.confirm_principle(
        str(item["id"]), caller=caller, device_id=device_id,
        statement=statement, scope=scope, applies_to=applies,
    ))


def _keep_decision(
    client: Any, caller: Any, device_id: str, item: dict[str, Any],
    decision: dict[str, Any], *, question: str | None, answer: str | None,
    scope: str | None,
) -> str:
    """The owner's answer, recorded for `ask`: it replaces their earlier answer to the
    same question in the same place."""
    from varyz.personal_model.query import AnswerBasis

    question = question or str(decision["question"])
    answer = answer or str(decision["answer"])
    scope = normalize_scope(scope if scope is not None else str(item["scope"]))
    earlier = next((
        entry for entry in client.knowledge()
        if normalize_scope(str(entry.get("scope") or GLOBAL_SCOPE)) == scope
        and normalize_question(str(entry["question"])) == normalize_question(question)
    ), None)
    knowledge_id = client.remember(
        question=question, answer=answer, caller=caller, device_id=device_id,
        basis=AnswerBasis.EXPLICIT, source=str(item.get("source") or "review"),
        supersedes_id=str(earlier["id"]) if earlier else None, scope=scope,
    )
    client.retract_principle(str(item["id"]), caller=caller, device_id=device_id,
                             reason="kept as a decision")
    return str(knowledge_id)


def is_trial(item: dict[str, Any]) -> bool:
    """A rule in effect on trial, not a candidate (``Principle.trial``)."""
    return item.get("status") == "confirmed" and isinstance(
        (item.get("provenance") or {}).get("trial"), dict)


def drop_candidate(client: Any, caller: Any, device_id: str, item: dict[str, Any]) -> str:
    reason = TRIAL_DROPPED if is_trial(item) else "rejected in review"
    return str(client.retract_principle(str(item["id"]), caller=caller, device_id=device_id,
                                        reason=reason))


def _view(item: dict[str, Any]) -> dict[str, Any]:
    provenance = item.get("provenance") or {}
    quotes = provenance.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        quotes = [{"quote": provenance["quote"], "occurred": provenance.get("occurred")}] \
            if provenance.get("quote") else []
    workflow = provenance.get("workflow") if isinstance(provenance.get("workflow"), dict) \
        else None
    decision = provenance.get("decision") if isinstance(provenance.get("decision"), dict) \
        else None
    corrections = provenance.get("corrections")
    correction = next((found for found in corrections if isinstance(found, dict)), None) \
        if isinstance(corrections, list) else None
    trial = provenance.get("trial") if is_trial(item) else None
    return {
        "id": item["id"],
        "statement": item["statement"],
        "status": "trial" if trial else {
            "confirmed": "live", "candidate": "pending", "withdrawn": "withdrawn",
        }.get(str(item.get("status")), "pending"),
        "source": str(item.get("source") or ""),
        "created_at": str(item.get("created_at") or "")[:10],
        "withdrawn_at": str(item.get("withdrawn_at") or "")[:10],
        "withdrawn_reason": str(item.get("withdrawn_reason") or ""),
        "confirmation": str((provenance.get("trial_ended") or {}).get("how") or ""),
        # In effect on trial since then, on this evidence: the spot-check.
        "trial": {"since": str(trial.get("since") or "")[:10],
                  "signals": [str(signal) for signal in trial.get("signals") or []]}
        if trial else None,
        # A version folder stored before versions were folded is its project.
        "scope": stored_scope(item["scope"]),
        "applies_to": applies_to(item),
        "slot": slot_of(item),
        "domain": item.get("domain") or (workflow or {}).get("domain") or "",
        "messages": provenance.get("messages") or len(quotes) or None,
        "quotes": [{"quote": str(q.get("quote", "")), "occurred": str(q.get("occurred") or "")[:10]}
                   for q in quotes if isinstance(q, dict)][:6],
        "workflow": {"name": workflow["name"], "steps": workflow["steps"]} if workflow else None,
        "decision": {"question": decision["question"], "answer": decision["answer"]}
        if decision else None,
        "why": str(item.get("rationale") or ""),
        # The mistake behind it, first: what the agent did and what the owner wanted.
        "correction": {"agent_did": str(correction.get("agent_did") or ""),
                       "wanted": str(correction.get("wanted") or "")}
        if correction else None,
        "corrections": len(corrections) if isinstance(corrections, list) else 0,
        "lifted_from": [str(name) for name in provenance.get("lifted_from") or []],
        "file": provenance.get("file"),
    }


class ReviewSession:
    """The candidates, and the owner's decisions on them, through the daemon."""

    def __init__(self, client: Any, caller: Any, device_id: str, cfg: Any = None) -> None:
        self.client = client
        self.caller = caller
        self.device_id = device_id
        self.cfg = cfg
        self.offered = False
        self.token = secrets.token_urlsafe(24)
        self.kept = 0
        self.dropped = 0
        self._lock = threading.Lock()
        # The trial rules this session asks about, drawn once: deciding one must
        # not draw another, or the spot-check would never end.
        self._sample: list[str] | None = None

    def _spot_check(self) -> list[str]:
        if self._sample is None:
            try:
                self._sample = [str(rule_id) for rule_id in
                                self.client.trial_rules().get("spot_check_ids") or []]
            except Exception:  # a daemon from before trial rules: candidates only
                self._sample = []
        return self._sample

    def _candidates(self) -> dict[str, dict[str, Any]]:
        found = {str(item["id"]): dict(item) for item in self.client.principles("candidate")}
        sample = set(self._spot_check())
        if sample:
            for item in self.client.principles("confirmed"):
                if str(item["id"]) in sample and is_trial(item):
                    found[str(item["id"])] = dict(item)
        return found

    def pending(self) -> list[dict[str, Any]]:
        """The candidates and this session's spot-check of trial rules."""
        return list(self._candidates().values())

    def trial_count(self) -> int:
        """How many trial rules this session asks about."""
        return sum(1 for item in self._candidates().values() if is_trial(item))

    def items(self) -> dict[str, Any]:
        views = [_view(item) for item in self._collection().values()]
        views.sort(key=lambda view: (
            view["trial"] is None,
            GROUP_ORDER.index(view["applies_to"]), str(view["scope"]).casefold(),
            SLOT_ORDER.index(view["slot"]), -view["corrections"], -(view["messages"] or 0),
        ))
        counts = {status: sum(view["status"] == status for view in views)
                  for status in ("live", "trial", "pending", "withdrawn")}
        return {"items": views, "counts": counts,
                "kept": self.kept, "dropped": self.dropped}

    def _collection(self) -> dict[str, dict[str, Any]]:
        return {str(item["id"]): dict(item)
                for status in ("candidate", "confirmed", "withdrawn")
                for item in self.client.principles(status)}

    def decide(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            candidates = self._collection()
            done: list[str] = []
            errors: dict[str, str] = {}
            for decision in decisions:
                item = candidates.get(str(decision.get("id")))
                action = decision.get("action")
                if item is None or item.get("status") == "withdrawn":
                    errors[str(decision.get("id"))] = (
                        "This rule is no longer available. Refresh the page."
                    )
                    continue
                if action not in {"keep", "drop"}:
                    continue
                edits = _edits(decision, item)
                try:
                    if action == "keep":
                        if item.get("status") == "confirmed" and not is_trial(item):
                            if not edits:
                                continue
                            provenance = dict(item.get("provenance") or {})
                            if "applies" in edits:
                                provenance["applies_to"] = edits["applies"]
                            self.client.add_principle(
                                statement=edits.get("statement", item["statement"]),
                                scope=edits.get("scope", item["scope"]),
                                kind=item["kind"], domain=item.get("domain") or "",
                                rationale=item.get("rationale") or "", provenance=provenance,
                                source="owner:review", supersedes_id=str(item["id"]),
                                caller=self.caller, device_id=self.device_id,
                            )
                        else:
                            keep_candidate(self.client, self.caller, self.device_id, item, **edits)
                        self.kept += 1
                    else:
                        if item.get("status") == "confirmed" and not is_trial(item):
                            self.client.retract_principle(
                                str(item["id"]), caller=self.caller, device_id=self.device_id,
                                reason="withdrawn by the owner in rules review",
                            )
                        else:
                            drop_candidate(self.client, self.caller, self.device_id, item)
                        self.dropped += 1
                except Exception as exc:  # one refused item must not lose the rest
                    errors[str(item["id"])] = str(exc)
                    continue
                done.append(str(item["id"]))
            return {"done": done, "errors": errors}


    def _own_statements(self) -> list[str]:
        from varyz.card.work_with import own_statements_from_principles

        return own_statements_from_principles(self.client.principles("confirmed"))

    def card(self, name: object = None, lines: object = None) -> dict[str, Any]:
        """The owner's card in both styles, as the page shows it.

        ``name`` and ``lines`` are what the owner changed on the page; both are
        remembered for next time. Showing the card counts as the owner having seen
        its lines, which is what Monthly Varyz Wrapped compares against.
        """
        from varyz.card import work_with as card
        from varyz.card.rarity import BASIS, share_text

        statements = self._own_statements()
        own = card.owned(statements)
        data_dir = Path(self.cfg.data_dir) if self.cfg is not None else None
        with self._lock:
            state = card.load_state(data_dir) if data_dir is not None else {}
            if isinstance(name, str):
                who = card.clean_name(name)
            else:
                who = str(state.get("name") or "") or card.git_first_name()
            picked = card.valid_pick(lines) or card.valid_pick(state.get("lines")) \
                or card.default_pick(own)
            if data_dir is not None:
                card.mark_seen(state, who, statements, picked)
                if card.valid_pick(lines):
                    state["lines"] = picked
                card.save_state(data_dir, state)
        kind = card.card_type(self._type_rules())
        return {
            "name": who,
            "title": card.card_title(who, kind),
            "type": {"key": kind.key, "title": kind.title, "line": kind.line,
                     "share": share_text(kind.key)},
            "basis": BASIS,
            "lines": picked,
            "choices": [{"key": line.key, "text": line.plain, "own": line.key in own}
                        for line in card.LINES],
            "images": {style: card.data_url(card.render_png(who, kind, picked, style))
                       for style in card.STYLES},
            "file": card.file_stem(who),
        }

    def _type_rules(self) -> list[Any]:
        """All the owner's rules, from the archive when it can be read."""
        from varyz.card import work_with as card
        from varyz.card.source import load_owner_rules

        if self.cfg is not None and Path(self.cfg.db_path).is_file():
            try:
                return card.type_rules(load_owner_rules(Path(self.cfg.db_path)))
            except Exception:  # a locked or older archive falls back to the daemon's rows
                pass
        return card.type_rules_from_principles(self.client.principles("confirmed"))

    def keep_card_lines(self, lines: object) -> list[str]:
        """Keep the card's starter lines as the owner's rules; the keys added.

        A card must not say what the owner's agents were not told, so a starter
        becomes a confirmed general rule when the owner keeps the card.
        """
        from varyz.card import work_with as card

        picked = card.valid_pick(lines)
        if picked is None:
            raise ValueError("expected three card lines")
        with self._lock:
            own = card.owned(self._own_statements())
            added: list[str] = []
            for key in picked:
                if key in own:
                    continue
                self.client.add_principle(
                    statement=card.BY_KEY[key].rule, scope=GLOBAL_SCOPE, kind="rule",
                    source=card.CARD_SOURCE,
                    provenance={"card_line": key, "applies_to": "work"},
                    caller=self.caller, device_id=self.device_id,
                )
                added.append(key)
            return added

    def network_offer(self) -> dict[str, str] | None:
        """The Varyz network popup, once a save has shown what reviewing is worth.

        At most once per review, and only when analytics.claim_offer says it is due.
        """
        from varyz import analytics

        with self._lock:
            if self.cfg is None or self.offered or not analytics.claim_offer(self.cfg):
                return None
            self.offered = True
        return {"title": analytics.CONSENT_TITLE, "message": analytics.CONSENT_MESSAGE,
                "counts": analytics.CONSENT_COUNTS, "privacy": analytics.CONSENT_PRIVACY}

    def network_answer(self, share: bool) -> bool:
        """The owner's answer to this review's popup. The x and Escape send no."""
        from varyz import analytics

        if self.cfg is None or not self.offered:
            raise PermissionError("no network offer was shown in this review")
        analytics.consent(self.cfg, share)
        return bool(analytics.status(self.cfg)["consent"])


def _edits(decision: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """What the owner changed on the page: only fields that differ from the candidate."""

    def text(value: Any) -> str:
        return " ".join(value.split()) if isinstance(value, str) else ""

    edits: dict[str, Any] = {}
    statement = text(decision.get("statement"))
    if statement and statement != item["statement"]:
        edits["statement"] = statement
    scope = decision.get("scope")
    if isinstance(scope, str) and text(scope) != stored_scope(item["scope"]):
        edits["scope"] = text(scope) or GLOBAL_SCOPE
    applies = decision.get("applies_to")
    if applies in APPLIES_TO and applies != applies_to(item):
        edits["applies"] = applies
    if "applies" in edits or "scope" in edits:
        # Every project's rule says what it is about; a project's rule is the project's.
        global_scope = stored_scope(edits.get("scope", item["scope"])) == GLOBAL_SCOPE
        chosen = edits.get("applies", applies_to(item))
        edits["applies"] = (chosen if chosen != "project" else "work") if global_scope \
            else "project"
    workflow = (item.get("provenance") or {}).get("workflow")
    if isinstance(workflow, dict):
        name = text(decision.get("name"))
        if name and name != workflow.get("name"):
            edits["name"] = name
        raw = decision.get("steps")
        steps = [text(step) for step in raw if text(step)] if isinstance(raw, list) else []
        if steps and steps != list(workflow.get("steps") or []):
            edits["steps"] = steps
        edits.pop("statement", None)
    decision_was = (item.get("provenance") or {}).get("decision")
    if isinstance(decision_was, dict):
        for field in ("question", "answer"):
            value = text(decision.get(field))
            if value and value != decision_was.get(field):
                edits[field] = value
        edits.pop("statement", None)
    return edits


def render_page(token: str) -> bytes:
    from varyz.card import layout
    from varyz.card.fonts import SANS_STACK, SERIF_STACK
    from varyz.card.render import MARK_PATH

    values = {
        "TOKEN": json.dumps(token),
        "MARK_PATH_URL": quote(MARK_PATH),
        "MARK_PATH": MARK_PATH,
        "NIGHT": layout.NIGHT,
        "INK": layout.INK,
        "MUTED": layout.MUTED,
        "DIM": layout.DIM,
        "SIGNAL": layout.SIGNAL,
        "LINE": layout.LINE,
        "SERIF_STACK": SERIF_STACK,
        "SANS_STACK": SANS_STACK,
    }
    html = PAGE.read_text(encoding="utf-8")
    for name, value in values.items():
        html = html.replace("{{" + name + "}}", value)
    return html.encode("utf-8")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: ReviewSession, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__((host, port), _Handler)
        self.session = session
        self.last_request = time.monotonic()
        self.closed = threading.Event()
        # Done was pressed: the page shows the card and no longer pings.
        self.done = False

    def idle_limit(self, limit: float = IDLE_LIMIT) -> float:
        return min(limit, DONE_IDLE_LIMIT) if self.done else limit

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/?t={self.session.token}"

    def allowed_hosts(self) -> set[str]:
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}


class _Handler(BaseHTTPRequestHandler):
    server: ReviewServer
    server_version = "varyz-review"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              *, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json")

    def _text(self, status: HTTPStatus, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _guarded(self) -> bool:
        self.server.last_request = time.monotonic()
        if self.headers.get("Host", "") not in self.server.allowed_hosts():
            self._text(HTTPStatus.FORBIDDEN, "wrong host")
            return False
        return True

    def _token_ok(self, given: str | None) -> bool:
        return given is not None and secrets.compare_digest(given, self.server.session.token)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._guarded():
            return
        parts = urlsplit(self.path)
        if parts.path == "/":
            if not self._token_ok(parse_qs(parts.query).get("t", [None])[0]):
                self._text(HTTPStatus.FORBIDDEN,
                           "Open the link `varyz rules review` printed; this one has no key.")
                return
            self.server.done = False  # The page opened again: it pings again.
            self._send(HTTPStatus.OK, render_page(self.server.session.token),
                       "text/html; charset=utf-8")
            return
        if parts.path.startswith("/fonts/"):
            from varyz.setup_web import _fonts, font_type

            font = _fonts().get(parts.path.removeprefix("/fonts/"))
            if font is None:
                self._text(HTTPStatus.NOT_FOUND, "no such font")
                return
            self._send(HTTPStatus.OK, font.read_bytes(), font_type(font), cache=True)
            return
        if not self._token_ok(self.headers.get(_TOKEN_HEADER)):
            self._text(HTTPStatus.FORBIDDEN, "missing key")
            return
        if parts.path == "/api/ping":
            self._json({"ok": True})
            return
        if parts.path == "/api/items":
            try:
                self._json(self.server.session.items())
            except Exception as exc:
                self._json({"error": f"varyz could not list them: {exc}"},
                           HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._guarded():
            return
        if not self._token_ok(self.headers.get(_TOKEN_HEADER)):
            self._text(HTTPStatus.FORBIDDEN, "missing key")
            return
        path = urlsplit(self.path).path
        if path == "/api/close":
            self._json({"closed": True})
            self.server.closed.set()
            return
        if path == "/api/done":
            self.server.done = True
            self._json({"done": True})
            return
        if path in {"/api/card", "/api/card/keep"}:
            self._card(path)
            return
        if path == "/api/network/offer":
            try:
                offer = self.server.session.network_offer()
            except Exception:
                offer = None  # The offer never gets in the way of review.
            self._json({"offer": offer})
            return
        if path == "/api/network/answer":
            answer = self._body()
            share = answer.get("share") if isinstance(answer, dict) else None
            if not isinstance(share, bool):
                self._text(HTTPStatus.BAD_REQUEST, "expected {\"share\": true|false}")
                return
            try:
                self._json({"sharing": self.server.session.network_answer(share)})
            except PermissionError as exc:
                self._text(HTTPStatus.CONFLICT, str(exc))
            return
        if path != "/api/decide":
            self._text(HTTPStatus.NOT_FOUND, "not found")
            return
        body = self._body()
        decisions = body.get("decisions") if isinstance(body, dict) else None
        if not isinstance(decisions, list) or not all(isinstance(d, dict) for d in decisions):
            self._text(HTTPStatus.BAD_REQUEST, "expected {\"decisions\": [...]}")
            return
        try:
            self._json(self.server.session.decide(decisions))
        except Exception as exc:
            self._json({"error": f"varyz could not save them: {exc}"},
                       HTTPStatus.SERVICE_UNAVAILABLE)

    def _card(self, path: str) -> None:
        body = self._body()
        if not isinstance(body, dict):
            self._text(HTTPStatus.BAD_REQUEST, "expected a JSON object")
            return
        try:
            if path == "/api/card/keep":
                self._json({"kept": self.server.session.keep_card_lines(body.get("lines"))})
            else:
                self._json(self.server.session.card(body.get("name"), body.get("lines")))
        except ValueError as exc:
            self._text(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._json({"error": f"varyz could not make the card: {exc}"},
                       HTTPStatus.SERVICE_UNAVAILABLE)

    def _body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 <= length <= _MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None


def serve(session: ReviewSession, *, open_browser: bool = True,
          idle_limit: float = IDLE_LIMIT) -> tuple[int, int]:
    """Serve the page until the owner closes it; (kept, dropped)."""
    from varyz.cli import console

    server = ReviewServer(session)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                              daemon=True, name="varyz-review-server")
    thread.start()
    console.print(f"Review is open in your browser: {server.url}", highlight=False,
                  soft_wrap=True)
    if open_browser:
        try:
            webbrowser.open(server.url)
        except webbrowser.Error:
            console.print("Could not open a browser; open the link above yourself.")
    try:
        while not server.closed.wait(1.0):
            if time.monotonic() - server.last_request > server.idle_limit(idle_limit):
                if not server.done:
                    console.print("Nothing heard from the page for a while; stopping.")
                break
    except KeyboardInterrupt:
        console.print("Stopped.")
    finally:
        server.shutdown()
        server.server_close()
    return session.kept, session.dropped


__all__ = [
    "ReviewServer", "ReviewSession", "drop_candidate", "keep_candidate", "serve", "slot_of",
]
