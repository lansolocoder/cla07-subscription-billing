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
    """End-to-end checks for payment registration and settlement matching."""

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
        self, customer_id: str = "c1", plan: str = "basic", price_cents: int = 990
    ) -> None:
        result = self.invoke(
            "subscription", "create",
            "--customer-id", customer_id,
            "--plan", plan,
            "--price-cents", str(price_cents),
            "--start-date", "2026-01-01",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def record_usage(self, quantity: int, usage_date: str = "2026-01-10",
                    customer_id: str = "c1", plan: str = "basic") -> None:
        result = self.invoke(
            "usage", "record",
            "--customer-id", customer_id, "--plan", plan,
            "--usage-date", usage_date, "--quantity", str(quantity),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def generate_bill(self, customer_id: str = "c1", plan: str = "basic") -> dict:
        result = self.invoke(
            "bill", "generate",
            "--customer-id", customer_id, "--plan", plan,
            "--period-start", "2026-01-01", "--period-end", "2026-01-31",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def pay(self, reference: str, amount: int, bill_id: int = 1,
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

    def match(self, bill_id: int = 1) -> subprocess.CompletedProcess[str]:
        return self.invoke("payment", "match", "--bill-id", str(bill_id))

    def _payment_row_count(self) -> int:
        import sqlite3

        if not self.db_path.exists():
            return 0
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='payments'"
            ).fetchall()
            if not rows:
                return 0
            return int(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0])

    def test_record_payment_outputs_applied_record(self) -> None:
        self.create_subscription()
        bill = self.generate_bill()
        self.assertEqual(bill["total_cents"], 990)

        result = self.pay("r1", 500)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": 1,
                "bill_id": 1,
                "customer_id": "c1",
                "plan": "basic",
                "reference": "r1",
                "amount_cents": 500,
                "payment_date": "2026-02-05",
                "status": "applied",
            },
        )
        self.assertEqual(result.stderr, "")

    def test_payment_ids_are_global_monotonic_and_stable(self) -> None:
        self.create_subscription()
        self.create_subscription(customer_id="c2", plan="pro", price_cents=1990)
        first_bill = self.generate_bill()
        second_bill = self.generate_bill(customer_id="c2", plan="pro")
        self.assertEqual([first_bill["id"], second_bill["id"]], [1, 2])

        first = self.pay("r1", 100, bill_id=1)
        second = self.pay("r2", 200, bill_id=2, customer_id="c2", plan="pro")
        self.assertEqual(json.loads(first.stdout)["id"], 1)
        self.assertEqual(json.loads(second.stdout)["id"], 2)

        # Re-read from a fresh process: ids persist and list follows id order.
        listing = self.invoke("payment", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        rows = json.loads(listing.stdout)
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual([row["reference"] for row in rows], ["r1", "r2"])
        self.assertEqual([row["bill_id"] for row in rows], [1, 2])
        self.assertEqual(len(listing.stdout.splitlines()), 1)

    def test_record_validates_arguments_and_date(self) -> None:
        self.create_subscription()
        self.generate_bill()

        bad_date = self.pay("r1", 500, payment_date="2026-02-30")
        self.assertEqual(bad_date.returncode, 2)
        self.assertEqual(bad_date.stdout, "")
        self.assertIn("payment-date", bad_date.stderr)

        not_a_date = self.pay("r2", 500, payment_date="not-a-date")
        self.assertEqual(not_a_date.returncode, 2)
        self.assertEqual(not_a_date.stdout, "")

        zero = self.pay("r3", 0)
        self.assertEqual(zero.returncode, 2)
        self.assertEqual(zero.stdout, "")

        negative = self.pay("r4", -1)
        self.assertEqual(negative.returncode, 2)
        self.assertEqual(negative.stdout, "")

        not_an_integer = self.pay("r5", 5)  # valid amount used as sanity below
        self.assertEqual(not_an_integer.returncode, 0, not_an_integer.stderr)
        non_integer = self.invoke(
            "payment", "record",
            "--customer-id", "c1", "--plan", "basic", "--bill-id", "1",
            "--amount-cents", "abc",
            "--payment-date", "2026-02-05", "--reference", "r6",
        )
        self.assertEqual(non_integer.returncode, 2)
        self.assertEqual(non_integer.stdout, "")

        empty_reference = self.invoke(
            "payment", "record",
            "--customer-id", "c1", "--plan", "basic", "--bill-id", "1",
            "--amount-cents", "100",
            "--payment-date", "2026-02-05", "--reference", "",
        )
        self.assertEqual(empty_reference.returncode, 2)
        self.assertEqual(empty_reference.stdout, "")

        # Only the one valid payment was persisted.
        self.assertEqual(self._payment_row_count(), 1)

    def test_record_requires_bill_belonging_to_customer_and_plan(self) -> None:
        self.create_subscription()
        self.create_subscription(customer_id="c2", plan="pro", price_cents=1990)
        self.generate_bill()
        self.generate_bill(customer_id="c2", plan="pro")

        missing_bill = self.pay("r1", 100, bill_id=999)
        self.assertEqual(missing_bill.returncode, 4)
        self.assertIn("no bill", missing_bill.stderr)
        self.assertEqual(missing_bill.stdout, "")

        wrong_customer = self.pay("r2", 100, bill_id=1, customer_id="ghost")
        self.assertEqual(wrong_customer.returncode, 4)
        self.assertIn("no bill", wrong_customer.stderr)
        self.assertEqual(wrong_customer.stdout, "")

        wrong_plan = self.pay("r3", 100, bill_id=1, plan="pro")
        self.assertEqual(wrong_plan.returncode, 4)
        self.assertEqual(wrong_plan.stdout, "")

        # Bill 2 belongs to c2/pro, so c1/basic must not register against it.
        cross = self.pay("r4", 100, bill_id=2)
        self.assertEqual(cross.returncode, 4)
        self.assertEqual(cross.stdout, "")

        self.assertEqual(self._payment_row_count(), 0)

    def test_invalid_date_beats_missing_bill(self) -> None:
        # Validation order: argument/date errors (2) before bill lookup (4).
        result = self.pay("r1", 100, bill_id=999, payment_date="2026-02-30")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self._payment_row_count(), 0)

    def test_duplicate_reference_exits_5_without_overwriting(self) -> None:
        self.create_subscription()
        self.create_subscription(customer_id="c2", plan="pro", price_cents=1990)
        self.generate_bill()
        self.generate_bill(customer_id="c2", plan="pro")

        first = self.pay("same-ref", 500, bill_id=1)
        self.assertEqual(first.returncode, 0, first.stderr)

        # The reference is globally unique even across different bills.
        duplicate = self.pay("same-ref", 700, bill_id=2, customer_id="c2", plan="pro")
        self.assertEqual(duplicate.returncode, 5)
        self.assertIn("duplicate", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")

        listing = json.loads(self.invoke("payment", "list").stdout)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["amount_cents"], 500)
        self.assertEqual(listing[0]["bill_id"], 1)

    def test_match_settles_exactly_and_is_idempotent(self) -> None:
        self.create_subscription()
        self.record_usage(8)
        bill = self.generate_bill()
        self.assertEqual(bill["total_cents"], 1070)

        self.assertEqual(self.pay("r1", 500).returncode, 0)
        self.assertEqual(self.pay("r2", 570).returncode, 0)

        result = self.match()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": 1, "reference": "r1", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": 1, "reference": "r2", "amount_cents": 570,
                 "applied_cents": 570, "result": "settled"},
                {"bill_id": 1, "total_cents": 1070, "paid_cents": 1070,
                 "status": "paid"},
            ],
        )

        # Repeating the reconciliation must print identical output and not
        # accumulate write-offs twice.
        repeated = self.match()
        self.assertEqual(repeated.stdout, result.stdout)
        self.assertEqual(self.db_path.read_bytes(),
                         self.db_path.read_bytes())

    def test_match_clips_overshoot_and_marks_further_payments_overpaid(self) -> None:
        self.create_subscription()
        self.record_usage(8)  # total 1070
        self.generate_bill()

        self.pay("r1", 500)
        self.pay("r2", 700)   # 570 needed; clips to exactly 570, settles
        self.pay("r3", 100)   # after settlement: overpaid, applies nothing

        result = self.match()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": 1, "reference": "r1", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": 1, "reference": "r2", "amount_cents": 700,
                 "applied_cents": 570, "result": "settled"},
                {"bill_id": 1, "reference": "r3", "amount_cents": 100,
                 "applied_cents": 0, "result": "overpaid"},
                {"bill_id": 1, "total_cents": 1070, "paid_cents": 1070,
                 "status": "partial"},
            ],
        )

        # Idempotent after overpayment too.
        self.assertEqual(self.match().stdout, result.stdout)

    def test_match_first_payment_already_exceeds_total(self) -> None:
        self.create_subscription()
        self.generate_bill()  # total 990
        self.pay("r1", 1000)
        self.pay("r2", 10)

        lines = [json.loads(line) for line in self.match().stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": 1, "reference": "r1", "amount_cents": 1000,
                 "applied_cents": 990, "result": "settled"},
                {"bill_id": 1, "reference": "r2", "amount_cents": 10,
                 "applied_cents": 0, "result": "overpaid"},
                {"bill_id": 1, "total_cents": 990, "paid_cents": 990,
                 "status": "partial"},
            ],
        )

    def test_match_underpaid_bill_stays_open(self) -> None:
        self.create_subscription()
        self.generate_bill()  # total 990
        self.pay("r1", 400)
        self.pay("r2", 500)

        lines = [json.loads(line) for line in self.match().stdout.splitlines()]
        self.assertEqual(
            lines,
            [
                {"bill_id": 1, "reference": "r1", "amount_cents": 400,
                 "applied_cents": 400, "result": "underpaid"},
                {"bill_id": 1, "reference": "r2", "amount_cents": 500,
                 "applied_cents": 500, "result": "underpaid"},
                {"bill_id": 1, "total_cents": 990, "paid_cents": 900,
                 "status": "open"},
            ],
        )

    def test_match_without_payments_only_prints_summary(self) -> None:
        self.create_subscription()
        self.generate_bill()  # total 990

        result = self.match()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {"bill_id": 1, "total_cents": 990, "paid_cents": 0, "status": "open"},
        )
        self.assertEqual(self.match().stdout, result.stdout)

    def test_match_unknown_bill_exits_4(self) -> None:
        result = self.match(999)
        self.assertEqual(result.returncode, 4)
        self.assertIn("no bill", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_match_is_read_only(self) -> None:
        self.create_subscription()
        self.record_usage(8)
        self.generate_bill()
        self.pay("r1", 500)
        before = self.db_path.read_bytes()
        result = self.match()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db_path.read_bytes(), before)
        # Running it repeatedly leaves the file byte-identical.
        self.match()
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_match_scoped_to_one_bill_while_list_spans_all(self) -> None:
        self.create_subscription()
        self.create_subscription(customer_id="c2", plan="pro", price_cents=1990)
        bill_one = self.generate_bill()
        bill_two = self.generate_bill(customer_id="c2", plan="pro")
        self.assertEqual([bill_one["id"], bill_two["id"]], [1, 2])

        self.pay("r1", 990, bill_id=1)
        self.pay("r2", 100, bill_id=2, customer_id="c2", plan="pro")

        match_one = [json.loads(line) for line in self.match(1).stdout.splitlines()]
        self.assertEqual(
            match_one,
            [
                {"bill_id": 1, "reference": "r1", "amount_cents": 990,
                 "applied_cents": 990, "result": "settled"},
                {"bill_id": 1, "total_cents": 990, "paid_cents": 990,
                 "status": "paid"},
            ],
        )
        match_two = [json.loads(line) for line in self.match(2).stdout.splitlines()]
        self.assertEqual(
            match_two,
            [
                {"bill_id": 2, "reference": "r2", "amount_cents": 100,
                 "applied_cents": 100, "result": "underpaid"},
                {"bill_id": 2, "total_cents": 1990, "paid_cents": 100,
                 "status": "open"},
            ],
        )

    def test_payment_list_empty_outputs_empty_array(self) -> None:
        result = self.invoke("payment", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertEqual(len(result.stdout.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
