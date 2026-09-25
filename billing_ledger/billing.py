"""Bill generation and querying backed by the same SQLite ledger.

Bills persist next to subscriptions and usage records; bill ids are globally
unique, monotonically allocated by SQLite AUTOINCREMENT and stable across
processes. A subscription may hold at most one bill per (period_start,
period_end) pair.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta

from .subscriptions import _parse_start_date
from .usage import _usage_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    billable_quantity INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (subscription_id, period_start, period_end),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""

_BILLING_UNIT = 100


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _billing_connection() -> sqlite3.Connection:
    # _usage_connection already creates the subscriptions and usage tables;
    # add the bills table in the same database so all ledger data shares one file.
    connection = _usage_connection()
    connection.execute(_SCHEMA)
    return connection


def _find_subscription(
    connection: sqlite3.Connection, customer_id: str, plan: str
) -> tuple[int, int, str, int] | None:
    row = connection.execute(
        "SELECT id, price_cents, start_date, trial_days FROM subscriptions"
        " WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]), str(row[2]), int(row[3])


def _bill_record(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": int(row[0]),
        "subscription_id": int(row[1]),
        "period_start": row[2],
        "period_end": row[3],
        "billable_quantity": int(row[4]),
        "amount_cents": int(row[5]),
        "status": row[6],
    }


def _billable_quantity(
    connection: sqlite3.Connection,
    subscription_id: int,
    period_start: str,
    period_end: str,
    start_date: str,
    trial_days: int,
) -> int:
    """Sum usage inside the period, excluding the subscription's trial window.

    The trial window covers ``trial_days`` consecutive days starting at the
    subscription's ``start_date`` (day 1); ``trial_days == 0`` means no trial.
    Period dates earlier than ``start_date`` can never hold usage records, so
    they contribute 0 naturally.
    """
    clauses = ["subscription_id = ?", "usage_date >= ?", "usage_date <= ?"]
    parameters: list[object] = [subscription_id, period_start, period_end]
    if trial_days > 0:
        parsed_start = _parse_start_date(start_date)
        trial_end = (parsed_start + timedelta(days=trial_days - 1)).isoformat()
        clauses.append("(usage_date < ? OR usage_date > ?)")
        parameters.extend([start_date, trial_end])
    row = connection.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
        f" WHERE {' AND '.join(clauses)}",
        parameters,
    ).fetchone()
    return int(row[0])


def generate(customer_id: str, plan: str, period_start: str, period_end: str) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    parsed_start = _parse_start_date(period_start)
    if parsed_start is None:
        return _fail(f"--period-start is not a valid YYYY-MM-DD date: {period_start}")
    parsed_end = _parse_start_date(period_end)
    if parsed_end is None:
        return _fail(f"--period-end is not a valid YYYY-MM-DD date: {period_end}")
    if parsed_start > parsed_end:
        return _fail(
            f"--period-start must not be later than --period-end: {period_start} > {period_end}"
        )

    connection = _billing_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, price_cents, start_date, trial_days = found

        quantity = _billable_quantity(
            connection, subscription_id, period_start, period_end, start_date, trial_days
        )
        # Zero billable usage (e.g. the whole period falls inside the trial
        # window) bills 0 segments; positive usage rounds up per 100 units.
        segments = 0 if quantity == 0 else -(-quantity // _BILLING_UNIT)
        amount_cents = segments * price_cents

        try:
            cursor = connection.execute(
                "INSERT INTO bills (subscription_id, period_start, period_end,"
                " billable_quantity, amount_cents, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (subscription_id, period_start, period_end, quantity, amount_cents, "issued"),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                "billing-ledger: duplicate bill for "
                f"customer_id={customer_id} plan={plan} "
                f"period_start={period_start} period_end={period_end}",
                file=sys.stderr,
            )
            return 3
        bill_id = cursor.lastrowid
    finally:
        connection.close()

    record = {
        "id": bill_id,
        "subscription_id": subscription_id,
        "period_start": period_start,
        "period_end": period_end,
        "billable_quantity": quantity,
        "amount_cents": amount_cents,
        "status": "issued",
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_bills(customer_id: str, plan: str) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _billing_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id = found[0]
        rows = connection.execute(
            "SELECT id, subscription_id, period_start, period_end,"
            " billable_quantity, amount_cents, status FROM bills"
            " WHERE subscription_id = ? ORDER BY id ASC",
            (subscription_id,),
        ).fetchall()
    finally:
        connection.close()

    records = [_bill_record(row) for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0
