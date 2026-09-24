"""Subscription registration and querying backed by a SQLite ledger."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path


def _default_db_path() -> Path:
    override = os.environ.get("BILLING_LEDGER_DB")
    return Path(override) if override else Path(__file__).resolve().parents[1] / "ledger.db"


DB_PATH = _default_db_path()

_DATE_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    trial_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (customer_id, plan)
)
"""


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute(_SCHEMA)
    return connection


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _parse_start_date(raw: str) -> date | None:
    if not _DATE_FORMAT.match(raw):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _record(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row[0],
        "customer_id": row[1],
        "plan": row[2],
        "price_cents": row[3],
        "start_date": row[4],
        "trial_days": row[5],
        "status": row[6],
    }


def create(customer_id: str, plan: str, price_cents: int, start_date: str, trial_days: int) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if price_cents < 0:
        return _fail("--price-cents must be a non-negative integer")
    if trial_days < 0:
        return _fail("--trial-days must be a non-negative integer")
    parsed_date = _parse_start_date(start_date)
    if parsed_date is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    if parsed_date > datetime.now(timezone.utc).date():
        return _fail(f"--start-date must not be later than today (UTC): {start_date}")

    status = "trial" if trial_days > 0 else "active"
    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO subscriptions (customer_id, plan, price_cents, start_date, trial_days, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, plan, price_cents, start_date, trial_days, status),
        )
        connection.commit()
        subscription_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        connection.rollback()
        print(
            f"billing-ledger: duplicate subscription for customer_id={customer_id} plan={plan}",
            file=sys.stderr,
        )
        return 3
    finally:
        connection.close()

    record = {
        "id": subscription_id,
        "customer_id": customer_id,
        "plan": plan,
        "price_cents": price_cents,
        "start_date": start_date,
        "trial_days": trial_days,
        "status": status,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_all() -> int:
    records = []
    if DB_PATH.exists():
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id, customer_id, plan, price_cents, start_date, trial_days, status"
                " FROM subscriptions ORDER BY start_date ASC, id DESC"
            ).fetchall()
        finally:
            connection.close()
        records = [_record(row) for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0
