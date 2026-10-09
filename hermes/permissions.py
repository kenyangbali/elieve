"""Fase 3 — permission classifier 2 tahap (STUB, belum diimplementasi).

Masalah yang diselesaikan: v1 memakai deny-list regex statis
(hermes/tools/exec.py: DENY_RULES). Regex tidak paham konteks:
"rm -rf /tmp/sampah" aman tapi pola "curl ... | sh" bisa lolos lewat
variasi penulisan. Butuh penilaian semantik, bukan pencocokan pola.

Desain (lihat docs/ARCHITECTURE.md §3):
  Setiap tool call melewati gerbang 2 tahap SEBELUM dieksekusi:
    Tahap 1 — classifier kilat: model kecil/murah (mis. ag/gemini-3-flash)
      menjawab YA/TIDAK dalam <64 token: "apakah aksi ini destruktif /
      keluar dari izin / exfiltrate data?" Target latency <2 detik.
    Tahap 2 — reasoning pass: HANYA bila tahap 1 ragu/tidak yakin.
      Model utama menimbang maksud perintah (maks ~4K token) lalu
      putuskan: allow / deny / ask.
  Keputusan dicatat ke audit log (tool, argumen ringkas, verdict)
  agar bisa diaudit belakangan.
  Deny-list regex v1 TETAP dipertahankan sebagai lapisan 0
  (fail-closed bila classifier tidak bisa dihubungi).

API yang direncanakan:
    gate = PermissionGate(fast_model="ag/gemini-3-flash",
                          deep_model="ag/claude-opus-4-6-thinking")
    verdict = gate.check("exec", {"command": "rm -rf /tmp/x"}, api_key)
    # verdict: "allow" | "deny" | "ask" (+ alasan singkat)

Kelas di bawah ini hanya kontrak interface.
"""


class PermissionGate:
    """Gerbang izin 2 tahap untuk setiap tool call (fase 3)."""

    def __init__(self, fast_model="ag/gemini-3-flash",
                 deep_model="ag/claude-opus-4-6-thinking"):
        self.fast_model = fast_model
        self.deep_model = deep_model

    def check(self, tool_name: str, args: dict, api_key: str) -> dict:
        """Nilai tool call; kembalikan {"verdict": ..., "reason": ...}."""
        raise NotImplementedError(
            "Fase 3 belum diimplementasi. Lihat docs/ARCHITECTURE.md §3."
        )
