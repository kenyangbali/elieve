# GAP AUDIT — hermes-agent vs Claude Code (fitur publik)

Tanggal: 2026-10-09
Metode: perbandingan arsitektur `hermes-agent/` (4 fase selesai, 108 test)
terhadap fitur Claude Code yang **terdokumentasi publik** di docs resmi
Anthropic. **Analisa murni — tidak memakai/mencari source bocor dalam
bentuk apa pun, tidak menyalin kode dari mana pun.**

Sumber publik yang dipakai:
- https://docs.anthropic.com/en/docs/claude-code/hooks (hooks reference)
- https://docs.anthropic.com/en/docs/claude-code/memory (CLAUDE.md + auto memory)
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/slash-commands (skills)
- https://code.claude.com/docs/en/plugins, /plugin-marketplaces, /mcp
  (direferensikan dari riset publik)

Konteks prioritas: **operasi bug bounty otonom** (bukan coding assistant
umum). Hermes jalan non-interaktif, multi-run panjang, multi-model via
9router (`ag/*`).

---

## 1. Tabel pemetaan

| # | Fitur Claude Code (publik) | Status di hermes-agent |
|---|---|---|
| 1 | **Hooks lifecycle** (30+ event: SessionStart/End, UserPromptSubmit, PreToolUse/PostToolUse(+Failure), PermissionRequest/Denied, Stop, SubagentStart/Stop, PreCompact/PostCompact, FileChanged, Notification, …) | **Sebagian** — PermissionGate ≈ PreToolUse; ada audit log; autoDream berkala. Tanpa sistem hook umum (tak ada SessionStart/Stop/PostToolUse hook, tak ada hook user-defined) |
| 2 | **Context compaction** (`/compact` + auto-compact + PreCompact hook) | **Ada** — Fase 1: 4 lapis (micro, threshold 70–95%, full via LLM, prefix preservation) |
| 3 | **Memory: CLAUDE.md + auto memory** (instruksi proyek berlapis + catatan otomatis Claude) | **Ada (sebagian)** — AgentMemory + autoDream ≈ auto memory. Tanpa hierarki instruksi ala CLAUDE.md (user/project/local) |
| 4 | **Subagents / Task tool** (konteks sendiri, frontmatter: description, tools, model, permissionMode; auto-delegasi berdasar deskripsi; tipe built-in Explore/Plan) | **Sebagian** — Orchestrator spawn worker subprocess paralel. Tanpa auto-delegasi berdasar deskripsi, tanpa tipe built-in, tanpa pewarisan permission |
| 5 | **Permission modes** (default / acceptEdits / plan / bypassPermissions / dontAsk) | **Sebagian** — gate allow/deny/ask + flag `--no-exec` (read-only). Tanpa mode plan, tanpa mode-level policy |
| 6 | **Plan mode** (EnterPlanMode: riset read-only → papar rencana → approve → eksekusi) | **Belum** |
| 7 | **TodoWrite / Task tools** (checklist terstruktur per sesi: pending/in_progress/completed) | **Belum** — `progress.json` hanya hitung step, bukan daftar tugas |
| 8 | **Checkpoints / rewind / resume / fork** (checkpoint tiap prompt, `/rewind`, `--resume`, `--fork-session`) | **Sebagian** — run resumable via progress.json; tanpa resume percakapan, tanpa checkpoint/rewind |
| 9 | **`/context`** (visualisasi isi context window) | **Belum** — estimasi token ada internal, tanpa tampilan user |
| 10 | **`/cost`, `/usage`** (akuntansi token & kuota per sesi) | **Belum** |
| 11 | **MCP integration** (`.mcp.json`, stdio/SSE/streamable-http, tool `mcp__server__tool`) | **Belum** |
| 12 | **Skills (SKILL.md)** (progressive disclosure, auto-invoke via description) | **Belum** |
| 13 | **Slash commands** (legacy `.claude/commands/`, kini unified ke skills) | **Belum** — hanya flag CLI |
| 14 | **Plugins / marketplace** (`.claude-plugin/plugin.json`, distribusi via marketplace) | **Belum** |
| 15 | **Background agents / agent teams** (sesi background yang bisa di-steer) | **Sebagian** — worker subprocess fire-and-forget; tanpa steering |
| 16 | **AskUserQuestion** (klarifikasi interaktif terstruktur) | **Belum** — by design non-interaktif (verdict `ask` = soft-deny) |
| 17 | **Worktree isolation** (subagent jalan di git worktree terisolasi) | **Belum** |
| 18 | **WebFetch / WebSearch tools** (fetch→markdown, search backend) | **Belum** — bisa via `exec`+curl, tanpa tool khusus |

Ringkasan: **2 ada** (compaction, memory), **6 sebagian**, **10 belum** →
**16 gap** total.

---

## 2. Daftar GAP (diprioritaskan untuk bug bounty otonom)

### G1. Hook lifecycle system — effort: SEDANG
Claude Code punya 30+ event hook (PreToolUse, PostToolUse, SessionStart,
Stop, PreCompact, …) berisi perintah deterministik yang **tidak bisa
di-skip model**. Hermes baru punya satu titik (permission gate ≈
PreToolUse). Untuk bounty otonom berjam-jam, hook berarti: blokir pola
berbahaya secara deterministik, QC otomatis tiap PostToolUse (mis. cek
`OUT.md` parsial), simpan state saat Stop, dan checkpoint sebelum
compaction — tanpa bergantung "model biasanya nurut".

### G2. Structured task tracking (TodoWrite) — effort: KECIL
Claude Code melacak checklist terstruktur per sesi. Hermes cuma hitung
step di `progress.json`. Ronde bounty = puluhan target × checklist
(recon → hunt → PoC → QC → draft); tanpa todo terstruktur, cakupan
mudah bocor dan resume run panjang buta prioritas. Implementasi ringan:
JSON list + tool `todo_write`/`todo_read`, ikut tersimpan saat compaction.

### G3. Akuntansi token/biaya + visibilitas konteks (`/cost`, `/context`) — effort: KECIL
Claude Code menampilkan token & spend per sesi. Hermes tanpa akuntansi
sama sekali — padahal operasi jalan di 11 koneksi Antigravity round-robin
dengan kuota 5-jam/mingguan per akun. Tanpa ini, satu run liar bisa
menghabiskan kuota akun Pro tanpa jejak. Butuh: log token per run +
per model, guardrail budget, dan perintah status konteks.

### G4. Checkpoints / resume percakapan — effort: SEDANG
Claude Code menyimpan checkpoint tiap prompt dan bisa `--resume` sesi
lama. Hermes tiap run mulai dari nol; `progress.json` hanya simpan step
terakhir, bukan percakapan. Run bounty 40-step yang mati di step 35
kehilangan seluruh konteks investigasi — padahal konteks itulah yang
mahal. Butuh: snapshot messages per N step + mode resume dari snapshot.

### G5. MCP client — effort: SEDANG–BESAR
MCP adalah cara standar Claude Code menyambung tool eksternal (browser,
DB, API). Hermes toolset-nya tertutup (read/search/exec/memory). Untuk
bounty, MCP membuka: browser automation (Playwright) untuk PoC dinamis,
klien HTTP terstruktur, query DB temuan — tanpa menambah kode tool
manual satu per satu.

### G6. Plan mode — effort: SEDANG
Mode riset read-only: agent memetakan permukaan dulu, memaparkan rencana,
baru eksekusi setelah approve. Untuk bounty otonom, variannya adalah
"recon gate": worker recon (read-only, murah) wajib selesai sebelum
worker exploit mahal di-spawn — mencegah bakar kuota opus untuk target
yang ternyata hardened.

### G7. Skills system (SKILL.md) — effort: SEDANG
Claude Code memuat prosedur panjang on-demand via SKILL.md. Playbook
bounty Bayu (pola XSS, JMAP, WordPress) saat ini tertanam di prompt
koordinator — memakan konteks tiap run. Sebagai skill, playbook hanya
dimuat saat relevan: hemat token + playbook bisa di-versioning terpisah.

### G8. Auto-delegasi subagent by description — effort: SEDANG
Claude Code mendelegasikan otomatis berdasar deskripsi subagent.
Orchestrator Hermes butuh mandor LLM memecah task manual tiap run.
Registry worker bertipe ("recon-web", "poc-dinamis", "qc") dengan
deskripsi + auto-routing = ronde bounty bisa jalan tanpa tahap planning
LLM yang mahal setiap kali.

### G9. Slash commands / CLI verbs — effort: KECIL
Perintah seperti `/status`, `/tidy`, `/resume` untuk inspeksi cepat.
Hermes hanya punya flag CLI tersebar. Nilai operasional: cek status run
tanpa membuka file JSON manual.

### G10. Worktree isolation untuk worker — effort: SEDANG
Claude Code bisa mengisolasi subagent di git worktree. Worker Hermes
berbagi filesystem — satu worker liar bisa mengotori outdir worker lain.
Isolasi = kegagalan tertampung, artefak PoC tidak tercampur.

### G11. WebFetch / WebSearch tools — effort: KECIL
Tool khusus fetch→markdown dan search backend. Recon bounty sering butuh
baca docs/blog keamanan; via `exec`+curl hasilnya mentah dan boros token.
Tool fetch dengan ekstraksi ringkas menghemat konteks signifikan.

### G12. `/doctor` diagnostics — effort: KECIL
Claude Code punya checkup setup (config, hook lambat, duplikat skill).
Hermes tanpa diagnostik mandiri — masalah seperti "DB 9router tak
terbaca" baru ketahuan saat run gagal. Satu perintah `doctor` memangkas
waktu debug infra.

### G13. Plugin / marketplace — effort: BESAR
Format distribusi `.claude-plugin/`. Relevan hanya bila hermes-agent
didistribusikan ke pihak lain (rencana armada VM Bayu). Untuk operasi
bounty saat ini: prioritas rendah.

### G14. AskUserQuestion — effort: KECIL
Klarifikasi interaktif terstruktur. Hermes by design non-interaktif
(directive "gas terus"), jadi gap ini disengaja — prioritas terendah.

---

## 3. Rekomendasi 3 teratas untuk dibangun berikutnya

### #1 — Hook lifecycle system (G1)
**Alasan:** satu-satunya mekanisme *deterministik* di daftar ini. Semua
yang lain masih bergantung pada model "nurut". Untuk operasi otonom yang
jalan berjam-jam tanpa pengawasan, hook adalah pembeda antara "biasanya
aman" dan "dijamin aman": blokir destruktif, QC otomatis pasca-tool,
persist saat stop, checkpoint pra-compaction. Fondasi ini juga dipakai
oleh G2/G4 nantinya (todo/checkpoint sebagai hook).

### #2 — Structured task tracking (G2)
**Alasan:** effort kecil, dampak langsung ke kualitas ronde. Koordinator
bounty saat ini melacak cakupan via file markdown manual yang rawan
drift. Todo terstruktur yang survive compaction = cakupan 20-plugin
tercatat rapi, resume run tahu persis posisi, dan QC bisa verifikasi
"semua target tersentuh" secara mekanis.

### #3 — Akuntansi token/biaya + visibilitas konteks (G3)
**Alasan:** uang dan kuota. Operasi berjalan di 11 akun round-robin
dengan batas 5-jam/mingguan; hari ini tidak ada yang mencatat berapa
token dibakar per run per model. Tanpa ini, optimasi biaya (yang jadi
alasan membangun compaction) tidak bisa diukur, dan satu run liar bisa
menghabiskan kuota akun Pro tanpa peringatan.

---

*Catatan: estimasi effort relatif terhadap basis kode hermes-agent saat
ini (Python, ~108 test). "Kecil" ≈ 1 modul + test < 1 hari kerja builder;
"Sedang" ≈ integrasi loop + config; "Besar" ≈ subsistem baru lintas modul.*
