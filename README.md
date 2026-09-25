# 订阅计费与账单对账

用于本地订阅用量计费与账单对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m billing_ledger --help
python3 -m billing_ledger --version
python3 -m unittest discover -s tests -v
```

数据持久化在仓库根目录的 `ledger.db`（SQLite，已在 `.gitignore` 中忽略）；
设置环境变量 `BILLING_LEDGER_DB` 可改用其他数据库文件路径。

## 订阅登记与查询

```bash
# 登记一条订阅（start_date 须为合法 YYYY-MM-DD 且不晚于今天 UTC）
python3 -m billing_ledger subscription create \
  --customer-id c1 --plan basic --price-cents 990 --start-date 2026-01-01

# 列出全部订阅（单行 JSON 数组）
python3 -m billing_ledger subscription list
```

## 用量录入

```bash
# 录入一条用量：成功时 stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger usage record \
  --customer-id c1 --plan basic --usage-date 2026-01-10 --quantity 5
# {"id":1,"customer_id":"c1","plan":"basic","usage_date":"2026-01-10","quantity":5}
```

- 用量记录与订阅同库持久化；记录号在同一数据库中全局唯一、单调分配、跨进程稳定。
- 日期规则与订阅登记一致（合法 `YYYY-MM-DD`，不晚于今天 UTC），且不得早于该订阅的
  `start_date`：非法日期或早于开始日期写 stderr、退出码 2，不写入数据。
- 找不到匹配的 `customer_id` 与 `plan` 订阅组合：stderr、退出码 4，不写入数据。
- 同一订阅同一日期允许跨提交重复录入多条（汇总时求和）。
- 一次提交可重复成对指定 `--usage-date` 与 `--quantity` 录入多条；若同一次提交中出现
  同订阅同日期的多条记录，则整批原子拒绝：stderr 提示重复、退出码 3，不写入任何一条。

```bash
# 一次提交多条（每条成功记录输出一行 JSON）
python3 -m billing_ledger usage record --customer-id c1 --plan basic \
  --usage-date 2026-01-11 --quantity 2 \
  --usage-date 2026-01-12 --quantity 4
```

## 逐条用量查询

```bash
# 按 id 升序输出单行 JSON 数组；订阅不存在时 stderr、退出码 4
python3 -m billing_ledger usage list --customer-id c1 --plan basic
# [{"id":1,"usage_date":"2026-01-10","quantity":5},...]
```

## 按日汇总

```bash
# 覆盖全部历史用量，按 usage_date 升序，仅列出有用量记录的日期
python3 -m billing_ledger usage summary --customer-id c1 --plan basic
# [{"usage_date":"2026-01-10","total":8},...]

# 可选闭区间范围（含端点）；日期非法或开始晚于结束时 stderr、退出码 2
python3 -m billing_ledger usage summary --customer-id c1 --plan basic \
  --start-date 2026-01-10 --end-date 2026-01-31
```

## 计费周期用量对账明细

```bash
# 生成 [start-date, end-date] 闭区间的对账明细，stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger usage reconcile --customer-id c1 --plan basic \
  --start-date 2026-01-01 --end-date 2026-01-31
# {"subscription_id":1,"period_start":"2026-01-01","period_end":"2026-01-31",
#  "days":[{"usage_date":"2026-01-01","total":0,"missing":true},...],
#  "cycle_total":8,"coverage":{"present_days":2,"missing_days":29}}
```

- `days` 按 `usage_date` 升序覆盖周期内每一个日期：无记录的日期 `total` 为 0 且
  `missing` 为 `true`；同订阅同日期的多条用量求和。
- `cycle_total` 为周期内全部日期 `total` 之和；`coverage` 统计有/无用量记录的日期数，
  两者之和恒等于周期内日期总数。
- 周期起止与订阅 `start_date` 无须相互约束：早于订阅开始日期的日期照常出现并标记缺失。
- 该查询不产生任何持久化写入。
- 日期非法或 `start_date` 晚于 `end_date`：stderr、退出码 2；
  找不到匹配的订阅组合：stderr、退出码 4。

## 账单生成与查询

```bash
# 为某订阅生成 [period-start, period-end] 闭区间的账单，
# stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger billing generate --customer-id c1 --plan basic \
  --period-start 2026-01-01 --period-end 2026-01-31
# {"id":1,"subscription_id":1,"period_start":"2026-01-01","period_end":"2026-01-31",
#  "billable_quantity":8,"amount_cents":990,"status":"issued"}

# 按 id 升序列出该订阅的全部账单（单行 JSON 数组）
python3 -m billing_ledger billing list --customer-id c1 --plan basic
```

- 计费规则：`billable_quantity` 为周期内该订阅全部用量记录（同日多条求和）之和，
  但试用期（`start_date` 当天为第 1 天、连续 `trial_days` 天）内的用量不计费；
  可计费用量为 0 时 `amount_cents` 为 0，用量大于 0 时每 100 单位向上取整为 1 个
  计费段，`amount_cents` = 段数 × `price_cents`。周期允许早于订阅 `start_date`，
  这些日期用量为 0。
- 账单与订阅、用量同库持久化；`id` 全局唯一、单调分配。
- 同一订阅同一 `(period_start, period_end)` 重复生成：stderr、退出码 3，不写入任何记录。
- 日期非法或 `period-start` 晚于 `period-end`：stderr、退出码 2；
  找不到匹配的订阅组合：stderr、退出码 4。任何失败均不改动既有账单与用量数据。

## 收款登记与差异归集

```bash
# 登记一笔收款并匹配到指定账单，stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger billing payment --customer-id c1 --plan basic \
  --bill-id 1 --amount-cents 990 --payment-ref pay-2026-001
# {"id":1,"bill_id":1,"amount_cents":990,"payment_ref":"pay-2026-001","status":"applied"}

# 读回指定账单的收款差异（单行紧凑 JSON，退出码 0）
python3 -m billing_ledger billing payment-status --customer-id c1 --plan basic --bill-id 1
# {"bill_id":1,"amount_cents":990,"paid_cents":990,"balance_cents":0,"status":"paid"}
```

- `bill-id` 必须属于该 `customer-id` 与 `plan` 的订阅；`amount-cents` 为 ≥1 整数；
  `payment-ref` 非空。收款与账单同库持久化，`id` 全局唯一、单调分配。
- 同一 `payment-ref` 全库只允许匹配一次（含原样重跑）：stderr、退出码 3，
  不写新记录、不改动既有数据。同一账单可累计多笔收款。
- `payment-status` 中 `paid_cents` 为该账单收款合计，`balance_cents` =
  `amount_cents` − `paid_cents`（可为负）；`status` 仅按 `balance_cents` 取值：
  大于 0 为 `unpaid`，等于 0 为 `paid`，小于 0 为 `overpaid`。
- 参数或金额非法：stderr、退出码 2；重复流水号：退出码 3；找不到订阅或账单
  （含 `bill-id` 不属该订阅）：退出码 4。任何失败均不改动既有账单、收款与用量数据。

所有成功与查询输出均写入 stdout 且为单行 JSON；错误信息写入 stderr。
