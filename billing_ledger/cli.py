"""Command-line entry point."""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timezone

from . import __version__
from . import ledger


def _today_utc() -> object:
    return datetime.now(timezone.utc).date()


def _build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(
        prog="billing-ledger",
        description="Local 订阅计费与账单对账 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    resource_subparsers = parser.add_subparsers(dest="resource", metavar="resource")
    subscription = resource_subparsers.add_parser(
        "subscription", help="订阅登记与查询"
    )
    action_subparsers = subscription.add_subparsers(dest="action")

    create = action_subparsers.add_parser("create", help="登记一条订阅")
    create.add_argument("--customer-id", required=True)
    create.add_argument("--plan", required=True)
    create.add_argument("--price-cents", required=True, type=int)
    create.add_argument("--start-date", required=True)
    create.add_argument("--trial-days", type=int, default=0)

    action_subparsers.add_parser("list", help="列出全部订阅")

    return parser, subscription


def _fail(message: str) -> int:
    print(f"billing-ledger: error: {message}", file=sys.stderr)
    return 2


def _run_create(args: argparse.Namespace) -> int:
    customer_id = args.customer_id
    plan = args.plan
    if customer_id == "":
        return _fail("--customer-id must not be empty")
    if plan == "":
        return _fail("--plan must not be empty")

    price_cents = args.price_cents
    if price_cents < 0:
        return _fail("--price-cents must be a non-negative integer")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.start_date):
        return _fail("--start-date must use the format YYYY-MM-DD")
    try:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    except ValueError:
        return _fail("--start-date must use the format YYYY-MM-DD")

    if start_date > _today_utc():
        return _fail(
            f"--start-date {args.start_date} must not be later than today (UTC)"
        )

    trial_days = args.trial_days
    try:
        record = ledger.add_subscription(
            customer_id=customer_id,
            plan=plan,
            price_cents=price_cents,
            start_date=start_date.isoformat(),
            trial_days=trial_days,
        )
    except ledger.DuplicateSubscriptionError:
        print(
            "billing-ledger: error: duplicate subscription for "
            f"customer_id={customer_id!r} plan={plan!r}",
            file=sys.stderr,
        )
        return 3

    print(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
    return 0


def _run_list() -> int:
    print(
        json.dumps(
            ledger.list_subscriptions(), separators=(",", ":"), ensure_ascii=False
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser, subscription = _build_parser()
    args = parser.parse_args(argv)

    if args.resource is None:
        parser.print_help()
        return 0
    if args.action is None:
        subscription.print_help()
        return 0
    if args.action == "create":
        return _run_create(args)
    return _run_list()
