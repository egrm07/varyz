"""The bounded limiter must not lock existing visitors out at a minute rollover."""

import pytest

from varyz.cloud.store import Store


@pytest.mark.parametrize("existing", ["global", "existing-peer:general"])
def test_full_rate_table_resets_existing_counter_without_admitting_new_keys(
    tmp_path, monkeypatch, existing
):
    now = 10000
    current = now // 60 * 60
    monkeypatch.setattr("varyz.cloud.store.time.time", lambda: now)
    store = Store(tmp_path / "synthetic.db")
    with store.connection() as db:
        db.executemany(
            "INSERT INTO rate_limits VALUES(?,?,?)",
            [(existing, current - 60, 2)]
            + [(f"synthetic-{i}", current - 60, 1) for i in range(9999)],
        )

    assert not store.allow_request("new-peer", 60, 2)
    assert store.allow_request(existing, 60, 2)
    assert store.allow_request(existing, 60, 2)
    assert not store.allow_request(existing, 60, 2)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] == 10000
        row = db.execute("SELECT * FROM rate_limits WHERE key=?", (existing,)).fetchone()
        assert (row["window"], row["count"]) == (current, 2)
