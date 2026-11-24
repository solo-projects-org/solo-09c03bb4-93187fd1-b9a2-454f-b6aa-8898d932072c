"""Tests for the snapshot-derived usage index and discrepancy audit."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import usage
from app.db import Database
from app.config import load_settings

RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]
WINDOW = BEFORE + AFTER


def _payload(values: dict) -> dict:
    return {
        "readings": [
            {
                "date": d.isoformat() if isinstance(d, date) else d,
                "kwh_per_lamp": v,
            }
            for d, v in sorted(
                values.items(),
                key=lambda item: item[0].isoformat()
                if isinstance(item[0], date)
                else item[0],
            )
        ]
    }


def _flat(days, value: float) -> dict[date, float]:
    return {d: value for d in days}


def _seed_two_reports_with_a_disagreement(client):
    """Two reports that cite the same segment/day with different values.

    R1 uses control C1 value 9.7 on 2026-03-13; a correction changes the live
    reading, then R2 is issued citing the new value. R1's snapshot keeps the
    old one, so the usage index must record both for that day.
    """

    ids = {}
    for name, retrofit in [("T", RETROFIT), ("C1", None), ("C2", None)]:
        resp = client.post(
            "/api/segments",
            json={"name": name, "retrofit_date": retrofit.isoformat() if retrofit else None},
        )
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]

    client.put(
        f"/api/segments/{ids['T']}/readings",
        json=_payload({**_flat(BEFORE, 10.0), **_flat(AFTER, 8.0)}),
    )
    client.put(
        f"/api/segments/{ids['C1']}/readings",
        json=_payload({**_flat(BEFORE, 10.2), **_flat(AFTER, 9.7)}),
    )
    client.put(
        f"/api/segments/{ids['C2']}/readings",
        json=_payload({**_flat(BEFORE, 10.4), **_flat(AFTER, 9.9)}),
    )
    r1 = client.post("/api/reports", params={"segment_id": ids["T"]}).json()["report"]
    client.put(
        f"/api/segments/{ids['C1']}/readings",
        json=_payload({date(2026, 3, 13): 9.0}),
    )
    r2 = client.post("/api/reports", params={"segment_id": ids["T"]}).json()["report"]
    return ids, r1["id"], r2["id"]


def test_new_reports_are_indexed_immediately_and_query_finds_disagreement(client):
    ids, r1, r2 = _seed_two_reports_with_a_disagreement(client)
    status = client.get("/api/usage-index/backfill").json()
    # Nothing to backfill: both reports were indexed at issuance time.
    assert status["status"] == "not_started"
    assert status["indexed_reports"] == 2
    assert status["remaining_reports"] == 0

    resp = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-03", "end": "2026-03-17"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backfill_status"] == "not_started"
    assert body["pending_check_dates"] == []
    by_day = {d["date"]: d for d in body["days"]}
    # Skipped retrofit day was never cited.
    assert "2026-03-10" not in by_day or by_day["2026-03-10"]["state"] == "no_data"

    bad = by_day["2026-03-13"]
    assert bad["state"] == "discrepancy"
    values = {group["kwh_per_lamp"]: group["reports"] for group in bad["values"]}
    assert set(values) == {9.7, 9.0}
    assert values[9.7] == [{"report_id": r1, "role": "control", "status": "pending_recalc"}]
    assert values[9.0] == [{"report_id": r2, "role": "control", "status": "issued"}]

    # Every other cited day is consistent (same value across the two reports).
    for day in WINDOW:
        if day == date(2026, 3, 13):
            continue
        assert by_day[day.isoformat()]["state"] == "consistent"


def test_pending_recalc_reports_stay_in_the_index(client):
    ids, r1, r2 = _seed_two_reports_with_a_disagreement(client)
    detail = client.get(f"/api/reports/{r1}").json()
    assert detail["status"] == "pending_recalc"
    resp = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-13", "end": "2026-03-13"},
    )
    states = {g["reports"][0]["status"] for g in resp.json()["days"][0]["values"]}
    assert states == {"pending_recalc", "issued"}


def test_backfill_batches_index_old_reports_and_completes(client):
    ids, _, _ = _seed_two_reports_with_a_disagreement(client)
    db = Database(load_settings().db_path)

    # Simulate reports that predate the index: strip their index rows and any
    # backfill state, without touching the reports themselves.
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")

    status = client.get("/api/usage-index/backfill").json()
    assert status["status"] == "not_started"
    assert status["remaining_reports"] == 2

    first = client.post(
        "/api/usage-index/backfill/batches",
        params={"max_batches": 1, "batch_size": 1},
    ).json()
    assert first["status"] == "running"
    assert first["batches_run"] == 1
    assert first["last_batch_size"] == 1
    assert first["indexed_reports"] == 1
    assert first["remaining_reports"] == 1

    second = client.post(
        "/api/usage-index/backfill/batches",
        params={"max_batches": 1, "batch_size": 1},
    ).json()
    assert second["status"] == "completed"
    assert second["indexed_reports"] == 2
    assert second["remaining_reports"] == 0

    third = client.post(
        "/api/usage-index/backfill/batches", params={"max_batches": 1}
    ).json()
    assert third["status"] == "completed"
    assert third["batches_run"] == 0

    resp = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-13", "end": "2026-03-13"},
    )
    assert resp.json()["days"][0]["state"] == "discrepancy"


def test_failed_batch_does_not_advance_progress_and_records_error(client):
    _seed_two_reports_with_a_disagreement(client)
    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")

    calls: list[list[int]] = []

    def boom(conn, report_ids):
        calls.append(list(report_ids))
        raise RuntimeError("simulated snapshot failure")

    with pytest.raises(RuntimeError, match="simulated"):
        usage.run_backfill_batches(db, max_batches=1, on_batch_attempt=boom)

    with db.read() as conn:
        state = conn.execute(
            "SELECT * FROM usage_index_backfill_state WHERE id = 1"
        ).fetchone()
        indexed = conn.execute(
            "SELECT COUNT(*) AS n FROM report_reading_usage"
        ).fetchone()["n"]
    assert state["status"] == "running"
    assert state["last_processed_report_id"] == 0
    assert "simulated snapshot failure" in state["last_error"]
    assert indexed == 0  # whole batch rolled back

    # Retry without the failing hook resumes from exactly the same batch.
    result = usage.run_backfill_batches(db, max_batches=1, batch_size=1)
    assert result["status"] == "running"
    assert result["last_processed_report_id"] == calls[0][0]
    assert result["last_error"] is None


def test_backfill_resumes_after_restart(client):
    _seed_two_reports_with_a_disagreement(client)
    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")

    usage.run_backfill_batches(db, max_batches=1, batch_size=1)
    # A brand-new Database instance models a process restart; state is durable.
    restarted = Database(load_settings().db_path)
    result = usage.run_backfill_batches(restarted, max_batches=10, batch_size=1)
    assert result["status"] == "completed"
    assert result["indexed_reports"] == 2


def test_query_during_backfill_marks_only_covered_days_pending(client):
    ids, r1, r2 = _seed_two_reports_with_a_disagreement(client)
    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")

    # Index only r1 (the lower id): r2 remains an unindexed old report.
    usage.run_backfill_batches(db, max_batches=1, batch_size=1)

    resp = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-01", "end": "2026-03-20"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backfill_status"] == "running"
    pending = {d["date"]: d for d in body["pending_check_dates"]}
    # r2's before/after window days are pending; skipped day and outside days
    # are not.
    expected_pending = {d.isoformat() for d in WINDOW}
    assert set(pending) == expected_pending
    assert pending["2026-03-13"]["unbackfilled_report_ids"] == [r2]
    # No conclusion is produced for a pending day.
    audited = {d["date"]: d for d in body["days"]}
    for iso in expected_pending:
        assert iso not in audited
    # Days outside any unbackfilled window are still audited normally.
    assert audited["2026-03-01"]["state"] == "no_data"
    assert audited["2026-03-18"]["state"] == "no_data"
    assert "2026-03-10" in audited  # skipped day is not part of any window


def test_query_uses_one_committed_snapshot(client, monkeypatch):
    ids, r1, r2 = _seed_two_reports_with_a_disagreement(client)
    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")

    # Index r1, leave r2 unindexed.
    usage.run_backfill_batches(db, max_batches=1, batch_size=1)

    seen: list[object] = []
    real_connect = Database.connect

    def spy_connect(self):
        conn = real_connect(self)
        if len(seen) == 0:
            seen.append(conn)
        return conn

    # The audit opens exactly one read connection/transaction; every decision
    # (pending flags + discrepancies) therefore comes from the same snapshot.
    monkeypatch.setattr(Database, "connect", spy_connect)
    body = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-03", "end": "2026-03-17"},
    ).json()
    assert len(seen) == 1
    assert {d["date"] for d in body["pending_check_dates"]} == {
        d.isoformat() for d in WINDOW
    }


def test_new_report_between_batches_is_audited_immediately(client):
    ids, r1, r2 = _seed_two_reports_with_a_disagreement(client)
    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")

    usage.run_backfill_batches(db, max_batches=1, batch_size=1)

    # Issue a third report while backfill is incomplete. Issuance indexes it.
    r3 = client.post("/api/reports", params={"segment_id": ids["T"]}).json()["report"]["id"]
    with db.read() as conn:
        indexed = conn.execute(
            "SELECT COUNT(DISTINCT report_id) AS n FROM report_reading_usage"
        ).fetchone()["n"]
        unindexed_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT r.id FROM reports r WHERE NOT EXISTS ("
                "SELECT 1 FROM report_reading_usage u WHERE u.report_id = r.id)"
            ).fetchall()
        ]
    assert indexed == 2  # r1 from backfill + r3 from issuance
    assert unindexed_ids == [r2]

    # The next batch must not re-read r3's snapshot and finishes on r2.
    result = usage.run_backfill_batches(db, max_batches=5, batch_size=1)
    assert result["status"] == "completed"
    assert result["indexed_reports"] == 3


def test_range_validation(client):
    ids, _, _ = _seed_two_reports_with_a_disagreement(client)
    bad_range = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-01", "end": "2026-04-01"},
    )
    assert bad_range.status_code == 422
    reversed_range = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-10", "end": "2026-03-01"},
    )
    assert reversed_range.status_code == 422
    missing = client.get(
        f"/api/segments/9999/usage-discrepancies",
        params={"start": "2026-03-01", "end": "2026-03-02"},
    )
    assert missing.status_code == 404
    # Exactly 31 days is allowed.
    ok = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-01", "end": "2026-03-31"},
    )
    assert ok.status_code == 200


def test_batch_size_is_capped_at_100(client):
    too_big = client.post(
        "/api/usage-index/backfill/batches", params={"batch_size": 101}
    )
    assert too_big.status_code == 422
    bad_batches = client.post(
        "/api/usage-index/backfill/batches", params={"max_batches": 0}
    )
    assert bad_batches.status_code == 422


def test_audit_never_reads_live_readings_table(client, monkeypatch):
    ids, _, _ = _seed_two_reports_with_a_disagreement(client)

    original_connect = Database.connect

    class GuardedConnection:
        """Connection wrapper that rejects any access to the readings table."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            compact = " ".join(sql.split())
            lowered = compact.lower()
            if (
                " from readings " in lowered
                or " into readings " in lowered
                or "update readings " in lowered
                or "delete from readings" in lowered
            ):
                raise AssertionError(f"audit query touched readings table: {sql}")
            return self._inner.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._inner, item)

    def guarded_connect(self):
        return GuardedConnection(original_connect(self))

    monkeypatch.setattr(Database, "connect", guarded_connect)

    resp = client.get(
        f"/api/segments/{ids['C1']}/usage-discrepancies",
        params={"start": "2026-03-03", "end": "2026-03-17"},
    )
    assert resp.status_code == 200
    assert resp.json()["days"]  # still answered from the index


def test_backfill_does_not_modify_old_reports(client):
    ids, r1, r2 = _seed_two_reports_with_a_disagreement(client)
    before = client.get(f"/api/reports/{r1}").json()
    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute("DELETE FROM report_reading_usage")
        conn.execute("DELETE FROM usage_index_backfill_state")
    usage.run_backfill_batches(db, max_batches=10)
    after = client.get(f"/api/reports/{r1}").json()
    assert after == before
    assert after["id"] == r1
