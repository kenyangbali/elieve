"""Unit test Gap 1 — hook lifecycle system (hermes/hooks.py).

Jalan tanpa 9router / API key / network (get_api_key & call_model di-mock
untuk integrasi loop):
    cd ~/workspace/hermes-agent && python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.hooks import (  # noqa: E402
    EVENTS,
    HOOK_ACTIONS,
    HookRunner,
    block_destructive_exec,
    checkpoint_state,
    log_error,
    persist_state,
    qc_tool_output,
)
import hermes.loop as LOOP  # noqa: E402


def _tmpdir():
    return tempfile.mkdtemp(prefix="hooks-test-")


class TestBuiltinActions(unittest.TestCase):
    """Aksi bawaan deterministik (tanpa LLM)."""

    def test_block_destructive_exec_rm_rf(self):
        ok, reason = block_destructive_exec(
            {"tool_name": "exec", "tool_args": {"command": "rm -rf /"}})
        self.assertFalse(ok)
        self.assertIn("rm", reason.lower())

    def test_block_destructive_exec_variants(self):
        for cmd in ("sudo mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda",
                    "tar cf - . | dd of=/dev/null",
                    ":(){ :|:& };:",
                    "rm -fr ~/workspace"):
            ok, _ = block_destructive_exec(
                {"tool_name": "exec", "tool_args": {"command": cmd}})
            self.assertFalse(ok, f"harus diblokir: {cmd}")

    def test_block_destructive_exec_allows_safe(self):
        for cmd in ("ls -la /tmp", "grep -r foo /home/hatch/workspace"):
            ok, _ = block_destructive_exec(
                {"tool_name": "exec", "tool_args": {"command": cmd}})
            self.assertTrue(ok, f"harus lolos: {cmd}")

    def test_block_destructive_exec_ignores_other_tools(self):
        ok, reason = block_destructive_exec(
            {"tool_name": "read_file",
             "tool_args": {"path": "/tmp/rm -rf tidak relevan"}})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_qc_tool_output_oversize(self):
        res = qc_tool_output({"tool_output": "x" * 12001})
        self.assertIn("output_oversize:12001", res["qc_flags"])

    def test_qc_tool_output_traceback(self):
        res = qc_tool_output(
            {"tool_output": "File a.py, line 1\nTraceback (most recent call)"})
        self.assertIn("traceback_found", res["qc_flags"])

    def test_qc_tool_output_clean(self):
        self.assertEqual(qc_tool_output({"tool_output": "ok"})["qc_flags"],
                         [])

    def test_checkpoint_state_writes_file(self):
        d = _tmpdir()
        res = checkpoint_state({"outdir": d, "step": 7,
                                "tasks_summary": "recon done"})
        path = os.path.join(d, ".precompact.json")
        self.assertEqual(res["checkpoint"], path)
        with open(path) as f:
            rec = json.load(f)
        self.assertEqual(rec["step"], 7)
        self.assertEqual(rec["tasks_summary"], "recon done")
        self.assertIn("ts", rec)

    def test_persist_state_writes_file(self):
        d = _tmpdir()
        res = persist_state({"outdir": d, "step": 40, "status": "done",
                             "task": "audit x", "note": ""})
        path = os.path.join(d, ".final_state.json")
        self.assertEqual(res["persisted"], path)
        with open(path) as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["step"], 40)

    def test_log_error_appends_jsonl(self):
        d = _tmpdir()
        log_error({"outdir": d, "step": 3, "error": "boom"})
        with open(os.path.join(d, "errors.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["error"], "boom")
        self.assertEqual(rec["step"], 3)


class TestHookRunnerConfig(unittest.TestCase):
    """Validasi config: error jelas; kosong -> no-op."""

    def test_unknown_event_raises_clear(self):
        with self.assertRaises(ValueError) as cm:
            HookRunner({"SessionStart": [{"action": "block_destructive_exec"}]})
        self.assertIn("SessionStart", str(cm.exception))
        self.assertIn("tidak dikenal", str(cm.exception))

    def test_unknown_action_raises_clear(self):
        with self.assertRaises(ValueError) as cm:
            HookRunner({"PreToolUse": [{"action": "tak_ada_action_ini"}]})
        self.assertIn("tak_ada_action_ini", str(cm.exception))
        self.assertIn("tidak terdaftar", str(cm.exception))

    def test_bad_spec_raises_clear(self):
        with self.assertRaises(ValueError) as cm:
            HookRunner({"PreToolUse": [{"foo": 1}]})
        self.assertIn("tidak valid", str(cm.exception))

    def test_empty_config_is_noop(self):
        for cfg in (None, {}):
            r = HookRunner(cfg, outdir=_tmpdir())
            self.assertFalse(r.has("PreToolUse"))
            self.assertEqual(r.pre_tool_use({"step": 1}), (True, []))
            self.assertEqual(r.post_tool_use({"step": 1}),
                             {"qc_flags": [], "blocked": False})
            self.assertEqual(r.pre_compact({"step": 1}), {})
            self.assertEqual(r.on_stop({"step": 1, "status": "done"}), {})

    def test_string_spec_accepted_as_action_name(self):
        r = HookRunner({"PreToolUse": ["block_destructive_exec"]})
        ok, _ = r.pre_tool_use(
            {"tool_name": "exec", "tool_args": {"command": "rm -rf /"}})
        self.assertFalse(ok)


class TestShellActions(unittest.TestCase):
    """Aksi shell: env, timeout, kegagalan tidak crash-kan run."""

    def test_shell_receives_env(self):
        d = _tmpdir()
        r = HookRunner({"PostToolUse": [{"shell": "echo $HERMES_EVENT"}]},
                       outdir=d)
        res = r.post_tool_use({"step": 5})
        # shell sukses -> tidak ada flag, tidak diblokir
        self.assertEqual(res, {"qc_flags": [], "blocked": False})

    def test_shell_failure_nonblocking_does_not_block(self):
        r = HookRunner({"PreToolUse": [{"shell": "exit 3"}]},
                       outdir=_tmpdir())
        ok, reasons = r.pre_tool_use({"step": 1})
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_shell_failure_blocking_blocks_pre_tool_use(self):
        r = HookRunner(
            {"PreToolUse": [{"shell": "exit 3", "blocking": True}]},
            outdir=_tmpdir())
        ok, reasons = r.pre_tool_use({"step": 1})
        self.assertFalse(ok)
        self.assertTrue(any("shell hook blocking gagal" in x
                            for x in reasons))

    def test_shell_timeout_recorded_not_crash(self):
        r = HookRunner(
            {"PreToolUse": [{"shell": "sleep 30", "timeout": 1}]},
            outdir=_tmpdir())
        ok, reasons = r.pre_tool_use({"step": 1})
        self.assertTrue(ok)  # non-blocking -> run lanjut
        self.assertEqual(reasons, [])

    def test_shell_timeout_blocking_blocks(self):
        r = HookRunner(
            {"PreToolUse": [{"shell": "sleep 30", "timeout": 1,
                             "blocking": True}]},
            outdir=_tmpdir())
        ok, _ = r.pre_tool_use({"step": 1})
        self.assertFalse(ok)


class TestLoopIntegration(unittest.TestCase):
    """Hook terpasang di titik tepat loop ReAct."""

    def _loop(self, hooks_cfg, **kw):
        outdir = _tmpdir()
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.HermesLoop(task="tugas uji", outdir=outdir,
                                   hooks_cfg=hooks_cfg, **kw)
        return loop

    def _msg(self, content="TIDAK ADA TEMUAN", tool_calls=None):
        m = {"content": content}
        if tool_calls is not None:
            m["tool_calls"] = tool_calls
        return m

    def test_pre_tool_use_blocks_rm_rf_in_loop(self):
        loop = self._loop(
            {"PreToolUse": [{"action": "block_destructive_exec"}]})
        result = loop._dispatch_tool(
            "exec", {"command": "rm -rf /"}, )
        self.assertTrue(result.startswith("HOOK DITOLAK"))
        self.assertIn("rm -rf", result)

    def test_pre_tool_use_allows_safe_exec(self):
        loop = self._loop(
            {"PreToolUse": [{"action": "block_destructive_exec"}]})
        result = loop._dispatch_tool("exec", {"command": "echo halo"})
        self.assertIn("halo", result)

    def test_post_tool_use_qc_flags_recorded(self):
        loop = self._loop(
            {"PostToolUse": [{"action": "qc_tool_output"}]})
        result = loop._dispatch_tool("exec", {"command": "echo ok"})
        self.assertIn("ok", result)  # jalur tool tetap jalan normal
        # qc hook membaca output tool; verifikasi via ctx manual:
        big = "y" * 12001
        post = loop.hooks.post_tool_use(
            {"tool_name": "exec", "tool_output": big, "step": 1})
        self.assertIn("output_oversize:12001", post["qc_flags"])

    def _run_with_mocked_model(self, loop, msg_or_exc, max_steps=3):
        with mock.patch.object(LOOP, "call_model") as cm:
            if isinstance(msg_or_exc, Exception):
                cm.side_effect = msg_or_exc
            else:
                cm.return_value = (msg_or_exc, {})
            rc = loop.run()
        return rc

    def test_on_stop_fired_on_done(self):
        loop = self._loop({"OnStop": [{"action": "persist_state"}]})
        rc = self._run_with_mocked_model(loop, self._msg())
        self.assertEqual(rc, 0)
        path = os.path.join(loop.outdir, ".final_state.json")
        with open(path) as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["step"], 1)

    def test_on_stop_fired_on_max_steps(self):
        loop = self._loop({"OnStop": [{"action": "persist_state"}]},
                          max_steps=1)
        tc = [{"id": "c1",
               "function": {"name": "read_file",
                            "arguments": json.dumps(
                                {"path": os.path.join(
                                    os.path.dirname(os.path.abspath(
                                        __file__)), "..", "README.md")})}}]
        rc = self._run_with_mocked_model(loop, self._msg(tool_calls=tc))
        self.assertEqual(rc, 0)
        with open(os.path.join(loop.outdir, ".final_state.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "max_steps")

    def test_on_stop_and_on_error_fired_on_error(self):
        loop = self._loop({"OnStop": [{"action": "persist_state"}],
                           "OnError": [{"action": "log_error"}]})
        rc = self._run_with_mocked_model(loop, RuntimeError("boom"))
        self.assertEqual(rc, 1)
        with open(os.path.join(loop.outdir, ".final_state.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "error")
        with open(os.path.join(loop.outdir, "errors.jsonl")) as f:
            line = f.readline()
        self.assertIn("boom", line)

    def test_on_stop_fired_on_rate_limited(self):
        loop = self._loop({"OnStop": [{"action": "persist_state"}]})
        rc = self._run_with_mocked_model(
            loop, LOOP.RateLimited("HTTP 429"))
        self.assertEqual(rc, 0)
        with open(os.path.join(loop.outdir, ".final_state.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "rate_limited")

    def test_pre_compact_writes_checkpoint(self):
        loop = self._loop(
            {"PreCompact": [{"action": "checkpoint_state"}]},
            max_steps=2)
        tc = [{"id": "c1",
               "function": {"name": "read_file",
                            "arguments": json.dumps(
                                {"path": os.path.join(
                                    os.path.dirname(os.path.abspath(
                                        __file__)), "..", "README.md")})}}]
        # turn 1: tool call (compaction pipeline jalan -> PreCompact fire),
        # turn 2: jawaban akhir.
        with mock.patch.object(LOOP, "call_model") as cm:
            cm.side_effect = [(self._msg(tool_calls=tc), {}),
                              (self._msg(), {})]
            loop.run()
        path = os.path.join(loop.outdir, ".precompact.json")
        with open(path) as f:
            rec = json.load(f)
        self.assertIn("step", rec)
        self.assertIn("ts", rec)

    def test_failing_hook_does_not_crash_run(self):
        HOOK_ACTIONS["__test_boom__"] = lambda ctx: 1 / 0
        try:
            loop = self._loop(
                {"PreToolUse": [{"action": "__test_boom__"}],
                 "PostToolUse": [{"action": "__test_boom__"}]})
            # PreToolUse fail-closed -> blokir, tapi run TIDAK raise
            result = loop._dispatch_tool("read_file",
                                         {"path": "/tmp/x"})
            self.assertTrue(result.startswith("HOOK DITOLAK"))
            self.assertIn("fail-closed", result)
            # full run tetap selesai normal
            rc = self._run_with_mocked_model(loop, self._msg())
            self.assertEqual(rc, 0)
            with open(os.path.join(loop.outdir, "errors.jsonl")) as f:
                content = f.read()
            self.assertIn("__test_boom__", content)
        finally:
            del HOOK_ACTIONS["__test_boom__"]


if __name__ == "__main__":
    unittest.main()
