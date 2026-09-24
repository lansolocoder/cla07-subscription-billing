"""Usage recording, per-record querying and daily summarisation.

Records live in the same SQLite ledger as subscriptions; record ids are
globally unique, monotonically allocated by SQLite AUTOINCREMENT and stable
across processes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from .subscriptions import _connect, _parse_start_date

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    usage_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
)
"""


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _usage_connection() -> sqlite3.Connection:
    # _connect already creates the subscriptions table; add the usage table in
    # the same database so both kinds of data share one file.
    connection = _connect()
    connection.execute(_SCHEMA)
    return connection


def _find_subscription(
    connection: sqlite3.Connection, customer_id: str, plan: str
) -> tuple[int, str] | None:
    row = connection.execute(
        "SELECT id, start_date FROM subscriptions WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


def _find_subscription_detail(
    connection: sqlite3.Connection, customer_id: str, plan: str
) -> tuple[int, str, int, int] | None:
    row = connection.execute(
        "SELECT id, start_date, price_cents, trial_days FROM subscriptions"
        " WHERE customer_id = ? AND plan = ?",
        (customer_id, plan),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2]), int(row[3])


def _parse_date(raw: str) -> date | None:
    return _parse_start_date(raw)


def record(customer_id: str, plan: str, entries: list[tuple[str, int]]) -> int:
    """Record one or more usage entries in a single atomic submission.

    ``entries`` holds ``(usage_date, quantity)`` pairs in command-line order.
    Several entries for the same subscription on the same date are allowed
    across separate submissions (they add up in the summary), but duplicate
    dates within one submission reject the whole batch.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    parsed_entries: list[tuple[str, date, int]] = []
    today = datetime.now(timezone.utc).date()
    for usage_date, quantity in entries:
        if quantity < 0:
            return _fail("--quantity must be a non-negative integer")
        parsed_date = _parse_date(usage_date)
        if parsed_date is None:
            return _fail(f"--usage-date is not a valid YYYY-MM-DD date: {usage_date}")
        if parsed_date > today:
            return _fail(f"--usage-date must not be later than today (UTC): {usage_date}")
        parsed_entries.append((usage_date, parsed_date, quantity))

    connection = _usage_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, raw_start_date = found
        start_date = _parse_date(raw_start_date)
        for usage_date, parsed_date, _ in parsed_entries:
            if parsed_date < start_date:
                return _fail(
                    f"--usage-date must not be earlier than subscription start_date "
                    f"{raw_start_date}: {usage_date}"
                )

        seen_dates: set[str] = set()
        for usage_date, _, _ in parsed_entries:
            if usage_date in seen_dates:
                print(
                    "billing-ledger: duplicate usage entries in the same submission for "
                    f"customer_id={customer_id} plan={plan} usage_date={usage_date}",
                    file=sys.stderr,
                )
                return 3
            seen_dates.add(usage_date)

        records: list[dict[str, object]] = []
        try:
            for usage_date, _, quantity in parsed_entries:
                cursor = connection.execute(
                    "INSERT INTO usage_records (subscription_id, usage_date, quantity)"
                    " VALUES (?, ?, ?)",
                    (subscription_id, usage_date, quantity),
                )
                records.append(
                    {
                        "id": cursor.lastrowid,
                        "customer_id": customer_id,
                        "plan": plan,
                        "usage_date": usage_date,
                        "quantity": quantity,
                    }
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()

    for item in records:
        print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_records(customer_id: str, plan: str) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _usage_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, _ = found
        rows = connection.execute(
            "SELECT id, usage_date, quantity FROM usage_records"
            " WHERE subscription_id = ? ORDER BY id ASC",
            (subscription_id,),
        ).fetchall()
    finally:
        connection.close()

    records = [
        {"id": int(row[0]), "usage_date": row[1], "quantity": int(row[2])} for row in rows
    ]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0


def summary(
    customer_id: str,
    plan: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    parsed_start = None
    parsed_end = None
    if start_date is not None:
        parsed_start = _parse_date(start_date)
        if parsed_start is None:
            return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    if end_date is not None:
        parsed_end = _parse_date(end_date)
        if parsed_end is None:
            return _fail(f"--end-date is not a valid YYYY-MM-DD date: {end_date}")
    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        return _fail(
            f"--start-date must not be later than --end-date: {start_date} > {end_date}"
        )

    connection = _usage_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, _ = found

        clauses = ["subscription_id = ?"]
        parameters: list[object] = [subscription_id]
        if parsed_start is not None:
            clauses.append("usage_date >= ?")
            parameters.append(start_date)
        if parsed_end is not None:
            clauses.append("usage_date <= ?")
            parameters.append(end_date)
        rows = connection.execute(
            "SELECT usage_date, SUM(quantity) FROM usage_records"
            f" WHERE {' AND '.join(clauses)}"
            " GROUP BY usage_date ORDER BY usage_date ASC",
            parameters,
        ).fetchall()
    finally:
        connection.close()

    totals = [{"usage_date": row[0], "total": int(row[1])} for row in rows]
    print(json.dumps(totals, ensure_ascii=False, separators=(",", ":")))
    return 0


def reconcile(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Build the per-day usage reconciliation detail for one billing cycle.

    Read-only: every date in the inclusive ``[start_date, end_date]`` range
    appears exactly once, flagged ``missing`` when no usage was recorded.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    parsed_start = _parse_date(start_date)
    if parsed_start is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    parsed_end = _parse_date(end_date)
    if parsed_end is None:
        return _fail(f"--end-date is not a valid YYYY-MM-DD date: {end_date}")
    if parsed_start > parsed_end:
        return _fail(
            f"--start-date must not be later than --end-date: {start_date} > {end_date}"
        )

    connection = _usage_connection()
    try:
        found = _find_subscription(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, _ = found
        rows = connection.execute(
            "SELECT usage_date, SUM(quantity) FROM usage_records"
            " WHERE subscription_id = ? AND usage_date >= ? AND usage_date <= ?"
            " GROUP BY usage_date",
            (subscription_id, start_date, end_date),
        ).fetchall()
    finally:
        connection.close()

    totals_by_date = {row[0]: int(row[1]) for row in rows}
    days: list[dict[str, object]] = []
    cycle_total = 0
    present_days = 0
    current = parsed_start
    while current <= parsed_end:
        usage_date = current.isoformat()
        total = totals_by_date.get(usage_date, 0)
        missing = usage_date not in totals_by_date
        if not missing:
            present_days += 1
        cycle_total += total
        days.append({"usage_date": usage_date, "total": total, "missing": missing})
        current += timedelta(days=1)

    detail = {
        "subscription_id": subscription_id,
        "period_start": start_date,
        "period_end": end_date,
        "days": days,
        "cycle_total": cycle_total,
        "coverage": {"present_days": present_days, "missing_days": len(days) - present_days},
    }
    print(json.dumps(detail, ensure_ascii=False, separators=(",", ":")))
    return 0


def cycles(customer_id: str, plan: str, start_date: str, end_date: str) -> int:
    """Attribute every day in ``[start_date, end_date]`` to a billing cycle.

    Read-only. ``billing_start`` is the subscription ``start_date`` shifted by
    ``trial_days``; days before ``start_date`` are reported with ``cycle_no``
    0 and no charge, trial days are flagged with no charge, and every billing
    day accrues ``price_cents`` against its 30-day cycle.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    parsed_start = _parse_date(start_date)
    if parsed_start is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    parsed_end = _parse_date(end_date)
    if parsed_end is None:
        return _fail(f"--end-date is not a valid YYYY-MM-DD date: {end_date}")
    if parsed_start > parsed_end:
        return _fail(
            f"--start-date must not be later than --end-date: {start_date} > {end_date}"
        )

    connection = _connect()
    try:
        found = _find_subscription_detail(connection, customer_id, plan)
        if found is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        subscription_id, raw_sub_start, price_cents, trial_days = found
    finally:
        connection.close()

    sub_start = _parse_date(raw_sub_start)
    billing_start = sub_start + timedelta(days=trial_days)
    trial_start = raw_sub_start if trial_days > 0 else None
    trial_end = (billing_start - timedelta(days=1)).isoformat() if trial_days > 0 else None

    days: list[dict[str, object]] = []
    cycle_total_cents = 0
    current = parsed_start
    while current <= parsed_end:
        usage_date = current.isoformat()
        if current < sub_start:
            cycle_no = 0
            is_trial = False
            amount_cents = 0
        elif current < billing_start:
            cycle_no = 0
            is_trial = True
            amount_cents = 0
        else:
            cycle_no = (current - billing_start).days // 30 + 1
            is_trial = False
            amount_cents = price_cents
        cycle_total_cents += amount_cents
        days.append(
            {
                "usage_date": usage_date,
                "cycle_no": cycle_no,
                "is_trial": is_trial,
                "amount_cents": amount_cents,
            }
        )
        current += timedelta(days=1)

    detail = {
        "subscription_id": subscription_id,
        "period_start": start_date,
        "period_end": end_date,
        "trial_start": trial_start,
        "trial_end": trial_end,
        "days": days,
        "cycle_total_cents": cycle_total_cents,
    }
    print(json.dumps(detail, ensure_ascii=False, separators=(",", ":")))
    return 0
