"""SQLite-backed persistence for subscription records."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DATABASE_PATH = Path(__file__).resolve().parent.parent / "ledger.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    trial_days INTEGER NOT NULL,
    UNIQUE (customer_id, plan)
)
"""


class DuplicateSubscriptionError(Exception):
    """Raised when a (customer_id, plan) pair is already registered."""


def _to_record(row: tuple[int, str, str, int, str, int]) -> dict[str, object]:
    return {
        "id": row[0],
        "customer_id": row[1],
        "plan": row[2],
        "price_cents": row[3],
        "start_date": row[4],
        "trial_days": row[5],
        "status": "trial" if row[5] > 0 else "active",
    }


def add_subscription(
    customer_id: str,
    plan: str,
    price_cents: int,
    start_date: str,
    trial_days: int,
) -> dict[str, object]:
    """Insert a subscription and return it as a record.

    Raises DuplicateSubscriptionError when the (customer_id, plan) pair
    already exists; the stored record is left untouched.
    """
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(_SCHEMA)
        try:
            cursor = connection.execute(
                """
                INSERT INTO subscriptions
                    (customer_id, plan, price_cents, start_date, trial_days)
                VALUES (?, ?, ?, ?, ?)
                """,
                (customer_id, plan, price_cents, start_date, trial_days),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateSubscriptionError(
                f"duplicate subscription: customer_id={customer_id!r} plan={plan!r}"
            ) from exc
        row = connection.execute(
            "SELECT id, customer_id, plan, price_cents, start_date, trial_days"
            " FROM subscriptions WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return _to_record(row)


def list_subscriptions() -> list[dict[str, object]]:
    """Return every subscription ordered by start_date asc, then id desc."""
    if not DATABASE_PATH.exists():
        return []
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(_SCHEMA)
        rows = connection.execute(
            "SELECT id, customer_id, plan, price_cents, start_date, trial_days"
            " FROM subscriptions ORDER BY start_date ASC, id DESC"
        ).fetchall()
    return [_to_record(row) for row in rows]
