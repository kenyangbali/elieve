#!/usr/bin/env python3
"""Gap 1 (docs/GAP-AUDIT.md G1) — hook lifecycle system.

Claude Code punya 30+ event hook berisi perintah deterministik yang
**tidak bisa di-skip model**. Elieve punya modul ini sebagai jawabannya.

Event deterministik:
  PreToolUse   — sebelum tool dijalankan (SEBELUM permission gate).
                 Boleh memblokir tool call secara deterministik.
  PostToolUse  — sesudah tool selesai. Boleh menandai qc_flags; tidak
                 memblokir (kecuali shell `blocking: true` yang gagal).
  PreCompact   — sebelum pipeline compaction memampatkan konteks.
  PostCompact  — sesudah compaction.
  OnStop       — di SEMUA jalur keluar loop (done, max_steps, error,
                 rate_limited).
  OnError      — saat exception di loop; selalu dicatat ke errors.jsonl.

Definisi hook via YAML (blok `hooks:`):
    hooks:
      PreToolUse: [{action: "block_destructive_exec"}]
      PostToolUse: [{action: "qc_tool_output"}]
      PreCompact: [{action: "checkpoint_state"}]
      OnStop: [{action: "persist_state"}]
Tiap aksi EITHER:
  - {action: "nama"}      -> callable python terdaftar di HOOK_ACTIONS
  - {shell: "cmd ..."}    -> perintah shell, timeout default 15 dtk,
                             env ELIEVE_EVENT / ELIEVE_OUTDIR / ELIEVE_STEP.
                             Opsional: {timeout: 5, blocking: true}.

Kontrak kegagalan (dipakai semua event):
  - Hook python yang melempar exception: dicatat ke errors.jsonl,
    TIDAK crash-kan run. Di PreToolUse -> fail-CLOSED (tool diblokir,
    alasan "hook error"); di event lain -> fail-open (run lanjut).
  - Aksi shell yang gagal (rc != 0 / timeout): dicatat, TIDAK crash-kan
    run. Di PreToolUse + blocking: true -> tool diblokir (fail-closed).
  - Nama action yang tidak terdaftar / event tak dikenal / spec rusak:
    error JELAS (ValueError) saat HookRunner dibangun — sebelum run.

Semua aksi di HOOK_ACTIONS menerima SATU argumen: dict ctx dengan kunci
umum: event, step, outdir, task, tool_name, tool_args, tool_output,
error, status, note, compact_actions, tasks_summary, last_tool.
Implementasi original, tanpa LLM di jalur blokir (deterministik murni).
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone

log = logging.getLogger("elieve.hooks")

EVENTS = ("PreToolUse", "PostToolUse", "PreCompact",
          "PostCompact", "OnStop", "OnError")

DEFAULT_SHELL_TIMEOUT_S = 15
QC_OUTPUT_MAX_CHARS = 12000


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _outdir_of(ctx, fallback):
    d = (ctx or {}).get("outdir") or fallback
    if not d:
        d = os.path.join(tempfile.gettempdir(), "elieve-hooks")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _append_jsonl(path, record):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("hooks: gagal tulis %s: %s", path, e)


# ------------------------------------------------------------------
# Aksi bawaan (HOOK_ACTIONS)
# ------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS = [
    (r"\brm\s+-[a-zA-Z]*r", "rm -rf (rekursif)"),
    (r"\bmkfs\b", "mkfs"),
    (r"\bdd\b[^;&|]*\bif=", "dd if="),
    (r"\bdd\b[^;&|]*\bof=/dev/", "dd ke device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", "fork bomb"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutdown/reboot"),
]


def block_destructive_exec(ctx):
    """PreToolUse: tolak exec dengan pola destruktif. Deterministik, tanpa LLM.

    Return (allowed: bool, reason: str)."""
    if (ctx or {}).get("tool_name") != "exec":
        return True, ""
    cmd = str(((ctx or {}).get("tool_args") or {}).get("command") or "")
    for pat, label in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, cmd):
            return False, (
                f"hook block_destructive_exec: pola destruktif "
                f"terdeteksi ({label}) pada: {cmd[:160]}"
            )
    return True, ""


def qc_tool_output(ctx):
    """PostToolUse: tandai output tool yang mencurigakan.

    Return dict {"qc_flags": [...]}; kosong bila bersih. Tidak memblokir."""
    out = str((ctx or {}).get("tool_output") or "")
    flags = []
    if len(out) > QC_OUTPUT_MAX_CHARS:
        flags.append(f"output_oversize:{len(out)}")
    if "Traceback" in out:
        flags.append("traceback_found")
    return {"qc_flags": flags}


def checkpoint_state(ctx):
    """PreCompact: tulis checkpoint state ke <outdir>/.precompact.json.

    Berisi step + timestamp + ringkasan tasks. Return dict info file."""
    ctx = ctx or {}
    outdir = _outdir_of(ctx, None)
    path = os.path.join(outdir, ".precompact.json")
    record = {
        "ts": _utcnow(),
        "event": "PreCompact",
        "step": ctx.get("step", 0),
        "tasks_summary": ctx.get("tasks_summary") or "",
        "last_tool": ctx.get("last_tool"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except OSError as e:
        return {"error": f"checkpoint gagal: {e}"}
    return {"checkpoint": path}


def persist_state(ctx):
    """OnStop: persist state akhir ke <outdir>/.final_state.json.

    Dipanggil di semua jalur keluar loop. Return dict info file."""
    ctx = ctx or {}
    outdir = _outdir_of(ctx, None)
    path = os.path.join(outdir, ".final_state.json")
    record = {
        "ts": _utcnow(),
        "event": "OnStop",
        "step": ctx.get("step", 0),
        "status": ctx.get("status", "unknown"),
        "note": ctx.get("note", ""),
        "task": str(ctx.get("task") or "")[:200],
        "last_tool": ctx.get("last_tool"),
        "qc_flags": list(ctx.get("qc_flags") or []),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except OSError as e:
        return {"error": f"persist gagal: {e}"}
    return {"persisted": path}


def log_error(ctx):
    """OnError: catat error ke <outdir>/errors.jsonl. Return dict info."""
    ctx = ctx or {}
    outdir = _outdir_of(ctx, None)
    record = {
        "ts": _utcnow(),
        "event": "OnError",
        "step": ctx.get("step", 0),
        "error": str(ctx.get("error") or "")[:2000],
    }
    _append_jsonl(os.path.join(outdir, "errors.jsonl"), record)
    return {"logged": True}


HOOK_ACTIONS = {
    "block_destructive_exec": block_destructive_exec,
    "qc_tool_output": qc_tool_output,
    "checkpoint_state": checkpoint_state,
    "persist_state": persist_state,
    "log_error": log_error,
}


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------

def _norm_pre_result(res, name):
    """Normalisasi return aksi PreToolUse -> (allowed: bool, reason: str)."""
    if isinstance(res, (tuple, list)) and len(res) == 2:
        return bool(res[0]), str(res[1] or "")
    raise ValueError(
        f"aksi PreToolUse '{name}' harus return (allowed, reason), "
        f"dapat: {type(res).__name__} {res!r}"
    )


class HookRunner:
    """Jalankan hook per event sesuai blok `hooks:` di config YAML.

    hooks_cfg: dict event -> list spec aksi. Spec: {"action": nama} atau
      {"shell": cmd, "timeout": detik, "blocking": bool}; string polos
      juga diterima sebagai nama action.
    outdir: direktori output default untuk checkpoint/persist/errors.
    Config kosong / None -> semua event no-op (aman).
    """

    def __init__(self, hooks_cfg=None, outdir=None, shell_timeout_s=None):
        self.shell_timeout_s = float(
            shell_timeout_s if shell_timeout_s is not None
            else DEFAULT_SHELL_TIMEOUT_S
        )
        self.outdir = outdir
        self._specs = {e: [] for e in EVENTS}
        for event, actions in (hooks_cfg or {}).items():
            if event not in EVENTS:
                raise ValueError(
                    f"hooks: event '{event}' tidak dikenal. "
                    f"Event valid: {', '.join(EVENTS)}"
                )
            if actions is None:
                continue
            if not isinstance(actions, (list, tuple)):
                raise ValueError(
                    f"hooks.{event}: harus daftar aksi, "
                    f"dapat: {type(actions).__name__}"
                )
            for spec in actions:
                self._specs[event].append(self._parse_action(event, spec))

    def _parse_action(self, event, spec):
        if isinstance(spec, str):
            name = spec.strip()
            if name not in HOOK_ACTIONS:
                raise ValueError(
                    f"hooks.{event}: action '{name}' tidak terdaftar. "
                    f"Tersedia: {', '.join(sorted(HOOK_ACTIONS))}"
                )
            return ("call", name, {})
        if isinstance(spec, dict):
            if "shell" in spec:
                cmd = str(spec.get("shell") or "")
                if not cmd.strip():
                    raise ValueError(
                        f"hooks.{event}: 'shell' kosong — isi perintah shell."
                    )
                try:
                    timeout = float(
                        spec.get("timeout", self.shell_timeout_s))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"hooks.{event}: 'timeout' harus angka, "
                        f"dapat: {spec.get('timeout')!r}"
                    )
                return ("shell", cmd, {
                    "timeout": timeout,
                    "blocking": bool(spec.get("blocking", False)),
                })
            if "action" in spec:
                name = str(spec.get("action") or "").strip()
                if name not in HOOK_ACTIONS:
                    raise ValueError(
                        f"hooks.{event}: action '{name}' tidak terdaftar. "
                        f"Tersedia: {', '.join(sorted(HOOK_ACTIONS))}"
                    )
                return ("call", name, {})
        raise ValueError(
            f"hooks.{event}: spec aksi tidak valid: {spec!r}. "
            f"Pakai {{action: nama}} atau {{shell: cmd}}."
        )

    # -- util internal ---------------------------------------------

    def has(self, event):
        """True bila ada hook terdaftar untuk event."""
        return bool(self._specs.get(event))

    def _record_error(self, event, action_name, err, ctx):
        outdir = _outdir_of(ctx, self.outdir)
        _append_jsonl(os.path.join(outdir, "errors.jsonl"), {
            "ts": _utcnow(),
            "event": f"hook_error@{event}",
            "action": action_name,
            "step": (ctx or {}).get("step", 0),
            "error": str(err)[:1000],
        })
        log.warning("hooks: %s/%s gagal: %s", event, action_name, err)

    def _run_shell(self, cmd, ctx, event, timeout):
        """Jalankan shell hook; return dict hasil (tidak pernah raise)."""
        env = dict(os.environ)
        env["ELIEVE_EVENT"] = event
        env["ELIEVE_OUTDIR"] = _outdir_of(ctx, self.outdir)
        env["ELIEVE_STEP"] = str((ctx or {}).get("step", 0))
        try:
            p = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout,
            )
            return {"rc": p.returncode, "stdout": p.stdout or "",
                    "stderr": p.stderr or "", "timed_out": False}
        except subprocess.TimeoutExpired as e:
            out = e.stdout
            err = e.stderr
            return {"rc": -1,
                    "stdout": out.decode() if isinstance(out, bytes)
                    else (out or ""),
                    "stderr": err.decode() if isinstance(err, bytes)
                    else (err or ""),
                    "timed_out": True}

    # -- event API ---------------------------------------------------

    def pre_tool_use(self, ctx):
        """Return (allowed: bool, reasons: [str]).

        Satu hook return False -> tool DIBLOKIR deterministik.
        Hook yang error -> fail-CLOSED (blokir + catat)."""
        ctx = dict(ctx or {})
        allowed, reasons = True, []
        for kind, target, opts in self._specs["PreToolUse"]:
            try:
                if kind == "call":
                    res = HOOK_ACTIONS[target](
                        dict(ctx, event="PreToolUse"))
                    ok, reason = _norm_pre_result(res, target)
                else:  # shell
                    r = self._run_shell(target, ctx, "PreToolUse",
                                        opts["timeout"])
                    if r["rc"] == 0 and not r["timed_out"]:
                        ok, reason = True, ""
                    elif opts["blocking"]:
                        suffix = "timeout" if r["timed_out"] else \
                            f"rc={r['rc']}"
                        ok, reason = False, (
                            f"shell hook blocking gagal "
                            f"({suffix}): {target[:80]}"
                        )
                    else:
                        self._record_error(
                            "PreToolUse", target,
                            f"shell gagal non-blocking "
                            f"(rc={r['rc']}, timeout={r['timed_out']})",
                            ctx)
                        ok, reason = True, ""
            except Exception as e:  # fail-closed untuk keamanan
                self._record_error("PreToolUse", target, e, ctx)
                ok, reason = False, (
                    f"hook error (fail-closed): {target}: {e}"
                )
            if not ok:
                allowed = False
                if reason:
                    reasons.append(str(reason))
        return allowed, reasons

    def post_tool_use(self, ctx):
        """Return dict {"qc_flags": [...], "blocked": bool}.

        Tidak memblokir tool (sudah jalan); shell blocking:true yang gagal
        hanya menandai blocked=True agar pemanggil mencatat QC."""
        ctx = dict(ctx or {})
        flags, blocked = [], False
        for kind, target, opts in self._specs["PostToolUse"]:
            try:
                if kind == "call":
                    res = HOOK_ACTIONS[target](
                        dict(ctx, event="PostToolUse"))
                    if isinstance(res, dict):
                        flags.extend(res.get("qc_flags") or [])
                    else:
                        raise ValueError(
                            f"aksi PostToolUse '{target}' harus return dict, "
                            f"dapat: {type(res).__name__}"
                        )
                else:  # shell
                    r = self._run_shell(target, ctx, "PostToolUse",
                                        opts["timeout"])
                    if r["rc"] != 0 or r["timed_out"]:
                        self._record_error(
                            "PostToolUse", target,
                            f"shell gagal (rc={r['rc']}, "
                            f"timeout={r['timed_out']})", ctx)
                        if opts["blocking"]:
                            blocked = True
                            flags.append(
                                f"shell_blocking_failed:{target[:40]}")
            except Exception as e:
                self._record_error("PostToolUse", target, e, ctx)
        return {"qc_flags": flags, "blocked": blocked}

    def _run_simple(self, event, ctx):
        """Event tanpa semantik blokir: jalankan semua, kumpulkan info."""
        ctx = dict(ctx or {})
        info = {}
        for kind, target, opts in self._specs[event]:
            try:
                if kind == "call":
                    res = HOOK_ACTIONS[target](dict(ctx, event=event))
                    if isinstance(res, dict):
                        info.update(res)
                else:
                    r = self._run_shell(target, ctx, event, opts["timeout"])
                    if r["rc"] != 0 or r["timed_out"]:
                        self._record_error(
                            event, target,
                            f"shell gagal (rc={r['rc']}, "
                            f"timeout={r['timed_out']})", ctx)
                    else:
                        info[f"shell:{target[:40]}"] = "ok"
            except Exception as e:
                self._record_error(event, target, e, ctx)
        return info

    def pre_compact(self, ctx):
        return self._run_simple("PreCompact", ctx)

    def post_compact(self, ctx):
        return self._run_simple("PostCompact", ctx)

    def on_stop(self, ctx):
        """Persist state akhir; dipanggil di semua jalur keluar loop."""
        ctx = dict(ctx or {})
        try:
            return self._run_simple("OnStop", ctx)
        except Exception as e:  # jangan pernah crash-kan penutupan run
            self._record_error("OnStop", "<runner>", e, ctx)
            return {}

    def on_error(self, ctx):
        """Selalu catat error ke errors.jsonl, lalu jalankan hook OnError."""
        ctx = dict(ctx or {})
        try:
            log_error(dict(ctx, event="OnError",
                           outdir=ctx.get("outdir") or self.outdir))
            return self._run_simple("OnError", ctx)
        except Exception as e:
            self._record_error("OnError", "<runner>", e, ctx)
            return {}
