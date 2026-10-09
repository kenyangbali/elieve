"""Unit test Gap 3 — akuntansi token/biaya (elieve/accounting.py).

Jalan tanpa 9router / API key / network (get_api_key & call_model di-mock
untuk integrasi loop; usage selalu palsu):
    cd ~/workspace/elieve && python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elieve.accounting import (  # noqa: E402
    Accounting,
    UsageTracker,
    estimate_cost_for,
)
import elieve.loop as LOOP  # noqa: E402

PRICES = {
    "ag/gemini-3-flash": {"input_per_1k": 0.0005, "output_per_1k": 0.002},
    "ag/claude-opus-4-6-thinking": {"input_per_1k": 0.015, "output_per_1k": 0.075},
}


def _tmpdir():
    return tempfile.mkdtemp(prefix="acct-test-")


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


class TestUsageTracker(unittest.TestCase):
    """Akumulasi multi-model + serialisasi."""

    def test_record_multi_model_accumulates(self):
        t = UsageTracker()
        t.record("ag/gemini-3-flash",
                 {"prompt_tokens": 1000, "completion_tokens": 200})
        t.record("ag/gemini-3-flash",
                 {"prompt_tokens": 500, "completion_tokens": 100,
                  "total_tokens": 600})
        t.record("ag/claude-opus-4-6-thinking",
                 {"prompt_tokens": 3000, "completion_tokens": 700})
        got = t.totals()
        flash = got["models"]["ag/gemini-3-flash"]
        self.assertEqual(flash["prompt_tokens"], 1500)
        self.assertEqual(flash["completion_tokens"], 300)
        self.assertEqual(flash["total_tokens"], 1800)  # 1200 + 600
        self.assertEqual(flash["calls"], 2)
        opus = got["models"]["ag/claude-opus-4-6-thinking"]
        self.assertEqual(opus["total_tokens"], 3700)
        self.assertEqual(got["grand"]["total_tokens"], 5500)
        self.assertEqual(got["grand"]["calls"], 3)

    def test_record_partial_usage_defaults_zero(self):
        t = UsageTracker()
        t.record("ag/x", {})
        t.record("ag/x", {"prompt_tokens": 10})  # completion hilang -> 0
        b = t.totals()["models"]["ag/x"]
        self.assertEqual(b["prompt_tokens"], 10)
        self.assertEqual(b["completion_tokens"], 0)
        self.assertEqual(b["total_tokens"], 10)

    def test_to_dict_json_serializable(self):
        t = UsageTracker()
        t.record("ag/x", {"prompt_tokens": 5, "completion_tokens": 5})
        s = json.dumps(t.to_dict(), ensure_ascii=False)
        back = json.loads(s)
        self.assertEqual(back["models"]["ag/x"]["calls"], 1)


class TestEstimatedCost(unittest.TestCase):
    """Estimasi biaya sesuai tabel harga."""

    def test_cost_matches_price_table(self):
        acct = Accounting({"prices": PRICES}, outdir=_tmpdir())
        acct.tracker.record(
            "ag/gemini-3-flash",
            {"prompt_tokens": 2000, "completion_tokens": 1000})
        cost = acct.estimated_cost()
        # 2000/1000*0.0005 + 1000/1000*0.002 = 0.001 + 0.002
        self.assertAlmostEqual(
            cost["per_model"]["ag/gemini-3-flash"], 0.003, places=6)
        self.assertAlmostEqual(cost["total_usd"], 0.003, places=6)
        self.assertEqual(cost["unknown_prices"], [])

    def test_unknown_price_cost_zero_with_warning(self):
        acct = Accounting({"prices": {}}, outdir=_tmpdir())
        acct.tracker.record("ag/model-tak-dikenal",
                            {"prompt_tokens": 9999, "completion_tokens": 1})
        cost, printed = _capture(acct.estimated_cost)
        self.assertEqual(cost["per_model"]["ag/model-tak-dikenal"], 0.0)
        self.assertEqual(cost["total_usd"], 0.0)
        self.assertIn("ag/model-tak-dikenal", cost["unknown_prices"])
        self.assertIn("tidak ada harga", printed)  # warning, bukan crash
        # warning hanya sekali
        _, printed2 = _capture(acct.estimated_cost)
        self.assertNotIn("tidak ada harga", printed2)

    def test_estimate_cost_for_helper(self):
        self.assertAlmostEqual(
            estimate_cost_for({"prompt_tokens": 1000, "completion_tokens": 0},
                              {"input_per_1k": 0.01, "output_per_1k": 0.1}),
            0.01, places=6)
        self.assertEqual(estimate_cost_for({"prompt_tokens": 1}, None), 0.0)


class TestPersist(unittest.TestCase):
    """usage.json tertulis dengan struktur yang benar."""

    def test_save_writes_usage_json(self):
        outdir = _tmpdir()
        acct = Accounting({"prices": PRICES}, outdir=outdir)
        acct.tracker.record("ag/gemini-3-flash",
                            {"prompt_tokens": 100, "completion_tokens": 50})
        path = acct.save()
        self.assertEqual(path, os.path.join(outdir, "usage.json"))
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["models"]["ag/gemini-3-flash"]["calls"], 1)
        self.assertEqual(data["grand"]["total_tokens"], 150)
        self.assertIn("total_estimated_cost_usd", data)
        self.assertIn("updated_at", data)

    def test_periodic_save_every_10_steps(self):
        outdir = _tmpdir()
        acct = Accounting({"prices": PRICES}, outdir=outdir)
        acct.maybe_periodic_save(5)
        self.assertFalse(os.path.exists(acct.usage_path()))
        acct.maybe_periodic_save(10)
        self.assertTrue(os.path.exists(acct.usage_path()))
        acct.maybe_periodic_save(20)
        self.assertTrue(os.path.exists(acct.usage_path()))

    def test_summary_text_contains_numbers(self):
        acct = Accounting({"prices": PRICES}, outdir=_tmpdir())
        acct.tracker.record("ag/gemini-3-flash",
                            {"prompt_tokens": 1234, "completion_tokens": 56})
        _, printed = _capture(print, acct.summary_text())
        self.assertIn("ag/gemini-3-flash", printed)
        self.assertIn("1,234", printed)
        self.assertIn("TOTAL", printed)
        self.assertIn("USD", printed)


class TestContextWarning(unittest.TestCase):
    """Warning saat pemakaian konteks >= context_warn_pct%."""

    def test_warn_at_80_percent(self):
        acct = Accounting({"context_warn_pct": 80}, outdir=_tmpdir(),
                          context_limit=10000)
        self.assertFalse(acct.check_context_warning(7999))
        hit, printed = _capture(acct.check_context_warning, 8000)
        self.assertTrue(hit)
        self.assertIn("80%", printed)
        self.assertTrue(acct.context_warned)

    def test_warn_only_once(self):
        acct = Accounting({"context_warn_pct": 80}, outdir=_tmpdir(),
                          context_limit=10000)
        self.assertTrue(acct.check_context_warning(9000))
        _, printed = _capture(acct.check_context_warning, 9500)
        self.assertEqual(printed, "")  # tidak spam

    def test_no_limit_no_warn(self):
        acct = Accounting({}, outdir=_tmpdir(), context_limit=None)
        self.assertFalse(acct.check_context_warning(10 ** 9))


class TestCostCap(unittest.TestCase):
    """run_cost_cap: 0 = nonaktif; tercapai -> sinyal stop."""

    def test_cap_zero_disabled(self):
        acct = Accounting({"prices": PRICES, "run_cost_cap": 0},
                          outdir=_tmpdir())
        acct.tracker.record("ag/claude-opus-4-6-thinking",
                            {"prompt_tokens": 10 ** 9,
                             "completion_tokens": 10 ** 9})
        self.assertFalse(acct.cap_reached())

    def test_cap_reached_when_cost_exceeds(self):
        acct = Accounting({"prices": PRICES, "run_cost_cap": 0.01},
                          outdir=_tmpdir())
        self.assertFalse(acct.cap_reached())
        acct.tracker.record("ag/gemini-3-flash",
                            {"prompt_tokens": 100000,
                             "completion_tokens": 0})
        # 100000/1000*0.0005 = 0.05 >= 0.01
        self.assertTrue(acct.cap_reached())
        self.assertIn("budget tercapai", acct.cap_message())


def _make_loop(outdir, accounting_cfg):
    """ElieveLoop tanpa DB/network: get_api_key di-mock."""
    with mock.patch.object(LOOP, "get_api_key", return_value="TEST-KEY"):
        loop = LOOP.ElieveLoop(
            task="tugas uji",
            outdir=outdir,
            model="ag/gemini-3-flash",
            max_steps=5,
            accounting_cfg=accounting_cfg,
            tasks_cfg={"enabled": False},
            memory_cfg={"enabled": False},
        )
    return loop


class TestLoopIntegration(unittest.TestCase):
    """Integrasi loop: record tiap call_model; cap -> stop rapi."""

    def test_cost_cap_stops_gracefully(self):
        outdir = _tmpdir()
        cfg = {"enabled": True, "prices": PRICES, "run_cost_cap": 0.01}
        loop = _make_loop(outdir, cfg)
        # usage palsu: biaya 100000 prompt token flash = $0.05 >= cap $0.01
        fake_usage = {"prompt_tokens": 100000, "completion_tokens": 500}
        fake_msg = {"content": "tidak dipakai", "tool_calls": None}
        with mock.patch.object(LOOP, "call_model",
                               return_value=(fake_msg, fake_usage)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = loop.run()
        printed = buf.getvalue()
        self.assertEqual(rc, 0, "cost cap harus return 0, bukan crash")
        with open(os.path.join(outdir, "progress.json")) as f:
            prog = json.load(f)
        self.assertEqual(prog["status"], "cost_capped")
        self.assertIn("budget tercapai", prog["note"])
        with open(os.path.join(outdir, "OUT.md")) as f:
            out_md = f.read()
        self.assertIn("Status: cost_capped", out_md)
        self.assertIn("budget tercapai", out_md)
        # usage.json tertulis
        with open(os.path.join(outdir, "usage.json")) as f:
            usage = json.load(f)
        self.assertEqual(
            usage["models"]["ag/gemini-3-flash"]["prompt_tokens"], 100000)
        # ringkasan tercetak ke stdout
        self.assertIn("Ringkasan pemakaian", printed)
        self.assertIn("USD", printed)

    def test_done_run_writes_usage_and_summary(self):
        outdir = _tmpdir()
        loop = _make_loop(outdir, {"enabled": True, "prices": PRICES})
        fake_usage = {"prompt_tokens": 100, "completion_tokens": 20}
        fake_msg = {"content": "TIDAK ADA TEMUAN", "tool_calls": None}
        with mock.patch.object(LOOP, "call_model",
                               return_value=(fake_msg, fake_usage)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = loop.run()
        self.assertEqual(rc, 0)
        with open(os.path.join(outdir, "usage.json")) as f:
            usage = json.load(f)
        self.assertEqual(usage["grand"]["total_tokens"], 120)
        self.assertIn("Ringkasan pemakaian", buf.getvalue())

    def test_accounting_disabled_is_noop(self):
        outdir = _tmpdir()
        loop = _make_loop(outdir, {"enabled": False})
        self.assertFalse(loop.acct.enabled)
        # record manual tetap jalan di tracker, tapi loop tak menulis file
        fake_msg = {"content": "TIDAK ADA TEMUAN", "tool_calls": None}
        with mock.patch.object(LOOP, "call_model",
                               return_value=(fake_msg, {})):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = loop.run()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(outdir, "usage.json")))

    def test_estimated_usage_when_provider_silent(self):
        outdir = _tmpdir()
        loop = _make_loop(outdir, {"enabled": True, "prices": PRICES})
        fake_msg = {"content": "TIDAK ADA TEMUAN", "tool_calls": None}
        with mock.patch.object(LOOP, "call_model",
                               return_value=(fake_msg, {})):
            with contextlib.redirect_stdout(io.StringIO()):
                loop.run()
        with open(os.path.join(outdir, "usage.json")) as f:
            usage = json.load(f)
        m = usage["models"]["ag/gemini-3-flash"]
        self.assertEqual(m["calls"], 1)
        self.assertEqual(m["estimated_calls"], 1)
        self.assertGreater(m["prompt_tokens"], 0)  # fallback chars/4


if __name__ == "__main__":
    unittest.main()
