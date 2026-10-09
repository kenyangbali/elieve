#!/usr/bin/env python3
"""Gap 5 (docs/GAP-AUDIT.md G5) — MCP client (Model Context Protocol).

Klien JSON-RPC 2.0 minimal tapi benar untuk menyambung tool eksternal
(browser automation, DB, API) ke loop Elieve tanpa menambah kode tool
manual satu per satu. Implementasi original; hanya memakai perilaku
protokol yang terdokumentasi publik.

Transport:
  - stdio: spawn `command` + `args`; JSON-RPC newline-delimited via
    stdin/stdout (stderr proses dibuang ke DEVNULL agar tak memblokir).
  - streamable-http: POST JSON-RPC ke `url` dengan
    Accept: application/json, text/event-stream; respons boleh JSON
    langsung atau SSE stream. Header `Mcp-Session-Id` diteruskan balik
    bila server memberinya.
  - sse (legacy, deprecated di spec): GET event stream dulu untuk
    mengambil endpoint message, lalu POST ke sana; respons dibaca dari
    stream yang sama. Butuh package `requests`.

Operasi: initialize, tools/list (dengan paginasi cursor), tools/call —
semua dengan timeout (default 30 dtk, configurable per server).

Kontrak kegagalan:
  - server mati / unreachable / timeout saat start -> dicatat ke stderr,
    server di-skip; server lain tetap jalan.
  - tools/call yang gagal -> STRING error sebagai observasi model
    (BUKAN exception, BUKAN crash).
  - satu server gagal tidak menggagalkan server lain.

Registrasi ke loop: bind_mcp(mcp_cfg) di elieve/tools/__init__.py —
tiap tool MCP terekspos sebagai `mcp__<server>__<tool>` di DISPATCH +
TOOL_SCHEMAS. unbind_mcp() melepas semuanya + mematikan proses stdio.
"""

import json
import os
import re
import select
import subprocess
import sys
import time
from urllib.parse import urljoin

DEFAULT_TIMEOUT_S = 30
PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "elieve"
CLIENT_VERSION = "0.1.0"

TRANSPORTS = ("stdio", "streamable-http", "sse")


class MCPError(Exception):
    """Kegagalan protokol/transport MCP (start, initialize, list, RPC)."""


# ---------------------------------------------------------------------------
# Nama tool + konversi skema
# ---------------------------------------------------------------------------

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_name(s):
    """Bersihkan nama agar aman dipakai sebagai nama tool function-calling."""
    s = _SANITIZE_RE.sub("_", str(s or "").strip())
    return s.strip("_") or "unnamed"


def mcp_tool_name(server_name, tool_name):
    """Nama tool ala Claude Code: mcp__<server>__<tool>."""
    return "mcp__{}__{}".format(
        sanitize_name(server_name), sanitize_name(tool_name))


def mcp_function_schema(tool_name, tool_def):
    """Konversi definisi tool MCP -> skema function-calling OpenAI-style.

    inputSchema MCP sudah JSON Schema; diteruskan apa adanya (format yang
    dipakai loop, lih. READ_SCHEMAS di elieve/tools/read.py).
    """
    tool_def = tool_def or {}
    params = tool_def.get("inputSchema") or {}
    if not isinstance(params, dict):
        params = {}
    params = dict(params)
    params.setdefault("type", "object")
    desc = tool_def.get("description") or tool_def.get("title") or ""
    desc = ("[MCP] " + str(desc)).strip()
    if len(desc) > 600:
        desc = desc[:597] + "..."
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": desc,
            "parameters": params,
        },
    }


def _content_blocks_to_text(content):
    """Ubah MCP content blocks -> string observasi untuk model."""
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "image":
            parts.append("[image {} — data dihilangkan]".format(
                block.get("mimeType", "unknown")))
        elif btype == "audio":
            parts.append("[audio {} — data dihilangkan]".format(
                block.get("mimeType", "unknown")))
        elif btype == "resource":
            res = block.get("resource") or {}
            parts.append("[resource: {}]".format(
                res.get("uri", "?") if isinstance(res, dict) else "?"))
        else:
            parts.append("[blok {} dihilangkan]".format(btype or "?"))
    return "\n".join(p for p in parts if p)


def _result_to_text(server_name, tool_name, result):
    """Ubah result tools/call -> string. isError pun jadi observasi."""
    if not isinstance(result, dict):
        return "(hasil non-dict dari {}/{})".format(server_name, tool_name)
    text = _content_blocks_to_text(result.get("content"))
    if result.get("isError"):
        return "MCP tool error: " + (text or "(tanpa pesan)")
    return text if text else "(hasil kosong)"


# ---------------------------------------------------------------------------
# Config loading: blok `mcp:` YAML + .mcp.json ala Claude Code
# ---------------------------------------------------------------------------

def _normalize_spec(name, raw):
    """Validasi satu spec server -> dict ternormalisasi. ValueError bila rusak."""
    if not isinstance(raw, dict):
        raise ValueError("spec harus object/dict")
    command = raw.get("command")
    command = str(command).strip() if command else ""
    url = raw.get("url")
    url = str(url).strip() if url else ""
    if command and url:
        raise ValueError(
            "isi salah satu: 'command' (stdio) ATAU 'url' (HTTP), bukan keduanya")
    if not command and not url:
        raise ValueError("butuh 'command' (stdio) atau 'url' (HTTP)")
    args = raw.get("args") or []
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("'args' harus list of string")
    transport = raw.get("transport")
    transport = str(transport).strip().lower() if transport else ""
    if transport and transport not in TRANSPORTS:
        raise ValueError("transport {!r} tak dikenal {}".format(
            transport, TRANSPORTS))
    if transport == "stdio" and not command:
        raise ValueError("transport 'stdio' butuh 'command'")
    if transport in ("streamable-http", "sse") and not url:
        raise ValueError("transport {!r} butuh 'url'".format(transport))
    timeout = raw.get("timeout_s")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise ValueError("'timeout_s' harus angka")
        if timeout <= 0:
            raise ValueError("'timeout_s' harus > 0")
    return {
        "name": name,
        "command": command or None,
        "args": list(args),
        "url": url or None,
        "transport": transport or None,  # None -> default by command/url
        "timeout_s": timeout,
    }


def load_server_specs(mcp_cfg, cwd=None):
    """Kumpulkan spec server dari blok `mcp:` YAML + `.mcp.json` di cwd.

    Format YAML:
        mcp:
          enabled: true
          timeout_s: 30
          servers:
            - {name: x, command: npx, args: [...]}   # stdio
            - {name: y, url: http://...}             # streamable-http
    Format .mcp.json (ala Claude Code):
        {"mcpServers": {"nama": {"command": ..., "args": [...]},
                        "lain": {"url": "..."}}}

    YAML menang bila nama tabrakan. Return (specs, warnings); spec rusak
    di-skip dengan warning (bukan crash).
    """
    cfg = mcp_cfg or {}
    if not cfg.get("enabled", True):
        return [], []
    specs = []
    warnings = []
    seen = set()

    def _add(source, raw_name, raw):
        name = str(raw_name or "").strip()
        if not name:
            warnings.append("{}: server tanpa nama — di-skip.".format(source))
            return
        if name in seen:
            warnings.append(
                "{}: nama {!r} duplikat — di-skip (yang pertama menang).".format(
                    source, name))
            return
        try:
            spec = _normalize_spec(name, raw or {})
        except ValueError as e:
            warnings.append(
                "{}: server {!r} tidak valid: {} — di-skip.".format(
                    source, name, e))
            return
        seen.add(name)
        specs.append(spec)

    entries = cfg.get("servers") or []
    if not isinstance(entries, list):
        warnings.append("mcp.servers: bukan list — di-skip.")
    else:
        for entry in entries:
            if not isinstance(entry, dict):
                warnings.append("mcp.servers: entri bukan dict — di-skip.")
                continue
            _add("mcp.servers", entry.get("name"), entry)

    mcpath = os.path.join(os.path.abspath(cwd or os.getcwd()), ".mcp.json")
    if os.path.isfile(mcpath):
        try:
            with open(mcpath, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            warnings.append(".mcp.json: gagal dibaca ({}) — di-skip.".format(e))
        else:
            servers = (data or {}).get("mcpServers") or {}
            if not isinstance(servers, dict):
                warnings.append(
                    ".mcp.json: 'mcpServers' bukan object — di-skip.")
            else:
                for name, raw in servers.items():
                    _add(".mcp.json", name, raw)

    return specs, warnings


# ---------------------------------------------------------------------------
# HTTP helpers (requests bila ada, urllib sebagai fallback)
# ---------------------------------------------------------------------------

def _header_lookup(headers, name):
    lname = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == lname:
            return v
    return None


def _http_post(url, headers, payload, timeout):
    """POST JSON. Return (status, headers_dict, body_bytes)."""
    data = json.dumps(payload).encode("utf-8")
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        r = requests.post(url, headers=headers, data=data, timeout=timeout)
        return r.status_code, dict(r.headers), r.content
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _parse_streamable_body(content_type, body):
    """Parse respons streamable-http: JSON langsung atau SSE stream."""
    if "text/event-stream" in (content_type or ""):
        for raw_line in body.decode("utf-8", "replace").splitlines():
            line = raw_line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    return json.loads(payload)
        raise MCPError("stream SSE kosong dari server")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as e:
        raise MCPError("respons HTTP bukan JSON: {}".format(e))


def _parse_sse_endpoint(lines, base_url):
    """Ambil endpoint message dari event stream SSE legacy.

    `lines`: iterable baris teks. Return absolute message URL atau None.
    """
    event = None
    data_buf = []
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            # baris kosong = dispatch satu event
            if data_buf:
                data_text = "\n".join(data_buf)
                if (event == "endpoint" or not event) and (
                        data_text.startswith("/")
                        or data_text.startswith("http")):
                    return urljoin(base_url, data_text)
            event, data_buf = None, []
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            data_buf = []
        elif line.startswith("data:"):
            data_buf.append(line[5:].strip())
    return None


# ---------------------------------------------------------------------------
# Pembaca baris berbasis fd + select (untuk stdio)
# ---------------------------------------------------------------------------

class _FdLineReader:
    """Readline dengan timeout di atas file descriptor (Linux)."""

    def __init__(self, fd):
        self.fd = fd
        self.buf = bytearray()

    def readline(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl])
                del self.buf[:nl + 1]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    "timeout ({}s) membaca stream".format(timeout))
            r, _, _ = select.select([self.fd], [], [], remaining)
            if not r:
                raise MCPError(
                    "timeout ({}s) membaca stream".format(timeout))
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            except OSError as e:
                raise MCPError("stream error: {}".format(e))
            if not chunk:
                raise MCPError("stream ditutup (proses mati?)")
            self.buf.extend(chunk)


# ---------------------------------------------------------------------------
# MCPServer — satu server, satu transport, lifecycle start/stop
# ---------------------------------------------------------------------------

class MCPServer:
    """Satu server MCP: config + transport + lifecycle.

    Pakai sebagai context manager atau panggil start()/stop() manual.
    initialize()/list_tools() melempar MCPError bila gagal (ditangani
    MCPClient.start_all -> stderr + skip). call_tool() TIDAK PERNAH
    melempar untuk kegagalan transport — mengembalikan string error.
    """

    def __init__(self, name, command=None, args=None, url=None,
                 transport=None, timeout_s=None):
        self.name = str(name)
        self.command = command
        self.args = list(args or [])
        self.url = url
        if transport:
            self.transport = transport
        else:
            self.transport = "stdio" if command else "streamable-http"
        if self.transport == "stdio" and not command:
            raise MCPError("transport 'stdio' butuh command")
        if self.transport in ("streamable-http", "sse") and not url:
            raise MCPError(
                "transport {!r} butuh url".format(self.transport))
        self.timeout = float(timeout_s) if timeout_s else DEFAULT_TIMEOUT_S
        self._proc = None
        self._reader = None
        self._id = 0
        self._session_id = None
        self._message_url = None   # legacy SSE
        self._sse_iter = None      # legacy SSE: iterator baris stream
        self._sse_resp = None      # legacy SSE: respons GET yang terbuka
        self.tools_cache = []

    # -- lifecycle ---------------------------------------------------

    def start(self):
        """Spawn (stdio) + initialize. MCPError bila gagal."""
        if self.transport == "stdio":
            cmd = [self.command] + self.args
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                raise MCPError(
                    "command tidak ditemukan: {!r}".format(self.command))
            except OSError as e:
                raise MCPError(
                    "gagal spawn {!r}: {}".format(self.command, e))
            os.set_blocking(self._proc.stdout.fileno(), False)
            self._reader = _FdLineReader(self._proc.stdout.fileno())
            time.sleep(0.2)  # beri napas; deteksi mati-cepat
            if self._proc.poll() is not None:
                raise MCPError(
                    "proses {!r} langsung exit (rc={})".format(
                        self.command, self._proc.returncode))
        return self.initialize()

    def stop(self):
        """Matikan proses stdio / tutup stream SSE. Best effort."""
        if self._sse_resp is not None:
            try:
                self._sse_resp.close()
            except Exception:
                pass
            self._sse_resp = None
            self._sse_iter = None
        proc, self._proc = self._proc, None
        self._reader = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        except Exception:
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- operasi protokol --------------------------------------------

    def _next_id(self):
        self._id += 1
        return self._id

    def initialize(self):
        """Handshake initialize + notifikasi initialized."""
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })
        try:
            self._notify("notifications/initialized", {})
        except MCPError:
            pass  # notifikasi = best effort
        return result or {}

    def list_tools(self):
        """tools/list dengan paginasi cursor. MCPError bila gagal."""
        tools = []
        cursor = None
        for _ in range(20):  # batas paginasi waras
            params = {}
            if cursor:
                params["cursor"] = cursor
            result = self._rpc("tools/list", params) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_cache = tools
        return tools

    def call_tool(self, tool_name, arguments=None):
        """tools/call. SELALU mengembalikan string (error -> string)."""
        args = arguments or {}
        try:
            json.dumps(args)
        except (TypeError, ValueError) as e:
            return "MCP error ({}/{}): argumen tak bisa di-JSON-kan: {}".format(
                self.name, tool_name, e)
        try:
            result = self._rpc("tools/call", {
                "name": tool_name,
                "arguments": args,
            }) or {}
        except Exception as e:
            return "MCP error ({}/{}): {}".format(self.name, tool_name, e)
        return _result_to_text(self.name, tool_name, result)

    # -- dispatch RPC -----------------------------------------------

    def _rpc(self, method, params=None):
        if self.transport == "stdio":
            return self._stdio_rpc(method, params)
        if self.transport == "sse":
            return self._sse_rpc(method, params)
        return self._streamable_rpc(method, params)

    def _notify(self, method, params=None):
        if self.transport == "stdio":
            data = (json.dumps({"jsonrpc": "2.0", "method": method,
                                "params": params or {}}) + "\n").encode()
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, AttributeError) as e:
                raise MCPError("gagal kirim notifikasi: {}".format(e))
            return
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        url = self._message_url or self.url
        status, _, _ = _http_post(
            url,
            headers,
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            self.timeout,
        )
        if status >= 400:
            raise MCPError("notifikasi HTTP {}".format(status))

    @staticmethod
    def _check_message(msg, rid, method):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            raise MCPError(
                "respons tak valid untuk {}: {!r}".format(method, msg))
        if msg.get("id") != rid:
            raise MCPError(
                "id respons tak cocok untuk {} (mau {}, dapat {})".format(
                    method, rid, msg.get("id")))
        if "error" in msg:
            err = msg["error"] or {}
            raise MCPError("JSON-RPC error {}: {}".format(
                err.get("code"), err.get("message")))
        return msg.get("result")

    # -- stdio -------------------------------------------------------

    def _stdio_write(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, AttributeError) as e:
            raise MCPError("stdio rusak (proses mati?): {}".format(e))

    def _stdio_rpc(self, method, params=None):
        rid = self._next_id()
        self._stdio_write({"jsonrpc": "2.0", "id": rid, "method": method,
                           "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    "timeout ({}s) menunggu respons '{}'".format(
                        self.timeout, method))
            try:
                raw = self._reader.readline(remaining)
            except MCPError as e:
                # bedakan tutup-stream vs timeout murni
                if "ditutup" in str(e) or "mati" in str(e):
                    raise MCPError(
                        "server stdio mati saat '{}': {}".format(method, e))
                raise MCPError(
                    "timeout ({}s) menunggu respons '{}'".format(
                        self.timeout, method))
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # baris log nyasar dari server — abaikan
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue  # notifikasi / respons basi — lewati
            return self._check_message(msg, rid, method)

    # -- streamable-http ----------------------------------------------

    def _streamable_rpc(self, method, params=None):
        rid = self._next_id()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            status, resp_headers, body = _http_post(
                self.url, headers,
                {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params or {}},
                self.timeout)
        except Exception as e:
            raise MCPError("HTTP request gagal ({}): {}".format(self.url, e))
        sid = _header_lookup(resp_headers, "mcp-session-id")
        if sid:
            self._session_id = sid
        if status == 404:
            raise MCPError("HTTP 404: {} bukan endpoint MCP?".format(self.url))
        if status >= 400:
            raise MCPError("HTTP {} dari {}: {!r}".format(
                status, self.url, body[:200]))
        ctype = _header_lookup(resp_headers, "content-type") or ""
        try:
            msg = _parse_streamable_body(ctype, body)
        except ValueError as e:
            raise MCPError("respons tak valid: {}".format(e))
        return self._check_message(msg, rid, method)

    # -- legacy SSE -----------------------------------------------------

    def _sse_ensure(self):
        """Buka GET event stream + ambil endpoint message (sekali saja)."""
        if self._message_url:
            return
        try:
            import requests
        except ImportError:
            raise MCPError(
                "transport 'sse' butuh package 'requests' (tak terinstal)")
        try:
            resp = requests.get(
                self.url,
                headers={"Accept": "text/event-stream"},
                stream=True,
                timeout=(10, self.timeout),
            )
        except Exception as e:
            raise MCPError("SSE GET gagal ({}): {}".format(self.url, e))
        if resp.status_code >= 400:
            raise MCPError("SSE GET HTTP {}".format(resp.status_code))
        endpoint = None
        try:
            deadline = time.monotonic() + self.timeout
            lines = []
            for line in resp.iter_lines(decode_unicode=True):
                lines.append(line)
                if time.monotonic() > deadline:
                    break
                if len(lines) > 500:
                    break
                ep = _parse_sse_endpoint(lines, self.url)
                if ep:
                    endpoint = ep
                    break
        except Exception as e:
            resp.close()
            raise MCPError("gagal baca SSE stream: {}".format(e))
        if not endpoint:
            resp.close()
            raise MCPError("SSE stream tak memberi endpoint message")
        self._message_url = endpoint
        self._sse_resp = resp
        self._sse_iter = resp.iter_lines(decode_unicode=True)

    def _sse_rpc(self, method, params=None):
        self._sse_ensure()
        rid = self._next_id()
        try:
            import requests
        except ImportError:
            raise MCPError(
                "transport 'sse' butuh package 'requests' (tak terinstal)")
        try:
            pr = requests.post(
                self._message_url,
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": rid, "method": method,
                      "params": params or {}},
                timeout=self.timeout,
            )
        except Exception as e:
            raise MCPError("SSE POST gagal: {}".format(e))
        if pr.status_code >= 400:
            raise MCPError("SSE POST HTTP {}".format(pr.status_code))
        # Respons JSON-RPC datang lewat stream GET yang masih terbuka.
        deadline = time.monotonic() + self.timeout
        buf_event, buf_data = None, []
        try:
            for line in self._sse_iter:
                if time.monotonic() > deadline:
                    raise MCPError(
                        "timeout ({}s) menunggu respons SSE '{}'".format(
                            self.timeout, method))
                text = (line or "").strip()
                if not text:
                    if buf_data:
                        try:
                            msg = json.loads("\n".join(buf_data))
                        except ValueError:
                            msg = None
                        if (isinstance(msg, dict)
                                and msg.get("id") == rid):
                            return self._check_message(msg, rid, method)
                    buf_event, buf_data = None, []
                    continue
                if text.startswith("event:"):
                    buf_event = text[6:].strip()
                elif text.startswith("data:"):
                    buf_data.append(text[5:].strip())
        except MCPError:
            raise
        except Exception as e:
            raise MCPError("SSE stream putus: {}".format(e))
        raise MCPError("SSE stream habis tanpa respons untuk '{}'".format(method))


# ---------------------------------------------------------------------------
# MCPClient — manajer banyak server
# ---------------------------------------------------------------------------

class MCPClient:
    """Kelola banyak MCPServer: tambah spec, start semua, kumpulkan tools."""

    def __init__(self, default_timeout_s=DEFAULT_TIMEOUT_S):
        self.default_timeout = default_timeout_s
        self.servers = {}    # name -> MCPServer (yang hidup)
        self.failures = []   # [{"server": name, "error": str}]

    def add_spec(self, spec):
        """Tambah satu spec (dict dari load_server_specs)."""
        name = spec["name"]
        if name in self.servers:
            raise MCPError("server {!r} sudah terdaftar".format(name))
        self.servers[name] = MCPServer(
            name=name,
            command=spec.get("command"),
            args=spec.get("args"),
            url=spec.get("url"),
            transport=spec.get("transport"),
            timeout_s=spec.get("timeout_s") or self.default_timeout,
        )

    def start_all(self):
        """Start + initialize + list_tools tiap server.

        Kegagalan satu server: dicatat ke stderr + self.failures, server
        di-skip; server lain tetap diproses. Tak pernah melempar.
        """
        for name in list(self.servers):
            srv = self.servers[name]
            try:
                srv.start()
                srv.list_tools()
            except Exception as e:
                self.failures.append({"server": name, "error": str(e)})
                sys.stderr.write(
                    "[mcp] server {!r} gagal: {} — di-skip.\n".format(
                        name, e))
                try:
                    srv.stop()
                except Exception:
                    pass
                del self.servers[name]

    def stop_all(self):
        """Matikan semua server. Best effort, tak pernah melempar."""
        for name, srv in list(self.servers.items()):
            try:
                srv.stop()
            except Exception as e:
                sys.stderr.write(
                    "[mcp] warning: stop server {!r}: {}\n".format(name, e))

    def entries(self):
        """Daftar tool siap registrasi: [{tool_name, schema, fn, server}]."""
        out = []
        for name, srv in self.servers.items():
            for tool in srv.tools_cache or []:
                if not isinstance(tool, dict):
                    continue
                tdef_name = str(tool.get("name") or "").strip()
                if not tdef_name:
                    continue
                mcp_name = mcp_tool_name(name, tdef_name)
                out.append({
                    "tool_name": mcp_name,
                    "schema": mcp_function_schema(mcp_name, tool),
                    "fn": _make_tool_fn(srv, tdef_name),
                    "server": name,
                })
        return out

    def __enter__(self):
        self.start_all()
        return self

    def __exit__(self, *exc):
        self.stop_all()
        return False


def _make_tool_fn(server, tool_name):
    """Bungkus server.call_tool jadi callable tool ala elieve.tools.

    Semua exception -> string observasi (loop menangkapnya sebagai hasil
    tool biasa). Output dipotong ke batas standar tools.
    """
    def _mcp_tool(**kwargs):
        try:
            from .tools import _truncate  # lazy: hindari circular import
            return _truncate(
                server.call_tool(tool_name, kwargs), note=" [mcp]")
        except Exception as e:
            return "MCP error ({}/{}): {}".format(
                server.name, tool_name, e)
    _mcp_tool.__name__ = "mcp_tool"
    _mcp_tool.__doc__ = "MCP tool {}/{} (server {!r}).".format(
        server.name, tool_name, server.name)
    return _mcp_tool


# ---------------------------------------------------------------------------
# Registrasi ke registry elieve.tools (DISPATCH + TOOL_SCHEMAS)
# ---------------------------------------------------------------------------

_BOUND = {"client": None, "names": []}


def bind_mcp(mcp_cfg, cwd=None):
    """Start server MCP terkonfigurasi + daftarkan tool-nya.

    Tiap tool terekspos sebagai `mcp__<server>__<tool>` di DISPATCH dan
    TOOL_SCHEMAS (elieve/tools/__init__.py). Tanpa config / tanpa server /
    semua server gagal -> NO-OP (return None): registry tak tersentuh,
    loop jalan persis seperti biasa.

    Return callable cleanup (stop server + unbind), atau None bila
    nothing terdaftar.
    """
    from .tools import DISPATCH, TOOL_SCHEMAS  # lazy: hindari circular import
    specs, warnings = load_server_specs(mcp_cfg, cwd=cwd)
    for w in warnings:
        sys.stderr.write("[mcp] {}\n".format(w))
    if not specs:
        return None
    client = MCPClient()
    for spec in specs:
        try:
            client.add_spec(spec)
        except Exception as e:
            sys.stderr.write(
                "[mcp] spec {!r} ditolak: {} — di-skip.\n".format(
                    spec.get("name"), e))
    if not client.servers:
        return None
    client.start_all()
    if not client.servers:
        return None  # semua server gagal — registry tak tersentuh
    names = []
    for entry in client.entries():
        tname = entry["tool_name"]
        if tname in DISPATCH:
            sys.stderr.write(
                "[mcp] nama tool {!r} tabrakan — di-skip.\n".format(tname))
            continue
        DISPATCH[tname] = entry["fn"]
        TOOL_SCHEMAS.append(entry["schema"])
        names.append(tname)
    if not names:
        client.stop_all()
        return None
    _BOUND["client"] = client
    _BOUND["names"] = names
    sys.stderr.write("[mcp] {} server aktif, {} tool terdaftar.\n".format(
        len(client.servers), len(names)))
    return unbind_mcp


def unbind_mcp():
    """Lepas semua tool mcp__* dari registry + matikan server. Idempoten."""
    from .tools import DISPATCH, TOOL_SCHEMAS  # lazy: hindari circular import
    names = _BOUND.get("names") or []
    doomed = set(names)
    for n in names:
        DISPATCH.pop(n, None)
    if doomed:
        TOOL_SCHEMAS[:] = [
            s for s in TOOL_SCHEMAS
            if s.get("function", {}).get("name") not in doomed
        ]
    _BOUND["names"] = []
    client = _BOUND.get("client")
    _BOUND["client"] = None
    if client is not None:
        client.stop_all()


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MCPClient",
    "MCPError",
    "MCPServer",
    "bind_mcp",
    "load_server_specs",
    "mcp_function_schema",
    "mcp_tool_name",
    "sanitize_name",
    "unbind_mcp",
]
