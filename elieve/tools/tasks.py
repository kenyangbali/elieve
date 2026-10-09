"""Tool task tracking: `task_update` — kelola daftar task run ini.

Tool ini butuh TaskList yang di-bind oleh loop utama sebelum run
(`bind_tasks`). Tanpa binding, tool menolak (ToolError) agar agent
mendapat umpan balik yang jelas, bukan crash diam-diam.

Pola persis mengikuti elieve/tools/memory.py (bind/unbind per-run).
"""

from . import ToolError

_TASKS = None


def bind_tasks(task_list):
    """Sambungkan tool task_update ke TaskList milik run yang aktif."""
    global _TASKS
    _TASKS = task_list


def unbind_tasks():
    global _TASKS
    _TASKS = None


def task_update(action, title=None, status=None) -> str:
    """Kelola daftar task terstruktur run ini.

    action="add":   tambah task (title wajib) -> status awal pending.
    action="set":   ubah status (title = judul ATAU nomor index; status =
                   pending | in_progress | completed).
    action="list":  tampilkan seluruh daftar.

    Hasil tool ini dikecualikan dari pemotongan micro_compact
    (elieve/compaction.py STATEFUL_TOOL_NAMES) agar state task survive.
    """
    if _TASKS is None:
        raise ToolError("daftar task belum di-bind ke run ini (hubungi operator).")
    action = str(action or "").strip().lower()
    if action == "add":
        if not str(title or "").strip():
            raise ToolError("action 'add' butuh 'title'.")
        try:
            idx = _TASKS.add(title)
        except ValueError as e:
            raise ToolError(str(e))
        return f"[task] ditambahkan #{idx}: '{title}' (pending)."
    if action == "set":
        if title is None or not str(status or "").strip():
            raise ToolError("action 'set' butuh 'title' dan 'status'.")
        try:
            t = _TASKS.set_status(title, status)
        except ValueError as e:
            raise ToolError(str(e))
        return f"[task] '{t['title']}' -> {t['status']}."
    if action == "list":
        return "[task] daftar:\n" + _TASKS.format_list()
    raise ToolError(
        f"action '{action}' tidak dikenal. Pakai: add | set | list."
    )


TASK_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": (
                "Kelola daftar task terstruktur sesi ini (checklist kerja: "
                "pending/in_progress/completed). Pakai 'add' untuk menambah "
                "task, 'set' untuk mengubah status (satu task in_progress "
                "dalam satu waktu), 'list' untuk melihat daftar. Update "
                "daftar tiap ada progres nyata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "set", "list"],
                        "description": "aksi: add | set | list",
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "judul task (add/set), atau nomor index "
                            "0-based untuk set"
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "status baru (hanya untuk action set)",
                    },
                },
                "required": ["action"],
            },
        },
    },
]
