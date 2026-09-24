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

所有成功与查询输出均写入 stdout 且为单行 JSON；错误信息写入 stderr。

## 账单生成

```bash
# 按已有订阅与用量为指定计费周期生成一张订阅账单
# 成功时 stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger invoice generate --customer-id c1 --plan basic \
  --start-date 2026-02-01 --end-date 2026-02-28
# {"subscription_id":1,"period_start":"2026-02-01","period_end":"2026-02-28",
#  "days":[{"usage_date":"2026-02-01","quantity":0,"billable":true,"amount_cents":0},...],
#  "subtotal_cents":0,"trial_days_used":0}
```

- 周期内每天按该订阅 `price_cents`（每天每单位的分）乘以当日用量合计计价；
  无用量日期 `quantity` 为 0、金额 0 分。
- 自订阅 `start_date`（含）起连续 `trial_days` 天为试用期：落在试用期的日期
  `billable` 为 `false`、`amount_cents` 为 0，但用量照常计入 `quantity`；
  `trial_days_used` 为周期内落在试用期的天数。
- `days` 按日期升序覆盖周期内每个日期（与订阅开始日期无须相互约束）；
  `subtotal_cents` 为各计费日金额合计。
- 账单持久化在同一 ledger 中。同一订阅同一周期（`customer_id`+`plan`+
  `period_start`+`period_end`）唯一：重复生成时 stderr、退出码 3，
  不覆盖、不新增记录，既有账单保持原样可读回。
- 日期非法、晚于今天 UTC，或 `start_date` 晚于 `end_date`：stderr、退出码 2，
  不写入任何数据；找不到匹配的订阅组合：stderr、退出码 4，同样不写入。
- 生成账单不会修改或删除任何用量记录。

尚未实现收款核对与差异归集。
