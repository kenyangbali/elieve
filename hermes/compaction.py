"""Fase 1 — context compaction (STUB, belum diimplementasi).

Masalah yang diselesaikan: riwayat `messages` di HermesLoop tumbuh tanpa
batas sampai max_steps. Tiap turn mengirim ulang seluruh riwayat = token
input membengkak, biaya naik, dan model "lupa" konteks awal karena
terdorong keluar jendela.

Desain (lihat docs/ARCHITECTURE.md §1):
  Pipeline pemampatan berlapis yang dijalankan saat token riwayat melewati
  ambang (mis. 70% jendela model):
    1. Identifikasi segmen "dingin": tool call + hasil lama yang tidak
       dirujuk beberapa turn terakhir.
    2. Ringkas tiap segmen dingin jadi 1-3 kalimat memakai model murah
       (mis. ag/gemini-3-flash) — BUKAN model utama, agar murah.
    3. PENTING: pertahankan prefix prompt yang ter-cache. Struktur ulang
       messages hasil kompresi agar bagian system prompt + awal percakapan
       tetap identik byte-per-byte sebisa mungkin, sehingga cache prompt
       provider tidak invalid dan biaya turn berulang turun drastis.
    4. Sisipkan ringkasan sebagai satu pesan `system`/`user` bertanda
       "[ringkasan turn 1..N]" tepat setelah pesan yang dipertahankan.
    5. Jangan pernah memampatkan: system prompt, task awal, dan N turn
       terakhir (jendela kerja).

API yang direncanakan:
    compactor = ContextCompactor(cache_model="ag/gemini-3-flash",
                                threshold_ratio=0.7)
    messages = compactor.maybe_compact(messages, api_key)

Kelas di bawah ini hanya kontrak interface agar loop.py bisa dihubungkan
nanti tanpa refactor besar.
"""


class ContextCompactor:
    """Pemampat riwayat percakapan berlapis (fase 1)."""

    def __init__(self, cache_model="ag/gemini-3-flash", threshold_ratio=0.7,
                 keep_last_turns=6):
        self.cache_model = cache_model
        self.threshold_ratio = threshold_ratio
        self.keep_last_turns = keep_last_turns

    def maybe_compact(self, messages, api_key):
        """Kembalikan messages (dimampatkan bila melewati ambang).

        Belum diimplementasi — saat ini mengembalikan messages apa adanya.
        """
        raise NotImplementedError(
            "Fase 1 belum diimplementasi. Lihat docs/ARCHITECTURE.md §1."
        )
