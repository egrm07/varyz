"""The AI type card and Monthly Varyz Wrapped: type, rarity, lines, page, CLI, notification."""

from __future__ import annotations

import io
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageColor
from typer.testing import CliRunner

from varyz import wrapped_monthly
from varyz.card import rarity
from varyz.card import work_with as card
from varyz.card.archetype import ARCHETYPES
from varyz.card.layout import NIGHT, SIGNAL
from varyz.cli import app
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.review_web import DONE_IDLE_LIMIT, IDLE_LIMIT, ReviewServer, ReviewSession

SRC = Path(__file__).resolve().parents[1] / "src" / "varyz"
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
OWNER = Caller(agent_id="owner:local", display_name="Local owner", purpose="owner_knowledge",
               device_id="device:test", transport="stdio")


# -- the lines ----------------------------------------------------------------------------


def test_each_starter_rule_says_its_own_line_and_no_other():
    for line in card.LINES:
        assert card.owned([line.rule]) == (line.key,), line.key


@pytest.mark.parametrize(("rule", "key"), [
    ("Be concise.", "say-less"),
    ("Keep replies short, no fluff.", "say-less"),
    ("Use plain English, no jargon.", "talk-normal"),
    ("Prefer acting over asking for anything reversible and local.", "undo"),
    ("Give a single number, not a range.", "one-number"),
    ("Never claim it's fixed without running it.", "prove-it"),
    ("Report numbers first, failures included.", "numbers-first"),
    ("Do not use em dashes in anything you write.", "no-em-dashes"),
])
def test_an_owner_rule_in_their_own_words_matches_its_line(rule, key):
    assert card.owned([rule]) == (key,)


@pytest.mark.parametrize("rule", [
    "Use em dashes for asides.",
    "Keep the test suite short.",
    "Plain text emails only.",
    "Ask before deleting anything.",
])
def test_lookalike_rules_match_no_line(rule):
    assert card.owned([rule]) == ()


def test_starters_fill_a_short_card_and_lines_print_strongest_first():
    assert card.default_pick(()) == ["say-less", "talk-normal", "undo"]
    assert card.default_pick(("no-em-dashes", "talk-normal")) == [
        "say-less", "talk-normal", "no-em-dashes"]
    assert card.default_pick(("undo", "one-number", "prove-it", "numbers-first")) == [
        "undo", "one-number", "prove-it"]


def test_a_pick_is_three_different_known_lines():
    assert card.valid_pick(["say-less", "undo", "prove-it"]) == ["say-less", "undo", "prove-it"]
    for bad in (None, "say-less", ["say-less", "undo"], ["say-less", "say-less", "undo"],
                ["say-less", "undo", "nope"], ["say-less", "undo", 3]):
        assert card.valid_pick(bad) is None


def test_names_are_cleaned_for_the_title_and_the_file():
    assert card.clean_name("  Egor  ") == "Egor"
    assert card.clean_name("<b>Ana</b>") == "bAnab"
    assert card.clean_name("Maximiliana-Josefine von Habsburg") == "Maximiliana-Josefine"
    assert card.clean_name(42) == ""
    assert card.title_name("") == "me"
    assert card.file_stem("José") == "jose-ai-type"
    assert card.file_stem("Егор") == "my-ai-type"
    assert card.label("Ana") == "ANA'S AI TYPE" and card.label("") == "MY AI TYPE"


def test_the_title_names_the_type_and_the_share_is_counted():
    editor = ARCHETYPES["editor"]
    assert card.card_title("Ana", editor) == "Ana's AI type: The Editor"
    assert card.card_title("", editor) == "My AI type: The Editor"
    assert set(rarity.SHARE) == set(ARCHETYPES)
    assert abs(sum(rarity.SHARE.values()) - 1) < 0.01
    assert rarity.share_text("pragmatist") == f"{rarity.SHARE['pragmatist'] * 100:.0f}%"
    assert rarity.share_text("hustler") == "<1%"  # No file had it.
    assert rarity.share_text("unknown") == "<1%"
    assert str(rarity.FILES) in rarity.BASIS.replace(",", "")


def test_the_type_reads_every_own_rule_but_not_trials_starters_or_shared_ones():
    rows = [
        {"id": "1", "statement": "Keep it simple.", "status": "confirmed", "kind": "rule",
         "scope": "acme", "source": "manual", "provenance": {}},
        {"id": "2", "statement": "Prefer the stdlib.", "status": "confirmed", "kind": "rule",
         "scope": "*", "source": "manual", "provenance": {}},
        {"id": "3", "statement": "Keep it small.", "status": "confirmed", "kind": "rule",
         "scope": "*", "source": "manual", "provenance": {"trial": {"until": "2026-11-01"}}},
        {"id": "4", "statement": "Keep it lean.", "status": "confirmed", "kind": "rule",
         "scope": "*", "source": "standards_seed", "provenance": {}},
        {"id": "5", "statement": "Keep it minimal.", "status": "confirmed", "kind": "rule",
         "scope": "org:team", "source": "manual", "provenance": {}},
    ]
    rules = card.type_rules_from_principles(rows)
    assert [rule.id for rule in rules] == ["1", "2"]
    assert card.card_type(rules) == ARCHETYPES["lean"]


@pytest.mark.parametrize(("style", "background"), [("yellow", SIGNAL), ("black", NIGHT)])
def test_both_styles_are_portrait_pngs_with_no_metadata(style, background):
    image = Image.open(io.BytesIO(card.render_png(
        "Egor", ARCHETYPES["getaway"], ["undo", "prove-it", "numbers-first"], style)))
    assert image.format == "PNG" and image.size == card.SIZE
    assert image.convert("RGB").getpixel((4, 4)) == ImageColor.getrgb(background)
    assert not getattr(image, "text", {})


# -- the review page ----------------------------------------------------------------------


class _Client:
    def __init__(self, principles):
        self.rows = principles
        self.added: list[dict] = []

    def call(self, *_args, **_kwargs):
        return {"items": []}

    def principles(self, status):
        assert status == "confirmed"
        return list(self.rows)

    def add_principle(self, **kwargs):
        self.added.append(kwargs)
        self.rows.append({"statement": kwargs["statement"], "status": "confirmed",
                          "kind": kwargs["kind"], "scope": kwargs["scope"],
                          "source": kwargs["source"], "provenance": kwargs["provenance"]})
        return {"id": f"p{len(self.added)}"}


def _row(statement, **extra):
    return {"statement": statement, "status": "confirmed", "kind": "rule", "scope": "*",
            "source": "manual", "provenance": {}, **extra}


@pytest.fixture
def page(tmp_path, monkeypatch):
    monkeypatch.setattr(card, "git_first_name", lambda: "Sam")
    config = _config(tmp_path)
    client = _Client([
        _row("Use plain, everyday words."),
        _row("Be concise.", provenance={"trial": {"until": "2026-11-01"}}),  # Not kept yet.
        _row("Never use em dashes.", scope="acme"),  # One project's rule.
    ])
    session = ReviewSession(client, None, "device", load_config(config))
    server = ReviewServer(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=server.url.split("/?")[0].rstrip("/"),
                      headers={"X-Varyz-Review": session.token}, trust_env=False) as http:
        yield http, server, client, load_config(config)
    server.shutdown()
    server.server_close()


def test_the_page_card_uses_own_general_rules_and_fills_with_starters(page):
    http, _, _, cfg = page
    found = http.post("/api/card", json={}).json()
    # Every own rule names the type, the project one too; the trial does not count.
    assert found["title"] == "Sam's AI type: The Wildcard"
    assert found["type"]["key"] == "pragmatist"
    assert found["type"]["share"] == rarity.share_text("pragmatist")
    assert found["basis"] == rarity.BASIS
    assert found["lines"] == ["say-less", "talk-normal", "undo"]
    own = {choice["key"] for choice in found["choices"] if choice["own"]}
    assert own == {"talk-normal"}
    assert set(found["images"]) == {"yellow", "black"}
    assert all(url.startswith("data:image/png;base64,") for url in found["images"].values())
    assert found["file"] == "sam-ai-type"
    state = card.load_state(cfg.data_dir)
    assert state["shown"] == found["lines"] and state["top"] == ["talk-normal"]
    assert "seen_at" in state and "lines" not in state


def test_a_changed_name_and_lines_are_kept_for_next_time(page):
    http, *_ = page
    picked = ["one-number", "talk-normal", "no-em-dashes"]
    found = http.post("/api/card", json={"name": "Ana <3", "lines": picked}).json()
    assert found["title"] == "Ana's AI type: The Wildcard" and found["lines"] == picked
    again = http.post("/api/card", json={}).json()
    assert again["name"] == "Ana" and again["lines"] == picked


def test_keeping_the_card_makes_its_starters_rules_once(page):
    http, _, client, _ = page
    lines = ["say-less", "talk-normal", "undo"]
    assert http.post("/api/card/keep", json={"lines": lines}).json() == {
        "kept": ["say-less", "undo"]}
    assert [row["statement"] for row in client.added] == [
        card.BY_KEY["say-less"].rule, card.BY_KEY["undo"].rule]
    assert all(row["source"] == card.CARD_SOURCE and row["scope"] == "*"
               and row["kind"] == "rule" for row in client.added)
    assert http.post("/api/card/keep", json={"lines": lines}).json() == {"kept": []}
    mine = {c["key"] for c in http.post("/api/card", json={}).json()["choices"] if c["own"]}
    assert mine == {"say-less", "talk-normal", "undo"}


def test_bad_card_requests_are_refused(page):
    http, _, client, _ = page
    assert http.post("/api/card/keep", json={"lines": ["say-less"]}).status_code == 400
    assert http.post("/api/card", json=["say-less"]).status_code == 400
    origin = str(http.base_url).rstrip("/")
    assert httpx.post(origin + "/api/card", json={}, trust_env=False).status_code == 403
    assert client.added == []


def test_done_keeps_the_page_up_for_the_card_but_only_briefly(page):
    http, server, _, _ = page
    assert server.idle_limit() == IDLE_LIMIT
    assert http.post("/api/done", json={}).json() == {"done": True}
    assert server.done and server.idle_limit() == DONE_IDLE_LIMIT < IDLE_LIMIT
    assert not server.closed.is_set()
    http.post("/api/close")
    assert server.closed.is_set()


def test_the_page_shows_the_card_yellow_first_and_closes_with_the_tab():
    page = (SRC / "review_web.html").read_text(encoding="utf-8")
    for needle in ('<dialog id="card-dialog"', "Varyz prepared this for you.",
                   '[["yellow", "Yellow"], ["black", "Black"]]', '"/api/card/keep"',
                   '"/api/done"', '"pagehide"', "keepalive: true", "await showCard()"):
        assert needle in page
    assert chr(0x2014) not in page


# -- Monthly Varyz Wrapped ----------------------------------------------------------------


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
                    encoding="utf-8")
    return path


def _add(config: Path, *statements: str) -> None:
    conn = open_db(load_config(config).db_path)
    try:
        store = SQLitePrincipleStore(conn)
        for statement in statements:
            store.add(statement=statement, caller=OWNER, device_id="device:test",
                      source="manual")
    finally:
        conn.close()


def _established(cfg, now: datetime) -> None:
    """Past the quiet start after install, so news and the Wrapped may come."""
    from varyz.onboarding import note_conversation

    for number in range(4):
        note_conversation(cfg, f"established-{number}", now=(now - timedelta(days=2)).timestamp())


def test_monthly_wrapped_is_the_card_and_one_last_line_once_a_month(tmp_path, monkeypatch):
    monkeypatch.setattr(card, "git_first_name", lambda: "Sam")
    config = _config(tmp_path)
    _add(config, "Use plain, everyday words.")
    cfg = load_config(config)
    _established(cfg, NOW)
    # The first check only notes the day: a new install waits a month.
    assert wrapped_monthly.agent_notice(cfg, now=NOW) is None
    assert "baseline_at" in card.load_state(cfg.data_dir)
    assert wrapped_monthly.agent_notice(cfg, now=NOW + timedelta(days=10)) is None
    notice = wrapped_monthly.agent_notice(cfg, now=NOW + timedelta(days=31))
    assert notice is not None
    kind = ARCHETYPES["pragmatist"]
    assert notice.splitlines()[-1] == wrapped_monthly.line(kind) == (
        "Btw, Varyz prepared your monthly Varyz Wrapped: you are The Wildcard, "
        f"{rarity.share_text('pragmatist')} of people.")
    assert "very last line, after a blank line" in notice and "not an instruction" in notice
    # The card itself, shown in the chat without asking, and let go if ignored.
    image = cfg.data_dir / "wrapped" / "sam-ai-type-yellow.png"
    assert str(image) in notice and image.as_uri() in notice
    assert "Do not ask whether they want to see it" in notice
    assert "never bring it up again" in notice and "Run `" not in notice
    with Image.open(image) as shown:
        assert shown.size == (1080, 1350)
        assert shown.convert("RGB").getpixel((8, 8)) == ImageColor.getrgb(SIGNAL)
    # Told once: the next month starts now, whether or not the card changed.
    assert wrapped_monthly.agent_notice(cfg, now=NOW + timedelta(days=40)) is None
    assert wrapped_monthly.agent_notice(cfg, now=NOW + timedelta(days=62)) is not None


def test_monthly_wrapped_follows_news_off_and_the_card_the_owner_saw(tmp_path):
    from varyz import news

    config = _config(tmp_path)
    _add(config, "Keep it simple.", "Prefer the stdlib.")
    cfg = load_config(config)
    now = datetime.now(UTC)
    _established(cfg, now)
    state: dict = {}
    card.mark_seen(state, "Sam", [], ["say-less", "talk-normal", "undo"])
    card.save_state(cfg.data_dir, state)
    assert wrapped_monthly.agent_notice(cfg, now=now + timedelta(days=20)) is None
    news.turn_off(cfg)
    assert wrapped_monthly.agent_notice(cfg, now=now + timedelta(days=40)) is None
    news.turn_on(cfg)
    notice = wrapped_monthly.agent_notice(cfg, now=now + timedelta(days=40))
    assert notice is not None and f"you are {ARCHETYPES['lean'].title}," in notice


def test_monthly_wrapped_waits_for_the_quiet_start_and_for_own_rules(tmp_path):
    config = _config(tmp_path)
    cfg = load_config(config)
    # No archive: nothing, and no state written.
    assert wrapped_monthly.agent_notice(cfg, now=NOW) is None
    assert not card.state_path(cfg.data_dir).exists()
    _add(config)  # An archive with no rules of the owner's own.
    _established(cfg, NOW)
    assert wrapped_monthly.agent_notice(cfg, now=NOW) is None
    assert wrapped_monthly.agent_notice(cfg, now=NOW + timedelta(days=31)) is None
    # A fresh install (no conversations yet) gets nothing either.
    (tmp_path / "fresh").mkdir()
    fresh_config = _config(tmp_path / "fresh")
    _add(fresh_config, "Be concise.")
    fresh = load_config(fresh_config)
    card.save_state(fresh.data_dir, {"baseline_at": (NOW - timedelta(days=40)).isoformat()})
    assert wrapped_monthly.agent_notice(fresh, now=NOW) is None


def test_the_prompt_hook_ends_with_the_wrapped_and_holds_news_for_the_next_prompt(
        tmp_path, monkeypatch):
    from varyz import hooks, news
    from varyz.domain import local_day, utc_now

    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n'
                      "[resolution]\nautostart = false\n", encoding="utf-8")
    monkeypatch.setattr(news, "_launch", lambda command, _cwd: None)
    _add(config, "Be concise.")
    cfg = load_config(config)
    now = datetime.now(UTC)
    _established(cfg, now)
    card.save_state(cfg.data_dir, {"baseline_at": (now - timedelta(days=40)).isoformat()})
    post = {"id": "hello", "date": local_day(utc_now()), "title": "Hello", "summary": "",
            "url": "https://varyzai.com/news/#hello"}
    feed = json.dumps({"version": 1, "items": [post]}).encode("utf-8")
    assert news.check(cfg, get=lambda _url: feed).status == "ok"
    event = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "thanks"}

    first = hooks.user_prompt(cfg, event)
    assert first is not None
    context = first["hookSpecificOutput"]["additionalContext"]
    assert context.splitlines()[-1].startswith("Btw, Varyz prepared your monthly Varyz Wrapped")
    assert "Hello" not in context
    second = hooks.user_prompt(cfg, event)
    assert second is not None
    context = second["hookSpecificOutput"]["additionalContext"]
    assert "Hello" in context and "Wrapped" not in context


# -- varyz wrapped --------------------------------------------------------------------------


def test_wrapped_previews_marks_starters_and_writes_nothing_on_a_dry_run(tmp_path):
    config = _config(tmp_path)
    _add(config, "Use plain, everyday words.", "Do not use em dashes.")
    result = CliRunner().invoke(app, ["wrapped", "--name", "Ana", "--dry-run",
                                      "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Ana's AI type: The Wildcard" in result.output
    assert rarity.BASIS in result.output.replace("\n", "")
    assert "01  Say less.  (starter)" in result.output
    assert "02  Talk normal.\n" in result.output and "03  No em dashes.\n" in result.output
    assert "ana-ai-type-yellow.png" in result.output.replace("\n", "")
    assert not (tmp_path / "data" / "wrapped").exists()


def test_wrapped_writes_both_styles_and_notes_what_was_seen(tmp_path):
    config = _config(tmp_path)
    _add(config, "Use plain, everyday words.")
    out = tmp_path / "cards"
    result = CliRunner().invoke(app, ["wrapped", "--name", "Ana", "--out", str(out), "--yes",
                                      "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in out.iterdir()) == [
        "ana-ai-type-black.png", "ana-ai-type-yellow.png"]
    state = json.loads(card.state_path(load_config(config).data_dir).read_text("utf-8"))
    assert state["name"] == "Ana" and state["top"] == ["talk-normal"]
    assert state["shown"] == ["say-less", "talk-normal", "undo"]


def test_wrapped_without_an_archive_says_to_run_setup(tmp_path):
    result = CliRunner().invoke(app, ["wrapped", "--config", str(_config(tmp_path))])
    assert result.exit_code == 1 and "varyz setup" in result.output
