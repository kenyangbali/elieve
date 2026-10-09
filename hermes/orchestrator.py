"""Fase 4 — multi-agent orchestration (STUB, belum diimplementasi).

Masalah yang diselesaikan: satu agent mengerjakan semuanya = lambat
dan mudah "tunnel vision". Audit besar (mis. satu repo WordPress)
lebih cepat bila dipecah ke sub-agent spesialis yang jalan paralel.

Desain (lihat docs/ARCHITECTURE.md §4):
  - Orchestrator (model kuat) memecah task jadi subtask independen,
    lalu spawn N HermesLoop worker — masing-masing dengan:
      * toolset TERBATAS sesuai subtask (mis. worker "baca saja":
        read_file/list_dir/grep tanpa exec),
      * outdir sendiri (hasil terisolasi),
      * budget max_steps sendiri.
  - Worker TIDAK boleh spawn worker lain (kedalaman maks 1) untuk
    mencegah ledakan rekursif.
  - Orchestrator menggabungkan OUT.md tiap worker jadi satu laporan,
    menandai temuan yang perlu verifikasi silang antar worker.
  - Kegagalan satu worker tidak menggagalkan yang lain; progress tiap
    worker tetap resumable via progress.json masing-masing.

API yang direncanakan:
    orch = Orchestrator(model="ag/claude-opus-4-6-thinking", max_workers=4)
    report = orch.run(task, outdir, api_key)   # -> path OUT.md gabungan

Kelas di bawah ini hanya kontrak interface.
"""


class Orchestrator:
    """Koordinator multi-worker HermesLoop (fase 4)."""

    def __init__(self, model="ag/claude-opus-4-6-thinking", max_workers=4):
        self.model = model
        self.max_workers = max_workers

    def run(self, task: str, outdir: str, api_key: str) -> str:
        """Jalankan task via worker paralel; kembalikan path laporan gabungan."""
        raise NotImplementedError(
            "Fase 4 belum diimplementasi. Lihat docs/ARCHITECTURE.md §4."
        )
