# Gap 3 — Akuntansi token & biaya

Modul: `hermes/accounting.py`. Terintegrasi di `HermesLoop`
(`hermes/loop.py`, param `accounting_cfg=`).

## Yang dicatat

Tiap selesai `call_model`, loop memanggil
`loop._accounting_after_call(usage, messages, step)` yang:

1. `UsageTracker.record(model, usage)` — akumulasi `prompt_tokens`,
   `completion_tokens`, `total_tokens`, `calls` per model.
   Bila provider tidak mengirim `usage` → fallback estimasi
   `chars/4` (via `estimate_tokens`) dengan flag `estimated=True`.
2. Warning konteks: bila `prompt_tokens` terakhir ≥
   `accounting.context_warn_pct`% (default 80) dari
   `compaction.context_limit` → cetak PERINGATAN sekali per run.
3. Simpan `<outdir>/usage.json` berkala tiap 10 step (best effort —
   kegagalan tulis tidak menghentikan run).
4. Cek `run_cost_cap`.

## Estimasi biaya

Harga dari blok config:

```yaml
accounting:
  enabled: true
  prices:
    ag/claude-opus-4-6-thinking: {input_per_1k: 0.015, output_per_1k: 0.075}
  run_cost_cap: 0.0        # USD; 0 = nonaktif
  context_warn_pct: 80
```

Harga = USD per 1K token. **Nilai bawaan = PLACEHOLDER** —
sesuaikan dengan harga aktual provider sebelum dipakai budgeting.
Model tanpa entri harga → biaya 0 + warning sekali (tidak crash).

## Cost cap

`run_cost_cap > 0` dan estimasi biaya run ≥ cap → run berhenti rapi:

- `progress.json` → `status: "cost_capped"`, note berisi pesan
  "budget tercapai".
- `OUT.md` ditulis dengan status `cost_capped` + pesan yang sama.
- Hook `OnStop` tetap dijalankan, `usage.json` tetap ditulis,
  ringkasan tercetak ke stdout, `run()` return 0 (bukan crash).

## Akhir run

Di semua jalur keluar (`done`, `max_steps`, `rate_limited`, `error`,
`cost_capped`), loop memanggil `_finish_accounting()`:

- tulis `<outdir>/usage.json` (models, grand total, biaya per model +
  total USD, `unknown_prices`, cap, limit, timestamp),
- cetak ringkasan ke stdout: token per model, total token, estimasi
  biaya USD.

`accounting.enabled: false` → seluruh Gap 3 nonaktif (no-op).
