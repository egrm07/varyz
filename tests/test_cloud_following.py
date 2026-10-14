"""Public Varyz: publish standards, follow by username, and which creator a task uses."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from varyz.cloud import collaboration
from varyz.cloud.app import Settings, create_app
from varyz.cloud.following import mentions, route
from varyz.cloud.sharing_service import SCOPE, SharingError, identifier

BASE = "https://cloud.example.test"
QUESTION = "Should every pull request include tests?"
REVIEW_RULE = "In code review, read the whole diff before commenting on any line."


@pytest.fixture
def cloud(tmp_path):
    app = create_app(
        Settings(
            database=str(tmp_path / "following.sqlite"),
            public_url=BASE,
            shared_varyz_enabled=True,
            archive_encryption_key=Fernet.generate_key().decode(),
        )
    )
    store = app.state.store
    with store.connection() as db:
        for name, status in (("egr", "active"), ("ann", "active"), ("joemama", "inactive")):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified,username) "
                "VALUES(?,?,?,?,1,?)",
                (name, name + "@example.test", "unused", status, name),
            )
    push(store, "egr", "rule", "review", {"statement": REVIEW_RULE, "scope": "*"})
    push(store, "egr", "answer", "tests", {"question": QUESTION, "answer": "Yes, always.",
                                           "scope": "*"})
    push(store, "egr", "rule", "private", {"statement": "PRIVATE_PROJECT", "scope": "website"})
    push(store, "ann", "rule", "review", {"statement": "Approve small PRs the same day.",
                                          "scope": "*"})
    push(store, "ann", "answer", "tests", {"question": QUESTION, "answer": "Only for bugs.",
                                           "scope": "*"})
    return app, store, app.state.sharing.following


def push(store, owner, kind, record_id, payload, version=0):
    store.push(owner, [{"kind": kind, "id": record_id, "base_version": version,
                        "payload": payload}])


def publish(following, store, owner, topic="code review", ids=("review", "tests")):
    with store.connection() as db:
        selection = [
            {"kind": row["kind"], "id": row["id"], "version": row["version"]}
            for row in db.execute(
                "SELECT kind,id,version FROM records WHERE account=? ORDER BY kind,id", (owner,)
            ).fetchall()
            if row["id"] in ids
        ]
    preview = following.preview(owner, "session-" + owner, topic, selection)
    return following.publish(owner, "session-" + owner, preview["preview_id"], identifier())


def browser(app, store, name=None):
    client = TestClient(app, base_url=BASE)
    if name:
        client.cookies.set(
            "__Host-session", store.credential(name, "session", 3600), domain="cloud.example.test"
        )
    client.cookies.set("__Host-csrf", "test-browser-csrf", domain="cloud.example.test")
    return client


def bearer(store, name, **extra):
    return {"Authorization": "Bearer " + store.credential(name, "access", 3600, "native-host",
                                                          **extra)}


def test_everyday_words_reach_the_topic_a_creator_published():
    covered = ["code-review", "copywriting", "testing"]
    assert mentions("Review this pull request", covered) == ["code-review"]
    assert mentions("Look over my changes before I merge", covered) == []
    assert mentions("Review my changes before I merge", covered) == ["code-review"]
    assert mentions("Write the landing page copy", covered) == ["copywriting"]
    assert mentions("Copy the file to dist", covered) == []
    # A creator's own topic name works without any alias.
    assert mentions("Plan the pricing page", ["pricing-page"]) == ["pricing-page"]


def test_routing_named_then_picked_then_latest():
    ann = {"id": "a", "username": "ann", "topics": {"code-review", "writing"}}
    bea = {"id": "b", "username": "bea", "topics": {"code-review"}}
    creators = [bea, ann]  # Newest follow first.

    [entry], also = route("Review this pull request", creators, {})
    assert (entry["creator"], entry["why"]) == (bea, "latest")
    assert also == [{"topic": "Code review", "creators": ["@ann"]}]

    [entry], _ = route("Review this pull request", creators, {"code-review": "a"})
    assert (entry["creator"], entry["why"]) == (ann, "picked")

    [entry], _ = route("Review this PR with @bea", creators, {"code-review": "a"})
    assert (entry["creator"], entry["why"]) == (bea, "named")
    [entry], _ = route("Review this PR using bea's Varyz", creators, {"code-review": "a"})
    assert entry["creator"] is bea

    # A named creator with no matching topic lends every topic they published.
    chosen, _ = route("Plan my week the way @ann would", creators, {})
    assert {(e["topic"], e["creator"]["username"], e["why"]) for e in chosen} == {
        ("code-review", "ann", "named"),
        ("writing", "ann", "named"),
    }
    assert route("Plan my week", creators, {}) == ([], [])
    assert route("Review this pull request", [], {}) == ([], [])


def test_creator_publishes_and_anyone_can_read_the_profile(cloud):
    app, store, following = cloud
    suggested = following.suggested_topics("egr")
    assert "code-review" in [topic["slug"] for topic in suggested]
    choose = following.candidates("egr", "Code review")
    ticked = {item["id"]: item["suggested"] for item in choose["items"]}
    # Only General preferences are offered; answers are never pre-ticked.
    assert ticked == {"review": True, "tests": False}

    result = publish(following, store, "egr")
    assert result["status"] == "published" and result["published"] == 2
    assert result["post"].endswith(BASE + "/@egr")
    assert following.status("egr")["topics"] == [
        {"topic": "Code review", "slug": "code-review", "standards": 2}
    ]

    visitor = browser(app, store)
    page = visitor.get("/@egr")
    assert page.status_code == 200
    assert REVIEW_RULE in page.text and "Yes, always." in page.text
    assert "PRIVATE_PROJECT" not in page.text
    assert 'property="og:title"' in page.text and BASE + "/@egr" in page.text
    moved = visitor.get("/@EGR", follow_redirects=False)
    assert (moved.status_code, moved.headers["location"]) == (301, "/@egr")
    assert visitor.get("/@nobody_here").status_code == 404
    assert visitor.get("/@a").status_code == 404


def test_signed_out_follow_finishes_after_sign_in(cloud):
    app, store, following = cloud
    publish(following, store, "egr")
    client = browser(app, store)
    started = client.post("/@egr/follow", data={"csrf": "test-browser-csrf"},
                          follow_redirects=False)
    assert (started.status_code, started.headers["location"]) == (303, "/login")
    client.cookies.set(
        "__Host-session", store.credential("joemama", "session", 3600),
        domain="cloud.example.test",
    )
    back = client.get("/account/shared", follow_redirects=False)
    assert (back.status_code, back.headers["location"]) == (303, "/@egr?followed=1")
    assert "You now follow @egr." in client.get("/@egr?followed=1").text
    assert following.following("joemama")["following"][0]["creator"] == "@egr"
    # The continuation is used once.
    assert client.get("/account/shared", follow_redirects=False).status_code == 200


def test_follower_is_free_and_picks_whose_standards_a_topic_uses(cloud):
    app, store, following = cloud
    publish(following, store, "egr")
    publish(following, store, "ann")
    client = TestClient(app, base_url=BASE)
    headers = bearer(store, "joemama")

    def call(path, body):
        response = client.post("/api/sharing/v1/following" + path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    empty = call("/brief", {"task": "Review this pull request"})
    assert empty["standards"] == [] and empty["following"] == []
    followed = call("", {"action": "follow", "username": "@egr"})
    assert followed["status"] == "following" and followed["topics"] == ["Code review"]
    assert "Your own Varyz rules still win" in followed["next"]
    call("", {"action": "follow", "username": "ann"})

    brief = call("/brief", {"task": "Review this pull request"})
    [group] = brief["standards"]
    assert (group["creator"], group["why"]) == ("@ann", "the creator the owner followed "
                                                "most recently for the topic")
    assert brief["also_available"] == [{"topic": "Code review", "creators": ["@egr"]}]
    assert "PRIVATE_PROJECT" not in json.dumps(brief)

    call("", {"action": "pick", "username": "egr", "topic": "code review"})
    assert call("/brief", {"task": "Review this pull request"})["standards"][0]["creator"] == (
        "@egr"
    )
    named = call("/brief", {"task": "Review this pull request with @ann"})
    assert named["standards"][0]["creator"] == "@ann"
    [choice] = call("", {"action": "list"})["choices"]
    assert (choice["using"], choice["why"]) == ("@egr", "the owner picked this creator for "
                                                "the topic")
    assert choice["picked"]

    [answer] = call("/ask", {"questions": [QUESTION], "task": "Review this pull request"})[
        "results"
    ]
    assert (answer["answer"], answer["creator"]) == ("Yes, always.", "@egr")
    assert "Not the owner's own answer" in answer["label"]
    [missing] = call("/ask", {"questions": ["Which colour for the logo?"]})["results"]
    assert missing["status"] == "not_found"

    call("", {"action": "unfollow", "username": "egr"})
    assert call("/brief", {"task": "Review this pull request"})["standards"][0]["creator"] == (
        "@ann"
    )
    uses = following.status("egr")["uses_this_week"]
    assert uses == {"tasks": 1, "answers": 1}

    for body, status in (
        ({"action": "follow", "username": "joemama"}, 422),
        ({"action": "follow", "username": "nobody_here"}, 404),
        ({"action": "pick", "username": "ann", "topic": "cooking"}, 404),
    ):
        response = client.post("/api/sharing/v1/following", json=body, headers=headers)
        assert response.status_code == status


def test_publishing_needs_the_plan_and_following_does_not(cloud):
    app, store, following = cloud
    push(store, "joemama", "rule", "mine", {"statement": "Keep PRs small.", "scope": "*"})
    with pytest.raises(SharingError) as failure:
        following.preview(
            "joemama", "s", "code review", [{"kind": "rule", "id": "mine", "version": 1}]
        )
    assert (failure.value.status, failure.value.code) == (402, "plan_required")
    page = browser(app, store, "joemama").get("/account/public")
    assert page.status_code == 200 and "Following others stays free" in page.text


def test_creator_flow_in_the_browser_choose_review_publish_remove_off(cloud):
    app, store, following = cloud
    client = browser(app, store, "egr")
    home = client.get("/account/public")
    assert home.status_code == 200 and "Code review" in home.text
    choose = client.get("/account/public/topic", params={"name": "code review"})
    assert choose.status_code == 200 and REVIEW_RULE in choose.text
    with store.connection() as db:
        version = db.execute(
            "SELECT version FROM records WHERE account='egr' AND id='review'"
        ).fetchone()["version"]
    review = client.post(
        "/account/public/preview",
        data={"csrf": "test-browser-csrf", "topic": "code review",
              "selected": ["rule:review:" + str(version)]},
    )
    assert review.status_code == 200 and REVIEW_RULE in review.text
    preview_id = review.text.split('name="preview_id" value="', 1)[1].split('"', 1)[0]
    key = review.text.split('name="idempotency_key" value="', 1)[1].split('"', 1)[0]
    unconfirmed = client.post(
        "/account/public/publish",
        data={"csrf": "test-browser-csrf", "preview_id": preview_id, "idempotency_key": key},
        follow_redirects=False,
    )
    assert unconfirmed.status_code == 422
    done = client.post(
        "/account/public/publish",
        data={"csrf": "test-browser-csrf", "preview_id": preview_id, "idempotency_key": key,
              "reviewed": "yes"},
        follow_redirects=False,
    )
    assert done.headers["location"] == "/account/public?done=published"
    assert REVIEW_RULE in client.get("/@egr").text

    client.post("/account/public/remove", data={"csrf": "test-browser-csrf",
                                                "topic": "code-review"})
    assert following.status("egr")["topics"] == []
    publish(following, store, "egr")
    client.post("/account/public/off", data={"csrf": "test-browser-csrf"})
    assert not following.status("egr")["published"]


def test_choose_batches_preserve_selection_and_finish_reviews(cloud):
    class Inputs(HTMLParser):
        def __init__(self, text):
            super().__init__()
            self.inputs = []
            self.feed(text)

        def handle_starttag(self, tag, attrs):
            if tag == "input":
                self.inputs.append(dict(attrs))

        def selected(self):
            return [i["value"] for i in self.inputs if i.get("name") == "selected"
                    and (i.get("type") == "hidden" or "checked" in i)]

        def boxes(self):
            return [i for i in self.inputs if i.get("type") == "checkbox"]

    app, store, following = cloud
    for n in range(28):
        push(store, "egr", "rule", f"batch-{n:02}",
             {"statement": f"Code review standard {n}", "scope": "*"})
    client = browser(app, store, "egr")
    page = client.get("/account/public/topic?name=code-review")
    inputs = Inputs(page.text)
    assert len(inputs.boxes()) == 12
    assert len(inputs.selected()) == 29  # Suggested choices beyond the first page survive.
    assert "Code review standard 12" not in page.text
    assert "PRIVATE_PROJECT" not in page.text
    omitted = inputs.boxes()[0]["value"]
    chosen = [value for value in inputs.selected() if value != omitted]
    data = {"csrf": "test-browser-csrf", "topic": "code-review", "selected": chosen,
            "visible_count": "12", "intent": "load-more"}
    page = client.post("/account/public/preview", data=data)
    assert page.status_code == 200
    inputs = Inputs(page.text)
    assert len(inputs.boxes()) == 24 and inputs.selected() == chosen
    assert 'name="preview_id"' not in page.text  # Loading does not create a review.
    data["visible_count"] = "24"
    page = client.post("/account/public/preview", data=data)
    inputs = Inputs(page.text)
    assert len(inputs.boxes()) == 30 and inputs.selected() == chosen
    assert ">Load more</button>" not in page.text
    # A validation error keeps all loaded rows and the exact selection.
    invalid = {**data, "visible_count": "30", "selected": [], "intent": ""}
    page = client.post("/account/public/preview", data=invalid)
    assert page.status_code == 422
    assert len(Inputs(page.text).boxes()) == 30 and not Inputs(page.text).selected()
    data.pop("intent")
    page = client.post("/account/public/preview", data=data)
    assert page.status_code == 200 and "Review code review standards" in page.text
    assert 'name="preview_id"' in page.text
    assert not following.status("egr")["published"]
    assert REVIEW_RULE not in client.get("/@egr").text


def test_a_standard_leaves_the_page_when_it_stops_being_general(cloud):
    _, store, following = cloud
    publish(following, store, "egr")
    following.follow("joemama", {"action": "follow", "username": "egr"})
    push(store, "egr", "rule", "review", {"statement": REVIEW_RULE, "scope": "website"},
         version=1)
    page = following.page(None, "egr")
    shown = [item["body"] for topic in page["topics"] for item in topic["items"]]
    assert shown == [{"question": QUESTION, "answer": "Yes, always."}]
    assert REVIEW_RULE not in json.dumps(following.brief("joemama", "Review this pull request"))


def test_creator_without_an_active_plan_stops_serving_followers(cloud):
    _, store, following = cloud
    publish(following, store, "egr")
    following.follow("joemama", {"action": "follow", "username": "egr"})
    with store.connection() as db:
        db.execute("UPDATE accounts SET status='inactive' WHERE id='egr'")
    brief = following.brief("joemama", "Review this pull request")
    assert brief["standards"] == [] and brief["following"][0]["paused"] is True


def test_guest_mcp_endpoint_carries_the_follow_tools(cloud):
    app, store, following = cloud
    publish(following, store, "egr")
    client = TestClient(app, base_url=BASE)
    headers = bearer(store, "joemama", scope=SCOPE)

    def rpc(method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        response = client.post("/mcp/shared", json=body, headers=headers)
        assert response.status_code == 200
        return response.json()["result"]

    names = {tool["name"] for tool in rpc("tools/list")["tools"]}
    assert {"follow", "followed_brief", "followed_ask"} <= names
    rpc("tools/call", {"name": "follow", "arguments": {"action": "follow", "username": "egr"}})
    brief = rpc("tools/call", {"name": "followed_brief",
                               "arguments": {"task": "Review this pull request"}})
    assert brief["structuredContent"]["standards"][0]["creator"] == "@egr"
    asked = rpc("tools/call", {"name": "followed_ask", "arguments": {"questions": [QUESTION]}})
    assert asked["structuredContent"]["results"][0]["answer"] == "Yes, always."


def test_hosted_owner_ask_and_start_task_fall_back_to_followed_standards(cloud):
    app, store, following = cloud
    publish(following, store, "egr")
    following.follow("ann", {"action": "follow", "username": "egr"})
    client = TestClient(app, base_url=BASE)
    headers = bearer(store, "ann")

    def tool(name, arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        response = client.post("/mcp", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return json.loads(response.json()["result"]["content"][0]["text"])

    # Ann's own answer to the same question wins over the one she follows.
    assert tool("ask", {"question": QUESTION, "project": "site"})["answer"] == "Only for bugs."
    other = "Should a pull request have one reviewer or two?"
    push(store, "egr", "answer", "reviewers", {"question": other, "answer": "One.",
                                               "scope": "*"})
    publish(following, store, "egr", ids=("review", "tests", "reviewers"))
    followed = tool("ask", {"question": other, "project": "site"})
    assert (followed["answer"], followed["basis"], followed["creator"]) == (
        "One.", "followed", "@egr")
    permission = tool(
        "ask", {"question": other, "project": "site", "decision_kind": "permission"}
    )
    assert permission.get("basis") != "followed"
    started = tool("start_task", {"task": "Review this pull request", "project": "site"})
    assert started["followed"]["standards"][0]["creator"] == "@egr"


class FakeCloud:
    def __init__(self, following=True, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.following, self.fail = following, fail

    def __call__(self, config_path, arguments, *, timeout=15.0, path=collaboration.PATH):
        self.calls.append((path, arguments))
        if self.fail:
            raise collaboration.CloudError("Cloud is unreachable")
        if path == collaboration.FOLLOWING:
            return {"following": [{"creator": "@egr"}] if self.following else []}
        if path.endswith("/brief"):
            return {"standards": [{"creator": "@egr"}], "following": [{"creator": "@egr"}]}
        return {"results": [
            {"status": "answered", "question": q, "answer": "Yes.", "creator": "@egr"}
            if "tests" in q else {"status": "not_found", "question": q}
            for q in arguments["questions"]
        ]}


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(collaboration, "_account", lambda config_path: ("acct", "token"))
    collaboration._follows.clear()
    collaboration._briefs.clear()

    def use(fake):
        monkeypatch.setattr(collaboration, "call", fake)
        return fake

    yield use
    collaboration._follows.clear()
    collaboration._briefs.clear()


def test_local_varyz_sends_a_task_only_when_the_person_follows_someone(local):
    nobody = local(FakeCloud(following=False))
    assert collaboration.followed_brief(None, "Review this pull request") is None
    assert collaboration.followed_answers(None, [QUESTION]) == {}
    assert [path for path, _ in nobody.calls] == [collaboration.FOLLOWING]
    assert nobody.calls[0][1] == {"action": "list"}

    collaboration._follows.clear()
    fake = local(FakeCloud())
    assert collaboration.followed_brief(None, "Review this pull request")["standards"]
    assert collaboration.followed_brief(None, "Review this pull request")["standards"]
    assert [path for path, _ in fake.calls] == [
        collaboration.FOLLOWING, collaboration.FOLLOWING + "/brief"]
    questions = [QUESTION, "Which colour?"] + [f"Choice {n}?" for n in range(10)]
    found = collaboration.followed_answers(None, questions + [QUESTION])
    assert list(found) == [QUESTION]
    assert [len(body["questions"]) for path, body in fake.calls if path.endswith("/ask")] == [
        10, 2]

    # Following someone new reaches the next briefing at once.
    collaboration.follow(None, {"action": "follow", "username": "ann"})
    assert "acct" not in collaboration._follows and not collaboration._briefs


def test_local_varyz_goes_quiet_when_cloud_fails(local):
    fake = local(FakeCloud(fail=True))
    assert collaboration.followed_brief(None, "Review this pull request") is None
    assert collaboration.followed_answers(None, [QUESTION]) == {}
    assert len(fake.calls) == 1


def test_local_ask_uses_a_followed_answer_only_when_the_owner_has_none(
    tmp_path: Path, monkeypatch
) -> None:
    from varyz.db.connection import open_db
    from varyz.mcp_server import VaryzServer
    from varyz.resolution.client import ResolutionClient
    from varyz.resolution.daemon import build_resolution_daemon
    from varyz.resolution.escalation import NullNotifier
    from varyz.trust import register_agent

    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
                      encoding="utf-8")
    seen: list[list[str]] = []

    def followed_answers(config_path, questions, task=""):
        seen.append(list(questions))
        return {q: {"status": "answered", "question": q, "answer": "Yes, always.",
                    "creator": "@egr", "topic": "Code review",
                    "label": "Published standard from @egr"} for q in questions}

    monkeypatch.setattr(collaboration, "followed_answers", followed_answers)
    daemon = build_resolution_daemon(tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0,
                                     device_id="device:test", notifier=NullNotifier())
    with daemon:
        conn = open_db(tmp_path / "data" / "varyz.db")
        token = register_agent(conn, agent_id="agent:claude-code:local",
                               display_name="Claude Code", device_id="local").token
        conn.close()
        server = VaryzServer(config, agent_token=token,
                             resolution_client=ResolutionClient(*daemon.address))
        try:
            result = server.call("ask", {"question": QUESTION})
            assert result["followed_answer"]["answer"] == "Yes, always."
            assert result["followed_answer"]["basis"] == "followed"
            assert "never record it as an owner rule" in result["followed_answer"]["how"]
            assert "ask_owner" not in result and "deferred_owner" not in result
            assert server.call("owner_questions", {})["pending"] == 0

            permission = server.call(
                "ask", {"question": "May I push to main?", "decision_kind": "permission"}
            )
            assert "followed_answer" not in permission and "ask_owner" in permission
            assert seen == [[QUESTION]]

            batch = server.call("ask", {"questions": [
                {"question": QUESTION}, {"question": "Which font?", "decision_kind": "blocking"},
            ]})
            assert batch["results"][0]["followed_answer"]["creator"] == "@egr"
            # Only the question nobody answered goes to the owner.
            asked = batch["ask_owner_batch"]["questions"]
            assert [q["question"] for q in asked] == ["Which font?"]
            assert seen == [[QUESTION], [QUESTION]]
        finally:
            server.close()
    daemon.stop()
