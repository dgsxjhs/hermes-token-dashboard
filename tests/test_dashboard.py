"""
Tests for Hermes Token Dashboard v2.2
Usage: python3 -m unittest discover -s tests
"""

import unittest
import os
import importlib.util

# Import dashboard module with hyphenated filename
_dash_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "hermes-token-dashboard.py")
_spec = importlib.util.spec_from_file_location("dashboard", _dash_path)
_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dash)


class TestPricing(unittest.TestCase):
    """v2.1 pricing — free models detected first."""

    def setUp(self): self.dash = _dash

    def test_free_keyword_first(self):
        p, k = self.dash.get_pricing("minimax/mimo-v2.5:free")
        self.assertEqual(k, "free"); self.assertEqual(p["input"], 0)

    def test_free_nemotron(self):
        p, k = self.dash.get_pricing("nvidia/nemotron-3-super-120b-a12b:free")
        self.assertEqual(k, "free")

    def test_paid_deepseek(self):
        p, k = self.dash.get_pricing("deepseek-v4-flash")
        self.assertEqual(k, "deepseek-v4-flash"); self.assertGreater(p["input"], 0)

    def test_paid_with_prefix(self):
        p, k = self.dash.get_pricing("deepseek/deepseek-v4-pro")
        self.assertEqual(k, "deepseek-v4-pro")

    def test_unknown_default(self):
        p, k = self.dash.get_pricing("xyz-nonexistent")
        self.assertEqual(k, "default")


class TestNormalization(unittest.TestCase):
    def setUp(self): self.dash = _dash

    def test_org_prefix(self):
        self.assertEqual(self.dash.normalize_model("a/b"), "b")

    def test_at_prefix(self):
        self.assertEqual(self.dash.normalize_model("@p:x"), "x")

    def test_none(self):
        self.assertEqual(self.dash.normalize_model(None), "unknown")


class TestV22DataStructures(unittest.TestCase):
    """v2.2: verify new API fields exist with correct structure."""

    def setUp(self): self.dash = _dash

    def _make_session(self, model="gpt-5.4", provider="openai", inp=1000, out=100, cr=5000, cw=0, rt=50, msg=5, api=3):
        return {
            "id": "test-1", "model": model, "billing_provider": provider,
            "started_at": 1700000000.0, "ended_at": 1700000100.0, "source": "test",
            "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cr,
            "cache_write_tokens": cw, "reasoning_tokens": rt,
            "message_count": msg, "tool_call_count": 0, "api_call_count": api,
        }

    def test_active_formula(self):
        """active = input + output + reasoning"""
        s = [self._make_session()]
        stats = self.dash.aggregate_stats(s)
        self.assertEqual(stats["summary"]["active"], 1000 + 100 + 50)

    def test_cache_hit_rate_formula(self):
        """cache_hit_rate = cache_read / (input + cache_read)"""
        s = [self._make_session(inp=1000, cr=4000)]
        stats = self.dash.aggregate_stats(s)
        expected = round(4000 / 5000 * 100, 2)
        self.assertEqual(stats["summary"]["cache_hit_rate"], expected)

    def test_days_field_exists(self):
        s = [self._make_session()]
        stats = self.dash.aggregate_stats(s)
        self.assertIn("days", stats)
        self.assertGreater(len(stats["days"]), 0)

    def test_7day_hourly(self):
        """7 days should return hourly days entries."""
        import time
        now = time.time()
        sessions = []
        for i in range(24):
            sessions.append({
                "id": f"h-{i}", "model": "x", "billing_provider": "p",
                "started_at": now - i * 3600, "ended_at": now - i * 3600 + 100,
                "source": "t",
                "input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "reasoning_tokens": 0,
                "message_count": 0, "tool_call_count": 0, "api_call_count": 1,
            })
        stats = self.dash.aggregate_stats(sessions, range_days=7)
        self.assertGreater(len(stats["days"]), 0)
        # Hourly entries have "T" in the date key
        self.assertIn("T", stats["days"][0]["date"])

    def test_30day_daily(self):
        """30 days should return daily entries (no T in date)."""
        s = [self._make_session()]
        stats = self.dash.aggregate_stats(s, range_days=30)
        if stats["days"]:
            self.assertNotIn("T", stats["days"][0]["date"])

    def test_models_aggregation(self):
        """models should have name, total, active, sessions, estimated_cost."""
        s = [self._make_session(model="gpt-5.4")]
        stats = self.dash.aggregate_stats(s)
        self.assertGreater(len(stats["models"]), 0)
        m = stats["models"][0]
        self.assertEqual(m["name"], "gpt-5.4")
        self.assertGreater(m["total"], 0)
        self.assertIn("sessions", m)
        self.assertIn("estimated_cost", m)

    def test_models_sort_by_total(self):
        s = [self._make_session(model="a", inp=100), self._make_session(model="b", inp=10000)]
        stats = self.dash.aggregate_stats(s)
        self.assertEqual(stats["models"][0]["name"], "b")

    def test_providers_aggregation(self):
        s = [self._make_session(provider="xyz-prov")]
        stats = self.dash.aggregate_stats(s)
        prov_names = [p["name"] for p in stats["providers"]]
        self.assertIn("xyz-prov", prov_names)

    def test_provider_model_trends_exists(self):
        s = [self._make_session()]
        stats = self.dash.aggregate_stats(s)
        self.assertIn("provider_model_trends", stats)
        self.assertIsInstance(stats["provider_model_trends"], list)

    def test_free_model_cost_zero(self):
        s = [self._make_session(model="nvidia/nemotron-3-super-120b-a12b:free")]
        stats = self.dash.aggregate_stats(s)
        if stats["models"]:
            m = stats["models"][0]
            self.assertLess(abs(m["estimated_cost"]), 0.01)

    def test_meta_fields(self):
        s = [self._make_session()]
        stats = self.dash.aggregate_stats(s)
        m = stats["meta"]
        self.assertIn("timezone", m)
        self.assertEqual(m["timezone"], "UTC+8")
        self.assertIn("generated_at", m)
        self.assertIn("scanned_rows", m)


class TestNoMorePycTracking(unittest.TestCase):
    """Verify .pyc / __pycache__ are gitignored."""

    def test_gitignore_exists(self):
        gitignore = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".gitignore")
        self.assertTrue(os.path.exists(gitignore))
        content = open(gitignore).read()
        self.assertIn("__pycache__", content)
        self.assertIn("*.pyc", content)


if __name__ == "__main__":
    unittest.main()
