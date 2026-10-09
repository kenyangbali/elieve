"""Fase 2 — MEMORY.md + autoDream (STUB, belum diimplementasi).

Masalah yang diselesaikan: tiap sesi Hermes mulai dari nol. Preferensi,
keputusan, dan pelajaran dari run sebelumnya hilang. Context window
bukan tempat menyimpan pengetahuan lintas sesi.

Desain (lihat docs/ARCHITECTURE.md §2):
  - `MEMORY.md` per workspace/outdir: file markdown berisi pengingat
    SINGKAT (maks ~150 karakter per butir), BUKAN arsip lengkap. Contoh:
    "- Bug SQLi di auth.py pola `f-string` query (2026-10-09)"
  - Agent menulis ingatan via tool `remember(fakta)`; dibaca otomatis
    sebagai konteks di awal tiap run via `recall()`.
  - `autoDream`: proses latar yang berjalan tiap N run / tiap idle —
    membaca butir-butir mentah, menggabungkan duplikat, menghapus yang
    basi, dan merapikan format. Tidak pernah menyimpan rahasia
    (API key, token, kredensial) — filter sebelum tulis.
  - Ingatan bersifat lokal per outdir agar tidak bocor antar task.

API yang direncanakan:
    mem = AgentMemory(path="/run/outdir/MEMORY.md")
    mem.remember("pola bug X di file Y")   # tulis butir baru
    context = mem.recall()                 # string konteks utk system prompt
    mem.tidy(api_key)                      # autoDream: rapikan (model murah)

Kelas di bawah ini hanya kontrak interface.
"""


class AgentMemory:
    """Ingatan lintas sesi berbasis file markdown (fase 2)."""

    def __init__(self, path):
        self.path = path

    def remember(self, fact: str) -> None:
        """Simpan satu butir ingatan singkat."""
        raise NotImplementedError(
            "Fase 2 belum diimplementasi. Lihat docs/ARCHITECTURE.md §2."
        )

    def recall(self) -> str:
        """Kembalikan seluruh ingatan sebagai teks konteks."""
        raise NotImplementedError(
            "Fase 2 belum diimplementasi. Lihat docs/ARCHITECTURE.md §2."
        )

    def tidy(self, api_key) -> None:
        """autoDream: gabung duplikat, hapus yang basi, rapikan format."""
        raise NotImplementedError(
            "Fase 2 belum diimplementasi. Lihat docs/ARCHITECTURE.md §2."
        )
