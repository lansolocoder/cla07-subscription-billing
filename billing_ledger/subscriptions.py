"""Subscription registration and querying backed by a SQLite ledger."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _default_db_path() -> Path:
    override = os.environ.get("BILLING_LEDGER_DB")
    return Path(override) if override else Path(__file__).resolve().parents[1] / "ledger.db"


DB_PATH = _default_db_path()

_DATE_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    trial_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    trial_end_date TEXT,
    trial_to_active_date TEXT,
    UNIQUE (customer_id, plan)
)
"""

# Columns added after the original schema; values are backfilled for old rows.
_ADDED_COLUMNS = ("trial_end_date", "trial_to_active_date")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute(_SCHEMA)
    existing = {row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")}
    for column in _ADDED_COLUMNS:
        if column not in existing:
            connection.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} TEXT")
    if "trial_to_active_date" not in existing:
        # Rows created before trial dates were tracked: trial_days=0 means the
        # subscription was active from start_date; positive trial_days means a
        # trial interval of [start_date, start_date+trial_days-1].
        connection.execute(
            "UPDATE subscriptions SET trial_to_active_date ="
            " date(start_date, '+' || trial_days || ' days')"
            " WHERE trial_to_active_date IS NULL"
        )
    if "trial_end_date" not in existing:
        connection.execute(
            "UPDATE subscriptions SET trial_end_date ="
            " date(start_date, '+' || (trial_days - 1) || ' days')"
            " WHERE trial_days > 0 AND trial_end_date IS NULL"
        )
    connection.commit()
    return connection


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _parse_start_date(raw: str) -> date | None:
    if not _DATE_FORMAT.match(raw):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _record(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row[0],
        "customer_id": row[1],
        "plan": row[2],
        "price_cents": row[3],
        "start_date": row[4],
        "trial_days": row[5],
        "status": row[6],
    }


def create(
    customer_id: str,
    plan: str,
    price_cents: int,
    start_date: str,
    trial_days: int | None = None,
    trial_end_date: str | None = None,
) -> int:
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if price_cents < 0:
        return _fail("--price-cents must be a non-negative integer")
    if trial_days is not None and trial_days < 0:
        return _fail("--trial-days must be a non-negative integer")
    if trial_days is not None and trial_end_date is not None:
        return _fail("--trial-days and --trial-end-date are mutually exclusive")
    parsed_date = _parse_start_date(start_date)
    if parsed_date is None:
        return _fail(f"--start-date is not a valid YYYY-MM-DD date: {start_date}")
    today = datetime.now(timezone.utc).date()
    if parsed_date > today:
        return _fail(f"--start-date must not be later than today (UTC): {start_date}")

    if trial_end_date is not None:
        parsed_end = _parse_start_date(trial_end_date)
        if parsed_end is None:
            return _fail(
                f"--trial-end-date is not a valid YYYY-MM-DD date: {trial_end_date}"
            )
        if parsed_end < parsed_date:
            return _fail(
                "--trial-end-date must not be earlier than --start-date: "
                f"{trial_end_date} < {start_date}"
            )
        if parsed_end < today:
            return _fail(
                "--trial-end-date must not be earlier than today (UTC): "
                f"{trial_end_date}"
            )
        computed_trial_days = (parsed_end - parsed_date).days + 1
        trial_to_active_date = (parsed_end + timedelta(days=1)).isoformat()
        status = "trial"
    elif trial_days:
        computed_trial_days = trial_days
        trial_end_date = (parsed_date + timedelta(days=trial_days - 1)).isoformat()
        trial_to_active_date = (parsed_date + timedelta(days=trial_days)).isoformat()
        status = "trial"
    else:
        computed_trial_days = 0
        trial_end_date = None
        trial_to_active_date = start_date
        status = "active"

    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO subscriptions (customer_id, plan, price_cents, start_date,"
            " trial_days, status, trial_end_date, trial_to_active_date)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                plan,
                price_cents,
                start_date,
                computed_trial_days,
                status,
                trial_end_date,
                trial_to_active_date,
            ),
        )
        connection.commit()
        subscription_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        connection.rollback()
        print(
            f"billing-ledger: duplicate subscription for customer_id={customer_id} plan={plan}",
            file=sys.stderr,
        )
        return 3
    finally:
        connection.close()

    record = {
        "id": subscription_id,
        "customer_id": customer_id,
        "plan": plan,
        "price_cents": price_cents,
        "start_date": start_date,
        "trial_days": computed_trial_days,
        "status": status,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


def list_all() -> int:
    records = []
    if DB_PATH.exists():
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id, customer_id, plan, price_cents, start_date, trial_days, status"
                " FROM subscriptions ORDER BY start_date ASC, id DESC"
            ).fetchall()
        finally:
            connection.close()
        records = [_record(row) for row in rows]
    print(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    return 0


def _trial_view(row: tuple) -> dict:
    return {
        "customer_id": row[0],
        "plan": row[1],
        "start_date": row[2],
        "trial_days": int(row[3]),
        "trial_end_date": row[4],
        "trial_to_active_date": row[5],
        "status": row[6],
    }


def get(customer_id: str, plan: str) -> int:
    """Print the trial information of one subscription as one compact JSON line."""
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT customer_id, plan, start_date, trial_days, trial_end_date,"
            " trial_to_active_date, status FROM subscriptions"
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

    print(json.dumps(_trial_view(row), ensure_ascii=False, separators=(",", ":")))
    return 0


def activate(customer_id: str, plan: str, as_of: str | None = None) -> int:
    """Convert a trial subscription to active status.

    The conversion takes effect on ``as_of`` (defaults to today UTC); it is
    rejected while ``as_of`` is earlier than the persisted conversion date.
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")

    if as_of is None:
        parsed_as_of = datetime.now(timezone.utc).date()
        as_of = parsed_as_of.isoformat()
    else:
        parsed_as_of = _parse_start_date(as_of)
        if parsed_as_of is None:
            return _fail(f"--as-of is not a valid YYYY-MM-DD date: {as_of}")

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT trial_to_active_date FROM subscriptions"
            " WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if row is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4
        conversion_date = _parse_start_date(str(row[0]))
        if parsed_as_of < conversion_date:
            return _fail(
                f"--as-of must not be earlier than trial_to_active_date {row[0]}: {as_of}"
            )
        connection.execute(
            "UPDATE subscriptions SET status = 'active'"
            " WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        )
        connection.commit()
    finally:
        connection.close()

    result = {
        "customer_id": customer_id,
        "plan": plan,
        "status": "active",
        "activated_on": as_of,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def modify(
    customer_id: str,
    plan: str,
    plan_new: str | None = None,
    price_cents: int | None = None,
    trial_end_date: str | None = None,
) -> int:
    """Change the plan, price and/or trial end date of a trial subscription.

    All supplied changes take effect atomically; only ``trial`` subscriptions
    may be changed. Error precedence (first hit wins): invalid arguments or
    dates, changing an ``active`` subscription, a new trial end date earlier
    than the current one (all exit 2); then subscription not found or a plan
    rename colliding with an existing (customer_id, plan) pair (exit 4).
    """
    if not customer_id:
        return _fail("--customer-id must not be empty")
    if not plan:
        return _fail("--plan must not be empty")
    if plan_new is None and price_cents is None and trial_end_date is None:
        return _fail(
            "at least one of --plan-new, --price-cents or --trial-end-date is required"
        )
    if plan_new is not None and not plan_new:
        return _fail("--plan-new must not be empty")
    if plan_new is not None and plan_new == plan:
        return _fail("--plan-new must differ from the current plan")
    if price_cents is not None and price_cents < 0:
        return _fail("--price-cents must be a non-negative integer")

    parsed_new_end: date | None = None
    if trial_end_date is not None:
        parsed_new_end = _parse_start_date(trial_end_date)
        if parsed_new_end is None:
            return _fail(
                f"--trial-end-date is not a valid YYYY-MM-DD date: {trial_end_date}"
            )

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT id, price_cents, start_date, status, trial_days,"
            " trial_end_date, trial_to_active_date"
            " FROM subscriptions WHERE customer_id = ? AND plan = ?",
            (customer_id, plan),
        ).fetchone()
        if row is None:
            print(
                f"billing-ledger: no subscription found for customer_id={customer_id} plan={plan}",
                file=sys.stderr,
            )
            return 4

        subscription_id = int(row[0])
        effective_price_cents = price_cents if price_cents is not None else int(row[1])
        start_date = _parse_start_date(str(row[2]))
        status = str(row[3])
        effective_trial_days = int(row[4])
        effective_trial_end = row[5]
        effective_trial_to_active = row[6]
        old_trial_end = _parse_start_date(str(row[5]))

        if status != "trial":
            return _fail(f"only trial subscriptions can be modified; status is {status}")

        if parsed_new_end is not None:
            if start_date is None:
                return _fail(f"stored start_date is not a valid YYYY-MM-DD date: {row[2]}")
            today = datetime.now(timezone.utc).date()
            if parsed_new_end < start_date:
                return _fail(
                    "--trial-end-date must not be earlier than --start-date: "
                    f"{trial_end_date} < {row[2]}"
                )
            if parsed_new_end < today:
                return _fail(
                    "--trial-end-date must not be earlier than today (UTC): "
                    f"{trial_end_date}"
                )
            if old_trial_end is None:
                return _fail("stored trial_end_date is missing for a trial subscription")
            if parsed_new_end < old_trial_end:
                return _fail(
                    "--trial-end-date must not be earlier than the current trial_end_date: "
                    f"{trial_end_date} < {old_trial_end.isoformat()}"
                )
            effective_trial_days = (parsed_new_end - start_date).days + 1
            effective_trial_end = trial_end_date
            effective_trial_to_active = (parsed_new_end + timedelta(days=1)).isoformat()

        effective_plan = plan_new if plan_new is not None else plan
        if plan_new is not None:
            collision = connection.execute(
                "SELECT 1 FROM subscriptions WHERE customer_id = ? AND plan = ? AND id <> ?",
                (customer_id, plan_new, subscription_id),
            ).fetchone()
            if collision is not None:
                print(
                    "billing-ledger: a subscription already exists for"
                    f" customer_id={customer_id} plan={plan_new}",
                    file=sys.stderr,
                )
                return 4

        try:
            connection.execute(
                "UPDATE subscriptions SET plan = ?, price_cents = ?, trial_days = ?,"
                " trial_end_date = ?, trial_to_active_date = ? WHERE id = ?",
                (
                    effective_plan,
                    effective_price_cents,
                    effective_trial_days,
                    effective_trial_end,
                    effective_trial_to_active,
                    subscription_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        updated = connection.execute(
            "SELECT customer_id, plan, trial_days, trial_end_date,"
            " trial_to_active_date, status FROM subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "customer_id": updated[0],
                "plan": updated[1],
                "trial_days": int(updated[2]),
                "trial_end_date": updated[3],
                "trial_to_active_date": updated[4],
                "status": updated[5],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0
