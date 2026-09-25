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
    """End-to-end checks for invoice generation and discount registration."""

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
        price_cents: int = 100,
        start_date: str = "2026-01-01",
        trial_days: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", str(price_cents),
            "--start-date", start_date,
            "--trial-days", str(trial_days),
        )

    def record(self, usage_date: str, quantity: int, customer_id: str = "c1", plan: str = "basic") -> None:
        result = self.invoke(
            "usage", "record", "--customer-id", customer_id, "--plan", plan,
            "--usage-date", usage_date, "--quantity", str(quantity),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def generate(self, *extra: str, customer_id: str = "c1", plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "invoice", "generate", "--customer-id", customer_id, "--plan", plan, *extra
        )

    def register_discount(self, *extra: str, customer_id: str = "c1", plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "invoice", "register-discount", "--customer-id", customer_id, "--plan", plan, *extra
        )

    def _table_count(self, table: str) -> int:
        import sqlite3

        if not self.db_path.exists():
            return 0
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchall()
            if not rows:
                return 0
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_generation_bills_daily_sums_without_trial(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        self.record("2026-01-10", 3)
        self.record("2026-01-11", 2)

        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "invoice_id": 1,
                "customer_id": "c1",
                "plan": "basic",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "billed_usage": 10,
                "subtotal_cents": 1000,
                "discount_percent": 0,
                "total_cents": 1000,
                "trailing_days": 0,
            },
        )

    def test_trial_days_are_free_including_start_date(self) -> None:
        self.assertEqual(self.create_subscription(trial_days=7).returncode, 0)
        self.record("2026-01-01", 100)
        self.record("2026-01-07", 50)
        self.record("2026-01-08", 5)
        self.record("2026-01-09", 3)

        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["billed_usage"], 8)
        self.assertEqual(payload["subtotal_cents"], 800)
        self.assertEqual(payload["total_cents"], 800)
        self.assertEqual(payload["trailing_days"], 7)

    def test_trial_overlap_counts_only_days_inside_cycle(self) -> None:
        self.assertEqual(self.create_subscription(trial_days=7).returncode, 0)
        self.record("2026-01-06", 4)  # trial
        self.record("2026-01-08", 6)  # chargeable

        result = self.generate("--start-date", "2026-01-05", "--end-date", "2026-01-20")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["trailing_days"], 3)  # Jan 5, 6, 7
        self.assertEqual(payload["billed_usage"], 6)

        # Cycle fully inside the trial: nothing is billable.
        fully_trial = self.generate("--start-date", "2026-01-02", "--end-date", "2026-01-04")
        trial_payload = json.loads(fully_trial.stdout)
        self.assertEqual(trial_payload["trailing_days"], 3)
        self.assertEqual(trial_payload["billed_usage"], 0)
        self.assertEqual(trial_payload["subtotal_cents"], 0)
        self.assertEqual(trial_payload["total_cents"], 0)

        # Cycle after the trial: no overlap, everything billable.
        after = self.generate("--start-date", "2026-02-01", "--end-date", "2026-02-28")
        after_payload = json.loads(after.stdout)
        self.assertEqual(after_payload["trailing_days"], 0)
        self.assertEqual(after_payload["invoice_id"], 3)

    def test_repeated_generation_is_idempotent_and_identical(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)

        arguments = ("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        first = self.generate(*arguments)
        second = self.generate(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout)["invoice_id"], 1)
        self.assertEqual(self._table_count("invoices"), 1)

    def test_distinct_cycles_and_customers_get_monotonic_ids(self) -> None:
        self.assertEqual(self.create_subscription("c1", "basic").returncode, 0)
        self.assertEqual(self.create_subscription("c2", "pro").returncode, 0)

        jan = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        feb = self.generate("--start-date", "2026-02-01", "--end-date", "2026-02-28")
        c2 = self.generate(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            customer_id="c2", plan="pro",
        )
        self.assertEqual(json.loads(jan.stdout)["invoice_id"], 1)
        self.assertEqual(json.loads(feb.stdout)["invoice_id"], 2)
        self.assertEqual(json.loads(c2.stdout)["invoice_id"], 3)
        self.assertEqual(self._table_count("invoices"), 3)

    def test_discount_applied_when_registered_before_generation(self) -> None:
        self.assertEqual(self.create_subscription(price_cents=100).returncode, 0)
        self.record("2026-01-10", 3)

        registered = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "20",
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(registered.stdout, "")

        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["billed_usage"], 3)
        self.assertEqual(payload["subtotal_cents"], 300)
        self.assertEqual(payload["discount_percent"], 20)
        self.assertEqual(payload["total_cents"], 240)

    def test_discount_rounds_total_down_to_cents(self) -> None:
        self.assertEqual(self.create_subscription(price_cents=33).returncode, 0)
        self.record("2026-01-10", 3)  # subtotal 99

        registered = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "33",
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)

        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["subtotal_cents"], 99)
        self.assertEqual(payload["total_cents"], 66)  # 99 * 67 // 100

    def test_full_discount_is_allowed(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        registered = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "100",
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(json.loads(result.stdout)["total_cents"], 0)

    def test_duplicate_discount_rejected_and_keeps_original(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)

        first = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "20",
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        duplicate = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "50",
        )
        self.assertEqual(duplicate.returncode, 3)
        self.assertIn("already registered", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")
        self.assertEqual(self._table_count("invoice_discounts"), 1)

        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["discount_percent"], 20)
        self.assertEqual(payload["total_cents"], 400)

    def test_discount_after_generation_does_not_rewrite_invoice(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)

        arguments = ("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        first = self.generate(*arguments)
        self.assertEqual(json.loads(first.stdout)["total_cents"], 500)

        registered = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "20",
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)

        second = self.generate(*arguments)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(self._table_count("invoices"), 1)

    def test_discount_requires_positive_prediscount_amount(self) -> None:
        self.assertEqual(self.create_subscription(trial_days=7).returncode, 0)
        self.record("2026-01-03", 9)  # inside trial, so billable amount is 0

        result = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-07",
            "--discount-percent", "20",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("greater than 0", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._table_count("invoice_discounts"), 0)

        # Usage on the first chargeable day makes registration valid even for
        # a cycle overlapping the trial.
        self.record("2026-01-08", 1)
        ok = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "20",
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_discount_validates_parameters_with_exit_3(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        base = ("--start-date", "2026-01-01", "--end-date", "2026-01-31")

        for bad_value in ["-1", "101", "abc", "20.5", ""]:
            with self.subTest(bad_value=bad_value):
                result = self.register_discount(*base, "--discount-percent", bad_value)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self._table_count("invoice_discounts"), 0)

        bad_date = self.register_discount(
            "--start-date", "2026-02-30", "--end-date", "2026-01-31",
            "--discount-percent", "20",
        )
        self.assertEqual(bad_date.returncode, 3)

        inverted = self.register_discount(
            "--start-date", "2026-01-31", "--end-date", "2026-01-01",
            "--discount-percent", "20",
        )
        self.assertEqual(inverted.returncode, 3)
        self.assertEqual(self._table_count("invoice_discounts"), 0)

    def test_discount_unknown_subscription_exits_4(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        result = self.register_discount(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            "--discount-percent", "20",
            customer_id="ghost", plan="basic",
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._table_count("invoice_discounts"), 0)

    def test_generation_validates_dates_with_exit_2(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        bad_date = self.generate("--start-date", "2026-02-30", "--end-date", "2026-03-01")
        self.assertEqual(bad_date.returncode, 2)
        self.assertEqual(bad_date.stdout, "")

        bad_end = self.generate("--start-date", "2026-01-01", "--end-date", "not-a-date")
        self.assertEqual(bad_end.returncode, 2)
        self.assertEqual(bad_end.stdout, "")

        inverted = self.generate("--start-date", "2026-01-12", "--end-date", "2026-01-10")
        self.assertEqual(inverted.returncode, 2)
        self.assertEqual(inverted.stdout, "")
        self.assertEqual(self._table_count("invoices"), 0)

    def test_generation_unknown_subscription_exits_4(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        result = self.generate(
            "--start-date", "2026-01-01", "--end-date", "2026-01-31",
            customer_id="ghost", plan="basic",
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._table_count("invoices"), 0)

    def test_generation_output_field_order_is_fixed(self) -> None:
        self.assertEqual(self.create_subscription(trial_days=3).returncode, 0)
        self.record("2026-01-05", 2)
        result = self.generate("--start-date", "2026-01-01", "--end-date", "2026-01-31")
        self.assertEqual(
            result.stdout.strip(),
            '{"invoice_id":1,"customer_id":"c1","plan":"basic",'
            '"period_start":"2026-01-01","period_end":"2026-01-31",'
            '"billed_usage":2,"subtotal_cents":200,"discount_percent":0,'
            '"total_cents":200,"trailing_days":3}',
        )


if __name__ == "__main__":
    unittest.main()
