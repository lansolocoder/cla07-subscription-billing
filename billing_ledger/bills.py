"""Bill generation, adjustment, voiding and querying for one billing cycle.

Bills live in the same SQLite ledger as subscriptions and usage records;
bill ids are globally unique, monotonically allocated by SQLite
AUTOINCREMENT and stable across processes. The business key of a bill is
(customer_id, plan, period_start, period_end): generating the same cycle
again reads the existing bill back without changing it.

Adjustments are post-generation corrections (credit subtracts, debit adds)
kept in the bill_adjustments table. At most one adjustment is effective per
bill: a repeated adjust logically overwrites the previous one and the bill
total is recomputed from base_cents + usage_cents plus the latest
adjustment, never below zero. Adjustment references are globally unique; a
duplicate reference is rejected without overwriting anything.

Voiding flips an open bill to ``voided`` without touching its amounts; a
voided bill rejects further voids, payments and adjustments.
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

_ADJUSTMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bill_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    FOREIGN KEY (bill_id) REFERENCES bills (id)
)
"""

_USAGE_CENTS_PER_UNIT = 10


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _bill_connection() -> sqlite3.Connection:
    # _usage_connection already creates the subscriptions and usage tables;
    # add the bills and bill_adjustments tables in the same database so all
    # data shares one file.
    connection = _usage_connection()
    connection.execute(_SCHEMA)
    connection.execute(_ADJUSTMENTS_SCHEMA)
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


def _settlement(
    total_cents: int, rows: list[tuple]
) -> tuple[list[dict], int, bool]:
    """Apply payments in order and derive per-payment reconciliation lines.

    Returns the lines, the total actually applied and whether any payment
    arrived after the bill was already settled (over-collection).

    Each payment applies at most the remaining balance: the payment whose
    cumulative applied amount reaches ``total_cents`` settles the bill (its
    excess counts only up to exact settlement); every later payment applies
    nothing and is ``overpaid``. If the running total never reaches the bill
    total, each effective payment that fails to settle is ``underpaid`` (in
    particular the final payment keeps the bill open).
    """
    lines: list[dict] = []
    applied_total = 0
    settled = False
    over_collected = False
    for row in rows:
        bill_id = int(row[0])
        reference = row[1]
        amount = int(row[2])
        if settled:
            applied = 0
            result = "overpaid"
            over_collected = True
        else:
            remaining = total_cents - applied_total
            applied = min(amount, remaining)
            applied_total += applied
            if applied_total >= total_cents:
                settled = True
                result = "settled"
            else:
                result = "underpaid"
        lines.append(
            {
                "bill_id": bill_id,
                "reference": reference,
                "amount_cents": amount,
                "applied_cents": applied,
                "result": result,
            }
        )
    return lines, applied_total, over_collected


def _derive_status(total_cents: int, rows: list[tuple]) -> str:
    """Derive the payment-match summary status for a bill total.

    Shared by ``bill adjust`` (which persists the recomputed status) and
    ``payment match`` (which reports it read-only) so both always agree.
    """
    _, paid_cents, over_collected = _settlement(total_cents, rows)
    if not rows:
        return "open"
    if paid_cents < total_cents:
        return "open"
    if over_collected:
        return "partial"
    return "paid"


def _payment_rows(connection: sqlite3.Connection, bill_id: int) -> list[tuple]:
    """Read the payments registered for one bill in registration order.

    The payments table is owned by the payments module and may not exist
    yet if no payment command ever ran against this database.
    """
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payments'"
    ).fetchone()
    if has_table is None:
        return []
    return list(
        connection.execute(
            "SELECT bill_id, reference, amount_cents FROM payments"
            " WHERE bill_id = ? ORDER BY id ASC",
            (bill_id,),
        ).fetchall()
    )


def _find_bill(
    connection: sqlite3.Connection, bill_id: int, customer_id: str, plan: str
) -> tuple | None:
    return connection.execute(
        "SELECT id, base_cents, usage_cents, status FROM bills"
        " WHERE id = ? AND customer_id = ? AND plan = ?",
        (bill_id, customer_id, plan),
    ).fetchone()


def _no_bill(bill_id: int, customer_id: str, plan: str) -> int:
    print(
        "billing-ledger: no bill found for"
        f" bill_id={bill_id} customer_id={customer_id} plan={plan}",
        file=sys.stderr,
    )
    return 4


def adjust(
    customer_id: str,
    plan: str,
    bill_id: int,
    kind: str,
    amount_cents: int,
    reason: str,
    reference: str,
) -> int:
    """Append an adjustment to a bill and recompute its total and status.

    A credit subtracts from and a debit adds to the original total
    (base_cents + usage_cents); the adjusted total never drops below zero.
    At most one adjustment is effective per bill: a repeated adjust
    logically overwrites the previous one and recomputes from the original
    amounts. The reference is globally unique across all adjustments; a
    duplicate is rejected (exit 5) without overwriting anything. The new
    status follows the payment-match summary rule and is persisted so
    ``bill list`` reflects it. Voided bills reject adjustments (exit 2).
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if kind not in ("credit", "debit"):
        return _fail(f"--kind must be credit or debit: {kind}")
    if amount_cents <= 0:
        return _fail("--amount-cents must be a positive integer")
    if not reason:
        return _fail("--reason must not be empty")
    if not reference:
        return _fail("--reference must not be empty")

    connection = _bill_connection()
    try:
        bill = _find_bill(connection, bill_id, customer_id, plan)
        if bill is None:
            return _no_bill(bill_id, customer_id, plan)
        base_cents, usage_cents, status = int(bill[1]), int(bill[2]), bill[3]
        if status == "voided":
            return _fail(f"cannot adjust a voided bill: bill_id={bill_id}")

        try:
            connection.execute(
                "INSERT INTO bill_adjustments (bill_id, kind, amount_cents,"
                " reason, reference) VALUES (?, ?, ?, ?, ?)",
                (bill_id, kind, amount_cents, reason, reference),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            print(
                f"billing-ledger: duplicate adjustment reference: {reference}",
                file=sys.stderr,
            )
            return 5

        delta = amount_cents if kind == "debit" else -amount_cents
        total_cents = max(0, base_cents + usage_cents + delta)
        new_status = _derive_status(total_cents, _payment_rows(connection, bill_id))
        connection.execute(
            "UPDATE bills SET total_cents = ?, status = ? WHERE id = ?",
            (total_cents, new_status, bill_id),
        )
        connection.commit()
    finally:
        connection.close()

    record = {
        "bill_id": bill_id,
        "kind": kind,
        "amount_cents": amount_cents,
        "reference": reference,
        "reason": reason,
        "base_cents": base_cents,
        "usage_cents": usage_cents,
        "total_cents": total_cents,
        "status": new_status,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def void(customer_id: str, plan: str, bill_id: int) -> int:
    """Void an open bill, leaving its amounts untouched.

    Only ``open`` bills can be voided; voiding an already voided, paid or
    partial bill is rejected (exit 2) without writing anything. Registered
    payments stay as they are and ``payment match`` keeps reporting the
    voided bill read-only with summary status ``voided``.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _bill_connection()
    try:
        bill = _find_bill(connection, bill_id, customer_id, plan)
        if bill is None:
            return _no_bill(bill_id, customer_id, plan)
        status = bill[3]
        if status != "open":
            return _fail(
                f"only open bills can be voided: bill_id={bill_id} status={status}"
            )
        connection.execute(
            "UPDATE bills SET status = 'voided' WHERE id = ?", (bill_id,)
        )
        connection.commit()
    finally:
        connection.close()

    print(
        json.dumps(
            {"bill_id": bill_id, "status": "voided"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0
