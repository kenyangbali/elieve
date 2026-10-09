# Arsitektur hermes-agent

Desain ini terinspirasi pola umum arsitektur agent coding modern
(agent loop + tool sandbox + permission pipeline + context management)
yang terdokumentasi luas di literatur publik. Seluruh implementasi di
repo ini adalah karya original — tidak ada kode hasil bocoran apa pun.

Prinsip inti: **model bisa diganti-ganti, harness yang menentukan kualitas.**
Karena itu investasi utama repo ini ada di harness (`hermes/`), bukan di
prompt.

```
                    ┌─────────────┐
                    │   HermesLoop │  (hermes/loop.py — v1, sudah jalan)
                    └──────┬──────┘
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ ContextCompactor│ │PermissionGate │  │ AgentMemory   │
│ (fase 1)      │  │ (fase 3)      │  │ (fase 2)      │
│ hemat token   │  │ izin 2 tahap  │  │ ingatan lintas│
└───────────────┘  └───────────────┘  │ sesi          │
                                     └───────────────┘
        ┌────────────────────────────────────────┐
        ▼                                        │
┌───────────────┐                                 │
│ Orchestrator  │  (fase 4 — koordinasi worker)   │
└───────────────┘                                 │
```

---

## 1. Context Compaction (Fase 1) — `hermes/compaction.py`

**Masalah.** Riwayat `messages` tumbuh setiap turn. Tanpa pemampatan:
biaya input membengkak, konteks awal terdorong keluar jendela, dan
setiap turn membayar ulang token yang sama.

**Desain.**
1. **Ambang pemicu**: saat estimasi token riwayat > 70% jendela model.
2. **Segmen dingin**: tool call + hasil yang tidak dirujuk dalam N turn
   terakhir ditandai untuk diringkas. Yang TIDAK PERNAH disentuh:
   system prompt, task awal, dan 6 turn terakhir (jendela kerja).
3. **Ringkasan murah**: tiap segmen dingin diringkas jadi 1–3 kalimat
   memakai model kecil/murah (`ag/gemini-3-flash`), bukan model utama.
4. **Pertahankan cache**: susun ulang `messages` hasil kompresi agar
   prefix (system prompt + awal percakapan) sedekat mungkin identik
   dengan sebelumnya — cache prompt provider tetap hit, biaya turn
   berulang turun signifikan.
5. Sisipkan ringkasan sebagai satu pesan bertanda
   `[ringkasan turn 1..N]` tepat setelah bagian yang dipertahankan.

**Kontrak.** `ContextCompactor.maybe_compact(messages, api_key)` —
dipanggil di awal tiap iterasi loop; no-op bila di bawah ambang.

## 2. MEMORY.md + autoDream (Fase 2) — `hermes/memory.py`

**Masalah.** Tiap sesi mulai dari nol; pelajaran run sebelumnya hilang.

**Desain.**
- Satu file `MEMORY.md` per outdir berisi butir **singkat** (maks ~150
  karakter), mis. `- Pola SQLi f-string di auth.py (2026-10-09)`.
- `remember(fakta)`: agent menulis butir baru kapan saja.
- `recall()`: seluruh isi dibaca sebagai konteks tambahan di awal run.
- `autoDream`: job perapian berkala (tiap N run) — gabung duplikat,
  hapus yang basi, rapikan format, memakai model murah.
- **Filter rahasia**: API key, token, kredensial tidak pernah ditulis.

## 3. Permission Classifier 2 Tahap (Fase 3) — `hermes/permissions.py`

**Masalah.** Deny-list regex (v1) tidak paham konteks dan rapuh terhadap
variasi penulisan.

**Desain.** Setiap tool call melewati gerbang sebelum dieksekusi:
- **Tahap 1 — kilat**: model kecil menjawab YA/TIDAK (<64 token,
  target <2 dtk): "apakah aksi ini destruktif / keluar izin /
  exfiltrate data?"
- **Tahap 2 — reasoning**: hanya bila tahap 1 ragu. Model utama
  menimbang maksud perintah lalu putuskan `allow` / `deny` / `ask`.
- Semua verdict dicatat ke audit log (tool, argumen ringkas, verdict).
- Regex v1 tetap sebagai **lapisan 0 fail-closed** bila classifier
  tidak terjangkau.
- **Opsional & pluggable** (keputusan Bayu 2026-10-09): classifier aktif
  hanya bila `classifier_model` diisi di config (**auto-on**); kosong =
  **auto-off** (regex lapisan 0 saja). API custom via `classifier_api_base`
  + key dari env var (`classifier_api_key_env`). Model mati (timeout/error)
  -> fallback lapisan 0 per-call, auto-disable setelah 3 gagal beruntun.
  Run tidak pernah crash gara-gara classifier.

## 4. Multi-Agent Orchestration (Fase 4) — `hermes/orchestrator.py`

**Masalah.** Satu agent untuk audit besar = lambat dan tunnel vision.

**Desain.**
- Orchestrator (model kuat) memecah task jadi subtask independen,
  spawn maks 4 `HermesLoop` worker paralel.
- Tiap worker: **toolset terbatas** sesuai subtask (mis. mode baca-saja
  tanpa `exec`), outdir + budget `max_steps` sendiri.
- Worker **dilarang spawn worker lain** (kedalaman maks 1).
- Orchestrator menggabungkan `OUT.md` tiap worker jadi satu laporan;
  kegagalan satu worker tidak menggagalkan yang lain.
- Tiap worker tetap resumable via `progress.json` masing-masing.

**Catatan desain pluggable (keputusan Bayu 2026-10-09, pola = classifier
Fase 3).** Orchestrator OPSIONAL & "by choose": `orchestrator_model`
kosong / `enabled: false` → 100% single-agent (tidak ada perubahan
perilaku default). Model mandor bisa diganti + API custom via
`orchestrator_api_base` / `orchestrator_api_key_env` (key HANYA via env
var). Mandor mati → `OrchestratorError` → `loop.main` fallback ke
single-agent + warning; run tidak crash. Model mandor tunduk pada
`model_policy` (allow/forbid) yang sama seperti loop utama.

---

## Urutan implementasi

1. **Fase 1 — compaction** (dampak biaya langsung, risiko rendah)
2. **Fase 2 — memory** (murah, gampang, fondasi personalisasi)
3. **Fase 3 — permission gate** (butuh model kecil tambahan)
4. **Fase 4 — orchestrator** (paling kompleks, terakhir)

Setiap fase: implementasi → unit test → micro-audit nyata →
baru lanjut fase berikutnya. Baseline v1 tidak boleh rusak.
