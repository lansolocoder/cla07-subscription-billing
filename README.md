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

## 账单生成

```bash
# 为闭区间 [start-date, end-date] 生成账单；成功时 stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger invoice generate --customer-id c1 --plan basic \
  --start-date 2026-01-01 --end-date 2026-01-31
# {"invoice_id":1,"customer_id":"c1","plan":"basic","period_start":"2026-01-01",
#  "period_end":"2026-01-31","billed_usage":80,"subtotal_cents":79200,
#  "discount_percent":0,"total_cents":79200,"trailing_days":7}
```

- 业务键为 `(customer_id, plan, period_start, period_end)`：同参数重复生成
  幂等返回既有账单（含首次分配的 `invoice_id`），不重复计费、不重新计价；
  不同周期互不影响。
- `invoice_id` 在同一数据库中全局唯一、单调分配（SQLite AUTOINCREMENT）。
- 周期内同一日期的多条用量记录先按日求和，再对计费用量求和；
  `billed_usage` 为计费用量总量，`subtotal_cents = billed_usage × price_cents`。
- 自订阅 `start_date`（含当天）起连续 `trial_days` 天为试用，试用日用量
  不计费，试用结束次日起计费。`trailing_days` 为试用区间与本周期的重叠
  日期数；试用区间与周期不重叠时为 0。
- 周期起止无须早于/晚于订阅 `start_date`：试用只按其与周期的实际重叠生效。
- 日期非法或 `start_date` 晚于 `end_date`：stderr、退出码 2；
  找不到匹配的订阅组合：stderr、退出码 4。

### 按比例折扣登记

```bash
# 为指定账单业务键登记一次折扣；成功无 stdout 输出，退出码 0
python3 -m billing_ledger invoice register-discount --customer-id c1 --plan basic \
  --start-date 2026-01-01 --end-date 2026-01-31 --discount-percent 20
```

- 折扣为 0–100 的整数百分点；20 表示按八折计费。每个业务键至多登记一次，
  重复登记拒绝（stderr、退出码 3），原折扣保持不变；未登记按 0。
- 仅当折扣前金额（`billed_usage × price_cents`，同样剔除试用日）严格大于 0
  时才可登记，否则 stderr、退出码 3。
- `--discount-percent` 非整数或越界：stderr、退出码 3；
  日期非法或起止颠倒：stderr、退出码 3；找不到订阅：stderr、退出码 4。
- 任一失败都不写入折扣记录。
- 折扣可在账单生成之前或之后登记：已生成账单的值不会被追溯改写；之后首次
  生成的账单按
  `total_cents = subtotal_cents × (100 − discount_percent) ÷ 100`
  向下取整到整数分。

所有成功与查询输出均写入 stdout 且为单行 JSON（折扣登记成功无输出）；
错误信息写入 stderr。
收款核对与差异归集尚未实现。
