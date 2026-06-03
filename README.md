# Hermes Token Dashboard

仿 [opencode-token-dashboard](https://github.com/heimoshuiyu/opencode-token-dashboard) 风格的本地 Token 用量看板，为 Hermes Agent 设计。

**数据源:** `~/.hermes/state.db` → `sessions` 表  
**技术栈:** Python 3 标准库（`http.server` + `sqlite3`）+ Chart.js CDN  
**零外部依赖**

## 快速开始

```bash
python3 hermes-token-dashboard.py
# → http://127.0.0.1:8765
```

## 功能

- Hero 面板 + 6 档时间范围（7天小时/30/90/180/365/All）
- 11 项指标选择器，所有图表联动
- 明暗主题 + 中英文切换（localStorage 记忆）
- Summary cards 按 metric 从 `days` 精确计算
- 趋势图 + 双指标 Breakdown + 横向模型排行 + Provider 环形图
- 多线缓存命中率趋势图（solo 模式 legend + 增强 tooltip）
- SSE 实时推送 + WAL 感知签名
- UTC+8 固定时区
- free 模型成本为 0

## API

`GET /api/usage?range={7|30|90|180|365|all|null}`  
`GET /health`

## 测试

```bash
python3 -m py_compile hermes-token-dashboard.py
python3 -m unittest discover -s tests   # 44 个单元测试
```

## v2.6.1

- 修复 7 天小时粒度下 `days[]` 成本字段不完整的问题
- `estimated_no_cache_cost` 和 `estimated_cache_savings` 现在在小时 bucket 中也准确返回

## v2.6

新增成本洞察：
- 当前范围估算成本
- 无缓存成本估算
- 缓存节省金额
- 30 天成本预测
- 模型成本效率排行（每百万活跃 Token 成本）
- 缓存节省排行
- 使用时段统计（24 小时柱状图）

说明：
- 成本为估算值
- 缓存节省为基于价格表推算，不代表账单真实抵扣

## v2.3

- 代码可读性整理：Python 配置/函数体正常换行缩进，HTML/CSS/JS 按模块分段
- 保留兼容别名 `_db_signature()` / `_file_mtime_size()`
- 测试数量确认：27 个

## License

MIT
