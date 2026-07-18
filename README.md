# 网格策略分钟级回测

这是从投资组合系统拆出的独立项目。程序只依赖 Python 标准库，不使用 SQLite、npm 或任何第三方包；策略配置、行情缓存和回测报告全部保存为 JSON。

## 启动

```text
python start.py
```

浏览器打开 `http://127.0.0.1:8765`，默认方案已经设置为：

- 代码：588000
- 监控区间：1.45～2.05
- 基准价：1.74
- 上涨触发：2.5%
- 回落卖出：20%
- 下跌触发：2.5%
- 反弹买入：30%
- 单次金额：3000 元
- 买入委托：卖一 + 0.001
- 卖出委托：买一 - 0.001
- 最低保留仓位：20%（按初始持仓股数计算，设置为 0% 时允许完全空仓）
- 有效期：30 个自然日

为使网格与直接持有可以公平比较，默认使用 30000 元初始资产和 50% 初始底仓。最低保留仓位用于保留长期核心仓位，不能高于初始底仓；这些值、佣金和全部策略参数都可以在页面修改。

每次回测完成后，桌面网页会展示：

- 分钟 K 线，可切换最近 1 日、5 日或全部行情；
- 网格买入和卖出成交点，分别使用不同形状及“买/卖”文字标记；
- 网格交易累计收益实线与直接持有累计收益虚线；
- 鼠标悬停或键盘左右键对应的分钟开高低收、网格收益和持有收益；
- 最终收益、超额收益、最大回撤、佣金、最终仓位、平均仓位和逐笔成交明细；正收益但低仓位时会给出提示。

## JSON 文件

- `data/config.json`：最近一次使用的策略方案。
- `data/market/{代码}_1m_{天数}d.json`：最近取得的分钟行情缓存。
- `data/reports/{报告编号}.json`：每次完整回测结果。

## Docker

项目提供 Docker 镜像和 Compose 配置。直接从 GHCR 启动最新镜像：

```text
docker compose pull
docker compose up -d --no-build
```

首次本地构建或修改源码后，可以使用：

```text
docker compose up -d --build
```

浏览器访问 `http://服务器IP:8765`。Compose 使用 `grid-backtest-data` 卷保存配置、
行情缓存、回测报告和优化历史；GitHub Actions 会在 `main`、`master` 推送或 `v*` 标签
推送时自动构建并发布 `ghcr.io/xsxiaosa/grid-strategy-backtest`。

Compose 同时启动 Watchtower，每 5 分钟检查一次 GHCR。镜像更新后会自动拉取新镜像、
重建 `grid-backtest` 容器并保留数据卷。服务器首次部署私有 GHCR 镜像前先执行：

```text
docker login ghcr.io
docker compose pull
docker compose up -d --no-build
```

默认从 `/root/.docker/config.json` 读取 GHCR 登录信息。如果 Docker 使用其他用户登录，
可以设置 `DOCKER_CONFIG_FILE` 指向对应的 `config.json`。

如果 GHCR 镜像属于其他仓库所有者，可以在 PowerShell 中覆盖镜像地址：

```text
$env:GRID_BACKTEST_IMAGE = "ghcr.io/你的用户名/grid-strategy-backtest:latest"
docker compose up -d --no-build
```

行情请求失败时，如果存在相同证券和周期的缓存，程序会使用缓存并在报告中给出警告。分钟行情来自 Yahoo Finance Chart API，属于非交易所官方数据，只适合策略研究。

## 测试

```text
python -m unittest discover -s tests -v
```

## 参数组合优化

浏览器打开 `http://127.0.0.1:8765/optimizer.html` 可以使用与普通回测完全相同的成交口径执行四参数分阶段优化。单次金额及其他账户条件在一次任务中固定，只搜索上涨触发、回落卖出、下跌触发和反弹买入。

网页优化分为全范围等比粗搜索、两端 5% 邻域搜索和两端 1% 精细搜索。粗搜索每维默认使用 15 个采样值，页面可在 2 至 25 之间调整；后两轮会选择收益靠前且参数位置分散的候选，避免同一收益平台占满细化名额。固定条件相同的后续任务还会复用最近历史结果，因此扩大参数范围不会丢失已经找到且仍位于新范围内的优质参数。结果会同时显示最终仓位和平均仓位，正收益但低仓位的候选会排在正常仓位候选之后并标记提示。页面持续显示后台进度，完成后列出最终收益率最好和最差的各十组，并将摘要保存到 `data/optimizations/{任务编号}.json`。这是速度可控的启发式迭代搜索，不保证数学上的全局最优。
