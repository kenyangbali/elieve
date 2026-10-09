"""Unit test Fase 2 — AgentMemory (MEMORY.md + autoDream).

Jalan tanpa 9router / API key / network (post_fn palsu untuk tidy,
get_api_key di-mock untuk integrasi loop):
    cd ~/workspace/hermes-agent && python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.memory import (  # noqa: E402
    AgentMemory,
    contains_secret,
    DEFAULT_MAX_FACT_CHARS,
)
from hermes import tools as T  # noqa: E402
from hermes.tools import memory as TM  # noqa: E402
import hermes.loop as LOOP  # noqa: E402


def fresh_mem(**kw):
    d = tempfile.mkdtemp()
    return AgentMemory(os.path.join(d, "MEMORY.md"), **kw), d


class TestRememberRecall(unittest.TestCase):
    def test_roundtrip(self):
        mem, _ = fresh_mem()
        self.assertTrue(mem.remember("Pola SQLi f-string di auth.py"))
        text = mem.recall()
        self.assertIn("Pola SQLi f-string di auth.py", text)
        self.assertRegex(text, r"\(\d{4}-\d{2}-\d{2}\)")

    def test_recall_empty_when_no_file(self):
        mem, d = fresh_mem()
        self.assertEqual(mem.recall(), "")
        self.assertFalse(os.path.exists(os.path.join(d, "MEMORY.md")))

    def test_empty_fact_rejected(self):
        mem, _ = fresh_mem()
        self.assertFalse(mem.remember(""))
        self.assertFalse(mem.remember("   "))
        self.assertEqual(mem.recall(), "")

    def test_duplicate_rejected(self):
        mem, _ = fresh_mem()
        self.assertTrue(mem.remember("Plugin X rawan open redirect"))
        self.assertFalse(mem.remember("Plugin X rawan open redirect"))
        # variasi spasi/kapital tetap dianggap duplikat
        self.assertFalse(mem.remember("  plugin x RAWAN open   redirect "))
        self.assertEqual(len(mem.recall().splitlines()), 1)

    def test_truncate_long_fact(self):
        mem, _ = fresh_mem()
        long_fact = "kata " * 60  # ~300 char
        self.assertTrue(mem.remember(long_fact))
        line = mem.recall().splitlines()[0]
        body = line[2:].rsplit(" (", 1)[0]
        self.assertLessEqual(len(body), DEFAULT_MAX_FACT_CHARS + 3)
        self.assertTrue(body.endswith("..."))
        # potong di batas kata: kata terakhir utuh, bukan fragmen
        self.assertTrue(body[:-3].rstrip().endswith("kata"))


class TestSecretFilter(unittest.TestCase):
    SECRETS = [
        "deploy token ghp_abcdefghijklmnopqrstuvwx di server",
        "kunci sk-abcdefghij1234567890 untuk openai",
        "slack webhook xoxb-1234567890-abcdefghijklmnop",
        "aws key AKIAIOSFODNN7EXAMPLE aktif",
        "header Authorization: Bearer abcdefghij1234567890 dikirim",
        "config api_key=supersecret12345 di file",
        "login password: hunter2x harus diganti",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA",
        "db passwd=rahasia123 di env",
        "client secret=abc123def456 untuk oauth",
    ]

    def test_contains_secret_labels(self):
        for s in self.SECRETS:
            label = contains_secret(s)
            self.assertIsNotNone(label, f"pola lolos filter: {s[:30]}")

    def test_remember_rejects_secrets(self):
        mem, d = fresh_mem()
        for s in self.SECRETS:
            self.assertFalse(mem.remember(s), f"tersimpan! {s[:30]}")
        self.assertFalse(os.path.exists(os.path.join(d, "MEMORY.md")))

    def test_normal_text_passes(self):
        self.assertIsNone(contains_secret("Pola XSS di komentar plugin Y"))
        self.assertIsNone(contains_secret("Bearer token adalah konsep auth"))
        # 'password' tanpa assignment bukan pola rahasia
        self.assertIsNone(contains_secret("cek password hashing di login.php"))


class TestTidy(unittest.TestCase):
    def test_tidy_merges_duplicates(self):
        mem, _ = fresh_mem()
        mem.remember("Pola SQLi f-string di auth.py")
        mem.remember("auth.py pakai f-string untuk query SQL (pola SQLi)")
        mem.remember("Plugin Z versi lama rawan RCE")

        def fake_post(payload, api_key):
            self.assertEqual(payload["model"], "ag/gemini-3-flash")
            return (
                "- Pola SQLi f-string untuk query di auth.py (2026-10-09)\n"
                "- Plugin Z versi lama rawan RCE (2026-10-09)\n"
            )

        out = mem.tidy("dummy-key", post_fn=fake_post)
        lines = [l for l in out.splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2)
        # file juga diperbarui
        self.assertEqual(len(mem.recall().splitlines()), 2)

    def test_tidy_drops_secret_output(self):
        mem, _ = fresh_mem()
        mem.remember("Catatan audit plugin A")
        mem.remember("Catatan audit plugin B")

        def fake_post(payload, api_key):
            return (
                "- Catatan audit plugin A (2026-10-09)\n"
                "- token bocor ghp_abcdefghijklmnopqrstuvwx (2026-10-09)\n"
            )

        out = mem.tidy("dummy-key", post_fn=fake_post)
        self.assertNotIn("ghp_", out)
        self.assertIn("Catatan audit plugin A", out)

    def test_tidy_rejects_forbidden_model(self):
        # Policy equivalent of the old hardcoded rule, now explicit.
        policy = {"allow": ["ag/*"], "forbid": ["bns/*", "oc/*"]}
        mem, _ = fresh_mem()
        mem.remember("satu")
        mem.remember("dua")
        with self.assertRaises(ValueError):
            mem.tidy("k", model="bns/deepseek-v4.1-flash",
                     model_policy=policy)
        with self.assertRaises(ValueError):
            mem.tidy("k", model="oc/muse-spark-1.3", model_policy=policy)

    def test_tidy_noop_when_few_bullets(self):
        mem, _ = fresh_mem()
        mem.remember("satu-satunya butir")

        def no_call(payload, api_key):
            raise AssertionError("post_fn tidak boleh dipanggil")

        out = mem.tidy("k", post_fn=no_call)
        self.assertIn("satu-satunya butir", out)


class TestRememberTool(unittest.TestCase):
    def test_schema_registered(self):
        names = [s["function"]["name"] for s in T.TOOL_SCHEMAS]
        self.assertIn("remember", names)
        self.assertIn("remember", T.DISPATCH)

    def test_tool_roundtrip(self):
        mem, _ = fresh_mem()
        TM.bind_memory(mem)
        try:
            out = T.DISPATCH["remember"](fact="Pola bug dari tool")
            self.assertIn("tersimpan", out)
            self.assertIn("Pola bug dari tool", mem.recall())
            out2 = T.DISPATCH["remember"](
                fact="api_key=zzzz1234 rahasia")
            self.assertIn("DITOLAK", out2)
        finally:
            TM.unbind_memory()

    def test_tool_unbound_raises(self):
        TM.unbind_memory()
        with self.assertRaises(T.ToolError):
            T.DISPATCH["remember"](fact="x")


class TestLoopIntegration(unittest.TestCase):
    def _make_loop(self, outdir, **kw):
        cfg = {"enabled": True, "tidy_every_runs": 5,
               "max_fact_chars": 150,
               "summarizer_model": "ag/gemini-3-flash"}
        cfg.update(kw)
        with mock.patch.object(LOOP, "get_api_key", return_value="test-key"):
            return LOOP.HermesLoop(task="t", outdir=outdir,
                                   model="ag/gemini-3-flash", memory_cfg=cfg)

    def test_recall_injected_into_system_prompt(self):
        d = tempfile.mkdtemp()
        mem = AgentMemory(os.path.join(d, "MEMORY.md"))
        mem.remember("Ingatan penting dari sesi lalu")
        loop = self._make_loop(d)

        captured = {}

        def fake_call(messages, model, provider_cfg, api_key=None,
                      tools=None):
            captured["system"] = messages[0]["content"]
            return ({"content": "TIDAK ADA TEMUAN", "tool_calls": None},
                    {"prompt_tokens": 50})

        with mock.patch.object(LOOP, "call_model", side_effect=fake_call):
            rc = loop.run()
        self.assertEqual(rc, 0)
        self.assertIn("## Ingatan sesi lalu", captured["system"])
        self.assertIn("Ingatan penting dari sesi lalu", captured["system"])
        # tool remember ter-bind selama run
        self.assertIsNotNone(TM._MEMORY)

    def test_no_memory_section_when_empty(self):
        d = tempfile.mkdtemp()
        loop = self._make_loop(d)
        captured = {}

        def fake_call(messages, model, provider_cfg, api_key=None,
                      tools=None):
            captured["system"] = messages[0]["content"]
            return ({"content": "TIDAK ADA TEMUAN", "tool_calls": None}, {})

        with mock.patch.object(LOOP, "call_model", side_effect=fake_call):
            loop.run()
        self.assertNotIn("## Ingatan sesi lalu", captured["system"])

    def test_tidy_counter(self):
        d = tempfile.mkdtemp()
        loop = self._make_loop(d, tidy_every_runs=2)
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            pass
        with mock.patch.object(AgentMemory, "tidy",
                               return_value="") as mtidy:
            for _ in range(3):
                loop._setup_memory()
        # tidy jalan pada run ke-2 saja
        self.assertEqual(mtidy.call_count, 1)
        with open(os.path.join(d, ".memory_counter")) as f:
            self.assertEqual(f.read().strip(), "3")

    def test_memory_disabled(self):
        d = tempfile.mkdtemp()
        loop = self._make_loop(d, enabled=False)
        self.assertEqual(loop._setup_memory(), "")
        self.assertIsNone(TM._MEMORY)

    def test_tidy_cli_flag(self):
        d = tempfile.mkdtemp()
        mem = AgentMemory(os.path.join(d, "MEMORY.md"))
        mem.remember("butir a")
        mem.remember("butir b")
        with mock.patch.object(LOOP, "get_api_key", return_value="k"), \
             mock.patch.object(AgentMemory, "tidy",
                               return_value="- butir a (2026-10-09)") as mt:
            rc = LOOP.main(["--task", "x", "--outdir", d, "--tidy"])
        self.assertEqual(rc, 0)
        mt.assert_called_once()

    def test_forbidden_summarizer_rejected_in_loop(self):
        d = tempfile.mkdtemp()
        policy = {"allow": ["ag/*"], "forbid": ["bns/*", "oc/*"]}
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            with self.assertRaises(ValueError):
                LOOP.HermesLoop(
                    task="t", outdir=d, model="ag/gemini-3-flash",
                    memory_cfg={"summarizer_model": "bns/x"},
                    model_policy=policy)


if __name__ == "__main__":
    unittest.main()
