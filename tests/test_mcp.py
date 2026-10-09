#!/usr/bin/env python3
"""Tests untuk Gap 5 — elieve/mcp.py (MCP client).

Fake MCP server berupa script python kecil via stdio (initialize +
tools/list + tools/call minimal) + fake HTTP streamable server di thread.
Semua test lokal; tanpa network eksternal, tanpa LLM.
"""

import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr

from elieve import mcp
from elieve import tools as tools_pkg
from elieve.tools import DISPATCH, TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Fake MCP server via stdio (script python kecil)
# ---------------------------------------------------------------------------

FAKE_SERVER_SCRIPT = r'''
import sys, json, time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue  # baris sampah diabaikan
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"}}})
        elif method == "notifications/initialized":
            pass  # notifikasi: tanpa respons
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "echo", "description": "Echo back text",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
                {"name": "slow", "description": "Sleep then reply",
                 "inputSchema": {"type": "object",
                                 "properties": {"sleep_s": {"type": "number"}}}},
                {"name": "boom", "description": "Always fails",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "weird name!", "description": "Needs sanitize",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text",
                     "text": "echo: " + str(args.get("text", ""))}]}})
            elif name == "slow":
                time.sleep(float(args.get("sleep_s", 5)))
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": "slow done"}]}})
            elif name == "boom":
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "kaput"}]}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32602, "message": "unknown tool: " + str(name)}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "unknown method"}})

main()
'''

DEAD_SERVER_SCRIPT = "import sys; sys.exit(3)\n"


# ---------------------------------------------------------------------------
# Fake HTTP streamable MCP server (thread)
# ---------------------------------------------------------------------------

class _FakeHTTPHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeMCP/0.1"

    def _dispatch(self, req):
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "hecho", "description": "HTTP echo",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}}}}]}}
        if method == "tools/call":
            args = params.get("arguments") or {}
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text",
                 "text": "hecho: " + str(args.get("text", ""))}]}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "unknown method"}}

    def do_POST(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            req = {}
        if req.get("method", "").startswith("notifications/"):
            self.send_response(202)
            self.end_headers()
            return
        payload = json.dumps(self._dispatch(req)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Base dengan fake server stdio
# ---------------------------------------------------------------------------

class _StdioCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.script = os.path.join(cls.tmp.name, "fake_mcp.py")
        with open(cls.script, "w") as f:
            f.write(FAKE_SERVER_SCRIPT)
        cls.dead_script = os.path.join(cls.tmp.name, "dead_mcp.py")
        with open(cls.dead_script, "w") as f:
            f.write(DEAD_SERVER_SCRIPT)

    @classmethod
    def tearDownClass(cls):
        mcp.unbind_mcp()
        cls.tmp.cleanup()

    def tearDown(self):
        mcp.unbind_mcp()  # jangan bocorkan tool ke test lain

    def _spec(self, name="fakesrv", **kw):
        spec = {"name": name, "command": sys.executable,
                "args": [self.script]}
        spec.update(kw)
        return spec


# ---------------------------------------------------------------------------
# Nama + skema + config loading (tanpa proses)
# ---------------------------------------------------------------------------

class TestMCPNaming(unittest.TestCase):
    def test_tool_name_format(self):
        self.assertEqual(mcp.mcp_tool_name("srv", "echo"),
                         "mcp__srv__echo")

    def test_tool_name_sanitized(self):
        self.assertEqual(mcp.mcp_tool_name("my srv!", "weird name!"),
                         "mcp__my_srv__weird_name")

    def test_sanitize_empty(self):
        self.assertEqual(mcp.sanitize_name("!!!"), "unnamed")

    def test_schema_conversion(self):
        schema = mcp.mcp_function_schema("mcp__s__echo", {
            "name": "echo",
            "description": "Echo back",
            "inputSchema": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]},
        })
        self.assertEqual(schema["type"], "function")
        fn = schema["function"]
        self.assertEqual(fn["name"], "mcp__s__echo")
        self.assertIn("Echo back", fn["description"])
        self.assertEqual(fn["parameters"]["type"], "object")
        self.assertEqual(fn["parameters"]["required"], ["text"])

    def test_schema_defaults(self):
        schema = mcp.mcp_function_schema("mcp__s__x", {"name": "x"})
        self.assertEqual(
            schema["function"]["parameters"], {"type": "object"})

    def test_load_specs_yaml(self):
        specs, warnings = mcp.load_server_specs({
            "enabled": True,
            "servers": [
                {"name": "a", "command": "npx", "args": ["-y", "x"]},
                {"name": "b", "url": "http://localhost:1/mcp"},
                {"name": "rusak"},  # di-skip + warning
            ],
        }, cwd="/tmp")
        self.assertEqual([s["name"] for s in specs], ["a", "b"])
        self.assertEqual(len(warnings), 1)
        self.assertIsNone(specs[0]["transport"])  # default by command/url
        self.assertEqual(specs[0]["args"], ["-y", "x"])

    def test_load_specs_disabled(self):
        specs, _ = mcp.load_server_specs(
            {"enabled": False,
             "servers": [{"name": "a", "command": "x"}]})
        self.assertEqual(specs, [])

    def test_load_specs_mcp_json(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".mcp.json"), "w") as f:
                json.dump({"mcpServers": {
                    "fromjson": {"command": "npx", "args": ["y"]},
                    "httpjson": {"url": "http://localhost:2/mcp"},
                }}, f)
            specs, warnings = mcp.load_server_specs(None, cwd=d)
            names = sorted(s["name"] for s in specs)
            self.assertEqual(names, ["fromjson", "httpjson"])
            self.assertEqual(warnings, [])

    def test_load_specs_yaml_wins_collision(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".mcp.json"), "w") as f:
                json.dump({"mcpServers": {
                    "dup": {"command": "from-json"}}}, f)
            specs, warnings = mcp.load_server_specs({
                "servers": [{"name": "dup", "command": "from-yaml"}],
            }, cwd=d)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0]["command"], "from-yaml")
            self.assertTrue(any("duplikat" in w for w in warnings))

    def test_load_specs_bad_mcp_json(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".mcp.json"), "w") as f:
                f.write("{bukan json")
            specs, warnings = mcp.load_server_specs(None, cwd=d)
            self.assertEqual(specs, [])
            self.assertTrue(any(".mcp.json" in w for w in warnings))

    def test_sse_endpoint_parse(self):
        lines = [
            "event: endpoint",
            "data: /message?sessionId=abc-123",
            "",
        ]
        url = mcp._parse_sse_endpoint(lines, "http://h:1/sse")
        self.assertEqual(url, "http://h:1/message?sessionId=abc-123")
        self.assertIsNone(mcp._parse_sse_endpoint([": ping", ""], "http://h/"))


# ---------------------------------------------------------------------------
# stdio roundtrip
# ---------------------------------------------------------------------------

class TestMCPStdio(_StdioCase):
    def test_list_roundtrip(self):
        srv = mcp.MCPServer(name="fakesrv", command=sys.executable,
                            args=[self.script], timeout_s=10)
        try:
            srv.start()
            tools = srv.list_tools()
        finally:
            srv.stop()
        names = sorted(t["name"] for t in tools)
        self.assertEqual(names, ["boom", "echo", "slow", "weird name!"])

    def test_call_roundtrip(self):
        srv = mcp.MCPServer(name="fakesrv", command=sys.executable,
                            args=[self.script], timeout_s=10)
        try:
            srv.start()
            out = srv.call_tool("echo", {"text": "halo"})
        finally:
            srv.stop()
        self.assertEqual(out, "echo: halo")

    def test_call_iserror_is_observation(self):
        srv = mcp.MCPServer(name="fakesrv", command=sys.executable,
                            args=[self.script], timeout_s=10)
        try:
            srv.start()
            out = srv.call_tool("boom", {})
        finally:
            srv.stop()
        self.assertIn("kaput", out)
        self.assertIn("MCP tool error", out)

    def test_call_unknown_tool_error_string(self):
        srv = mcp.MCPServer(name="fakesrv", command=sys.executable,
                            args=[self.script], timeout_s=10)
        try:
            srv.start()
            out = srv.call_tool("takada", {})
        finally:
            srv.stop()
        self.assertIn("MCP error", out)
        self.assertIn("unknown tool", out)

    def test_call_timeout_returns_error_string(self):
        srv = mcp.MCPServer(name="fakesrv", command=sys.executable,
                            args=[self.script], timeout_s=1)
        try:
            srv.start()
            out = srv.call_tool("slow", {"sleep_s": 5})
        finally:
            srv.stop()
        self.assertIsInstance(out, str)
        self.assertIn("timeout", out.lower())

    def test_dead_server_start_raises(self):
        srv = mcp.MCPServer(name="dead", command=sys.executable,
                            args=[self.dead_script], timeout_s=5)
        with self.assertRaises(mcp.MCPError):
            srv.start()
        srv.stop()  # tak boleh melempar

    def test_missing_command_start_raises(self):
        srv = mcp.MCPServer(name="nope", command="/tak/ada/bin-xyz-123",
                            timeout_s=5)
        with self.assertRaises(mcp.MCPError):
            srv.start()

    def test_context_manager(self):
        with mcp.MCPServer(name="fakesrv", command=sys.executable,
                           args=[self.script], timeout_s=10) as srv:
            self.assertEqual(srv.call_tool("echo", {"text": "ctx"}),
                             "echo: ctx")
        self.assertIsNone(srv._proc)


# ---------------------------------------------------------------------------
# bind/unbind ke registry elieve.tools
# ---------------------------------------------------------------------------

class TestMCPBind(_StdioCase):
    def test_bind_registers_dispatch_and_schemas(self):
        err = io.StringIO()
        with redirect_stderr(err):
            cleanup = tools_pkg.bind_mcp(
                {"servers": [self._spec()]}, cwd="/tmp")
        try:
            self.assertIsNotNone(cleanup)
            self.assertIn("mcp__fakesrv__echo", DISPATCH)
            self.assertIn("mcp__fakesrv__slow", DISPATCH)
            self.assertIn("mcp__fakesrv__boom", DISPATCH)
            # nama aneh di-sanitize
            self.assertIn("mcp__fakesrv__weird_name", DISPATCH)
            names = [s["function"]["name"] for s in TOOL_SCHEMAS]
            self.assertIn("mcp__fakesrv__echo", names)
            # panggil via DISPATCH seperti loop melakukannya
            out = DISPATCH["mcp__fakesrv__echo"](text="via-dispatch")
            self.assertEqual(out, "echo: via-dispatch")
            # error tool -> string observasi, bukan exception
            out2 = DISPATCH["mcp__fakesrv__boom"]()
            self.assertIn("kaput", out2)
        finally:
            cleanup()
        self.assertNotIn("mcp__fakesrv__echo", DISPATCH)
        names = [s["function"]["name"] for s in TOOL_SCHEMAS]
        self.assertNotIn("mcp__fakesrv__echo", names)

    def test_bind_skips_dead_server_keeps_live(self):
        err = io.StringIO()
        with redirect_stderr(err):
            cleanup = tools_pkg.bind_mcp({"servers": [
                self._spec("live"),
                {"name": "dead", "command": sys.executable,
                 "args": [self.dead_script], "timeout_s": 5},
            ]}, cwd="/tmp")
        try:
            self.assertIsNotNone(cleanup)
            self.assertIn("mcp__live__echo", DISPATCH)
            self.assertNotIn("mcp__dead__echo", DISPATCH)
            self.assertIn("dead", err.getvalue())  # dicatat di stderr
        finally:
            cleanup()

    def test_bind_noop_without_config(self):
        before_dispatch = set(DISPATCH)
        before_schemas = len(TOOL_SCHEMAS)
        self.assertIsNone(tools_pkg.bind_mcp(None, cwd="/tmp"))
        self.assertIsNone(
            tools_pkg.bind_mcp({"enabled": False}, cwd="/tmp"))
        self.assertIsNone(
            tools_pkg.bind_mcp({"servers": []}, cwd="/tmp"))
        self.assertEqual(set(DISPATCH), before_dispatch)
        self.assertEqual(len(TOOL_SCHEMAS), before_schemas)

    def test_bind_all_dead_noop(self):
        err = io.StringIO()
        with redirect_stderr(err):
            cleanup = tools_pkg.bind_mcp({"servers": [
                {"name": "dead", "command": sys.executable,
                 "args": [self.dead_script], "timeout_s": 5},
            ]}, cwd="/tmp")
        self.assertIsNone(cleanup)
        self.assertNotIn("mcp__dead__echo", DISPATCH)

    def test_unbind_idempotent(self):
        tools_pkg.unbind_mcp()
        tools_pkg.unbind_mcp()  # tak boleh melempar


# ---------------------------------------------------------------------------
# HTTP streamable
# ---------------------------------------------------------------------------

class TestMCPHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeHTTPHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        mcp.unbind_mcp()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def tearDown(self):
        mcp.unbind_mcp()

    def test_streamable_list_call_roundtrip(self):
        url = "http://127.0.0.1:{}/mcp".format(self.port)
        srv = mcp.MCPServer(name="httpsrv", url=url, timeout_s=10)
        try:
            srv.start()
            tools = srv.list_tools()
            self.assertEqual([t["name"] for t in tools], ["hecho"])
            out = srv.call_tool("hecho", {"text": "hai"})
        finally:
            srv.stop()
        self.assertEqual(out, "hecho: hai")

    def test_unreachable_http_skipped(self):
        port = _free_port()  # pasti tak ada yang listen
        err = io.StringIO()
        with redirect_stderr(err):
            cleanup = tools_pkg.bind_mcp(
                {"servers": [{"name": "down",
                              "url": "http://127.0.0.1:{}/mcp".format(port),
                              "timeout_s": 3}]},
                cwd="/tmp")
        self.assertIsNone(cleanup)
        self.assertNotIn("mcp__down__hecho", DISPATCH)
        self.assertIn("down", err.getvalue())

    def test_http_404_is_mcp_error(self):
        srv = mcp.MCPServer(
            name="bad", url="http://127.0.0.1:{}/tak-ada".format(self.port),
            timeout_s=5)
        # path tak dikenal -> HTTP 404 -> MCPError, bukan crash.
        with self.assertRaises(mcp.MCPError):
            srv.start()
        srv.stop()


if __name__ == "__main__":
    unittest.main()
