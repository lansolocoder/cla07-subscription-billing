"""Checks for subscription registration and listing."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "ledger.db"


class SubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._backup = None
        if DB_PATH.exists():
            self._backup = DB_PATH.read_bytes()
            DB_PATH.unlink()

    def tearDown(self) -> None:
        if DB_PATH.exists():
            DB_PATH.unlink()
        if self._backup is not None:
            DB_PATH.write_bytes(self._backup)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "billing_ledger", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def create(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.invoke("subscription", "create", *arguments)

    def test_create_trial_subscription(self) -> None:
        result = self.create(
            "--customer-id", "cust-1",
            "--plan", "pro",
            "--price-cents", "1999",
            "--start-date", "2026-01-15",
            "--trial-days", "14",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)
        self.assertEqual(
            record,
            {
                "id": 1,
                "customer_id": "cust-1",
                "plan": "pro",
                "price_cents": 1999,
                "start_date": "2026-01-15",
                "trial_days": 14,
                "status": "trial",
            },
        )
        self.assertEqual(
            set(record),
            {
                "id",
                "customer_id",
                "plan",
                "price_cents",
                "start_date",
                "trial_days",
                "status",
            },
        )

    def test_create_active_subscription_defaults_trial_days_to_zero(self) -> None:
        result = self.create(
            "--customer-id", "cust-2",
            "--plan", "basic",
            "--price-cents", "0",
            "--start-date", "2026-01-15",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)
        self.assertEqual(record["trial_days"], 0)
        self.assertEqual(record["status"], "active")

    def test_create_persists_and_list_reads_back(self) -> None:
        first = self.create(
            "--customer-id", "cust-1",
            "--plan", "pro",
            "--price-cents", "1999",
            "--start-date", "2026-03-01",
            "--trial-days", "7",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.create(
            "--customer-id", "cust-2",
            "--plan", "basic",
            "--price-cents", "500",
            "--start-date", "2026-02-01",
        )
        self.assertEqual(second.returncode, 0, second.stderr)

        result = self.invoke("subscription", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        records = json.loads(result.stdout)
        self.assertEqual([record["start_date"] for record in records], [
            "2026-02-01",
            "2026-03-01",
        ])
        self.assertEqual(records[1]["customer_id"], "cust-1")
        self.assertEqual(records[1]["status"], "trial")
        self.assertEqual(records[0]["status"], "active")

    def test_list_empty_outputs_empty_array(self) -> None:
        result = self.invoke("subscription", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertFalse(DB_PATH.exists())

    def test_list_orders_by_start_date_then_id_descending(self) -> None:
        # Inserted intentionally out of order; same-date ids ascend on insert.
        for customer_id, start_date in [
            ("a", "2026-05-01"),
            ("b", "2026-03-01"),
            ("c", "2026-03-01"),
            ("d", "2026-04-01"),
        ]:
            result = self.create(
                "--customer-id", customer_id,
                "--plan", "p",
                "--price-cents", "100",
                "--start-date", start_date,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        result = self.invoke("subscription", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual(
            [(record["start_date"], record["customer_id"]) for record in records],
            [
                ("2026-03-01", "c"),  # same date: higher id first
                ("2026-03-01", "b"),
                ("2026-04-01", "d"),
                ("2026-05-01", "a"),
            ],
        )

    def test_duplicate_registration_is_rejected(self) -> None:
        first = self.create(
            "--customer-id", "cust-9",
            "--plan", "pro",
            "--price-cents", "1999",
            "--start-date", "2026-01-01",
            "--trial-days", "30",
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        duplicate = self.create(
            "--customer-id", "cust-9",
            "--plan", "pro",
            "--price-cents", "1",
            "--start-date", "2026-02-02",
            "--trial-days", "0",
        )
        self.assertEqual(duplicate.returncode, 3)
        self.assertEqual(duplicate.stdout, "")
        self.assertIn("cust-9", duplicate.stderr)
        self.assertIn("pro", duplicate.stderr)

        result = self.invoke("subscription", "list")
        records = json.loads(result.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["price_cents"], 1999)
        self.assertEqual(records[0]["start_date"], "2026-01-01")
        self.assertEqual(records[0]["trial_days"], 30)
        self.assertEqual(records[0]["status"], "trial")

    def test_future_start_date_is_rejected_without_writing(self) -> None:
        tomorrow = (
            datetime.now(timezone.utc).date() + timedelta(days=1)
        ).isoformat()
        result = self.create(
            "--customer-id", "cust-future",
            "--plan", "pro",
            "--price-cents", "100",
            "--start-date", tomorrow,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(tomorrow, result.stderr)
        self.assertFalse(DB_PATH.exists())

        listing = self.invoke("subscription", "list")
        self.assertEqual(listing.stdout.strip(), "[]")

    def test_today_start_date_is_accepted(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        result = self.create(
            "--customer-id", "cust-now",
            "--plan", "pro",
            "--price-cents", "100",
            "--start-date", today,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_inputs(self) -> None:
        cases = [
            ("bad date", ["--start-date", "2026-02-30"]),
            ("wrong format", ["--start-date", "2026/01/01"]),
            ("unpadded date", ["--start-date", "2026-1-1"]),
            ("negative price", ["--price-cents", "-1"]),
            ("non-integer price", ["--price-cents", "abc"]),
            ("empty customer", ["--customer-id", ""]),
            ("empty plan", ["--plan", ""]),
            ("non-integer trial days", ["--trial-days", "x"]),
        ]
        base = [
            "--customer-id", "cust-x",
            "--plan", "plan-x",
            "--price-cents", "100",
            "--start-date", "2026-01-01",
        ]
        for label, overrides in cases:
            with self.subTest(label):
                arguments = base.copy()
                arguments.extend(overrides)
                result = self.create(*arguments)
                self.assertEqual(result.returncode, 2, label)
                self.assertEqual(result.stdout, "", label)
                self.assertNotEqual(result.stderr, "", label)
                self.assertFalse(DB_PATH.exists(), label)

    def test_subcommands_visible_in_help(self) -> None:
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("subscription", result.stdout)

        result = self.invoke("subscription", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create", result.stdout)
        self.assertIn("list", result.stdout)


if __name__ == "__main__":
    unittest.main()
