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


class UsageLedgerTests(unittest.TestCase):
    """End-to-end checks for usage recording, listing and summarisation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.env = {**os.environ, "BILLING_LEDGER_DB": str(self.db_path)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

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
        self, customer_id: str = "c1", plan: str = "basic", start_date: str = "2026-01-01"
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", "990",
            "--start-date", start_date,
        )

    def test_record_requires_existing_subscription(self) -> None:
        result = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-01-10", "--quantity", "5",
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._usage_row_count(), 0)

    def _usage_row_count(self) -> int:
        import sqlite3

        if not self.db_path.exists():
            return 0
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_records'"
            ).fetchall()
            if not rows:
                return 0
            return int(connection.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0])

    def test_record_validates_date_and_start_date(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        bad_date = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-02-30", "--quantity", "1",
        )
        self.assertEqual(bad_date.returncode, 2)
        self.assertEqual(bad_date.stdout, "")

        before_start = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2025-12-31", "--quantity", "1",
        )
        self.assertEqual(before_start.returncode, 2)
        self.assertIn("start_date", before_start.stderr)
        self.assertEqual(before_start.stdout, "")

    def test_record_and_query_flow(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        first = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-01-10", "--quantity", "5",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        record = json.loads(first.stdout)
        self.assertEqual(
            record,
            {"id": 1, "customer_id": "c1", "plan": "basic",
             "usage_date": "2026-01-10", "quantity": 5},
        )
        self.assertEqual(len(first.stdout.splitlines()), 1)

        # Same subscription/date in a separate submission is allowed.
        second = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-01-10", "--quantity", "3",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["id"], 2)

        third = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-01-11", "--quantity", "0",
        )
        self.assertEqual(third.returncode, 0, third.stderr)

        listing = self.invoke("usage", "list", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(
            json.loads(listing.stdout),
            [
                {"id": 1, "usage_date": "2026-01-10", "quantity": 5},
                {"id": 2, "usage_date": "2026-01-10", "quantity": 3},
                {"id": 3, "usage_date": "2026-01-11", "quantity": 0},
            ],
        )
        self.assertEqual(len(listing.stdout.splitlines()), 1)

        summary = self.invoke("usage", "summary", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertEqual(
            json.loads(summary.stdout),
            [
                {"usage_date": "2026-01-10", "total": 8},
                {"usage_date": "2026-01-11", "total": 0},
            ],
        )

    def test_duplicate_dates_in_one_submission_are_rejected_atomically(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        result = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-01-11", "--quantity", "2",
            "--usage-date", "2026-01-12", "--quantity", "4",
            "--usage-date", "2026-01-11", "--quantity", "7",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("duplicate", result.stderr)
        self.assertEqual(result.stdout, "")

        listing = self.invoke("usage", "list", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(json.loads(listing.stdout), [])

    def test_summary_date_range_is_inclusive_and_validated(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        for day, quantity in [("2026-01-10", 5), ("2026-01-11", 3), ("2026-01-12", 2)]:
            result = self.invoke(
                "usage", "record", "--customer-id", "c1", "--plan", "basic",
                "--usage-date", day, "--quantity", str(quantity),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        ranged = self.invoke(
            "usage", "summary", "--customer-id", "c1", "--plan", "basic",
            "--start-date", "2026-01-11", "--end-date", "2026-01-12",
        )
        self.assertEqual(ranged.returncode, 0, ranged.stderr)
        self.assertEqual(
            json.loads(ranged.stdout),
            [
                {"usage_date": "2026-01-11", "total": 3},
                {"usage_date": "2026-01-12", "total": 2},
            ],
        )

        empty = self.invoke(
            "usage", "summary", "--customer-id", "c1", "--plan", "basic",
            "--start-date", "2026-02-01", "--end-date", "2026-02-28",
        )
        self.assertEqual(json.loads(empty.stdout), [])

        bad_range = self.invoke(
            "usage", "summary", "--customer-id", "c1", "--plan", "basic",
            "--start-date", "2026-01-12", "--end-date", "2026-01-10",
        )
        self.assertEqual(bad_range.returncode, 2)
        self.assertEqual(bad_range.stdout, "")

        bad_date = self.invoke(
            "usage", "summary", "--customer-id", "c1", "--plan", "basic",
            "--start-date", "not-a-date",
        )
        self.assertEqual(bad_date.returncode, 2)

    def test_record_ids_are_global_and_monotonic_across_subscriptions(self) -> None:
        self.assertEqual(self.create_subscription("c1", "basic", "2026-01-01").returncode, 0)
        self.assertEqual(self.create_subscription("c2", "pro", "2026-01-01").returncode, 0)

        first = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", "2026-01-10", "--quantity", "1",
        )
        second = self.invoke(
            "usage", "record", "--customer-id", "c2", "--plan", "pro",
            "--usage-date", "2026-01-10", "--quantity", "1",
        )
        self.assertEqual(json.loads(first.stdout)["id"], 1)
        self.assertEqual(json.loads(second.stdout)["id"], 2)

        # Queries are scoped to the given subscription.
        c1_list = self.invoke("usage", "list", "--customer-id", "c1", "--plan", "basic")
        c2_list = self.invoke("usage", "list", "--customer-id", "c2", "--plan", "pro")
        self.assertEqual([r["id"] for r in json.loads(c1_list.stdout)], [1])
        self.assertEqual([r["id"] for r in json.loads(c2_list.stdout)], [2])

    def test_querying_unknown_subscription_exits_4(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        listing = self.invoke("usage", "list", "--customer-id", "ghost", "--plan", "basic")
        summary = self.invoke("usage", "summary", "--customer-id", "ghost", "--plan", "basic")
        self.assertEqual(listing.returncode, 4)
        self.assertEqual(summary.returncode, 4)
        self.assertEqual(listing.stdout, "")
        self.assertEqual(summary.stdout, "")


class BillingReconcileTests(unittest.TestCase):
    """End-to-end checks for the billing-cycle reconciliation detail."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.env = {**os.environ, "BILLING_LEDGER_DB": str(self.db_path)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "billing_ledger", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=self.env,
        )

    def reconcile(self, customer_id: str, plan: str, start: str, end: str):
        return self.invoke(
            "billing", "reconcile",
            "--customer-id", customer_id, "--plan", plan,
            "--start-date", start, "--end-date", end,
        )

    def create_subscription(self, customer_id: str = "c1", plan: str = "basic"):
        return self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", "990",
            "--start-date", "2026-01-05",
        )

    def record(self, day: str, quantity: str, customer_id: str = "c1", plan: str = "basic"):
        return self.invoke(
            "usage", "record",
            "--customer-id", customer_id, "--plan", plan,
            "--usage-date", day, "--quantity", quantity,
        )

    def test_full_period_report_shape_and_totals(self) -> None:
        create = self.create_subscription()
        self.assertEqual(create.returncode, 0, create.stderr)
        subscription_id = json.loads(create.stdout)["id"]

        for day, quantity in [("2026-01-06", "5"), ("2026-01-06", "3"),
                             ("2026-01-08", "0"), ("2026-01-10", "2")]:
            result = self.record(day, quantity)
            self.assertEqual(result.returncode, 0, result.stderr)

        result = self.reconcile("c1", "basic", "2026-01-04", "2026-01-11")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)

        self.assertEqual(
            report,
            {
                "subscription_id": subscription_id,
                "period_start": "2026-01-04",
                "period_end": "2026-01-11",
                "days": [
                    {"usage_date": "2026-01-04", "total": 0, "missing": True},
                    {"usage_date": "2026-01-05", "total": 0, "missing": True},
                    {"usage_date": "2026-01-06", "total": 8, "missing": False},
                    {"usage_date": "2026-01-07", "total": 0, "missing": True},
                    # A zero-quantity record still makes the day present.
                    {"usage_date": "2026-01-08", "total": 0, "missing": False},
                    {"usage_date": "2026-01-09", "total": 0, "missing": True},
                    {"usage_date": "2026-01-10", "total": 2, "missing": False},
                    {"usage_date": "2026-01-11", "total": 0, "missing": True},
                ],
                "cycle_total": 10,
                "coverage": {"present_days": 3, "missing_days": 5},
            },
        )

        dates = [day["usage_date"] for day in report["days"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(
            report["coverage"]["present_days"] + report["coverage"]["missing_days"],
            len(report["days"]),
        )
        self.assertEqual(
            report["cycle_total"], sum(day["total"] for day in report["days"])
        )

    def test_dates_before_subscription_start_are_listed_as_missing(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        result = self.reconcile("c1", "basic", "2025-12-31", "2026-01-05")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["days"]), 6)
        self.assertEqual(report["days"][0],
                         {"usage_date": "2025-12-31", "total": 0, "missing": True})
        self.assertTrue(all(day["missing"] for day in report["days"]))
        self.assertEqual(report["cycle_total"], 0)
        self.assertEqual(report["coverage"], {"present_days": 0, "missing_days": 6})

    def test_single_day_period_inclusive_endpoints(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        present = self.reconcile("c1", "basic", "2026-01-06", "2026-01-06")
        self.assertEqual(present.returncode, 0, present.stderr)
        report = json.loads(present.stdout)
        self.assertEqual(
            report["days"],
            [{"usage_date": "2026-01-06", "total": 0, "missing": True}],
        )
        self.assertEqual(report["coverage"], {"present_days": 0, "missing_days": 1})

        self.assertEqual(self.record("2026-01-06", "7").returncode, 0)
        filled = self.reconcile("c1", "basic", "2026-01-06", "2026-01-06")
        report = json.loads(filled.stdout)
        self.assertEqual(
            report["days"],
            [{"usage_date": "2026-01-06", "total": 7, "missing": False}],
        )
        self.assertEqual(report["cycle_total"], 7)
        self.assertEqual(report["coverage"], {"present_days": 1, "missing_days": 0})

    def test_invalid_dates_and_reversed_range_exit_2(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        for start, end in [
            ("not-a-date", "2026-01-10"),
            ("2026-02-30", "2026-01-10"),
            ("2026-01-01", "2026-13-01"),
            ("2026-01-10", "2026-01-01"),
        ]:
            with self.subTest(start=start, end=end):
                result = self.reconcile("c1", "basic", start, end)
                self.assertEqual(result.returncode, 2)
                self.assertNotEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")

    def test_unknown_subscription_exits_4(self) -> None:
        result = self.reconcile("ghost", "basic", "2026-01-01", "2026-01-31")
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_query_is_scoped_to_subscription(self) -> None:
        self.assertEqual(self.create_subscription("c1", "basic").returncode, 0)
        self.assertEqual(self.create_subscription("c2", "pro").returncode, 0)
        self.assertEqual(self.record("2026-01-06", "9", "c1", "basic").returncode, 0)

        c1 = self.reconcile("c1", "basic", "2026-01-05", "2026-01-07")
        c2 = self.reconcile("c2", "pro", "2026-01-05", "2026-01-07")
        self.assertEqual(json.loads(c1.stdout)["cycle_total"], 9)
        self.assertEqual(json.loads(c2.stdout)["cycle_total"], 0)
        self.assertTrue(all(day["missing"] for day in json.loads(c2.stdout)["days"]))

    def test_query_is_read_only(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        result = self.reconcile("c1", "basic", "2026-01-01", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)

        import sqlite3

        with sqlite3.connect(self.db_path) as connection:
            usage_count = int(
                connection.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertEqual(usage_count, 0)
        self.assertEqual(tables, {"subscriptions", "usage_records", "sqlite_sequence"})


if __name__ == "__main__":
    unittest.main()
