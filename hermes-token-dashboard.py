#!/usr/bin/env python3
"""
Hermes Token Dashboard v2.3

Local token usage dashboard for Hermes Agent.

Data source:
    ~/.hermes/state.db -> sessions table

Run:
    python3 hermes-token-dashboard.py
    http://127.0.0.1:8765

Design:
    Python standard library only + Chart.js CDN.
    No Node.js, no Rust, no Python third-party dependencies.
    UTC+8 fixed timezone.

v2.3: Code readability improvement — de-compressed Python and HTML/JS.
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
# 1. Configuration
# ═══════════════════════════════════════════════════════════════

DB_PATH = os.path.expanduser("~/.hermes/state.db")
DB_WAL_PATH = DB_PATH + "-wal"
DB_SHM_PATH = DB_PATH + "-shm"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
POLL_INTERVAL = 3  # seconds between SSE push checks
TZ = timezone(timedelta(hours=8))  # UTC+8, Beijing time


# ═══════════════════════════════════════════════════════════════
# 2. Model Pricing Table (USD per 1M tokens)
# ═══════════════════════════════════════════════════════════════

MODEL_PRICING = {
    "deepseek-v4-pro":      {"input": 12,   "output": 24,  "cache_read": 1,     "cache_write": 12,  "reasoning": 12},
    "deepseek-v4-flash":    {"input": 1,    "output": 2,   "cache_read": 0.2,   "cache_write": 1,   "reasoning": 1},
    "deepseek-v4":          {"input": 1,    "output": 2,   "cache_read": 0.2,   "cache_write": 1,   "reasoning": 1},
    "gpt-5.5":              {"input": 2.5,  "output": 10,  "cache_read": 1.25,  "cache_write": 2.5, "reasoning": 15},
    "gpt-5.4":              {"input": 1.25, "output": 5,   "cache_read": 0.625, "cache_write": 1.25,"reasoning": 7.5},
    "gpt-5":                {"input": 1.25, "output": 5,   "cache_read": 0.625, "cache_write": 1.25,"reasoning": 7.5},
    "gemini-3.1-flash-lite":{"input": 0.075,"output": 0.30,"cache_read": 0.01875,"cache_write": 0.075,"reasoning": 0.075},
    "gemini-3.0-flash":     {"input": 0.15, "output": 0.60,"cache_read": 0.0375,"cache_write": 0.15,"reasoning": 0.15},
    "gemini-3":             {"input": 1.25, "output": 5,   "cache_read": 0.3125,"cache_write": 1.25,"reasoning": 5},
    "minimax-m2.7":         {"input": 0.3,  "output": 1.2, "cache_read": 0.075, "cache_write": 0.3, "reasoning": 0.3},
    "minimax-m2.5":         {"input": 0.3,  "output": 1.2, "cache_read": 0.075, "cache_write": 0.3, "reasoning": 0.3},
    "mimo-v2.5-pro":        {"input": 1.5,  "output": 6,   "cache_read": 0.375, "cache_write": 1.5, "reasoning": 1.5},
    "mimo-v2.5":            {"input": 1.5,  "output": 6,   "cache_read": 0.375, "cache_write": 1.5, "reasoning": 1.5},
    "mimo-v2.5-free":       {"input": 0,    "output": 0,   "cache_read": 0,     "cache_write": 0,   "reasoning": 0},
    "gemma-4-31b-it":       {"input": 0,    "output": 0,   "cache_read": 0,     "cache_write": 0,   "reasoning": 0},
    "default":              {"input": 1,    "output": 3,   "cache_read": 0.25,  "cache_write": 1,   "reasoning": 1},
}

# Keywords that identify free-tier models
FREE_KEYWORDS = [
    ":free", "mimo-free", "mimo-v2.5-free",
    "gemma", "nemotron", "gpt-oss", "hy3-preview", "local",
]


def get_pricing(model_name):
    """Return (pricing_dict, matched_key) for a model name.

    Matching order:
    1. Exact match in MODEL_PRICING (case-insensitive)
    2. Free keyword detection (highest priority before prefix matching)
    3. Strip org/ prefix (OpenRouter format), then longest-prefix match
    4. Fallback to "default" pricing
    """
    if not model_name:
        return MODEL_PRICING["default"], "default"

    lower_full = model_name.lower()

    # Step 1: exact match
    if lower_full in MODEL_PRICING:
        return MODEL_PRICING[lower_full], lower_full

    # Step 2: free model detection (before any prefix matching)
    for kw in FREE_KEYWORDS:
        if kw in lower_full:
            free_pricing = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
            return free_pricing, "free"

    # Step 3: strip org/ prefix for OpenRouter-style model names
    stripped = model_name
    if "/" in stripped:
        stripped = stripped.split("/")[-1]

    # Step 4: longest-prefix match against known paid models
    sorted_keys = sorted(
        [k for k in MODEL_PRICING if k != "default"],
        key=len, reverse=True
    )
    lower_stripped = stripped.lower()
    for key in sorted_keys:
        if lower_stripped.startswith(key):
            return MODEL_PRICING[key], key

    # Step 5: unknown model → default pricing
    return MODEL_PRICING["default"], "default"


# ═══════════════════════════════════════════════════════════════
# 3. Data Engine — WAL-aware SQLite reader with signature cache
# ═══════════════════════════════════════════════════════════════

_cache = None
_cache_signature = ""


def _fms(path):
    """Return mtime:size string for a file, or empty string if not found."""
    try:
        stat = os.stat(path)
        return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        return ""


def _db_sig():
    """Build a signature from state.db, state.db-wal, and state.db-shm.
    Any change in any of these files triggers cache invalidation.
    """
    return "|".join([
        _fms(DB_PATH),
        _fms(DB_WAL_PATH),
        _fms(DB_SHM_PATH),
    ])


# Compatibility aliases for older test code
_file_mtime_size = _fms
_db_signature = _db_sig


class DatabaseError(Exception):
    """Raised when the SQLite database cannot be read."""
    pass


def _read_db():
    """Read all sessions from state.db.
    Returns a list of dicts. Raises DatabaseError on failure.
    """
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
    """Return cached session data, or re-read from DB if signature changed."""
    global _cache, _cache_signature
    sig = _db_sig()
    if not force and _cache is not None and sig == _cache_signature:
        return _cache
    _cache = _read_db()
    _cache_signature = sig
    return _cache


# ═══════════════════════════════════════════════════════════════
# 4. Aggregation Helpers
# ═══════════════════════════════════════════════════════════════

def normalize_model(m):
    """Normalize model name: strip @provider: and org/ prefixes."""
    if not m:
        return "unknown"
    if m.startswith("@"):
        m = m.split(":", 1)[-1] if ":" in m else m
    if "/" in m:
        m = m.split("/")[-1]
    return m


def _sg(d, key, default=0):
    """Safe get from dict, returning default for None/0 values."""
    return d.get(key) or default


def _calc_cost(model, inp, out, cr, cw, rt):
    """Calculate estimated cost in USD for a single session."""
    pricing, _ = get_pricing(model)
    return (
        inp * pricing["input"]
        + out * pricing["output"]
        + cr * pricing["cache_read"]
        + cw * pricing["cache_write"]
        + rt * pricing["reasoning"]
    ) / 1_000_000


def _build_day_entry(key, d, is_hourly=False):
    """Build a single day/hour entry for the 'days' array."""
    total_tokens = (
        d["input"] + d["output"] + d["cache_read"]
        + d["cache_write"] + d["reasoning"]
    )
    denom = d["input"] + d["cache_read"]
    return {
        "date": key,
        "total": total_tokens,
        "active": d["input"] + d["output"] + d["reasoning"],
        "input": d["input"],
        "output": d["output"],
        "reasoning": d["reasoning"],
        "cache_read": d["cache_read"],
        "cache_write": d["cache_write"],
        "cache_hit_rate": round(d["cache_read"] / denom * 100, 2) if denom > 0 else 0.0,
        "runtime_dedup": 0,
        "user_message_count": d.get("messages", 0),
        "estimated_cost": 0,
    }


def _build_model_entry(model_key, d):
    """Build a model aggregate entry for the 'models' array."""
    total_tokens = (
        d["input"] + d["output"] + d["cache_read"]
        + d["cache_write"] + d["reasoning"]
    )
    denom = d["input"] + d["cache_read"]
    return {
        "name": model_key,
        "total": total_tokens,
        "active": d["input"] + d["output"] + d["reasoning"],
        "input": d["input"],
        "output": d["output"],
        "reasoning": d["reasoning"],
        "cache_read": d["cache_read"],
        "cache_write": d["cache_write"],
        "cache_hit_rate": round(d["cache_read"] / denom * 100, 2) if denom > 0 else 0.0,
        "runtime_dedup": 0,
        "user_message_count": 0,
        "estimated_cost": d["cost"],
        "sessions": d["sessions"],
        "api_calls": d["api_calls"],
    }


# ═══════════════════════════════════════════════════════════════
# 5. Main Aggregation
# ═══════════════════════════════════════════════════════════════

def aggregate_stats(sessions, range_days=None):
    """Aggregate session data into the full API response structure.

    Returns a dict with keys: meta, summary, trend, hour_trend, days,
    models, providers, provider_model_trends, composition.
    """
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Filter by time range
    if range_days:
        cutoff = now - timedelta(days=range_days)
        sessions = [
            s for s in sessions
            if s.get("started_at")
            and datetime.fromtimestamp(s["started_at"], TZ) >= cutoff
        ]

    # Accumulators
    agg = {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "reasoning": 0, "api_calls": 0, "messages": 0, "cost": 0.0,
    }
    today_tokens = 0
    by_model = {}
    by_provider_raw = {}
    by_day = {}
    by_hour = {}
    intervals = []
    pm_trends_raw = {}  # (provider, model) -> {day_key: {input, cache_read, cache_write, total}}

    # Iterate sessions
    for s in sessions:
        inp = _sg(s, "input_tokens")
        out = _sg(s, "output_tokens")
        cr = _sg(s, "cache_read_tokens")
        cw = _sg(s, "cache_write_tokens")
        rt = _sg(s, "reasoning_tokens")
        api = _sg(s, "api_call_count")
        msg = _sg(s, "message_count")
        model = s.get("model") or "unknown"
        provider = s.get("billing_provider") or "unknown"
        model_key = normalize_model(model)

        # Global aggregates
        agg["input"] += inp
        agg["output"] += out
        agg["cache_read"] += cr
        agg["cache_write"] += cw
        agg["reasoning"] += rt
        agg["api_calls"] += api
        agg["messages"] += msg
        agg["cost"] += _calc_cost(model, inp, out, cr, cw, rt)

        # Today tokens
        if s.get("started_at"):
            st = datetime.fromtimestamp(s["started_at"], TZ)
            if st >= today_start:
                today_tokens += inp + out + cr + cw + rt

        # Per-model aggregation
        bm_d = by_model.setdefault(model_key, {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "reasoning": 0, "sessions": 0, "cost": 0, "api_calls": 0,
        })
        bm_d["input"] += inp
        bm_d["output"] += out
        bm_d["cache_read"] += cr
        bm_d["cache_write"] += cw
        bm_d["reasoning"] += rt
        bm_d["sessions"] += 1
        bm_d["cost"] += _calc_cost(model, inp, out, cr, cw, rt)
        bm_d["api_calls"] += api

        # Per-provider aggregation
        bp_p = by_provider_raw.setdefault(provider, {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "reasoning": 0, "cost": 0.0,
        })
        bp_p["input"] += inp
        bp_p["output"] += out
        bp_p["cache_read"] += cr
        bp_p["cache_write"] += cw
        bp_p["reasoning"] += rt
        bp_p["cost"] += _calc_cost(model, inp, out, cr, cw, rt)

        # Time-based aggregation
        if s.get("started_at"):
            day_key = st.strftime("%Y-%m-%d")
            bd = by_day.setdefault(day_key, {
                "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                "reasoning": 0, "messages": 0, "sessions": 0,
            })
            bd["input"] += inp
            bd["output"] += out
            bd["cache_read"] += cr
            bd["cache_write"] += cw
            bd["reasoning"] += rt
            bd["messages"] += msg
            bd["sessions"] += 1

            # Hour-level aggregation
            hour_key = st.strftime("%Y-%m-%dT%H:00")
            bh = by_hour.setdefault(hour_key, {
                "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                "reasoning": 0, "messages": 0, "total": 0,
            })
            bh["input"] += inp
            bh["output"] += out
            bh["cache_read"] += cr
            bh["cache_write"] += cw
            bh["reasoning"] += rt
            bh["messages"] += msg
            bh["total"] += inp + out + cr + cw + rt

            # Provider-model trends (day-level only)
            pm_key = (provider, model_key)
            pmd = pm_trends_raw.setdefault(pm_key, {}).setdefault(day_key, {
                "input": 0, "cache_read": 0, "cache_write": 0, "total": 0,
            })
            pmd["input"] += inp
            pmd["cache_read"] += cr
            pmd["cache_write"] += cw
            pmd["total"] += inp + out + cr + cw + rt

        # Runtime intervals for dedup
        if s.get("started_at") and s.get("ended_at"):
            intervals.append((s["started_at"], s["ended_at"]))

    # Peak day
    peak_day = max(
        by_day.items(),
        key=lambda x: (
            x[1]["input"] + x[1]["output"] + x[1]["cache_read"]
            + x[1]["cache_write"] + x[1]["reasoning"]
        ),
        default=("—", {}),
    )
    peak_val = (
        peak_day[1].get("input", 0) + peak_day[1].get("output", 0)
        + peak_day[1].get("cache_read", 0) + peak_day[1].get("cache_write", 0)
        + peak_day[1].get("reasoning", 0)
    )

    # Deduplicated runtime (merge overlapping intervals)
    runtime_dedup = 0
    if intervals:
        intervals.sort()
        merged = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        runtime_dedup = sum(e - s for s, e in merged)

    # Overall metrics
    all_total = (
        agg["input"] + agg["output"] + agg["cache_read"]
        + agg["cache_write"] + agg["reasoning"]
    )
    ch_denom = agg["input"] + agg["cache_read"]
    cache_hit_rate = round(agg["cache_read"] / ch_denom * 100, 2) if ch_denom > 0 else 0.0
    days_count = len(by_day) or 1
    daily_avg = all_total / days_count

    # Active sessions (last 30 minutes)
    active_cutoff = (now - timedelta(minutes=30)).timestamp()
    active_sessions = len([
        s for s in sessions
        if s.get("started_at") and s["started_at"] >= active_cutoff
    ])

    # Build 'days' array (hourly for 7d, daily otherwise)
    is_7d = (range_days == 7)
    source = by_hour if is_7d else by_day
    days = [_build_day_entry(k, v, is_7d) for k, v in sorted(source.items())]

    # Build 'models' array (sorted by total tokens)
    models = [_build_model_entry(k, v) for k, v in by_model.items()]
    models.sort(key=lambda x: x["total"], reverse=True)

    # Build 'providers' array
    providers = []
    for p, d in by_provider_raw.items():
        total_tokens = (
            d["input"] + d["output"] + d["cache_read"]
            + d["cache_write"] + d["reasoning"]
        )
        denom = d["input"] + d["cache_read"]
        providers.append({
            "name": p,
            "total": total_tokens,
            "active": d["input"] + d["output"] + d["reasoning"],
            "input": d["input"],
            "output": d["output"],
            "reasoning": d["reasoning"],
            "cache_read": d["cache_read"],
            "cache_write": d["cache_write"],
            "cache_hit_rate": round(d["cache_read"] / denom * 100, 2) if denom > 0 else 0.0,
            "estimated_cost": d["cost"],
        })
    providers.sort(key=lambda x: x["total"], reverse=True)

    # Build 'provider_model_trends' (Top 12)
    provider_model_trends = []
    for (prov, mod), day_map in pm_trends_raw.items():
        total_t = sum(v["total"] for v in day_map.values())
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
            "_total": total_t,
        })
    provider_model_trends.sort(key=lambda x: x["_total"], reverse=True)
    provider_model_trends = provider_model_trends[:12]
    for entry in provider_model_trends:
        del entry["_total"]

    # Date range
    dates_in_range = sorted(by_day.keys())

    # Assemble response
    return {
        "meta": {
            "database": "state.db",
            "range": str(range_days) if range_days else "all",
            "first_day": dates_in_range[0] if dates_in_range else "—",
            "last_day": dates_in_range[-1] if dates_in_range else "—",
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
            "active": agg["input"] + agg["output"] + agg["reasoning"],
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
             for m in models],
            key=lambda x: x["total"], reverse=True,
        )[:8],
        "provider_distribution": [
            {"provider": p["name"], "total": p["total"]} for p in providers
        ],
        "composition": {
            "input": agg["input"],
            "output": agg["output"],
            "cache_read": agg["cache_read"],
            "cache_write": agg["cache_write"],
            "reasoning": agg["reasoning"],
        },
    }


# ═══════════════════════════════════════════════════════════════
# 6. API Response Cache
# ═══════════════════════════════════════════════════════════════

_api_cache = {}
_api_cache_lock = threading.Lock()


def cached_stats(range_days):
    """Return cached aggregated stats for a given range, recomputing if DB changed."""
    key = f"range_{range_days}"
    sig = _db_sig()
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
# 7. HTML Page (embedded, single-file)
# ═══════════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Token Dashboard v2.3</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js"></script>
<style>
/* ═══ CSS Variables — Light Theme ═══ */
:root {
  --bg: #f8fafc;
  --card-bg: #fff;
  --border: #e2e8f0;
  --text: #0f172a;
  --text-muted: #64748b;
  --primary: #3b82f6;
  --primary-light: #dbeafe;
  --emerald: #10b981;
  --amber: #f59e0b;
  --purple: #8b5cf6;
  --rose: #f43f5e;
  --radius: 12px;
  --shadow: 0 1px 2px rgba(0,0,0,0.03);
}

/* ═══ Dark Theme Overrides ═══ */
.dark {
  --bg: #0f172a;
  --card-bg: #1e293b;
  --border: #334155;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --primary: #60a5fa;
  --primary-light: #1e3a5f;
  --emerald: #34d399;
  --amber: #fbbf24;
  --purple: #a78bfa;
  --rose: #fb7185;
  --shadow: 0 1px 2px rgba(0,0,0,0.2);
}

/* ═══ Reset & Base ═══ */
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}
.container { max-width: 1600px; margin: 0 auto; padding: 16px 24px; }

/* ═══ Hero Panel ═══ */
.hero {
  background: linear-gradient(135deg, #eff6ff, #f5f3ff, #f0fdf4);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px 24px;
  margin-bottom: 14px;
  box-shadow: var(--shadow);
}
.dark .hero {
  background: linear-gradient(135deg, #1e293b, #1a1a2e, #1e293b);
}
.hero h1 { font-size: 22px; font-weight: 700; }
.hero .badge {
  font-size: 11px; font-weight: 600;
  padding: 3px 10px; border-radius: 10px;
  background: var(--primary-light); color: var(--primary);
}
.hero-meta {
  display: flex; gap: 14px; flex-wrap: wrap;
  font-size: 12px; color: var(--text-muted); margin-top: 8px;
}

/* ═══ Controls Bar ═══ */
.controls {
  display: flex; align-items: center; gap: 6px;
  margin-bottom: 14px; flex-wrap: wrap;
  padding: 6px 10px;
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow);
}
.ctrl-btn {
  padding: 5px 12px;
  border: 1px solid var(--border); background: transparent;
  color: var(--text-muted); border-radius: 7px;
  cursor: pointer; font-size: 12px; font-weight: 500;
}
.ctrl-btn.active {
  background: var(--primary); color: #fff; border-color: var(--primary);
}
.ctrl-select {
  padding: 5px 8px;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--card-bg); color: var(--text); font-size: 12px;
}

/* ═══ Summary Cards ═══ */
.cards-row {
  display: grid; grid-template-columns: repeat(5, 1fr);
  gap: 10px; margin-bottom: 14px;
}
.stat-card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px; box-shadow: var(--shadow);
}
.stat-card .lbl {
  font-size: 11px; color: var(--text-muted); font-weight: 500; margin-bottom: 4px;
}
.stat-card .val {
  font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums;
}
.stat-card .sub { font-size: 10px; color: var(--text-muted); }

/* ═══ Chart Layout ═══ */
.chart-row2 {
  display: grid; grid-template-columns: 2fr 1fr;
  gap: 14px; margin-bottom: 14px;
}
.chart-row3 {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 14px; margin-bottom: 14px;
}
.chart-card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow);
}
.chart-card h3 { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.chart-wrap { position: relative; }
.chart-wrap canvas { width: 100% !important; }

/* ═══ Footer ═══ */
footer {
  text-align: center; color: var(--text-muted);
  font-size: 10px; padding: 12px 0 20px;
}

/* ═══ Responsive ═══ */
@media (max-width: 768px) {
  .cards-row { grid-template-columns: repeat(2, 1fr); }
  .chart-row2, .chart-row3 { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="container">

<!-- ═══ Hero Panel ═══ -->
<div class="hero">
  <div>
    <h1 style="display:inline">Hermes Token Dashboard</h1>
    <span class="badge">v2.3</span>
  </div>
  <div class="hero-meta">
    <span id="hMeta">Loading...</span>
  </div>
</div>

<!-- ═══ Controls Bar ═══ -->
<div class="controls">

  <!-- Time Range Buttons -->
  <button class="ctrl-btn active" data-range="30">30d</button>
  <button class="ctrl-btn" data-range="7">7d</button>
  <button class="ctrl-btn" data-range="90">90d</button>
  <button class="ctrl-btn" data-range="180">180d</button>
  <button class="ctrl-btn" data-range="365">365d</button>
  <button class="ctrl-btn" data-range="all">All</button>

  <!-- Metric Selector -->
  <select class="ctrl-select" id="metricSelect">
    <option value="total">Total</option>
    <option value="active">Active</option>
    <option value="input">Input</option>
    <option value="output">Output</option>
    <option value="reasoning">Reasoning</option>
    <option value="cache_read">Cache Read</option>
    <option value="cache_write">Cache Write</option>
    <option value="cache_hit_rate">Cache Hit%</option>
    <option value="user_message_count">Messages</option>
    <option value="runtime_dedup">Runtime</option>
    <option value="estimated_cost">Cost</option>
  </select>

  <!-- Utility Buttons -->
  <button class="ctrl-btn" onclick="fetchData()" title="Refresh">↻</button>
  <button class="ctrl-btn" id="btnTheme" onclick="toggleTheme()" title="Toggle theme">☀</button>
  <button class="ctrl-btn" id="btnLang" onclick="toggleLang()" title="Toggle language">EN</button>
</div>

<!-- ═══ Summary Cards ═══ -->
<div class="cards-row">
  <div class="stat-card">
    <div class="lbl">Today</div>
    <div class="val" id="cToday">—</div>
    <div class="sub" id="cTodaySub">—</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Total</div>
    <div class="val" id="cTotal">—</div>
    <div class="sub" id="cTotalSub">—</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Daily Avg</div>
    <div class="val" id="cAvg">—</div>
    <div class="sub" id="cAvgSub">—</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Peak</div>
    <div class="val" id="cPeak">—</div>
    <div class="sub" id="cPeakSub">—</div>
  </div>
  <div class="stat-card">
    <div class="lbl" id="cUtilLbl">Messages</div>
    <div class="val" id="cUtil">—</div>
    <div class="sub" id="cUtilSub">—</div>
  </div>
</div>

<!-- ═══ Chart Row: Trend + Breakdown ═══ -->
<div class="chart-row2">
  <div class="chart-card">
    <h3>TREND</h3>
    <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>BREAKDOWN</h3>
    <div id="gvCache" style="font-size:24px;font-weight:700;color:var(--purple);text-align:center">—</div>
    <div style="font-size:11px;text-align:center;color:var(--text-muted)">Cache Ratio</div>
    <div id="gvReason" style="font-size:24px;font-weight:700;color:var(--amber);text-align:center;margin-top:8px">—</div>
    <div style="font-size:11px;text-align:center;color:var(--text-muted)">Reasoning Ratio</div>
  </div>
</div>

<!-- ═══ Chart Row: Leaderboard + Distribution ═══ -->
<div class="chart-row3">
  <div class="chart-card">
    <h3>LEADERBOARD</h3>
    <div class="chart-wrap" style="height:260px"><canvas id="modelChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>DISTRIBUTION</h3>
    <div class="chart-wrap" style="height:260px"><canvas id="provChart"></canvas></div>
  </div>
</div>

<!-- ═══ Cache Hit Rate Trend ═══ -->
<div class="chart-card" style="margin-bottom:14px">
  <h3>PERFORMANCE</h3>
  <div class="chart-wrap" style="height:260px"><canvas id="chTrendChart"></canvas></div>
</div>

<footer>Hermes Token Dashboard · v2.3 · UTC+8</footer>
</div>

<script>
// ═══════════════════════════════════════════════
// Global State
// ═══════════════════════════════════════════════
var lang = 'zh';
var metric = 'total';
var currentRange = 30;
var lastData = null;
var soloCacheKey = null;

// Chart instances
var trendChart, provChart, modelChart, chTrendChart;

// ═══════════════════════════════════════════════
// i18n Dictionaries
// ═══════════════════════════════════════════════
var L = {
  zh: { d: '天', all: '全部', per_day: '/天', msg: '消息', tot: '总量' },
  en: { d: 'd', all: 'All', per_day: '/day', msg: 'Messages', tot: 'Total' }
};

// ═══════════════════════════════════════════════
// i18n Helpers
// ═══════════════════════════════════════════════
function t(k) { return (L[lang] || L.zh)[k] || k; }

function setLang(l) {
  lang = l;
  document.getElementById('btnLang').textContent = (l === 'zh') ? 'EN' : '中文';
  try { localStorage.setItem('dash-lang', l); } catch (e) {}
  if (lastData) updateUI(lastData);
}

function toggleLang() {
  setLang(lang === 'zh' ? 'en' : 'zh');
}

// ═══════════════════════════════════════════════
// Theme Helpers
// ═══════════════════════════════════════════════
function setTheme(dark) {
  document.documentElement.classList.toggle('dark', dark);
  document.getElementById('btnTheme').textContent = dark ? '☾' : '☀';
  try { localStorage.setItem('dash-theme', dark ? 'dark' : 'light'); } catch (e) {}
}

function toggleTheme() {
  setTheme(!document.documentElement.classList.contains('dark'));
}

// ═══════════════════════════════════════════════
// Format Helpers
// ═══════════════════════════════════════════════
function fm(v) {
  if (v == null || isNaN(v)) return '—';
  return v.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function fs(v) {
  if (v == null || isNaN(v)) return '—';
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return v.toLocaleString();
}

function fc(v) {
  if (v == null) return '—';
  return '$' + v.toFixed(2);
}

function sm(n) {
  if (!n) return '?';
  var s = String(n);
  if (s.includes('/')) s = s.split('/').pop();
  s = s.replace(/-preview|-a3b|-a12b|:free|:beta/g, '');
  if (s.length > 18) s = s.slice(0, 16) + '…';
  return s;
}

// ═══════════════════════════════════════════════
// Metric Value Extraction
// ═══════════════════════════════════════════════
function getVal(entry, m) {
  if (m === 'cache_hit_rate') return entry.cache_hit_rate || 0;
  if (m === 'user_message_count') return entry.user_message_count || 0;
  if (m === 'runtime_dedup') return entry.runtime_dedup || 0;
  if (m === 'estimated_cost') return entry.estimated_cost || 0;
  return entry[m] || 0;
}

function fv(v, m) {
  if (m === 'cache_hit_rate') return v.toFixed(1) + '%';
  if (m === 'estimated_cost') return fc(v);
  if (m === 'runtime_dedup') {
    var h = Math.floor(v / 3600);
    var mi = Math.floor((v % 3600) / 60);
    return h + 'h ' + mi + 'm';
  }
  return fs(v);
}

function ml(m) {
  if (m === 'estimated_cost') return 'Cost';
  return m.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

// ═══════════════════════════════════════════════
// Cache Hit Trend Colors
// ═══════════════════════════════════════════════
var PM_COLORS = [
  '#3b82f6', '#10b981', '#8b5cf6', '#f59e0b',
  '#f43f5e', '#06b6d4', '#ec4899', '#6366f1',
  '#14b8a6', '#eab308', '#ef4444', '#84cc16'
];

// ═══════════════════════════════════════════════
// Metric-Aware Card Helpers (v2.2.3)
// ═══════════════════════════════════════════════

function getTodayStr() {
  var n = new Date();
  return n.getFullYear() + '-'
    + String(n.getMonth() + 1).padStart(2, '0') + '-'
    + String(n.getDate()).padStart(2, '0');
}

function getTodayValue(days, metric) {
  var today = getTodayStr();
  var total = 0, is = 0, cs = 0, found = false;
  for (var i = 0; i < days.length; i++) {
    var d = days[i];
    if (d.date.slice(0, 10) === today) {
      found = true;
      if (metric === 'cache_hit_rate') {
        is += d.input || 0;
        cs += d.cache_read || 0;
      } else {
        total += getVal(d, metric);
      }
    }
  }
  if (metric === 'cache_hit_rate') {
    return found && (is + cs) > 0 ? cs / (is + cs) * 100 : null;
  }
  return found ? total : null;
}

function getLatestValue(days, metric) {
  for (var i = days.length - 1; i >= 0; i--) {
    var d = days[i];
    var v = getVal(d, metric);
    if (v > 0 || (metric === 'cache_hit_rate' && (d.input + d.cache_read) > 0)) {
      if (metric === 'cache_hit_rate') {
        var cd = d.input + d.cache_read;
        return cd > 0 ? d.cache_read / cd * 100 : 0;
      }
      return v;
    }
  }
  return 0;
}

function groupByDay(days) {
  var groups = {};
  for (var i = 0; i < days.length; i++) {
    var dk = days[i].date.slice(0, 10);
    if (!groups[dk]) groups[dk] = [];
    groups[dk].push(days[i]);
  }
  return groups;
}

function calcAvg(days, metric) {
  if (days.length === 0) return 0;

  // cache_hit_rate: use total formula, not simple average
  if (metric === 'cache_hit_rate') {
    var ti = 0, tc = 0;
    for (var i = 0; i < days.length; i++) {
      ti += days[i].input || 0;
      tc += days[i].cache_read || 0;
    }
    return (ti + tc) > 0 ? tc / (ti + tc) * 100 : 0;
  }

  var is7 = currentRange === 7;
  if (is7) {
    // 7-day hourly: group by day, then average
    var groups = groupByDay(days);
    var sum = 0, count = 0;
    for (var dk in groups) {
      var g = groups[dk];
      var daySum = 0;
      for (var j = 0; j < g.length; j++) daySum += getVal(g[j], metric);
      sum += daySum;
      count++;
    }
    return count > 0 ? sum / count : 0;
  }

  // Daily: simple average
  var total = 0;
  for (var i = 0; i < days.length; i++) total += getVal(days[i], metric);
  return total / days.length;
}

function findPeak(days, metric) {
  if (days.length === 0) return { value: 0, date: '—' };

  var best = days[0];
  var bestVal = getVal(best, metric);

  for (var i = 1; i < days.length; i++) {
    var v = getVal(days[i], metric);
    if (v > bestVal) { bestVal = v; best = days[i]; }
  }

  if (metric === 'cache_hit_rate') {
    var cd = best.input + best.cache_read;
    bestVal = cd > 0 ? best.cache_read / cd * 100 : 0;
  }

  return { value: bestVal, date: best.date };
}

// ═══════════════════════════════════════════════
// Cache Hit Trend Solo Mode
// ═══════════════════════════════════════════════

function setCacheSolo(key) {
  if (soloCacheKey === key) {
    soloCacheKey = null;  // exit solo
  } else {
    soloCacheKey = key;   // enter solo
  }
  chTrendChart.data.datasets.forEach(function(ds, i) {
    if (soloCacheKey === null) {
      ds.hidden = false;
    } else {
      ds.hidden = (i !== soloCacheKey);
    }
  });
  chTrendChart.update();
}

// ═══════════════════════════════════════════════
// Chart Initialization
// ═══════════════════════════════════════════════

function initCharts() {
  // Trend chart — line with dual Y-axis
  trendChart = new Chart(
    document.getElementById('trendChart').getContext('2d'),
    {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: '', data: [], borderColor: '#3b82f6', borderWidth: 2, yAxisID: 'y', tension: 0.3, pointRadius: 0 },
          { label: '', data: [], borderColor: '#10b981', borderDash: [5, 3], borderWidth: 1.5, yAxisID: 'y1', tension: 0.3, pointRadius: 0 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { grid: { display: false } },
          y: {},
          y1: { position: 'right', grid: { display: false } }
        },
        plugins: [
          { id: 'th', beforeInit: function(c) { c.canvas.parentNode.style.height = '220px'; } }
        ]
      }
    }
  );

  // Provider donut chart
  provChart = new Chart(
    document.getElementById('provChart').getContext('2d'),
    {
      type: 'doughnut',
      data: {
        labels: [],
        datasets: [{
          data: [],
          backgroundColor: ['#3b82f6', '#10b981', '#8b5cf6', '#f59e0b', '#f43f5e', '#06b6d4', '#94a3b8']
        }]
      },
      options: {
        cutout: '60%',
        plugins: { legend: { position: 'bottom', labels: { font: { size: 10 } } } }
      }
    }
  );

  // Model ranking — horizontal bar chart
  modelChart = new Chart(
    document.getElementById('modelChart').getContext('2d'),
    {
      type: 'bar',
      data: {
        labels: [],
        datasets: [{
          data: [],
          backgroundColor: function(ctx) { return ctx.dataIndex === 0 ? '#f59e0b' : '#3b82f6'; },
          borderRadius: 4
        }]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } }
      }
    }
  );

  // Cache hit rate trend — multi-line chart
  chTrendChart = new Chart(
    document.getElementById('chTrendChart').getContext('2d'),
    {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'bottom',
            labels: { font: { size: 9 }, usePointStyle: true, pointStyleWidth: 6 }
          }
        },
        scales: {
          x: { grid: { display: false } },
          y: {
            min: 0, max: 100,
            ticks: { callback: function(v) { return v + '%'; } }
          }
        }
      },
      plugins: [
        { id: 'cth', beforeInit: function(c) { c.canvas.parentNode.style.height = '260px'; } }
      ]
    }
  );
}

// ═══════════════════════════════════════════════
// Main UI Update
// ═══════════════════════════════════════════════

function updateUI(data) {
  lastData = data;
  if (data.error) return;

  var s = data.summary;
  var m = data.meta;
  var d = data.days || [];

  // Hero metadata
  document.getElementById('hMeta').textContent =
    'Range: ' + (m.range === 'all' ? t('all') : m.range + ' ' + t('d'))
    + ' · ' + m.first_day + ' — ' + m.last_day
    + ' · Sessions: ' + s.sessions
    + ' · Updated: ' + new Date().toLocaleTimeString()
    + ' · UTC+8';

  // --- Summary Cards (metric-aware from days) ---

  // Today / Latest
  var tv = getTodayValue(d, metric);
  var isLatest = !tv && tv !== 0;
  document.getElementById('cToday').textContent = fv(
    tv != null ? tv : getLatestValue(d, metric), metric
  );
  document.getElementById('cTodaySub').textContent =
    (isLatest || tv == null ? 'Latest ' : '') + ml(metric);

  // Total
  var totalV = 0;
  for (var i = 0; i < d.length; i++) totalV += getVal(d[i], metric);
  if (metric === 'cache_hit_rate') totalV = calcAvg(d, metric);
  document.getElementById('cTotal').textContent = fv(totalV, metric);
  document.getElementById('cTotalSub').textContent = 'Period ' + ml(metric);

  // Daily Avg
  var av = calcAvg(d, metric);
  document.getElementById('cAvg').textContent = fv(av, metric);
  document.getElementById('cAvgSub').textContent = t('per_day');

  // Peak
  var pk = findPeak(d, metric);
  document.getElementById('cPeak').textContent = fv(pk.value, metric);
  document.getElementById('cPeakSub').textContent = 'on ' + pk.date;

  // Utility card (Messages ↔ Total Tokens)
  var isMsg = (metric === 'user_message_count');
  document.getElementById('cUtilLbl').textContent = isMsg ? t('tot') : t('msg');
  document.getElementById('cUtil').textContent = isMsg ? fs(s.total) : fm(s.user_message_count);
  document.getElementById('cUtilSub').textContent = isMsg
    ? 'Period sum'
    : fm(s.messages_per_day) + ' ' + t('per_day');

  // --- Trend Chart ---
  var is7 = currentRange === 7;
  trendChart.data.labels = d.map(function(x) {
    return is7
      ? x.date.slice(5, 10) + ' ' + x.date.slice(11, 16)
      : x.date.slice(5);
  });
  trendChart.data.datasets[0].data = d.map(function(x) { return getVal(x, metric); });
  trendChart.data.datasets[0].label = ml(metric);
  trendChart.data.datasets[1].data = d.map(function(x) {
    return metric === 'user_message_count' ? x.total : x.user_message_count;
  });
  trendChart.data.datasets[1].label = metric === 'user_message_count' ? t('tot') : t('msg');
  trendChart.update();

  // --- Breakdown Gauges ---
  var chDenom = s.input + s.cache_read;
  document.getElementById('gvCache').textContent =
    (chDenom > 0 ? (s.cache_read / chDenom * 100).toFixed(1) : '0.0') + '%';

  var outDenom = s.output + s.reasoning;
  document.getElementById('gvReason').textContent =
    (outDenom > 0 ? (s.reasoning / outDenom * 100).toFixed(1) : '0.0') + '%';

  // --- Provider Distribution (sorted by current metric) ---
  var pv = (data.providers || []).slice();
  pv.sort(function(a, b) { return getVal(b, metric) - getVal(a, metric); });
  var topP = pv.slice(0, 6);
  var provOthers = 0;
  pv.slice(6).forEach(function(p) { provOthers += getVal(p, metric); });

  provChart.data.labels = topP.map(function(p) { return p.name; }).concat(provOthers > 0 ? ['Other'] : []);
  provChart.data.datasets[0].data = topP.map(function(p) { return getVal(p, metric); }).concat(provOthers > 0 ? [provOthers] : []);
  provChart.update();

  // --- Model Ranking (sorted by current metric) ---
  var md = (data.models || []).slice();
  md.sort(function(a, b) { return getVal(b, metric) - getVal(a, metric); });
  var topM = md.slice(0, 8);
  modelChart.data.labels = topM.map(function(m) { return sm(m.name); });
  modelChart.data.datasets[0].data = topM.map(function(m) { return getVal(m, metric); });
  modelChart.update();

  // --- Cache Hit Rate Trend (solo mode + enhanced tooltip) ---
  var pmts = data.provider_model_trends || [];
  if (pmts.length > 0) {
    chTrendChart.data.labels = pmts[0].days.map(function(d) { return d.date.slice(5); });
  }

  soloCacheKey = null;

  chTrendChart.data.datasets = pmts.map(function(pmt, i) {
    return {
      label: sm(pmt.provider) + '/' + sm(pmt.model),
      data: pmt.days.map(function(d) { return d.cache_hit_rate || 0; }),
      borderColor: PM_COLORS[i % 12],
      backgroundColor: PM_COLORS[i % 12],
      tension: 0.3,
      pointRadius: 0,
      borderWidth: 2,
      inputData: pmt.days.map(function(d) { return d.input || 0; }),
      cacheReadData: pmt.days.map(function(d) { return d.cache_read || 0; }),
      cacheWriteData: pmt.days.map(function(d) { return d.cache_write || 0; })
    };
  });

  // Solo mode legend handler
  chTrendChart.options.plugins.legend.onClick = function(e, item, legend) {
    if (!item || item.datasetIndex === undefined) return;
    setCacheSolo(item.datasetIndex);
  };

  // Enhanced tooltip
  chTrendChart.options.plugins.tooltip = {
    callbacks: {
      title: function(items) { return items[0] ? items[0].label : ''; },
      label: function(ctx) {
        var ds = ctx.dataset;
        var idx = ctx.dataIndex;
        return [
          'Cache Hit: ' + ctx.parsed.y.toFixed(1) + '%',
          'Input: ' + fs(ds.inputData ? ds.inputData[idx] : 0),
          'Cache Read: ' + fs(ds.cacheReadData ? ds.cacheReadData[idx] : 0),
          'Cache Write: ' + fs(ds.cacheWriteData ? ds.cacheWriteData[idx] : 0)
        ];
      }
    }
  };

  chTrendChart.update();
}

// ═══════════════════════════════════════════════
// Data Fetching
// ═══════════════════════════════════════════════

async function fetchData() {
  try {
    var rp = (currentRange === null) ? 'all' : currentRange;
    var r = await fetch('/api/usage?range=' + rp);
    if (r.ok) updateUI(await r.json());
  } catch (e) {}
}

// ═══════════════════════════════════════════════
// Boot — restore state, bind events, start
// ═══════════════════════════════════════════════

(function() {
  // Restore language
  try { var sl = localStorage.getItem('dash-lang'); if (sl) lang = sl; } catch (e) {}
  setLang(lang);

  // Restore theme
  try {
    var st = localStorage.getItem('dash-theme');
    if (st === 'dark') setTheme(true);
  } catch (e) {}

  // Time range button listeners
  document.querySelectorAll('.ctrl-btn[data-range]').forEach(function(b) {
    b.addEventListener('click', function() {
      document.querySelectorAll('.ctrl-btn[data-range]').forEach(function(x) {
        x.classList.remove('active');
      });
      b.classList.add('active');
      currentRange = b.dataset.range === 'all' ? null : parseInt(b.dataset.range, 10);
      fetchData();
    });
  });

  // Metric selector listener
  document.getElementById('metricSelect').addEventListener('change', function() {
    metric = this.value;
    if (lastData) updateUI(lastData);
  });

  // Initialize and start
  initCharts();
  fetchData();
  setInterval(fetchData, 4000);
})();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# 8. HTTP Server
# ═══════════════════════════════════════════════════════════════

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard."""

    def log_message(self, format, *args):
        """Suppress default access log."""
        pass

    def _send_json(self, data, status=200):
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        """Send the main HTML page."""
        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self):
        """Send Server-Sent Events stream for live updates."""
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
                sig = _db_sig()
                current_sig = _api_cache.get("range_sse", {}).get("sig", "")
                if sig != current_sig:
                    with _api_cache_lock:
                        _api_cache["range_sse"] = {"sig": sig, "data": None}
                    self.wfile.write("data: {\"updated\": true}\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        """Route incoming GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/":
            self._send_html()

        elif path == "/health":
            self._send_json({"ok": True})

        elif path == "/api/usage":
            # Parse range parameter (compatible with all, null, empty, missing)
            range_val = params.get("range", [None])[0]
            range_days = None
            if range_val and range_val not in ("all", "null", ""):
                try:
                    range_days = int(range_val)
                except (ValueError, TypeError):
                    pass
            try:
                stats = cached_stats(range_days)
                self._send_json(stats)
            except DatabaseError as e:
                self._send_json({
                    "error": "database_not_found",
                    "message": str(e),
                }, 500)
            except Exception as e:
                self._send_json({
                    "error": "internal_error",
                    "message": str(e),
                }, 500)

        elif path == "/api/events":
            try:
                self._send_sse()
            except Exception:
                pass

        else:
            self._send_json({"error": "not_found", "message": "Unknown path"}, 404)


# ═══════════════════════════════════════════════════════════════
# 9. Entry Point
# ═══════════════════════════════════════════════════════════════

def main():
    """Start the dashboard HTTP server."""
    print("  Hermes Token Dashboard v2.3")
    print(f"  Data source: {DB_PATH}")
    print(f"  Timezone:   UTC+8")
    print(f"  Listening:  http://{HOST}:{PORT}")
    print(f"  Press Ctrl+C to stop")
    print()
    server = http.server.ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
