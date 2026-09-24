"""Checks for the documented command-line entry point."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "billing_ledger", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_and_no_arguments(self) -> None:
        for arguments in [(), ("--help",)]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--help", result.stdout)
                self.assertIn("--version", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_version(self) -> None:
        result = self.invoke("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "billing-ledger 0.1.0")
        self.assertEqual(result.stderr, "")

    def test_unknown_argument_is_an_error(self) -> None:
        result = self.invoke("--unknown-option")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--unknown-option", result.stderr)
        self.assertEqual(result.stdout, "")


class UsageCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            **os.environ,
            "BILLING_LEDGER_DB": str(Path(self.tmp.name) / "ledger.db"),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "billing_ledger", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=self.env,
        )

    def create_subscription(
        self, customer_id: str = "cust-1", plan: str = "pro", start_date: str = "2026-01-01"
    ) -> None:
        result = self.invoke(
            "subscription",
            "create",
            "--customer-id",
            customer_id,
            "--plan",
            plan,
            "--price-cents",
            "100",
            "--start-date",
            start_date,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def record_usage(
        self,
        customer_id: str = "cust-1",
        plan: str = "pro",
        usage_date: str = "2026-02-01",
        quantity: str = "5",
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "usage",
            "record",
            "--customer-id",
            customer_id,
            "--plan",
            plan,
            "--usage-date",
            usage_date,
            "--quantity",
            quantity,
        )

    def list_usage(self, customer_id: str = "cust-1", plan: str = "pro"):
        result = self.invoke("usage", "list", "--customer-id", customer_id, "--plan", plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_record_outputs_compact_json_and_allocates_monotonic_ids(self) -> None:
        self.create_subscription()
        first = self.record_usage(usage_date="2026-02-01", quantity="5")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(
            first.stdout.strip(),
            '{"id":1,"customer_id":"cust-1","plan":"pro","usage_date":"2026-02-01","quantity":5}',
        )
        second = self.record_usage(usage_date="2026-02-02", quantity="7")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["id"], 2)

    def test_record_rejects_invalid_usage_date(self) -> None:
        self.create_subscription()
        for bad_date in ["2026-13-01", "2026/02/01", "not-a-date"]:
            with self.subTest(bad_date=bad_date):
                result = self.record_usage(usage_date=bad_date)
                self.assertEqual(result.returncode, 2)
                self.assertNotEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.list_usage(), [])

    def test_record_rejects_date_before_subscription_start(self) -> None:
        self.create_subscription(start_date="2026-03-01")
        result = self.record_usage(usage_date="2026-02-28")
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(result.stderr, "")
        self.assertEqual(self.list_usage(), [])

    def test_record_rejects_unknown_subscription(self) -> None:
        result = self.record_usage()
        self.assertEqual(result.returncode, 4)
        self.assertNotEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_record_allows_same_date_across_submissions(self) -> None:
        self.create_subscription()
        self.assertEqual(self.record_usage(usage_date="2026-02-01", quantity="5").returncode, 0)
        self.assertEqual(self.record_usage(usage_date="2026-02-01", quantity="3").returncode, 0)
        self.assertEqual(
            self.list_usage(),
            [
                {"id": 1, "usage_date": "2026-02-01", "quantity": 5},
                {"id": 2, "usage_date": "2026-02-01", "quantity": 3},
            ],
        )

    def test_list_requires_existing_subscription(self) -> None:
        result = self.invoke("usage", "list", "--customer-id", "ghost", "--plan", "pro")
        self.assertEqual(result.returncode, 4)
        self.assertNotEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_summary_sums_per_day_and_filters_by_range(self) -> None:
        self.create_subscription()
        for usage_date, quantity in [
            ("2026-02-01", "5"),
            ("2026-02-01", "3"),
            ("2026-02-03", "2"),
            ("2026-02-05", "9"),
        ]:
            self.assertEqual(
                self.record_usage(usage_date=usage_date, quantity=quantity).returncode, 0
            )

        all_days = self.invoke("usage", "summary", "--customer-id", "cust-1", "--plan", "pro")
        self.assertEqual(all_days.returncode, 0, all_days.stderr)
        self.assertEqual(
            json.loads(all_days.stdout),
            [
                {"usage_date": "2026-02-01", "total": 8},
                {"usage_date": "2026-02-03", "total": 2},
                {"usage_date": "2026-02-05", "total": 9},
            ],
        )

        ranged = self.invoke(
            "usage",
            "summary",
            "--customer-id",
            "cust-1",
            "--plan",
            "pro",
            "--start-date",
            "2026-02-01",
            "--end-date",
            "2026-02-03",
        )
        self.assertEqual(ranged.returncode, 0, ranged.stderr)
        self.assertEqual(
            json.loads(ranged.stdout),
            [
                {"usage_date": "2026-02-01", "total": 8},
                {"usage_date": "2026-02-03", "total": 2},
            ],
        )

    def test_summary_rejects_invalid_range(self) -> None:
        self.create_subscription()
        for extra in [
            ("--start-date", "2026-02-30"),
            ("--end-date", "not-a-date"),
            ("--start-date", "2026-02-10", "--end-date", "2026-02-01"),
        ]:
            with self.subTest(extra=extra):
                result = self.invoke(
                    "usage", "summary", "--customer-id", "cust-1", "--plan", "pro", *extra
                )
                self.assertEqual(result.returncode, 2)
                self.assertNotEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")

    def test_summary_requires_existing_subscription(self) -> None:
        result = self.invoke("usage", "summary", "--customer-id", "ghost", "--plan", "pro")
        self.assertEqual(result.returncode, 4)
        self.assertNotEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
