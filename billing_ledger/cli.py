"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from . import bills, payments, subscriptions, usage


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid non-negative integer: {raw!r}")
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer: {raw!r}")
    return value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid positive integer: {raw!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {raw!r}")
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

    change = subscription_subparsers.add_parser(
        "change", help="变更试用订阅的计划、价格或试用结束日期（整批原子生效）."
    )
    change.add_argument("--customer-id", required=True, help="客户标识，非空.")
    change.add_argument("--plan", required=True, help="当前订阅计划，非空.")
    change.add_argument("--plan-new", default=None, help="新计划名，非空且须不同于当前计划.")
    change.add_argument(
        "--price-cents",
        type=_non_negative_int,
        default=None,
        help="新价格，单位分，非负整数.",
    )
    change.add_argument(
        "--trial-end-date", default=None, help="新试用结束日期（含），YYYY-MM-DD."
    )

    activate = subscription_subparsers.add_parser("activate", help="试用转正.")
    activate.add_argument("--customer-id", required=True, help="客户标识，非空.")
    activate.add_argument("--plan", required=True, help="订阅计划，非空.")
    activate.add_argument(
        "--as-of",
        help="转正生效日期，YYYY-MM-DD，缺省为今天 UTC；早于转正日期时拒绝.",
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

    bill = subparsers.add_parser("bill", help="生成与查询账单.")
    bill_subparsers = bill.add_subparsers(dest="bill_command")

    generate = bill_subparsers.add_parser("generate", help="生成一个计费周期的账单（幂等）.")
    generate.add_argument("--customer-id", required=True, help="客户标识，非空.")
    generate.add_argument("--plan", required=True, help="订阅计划，非空.")
    generate.add_argument("--period-start", required=True, help="计费周期起始日期（含），YYYY-MM-DD.")
    generate.add_argument("--period-end", required=True, help="计费周期结束日期（含），YYYY-MM-DD.")

    bill_list = bill_subparsers.add_parser("list", help="按 id 升序列出某订阅的全部账单.")
    bill_list.add_argument("--customer-id", required=True, help="客户标识，非空.")
    bill_list.add_argument("--plan", required=True, help="订阅计划，非空.")

    payment = subparsers.add_parser("payment", help="收款登记与账单结清核对.")
    payment_subparsers = payment.add_subparsers(dest="payment_command")

    payment_record = payment_subparsers.add_parser("record", help="登记一笔收款.")
    payment_record.add_argument("--customer-id", required=True, help="客户标识，非空.")
    payment_record.add_argument("--plan", required=True, help="订阅计划，非空.")
    payment_record.add_argument("--bill-id", required=True, type=int, help="账单号，正整数.")
    payment_record.add_argument(
        "--amount-cents",
        required=True,
        type=_positive_int,
        help="收款金额，单位分，正整数.",
    )
    payment_record.add_argument("--payment-date", required=True, help="收款日期，YYYY-MM-DD.")
    payment_record.add_argument("--reference", required=True, help="收款凭证号，全局唯一、非空.")

    payment_match = payment_subparsers.add_parser("match", help="按登记顺序逐笔核对某账单的收款.")
    payment_match.add_argument("--bill-id", required=True, type=int, help="账单号，正整数.")

    payment_subparsers.add_parser("list", help="按登记顺序列出全部收款.")

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
        if args.subscription_command == "change":
            return subscriptions.change(
                customer_id=args.customer_id,
                plan=args.plan,
                plan_new=args.plan_new,
                price_cents=args.price_cents,
                trial_end_date=args.trial_end_date,
            )
        if args.subscription_command == "activate":
            return subscriptions.activate(
                customer_id=args.customer_id,
                plan=args.plan,
                as_of=args.as_of,
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

    if args.command == "bill":
        if args.bill_command == "generate":
            return bills.generate(
                customer_id=args.customer_id,
                plan=args.plan,
                period_start=args.period_start,
                period_end=args.period_end,
            )
        if args.bill_command == "list":
            return bills.list_bills(customer_id=args.customer_id, plan=args.plan)

    if args.command == "payment":
        if args.payment_command == "record":
            return payments.record(
                customer_id=args.customer_id,
                plan=args.plan,
                bill_id=args.bill_id,
                amount_cents=args.amount_cents,
                payment_date=args.payment_date,
                reference=args.reference,
            )
        if args.payment_command == "match":
            return payments.match(bill_id=args.bill_id)
        if args.payment_command == "list":
            return payments.list_payments()

    parser.print_help()
    return 0
