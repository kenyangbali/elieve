"""Tool baca filesystem: read_file dan list_dir."""

import os

from . import _resolve, _truncate, ToolError


def read_file(path, offset=1, limit=200) -> str:
    target = _resolve(path)
    if not os.path.isfile(target):
        raise ToolError(f"bukan file: {path}")
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 200), 500))
    sel = lines[offset - 1 : offset - 1 + limit]
    out = "".join(f"{i:5d}: {l}" for i, l in enumerate(sel, start=offset))
    return _truncate(
        f"file: {target} (baris {offset}-{offset + len(sel) - 1} dari {len(lines)})\n{out}"
    )


def list_dir(path) -> str:
    target = _resolve(path)
    if not os.path.isdir(target):
        raise ToolError(f"bukan direktori: {path}")
    entries = sorted(os.listdir(target))
    lines = []
    for e in entries[:300]:
        full = os.path.join(target, e)
        lines.append(("[DIR] " if os.path.isdir(full) else "[FILE] ") + e)
    extra = f"\n... +{len(entries) - 300} entri lain" if len(entries) > 300 else ""
    return f"dir: {target} ({len(entries)} entri)\n" + "\n".join(lines) + extra


READ_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Baca file teks. Path WAJIB absolut dan di bawah /home/hatch/workspace atau /tmp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "baris mulai (1-based)"},
                    "limit": {"type": "integer", "description": "jumlah baris (maks 500)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List isi direktori. Path WAJIB absolut dan di bawah /home/hatch/workspace atau /tmp.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]
