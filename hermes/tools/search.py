"""Tool pencarian: grep (regex, pakai rg bila tersedia)."""

import shutil
import subprocess

from . import _resolve, _truncate, ToolError


def grep(pattern, path, case_insensitive=False) -> str:
    target = _resolve(path)
    if not pattern:
        raise ToolError("pattern kosong.")
    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--max-count", "20"]
        if case_insensitive:
            cmd.append("-i")
        cmd += ["--", pattern, target]
    else:
        cmd = ["grep", "-rn"]
        if case_insensitive:
            cmd.append("-i")
        cmd += ["--", pattern, target]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ToolError("grep timeout (60 dtk).")
    out = (r.stdout or "") + (r.stderr or "")
    lines = [line for line in out.splitlines() if line.strip()][:80]
    suffix = "\n...[hasil dipotong, >80 baris]" if len(out.splitlines()) > 80 else ""
    return _truncate(
        f"grep '{pattern}' di {target} -> {len(lines)} baris:\n"
        + "\n".join(lines)
        + suffix
    )


SEARCH_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search a regex pattern (uses rg when available) in a file/directory. "
            "Path MUST be absolute and under the configured workspace root or /tmp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "case_insensitive": {"type": "boolean"},
                },
                "required": ["pattern", "path"],
            },
        },
    },
]
