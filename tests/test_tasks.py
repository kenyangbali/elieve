"""Unit test Gap 2 — structured task tracking (hermes/tasks.py).

Jalan tanpa 9router / API key / network (get_api_key di-mock untuk
integrasi loop):
    cd ~/workspace/hermes-agent && python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.tasks import TaskList  # noqa: E402
from hermes import tools as T  # noqa: E402
from hermes.tools import tasks as TT  # noqa: E402
from hermes.compaction import micro_compact, mask_observations  # noqa: E402
import hermes.loop as LOOP  # noqa: E402


def fresh_tasks(**kw):
    d = tempfile.mkdtemp()
    return TaskList(os.path.join(d, "tasks.json"), **kw), d


class TestTaskListRoundtrip(unittest.TestCase):
    def test_add_set_list(self):
        tl, _ = fresh_tasks()
        i0 = tl.add("Audit auth.py")
        i1 = tl.add("Cek open redirect")
        self.assertEqual((i0, i1), (0, 1))
        tl.set_status(0, "in_progress")
        tl.set_status("Cek open redirect", "completed")
        got = tl.list()
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0], {"title": "Audit auth.py",
                                 "status": "in_progress"})
        self.assertEqual(got[1], {"title": "Cek open redirect",
                                 "status": "completed"})

    def test_set_by_numeric_string_index(self):
        tl, _ = fresh_tasks()
        tl.add("Satu")
        tl.set_status("0", "completed")
        self.assertEqual(tl.list()[0]["status"], "completed")

    def test_list_returns_copies(self):
        tl, _ = fresh_tasks()
        tl.add("Satu")
        got = tl.list()
        got[0]["status"] = "completed"
        self.assertEqual(tl.list()[0]["status"], "pending")

    def test_persist_reload_from_disk(self):
        tl, d = fresh_tasks()
        tl.add("Audit auth.py")
        tl.add("Cek CORS")
        tl.set_status(1, "in_progress")
        path = os.path.join(d, "tasks.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("tasks", raw)
        self.assertEqual(len(raw["tasks"]), 2)
        # tulis atomik: tidak ada file tmp sisa
        self.assertEqual([f for f in os.listdir(d)
                          if f.startswith(".tasks-")], [])
        # instance baru membaca ulang dari disk
        tl2 = TaskList(path)
        self.assertEqual(tl2.list(), tl.list())
        self.assertEqual(tl2.summary(), tl.summary())

    def test_corrupt_json_starts_empty(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "tasks.json")
        with open(path, "w") as f:
            f.write("{bukan json")
        tl = TaskList(path)
        self.assertEqual(tl.list(), [])
        self.assertEqual(tl.summary(), "")
        # tetap bisa dipakai setelah rusak
        tl.add("Baru")
        self.assertEqual(len(tl.list()), 1)

    def test_missing_file_starts_empty(self):
        tl, _ = fresh_tasks()
        self.assertEqual(tl.list(), [])
        self.assertEqual(tl.summary(), "")


class TestTaskListValidation(unittest.TestCase):
    def test_invalid_status_rejected(self):
        tl, _ = fresh_tasks()
        tl.add("Satu")
        for bad in ("done", "DONE ", "", "running", None):
            with self.assertRaises(ValueError, msg=f"status={bad!r}"):
                tl.set_status(0, bad)
        self.assertEqual(tl.list()[0]["status"], "pending")

    def test_invalid_target_rejected(self):
        tl, _ = fresh_tasks()
        tl.add("Satu")
        with self.assertRaises(ValueError):
            tl.set_status(5, "completed")
        with self.assertRaises(ValueError):
            tl.set_status("tidak ada", "completed")

    def test_empty_title_rejected(self):
        tl, _ = fresh_tasks()
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                tl.add(bad)

    def test_duplicate_title_rejected(self):
        tl, _ = fresh_tasks()
        tl.add("Audit auth.py")
        with self.assertRaises(ValueError):
            tl.add("audit AUTH.py")  # case-insensitive
        self.assertEqual(len(tl.list()), 1)

    def test_max_tasks_enforced(self):
        tl, _ = fresh_tasks(max_tasks=2)
        tl.add("Satu")
        tl.add("Dua")
        with self.assertRaises(ValueError):
            tl.add("Tiga")
        self.assertEqual(len(tl.list()), 2)


class TestSummary(unittest.TestCase):
    def test_summary_format(self):
        tl, _ = fresh_tasks()
        tl.add("Satu")
        tl.add("Dua")
        tl.add("Tiga")
        tl.set_status(0, "in_progress")
        tl.set_status(2, "completed")
        self.assertEqual(tl.summary(), "1/3 selesai, aktif: Satu")

    def test_summary_no_active(self):
        tl, _ = fresh_tasks()
        tl.add("Satu")
        tl.add("Dua")
        tl.set_status(0, "completed")
        self.assertEqual(tl.summary(), "1/2 selesai")

    def test_summary_truncated(self):
        tl, _ = fresh_tasks()
        tl.add("A" * 500)
        tl.set_status(0, "in_progress")
        s = tl.summary(max_chars=50)
        self.assertLessEqual(len(s), 50)

    def test_format_list(self):
        tl, _ = fresh_tasks()
        self.assertEqual(tl.format_list(), "(belum ada task)")
        tl.add("Satu")
        tl.set_status(0, "in_progress")
        self.assertEqual(tl.format_list(), "0. [in_progress] Satu")


def _conv_with_stateful_results():
    """Riwayat: 2 turn lama (grep + task_update, konten panjang) + 1 turn baru."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "a",
         "tool_calls": [{"id": "c1",
                         "function": {"name": "task_update",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "task_update",
         "content": "[task] ditambahkan #0: 'Audit auth.py' (pending)." * 20},
        {"role": "assistant", "content": "b",
         "tool_calls": [{"id": "c2",
                         "function": {"name": "grep",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "grep",
         "content": "y" * 3000},
        {"role": "assistant", "content": "recent",
         "tool_calls": [{"id": "c3",
                         "function": {"name": "read_file",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c3", "name": "read_file",
         "content": "short"},
    ]


class TestCompactionProtectsTaskState(unittest.TestCase):
    def test_micro_compact_skips_task_update(self):
        msgs = _conv_with_stateful_results()
        out = micro_compact(msgs, recency_window=1, max_tool_chars=2000)
        by_id = {m.get("tool_call_id"): m for m in out
                 if m.get("role") == "tool"}
        # grep lama -> dipotong jadi pointer
        self.assertIn("[offloaded:", by_id["c2"]["content"])
        # task_update lama -> VERBATIM, tidak dipotong
        self.assertEqual(by_id["c1"]["content"], msgs[3]["content"])
        self.assertIn("Audit auth.py", by_id["c1"]["content"])

    def test_mask_observations_skips_task_update(self):
        msgs = _conv_with_stateful_results()
        out = mask_observations(msgs, recency_window=1)
        by_id = {m.get("tool_call_id"): m for m in out
                 if m.get("role") == "tool"}
        self.assertEqual(by_id["c2"]["content"], "[offloaded to scratch]")
        self.assertEqual(by_id["c1"]["content"], msgs[3]["content"])


class TestTaskUpdateTool(unittest.TestCase):
    def test_schema_registered(self):
        names = [s["function"]["name"] for s in T.TOOL_SCHEMAS]
        self.assertIn("task_update", names)
        self.assertIn("task_update", T.DISPATCH)
        schema = next(s for s in T.TOOL_SCHEMAS
                      if s["function"]["name"] == "task_update")
        params = schema["function"]["parameters"]["properties"]
        self.assertEqual(params["action"]["enum"], ["add", "set", "list"])
        self.assertEqual(params["status"]["enum"],
                         ["pending", "in_progress", "completed"])

    def test_tool_roundtrip(self):
        tl, _ = fresh_tasks()
        TT.bind_tasks(tl)
        try:
            out = T.DISPATCH["task_update"](action="add",
                                             title="Audit auth.py")
            self.assertIn("ditambahkan #0", out)
            out = T.DISPATCH["task_update"](action="set", title="0",
                                             status="in_progress")
            self.assertIn("in_progress", out)
            out = T.DISPATCH["task_update"](action="list")
            self.assertIn("0. [in_progress] Audit auth.py", out)
            self.assertEqual(tl.summary(), "0/1 selesai, aktif: Audit auth.py")
        finally:
            TT.unbind_tasks()

    def test_tool_unbound_raises(self):
        TT.unbind_tasks()
        with self.assertRaises(T.ToolError):
            T.DISPATCH["task_update"](action="list")

    def test_tool_invalid_inputs(self):
        tl, _ = fresh_tasks()
        TT.bind_tasks(tl)
        try:
            with self.assertRaises(T.ToolError):
                T.DISPATCH["task_update"](action="add")  # tanpa title
            with self.assertRaises(T.ToolError):
                T.DISPATCH["task_update"](action="set", title="x")  # tanpa status
            with self.assertRaises(T.ToolError):
                T.DISPATCH["task_update"](action="set", title="x",
                                           status="done")  # status invalid
            with self.assertRaises(T.ToolError):
                T.DISPATCH["task_update"](action="hapus")  # action invalid
        finally:
            TT.unbind_tasks()


class TestLoopIntegration(unittest.TestCase):
    def _make_loop(self, outdir, **kw):
        cfg = {"enabled": True, "max_tasks": 64, "summary_max_chars": 300}
        cfg.update(kw)
        with mock.patch.object(LOOP, "get_api_key", return_value="test-key"):
            return LOOP.HermesLoop(task="t", outdir=outdir,
                                   model="ag/gemini-3-flash", tasks_cfg=cfg)

    def _run_once(self, loop):
        captured = {}

        def fake_call(messages, model, provider_cfg, api_key=None,
                      tools=None):
            captured["system"] = messages[0]["content"]
            captured["tool_schemas"] = [s["function"]["name"]
                                       for s in (tools or [])]
            return ({"content": "TIDAK ADA TEMUAN", "tool_calls": None},
                    {"prompt_tokens": 50})

        with mock.patch.object(LOOP, "call_model", side_effect=fake_call):
            rc = loop.run()
        return rc, captured

    def test_summary_injected_into_system_prompt(self):
        d = tempfile.mkdtemp()
        loop = self._make_loop(d)
        loop.tasks.add("Audit auth.py")
        loop.tasks.add("Cek CORS")
        loop.tasks.set_status(0, "in_progress")
        rc, captured = self._run_once(loop)
        self.assertEqual(rc, 0)
        self.assertIn("## Daftar task", captured["system"])
        self.assertIn("0/2 selesai, aktif: Audit auth.py", captured["system"])
        self.assertIn("task_update", captured["tool_schemas"])
        # tool ter-bind selama run
        self.assertIsNotNone(TT._TASKS)
        # persist ke tasks.json
        with open(os.path.join(d, "tasks.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["tasks"]), 2)
        # hook ctx membawa ringkasan nyata (bukan placeholder)
        self.assertEqual(loop._hook_ctx_base()["tasks_summary"],
                         "0/2 selesai, aktif: Audit auth.py")

    def test_no_tasks_section_when_empty(self):
        d = tempfile.mkdtemp()
        loop = self._make_loop(d)
        rc, captured = self._run_once(loop)
        self.assertEqual(rc, 0)
        self.assertNotIn("## Daftar task", captured["system"])

    def test_tasks_disabled(self):
        d = tempfile.mkdtemp()
        loop = self._make_loop(d, enabled=False)
        self.assertIsNone(loop.tasks)
        self.assertEqual(loop._tasks_block(), "")
        self.assertIsNone(TT._TASKS)
        rc, captured = self._run_once(loop)
        self.assertEqual(rc, 0)
        self.assertNotIn("## Daftar task", captured["system"])

    def test_tasks_yaml_block_parsed(self):
        cfg = LOOP.load_config(
            os.path.join(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))),
                "configs", "bug-hunter.yaml"))
        self.assertIn("tasks", cfg)
        self.assertTrue(cfg["tasks"]["enabled"])
        self.assertEqual(cfg["tasks"]["max_tasks"], 64)
        self.assertEqual(cfg["tasks"]["summary_max_chars"], 300)


if __name__ == "__main__":
    unittest.main()
