"""Bill generation and querying backed by the SQLite ledger.

Bills live in the same database as subscriptions and usage records; bill ids
are globally unique, monotonically allocated by SQLite AUTOINCREMENT and
stable across processes. The business key of a bill is
``(customer_id, plan, period_start, period_end)``: generating the same key
again is an idempotent read-back of the existing bill.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date

from .subscriptions import _connect, _parse_start_date
from .usage import _SCHEMA as _USAGE_SCHEMA

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    base_cents INTEGER NOT NULL,
    usage_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (customer_id, plan, period_start, period_end),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""

# Usage is billed at a flat 10 cents per recorded unit.
_USAGE_RATE_CENTS = 10


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _bills_connection() -> sqlite3.Connection:
    # _connect already creates the subscriptions table; add the usage and
    # bills tables in the same database so all ledger data shares one file.
    connection = _connect()
    connection.execute(_USAGE_SCHEMA)
    connection.execute(_SCHEMA)
    return connection


def _bill_view(row: tuple) -> dict:
    return {
        "id": int(row[0]),
        "customer_id": row[1],
        "plan": row[2],
        "period_start": row[3],
        "period_end": row[4],
        "base_cents": int(row[5]),
        "usage_cents": int(row[6]),
        "total_cents": int(row[7]),
        "status": row[8],
    }


_SELECT_BILL = (
    "SELECT id, customer_id, plan, period_start, period_end,"
    " base_cents, usage_cents, total_cents, status FROM bills"
)


def _find_bill(
    connection: sqlite3.Connection,
    customer_id: str,
    plan: str,
    period_start: str,
    period_end: str,
) -> dict | None:
    row = connection.execute(
        f"{_SELECT_BILL}"
        " WHERE customer_id = ? AND plan = ? AND period_start = ? AND period_end = ?",
        (customer_id, plan, period_start, period_end),
    ).fetchone()
    return _bill_view(row) if row is not None else None


def generate(customer_id: str, plan: str, period_start: str, period_end: str) -> int:
    """Generate the bill for one subscription and billing period.

    Only ``active`` subscriptions may be billed. The base fee is prorated by
    the number of billable days in the period: days not earlier than
    ``max(start_date, trial_to_active_date)``. The usage fee is the period's
    total recorded quantity times 10 cents.
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
            f"--period-start must not be later than --period-end: "
            f"{period_start} > {period_end}"
        )

    connection = _bills_connection()
    try:
        row = connection.execute(
            "SELECT id, price_cents, start_date, trial_to_active_date, status"
            " FROM subscriptions WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if row is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, price_cents, start_date, conversion_date, status = row

        if status != "active":
            return _fail(
                "only active subscriptions can be billed:"
                f" customer_id={customer_id} plan={plan} status={status}"
            )

        existing = _find_bill(connection, customer_id, plan, period_start, period_end)
        if existing is not None:
            # Idempotent read-back: never create a second bill or touch the
            # existing amounts and status for the same business key.
            print(json.dumps(existing, ensure_ascii=False, separators=(",", ":")))
            return 0

        period_days = (parsed_end - parsed_start).days + 1
        billable_from = max(
            parsed_start,
            _parse_start_date(str(start_date)),
            _parse_start_date(str(conversion_date)),
        )
        billable_days = max(0, (parsed_end - billable_from).days + 1)
        base_cents = (int(price_cents) * billable_days) // period_days

        cycle_total = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?",
            (subscription_id, period_start, period_end),
        ).fetchone()[0]
        usage_cents = int(cycle_total) * _USAGE_RATE_CENTS
        total_cents = base_cents + usage_cents

        try:
            cursor = connection.execute(
                "INSERT INTO bills (subscription_id, customer_id, plan, period_start,"
                " period_end, base_cents, usage_cents, total_cents, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (
                    subscription_id,
                    customer_id,
                    plan,
                    period_start,
                    period_end,
                    base_cents,
                    usage_cents,
                    total_cents,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            # A concurrent generator won the race on the business key: read
            # the existing bill back instead of failing.
            connection.rollback()
            existing = _find_bill(connection, customer_id, plan, period_start, period_end)
            print(json.dumps(existing, ensure_ascii=False, separators=(",", ":")))
            return 0

        bill = {
            "id": cursor.lastrowid,
            "customer_id": customer_id,
            "plan": plan,
            "period_start": period_start,
            "period_end": period_end,
            "base_cents": base_cents,
            "usage_cents": usage_cents,
            "total_cents": total_cents,
            "status": "open",
        }
    finally:
        connection.close()

    print(json.dumps(bill, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_bills(customer_id: str, plan: str) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _bills_connection()
    try:
        found = connection.execute(
            "SELECT id FROM subscriptions WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        rows = connection.execute(
            f"{_SELECT_BILL} WHERE customer_id = ? AND plan = ? ORDER BY id ASC",
            (customer_id, plan),
        ).fetchall()
    finally:
        connection.close()

    bills = [_bill_view(row) for row in rows]
    print(json.dumps(bills, ensure_ascii=False, separators=(",", ":")))
    return 0
