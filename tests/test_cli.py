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


class ReconcileTests(unittest.TestCase):
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

    def record(self, usage_date: str, quantity: int) -> None:
        result = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", usage_date, "--quantity", str(quantity),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def reconcile(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.invoke("usage", "reconcile", "--customer-id", "c1", "--plan", "basic", *extra)

    def test_reconcile_fills_every_day_and_marks_missing(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        self.record("2026-01-10", 3)
        self.record("2026-01-12", 0)

        result = self.reconcile("--start-date", "2026-01-09", "--end-date", "2026-01-13")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "subscription_id": 1,
                "period_start": "2026-01-09",
                "period_end": "2026-01-13",
                "days": [
                    {"usage_date": "2026-01-09", "total": 0, "missing": True},
                    {"usage_date": "2026-01-10", "total": 8, "missing": False},
                    {"usage_date": "2026-01-11", "total": 0, "missing": True},
                    {"usage_date": "2026-01-12", "total": 0, "missing": False},
                    {"usage_date": "2026-01-13", "total": 0, "missing": True},
                ],
                "cycle_total": 8,
                "coverage": {"present_days": 2, "missing_days": 3},
            },
        )

    def test_reconcile_is_not_bound_by_subscription_start_date(self) -> None:
        self.assertEqual(self.create_subscription(start_date="2026-01-05").returncode, 0)
        self.record("2026-01-05", 2)

        result = self.reconcile("--start-date", "2026-01-03", "--end-date", "2026-01-05")
        self.assertEqual(result.returncode, 0, result.stderr)
        detail = json.loads(result.stdout)
        self.assertEqual(
            detail["days"],
            [
                {"usage_date": "2026-01-03", "total": 0, "missing": True},
                {"usage_date": "2026-01-04", "total": 0, "missing": True},
                {"usage_date": "2026-01-05", "total": 2, "missing": False},
            ],
        )
        self.assertEqual(detail["cycle_total"], 2)
        self.assertEqual(detail["coverage"], {"present_days": 1, "missing_days": 2})

    def test_reconcile_single_day_period(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        result = self.reconcile("--start-date", "2026-01-10", "--end-date", "2026-01-10")
        self.assertEqual(result.returncode, 0, result.stderr)
        detail = json.loads(result.stdout)
        self.assertEqual(
            detail["days"], [{"usage_date": "2026-01-10", "total": 0, "missing": True}]
        )
        self.assertEqual(detail["cycle_total"], 0)
        self.assertEqual(detail["coverage"], {"present_days": 0, "missing_days": 1})

    def test_reconcile_validates_dates(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        bad_date = self.reconcile("--start-date", "2026-02-30", "--end-date", "2026-03-01")
        self.assertEqual(bad_date.returncode, 2)
        self.assertEqual(bad_date.stdout, "")

        bad_end = self.reconcile("--start-date", "2026-01-01", "--end-date", "not-a-date")
        self.assertEqual(bad_end.returncode, 2)
        self.assertEqual(bad_end.stdout, "")

        inverted = self.reconcile("--start-date", "2026-01-12", "--end-date", "2026-01-10")
        self.assertEqual(inverted.returncode, 2)
        self.assertEqual(inverted.stdout, "")

    def test_reconcile_unknown_subscription_exits_4(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        result = self.invoke(
            "usage", "reconcile", "--customer-id", "ghost", "--plan", "basic",
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_reconcile_does_not_write(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        before = self.db_path.read_bytes()
        result = self.reconcile("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db_path.read_bytes(), before)


class InvoiceTests(unittest.TestCase):
    """End-to-end checks for invoice generation and per-subscription queries."""

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
        self,
        customer_id: str = "c1",
        plan: str = "basic",
        start_date: str = "2026-01-01",
        price_cents: int = 1000,
        trial_days: int = 0,
    ) -> None:
        result = self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", str(price_cents),
            "--start-date", start_date,
            "--trial-days", str(trial_days),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def record_usage(self, usage_date: str, quantity: int, customer_id: str = "c1",
                     plan: str = "basic") -> None:
        result = self.invoke(
            "usage", "record", "--customer-id", customer_id, "--plan", plan,
            "--usage-date", usage_date, "--quantity", str(quantity),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def generate(self, *extra: str, customer_id: str = "c1",
                 plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "invoice", "generate", "--customer-id", customer_id, "--plan", plan, *extra
        )

    def test_invoice_bills_non_trial_days_at_price_cents(self) -> None:
        self.create_subscription(price_cents=990)
        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": 1,
                "subscription_id": 1,
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "billed_days": 31,
                "trial_days_in_period": 0,
                "amount_cents": 31 * 990,
                "usage_total": 0,
            },
        )

    def test_invoice_applies_trial_window_inside_period(self) -> None:
        # Trial covers Jan 1st through Jan 7th (7 consecutive days inclusive).
        self.create_subscription(price_cents=1000, trial_days=7)
        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["trial_days_in_period"], 7)
        self.assertEqual(record["billed_days"], 24)
        self.assertEqual(record["amount_cents"], 24000)

    def test_trial_window_is_clipped_to_period_bounds(self) -> None:
        # Trial Jan 1..7; period Jan 5..10 intersects on Jan 5, 6, 7.
        self.create_subscription(price_cents=1000, trial_days=7)
        result = self.generate("--start-date", "2026-01-05", "--end-date", "2026-01-10")
        record = json.loads(result.stdout)
        self.assertEqual(record["trial_days_in_period"], 3)
        self.assertEqual(record["billed_days"], 3)
        self.assertEqual(record["amount_cents"], 3000)

    def test_trial_window_can_extend_before_period_start(self) -> None:
        # Period starts before the subscription; only Jan 1..2 are trial days.
        self.create_subscription(start_date="2026-01-01", price_cents=1000, trial_days=7)
        result = self.generate("--start-date", "2025-12-30", "--end-date", "2026-01-02")
        record = json.loads(result.stdout)
        self.assertEqual(record["trial_days_in_period"], 2)
        self.assertEqual(record["billed_days"], 2)
        self.assertEqual(record["amount_cents"], 2000)

    def test_usage_total_sums_records_per_day_with_zero_for_missing_days(self) -> None:
        self.create_subscription(price_cents=500)
        self.record_usage("2026-01-10", 5)
        self.record_usage("2026-01-10", 3)
        self.record_usage("2026-01-31", 2)
        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        record = json.loads(result.stdout)
        self.assertEqual(record["usage_total"], 10)
        self.assertEqual(record["billed_days"], 31)
        self.assertEqual(record["amount_cents"], 15500)

    def test_single_day_period(self) -> None:
        self.create_subscription(price_cents=1000, trial_days=7)
        trial_day = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-01")
        self.assertEqual(json.loads(trial_day.stdout)["billed_days"], 0)
        self.assertEqual(json.loads(trial_day.stdout)["amount_cents"], 0)
        self.assertEqual(json.loads(trial_day.stdout)["trial_days_in_period"], 1)

        billed_day = self.generate("--start-date", "2026-01-08", "--end-date", "2026-01-08")
        self.assertEqual(json.loads(billed_day.stdout)["billed_days"], 1)
        self.assertEqual(json.loads(billed_day.stdout)["amount_cents"], 1000)
        self.assertEqual(json.loads(billed_day.stdout)["trial_days_in_period"], 0)

    def test_duplicate_period_is_rejected_without_writing(self) -> None:
        self.create_subscription(price_cents=990)
        first = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_id = json.loads(first.stdout)["id"]

        duplicate = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(duplicate.returncode, 3)
        self.assertIn("duplicate", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")

        listing = self.invoke("invoice", "list", "--customer-id", "c1", "--plan", "basic")
        records = json.loads(listing.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], first_id)

        # The rejected attempt must not consume an invoice id.
        other = self.generate("--start-date", "2026-02-01", "--end-date", "2026-02-28")
        self.assertEqual(json.loads(other.stdout)["id"], first_id + 1)

    def test_invalid_dates_exit_2_without_writing(self) -> None:
        self.create_subscription()
        bad_start = self.generate("--start-date", "2026-02-30", "--end-date", "2026-03-01")
        self.assertEqual(bad_start.returncode, 2)
        self.assertEqual(bad_start.stdout, "")

        bad_end = self.generate("--start-date", "2026-01-01", "--end-date", "not-a-date")
        self.assertEqual(bad_end.returncode, 2)
        self.assertEqual(bad_end.stdout, "")

        inverted = self.generate("--start-date", "2026-01-12", "--end-date", "2026-01-10")
        self.assertEqual(inverted.returncode, 2)
        self.assertEqual(inverted.stdout, "")

        listing = self.invoke("invoice", "list", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(json.loads(listing.stdout), [])

    def test_unknown_subscription_exits_4(self) -> None:
        self.create_subscription()
        result = self.generate(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            customer_id="ghost", plan="basic",
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")

        listing = self.invoke("invoice", "list", "--customer-id", "ghost", "--plan", "basic")
        self.assertEqual(listing.returncode, 4)
        self.assertEqual(listing.stdout, "")

    def test_list_is_scoped_and_sorted_by_id(self) -> None:
        self.create_subscription("c1", "basic", price_cents=100)
        self.create_subscription("c2", "pro", price_cents=200)

        c2_jan = self.invoke(
            "invoice", "generate", "--customer-id", "c2", "--plan", "pro",
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
        )
        c1_jan = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        c1_feb = self.generate("--start-date", "2026-02-01", "--end-date", "2026-02-28")
        self.assertEqual(json.loads(c2_jan.stdout)["id"], 1)
        self.assertEqual(json.loads(c1_jan.stdout)["id"], 2)
        self.assertEqual(json.loads(c1_feb.stdout)["id"], 3)

        c1_list = self.invoke("invoice", "list", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(c1_list.returncode, 0, c1_list.stderr)
        c1_records = json.loads(c1_list.stdout)
        self.assertEqual([r["id"] for r in c1_records], [2, 3])
        self.assertEqual(len(c1_list.stdout.splitlines()), 1)
        self.assertEqual(
            c1_records[0],
            {
                "id": 2,
                "subscription_id": 1,
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "billed_days": 31,
                "trial_days_in_period": 0,
                "amount_cents": 3100,
                "usage_total": 0,
            },
        )

        c2_list = self.invoke("invoice", "list", "--customer-id", "c2", "--plan", "pro")
        self.assertEqual([r["id"] for r in json.loads(c2_list.stdout)], [1])

    def test_invoice_with_any_usage_history_works(self) -> None:
        # Invoice generation must work when no usage command ever ran.
        self.create_subscription(price_cents=100)
        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-07")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["usage_total"], 0)


if __name__ == "__main__":
    unittest.main()
