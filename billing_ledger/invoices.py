"""Invoice generation and per-cycle discount registration.

Invoices are idempotent on the business key
``(customer_id, plan, cycle_start, cycle_end)``: repeating generation with the
same parameters returns the stored invoice unchanged and never bills twice.
Invoice ids are globally unique and monotonically allocated by SQLite
AUTOINCREMENT, so different billing cycles never interfere with one another.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta

from .subscriptions import _parse_start_date
from .usage import _find_subscription, _usage_connection

_INVOICES_SCHEMA = """
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

_DISCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_discounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    discount_percent INTEGER NOT NULL,
    UNIQUE (customer_id, plan, period_start, period_end)
)
"""

_INVOICE_COLUMNS = (
    "id, customer_id, plan, period_start, period_end, billed_usage,"
    " subtotal_cents, discount_percent, total_cents, trailing_days"
)


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _invoice_connection() -> sqlite3.Connection:
    # _usage_connection already creates the subscriptions and usage tables;
    # add the invoice and discount tables in the same database so all data
    # shares one file.
    connection = _usage_connection()
    connection.execute(_INVOICES_SCHEMA)
    connection.execute(_DISCOUNTS_SCHEMA)
    return connection


def _invoice_payload(row: sqlite3.Row | tuple) -> dict[str, object]:
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


def _fetch_existing_invoice(
    connection: sqlite3.Connection,
    customer_id: str,
    plan: str,
    period_start: str,
    period_end: str,
) -> dict[str, object] | None:
    row = connection.execute(
        f"SELECT {_INVOICE_COLUMNS} FROM invoices"
        " WHERE customer_id = ? AND plan = ? AND period_start = ? AND period_end = ?",
        (customer_id, plan, period_start, period_end),
    ).fetchone()
    return _invoice_payload(row) if row is not None else None


def _print_invoice(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _trial_window(
    subscription_start, trial_days: int
) -> tuple[object | None, object]:
    """Return the ``[first_trial_day, last_trial_day]`` trial window.

    The trial covers the first ``trial_days`` consecutive days starting on the
    subscription ``start_date`` (that day included). With no trial, the first
    element is ``None`` and the second is one day before the chargeable range.
    """
    if trial_days <= 0:
        return None, subscription_start - timedelta(days=1)
    return subscription_start, subscription_start + timedelta(days=trial_days - 1)


def generate(
    customer_id: str, plan: str, start_date: str, end_date: str
) -> int:
    """Generate (or return the existing) invoice for one billing cycle."""
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
        subscription_id, raw_subscription_start = found
        subscription_start = _parse_start_date(raw_subscription_start)
        assert subscription_start is not None

        # Idempotent fast path: the same business key always returns the
        # originally generated invoice, byte for byte.
        existing = _fetch_existing_invoice(
            connection, customer_id, plan, start_date, end_date
        )
        if existing is not None:
            _print_invoice(existing)
            return 0

        price_cents, trial_days = connection.execute(
            "SELECT price_cents, trial_days FROM subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()
        price_cents = int(price_cents)
        trial_days = int(trial_days)

        # Only the trial window's overlap with the billing cycle is free; the
        # day after the trial ends starts the chargeable range.
        _, trial_end = _trial_window(subscription_start, trial_days)
        if trial_days <= 0:
            trailing_days = 0
        else:
            overlap_start = max(parsed_start, subscription_start)
            overlap_end = min(parsed_end, trial_end)
            trailing_days = (
                (overlap_end - overlap_start).days + 1
                if overlap_start <= overlap_end
                else 0
            )
        chargeable_start = max(parsed_start, trial_end + timedelta(days=1))

        if chargeable_start <= parsed_end:
            billed_usage = int(
                connection.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
                    " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?",
                    (subscription_id, chargeable_start.isoformat(), end_date),
                ).fetchone()[0]
            )
        else:
            billed_usage = 0

        discount_row = connection.execute(
            "SELECT discount_percent FROM invoice_discounts"
            " WHERE customer_id = ? AND plan = ? AND period_start = ? AND period_end = ?",
            (customer_id, plan, start_date, end_date),
        ).fetchone()
        discount_percent = int(discount_row[0]) if discount_row is not None else 0

        subtotal_cents = billed_usage * price_cents
        total_cents = subtotal_cents * (100 - discount_percent) // 100

        try:
            cursor = connection.execute(
                "INSERT INTO invoices (customer_id, plan, period_start, period_end,"
                " billed_usage, subtotal_cents, discount_percent, total_cents, trailing_days)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            # Another process won the race for this business key: return its
            # invoice instead of billing a second time.
            connection.rollback()
            raced = _fetch_existing_invoice(
                connection, customer_id, plan, start_date, end_date
            )
            assert raced is not None
            _print_invoice(raced)
            return 0
        invoice_id = int(cursor.lastrowid)
    finally:
        connection.close()

    _print_invoice(
        {
            "invoice_id": invoice_id,
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
    )
    return 0


def register_discount(
    customer_id: str,
    plan: str,
    start_date: str,
    end_date: str,
    discount_percent: int,
) -> int:
    """Register one prorated discount for a single invoice business key.

    Invalid arguments or a duplicate registration exit 3; a missing
    subscription exits 4. The pre-discount amount must be strictly positive
    for a discount to be registered, and a rejected registration writes
    nothing.
    """
    if not customer_id:
        print("billing-ledger: error: --customer-id must not be empty", file=sys.stderr)
        return 3
    if not plan:
        print("billing-ledger: error: --plan must not be empty", file=sys.stderr)
        return 3
    parsed_start = _parse_start_date(start_date)
    if parsed_start is None:
        print(
            f"billing-ledger: error: --start-date is not a valid YYYY-MM-DD date: {start_date}",
            file=sys.stderr,
        )
        return 3
    parsed_end = _parse_start_date(end_date)
    if parsed_end is None:
        print(
            f"billing-ledger: error: --end-date is not a valid YYYY-MM-DD date: {end_date}",
            file=sys.stderr,
        )
        return 3
    if parsed_start > parsed_end:
        print(
            "billing-ledger: error: --start-date must not be later than --end-date:"
            f" {start_date} > {end_date}",
            file=sys.stderr,
        )
        return 3
    if discount_percent < 0 or discount_percent > 100:
        print(
            "billing-ledger: error: --discount-percent must be an integer between 0 and 100:"
            f" {discount_percent}",
            file=sys.stderr,
        )
        return 3

    connection = _invoice_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, raw_subscription_start = found
        subscription_start = _parse_start_date(raw_subscription_start)
        assert subscription_start is not None

        duplicate = connection.execute(
            "SELECT 1 FROM invoice_discounts"
            " WHERE customer_id = ? AND plan = ? AND period_start = ? AND period_end = ?",
            (customer_id, plan, start_date, end_date),
        ).fetchone()
        if duplicate is not None:
            print(
                "billing-ledger: error: discount already registered for business key"
                f" customer_id={customer_id} plan={plan} period_start={start_date}"
                f" period_end={end_date}",
                file=sys.stderr,
            )
            return 3

        # Compute the pre-discount amount with the same trial rules as
        # generation so discounts can only attach when billing is positive.
        price_cents, trial_days = connection.execute(
            "SELECT price_cents, trial_days FROM subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()
        price_cents = int(price_cents)
        trial_days = int(trial_days)
        _, trial_end = _trial_window(subscription_start, trial_days)
        chargeable_start = max(parsed_start, trial_end + timedelta(days=1))

        if chargeable_start <= parsed_end:
            billed_usage = int(
                connection.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
                    " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?",
                    (subscription_id, chargeable_start.isoformat(), end_date),
                ).fetchone()[0]
            )
        else:
            billed_usage = 0
        subtotal_cents = billed_usage * price_cents

        if subtotal_cents <= 0:
            print(
                "billing-ledger: error: pre-discount amount must be greater than 0 to register"
                " a discount",
                file=sys.stderr,
            )
            return 3

        try:
            connection.execute(
                "INSERT INTO invoice_discounts (customer_id, plan, period_start, period_end,"
                " discount_percent) VALUES (?, ?, ?, ?, ?)",
                (customer_id, plan, start_date, end_date, discount_percent),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                "billing-ledger: error: discount already registered for business key"
                f" customer_id={customer_id} plan={plan} period_start={start_date}"
                f" period_end={end_date}",
                file=sys.stderr,
            )
            return 3
    finally:
        connection.close()

    return 0
