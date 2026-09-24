"""Subscription invoice generation backed by the shared SQLite ledger.

One invoice per subscription per billing period: the invoices table carries a
UNIQUE constraint on (subscription_id, period_start, period_end) so repeated
generation for the same cycle is rejected atomically and the stored invoice
stays readable unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from .subscriptions import _connect, _parse_start_date
from .usage import _SCHEMA as _USAGE_SCHEMA

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    trial_days_used INTEGER NOT NULL,
    invoice_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (subscription_id, period_start, period_end),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _invoice_connection() -> sqlite3.Connection:
    # _connect already creates the subscriptions table; create the usage and
    # invoices tables in the same database so all data shares one file.
    connection = _connect()
    connection.execute(_USAGE_SCHEMA)
    connection.execute(_SCHEMA)
    return connection


def generate(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Generate one subscription invoice for the inclusive billing period."""
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
    today = datetime.now(timezone.utc).date()
    if parsed_start > today:
        return _fail(f"--start-date must not be later than today (UTC): {start_date}")
    if parsed_end > today:
        return _fail(f"--end-date must not be later than today (UTC): {end_date}")
    if parsed_start > parsed_end:
        return _fail(
            f"--start-date must not be later than --end-date: {start_date} > {end_date}"
        )

    connection = _invoice_connection()
    try:
        row = connection.execute(
            "SELECT id, price_cents, start_date, trial_days FROM subscriptions"
            " WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if row is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id = int(row[0])
        price_cents = int(row[1])
        subscription_start = _parse_start_date(str(row[2]))
        trial_days = int(row[3])
        trial_end = subscription_start + timedelta(days=trial_days)

        usage_rows = connection.execute(
            "SELECT usage_date, SUM(quantity) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?"
            " GROUP BY usage_date",
            (subscription_id, start_date, end_date),
        ).fetchall()

        quantity_by_date = {row[0]: int(row[1]) for row in usage_rows}

        days: list[dict[str, object]] = []
        subtotal_cents = 0
        trial_days_used = 0
        current = parsed_start
        while current <= parsed_end:
            usage_date = current.isoformat()
            quantity = quantity_by_date.get(usage_date, 0)
            billable = not (
                trial_days > 0 and subscription_start <= current < trial_end
            )
            if not billable:
                trial_days_used += 1
                amount_cents = 0
            else:
                amount_cents = quantity * price_cents
            subtotal_cents += amount_cents
            days.append(
                {
                    "usage_date": usage_date,
                    "quantity": quantity,
                    "billable": billable,
                    "amount_cents": amount_cents,
                }
            )
            current += timedelta(days=1)

        invoice = {
            "subscription_id": subscription_id,
            "period_start": start_date,
            "period_end": end_date,
            "days": days,
            "subtotal_cents": subtotal_cents,
            "trial_days_used": trial_days_used,
        }
        invoice_json = json.dumps(invoice, ensure_ascii=False, separators=(",", ":"))

        try:
            connection.execute(
                "INSERT INTO invoices (subscription_id, period_start, period_end,"
                " subtotal_cents, trial_days_used, invoice_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    subscription_id,
                    start_date,
                    end_date,
                    subtotal_cents,
                    trial_days_used,
                    invoice_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                "billing-ledger: duplicate invoice for"
                f" customer_id={customer_id} plan={plan}"
                f" period_start={start_date} period_end={end_date}",
                file=sys.stderr,
            )
            return 3
    finally:
        connection.close()

    print(invoice_json)
    return 0
