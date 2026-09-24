"""Billing-cycle attribution querying.

Read-only projection of subscription calendar semantics onto an arbitrary
inclusive date range: every day is attributed to a billing cycle (cycle 0 for
days outside any billed period), flagged as trial-covered, and priced at the
subscription ``price_cents`` when billable.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

from .subscriptions import DB_PATH, _connect, _parse_start_date

CYCLE_LENGTH_DAYS = 30


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def attribution(
    customer_id: str,
    plan: str,
    start_date: str,
    end_date: str,
) -> int:
    """Attribute every day in ``[start_date, end_date]`` to a billing cycle.

    ``billing_start`` is the subscription start plus ``trial_days``; the trial
    window is ``[start_date, billing_start - 1]`` and the first billed cycle is
    ``[billing_start, billing_start + 29]`` with further 30-day cycles after.
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
    if parsed_start > parsed_end:
        return _fail(
            f"--start-date must not be later than --end-date: {start_date} > {end_date}"
        )

    # Read-only: a missing ledger means the subscription cannot exist, and we
    # must not create a database file just to answer a query.
    if not DB_PATH.exists():
        print(
            f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
            file=sys.stderr,
        )
        return 4

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT id, start_date, trial_days, price_cents FROM subscriptions"
            " WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        print(
            f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
            file=sys.stderr,
        )
        return 4

    subscription_id = int(row[0])
    subscription_start = _parse_start_date(str(row[1]))
    trial_days = int(row[2])
    price_cents = int(row[3])

    billing_start = subscription_start + timedelta(days=trial_days)
    if trial_days > 0:
        trial_start = subscription_start.isoformat()
        trial_end = (billing_start - timedelta(days=1)).isoformat()
    else:
        trial_start = None
        trial_end = None

    days: list[dict[str, object]] = []
    cycle_total = 0
    current = parsed_start
    while current <= parsed_end:
        usage_date = current.isoformat()
        if current < subscription_start:
            cycle_no = 0
            is_trial = False
            amount = 0
        elif trial_days > 0 and current < billing_start:
            cycle_no = 0
            is_trial = True
            amount = 0
        else:
            cycle_no = (current - billing_start).days // CYCLE_LENGTH_DAYS + 1
            is_trial = False
            amount = price_cents
        cycle_total += amount
        days.append(
            {
                "usage_date": usage_date,
                "cycle_no": cycle_no,
                "is_trial": is_trial,
                "amount_cents": amount,
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
        "cycle_total_cents": cycle_total,
    }
    print(json.dumps(detail, ensure_ascii=False, separators=(",", ":")))
    return 0
