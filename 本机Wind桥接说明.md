# Investment Halo 本机 Wind 桥接

桥接服务已配置为 macOS 用户级 LaunchAgent，登录后自动启动。它使用已安装的系统 Python 3.15 和 WindPy，仅监听 `127.0.0.1:8765`，不接受下单请求，也不会把 Wind 授权或行情转发到公网。

## 打开网页

在本机浏览器访问：<http://127.0.0.1:8765/>。不要双击 `index.html`；实时数据模式需要通过本机桥接地址打开网页。页面会自动连接桥接并载入 Wind 历史行情与基本面；桥接启动较慢时会自动重试。

桥接会读取 Wind 最近约 3 年的日收盘价（用于风险估计和策略回测），股票另读取 PE_TTM、PB_LF、ROE_TTM 历史字段。每日任务按北京时间工作日 16:05 启动，先检查沪深 300 ETF 是否已有当日收盘，再计算全资产行情、20 日动量信号和风险平价结果并保存快照；如果 Wind 尚未发布当日数据，会每 10 分钟重试至 21:00，周末、节假日或 Wind 未更新时不会伪造结果。网页打开时读取最新快照；保持打开时每分钟检查一次新快照。所有结果仅为模拟，不会下单。

## 检查服务

- 行情连接状态：<http://127.0.0.1:8765/api/health>
- 只读行情 JSON：<http://127.0.0.1:8765/api/quotes>
- 每日自动结果：<http://127.0.0.1:8765/api/daily-results>
- 本地快照：`data/daily/latest.json` 及按日期归档的 JSON 文件
- 运行日志：`~/Library/Logs/InvestmentHalo/bridge.log`
- 错误日志：`~/Library/Logs/InvestmentHalo/bridge-error.log`

## 手动控制

```bash
# 立即启动或重启
launchctl kickstart -k gui/$(id -u)/com.investmenthalo.wind-bridge

# 停止并取消本次登录会话中的服务
launchctl bootout gui/$(id -u)/com.investmenthalo.wind-bridge

# 再次加载服务配置
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investmenthalo.wind-bridge.plist
```

桥接程序位于 `wind_bridge.py`。网页默认模拟本金为人民币 100 万，支持调整本金、追加或提取资金；本金和手工录入的模拟持仓保存在当前浏览器。风险平价标的权重会换算为目标金额与理论数量，并与模拟已持仓分列显示。这不是券商账户持仓同步；估算现金按本金减当前持仓成本计算。网页可以用共同有效的 Wind 日收益样本计算等风险贡献权重，并展示样本区间、年化波动和风险贡献；风险平价计算采用 10% 对角收缩以改善数值稳定，并将标的权重汇总到权益与债券大类。趋势动量和低波动策略支持调参和历史模拟回测。Wind 桥接还读取股票 PE_TTM、PB_LF、ROE_TTM 三年序列，价值与质量策略仅适用于有基本面数据的股票；ETF和债券不适用。回测是研究用途的历史模拟，不会下单。
