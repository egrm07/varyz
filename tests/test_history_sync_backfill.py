import json

import httpx
import pytest

from tests.test_cloud_archive import hosted as hosted
from tests.test_cloud_archive import local_entry
from varyz.cloud.archive_sync import ArchiveSync, approve, read_archive_journal
from varyz.cloud.client import CloudClient, CloudError


def test_batched_backfill_resumes_and_does_not_duplicate(conn, tmp_path, hosted):
    identities = [f"history-{i}" for i in range(30)]
    for identity in identities:
        local_entry(conn, identity)
    path = tmp_path / "archive.json"
    approve(conn, path, identities, "demo")
    server, _, auth = hosted
    token = auth()["Authorization"].split()[1]
    batches = []

    def transport(request):
        if request.url.path == "/api/archive/sync":
            batch = json.loads(request.content)["records"]
            batches.append(len(batch))
            if len(batches) == 2:
                raise httpx.ConnectError("interrupted")
        response = server.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, content=response.content)

    client = CloudClient("http://localhost", {"access_token": token},
                         http=httpx.Client(transport=httpx.MockTransport(transport)))
    worker = ArchiveSync(conn, path)
    with pytest.raises(CloudError):
        worker.sync(client, "alice")
    assert len(read_archive_journal(path)["records"]) == 25
    assert worker.sync(client, "alice")["uploaded"] == 5
    assert worker.sync(client, "alice")["uploaded"] == 0
    assert batches == [25, 5, 5]


@pytest.mark.parametrize("failures", [1, 4])
def test_rate_limit_retries_are_bounded(monkeypatch, failures):
    sleeps = []
    monkeypatch.setattr("varyz.cloud.client.time.sleep", sleeps.append)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(429 if len(calls) <= failures else 200, json={})

    client = CloudClient("http://localhost", {"access_token": "test"},
                         http=httpx.Client(transport=httpx.MockTransport(transport)))
    if failures == 4:
        with pytest.raises(CloudError, match="429"):
            client.request("POST", "/api/archive/sync", json={"records": []})
    else:
        assert client.request("POST", "/api/archive/sync", json={"records": []}) == {}
    assert len(sleeps) == min(failures, 3)
    assert len(calls) == min(failures + 1, 4)
