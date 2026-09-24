"""Command-line entry point."""

import argparse
from collections.abc import Sequence

from . import __version__
from . import subscriptions


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid non-negative integer: {raw!r}")
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer: {raw!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="billing-ledger",
        description="Local 订阅计费与账单对账 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")
    subscription = subparsers.add_parser("subscription", help="登记与查询订阅.")
    subscription_subparsers = subscription.add_subparsers(dest="subscription_command")

    create = subscription_subparsers.add_parser("create", help="登记一条新订阅.")
    create.add_argument("--customer-id", required=True, help="客户标识，非空.")
    create.add_argument("--plan", required=True, help="订阅计划，非空.")
    create.add_argument(
        "--price-cents", required=True, type=_non_negative_int, help="价格，单位分，非负整数."
    )
    create.add_argument("--start-date", required=True, help="开始日期，YYYY-MM-DD.")
    create.add_argument("--trial-days", type=int, default=0, help="试用天数，0 表示无试用，缺省 0.")

    subscription_subparsers.add_parser("list", help="列出全部订阅.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "subscription":
        if args.subscription_command == "create":
            return subscriptions.create(
                customer_id=args.customer_id,
                plan=args.plan,
                price_cents=args.price_cents,
                start_date=args.start_date,
                trial_days=args.trial_days,
            )
        if args.subscription_command == "list":
            return subscriptions.list_all()

    parser.print_help()
    return 0
