"""Unit test Gap 6 — Plan mode (riset read-only -> PLAN.md -> berhenti)
+ recon gate orchestrator (orchestrator.plan_first).

Jalan tanpa 9router / API key / network (call_model & get_api_key
di-mock; recon_plan_fn & spawn_fn palsu):
    cd ~/workspace/elieve && python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import elieve.loop as LOOP  # noqa: E402
from elieve import tools  # noqa: E402
from elieve.orchestrator import (  # noqa: E402
    Orchestrator,
    OrchestratorError,
)
from elieve.planmode import (  # noqa: E402
    PLAN_SECTIONS,
    resolve_plan_model,
    run_plan,
    write_plan_md,
)
from elieve.providers import ProviderConfig  # noqa: E402

# Policy setara aturan Bayu: ag/* boleh; bns/* dan oc/* ditolak.
POLICY = {"allow": ["ag/*"], "forbid": ["bns/*", "oc/*"]}

PLAN_FINAL = (
    "## Tujuan\n"
    "Petakan permukaan target.\n\n"
    "## Permukaan yang dipetakan\n"
    "- endpoint /api/v1\n\n"
    "## Langkah rencana\n"
    "1. recon\n2. uji\n\n"
    "## Estimasi\n"
    "2 jam.\n"
)


def make_provider(**kw):
    params = dict(
        base_url="http://127.0.0.1:1/v1",
        api_key_env="ELIEVE_TEST_PLAN_KEY",
        model="ag/gemini-3-flash",
    )
    params.update(kw)
    return ProviderConfig(**params)


class FakePlanModel:
    """Model palsu 3 panggilan: read -> coba exec -> jawaban rencana.

    Mencatat schemas `tools` yang diterima tiap panggilan dan pesan
    `tool` terakhir (untuk verifikasi penolakan exec).
    """

    def __init__(self, target_file):
        self.calls = 0
        self.target_file = target_file
        self.tools_seen = []
        self.last_messages = None

    def __call__(self, messages, model, provider_cfg,
                 api_key=None, tools=None):
        self.calls += 1
        self.last_messages = messages
        self.tools_seen.append(list(tools or []))

        def tc(cid, name, args):
            return {
                "id": cid, "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args)},
            }

        if self.calls == 1:
            return ({"content": "",
                     "tool_calls": [tc("c1", "read_file",
                                       {"path": self.target_file})]}, {})
        if self.calls == 2:
            # Upaya memanggil exec di mode plan -> harus DITOLAK sebagai
            # observasi (bukan crash).
            return ({"content": "",
                     "tool_calls": [tc("c2", "exec",
                                       {"command": "echo JANGAN-JALAN"})]},
                    {})
        return ({"content": PLAN_FINAL}, {})


class ResolvePlanModelTest(unittest.TestCase):
    def test_kosong_pakai_model_utama(self):
        self.assertEqual(
            resolve_plan_model({}, "ag/gemini-3-flash", POLICY),
            "ag/gemini-3-flash")

    def test_plan_model_dipakai(self):
        self.assertEqual(
            resolve_plan_model({"plan_model": "ag/gemini-3.1-pro"},
                               "ag/gemini-3-flash", POLICY),
            "ag/gemini-3.1-pro")

    def test_bns_ditolak(self):
        with self.assertRaises(ValueError):
            resolve_plan_model({"plan_model": "bns/deepseek-v4.1-flash"},
                               "ag/x", POLICY)

    def test_oc_ditolak(self):
        with self.assertRaises(ValueError):
            resolve_plan_model({}, "oc/muse-spark-1.3", POLICY)

    def test_kosong_semua_error(self):
        with self.assertRaises(ValueError):
            resolve_plan_model({}, "", POLICY)


class WritePlanMdTest(unittest.TestCase):
    def test_bagian_wajib_ada(self):
        d = tempfile.mkdtemp()
        p = write_plan_md(d, task="t", model="ag/x",
                          final_text=PLAN_FINAL, lang="id")
        with open(p) as f:
            text = f.read()
        for sec in PLAN_SECTIONS["id"]:
            self.assertIn("## " + sec, text)

    def test_bagian_hilang_ditandai_bukan_dikarang(self):
        d = tempfile.mkdtemp()
        p = write_plan_md(d, task="t", model="ag/x",
                          final_text="cuma satu paragraf.", lang="id")
        with open(p) as f:
            text = f.read()
        for sec in PLAN_SECTIONS["id"]:
            self.assertIn("## " + sec, text)
        # penanda jujur ada, isi karangan tidak ada
        self.assertIn("tidak dihasilkan model", text)

    def test_en_sections(self):
        d = tempfile.mkdtemp()
        p = write_plan_md(d, task="t", model="ag/x",
                          final_text="## Goal\nx", lang="en")
        with open(p) as f:
            text = f.read()
        self.assertIn("## Goal", text)
        self.assertIn("## Estimate", text)


class PlanRunTest(unittest.TestCase):
    """Simulasi run plan end-to-end dengan model fake."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.outdir = tempfile.mkdtemp()
        with open(os.path.join(self.ws, "target.txt"), "w") as f:
            f.write("isi target\n")
        tools.configure_roots(self.ws)
        self.fake = FakePlanModel(
            os.path.join(self.ws, "target.txt"))

    def _run(self, **kw):
        params = dict(
            task="petakan target",
            outdir=self.outdir,
            model="ag/gemini-3-flash",
            provider_cfg=make_provider(),
            model_policy=POLICY,
            max_steps=10,
            lang="id",
            workspace_root=self.ws,
        )
        params.update(kw)
        with mock.patch.object(LOOP, "get_api_key", return_value="k"), \
             mock.patch.object(LOOP, "call_model",
                               side_effect=self.fake), \
             mock.patch.object(LOOP, "MODEL_CALL_DELAY_S", 0):
            return run_plan(**params)

    def test_plan_berhenti_setelah_rencana(self):
        rc = self._run()
        self.assertEqual(rc, 0)
        # tepat 3 panggilan model: read -> exec(ditolak) -> jawaban akhir
        self.assertEqual(self.fake.calls, 3)
        prog = json.load(open(os.path.join(self.outdir, "progress.json")))
        self.assertEqual(prog["status"], "done")

    def test_plan_md_tertulis_dengan_bagian(self):
        self._run()
        plan_md = os.path.join(self.outdir, "PLAN.md")
        self.assertTrue(os.path.isfile(plan_md))
        text = open(plan_md).read()
        for sec in PLAN_SECTIONS["id"]:
            self.assertIn("## " + sec, text)
        self.assertIn("## Tujuan", text)

    def test_exec_tidak_ada_di_toolset(self):
        # model tidak pernah melihat schema exec ...
        self._run()
        for schemas in self.fake.tools_seen:
            names = [s.get("function", {}).get("name") for s in schemas]
            self.assertNotIn("exec", names)

    def test_exec_ditolak_sebagai_observasi_bukan_crash(self):
        # ... dan upaya memanggil exec dikembalikan sebagai observasi
        # "unknown tool", run tetap lanjut sampai selesai (rc 0).
        rc = self._run()
        self.assertEqual(rc, 0)
        tool_msgs = [m.get("content", "")
                     for m in (self.fake.last_messages or [])
                     if m.get("role") == "tool"]
        self.assertTrue(
            any("unknown tool: exec" in c for c in tool_msgs),
            "observasi penolakan exec tidak ditemukan di pesan tool")

    def test_loop_level_no_exec(self):
        # mekanisme yang dipakai plan = no_exec yang sudah ada
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.ElieveLoop(
                task="t", outdir=tempfile.mkdtemp(),
                model="ag/gemini-3-flash", no_exec=True)
        self.assertNotIn("exec", loop.dispatch)
        names = [s["function"]["name"] for s in loop.tool_schemas]
        self.assertNotIn("exec", names)


# -- recon gate orchestrator --------------------------------------------

def make_orch(**kw):
    params = dict(
        orchestrator_model="ag/gemini-3-flash",
        provider_cfg=make_provider(),
        worker_model="ag/gemini-3-flash",
        max_workers=4,
    )
    params.update(kw)
    return Orchestrator(**params)


def fake_plan_2(task):
    return [
        {"title": "A", "task": "kerjakan A", "readonly": True},
        {"title": "B", "task": "kerjakan B", "readonly": False},
    ]


class ReconGateTest(unittest.TestCase):
    def _fakes(self, order):
        def fake_recon(task, outdir):
            order.append("recon")
            os.makedirs(outdir, exist_ok=True)
            p = os.path.join(outdir, "PLAN.md")
            with open(p, "w") as f:
                f.write("# rencana palsu\n")
            return p

        def fake_spawn(i, subtask, wdir):
            order.append(f"spawn{i}")
            os.makedirs(wdir, exist_ok=True)
            with open(os.path.join(wdir, "OUT.md"), "w") as f:
                f.write("# Elieve — hasil\n\nok\n")
            return {"index": i, "title": subtask["title"],
                    "status": "done", "outdir": wdir,
                    "returncode": 0, "note": "", "elapsed_s": 0}

        return fake_recon, fake_spawn

    def test_plan_first_recon_dulu_baru_spawn(self):
        order = []
        fake_recon, fake_spawn = self._fakes(order)
        orch = make_orch(plan_first=True,
                         plan_fn=fake_plan_2,
                         spawn_fn=fake_spawn,
                         recon_plan_fn=fake_recon)
        outdir = tempfile.mkdtemp()
        orch.run("task", outdir)
        # recon jalan DULU, worker di-spawn SETELAHNYA
        self.assertEqual(order[0], "recon")
        self.assertTrue(all(o.startswith("spawn") for o in order[1:]))
        self.assertEqual(len(order), 3)
        # PLAN.md ada di root outdir sebagai bukti gate lolos
        self.assertTrue(os.path.isfile(os.path.join(outdir, "PLAN.md")))
        # info recon tercatat di progress.json
        with open(os.path.join(outdir, "progress.json")) as f:
            prog = json.load(f)
        self.assertIn("recon_plan", prog)

    def test_plan_first_false_tanpa_recon(self):
        order = []
        _, fake_spawn = self._fakes(order)
        orch = make_orch(plan_first=False,
                         plan_fn=fake_plan_2,
                         spawn_fn=fake_spawn,
                         recon_plan_fn=lambda t, d: order.append("recon"))
        outdir = tempfile.mkdtemp()
        orch.run("task", outdir)
        self.assertNotIn("recon", order)
        self.assertFalse(os.path.exists(os.path.join(outdir, "PLAN.md")))

    def test_plan_first_default_off(self):
        orch = make_orch()
        self.assertFalse(orch.plan_first)

    def test_recon_gagal_worker_tidak_di_spawn(self):
        order = []

        def fake_recon_gagal(task, outdir):
            order.append("recon")
            return None  # PLAN.md tidak ada -> gate gagal

        def fake_spawn(i, subtask, wdir):
            order.append(f"spawn{i}")
            return {"index": i, "title": subtask["title"],
                    "status": "done", "outdir": wdir,
                    "returncode": 0, "note": "", "elapsed_s": 0}

        orch = make_orch(plan_first=True,
                         plan_fn=fake_plan_2,
                         spawn_fn=fake_spawn,
                         recon_plan_fn=fake_recon_gagal)
        with self.assertRaises(OrchestratorError):
            orch.run("task", tempfile.mkdtemp())
        self.assertEqual(order, ["recon"])  # spawn tidak pernah jalan

    def test_from_config_baca_plan_first(self):
        orch = Orchestrator.from_config(
            {"orchestrator_model": "ag/gemini-3-flash",
             "plan_first": True, "plan_model": "ag/gemini-3.1-pro"},
            task="t", outdir=tempfile.mkdtemp(),
            model="ag/gemini-3-flash", provider_cfg=make_provider(),
            model_policy=POLICY)
        self.assertTrue(orch.plan_first)
        self.assertEqual(orch.plan_model, "ag/gemini-3.1-pro")

    def test_from_config_default_plan_first_false(self):
        orch = Orchestrator.from_config(
            {"orchestrator_model": "ag/gemini-3-flash"},
            task="t", outdir=tempfile.mkdtemp(),
            model="ag/gemini-3-flash", provider_cfg=make_provider())
        self.assertFalse(orch.plan_first)
        self.assertEqual(orch.plan_model, "")

    def test_plan_model_bns_ditolak(self):
        orch = make_orch(plan_first=True,
                         plan_model="bns/deepseek-v4.1-flash",
                         model_policy=POLICY,
                         plan_fn=fake_plan_2,
                         spawn_fn=lambda i, s, w: None)
        with self.assertRaises(OrchestratorError):
            orch.run("task", tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
