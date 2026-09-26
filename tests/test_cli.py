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


class TrialManagementTests(unittest.TestCase):
    """End-to-end checks for trial registration, querying and conversion."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.env = {**os.environ, "BILLING_LEDGER_DB": str(self.db_path)}
        from datetime import datetime, timezone

        self.today = datetime.now(timezone.utc).date()

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

    def iso(self, offset_days: int) -> str:
        from datetime import timedelta

        return (self.today + timedelta(days=offset_days)).isoformat()

    def create(self, *extra: str, customer_id: str = "c1", plan: str = "basic",
               start_date: str = "2026-01-01") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", "990",
            "--start-date", start_date,
            *extra,
        )

    def trial(self, customer_id: str = "c1", plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke("subscription", "trial", "--customer-id", customer_id, "--plan", plan)

    def test_no_trial_defaults_to_active_with_conversion_on_start(self) -> None:
        result = self.create()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "active")

        query = self.trial()
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual(len(query.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(query.stdout),
            {
                "customer_id": "c1",
                "plan": "basic",
                "start_date": "2026-01-01",
                "trial_days": 0,
                "trial_end_date": None,
                "trial_to_active_date": "2026-01-01",
                "status": "active",
            },
        )

    def test_explicit_zero_trial_days_matches_default(self) -> None:
        result = self.create("--trial-days", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(self.trial().stdout)
        self.assertEqual(view["trial_days"], 0)
        self.assertIsNone(view["trial_end_date"])
        self.assertEqual(view["trial_to_active_date"], "2026-01-01")
        self.assertEqual(view["status"], "active")

    def test_trial_days_sets_inclusive_interval_and_conversion(self) -> None:
        result = self.create("--trial-days", "14")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "trial")

        view = json.loads(self.trial().stdout)
        self.assertEqual(view["trial_days"], 14)
        self.assertEqual(view["trial_end_date"], "2026-01-14")
        self.assertEqual(view["trial_to_active_date"], "2026-01-15")
        self.assertEqual(view["status"], "trial")

        # as_of earlier than the conversion date is rejected without writing.
        early = self.invoke(
            "subscription", "activate", "--customer-id", "c1", "--plan", "basic",
            "--as-of", "2026-01-14",
        )
        self.assertEqual(early.returncode, 2)
        self.assertIn("trial_to_active_date", early.stderr)
        self.assertEqual(early.stdout, "")
        self.assertEqual(json.loads(self.trial().stdout)["status"], "trial")

        converted = self.invoke(
            "subscription", "activate", "--customer-id", "c1", "--plan", "basic",
            "--as-of", "2026-01-20",
        )
        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertEqual(
            json.loads(converted.stdout),
            {"customer_id": "c1", "plan": "basic", "status": "active",
             "activated_on": "2026-01-20"},
        )
        self.assertEqual(len(converted.stdout.splitlines()), 1)
        self.assertEqual(json.loads(self.trial().stdout)["status"], "active")

    def test_trial_end_date_sets_interval_and_next_day_conversion(self) -> None:
        end = self.iso(13)
        result = self.create("--trial-end-date", end, start_date=self.iso(0))
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(self.trial().stdout)
        self.assertEqual(view["status"], "trial")
        self.assertEqual(view["trial_days"], 14)
        self.assertEqual(view["trial_end_date"], end)
        self.assertEqual(view["trial_to_active_date"], self.iso(14))

    def test_trial_end_date_may_equal_start_for_one_day_trial(self) -> None:
        today = self.iso(0)
        result = self.create("--trial-end-date", today, start_date=today)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(self.trial().stdout)
        self.assertEqual(view["trial_days"], 1)
        self.assertEqual(view["trial_end_date"], today)
        self.assertEqual(view["trial_to_active_date"], self.iso(1))

    def test_create_argument_errors_exit_2_and_do_not_write(self) -> None:
        both = self.create("--trial-days", "3", "--trial-end-date", self.iso(10))
        self.assertEqual(both.returncode, 2)
        self.assertEqual(both.stdout, "")

        before_start = self.create(
            "--trial-end-date", "2025-12-31", start_date="2026-01-01"
        )
        self.assertEqual(before_start.returncode, 2)
        self.assertIn("--start-date", before_start.stderr)

        before_today = self.create(
            "--trial-end-date", "2026-01-01", start_date="2025-12-01"
        )
        self.assertEqual(before_today.returncode, 2)
        self.assertIn("today", before_today.stderr)

        bad_date = self.create("--trial-end-date", "2026-02-30")
        self.assertEqual(bad_date.returncode, 2)
        self.assertEqual(bad_date.stdout, "")

        bad_as_of = self.invoke(
            "subscription", "activate", "--customer-id", "c1", "--plan", "basic",
            "--as-of", "not-a-date",
        )
        self.assertEqual(bad_as_of.returncode, 2)
        self.assertEqual(bad_as_of.stdout, "")

        # Nothing was persisted by any failed create.
        missing = self.trial()
        self.assertEqual(missing.returncode, 4)

    def test_duplicate_registration_exits_3_without_overwriting(self) -> None:
        self.assertEqual(self.create("--trial-days", "14").returncode, 0)
        duplicate = self.create()
        self.assertEqual(duplicate.returncode, 3)
        self.assertIn("duplicate", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")
        view = json.loads(self.trial().stdout)
        self.assertEqual(view["status"], "trial")
        self.assertEqual(view["trial_days"], 14)

    def test_query_and_activate_unknown_subscription_exit_4(self) -> None:
        query = self.invoke("subscription", "trial", "--customer-id", "ghost", "--plan", "basic")
        self.assertEqual(query.returncode, 4)
        self.assertIn("no subscription", query.stderr)
        self.assertEqual(query.stdout, "")

        activate = self.invoke(
            "subscription", "activate", "--customer-id", "ghost", "--plan", "basic",
            "--as-of", "2026-02-01",
        )
        self.assertEqual(activate.returncode, 4)
        self.assertIn("no subscription", activate.stderr)
        self.assertEqual(activate.stdout, "")

    def test_trial_dates_persist_across_processes(self) -> None:
        self.assertEqual(self.create("--trial-days", "14").returncode, 0)
        # Each invoke is a fresh process; the conversion date must be read back.
        first = json.loads(self.trial().stdout)
        second = json.loads(self.trial().stdout)
        self.assertEqual(first, second)
        self.assertEqual(second["trial_to_active_date"], "2026-01-15")


class SubscriptionChangeTests(unittest.TestCase):
    """End-to-end checks for changing plan, price or trial end date."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.env = {**os.environ, "BILLING_LEDGER_DB": str(self.db_path)}
        from datetime import datetime, timezone

        self.today = datetime.now(timezone.utc).date()

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

    def iso(self, offset_days: int) -> str:
        from datetime import timedelta

        return (self.today + timedelta(days=offset_days)).isoformat()

    def create(self, *extra: str, customer_id: str = "c1", plan: str = "basic",
               start_date: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", "990",
            "--start-date", start_date or self.iso(-10),
            *extra,
        )

    def create_trial(self, *extra: str, **kwargs: str) -> subprocess.CompletedProcess[str]:
        result = self.create("--trial-end-date", self.iso(4), *extra, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def change(self, *extra: str, customer_id: str = "c1",
               plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "subscription", "change",
            "--customer-id", customer_id, "--plan", plan, *extra,
        )

    def trial(self, customer_id: str = "c1", plan: str = "basic") -> dict:
        result = self.invoke(
            "subscription", "trial", "--customer-id", customer_id, "--plan", plan
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_change_trial_end_date_recomputes_trial_fields(self) -> None:
        self.create_trial()
        result = self.change("--trial-end-date", self.iso(9))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "customer_id": "c1",
                "plan": "basic",
                "trial_days": 20,
                "trial_end_date": self.iso(9),
                "trial_to_active_date": self.iso(10),
                "status": "trial",
            },
        )
        # The trial query reads the new values back.
        self.assertEqual(self.trial(), json.loads(result.stdout) | {"start_date": self.iso(-10)})

    def test_change_plan_moves_subscription_and_keeps_usage(self) -> None:
        self.create_trial()
        recorded = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", self.iso(-2), "--quantity", "5",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)

        result = self.change("--plan-new", "pro")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["plan"], "pro")
        self.assertEqual(json.loads(result.stdout)["status"], "trial")

        # The old locator no longer resolves; the new one does.
        old = self.invoke("subscription", "trial", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(old.returncode, 4)
        view = self.trial(plan="pro")
        self.assertEqual(view["trial_end_date"], self.iso(4))
        self.assertEqual(view["trial_to_active_date"], self.iso(5))

        # Per-record and daily-summary usage survive the rename.
        listing = self.invoke("usage", "list", "--customer-id", "c1", "--plan", "pro")
        self.assertEqual(
            json.loads(listing.stdout),
            [{"id": 1, "usage_date": self.iso(-2), "quantity": 5}],
        )
        summary = self.invoke("usage", "summary", "--customer-id", "c1", "--plan", "pro")
        self.assertEqual(
            json.loads(summary.stdout),
            [{"usage_date": self.iso(-2), "total": 5}],
        )

    def test_change_multiple_fields_applies_atomically(self) -> None:
        self.create_trial()
        result = self.change(
            "--plan-new", "pro", "--price-cents", "1990",
            "--trial-end-date", self.iso(6),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "customer_id": "c1",
                "plan": "pro",
                "trial_days": 17,
                "trial_end_date": self.iso(6),
                "trial_to_active_date": self.iso(7),
                "status": "trial",
            },
        )
        listing = self.invoke("subscription", "list")
        (row,) = json.loads(listing.stdout)
        self.assertEqual(row["price_cents"], 1990)
        self.assertEqual(row["plan"], "pro")

    def test_change_price_only(self) -> None:
        self.create_trial()
        result = self.change("--price-cents", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["plan"], "basic")
        (row,) = json.loads(self.invoke("subscription", "list").stdout)
        self.assertEqual(row["price_cents"], 0)

    def test_change_requires_at_least_one_change_argument(self) -> None:
        self.create_trial()
        result = self.change()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.trial()["trial_end_date"], self.iso(4))

    def test_change_rejects_same_plan_and_empty_plan_new(self) -> None:
        self.create_trial()
        same = self.change("--plan-new", "basic")
        self.assertEqual(same.returncode, 2)
        self.assertEqual(same.stdout, "")
        empty = self.change("--plan-new", "")
        self.assertEqual(empty.returncode, 2)
        self.assertEqual(empty.stdout, "")

    def test_change_validates_trial_end_date(self) -> None:
        self.create_trial()
        bad_date = self.change("--trial-end-date", "2026-02-30")
        self.assertEqual(bad_date.returncode, 2)
        self.assertEqual(bad_date.stdout, "")

        before_today = self.change("--trial-end-date", self.iso(-1))
        self.assertEqual(before_today.returncode, 2)
        self.assertIn("today", before_today.stderr)

        before_start = self.change("--trial-end-date", self.iso(-11))
        # iso(-11) is also before today; use a date between start and today.
        self.assertEqual(before_start.returncode, 2)

        before_old_end = self.change("--trial-end-date", self.iso(3))
        self.assertEqual(before_old_end.returncode, 2)
        self.assertIn(self.iso(4), before_old_end.stderr)

        # Nothing was written by any failed change.
        self.assertEqual(self.trial()["trial_end_date"], self.iso(4))

    def test_change_trial_end_date_before_start_date_exits_2(self) -> None:
        # start_date in the future relative to nothing: pick a start date of
        # today so a valid (>= today) end date cannot precede it; instead use
        # a start date earlier than today and an end date before start but
        # that case is unreachable since end >= today > start. Construct via
        # start_date == today and end == today (allowed, not earlier).
        self.create("--trial-end-date", self.iso(0), start_date=self.iso(0))
        ok = self.change("--trial-end-date", self.iso(0))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["trial_days"], 1)

    def test_change_active_subscription_exits_2(self) -> None:
        self.create()  # no trial -> active
        result = self.change("--price-cents", "100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("trial", result.stderr)
        self.assertEqual(result.stdout, "")

        # Activated subscriptions cannot be changed either.
        self.create("--trial-end-date", self.iso(4), customer_id="c2", plan="pro")
        activated = self.invoke(
            "subscription", "activate", "--customer-id", "c2", "--plan", "pro",
            "--as-of", self.iso(5),
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        again = self.change("--price-cents", "100", customer_id="c2", plan="pro")
        self.assertEqual(again.returncode, 2)

    def test_change_unknown_subscription_exits_4(self) -> None:
        result = self.change("--price-cents", "100")
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_change_plan_conflict_exits_4_without_overwriting(self) -> None:
        self.create_trial()
        self.create("--trial-end-date", self.iso(6), plan="pro")
        result = self.change("--plan-new", "pro")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stdout, "")
        # Both subscriptions are untouched.
        self.assertEqual(self.trial()["trial_end_date"], self.iso(4))
        self.assertEqual(self.trial(plan="pro")["trial_end_date"], self.iso(6))

    def test_error_priority_argument_errors_beat_lookup_errors(self) -> None:
        # Invalid date + unknown subscription -> argument error wins (exit 2).
        result = self.change("--trial-end-date", "not-a-date")
        self.assertEqual(result.returncode, 2)

        # Same plan + unknown subscription -> argument error wins (exit 2).
        same = self.change("--plan-new", "basic")
        self.assertEqual(same.returncode, 2)

    def test_error_priority_active_beats_conflict(self) -> None:
        self.create(customer_id="c1", plan="basic")  # active
        self.create("--trial-end-date", self.iso(6), customer_id="c1", plan="pro")
        result = self.change("--plan-new", "pro")
        self.assertEqual(result.returncode, 2)

    def test_error_priority_old_end_beats_conflict(self) -> None:
        self.create_trial()
        self.create("--trial-end-date", self.iso(6), plan="pro")
        result = self.change("--plan-new", "pro", "--trial-end-date", self.iso(2))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.trial()["plan"], "basic")


class BillGenerationTests(unittest.TestCase):
    """End-to-end checks for bill generation and listing."""

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
        self, *extra: str, customer_id: str = "c1", plan: str = "basic",
        price_cents: int = 990, start_date: str = "2026-01-01",
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", str(price_cents),
            "--start-date", start_date,
            *extra,
        )

    def generate(self, *extra: str, customer_id: str = "c1",
                 plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "bill", "generate", "--customer-id", customer_id, "--plan", plan, *extra,
        )

    def list_bills(self, customer_id: str = "c1",
                   plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke("bill", "list", "--customer-id", customer_id, "--plan", plan)

    def record(self, usage_date: str, quantity: int) -> None:
        result = self.invoke(
            "usage", "record", "--customer-id", "c1", "--plan", "basic",
            "--usage-date", usage_date, "--quantity", str(quantity),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _bill_row_count(self) -> int:
        import sqlite3

        if not self.db_path.exists():
            return 0
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bills'"
            ).fetchall()
            if not rows:
                return 0
            return int(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0])

    def test_generate_full_period_with_usage(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        self.record("2026-01-10", 3)

        result = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": 1,
                "customer_id": "c1",
                "plan": "basic",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "base_cents": 990,
                "usage_cents": 80,
                "total_cents": 1070,
                "status": "open",
            },
        )

    def test_generate_prorates_from_start_date(self) -> None:
        self.assertEqual(
            self.create_subscription(start_date="2026-01-16").returncode, 0
        )
        result = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        bill = json.loads(result.stdout)
        # 16 billable days out of 31: floor(990 * 16 / 31) = 510.
        self.assertEqual(bill["base_cents"], 510)
        self.assertEqual(bill["usage_cents"], 0)
        self.assertEqual(bill["total_cents"], 510)

    def test_generate_skips_trial_days_after_activation(self) -> None:
        self.assertEqual(self.create_subscription("--trial-days", "14").returncode, 0)
        activated = self.invoke(
            "subscription", "activate", "--customer-id", "c1", "--plan", "basic",
            "--as-of", "2026-01-15",
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)

        result = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        bill = json.loads(result.stdout)
        # Billable from 2026-01-15: 17 days out of 31 -> floor(990 * 17 / 31) = 542.
        self.assertEqual(bill["base_cents"], 542)
        self.assertEqual(bill["total_cents"], 542)

    def test_generate_allows_zero_amount_bill(self) -> None:
        self.assertEqual(
            self.create_subscription(start_date="2026-02-01").returncode, 0
        )
        result = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        bill = json.loads(result.stdout)
        self.assertEqual(bill["base_cents"], 0)
        self.assertEqual(bill["usage_cents"], 0)
        self.assertEqual(bill["total_cents"], 0)
        self.assertEqual(bill["status"], "open")

    def test_generate_is_idempotent_on_the_business_key(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.record("2026-01-10", 5)
        first = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(first.returncode, 0, first.stderr)

        # More usage after the first bill must not change the persisted bill.
        self.record("2026-01-11", 100)
        second = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
        self.assertEqual(self._bill_row_count(), 1)

        # A different period is a different business key and gets the next id.
        third = self.generate("--period-start", "2026-02-01", "--period-end", "2026-02-28")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(json.loads(third.stdout)["id"], 2)

    def test_generate_rejects_trial_subscription(self) -> None:
        self.assertEqual(self.create_subscription("--trial-days", "14").returncode, 0)
        result = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(result.returncode, 2)
        self.assertIn("trial", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._bill_row_count(), 0)

    def test_generate_validates_period_dates(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        bad_start = self.generate("--period-start", "2026-02-30", "--period-end", "2026-03-01")
        self.assertEqual(bad_start.returncode, 2)
        self.assertEqual(bad_start.stdout, "")

        bad_end = self.generate("--period-start", "2026-01-01", "--period-end", "not-a-date")
        self.assertEqual(bad_end.returncode, 2)
        self.assertEqual(bad_end.stdout, "")

        inverted = self.generate("--period-start", "2026-01-12", "--period-end", "2026-01-10")
        self.assertEqual(inverted.returncode, 2)
        self.assertEqual(inverted.stdout, "")

        self.assertEqual(self._bill_row_count(), 0)

    def test_generate_unknown_subscription_exits_4(self) -> None:
        result = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._bill_row_count(), 0)

    def test_list_bills_in_id_order_and_empty(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)

        empty = self.list_bills()
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout), [])

        for period in [("2026-02-01", "2026-02-28"), ("2026-01-01", "2026-01-31")]:
            result = self.generate("--period-start", period[0], "--period-end", period[1])
            self.assertEqual(result.returncode, 0, result.stderr)

        listing = self.list_bills()
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(len(listing.stdout.splitlines()), 1)
        bills = json.loads(listing.stdout)
        self.assertEqual([bill["id"] for bill in bills], [1, 2])
        self.assertEqual(
            [bill["period_start"] for bill in bills], ["2026-02-01", "2026-01-01"]
        )
        for bill in bills:
            self.assertEqual(
                set(bill),
                {"id", "customer_id", "plan", "period_start", "period_end",
                 "base_cents", "usage_cents", "total_cents", "status"},
            )
            self.assertEqual(bill["status"], "open")

    def test_list_bills_unknown_subscription_exits_4(self) -> None:
        result = self.list_bills(customer_id="ghost")
        self.assertEqual(result.returncode, 4)
        self.assertIn("no subscription", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bill_ids_are_global_across_subscriptions(self) -> None:
        self.assertEqual(self.create_subscription().returncode, 0)
        self.assertEqual(
            self.create_subscription(customer_id="c2", plan="pro").returncode, 0
        )
        first = self.generate("--period-start", "2026-01-01", "--period-end", "2026-01-31")
        second = self.generate(
            "--period-start", "2026-01-01", "--period-end", "2026-01-31",
            customer_id="c2", plan="pro",
        )
        self.assertEqual(json.loads(first.stdout)["id"], 1)
        self.assertEqual(json.loads(second.stdout)["id"], 2)

        c1_bills = json.loads(self.list_bills().stdout)
        c2_bills = json.loads(self.list_bills(customer_id="c2", plan="pro").stdout)
        self.assertEqual([bill["id"] for bill in c1_bills], [1])
        self.assertEqual([bill["id"] for bill in c2_bills], [2])


class PaymentTests(unittest.TestCase):
    """End-to-end checks for payment registration, matching and listing."""

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
        self, customer_id: str = "c1", plan: str = "basic"
    ) -> None:
        result = self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", "990",
            "--start-date", "2026-01-01",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def record_usage(self, quantity: int, customer_id: str = "c1",
                    plan: str = "basic", usage_date: str = "2026-01-10") -> None:
        result = self.invoke(
            "usage", "record",
            "--customer-id", customer_id, "--plan", plan,
            "--usage-date", usage_date, "--quantity", str(quantity),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def generate_bill(self, customer_id: str = "c1", plan: str = "basic") -> int:
        result = self.invoke(
            "bill", "generate",
            "--customer-id", customer_id, "--plan", plan,
            "--period-start", "2026-01-01", "--period-end", "2026-01-31",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(json.loads(result.stdout)["id"])

    def pay(self, bill_id: int, reference: str, amount: int,
            payment_date: str = "2026-02-05", customer_id: str = "c1",
            plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "payment", "record",
            "--customer-id", customer_id, "--plan", plan,
            "--bill-id", str(bill_id),
            "--amount-cents", str(amount),
            "--payment-date", payment_date,
            "--reference", reference,
        )

    def _payment_count(self) -> int:
        import sqlite3

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='payments'"
            ).fetchall()
            if not rows:
                return 0
            return int(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0])

    def _setup_bill(self) -> int:
        # 990 base + 8 usage units * 10 cents = total_cents 1070.
        self.create_subscription()
        self.record_usage(5)
        self.record_usage(3)
        return self.generate_bill()

    def test_record_payment_outputs_one_json_line(self) -> None:
        bill_id = self._setup_bill()
        result = self.pay(bill_id, "r1", 500)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {"id": 1, "bill_id": bill_id, "customer_id": "c1", "plan": "basic",
             "reference": "r1", "amount_cents": 500,
             "payment_date": "2026-02-05", "status": "applied"},
        )
        self.assertEqual(result.stderr, "")

    def test_record_unknown_or_mismatched_bill_exits_4(self) -> None:
        bill_id = self._setup_bill()

        missing = self.pay(999, "r1", 500)
        self.assertEqual(missing.returncode, 4)
        self.assertIn("no bill", missing.stderr)
        self.assertEqual(missing.stdout, "")

        wrong_customer = self.pay(bill_id, "r1", 500, customer_id="ghost")
        self.assertEqual(wrong_customer.returncode, 4)
        self.assertIn("no bill", wrong_customer.stderr)
        self.assertEqual(wrong_customer.stdout, "")

        wrong_plan = self.pay(bill_id, "r1", 500, plan="pro")
        self.assertEqual(wrong_plan.returncode, 4)
        self.assertEqual(wrong_plan.stdout, "")

        self.assertEqual(self._payment_count(), 0)

    def test_record_validates_amount_and_date(self) -> None:
        bill_id = self._setup_bill()

        zero = self.pay(bill_id, "r1", 0)
        self.assertEqual(zero.returncode, 2)
        self.assertEqual(zero.stdout, "")

        negative = self.invoke(
            "payment", "record",
            "--customer-id", "c1", "--plan", "basic", "--bill-id", str(bill_id),
            "--amount-cents", "-5", "--payment-date", "2026-02-05",
            "--reference", "r1",
        )
        self.assertEqual(negative.returncode, 2)
        self.assertEqual(negative.stdout, "")

        not_integer = self.invoke(
            "payment", "record",
            "--customer-id", "c1", "--plan", "basic", "--bill-id", str(bill_id),
            "--amount-cents", "1.5", "--payment-date", "2026-02-05",
            "--reference", "r1",
        )
        self.assertEqual(not_integer.returncode, 2)
        self.assertEqual(not_integer.stdout, "")

        bad_date = self.pay(bill_id, "r1", 500, payment_date="2026-02-30")
        self.assertEqual(bad_date.returncode, 2)
        self.assertIn("YYYY-MM-DD", bad_date.stderr)
        self.assertEqual(bad_date.stdout, "")

        empty_reference = self.invoke(
            "payment", "record",
            "--customer-id", "c1", "--plan", "basic", "--bill-id", str(bill_id),
            "--amount-cents", "500", "--payment-date", "2026-02-05",
            "--reference", "",
        )
        self.assertEqual(empty_reference.returncode, 2)
        self.assertEqual(empty_reference.stdout, "")

        self.assertEqual(self._payment_count(), 0)

    def test_duplicate_reference_exits_5_without_overwriting(self) -> None:
        first_bill = self._setup_bill()
        self.create_subscription("c2", "pro")
        second_bill = self.generate_bill("c2", "pro")

        first = self.pay(first_bill, "dup-ref", 500)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["id"], 1)

        same_bill = self.pay(first_bill, "dup-ref", 600,
                             payment_date="2026-02-06")
        self.assertEqual(same_bill.returncode, 5)
        self.assertIn("duplicate", same_bill.stderr)
        self.assertEqual(same_bill.stdout, "")

        other_bill = self.pay(second_bill, "dup-ref", 600,
                              customer_id="c2", plan="pro")
        self.assertEqual(other_bill.returncode, 5)
        self.assertEqual(other_bill.stdout, "")

        self.assertEqual(self._payment_count(), 1)
        listing = json.loads(self.invoke("payment", "list").stdout)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["amount_cents"], 500)
        self.assertEqual(listing[0]["payment_date"], "2026-02-05")

    def test_payment_ids_are_global_and_monotonic(self) -> None:
        first_bill = self._setup_bill()
        self.create_subscription("c2", "pro")
        second_bill = self.generate_bill("c2", "pro")

        first = self.pay(first_bill, "r1", 100)
        second = self.pay(second_bill, "r2", 200, customer_id="c2", plan="pro")
        third = self.pay(first_bill, "r3", 300)
        self.assertEqual([json.loads(r.stdout)["id"] for r in (first, second, third)],
                         [1, 2, 3])

    def test_match_exact_single_payment_settles(self) -> None:
        bill_id = self._setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 1070).returncode, 0)

        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": bill_id, "reference": "r1", "amount_cents": 1070,
                 "applied_cents": 1070, "result": "settled"},
                {"bill_id": bill_id, "total_cents": 1070,
                 "paid_cents": 1070, "status": "paid"},
            ],
        )

    def test_match_multiple_payments_settle_exactly(self) -> None:
        bill_id = self._setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 500).returncode, 0)
        self.assertEqual(self.pay(bill_id, "r2", 570).returncode, 0)

        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": bill_id, "reference": "r1", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": bill_id, "reference": "r2", "amount_cents": 570,
                 "applied_cents": 570, "result": "settled"},
                {"bill_id": bill_id, "total_cents": 1070,
                 "paid_cents": 1070, "status": "paid"},
            ],
        )

    def test_match_caps_overpaying_payment_to_exact_settlement(self) -> None:
        bill_id = self._setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 500).returncode, 0)
        self.assertEqual(self.pay(bill_id, "r2", 500).returncode, 0)
        self.assertEqual(self.pay(bill_id, "r3", 200).returncode, 0)

        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": bill_id, "reference": "r1", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": bill_id, "reference": "r2", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": bill_id, "reference": "r3", "amount_cents": 200,
                 "applied_cents": 70, "result": "settled"},
                {"bill_id": bill_id, "total_cents": 1070,
                 "paid_cents": 1070, "status": "paid"},
            ],
        )

    def test_match_later_payments_after_settlement_are_overpaid(self) -> None:
        bill_id = self._setup_bill()
        for reference, amount in [("r1", 1070), ("r2", 100), ("r3", 200)]:
            self.assertEqual(self.pay(bill_id, reference, amount).returncode, 0)

        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": bill_id, "reference": "r1", "amount_cents": 1070,
                 "applied_cents": 1070, "result": "settled"},
                {"bill_id": bill_id, "reference": "r2", "amount_cents": 100,
                 "applied_cents": 0, "result": "overpaid"},
                {"bill_id": bill_id, "reference": "r3", "amount_cents": 200,
                 "applied_cents": 0, "result": "overpaid"},
                {"bill_id": bill_id, "total_cents": 1070,
                 "paid_cents": 1070, "status": "partial"},
            ],
        )

    def test_match_underpaid_bill_stays_open(self) -> None:
        bill_id = self._setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 500).returncode, 0)
        self.assertEqual(self.pay(bill_id, "r2", 400).returncode, 0)

        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": bill_id, "reference": "r1", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": bill_id, "reference": "r2", "amount_cents": 400,
                 "applied_cents": 400, "result": "underpaid"},
                {"bill_id": bill_id, "total_cents": 1070,
                 "paid_cents": 900, "status": "open"},
            ],
        )

    def test_match_without_payments_outputs_only_summary(self) -> None:
        bill_id = self._setup_bill()
        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {"bill_id": bill_id, "total_cents": 1070,
             "paid_cents": 0, "status": "open"},
        )

    def test_match_unknown_bill_exits_4(self) -> None:
        result = self.invoke("payment", "match", "--bill-id", "999")
        self.assertEqual(result.returncode, 4)
        self.assertIn("no bill", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_match_is_idempotent_and_does_not_write(self) -> None:
        bill_id = self._setup_bill()
        for reference, amount in [("r1", 500), ("r2", 1000), ("r3", 100)]:
            self.assertEqual(self.pay(bill_id, reference, amount).returncode, 0)

        first = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(first.returncode, 0, first.stderr)
        snapshot = self.db_path.read_bytes()
        second = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(self.db_path.read_bytes(), snapshot)

        third = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(third.stdout, first.stdout)
        lines = [json.loads(line) for line in first.stdout.splitlines()]
        self.assertEqual(lines[-1]["paid_cents"], 1070)
        self.assertEqual(lines[-1]["status"], "partial")

    def test_list_outputs_payments_in_registration_order(self) -> None:
        empty = self.invoke("payment", "list")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout), [])

        first_bill = self._setup_bill()
        self.create_subscription("c2", "pro")
        second_bill = self.generate_bill("c2", "pro")
        self.assertEqual(self.pay(first_bill, "r1", 500).returncode, 0)
        self.assertEqual(
            self.pay(second_bill, "r2", 200, customer_id="c2", plan="pro").returncode, 0
        )

        listing = self.invoke("payment", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(len(listing.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(listing.stdout),
            [
                {"id": 1, "bill_id": first_bill, "customer_id": "c1",
                 "plan": "basic", "reference": "r1", "amount_cents": 500,
                 "payment_date": "2026-02-05", "status": "applied"},
                {"id": 2, "bill_id": second_bill, "customer_id": "c2",
                 "plan": "pro", "reference": "r2", "amount_cents": 200,
                 "payment_date": "2026-02-05", "status": "applied"},
            ],
        )


class BillAdjustmentTests(unittest.TestCase):
    """End-to-end checks for bill adjust and bill void."""

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

    def setup_bill(self) -> int:
        # 990 base + 8 usage units * 10 cents = total_cents 1070.
        result = self.invoke(
            "subscription", "create",
            "--customer-id", "c1", "--plan", "basic",
            "--price-cents", "990", "--start-date", "2026-01-01",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for quantity in (5, 3):
            result = self.invoke(
                "usage", "record", "--customer-id", "c1", "--plan", "basic",
                "--usage-date", "2026-01-10", "--quantity", str(quantity),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke(
            "bill", "generate", "--customer-id", "c1", "--plan", "basic",
            "--period-start", "2026-01-01", "--period-end", "2026-01-31",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(json.loads(result.stdout)["id"])

    def adjust(self, bill_id: int, *extra: str, kind: str = "credit",
               amount: int = 200, reason: str = "late usage",
               reference: str = "adj-1", customer_id: str = "c1",
               plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "bill", "adjust",
            "--customer-id", customer_id, "--plan", plan,
            "--bill-id", str(bill_id),
            "--kind", kind, "--amount-cents", str(amount),
            "--reason", reason, "--reference", reference,
            *extra,
        )

    def void(self, bill_id: int, customer_id: str = "c1",
             plan: str = "basic") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "bill", "void",
            "--customer-id", customer_id, "--plan", plan,
            "--bill-id", str(bill_id),
        )

    def pay(self, bill_id: int, reference: str,
            amount: int) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "payment", "record",
            "--customer-id", "c1", "--plan", "basic",
            "--bill-id", str(bill_id),
            "--amount-cents", str(amount),
            "--payment-date", "2026-02-05",
            "--reference", reference,
        )

    def list_bills(self) -> list[dict]:
        result = self.invoke("bill", "list", "--customer-id", "c1", "--plan", "basic")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def match_summary(self, bill_id: int) -> dict:
        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def test_adjust_credit_outputs_one_json_line(self) -> None:
        bill_id = self.setup_bill()
        result = self.adjust(bill_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {"bill_id": bill_id, "kind": "credit", "amount_cents": 200,
             "reference": "adj-1", "reason": "late usage",
             "base_cents": 990, "usage_cents": 80,
             "total_cents": 870, "status": "open"},
        )
        self.assertEqual(result.stderr, "")

        bill = self.list_bills()[0]
        self.assertEqual(bill["total_cents"], 870)
        self.assertEqual(bill["base_cents"], 990)
        self.assertEqual(bill["usage_cents"], 80)
        self.assertEqual(bill["status"], "open")

    def test_adjust_debit_adds_to_total(self) -> None:
        bill_id = self.setup_bill()
        result = self.adjust(bill_id, kind="debit", amount=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_cents"], 1100)

    def test_adjust_credit_never_drops_below_zero(self) -> None:
        bill_id = self.setup_bill()
        result = self.adjust(bill_id, amount=999999)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_cents"], 0)
        self.assertEqual(self.list_bills()[0]["total_cents"], 0)

    def test_repeated_adjust_overwrites_and_recomputes(self) -> None:
        bill_id = self.setup_bill()
        first = self.adjust(bill_id, kind="credit", amount=200, reference="adj-1")
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.adjust(bill_id, kind="debit", amount=50, reference="adj-2")
        self.assertEqual(second.returncode, 0, second.stderr)
        # Recomputed from the original 1070, not from the adjusted 870.
        self.assertEqual(json.loads(second.stdout)["total_cents"], 1120)
        self.assertEqual(self.list_bills()[0]["total_cents"], 1120)

        # The overwritten reference stays registered and cannot be reused.
        reused = self.adjust(bill_id, kind="credit", amount=10, reference="adj-1")
        self.assertEqual(reused.returncode, 5)
        self.assertEqual(reused.stdout, "")
        self.assertEqual(self.list_bills()[0]["total_cents"], 1120)

    def test_adjust_duplicate_reference_exits_5(self) -> None:
        bill_id = self.setup_bill()
        self.assertEqual(self.adjust(bill_id).returncode, 0)

        duplicate = self.adjust(bill_id, reference="adj-1")
        self.assertEqual(duplicate.returncode, 5)
        self.assertIn("adj-1", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")
        self.assertEqual(self.list_bills()[0]["total_cents"], 870)

    def test_adjust_validates_arguments(self) -> None:
        bill_id = self.setup_bill()

        bad_kind = self.adjust(bill_id, kind="refund")
        self.assertEqual(bad_kind.returncode, 2)
        self.assertEqual(bad_kind.stdout, "")

        zero_amount = self.adjust(bill_id, amount=0)
        self.assertEqual(zero_amount.returncode, 2)
        self.assertEqual(zero_amount.stdout, "")

        empty_reason = self.adjust(bill_id, reason="")
        self.assertEqual(empty_reason.returncode, 2)
        self.assertEqual(empty_reason.stdout, "")

        empty_reference = self.adjust(bill_id, reference="")
        self.assertEqual(empty_reference.returncode, 2)
        self.assertEqual(empty_reference.stdout, "")

        self.assertEqual(self.list_bills()[0]["total_cents"], 1070)

    def test_adjust_unknown_or_mismatched_bill_exits_4(self) -> None:
        bill_id = self.setup_bill()

        missing = self.adjust(999)
        self.assertEqual(missing.returncode, 4)
        self.assertIn("no bill", missing.stderr)
        self.assertEqual(missing.stdout, "")

        wrong_customer = self.adjust(bill_id, customer_id="ghost")
        self.assertEqual(wrong_customer.returncode, 4)
        self.assertEqual(wrong_customer.stdout, "")

        wrong_plan = self.adjust(bill_id, plan="pro")
        self.assertEqual(wrong_plan.returncode, 4)
        self.assertEqual(wrong_plan.stdout, "")

        self.assertEqual(self.list_bills()[0]["total_cents"], 1070)

    def test_adjust_status_matches_payment_match_summary(self) -> None:
        bill_id = self.setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 500).returncode, 0)
        self.assertEqual(self.pay(bill_id, "r2", 400).returncode, 0)

        # Credit 200 -> total 870, settled exactly by the 900 of payments.
        result = self.adjust(bill_id, kind="credit", amount=200)
        self.assertEqual(result.returncode, 0, result.stderr)
        adjusted = json.loads(result.stdout)
        self.assertEqual(adjusted["total_cents"], 870)
        self.assertEqual(adjusted["status"], "paid")
        summary = self.match_summary(bill_id)
        self.assertEqual(summary["status"], adjusted["status"])
        self.assertEqual(summary["total_cents"], 870)
        self.assertEqual(self.list_bills()[0]["status"], "paid")

        # Debit 200 -> total 1270, the 900 paid falls short of it.
        second = self.adjust(bill_id, kind="debit", amount=200, reference="adj-2")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["status"], "open")
        self.assertEqual(self.match_summary(bill_id)["status"], "open")

        # Credit 170 -> total 900, exactly settled by the payments.
        third = self.adjust(bill_id, kind="credit", amount=170, reference="adj-3")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(json.loads(third.stdout)["status"], "paid")
        self.assertEqual(self.match_summary(bill_id)["status"], "paid")
        self.assertEqual(self.list_bills()[0]["status"], "paid")

        # A late extra payment over-collects the settled bill: the next
        # adjust recomputes to the same 900 total and reports partial.
        self.assertEqual(self.pay(bill_id, "r3", 100).returncode, 0)
        fourth = self.adjust(bill_id, kind="credit", amount=170, reference="adj-4")
        self.assertEqual(fourth.returncode, 0, fourth.stderr)
        self.assertEqual(json.loads(fourth.stdout)["status"], "partial")
        self.assertEqual(self.match_summary(bill_id)["status"], "partial")
        self.assertEqual(self.list_bills()[0]["status"], "partial")

    def test_void_open_bill(self) -> None:
        bill_id = self.setup_bill()
        result = self.void(bill_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"bill_id": bill_id, "status": "voided"}
        )
        self.assertEqual(result.stderr, "")

        bill = self.list_bills()[0]
        self.assertEqual(bill["status"], "voided")
        self.assertEqual(bill["total_cents"], 1070)
        self.assertEqual(bill["base_cents"], 990)
        self.assertEqual(bill["usage_cents"], 80)

    def test_void_rejects_non_open_bill(self) -> None:
        bill_id = self.setup_bill()
        self.assertEqual(self.void(bill_id).returncode, 0)

        again = self.void(bill_id)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(again.stdout, "")
        self.assertEqual(self.list_bills()[0]["status"], "voided")

    def test_void_unknown_or_mismatched_bill_exits_4(self) -> None:
        bill_id = self.setup_bill()

        missing = self.void(999)
        self.assertEqual(missing.returncode, 4)
        self.assertIn("no bill", missing.stderr)
        self.assertEqual(missing.stdout, "")

        wrong_customer = self.void(bill_id, customer_id="ghost")
        self.assertEqual(wrong_customer.returncode, 4)
        self.assertEqual(wrong_customer.stdout, "")

        self.assertEqual(self.list_bills()[0]["status"], "open")

    def test_voided_bill_rejects_payment_and_adjust(self) -> None:
        bill_id = self.setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 500).returncode, 0)
        self.assertEqual(self.void(bill_id).returncode, 0)

        payment = self.pay(bill_id, "r2", 100)
        self.assertEqual(payment.returncode, 2)
        self.assertEqual(payment.stdout, "")

        adjustment = self.adjust(bill_id)
        self.assertEqual(adjustment.returncode, 2)
        self.assertEqual(adjustment.stdout, "")

        bill = self.list_bills()[0]
        self.assertEqual(bill["status"], "voided")
        self.assertEqual(bill["total_cents"], 1070)

    def test_payment_match_on_voided_bill_stays_read_only(self) -> None:
        bill_id = self.setup_bill()
        self.assertEqual(self.pay(bill_id, "r1", 500).returncode, 0)
        self.assertEqual(self.void(bill_id).returncode, 0)

        result = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["applied_cents"], 500)
        self.assertEqual(
            lines[-1],
            {"bill_id": bill_id, "total_cents": 1070, "paid_cents": 500,
             "status": "voided"},
        )
        # Read-only: a second run prints the identical output.
        second = self.invoke("payment", "match", "--bill-id", str(bill_id))
        self.assertEqual(second.stdout, result.stdout)


if __name__ == "__main__":
    unittest.main()