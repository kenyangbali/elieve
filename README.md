# hermes-agent

Framework agent AI modular — ReAct loop dengan function calling,
toolset ter-sandbox, dan arsitektur yang dirancang untuk ditingkatkan
bertahap (context compaction → memory → permission gate → orchestrator).

Baseline v1 di-port rapi dari `hermes-hunter` (loop pemburu bug yang
sudah terbukti jalan produksi) menjadi struktur paket Python yang
bersih.

## Quickstart

```bash
cd ~/workspace/hermes-agent

# lihat opsi
python3 -m hermes.loop --help

# contoh run (butuh 9router aktif di localhost:20128 + Default Key)
python3 -m hermes.loop \
  --task "Audit keamanan direktori /home/hatch/workspace/contoh" \
  --outdir /tmp/hermes-run-1

# pakai profil
python3 -m hermes.loop \
  --config configs/bug-hunter.yaml \
  --task "Audit keamanan ..." \
  --outdir /tmp/hermes-run-1

# unit test tools (tanpa butuh API)
python3 -m unittest discover -s tests
```

Hasil tiap run: `OUT.md` (laporan akhir) + `progress.json` (resumable)
di `--outdir`.

## Struktur

```
hermes-agent/
├── README.md
├── docs/ARCHITECTURE.md      # desain 4 pola peningkatan
├── hermes/
│   ├── loop.py               # ReAct loop v1 (HermesLoop)
│   ├── compaction.py         # fase 1 (stub)
│   ├── memory.py             # fase 2 (stub)
│   ├── permissions.py        # fase 3 (stub)
│   ├── orchestrator.py       # fase 4 (stub)
│   └── tools/                # read, search, exec (ter-sandbox)
├── configs/bug-hunter.yaml   # profil siap pakai
└── tests/test_tools.py
```

## Aturan model

- **Boleh**: `ag/*` (default `ag/claude-opus-4-6-thinking`)
- **Dilarang keras**: `bns/*`, `oc/*` — ditolak di kode sebelum request.

## Fase Pengembangan

| Fase | Komponen | Status | Dampak |
|------|----------|--------|--------|
| 1 | **Context compaction** (`hermes/compaction.py`) | stub | Hemat token/biaya; ringkas riwayat dingin, pertahankan cache prompt |
| 2 | **MEMORY.md + autoDream** (`hermes/memory.py`) | stub | Ingatan lintas sesi; perapian otomatis berkala |
| 3 | **Permission classifier 2 tahap** (`hermes/permissions.py`) | stub | Izin semantik (kilat + reasoning), gantikan regex statis |
| 4 | **Multi-agent orchestrator** (`hermes/orchestrator.py`) | stub | Worker paralel bertask-spesifik, laporan gabungan |

Aturan main: satu fase selesai (implementasi → unit test → micro-audit
nyata) baru lanjut ke fase berikutnya. Baseline v1 tidak boleh rusak.

Detail desain tiap fase: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
