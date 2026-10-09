# MCP client (Gap 5)

Klien Model Context Protocol di `elieve/mcp.py` — cara standar menyambung
tool eksternal (browser automation, DB, API) ke loop tanpa menambah kode
tool manual satu per satu.

## Konfigurasi

Blok `mcp:` di YAML (lihat `configs/bug-hunter.yaml`):

```yaml
mcp:
  enabled: true
  timeout_s: 30        # timeout per operasi (initialize/list/call)
  servers:
    - name: playwright              # stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-playwright"]
    - name: api-internal            # HTTP (default: streamable-http)
      url: http://127.0.0.1:8000/mcp
      # transport: sse              # opsional: legacy SSE (butuh `requests`)
```

File `.mcp.json` di cwd (format ala Claude Code) juga dibaca:

```json
{"mcpServers": {
  "playwright": {"command": "npx", "args": ["-y", "..."]},
  "api": {"url": "http://127.0.0.1:8000/mcp"}
}}
```

YAML menang bila nama server tabrakan. Tanpa blok `mcp:` dan tanpa
`.mcp.json`, `bind_mcp()` adalah no-op — loop jalan persis seperti biasa
(tidak ada tool tambahan, tidak ada proses di-spawn).

## Nama tool

Tiap tool server terekspos sebagai `mcp__<server>__<tool>`
(mis. `mcp__playwright__browser_navigate`). Nama dibersihkan ke
`[A-Za-z0-9_-]`; tabrakan nama di-skip dengan peringatan di stderr.
Skema input JSON Schema MCP diteruskan apa adanya ke format
function-calling yang dipakai loop.

Tool MCP lewat jalur normal: PreToolUse hook → permission gate →
eksekusi → PostToolUse hook, sama seperti tool bawaan.

## Kontrak kegagalan

- Server mati / unreachable / timeout saat start → dicatat ke stderr,
  server di-skip; server lain tetap jalan.
- `tools/call` yang gagal (termasuk `isError: true` dari server) →
  **string** error sebagai observasi model, bukan exception/crash.
- Proses stdio dimatikan rapi saat run selesai (`finally` di `main()`),
  di semua jalur keluar termasuk `SystemExit`.

## Batasan

- `tools/list` paginasi dibatasi 20 halaman.
- Transport `sse` (legacy, deprecated di spec) butuh package `requests`;
  `streamable-http` dan `stdio` jalan dengan stdlib saja.
- MCP server berjalan dengan hak proses loop — hanya daftarkan server
  yang dipercaya; permission gate tetap berlaku per tool call.

## Test

`tests/test_mcp.py` — fake server stdio (script python inline) + fake
HTTP streamable (thread): list/call roundtrip, timeout → string error,
server mati/unreachable → skip + stderr, konversi nama
`mcp__<server>__<tool>`, bind/unbind registry, loading `.mcp.json`.
