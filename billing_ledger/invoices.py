"""Invoice generation and per-subscription invoice querying.

Invoices live in the same SQLite ledger as subscriptions and usage; invoice
ids are globally unique, monotonically allocated by SQLite AUTOINCREMENT and
stable across processes. At most one invoice may exist for a given
subscription and billing period.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta

from .subscriptions import _connect, _parse_start_date
from .usage import _SCHEMA as _USAGE_SCHEMA

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    billed_days INTEGER NOT NULL,
    trial_days_in_period INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    usage_total INTEGER NOT NULL,
    UNIQUE (subscription_id, period_start, period_end),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _invoice_connection() -> sqlite3.Connection:
    # _connect already creates the subscriptions table; add the invoices table
    # in the same database so all data shares one file. The usage table is
    # ensured too so usage totals can be computed even if none was recorded.
    connection = _connect()
    connection.execute(_USAGE_SCHEMA)
    connection.execute(_SCHEMA)
    return connection


def _invoice_record(row: tuple) -> dict:
    return {
        "id": int(row[0]),
        "subscription_id": int(row[1]),
        "period_start": row[2],
        "period_end": row[3],
        "billed_days": int(row[4]),
        "trial_days_in_period": int(row[5]),
        "amount_cents": int(row[6]),
        "usage_total": int(row[7]),
    }


def _find_subscription(
    connection: sqlite3.Connection, customer_id: str, plan: str
) -> tuple[int, int, date, int] | None:
    row = connection.execute(
        "SELECT id, price_cents, start_date, trial_days FROM subscriptions"
        " WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]), _parse_start_date(str(row[2])), int(row[3])


def _trial_days_in_period(
    period_start: date, period_end: date, subscription_start: date, trial_days: int
) -> int:
    """Count period dates covered by the trial window.

    The trial runs for ``trial_days`` consecutive days starting at the
    subscription ``start_date`` (``[start_date, start_date + trial_days - 1]``).
    It may extend beyond either period boundary; only the overlap with the
    period is free.
    """
    if trial_days <= 0:
        return 0
    trial_start = subscription_start
    trial_end = subscription_start + timedelta(days=trial_days - 1)
    overlap_start = max(period_start, trial_start)
    overlap_end = min(period_end, trial_end)
    if overlap_end < overlap_start:
        return 0
    return (overlap_end - overlap_start).days + 1


def generate(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Generate one invoice for the inclusive [start_date, end_date] cycle."""
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    parsed_start = _parse_start_date(start_date)
    if parsed_start is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    parsed_end = _parse_start_date(end_date)
    if parsed_end is None:
        return _fail(f"--end-date is not a valid YYYY-MM-DD date: {end_date}")
    if parsed_start > parsed_end:
        return _fail(
            f"--start-date must not be later than --end-date: {start_date} > {end_date}"
        )

    connection = _invoice_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, price_cents, subscription_start, trial_days = found

        period_days = (parsed_end - parsed_start).days + 1
        trial_days_in_period = _trial_days_in_period(
            parsed_start, parsed_end, subscription_start, trial_days
        )
        billed_days = period_days - trial_days_in_period
        amount_cents = billed_days * price_cents

        usage_row = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?",
            (subscription_id, start_date, end_date),
        ).fetchone()
        usage_total = int(usage_row[0])

        try:
            cursor = connection.execute(
                "INSERT INTO invoices (subscription_id, period_start, period_end,"
                " billed_days, trial_days_in_period, amount_cents, usage_total)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    subscription_id,
                    start_date,
                    end_date,
                    billed_days,
                    trial_days_in_period,
                    amount_cents,
                    usage_total,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                "billing-ledger: duplicate invoice for subscription_id="
                f"{subscription_id} period {start_date}..{end_date}",
                file=sys.stderr,
            )
            return 3
        invoice_id = cursor.lastrowid
    finally:
        connection.close()

    record = {
        "id": invoice_id,
        "subscription_id": subscription_id,
        "period_start": start_date,
        "period_end": end_date,
        "billed_days": billed_days,
        "trial_days_in_period": trial_days_in_period,
        "amount_cents": amount_cents,
        "usage_total": usage_total,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_for_subscription(customer_id: str, plan: str) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _invoice_connection()
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
            "SELECT id, subscription_id, period_start, period_end, billed_days,"
            " trial_days_in_period, amount_cents, usage_total FROM invoices"
            " WHERE subscription_id = ? ORDER BY id ASC",
            (subscription_id,),
        ).fetchall()
    finally:
        connection.close()

    records = [_invoice_record(row) for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0
