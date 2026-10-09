# CHECKPOINTS.md — Gap 4: Checkpoints / resume percakapan

Run bounty 40-step yang mati di step 35 kehilangan seluruh konteks
investigasi — padahal konteks itulah yang mahal. Modul
`elieve/checkpoints.py` + integrasi `elieve/loop.py` menutup gap ini.

## Snapshot

`save_checkpoint(outdir, step, messages, state)` menulis
`<outdir>/checkpoints/ckpt-<step>.json` berisi:

- `messages` — riwayat percakapan lengkap (system/user/assistant/tool),
- `step` — step terakhir yang selesai,
- `state` — ringkasan: `task`, `model`, `max_steps`, usage tracker
  (`elieve.accounting`), dan daftar task (`elieve.tasks`).

Penulisan ATOMIK (file tmp di direktori yang sama + `os.replace`) agar
file setengah-tulis tidak pernah terlihat.

Kapan disimpan (bila `checkpoints.enabled`, default true):

1. **Tiap N step** (`checkpoints.every_n_steps`, default 10) — di akhir
   step yang selesai tool-call-nya.
2. **Sebelum compaction** — saat pipeline threshold benar-benar
   memampatkan konteks (titik waktu hook PreCompact yang sama; tidak ada
   mekanisme duplikat, hanya satu pemanggilan `save_checkpoint`).
3. **Paksa** saat loop keluar via `max_steps` — agar selalu bisa resume.

Kegagalan tulis = warning di stdout, TIDAK PERNAH crash-kan run.

## Resume

```bash
python3 -m elieve.loop --resume <outdir> [--task "..." --model ...]
```

- Memuat checkpoint TERAKHIR dari `<outdir>/checkpoints/`.
- Loop lanjut dari **step+messages+state** snapshot (step berikutnya =
  N+1), bukan dari nol. Usage tracker dipulihkan agar `usage.json`
  kontinu; blok "## Daftar task" basi di-strip lalu disuntik fresh.
- `--task` opsional: diambil dari checkpoint bila tidak diisi.
- Output tetap ke `<outdir>` yang sama (OUT.md, progress.json,
  tasks.json berlanjut).
- Checkpoint corrupt / tidak ada -> error JELAS ke stderr + exit 2
  (bukan traceback misterius). `--resume` melewati orchestrator
  (selalu single-agent).

## Inspeksi

```bash
python3 -m elieve.loop --list-checkpoints --outdir <outdir>   # exit 0
```

## Config

```yaml
checkpoints:
  enabled: true
  every_n_steps: 10
```

Tanpa blok ini: default `enabled=true, every_n_steps=10`. Tanpa flag
`--resume`/`--list-checkpoints`, perilaku run tidak berubah (hanya file
tambahan di `<outdir>/checkpoints/`).
