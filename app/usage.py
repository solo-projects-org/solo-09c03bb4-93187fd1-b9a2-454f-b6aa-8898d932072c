"""Usage index backfill and per-segment discrepancy audit.

Design notes
------------
* ``report_reading_usage`` is the only table the audit query reads.  Each row
  is one ``(report, segment, day, kWh, role)`` citation extracted from an
  immutable report snapshot.  The current ``readings`` table is never read, so
  corrections cannot change what a report said.
* New reports are indexed synchronously inside the report-issuance write
  transaction, so they participate in audits immediately.
* Reports that predate the index are backfilled in batches of at most
  ``BACKFILL_BATCH_SIZE`` snapshots.  Every batch is one ``BEGIN IMMEDIATE``
  transaction: if anything fails the whole batch rolls back and the
  high-water mark does not move, so progress is only advanced by fully
  indexed batches and a restart simply re-runs the same batch.
* While backfill is incomplete, any queried day covered by the before/after
  window of an unindexed report is returned as ``pending_check`` without a
  conclusion.  All other days are audited normally.  Each query runs in a
  single read transaction, so its pending flags and discrepancies come from
  the same committed state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Callable

from .db import Database, utcnow_iso
from .models import (
    BackfillBatchOut,
    PendingCheckDay,
    UsageDiscrepancyDay,
    UsageDiscrepancyOut,
    UsageReportRef,
    UsageValueGroup,
)

BACKFILL_BATCH_SIZE = 100
MAX_QUERY_DAYS = 31

BatchHook = Callable[[sqlite3.Connection, list[int]], None]


class UsageAuditError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Indexing one report from its immutable snapshot
# ---------------------------------------------------------------------------


def index_report(conn: sqlite3.Connection, report_id: int) -> int:
    """Extract every cited reading from one report snapshot into the index.

    Only the snapshot stored in ``reports`` is read; the live readings table
    is never touched.  Returns the number of rows inserted for the report.
    ``INSERT OR IGNORE`` makes re-indexing idempotent so backfill resumes are
    safe even if a report was partially processed before a crash boundary.
    """

    row = conn.execute(
        "SELECT target_segment_id, snapshot_json FROM reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        raise UsageAuditError(404, f"report {report_id} not found")

    snapshot = json.loads(row["snapshot_json"])
    entries: list[tuple[int, int, str, float, str]] = []
    target = snapshot["target"]
    for reading_date, value in target["readings"].items():
        entries.append((report_id, target["id"], reading_date, value, "target"))
    for control in snapshot.get("controls", []):
        for reading_date, value in control["readings"].items():
            entries.append((report_id, control["id"], reading_date, value, "control"))

    conn.executemany(
        "INSERT OR IGNORE INTO report_reading_usage "
        "(report_id, segment_id, reading_date, kwh_per_lamp, role) "
        "VALUES (?, ?, ?, ?, ?)",
        entries,
    )
    return len(entries)


# ---------------------------------------------------------------------------
# Backfill state machine
# ---------------------------------------------------------------------------


def _get_state_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM usage_index_backfill_state WHERE id = 1"
    ).fetchone()


def _ensure_state_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = _get_state_row(conn)
    if row is None:
        conn.execute(
            "INSERT INTO usage_index_backfill_state "
            "(id, status, last_processed_report_id, last_batch_size, "
            "last_error, updated_at) VALUES (1, 'running', 0, 0, NULL, ?)",
            (utcnow_iso(),),
        )
        row = _get_state_row(conn)
    return row


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    total = conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
    indexed = conn.execute(
        "SELECT COUNT(DISTINCT report_id) AS n FROM report_reading_usage"
    ).fetchone()["n"]
    return total, indexed


def _status_dict(row: sqlite3.Row | None, conn: sqlite3.Connection) -> dict:
    total, indexed = _counts(conn)
    if row is None:
        return {
            "status": "not_started",
            "last_processed_report_id": 0,
            "last_batch_size": 0,
            "total_reports": total,
            "indexed_reports": indexed,
            "remaining_reports": total - indexed,
            "last_error": None,
            "updated_at": None,
        }
    return {
        "status": row["status"],
        "last_processed_report_id": row["last_processed_report_id"],
        "last_batch_size": row["last_batch_size"],
        "total_reports": total,
        "indexed_reports": indexed,
        "remaining_reports": total - indexed,
        "last_error": row["last_error"],
        "updated_at": row["updated_at"],
    }


def get_backfill_status(db: Database) -> dict:
    with db.read() as conn:
        return _status_dict(_get_state_row(conn), conn)


def run_backfill_batches(
    db: Database,
    *,
    max_batches: int = 1,
    batch_size: int = BACKFILL_BATCH_SIZE,
    on_batch_attempt: BatchHook | None = None,
) -> dict:
    """Run up to ``max_batches`` backfill batches and return the new state.

    Each batch reads at most ``batch_size`` unindexed reports (never more than
    100), indexes them from their snapshots, and only then advances the
    high-water mark -- all in one write transaction.  Any failure rolls the
    batch back, records ``last_error`` in a separate transaction, and re-raises
    without moving progress.
    """

    if max_batches < 1:
        raise UsageAuditError(422, "max_batches must be >= 1")
    if not 1 <= batch_size <= BACKFILL_BATCH_SIZE:
        raise UsageAuditError(
            422, f"batch_size must be between 1 and {BACKFILL_BATCH_SIZE}"
        )

    # Initialise (or wake up) the state row in its own transaction so a later
    # batch rollback cannot erase backfill progress metadata.
    with db.write() as conn:
        state = _ensure_state_row(conn)

    try:
        with db.write() as conn:
            state = _get_state_row(conn)
            # A row marked completed can still have work if a report was
            # inserted outside the normal issuance path; fall through and let
            # the batch query pick it up.
            total, indexed = _counts(conn)
            if state["status"] == "completed" and total == indexed:
                return BackfillBatchOut(
                    **_status_dict(state, conn),
                    batches_run=0,
                    batch_limit=max_batches,
                ).model_dump()

            batches_run = 0
            while batches_run < max_batches:
                # Only unindexed reports above the high-water mark. Reports
                # issued after backfill started are already indexed, so they
                # are skipped without ever reading their snapshots again.
                batch = conn.execute(
                    "SELECT r.id AS id FROM reports r "
                    "WHERE r.id > ? AND NOT EXISTS ("
                    "    SELECT 1 FROM report_reading_usage u "
                    "    WHERE u.report_id = r.id"
                    ") ORDER BY r.id LIMIT ?",
                    (state["last_processed_report_id"], batch_size),
                ).fetchall()
                if not batch:
                    break

                report_ids = [row["id"] for row in batch]
                if on_batch_attempt is not None:
                    on_batch_attempt(conn, report_ids)
                for report_id in report_ids:
                    index_report(conn, report_id)

                last_batch_size = len(report_ids)
                conn.execute(
                    "UPDATE usage_index_backfill_state "
                    "SET status = 'running', last_processed_report_id = ?, "
                    "last_batch_size = ?, last_error = NULL, updated_at = ? "
                    "WHERE id = 1",
                    (report_ids[-1], last_batch_size, utcnow_iso()),
                )
                state = _get_state_row(conn)
                batches_run += 1

            # The batch budget may be exhausted exactly on the last pending
            # report; completion is decided by whether anything remains.
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM reports r WHERE NOT EXISTS ("
                "    SELECT 1 FROM report_reading_usage u WHERE u.report_id = r.id"
                ")"
            ).fetchone()["n"]
            if remaining == 0:
                conn.execute(
                    "UPDATE usage_index_backfill_state SET status = 'completed', "
                    "last_error = NULL, updated_at = ? WHERE id = 1",
                    (utcnow_iso(),),
                )
            state = _get_state_row(conn)

            result = BackfillBatchOut(
                **_status_dict(state, conn),
                batches_run=batches_run,
                batch_limit=max_batches,
            ).model_dump()
            return result
    except BaseException as exc:  # the batch rolled back; remember why
        try:
            with db.write() as conn:
                row = _get_state_row(conn)
                if row is not None:
                    conn.execute(
                        "UPDATE usage_index_backfill_state SET last_error = ?, "
                        "updated_at = ? WHERE id = 1",
                        (f"{type(exc).__name__}: {exc}"[:500], utcnow_iso()),
                    )
        except sqlite3.Error:
            pass
        raise


# ---------------------------------------------------------------------------
# Discrepancy audit
# ---------------------------------------------------------------------------


def _daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def query_discrepancies(
    db: Database, segment_id: int, start: date, end: date
) -> dict:
    """Audit one segment over an inclusive range of at most 31 calendar days.

    Runs entirely in one read transaction.  Days covered by an unindexed
    report's before/after window are flagged ``pending_check``; every other
    day is classified ``discrepancy`` / ``consistent`` / ``no_data`` purely
    from the usage index.
    """

    if end < start:
        raise UsageAuditError(422, "end must be on or after start")
    if (end - start).days + 1 > MAX_QUERY_DAYS:
        raise UsageAuditError(
            422, f"date range must not exceed {MAX_QUERY_DAYS} days"
        )

    with db.read() as conn:
        segment = conn.execute(
            "SELECT 1 FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        if segment is None:
            raise UsageAuditError(404, f"segment {segment_id} not found")

        state = _get_state_row(conn)
        backfill_status = state["status"] if state is not None else "not_started"

        # Unindexed reports whose before/after windows overlap the query range.
        # No snapshot JSON is parsed here: window bounds are plain columns.
        unindexed = conn.execute(
            "SELECT r.id, r.before_start, r.before_end, r.after_start, r.after_end "
            "FROM reports r WHERE NOT EXISTS ("
            "    SELECT 1 FROM report_reading_usage u WHERE u.report_id = r.id"
            f") AND r.before_start <= ? AND r.after_end >= ? ORDER BY r.id",
            (end.isoformat(), start.isoformat()),
        ).fetchall()

        pending_map: dict[date, list[int]] = {}
        queried_days = _daterange(start, end)
        for report in unindexed:
            before_start = date.fromisoformat(report["before_start"])
            before_end = date.fromisoformat(report["before_end"])
            after_start = date.fromisoformat(report["after_start"])
            after_end = date.fromisoformat(report["after_end"])
            for day in queried_days:
                in_before = before_start <= day <= before_end
                in_after = after_start <= day <= after_end
                if in_before or in_after:
                    pending_map.setdefault(day, []).append(report["id"])

        usage_rows = conn.execute(
            "SELECT u.reading_date, u.kwh_per_lamp, u.report_id, u.role, r.status "
            "FROM report_reading_usage u JOIN reports r ON r.id = u.report_id "
            "WHERE u.segment_id = ? AND u.reading_date BETWEEN ? AND ? "
            "ORDER BY u.reading_date, u.report_id",
            (segment_id, start.isoformat(), end.isoformat()),
        ).fetchall()

        grouped: dict[date, dict[float, list[UsageReportRef]]] = {}
        for row in usage_rows:
            day = date.fromisoformat(row["reading_date"])
            refs = grouped.setdefault(day, {}).setdefault(
                row["kwh_per_lamp"], []
            )
            refs.append(
                UsageReportRef(
                    report_id=row["report_id"], role=row["role"], status=row["status"]
                )
            )

        pending_days: list[PendingCheckDay] = []
        audited_days: list[UsageDiscrepancyDay] = []
        for day in queried_days:
            if day in pending_map:
                pending_days.append(
                    PendingCheckDay(
                        date=day,
                        reason="unbackfilled_report_window",
                        unbackfilled_report_ids=pending_map[day],
                    )
                )
                continue
            value_groups = grouped.get(day)
            if not value_groups:
                audited_days.append(
                    UsageDiscrepancyDay(date=day, state="no_data", values=[])
                )
                continue
            groups = [
                UsageValueGroup(kwh_per_lamp=value, reports=refs)
                for value, refs in sorted(value_groups.items())
            ]
            state_name = "discrepancy" if len(groups) > 1 else "consistent"
            audited_days.append(
                UsageDiscrepancyDay(date=day, state=state_name, values=groups)
            )

        return UsageDiscrepancyOut(
            segment_id=segment_id,
            start=start,
            end=end,
            backfill_status=backfill_status,
            pending_check_dates=pending_days,
            days=audited_days,
        ).model_dump()
