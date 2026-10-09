# Hook Lifecycle System (Gap 1)

Event deterministik yang **tidak bisa di-skip model** — inspirasi Claude
Code hooks, diimplementasikan original di `hermes/hooks.py`.

## Event

| Event | Titik fire | Semantik |
|---|---|---|
| `PreToolUse` | `loop._dispatch_tool`, SEBELUM permission gate | Satu hook return `(False, reason)` → tool DIBLOKIR deterministik; hasil tool diganti pesan blokir, loop lanjut. |
| `PostToolUse` | sesudah tool selesai | Boleh return `{"qc_flags": [...]}`; tidak memblokir (kecuali shell `blocking: true` yang gagal). |
| `PreCompact` | `loop._maybe_threshold_compact`, sebelum pipeline | mis. `checkpoint_state` → `<outdir>/.precompact.json`. |
| `PostCompact` | sesudah pipeline compaction | — |
| `OnStop` | SEMUA jalur keluar `run()` (done, max_steps, error, rate_limited) | mis. `persist_state` → `<outdir>/.final_state.json`. |
| `OnError` | handler exception di `run()` | selalu tercatat ke `<outdir>/errors.jsonl`, lalu hook config jalan. |

## Config (`configs/bug-hunter.yaml`, blok `hooks:`)

```yaml
hooks:
  PreToolUse:
    - {action: "block_destructive_exec"}
  PostToolUse:
    - {action: "qc_tool_output"}
  PreCompact:
    - {action: "checkpoint_state"}
  OnStop:
    - {action: "persist_state"}
```

Tiap aksi EITHER `{action: nama}` (callable terdaftar di `HOOK_ACTIONS`)
OR `{shell: "cmd ...", timeout: 15, blocking: false}` (shell, env
`HERMES_EVENT` / `HERMES_OUTDIR` / `HERMES_STEP`). String polos juga
diterima sebagai nama action.

## Aksi bawaan (`HOOK_ACTIONS`)

- `block_destructive_exec` — tolak exec berpola `rm -rf`, `mkfs`,
  `dd if=`, `dd of=/dev/`, fork bomb, shutdown/reboot. Deterministik,
  tanpa LLM. Hook yang error di PreToolUse → **fail-closed** (blokir).
- `qc_tool_output` — flag output >12000 char / mengandung "Traceback".
- `checkpoint_state` — tulis `<outdir>/.precompact.json`.
- `persist_state` — tulis `<outdir>/.final_state.json`.
- `log_error` — append `<outdir>/errors.jsonl`.

## Kontrak kegagalan

- Hook python melempar exception → dicatat ke `errors.jsonl`, run TIDAK
  crash. PreToolUse fail-closed; event lain fail-open.
- Shell gagal (rc≠0 / timeout) → dicatat, run lanjut. Di PreToolUse +
  `blocking: true` → tool diblokir.
- Action tak terdaftar / event tak dikenal / spec rusak → `ValueError`
  JELAS saat `HookRunner` dibangun (sebelum run).

Test: `tests/test_hooks.py` (29 test, tanpa network/9router).
