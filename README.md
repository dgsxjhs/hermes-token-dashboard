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
python3 -m unittest discover -s tests   # 27 个单元测试
```

## v2.3

- 代码可读性整理：Python 配置/函数体正常换行缩进，HTML/CSS/JS 按模块分段
- 保留兼容别名 `_db_signature()` / `_file_mtime_size()`
- 测试数量确认：27 个

## License

MIT
