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
# 不带试用参数（或显式 --trial-days 0）：无试用，状态 active，转正日期即 start_date
python3 -m billing_ledger subscription create \
  --customer-id c1 --plan basic --price-cents 990 --start-date 2026-01-01

# 按试用天数登记：试用区间 [start_date, start_date+trial_days-1]，
# 转正日期为 start_date+trial_days，状态 trial
python3 -m billing_ledger subscription create \
  --customer-id c2 --plan pro --price-cents 1990 \
  --start-date 2026-01-01 --trial-days 14

# 按试用结束日期登记：试用区间 [start_date, trial_end_date]，
# 转正日期为 trial_end_date+1 天，trial_days 按区间天数回填
python3 -m billing_ledger subscription create \
  --customer-id c3 --plan pro --price-cents 1990 \
  --start-date 2026-01-01 --trial-end-date 2026-01-14

# 列出全部订阅（单行 JSON 数组）
python3 -m billing_ledger subscription list
```

- `--trial-days` 与 `--trial-end-date` 至少给一个即可，两者同时给出视为参数错误；
  两者都缺省（或 `--trial-days 0` 且无 `--trial-end-date`）表示无试用。
- `--trial-end-date` 须为合法 `YYYY-MM-DD`，不早于 `start_date`，且不早于今天 UTC。

## 试用查询与转正

```bash
# 查询单条订阅的试用信息（一行紧凑 JSON；订阅不存在时 stderr、退出码 4）
python3 -m billing_ledger subscription trial --customer-id c2 --plan pro
# {"customer_id":"c2","plan":"pro","start_date":"2026-01-01","trial_days":14,
#  "trial_end_date":"2026-01-14","trial_to_active_date":"2026-01-15","status":"trial"}
# 无试用时 trial_days 为 0、trial_end_date 为 null、status 为 active。
# `subscription get` 为 `subscription trial` 的别名。

# 试用转正：--as-of 缺省为今天 UTC；as_of 早于转正日期时拒绝（退出码 2），
# 不早于转正日期时状态变为 active，activated_on 为实际生效的 as_of 日期
python3 -m billing_ledger subscription activate \
  --customer-id c2 --plan pro --as-of 2026-01-15
# {"customer_id":"c2","plan":"pro","status":"active","activated_on":"2026-01-15"}
```

## 订阅变更

```bash
# 变更试用订阅：按 customer_id 与 plan 定位，--plan-new / --price-cents /
# --trial-end-date 至少给一项，可多项同时指定、整批原子生效
python3 -m billing_ledger subscription change \
  --customer-id c2 --plan pro --plan-new enterprise --price-cents 2990 \
  --trial-end-date 2026-01-20
# {"customer_id":"c2","plan":"enterprise","trial_days":20,
#  "trial_end_date":"2026-01-20","trial_to_active_date":"2026-01-21","status":"trial"}
```

- 仅状态为 `trial` 的订阅允许变更；`active` 订阅变更报错（stderr、退出码 2）。
- `--plan-new` 须非空且不同于当前 plan；`--price-cents` 须为非负整数；
  `--trial-end-date` 须为合法 `YYYY-MM-DD`，不早于 `start_date`、不早于今天 UTC，
  也不早于当前试用结束日期。
- 变更试用结束日期后，`trial_days` 按 `[start_date, 新结束日期]` 的天数重新计算，
  转正日期为新结束日期加一天，`status` 仍为 `trial`。
- 计划改名后按新组合定位；若 `customer_id` 与新计划名的组合已存在则整批拒绝、
  不覆盖（stderr、退出码 4）；已录入的逐条用量与按日汇总不受影响。
- 一次提交同时命中多类错误时按优先级报错：参数或日期非法、对 active 订阅变更、
  新结束日期早于旧结束日期（均为 stderr、退出码 2），均未命中才判找不到订阅或
  改名冲突（stderr、退出码 4）；任何失败都不写入数据。

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
# 为 [period-start, period-end] 闭区间生成账单，stdout 输出一行紧凑 JSON，退出码 0
python3 -m billing_ledger bill generate --customer-id c1 --plan basic \
  --period-start 2026-01-01 --period-end 2026-01-31
# {"id":1,"customer_id":"c1","plan":"basic","period_start":"2026-01-01",
#  "period_end":"2026-01-31","base_cents":990,"usage_cents":80,"total_cents":1070,
#  "status":"open"}

# 列出某订阅的全部账单（单行 JSON 数组，按 id 升序；无账单时输出 []）
python3 -m billing_ledger bill list --customer-id c1 --plan basic
```

- 仅状态为 `active` 的订阅允许生成账单；`trial` 订阅拒绝（stderr、退出码 2，不写入数据）。
- 账单业务键为 `(customer_id, plan, period_start, period_end)`：重复生成同一业务键为
  幂等读回——不新建账单、不改既有金额与状态，stdout 输出既有账单记录、退出码 0。
- 基础费按周期内计费天数分摊：`base_cents = floor(price_cents × 计费天数 ÷ 周期日数)`；
  计费天数为周期内不早于 `max(start_date, trial_to_active_date)` 的日期数，区间外与
  试用期内日期不计费；周期日数为周期日历天数。
- 用量费为周期内用量总量（`cycle_total`）乘以 10 分；`total_cents` 为基础费与用量费
  之和，无分摊或用量时可为 0。
- 账单与订阅、用量同库持久化；账单号在同一数据库中全局唯一、单调分配、跨进程稳定，
  新账单状态恒为 `open`。
- 日期非法或 `period_start` 晚于 `period_end`：stderr、退出码 2，不写入数据；
  找不到匹配的订阅组合：stderr、退出码 4，不写入数据。

所有成功与查询输出均写入 stdout 且为单行 JSON；错误信息写入 stderr。
退出码约定：参数或日期非法为 2（不写入数据）；重复登记同一 `customer_id` 与 `plan`
组合为 3（不覆盖已有订阅）；找不到匹配的订阅组合为 4；成功为 0。
尚未实现收款核对与差异归集。
