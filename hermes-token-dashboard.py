#!/usr/bin/env python3
"""
Hermes Token Dashboard v2.2 — 对标 opencode-token-dashboard 页面结构
数据源: ~/.hermes/state.db → sessions 表
启动: python3 hermes-token-dashboard.py → http://localhost:8765

v2.2:
- Hero 面板 + 控制栏（指标选择器/刷新/主题/语言）
- API 增强：days / models / providers / provider_model_trends
- 趋势图跟随指标、双半圆组成图、横柱模型排行、多线缓存命中率
- 明暗主题、中英文切换
"""

import sqlite3
import json
import time
import os
import threading
import http.server
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════
DB_PATH = os.path.expanduser("~/.hermes/state.db")
DB_WAL_PATH = DB_PATH + "-wal"
DB_SHM_PATH = DB_PATH + "-shm"
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


def get_pricing(model_name):
    if not model_name:
        return MODEL_PRICING["default"], "default"
    lower_full = model_name.lower()
    if lower_full in MODEL_PRICING:
        return MODEL_PRICING[lower_full], lower_full
    for kw in FREE_KEYWORDS:
        if kw in lower_full:
            free = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
            return free, "free"
    stripped = model_name
    if "/" in stripped:
        stripped = stripped.split("/")[-1]
    sorted_keys = sorted([k for k in MODEL_PRICING if k != "default"], key=len, reverse=True)
    lower = stripped.lower()
    for key in sorted_keys:
        if lower.startswith(key):
            return MODEL_PRICING[key], key
    return MODEL_PRICING["default"], "default"


# ═══════════════════════════════════════════════════════════════
# Data Engine (WAL-aware)
# ═══════════════════════════════════════════════════════════════
_cache = None
_cache_signature = ""


def _file_mtime_size(path):
    try:
        stat = os.stat(path)
        return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        return ""


def _db_signature():
    parts = [_file_mtime_size(DB_PATH), _file_mtime_size(DB_WAL_PATH), _file_mtime_size(DB_SHM_PATH)]
    return "|".join(parts)


class DatabaseError(Exception):
    pass


def _read_db():
    try:
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
    except sqlite3.Error as e:
        raise DatabaseError(str(e))


def get_data(force=False):
    global _cache, _cache_signature
    sig = _db_signature()
    if not force and _cache is not None and sig == _cache_signature:
        return _cache
    _cache = _read_db()
    _cache_signature = sig
    return _cache


# ═══════════════════════════════════════════════════════════════
# Aggregation (v2.2 enhanced)
# ═══════════════════════════════════════════════════════════════

def normalize_model(m):
    if not m:
        return "unknown"
    if m.startswith("@"):
        m = m.split(":", 1)[-1] if ":" in m else m
    if "/" in m:
        m = m.split("/")[-1]
    return m


def _safe_get(d, key, default=0):
    return d.get(key) or default


def _calc_cost(model, inp, out, cr, cw, rt):
    pricing, _ = get_pricing(model)
    return (inp * pricing["input"] + out * pricing["output"] +
            cr * pricing["cache_read"] + cw * pricing["cache_write"] +
            rt * pricing["reasoning"]) / 1_000_000


def _build_model_entry(model_key, d):
    mt = d["input"] + d["output"] + d["cache_read"] + d["cache_write"] + d["reasoning"]
    active = d["input"] + d["output"] + d["reasoning"]
    ch_denom = d["input"] + d["cache_read"]
    return {
        "name": model_key,
        "total": mt,
        "active": active,
        "input": d["input"],
        "output": d["output"],
        "reasoning": d["reasoning"],
        "cache_read": d["cache_read"],
        "cache_write": d["cache_write"],
        "cache_hit_rate": round(d["cache_read"] / ch_denom * 100, 2) if ch_denom > 0 else 0.0,
        "runtime_dedup": 0,
        "user_message_count": 0,
        "estimated_cost": d["cost"],
        "sessions": d["sessions"],
        "api_calls": d["api_calls"],
    }


def _build_day_entry(key, d, is_hourly=False):
    mt = d["input"] + d["output"] + d["cache_read"] + d["cache_write"] + d["reasoning"]
    active = d["input"] + d["output"] + d["reasoning"]
    ch_denom = d["input"] + d["cache_read"]
    return {
        "date": key,
        "total": mt,
        "active": active,
        "input": d["input"],
        "output": d["output"],
        "reasoning": d["reasoning"],
        "cache_read": d["cache_read"],
        "cache_write": d["cache_write"],
        "cache_hit_rate": round(d["cache_read"] / ch_denom * 100, 2) if ch_denom > 0 else 0.0,
        "runtime_dedup": 0,
        "user_message_count": d.get("messages", 0),
        "estimated_cost": 0,
    }


def aggregate_stats(sessions, range_days=None):
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if range_days:
        cutoff = now - timedelta(days=range_days)
        sessions = [s for s in sessions if s.get("started_at") and
                    datetime.fromtimestamp(s["started_at"], TZ) >= cutoff]

    agg = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0,
           "api_calls": 0, "messages": 0, "cost": 0.0}
    today_tokens = 0
    by_model = {}
    by_provider_raw = {}
    by_day = {}
    by_hour = {}
    intervals = []
    # provider_model_trends: {(provider, model): {day_key: {input, cache_read, cache_write}}}
    pm_trends_raw = {}

    for s in sessions:
        inp = _safe_get(s, "input_tokens")
        out = _safe_get(s, "output_tokens")
        cr = _safe_get(s, "cache_read_tokens")
        cw = _safe_get(s, "cache_write_tokens")
        rt = _safe_get(s, "reasoning_tokens")
        api = _safe_get(s, "api_call_count")
        msg = _safe_get(s, "message_count")
        model = s.get("model") or "unknown"
        provider = s.get("billing_provider") or "unknown"
        model_key = normalize_model(model)

        agg["input"] += inp
        agg["output"] += out
        agg["cache_read"] += cr
        agg["cache_write"] += cw
        agg["reasoning"] += rt
        agg["api_calls"] += api
        agg["messages"] += msg
        cost = _calc_cost(model, inp, out, cr, cw, rt)
        agg["cost"] += cost

        if s.get("started_at"):
            st = datetime.fromtimestamp(s["started_at"], TZ)
            if st >= today_start:
                today_tokens += inp + out + cr + cw + rt

        if model_key not in by_model:
            by_model[model_key] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                   "reasoning": 0, "sessions": 0, "cost": 0, "api_calls": 0}
        bm = by_model[model_key]
        bm["input"] += inp; bm["output"] += out; bm["cache_read"] += cr
        bm["cache_write"] += cw; bm["reasoning"] += rt
        bm["sessions"] += 1; bm["cost"] += cost; bm["api_calls"] += api

        if provider not in by_provider_raw:
            by_provider_raw[provider] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                         "reasoning": 0, "cost": 0.0}
        bp = by_provider_raw[provider]
        bp["input"] += inp; bp["output"] += out; bp["cache_read"] += cr
        bp["cache_write"] += cw; bp["reasoning"] += rt; bp["cost"] += cost

        if s.get("started_at"):
            day_key = st.strftime("%Y-%m-%d")
            if day_key not in by_day:
                by_day[day_key] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                   "reasoning": 0, "messages": 0, "sessions": 0}
            bd = by_day[day_key]
            bd["input"] += inp; bd["output"] += out; bd["cache_read"] += cr
            bd["cache_write"] += cw; bd["reasoning"] += rt
            bd["messages"] += msg; bd["sessions"] += 1

            hour_key = st.strftime("%Y-%m-%dT%H:00")
            if hour_key not in by_hour:
                by_hour[hour_key] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                     "reasoning": 0, "messages": 0, "total": 0}
            by_hour[hour_key]["input"] += inp
            by_hour[hour_key]["output"] += out
            by_hour[hour_key]["cache_read"] += cr
            by_hour[hour_key]["cache_write"] += cw
            by_hour[hour_key]["reasoning"] += rt
            by_hour[hour_key]["messages"] += msg
            by_hour[hour_key]["total"] += inp + out + cr + cw + rt

            # provider_model_trends
            pm_key = (provider, model_key)
            if pm_key not in pm_trends_raw:
                pm_trends_raw[pm_key] = {}
            if day_key not in pm_trends_raw[pm_key]:
                pm_trends_raw[pm_key][day_key] = {"input": 0, "cache_read": 0, "cache_write": 0, "total": 0}
            pmd = pm_trends_raw[pm_key][day_key]
            pmd["input"] += inp; pmd["cache_read"] += cr
            pmd["cache_write"] += cw; pmd["total"] += inp + out + cr + cw + rt

        if s.get("started_at") and s.get("ended_at"):
            intervals.append((s["started_at"], s["ended_at"]))

    peak_day = max(by_day.items(),
                   key=lambda x: (x[1]["input"] + x[1]["output"] + x[1]["cache_read"] +
                                  x[1]["cache_write"] + x[1]["reasoning"]),
                   default=("—", {}))
    peak_val = (peak_day[1].get("input", 0) + peak_day[1].get("output", 0) +
                peak_day[1].get("cache_read", 0) + peak_day[1].get("cache_write", 0) +
                peak_day[1].get("reasoning", 0))
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
    active = agg["input"] + agg["output"] + agg["reasoning"]
    ch_denom = agg["input"] + agg["cache_read"]
    cache_hit_rate = round(agg["cache_read"] / ch_denom * 100, 2) if ch_denom > 0 else 0.0

    days_count = len(by_day) or 1
    daily_avg = all_total / days_count
    active_cutoff = (now - timedelta(minutes=30)).timestamp()
    active_sessions = len([s for s in sessions if s.get("started_at") and s["started_at"] >= active_cutoff])

    # v2.2: days 统一数据
    is_7d = (range_days == 7)
    if is_7d:
        days = [_build_day_entry(k, v, True) for k, v in sorted(by_hour.items())]
    else:
        days = [_build_day_entry(k, v, False) for k, v in sorted(by_day.items())]

    # v2.2: models
    models = [_build_model_entry(k, v) for k, v in by_model.items()]
    models.sort(key=lambda x: x["total"], reverse=True)

    # v2.2: providers
    providers = []
    for p, d in by_provider_raw.items():
        mt = d["input"] + d["output"] + d["cache_read"] + d["cache_write"] + d["reasoning"]
        active_p = d["input"] + d["output"] + d["reasoning"]
        ch_denom_p = d["input"] + d["cache_read"]
        providers.append({
            "name": p,
            "total": mt,
            "active": active_p,
            "input": d["input"],
            "output": d["output"],
            "reasoning": d["reasoning"],
            "cache_read": d["cache_read"],
            "cache_write": d["cache_write"],
            "cache_hit_rate": round(d["cache_read"] / ch_denom_p * 100, 2) if ch_denom_p > 0 else 0.0,
            "estimated_cost": d["cost"],
        })
    providers.sort(key=lambda x: x["total"], reverse=True)

    # v2.2: provider_model_trends
    provider_model_trends = []
    for (prov, mod), day_map in pm_trends_raw.items():
        mt = sum(v["total"] for v in day_map.values())
        days_list = []
        for dk, dv in sorted(day_map.items()):
            chd = dv["input"] + dv["cache_read"]
            days_list.append({
                "date": dk,
                "input": dv["input"],
                "cache_read": dv["cache_read"],
                "cache_write": dv["cache_write"],
                "cache_hit_rate": round(dv["cache_read"] / chd * 100, 2) if chd > 0 else 0.0,
                "total": dv["total"],
            })
        provider_model_trends.append({
            "provider": prov,
            "model": mod,
            "days": days_list,
            "_total": mt,
        })
    provider_model_trends.sort(key=lambda x: x["_total"], reverse=True)
    provider_model_trends = provider_model_trends[:12]
    for pmt in provider_model_trends:
        del pmt["_total"]

    dates_in_range = sorted(by_day.keys())
    first_day = dates_in_range[0] if dates_in_range else "—"
    last_day = dates_in_range[-1] if dates_in_range else "—"

    return {
        "meta": {
            "database": "state.db",
            "range": str(range_days) if range_days else "all",
            "first_day": first_day,
            "last_day": last_day,
            "timezone": "UTC+8",
            "generated_at": datetime.now(TZ).isoformat(),
            "database_path": DB_PATH,
            "scanned_rows": len(sessions),
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
            "active": active,
            "cache_hit_rate": cache_hit_rate,
            "runtime": 0,
            "runtime_dedup": runtime_dedup,
            "user_message_count": agg["messages"],
            "messages_per_day": round(agg["messages"] / days_count) if days_count else 0,
            "estimated_cost": agg["cost"],
            "sessions": len(sessions),
            "active_sessions": active_sessions,
            "api_calls": agg["api_calls"],
            "unique_models": len(by_model),
        },
        "trend": sorted(by_day.items()),
        "hour_trend": sorted(by_hour.items()),
        "days": days,
        "models": models,
        "providers": providers,
        "provider_model_trends": provider_model_trends,
        "model_ranking": models[:8],
        "model_cache_rates": sorted(
            [{"model": m["name"], "total": m["total"], "cache_hit_pct": m["cache_hit_rate"]}
             for m in models], key=lambda x: x["total"], reverse=True
        )[:8],
        "provider_distribution": [{"provider": p["name"], "total": p["total"]} for p in providers],
        "composition": {
            "input": agg["input"],
            "output": agg["output"],
            "cache_read": agg["cache_read"],
            "cache_write": agg["cache_write"],
            "reasoning": agg["reasoning"],
        },
    }


# ═══════════════════════════════════════════════════════════════
# API Cache
# ═══════════════════════════════════════════════════════════════
_api_cache = {}
_api_cache_lock = threading.Lock()


def cached_stats(range_days):
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
# HTML (v2.2 complete rewrite)
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
  --bg: #f8fafc; --card-bg: #fff; --border: #e2e8f0;
  --text: #0f172a; --text-muted: #64748b; --text-dim: #94a3b8;
  --primary: #3b82f6; --primary-light: #dbeafe;
  --emerald: #10b981; --emerald-light: #d1fae5;
  --amber: #f59e0b; --amber-light: #fef3c7;
  --purple: #8b5cf6; --purple-light: #ede9fe;
  --rose: #f43f5e; --rose-light: #ffe4e6;
  --cyan: #06b6d4; --cyan-light: #cffafe;
  --grid: rgba(148,163,184,0.06); --grid-dark: rgba(148,163,184,0.12);
  --hero-bg: linear-gradient(135deg, #eff6ff 0%, #f5f3ff 50%, #f0fdf4 100%);
  --radius: 12px; --radius-lg: 16px;
  --shadow: 0 1px 2px rgba(0,0,0,0.03);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.05);
}
.dark {
  --bg: #0f172a; --card-bg: #1e293b; --border: #334155;
  --text: #e2e8f0; --text-muted: #94a3b8; --text-dim: #64748b;
  --primary: #60a5fa; --primary-light: #1e3a5f;
  --emerald: #34d399; --emerald-light: #064e3b;
  --amber: #fbbf24; --amber-light: #78350f;
  --purple: #a78bfa; --purple-light: #4c1d95;
  --rose: #fb7185; --rose-light: #4c0519;
  --cyan: #22d3ee; --cyan-light: #164e63;
  --grid: rgba(148,163,184,0.04); --grid-dark: rgba(148,163,184,0.08);
  --hero-bg: linear-gradient(135deg, #1e293b 0%, #1a1a2e 50%, #1e293b 100%);
  --shadow: 0 1px 2px rgba(0,0,0,0.2);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.3);
}
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;
  -webkit-font-smoothing:antialiased;
  background-image:
    linear-gradient(var(--grid) 1px,transparent 1px),
    linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:40px 40px;
}
.dark body{background-image:linear-gradient(var(--grid-dark) 1px,transparent 1px),linear-gradient(90deg,var(--grid-dark) 1px,transparent 1px)}
.container{max-width:1600px;margin:0 auto;padding:16px 24px}

/* Hero */
.hero{
  background:var(--hero-bg);border:1px solid var(--border);
  border-radius:var(--radius-lg);padding:24px 28px;margin-bottom:16px;
  box-shadow:var(--shadow);position:relative;overflow:hidden;
}
.hero::before{
  content:'';position:absolute;top:-50%;right:-20%;
  width:300px;height:300px;border-radius:50%;
  background:radial-gradient(circle,rgba(59,130,246,0.08) 0%,transparent 70%);
}
.hero::after{
  content:'';position:absolute;bottom:-30%;left:-10%;
  width:200px;height:200px;border-radius:50%;
  background:radial-gradient(circle,rgba(139,92,246,0.06) 0%,transparent 70%);
}
.hero-content{position:relative;z-index:1}
.hero-top{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.hero h1{font-size:22px;font-weight:700;letter-spacing:-0.3px}
.hero .badge{
  font-size:11px;font-weight:600;padding:3px 10px;border-radius:10px;
  background:var(--primary-light);color:var(--primary)
}
.hero .subtitle{font-size:13px;color:var(--text-muted);margin-bottom:12px}
.hero-meta{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-muted)}
.hero-meta span strong{color:var(--text)}

/* Controls */
.controls{
  display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap;
  padding:8px 12px;background:var(--card-bg);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);
}
.ctrl-btn{
  padding:6px 14px;border:1px solid var(--border);background:transparent;
  color:var(--text-muted);border-radius:8px;cursor:pointer;font-size:12px;
  font-weight:500;transition:all .15s;white-space:nowrap;
}
.ctrl-btn:hover{background:var(--bg);color:var(--text)}
.ctrl-btn.active{background:var(--primary);color:#fff;border-color:var(--primary)}
.ctrl-sep{width:1px;height:20px;background:var(--border);margin:0 4px}
.ctrl-select{
  padding:6px 10px;border:1px solid var(--border);border-radius:8px;
  background:var(--card-bg);color:var(--text);font-size:12px;cursor:pointer;
}

/* Cards row */
.cards-row{
  display:grid;grid-template-columns:repeat(5,1fr);
  gap:10px;margin-bottom:14px;
}
.stat-card{
  background:var(--card-bg);border:1px solid var(--border);
  border-radius:var(--radius);padding:14px;box-shadow:var(--shadow);
  transition:border-color .15s;
}
.stat-card:hover{border-color:var(--primary)}
.stat-card .lbl{font-size:11px;color:var(--text-muted);font-weight:500;text-transform:uppercase;letter-spacing:0.3px;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center}
.stat-card .val{font-size:24px;font-weight:700;letter-spacing:-0.5px;font-variant-numeric:tabular-nums}
.stat-card .sub{font-size:11px;color:var(--text-dim);margin-top:2px}
.icon-sq{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:13px}
.is-blue{background:var(--primary-light);color:var(--primary)}
.is-emerald{background:var(--emerald-light);color:var(--emerald)}
.is-amber{background:var(--amber-light);color:var(--amber)}
.is-purple{background:var(--purple-light);color:var(--purple)}
.is-rose{background:var(--rose-light);color:var(--rose)}
.is-cyan{background:var(--cyan-light);color:var(--cyan)}

/* Chart grid */
.chart-row2{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:14px}
.chart-row3{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.chart-card{
  background:var(--card-bg);border:1px solid var(--border);
  border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);
  transition:border-color .15s;
}
.chart-card:hover{border-color:var(--primary)}
.chart-card h3{font-size:13px;font-weight:600;margin-bottom:12px;color:var(--text);display:flex;align-items:center;gap:6px}
.chart-card h3 .badge{font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;background:var(--primary-light);color:var(--primary)}
.chart-wrap{position:relative}
.chart-wrap canvas{width:100%!important}

/* Gauges */
.gauges-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.gauge-card{text-align:center;padding:12px 8px}
.gauge-label{font-size:11px;color:var(--text-muted)}
.gauge-value{font-size:26px;font-weight:700;margin-bottom:2px}
.gv-purple{color:var(--purple)}.gv-amber{color:var(--amber)}

/* Table */
.table-scroll{max-height:360px;overflow-y:auto}
.table-scroll table{width:100%;border-collapse:collapse;font-size:12px}
.table-scroll th{
  position:sticky;top:0;background:var(--card-bg);
  color:var(--text-dim);font-weight:500;text-align:left;
  padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:0.3px;
  border-bottom:1px solid var(--border);
}
.table-scroll td{padding:6px 10px;border-bottom:1px solid rgba(148,163,184,0.15)}
.table-scroll tr:hover td{background:var(--bg)}
footer{text-align:center;color:var(--text-dim);font-size:10px;padding:12px 0 20px}

@media(max-width:1024px){.cards-row{grid-template-columns:repeat(3,1fr)}}
@media(max-width:768px){.cards-row{grid-template-columns:repeat(2,1fr)}.chart-row2,.chart-row3{grid-template-columns:1fr}}
@media(max-width:500px){.cards-row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
<!-- Hero -->
<div class="hero"><div class="hero-content">
  <div class="hero-top">
    <h1 data-i18n="title">Hermes Token Dashboard</h1>
    <span class="badge">Hermes Usage Monitor</span>
  </div>
  <div class="subtitle" data-i18n="subtitle">Local token usage dashboard for Hermes Agent</div>
  <div class="hero-meta">
    <span data-i18n="range">Range</span>: <strong id="hRange">—</strong> ·
    <span id="hDates">—</span> ·
    <span><span data-i18n="sessions_lc">sessions</span>: <strong id="hSessions">—</strong></span> ·
    <span><span data-i18n="updated">Updated</span>: <strong id="hUpdated">—</strong></span> ·
    <span>UTC+8</span>
  </div>
</div></div>

<!-- Controls -->
<div class="controls">
  <button class="ctrl-btn active" data-range="7">7<span data-i18n="d">d</span></button>
  <button class="ctrl-btn" data-range="30">30<span data-i18n="d">d</span></button>
  <button class="ctrl-btn" data-range="90">90<span data-i18n="d">d</span></button>
  <button class="ctrl-btn" data-range="180">180<span data-i18n="d">d</span></button>
  <button class="ctrl-btn" data-range="365">365<span data-i18n="d">d</span></button>
  <button class="ctrl-btn" data-range="all"><span data-i18n="all">All</span></button>
  <span class="ctrl-sep"></span>
  <select class="ctrl-select" id="metricSelect">
    <option value="total">Total Tokens</option>
    <option value="active">Active Tokens</option>
    <option value="input">Input</option>
    <option value="output">Output</option>
    <option value="reasoning">Reasoning</option>
    <option value="cache_read">Cache Read</option>
    <option value="cache_write">Cache Write</option>
    <option value="cache_hit_rate">Cache Hit Rate</option>
    <option value="user_message_count">Messages</option>
    <option value="runtime_dedup">Runtime</option>
    <option value="estimated_cost">Est. Cost</option>
  </select>
  <span class="ctrl-sep"></span>
  <button class="ctrl-btn" id="btnRefresh" onclick="fetchData()">↻</button>
  <button class="ctrl-btn" id="btnTheme" onclick="toggleTheme()">☀</button>
  <button class="ctrl-btn" id="btnLang" onclick="toggleLang()">EN</button>
</div>

<!-- Cards -->
<div class="cards-row">
  <div class="stat-card"><div class="lbl" data-i18n="today">Today<span class="icon-sq is-blue">&#x1F4C5;</span></div><div class="val" id="cToday">—</div><div class="sub" id="cTodaySub">—</div></div>
  <div class="stat-card"><div class="lbl" data-i18n="total">Total<span class="icon-sq is-emerald">&#x2211;</span></div><div class="val" id="cTotal">—</div><div class="sub" id="cTotalSub">—</div></div>
  <div class="stat-card"><div class="lbl" data-i18n="daily_avg">Daily Avg<span class="icon-sq is-purple">&#x2205;</span></div><div class="val" id="cAvg">—</div><div class="sub" id="cAvgSub">—</div></div>
  <div class="stat-card"><div class="lbl" data-i18n="peak">Peak<span class="icon-sq is-amber">&#x25B2;</span></div><div class="val" id="cPeak">—</div><div class="sub" id="cPeakSub">—</div></div>
  <div class="stat-card"><div class="lbl" id="cUtilLbl">Messages<span class="icon-sq is-rose">&#x1F4AC;</span></div><div class="val" id="cUtil">—</div><div class="sub" id="cUtilSub">—</div></div>
</div>

<!-- Trend + Breakdown -->
<div class="chart-row2">
  <div class="chart-card"><h3><span data-i18n="trend">TREND</span> <span class="badge" id="trendBadge">Daily</span></h3><div class="chart-wrap"><canvas id="trendChart"></canvas></div></div>
  <div class="chart-card">
    <h3><span data-i18n="breakdown">BREAKDOWN</span> <span class="badge">Token Composition</span></h3>
    <div class="gauges-row">
      <div class="gauge-card"><div class="gauge-label">Input Side</div><div class="gauge-value gv-purple" id="gvCache">—</div><div class="gauge-label">Cache Ratio</div></div>
      <div class="gauge-card"><div class="gauge-label">Output Side</div><div class="gauge-value gv-amber" id="gvReason">—</div><div class="gauge-label">Reasoning Ratio</div></div>
    </div>
    <div style="margin-top:8px"><canvas id="compChart" style="max-height:120px"></canvas></div>
  </div>
</div>

<!-- Leaderboard + Provider -->
<div class="chart-row3">
  <div class="chart-card"><h3><span data-i18n="leaderboard">LEADERBOARD</span> <span class="badge">Model</span></h3><div class="chart-wrap" style="height:280px"><canvas id="modelChart"></canvas></div></div>
  <div class="chart-card"><h3><span data-i18n="distribution">DISTRIBUTION</span> <span class="badge">Provider</span></h3><div class="chart-wrap" style="height:280px;display:flex;align-items:center;justify-content:center"><canvas id="provChart"></canvas></div></div>
</div>

<!-- Cache Hit Trend -->
<div class="chart-card" style="margin-bottom:14px">
  <h3><span data-i18n="performance">PERFORMANCE</span> <span class="badge">Cache Hit Rate by Provider+Model</span></h3>
  <div class="chart-wrap" style="height:260px"><canvas id="chTrendChart"></canvas></div>
</div>

<footer>Hermes Token Dashboard · state.db · v2.2 · UTC+8</footer>
</div>

<script>
// ═══ i18n ═══
var L = {
  zh:{title:'Hermes Token Dashboard',subtitle:'Hermes Agent 本地 Token 用量看板',range:'范围',sessions_lc:'sessions',updated:'更新',
    d:'天',all:'全部',today:'今日',total:'总计',daily_avg:'日均',peak:'峰值',
    trend:'趋势',breakdown:'分解',leaderboard:'排行',distribution:'分布',performance:'性能',
    msg:'消息',tot:'总量',per_day:'/天'},
  en:{title:'Hermes Token Dashboard',subtitle:'Local token usage dashboard for Hermes Agent',range:'Range',sessions_lc:'sessions',updated:'Updated',
    d:'d',all:'All',today:'Today',total:'Total',daily_avg:'Daily Avg',peak:'Peak',
    trend:'TREND',breakdown:'BREAKDOWN',leaderboard:'LEADERBOARD',distribution:'DISTRIBUTION',performance:'PERFORMANCE',
    msg:'Messages',tot:'Total','per_day':'/day'}
};
var lang='zh';
var metric='total', currentRange=30, lastData=null;
var trendChart,compChart,provChart,modelChart,chTrendChart;
var POLL_MS=3000;

function t(key){return (L[lang]||L.zh)[key]||key}
function setLang(l){lang=l;document.getElementById('btnLang').textContent=l==='zh'?'EN':'中文';
  document.querySelectorAll('[data-i18n]').forEach(function(el){el.textContent=t(el.dataset.i18n)});
  try{localStorage.setItem('dash-lang',l)}catch(e){};if(lastData)updateUI(lastData)}
function toggleLang(){setLang(lang==='zh'?'en':'zh')}

// Theme
function setTheme(dark){
  document.documentElement.classList.toggle('dark',dark);
  document.getElementById('btnTheme').textContent=dark?'☾':'☀';
  try{localStorage.setItem('dash-theme',dark?'dark':'light')}catch(e){}
}
function toggleTheme(){setTheme(!document.documentElement.classList.contains('dark'))}

// Format
function fm(v){if(v==null||isNaN(v))return'—';return v.toLocaleString('en-US',{maximumFractionDigits:0})}
function fs(v){if(v==null||isNaN(v))return'—';if(v>=1e9)return(v/1e9).toFixed(2)+'B';if(v>=1e6)return(v/1e6).toFixed(2)+'M';if(v>=1e3)return(v/1e3).toFixed(1)+'K';return v.toLocaleString()}
function fc(v){if(v==null||isNaN(v))return'—';return'$'+v.toFixed(2)}
function sm(n){if(!n)return'unknown';var s=String(n);if(s.includes('/'))s=s.split('/').pop();
  s=s.replace(/-preview|-a3b|-a12b|:free|:beta/g,'');if(s.length>18)s=s.slice(0,16)+'…';return s}

// Metric helpers
function getVal(entry, m){
  if(m==='cache_hit_rate')return entry.cache_hit_rate||0;
  if(m==='user_message_count')return entry.user_message_count||0;
  if(m==='runtime_dedup')return entry.runtime_dedup||0;
  if(m==='estimated_cost')return entry.estimated_cost||0;
  return entry[m]||0;
}
function fmtVal(v,m){if(m==='cache_hit_rate')return v.toFixed(1)+'%';if(m==='estimated_cost')return fc(v);if(m==='runtime_dedup'){var h=Math.floor(v/3600),mi=Math.floor((v%3600)/60);return h+'h '+mi+'m'}return fs(v)}
function cardLabel(m){
  if(m==='estimated_cost')return'Est. Cost';
  return m.replace(/_/g,' ').replace(/\b\w/g,function(c){return c.toUpperCase()});
}

// Init charts
function initCharts(){
  var tCtx=document.getElementById('trendChart').getContext('2d');
  trendChart=new Chart(tCtx,{type:'line',data:{labels:[],datasets:[
    {label:'',data:[],borderColor:'#3b82f6',backgroundColor:function(ctx){var g=ctx.chart.ctx.createLinearGradient(0,0,0,220);g.addColorStop(0,'rgba(59,130,246,0.2)');g.addColorStop(1,'rgba(59,130,246,0)');return g},fill:true,tension:0.3,pointRadius:0,pointHitRadius:8,borderWidth:2,yAxisID:'y'},
    {label:'',data:[],borderColor:'#10b981',borderDash:[5,3],tension:0.3,pointRadius:0,pointHitRadius:8,borderWidth:1.5,yAxisID:'y1'}
  ]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    plugins:{legend:{labels:{usePointStyle:true,pointStyleWidth:8,padding:20,font:{size:11},color:'#64748b'}},
    tooltip:{callbacks:{label:function(ctx){return ctx.dataset.label+': '+fmtVal(ctx.parsed.y,metric==='user_message_count'&&ctx.datasetIndex===0?'user_message_count':metric)}}}},
    scales:{x:{ticks:{color:'#94a3b8',maxRotation:45,font:{size:10}},grid:{display:false}},
      y:{type:'linear',position:'left',ticks:{color:'#94a3b8',callback:function(v){return fmtVal(v,metric)},font:{size:10}},grid:{color:'#f1f5f9'},border:{display:false}},
      y1:{type:'linear',position:'right',ticks:{color:'#94a3b8',font:{size:10}},grid:{display:false},border:{display:false}}}},
    plugins:[{id:'th',beforeInit:function(c){c.canvas.parentNode.style.height='220px'}}]});

  compChart=new Chart(document.getElementById('compChart').getContext('2d'),{type:'bar',data:{labels:[],datasets:[{data:[],backgroundColor:[]}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:true,plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#94a3b8',callback:fs,font:{size:9}},grid:{display:false}},y:{ticks:{color:'#64748b',font:{size:10}},grid:{display:false}}}}});

  provChart=new Chart(document.getElementById('provChart').getContext('2d'),{type:'doughnut',data:{labels:[],datasets:[{data:[],backgroundColor:['#3b82f6','#10b981','#8b5cf6','#f59e0b','#f43f5e','#06b6d4','#94a3b8'],borderWidth:2,borderColor:'#fff'}]},options:{responsive:true,cutout:'60%',plugins:{legend:{position:'bottom',labels:{padding:12,font:{size:10},color:'#64748b'}}}}});

  modelChart=new Chart(document.getElementById('modelChart').getContext('2d'),{type:'bar',data:{labels:[],datasets:[{data:[],backgroundColor:function(ctx){return ctx.dataIndex===0?'#f59e0b':'#3b82f6'},borderRadius:4}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:function(ctx){return fmtVal(ctx.parsed.x,metric)}}}},scales:{x:{ticks:{color:'#94a3b8',callback:function(v){return fmtVal(v,metric)},font:{size:10}},grid:{color:'#f1f5f9'}},y:{ticks:{color:'#64748b',font:{size:10}},grid:{display:false}}}}});

  chTrendChart=new Chart(document.getElementById('chTrendChart').getContext('2d'),{type:'line',data:{labels:[],datasets:[]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{position:'bottom',labels:{font:{size:9},color:'#64748b',padding:6,usePointStyle:true,pointStyleWidth:6},onClick:function(e,item,legend){if(!item)return;var ci=item.datasetIndex;var meta=this.chart.getDatasetMeta(ci);meta.hidden=meta.hidden===null?!meta.hidden:null;this.chart.update()}}},scales:{x:{ticks:{color:'#94a3b8',maxRotation:45,font:{size:9}},grid:{display:false}},y:{min:0,max:100,ticks:{color:'#94a3b8',callback:function(v){return v+'%'},font:{size:10}},grid:{color:'#f1f5f9'}}}},plugins:[{id:'chth',beforeInit:function(c){c.canvas.parentNode.style.height='260px'}}]});
}

// Colors
var PM_COLORS=['#3b82f6','#10b981','#8b5cf6','#f59e0b','#f43f5e','#06b6d4','#ec4899','#6366f1','#14b8a6','#eab308','#ef4444','#84cc16'];

// Update UI
function updateUI(data){
  lastData=data;
  if(data.error){document.querySelector('.hero .subtitle').textContent='Error: '+data.message;return}
  var s=data.summary,m=data.meta,d=data.days||[];

  // Hero
  document.getElementById('hRange').textContent=m.range==='all'?t('all'):m.range+' '+t('d');
  document.getElementById('hDates').textContent=m.first_day+' — '+m.last_day;
  document.getElementById('hSessions').textContent=s.sessions;
  document.getElementById('hUpdated').textContent=new Date().toLocaleTimeString();

  // Cards follow metric
  var mainVal=getVal(s,metric);
  var todayVal=metric==='estimated_cost'?0:getVal(s,metric);
  if(metric==='user_message_count')todayVal=s.user_message_count;
  var peakDay=s.peak_day||'—';
  var peakVal=getVal({total:s.peak,active:s.peak,input:s.input,output:s.output,reasoning:s.reasoning,cache_read:s.cache_read,cache_write:s.cache_write,cache_hit_rate:s.cache_hit_rate,user_message_count:s.user_message_count,runtime_dedup:s.runtime_dedup,estimated_cost:s.estimated_cost},metric);
  if(metric==='daily_avg'){mainVal=s.daily_avg;todayVal=s.daily_avg}
  if(metric==='cache_hit_rate'){mainVal=s.cache_hit_rate;todayVal=s.cache_hit_rate;peakVal=s.cache_hit_rate}
  if(metric==='runtime_dedup'){mainVal=s.runtime_dedup;todayVal=s.runtime_dedup;peakVal=s.runtime_dedup}

  document.getElementById('cToday').textContent=fmtVal(todayVal,metric);
  document.getElementById('cTodaySub').textContent=cardLabel(metric);
  document.getElementById('cTotal').textContent=fmtVal(mainVal,metric);
  document.getElementById('cTotalSub').textContent='Period '+cardLabel(metric);
  document.getElementById('cAvg').textContent=fmtVal(s.daily_avg,metric==='cache_hit_rate'?'cache_hit_rate':metric==='estimated_cost'?'estimated_cost':metric);
  document.getElementById('cAvgSub').textContent=t('per_day');
  document.getElementById('cPeak').textContent=fmtVal(peakVal,metric);
  document.getElementById('cPeakSub').textContent='on '+peakDay;

  // Utility card
  var isMsg=metric==='user_message_count';
  document.getElementById('cUtilLbl').innerHTML=(isMsg?t('tot'):t('msg'))+' <span class="icon-sq '+(isMsg?'is-emerald':'is-rose')+'">'+(isMsg?'∑':'💬')+'</span>';
  document.getElementById('cUtil').textContent=isMsg?fs(s.total):fm(s.user_message_count);
  document.getElementById('cUtilSub').textContent=isMsg?'Period sum':fm(s.messages_per_day)+' '+t('per_day');

  // Trend
  var is7=currentRange===7;
  document.getElementById('trendBadge').textContent=is7?'Hourly':'Daily';
  trendChart.data.labels=d.map(function(x){return is7?x.date.slice(5,10)+' '+x.date.slice(11,16):x.date.slice(5)});
  var mainData=d.map(function(x){return getVal(x,metric)});
  var auxData=d.map(function(x){return metric==='user_message_count'?x.total:x.user_message_count});
  trendChart.data.datasets[0].data=mainData;
  trendChart.data.datasets[0].label=cardLabel(metric);
  trendChart.data.datasets[1].data=auxData;
  trendChart.data.datasets[1].label=metric==='user_message_count'?t('tot'):t('msg');
  trendChart.data.datasets[1].hidden=(mainData.length===0);
  trendChart.update();

  // Breakdown gauges
  var chDenom=s.input+s.cache_read;
  document.getElementById('gvCache').textContent=(chDenom>0?(s.cache_read/chDenom*100).toFixed(1):'0.0')+'%';
  var outDenom=s.output+s.reasoning;
  document.getElementById('gvReason').textContent=(outDenom>0?(s.reasoning/outDenom*100).toFixed(1):'0.0')+'%';
  compChart.data.labels=['Input','Cache Read','Cache Write','Output','Reasoning'];
  compChart.data.datasets[0].data=[s.input,s.cache_read,s.cache_write,s.output,s.reasoning];
  compChart.data.datasets[0].backgroundColor=['#3b82f6','#8b5cf6','#f59e0b','#10b981','#f43f5e'];
  compChart.update();

  // Provider
  var provs=(data.providers||[]).slice(0,6);
  var provOthers=0;
  (data.providers||[]).slice(6).forEach(function(p){provOthers+=getVal(p,metric)});
  var provData=provs.map(function(p){return getVal(p,metric)});
  var provLabels=provs.map(function(p){return p.name});
  if(provOthers>0){provData.push(provOthers);provLabels.push('Other')}
  provChart.data.labels=provLabels;
  provChart.data.datasets[0].data=provData;
  provChart.update();

  // Model ranking
  var models=(data.models||[]).slice(0,8);
  modelChart.data.labels=models.map(function(m){return sm(m.name)});
  modelChart.data.datasets[0].data=models.map(function(m){return getVal(m,metric)});
  modelChart.update();

  // Cache hit rate trend
  var pmts=data.provider_model_trends||[];
  chTrendChart.data.labels=(pmts[0]||{}).days?pmts[0].days.map(function(d){return d.date.slice(5)}):[];
  chTrendChart.data.datasets=pmts.map(function(pmt,i){
    return {label:sm(pmt.provider)+'/'+sm(pmt.model),data:pmt.days.map(function(d){return d.cache_hit_rate||0}),borderColor:PM_COLORS[i%12],backgroundColor:PM_COLORS[i%12],tension:0.3,pointRadius:0,borderWidth:1.5};
  });
  chTrendChart.update();
}

async function fetchData(){
  try{
    var rp=(currentRange===null)?'all':currentRange;
    var r=await fetch('/api/usage?range='+rp);
    if(r.ok)updateUI(await r.json());else console.warn('API status:',r.status)
  }catch(e){console.warn('Fetch failed:',e.message||e)}
}

function connectSSE(){
  var es=new EventSource('/api/events');
  es.onmessage=function(){fetchData()};
  es.onerror=function(){es.close()}
}

// Init
(function(){
  try{var sl=localStorage.getItem('dash-lang');if(sl)lang=sl}catch(e){}
  try{var st=localStorage.getItem('dash-theme');if(st==='dark')setTheme(true)}catch(e){}
  setLang(lang);
  document.querySelectorAll('.controls .ctrl-btn[data-range]').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('.controls .ctrl-btn[data-range]').forEach(function(x){x.classList.remove('active')});
      b.classList.add('active');
      currentRange=b.dataset.range==='all'?null:parseInt(b.dataset.range,10);
      fetchData();
    });
  });
  document.querySelectorAll('.controls .ctrl-btn[data-range="30"]').forEach(function(b){b.classList.add('active')});
  document.getElementById('metricSelect').addEventListener('change',function(){
    metric=this.value;if(lastData)updateUI(lastData);
  });
  initCharts();fetchData();connectSSE();setInterval(fetchData,POLL_MS+1000);
})();
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
            if range_val and range_val not in ("all", "null", ""):
                try: range_days = int(range_val)
                except (ValueError, TypeError): range_days = None
            else: range_days = None
            try:
                stats = cached_stats(range_days)
                self._send_json(stats)
            except DatabaseError as e:
                self._send_json({"error": "database_not_found", "message": str(e)}, 500)
            except Exception as e:
                self._send_json({"error": "internal_error", "message": str(e)}, 500)
        elif path == "/api/events":
            try: self._send_sse()
            except Exception: pass
        else:
            self._send_json({"error": "not_found", "message": "Unknown path"}, 404)


def main():
    print("  Hermes Token Dashboard v2.2")
    print(f"  Data source: {DB_PATH}")
    print(f"  Timezone: UTC+8")
    print(f"  → http://{HOST}:{PORT}")
    server = http.server.ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    try: server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
