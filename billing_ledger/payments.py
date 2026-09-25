"""Payment registration, matching and balance reconciliation.

Payments persist in the same SQLite ledger as subscriptions, usage records
and bills; payment ids are globally unique, monotonically allocated by SQLite
AUTOINCREMENT and stable across processes. A ``payment_ref`` may be matched
at most once across the whole ledger, while a single bill may accumulate
any number of payments.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from .billing import _billing_connection, _find_subscription

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    payment_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    FOREIGN KEY (bill_id) REFERENCES bills (id)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _payments_connection() -> sqlite3.Connection:
    # _billing_connection already creates the subscriptions, usage and bills
    # tables; add the payments table in the same database so all ledger data
    # shares one file.
    connection = _billing_connection()
    connection.execute(_SCHEMA)
    return connection


def _find_bill(
    connection: sqlite3.Connection, subscription_id: int, bill_id: int
) -> tuple[int, int] | None:
    row = connection.execute(
        "SELECT id, amount_cents FROM bills WHERE id = ? AND subscription_id = ?",
        (bill_id, subscription_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1])


def payment(
    customer_id: str, plan: str, bill_id: int, amount_cents: int, payment_ref: str
) -> int:
    """Register a payment and match it to the given bill."""
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if amount_cents < 1:
        return _fail("--amount-cents must be a positive integer")
    if not payment_ref:
        return _fail("--payment-ref must not be empty")

    connection = _payments_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id = found[0]

        bill = _find_bill(connection, subscription_id, bill_id)
        if bill is None:
            print(
                f"billing-ledger: no bill found for customer_id={customer_id} plan={plan}"
                f" bill_id={bill_id}",
                file=sys.stderr,
            )
            return 4

        try:
            cursor = connection.execute(
                "INSERT INTO payments (bill_id, amount_cents, payment_ref, status)"
                " VALUES (?, ?, ?, ?)",
                (bill_id, amount_cents, payment_ref, "applied"),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                f"billing-ledger: duplicate payment_ref={payment_ref}",
                file=sys.stderr,
            )
            return 3
        payment_id = cursor.lastrowid
    finally:
        connection.close()

    record = {
        "id": payment_id,
        "bill_id": bill_id,
        "amount_cents": amount_cents,
        "payment_ref": payment_ref,
        "status": "applied",
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def payment_status(customer_id: str, plan: str, bill_id: int) -> int:
    """Read back the paid total and remaining balance for one bill."""
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _payments_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id = found[0]

        bill = _find_bill(connection, subscription_id, bill_id)
        if bill is None:
            print(
                f"billing-ledger: no bill found for customer_id={customer_id} plan={plan}"
                f" bill_id={bill_id}",
                file=sys.stderr,
            )
            return 4
        _, amount_cents = bill

        row = connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE bill_id = ?",
            (bill_id,),
        ).fetchone()
        paid_cents = int(row[0])
    finally:
        connection.close()

    balance_cents = amount_cents - paid_cents
    if balance_cents > 0:
        status = "unpaid"
    elif balance_cents < 0:
        status = "overpaid"
    else:
        status = "paid"

    record = {
        "bill_id": bill_id,
        "amount_cents": amount_cents,
        "paid_cents": paid_cents,
        "balance_cents": balance_cents,
        "status": status,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0
