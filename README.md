# Hermes Token Dashboard

仿 [opencode-token-dashboard](https://github.com/heimoshuiyu/opencode-token-dashboard) 风格的本地 Token 用量看板，为 Hermes Agent 设计。

**数据源:** `~/.hermes/state.db` → `sessions` 表  
**技术栈:** Python 3 标准库（`http.server` + `sqlite3`）+ Chart.js CDN  
**零外部依赖** — 不需要 pip install、Node.js、Rust。

---

## 快速开始

```bash
python3 hermes-token-dashboard.py
# → http://127.0.0.1:8765
```

环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8765` | 监听端口 |

```bash
HOST=0.0.0.0 PORT=9000 python3 hermes-token-dashboard.py
```

---

## 功能

- **趋势图** — 7 天用小时粒度，30/90/180/365/全部用天粒度。折线图 + 渐变面积填充，双 Y 轴（Token + Messages）
- **汇总卡片** — Today / Total / Daily Avg / Peak / Messages 五卡概览
- **缓存命中率** — input side 口径（`cache_read / (input + cache_read)`）
- **Token 构成** — Input / Output / Cache Read / Cache Write / Reasoning 分解
- **模型排行** — Top 8 表格，第一名金徽章高亮
- **Provider 分布** — 环形图
- **缓存命中率趋势** — 按模型竖柱图
- **实时更新** — SSE 推送（3s 检查 DB 变化）+ HTTP 轮询兜底
- **API 响应缓存** — 基于 `state.db` + `.db-wal` + `.db-shm` 的 mtime+size 签名

---

## 时区

**所有统计统一使用北京时间 UTC+8。**

包括 Today、Daily Avg、Peak Day、趋势图分组。不会跟随系统时区变化。

---

## API

### `GET /api/usage?range={range}`

**参数：**

| range | 说明 | 粒度 |
|-------|------|------|
| `7` | 最近 7 天 | 小时 |
| `30` / `90` / `180` / `365` | 对应天数 | 天 |
| `all` / `null` / 空 / 未传 | 全部 | 天 |

**正常响应：**

```json
{
  "meta": { "database": "state.db", "range": "30", "first_day": "2026-05-02", "last_day": "2026-06-01", "timezone": "UTC+8" },
  "summary": { "total": 1150000000, "today": 600000, "daily_avg": 37000000, "peak": 230000000, "cache_hit_pct": 93.1, ... },
  "trend": [...],
  "hour_trend": [...],
  "model_ranking": [...],
  "provider_distribution": [...],
  "composition": {...}
}
```

**错误响应（数据库不可用等）：**

```json
{
  "error": "database_not_found",
  "message": "Database error: unable to open database file"
}
```

### `GET /health`

```json
{"ok": true}
```

---

## 定价表

内置定价表（USD / 1M tokens）：

| 模型 | Input | Output | Cache Read | Cache Write | Reasoning |
|------|-------|--------|------------|-------------|-----------|
| deepseek-v4-pro | $12 | $24 | $1 | $12 | $12 |
| deepseek-v4-flash | $1 | $2 | $0.20 | $1 | $1 |
| gpt-5.5 | $2.50 | $10 | $1.25 | $2.50 | $15 |
| gpt-5.4 | $1.25 | $5 | $0.625 | $1.25 | $7.50 |
| gemini-3.1-flash-lite | $0.075 | $0.30 | $0.01875 | $0.075 | $0.075 |
| 默认 (fallback) | $1 | $3 | $0.25 | $1 | $1 |

免费模型（`:free`、`gemma`、`nemotron`、`gpt-oss`、`hy3-preview` 等）成本为 $0。

---

## 缓存命中率公式

**v2.1 起使用 input side 口径：**

```
cache_hit_pct = cache_read / (input + cache_read) × 100%
```

分母为 0 时命中率为 0%。

---

## 模型名去重

后端自动归一化模型名：
- 去掉 `@provider:` 前缀（如 `@opencode-go:deepseek-v4-flash` → `deepseek-v4-flash`）
- 去掉 `org/` 前缀（如 `deepseek/deepseek-v4-flash` → `deepseek-v4-flash`）

同一模型的不同路由渠道合并统计。

---

## 刷新机制

看板通过文件 `mtime+size` 签名判断数据是否变化。**同时检查三个文件：**

- `~/.hermes/state.db`
- `~/.hermes/state.db-wal`
- `~/.hermes/state.db-shm`

任一文件变化即触发重新聚合 + SSE 推送。兼容 SQLite WAL 模式。

---

## 文件结构

```
hermes-token-dashboard/
├── hermes-token-dashboard.py  # 主程序（单文件，零依赖）
├── README.md
└── tests/
    └── test_dashboard.py      # 单元测试
```

---

## 测试

```bash
python3 -m py_compile hermes-token-dashboard.py
python3 -m unittest discover -s tests
```

---

## License

MIT

---

## v2.2.3 修复

- **Summary cards 按当前 metric 动态计算** — Today/Total/Daily Avg/Peak 全部从 `days` 精确计算
- **7 天小时粒度下 Daily Avg 按自然日计算** — 先按日合并再平均
- **Cache Hit Rate 指标正确处理** — 按 `cache_read/(input+cache_read)` 公式重算
- **Cache Hit Trend legend 支持 solo 模式** — 点击单条线只显示该线，再点恢复全部
- **Model/Provider 排序跟随当前 metric** — 切换指标后排行分布真实反映
- **Cache Hit Trend tooltip 增强** — 显示 cache hit % / input / cache_read / cache_write
