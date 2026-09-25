"""Invoice generation and proportional discount registration.

Invoices are keyed by the business key (customer_id, plan, period_start,
period_end): generating twice with the same key returns the stored invoice
verbatim and never bills twice.  Invoice ids are globally unique and
monotonically allocated by SQLite AUTOINCREMENT, independent across cycles.

Discounts are registered once per business key before the invoice is
generated; the generated invoice snapshots every computed field, so later
discount registrations never change an existing invoice.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta

from .subscriptions import _parse_start_date
from .usage import _usage_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    billed_usage INTEGER NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    discount_percent INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    trailing_days INTEGER NOT NULL,
    UNIQUE (customer_id, plan, period_start, period_end)
)
"""

_DISCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS discounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    discount_percent INTEGER NOT NULL,
    UNIQUE (customer_id, plan, period_start, period_end)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _invoice_connection() -> sqlite3.Connection:
    # _usage_connection already creates the subscriptions and usage tables;
    # add the invoice and discount tables in the same database file.
    connection = _usage_connection()
    connection.execute(_SCHEMA)
    connection.execute(_DISCOUNT_SCHEMA)
    return connection


def _find_subscription(
    connection: sqlite3.Connection, customer_id: str, plan: str
) -> tuple[int, str, int, int] | None:
    row = connection.execute(
        "SELECT id, start_date, trial_days, price_cents FROM subscriptions"
        " WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2]), int(row[3])


def _parse_period(start_date: str, end_date: str) -> tuple[date, date] | int:
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
    return parsed_start, parsed_end


def _trial_last_day(subscription_start: date, trial_days: int) -> date | None:
    """Last trial-covered date; trial runs from start_date for trial_days days."""
    if trial_days <= 0:
        return None
    return subscription_start + timedelta(days=trial_days - 1)


def _trailing_days(
    subscription_start: date, trial_days: int, period_start: date, period_end: date
) -> int:
    """Number of dates inside the period that are covered by the trial."""
    trial_last = _trial_last_day(subscription_start, trial_days)
    if trial_last is None:
        return 0
    overlap_start = max(period_start, subscription_start)
    overlap_end = min(period_end, trial_last)
    if overlap_start > overlap_end:
        return 0
    return (overlap_end - overlap_start).days + 1


def _billed_usage(
    connection: sqlite3.Connection,
    subscription_id: int,
    period_start: str,
    period_end: str,
    trial_last: date | None,
) -> int:
    """Sum per-day usage over the billed days of the period.

    Usage on trial-covered days is excluded; multiple records for the same
    subscription and date are summed per day first, then across days.
    """
    clauses = ["subscription_id = ?", "usage_date >= ?", "usage_date <= ?"]
    parameters: list[object] = [subscription_id, period_start, period_end]
    if trial_last is not None:
        clauses.append("usage_date > ?")
        parameters.append(trial_last.isoformat())
    rows = connection.execute(
        "SELECT usage_date, SUM(quantity) FROM usage_records"
        f" WHERE {' AND '.join(clauses)}"
        " GROUP BY usage_date",
        parameters,
    ).fetchall()
    return sum(int(row[1]) for row in rows)


def _invoice_record(row: sqlite3.Row | tuple) -> dict:
    return {
        "invoice_id": int(row[0]),
        "customer_id": row[1],
        "plan": row[2],
        "period_start": row[3],
        "period_end": row[4],
        "billed_usage": int(row[5]),
        "subtotal_cents": int(row[6]),
        "discount_percent": int(row[7]),
        "total_cents": int(row[8]),
        "trailing_days": int(row[9]),
    }


def _find_invoice(
    connection: sqlite3.Connection,
    customer_id: str,
    plan: str,
    period_start: str,
    period_end: str,
) -> dict | None:
    row = connection.execute(
        "SELECT id, customer_id, plan, period_start, period_end, billed_usage,"
        " subtotal_cents, discount_percent, total_cents, trailing_days"
        " FROM invoices"
        " WHERE customer_id = ? AND plan = ? AND period_start = ? AND period_end = ?",
        (customer_id, plan, period_start, period_end),
    ).fetchone()
    return _invoice_record(row) if row is not None else None


def _discount_percent(
    connection: sqlite3.Connection,
    customer_id: str,
    plan: str,
    period_start: str,
    period_end: str,
) -> int:
    row = connection.execute(
        "SELECT discount_percent FROM discounts"
        " WHERE customer_id = ? AND plan = ? AND period_start = ? AND period_end = ?",
        (customer_id, plan, period_start, period_end),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def generate(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Generate the invoice for one billing cycle, idempotently.

    The business key (customer_id, plan, period_start, period_end) identifies
    the invoice: a repeated call with the same key prints the stored invoice
    exactly as first generated and never bills twice.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    period = _parse_period(start_date, end_date)
    if isinstance(period, int):
        return period
    period_start, period_end = period

    connection = _invoice_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4

        existing = _find_invoice(connection, customer_id, plan, start_date, end_date)
        if existing is not None:
            record = existing
        else:
            subscription_id, raw_start_date, trial_days, price_cents = found
            subscription_start = _parse_start_date(raw_start_date)
            trial_last = _trial_last_day(subscription_start, trial_days)
            billed_usage = _billed_usage(
                connection, subscription_id, start_date, end_date, trial_last
            )
            subtotal_cents = billed_usage * price_cents
            discount_percent = _discount_percent(
                connection, customer_id, plan, start_date, end_date
            )
            total_cents = subtotal_cents * (100 - discount_percent) // 100
            trailing_days = _trailing_days(
                subscription_start, trial_days, period_start, period_end
            )
            try:
                cursor = connection.execute(
                    "INSERT INTO invoices (customer_id, plan, period_start, period_end,"
                    " billed_usage, subtotal_cents, discount_percent, total_cents,"
                    " trailing_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        customer_id,
                        plan,
                        start_date,
                        end_date,
                        billed_usage,
                        subtotal_cents,
                        discount_percent,
                        total_cents,
                        trailing_days,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                # Another process won the race for this business key: return
                # the invoice it stored instead of billing twice.
                connection.rollback()
                record = _find_invoice(connection, customer_id, plan, start_date, end_date)
            else:
                record = {
                    "invoice_id": cursor.lastrowid,
                    "customer_id": customer_id,
                    "plan": plan,
                    "period_start": start_date,
                    "period_end": end_date,
                    "billed_usage": billed_usage,
                    "subtotal_cents": subtotal_cents,
                    "discount_percent": discount_percent,
                    "total_cents": total_cents,
                    "trailing_days": trailing_days,
                }
    finally:
        connection.close()

    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def discount(
    customer_id: str, plan: str, start_date: str, end_date: str, percent: str
) -> int:
    """Register a one-time proportional discount for a business key.

    Only registrable while the pre-discount amount for the cycle is positive;
    a duplicate registration for the same key is rejected and leaves the
    original discount unchanged.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    period = _parse_period(start_date, end_date)
    if isinstance(period, int):
        return period
    period_start, period_end = period

    connection = _invoice_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4

        try:
            discount_percent = int(percent)
        except ValueError:
            print(
                f"billing-ledger: error: --percent must be an integer between 0 and 100: {percent!r}",
                file=sys.stderr,
            )
            return 3
        if not 0 <= discount_percent <= 100:
            print(
                f"billing-ledger: error: --percent must be an integer between 0 and 100: {percent!r}",
                file=sys.stderr,
            )
            return 3

        subscription_id, raw_start_date, trial_days, price_cents = found
        subscription_start = _parse_start_date(raw_start_date)
        trial_last = _trial_last_day(subscription_start, trial_days)
        billed_usage = _billed_usage(
            connection, subscription_id, start_date, end_date, trial_last
        )
        if billed_usage * price_cents <= 0:
            print(
                "billing-ledger: error: discount requires a positive pre-discount amount"
                f" for customer_id={customer_id} plan={plan}"
                f" period=[{start_date}, {end_date}]",
                file=sys.stderr,
            )
            return 3

        try:
            connection.execute(
                "INSERT INTO discounts (customer_id, plan, period_start, period_end,"
                " discount_percent) VALUES (?, ?, ?, ?, ?)",
                (customer_id, plan, start_date, end_date, discount_percent),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                "billing-ledger: error: discount already registered for"
                f" customer_id={customer_id} plan={plan} period=[{start_date}, {end_date}]",
                file=sys.stderr,
            )
            return 3
    finally:
        connection.close()

    record = {
        "customer_id": customer_id,
        "plan": plan,
        "period_start": start_date,
        "period_end": end_date,
        "discount_percent": discount_percent,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0
