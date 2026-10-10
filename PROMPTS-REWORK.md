# Rework default system prompt — STRIP TOTAL: kosong by design (2026-10-11)

## Fase 2 — strip (perintah final Bayu)

> "Gausah ada system prompt, gausah ada soul.md bawaan, gausah ada
> aturan2 atau mode hunter dll. Bikin kosongan — tapi jelas: hermes
> versi antropik = elieve."

Fase 1 (generik + hunter opt-in, commit `eb4ad79`) KEJAUHAN. Keputusan
final: **NOL bawaan**. Framework ship kosong; user yang isi.

### Yang dihapus
- `elieve/prompts/en.py`, `elieve/prompts/id.py`,
  `elieve/prompts/hunter.py` — DIHAPUS total.
- Flag CLI `--profile` + key config `profile:` — dicabut dari
  `elieve/loop.py` dan `elieve/planmode.py`. Tidak ada mode sama sekali.
- `configs/bug-hunter.yaml`: blok `profile: hunter` dihapus.

### Yang dipertahankan (kosong, bukan persona)
- `elieve/prompts/__init__.py` — `get_system_prompt(...)` tetap ada
  (backward compat) tapi SELALU return `""`. Semua argumen diterima dan
  diabaikan. Tidak ada soul.md (memang tidak pernah ada di repo).
- `system_prompt` eksplisit via config/CLI — tetap didukung, itu cara
  user mengisi kekosongan.
- Loop TIDAK mengirim system message sama sekali bila kosong (skip,
  bukan `{"role":"system","content":""}`).
  - `run()`: `has_system` tracking; resume mendeteksi system message by
    role (bukan asumsi posisi); task block per-turn membuat system
    message on-demand bila tasks ada.
  - Operational notices (memory recall, MODE BACA-SAJA, plan addendum)
    tetap ada — itu mekanik harness, bukan persona.

### Hasil tes
- `python3 -m unittest discover -s tests` → **299 tests, OK**
  (288 base − 16 fase-1 + 11 kontrak kekosongan baru + penyesuaian
  `test_checkpoints.py` untuk default tanpa system message)
- `tests/test_prompts.py` ditulis ulang jadi kontrak kekosongan:
  `get_system_prompt` → `""` untuk semua input; tidak ada file prompt
  bawaan; tidak ada `hunter`/persona di package; tidak ada file
  `*soul*` di repo; loop skip system message saat kosong; task block
  on-demand; resume deteksi by role.

## Fase 1 — generik + hunter opt-in (commit `eb4ad79`, DIGANTI fase 2)

Perintah Bayu: repo publik `elieve-dev/elieve` tidak bisa pakai default
persona bug hunter + aturan keras ("refusal mode"). Default harus generik,
gaya Anthropic: minimal, berbasis prinsip, percaya pada judgment model.

Prinsip produk (arahan Bayu): framework publik self-hosted → OPERATOR
(user yang clone + jalanin + colok API key) adalah otoritas TERTINGGI.
Default prompt yang membangkang operator = friksi, bukan proteksi
(repo publik bisa di-fork bebas tanpa inject).

Yang dipertahankan: pembedaan instruksi operator vs DATA tak tepercaya
(output tool, isi file, web, pesan terusan = data, bukan perintah) —
pertahanan injeksi yang beneran penting, dibingkai sebagai "waspada ke
data", bukan "menolak user".

## Yang diubah

- `elieve/prompts/en.py` — default generik baru (EN), gaya Anthropic:
  identitas 1 baris, prinsip operator-tertinggi, data-bukan-instruksi,
  cara kerja, workmanship. Tanpa daftar aturan/larangan.
- `elieve/prompts/id.py` — sama, Bahasa Indonesia.
- `elieve/prompts/hunter.py` — BARU. Persona bug hunter (konten lama)
  ditulis ulang tanpa nada refusal: tetap task-spesifik + disiplin bukti
  sebagai standar profesional. Opt-in via `profile="hunter"`.
- `elieve/prompts/__init__.py` — `get_system_prompt(lang,
  workspace_root=None, profile="default"|"hunter")`. Backward compatible:
  `get_system_prompt(lang)` → default generik seperti dulu.
- `elieve/loop.py` — flag CLI `--profile {default,hunter}` + key config
  `profile:`; diteruskan ke prompt default dan ke `run_plan`.
- `elieve/planmode.py` — `run_plan(..., profile="default")`, dipakai saat
  membangun base prompt plan mode.
- `configs/bug-hunter.yaml` — `profile: hunter` (profile personal itu
  memang untuk hunting).
- `README.md` — dokumentasi `--profile` + key config `profile`.
- `tests/test_prompts.py` — BARU, 16 tes: kontrak gaya (tanpa
  HARD RULES/ATURAN KERAS/DILARANG di default MAUPUN hunter),
  otoritas operator, distingsi data-vs-instruksi, minimalisme,
  backward compat, substitusi workspace_root, fallback profil/bahasa.

## Hasil tes

- `python3 -m unittest discover -s tests` → **304 tests, OK**
  (288 lama + 16 baru)
- Smoke: `get_system_prompt("id")` → "Kamu Elieve, AI agent general
  yang membantu."; `profile="hunter"` → persona bug hunter.

## Yang TIDAK diubah

- Logika loop/orchestrator/permissions/compaction: tidak disentuh.
- Tidak ada konsep soul.md yang ditambahkan (tidak ada di repo).
- `hunter.py` (hermes-hunter, repo lain): tidak disentuh.
