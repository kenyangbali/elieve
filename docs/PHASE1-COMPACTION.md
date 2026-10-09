# Fase 1: Context Compaction

Prioritas tertinggi. Target: turunkan pemakaian token 50-76% di sesi panjang
tanpa mengorbankan kualitas kerja agent.

## Inspirasi

Bocoran arsitektur Claude Code (Maret 2026) menunjukkan sistem compaction
4 lapis. Insight kunci: **full compaction yang "wah" justru paling jarang
kepakai — microcompaction diam-diam yang mengerjakan 90% kerja.**

## Desain: 4 Lapis

### Lapis 1 — Microcompaction (per-turn, tanpa LLM)

Jalan tiap turn SEBELUM request ke model. Murni operasi string, nol biaya.

- **Truncate hasil tool lama**: hasil `read_file`/`grep`/`exec` yang berumur
  > N turn diganti pointer `[offloaded: <ringkasan 1 baris>]`.
- **Time-based clearing**: entri > 24 jam dihapus (relevan untuk sesi panjang).
- **Aturan**: JANGAN sentuh N turn terakhir (recency window, default 5).
  JANGAN sentuh hasil tool yang sedang "aktif" (mis. file yang lagi diedit).

```python
def micro_compact(messages, recency_window=5, max_tool_chars=2000):
    """Potong hasil tool lama jadi pointer. Return messages baru."""
```

### Lapis 2 — Threshold Pipeline (progresif agresif)

Monitor `prompt_tokens` dari tiap respons API. Ketika mendekati batas:

| % dari limit | Aksi |
|---|---|
| 70% | Warning: log + catat tren |
| 80% | Observation masking: hasil tool lama → `[offloaded to scratch]` |
| 85% | Fast pruning: jalan mundur, prune di luar recency window |
| 90% | Aggressive masking: kecilkan preservation window (5 → 2) |
| 95% | Full compaction (Lapis 3) |

### Lapis 3 — Full Compaction (pakai LLM, jarang)

Ketika 95% limit tercapai:

1. Kirim request khusus ke model: system prompt sama + seluruh history +
   instruksi "ringkas jadi status kerja".
2. Ringkasan WAJIB memuat: keputusan yang dibuat, issue belum selesai,
   state implementasi, file yang sedang dikerjakan.
3. Ganti history dengan SATU pesan: `[COMPACTED] <ringkasan>` + N turn
   terakhir verbatim (jangan diringkas).
4. Turn berikutnya otomatis rebuild cache dari ringkasan yang pendek.

```python
def full_compact(messages, model, api_key, keep_recent=5):
    """Ringkas history via LLM. Return [boundary_msg] + recent_turns."""
```

### Lapis 4 — Prompt Cache Preservation

Prinsip: **jangan ubah prefix yang sudah tercache.**

- System prompt + tool schemas + N pesan pertama = prefix. JANGAN pernah
  edit/di-reorder bagian ini.
- Compaction HANYA boleh memotong dari TENGAH (setelah prefix, sebelum
  recency window).
- Ini yang bikin cache-hit rate tinggi → biaya turun drastis.

```
[prefix: system + tools + pesan awal] ← JANGAN DIUBAH (cached)
[tengah: history lama]                ← BOLEH dicompact
[recency window: 5 turn terakhir]     ← JANGAN DIUBAH (verbatim)
```

## Konfigurasi

```yaml
# configs/bug-hunter.yaml
compaction:
  enabled: true
  context_limit: 64000        # batas sebelum pipeline agresif
  recency_window: 5           # turn terakhir yang tidak disentuh
  micro_max_tool_chars: 2000  # hasil tool di atas ini dipotong
  thresholds: [70, 80, 85, 90, 95]
```

## Testing

- `tests/test_compaction.py`: unit test tiap lapis
- Simulasi 40-turn session: assert token akhir < 50% tanpa compaction
- Assert prefix tidak berubah setelah compaction (cache preservation)

## Estimasi Dampak

- Microcompaction saja: ~50% penghematan (data OpenDev)
- + cache preservation: total ~76% (data Anthropic)
- Untuk operasi bounty (ratusan request/hari via ag/*): penghematan signifikan
  dari kuota 5-jam + mingguan tiap akun

> Catatan: angka di atas adalah data sekunder dari sumber eksternal, BUKAN
> hasil benchmark Hermes. Hasil **simulasi lokal** (heuristik chars/4,
> `tests/test_compaction.py::TestSimulation40Turn`, sesi sintetis 40-turn
> dengan output tool ~5000 chars/turn): tanpa compaction ~52.083 token vs
> dengan micro_compact per-turn ~9.278 token (17,8% dari awal, hemat 82,2%).
> Ini simulasi, bukan pengukuran produksi.
