"""Local pickup tools use the same Cloud leases, never an independent local claim."""
from pathlib import Path

from varyz.config import find_config_file
from varyz.scopes import normalize_scope


def call(config: Path | None, action: str, arguments: dict, *, timeout: float | None = None):
    from varyz.cloud.client import CloudClient, credential_name, load_tokens
    from varyz.cloud.session_report_sync import journal_path, read_journal

    resolved = find_config_file(config)
    if resolved is None:
        return {"error": "Cloud report syncing is not configured"}
    journal = read_journal(journal_path(resolved))
    project = arguments["project"].casefold()
    if not journal["enabled"] or project not in journal["projects"]:
        return {"error": "This project is not approved for Cloud reports"}
    from varyz.cloud.project_policy import exclusions

    if normalize_scope(project) in exclusions(resolved):
        return {"error": "This project is excluded from Cloud syncing"}
    name = credential_name(resolved, journal["url"])
    client = CloudClient(journal["url"], load_tokens(name), credential=name)
    try:
        extra = {} if timeout is None else {"timeout": timeout}
        return client.request("POST", "/api/reports/" + action, json=arguments, **extra)
    finally:
        client.close()
