"""Tool ingatan: `remember` — tulis butir ke MEMORY.md run ini.

Tool ini butuh AgentMemory yang di-bind oleh loop utama sebelum run
(`bind_memory`). Tanpa binding, tool menolak (ToolError) agar agent
mendapat umpan balik yang jelas, bukan crash diam-diam.
"""

from . import ToolError

_MEMORY = None


def bind_memory(mem):
    """Sambungkan tool remember ke AgentMemory milik run yang aktif."""
    global _MEMORY
    _MEMORY = mem


def unbind_memory():
    global _MEMORY
    _MEMORY = None


def remember(fact) -> str:
    """Simpan satu butir ingatan singkat (maks ~150 karakter).

    API key/token/password/kredensial DITOLAK otomatis dan tidak
    pernah ditulis.
    """
    if _MEMORY is None:
        raise ToolError("ingatan belum di-bind ke run ini (hubungi operator).")
    ok = _MEMORY.remember(str(fact or ""))
    if ok:
        return "Ingatan tersimpan di MEMORY.md."
    return (
        "Ingatan DITOLAK (kosong, duplikat, atau mengandung pola rahasia "
        "— rahasia tidak pernah disimpan)."
    )


REMEMBER_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Simpan satu butir ingatan singkat ke MEMORY.md sesi ini "
                "(pelajaran, pola bug, keputusan; maks ~150 karakter). "
                "DILARANG menyimpan API key, token, password, atau kredensial "
                "— butir seperti itu otomatis ditolak."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "butir ingatan singkat",
                    },
                },
                "required": ["fact"],
            },
        },
    },
]
