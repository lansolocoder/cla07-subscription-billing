# 订阅计费与账单对账

用于本地订阅用量计费与账单对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m billing_ledger --help
python3 -m billing_ledger --version
python3 -m unittest discover -s tests -v
```

当前提供订阅登记与查询、用量录入、逐条用量查询与按日汇总；尚未实现账单生成以及收款核对与差异归集。

```bash
# 订阅
python3 -m billing_ledger subscription create --customer-id c1 --plan pro --price-cents 500 --start-date 2026-01-01
python3 -m billing_ledger subscription list

# 用量
python3 -m billing_ledger usage record --customer-id c1 --plan pro --usage-date 2026-02-01 --quantity 4
python3 -m billing_ledger usage list --customer-id c1 --plan pro
python3 -m billing_ledger usage summary --customer-id c1 --plan pro [--start-date 2026-02-01] [--end-date 2026-02-29]
```

用量记录与订阅同库存储（默认 `ledger.db`，可用环境变量 `BILLING_LEDGER_DB` 覆盖），记录号全局唯一、单调递增。同一订阅同一日期可重复录入，汇总时求和。退出码：0 成功；2 参数或日期非法（含用量日期早于订阅 start_date）；3 同一次提交内出现同订阅同日期的重复记录（全部拒绝）；4 订阅不存在。
