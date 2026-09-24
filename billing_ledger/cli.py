"""Command-line entry point."""

import argparse
from collections.abc import Sequence

from . import __version__
from . import subscriptions
from . import usage


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

    usage_parser = subparsers.add_parser("usage", help="用量登记与查询.")
    usage_subparsers = usage_parser.add_subparsers(dest="usage_command")

    record = usage_subparsers.add_parser("record", help="录入一条用量记录.")
    record.add_argument("--customer-id", required=True, help="客户标识，非空.")
    record.add_argument("--plan", required=True, help="订阅计划，非空.")
    record.add_argument("--usage-date", required=True, help="用量发生日期，YYYY-MM-DD.")
    record.add_argument(
        "--quantity", required=True, type=_non_negative_int, help="用量数值，非负整数."
    )

    usage_list = usage_subparsers.add_parser("list", help="逐条列出订阅的用量记录.")
    usage_list.add_argument("--customer-id", required=True, help="客户标识，非空.")
    usage_list.add_argument("--plan", required=True, help="订阅计划，非空.")

    summary = usage_subparsers.add_parser("summary", help="按日汇总订阅用量.")
    summary.add_argument("--customer-id", required=True, help="客户标识，非空.")
    summary.add_argument("--plan", required=True, help="订阅计划，非空.")
    summary.add_argument("--start-date", help="汇总起始日期（含），YYYY-MM-DD.")
    summary.add_argument("--end-date", help="汇总结束日期（含），YYYY-MM-DD.")

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

    if args.command == "usage":
        if args.usage_command == "record":
            return usage.record(
                customer_id=args.customer_id,
                plan=args.plan,
                usage_date=args.usage_date,
                quantity=args.quantity,
            )
        if args.usage_command == "list":
            return usage.list_records(customer_id=args.customer_id, plan=args.plan)
        if args.usage_command == "summary":
            return usage.summarize(
                customer_id=args.customer_id,
                plan=args.plan,
                start_date=args.start_date,
                end_date=args.end_date,
            )

    parser.print_help()
    return 0
