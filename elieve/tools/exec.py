"""Shell execution tool with a destructive-pattern deny-list.

Design note: v1 uses a forbidden-pattern list (regex) + rm validation.
Phase 3 (docs/ARCHITECTURE.md) strengthens this with the PermissionGate:
a 2-stage fast classifier before any command runs.
"""

import os
import re
import subprocess

from . import _truncate, _under_allowed, ToolError, get_workspace_root

EXEC_TIMEOUT_S = 120

DENY_RULES = [
    (r"\bmkfs\b", "mkfs (format filesystem)"),
    (r"\bdd\b[^|;]*\bof\s*=\s*/dev/", "dd menulis ke device"),
    (r":\(\)\s*\{\s*:\|\:&\s*\}\s*;", "fork bomb"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "shutdown/reboot/kontrol host"),
    (r">\s*/dev/(sd|hd|nvme|vd)", "redirect ke disk device"),
    (r"\|\s*(sh|bash|zsh)\b", "pipe ke shell"),
    (r"\bcurl\b[^|;]*\|\s*sh\b", "curl | sh"),
    (r"\bwget\b[^|;]*\|\s*sh\b", "wget | sh"),
]


def _exec_allowed(cmd: str):
    low = " " + cmd.lower() + " "
    for pat, label in DENY_RULES:
        if re.search(pat, low):
            return False, f"REJECTED: destructive pattern detected ({label})."
    m = re.search(r"\brm\s+((?:-[a-zA-Z]+\s+)*)(.*)", low)
    if m:
        flags, rest = m.group(1), m.group(2)
        recursive = "r" in flags.replace("-", "")
        targets = [t for t in re.split(r"\s+", rest.strip()) if t and not t.startswith("-")]
        for t in targets:
            if t in ("/", "/*", "~", "$home", ".", "./"):
                return False, "REJECTED: rm targeting root/home/working directory."
            if os.path.isabs(t) and not _under_allowed(os.path.realpath(t)):
                return False, f"REJECTED: rm outside allowed roots: {t}"
        if recursive and not targets:
            return False, "REJECTED: recursive rm without a clear target."
    return True, ""


def exec(command, timeout=EXEC_TIMEOUT_S) -> str:  # noqa: A001 - public tool name
    if not command or not command.strip():
        raise ToolError("empty command.")
    ok, reason = _exec_allowed(command)
    if not ok:
        raise ToolError(reason)
    cwd = get_workspace_root()  # forced — exec cannot leave the workspace
    if not os.path.isdir(cwd):
        raise ToolError(f"workspace root does not exist: {cwd}")
    try:
        r = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=int(timeout or EXEC_TIMEOUT_S),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"exec timeout ({timeout}s).")
    out = (r.stdout or "") + (r.stderr or "")
    if not out.strip():
        out = f"(no output; exit={r.returncode})"
    return _truncate(f"$ {command}\n[exit={r.returncode}, cwd={cwd}]\n{out}")


EXEC_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Run a bash command. cwd is ALWAYS the configured "
            "workspace root. Destructive patterns (rm -rf /, mkfs, etc.) "
            "are REJECTED.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "detik (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
]
