"""Usage recording, per-record querying, and daily rollups backed by the SQLite ledger."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

from . import subscriptions

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    usage_date TEXT NOT NULL,
    quantity INTEGER NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    connection = subscriptions._connect()
    connection.execute(_SCHEMA)
    return connection


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _find_subscription(connection: sqlite3.Connection, customer_id: str, plan: str):
    return connection.execute(
        "SELECT id, start_date FROM subscriptions WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()


def _missing_subscription(customer_id: str, plan: str) -> int:
    print(
        f"billing-ledger: no subscription for customer_id={customer_id} plan={plan}",
        file=sys.stderr,
    )
    return 4


def _insert_records(
    connection: sqlite3.Connection, subscription_id: int, entries: list[tuple[str, int]]
) -> list[int] | None:
    """Atomically insert usage entries for one subscription.

    Returns the assigned record ids, or None without writing anything when the
    same submission carries more than one record for the same usage_date.
    """
    seen_dates: set[str] = set()
    for usage_date, _quantity in entries:
        if usage_date in seen_dates:
            return None
        seen_dates.add(usage_date)

    ids = []
    try:
        for usage_date, quantity in entries:
            cursor = connection.execute(
                "INSERT INTO usage_records (subscription_id, usage_date, quantity) VALUES (?, ?, ?)",
                (subscription_id, usage_date, quantity),
            )
            ids.append(cursor.lastrowid)
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    return ids


def record(customer_id: str, plan: str, usage_date: str, quantity: int) -> int:
    if quantity < 0:
        return _fail("--quantity must be a non-negative integer")
    parsed_date = subscriptions._parse_start_date(usage_date)
    if parsed_date is None:
        return _fail(f"--usage-date is not a valid YYYY-MM-DD date: {usage_date}")
    if parsed_date > datetime.now(timezone.utc).date():
        return _fail(f"--usage-date must not be later than today (UTC): {usage_date}")

    connection = _connect()
    try:
        subscription = _find_subscription(connection, customer_id, plan)
        if subscription is None:
            return _missing_subscription(customer_id, plan)
        subscription_id, start_date = subscription
        if usage_date < start_date:
            return _fail(
                f"--usage-date must not be earlier than the subscription start_date: "
                f"{usage_date} < {start_date}"
            )
        ids = _insert_records(connection, subscription_id, [(usage_date, quantity)])
        if ids is None:
            print(
                f"billing-ledger: duplicate usage records for customer_id={customer_id} "
                f"plan={plan} usage_date={usage_date} in one submission",
                file=sys.stderr,
            )
            return 3
    finally:
        connection.close()

    record = {
        "id": ids[0],
        "customer_id": customer_id,
        "plan": plan,
        "usage_date": usage_date,
        "quantity": quantity,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_records(customer_id: str, plan: str) -> int:
    connection = _connect()
    try:
        subscription = _find_subscription(connection, customer_id, plan)
        if subscription is None:
            return _missing_subscription(customer_id, plan)
        rows = connection.execute(
            "SELECT id, usage_date, quantity FROM usage_records"
            " WHERE subscription_id = ? ORDER BY id ASC",
            (subscription[0],),
        ).fetchall()
    finally:
        connection.close()

    records = [{"id": row[0], "usage_date": row[1], "quantity": row[2]} for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0


def summarize(customer_id: str, plan: str, start_date: str | None, end_date: str | None) -> int:
    if start_date is not None and subscriptions._parse_start_date(start_date) is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    if end_date is not None and subscriptions._parse_start_date(end_date) is None:
        return _fail(f"--end-date is not a valid YYYY-MM-DD date: {end_date}")
    if start_date is not None and end_date is not None and start_date > end_date:
        return _fail(f"--start-date must not be later than --end-date: {start_date} > {end_date}")

    connection = _connect()
    try:
        subscription = _find_subscription(connection, customer_id, plan)
        if subscription is None:
            return _missing_subscription(customer_id, plan)
        query = "SELECT usage_date, SUM(quantity) FROM usage_records WHERE subscription_id = ?"
        params: list = [subscription[0]]
        if start_date is not None:
            query += " AND usage_date >= ?"
            params.append(start_date)
        if end_date is not None:
            query += " AND usage_date <= ?"
            params.append(end_date)
        query += " GROUP BY usage_date ORDER BY usage_date ASC"
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    summary = [{"usage_date": row[0], "total": row[1]} for row in rows]
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0
