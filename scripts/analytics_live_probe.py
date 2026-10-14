"""Verify live receipt with synthetic probes, excluded from usage totals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import keyring

from varyz import analytics
from varyz.config import Config


class ProbeTransport(httpx.BaseTransport):
    """Run the real sender while marking this synthetic archive's batch as a probe."""

    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport()
        self.last_payload: dict[str, Any] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.last_payload = json.loads(request.content) | {"sample_kind": "probe"}
        outgoing = httpx.Request(
            request.method, request.url, json=self.last_payload, extensions=request.extensions
        )
        return self.inner.handle_request(outgoing)

    def close(self) -> None:
        self.inner.close()


def main() -> int:
    token = keyring.get_password("varyz-cloud", "VARYZ_ANALYTICS_ADMIN_TOKEN")
    if not token:
        print("Owner analytics credential is missing; no requests sent.")
        return 1
    origin = analytics.ORIGIN
    authorization = {"Authorization": "Bearer " + token}
    checks = []
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        assert client.get(origin + "/health").status_code == 200
        checks.append("https_health")
        assert client.get(origin + "/api/analytics/summary").status_code == 401
        checks.append("unauthenticated_summary_refused")
        baseline = client.get(origin + "/api/analytics/summary", headers=authorization)
        baseline.raise_for_status()
        before = baseline.json()
        checks.append("owner_credentials_work")

        cfg = Config(base_dir=Path(__file__).resolve().parents[1] / ".tmp/analytics-live-probe")
        analytics.consent(cfg, False)
        analytics.consent(cfg, True)
        analytics.record(cfg, "setup_ready")
        transport = ProbeTransport()
        assert analytics.sync(cfg, force=True, transport=transport)["sent"] == 1
        assert analytics.doctor(cfg)["delivery_verified"]
        checks.append("real_sender_receipt_verified")

        report = transport.last_payload
        bad = client.post(origin + "/api/analytics", json=report | {"message": "synthetic-probe"})
        assert bad.status_code == 422 and "synthetic-probe" not in bad.text
        checks.append("unlisted_text_refused_without_echo")
        duplicate = client.post(origin + "/api/analytics", json=report)
        assert duplicate.json() == {"accepted": report["report_id"]}
        checks.append("retry_idempotence")
        conflict = client.post(
            origin + "/api/analytics", json=report | {"counters": {"setup_ready": 2}}
        )
        assert conflict.status_code == 409
        checks.append("changed_retry_refused")
        after_response = client.get(origin + "/api/analytics/summary", headers=authorization)
        after_response.raise_for_status()
        after = after_response.json()
        assert after["probes"] == before["probes"] + 1
        assert after["counters"] == before["counters"] and after["reports"] == before["reports"]
        checks.append("probe_excluded_from_usage_totals")
        analytics.consent(cfg, False)
        assert analytics.sync(cfg, force=True)["status"] == "off"
        checks.append("synthetic_consent_revoked")
    result = {
        "checks_passed": len(checks),
        "checks": checks,
        "usage_reports": after["reports"],
        "probe_reports": after["probes"],
        "origin": origin,
    }
    path = Path(__file__).resolve().parents[1] / "reports/ANALYTICS_LIVE_2026-10-04.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
