"""Unit test Fase 4 — Orchestrator (multi-agent, opsional & pluggable).

Jalan tanpa 9router / API key / network (plan_fn & spawn_fn palsu;
get_api_key di-mock untuk integrasi loop):
    cd ~/workspace/hermes-agent && python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.orchestrator import (  # noqa: E402
    Orchestrator,
    OrchestratorError,
    WORKER_ENV_FLAG,
    is_orchestrator_active,
)
import hermes.loop as LOOP  # noqa: E402


def make_orch(**kw):
    params = dict(
        orchestrator_model="ag/gemini-3-flash",
        api_key="kunci-palsu",
        worker_model="ag/gemini-3-flash",
        max_workers=4,
    )
    params.update(kw)
    return Orchestrator(**params)


def fake_plan_3(task):
    return [
        {"title": "A", "task": "kerjakan A: " + task, "readonly": True},
        {"title": "B", "task": "kerjakan B", "readonly": False},
        {"title": "C", "task": "kerjakan C"},
    ]


class ActiveTest(unittest.TestCase):
    def test_kosong_off(self):
        self.assertFalse(is_orchestrator_active({}))
        self.assertFalse(is_orchestrator_active({"orchestrator_model": ""}))
        self.assertFalse(
            is_orchestrator_active({"orchestrator_model": "   "}))

    def test_enabled_false_off(self):
        self.assertFalse(is_orchestrator_active(
            {"enabled": False, "orchestrator_model": "ag/gemini-3-flash"}))

    def test_terisi_on(self):
        self.assertTrue(is_orchestrator_active(
            {"orchestrator_model": "ag/gemini-3-flash"}))


class ValidationTest(unittest.TestCase):
    def test_bns_ditolak(self):
        with self.assertRaises(OrchestratorError):
            make_orch(orchestrator_model="bns/deepseek-v4.1-flash")

    def test_oc_ditolak(self):
        with self.assertRaises(OrchestratorError):
            make_orch(orchestrator_model="oc/muse-spark-1.3")

    def test_non_ag_ditolak_via_9router(self):
        with self.assertRaises(OrchestratorError):
            make_orch(orchestrator_model="gpt-4o")

    def test_ag_lolos(self):
        o = make_orch(orchestrator_model="ag/gemini-3.1-pro")
        self.assertEqual(o.orchestrator_model, "ag/gemini-3.1-pro")

    def test_model_kosong_ditolak(self):
        with self.assertRaises(OrchestratorError):
            make_orch(orchestrator_model="")

    def test_custom_base_model_bebas(self):
        with mock.patch.dict(os.environ, {"ORCH_KEY": "sekret"}):
            o = make_orch(orchestrator_model="model-bebas-31b",
                          api_base="https://contoh.invalid/v1",
                          api_key_env="ORCH_KEY")
        self.assertTrue(o.api_url.startswith("https://contoh.invalid"))

    def test_custom_base_tanpa_env_ditolak(self):
        with self.assertRaises(OrchestratorError):
            make_orch(orchestrator_model="model-bebas",
                      api_base="https://contoh.invalid/v1",
                      api_key_env="")

    def test_custom_base_env_kosong_ditolak(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCH_KEY_KOSONG_XYZ", None)
            with self.assertRaises(OrchestratorError):
                make_orch(orchestrator_model="model-bebas",
                          api_base="https://contoh.invalid/v1",
                          api_key_env="ORCH_KEY_KOSONG_XYZ")


class PlanTest(unittest.TestCase):
    def test_plan_3_subtask(self):
        o = make_orch(plan_fn=fake_plan_3)
        subs = o.plan("audit plugin X")
        self.assertEqual(len(subs), 3)
        self.assertEqual(subs[0]["title"], "A")
        self.assertTrue(subs[0]["readonly"])
        self.assertIn("audit plugin X", subs[0]["task"])

    def test_plan_dibatasi_max_workers(self):
        o = make_orch(max_workers=2, plan_fn=fake_plan_3)
        self.assertEqual(len(o.plan("t")), 2)

    def test_plan_gagal_muncul_orchestrator_error(self):
        def boom(task):
            raise RuntimeError("mandor mati")
        o = make_orch(plan_fn=boom)
        # Kontrak: plan() selalu OrchestratorError saat planning gagal,
        # agar pemanggil bisa fallback ke single-agent.
        with self.assertRaises(OrchestratorError):
            o.plan("t")

    def test_plan_kosong_ditolak(self):
        o = make_orch(plan_fn=lambda t: [])
        with self.assertRaises(OrchestratorError):
            o.plan("t")


class DepthGuardTest(unittest.TestCase):
    def test_worker_dilarang_run_orchestrator(self):
        o = make_orch(plan_fn=fake_plan_3)
        with mock.patch.dict(os.environ, {WORKER_ENV_FLAG: "1"}):
            with self.assertRaises(OrchestratorError):
                o.run("t", tempfile.mkdtemp())


class SpawnMergeTest(unittest.TestCase):
    def _spawn_ok(self, index, subtask, worker_outdir):
        os.makedirs(worker_outdir, exist_ok=True)
        with open(os.path.join(worker_outdir, "OUT.md"), "w") as f:
            f.write("# Hermes — hasil\n\n- Task: x\n\n---\n\n## temuan w%d\n"
                    % index)
        return {"index": index, "title": subtask["title"], "status": "done",
                "outdir": worker_outdir, "returncode": 0, "note": "",
                "elapsed_s": 0.1}

    def test_run_merge_3_worker(self):
        outdir = tempfile.mkdtemp()
        o = make_orch(plan_fn=fake_plan_3, spawn_fn=self._spawn_ok)
        out_md = o.run("audit X", outdir)
        self.assertTrue(os.path.isfile(out_md))
        with open(out_md) as f:
            body = f.read()
        for i in range(3):
            self.assertIn(f"Worker {i}", body)
            self.assertIn(f"temuan w{i}", body)
        with open(os.path.join(outdir, "progress.json")) as f:
            prog = json.load(f)
        self.assertEqual(prog["mode"], "orchestrator")
        self.assertEqual(len(prog["workers"]), 3)
        self.assertEqual(prog["status"], "done")

    def test_failure_isolation(self):
        def spawn_gagal(index, subtask, worker_outdir):
            if index == 1:
                raise RuntimeError("worker meledak")
            return self._spawn_ok(index, subtask, worker_outdir)

        outdir = tempfile.mkdtemp()
        o = make_orch(plan_fn=fake_plan_3, spawn_fn=spawn_gagal)
        out_md = o.run("audit X", outdir)
        with open(out_md) as f:
            body = f.read()
        # worker 1 gagal tercatat, worker lain tetap merge
        self.assertIn("Worker 1", body)
        self.assertIn("temuan w0", body)
        self.assertIn("temuan w2", body)
        with open(os.path.join(outdir, "progress.json")) as f:
            prog = json.load(f)
        by_idx = {w["index"]: w["status"] for w in prog["workers"]}
        self.assertEqual(by_idx[1], "error")
        self.assertEqual(by_idx[0], "done")
        self.assertEqual(prog["status"], "partial")

    def test_merge_tanpa_outmd_worker(self):
        def spawn_kosong(index, subtask, worker_outdir):
            os.makedirs(worker_outdir, exist_ok=True)
            return {"index": index, "title": subtask["title"],
                    "status": "done", "outdir": worker_outdir,
                    "returncode": 0, "note": "", "elapsed_s": 0}

        outdir = tempfile.mkdtemp()
        o = make_orch(plan_fn=fake_plan_3, spawn_fn=spawn_kosong)
        out_md = o.run("t", outdir)
        with open(out_md) as f:
            self.assertIn("tidak ada OUT.md", f.read())


class FromConfigTest(unittest.TestCase):
    def test_from_config(self):
        o = Orchestrator.from_config(
            {"orchestrator_model": "ag/gemini-3-flash",
             "max_workers": 2, "worker_max_steps": 10},
            task="t", outdir="/tmp/x", model="ag/gemini-3.1-pro",
            api_key="k")
        self.assertEqual(o.max_workers, 2)
        self.assertEqual(o.worker_max_steps, 10)
        # worker_model kosong -> pakai model utama
        self.assertEqual(o.worker_model, "ag/gemini-3.1-pro")


class NoExecTest(unittest.TestCase):
    def _loop(self, **kw):
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.HermesLoop(
                task="t", outdir=tempfile.mkdtemp(), **kw)
        return loop

    def test_default_ada_exec(self):
        loop = self._loop()
        self.assertIn("exec", loop.dispatch)
        names = [s["function"]["name"] for s in loop.tool_schemas]
        self.assertIn("exec", names)

    def test_no_exec_buang_exec(self):
        loop = self._loop(no_exec=True)
        self.assertNotIn("exec", loop.dispatch)
        names = [s["function"]["name"] for s in loop.tool_schemas]
        self.assertNotIn("exec", names)
        # tool lain tetap ada
        for t in ("read_file", "list_dir", "grep", "remember"):
            self.assertIn(t, loop.dispatch)

    def test_fallback_tool_tolak_exec_readonly(self):
        loop = self._loop(no_exec=True)
        fb = LOOP._fallback_tool_call(
            '```tool\n{"name": "exec", "arguments": {"command": "id"}}\n```',
            loop.dispatch)
        self.assertIsNone(fb)
        fb2 = LOOP._fallback_tool_call(
            '```tool\n{"name": "grep", "arguments": {"pattern": "x"}}\n```',
            loop.dispatch)
        self.assertIsNotNone(fb2)

    def test_fallback_default_tetap_kompatibel(self):
        # tanpa dispatch arg -> perilaku lama (DISPATCH global)
        fb = LOOP._fallback_tool_call(
            '```tool\n{"name": "exec", "arguments": {"command": "id"}}\n```')
        self.assertIsNotNone(fb)


class MainDepthGuardTest(unittest.TestCase):
    def test_main_tolak_worker_jalankan_orchestrator(self):
        cfg_path = os.path.join(tempfile.mkdtemp(), "c.yaml")
        with open(cfg_path, "w") as f:
            f.write("orchestrator:\n  enabled: true\n"
                    "  orchestrator_model: ag/gemini-3-flash\n")
        with mock.patch.dict(os.environ, {WORKER_ENV_FLAG: "1"}):
            rc = LOOP.main(["--task", "t",
                            "--outdir", tempfile.mkdtemp(),
                            "--config", cfg_path])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
