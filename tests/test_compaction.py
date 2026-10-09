"""Unit test Fase 1 — context compaction (4 lapis).

Jalan tanpa 9router / API key / network:
    cd ~/workspace/elieve && python3 -m unittest discover -s tests

Angka pada TestSimulation40Turn adalah SIMULASI LOKAL (heuristik chars/4),
bukan benchmark produksi.
"""

import copy
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elieve import compaction as C  # noqa: E402
from elieve.compaction import (  # noqa: E402
    ContextCompactor,
    apply_threshold_pipeline,
    estimate_tokens,
    full_compact,
    mask_observations,
    micro_compact,
    prune_middle,
)
from elieve.providers import ProviderConfig, check_model_allowed  # noqa: E402

# Policy equivalent of the old hardcoded "ag/* only, bns/*/oc/* rejected"
# rule — now threaded explicitly instead of baked into the code.
POLICY = {"allow": ["ag/*"], "forbid": ["bns/*", "oc/*"]}


def make_session(n_turns=10, tool_chars=5000, prefix_len=2):
    """Sesi sintetis: system + task + n_turns x (assistant+tool_call, tool)."""
    assert prefix_len == 2
    messages = [
        {"role": "system", "content": "SYSTEM PROMPT " * 40},
        {"role": "user", "content": "TASK: audit keamanan target X"},
    ]
    for i in range(n_turns):
        tcid = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Analisis langkah {i}: memeriksa file target.",
            "tool_calls": [{
                "id": tcid, "type": "function",
                "function": {"name": "read_file",
                             "arguments": json.dumps({"path": f"/tmp/f{i}.php"})},
            }],
        })
        messages.append({
            "role": "tool", "tool_call_id": tcid, "name": "read_file",
            "content": f"Hasil baca file nomor {i}\n" + ("x" * tool_chars),
        })
    return messages


def prefix_bytes(messages, n=2):
    return json.dumps(messages[:n], ensure_ascii=False, sort_keys=True)


class TestMicroCompact(unittest.TestCase):
    def test_old_tool_outputs_truncated(self):
        msgs = make_session(10, tool_chars=5000)
        out = micro_compact(msgs, recency_window=5, max_tool_chars=2000)
        # turn 0..4 (di luar window) -> pointer; turn 5..9 utuh
        for i in range(5):
            tool = out[2 + i * 2 + 1]
            self.assertEqual(tool["role"], "tool")
            self.assertTrue(tool["content"].startswith("[offloaded:"),
                            f"turn {i} tidak dipotong")
            self.assertLess(len(tool["content"]), 2000)
            # metadata pairing tetap ada
            self.assertEqual(tool["tool_call_id"], f"call_{i}")
        for i in range(5, 10):
            tool = out[2 + i * 2 + 1]
            self.assertIn("xxxx", tool["content"])

    def test_recency_window_untouched(self):
        msgs = make_session(10, tool_chars=5000)
        out = micro_compact(msgs, recency_window=5, max_tool_chars=2000)
        # 5 turn terakhir: assistant + tool verbatim
        self.assertEqual(out[-10:], msgs[-10:])

    def test_active_tool_call_protected(self):
        msgs = make_session(8, tool_chars=5000)
        # turn terakhir me-referensikan ulang tool_call_id turn 0 ("masih aktif")
        last_asst = dict(msgs[-2])
        last_asst["tool_calls"] = [{
            "id": "call_0", "type": "function",
            "function": {"name": "read_file",
                         "arguments": json.dumps({"path": "/tmp/f0.php"})},
        }]
        msgs[-2] = last_asst
        out = micro_compact(msgs, recency_window=5, max_tool_chars=2000)
        old_tool = out[3]  # tool result turn 0
        self.assertIn("xxxx", old_tool["content"],
                      "tool aktif (dirujuk turn terakhir) harus utuh")

    def test_prefix_untouched(self):
        msgs = make_session(6)
        before = prefix_bytes(msgs)
        out = micro_compact(msgs)
        self.assertEqual(prefix_bytes(out), before)

    def test_input_not_mutated(self):
        msgs = make_session(6)
        snapshot = copy.deepcopy(msgs)
        micro_compact(msgs)
        self.assertEqual(msgs, snapshot)

    def test_short_outputs_kept(self):
        msgs = make_session(6, tool_chars=100)
        out = micro_compact(msgs, max_tool_chars=2000)
        self.assertEqual(out, msgs)

    def test_stale_entries_dropped(self):
        msgs = make_session(8, tool_chars=100)
        old_ts = time.time() - 25 * 3600  # > 24 jam
        msgs[3] = dict(msgs[3], ts=old_ts)  # tool turn 0, zona tengah
        out = micro_compact(msgs, recency_window=5)
        self.assertEqual(len(out), len(msgs) - 1)
        self.assertNotIn("call_0", [m.get("tool_call_id") for m in out
                                    if m.get("role") == "tool"])


class TestEstimateTokens(unittest.TestCase):
    def test_heuristic_chars_per_4(self):
        msgs = [{"role": "user", "content": "x" * 400}]
        # 400/4=100 + overhead 8 per pesan
        self.assertEqual(estimate_tokens(msgs), 108)

    def test_empty(self):
        self.assertEqual(estimate_tokens([]), 0)


class TestThresholdPipeline(unittest.TestCase):
    def setUp(self):
        self.msgs = make_session(10, tool_chars=3000)

    def test_70pct_warning_only(self):
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=70 * 640, context_limit=64000)
        self.assertEqual(out, self.msgs)
        self.assertTrue(any(a.startswith("warning") for a in actions))

    def test_below_70_noop(self):
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=1000, context_limit=64000)
        self.assertEqual(out, self.msgs)
        self.assertEqual(actions, [])

    def test_80pct_observation_masking(self):
        before = prefix_bytes(self.msgs)
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=80 * 640 + 10, context_limit=64000)
        self.assertTrue(any("observation_masking" in a for a in actions))
        self.assertEqual(prefix_bytes(out), before)
        masked = [m for m in out[2:]
                  if m.get("role") == "tool"
                  and m.get("content") == "[offloaded to scratch]"]
        self.assertGreater(len(masked), 0)
        # pairing tool_call_id tetap ada
        self.assertTrue(all(m.get("tool_call_id") for m in masked))
        # 5 turn terakhir utuh
        self.assertEqual(out[-10:], self.msgs[-10:])

    def test_85pct_prune(self):
        before = prefix_bytes(self.msgs)
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=86 * 640, context_limit=64000)
        self.assertTrue(any("mask+prune" in a for a in actions))
        self.assertEqual(prefix_bytes(out), before)
        # assistant di zona tengah: content dikosongkan, tool_calls utuh
        mid_asst = out[2]  # assistant turn 0
        self.assertEqual(mid_asst["content"], "")
        self.assertTrue(mid_asst["tool_calls"])

    def test_90pct_aggressive_window_2(self):
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=91 * 640, context_limit=64000)
        self.assertTrue(any("aggressive_masking(window=2)" in a
                            for a in actions))
        masked = [m for m in out[2:]
                  if m.get("role") == "tool"
                  and m.get("content") == "[offloaded to scratch]"]
        # window=2 -> turn 0..7 termask (8 turn), bukan 5
        self.assertEqual(len(masked), 8)

    def test_95pct_full_compaction(self):
        calls = []

        def fake_summarizer(msgs):
            calls.append(True)
            return ([dict(m) for m in msgs[:2]]
                    + [{"role": "user", "content": "[COMPACTED] ringkasan"}]
                    + [dict(m) for m in msgs[-4:]])

        before = prefix_bytes(self.msgs)
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=96 * 640, context_limit=64000,
            summarizer=fake_summarizer)
        self.assertTrue(calls)
        self.assertTrue(any(a.startswith("full_compaction") for a in actions))
        self.assertEqual(prefix_bytes(out), before)
        self.assertIn("[COMPACTED]", out[2]["content"])

    def test_95pct_no_summarizer_fallback(self):
        out, actions = apply_threshold_pipeline(
            self.msgs, prompt_tokens=99 * 640, context_limit=64000,
            summarizer=None)
        self.assertTrue(any("fallback" in a for a in actions))
        masked = [m for m in out[2:]
                  if m.get("content") == "[offloaded to scratch]"]
        self.assertGreater(len(masked), 0)


class TestFullCompact(unittest.TestCase):
    def _fake_post(self, summary="Keputusan: pakai X.\nIssue: Y.\nState: Z."):
        def post(url, payload, api_key, timeout):
            # pastikan summarizer tidak membawa tools & model ag/*
            self.assertTrue(payload["model"].startswith("ag/"))
            self.assertNotIn("tools", payload)
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant",
                                          "content": summary}}]})
            return 200, body
        return post

    def test_structure(self):
        msgs = make_session(8, tool_chars=3000)
        before = prefix_bytes(msgs)
        out = full_compact(msgs, model="ag/gemini-3-flash", api_key="DUMMY",
                           keep_recent=5, post_fn=self._fake_post())
        # prefix verbatim + 1 boundary + 5 turn terakhir verbatim
        self.assertEqual(prefix_bytes(out), before)
        self.assertTrue(out[2]["content"].startswith("[COMPACTED]"))
        self.assertIn("Keputusan", out[2]["content"])
        self.assertEqual(out[3:], msgs[-10:])  # 5 turn x 2 pesan
        self.assertLess(len(out), len(msgs))

    def test_forbidden_model_rejected(self):
        msgs = make_session(4)
        for bad in ("bns/deepseek-v4.1-flash", "oc/muse-spark-1.3",
                    "gpt-4o"):
            with self.assertRaises(ValueError, msg=bad):
                full_compact(msgs, model=bad, api_key="DUMMY",
                             post_fn=self._fake_post(),
                             model_policy=POLICY)

    def test_empty_policy_allows_any_model(self):
        # No policy = unrestricted (the framework default).
        def post(url, payload, api_key, timeout):
            return 200, json.dumps(
                {"choices": [{"message": {"role": "assistant",
                                           "content": "ringkasan ok"}}]})
        msgs = make_session(4)
        out = full_compact(msgs, model="gpt-4o", api_key="DUMMY",
                           post_fn=post, model_policy={}, keep_recent=2)
        self.assertIn("[COMPACTED]", out[2]["content"])

    def test_nothing_to_compact_returns_copy(self):
        msgs = make_session(3, tool_chars=100)
        out = full_compact(msgs, model="ag/gemini-3-flash", api_key="DUMMY",
                           keep_recent=5, post_fn=self._fake_post())
        self.assertEqual(out, msgs)
        self.assertIsNot(out, msgs)

    def test_summary_must_cover_required_sections(self):
        captured = {}

        def post(url, payload, api_key, timeout):
            captured["prompt"] = payload["messages"][1]["content"]
            return 200, json.dumps(
                {"choices": [{"message": {"content": "ok"}}]})
        msgs = make_session(6)
        full_compact(msgs, model="ag/gemini-3-flash", api_key="DUMMY",
                     keep_recent=2, post_fn=post)
        for section in ("Keputusan", "Issue", "State implementasi",
                        "File yang sedang dikerjakan"):
            self.assertIn(section, captured["prompt"])


class TestPrefixPreservation(unittest.TestCase):
    """Lapis 4: prefix byte-identik setelah operasi apapun."""

    def test_all_layers_keep_prefix(self):
        msgs = make_session(10, tool_chars=4000)
        before = prefix_bytes(msgs)
        ops = [
            micro_compact(msgs),
            mask_observations(msgs),
            prune_middle(msgs),
            apply_threshold_pipeline(
                msgs, 90 * 640, 64000)[0],
            full_compact(msgs, "ag/gemini-3-flash", "DUMMY", keep_recent=5,
                         post_fn=lambda u, p, k, t: (
                             200, json.dumps({"choices": [
                                 {"message": {"content": "ringkas"}}]}))),
        ]
        for i, out in enumerate(ops):
            self.assertEqual(prefix_bytes(out), before, f"op {i} ubah prefix")


class TestContextCompactorClass(unittest.TestCase):
    def test_micro_only_without_usage(self):
        msgs = make_session(8, tool_chars=4000)
        comp = ContextCompactor()
        out, actions = comp.maybe_compact(msgs)
        self.assertEqual(actions, ["micro"])
        self.assertTrue(any(m.get("content", "").startswith("[offloaded:")
                            for m in out))

    def test_rejects_forbidden_cache_model(self):
        with self.assertRaises(ValueError):
            ContextCompactor(cache_model="bns/x", model_policy=POLICY)


class TestLoopIntegration(unittest.TestCase):
    def test_call_model_returns_message_and_usage(self):
        import elieve.loop as loopmod
        # call_model uses the name imported into elieve.loop's namespace
        orig = loopmod.post_chat_completions
        try:
            def fake_post(cfg, payload, api_key=None, timeout=None):
                assert "tools" in payload
                return 200, json.dumps({
                    "choices": [{"message": {"role": "assistant",
                                             "content": "halo"}}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 4},
                })
            loopmod.post_chat_completions = fake_post
            cfg = ProviderConfig(base_url="http://127.0.0.1:1/v1",
                                 api_key_env="ELIEVE_TEST_KEY")
            msg, usage = loopmod.call_model(
                [{"role": "user", "content": "hi"}], "ag/gemini-3-flash",
                cfg, api_key="K")
            self.assertEqual(msg["content"], "halo")
            self.assertEqual(usage["prompt_tokens"], 123)
        finally:
            loopmod.post_chat_completions = orig

    def test_compaction_config_block(self):
        import elieve.loop as loopmod
        cfg = loopmod.load_config(
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "configs", "bug-hunter.yaml"))
        comp = cfg.get("compaction") or {}
        self.assertTrue(comp.get("enabled"))
        self.assertEqual(comp["thresholds"], [70, 80, 85, 90, 95])
        self.assertTrue(comp["summarizer_model"].startswith("ag/"))
        # summarizer config harus lolos validasi policy dari YAML
        check_model_allowed(comp["summarizer_model"],
                            cfg.get("model_policy"))


class TestSimulation40Turn(unittest.TestCase):
    """Simulasi sesi 40-turn ala loop (micro per-turn). Angka = SIMULASI LOKAL."""

    def test_40_turn_savings(self):
        tool_chars = 5000
        raw = make_session(40, tool_chars=tool_chars)
        raw_tokens = estimate_tokens(raw)

        # simulasi loop: tiap turn -> micro_compact sebelum "request"
        sim = [
            {"role": "system", "content": "SYSTEM PROMPT " * 40},
            {"role": "user", "content": "TASK: audit keamanan target X"},
        ]
        for i in range(40):
            tcid = f"call_{i}"
            sim.append({
                "role": "assistant",
                "content": f"Analisis langkah {i}: memeriksa file target.",
                "tool_calls": [{
                    "id": tcid, "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": f"/tmp/f{i}.php"})},
                }],
            })
            sim.append({
                "role": "tool", "tool_call_id": tcid, "name": "read_file",
                "content": f"Hasil baca file nomor {i}\n" + ("x" * tool_chars),
            })
            sim = micro_compact(sim, recency_window=5, max_tool_chars=2000)
        compact_tokens = estimate_tokens(sim)

        ratio = compact_tokens / raw_tokens
        print(f"\n[simulasi lokal 40-turn] tanpa compaction: ~{raw_tokens} "
              f"token | dengan micro_compact: ~{compact_tokens} token "
              f"({ratio:.1%} dari awal, hemat {1 - ratio:.1%})")
        self.assertLess(ratio, 0.50,
                        "compaction harus memangkas < 50% token sesi 40-turn")


if __name__ == "__main__":
    unittest.main()
