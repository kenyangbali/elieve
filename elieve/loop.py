#!/usr/bin/env python3
"""elieve.loop — baseline v1 ReAct loop.

Pattern: task -> chat -> tool_calls -> run tools -> feed back -> ...
until the model finishes (final answer without tool calls) or max-steps.

Everything environment-specific is CONFIGURATION (see
configs/example.yaml): the provider (endpoint + key source), the model
allow/forbid policy, the sandbox workspace root, and the default prompt
language. Nothing personal is hardcoded here.

Per-run output (in --outdir):
  OUT.md        — the model's final report
  progress.json — last step (resumability convention)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from . import tools
from .tools import (
    DISPATCH, TOOL_SCHEMAS, ToolError,
    bind_memory, unbind_memory,
    bind_tasks, unbind_tasks,
)
from .permissions import PermissionGate
from .hooks import HookRunner
from .accounting import Accounting
from .checkpoints import (
    CheckpointError,
    DEFAULT_EVERY_N_STEPS,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
    should_save,
)
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
from .providers import (
    ProviderConfig,
    ProviderKeyError,
    check_model_allowed,
    post_chat_completions,
    resolve_api_key,
)
from .prompts import get_system_prompt

MODEL_CALL_DELAY_S = 3


class RateLimited(Exception):
    pass


def get_api_key(provider_cfg=None):
    """Resolve the provider API key (CLI wrapper: exit 2 on failure).

    Library equivalent: providers.resolve_api_key (raises ProviderKeyError).
    The key value is never printed or logged.
    """
    try:
        return resolve_api_key(provider_cfg)
    except ProviderKeyError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(2)


def call_model(messages, model, provider_cfg, api_key=None, tools=None):
    """Send a chat completion; return (message, usage).

    `usage` holds prompt_tokens/completion_tokens when the provider
    sends them, else an empty dict (callers fall back to chars/4).
    `tools`: function-call schemas; default is the global TOOL_SCHEMAS
    (read-only workers pass a subset without `exec`).
    """
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS if tools is None else tools,
        "tool_choice": "auto",
        "stream": False,
    }
    status, text = post_chat_completions(
        provider_cfg, payload, api_key=api_key)
    if status == 429:
        raise RateLimited(
            "HTTP 429 from provider — stopping cleanly without retry.")
    if status >= 400:
        raise RuntimeError(f"provider HTTP {status}: {text[:500]}")
    try:
        data = json.loads(text)
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return message, usage
    except Exception as e:
        raise RuntimeError(
            f"provider response could not be parsed: {e} :: {text[:300]}")


def _fallback_tool_call(text: str, dispatch=None):
    """Fallback when the model skips native function calling:
    a ```tool {"name": ..., "arguments": {...}}``` block.

    `dispatch`: the allowed tool mapping (default is the global DISPATCH;
    read-only workers pass a subset without `exec`)."""
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


_TASKS_BLOCK_RE = re.compile(r"\n\n## Daftar task\n.*$", re.S)


def _strip_tasks_block(content: str) -> str:
    """Buang blok '## Daftar task' injeksi turn lama (untuk resume).

    Loop menyuntik blok fresh tiap turn; tanpa strip, resume akan
    menumpuk blok basi di system prompt. Hanya blok di AKHIR yang dibuang.
    """
    return _TASKS_BLOCK_RE.sub("", content or "")


class ElieveLoop:
    """One ReAct session: send the task, iterate tool calls to a final answer."""

    def __init__(self, task, outdir, model=None, max_steps=40,
                 system_prompt=None, compaction_cfg=None,
                 memory_cfg=None, permissions_cfg=None, no_exec=False,
                 hooks_cfg=None, tasks_cfg=None,
                 accounting_cfg=None, provider_cfg=None, model_policy=None,
                 checkpoints_cfg=None, resume_record=None):
        self.task = task
        self.outdir = outdir
        # Provider + model policy come from configuration (never hardcoded).
        self.provider_cfg = (provider_cfg if provider_cfg is not None
                             else ProviderConfig())
        self.model_policy = dict(model_policy or {})
        raw_model = (model or self.provider_cfg.model or "").strip()
        if not raw_model:
            raise ValueError(
                "no model configured: pass --model, set top-level 'model:' "
                "in the config, or set provider.model.")
        # Policy-based validation (replaces the old hardcoded allow/forbid).
        self.model = check_model_allowed(raw_model, self.model_policy)
        self.max_steps = max_steps
        self.system_prompt = (system_prompt if system_prompt is not None
                              else get_system_prompt("en"))
        self.api_key = get_api_key(self.provider_cfg)
        # Gap 1 — hook lifecycle (deterministic, the model cannot skip it).
        # hooks_cfg None/empty -> every event is a no-op.
        self.hooks = HookRunner(hooks_cfg, outdir=outdir)
        self._step = 0
        self._last_tool = None
        self._qc_flags = []
        # Per-run toolset (Phase 4): read-only workers drop `exec`.
        self.no_exec = bool(no_exec)
        self.dispatch = dict(DISPATCH)
        if self.no_exec:
            self.dispatch.pop("exec", None)
        self.tool_schemas = [
            s for s in TOOL_SCHEMAS
            if s.get("function", {}).get("name") in self.dispatch
        ]
        # Compaction config (Phase 1); safe defaults when config is absent.
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
        # Early validation: the summarizer must satisfy the model policy.
        check_model_allowed(self.compaction["summarizer_model"],
                            self.model_policy)
        # Gap 3 — token/cost accounting (elieve/accounting.py).
        # accounting_cfg None/empty -> enabled by default, cap inactive.
        # context_limit comes from the compaction config (chars/4 fallback
        # when the provider sends no usage).
        self.acct = Accounting(
            accounting_cfg,
            outdir=outdir,
            context_limit=self.compaction["context_limit"],
        )
        # Memory config (Phase 2).
        self.memory_cfg = {
            "enabled": True,
            "tidy_every_runs": DEFAULT_TIDY_EVERY_RUNS,
            "max_fact_chars": DEFAULT_MAX_FACT_CHARS,
            "summarizer_model": MEMORY_SUMMARIZER_MODEL,
        }
        if memory_cfg:
            self.memory_cfg.update(memory_cfg)
        check_model_allowed(self.memory_cfg["summarizer_model"],
                            self.model_policy)
        self.memory = None  # AgentMemory is created in run() (needs outdir)
        # Permission gate config (Phase 3). The classifier is OPTIONAL:
        # an empty classifier_model means auto-off (layer-0 regex only).
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
            provider_cfg=self.provider_cfg,
            model_policy=self.model_policy,
        )
        # Gap 2 — structured task tracking (elieve/tasks.py).
        # tasks_cfg None/empty -> enabled by default.
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
        # Gap 4 — checkpoints / resume percakapan (elieve/checkpoints.py).
        # checkpoints_cfg None/empty -> enabled by default, tiap 10 step.
        self.checkpoints = {
            "enabled": True,
            "every_n_steps": DEFAULT_EVERY_N_STEPS,
        }
        if checkpoints_cfg:
            self.checkpoints.update(checkpoints_cfg)
        # Resume dari snapshot: step + messages + state, bukan dari nol.
        self._resume_record = resume_record
        if resume_record is not None:
            self._restore_resume_state(resume_record)

    # -- output ------------------------------------------------------

    def _write_progress(self, payload):
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(os.path.join(self.outdir, "progress.json"), "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _write_out(self, content, status):
        header = (
            "# Elieve — hasil\n\n"
            f"- Task: {self.task}\n"
            f"- Model: {self.model}\n"
            f"- Status: {status}\n"
            f"- Waktu: {datetime.now(timezone.utc).isoformat()}\n\n---\n\n"
        )
        with open(os.path.join(self.outdir, "OUT.md"), "w") as f:
            f.write(header + (content or "(kosong)"))

    # -- accounting (Gap 3) ------------------------------------------

    def _accounting_after_call(self, usage, messages, step):
        """Record usage after each call_model + guardrails.

        - record() into the UsageTracker (chars/4 estimate when the
          provider sends no usage; flagged estimated=True);
        - context warning when prompt_tokens >= context_warn_pct%;
        - periodic usage.json save every 10 steps (best effort);
        - run_cost_cap check.
        Returns True when the cap is hit -> the caller stops the run
        cleanly (status cost_capped, return 0, no crash).
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
            print(f"[elieve] {self.acct.cap_message()}", flush=True)
            return True
        return False

    # -- checkpoints (Gap 4) -----------------------------------------

    def _checkpoint_state(self):
        """Ringkasan state untuk snapshot: usage + tasks + identitas run."""
        return {
            "task": self.task,
            "model": self.model,
            "max_steps": self.max_steps,
            "usage": self.acct.tracker.to_dict(),
            "tasks": self.tasks.list() if self.tasks else [],
        }

    def _maybe_checkpoint(self, messages, force=False):
        """Simpan checkpoint bila jatuh tempo (tiap N step) / force=True.

        Best effort — kegagalan tulis dicatat ke stdout, TIDAK PERNAH
        crash-kan run (perilaku run default tidak berubah).
        """
        if not self.checkpoints.get("enabled", True):
            return
        if not force and not should_save(
                self._step, self.checkpoints.get("every_n_steps")):
            return
        try:
            path = save_checkpoint(
                self.outdir, self._step, messages,
                state=self._checkpoint_state())
        except Exception as e:  # corrupt config / disk penuh / dsb.
            print(f"[elieve] warning: checkpoint gagal: {e}", flush=True)
        else:
            print(f"[elieve] checkpoint: step {self._step} -> {path}",
                  flush=True)

    def _restore_resume_state(self, record):
        """Kembalikan ringkasan state dari snapshot (usage tracker).

        Task list tidak perlu dipulihkan manual: TaskList sudah membaca
        <outdir>/tasks.json yang sama saat resume ke outdir yang sama.
        Usage tracker diisi ulang agar usage.json kontinu antar resume.
        Best effort — tidak boleh crash-kan resume.
        """
        try:
            saved = (record.get("state") or {}).get("usage") or {}
            models = saved.get("models") or {}
            tracker = self.acct.tracker
            for model, data in models.items():
                if not isinstance(data, dict):
                    continue
                bucket = tracker._bucket(model)
                for k in ("prompt_tokens", "completion_tokens",
                          "total_tokens", "calls", "estimated_calls"):
                    try:
                        bucket[k] = max(0, int(data.get(k) or 0))
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            print(f"[elieve] warning: restore state resume gagal: {e}",
                  flush=True)

    def _finish_accounting(self):
        """Gap 3: write <outdir>/usage.json + print the summary to stdout.

        Called on EVERY run exit path (done/max_steps/error/
        rate_limited/cost_capped). Best effort — must never crash the run.
        """
        if not self.acct.enabled:
            return
        try:
            path = self.acct.save()
        except Exception as e:
            print(f"[accounting] failed to write usage.json: {e}", flush=True)
        else:
            print(f"[elieve] usage.json written: {path}", flush=True)
        print(self.acct.summary_text(), flush=True)

    # -- tool dispatch -----------------------------------------------

    def _run_tool(self, name, args):
        try:
            return str(self.dispatch[name](**args))
        except TypeError as e:
            return f"wrong arguments for {name}: {e}"
        except ToolError as e:
            return f"TOOL REJECTED: {e}"
        except Exception as e:
            return f"tool error ({name}): {e}"

    def _gated_tool(self, name, args):
        """A tool call through the permission gate (Phase 3) BEFORE running.

        Returns (allowed, result_text). A deny/ask verdict means the tool
        does NOT run; the model gets the refusal message and the loop
        continues normally (no crash).
        """
        verdict, reason = self.gate.check(name, args or {})
        if verdict == "deny":
            print(f"[elieve]   gate: DENY {name}: {reason[:120]}", flush=True)
            return False, f"IZIN DITOLAK oleh permission gate: {reason}"
        if verdict == "ask":
            print(f"[elieve]   gate: ASK {name}: {reason[:120]}", flush=True)
            return False, (
                "IZIN DITOLAK oleh permission gate (butuh konfirmasi manusia; "
                f"loop non-interaktif): {reason}"
            )
        return True, self._run_tool(name, args)

    # -- hook lifecycle (Gap 1) ---------------------------------------

    def _hook_ctx_base(self, extra=None):
        """Base ctx for every hook: step, outdir, task, last_tool, etc."""
        ctx = {
            "step": self._step,
            "outdir": self.outdir,
            "task": self.task,
            "last_tool": self._last_tool,
            "qc_flags": list(self._qc_flags),
            # Gap 2: the REAL task summary from TaskList (not a placeholder).
            "tasks_summary": self.tasks.summary() if self.tasks else "",
        }
        if extra:
            ctx.update(extra)
        return ctx

    def _dispatch_tool(self, name, args):
        """Full path of one tool call: PreToolUse -> permission gate ->
        execution -> PostToolUse.

        A blocked PreToolUse (allowed=False) means the tool does NOT run;
        the model gets the block message and the loop continues normally
        (no crash). Returns the tool result text.
        """
        ctx = self._hook_ctx_base({
            "tool_name": name,
            "tool_args": args or {},
        })
        allowed, reasons = self.hooks.pre_tool_use(ctx)
        if not allowed:
            reason = "; ".join(r for r in reasons if r) or "rejected"
            print(
                f"[elieve]   hook PreToolUse: BLOCK {name}: {reason[:160]}",
                flush=True,
            )
            result = f"HOOK DITOLAK oleh PreToolUse: {reason}"
        elif name not in self.dispatch:
            result = f"unknown tool: {name}"
        else:
            print(f"[elieve]   tool: {name} {str(args)[:120]}", flush=True)
            _ok, result = self._gated_tool(name, args)
        post = self.hooks.post_tool_use(
            dict(ctx, tool_output=str(result)))
        flags = post.get("qc_flags") or []
        if flags:
            self._qc_flags.extend(flags)
            print(
                f"[elieve]   hook PostToolUse: qc_flags={flags}",
                flush=True,
            )
        if post.get("blocked"):
            result = (
                "[QC] hook PostToolUse flagged the output "
                "(shell blocking failed).\n" + str(result)
            )
        return result

    def _fire_on_stop(self, status, note=""):
        """OnStop on every loop exit path; must never crash the run."""
        try:
            self.hooks.on_stop(
                self._hook_ctx_base({"status": status, "note": note}))
        except Exception as e:  # belt-and-suspenders
            print(f"[elieve] hook OnStop failed: {e}", flush=True)

    def _fire_on_error(self, err):
        """OnError: log to errors.jsonl + run the OnError hook."""
        try:
            self.hooks.on_error(
                self._hook_ctx_base({"error": str(err)[:1000]}))
        except Exception as e:  # belt-and-suspenders
            print(f"[elieve] hook OnError failed: {e}", flush=True)

    # -- compaction (Phase 1) ------------------------------------------

    def _summarize_for_compaction(self, messages):
        """Summarizer for the 95% pipeline: full_compact via a cheap model."""
        return full_compact(
            messages,
            model=self.compaction["summarizer_model"],
            api_key=self.api_key,
            keep_recent=int(self.compaction["full_compact_keep_recent"]),
            prefix_len=int(self.compaction["prefix_len"]),
            model_policy=self.model_policy,
            provider_cfg=self.provider_cfg,
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
        # Gap 1: PreCompact before compressing, PostCompact after.
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
        if actions:
            # Gap 4: snapshot SEBELUM konteks dimampatkan — memakai titik
            # waktu hook PreCompact yang sama (tanpa mekanisme duplikat).
            # force=True karena ini di luar jadwal periodik tiap-N-step.
            self._maybe_checkpoint(messages, force=True)
        return out, actions

    # -- task tracking (Gap 2) ----------------------------------------

    def _tasks_block(self):
        """The "## Daftar task" block for the system prompt. Empty if none.

        Re-injected FRESH every turn before the model call so task state
        survives compaction: the system prompt sits in the prefix_len zone
        that the compaction pipeline never touches (see elieve/tasks.py).
        """
        if not self.tasks:
            return ""
        summary = self.tasks.summary(
            max_chars=int(self.tasks_cfg["summary_max_chars"]))
        if not summary:
            return ""
        return "\n\n## Daftar task\n" + summary

    # -- memory (Phase 2) --------------------------------------------------

    def _memory_path(self):
        return os.path.join(self.outdir, "MEMORY.md")

    def _memory_counter_path(self):
        return os.path.join(self.outdir, ".memory_counter")

    def _setup_memory(self):
        """Create/bind AgentMemory, autoDream every N runs, recall context."""
        if not self.memory_cfg.get("enabled", True):
            unbind_memory()
            return ""
        self.memory = AgentMemory(
            self._memory_path(),
            max_fact_chars=int(self.memory_cfg["max_fact_chars"]),
        )
        bind_memory(self.memory)
        # autoDream: per-outdir counter; tidy every N runs.
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
            print(f"[elieve] warning: memory counter write failed: {e}",
                  flush=True)
        every = max(1, int(self.memory_cfg["tidy_every_runs"]))
        if counter % every == 0:
            try:
                print(f"[elieve] autoDream: tidying MEMORY.md (run #{counter}) ...",
                      flush=True)
                self.memory.tidy(
                    self.api_key,
                    model=self.memory_cfg["summarizer_model"],
                    model_policy=self.model_policy,
                    provider_cfg=self.provider_cfg,
                )
            except Exception as e:
                print(f"[elieve] autoDream failed (continuing without tidy): {e}",
                      flush=True)
        return self.memory.recall()

    # -- main --------------------------------------------------------

    def run(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        # Phase 2: previous-session memory is injected into the system prompt.
        recalled = self._setup_memory()
        system_prompt = self.system_prompt
        if recalled:
            system_prompt += "\n\n## Ingatan sesi lalu\n" + recalled
        if self.no_exec:
            # Phase 4: read-only worker — the model must know exec is gone.
            system_prompt += (
                "\n\nMODE BACA-SAJA: tool `exec` TIDAK tersedia di sesi ini. "
                "Jangan memanggil atau memintanya; gunakan read_file, "
                "list_dir, grep, dan remember."
            )
        # Gap 2: static system-prompt base; the task block is re-injected
        # fresh each turn (see _tasks_block) so it survives compaction.
        base_system_prompt = system_prompt
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.task},
        ]
        start_step = 0
        if self._resume_record is not None:
            # Gap 4 (--resume): LANJUT dari snapshot — step + messages +
            # state. Bukan mulai dari nol. Blok task lama di-strip agar
            # injeksi fresh tiap turn tidak menumpuk.
            rec = self._resume_record
            start_step = int(rec.get("step") or 0)
            base_system_prompt = _strip_tasks_block(
                (rec.get("messages") or [{}])[0].get("content") or "")
            messages = [dict(m) for m in (rec.get("messages") or [])]
            print(
                f"[elieve] resume dari checkpoint step {start_step} "
                f"({len(messages)} messages).",
                flush=True,
            )
        prog = {
            "task": self.task,
            "model": self.model,
            "outdir": self.outdir,
            "step": start_step,
            "max_steps": self.max_steps,
            "status": "running",
            "last_tool": None,
        }
        if start_step:
            prog["resumed_from"] = start_step
        self._write_progress(prog)

        step = start_step
        while step < self.max_steps:
            step += 1
            self._step = step  # hook ctx always knows the running step
            if step > 1:
                time.sleep(MODEL_CALL_DELAY_S)
            print(
                f"[elieve] step {step}/{self.max_steps} -> {self.model} ...",
                flush=True,
            )
            # Layer 1: micro-compaction every turn BEFORE the request (no LLM)
            messages = self._maybe_micro_compact(messages)
            # Gap 2: inject a fresh task summary into the system prompt each
            # turn. The system prompt sits in the compaction prefix -> it is
            # never cut.
            messages[0]["content"] = base_system_prompt + self._tasks_block()
            try:
                msg, usage = call_model(
                    messages, self.model, self.provider_cfg,
                    api_key=self.api_key, tools=self.tool_schemas)
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
                print(f"[elieve] {e}", flush=True)
                return 0
            except Exception as e:
                prog.update(step=step, status="error", note=str(e)[:300])
                self._write_progress(prog)
                self._fire_on_error(e)       # Gap 1: OnError
                self._fire_on_stop("error", str(e)[:300])
                self._finish_accounting()    # Gap 3: usage.json + summary
                print(f"[elieve] ERROR: {e}", flush=True)
                return 1

            # Gap 3 — accounting after every call_model; stop cleanly when
            # run_cost_cap is hit (status cost_capped, return 0).
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
            # Layers 2/3: threshold pipeline based on response prompt_tokens.
            # When usage is unavailable, estimate from the request just sent.
            prompt_tokens = (usage or {}).get("prompt_tokens")
            if not prompt_tokens:
                prompt_tokens = estimate_tokens(messages)
            messages, compact_actions = self._maybe_threshold_compact(
                messages, prompt_tokens
            )
            for act in compact_actions:
                print(
                    f"[elieve] compaction: {act} "
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
                    # Gap 1: the full tool path via _dispatch_tool
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
                self._maybe_checkpoint(messages)  # Gap 4: tiap N step
                continue

            content = msg.get("content") or ""
            fb = _fallback_tool_call(content, self.dispatch)
            if fb:
                name, args = fb
                print(f"[elieve]   tool (fallback): {name}", flush=True)
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
                self._maybe_checkpoint(messages)  # Gap 4: tiap N step
                continue

            # final answer — no tool calls
            prog.update(step=step, status="done")
            self._write_progress(prog)
            self._write_out(content, "done")
            self._fire_on_stop("done")
            self._finish_accounting()    # Gap 3: usage.json + summary
            print(f"[elieve] selesai di step {step}. OUT.md ditulis.", flush=True)
            return 0

        # Gap 4: checkpoint terakhir agar run bisa di-resume dari sini.
        self._maybe_checkpoint(messages, force=True)
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
        self._finish_accounting()    # Gap 3: usage.json + summary
        print("[elieve] max-steps tercapai.", flush=True)
        return 0


def load_config(path):
    """Read a YAML profile (needs PyYAML); return {} on failure."""
    try:
        import yaml  # noqa
    except ImportError:
        sys.stderr.write("warning: PyYAML missing — --config ignored.\n")
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="elieve — ReAct loop over an OpenAI-compatible provider."
    )
    ap.add_argument("--task", required=False, default=None,
                    help="task for the agent (optional with --resume: "
                         "taken from the checkpoint when omitted)")
    ap.add_argument("--outdir", required=False, default=None,
                    help="output directory (OUT.md + progress.json); "
                         "not needed with --resume (uses the resume dir)")
    ap.add_argument("--model", default=None,
                    help="model override (default: top-level config 'model:' "
                         "or provider.model)")
    ap.add_argument("--max-steps", type=int, default=40,
                    help="max ReAct steps (default 40)")
    ap.add_argument("--config", default=None,
                    help="YAML profile from configs/ (optional)")
    ap.add_argument("--system-prompt", default=None,
                    help="system prompt override (optional)")
    ap.add_argument("--workspace", default=None,
                    help="sandbox workspace root "
                         "(overrides config workspace_root)")
    ap.add_argument("--lang", default=None, choices=["en", "id"],
                    help="default prompt language: en|id "
                         "(overrides config language)")
    ap.add_argument("--profile", default=None, choices=["default", "hunter"],
                    help="prompt persona profile: default (generic) | hunter "
                         "(bug-hunter; overrides config profile)")
    ap.add_argument(
        "--tidy",
        action="store_true",
        help="tidy MEMORY.md in --outdir via autoDream, then exit "
             "(no task is run).",
    )
    ap.add_argument(
        "--no-exec",
        action="store_true",
        help="read-only mode: drop the `exec` tool from this run's toolset "
             "(used by orchestrator workers).",
    )
    ap.add_argument(
        "--plan",
        action="store_true",
        help="Gap 6: plan mode — riset read-only (exec dipaksa mati), "
             "tulis <outdir>/PLAN.md dari jawaban akhir, lalu berhenti "
             "(exit 0). Tidak ada fase eksekusi.",
    )
    ap.add_argument(
        "--resume",
        default=None, metavar="OUTDIR",
        help="Gap 4: continue from the latest checkpoint in OUTDIR "
             "(step + messages + state) instead of starting from zero. "
             "Outputs keep going into OUTDIR; --task defaults to the "
             "checkpoint's task.",
    )
    ap.add_argument(
        "--list-checkpoints",
        action="store_true",
        help="Gap 4: list checkpoints in --outdir, then exit 0.",
    )
    args = ap.parse_args(argv)

    # Gap 4: --list-checkpoints exits before any config/model/key work.
    if args.list_checkpoints:
        if not args.outdir:
            sys.stderr.write("ERROR: --list-checkpoints butuh --outdir.\n")
            return 2
        found = list_checkpoints(args.outdir)
        if found:
            for item in found:
                print(f"step {item['step']}: {item['path']}")
        else:
            print("(tidak ada checkpoint)")
        return 0

    # Gap 4: --resume loads the latest checkpoint from the given outdir.
    # Corrupt/missing -> clear error + non-zero exit (never a mystery).
    resume_record = None
    if args.resume:
        try:
            resume_record = load_checkpoint(args.resume)
        except CheckpointError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        outdir = os.path.abspath(args.resume)
        task = (args.task or resume_record.get("task") or "").strip()
        if not task:
            sys.stderr.write(
                "ERROR: --resume dipakai tanpa --task dan checkpoint "
                "tidak menyimpan task — isi --task.\n")
            return 2
    else:
        if not args.outdir:
            sys.stderr.write(
                "ERROR: --outdir wajib diisi (atau pakai --resume).\n")
            return 2
        if not args.task:
            sys.stderr.write(
                "ERROR: --task wajib diisi (atau pakai --resume).\n")
            return 2
        outdir = args.outdir
        task = args.task

    cfg = load_config(args.config) if args.config else {}
    memory_cfg = cfg.get("memory") or {}

    # Provider: endpoint + key source + default model (elieve/providers.py).
    # All environment specifics live in config — nothing hardcoded here.
    provider_cfg = ProviderConfig.from_dict(cfg.get("provider") or {})
    model_policy = cfg.get("model_policy") or {}

    # Sandbox roots: the configured workspace (+ /tmp, always allowed).
    workspace_root = os.path.abspath(
        args.workspace or cfg.get("workspace_root") or "./workspace")
    tools.configure_roots(workspace_root)

    # Default prompt language (config 'system_prompt' still overrides).
    lang = (args.lang or cfg.get("language") or "en").strip().lower()
    if lang not in ("en", "id"):
        sys.stderr.write(
            f"warning: unknown language {lang!r} — falling back to 'en'.\n")
        lang = "en"

    # Effective model: CLI flag > top-level config 'model:' > provider.model.
    model = (args.model or cfg.get("model") or provider_cfg.model or "").strip()

    # Prompt persona profile: 'default' (generic) or 'hunter' (bug-hunter).
    # Explicit config/CLI system_prompt still wins over everything.
    profile = (args.profile or cfg.get("profile") or "default").strip().lower()
    if profile not in ("default", "hunter"):
        sys.stderr.write(
            f"warning: unknown profile {profile!r} — falling back to 'default'.\n")
        profile = "default"

    # Default system prompt for the language+profile; explicit config/CLI wins.
    default_prompt = get_system_prompt(lang, workspace_root=workspace_root,
                                       profile=profile)
    system_prompt = (args.system_prompt or cfg.get("system_prompt")
                     or default_prompt)

    # Phase 4 — orchestrator (OPTIONAL & PLUGGABLE): active only when
    # enabled AND orchestrator_model is set. Empty model / enabled=false
    # -> 100% single-agent, as before.
    orch_cfg = cfg.get("orchestrator") or {}
    # Gap 6 — Plan mode: riset read-only -> PLAN.md -> berhenti (exit 0).
    # Berdiri sendiri: tanpa orchestrator dan tanpa MCP (tool eksternal
    # bisa mengeksekusi, jadi tidak dimuat di mode read-only ini).
    # --plan tidak mengubah perilaku default sama sekali.
    if args.plan:
        from .planmode import resolve_plan_model, run_plan
        plan_cfg = cfg.get("plan") or {}
        try:
            plan_model = resolve_plan_model(
                plan_cfg, model, model_policy)
        except ValueError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        return run_plan(
            task, outdir, plan_model,
            provider_cfg=provider_cfg,
            model_policy=model_policy,
            max_steps=int(plan_cfg.get("max_steps")
                          or cfg.get("max_steps", args.max_steps)),
            lang=lang,
            workspace_root=workspace_root,
            profile=profile,
            system_prompt=args.system_prompt or cfg.get("system_prompt"),
            compaction_cfg=cfg.get("compaction"),
            memory_cfg=memory_cfg or None,
            permissions_cfg=cfg.get("permissions"),
            hooks_cfg=cfg.get("hooks"),
            tasks_cfg=cfg.get("tasks"),
            accounting_cfg=cfg.get("accounting"),
            checkpoints_cfg=cfg.get("checkpoints"),
            resume_record=resume_record,
        )
    orch_wanted = bool(orch_cfg.get("enabled", True)) and bool(
        (orch_cfg.get("orchestrator_model") or "").strip()
    )
    if orch_wanted and resume_record is not None:
        # Gap 4: resume selalu single-agent (orchestrator tidak tahu
        # cara melanjutkan snapshot); catat, jangan crash.
        print("[elieve] --resume: orchestrator dilewati "
              "(lanjut single-agent).", flush=True)
        orch_wanted = False
    if orch_wanted:
        if os.environ.get("_ELIEVE_WORKER") == "1":
            sys.stderr.write(
                "ERROR: depth guard — workers may not run the orchestrator.\n"
            )
            return 2
        # Lazy import to avoid a cycle (orchestrator never imports loop).
        from .orchestrator import Orchestrator, OrchestratorError
        try:
            orch = Orchestrator.from_config(
                orch_cfg,
                task=task,
                outdir=outdir,
                model=model,
                provider_cfg=provider_cfg,
                model_policy=model_policy,
                workspace_root=workspace_root,
                language=lang,
                compaction_cfg=cfg.get("compaction"),
                memory_cfg=memory_cfg or None,
                permissions_cfg=cfg.get("permissions"),
                system_prompt=args.system_prompt
                or cfg.get("system_prompt"),
            )
            orch.run(task, outdir)
            return 0
        except OrchestratorError as e:
            # Dead planner / failed plan / depth guard: fall back to
            # single-agent; the run must not crash because of this.
            print(
                f"[elieve] orchestrator failed ({e}) — "
                f"falling back to single-agent.",
                flush=True,
            )

    if args.tidy:
        os.makedirs(outdir, exist_ok=True)
        mem = AgentMemory(
            os.path.join(outdir, "MEMORY.md"),
            max_fact_chars=int(memory_cfg.get("max_fact_chars",
                                              DEFAULT_MAX_FACT_CHARS)),
        )
        tidy_model = str(memory_cfg.get("summarizer_model",
                                        MEMORY_SUMMARIZER_MODEL))
        try:
            check_model_allowed(tidy_model, model_policy)
        except ValueError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        try:
            new_text = mem.tidy(get_api_key(provider_cfg), model=tidy_model,
                                model_policy=model_policy,
                                provider_cfg=provider_cfg)
        except Exception as e:
            sys.stderr.write(f"ERROR: tidy failed: {e}\n")
            return 1
        print("MEMORY.md after tidy:\n" + (new_text or "(empty)"))
        return 0

    # Gap 5 — MCP client (elieve/mcp.py): start server yang dikonfigurasi
    # (blok `mcp:` di YAML dan/atau .mcp.json di cwd) lalu daftarkan tiap
    # tool-nya sebagai mcp__<server>__<tool> di DISPATCH + TOOL_SCHEMAS.
    # Tanpa config -> bind_mcp() no-op (return None): loop jalan persis
    # seperti biasa. Server dimatikan rapi di finally (semua jalur keluar,
    # termasuk SystemExit dari resolusi API key).
    try:
        mcp_cleanup = tools.bind_mcp(cfg.get("mcp"), cwd=os.getcwd())
    except Exception as e:  # bug tak terduga di binding -> fail-open
        sys.stderr.write(
            "[mcp] warning: bind gagal ({}); lanjut tanpa MCP.\n".format(e))
        mcp_cleanup = None
    try:
        try:
            loop = ElieveLoop(
                task=task,
                outdir=outdir,
                model=model,
                max_steps=int(cfg.get("max_steps", args.max_steps)),
                system_prompt=system_prompt,
                compaction_cfg=cfg.get("compaction"),
                memory_cfg=memory_cfg or None,
                permissions_cfg=cfg.get("permissions"),
                no_exec=args.no_exec,
                hooks_cfg=cfg.get("hooks"),  # Gap 1: `hooks:` block in YAML
                tasks_cfg=cfg.get("tasks"),  # Gap 2: `tasks:` block in YAML
                accounting_cfg=cfg.get("accounting"),  # Gap 3: `accounting:` block
                checkpoints_cfg=cfg.get("checkpoints"),  # Gap 4: `checkpoints:` block
                resume_record=resume_record,  # Gap 4: --resume
                provider_cfg=provider_cfg,
                model_policy=model_policy,
            )
        except ValueError as e:
            # Config/CLI model or summarizer model violates the model policy.
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        return loop.run()
    finally:
        if mcp_cleanup is not None:
            try:
                mcp_cleanup()
            except Exception as e:
                sys.stderr.write(
                    "[mcp] warning: cleanup gagal: {}\n".format(e))


if __name__ == "__main__":
    sys.exit(main())
