"""Tool primitif hermes-agent.

Pola: toolset kecil (~12 kapabilitas) yang di-sandbox ketat.
Setiap tool melempar ToolError bila input di luar izin — loop utama
menangkapnya dan mengumpankannya kembali ke model sebagai observasi.

Helper bersama (_resolve, _truncate, ToolError) tinggal di sini agar
modul read/search/exec konsisten.
"""

import os

WORKSPACE_ROOT = "/home/hatch/workspace"
TMP_ROOT = "/tmp"
MAX_OUT_CHARS = 12000


class ToolError(Exception):
    """Input tool tidak valid / di luar izin sandbox."""


def _under_allowed(real_path: str) -> bool:
    return (
        real_path == WORKSPACE_ROOT
        or real_path.startswith(WORKSPACE_ROOT + "/")
        or real_path == TMP_ROOT
        or real_path.startswith(TMP_ROOT + "/")
    )


def _resolve(path: str) -> str:
    """Paksa path absolut di dalam area izin; kembalikan real path."""
    if not os.path.isabs(path):
        raise ToolError(
            f"path harus ABSOLUT, dapat: '{path}'. "
            f"Pakai /home/hatch/workspace/... atau /tmp/..."
        )
    real = os.path.realpath(path)
    if not _under_allowed(real):
        raise ToolError(
            f"path di luar area izin: '{path}' "
            f"(hanya {WORKSPACE_ROOT} atau {TMP_ROOT} yang boleh)."
        )
    return real


def _truncate(s: str, note: str = "") -> str:
    if len(s) > MAX_OUT_CHARS:
        return s[:MAX_OUT_CHARS] + f"\n...[dipotong {len(s) - MAX_OUT_CHARS} char{note}]"
    return s


from .read import read_file, list_dir, READ_SCHEMAS  # noqa: E402
from .search import grep, SEARCH_SCHEMAS  # noqa: E402
from .exec import exec as run_exec, EXEC_SCHEMAS  # noqa: E402
from .memory import remember, REMEMBER_SCHEMAS, bind_memory, unbind_memory  # noqa: E402
from .tasks import (  # noqa: E402
    task_update, TASK_SCHEMAS, bind_tasks, unbind_tasks,
)

DISPATCH = {
    "read_file": read_file,
    "list_dir": list_dir,
    "grep": grep,
    "exec": run_exec,
    "remember": remember,
    "task_update": task_update,
}

TOOL_SCHEMAS = (
    READ_SCHEMAS + SEARCH_SCHEMAS + EXEC_SCHEMAS
    + REMEMBER_SCHEMAS + TASK_SCHEMAS
)

__all__ = [
    "ToolError",
    "DISPATCH",
    "TOOL_SCHEMAS",
    "read_file",
    "list_dir",
    "grep",
    "run_exec",
    "remember",
    "task_update",
    "bind_memory",
    "unbind_memory",
    "bind_tasks",
    "unbind_tasks",
]
