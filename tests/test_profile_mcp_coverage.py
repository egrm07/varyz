"""Incomplete merge submissions cannot appear complete or queue a partial summary."""

from tests.test_profile_reader import _agent_server, _entry, _item, _merged
from tests.test_profile_reader import conn as conn
from tests.test_profile_reader import daemon_client as daemon_client


def test_agent_keeps_an_explicit_requirement_at_its_project_scope(tmp_path, conn, daemon_client):
    text = "The shop repair queue must remain usable offline, including entering new jobs."
    _entry(conn, "offline", text)
    server = _agent_server(tmp_path, daemon_client)
    try:
        task = server.call("profile_reading_task", {})
        item = _item(
            "rule", "Keep the repair queue usable offline, including entering new jobs.", (1, text)
        )
        item["durability"] = "project_requirement"
        result = server.call(
            "submit_profile_reading", {"task_id": task["task_id"], "items": [item]}
        )
        assert result["noted"] == 1
        task = server.call("profile_reading_task", {})
        result = server.call(
            "submit_profile_reading",
            {
                "task_id": task["task_id"],
                "items": [
                    _merged(
                        "rule",
                        "Keep the repair queue usable offline, including entering new jobs.",
                        1,
                    ),
                ],
            },
        )
        assert result["queued"] == 1
    finally:
        server.close()


def test_agent_merge_retry_is_actionable_and_unfinished_notes_stay_pending(
    tmp_path,
    conn,
    daemon_client,
):
    _entry(conn, "preview", "Always show a working preview before finishing a website.", day=1)
    _entry(conn, "dialog", "Make the signup dialog wider.", day=2)
    server = _agent_server(tmp_path, daemon_client)
    try:
        read = server.call("profile_reading_task", {})
        noted = server.call(
            "submit_profile_reading",
            {
                "task_id": read["task_id"],
                "items": [
                    _item(
                        "rule",
                        "Check a working website preview before reporting completion.",
                        (1, "Always show a working preview before finishing a website."),
                    ),
                    _item(
                        "rule",
                        "Use a wider signup dialog for the current preview.",
                        (2, "Make the signup dialog wider."),
                    ),
                ],
            },
        )
        assert noted["noted"] == 2
        merge = server.call("profile_reading_task", {})
        one = _merged("rule", "Check a working website preview before reporting completion.", 1)
        partial = server.call(
            "submit_profile_reading",
            {
                "task_id": merge["task_id"],
                "items": [one],
            },
        )
        assert partial["queued"] == 0 and partial["partial"] is True
        assert "profile_reading_task" in partial["error"]
        retry = server.call("profile_reading_task", {})
        assert retry["task_id"] == merge["task_id"]
        rejected = server.call(
            "submit_profile_reading",
            {
                "task_id": retry["task_id"],
                "items": [one],
            },
        )
        assert rejected["queued"] == 0 and rejected["partial"] is True
        assert server.call("profile_reading_task", {})["task_id"] == merge["task_id"]
        completed = server.call(
            "submit_profile_reading",
            {
                "task_id": merge["task_id"],
                "items": [
                    one,
                    {
                        "notes": [2],
                        "exclude": "One-off dialog sizing edit; no lasting preference.",
                    },
                ],
            },
        )
        # The workspace also forwards cross-project taste to a final merge.
        # The release keeps this explicitly project-scoped observation directly.
        if not completed["queued"]:
            final = server.call("profile_reading_task", {})
            completed = server.call(
                "submit_profile_reading",
                {
                    "task_id": final["task_id"],
                    "items": [one],
                },
            )
        assert completed["queued"] == 1
        assert server.call("profile_reading_task", {}) == {"done": True}
    finally:
        server.close()
