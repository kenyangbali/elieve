"""Unit test Gap 4 — checkpoints / resume percakapan (elieve/checkpoints.py).

Jalan tanpa 9router / API key / network (get_api_key & call_model di-mock
untuk integrasi loop):
    cd ~/workspace/elieve && python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elieve.checkpoints import (  # noqa: E402
    CheckpointError,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
    should_save,
)
import elieve.loop as LOOP  # noqa: E402


def _tmpdir():
    return tempfile.mkdtemp()


def _msgs():
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "audit plugin X"},
        {"role": "assistant", "content": "cek dulu",
         "tool_calls": [{"id": "1", "function": {
             "name": "exec", "arguments": '{"command": "echo hi"}'}}]},
        {"role": "tool", "tool_call_id": "1", "name": "exec",
         "content": "hi"},
    ]


def _state(task="audit plugin X", model="ag/gemini-3-flash"):
    return {
        "task": task,
        "model": model,
        "max_steps": 40,
        "usage": {"models": {model: {
            "prompt_tokens": 100, "completion_tokens": 20,
            "total_tokens": 120, "calls": 2, "estimated_calls": 0}},
            "grand": {"prompt_tokens": 100, "completion_tokens": 20,
                      "total_tokens": 120, "calls": 2, "estimated_calls": 0}},
        "tasks": [{"title": "Recon", "status": "completed"}],
    }


class TestSaveLoadRoundtrip(unittest.TestCase):
    def test_save_creates_file_and_loads_back(self):
        d = _tmpdir()
        path = save_checkpoint(d, 7, _msgs(), state=_state())
        self.assertEqual(
            path, os.path.join(d, "checkpoints", "ckpt-7.json"))
        self.assertTrue(os.path.exists(path))
        rec = load_checkpoint(d, 7)
        self.assertEqual(rec["step"], 7)
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["messages"], _msgs())
        self.assertEqual(rec["task"], "audit plugin X")
        self.assertEqual(rec["model"], "ag/gemini-3-flash")
        self.assertEqual(rec["state"]["tasks"],
                         [{"title": "Recon", "status": "completed"}])
        self.assertIn("saved_at", rec)

    def test_save_is_atomic_no_tmp_leftovers(self):
        d = _tmpdir()
        save_checkpoint(d, 3, _msgs())
        names = os.listdir(os.path.join(d, "checkpoints"))
        self.assertEqual(names, ["ckpt-3.json"])

    def test_save_rejects_bad_step(self):
        d = _tmpdir()
        for bad in (-1, "3", 2.5, True, None):
            with self.assertRaises(CheckpointError, msg=f"step={bad!r}"):
                save_checkpoint(d, bad, _msgs())

    def test_save_rejects_bad_messages(self):
        d = _tmpdir()
        for bad in ("bukan-list", [{"role": "user"}, "bukan-dict"]):
            with self.assertRaises(CheckpointError):
                save_checkpoint(d, 1, bad)

    def test_load_latest_by_default(self):
        d = _tmpdir()
        save_checkpoint(d, 10, _msgs())
        save_checkpoint(d, 20, _msgs())
        save_checkpoint(d, 5, _msgs())
        self.assertEqual(load_checkpoint(d)["step"], 20)
        self.assertEqual(load_checkpoint(d, 5)["step"], 5)

    def test_load_missing_step_error_is_clear(self):
        d = _tmpdir()
        save_checkpoint(d, 10, _msgs())
        with self.assertRaises(CheckpointError) as cm:
            load_checkpoint(d, 99)
        self.assertIn("tidak ditemukan", str(cm.exception))
        self.assertIn("step 10", str(cm.exception))

    def test_load_no_checkpoints_error_is_clear(self):
        d = _tmpdir()
        with self.assertRaises(CheckpointError) as cm:
            load_checkpoint(d)
        self.assertIn("tidak ada checkpoint", str(cm.exception))

    def test_load_corrupt_json_error_is_clear_not_mystery(self):
        d = _tmpdir()
        os.makedirs(os.path.join(d, "checkpoints"))
        with open(os.path.join(d, "checkpoints", "ckpt-1.json"), "w") as f:
            f.write("{ini bukan json valid,,,")
        with self.assertRaises(CheckpointError) as cm:
            load_checkpoint(d, 1)
        msg = str(cm.exception)
        self.assertIn("CORRUPT", msg)
        # bukan JSONDecodeError mentah yang lolos
        self.assertNotIsInstance(cm.exception, json.JSONDecodeError)

    def test_load_corrupt_structure_error_is_clear(self):
        d = _tmpdir()
        os.makedirs(os.path.join(d, "checkpoints"))
        with open(os.path.join(d, "checkpoints", "ckpt-2.json"), "w") as f:
            json.dump({"step": "bukan-int", "messages": "bukan-list"}, f)
        with self.assertRaises(CheckpointError) as cm:
            load_checkpoint(d, 2)
        self.assertIn("CORRUPT", str(cm.exception))

    def test_load_ignores_non_checkpoint_files(self):
        d = _tmpdir()
        save_checkpoint(d, 4, _msgs())
        cdir = os.path.join(d, "checkpoints")
        with open(os.path.join(cdir, ".ckpt-99.tmp"), "w") as f:
            f.write("tmp sisa")
        with open(os.path.join(cdir, "notes.txt"), "w") as f:
            f.write("catatan")
        self.assertEqual(list_checkpoints(d),
                         [{"step": 4, "path": os.path.join(cdir, "ckpt-4.json")}])

    def test_list_checkpoints_sorted(self):
        d = _tmpdir()
        for s in (30, 10, 20):
            save_checkpoint(d, s, _msgs())
        got = [i["step"] for i in list_checkpoints(d)]
        self.assertEqual(got, [10, 20, 30])

    def test_should_save(self):
        self.assertTrue(should_save(10, 10))
        self.assertTrue(should_save(20, 10))
        self.assertFalse(should_save(9, 10))
        self.assertFalse(should_save(0, 10))
        self.assertFalse(should_save(10, 0))
        self.assertFalse(should_save(10, "sepuluh"))


class TestResumeLoop(unittest.TestCase):
    def setUp(self):
        from elieve import tools as _tools
        self._prev_root = _tools.get_workspace_root()
        _tools.configure_roots("/tmp")

    def tearDown(self):
        from elieve import tools as _tools
        _tools.configure_roots(self._prev_root)

    def _loop(self, outdir, **kw):
        base = {"enabled": True, "every_n_steps": 2}
        base.update(kw.pop("checkpoints_cfg", {}) or {})
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            return LOOP.ElieveLoop(
                task="audit plugin X", outdir=outdir,
                model="ag/gemini-3-flash",
                checkpoints_cfg=base, **kw)

    def _tool_msg(self, cmd="echo hi"):
        return {"content": None, "tool_calls": [{
            "id": "c1", "function": {
                "name": "exec",
                "arguments": json.dumps({"command": cmd})}}]}

    def test_resume_continues_from_step_N_plus_1(self):
        """Simulasi: run mati di step 3 -> resume -> lanjut di step 4,
        dengan messages lengkap dari checkpoint."""
        d = _tmpdir()
        loop = self._loop(d, max_steps=10)

        calls = {"n": 0}

        def flaky_model(messages, model, provider_cfg, api_key=None,
                        tools=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                return self._tool_msg(f"echo step{calls['n']}"), {}
            raise RuntimeError("simulasi: proses mati di step 3")

        with mock.patch.object(LOOP, "call_model", side_effect=flaky_model), \
                mock.patch.object(LOOP, "MODEL_CALL_DELAY_S", 0):
            rc = loop.run()
        self.assertEqual(rc, 1)  # error di step 3
        # checkpoint periodik: step 2 tersimpan (tiap 2 step)
        rec = load_checkpoint(d)
        self.assertEqual(rec["step"], 2)
        # messages checkpoint berakhir di hasil tool step 2
        self.assertEqual(rec["messages"][-1]["role"], "tool")
        self.assertIn("step2", rec["messages"][-1]["content"])

        # --- resume ---
        seen = {}

        def final_model(messages, model, provider_cfg, api_key=None,
                        tools=None):
            seen["n_messages"] = len(messages)
            seen["last_role"] = messages[-1]["role"]
            return {"content": "TIDAK ADA TEMUAN"}, {}

        loop2 = self._loop(d, max_steps=10,
                           resume_record=load_checkpoint(d))
        with mock.patch.object(LOOP, "call_model", side_effect=final_model), \
                mock.patch.object(LOOP, "MODEL_CALL_DELAY_S", 0):
            rc2 = loop2.run()
        self.assertEqual(rc2, 0)
        # pesan lengkap dari checkpoint diteruskan ke model (bukan dari nol)
        # elieve ships no built-in prompt: no system message when the
        # operator supplies none -> user+asst+tool+asst+tool
        self.assertEqual(seen["n_messages"], 5)
        self.assertEqual(seen["last_role"], "tool")
        # step berlanjut N+1
        with open(os.path.join(d, "progress.json"), encoding="utf-8") as f:
            prog = json.load(f)
        self.assertEqual(prog["step"], 3)
        self.assertEqual(prog["status"], "done")
        self.assertEqual(prog["resumed_from"], 2)

    def test_resume_restores_usage_tracker(self):
        d = _tmpdir()
        save_checkpoint(d, 5, _msgs(), state=_state())
        loop = self._loop(d, resume_record=load_checkpoint(d))
        totals = loop.acct.tracker.totals()
        self.assertEqual(
            totals["models"]["ag/gemini-3-flash"]["calls"], 2)
        self.assertEqual(totals["grand"]["prompt_tokens"], 100)

    def test_resume_strips_stale_task_block(self):
        stale = ("kebijakan sistem\n\n## Daftar task\n"
                 "0/1 selesai, aktif: Recon")
        self.assertEqual(LOOP._strip_tasks_block(stale), "kebijakan sistem")
        # tanpa blok -> tidak berubah
        self.assertEqual(LOOP._strip_tasks_block("bersih"), "bersih")

    def test_checkpoints_disabled_writes_nothing(self):
        d = _tmpdir()
        loop = self._loop(
            d, checkpoints_cfg={"enabled": False, "every_n_steps": 1},
            max_steps=3)

        def m(messages, model, provider_cfg, api_key=None, tools=None):
            return self._tool_msg(), {}

        with mock.patch.object(LOOP, "call_model", side_effect=m), \
                mock.patch.object(LOOP, "MODEL_CALL_DELAY_S", 0):
            loop.run()  # max_steps -> selesai tanpa jawaban akhir
        self.assertFalse(
            os.path.exists(os.path.join(d, "checkpoints")))

    def test_default_run_behavior_unchanged(self):
        """Tanpa config checkpoints baru: tidak ada file checkpoint untuk
        run pendek; OUT.md + progress.json tetap seperti semula."""
        d = _tmpdir()
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.ElieveLoop(
                task="t", outdir=d, model="ag/gemini-3-flash")

        def m(messages, model, provider_cfg, api_key=None, tools=None):
            return {"content": "SELESAI"}, {}

        with mock.patch.object(LOOP, "call_model", side_effect=m):
            rc = loop.run()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(d, "OUT.md")))
        with open(os.path.join(d, "progress.json"), encoding="utf-8") as f:
            prog = json.load(f)
        self.assertEqual(prog["status"], "done")
        self.assertNotIn("resumed_from", prog)
        self.assertFalse(
            os.path.exists(os.path.join(d, "checkpoints")))


class TestResumeCLI(unittest.TestCase):
    def test_list_checkpoints_exit_0(self):
        d = _tmpdir()
        save_checkpoint(d, 3, _msgs())
        rc = LOOP.main(["--list-checkpoints", "--outdir", d])
        self.assertEqual(rc, 0)

    def test_list_checkpoints_empty_exit_0(self):
        rc = LOOP.main(["--list-checkpoints", "--outdir", _tmpdir()])
        self.assertEqual(rc, 0)

    def test_list_checkpoints_needs_outdir(self):
        rc = LOOP.main(["--list-checkpoints"])
        self.assertEqual(rc, 2)

    def test_resume_corrupt_checkpoint_exits_nonzero(self):
        d = _tmpdir()
        os.makedirs(os.path.join(d, "checkpoints"))
        with open(os.path.join(d, "checkpoints", "ckpt-1.json"), "w") as f:
            f.write("{{{{corrupt")
        rc = LOOP.main(["--resume", d])
        self.assertNotEqual(rc, 0)

    def test_resume_no_checkpoint_exits_nonzero(self):
        rc = LOOP.main(["--resume", _tmpdir()])
        self.assertNotEqual(rc, 0)

    def test_resume_cli_full_run(self):
        """--resume end-to-end: task diambil dari checkpoint bila --task
        tidak diisi; loop lanjut dari step+1."""
        d = _tmpdir()
        save_checkpoint(d, 2, _msgs(), state=_state())

        def m(messages, model, provider_cfg, api_key=None, tools=None):
            return {"content": "SELESAI DARI RESUME"}, {}

        with mock.patch.object(LOOP, "get_api_key", return_value="k"), \
                mock.patch.object(LOOP, "call_model", side_effect=m), \
                mock.patch.object(LOOP, "MODEL_CALL_DELAY_S", 0):
            rc = LOOP.main(["--resume", d, "--model", "ag/gemini-3-flash"])
        self.assertEqual(rc, 0)
        with open(os.path.join(d, "progress.json"), encoding="utf-8") as f:
            prog = json.load(f)
        self.assertEqual(prog["step"], 3)
        self.assertEqual(prog["status"], "done")
        self.assertEqual(prog["task"], "audit plugin X")
        with open(os.path.join(d, "OUT.md"), encoding="utf-8") as f:
            self.assertIn("SELESAI DARI RESUME", f.read())


if __name__ == "__main__":
    unittest.main()
