"""Offline container gate: synthetic account, indexed bounded pages and CSRF forms."""

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app

with TemporaryDirectory() as folder:
    app = create_app(Settings(database=str(Path(folder) / "cloud.sqlite")))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES('probe','probe@example.test','unused','active',1)"
        )
    store.push(
        "probe",
        [
            dict(
                kind="rule",
                id=f"r-{i}",
                base_version=0,
                payload=dict(statement=f"Synthetic preference {i}", scope="demo"),
            )
            for i in range(45)
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        client.cookies.set("session", store.credential("probe", "session", 3600))
        response = client.get("/?view=dashboard")
        assert response.status_code == 200 and "45 preferences" in response.text
        assert "Synthetic preference" not in response.text
        assert 'src="/dashboard.js"' in response.text
        token = client.cookies.get("csrf")
        headers = {"Accept": "application/json"}
        page = client.get("/account/preferences?project=demo", headers=headers).json()
        assert page["html"].count('class="cloud-statement"') == 20
        assert page["next_after"] is not None
        assert 'name="csrf" value="' + token + '"' in page["html"]
        assert (
            client.get("/account/preferences?project=other", headers=headers).json()["html"] == ""
        )
        assert client.get("/dashboard.js").status_code == 200
        client.get("/?view=dashboard&section=account")
        assert client.cookies.get("csrf") == token
        client.cookies.clear()
        assert client.get("/account/preferences", headers=headers).status_code == 401
print(
    "Cloud candidate: initial summaries, bounded lazy pages, isolation, scripts and CSRF verified"
)
