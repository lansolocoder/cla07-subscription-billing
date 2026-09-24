# 订阅计费与账单对账

用于本地订阅用量计费与账单对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m billing_ledger --help
python3 -m billing_ledger --version
python3 -m billing_ledger subscription create \
    --customer-id cust-1 --plan pro --price-cents 1999 \
    --start-date 2026-01-15 --trial-days 14
python3 -m billing_ledger subscription list
python3 -m unittest discover -s tests -v
```

已支持订阅登记与查询：`subscription create` 登记一条订阅（成功输出单行 JSON，退出码 0；同一 customer_id 与 plan 重复登记退出码 3；无效输入或 start_date 晚于当天 UTC 退出码 2），`subscription list` 按 start_date 升序、同日期按 id 降序输出 JSON 数组。数据持久化在仓库根目录的 `ledger.db`（SQLite，首次写入自动创建）。无参数显示帮助，未知参数以非零状态退出。尚未实现用量汇总、账单生成以及收款核对与差异归集。
