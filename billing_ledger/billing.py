"""Bill generation and querying backed by the same SQLite ledger.

Bills persist alongside subscriptions and usage records; bill ids are
globally unique, monotonically allocated by SQLite AUTOINCREMENT and stable
across processes. One subscription may hold at most one bill per
(period_start, period_end) pair.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta

from . import usage
from .subscriptions import _connect, _parse_start_date

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

_BILLABLE_UNIT = 100


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _billing_connection() -> sqlite3.Connection:
    # _connect already creates the subscriptions table; add the usage and
    # bills tables in the same database so all ledger data shares one file.
    connection = _connect()
    connection.execute(usage._SCHEMA)
    connection.execute(_SCHEMA)
    return connection


def _find_subscription(
    connection: sqlite3.Connection, customer_id: str, plan: str
) -> tuple[int, str, int, int] | None:
    row = connection.execute(
        "SELECT id, start_date, price_cents, trial_days FROM subscriptions"
        " WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2]), int(row[3])


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


def generate(customer_id: str, plan: str, period_start: str, period_end: str) -> int:
    """Generate and persist one bill for a subscription and billing period.

    Billable quantity sums all usage records inside the inclusive period,
    skipping the trial window (the first ``trial_days`` days starting at the
    subscription's ``start_date``; day one is ``start_date`` itself). The
    remaining quantity is rounded up to whole 100-unit segments and priced at
    ``price_cents`` per segment.
    """
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
        subscription_id, raw_start_date, price_cents, trial_days = found

        # Usage before the subscription start_date cannot exist, so period
        # dates earlier than start_date simply contribute zero.
        start_date = _parse_start_date(raw_start_date)
        billable_from = start_date + timedelta(days=trial_days)
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?"
            " AND usage_date >= ?",
            (subscription_id, period_start, period_end, billable_from.isoformat()),
        ).fetchone()
        billable_quantity = int(row[0])
        segments = -(-billable_quantity // _BILLABLE_UNIT)
        amount_cents = segments * price_cents

        duplicate = connection.execute(
            "SELECT 1 FROM bills WHERE subscription_id = ? AND period_start = ? AND period_end = ?",
            (subscription_id, period_start, period_end),
        ).fetchone()
        if duplicate is not None:
            print(
                "billing-ledger: duplicate bill for "
                f"customer_id={customer_id} plan={plan} "
                f"period_start={period_start} period_end={period_end}",
                file=sys.stderr,
            )
            return 3

        try:
            cursor = connection.execute(
                "INSERT INTO bills"
                " (subscription_id, period_start, period_end, billable_quantity,"
                " amount_cents, status) VALUES (?, ?, ?, ?, ?, ?)",
                (subscription_id, period_start, period_end, billable_quantity,
                 amount_cents, "issued"),
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
        "billable_quantity": billable_quantity,
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
        subscription_id, _, _, _ = found
        rows = connection.execute(
            "SELECT id, subscription_id, period_start, period_end, billable_quantity,"
            " amount_cents, status FROM bills WHERE subscription_id = ? ORDER BY id ASC",
            (subscription_id,),
        ).fetchall()
    finally:
        connection.close()

    records = [_bill_record(row) for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0
