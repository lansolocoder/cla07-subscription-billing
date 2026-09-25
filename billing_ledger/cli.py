"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from . import subscriptions, usage


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
    create.add_argument(
        "--trial-days",
        type=int,
        default=None,
        help="试用天数，正整数表示试用；0 或缺省且未给 --trial-end-date 时无试用.",
    )
    create.add_argument(
        "--trial-end-date",
        help="试用结束日期（含），YYYY-MM-DD；与 --trial-days 互斥，缺省表示无试用.",
    )

    subscription_subparsers.add_parser("list", help="列出全部订阅.")

    trial = subscription_subparsers.add_parser(
        "trial", aliases=["get"], help="查询单条订阅的试用信息."
    )
    trial.add_argument("--customer-id", required=True, help="客户标识，非空.")
    trial.add_argument("--plan", required=True, help="订阅计划，非空.")

    activate = subscription_subparsers.add_parser("activate", help="试用转正.")
    activate.add_argument("--customer-id", required=True, help="客户标识，非空.")
    activate.add_argument("--plan", required=True, help="订阅计划，非空.")
    activate.add_argument(
        "--as-of",
        help="转正生效日期，YYYY-MM-DD，缺省为今天 UTC；早于转正日期时拒绝.",
    )

    modify = subscription_subparsers.add_parser(
        "modify", help="变更试用订阅的计划、价格或试用结束日期."
    )
    modify.add_argument("--customer-id", required=True, help="客户标识，非空.")
    modify.add_argument("--plan", required=True, help="当前订阅计划，非空.")
    modify.add_argument("--plan-new", help="变更后的计划名，非空，且须与当前计划不同.")
    modify.add_argument(
        "--price-cents",
        type=_non_negative_int,
        default=None,
        help="变更后的价格，单位分，非负整数.",
    )
    modify.add_argument(
        "--trial-end-date",
        default=None,
        help="变更后的试用结束日期（含），YYYY-MM-DD；不得早于开始日期、今天 UTC 或旧结束日期.",
    )

    usage_parser = subparsers.add_parser("usage", help="登记与查询用量.")
    usage_subparsers = usage_parser.add_subparsers(dest="usage_command")

    record = usage_subparsers.add_parser("record", help="录入一条或多条用量记录.")
    record.add_argument("--customer-id", required=True, help="客户标识，非空.")
    record.add_argument("--plan", required=True, help="订阅计划，非空.")
    record.add_argument(
        "--usage-date",
        required=True,
        action="append",
        help="用量发生日期，YYYY-MM-DD；可与 --quantity 成对重复指定多条.",
    )
    record.add_argument(
        "--quantity",
        required=True,
        action="append",
        help="用量数值，非负整数；可与 --usage-date 成对重复指定多条.",
    )

    usage_list = usage_subparsers.add_parser("list", help="按 id 升序列出某订阅的逐条用量.")
    usage_list.add_argument("--customer-id", required=True, help="客户标识，非空.")
    usage_list.add_argument("--plan", required=True, help="订阅计划，非空.")

    summary = usage_subparsers.add_parser("summary", help="按日汇总某订阅的用量.")
    summary.add_argument("--customer-id", required=True, help="客户标识，非空.")
    summary.add_argument("--plan", required=True, help="订阅计划，非空.")
    summary.add_argument("--start-date", help="开始日期（含），YYYY-MM-DD，缺省覆盖全部历史.")
    summary.add_argument("--end-date", help="结束日期（含），YYYY-MM-DD，缺省覆盖全部历史.")

    reconcile = usage_subparsers.add_parser("reconcile", help="按计费周期生成用量对账明细.")
    reconcile.add_argument("--customer-id", required=True, help="客户标识，非空.")
    reconcile.add_argument("--plan", required=True, help="订阅计划，非空.")
    reconcile.add_argument("--start-date", required=True, help="计费周期起始日期（含），YYYY-MM-DD.")
    reconcile.add_argument("--end-date", required=True, help="计费周期结束日期（含），YYYY-MM-DD.")

    return parser


def _parse_entries(raw_dates: list[str], raw_quantities: list[str]) -> list[tuple[str, int]] | None:
    if len(raw_dates) != len(raw_quantities):
        print(
            "billing-ledger: error: --usage-date and --quantity must be given the same"
            f" number of times ({len(raw_dates)} dates vs {len(raw_quantities)} quantities)",
            file=sys.stderr,
        )
        return None
    entries: list[tuple[str, int]] = []
    for usage_date, raw_quantity in zip(raw_dates, raw_quantities, strict=True):
        try:
            quantity = int(raw_quantity)
        except ValueError:
            print(f"billing-ledger: error: invalid non-negative integer: {raw_quantity!r}", file=sys.stderr)
            return None
        if quantity < 0:
            print(f"billing-ledger: error: must be a non-negative integer: {raw_quantity!r}", file=sys.stderr)
            return None
        entries.append((usage_date, quantity))
    return entries


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
                trial_end_date=args.trial_end_date,
            )
        if args.subscription_command == "list":
            return subscriptions.list_all()
        if args.subscription_command in ("trial", "get"):
            return subscriptions.get(customer_id=args.customer_id, plan=args.plan)
        if args.subscription_command == "activate":
            return subscriptions.activate(
                customer_id=args.customer_id,
                plan=args.plan,
                as_of=args.as_of,
            )
        if args.subscription_command == "modify":
            return subscriptions.modify(
                customer_id=args.customer_id,
                plan=args.plan,
                plan_new=args.plan_new,
                price_cents=args.price_cents,
                trial_end_date=args.trial_end_date,
            )

    if args.command == "usage":
        if args.usage_command == "record":
            entries = _parse_entries(args.usage_date, args.quantity)
            if entries is None:
                return 2
            return usage.record(
                customer_id=args.customer_id,
                plan=args.plan,
                entries=entries,
            )
        if args.usage_command == "list":
            return usage.list_records(customer_id=args.customer_id, plan=args.plan)
        if args.usage_command == "summary":
            return usage.summary(
                customer_id=args.customer_id,
                plan=args.plan,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        if args.usage_command == "reconcile":
            return usage.reconcile(
                customer_id=args.customer_id,
                plan=args.plan,
                start_date=args.start_date,
                end_date=args.end_date,
            )

    parser.print_help()
    return 0
