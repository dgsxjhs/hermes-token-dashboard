#!/usr/bin/env python3
"""
Hermes Token Dashboard v2 — shadcn/ui 风格 · Recharts 级折线图 · 缓存命中率仪表盘
数据源: ~/.hermes/state.db → sessions 表
启动: python3 hermes-token-dashboard.py → http://localhost:8765
"""

import sqlite3
import json
import time
import os
import hashlib
import threading
import http.server
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════
DB_PATH = os.path.expanduser("~/.hermes/state.db")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
POLL_INTERVAL = 3
TZ = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════
# Pricing table (USD / 1M tokens)
# ═══════════════════════════════════════════════════════════════
MODEL_PRICING = {
    "deepseek-v4-pro":      {"input": 12, "output": 24, "cache_read": 1,   "cache_write": 12, "reasoning": 12},
    "deepseek-v4-flash":    {"input": 1,  "output": 2,  "cache_read": 0.2, "cache_write": 1,  "reasoning": 1},
    "deepseek-v4":          {"input": 1,  "output": 2,  "cache_read": 0.2, "cache_write": 1,  "reasoning": 1},
    "gpt-5.5":              {"input": 2.5, "output": 10,   "cache_read": 1.25, "cache_write": 2.5,  "reasoning": 15},
    "gpt-5.4":              {"input": 1.25, "output": 5,   "cache_read": 0.625, "cache_write": 1.25, "reasoning": 7.5},
    "gpt-5":                {"input": 1.25, "output": 5,   "cache_read": 0.625, "cache_write": 1.25, "reasoning": 7.5},
    "gemini-3.1-flash-lite": {"input": 0.075, "output": 0.30, "cache_read": 0.01875, "cache_write": 0.075, "reasoning": 0.075},
    "gemini-3.0-flash":      {"input": 0.15,  "output": 0.60, "cache_read": 0.0375,  "cache_write": 0.15,  "reasoning": 0.15},
    "gemini-3":              {"input": 1.25,  "output": 5,    "cache_read": 0.3125,  "cache_write": 1.25,  "reasoning": 5},
    "minimax-m2.7": {"input": 0.3, "output": 1.2, "cache_read": 0.075, "cache_write": 0.3, "reasoning": 0.3},
    "minimax-m2.5": {"input": 0.3, "output": 1.2, "cache_read": 0.075, "cache_write": 0.3, "reasoning": 0.3},
    "mimo-v2.5-pro":  {"input": 1.5, "output": 6, "cache_read": 0.375, "cache_write": 1.5, "reasoning": 1.5},
    "mimo-v2.5":      {"input": 1.5, "output": 6, "cache_read": 0.375, "cache_write": 1.5, "reasoning": 1.5},
    "mimo-v2.5-free": {"input": 0,   "output": 0, "cache_read": 0,     "cache_write": 0,   "reasoning": 0},
    "gemma-4-31b-it": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0},
    "default": {"input": 1, "output": 3, "cache_read": 0.25, "cache_write": 1, "reasoning": 1},
}

FREE_KEYWORDS = [":free", "mimo-free", "mimo-v2.5-free", "gemma", "nemotron", "gpt-oss", "hy3-preview", "local"]


def get_pricing(model_name: str | None) -> tuple[dict, str]:
    if not model_name:
        return MODEL_PRICING["default"], "default"
    original = model_name
    if original.lower() in MODEL_PRICING:
        return MODEL_PRICING[original.lower()], original.lower()
    if "/" in original:
        model_name = original.split("/")[-1]
    sorted_keys = sorted([k for k in MODEL_PRICING if k != "default"], key=len, reverse=True)
    lower = model_name.lower()
    for key in sorted_keys:
        if lower.startswith(key):
            return MODEL_PRICING[key], key
    for kw in FREE_KEYWORDS:
        if kw in lower:
            free = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
            return free, "free"
    return MODEL_PRICING["default"], "default"


# ═══════════════════════════════════════════════════════════════
# Data Engine with mtime+size cache
# ═══════════════════════════════════════════════════════════════
_cache: dict | None = None
_cache_signature: str = ""


def _db_signature() -> str:
    try:
        stat = os.stat(DB_PATH)
        return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        return ""


def _read_db() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, model, billing_provider, started_at, ended_at, source,
               input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
               reasoning_tokens, message_count, tool_call_count, api_call_count
        FROM sessions
        ORDER BY started_at DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_data(force=False) -> list[dict]:
    global _cache, _cache_signature
    sig = _db_signature()
    if not force and _cache is not None and sig == _cache_signature:
        return _cache
    _cache = _read_db()
    _cache_signature = sig
    return _cache


def aggregate_stats(sessions: list[dict], range_days: int | None = None) -> dict:
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if range_days:
        cutoff = now - timedelta(days=range_days)
        sessions = [s for s in sessions if s["started_at"] and
                    datetime.fromtimestamp(s["started_at"], TZ) >= cutoff]

    agg = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0,
           "api_calls": 0, "messages": 0, "cost": 0.0}
    today_tokens = 0
    by_model: dict[str, dict] = {}
    by_provider: dict[str, int] = {}
    by_day: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    intervals = []

    for s in sessions:
        inp = s["input_tokens"] or 0
        out = s["output_tokens"] or 0
        cr = s["cache_read_tokens"] or 0
        cw = s["cache_write_tokens"] or 0
        rt = s["reasoning_tokens"] or 0
        api = s["api_call_count"] or 0
        msg = s["message_count"] or 0
        model = s["model"] or "unknown"
        provider = s["billing_provider"] or "unknown"

        # Normalize model name
        def normalize_model(m):
            if not m: return "unknown"
            if m.startswith("@"):
                m = m.split(":", 1)[-1] if ":" in m else m
            if "/" in m:
                m = m.split("/")[-1]
            return m

        model_key = normalize_model(model)

        agg["input"] += inp
        agg["output"] += out
        agg["cache_read"] += cr
        agg["cache_write"] += cw
        agg["reasoning"] += rt
        agg["api_calls"] += api
        agg["messages"] += msg

        pricing, _ = get_pricing(model)
        cost = (inp * pricing["input"] + out * pricing["output"] +
                cr * pricing["cache_read"] + cw * pricing["cache_write"] +
                rt * pricing["reasoning"]) / 1_000_000
        agg["cost"] += cost

        if s["started_at"]:
            st = datetime.fromtimestamp(s["started_at"], TZ)
            if st >= today_start:
                today_tokens += inp + out + cr + cw + rt

        if model_key not in by_model:
            by_model[model_key] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                               "reasoning": 0, "sessions": 0, "cost": 0, "api_calls": 0}
        bm = by_model[model_key]
        bm["input"] += inp
        bm["output"] += out
        bm["cache_read"] += cr
        bm["cache_write"] += cw
        bm["reasoning"] += rt
        bm["sessions"] += 1
        bm["cost"] += cost
        bm["api_calls"] += api

        by_provider[provider] = by_provider.get(provider, 0) + inp + out + cr + cw + rt

        if s["started_at"]:
            day_key = st.strftime("%Y-%m-%d")
            if day_key not in by_day:
                by_day[day_key] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                   "reasoning": 0, "messages": 0, "sessions": 0}
            bd = by_day[day_key]
            bd["input"] += inp
            bd["output"] += out
            bd["cache_read"] += cr
            bd["cache_write"] += cw
            bd["reasoning"] += rt
            bd["messages"] += msg
            bd["sessions"] += 1

            hour_key = st.strftime("%Y-%m-%dT%H:00")
            if hour_key not in by_hour:
                by_hour[hour_key] = {"input": 0, "output": 0, "total": 0}
            by_hour[hour_key]["input"] += inp
            by_hour[hour_key]["output"] += out
            by_hour[hour_key]["total"] += inp + out + cr + cw + rt

        if s["started_at"] and s["ended_at"]:
            intervals.append((s["started_at"], s["ended_at"]))

    peak_day = max(by_day.items(), key=lambda x: (x[1]["input"] + x[1]["output"] + x[1]["cache_read"] + x[1]["cache_write"] + x[1]["reasoning"]), default=("—", {}))
    peak_val = (peak_day[1].get("input",0) + peak_day[1].get("output",0) + peak_day[1].get("cache_read",0) + peak_day[1].get("cache_write",0) + peak_day[1].get("reasoning",0))

    runtime_dedup = 0
    if intervals:
        intervals.sort()
        merged = [intervals[0]]
        for s, e in intervals[1:]:
            last_s, last_e = merged[-1]
            if s <= last_e:
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))
        runtime_dedup = sum(e - s for s, e in merged)

    all_total = agg["input"] + agg["output"] + agg["cache_read"] + agg["cache_write"] + agg["reasoning"]
    cache_hit = agg["cache_read"] / all_total * 100 if all_total > 0 else 0.0

    model_cache_rates = []
    for m, d in by_model.items():
        mt = d["input"] + d["output"] + d["cache_read"] + d["cache_write"] + d["reasoning"]
        if mt > 0:
            model_cache_rates.append({
                "model": m,
                "total": mt,
                "cache_hit_pct": round(d["cache_read"] / mt * 100, 2)
            })

    model_ranking = sorted(
        [{"model": m, "total": d["input"] + d["output"] + d["cache_read"] + d["cache_write"] + d["reasoning"],
          "cost": d["cost"], "sessions": d["sessions"], "api_calls": d["api_calls"]}
         for m, d in by_model.items()],
        key=lambda x: x["total"], reverse=True
    )[:8]

    provider_list = sorted(
        [{"provider": p, "total": t} for p, t in by_provider.items()],
        key=lambda x: x["total"], reverse=True
    )

    day_trend = sorted(by_day.items())
    hour_trend = sorted(by_hour.items())
    dates_in_range = [k for k, v in day_trend]
    first_day = dates_in_range[0] if dates_in_range else "—"
    last_day = dates_in_range[-1] if dates_in_range else "—"
    days_count = len(day_trend) or 1
    daily_avg = all_total / days_count
    active_cutoff = (now - timedelta(minutes=30)).timestamp()
    active_sessions = len([s for s in sessions if s["started_at"] and s["started_at"] >= active_cutoff])

    return {
        "meta": {
            "database": "state.db",
            "range": str(range_days) if range_days else "all",
            "first_day": first_day,
            "last_day": last_day,
        },
        "summary": {
            "total": all_total,
            "today": today_tokens,
            "daily_avg": round(daily_avg),
            "peak": peak_val,
            "peak_day": peak_day[0],
            "input": agg["input"],
            "output": agg["output"],
            "cache_read": agg["cache_read"],
            "cache_write": agg["cache_write"],
            "reasoning": agg["reasoning"],
            "sessions": len(sessions),
            "active_sessions": active_sessions,
            "api_calls": agg["api_calls"],
            "user_messages": agg["messages"],
            "messages_per_day": round(agg["messages"] / days_count) if days_count else 0,
            "estimated_cost": agg["cost"],
            "unique_models": len(by_model),
            "cache_hit_pct": round(cache_hit, 1),
            "runtime_dedup": runtime_dedup,
        },
        "trend": day_trend,
        "hour_trend": hour_trend,
        "model_ranking": model_ranking,
        "model_cache_rates": sorted(model_cache_rates, key=lambda x: x["total"], reverse=True)[:8],
        "provider_distribution": provider_list,
        "composition": {
            "input": agg["input"],
            "output": agg["output"],
            "cache_read": agg["cache_read"],
            "cache_write": agg["cache_write"],
            "reasoning": agg["reasoning"],
        },
    }


# ═══════════════════════════════════════════════════════════════
# API Response Cache
# ═══════════════════════════════════════════════════════════════
_api_cache: dict = {}
_api_cache_lock = threading.Lock()


def cached_stats(range_days: int | None) -> dict:
    key = f"range_{range_days}"
    sig = _db_signature()
    with _api_cache_lock:
        entry = _api_cache.get(key)
        if entry and entry["sig"] == sig:
            return entry["data"]
    sessions = get_data(force=True)
    stats = aggregate_stats(sessions, range_days)
    with _api_cache_lock:
        _api_cache[key] = {"sig": sig, "data": stats}
    return stats


# ═══════════════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════════════
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Token Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #f8fafc;
    --card-bg: #ffffff;
    --border: #e2e8f0;
    --text: #0f172a;
    --text-muted: #64748b;
    --text-dim: #94a3b8;
    --primary: #3b82f6;
    --primary-light: #dbeafe;
    --emerald: #10b981;
    --emerald-light: #d1fae5;
    --amber: #f59e0b;
    --amber-light: #fef3c7;
    --purple: #8b5cf6;
    --purple-light: #ede9fe;
    --rose: #f43f5e;
    --radius: 12px;
    --radius-sm: 8px;
    --shadow: 0 1px 2px 0 rgba(0,0,0,0.03);
    --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  .container { max-width: 1200px; margin: 0 auto; padding: 20px 24px; }
  .header {
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 20px; flex-wrap: wrap; gap: 12px;
  }
  .header-left h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.3px; color: var(--text); }
  .header-left .subtitle { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
  .header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .meta-card {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: var(--radius-sm); padding: 6px 12px; font-size: 12px; color: var(--text-muted);
    display: flex; align-items: center; gap: 6px;
  }
  .meta-card strong { color: var(--text); }
  .live-dot { width: 7px; height: 7px; background: var(--emerald); border-radius: 50%; display: inline-block; }
  .filter-bar {
    display: flex; align-items: center; gap: 6px; margin-bottom: 20px;
    flex-wrap: wrap; padding: 6px; background: var(--card-bg);
    border: 1px solid var(--border); border-radius: var(--radius);
  }
  .range-pill {
    padding: 6px 16px; border: none; background: transparent;
    color: var(--text-muted); border-radius: var(--radius-sm);
    cursor: pointer; font-size: 13px; font-weight: 500; transition: all .15s;
  }
  .range-pill:hover { background: var(--bg); color: var(--text); }
  .range-pill.active { background: var(--primary); color: #fff; }
  .summary-row {
    display: grid; grid-template-columns: repeat(5, 1fr);
    gap: 12px; margin-bottom: 20px;
  }
  .stat-card {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px;
    box-shadow: var(--shadow);
  }
  .stat-card .label {
    font-size: 12px; color: var(--text-muted); font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .stat-card .value {
    font-size: 26px; font-weight: 700; letter-spacing: -0.5px;
    font-variant-numeric: tabular-nums;
  }
  .stat-card .sub { font-size: 12px; color: var(--text-dim); margin-top: 4px; }
  .stat-card .icon { width: 32px; height: 32px; border-radius: var(--radius-sm); display: flex; align-items: center; justify-content: center; font-size: 15px; }
  .icon-blue { background: var(--primary-light); color: var(--primary); }
  .icon-emerald { background: var(--emerald-light); color: var(--emerald); }
  .icon-amber { background: var(--amber-light); color: var(--amber); }
  .icon-purple { background: var(--purple-light); color: var(--purple); }
  .icon-rose { background: #ffe4e6; color: var(--rose); }
  .chart-row {
    display: grid; grid-template-columns: 2fr 1fr;
    gap: 14px; margin-bottom: 14px;
  }
  .chart-card {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; box-shadow: var(--shadow);
    position: relative;
  }
  .chart-card h3 {
    font-size: 14px; font-weight: 600; margin-bottom: 14px;
    color: var(--text); display: flex; align-items: center; gap: 8px;
  }
  .chart-card h3 .badge {
    font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 10px;
    background: var(--primary-light); color: var(--primary);
  }
  .chart-wrap { position: relative; }
  .chart-wrap canvas { width: 100% !important; }
  .gauges-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .gauge-card { text-align: center; padding: 14px 10px; }
  .gauge-card .gauge-label { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
  .gauge-card .gauge-value { font-size: 28px; font-weight: 700; }
  .gauge-value.purple { color: var(--purple); }
  .gauge-value.amber { color: var(--amber); }
  .bottom-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
  .table-card { max-height: 400px; overflow-y: auto; }
  .table-card table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .table-card th {
    position: sticky; top: 0; background: var(--card-bg);
    color: var(--text-dim); font-weight: 500; text-align: left;
    padding: 8px 12px; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.3px; border-bottom: 1px solid var(--border);
  }
  .table-card td { padding: 8px 12px; border-bottom: 1px solid #f1f5f9; }
  .table-card tr:hover td { background: #f8fafc; }
  .model-name {
    max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    display: block; font-weight: 500;
  }
  .rank-1 .model-name { color: var(--amber); }
  .rank-badge {
    display: inline-block; width: 20px; height: 20px; line-height: 20px;
    text-align: center; border-radius: 6px; font-size: 11px; font-weight: 700;
    color: var(--text-dim); background: #f1f5f9;
  }
  .rank-1 .rank-badge { background: var(--amber); color: #fff; }
  .rank-2 .rank-badge { background: #e2e8f0; color: #64748b; }
  .rank-3 .rank-badge { background: #f1f5f9; color: #94a3b8; }
  footer {
    text-align: center; color: var(--text-dim); font-size: 11px;
    padding: 16px 0 24px;
  }
  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  @media (max-width: 900px) {
    .summary-row { grid-template-columns: repeat(2, 1fr); }
    .chart-row, .bottom-row { grid-template-columns: 1fr; }
    .gauges-row { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 500px) {
    .summary-row { grid-template-columns: 1fr; }
    .gauges-row { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-left">
      <h1>Hermes Token Dashboard</h1>
      <div class="subtitle" id="headerSubtitle">Loading...</div>
    </div>
    <div class="header-right">
      <div class="meta-card"><div class="live-dot"></div>Live</div>
      <div class="meta-card">Range: <strong id="metaRange">—</strong></div>
      <div class="meta-card">Messages: <strong id="metaMessages">—</strong></div>
      <div class="meta-card">Tokens: <strong id="metaTokens">—</strong></div>
    </div>
  </div>
  <div class="filter-bar">
    <button class="range-pill" data-range="7">7 天</button>
    <button class="range-pill active" data-range="30">30 天</button>
    <button class="range-pill" data-range="90">90 天</button>
    <button class="range-pill" data-range="180">180 天</button>
    <button class="range-pill" data-range="365">365 天</button>
    <button class="range-pill" data-range="all">全部</button>
  </div>
  <div class="summary-row">
    <div class="stat-card"><div class="label">Today <span class="icon icon-blue">&#x1F4C5;</span></div><div class="value" id="cardToday" style="color:var(--primary)">—</div><div class="sub">Total Tokens</div></div>
    <div class="stat-card"><div class="label">Total <span class="icon icon-emerald">&#x2211;</span></div><div class="value" id="cardTotal">—</div><div class="sub">Period sum</div></div>
    <div class="stat-card"><div class="label">Daily Avg <span class="icon icon-purple">&#x2205;</span></div><div class="value" id="cardDailyAvg">—</div><div class="sub">Per day</div></div>
    <div class="stat-card"><div class="label">Peak <span class="icon icon-amber">&#x25B2;</span></div><div class="value" id="cardPeak">—</div><div class="sub" id="cardPeakDay">—</div></div>
    <div class="stat-card"><div class="label">Messages <span class="icon icon-rose">&#x1F4AC;</span></div><div class="value" id="cardMessages">—</div><div class="sub" id="cardMsgDay">—</div></div>
  </div>
  <div class="chart-row">
    <div class="chart-card"><h3>TREND <span class="badge">每日趋势</span></h3><div class="chart-wrap"><canvas id="trendChart"></canvas></div></div>
    <div class="chart-card">
      <h3>BREAKDOWN <span class="badge">Token 组成</span></h3>
      <div class="gauges-row">
        <div class="gauge-card"><div class="gauge-label">Input Side</div><div class="gauge-value purple" id="gaugeCacheHit">—</div><div class="gauge-label" style="margin-top:2px">Cache Hit Ratio</div></div>
        <div class="gauge-card"><div class="gauge-label">Output Side</div><div class="gauge-value amber" id="gaugeInference">—</div><div class="gauge-label" style="margin-top:2px">Inference Ratio</div></div>
      </div>
      <div style="margin-top:12px"><canvas id="compositionChart" style="max-height:140px;"></canvas></div>
    </div>
  </div>
  <div class="bottom-row">
    <div class="chart-card table-card">
      <h3>LEADERBOARD <span class="badge">模型贡献</span></h3>
      <table><thead><tr><th>#</th><th>Model</th><th>Tokens</th><th>Cost</th><th>Sessions</th></tr></thead><tbody id="leaderboardBody"></tbody></table>
    </div>
    <div class="chart-card">
      <h3>DISTRIBUTION <span class="badge">Provider 分布</span></h3>
      <div class="chart-wrap" style="max-width:280px; margin:0 auto;"><canvas id="providerChart"></canvas></div>
    </div>
  </div>
  <div class="chart-card" style="margin-bottom:14px">
    <h3>PERFORMANCE <span class="badge">缓存命中率 · 按模型</span></h3>
    <div class="chart-wrap" style="height:200px;"><canvas id="cacheHitChart"></canvas></div>
  </div>
  <footer>Hermes Token Dashboard · <strong>state.db</strong> · Auto-refresh every 3s · v2.0</footer>
</div>
<script>
let currentRange = 30;
let trendChart, compositionChart, providerChart, cacheHitChart;
const POLL_MS = 3000;
function fmtNum(n) { if (n == null || isNaN(n)) return '—'; return n.toLocaleString('en-US', { maximumFractionDigits: 0 }); }
function fmtShort(n) { if (n == null || isNaN(n)) return '—'; if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B'; if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'; if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'; return n.toLocaleString(); }
function fmtCost(n) { if (n == null || isNaN(n)) return '—'; return '$' + n.toFixed(2); }
function shortModel(name) {
  if (!name) return 'unknown';
  let s = String(name);
  if (s.includes('/')) s = s.split('/').pop();
  s = s.replace(/-preview|-a3b|-a12b|:free|:beta/g, '');
  if (s.length > 22) s = s.slice(0, 20) + '\u2026';
  return s;
}
function initCharts() {
  const tCtx = document.getElementById('trendChart').getContext('2d');
  const tGrad = tCtx.createLinearGradient(0, 0, 0, 200);
  tGrad.addColorStop(0, 'rgba(59,130,246,0.2)'); tGrad.addColorStop(1, 'rgba(59,130,246,0.0)');
  const gGrad = tCtx.createLinearGradient(0, 0, 0, 200);
  gGrad.addColorStop(0, 'rgba(16,185,129,0.15)'); gGrad.addColorStop(1, 'rgba(16,185,129,0.0)');
  trendChart = new Chart(tCtx, {type:'line',data:{labels:[],datasets:[
    {label:'Total Tokens',data:[],borderColor:'#3b82f6',backgroundColor:tGrad,fill:true,tension:0.3,pointRadius:0,pointHitRadius:8,borderWidth:2,yAxisID:'y'},
    {label:'Messages',data:[],borderColor:'#10b981',backgroundColor:gGrad,fill:true,tension:0.3,pointRadius:0,pointHitRadius:8,borderWidth:1.5,borderDash:[5,3],yAxisID:'y1'}
  ]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{usePointStyle:true,pointStyleWidth:8,padding:20,font:{size:11},color:'#64748b'}},tooltip:{backgroundColor:'#fff',titleColor:'#0f172a',bodyColor:'#64748b',borderColor:'#e2e8f0',borderWidth:1,cornerRadius:8,padding:10,displayColors:true,callbacks:{label:ctx=>ctx.dataset.label+': '+fmtNum(ctx.parsed.y)}}},scales:{x:{ticks:{color:'#94a3b8',maxRotation:45,font:{size:10}},grid:{display:false}},y:{type:'linear',position:'left',title:{display:true,text:'Tokens',color:'#3b82f6',font:{size:11}},ticks:{color:'#94a3b8',callback:v=>fmtShort(v),font:{size:10}},grid:{color:'#f1f5f9'},border:{display:false}},y1:{type:'linear',position:'right',title:{display:true,text:'Messages',color:'#10b981',font:{size:11}},ticks:{color:'#94a3b8',callback:v=>fmtNum(v),font:{size:10}},grid:{display:false},border:{display:false}}},plugins:[{id:'trendH',beforeInit(chart){chart.canvas.parentNode.style.height='220px'}}]});

  const cCtx = document.getElementById('compositionChart').getContext('2d');
  compositionChart = new Chart(cCtx, {type:'bar',data:{labels:[],datasets:[{data:[],backgroundColor:['#3b82f6','#10b981','#8b5cf6','#f59e0b','#f43f5e'],borderWidth:0,borderRadius:4}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:true,plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#94a3b8',callback:v=>fmtShort(v),font:{size:9}},grid:{display:false},border:{display:false}},y:{ticks:{color:'#64748b',font:{size:10}},grid:{display:false},border:{display:false}}}}});

  const pCtx = document.getElementById('providerChart').getContext('2d');
  providerChart = new Chart(pCtx, {type:'doughnut',data:{labels:[],datasets:[{data:[],backgroundColor:['#3b82f6','#10b981','#8b5cf6','#f59e0b','#f43f5e','#06b6d4','#ec4899','#6366f1','#14b8a6','#eab308','#ef4444','#84cc16'],borderWidth:2,borderColor:'#fff'}]},options:{responsive:true,maintainAspectRatio:true,cutout:'60%',plugins:{legend:{position:'bottom',labels:{padding:12,font:{size:10},color:'#64748b',generateLabels:function(chart){return chart.data.labels.map((l,i)=>({text:l+' ('+fmtShort(chart.data.datasets[0].data[i])+')',fillStyle:chart.data.datasets[0].backgroundColor[i],strokeStyle:chart.data.datasets[0].backgroundColor[i],index:i,hidden:false}));}}}}}});

  const hCtx = document.getElementById('cacheHitChart').getContext('2d');
  cacheHitChart = new Chart(hCtx, {type:'bar',data:{labels:[],datasets:[{label:'Cache Hit %',data:[],backgroundColor:'#8b5cf6',borderWidth:0,borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>'Cache Hit: '+ctx.parsed.y.toFixed(1)+'%'}}},scales:{x:{ticks:{color:'#94a3b8',maxRotation:30,font:{size:9}},grid:{display:false}},y:{min:0,max:100,ticks:{color:'#94a3b8',callback:v=>v+'%',font:{size:10}},grid:{color:'#f1f5f9'},border:{display:false}}}}});
}

function updateUI(data) {
  const s = data.summary, m = data.meta;
  document.getElementById('headerSubtitle').textContent = (m.range==='all'?'All time':'Last '+m.range+' days')+' · '+m.first_day+' — '+m.last_day;
  document.getElementById('metaRange').textContent = m.range==='all'?'All':m.range+' days';
  document.getElementById('metaMessages').textContent = fmtNum(s.user_messages);
  document.getElementById('metaTokens').textContent = fmtShort(s.total);
  document.getElementById('cardToday').textContent = fmtNum(s.today);
  document.getElementById('cardTotal').textContent = fmtNum(s.total);
  document.getElementById('cardDailyAvg').textContent = fmtNum(s.daily_avg);
  document.getElementById('cardPeak').textContent = fmtNum(s.peak);
  document.getElementById('cardPeakDay').textContent = 'on '+(s.peak_day||'—');
  document.getElementById('cardMessages').textContent = fmtNum(s.user_messages);
  document.getElementById('cardMsgDay').textContent = fmtNum(s.messages_per_day)+'/day';
  document.getElementById('gaugeCacheHit').textContent = s.cache_hit_pct.toFixed(1)+'%';
  const infDenom = s.total||1;
  document.getElementById('gaugeInference').textContent = ((s.output+s.reasoning)/infDenom*100).toFixed(1)+'%';
  trendChart.data.labels = data.trend.map(t=>t[0].slice(5));
  trendChart.data.datasets[0].data = data.trend.map(t=>t[1].input+t[1].output+t[1].cache_read+t[1].cache_write+t[1].reasoning);
  trendChart.data.datasets[1].data = data.trend.map(t=>t[1].messages||0);
  trendChart.update();
  compositionChart.data.labels = ['Input','Output','Cache Read','Cache Write','Reasoning'];
  compositionChart.data.datasets[0].data = [s.input,s.output,s.cache_read,s.cache_write,s.reasoning];
  compositionChart.update();
  providerChart.data.labels = data.provider_distribution.map(p=>p.provider);
  providerChart.data.datasets[0].data = data.provider_distribution.map(p=>p.total);
  providerChart.update();
  document.getElementById('leaderboardBody').innerHTML = data.model_ranking.map((m,i)=>{
    const cls = i===0?'rank-1':i===1?'rank-2':i===2?'rank-3':'';
    return '<tr class="'+cls+'"><td><span class="rank-badge">'+(i+1)+'</span></td><td><span class="model-name">'+shortModel(m.model)+'</span></td><td>'+fmtShort(m.total)+'</td><td>'+fmtCost(m.cost)+'</td><td>'+m.sessions+'</td></tr>';
  }).join('');
  const crates = data.model_cache_rates||[];
  cacheHitChart.data.labels = crates.map(c=>shortModel(c.model));
  cacheHitChart.data.datasets[0].data = crates.map(c=>c.cache_hit_pct);
  cacheHitChart.update();
}
async function fetchData() {
  try { const r = await fetch('/api/usage?range='+currentRange); if(r.ok) updateUI(await r.json()); } catch(e){}
}
function connectSSE() {
  const es = new EventSource('/api/events');
  es.onmessage = () => fetchData();
  es.onerror = () => es.close();
}
document.querySelectorAll('.range-pill').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.range-pill').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    currentRange = btn.dataset.range==='all'?null:parseInt(btn.dataset.range);
    fetchData();
  });
});
initCharts();
fetchData();
connectSSE();
setInterval(fetchData, POLL_MS+1000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════════════════════

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b":ok\n\n")
        self.wfile.flush()
        try:
            while True:
                time.sleep(POLL_INTERVAL)
                sig = _db_signature()
                current = _api_cache.get("range_sse", {}).get("sig", "")
                if sig != current:
                    with _api_cache_lock:
                        _api_cache["range_sse"] = {"sig": sig, "data": None}
                    self.wfile.write("data: {\"updated\": true}\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/":
            self._send_html()
        elif path == "/health":
            self._send_json({"ok": True})
        elif path == "/api/usage":
            range_val = params.get("range", [None])[0]
            range_days = int(range_val) if range_val and range_val != "all" else None
            stats = cached_stats(range_days)
            self._send_json(stats)
        elif path == "/api/events":
            self._send_sse()
        else:
            self._send_json({"error": "not found"}, 404)


def main():
    print(f"  Hermes Token Dashboard v2")
    print(f"  Data source: {DB_PATH}")
    print(f"  → http://{HOST}:{PORT}")
    print(f"  Press Ctrl+C to stop\n")
    server = http.server.ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
