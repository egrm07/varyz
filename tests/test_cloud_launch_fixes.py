"""Regression tests for the cloud launch review: browser flows, scoped tokens,
redirect kinds, checkout recovery, MCP tool errors, question budget and deploy pins."""

import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from varyz.cloud import app as cloud_app  # noqa: E402
from varyz.cloud import store as cloud_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8787"
CHALLENGE = "c" * 43
PASSWORD = "correct horse battery"


@pytest.fixture
def cloud(tmp_path):
    settings = cloud_app.Settings(
        database=str(tmp_path / "cloud.sqlite3"),
        paddle_api_key="test-api-key",
        paddle_prices=("pri_paid", "pri_annual"),
        paddle_client_token="test_" + "a" * 24,
        email_sender=lambda message: None,
    )
    app = cloud_app.create_app(settings)
    store = app.state.store
    with store.connection() as db:
        for owner, status in (("alice", "active"), ("unpaid", "inactive")):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
                (owner, owner + "@example.test", cloud_app.password_hash(PASSWORD), status),
            )
    with TestClient(app, base_url=BASE) as client:
        yield client, store


def bearer(store, owner, client="varyz-local"):
    return {"Authorization": "Bearer " + store.credential(owner, "access", 3600, client)}


def csrf_from(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def test_account_computer_script_has_nonce_and_narrow_csp(tmp_path):
    app = cloud_app.create_app(
        cloud_app.Settings(
            database=str(tmp_path / "computer.sqlite3"),
            public_url="https://cloud.varyzai.com",
        )
    )
    store = app.state.store
    with store.connection() as db:
        for owner, status in (("alice", "active"), ("unpaid", "inactive")):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
                (owner, owner + "@example.test", "unused", status),
            )
    client = TestClient(app, base_url="https://cloud.varyzai.com")
    client.cookies.set("__Host-session", store.credential("alice", "session", 3600))
    page = client.get("/")
    nonce = re.search(r'<script nonce="([^"]+)" src="/computer-connect.js"', page.text)
    assert nonce is not None
    policy = page.headers["content-security-policy"]
    assert f"script-src 'nonce-{nonce.group(1)}'" in policy
    assert "connect-src 'self' http://127.0.0.1:1207" in policy
    assert "unsafe-inline" not in policy.split("script-src", 1)[1]
    asset = client.get("/computer-connect.js")
    assert asset.status_code == 200 and "text/javascript" in asset.headers["content-type"]
    assert "addEventListener('click'" in asset.text
    # Sliding renewal creates a host-scoped cookie; replace that session too
    # when switching synthetic users, rather than adding a domainless duplicate.
    client.cookies.clear()
    client.cookies.set("__Host-session", store.credential("unpaid", "session", 3600))
    unpaid = client.get("/")
    assert "computer-connect.js" not in unpaid.text
    assert "127.0.0.1:1207" not in unpaid.headers["content-security-policy"]


def call(client, headers, name, arguments):
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def consent(client, store, client_id, redirect):
    client.cookies.set("session", store.credential("alice", "session", 3600))
    query = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": CHALLENGE,
        "state": "s1",
    }
    return client.get("/oauth/authorize", params=query), query


def test_browser_form_posts_carry_an_accepted_origin(cloud):
    client, _ = cloud
    page = client.get("/login")
    # no-referrer made browsers send "Origin: null", which the origin guard rejects.
    assert page.headers["Referrer-Policy"] == "same-origin"
    assert "form-action 'self';" in page.headers["Content-Security-Policy"]
    form = {"csrf": csrf_from(page), "email": "alice@example.test", "password": PASSWORD}
    assert client.post("/login", data=form, headers={"Origin": "null"}).status_code == 403
    response = client.post("/login", data=form, headers={"Origin": BASE}, follow_redirects=False)
    assert response.status_code == 303
    assert response.cookies.get("session")


def test_consent_names_destination_and_allows_only_its_redirect_origin(cloud):
    client, store = cloud
    page, query = consent(client, store, "varyz-local", "http://127.0.0.1:8766/callback")
    assert page.status_code == 200
    assert "form-action 'self' http://127.0.0.1:8766;" in page.headers["Content-Security-Policy"]
    assert "Varyz on this computer" in page.text
    assert "local connection can also manage billing and delete your account" in page.text
    response = client.post(
        "/oauth/authorize",
        data={**query, "csrf": csrf_from(page)},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("http://127.0.0.1:8766/callback?code=")

    web = client.post(
        "/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
    ).json()["client_id"]
    page, _ = consent(client, store, web, "https://claude.ai/api/mcp/auth_callback")
    assert "form-action 'self' https://claude.ai;" in page.headers["Content-Security-Policy"]
    assert "<strong>claude.ai</strong>" in page.text
    assert "cannot change billing or delete your account" in page.text


def test_loopback_any_port_and_reverse_domain_app_schemes_register(cloud):
    client, store = cloud
    loopback = client.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1/cb"]})
    assert loopback.status_code == 200
    identifier = loopback.json()["client_id"]
    page, _ = consent(client, store, identifier, "http://127.0.0.1:53111/cb")
    assert page.status_code == 200 and "an app on this computer" in page.text
    assert "http://127.0.0.1:53111" in page.headers["Content-Security-Policy"]
    assert consent(client, store, identifier, "http://127.0.0.1:53111/other")[0].status_code == 400
    assert consent(client, store, identifier, "http://evil.test/cb")[0].status_code == 400

    app_scheme = client.post("/oauth/register", json={"redirect_uris": ["com.example.app:/oauth"]})
    assert app_scheme.status_code == 200
    page, _ = consent(client, store, app_scheme.json()["client_id"], "com.example.app:/oauth")
    assert "form-action 'self' com.example.app:;" in page.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    "uri",
    [
        "javascript:alert(1)",
        "myapp:/cb",  # Private-use schemes must be reverse-domain names (RFC 8252).
        "http://example.test/cb",
        "https://user@example.test/cb",
        "https://example.test/cb#fragment",
        "https://example.test:99999/cb",
    ],
)
def test_unsafe_redirects_are_rejected(cloud, uri):
    client, _ = cloud
    assert client.post("/oauth/register", json={"redirect_uris": [uri]}).status_code == 422


def test_connector_tokens_cannot_manage_billing_or_delete(cloud):
    client, store = cloud
    connector = bearer(store, "alice", "claude-connector")
    assert client.delete("/api/account", headers=connector).status_code == 403
    assert client.post("/api/billing/cancel", headers=connector).status_code == 403
    checkout = client.post(
        "/api/billing/checkout", headers=connector, json={"price_id": "pri_paid"}
    )
    assert checkout.status_code == 403
    assert client.get("/api/account", headers=connector).status_code == 200
    questions = call(client, connector, "owner_questions", {})
    assert not questions.get("isError")
    result = json.loads(questions["content"][0]["text"])
    assert result["questions"] == []
    assert result["away_mode"] == {
        "active": False,
        "scope": "task",
        "conversation_id": None,
    }
    assert client.delete("/api/account", headers=bearer(store, "alice")).status_code == 200


class Paddle:
    """Paddle API stand-in: paid recurring prices and transactions on our checkout page."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.cancel_status = 200
        self.checkout_base = BASE + "/checkout?_ptxn="
        self.counter = 0
        original = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        monkeypatch.setattr(
            cloud_app.httpx,
            "AsyncClient",
            lambda **options: original(transport=transport, **options),
        )

    def handle(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            price = {"unit_price": {"amount": "500"}, "billing_cycle": {"interval": "month"}}
            return httpx.Response(200, json={"data": price})
        if request.method == "PATCH":
            return httpx.Response(self.cancel_status, json={})
        self.counter += 1
        url = self.checkout_base + f"txn_{self.counter:012d}"
        return httpx.Response(201, json={"data": {"checkout": {"url": url}}})


def start_checkout(client, store, price="pri_paid"):
    return client.post(
        "/api/billing/checkout", headers=bearer(store, "unpaid"), json={"price_id": price}
    )


def test_stalled_checkout_retries_after_the_create_window(cloud, monkeypatch):
    client, store = cloud
    paddle = Paddle(monkeypatch)
    with store.connection() as db:
        db.execute(
            "INSERT INTO billing_checkouts VALUES(?,?,?,?)", ("unpaid", "pri_paid", "", time.time())
        )
    assert start_checkout(client, store).status_code == 409
    assert paddle.calls == []
    with store.connection() as db:
        stale = time.time() - cloud_app.CHECKOUT_CREATE_WINDOW - 1
        db.execute("UPDATE billing_checkouts SET created=? WHERE account=?", (stale, "unpaid"))
    response = start_checkout(client, store)
    assert response.status_code == 200
    assert response.json()["checkout_url"] == BASE + "/checkout?_ptxn=txn_000000000001"
    assert start_checkout(client, store).json() == response.json()  # Reused, not recreated.


def test_plan_switch_cancels_the_unpaid_checkout_first(cloud, monkeypatch):
    client, store = cloud
    paddle = Paddle(monkeypatch)
    monthly = start_checkout(client, store).json()["checkout_url"]
    annual = start_checkout(client, store, "pri_annual")
    assert annual.status_code == 200 and annual.json()["checkout_url"] != monthly
    assert ("PATCH", "/transactions/txn_000000000001", {"status": "canceled"}) in paddle.calls

    paddle.cancel_status = 400  # Already paid, or the provider refuses.
    refused = start_checkout(client, store)
    assert refused.status_code == 409 and "may already be paid" in refused.json()["detail"]
    # The paid-or-pending annual checkout stays the one on file.
    assert start_checkout(client, store, "pri_annual").json() == annual.json()


def test_checkout_url_off_our_checkout_page_is_refused(cloud, monkeypatch):
    client, store = cloud
    paddle = Paddle(monkeypatch)
    paddle.checkout_base = "https://pay.attacker.test/checkout?_ptxn="
    response = start_checkout(client, store)
    assert response.status_code == 502
    assert "unexpected checkout URL" in response.json()["detail"]


def test_dashboard_prices_every_plan_and_dates_the_subscription(cloud, monkeypatch):
    client, store = cloud
    provider = {
        "/prices/pri_paid": {
            "unit_price": {"amount": "500", "currency_code": "USD"},
            "billing_cycle": {"interval": "month", "frequency": 1},
            "tax_mode": "external",
        },
        "/prices/pri_annual": {
            "unit_price": {"amount": "4800", "currency_code": "USD"},
            "billing_cycle": {"interval": "year", "frequency": 1},
            "tax_mode": "external",
        },
        "/subscriptions/sub_alice": {
            "next_billed_at": "2026-11-07T10:00:00Z",
            "scheduled_change": {"action": "cancel", "effective_at": "2026-11-07T10:00:00Z"},
        },
    }
    monkeypatch.setattr(
        cloud_app, "paddle_read", lambda url, key, missing=None: provider.get(urlparse(url).path)
    )
    client.cookies.set("session", store.credential("unpaid", "session", 3600))
    unpaid = client.get("/?view=dashboard").text
    assert "Subscribe, $5 a month" in unpaid and "Subscribe, $48 a year" in unpaid
    assert 'value="pri_annual"' in unpaid and "added at checkout" in unpaid

    with store.connection() as db:
        db.execute("UPDATE accounts SET subscription=? WHERE id=?", ("sub_alice", "alice"))
    client.cookies.clear()
    client.cookies.set("session", store.credential("alice", "session", 3600))
    ending = client.get("/?view=dashboard").text
    assert "until 7 November 2026 and will not renew" in ending
    assert 'value="cancel_subscription"' not in ending


def test_browser_checkout_failure_is_a_page_not_json(cloud):
    client, store = cloud
    client.cookies.set("session", store.credential("unpaid", "session", 3600))
    client.get("/")
    response = client.post(
        "/account/action",
        data={"csrf": client.cookies.get("csrf"), "action": "checkout", "price_id": "pri_other"},
        headers={"Origin": BASE},
    )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "not configured for this price" in response.text
    assert 'href="/?view=dashboard"' in response.text


def test_mcp_tool_failures_are_tool_errors(cloud):
    client, store = cloud
    headers = bearer(store, "alice")
    missing = call(client, headers, "answer_owner_question", {"question_id": "x", "answer": "y"})
    assert missing["isError"] is True and "not found" in missing["content"][0]["text"]

    asked = call(client, headers, "ask", {"project": "site", "question": "Which font?"})
    question_id = json.loads(asked["content"][0]["text"])["question_id"]
    store.push(
        "alice",
        [
            {
                "kind": "answer",
                "id": question_id,
                "base_version": 0,
                "payload": {"question": "Which font?", "answer": "Inter", "scope": "site"},
            }
        ],
    )
    conflict = call(
        client, headers, "answer_owner_question", {"question_id": question_id, "answer": "Lora"}
    )
    assert conflict["isError"] is True
    assert "already answered differently" in conflict["content"][0]["text"]


def test_full_question_queue_degrades_and_answers_free_space(cloud, monkeypatch):
    client, store = cloud
    monkeypatch.setattr(cloud_store, "MAX_PENDING_QUESTIONS", 2)
    headers = bearer(store, "alice")

    def ask(question):
        result = call(client, headers, "ask", {"project": "site", "question": question})
        assert "isError" not in result
        return json.loads(result["content"][0]["text"])

    first, second = ask("First?"), ask("Second?")
    assert first["question_id"] and second["question_id"]
    full = ask("Third?")
    assert full["status"] == "ask_owner" and full["question_id"] is None
    assert "remember_owner_rule" in full["reason"]

    answered = call(
        client,
        headers,
        "answer_owner_question",
        {"question_id": first["question_id"], "answer": "Yes"},
    )
    assert "isError" not in answered
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    assert ask("Third?")["question_id"]


def test_container_pins_cloud_dependencies_and_trusts_only_the_edge():
    extra = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]["cloud"]
    requirements = (ROOT / "deploy/cloud/requirements.txt").read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^([a-z0-9][a-z0-9._-]*)==(\S+) \\$", requirements, re.M))
    names = {re.split(r"[\[<>=]", item)[0].lower().replace("_", "-") for item in extra}
    names |= {"psycopg-pool", "psycopg-binary"}  # psycopg[pool,binary]
    assert names <= set(pinned), names - set(pinned)
    blocks = [b for b in re.split(r"\n(?=[a-z0-9])", requirements) if "==" in b]
    assert all("--hash=sha256:" in block for block in blocks)
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package.get("version") for package in lock["package"]}
    assert {name: pinned[name] for name in names} == {name: locked[name] for name in names}

    dockerfile = (ROOT / "deploy/cloud/Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.txt" in dockerfile
    assert "--proxy-headers" in dockerfile and "--no-proxy-headers" not in dockerfile
    assert "FORWARDED_ALLOW_IPS=127.0.0.1" in dockerfile
    compose = (ROOT / "deploy/cloud/compose.yaml").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS: ${VARYZ_CLOUD_FORWARDED_ALLOW_IPS:-172.31.250.1}" in compose
    assert "gateway: 172.31.250.1" in compose
    ignore = (ROOT / "deploy/cloud/Dockerfile.dockerignore").read_text(encoding="utf-8")
    assert "!deploy/cloud/requirements.txt" in ignore


def test_container_pins_every_base_dependency_the_server_imports():
    # The image installs the package with --no-deps, so a base dependency the
    # app imports at startup must also be pinned, or the container crashloops
    # (python-ulid did on the first Hetzner deploy).
    probe = (
        "import sys, json, importlib.metadata as md\n"
        "before = set(sys.modules)\n"
        "import varyz.cloud.app\n"
        "dists = md.packages_distributions()\n"
        "top = {m.split('.')[0] for m in set(sys.modules) - before}\n"
        "print(json.dumps(sorted({d for m in top for d in dists.get(m, [])})))\n"
    )
    imported = json.loads(
        subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        ).stdout
    )
    base = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    base_names = {re.split(r"[\[<>=]", item)[0].lower() for item in base}
    needed = {name.lower().replace("_", "-") for name in imported} & base_names
    requirements = (ROOT / "deploy/cloud/requirements.txt").read_text(encoding="utf-8")
    pinned = set(re.findall(r"^([a-z0-9][a-z0-9._-]*)==", requirements, re.M))
    assert needed <= pinned, needed - pinned


def test_deploy_keeps_server_only_archive_key(tmp_path):
    """Rewriting cloud.env from Credential Manager must not drop the server-made archive key."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("deploy", ROOT / "deploy/cloud/deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    env = tmp_path / "cloud.env"

    def write(before):
        if before is None:
            env.unlink(missing_ok=True)
        else:
            env.write_text(before)
        script = deploy.WRITE_ENV.replace(repr(deploy.ENV_FILE), repr(env.as_posix()))
        subprocess.run([sys.executable, "-c", script], input="A=2\n", text=True, check=True)
        return env.read_text().splitlines()

    kept = "VARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY=k"
    assert write(f"A=1\n{kept}\nOLD=x\n") == ["A=2", kept]
    sharing = [
        "SHARED_VARYZ_ENABLED=true",
        "SHARED_VARYZ_ACCOUNTS=owner,guest",
        "SHARED_VARYZ_CHATGPT_PLUGIN_URL=https://fixture.test/plugin",
    ]
    assert write("\n".join([kept, *sharing]) + "\n") == ["A=2", kept, *sharing]
    assert write("SHARED_VARYZ_ENABLED=false\n") == ["A=2", "SHARED_VARYZ_ENABLED=false"]
    assert write("A=1\nVARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY=\n") == ["A=2"]
    assert write(None) == ["A=2"]


def preview_for(currency, monthly, annual, country):
    def line(price_id, interval, total):
        return {
            "price": {"id": price_id, "billing_cycle": {"interval": interval, "frequency": 1}},
            "formatted_unit_totals": {"total": total},
        }

    return {
        "currency_code": currency,
        "address": {"country_code": country},
        "details": {
            "line_items": [line("pri_paid", "month", monthly), line("pri_annual", "year", annual)]
        },
    }


def test_buyer_sees_and_pays_in_their_own_currency(tmp_path, monkeypatch):
    settings = cloud_app.Settings(
        database=str(tmp_path / "local.sqlite3"),
        paddle_api_key="test-api-key",
        paddle_prices=("pri_paid", "pri_annual"),
        paddle_client_token="test_" + "a" * 24,
        email_sender=lambda message: None,
    )
    app = cloud_app.create_app(settings)
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
            ("unpaid", "unpaid@example.test", "unused", "inactive"),
        )
    provider = {
        "/prices/pri_paid": {
            "unit_price": {"amount": "1200", "currency_code": "USD"},
            "billing_cycle": {"interval": "month", "frequency": 1},
            "tax_mode": "internal",
        },
        "/prices/pri_annual": {
            "unit_price": {"amount": "10800", "currency_code": "USD"},
            "billing_cycle": {"interval": "year", "frequency": 1},
            "tax_mode": "internal",
        },
    }
    previews = []

    def preview(url, key, body):
        previews.append(body)
        return preview_for("EUR", "€12.00", "€108.00", "DE")

    monkeypatch.setattr(
        cloud_app, "paddle_read", lambda url, key, missing=None: provider.get(urlparse(url).path)
    )
    monkeypatch.setattr(cloud_app, "paddle_write", preview)
    paddle = Paddle(monkeypatch)
    client = TestClient(app, base_url=BASE, client=("91.0.0.1", 50000))
    client.cookies.set("session", store.credential("unpaid", "session", 3600))
    page = client.get("/?view=dashboard").text
    assert "Subscribe, €12 a month" in page and "Subscribe, €108 a year" in page
    assert "Shown in your currency with tax" in page
    assert previews == [
        {
            "items": [
                {"price_id": "pri_paid", "quantity": 1},
                {"price_id": "pri_annual", "quantity": 1},
            ],
            "customer_ip_address": "91.0.0.1",
        }
    ]
    assert start_checkout(client, store).status_code == 200
    created = [body for method, path, body in paddle.calls if path == "/transactions"]
    assert created[0]["currency_code"] == "EUR"
    assert len(previews) == 1  # The checkout reused the cached preview.


def test_local_address_keeps_the_base_price_and_says_tax_is_included(cloud, monkeypatch):
    client, store = cloud
    provider = {
        "/prices/pri_paid": {
            "unit_price": {"amount": "1200", "currency_code": "USD"},
            "billing_cycle": {"interval": "month", "frequency": 1},
            "tax_mode": "internal",
        },
    }
    monkeypatch.setattr(
        cloud_app, "paddle_read", lambda url, key, missing=None: provider.get(urlparse(url).path)
    )
    monkeypatch.setattr(cloud_app, "paddle_write", lambda url, key, body: pytest.fail("no preview"))
    client.cookies.set("session", store.credential("unpaid", "session", 3600))
    page = client.get("/?view=dashboard").text
    assert "Subscribe, $12 a month" in page and "Tax included." in page


def test_public_pricing_sends_only_a_country(cloud, monkeypatch):
    client, store = cloud
    sent = []

    def preview(url, key, body):
        sent.append(body)
        return preview_for("GBP", "£9.99", "£89.99", body["address"]["country_code"])

    monkeypatch.setattr(cloud_app, "paddle_write", preview)
    answer = client.get("/api/pricing?country=gb")
    assert answer.status_code == 200
    assert answer.json() == {
        "country": "GB",
        "currency": "GBP",
        "tax_included": True,
        "monthly": "£9.99",
        "annual": "£89.99",
    }
    assert client.get("/api/pricing?country=GB").status_code == 200
    assert len(sent) == 1 and sent[0]["address"] == {"country_code": "GB"}
    assert client.get("/api/pricing?country=GBR").status_code == 400
    monkeypatch.setattr(cloud_app, "paddle_write", lambda url, key, body: None)
    assert client.get("/api/pricing?country=RU").status_code == 404


def test_whole_money_drops_only_zero_cents():
    assert cloud_app.whole_money("€12.00") == "€12"
    assert cloud_app.whole_money("10.00 CHF") == "10 CHF"
    assert cloud_app.whole_money("£9.99") == "£9.99"
    assert cloud_app.whole_money("¥1,897") == "¥1,897"
    assert cloud_app.whole_money("$1,200.00") == "$1,200"
