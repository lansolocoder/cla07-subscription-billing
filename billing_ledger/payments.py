"""Payment registration and bill settlement matching.

Payments live in the same SQLite ledger as subscriptions, usage records and
bills; payment ids are globally unique, monotonically allocated by SQLite
AUTOINCREMENT and stable across processes. A payment is uniquely keyed by its
non-empty ``reference`` across the whole ledger.

Matching is a read-only reconciliation: the amounts actually applied to a
bill are recomputed from the persisted payments on every invocation, so
repeating ``payment match`` prints the same result without ever accumulating
write-offs twice.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from .bills import _bill_connection
from .subscriptions import _parse_start_date

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    reference TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (reference),
    FOREIGN KEY (bill_id) REFERENCES bills (id)
)
"""

_FIELDS = (
    "id, bill_id, customer_id, plan, reference, amount_cents,"
    " payment_date, status"
)


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _payment_connection() -> sqlite3.Connection:
    # _bill_connection already creates the subscriptions, usage and bills
    # tables; add the payments table in the same database.
    connection = _bill_connection()
    connection.execute(_SCHEMA)
    return connection


def _view(row: tuple) -> dict:
    return {
        "id": int(row[0]),
        "bill_id": int(row[1]),
        "customer_id": row[2],
        "plan": row[3],
        "reference": row[4],
        "amount_cents": int(row[5]),
        "payment_date": row[6],
        "status": row[7],
    }


def record(
    customer_id: str,
    plan: str,
    bill_id: int,
    amount_cents: int,
    payment_date: str,
    reference: str,
) -> int:
    """Register one payment against an existing bill.

    Validation order matches the rest of the ledger: invalid arguments or
    dates fail first (exit 2), then the bill lookup (exit 4), and only then a
    duplicate reference is rejected (exit 5). No failure writes anything.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if not reference:
        return _fail("--reference must not be empty")
    if amount_cents <= 0:
        return _fail("--amount-cents must be a positive integer")
    if _parse_start_date(payment_date) is None:
        return _fail(f"--payment-date is not a valid YYYY-MM-DD date: {payment_date}")

    connection = _payment_connection()
    try:
        row = connection.execute(
            "SELECT id, customer_id, plan FROM bills WHERE id = ?",
            (bill_id,),
        ).fetchone()
        if row is None or row[1] != customer_id or row[2] != plan:
            print(
                "billing-ledger: no bill found for"
                f" bill_id={bill_id} customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4

        duplicate = connection.execute(
            "SELECT 1 FROM payments WHERE reference = ?", (reference,)
        ).fetchone()
        if duplicate is not None:
            print(
                f"billing-ledger: duplicate payment reference: {reference}",
                file=sys.stderr,
            )
            return 5

        try:
            cursor = connection.execute(
                "INSERT INTO payments (bill_id, customer_id, plan, reference,"
                " amount_cents, payment_date, status)"
                " VALUES (?, ?, ?, ?, ?, ?, 'applied')",
                (bill_id, customer_id, plan, reference, amount_cents, payment_date),
            )
            connection.commit()
            payment_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            # A concurrent registration won the race on the reference:
            # reject instead of overwriting the existing payment.
            connection.rollback()
            print(
                f"billing-ledger: duplicate payment reference: {reference}",
                file=sys.stderr,
            )
            return 5
    finally:
        connection.close()

    record = {
        "id": payment_id,
        "bill_id": bill_id,
        "customer_id": customer_id,
        "plan": plan,
        "reference": reference,
        "amount_cents": amount_cents,
        "payment_date": payment_date,
        "status": "applied",
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def match(bill_id: int) -> int:
    """Reconcile one bill against its payments in registration order.

    Each payment prints one write-off line: ``settled`` for the payment that
    brings the applied total to ``total_cents`` (its excess, if any, is
    clipped to the remaining amount), ``overpaid`` with ``applied_cents`` 0
    for payments arriving after settlement, and ``underpaid`` for an
    effective payment that still leaves the bill short. A final summary line
    reports the bill status: ``paid`` when settled exactly, ``open`` while
    short, ``partial`` when any payment arrived after settlement.
    """
    connection = _payment_connection()
    try:
        row = connection.execute(
            "SELECT total_cents FROM bills WHERE id = ?", (bill_id,)
        ).fetchone()
        if row is None:
            print(f"billing-ledger: no bill found for bill_id={bill_id}", file=sys.stderr)
            return 4
        total_cents = int(row[0])

        payments = connection.execute(
            "SELECT reference, amount_cents FROM payments"
            " WHERE bill_id = ? ORDER BY id ASC",
            (bill_id,),
        ).fetchall()
    finally:
        connection.close()

    paid_cents = 0
    has_overpayment = False
    for reference, raw_amount in payments:
        amount_cents = int(raw_amount)
        remaining = total_cents - paid_cents
        if remaining <= 0:
            applied_cents = 0
            result = "overpaid"
            has_overpayment = True
        else:
            applied_cents = min(amount_cents, remaining)
            paid_cents += applied_cents
            if paid_cents == total_cents:
                result = "settled"
            else:
                result = "underpaid"
        print(
            json.dumps(
                {
                    "bill_id": bill_id,
                    "reference": reference,
                    "amount_cents": amount_cents,
                    "applied_cents": applied_cents,
                    "result": result,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    if has_overpayment:
        status = "partial"
    elif paid_cents == total_cents:
        status = "paid"
    else:
        status = "open"
    print(
        json.dumps(
            {
                "bill_id": bill_id,
                "total_cents": total_cents,
                "paid_cents": paid_cents,
                "status": status,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


def list_all() -> int:
    """List every payment in registration (id) order as one JSON array line."""
    connection = _payment_connection()
    try:
        rows = connection.execute(
            f"SELECT {_FIELDS} FROM payments ORDER BY id ASC"
        ).fetchall()
    finally:
        connection.close()

    print(json.dumps([_view(row) for row in rows], ensure_ascii=False, separators=(",", ":")))
    return 0
