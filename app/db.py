"""SQLite access helpers.

The whole service deliberately uses short, explicit transactions with
``BEGIN IMMEDIATE`` for every write, including report generation.  SQLite then
serialises report generation and corrections at the database level, which is
what guarantees that a correction can never interleave between the snapshot
read and the report insert (and therefore can never miss marking a report as
pending recalculation).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    retrofit_date  TEXT,
    version        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    segment_id    INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    reading_date  TEXT NOT NULL,
    kwh_per_lamp  REAL NOT NULL CHECK (kwh_per_lamp >= 0),
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (segment_id, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_readings_date
    ON readings (reading_date);

CREATE TABLE IF NOT EXISTS reports (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_segment_id               INTEGER NOT NULL REFERENCES segments(id),
    target_name                     TEXT NOT NULL,
    target_retrofit_date            TEXT NOT NULL,
    before_start                    TEXT NOT NULL,
    before_end                      TEXT NOT NULL,
    after_start                     TEXT NOT NULL,
    after_end                       TEXT NOT NULL,
    skipped_date                    TEXT NOT NULL,
    target_before_avg               REAL NOT NULL,
    target_after_avg                REAL NOT NULL,
    target_reduction                REAL NOT NULL,
    control_avg_reduction           REAL NOT NULL,
    net_savings_per_lamp_per_day    REAL NOT NULL,
    controls_json                   TEXT NOT NULL,
    snapshot_json                   TEXT NOT NULL,
    status                          TEXT NOT NULL
        CHECK (status IN ('issued', 'pending_recalc')),
    stale_at                        TEXT,
    stale_reasons_json              TEXT NOT NULL DEFAULT '[]',
    stale_segment_ids_json          TEXT NOT NULL DEFAULT '[]',
    created_at                      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_target
    ON reports (target_segment_id, created_at);

-- Usage index: one row per (report, segment, calendar day) the report's
-- immutable snapshot actually cites.  Populated synchronously inside the
-- report-issuance write transaction for new reports, and backfilled batch by
-- batch for reports that predate the index.  Querying discrepancies only ever
-- reads this table; report snapshots are never parsed per query.
CREATE TABLE IF NOT EXISTS report_reading_usage (
    report_id     INTEGER NOT NULL REFERENCES reports(id),
    segment_id    INTEGER NOT NULL REFERENCES segments(id),
    reading_date  TEXT NOT NULL,
    kwh_per_lamp  REAL NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('target', 'control')),
    PRIMARY KEY (report_id, segment_id, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_usage_lookup
    ON report_reading_usage (segment_id, reading_date);
CREATE INDEX IF NOT EXISTS idx_usage_report
    ON report_reading_usage (report_id);

-- Backfill progress for reports issued before the usage index existed.
-- A single row (id = 1) holds the high-water mark: every report with
-- id <= last_processed_report_id has been attempted, and only successfully
-- indexed reports move the mark, so a failed batch leaves progress untouched
-- and a restart resumes with the same batch.
CREATE TABLE IF NOT EXISTS usage_index_backfill_state (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),
    status                      TEXT NOT NULL
        CHECK (status IN ('running', 'completed')),
    last_processed_report_id    INTEGER NOT NULL DEFAULT 0,
    last_batch_size             INTEGER NOT NULL DEFAULT 0,
    last_error                  TEXT,
    updated_at                  TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    """Small wrapper around sqlite3 with explicit read/write transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # explicit transactions below
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
