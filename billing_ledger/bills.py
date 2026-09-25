"""Bill generation and querying for one billing cycle.

Bills live in the same SQLite ledger as subscriptions and usage records;
bill ids are globally unique, monotonically allocated by SQLite
AUTOINCREMENT and stable across processes. The business key of a bill is
(customer_id, plan, period_start, period_end): generating the same cycle
again reads the existing bill back without changing it.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from .subscriptions import _parse_start_date
from .usage import _usage_connection

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
    UNIQUE (subscription_id, period_start, period_end),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""

_USAGE_CENTS_PER_UNIT = 10


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _bill_connection() -> sqlite3.Connection:
    # _usage_connection already creates the subscriptions and usage tables;
    # add the bills table in the same database so all data shares one file.
    connection = _usage_connection()
    connection.execute(_SCHEMA)
    return connection


def _view(row: tuple) -> dict:
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


_SELECT = (
    "SELECT id, customer_id, plan, period_start, period_end,"
    " base_cents, usage_cents, total_cents, status FROM bills"
)


def _find_existing(
    connection: sqlite3.Connection,
    subscription_id: int,
    period_start: str,
    period_end: str,
) -> tuple | None:
    return connection.execute(
        f"{_SELECT} WHERE subscription_id = ? AND period_start = ? AND period_end = ?",
        (subscription_id, period_start, period_end),
    ).fetchone()


def generate(customer_id: str, plan: str, period_start: str, period_end: str) -> int:
    """Generate the bill for one billing cycle of an active subscription.

    Idempotent on the business key (customer_id, plan, period_start,
    period_end): a repeated generation prints the persisted bill unchanged.
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

    connection = _bill_connection()
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

        existing = _find_existing(connection, subscription_id, period_start, period_end)
        if existing is not None:
            print(json.dumps(_view(existing), ensure_ascii=False, separators=(",", ":")))
            return 0

        period_days = (parsed_end - parsed_start).days + 1
        billable_from = max(
            _parse_start_date(str(start_date)),
            _parse_start_date(str(conversion_date)),
        )
        first_billable = max(parsed_start, billable_from)
        billable_days = max(0, (parsed_end - first_billable).days + 1)
        base_cents = int(price_cents) * billable_days // period_days

        cycle_total = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?",
            (subscription_id, period_start, period_end),
        ).fetchone()[0]
        usage_cents = int(cycle_total) * _USAGE_CENTS_PER_UNIT
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
            bill_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            # A concurrent generation won the race on the business key:
            # read the persisted bill back instead of failing.
            connection.rollback()
            existing = _find_existing(connection, subscription_id, period_start, period_end)
            print(json.dumps(_view(existing), ensure_ascii=False, separators=(",", ":")))
            return 0
    finally:
        connection.close()

    record = {
        "id": bill_id,
        "customer_id": customer_id,
        "plan": plan,
        "period_start": period_start,
        "period_end": period_end,
        "base_cents": base_cents,
        "usage_cents": usage_cents,
        "total_cents": total_cents,
        "status": "open",
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_bills(customer_id: str, plan: str) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _bill_connection()
    try:
        row = connection.execute(
            "SELECT id FROM subscriptions WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if row is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        rows = connection.execute(
            f"{_SELECT} WHERE subscription_id = ? ORDER BY id ASC",
            (int(row[0]),),
        ).fetchall()
    finally:
        connection.close()

    print(json.dumps([_view(row) for row in rows], ensure_ascii=False, separators=(",", ":")))
    return 0
