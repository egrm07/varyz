"""Lazy loading bounds, tenant isolation and stable page boundaries in the real app."""

from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app
from varyz.cloud.dashboard import PAGE_SIZE, preference_page, project_summaries


def seed(store, owner, scope, start, count):
    return store.push(
        owner,
        [
            dict(
                kind="rule",
                id=f"r-{i}",
                base_version=0,
                payload=dict(statement=f"Private preference text {i}", scope=scope),
            )
            for i in range(start, start + count)
        ],
    )


def test_dashboard_reads_summaries_then_only_requested_page(tmp_path, monkeypatch):
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite")))
    store = app.state.store
    with store.connection() as db:
        for name in ("owner", "other"):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) "
                "VALUES(?,?,?,'active',1)",
                (name, name + "@example.test", "unused"),
            )
    seed(store, "owner", "alpha", 0, 55)
    seed(store, "owner", "beta", 55, 2)
    seed(store, "other", "alpha", 900, 1)
    parsed = []
    original = store.record

    def record(row):
        parsed.append(row["id"])
        return original(row)

    monkeypatch.setattr(store, "record", record)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        client.cookies.set("session", store.credential("owner", "session", 3600))
        home = client.get("/?view=dashboard")
        assert home.status_code == 200
        assert (
            '<span class="cloud-tile-number">55</span>'
            '<span class="cloud-group-count">preferences' in home.text
        )
        assert "Private preference text" not in home.text and parsed == []
        assert 'src="/dashboard.js"' in home.text
        headers = {"Accept": "application/json"}
        first = client.get("/account/preferences?project=alpha", headers=headers).json()
        assert len(parsed) == PAGE_SIZE and first["html"].count('class="cloud-statement"') == 20
        assert "900" not in first["html"] and "Private preference text 55" not in first["html"]
        seed(store, "owner", "alpha", 100, 1)
        ids = set(parsed)
        after = first["next_after"]
        while after is not None:
            parsed.clear()
            page = client.get(
                "/account/preferences",
                headers=headers,
                params=dict(project="alpha", after=after, snapshot=first["snapshot"]),
            ).json()
            assert len(parsed) <= PAGE_SIZE and not ids.intersection(parsed)
            ids.update(parsed)
            after = page["next_after"]
        assert len(ids) == 55 and "r-100" not in ids
        fresh = preference_page(store, "owner", "alpha", after=first["snapshot"])
        assert [r["id"] for r in fresh["records"]] == ["r-100"]
        assert client.get("/account/preferences?project=alpha&after=-1").status_code == 400
        assert client.get("/account/preferences?after=9223372036854775808").status_code == 400
        assert (
            client.get("/account/preferences?project=missing", headers=headers).json()["html"] == ""
        )
        # HTML fallback is bounded and usable without JavaScript.
        assert "Next preferences" in client.get("/account/preferences?project=alpha").text
        token = client.cookies.get("csrf")
        client.get("/?view=dashboard&section=account")
        assert client.cookies.get("csrf") == token
        client.cookies.clear()
        assert client.get("/account/preferences", headers=headers).status_code == 401


def test_legacy_scopes_group_canonically_and_indexed_queries(tmp_path):
    from varyz.cloud.store import Store

    store = Store(tmp_path / "store.sqlite")
    seed(store, "owner", "all_projects", 0, 1)
    seed(store, "owner", "*", 1, 1)
    seed(store, "owner", "SAN7.14", 2, 1)
    assert [(g["scope"], g["count"]) for g in project_summaries(store, "owner")] == [
        ("*", 2),
        ("san7", 1),
    ]
    assert len(preference_page(store, "owner", "General")["records"]) == 2
    with store.connection() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM records WHERE account=? "
            "AND kind IN ('rule','answer','workflow') AND payload!='null' "
            "AND COALESCE(json_extract(payload,'$.scope'),'*')=? "
            "AND cursor>? ORDER BY cursor LIMIT 21",
            ("owner", "*", 0),
        ).fetchall()
    assert any("records_dashboard_scope" in row["detail"] for row in plan)


def test_project_delete_requires_csrf_confirmation_and_isolates_scope_and_owner(tmp_path):
    from cryptography.fernet import Fernet

    app = create_app(Settings(database=str(tmp_path / "delete.sqlite"),
                              archive_encryption_key=Fernet.generate_key().decode()))
    store = app.state.store
    with store.connection() as db:
        for owner in ("owner", "other"):
            db.execute("INSERT INTO accounts(id,email,password,status,email_verified) "
                       "VALUES(?,?,?,'active',1)", (owner, owner + "@example.test", "unused"))
    seed(store, "owner", "SAN7.14", 0, 1)
    seed(store, "owner", "san7", 1, 1)
    seed(store, "owner", "beta", 2, 1)
    seed(store, "owner", "*", 3, 1)
    seed(store, "other", "san7", 9, 1)
    app.state.session_reports.archive.permit("owner", "san7")
    app.state.session_reports.permit("owner", "san7")
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        client.cookies.set("session", store.credential("owner", "session", 3600))
        home = client.get("/?view=dashboard")
        assert 'class="cloud-project-menu-trigger"' in home.text
        assert 'class="cloud-project-delete"' in home.text
        data = dict(action="delete_project", project="san7", confirm_action="1")
        assert client.post("/account/action", data=data).status_code == 403
        data["csrf"] = client.cookies.get("csrf")
        assert client.post(
            "/account/action", data={**data, "confirm_action": "0"}
        ).status_code == 422
        assert len(preference_page(store, "owner", "san7")["records"]) == 2
        response = client.post("/account/action", data=data, follow_redirects=False)
        assert response.status_code == 303
        assert preference_page(store, "owner", "san7")["records"] == []
        assert len(preference_page(store, "owner", "beta")["records"]) == 1
        assert len(preference_page(store, "owner", "*")["records"]) == 1
        assert len(preference_page(store, "other", "san7")["records"]) == 1
        assert all(
            p["revoked"] for p in app.state.session_reports.archive.status("owner")["projects"]
        )
        assert all(p["revoked"] for p in app.state.session_reports.status("owner")["projects"])
        assert client.post("/account/action", data=data, follow_redirects=False).status_code == 303
        # Deletions are propagated using the existing versioned synchronization protocol.
        changes = store.pull("owner", 0, 20)["records"]
        assert sum(r["payload"] is None for r in changes) == 2
