#!/usr/bin/env python3
"""hermes.loop — ReAct loop baseline v1 (port rapi dari hermes-hunter/hunter.py).

Pola: task -> chat -> tool_calls -> eksekusi tool -> umpan balik -> ...
sampai model selesai (jawaban akhir tanpa tool call) atau max-steps.

Aturan model (perintah Bayu 2026-10-08):
  BOLEH: ag/*  (default ag/claude-opus-4-6-thinking buat audit berat,
                ag/gemini-3.1-pro alternatif kuat,
                ag/gemini-3-flash buat tugas ringan)
  DILARANG KERAS: bns/* dan oc/* (jangan sentuh kuota itu).

API key dibaca saat runtime dari ~/.9router/db/data.sqlite
(tabel apiKeys, baris name='Default Key'). TIDAK PERNAH di-hardcode,
di-print, atau ditulis ke file mana pun.

Output per run (di --outdir):
  OUT.md        — laporan akhir model
  progress.json — step terakhir (konvensi resumability)
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from .tools import (
    DISPATCH, TOOL_SCHEMAS, ToolError,
    bind_memory, unbind_memory,
    bind_tasks, unbind_tasks,
)
from .permissions import PermissionGate
from .hooks import HookRunner
from .accounting import Accounting
from .tasks import (
    TaskList,
    DEFAULT_MAX_TASKS,
    DEFAULT_SUMMARY_MAX_CHARS,
)
from .memory import (
    AgentMemory,
    DEFAULT_MAX_FACT_CHARS,
    DEFAULT_SUMMARIZER_MODEL as MEMORY_SUMMARIZER_MODEL,
    DEFAULT_TIDY_EVERY_RUNS,
)
from .compaction import (
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_KEEP_RECENT,
    DEFAULT_MAX_TOOL_CHARS,
    DEFAULT_PREFIX_LEN,
    DEFAULT_RECENCY_WINDOW,
    DEFAULT_SUMMARIZER_MODEL,
    DEFAULT_THRESHOLDS,
    apply_threshold_pipeline,
    estimate_tokens,
    fire_post_compact,
    fire_pre_compact,
    full_compact,
    micro_compact,
)

API_URL = "http://127.0.0.1:20128/v1/chat/completions"
DB_PATH = os.path.expanduser("~/.9router/db/data.sqlite")

ALLOWED_MODELS = {
    "ag/claude-opus-4-6-thinking": "audit berat (default)",
    "ag/gemini-3.1-pro": "alternatif kuat",
    "ag/gemini-3-flash": "cepat, tugas ringan",
}
DEFAULT_MODEL = "ag/claude-opus-4-6-thinking"
FORBIDDEN_PREFIXES = ("bns/", "oc/")

MODEL_CALL_DELAY_S = 3
REQUEST_TIMEOUT_S = 180

SYSTEM_PROMPT = """Kamu Hermes, bug hunter yang teliti dan jujur. Misi: cari bug keamanan nyata.

ATURAN KERAS (melanggar = gagal):
1. Hanya yang IN-SCOPE dari task. Jangan melebar ke target lain.
2. Setiap temuan WAJIB didukung bukti file:baris persis yang kamu baca SENDIRI via tool. DILARANG mengarang, menebak, atau mengklaim tanpa bukti.
3. DILARANG tindakan destruktif: jangan hapus/ubah file, jangan menyerang sistem, jangan exfiltrate data.
4. Hanya boleh akses path di bawah /home/hatch/workspace atau /tmp. Selalu pakai ABSOLUTE path.
5. Jika ragu apakah sesuatu bug atau bukan, catat sebagai "perlu verifikasi", jangan dipaksakan jadi temuan.

CARA KERJA:
- Gunakan function call yang tersedia: read_file, list_dir, grep, exec, remember, task_update.
- Tool `remember`: simpan pelajaran/pola penting ke ingatan sesi (MEMORY.md).
  JANGAN PERNAH simpan API key, token, password, atau kredensial apa pun.
- Tool `task_update`: kelola daftar task (add/set/list). Buat task untuk
  tiap langkah kerja berarti, tandai in_progress saat dikerjakan dan
  completed saat selesai. Satu task in_progress dalam satu waktu.
- Setelah semua bukti terkumpul (atau tidak ada temuan), BERHENTI memanggil tool dan tulis LAPORAN AKHIR sebagai jawaban teks biasa — itu yang akan disimpan sebagai hasil.
- Format laporan akhir:
  ## <judul temuan>
  - Lokasi: `path/file:baris`
  - Bukti: <kutipan kode / hasil observasi>
  - Dampak: <apa yang bisa dilakukan penyerang>
  - PoC: <langkah reproduksi, bila ada>
  Ulangi per temuan. Jika TIDAK ADA temuan: tulis "TIDAK ADA TEMUAN" + ringkasan area yang sudah diperiksa.
- Bahasa laporan: Indonesia. Jujur soal keterbatasan (mis. "belum terverifikasi runtime").

CADANGAN: bila function calling tidak tersedia, panggil tool lewat blok kode persis format ini:
```tool
{"name": "read_file", "arguments": {"path": "/home/hatch/workspace/..."}}
```
"""


class RateLimited(Exception):
    pass


def validate_model(model: str) -> str:
    """Tolak prefix terlarang sebelum request dibuat."""
    low = model.strip().lower()
    for prefix in FORBIDDEN_PREFIXES:
        if low.startswith(prefix):
            sys.stderr.write(
                f"ERROR: model '{model}' DILARANG — jangan sentuh kuota bns/* atau oc/*.\n"
                f"Model boleh-pakai: {', '.join(sorted(ALLOWED_MODELS))}\n"
            )
            sys.exit(2)
    return model.strip()


def _validate_compaction_model(model: str) -> str:
    """Summarizer compaction HANYA boleh ag/*. bns/*/oc/* DITOLAK."""
    low = (model or "").strip().lower()
    for prefix in FORBIDDEN_PREFIXES:
        if low.startswith(prefix):
            sys.stderr.write(
                f"ERROR: summarizer '{model}' DILARANG — jangan sentuh "
                f"kuota bns/* atau oc/*.\n"
            )
            sys.exit(2)
    if not low.startswith("ag/"):
        sys.stderr.write(
            f"ERROR: summarizer harus model ag/* (dapat '{model}').\n"
        )
        sys.exit(2)
    return model.strip()


def get_api_key(db_path: str = DB_PATH) -> str:
    try:
        con = sqlite3.connect(db_path)
        row = con.execute(
            "SELECT key FROM apiKeys WHERE name='Default Key' LIMIT 1"
        ).fetchone()
        con.close()
    except Exception as e:
        sys.stderr.write(f"ERROR: gagal baca DB 9router ({db_path}): {e}\n")
        sys.exit(2)
    if not row or not row[0]:
        sys.stderr.write(
            "ERROR: baris name='Default Key' tidak ada / kosong di tabel apiKeys.\n"
        )
        sys.exit(2)
    return row[0]


def _post_json(url, headers, payload, timeout):
    try:
        import requests  # noqa

        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        return r.status_code, r.text
    except ImportError:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


def call_model(messages, model, api_key, tools=None):
    """Kirim chat completion; kembalikan (message, usage).

    `usage` berisi prompt_tokens/completion_tokens bila provider
    memberikannya, else dict kosong (pemanggil pakai estimasi chars/4).
    `tools`: daftar schema function-call; default TOOL_SCHEMAS global
    (worker read-only meneruskan subset tanpa `exec`).
    """
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS if tools is None else tools,
        "tool_choice": "auto",
        "stream": False,
    }
    status, text = _post_json(
        API_URL,
        {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        payload,
        REQUEST_TIMEOUT_S,
    )
    if status == 429:
        raise RateLimited("HTTP 429 dari 9router — berhenti rapi tanpa retry.")
    if status >= 400:
        raise RuntimeError(f"9router HTTP {status}: {text[:500]}")
    try:
        data = json.loads(text)
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return message, usage
    except Exception as e:
        raise RuntimeError(f"respon 9router tidak bisa di-parse: {e} :: {text[:300]}")


def _fallback_tool_call(text: str, dispatch=None):
    """Cadangan bila model tidak pakai native function calling:
    blok ```tool {"name": ..., "arguments": {...}}```

    `dispatch`: mapping tool yang diizinkan (default DISPATCH global;
    worker read-only meneruskan subset tanpa `exec`)."""
    m = re.search(r"```tool\s*\n(\{.*?\})\s*```", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except Exception:
        return None
    name = d.get("name")
    allowed = DISPATCH if dispatch is None else dispatch
    if name in allowed:
        return name, d.get("arguments") or {}
    return None


class HermesLoop:
    """Satu sesi ReAct: kirim task, iterasi tool call sampai jawaban akhir."""

    def __init__(self, task, outdir, model=DEFAULT_MODEL, max_steps=40,
                 system_prompt=SYSTEM_PROMPT, compaction_cfg=None,
                 memory_cfg=None, permissions_cfg=None, no_exec=False,
                 hooks_cfg=None, tasks_cfg=None,
                 accounting_cfg=None):
        self.task = task
        self.outdir = outdir
        self.model = validate_model(model)
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.api_key = get_api_key()
        # Gap 1 — hook lifecycle (deterministik, tidak bisa di-skip model).
        # hooks_cfg None/kosong -> semua event no-op.
        self.hooks = HookRunner(hooks_cfg, outdir=outdir)
        self._step = 0
        self._last_tool = None
        self._qc_flags = []
        # Toolset per-run (Fase 4): worker read-only membuang `exec`.
        self.no_exec = bool(no_exec)
        self.dispatch = dict(DISPATCH)
        if self.no_exec:
            self.dispatch.pop("exec", None)
        self.tool_schemas = [
            s for s in TOOL_SCHEMAS
            if s.get("function", {}).get("name") in self.dispatch
        ]
        # Konfigurasi compaction (Fase 1); default aman bila config tak ada.
        self.compaction = {
            "enabled": True,
            "context_limit": DEFAULT_CONTEXT_LIMIT,
            "recency_window": DEFAULT_RECENCY_WINDOW,
            "micro_max_tool_chars": DEFAULT_MAX_TOOL_CHARS,
            "thresholds": list(DEFAULT_THRESHOLDS),
            "summarizer_model": DEFAULT_SUMMARIZER_MODEL,
            "full_compact_keep_recent": DEFAULT_KEEP_RECENT,
            "prefix_len": DEFAULT_PREFIX_LEN,
        }
        if compaction_cfg:
            self.compaction.update(compaction_cfg)
        # validasi awal: summarizer wajib ag/* (bns/*/oc/* ditolak di kode)
        _validate_compaction_model(self.compaction["summarizer_model"])
        # Gap 3 — akuntansi token/biaya (hermes/accounting.py).
        # accounting_cfg None/kosong -> enabled default true, cap nonaktif.
        # context_limit diambil dari config compaction (fallback chars/4
        # bila provider tak mengirim usage).
        self.acct = Accounting(
            accounting_cfg,
            outdir=outdir,
            context_limit=self.compaction["context_limit"],
        )
        # Konfigurasi memory (Fase 2).
        self.memory_cfg = {
            "enabled": True,
            "tidy_every_runs": DEFAULT_TIDY_EVERY_RUNS,
            "max_fact_chars": DEFAULT_MAX_FACT_CHARS,
            "summarizer_model": MEMORY_SUMMARIZER_MODEL,
        }
        if memory_cfg:
            self.memory_cfg.update(memory_cfg)
        _validate_compaction_model(self.memory_cfg["summarizer_model"])
        self.memory = None  # AgentMemory dibuat di run() (butuh outdir)
        # Konfigurasi permission gate (Fase 3). Classifier OPSIONAL:
        # classifier_model kosong -> auto-off (hanya regex lapisan-0).
        self.permissions = {
            "enabled": True,
            "classifier_model": "",
            "classifier_api_base": "",
            "classifier_api_key_env": "",
            "kilat_max_tokens": 32,
            "kilat_timeout_s": 10,
            "max_consecutive_failures": 3,
        }
        if permissions_cfg:
            self.permissions.update(permissions_cfg)
        self.gate = PermissionGate(
            cfg=self.permissions,
            deep_model=self.model,
            main_api_key=self.api_key,
            audit_path=os.path.join(outdir, "permission_audit.jsonl"),
        )
        # Gap 2 — structured task tracking (hermes/tasks.py).
        # tasks_cfg None/kosong -> enabled default true.
        self.tasks_cfg = {
            "enabled": True,
            "max_tasks": DEFAULT_MAX_TASKS,
            "summary_max_chars": DEFAULT_SUMMARY_MAX_CHARS,
        }
        if tasks_cfg:
            self.tasks_cfg.update(tasks_cfg)
        self.tasks = None
        if self.tasks_cfg.get("enabled", True):
            self.tasks = TaskList(
                os.path.join(outdir, "tasks.json"),
                max_tasks=self.tasks_cfg["max_tasks"],
            )
            bind_tasks(self.tasks)
        else:
            unbind_tasks()
        if self.model not in ALLOWED_MODELS:
            print(
                f"[hermes] peringatan: '{self.model}' bukan daftar dikenal; "
                f"yang disarankan: {', '.join(sorted(ALLOWED_MODELS))}",
                flush=True,
            )

    # -- output ------------------------------------------------------

    def _write_progress(self, payload):
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(os.path.join(self.outdir, "progress.json"), "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _write_out(self, content, status):
        header = (
            "# Hermes — hasil\n\n"
            f"- Task: {self.task}\n"
            f"- Model: {self.model}\n"
            f"- Status: {status}\n"
            f"- Waktu: {datetime.now(timezone.utc).isoformat()}\n\n---\n\n"
        )
        with open(os.path.join(self.outdir, "OUT.md"), "w") as f:
            f.write(header + (content or "(kosong)"))

    # -- accounting (Gap 3) ------------------------------------------

    def _accounting_after_call(self, usage, messages, step):
        """Catat usage tiap selesai call_model + guardrail.

        - record() ke UsageTracker (fallback estimasi chars/4 bila provider
          tak mengirim usage; ditandai estimated=True).
        - warning konteks bila prompt_tokens >= context_warn_pct%.
        - simpan usage.json berkala tiap 10 step (best effort).
        - cek run_cost_cap.
        Kembalikan True bila cap tercapai -> pemanggil menghentikan run
        dengan rapi (status cost_capped, return 0, bukan crash).
        """
        if not self.acct.enabled:
            return False
        estimated = not (usage or {}).get("prompt_tokens")
        if estimated:
            usage = {"prompt_tokens": estimate_tokens(messages),
                     "completion_tokens": 0}
        self.acct.tracker.record(self.model, usage, estimated=estimated)
        self.acct.check_context_warning(
            (usage or {}).get("prompt_tokens") or 0)
        self.acct.maybe_periodic_save(step)
        if self.acct.cap_reached():
            print(f"[hermes] {self.acct.cap_message()}", flush=True)
            return True
        return False

    def _finish_accounting(self):
        """Gap 3: tulis <outdir>/usage.json + cetak ringkasan ke stdout.

        Dipanggil di SEMUA jalur keluar run (done/max_steps/error/
        rate_limited/cost_capped). Best effort — tidak boleh crash-kan run.
        """
        if not self.acct.enabled:
            return
        try:
            path = self.acct.save()
        except Exception as e:
            print(f"[accounting] gagal tulis usage.json: {e}", flush=True)
        else:
            print(f"[hermes] usage.json ditulis: {path}", flush=True)
        print(self.acct.summary_text(), flush=True)

    # -- tool dispatch -----------------------------------------------

    def _run_tool(self, name, args):
        try:
            return str(self.dispatch[name](**args))
        except TypeError as e:
            return f"argumen salah untuk {name}: {e}"
        except ToolError as e:
            return f"TOOL DITOLAK: {e}"
        except Exception as e:
            return f"tool error ({name}): {e}"

    def _gated_tool(self, name, args):
        """Tool call lewat permission gate (Fase 3) SEBELUM dieksekusi.

        Kembalikan (allowed, result_text). verdict deny/ask -> tool TIDAK
        dijalankan; model diberi pesan penolakan dan loop lanjut normal
        (tidak crash).
        """
        verdict, reason = self.gate.check(name, args or {})
        if verdict == "deny":
            print(f"[hermes]   gate: DENY {name}: {reason[:120]}", flush=True)
            return False, f"IZIN DITOLAK oleh permission gate: {reason}"
        if verdict == "ask":
            print(f"[hermes]   gate: ASK {name}: {reason[:120]}", flush=True)
            return False, (
                "IZIN DITOLAK oleh permission gate (butuh konfirmasi manusia; "
                f"loop non-interaktif): {reason}"
            )
        return True, self._run_tool(name, args)

    # -- hook lifecycle (Gap 1) ---------------------------------------

    def _hook_ctx_base(self, extra=None):
        """Ctx dasar untuk semua hook: step, outdir, task, last_tool, dll."""
        ctx = {
            "step": self._step,
            "outdir": self.outdir,
            "task": self.task,
            "last_tool": self._last_tool,
            "qc_flags": list(self._qc_flags),
            # Gap 2: ringkasan task NYATA dari TaskList (bukan placeholder).
            "tasks_summary": self.tasks.summary() if self.tasks else "",
        }
        if extra:
            ctx.update(extra)
        return ctx

    def _dispatch_tool(self, name, args):
        """Jalur penuh satu tool call: PreToolUse -> permission gate ->
        eksekusi -> PostToolUse.

        PreToolUse diblokir (allowed=False) -> tool TIDAK dijalankan;
        model diberi pesan blokir dan loop lanjut normal (tidak crash).
        Kembalikan teks hasil tool."""
        ctx = self._hook_ctx_base({
            "tool_name": name,
            "tool_args": args or {},
        })
        allowed, reasons = self.hooks.pre_tool_use(ctx)
        if not allowed:
            reason = "; ".join(r for r in reasons if r) or "ditolak"
            print(
                f"[hermes]   hook PreToolUse: BLOKIR {name}: {reason[:160]}",
                flush=True,
            )
            result = f"HOOK DITOLAK oleh PreToolUse: {reason}"
        elif name not in self.dispatch:
            result = f"tool tidak dikenal: {name}"
        else:
            print(f"[hermes]   tool: {name} {str(args)[:120]}", flush=True)
            _ok, result = self._gated_tool(name, args)
        post = self.hooks.post_tool_use(
            dict(ctx, tool_output=str(result)))
        flags = post.get("qc_flags") or []
        if flags:
            self._qc_flags.extend(flags)
            print(
                f"[hermes]   hook PostToolUse: qc_flags={flags}",
                flush=True,
            )
        if post.get("blocked"):
            result = (
                "[QC] hook PostToolUse menandai output "
                "(shell blocking gagal).\n" + str(result)
            )
        return result

    def _fire_on_stop(self, status, note=""):
        """OnStop di semua jalur keluar loop; tidak boleh crash-kan run."""
        try:
            self.hooks.on_stop(
                self._hook_ctx_base({"status": status, "note": note}))
        except Exception as e:  # belt-and-suspenders
            print(f"[hermes] hook OnStop gagal: {e}", flush=True)

    def _fire_on_error(self, err):
        """OnError: catat ke errors.jsonl + jalankan hook OnError."""
        try:
            self.hooks.on_error(
                self._hook_ctx_base({"error": str(err)[:1000]}))
        except Exception as e:  # belt-and-suspenders
            print(f"[hermes] hook OnError gagal: {e}", flush=True)

    # -- compaction (Fase 1) ------------------------------------------

    def _summarize_for_compaction(self, messages):
        """Summarizer untuk pipeline 95%: full_compact via model murah ag/*."""
        return full_compact(
            messages,
            model=self.compaction["summarizer_model"],
            api_key=self.api_key,
            keep_recent=int(self.compaction["full_compact_keep_recent"]),
            prefix_len=int(self.compaction["prefix_len"]),
        )

    def _maybe_micro_compact(self, messages):
        if not self.compaction.get("enabled", True):
            return messages
        return micro_compact(
            messages,
            recency_window=int(self.compaction["recency_window"]),
            max_tool_chars=int(self.compaction["micro_max_tool_chars"]),
            prefix_len=int(self.compaction["prefix_len"]),
        )

    def _maybe_threshold_compact(self, messages, prompt_tokens):
        if not self.compaction.get("enabled", True):
            return messages, []
        # Gap 1: PreCompact sebelum pemampatan, PostCompact sesudahnya.
        ctx = self._hook_ctx_base()
        fire_pre_compact(self.hooks, ctx)
        out, actions = apply_threshold_pipeline(
            messages,
            prompt_tokens,
            context_limit=int(self.compaction["context_limit"]),
            thresholds=self.compaction["thresholds"],
            recency_window=int(self.compaction["recency_window"]),
            prefix_len=int(self.compaction["prefix_len"]),
            summarizer=self._summarize_for_compaction,
        )
        fire_post_compact(self.hooks, ctx, {"compact_actions": actions})
        return out, actions

    # -- task tracking (Gap 2) ----------------------------------------

    def _tasks_block(self):
        """Blok "## Daftar task" untuk system prompt. Kosong bila tak ada.

        Disuntik ulang SETIAP turn sebelum panggilan model agar state task
        survive compaction: system prompt masuk prefix_len yang tidak
        pernah disentuh pipeline compaction (lihat hermes/tasks.py).
        """
        if not self.tasks:
            return ""
        summary = self.tasks.summary(
            max_chars=int(self.tasks_cfg["summary_max_chars"]))
        if not summary:
            return ""
        return "\n\n## Daftar task\n" + summary

    # -- memory (Fase 2) --------------------------------------------------

    def _memory_path(self):
        return os.path.join(self.outdir, "MEMORY.md")

    def _memory_counter_path(self):
        return os.path.join(self.outdir, ".memory_counter")

    def _setup_memory(self):
        """Buat/bind AgentMemory, autoDream tiap N run, recall konteks."""
        if not self.memory_cfg.get("enabled", True):
            unbind_memory()
            return ""
        self.memory = AgentMemory(
            self._memory_path(),
            max_fact_chars=int(self.memory_cfg["max_fact_chars"]),
        )
        bind_memory(self.memory)
        # autoDream: counter per outdir; tidy tiap N run.
        counter = 0
        cpath = self._memory_counter_path()
        try:
            with open(cpath) as f:
                counter = int((f.read() or "0").strip() or 0)
        except (OSError, ValueError):
            counter = 0
        counter += 1
        try:
            with open(cpath, "w") as f:
                f.write(str(counter))
        except OSError as e:
            print(f"[hermes] peringatan: counter memory gagal ditulis: {e}",
                  flush=True)
        every = max(1, int(self.memory_cfg["tidy_every_runs"]))
        if counter % every == 0:
            try:
                print(f"[hermes] autoDream: tidy MEMORY.md (run ke-{counter}) ...",
                      flush=True)
                self.memory.tidy(
                    self.api_key,
                    model=self.memory_cfg["summarizer_model"],
                )
            except Exception as e:
                print(f"[hermes] autoDream gagal (lanjut tanpa tidy): {e}",
                      flush=True)
        return self.memory.recall()

    # -- main --------------------------------------------------------

    def run(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        # Fase 2: ingatan sesi lalu disuntik ke system prompt.
        recalled = self._setup_memory()
        system_prompt = self.system_prompt
        if recalled:
            system_prompt += "\n\n## Ingatan sesi lalu\n" + recalled
        if self.no_exec:
            # Fase 4: worker read-only — model wajib tahu exec tak tersedia.
            system_prompt += (
                "\n\nMODE BACA-SAJA: tool `exec` TIDAK tersedia di sesi ini. "
                "Jangan memanggil atau memintanya; gunakan read_file, "
                "list_dir, grep, dan remember."
            )
        # Gap 2: basis system prompt statis; blok task disuntik segar tiap
        # turn (lihat _tasks_block) agar survive compaction.
        base_system_prompt = system_prompt
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.task},
        ]
        prog = {
            "task": self.task,
            "model": self.model,
            "outdir": self.outdir,
            "step": 0,
            "max_steps": self.max_steps,
            "status": "running",
            "last_tool": None,
        }
        self._write_progress(prog)

        step = 0
        while step < self.max_steps:
            step += 1
            self._step = step  # hook ctx selalu tahu step berjalan
            if step > 1:
                time.sleep(MODEL_CALL_DELAY_S)
            print(
                f"[hermes] step {step}/{self.max_steps} -> {self.model} ...",
                flush=True,
            )
            # Lapis 1: microcompaction tiap turn SEBELUM request (tanpa LLM)
            messages = self._maybe_micro_compact(messages)
            # Gap 2: suntik ringkasan task segar ke system prompt tiap turn.
            # System prompt masuk prefix compaction -> tidak pernah dipotong.
            messages[0]["content"] = base_system_prompt + self._tasks_block()
            try:
                msg, usage = call_model(messages, self.model, self.api_key,
                                        tools=self.tool_schemas)
            except RateLimited as e:
                prog.update(step=step, status="rate_limited", note=str(e))
                self._write_progress(prog)
                self._write_out(
                    "BERHENTI: rate limited (HTTP 429). Progress tersimpan di "
                    "progress.json — lanjutkan manual bila kuota pulih.",
                    "rate_limited",
                )
                self._fire_on_stop("rate_limited", str(e))
                self._finish_accounting()    # Gap 3
                print(f"[hermes] {e}", flush=True)
                return 0
            except Exception as e:
                prog.update(step=step, status="error", note=str(e)[:300])
                self._write_progress(prog)
                self._fire_on_error(e)       # Gap 1: OnError
                self._fire_on_stop("error", str(e)[:300])
                self._finish_accounting()    # Gap 3: usage.json + ringkasan
                print(f"[hermes] ERROR: {e}", flush=True)
                return 1

            # Gap 3 — akuntansi tiap selesai call_model; stop rapi bila
            # run_cost_cap tercapai (status cost_capped, return 0).
            if self._accounting_after_call(usage, messages, step):
                note = self.acct.cap_message()
                prog.update(step=step, status="cost_capped", note=note)
                self._write_progress(prog)
                self._write_out(
                    "BERHENTI RAPI: " + note + "\n\n"
                    "Progress tersimpan di progress.json; rincian token di "
                    "usage.json. Naikkan accounting.run_cost_cap di config "
                    "untuk melanjutkan.",
                    "cost_capped",
                )
                self._fire_on_stop("cost_capped", note)
                self._finish_accounting()
                return 0

            assistant_msg = {"role": "assistant", "content": msg.get("content")}
            if msg.get("tool_calls"):
                assistant_msg["tool_calls"] = msg["tool_calls"]
            # Lapis 2/3: pipeline threshold berdasar prompt_tokens respons.
            # Bila usage tak tersedia, estimasi dari request yang baru dikirim.
            prompt_tokens = (usage or {}).get("prompt_tokens")
            if not prompt_tokens:
                prompt_tokens = estimate_tokens(messages)
            messages, compact_actions = self._maybe_threshold_compact(
                messages, prompt_tokens
            )
            for act in compact_actions:
                print(
                    f"[hermes] compaction: {act} "
                    f"(prompt_tokens~{prompt_tokens})",
                    flush=True,
                )
            prog["compaction_actions"] = compact_actions
            messages.append(assistant_msg)

            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    # Gap 1: jalur penuh tool lewat _dispatch_tool
                    # (PreToolUse -> permission gate -> exec -> PostToolUse).
                    result = self._dispatch_tool(name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": name,
                            "content": str(result),
                        }
                    )
                    prog["last_tool"] = name
                self._last_tool = prog.get("last_tool")
                if self._qc_flags:
                    prog["qc_flags"] = list(self._qc_flags)
                prog.update(step=step, status="running")
                self._write_progress(prog)
                continue

            content = msg.get("content") or ""
            fb = _fallback_tool_call(content, self.dispatch)
            if fb:
                name, args = fb
                print(f"[hermes]   tool (fallback): {name}", flush=True)
                result = self._dispatch_tool(name, args)
                messages.append(
                    {
                        "role": "user",
                        "content": f"[hasil tool {name}]\n{result}\nLanjutkan task.",
                    }
                )
                self._last_tool = name
                if self._qc_flags:
                    prog["qc_flags"] = list(self._qc_flags)
                prog.update(step=step, status="running", last_tool=name)
                self._write_progress(prog)
                continue

            # jawaban akhir — tidak ada tool call
            prog.update(step=step, status="done")
            self._write_progress(prog)
            self._write_out(content, "done")
            self._fire_on_stop("done")
            self._finish_accounting()    # Gap 3: usage.json + ringkasan
            print(f"[hermes] selesai di step {step}. OUT.md ditulis.", flush=True)
            return 0

        prog.update(
            status="max_steps",
            note=f"max-steps ({self.max_steps}) tercapai tanpa jawaban akhir.",
        )
        self._write_progress(prog)
        self._write_out(
            "TIDAK SELESAI: max-steps tercapai sebelum model memberi laporan akhir. "
            "Lihat progress.json untuk status terakhir.",
            "max_steps",
        )
        self._fire_on_stop("max_steps",
                           f"max-steps ({self.max_steps}) tercapai")
        self._finish_accounting()    # Gap 3: usage.json + ringkasan
        print("[hermes] max-steps tercapai.", flush=True)
        return 0


def load_config(path):
    """Baca profil YAML (butuh PyYAML); kembalikan dict kosong bila gagal."""
    try:
        import yaml  # noqa
    except ImportError:
        sys.stderr.write("peringatan: PyYAML tidak ada — --config diabaikan.\n")
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="hermes-agent v1 — ReAct loop (otak: model 9router)."
    )
    ap.add_argument("--task", required=True, help="tugas untuk agent")
    ap.add_argument("--outdir", required=True, help="direktori output (OUT.md + progress.json)")
    ap.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"override model (default: {DEFAULT_MODEL}). bns/* dan oc/* DITOLAK.",
    )
    ap.add_argument("--max-steps", type=int, default=40, help="maksimal langkah ReAct (default 40)")
    ap.add_argument("--config", default=None, help="profil YAML dari configs/ (opsional)")
    ap.add_argument("--system-prompt", default=None, help="override system prompt (opsional)")
    ap.add_argument(
        "--tidy",
        action="store_true",
        help="rapikan MEMORY.md di --outdir via autoDream lalu keluar "
             "(tanpa menjalankan task).",
    )
    ap.add_argument(
        "--no-exec",
        action="store_true",
        help="mode baca-saja: tool `exec` dibuang dari toolset run ini "
             "(dipakai worker orchestrator).",
    )
    args = ap.parse_args(argv)

    cfg = load_config(args.config) if args.config else {}
    memory_cfg = cfg.get("memory") or {}

    # Fase 4 — orchestrator (OPSIONAL, "by choose", keputusan Bayu 2026-10-09):
    # aktif hanya bila enabled + orchestrator_model terisi. Model kosong /
    # enabled=false -> 100% single-agent seperti sebelumnya.
    orch_cfg = cfg.get("orchestrator") or {}
    orch_wanted = bool(orch_cfg.get("enabled", True)) and bool(
        (orch_cfg.get("orchestrator_model") or "").strip()
    )
    if orch_wanted:
        if os.environ.get("_HERMES_WORKER") == "1":
            sys.stderr.write(
                "ERROR: depth guard — worker dilarang menjalankan "
                "orchestrator.\n"
            )
            return 2
        # Lazy import agar tidak circular (orchestrator tidak import loop).
        from .orchestrator import Orchestrator, OrchestratorError
        try:
            orch = Orchestrator.from_config(
                orch_cfg,
                task=args.task,
                outdir=args.outdir,
                model=cfg.get("model", args.model),
                api_key=get_api_key(),
                compaction_cfg=cfg.get("compaction"),
                memory_cfg=memory_cfg or None,
                permissions_cfg=cfg.get("permissions"),
                system_prompt=args.system_prompt
                or cfg.get("system_prompt"),
            )
            orch.run(args.task, args.outdir)
            return 0
        except OrchestratorError as e:
            # Mandor mati / plan gagal / depth guard: fallback single-agent,
            # run tidak boleh crash karenanya.
            print(
                f"[hermes] orchestrator gagal ({e}) — "
                f"fallback ke single-agent.",
                flush=True,
            )

    if args.tidy:
        os.makedirs(args.outdir, exist_ok=True)
        mem = AgentMemory(
            os.path.join(args.outdir, "MEMORY.md"),
            max_fact_chars=int(memory_cfg.get("max_fact_chars",
                                              DEFAULT_MAX_FACT_CHARS)),
        )
        model = str(memory_cfg.get("summarizer_model",
                                   MEMORY_SUMMARIZER_MODEL))
        _validate_compaction_model(model)  # ag/* saja; bns/*/oc/* -> exit 2
        try:
            new_text = mem.tidy(get_api_key(), model=model)
        except Exception as e:
            sys.stderr.write(f"ERROR: tidy gagal: {e}\n")
            return 1
        print("MEMORY.md setelah tidy:\n" + (new_text or "(kosong)"))
        return 0

    loop = HermesLoop(
        task=args.task,
        outdir=args.outdir,
        model=cfg.get("model", args.model),
        max_steps=int(cfg.get("max_steps", args.max_steps)),
        system_prompt=args.system_prompt or cfg.get("system_prompt", SYSTEM_PROMPT),
        compaction_cfg=cfg.get("compaction"),
        memory_cfg=memory_cfg or None,
        permissions_cfg=cfg.get("permissions"),
        no_exec=args.no_exec,
        hooks_cfg=cfg.get("hooks"),  # Gap 1: blok `hooks:` di YAML
        tasks_cfg=cfg.get("tasks"),  # Gap 2: blok `tasks:` di YAML
        accounting_cfg=cfg.get("accounting"),  # Gap 3: blok `accounting:`
    )
    return loop.run()


if __name__ == "__main__":
    sys.exit(main())
