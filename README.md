# 清算磁区反弹监控（Binance USDC永续 + Bark推送）

监控 ETH / BTC 的 Binance USDⓈ-M 永续合约（USDC本位：ETHUSDC / BTCUSDC），
自建清算价格聚类估算，双向监控（上方/下方磁区），价格距离磁区 ≤1% 时通过 Bark 推送提醒，
提醒内容包含基于ATR动态计算的建议挂单价格。触发后需价格拉开磁区 ≥2% 才会对同一磁区再次提醒。

**重要**：磁区是基于公开K线数据（成交量+资金费率）估算出来的清算价格聚类近似值，**不是**交易所真实持仓的清算数据，仅供参考，不构成任何投资建议。

## 部署步骤

### 1. 建仓库
把这个文件夹整个推到你自己的 GitHub 仓库（public 或 private 都可以，private仓库Actions有免费额度限制，注意查看）。

```bash
cd liquidation-magnet-monitor
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

### 2. 配置 Bark Key
1. iPhone 上安装 [Bark](https://apps.apple.com/app/bark-customed-notifications/id1403753865) app，打开后会看到你的专属推送地址，形如 `https://api.day.app/xxxxxxxxxxxxxxxxxxxx/`，中间那段就是你的 key。
2. 去 GitHub 仓库 Settings → Secrets and variables → Actions → New repository secret
   - Name: `BARK_KEY`
   - Value: 你的 Bark key（只填key本身，不要带URL）
3. 如果你是自建的 Bark 服务器（而不是官方 api.day.app），再加一个 secret：
   - Name: `BARK_SERVER`
   - Value: 你的服务器地址，比如 `https://bark.yourdomain.com`
   （不加的话默认用官方 api.day.app）

### 3. 启用 Actions
如果是新建仓库，Actions 通常默认开启。去仓库的 Actions 标签页确认 workflow 已经出现（Liquidation Magnet Monitor）。

### 4. 手动测试一次
Actions 页面 → 选择 Liquidation Magnet Monitor → Run workflow，手动跑一次，看日志确认能正常拉取 Binance 数据、计算、（如果触发条件满足）成功推送。

### 5. 定时运行
默认每10分钟跑一次（`.github/workflows/monitor.yml` 里的 cron 表达式），可以自行调整频率。

## 已知限制

- **GitHub Actions 定时任务的两个坑**：
  1. 官方不保证准点触发，高峰期可能延迟几分钟。
  2. **如果仓库连续60天没有任何提交，GitHub 会自动暂停 scheduled workflow**，需要手动去 Actions 页面重新启用（或者定期有 commit 就不会触发，比如 state.json 只要有变化被提交，理论上能避免，但不确定 GitHub 是否认可自动化 commit 算作"活跃"，建议每隔一段时间自己看一眼确保没被暂停）。
- state.json 存储冷却状态，由 workflow 自动 commit 回仓库，不需要你手动维护。
- Binance USDC永续（ETHUSDC / BTCUSDC）的成交量/深度通常比USDT永续小，作为"开仓价格代理"的样本会更稀疏，磁区估算的噪声可能比USDT合约版本更大，如果发现磁区不太稳定，可以尝试把 `monitor.py` 里的 `CANDLE_LIMIT`（回溯K线数量）调大，或者把 `STRENGTH_STD_MULT`（磁区强度阈值）调低。

## 主要可调参数（monitor.py 顶部配置区）

| 参数 | 说明 | 默认值 |
|---|---|---|
| `CANDLE_INTERVAL` / `CANDLE_LIMIT` | K线周期与回溯根数 | 1h / 200根 |
| `LEVERAGE_WEIGHTS` | 假设的杠杆分布权重 | 5x~100x |
| `MAINTENANCE_MARGIN_RATE` | 简化维持保证金率 | 0.5% |
| `TRIGGER_THRESHOLD` | 触发距离阈值 | 1% |
| `RELEASE_THRESHOLD` | 冷却解除阈值 | 2% |
| `ATR_K` | ATR buffer 系数 | 0.5 |
| `STRENGTH_STD_MULT` | 磁区强度阈值（均值+N倍标准差）| 1.5 |
