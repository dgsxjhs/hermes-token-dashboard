"""
Tests for Hermes Token Dashboard v2.1
Usage: python3 -m unittest discover -s tests
"""

import unittest
import sys
import os
import importlib.util

# 用 importlib 导入带连字符的文件名
_dash_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "hermes-token-dashboard.py")
_spec = importlib.util.spec_from_file_location("dashboard", _dash_path)
_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dash)


class TestPricing(unittest.TestCase):
    """测试 get_pricing() — v2.1 免费模型优先检测。"""

    def setUp(self):
        self.dash = _dash

    def test_free_keyword_detected_first(self):
        """mimo-v2.5:free 应优先匹配免费关键词，而非付费 mimo-v2.5。"""
        pricing, key = self.dash.get_pricing("minimax/mimo-v2.5:free")
        self.assertEqual(key, "free")
        self.assertEqual(pricing["input"], 0)
        self.assertEqual(pricing["output"], 0)

    def test_free_nemotron(self):
        pricing, key = self.dash.get_pricing("nvidia/nemotron-3-super-120b-a12b:free")
        self.assertEqual(key, "free")
        self.assertEqual(pricing["input"], 0)

    def test_free_gemma(self):
        pricing, key = self.dash.get_pricing("google/gemma-4-31b-it:free")
        self.assertEqual(key, "free")

    def test_free_hy3(self):
        pricing, key = self.dash.get_pricing("tencent/hy3-preview:free")
        self.assertEqual(key, "free")

    def test_paid_deepseek(self):
        pricing, key = self.dash.get_pricing("deepseek-v4-flash")
        self.assertEqual(key, "deepseek-v4-flash")
        self.assertEqual(pricing["input"], 1)
        self.assertEqual(pricing["output"], 2)
        self.assertGreater(pricing["cache_read"], 0)

    def test_paid_match_with_provider_prefix(self):
        """带 org/ 前缀的付费模型应正确匹配。"""
        pricing, key = self.dash.get_pricing("deepseek/deepseek-v4-pro")
        self.assertEqual(key, "deepseek-v4-pro")
        self.assertGreater(pricing["input"], 0)

    def test_exact_free_entry(self):
        """mimo-v2.5-free 在 MODEL_PRICING 中有精确条目。"""
        pricing, key = self.dash.get_pricing("mimo-v2.5-free")
        self.assertEqual(key, "mimo-v2.5-free")
        self.assertEqual(pricing["input"], 0)

    def test_unknown_model_gets_default(self):
        pricing, key = self.dash.get_pricing("some-nonexistent-model-v99")
        self.assertEqual(key, "default")
        self.assertGreater(pricing["input"], 0)

    def test_none_model(self):
        pricing, key = self.dash.get_pricing(None)
        self.assertEqual(key, "default")


class TestModelNormalization(unittest.TestCase):
    """测试 normalize_model()。"""

    def setUp(self):
        self.dash = _dash

    def test_org_prefix_stripped(self):
        self.assertEqual(
            self.dash.normalize_model("deepseek/deepseek-v4-flash"),
            "deepseek-v4-flash"
        )

    def test_at_provider_stripped(self):
        self.assertEqual(
            self.dash.normalize_model("@opencode-go:deepseek-v4-pro"),
            "deepseek-v4-pro"
        )

    def test_already_normal(self):
        self.assertEqual(
            self.dash.normalize_model("gpt-5.4"),
            "gpt-5.4"
        )

    def test_none_returns_unknown(self):
        self.assertEqual(self.dash.normalize_model(None), "unknown")


class TestCacheHitFormula(unittest.TestCase):
    """测试 v2.1 修正后的缓存命中率公式。"""

    def setUp(self):
        self.dash = _dash

    def test_cache_hit_calculation(self):
        """cache_hit = cache_read / (input + cache_read)"""
        sessions = [{
            "id": "test-1",
            "model": "deepseek-v4-flash",
            "billing_provider": "test",
            "started_at": 1700000000.0,
            "ended_at": 1700000100.0,
            "source": "test",
            "input_tokens": 100000,
            "output_tokens": 10000,
            "cache_read_tokens": 900000,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "message_count": 10,
            "tool_call_count": 5,
            "api_call_count": 5,
        }]
        stats = self.dash.aggregate_stats(sessions)
        # cache_hit_pct = 900000 / (100000 + 900000) = 90.0%
        self.assertAlmostEqual(stats["summary"]["cache_hit_pct"], 90.0, places=0)
        # 旧公式会是 cache_read / total = 900000/1010000 ≈ 89.1% — 不应等于这个
        old_formula = 900000 / (100000 + 10000 + 900000) * 100
        self.assertNotAlmostEqual(stats["summary"]["cache_hit_pct"], round(old_formula, 1), places=0)

    def test_zero_denom(self):
        """分母为 0 时命中率应为 0。"""
        sessions = [{
            "id": "test-0",
            "model": "some-model",
            "billing_provider": "test",
            "started_at": 1700000000.0,
            "ended_at": 1700000100.0,
            "source": "test",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "message_count": 0,
            "tool_call_count": 0,
            "api_call_count": 0,
        }]
        stats = self.dash.aggregate_stats(sessions)
        self.assertEqual(stats["summary"]["cache_hit_pct"], 0.0)


class TestDBSignature(unittest.TestCase):
    """测试 WAL 感知的 _db_signature()。"""

    def setUp(self):
        self.dash = _dash

    @unittest.skipUnless(
        os.path.exists(os.path.expanduser("~/.hermes/state.db")),
        "state.db not available — skip signature test"
    )
    def test_signature_not_empty(self):
        sig = self.dash._db_signature()
        self.assertIsInstance(sig, str)
        self.assertGreater(len(sig), 0)
        # 应包含分隔符 "|"
        self.assertIn("|", sig)

    def test_file_mtime_size_missing(self):
        """不存在的文件返回空字符串。"""
        result = self.dash._file_mtime_size("/nonexistent/path/db")
        self.assertEqual(result, "")


class TestAggregateStats(unittest.TestCase):
    """测试 aggregate_stats() 基本功能。"""

    def setUp(self):
        self.dash = _dash

    def test_empty_sessions(self):
        stats = self.dash.aggregate_stats([])
        self.assertEqual(stats["summary"]["total"], 0)
        self.assertEqual(stats["summary"]["sessions"], 0)
        self.assertEqual(len(stats["model_ranking"]), 0)

    def test_basic_aggregation(self):
        sessions = [{
            "id": "test-a",
            "model": "gpt-5.4",
            "billing_provider": "openai",
            "started_at": 1700000000.0,
            "ended_at": 1700003600.0,
            "source": "webui",
            "input_tokens": 50000,
            "output_tokens": 10000,
            "cache_read_tokens": 200000,
            "cache_write_tokens": 0,
            "reasoning_tokens": 5000,
            "message_count": 20,
            "tool_call_count": 10,
            "api_call_count": 10,
        }]
        stats = self.dash.aggregate_stats(sessions)
        self.assertEqual(stats["summary"]["input"], 50000)
        self.assertEqual(stats["summary"]["output"], 10000)
        self.assertEqual(stats["summary"]["sessions"], 1)
        self.assertEqual(len(stats["model_ranking"]), 1)
        self.assertGreater(stats["summary"]["estimated_cost"], 0)

    def test_range_filter(self):
        """range_days 应过滤掉过旧的 session。"""
        import time
        now = time.time()  # 用当前真实时间
        sessions = [
            {"id": "old", "model": "x", "billing_provider": "p",
             "started_at": now - 86400 * 10, "ended_at": now - 86400 * 10 + 100,
             "source": "test",
             "input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0,
             "message_count": 1, "tool_call_count": 0, "api_call_count": 1},
            {"id": "new", "model": "y", "billing_provider": "p",
             "started_at": now - 3600, "ended_at": now,
             "source": "test",
             "input_tokens": 2000, "output_tokens": 200, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0,
             "message_count": 1, "tool_call_count": 0, "api_call_count": 1},
        ]
        stats = _dash.aggregate_stats(sessions, range_days=3)
        self.assertEqual(stats["summary"]["sessions"], 1)
        model_names = [m["model"] for m in stats["model_ranking"]]
        self.assertIn("y", model_names)
        self.assertNotIn("x", model_names)


if __name__ == "__main__":
    unittest.main()
