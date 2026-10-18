
import httpx
import pytest

from varyz.cloud.client import CloudClient, CloudError


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
