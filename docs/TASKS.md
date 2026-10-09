# Structured Task Tracking (Gap 2)

Checklist terstruktur per-run ala Claude Code TodoWrite — inspirasi publik,
diimplementasikan original di `hermes/tasks.py`.

## Konsep

`TaskList("<outdir>/tasks.json")` mengelola `{title, status}` dengan status
`pending` / `in_progress` / `completed`. Persist **otomatis setiap mutasi**,
tulis atomik (file tmp + `os.replace`) agar crash di tengah tulis tidak
merusak `tasks.json`.

API: `add(title)`, `set_status(index|judul, status)`, `list()`,
`summary()` → ringkasan 1 baris, mis. `"3/8 selesai, aktif: Audit auth.py"`.

## Tool `task_update`

Schema `{action: "add"|"set"|"list", title?, status?}`, binding per-run via
`bind_tasks` / `unbind_tasks` (pola `remember` di `hermes/tools/memory.py`).
ToolError bila belum di-bind. Terdaftar di `DISPATCH` + `TOOL_SCHEMAS`.

## Kenapa state task survive compaction (pin dari compaction)

Tiga lapis pertahanan:

1. **Ringkasan disuntik ke system prompt tiap turn.** `HermesLoop` menulis
   ulang `messages[0]["content"] = base + "## Daftar task\n<summary>"` sebelum
   SETIAP panggilan model. System prompt masuk `prefix_len` yang tidak pernah
   disentuh pipeline compaction (Lapis 4). State selalu segar karena dihitung
   ulang dari `TaskList` yang hidup, bukan dari riwayat chat.
2. **Hasil tool `task_update` dikecualikan dari pemotongan.**
   `hermes/compaction.py` mengenali nama tool via `STATEFUL_TOOL_NAMES`:
   pesan `role: tool` bernama `task_update` tidak pernah di-offload oleh
   `micro_compact` maupun di-mask oleh `mask_observations`.
3. **Ground truth di disk.** `tasks.json` adalah sumber kebenaran; instance
   `TaskList` baru bisa dibangun ulang dari path kapan saja.

## Config (`configs/bug-hunter.yaml`, blok `tasks:`)

```yaml
tasks:
  enabled: true
  max_tasks: 64            # add() di atas batas ditolak
  summary_max_chars: 300   # batas ringkasan "## Daftar task" di system prompt
```

`enabled: false` → `TaskList` tidak dibuat, tool unbound (ToolError bila
dipanggil), tidak ada blok "## Daftar task" di prompt.
