"""Unit tests for elieve.prompts — the emptiness contract.

Product decision (Bayu 2026-10-11): elieve ("Hermes, Anthropic-style")
ships with NO built-in system prompt, persona, soul.md, rules, or modes.
The framework is blank by design; the operator supplies their own prompt
— or none at all. get_system_prompt() is kept for backward compatibility
and ALWAYS returns "". When nothing is supplied, the loop sends NO
system message at all.

Run: cd ~/workspace/hermes-agent && python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import elieve.loop as LOOP  # noqa: E402
from elieve import prompts as PROMPTS  # noqa: E402
from elieve.prompts import get_system_prompt  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "elieve")


class TestAlwaysEmpty(unittest.TestCase):
    def test_empty_for_every_signature(self):
        # Old signatures (lang / workspace_root / profile) are all
        # accepted, all ignored — always "".
        self.assertEqual(get_system_prompt(), "")
        self.assertEqual(get_system_prompt("en"), "")
        self.assertEqual(get_system_prompt("id"), "")
        self.assertEqual(get_system_prompt("xx"), "")
        self.assertEqual(get_system_prompt("en", workspace_root="/x/y"), "")
        self.assertEqual(get_system_prompt("id", profile="hunter"), "")
        self.assertEqual(get_system_prompt("en", "/x/y", "default"), "")

    def test_no_other_exports(self):
        self.assertEqual(PROMPTS.__all__, ["get_system_prompt"])


class TestNoBuiltinPromptFiles(unittest.TestCase):
    def test_prompts_dir_has_no_content_files(self):
        d = os.path.dirname(PROMPTS.__file__)
        py_files = sorted(f for f in os.listdir(d) if f.endswith(".py"))
        self.assertEqual(py_files, ["__init__.py"],
                         f"built-in prompt files must not exist: {py_files}")

    def test_no_hunter_or_persona_machinery_in_package(self):
        # No hunter/persona/profile-mode machinery anywhere in elieve/.
        hits = []
        for root, dirs, files in os.walk(PKG):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(root, f)
                with open(p, encoding="utf-8") as fh:
                    src = fh.read().lower()
                # "profile" survives ONLY as the ignored backward-compat
                # kwarg name of get_system_prompt — not as a mechanism.
                if "hunter" in src:
                    hits.append(f"{p}: hunter")
        self.assertEqual(hits, [], f"built-in persona remnants: {hits}")

    def test_no_soul_file_anywhere_in_repo(self):
        hits = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
            for f in files:
                if "soul" in f.lower():
                    hits.append(os.path.join(root, f))
        self.assertEqual(hits, [], f"soul.md must not exist: {hits}")


class TestLoopSkipsEmptySystemMessage(unittest.TestCase):
    def _run_and_capture(self, outdir, pre=None, **kw):
        cfg = {"enabled": True, "max_tasks": 64, "summary_max_chars": 300}
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.ElieveLoop(task="t", outdir=outdir,
                                   model="ag/gemini-3-flash",
                                   tasks_cfg=cfg, **kw)
        if pre:
            pre(loop)
        captured = {}

        def fake_call(messages, model, provider_cfg, api_key=None,
                      tools=None):
            captured["messages"] = [dict(m) for m in messages]
            return ({"content": "done", "tool_calls": None},
                    {"prompt_tokens": 50})

        with mock.patch.object(LOOP, "call_model", side_effect=fake_call):
            rc = loop.run()
        return rc, captured["messages"]

    def test_loop_default_system_prompt_is_empty(self):
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.ElieveLoop(task="t", outdir=tempfile.mkdtemp(),
                                   model="ag/gemini-3-flash")
        self.assertEqual(loop.system_prompt, "")

    def test_no_system_message_when_prompt_empty(self):
        d = tempfile.mkdtemp()
        rc, messages = self._run_and_capture(d, system_prompt="")
        self.assertEqual(rc, 0)
        roles = [m["role"] for m in messages]
        self.assertNotIn("system", roles,
                         "empty prompt must not send a system message")
        self.assertEqual(messages[0]["role"], "user")

    def test_system_message_present_when_prompt_supplied(self):
        d = tempfile.mkdtemp()
        rc, messages = self._run_and_capture(d,
                                             system_prompt="You are Bob.")
        self.assertEqual(rc, 0)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("You are Bob.", messages[0]["content"])

    def test_task_block_creates_system_message_on_demand(self):
        # Empty operator prompt + tasks present: the per-turn task block
        # creates the system message (it carries the block), rather than
        # polluting the user message.
        d = tempfile.mkdtemp()

        def pre(loop):
            loop.tasks.add("Audit auth.py")

        rc, messages = self._run_and_capture(d, system_prompt="", pre=pre)
        self.assertEqual(rc, 0)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("## Daftar task", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "t")


class TestResumeSystemDetection(unittest.TestCase):
    """--resume must detect the system message by role, not position."""

    def _resume_and_capture(self, record):
        d = tempfile.mkdtemp()
        cfg = {"enabled": True, "max_tasks": 64, "summary_max_chars": 300}
        with mock.patch.object(LOOP, "get_api_key", return_value="k"):
            loop = LOOP.ElieveLoop(task="t", outdir=d,
                                   model="ag/gemini-3-flash",
                                   tasks_cfg=cfg, resume_record=record)
        captured = {}

        def fake_call(messages, model, provider_cfg, api_key=None,
                      tools=None):
            captured["messages"] = [dict(m) for m in messages]
            return ({"content": "done", "tool_calls": None},
                    {"prompt_tokens": 50})

        with mock.patch.object(LOOP, "call_model", side_effect=fake_call):
            rc = loop.run()
        return rc, captured["messages"]

    def test_resume_keeps_system_message(self):
        record = {
            "version": 1, "step": 1,
            "messages": [
                {"role": "system", "content": "You are Bob."},
                {"role": "user", "content": "t"},
                {"role": "assistant", "content": "hi"},
            ],
            "state": {},
        }
        rc, messages = self._resume_and_capture(record)
        self.assertEqual(rc, 0)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("You are Bob.", messages[0]["content"])

    def test_resume_without_system_message(self):
        # Checkpoint from a prompt-less run: messages[0] is the user task
        # and must NOT be mistaken for a system prompt.
        record = {
            "version": 1, "step": 1,
            "messages": [
                {"role": "user", "content": "t"},
                {"role": "assistant", "content": "hi"},
            ],
            "state": {},
        }
        rc, messages = self._resume_and_capture(record)
        self.assertEqual(rc, 0)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "t")


if __name__ == "__main__":
    unittest.main()
