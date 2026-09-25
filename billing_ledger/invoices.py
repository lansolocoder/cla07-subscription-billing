"""Invoice generation and per-subscription invoice querying.

Invoices live in the same SQLite ledger as subscriptions and usage; invoice
ids are globally unique, monotonically allocated by SQLite AUTOINCREMENT and
stable across processes. One subscription may hold at most one invoice for a
given inclusive billing period.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta

from .subscriptions import _connect, _parse_start_date
from .usage import _SCHEMA as _USAGE_SCHEMA
from .usage import _find_subscription

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
    # _connect already creates the subscriptions table; add the usage and
    # invoices tables in the same database so every kind of data shares one
    # file and the usage_total query works even before any usage command ran.
    connection = _connect()
    connection.execute(_USAGE_SCHEMA)
    connection.execute(_SCHEMA)
    return connection


def _invoice_record(row: sqlite3.Row | tuple) -> dict:
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


def generate(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Generate one invoice for the inclusive [start_date, end_date] cycle.

    The trial runs for ``trial_days`` consecutive days beginning on the
    subscription's ``start_date``; only the trial days falling inside the
    requested period are free. Every other day in the period bills at the
    subscription's ``price_cents`` daily rate.
    """
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
        subscription_id, raw_sub_start = found
        subscription = connection.execute(
            "SELECT price_cents, start_date, trial_days FROM subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()
        price_cents = int(subscription[0])
        trial_days = int(subscription[2])
        sub_start = _parse_start_date(str(subscription[1]))

        # Serialise the duplicate check and the insert so concurrent processes
        # cannot both create the same (subscription, period) invoice.
        connection.execute("BEGIN IMMEDIATE")
        duplicate = connection.execute(
            "SELECT 1 FROM invoices"
            " WHERE subscription_id = ? AND period_start = ? AND period_end = ?",
            (subscription_id, start_date, end_date),
        ).fetchone()
        if duplicate is not None:
            connection.rollback()
            print(
                "billing-ledger: duplicate invoice for"
                f" customer_id={customer_id} plan={plan} period={start_date}..{end_date}",
                file=sys.stderr,
            )
            return 3

        total_days = (parsed_end - parsed_start).days + 1
        trial_days_in_period = 0
        if trial_days > 0:
            trial_first = max(parsed_start, sub_start)
            trial_last = min(parsed_end, sub_start + timedelta(days=trial_days - 1))
            if trial_last >= trial_first:
                trial_days_in_period = (trial_last - trial_first).days + 1
        billed_days = total_days - trial_days_in_period
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
            # Lost a race against another process creating the same invoice.
            connection.rollback()
            print(
                "billing-ledger: duplicate invoice for"
                f" customer_id={customer_id} plan={plan} period={start_date}..{end_date}",
                file=sys.stderr,
            )
            return 3
        invoice_id = int(cursor.lastrowid)
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
        subscription_id, _ = found
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
