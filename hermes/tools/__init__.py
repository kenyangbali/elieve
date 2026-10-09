"""hermes.tools — sandboxed primitive tools.

Pattern: a small toolset (~6 capabilities) inside a tight sandbox.
Every tool raises ToolError on out-of-policy input — the main loop
catches it and feeds it back to the model as an observation.

Shared helpers (_resolve, _truncate, ToolError) live here so the
read/search/exec modules stay consistent.

Sandbox roots: the primary root is configurable via configure_roots()
(default: ./workspace resolved absolute from the process cwd);
/tmp is always allowed as a second root.
"""

import os

TMP_ROOT = "/tmp"
MAX_OUT_CHARS = 12000

WORKSPACE_ROOT = os.path.abspath("./workspace")


def get_workspace_root():
    """Return the currently configured primary sandbox root."""
    return WORKSPACE_ROOT


def configure_roots(workspace_root):
    """Set the primary sandbox root.

    The absolute path is stored and returned. /tmp remains allowed as a
    second root regardless of this setting.
    """
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = os.path.abspath(workspace_root)
    return WORKSPACE_ROOT


class ToolError(Exception):
    """Tool input invalid / outside the sandbox policy."""


def _under_allowed(real_path: str) -> bool:
    return (
        real_path == WORKSPACE_ROOT
        or real_path.startswith(WORKSPACE_ROOT + "/")
        or real_path == TMP_ROOT
        or real_path.startswith(TMP_ROOT + "/")
    )


def _resolve(path: str) -> str:
    """Force an absolute path inside the allowed roots; return real path."""
    if not os.path.isabs(path):
        raise ToolError(
            f"path must be ABSOLUTE, got: '{path}'. "
            f"Use {WORKSPACE_ROOT}/... or /tmp/..."
        )
    real = os.path.realpath(path)
    if not _under_allowed(real):
        raise ToolError(
            f"path outside allowed roots: '{path}' "
            f"(allowed: {WORKSPACE_ROOT} or {TMP_ROOT})."
        )
    return real


def _truncate(s: str, note: str = "") -> str:
    if len(s) > MAX_OUT_CHARS:
        return s[:MAX_OUT_CHARS] + f"\n...[truncated {len(s) - MAX_OUT_CHARS} chars{note}]"
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
    "WORKSPACE_ROOT",
    "TMP_ROOT",
    "MAX_OUT_CHARS",
    "configure_roots",
    "get_workspace_root",
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
