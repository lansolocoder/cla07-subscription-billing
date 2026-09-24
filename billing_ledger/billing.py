"""Billing-cycle usage reconciliation detail (read-only).

Given a subscription (customer_id + plan) and an inclusive billing period,
every calendar date in the period is reported with its summed usage and a
missing flag. Nothing is written by this query beyond the schema bootstrap
shared with the other read commands.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta

from .subscriptions import _parse_start_date
from .usage import _find_subscription, _usage_connection


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def reconcile(
    customer_id: str,
    plan: str,
    start_date: str,
    end_date: str,
) -> int:
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
            " GROUP BY usage_date ORDER BY usage_date ASC",
            (subscription_id, start_date, end_date),
        ).fetchall()
    finally:
        connection.close()

    # Presence is keyed on record existence: a day carrying only zero-quantity
    # records is still "present" (missing=false) even though its total is 0.
    totals_by_date: dict[str, int] = {row[0]: int(row[1]) for row in rows}

    days: list[dict[str, object]] = []
    present_days = 0
    cycle_total = 0
    current = parsed_start
    while current <= parsed_end:
        usage_date = current.isoformat()
        if usage_date in totals_by_date:
            total = totals_by_date[usage_date]
            missing = False
            present_days += 1
        else:
            total = 0
            missing = True
        days.append({"usage_date": usage_date, "total": total, "missing": missing})
        cycle_total += total
        current += timedelta(days=1)

    total_days = len(days)
    report = {
        "subscription_id": subscription_id,
        "period_start": start_date,
        "period_end": end_date,
        "days": days,
        "cycle_total": cycle_total,
        "coverage": {
            "present_days": present_days,
            "missing_days": total_days - present_days,
        },
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0
