"""Tool eksekusi shell dengan deny-list pola destruktif.

Catatan desain: v1 memakai daftar pola terlarang (regex) + validasi rm.
Fase 3 (docs/ARCHITECTURE.md) mengganti/memperkuat ini dengan
PermissionGate: classifier kilat 2 tahap sebelum perintah dieksekusi.
"""

import os
import re
import subprocess

from . import _truncate, _under_allowed, ToolError

EXEC_CWD = "/home/hatch/workspace"  # cwd dipaksa — exec tidak boleh keluar
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
            return False, f"DITOLAK: pola destruktif terdeteksi ({label})."
    m = re.search(r"\brm\s+((?:-[a-zA-Z]+\s+)*)(.*)", low)
    if m:
        flags, rest = m.group(1), m.group(2)
        recursive = "r" in flags.replace("-", "")
        targets = [t for t in re.split(r"\s+", rest.strip()) if t and not t.startswith("-")]
        for t in targets:
            if t in ("/", "/*", "~", "$home", ".", "./"):
                return False, "DITOLAK: rm menarget root/home/direktori kerja."
            if os.path.isabs(t) and not _under_allowed(os.path.realpath(t)):
                return False, f"DITOLAK: rm di luar area izin: {t}"
        if recursive and not targets:
            return False, "DITOLAK: rm rekursif tanpa target jelas."
    return True, ""


def exec(command, timeout=EXEC_TIMEOUT_S) -> str:  # noqa: A001 - nama tool publik
    if not command or not command.strip():
        raise ToolError("command kosong.")
    ok, reason = _exec_allowed(command)
    if not ok:
        raise ToolError(reason)
    try:
        r = subprocess.run(
            ["bash", "-c", command],
            cwd=EXEC_CWD,  # dipaksa — tidak bisa keluar dari workspace
            capture_output=True,
            text=True,
            timeout=int(timeout or EXEC_TIMEOUT_S),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"exec timeout ({timeout} dtk).")
    out = (r.stdout or "") + (r.stderr or "")
    if not out.strip():
        out = f"(tidak ada output; exit={r.returncode})"
    return _truncate(f"$ {command}\n[exit={r.returncode}, cwd={EXEC_CWD}]\n{out}")


EXEC_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Jalankan perintah bash. cwd SELALU /home/hatch/workspace. "
            "Pola destruktif (rm -rf /, mkfs, dsb.) DITOLAK.",
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
