"""Payment registration and bill settlement reconciliation.

Payments live in the same SQLite ledger as subscriptions, usage and bills;
payment ids are globally unique, monotonically allocated by SQLite
AUTOINCREMENT and stable across processes. The reference of a payment is
globally unique as well: registering the same reference again is rejected
without overwriting the existing payment.

Reconciliation (``match``) is a read-only, idempotent computation over the
payments registered for a bill. Payments apply against the bill in
registration order; each payment's applied amount and result are derived
anew on every run, so matching never mutates persisted data and running it
again prints the identical result.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from .bills import _bill_connection, _derive_status, _settlement
from .subscriptions import _parse_start_date


_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL,
    payment_date TEXT NOT NULL,
    FOREIGN KEY (bill_id) REFERENCES bills (id)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _payment_connection() -> sqlite3.Connection:
    # _bill_connection already creates the subscriptions, usage and bills
    # tables; add the payments table in the same database so all data shares
    # one file.
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
        "status": "applied",
    }


_SELECT = (
    "SELECT p.id, p.bill_id, b.customer_id, b.plan, p.reference,"
    " p.amount_cents, p.payment_date FROM payments p JOIN bills b"
    " ON p.bill_id = b.id"
)


def record(
    customer_id: str,
    plan: str,
    bill_id: int,
    amount_cents: int,
    payment_date: str,
    reference: str,
) -> int:
    """Register one payment for an existing bill.

    The bill must exist and belong to the given customer and plan (exit 4)
    and must not be voided (exit 2). The payment reference is globally unique; a duplicate reference is
    rejected (exit 5) without overwriting the prior registration. Invalid
    dates and non-positive amounts reject the registration (exit 2) before
    any write.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if not reference:
        return _fail("--reference must not be empty")
    if amount_cents <= 0:
        return _fail("--amount-cents must be a positive integer")
    parsed_date = _parse_start_date(payment_date)
    if parsed_date is None:
        return _fail(f"--payment-date is not a valid YYYY-MM-DD date: {payment_date}")

    connection = _payment_connection()
    try:
        bill = connection.execute(
            "SELECT id, status FROM bills WHERE id = ? AND customer_id = ? AND plan = ?",
            (bill_id, customer_id, plan),
        ).fetchone()
        if bill is None:
            print(
                "billing-ledger: no bill found for"
                f" bill_id={bill_id} customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        if bill[1] == "voided":
            return _fail(f"cannot record a payment for a voided bill: bill_id={bill_id}")

        try:
            cursor = connection.execute(
                "INSERT INTO payments (bill_id, reference, amount_cents, payment_date)"
                " VALUES (?, ?, ?, ?)",
                (bill_id, reference, amount_cents, payment_date),
            )
            connection.commit()
            payment_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                f"billing-ledger: duplicate payment reference: {reference}",
                file=sys.stderr,
            )
            return 5
    finally:
        connection.close()

    result = {
        "id": payment_id,
        "bill_id": bill_id,
        "customer_id": customer_id,
        "plan": plan,
        "reference": reference,
        "amount_cents": amount_cents,
        "payment_date": payment_date,
        "status": "applied",
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def match(bill_id: int) -> int:
    """Reconcile registered payments against one bill (read-only, idempotent).

    Prints one reconciliation line per payment in registration order, then
    one summary line. Re-running the command prints the identical output
    and never accumulates applications twice. A voided bill still reports
    its lines as usual; its summary status stays ``voided``.
    """
    connection = _payment_connection()
    try:
        bill = connection.execute(
            "SELECT id, total_cents, status FROM bills WHERE id = ?",
            (bill_id,),
        ).fetchone()
        if bill is None:
            print(f"billing-ledger: no bill found for bill_id={bill_id}", file=sys.stderr)
            return 4
        total_cents = int(bill[1])
        bill_status = bill[2]
        rows = connection.execute(
            "SELECT bill_id, reference, amount_cents FROM payments"
            " WHERE bill_id = ? ORDER BY id ASC",
            (bill_id,),
        ).fetchall()
    finally:
        connection.close()

    lines, paid_cents, _ = _settlement(total_cents, list(rows))
    for line in lines:
        print(json.dumps(line, ensure_ascii=False, separators=(",", ":")))

    if bill_status == "voided":
        status = "voided"
    else:
        status = _derive_status(total_cents, list(rows))

    summary = {
        "bill_id": bill_id,
        "total_cents": total_cents,
        "paid_cents": paid_cents,
        "status": status,
    }
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_payments() -> int:
    """List every registered payment in registration order as one JSON array."""
    connection = _payment_connection()
    try:
        rows = connection.execute(f"{_SELECT} ORDER BY p.id ASC").fetchall()
    finally:
        connection.close()
    print(json.dumps([_view(row) for row in rows], ensure_ascii=False, separators=(",", ":")))
    return 0
