"""Tests for hermes.providers (Area D: GLOBALIZED test work).

Covers: env key resolution (incl. no key material in errors), custom
base_url actually being used for the request URL (HTTP layer mocked),
model allow/forbid policy semantics, the 9router key_provider path
(temporary sqlite DB only — the real ~/.9router DB is never touched),
configs/*.yaml validity, and workspace_root behavior parity.

No test here touches the network or the real 9router DB. Dummy key
values used below are synthetic test fixtures, never real secrets.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import yaml

from hermes import providers
from hermes.providers import (
    ProviderConfig,
    ProviderError,
    ProviderKeyError,
    check_model_allowed,
    post_chat_completions,
    resolve_api_key,
)
from hermes import tools as _tools

DUMMY_KEY = "sk-test-dummy-key-not-real-00000000"

# ---------------------------------------------------------------------------
# env key resolution
# ---------------------------------------------------------------------------


class TestEnvKeyResolution(unittest.TestCase):
    VAR = "HERMES_TEST_API_KEY_D"

    def setUp(self):
        os.environ.pop(self.VAR, None)

    def tearDown(self):
        os.environ.pop(self.VAR, None)

    def test_env_var_resolved(self):
        os.environ[self.VAR] = DUMMY_KEY
        cfg = ProviderConfig(base_url="http://x/v1", api_key_env=self.VAR)
        self.assertEqual(resolve_api_key(cfg), DUMMY_KEY)

    def test_missing_env_var_clear_error(self):
        cfg = ProviderConfig(base_url="http://x/v1", api_key_env=self.VAR)
        with self.assertRaises(ProviderKeyError) as cm:
            resolve_api_key(cfg)
        msg = str(cm.exception)
        self.assertIn(self.VAR, msg)          # names WHERE to fix
        self.assertNotIn(DUMMY_KEY, msg)      # never carries key material

    def test_empty_env_var_clear_error(self):
        os.environ[self.VAR] = ""
        cfg = ProviderConfig(base_url="http://x/v1", api_key_env=self.VAR)
        with self.assertRaises(ProviderKeyError) as cm:
            resolve_api_key(cfg)
        self.assertNotIn(DUMMY_KEY, str(cm.exception))

    def test_no_key_source_configured_clear_error(self):
        cfg = ProviderConfig(base_url="http://x/v1")
        with self.assertRaises(ProviderKeyError) as cm:
            resolve_api_key(cfg)
        self.assertIn("api_key_env", str(cm.exception))

    def test_error_message_never_leaks_key_even_when_set(self):
        # A different (set) env var must not appear in any error text.
        os.environ["HERMES_TEST_OTHER_D"] = DUMMY_KEY
        self.addCleanup(os.environ.pop, "HERMES_TEST_OTHER_D", None)
        cfg = ProviderConfig(base_url="http://x/v1",
                             api_key_env="HERMES_TEST_UNSET_D")
        with self.assertRaises(ProviderKeyError) as cm:
            resolve_api_key(cfg)
        self.assertNotIn(DUMMY_KEY, str(cm.exception))


# ---------------------------------------------------------------------------
# custom base_url is actually used for the request URL
# ---------------------------------------------------------------------------


class TestBaseUrlUsed(unittest.TestCase):
    def test_custom_base_url_used(self):
        seen = {}

        def fake_post(url, headers, payload, timeout):
            seen["url"] = url
            seen["headers"] = headers
            seen["payload"] = payload
            return 200, '{"choices": []}'

        cfg = ProviderConfig(base_url="http://127.0.0.1:9999/v1",
                             api_key_env="HERMES_UNUSED_D")
        with mock.patch.object(providers, "_post_json_requests", fake_post):
            post_chat_completions(cfg, {"model": "m"}, api_key=DUMMY_KEY)
        self.assertEqual(seen["url"],
                         "http://127.0.0.1:9999/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"],
                         "Bearer " + DUMMY_KEY)
        self.assertEqual(seen["payload"]["model"], "m")

    def test_trailing_slash_no_double_slash(self):
        seen = {}

        def fake_post(url, headers, payload, timeout):
            seen["url"] = url
            return 200, "{}"

        cfg = ProviderConfig(base_url="https://example.com/v1/",
                             api_key_env="HERMES_UNUSED_D")
        with mock.patch.object(providers, "_post_json_requests", fake_post):
            post_chat_completions(cfg, {}, api_key=DUMMY_KEY)
        self.assertEqual(seen["url"],
                         "https://example.com/v1/chat/completions")
        self.assertNotIn("//chat", seen["url"].split("://", 1)[1])

    def test_missing_base_url_raises(self):
        cfg = ProviderConfig(api_key_env="HERMES_UNUSED_D")
        with mock.patch.object(providers, "_post_json_requests") as fake:
            with self.assertRaises(ProviderError):
                post_chat_completions(cfg, {}, api_key=DUMMY_KEY)
            fake.assert_not_called()


# ---------------------------------------------------------------------------
# model policy semantics
# ---------------------------------------------------------------------------


class TestModelPolicy(unittest.TestCase):
    def test_allow_nonempty_permits_match_forbids_rest(self):
        policy = {"allow": ["ag/*"], "forbid": []}
        self.assertEqual(check_model_allowed("ag/foo", policy), "ag/foo")
        with self.assertRaises(ValueError):
            check_model_allowed("other/bar", policy)

    def test_forbid_rejects_even_when_allow_empty(self):
        policy = {"allow": [], "forbid": ["bns/*", "oc/*"]}
        with self.assertRaises(ValueError):
            check_model_allowed("bns/x", policy)
        with self.assertRaises(ValueError):
            check_model_allowed("oc/y", policy)
        self.assertEqual(check_model_allowed("ag/z", policy), "ag/z")

    def test_forbid_wins_over_allow(self):
        policy = {"allow": ["ag/*"], "forbid": ["ag/evil"]}
        with self.assertRaises(ValueError):
            check_model_allowed("ag/evil", policy)
        self.assertEqual(check_model_allowed("ag/good", policy), "ag/good")

    def test_empty_policy_unrestricted(self):
        for policy in (None, {}, {"allow": [], "forbid": []}):
            self.assertEqual(check_model_allowed("anything/model", policy),
                             "anything/model")

    def test_violation_raises_valueerror_not_systemexit(self):
        with self.assertRaises(ValueError):
            check_model_allowed("bns/x",
                                {"allow": ["ag/*"], "forbid": ["bns/*"]})

    def test_empty_model_name_rejected(self):
        with self.assertRaises(ValueError):
            check_model_allowed("   ", {"allow": ["ag/*"]})

    def test_name_normalized_stripped(self):
        self.assertEqual(check_model_allowed("  ag/foo  ", {}), "ag/foo")

    def test_match_case_insensitive(self):
        policy = {"allow": ["AG/*"], "forbid": ["BNS/*"]}
        self.assertEqual(check_model_allowed("ag/foo", policy), "ag/foo")
        with self.assertRaises(ValueError):
            check_model_allowed("BNS/x", policy)


# ---------------------------------------------------------------------------
# 9router key_provider path — TEMPORARY sqlite DB only
# ---------------------------------------------------------------------------


def _make_temp_9router_db(key_value=None):
    """Create a temp sqlite DB with an apiKeys table; return its path."""
    fd, path = tempfile.mkstemp(prefix="hermes-test-9router-", suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE apiKeys (name TEXT, key TEXT)")
        if key_value is not None:
            con.execute("INSERT INTO apiKeys (name, key) VALUES (?, ?)",
                        ("Default Key", key_value))
        con.commit()
    finally:
        con.close()
    return path


class TestNineRouterKeyProvider(unittest.TestCase):
    def test_resolves_from_temp_db(self):
        db = _make_temp_9router_db(DUMMY_KEY)
        self.addCleanup(os.unlink, db)
        cfg = ProviderConfig(base_url="http://x/v1", key_provider="9router")
        with mock.patch.object(providers, "NINE_ROUTER_DB", db):
            self.assertEqual(resolve_api_key(cfg), DUMMY_KEY)

    def test_missing_row_clear_error_no_key_material(self):
        db = _make_temp_9router_db(None)  # table exists, row absent
        self.addCleanup(os.unlink, db)
        cfg = ProviderConfig(base_url="http://x/v1", key_provider="9router")
        with mock.patch.object(providers, "NINE_ROUTER_DB", db):
            with self.assertRaises(ProviderKeyError) as cm:
                resolve_api_key(cfg)
        msg = str(cm.exception)
        self.assertIn("Default Key", msg)
        self.assertNotIn(DUMMY_KEY, msg)

    def test_unreadable_db_clear_error(self):
        cfg = ProviderConfig(base_url="http://x/v1", key_provider="9router")
        with mock.patch.object(providers, "NINE_ROUTER_DB",
                               "/nonexistent/hermes-test-9router.sqlite"):
            with self.assertRaises(ProviderKeyError):
                resolve_api_key(cfg)

    def test_unknown_key_provider_rejected(self):
        cfg = ProviderConfig(base_url="http://x/v1",
                             key_provider="not-a-provider")
        with self.assertRaises(ProviderKeyError):
            resolve_api_key(cfg)


# ---------------------------------------------------------------------------
# configs/*.yaml validity
# ---------------------------------------------------------------------------


class TestConfigs(unittest.TestCase):
    def _load(self, name):
        path = os.path.join(REPO_ROOT, "configs", name)
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_bug_hunter_yaml_loads_and_has_core_keys(self):
        cfg = self._load("bug-hunter.yaml")
        for key in ("provider", "model_policy", "workspace_root", "language"):
            self.assertIn(key, cfg, f"missing key: {key}")
        self.assertEqual(cfg["language"], "id")

    def test_bug_hunter_policy_forbids_bns_oc_allows_ag(self):
        cfg = self._load("bug-hunter.yaml")
        policy = cfg["model_policy"]
        self.assertIn("bns/*", policy["forbid"])
        self.assertIn("oc/*", policy["forbid"])
        self.assertIn("ag/*", policy["allow"])
        with self.assertRaises(ValueError):
            check_model_allowed("bns/x", policy)
        with self.assertRaises(ValueError):
            check_model_allowed("oc/y", policy)
        self.assertEqual(check_model_allowed("ag/y", policy), "ag/y")
        # canonical config model itself must pass its own policy
        top_model = (cfg.get("model") or "").strip()
        self.assertEqual(check_model_allowed(top_model, policy), top_model)
        self.assertEqual(cfg["provider"]["model"], top_model)

    def test_example_yaml_loads_and_policy_unrestricted(self):
        cfg = self._load("example.yaml")
        policy = cfg.get("model_policy") or {}
        self.assertEqual(check_model_allowed("bns/x", policy), "bns/x")
        self.assertEqual(check_model_allowed("oc/y", policy), "oc/y")
        self.assertEqual(check_model_allowed("ag/z", policy), "ag/z")
        self.assertIn("provider", cfg)
        self.assertEqual(cfg["provider"]["base_url"],
                         "https://api.openai.com/v1")

    def test_bug_hunter_provider_config_roundtrip(self):
        cfg = self._load("bug-hunter.yaml")
        pcfg = ProviderConfig.from_dict(cfg["provider"])
        self.assertEqual(pcfg.key_provider, "9router")
        self.assertEqual(pcfg.chat_url(),
                         "http://127.0.0.1:20128/v1/chat/completions")
        d = pcfg.to_dict()
        self.assertNotIn(DUMMY_KEY, repr(d))
        for k in ("base_url", "api_key_env", "model", "timeout_s",
                  "key_provider"):
            self.assertIn(k, d)


# ---------------------------------------------------------------------------
# loop wiring parity (explicit policy, explicit model, ValueError)
# ---------------------------------------------------------------------------


class TestLoopModelWiring(unittest.TestCase):
    """HermesLoop requires an explicit model and validates it against the
    explicit model_policy; violations raise ValueError (not SystemExit)."""

    def setUp(self):
        self.var = "HERMES_LOOP_TEST_KEY_D"
        os.environ[self.var] = DUMMY_KEY
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-loop-test-")

    def tearDown(self):
        os.environ.pop(self.var, None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_loop(self, **kw):
        from hermes.loop import HermesLoop
        provider_cfg = kw.pop("provider_cfg", None) or ProviderConfig(
            base_url="http://127.0.0.1:9999/v1", api_key_env=self.var)
        return HermesLoop(task="t", outdir=self.tmpdir,
                          provider_cfg=provider_cfg, **kw)

    def test_allowed_model_accepted(self):
        loop = self._make_loop(model="ag/foo",
                               model_policy={"allow": ["ag/*"],
                                             "forbid": ["bns/*", "oc/*"]})
        self.assertEqual(loop.model, "ag/foo")

    def test_forbidden_model_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self._make_loop(model="bns/x",
                            model_policy={"allow": ["ag/*"],
                                          "forbid": ["bns/*", "oc/*"]})

    def test_no_model_anywhere_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self._make_loop(model=None, model_policy={})

    def test_provider_default_model_used_when_no_override(self):
        provider_cfg = ProviderConfig(base_url="http://x/v1",
                                      api_key_env=self.var,
                                      model="ag/gemini-3-flash")
        loop = self._make_loop(provider_cfg=provider_cfg,
                               model_policy={"allow": ["ag/*"]})
        self.assertEqual(loop.model, "ag/gemini-3-flash")

    def test_call_model_signature(self):
        # Area-A decision: call_model(messages, model, provider_cfg,
        # api_key=None, tools=None) returns (message, usage).
        import inspect
        from hermes.loop import call_model
        sig = inspect.signature(call_model)
        self.assertEqual(list(sig.parameters),
                         ["messages", "model", "provider_cfg", "api_key",
                          "tools"])


# ---------------------------------------------------------------------------
# workspace_root behavior parity
# ---------------------------------------------------------------------------


class TestWorkspaceRootParity(unittest.TestCase):
    def setUp(self):
        self.prev_root = _tools.get_workspace_root()

    def tearDown(self):
        _tools.configure_roots(self.prev_root)

    def test_default_root_is_cwd_dot_workspace_absolute(self):
        # No config: the tools default is abspath("./workspace") computed at
        # import time from the process cwd. A subprocess is used on purpose:
        # importlib.reload() of hermes.tools inside this process would
        # recreate module-level names (ToolError, ...) and break identity
        # checks (assertRaises) in other test modules.
        import subprocess
        with tempfile.TemporaryDirectory(prefix="hermes-root-test-") as td:
            out = subprocess.run(
                [sys.executable, "-c",
                 "import os, sys; "
                 "sys.path.insert(0, %r); "
                 "import hermes.tools as t; "
                 "print(t.get_workspace_root())" % REPO_ROOT],
                cwd=td, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(),
                             os.path.abspath(os.path.join(td, "workspace")))

    def test_configure_roots_stores_absolute(self):
        with tempfile.TemporaryDirectory(prefix="hermes-root-test-") as td:
            rel = os.path.join(td, "sub")
            got = _tools.configure_roots(rel)
            self.assertTrue(os.path.isabs(got))
            self.assertEqual(got, os.path.abspath(rel))
            self.assertEqual(_tools.get_workspace_root(), got)

    def test_resolve_rejects_outside_root(self):
        with tempfile.TemporaryDirectory(prefix="hermes-root-test-") as td:
            _tools.configure_roots(td)
            # inside root: fine
            inside = os.path.join(td, "file.txt")
            self.assertEqual(_tools._resolve(inside),
                             os.path.realpath(inside))
            # /tmp: always allowed as a second root
            tmpfile = os.path.join(tempfile.gettempdir(), "hermes-ok.txt")
            self.assertEqual(_tools._resolve(tmpfile),
                             os.path.realpath(tmpfile))
            # elsewhere: rejected (same behavior as configure_roots parity)
            with self.assertRaises(_tools.ToolError):
                _tools._resolve("/etc/hostname")
            # relative path: rejected (must be absolute)
            with self.assertRaises(_tools.ToolError):
                _tools._resolve("relative/path.txt")

    def test_resolve_rejects_traversal_escaping_root(self):
        # Needs a sandbox root OUTSIDE /tmp (which is always allowed).
        base = "/var/tmp"
        if not (os.path.isdir(base) and os.access(base, os.W_OK)):
            self.skipTest("/var/tmp not writable in this environment")
        outer = tempfile.mkdtemp(prefix="hermes-root-test-", dir=base)
        import shutil
        self.addCleanup(shutil.rmtree, outer, True)
        root = os.path.join(outer, "root")
        os.mkdir(root)
        _tools.configure_roots(root)
        # ../ escapes the root but stays under /var/tmp -> rejected.
        with self.assertRaises(_tools.ToolError):
            _tools._resolve(os.path.join(root, "..", "escape.txt"))
        # a sibling dir under the same outer dir is also outside the root.
        with self.assertRaises(_tools.ToolError):
            _tools._resolve(os.path.join(outer, "sibling.txt"))


if __name__ == "__main__":
    unittest.main()
