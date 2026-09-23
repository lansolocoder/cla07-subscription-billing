# 订阅计费与账单对账

用于本地订阅用量计费与账单对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m billing_ledger --help
python3 -m billing_ledger --version
python3 -m unittest discover -s tests -v
```

当前仅提供帮助与版本查询入口；无参数显示帮助，未知参数以非零状态退出。尚未实现订阅登记、用量汇总、账单生成以及收款核对与差异归集，不会创建业务数据文件。
