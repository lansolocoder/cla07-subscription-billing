"""Subscription registration and querying backed by a SQLite ledger."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
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
    trial_end_date TEXT,
    trial_to_active_date TEXT NOT NULL,
    UNIQUE (customer_id, plan)
)
"""

_COLUMNS = (
    "id, customer_id, plan, price_cents, start_date, trial_days, status,"
    " trial_end_date, trial_to_active_date"
)


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute(_SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    # Add trial-management columns to databases created before they existed.
    existing = {row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")}
    if "trial_end_date" not in existing:
        connection.execute("ALTER TABLE subscriptions ADD COLUMN trial_end_date TEXT")
    if "trial_to_active_date" not in existing:
        # Legacy rows never had a conversion date: their start date is the
        # closest equivalent, matching the no-trial semantics.
        connection.execute(
            "ALTER TABLE subscriptions ADD COLUMN trial_to_active_date TEXT"
        )
        connection.execute(
            "UPDATE subscriptions SET trial_to_active_date = start_date"
            " WHERE trial_to_active_date IS NULL"
        )
    connection.commit()


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _not_found(customer_id: str, plan: str) -> int:
    print(
        f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
        file=sys.stderr,
    )
    return 4


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
        "trial_end_date": row[7],
        "trial_to_active_date": row[8],
    }


def create(
    customer_id: str,
    plan: str,
    price_cents: int,
    start_date: str,
    trial_days: int | None = None,
    trial_end_date: str | None = None,
) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if price_cents < 0:
        return _fail("--price-cents must be a non-negative integer")

    if (trial_days is None) == (trial_end_date is None):
        return _fail(
            "exactly one of --trial-days and --trial-end-date must be given"
        )

    parsed_start = _parse_start_date(start_date)
    if parsed_start is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    today = datetime.now(timezone.utc).date()
    if parsed_start > today:
        return _fail(f"--start-date must not be later than today (UTC): {start_date}")

    if trial_days is not None:
        if trial_days < 0:
            return _fail("--trial-days must be a non-negative integer")
        if trial_days == 0:
            parsed_trial_end: date | None = None
            trial_end = None
        else:
            parsed_trial_end = parsed_start + timedelta(days=trial_days - 1)
            trial_end = parsed_trial_end.isoformat()
    else:
        parsed_trial_end = _parse_start_date(trial_end_date)
        if parsed_trial_end is None:
            return _fail(
                f"--trial-end-date is not a valid YYYY-MM-DD date: {trial_end_date}"
            )
        if parsed_trial_end < parsed_start:
            return _fail(
                f"--trial-end-date must not be earlier than start_date {start_date}: "
                f"{trial_end_date}"
            )
        if parsed_trial_end < today:
            return _fail(
                f"--trial-end-date must not be earlier than today (UTC): {trial_end_date}"
            )
        trial_end = trial_end_date
        trial_days = (parsed_trial_end - parsed_start).days + 1

    has_trial = trial_end is not None
    status = "trial" if has_trial else "active"
    trial_to_active = (
        parsed_trial_end + timedelta(days=1) if has_trial else parsed_start
    ).isoformat()

    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO subscriptions (customer_id, plan, price_cents, start_date,"
            " trial_days, status, trial_end_date, trial_to_active_date)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                plan,
                price_cents,
                start_date,
                trial_days,
                status,
                trial_end,
                trial_to_active,
            ),
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
        "trial_end_date": trial_end,
        "trial_to_active_date": trial_to_active,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_all() -> int:
    records = []
    if DB_PATH.exists():
        connection = _connect()
        try:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM subscriptions ORDER BY start_date ASC, id DESC"
            ).fetchall()
        finally:
            connection.close()
        records = [_record(row) for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0


def _fetch(connection: sqlite3.Connection, customer_id: str, plan: str) -> dict | None:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM subscriptions WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    return _record(row) if row is not None else None


def trial_info(customer_id: str, plan: str) -> int:
    """Print the compact trial status of one subscription."""
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _connect()
    try:
        record = _fetch(connection, customer_id, plan)
    finally:
        connection.close()
    if record is None:
        return _not_found(customer_id, plan)

    payload = {
        "customer_id": record["customer_id"],
        "plan": record["plan"],
        "start_date": record["start_date"],
        "trial_days": record["trial_days"],
        "trial_end_date": record["trial_end_date"],
        "trial_to_active_date": record["trial_to_active_date"],
        "status": record["status"],
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


def activate(customer_id: str, plan: str, as_of: str | None = None) -> int:
    """Convert a trial subscription to active status as of the given date."""
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    today = datetime.now(timezone.utc).date()
    if as_of is None:
        parsed_as_of = today
        as_of = parsed_as_of.isoformat()
    else:
        parsed_as_of = _parse_start_date(as_of)
        if parsed_as_of is None:
            return _fail(f"--as-of is not a valid YYYY-MM-DD date: {as_of}")

    connection = _connect()
    try:
        record = _fetch(connection, customer_id, plan)
        if record is None:
            return _not_found(customer_id, plan)

        conversion_date = _parse_start_date(record["trial_to_active_date"])
        if parsed_as_of < conversion_date:
            return _fail(
                f"--as-of must not be earlier than trial_to_active_date "
                f"{record['trial_to_active_date']}: {as_of}"
            )

        connection.execute(
            "UPDATE subscriptions SET status = 'active' WHERE id = ?",
            (record["id"],),
        )
        connection.commit()
    finally:
        connection.close()

    payload = {
        "customer_id": customer_id,
        "plan": plan,
        "status": "active",
        "activated_on": as_of,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0
