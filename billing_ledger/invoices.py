"""Subscription invoice generation for fixed billing cycles.

Invoices live in the same SQLite ledger as subscriptions and usage
records. Exactly one invoice exists per
``(subscription_id, period_start, period_end)``; generating a duplicate
cycle rejects the request and leaves the stored invoice untouched.
Usage records are only ever read, never modified.
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
    detail TEXT NOT NULL,
    UNIQUE (subscription_id, period_start, period_end),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _invoice_connection() -> sqlite3.Connection:
    # _connect creates the subscriptions table; ensure the usage and
    # invoice tables exist in the same database file as well.
    connection = _connect()
    connection.execute(_USAGE_SCHEMA)
    connection.execute(_SCHEMA)
    return connection


def generate(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Generate one invoice for the inclusive ``[start_date, end_date]`` cycle.

    Each day's aggregated usage is priced at the subscription's
    ``price_cents`` per unit. Days covered by the subscription's trial
    window (``trial_days`` consecutive days from ``start_date``,
    inclusive) keep their usage detail but are not billed.
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
        subscription = connection.execute(
            "SELECT id, price_cents, start_date, trial_days FROM subscriptions"
            " WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if subscription is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id = int(subscription[0])
        price_cents = int(subscription[1])
        subscription_start = _parse_start_date(str(subscription[2]))
        trial_days = int(subscription[3])

        existing = connection.execute(
            "SELECT detail FROM invoices"
            " WHERE subscription_id = ? AND period_start = ? AND period_end = ?",
            (subscription_id, start_date, end_date),
        ).fetchone()
        if existing is not None:
            print(
                "billing-ledger: duplicate invoice for customer_id="
                f"{customer_id} plan={plan} period={start_date}..{end_date}",
                file=sys.stderr,
            )
            return 3

        rows = connection.execute(
            "SELECT usage_date, SUM(quantity) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?"
            " GROUP BY usage_date",
            (subscription_id, start_date, end_date),
        ).fetchall()
        totals_by_date = {row[0]: int(row[1]) for row in rows}

        # The trial window is [subscription_start, trial_end_exclusive);
        # with trial_days == 0 it is empty.
        trial_end_exclusive = subscription_start + timedelta(days=trial_days)

        days: list[dict[str, object]] = []
        subtotal_cents = 0
        trial_days_used = 0
        current: date = parsed_start
        while current <= parsed_end:
            usage_date = current.isoformat()
            quantity = totals_by_date.get(usage_date, 0)
            in_trial = trial_days > 0 and subscription_start <= current < trial_end_exclusive
            billable = not in_trial
            amount_cents = quantity * price_cents if billable else 0
            if billable:
                subtotal_cents += amount_cents
            else:
                trial_days_used += 1
            days.append(
                {
                    "usage_date": usage_date,
                    "quantity": quantity,
                    "billable": billable,
                    "amount_cents": amount_cents,
                }
            )
            current += timedelta(days=1)

        detail = {
            "subscription_id": subscription_id,
            "period_start": start_date,
            "period_end": end_date,
            "days": days,
            "subtotal_cents": subtotal_cents,
            "trial_days_used": trial_days_used,
        }

        try:
            connection.execute(
                "INSERT INTO invoices (subscription_id, period_start, period_end,"
                " subtotal_cents, trial_days_used, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    subscription_id,
                    start_date,
                    end_date,
                    subtotal_cents,
                    trial_days_used,
                    json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            # Concurrent generation of the same cycle lost the race; the
            # previously stored invoice stays intact.
            connection.rollback()
            print(
                "billing-ledger: duplicate invoice for customer_id="
                f"{customer_id} plan={plan} period={start_date}..{end_date}",
                file=sys.stderr,
            )
            return 3
    finally:
        connection.close()

    print(json.dumps(detail, ensure_ascii=False, separators=(",", ":")))
    return 0
