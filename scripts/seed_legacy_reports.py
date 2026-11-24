#!/usr/bin/env python3
"""Seed "legacy" reports for the usage-index backfill Swagger demo.

The service itself only indexes reports at issuance time.  To demonstrate the
backfill path (reports that predate the usage index) this script:

1. creates a target + two control segments and issues ``--reports`` reports
   through the public API, deliberately changing one control reading between
   reports so the audit has a real discrepancy to find;
2. connects directly to the SQLite file and deletes the generated usage-index
   rows plus any backfill state, turning those reports into unindexed "old"
   reports **without modifying the reports or their snapshots**.

Only the Python standard library is required.  Run it inside the compose
service (the DB lives in the ``lights-data`` volume)::

    docker compose exec api python scripts/seed_legacy_reports.py
    # or against a local database:
    python scripts/seed_legacy_reports.py --db ./data/lights.db \
        --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]
CHANGED_DAY = (RETROFIT + timedelta(days=3)).isoformat()  # 2026-03-13


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # pragma: no cover - demo helper
        body = exc.read().decode()
        raise SystemExit(f"{method} {url} failed ({exc.code}): {body}") from exc


def _readings(before: float, after: float) -> dict:
    return {
        "readings": [
            {"date": d.isoformat(), "kwh_per_lamp": before} for d in BEFORE
        ]
        + [{"date": d.isoformat(), "kwh_per_lamp": after} for d in AFTER]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="/data/lights.db")
    parser.add_argument("--reports", type=int, default=250)
    parser.add_argument("--suffix", default="legacy")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    _request("GET", f"{base}/healthz")

    def create_segment(name: str, retrofit: date | None) -> int:
        body = _request(
            "POST",
            f"{base}/api/segments",
            {
                "name": f"{name}-{args.suffix}",
                "retrofit_date": retrofit.isoformat() if retrofit else None,
            },
        )
        return body["id"]

    target = create_segment("目标", RETROFIT)
    control1 = create_segment("对照一", None)
    control2 = create_segment("对照二", None)

    _request(
        "PUT",
        f"{base}/api/segments/{target}/readings",
        _readings(10.0, 8.0),
    )
    _request(
        "PUT",
        f"{base}/api/segments/{control1}/readings",
        _readings(10.2, 9.7),
    )
    _request(
        "PUT",
        f"{base}/api/segments/{control2}/readings",
        _readings(10.4, 9.9),
    )

    report_ids: list[int] = []
    for i in range(args.reports):
        body = _request("POST", f"{base}/api/reports?segment_id={target}")
        if body["status"] != "issued":
            raise SystemExit(f"report {i} unexpectedly deferred: {body}")
        report_ids.append(body["report"]["id"])
        # From the second report on, control1 cites a different value on the
        # changed day; later corrections leave earlier snapshots untouched.
        if i == 0:
            _request(
                "PUT",
                f"{base}/api/segments/{control1}/readings",
                {"readings": [{"date": CHANGED_DAY, "kwh_per_lamp": 9.0}]},
            )

    print(f"issued {len(report_ids)} reports; resetting usage index ...")
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        deleted = conn.execute("DELETE FROM report_reading_usage").rowcount
        conn.execute("DELETE FROM usage_index_backfill_state")
        conn.commit()
    finally:
        conn.close()

    payload = {
        "target_segment_id": target,
        "control_segment_id": control1,
        "changed_day": CHANGED_DAY,
        "report_ids": report_ids,
        "index_rows_deleted": deleted,
        "window": {
            "before_start": BEFORE[-1].isoformat(),
            "before_end": BEFORE[0].isoformat(),
            "after_start": AFTER[0].isoformat(),
            "after_end": AFTER[-1].isoformat(),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(
        "\nNext: GET /api/usage-index/backfill, then "
        "POST /api/usage-index/backfill/batches?max_batches=1&batch_size=100, "
        "and query "
        f"/api/segments/{control1}/usage-discrepancies"
        "?start=2026-03-03&end=2026-03-17 while backfill is in progress."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
