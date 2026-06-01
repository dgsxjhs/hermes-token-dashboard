"""Tests for Hermes Token Dashboard v2.2.4
Usage: python3 -m unittest discover -s tests"""

import unittest
import os
import importlib.util

_dash_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "hermes-token-dashboard.py")
_spec = importlib.util.spec_from_file_location("dashboard", _dash_path)
_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dash)


class TestPricing(unittest.TestCase):
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
    def test_org_prefix(self): self.assertEqual(self.dash.normalize_model("a/b"), "b")
    def test_at_prefix(self): self.assertEqual(self.dash.normalize_model("@p:x"), "x")
    def test_none(self): self.assertEqual(self.dash.normalize_model(None), "unknown")


class TestV22DataStructures(unittest.TestCase):
    def setUp(self): self.dash = _dash
    def _mk(self, model="gpt-5.4", provider="openai", inp=1000, out=100, cr=5000, cw=0, rt=50, msg=5, api=3):
        return {"id":"t","model":model,"billing_provider":provider,"started_at":1700000000.0,"ended_at":1700000100.0,"source":"t","input_tokens":inp,"output_tokens":out,"cache_read_tokens":cr,"cache_write_tokens":cw,"reasoning_tokens":rt,"message_count":msg,"tool_call_count":0,"api_call_count":api}
    def test_active_formula(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s)
        self.assertEqual(st["summary"]["active"],1000+100+50)
    def test_cache_hit_rate_formula(self):
        s=[self._mk(inp=1000,cr=4000)]; st=self.dash.aggregate_stats(s)
        self.assertEqual(st["summary"]["cache_hit_rate"],round(4000/5000*100,2))
    def test_days_field_exists(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s)
        self.assertIn("days",st); self.assertGreater(len(st["days"]),0)
    def test_7day_hourly(self):
        import time; now=time.time()
        sessions=[{"id":f"h-{i}","model":"x","billing_provider":"p","started_at":now-i*3600,"ended_at":now-i*3600+100,"source":"t","input_tokens":100,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"message_count":0,"tool_call_count":0,"api_call_count":1} for i in range(24)]
        st=self.dash.aggregate_stats(sessions,range_days=7)
        self.assertGreater(len(st["days"]),0); self.assertIn("T",st["days"][0]["date"])
    def test_30day_daily(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s,range_days=30)
        if st["days"]: self.assertNotIn("T",st["days"][0]["date"])
    def test_models_aggregation(self):
        s=[self._mk(model="gpt-5.4")]; st=self.dash.aggregate_stats(s)
        self.assertGreater(len(st["models"]),0); m=st["models"][0]
        self.assertEqual(m["name"],"gpt-5.4"); self.assertGreater(m["total"],0)
    def test_models_sort_by_total(self):
        s=[self._mk(model="a",inp=100),self._mk(model="b",inp=10000)]
        st=self.dash.aggregate_stats(s); self.assertEqual(st["models"][0]["name"],"b")
    def test_providers_aggregation(self):
        s=[self._mk(provider="xyz")]; st=self.dash.aggregate_stats(s)
        self.assertIn("xyz",[p["name"] for p in st["providers"]])
    def test_provider_model_trends_exists(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s)
        self.assertIn("provider_model_trends",st)
    def test_free_model_cost_zero(self):
        s=[self._mk(model="nvidia/nemotron-3-super-120b-a12b:free")]
        st=self.dash.aggregate_stats(s)
        if st["models"]: self.assertLess(abs(st["models"][0]["estimated_cost"]),0.01)
    def test_meta_fields(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s); m=st["meta"]
        self.assertEqual(m["timezone"],"UTC+8"); self.assertIn("generated_at",m)


class TestNoMorePycTracking(unittest.TestCase):
    def test_gitignore(self):
        gi=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),".gitignore")
        self.assertTrue(os.path.exists(gi)); c=open(gi).read(); open(gi).close()
        self.assertIn("__pycache__",c); self.assertIn("*.pyc",c)


class TestV223Fields(unittest.TestCase):
    """v2.2.3/2.2.4: API field completeness and correctness."""
    def setUp(self): self.dash = _dash
    def _mk(self,model="gpt-5.4",provider="openai",inp=1000,out=100,cr=5000,cw=0,rt=50,msg=5,api=3):
        return {"id":"t","model":model,"billing_provider":provider,"started_at":1700000000.0,"ended_at":1700000100.0,"source":"t","input_tokens":inp,"output_tokens":out,"cache_read_tokens":cr,"cache_write_tokens":cw,"reasoning_tokens":rt,"message_count":msg,"tool_call_count":0,"api_call_count":api}

    def test_summary_fields(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s); su=st["summary"]
        for f in ["total","active","input","output","reasoning","cache_read","cache_write","cache_hit_rate","runtime_dedup","user_message_count","estimated_cost"]:
            self.assertIn(f,su,f"summary missing: {f}")

    def test_days_entry_fields(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s); d=st["days"][0]
        for f in ["date","total","active","input","output","reasoning","cache_read","cache_write","cache_hit_rate","runtime_dedup","user_message_count","estimated_cost"]:
            self.assertIn(f,d,f"days missing: {f}")

    def test_models_entry_fields(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s); m=st["models"][0]
        for f in ["name","total","active","input","output","reasoning","cache_read","cache_write","cache_hit_rate","runtime_dedup","user_message_count","estimated_cost","sessions","api_calls"]:
            self.assertIn(f,m,f"models missing: {f}")

    def test_providers_entry_fields(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s); p=st["providers"][0]
        for f in ["name","total","active","input","output","reasoning","cache_read","cache_write","cache_hit_rate","estimated_cost"]:
            self.assertIn(f,p,f"providers missing: {f}")

    def test_pm_trends_structure(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s); pmt=st["provider_model_trends"]
        self.assertIsInstance(pmt,list)
        if pmt:
            e=pmt[0]; self.assertIn("provider",e); self.assertIn("model",e); self.assertIn("days",e)
            d0=e["days"][0]
            for f in ["date","input","cache_read","cache_write","cache_hit_rate","total"]:
                self.assertIn(f,d0,f"pm_trends day missing: {f}")

    def test_7day_hourly_format(self):
        import time; now=time.time()
        sessions=[{"id":f"h-{i}","model":"x","billing_provider":"p","started_at":now-i*3600,"ended_at":now-i*3600+100,"source":"t","input_tokens":100,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"message_count":0,"tool_call_count":0,"api_call_count":1} for i in range(24)]
        st=self.dash.aggregate_stats(sessions,range_days=7)
        self.assertGreater(len(st["days"]),0); self.assertIn("T",st["days"][0]["date"])

    def test_30day_daily_format(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s,range_days=30)
        if st["days"]: self.assertNotIn("T",st["days"][0]["date"])

    def test_cache_hit_rate_formula_v2(self):
        s=[self._mk(inp=1000,cr=4000)]; st=self.dash.aggregate_stats(s)
        self.assertAlmostEqual(st["days"][0]["cache_hit_rate"],4000/5000*100,places=0)

    def test_estimated_cost_in_all(self):
        s=[self._mk()]; st=self.dash.aggregate_stats(s)
        self.assertIn("estimated_cost",st["summary"])
        self.assertIn("estimated_cost",st["days"][0])
        self.assertIn("estimated_cost",st["models"][0])
        self.assertIn("estimated_cost",st["providers"][0])


if __name__=="__main__": unittest.main()
