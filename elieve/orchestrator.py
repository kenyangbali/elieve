"""Phase 4 — multi-agent orchestration (full implementation).

Problem solved: one agent for a large audit is slow and tunnel-visioned.
The orchestrator splits a task into independent subtasks, then runs N
ElieveLoop workers in parallel (isolated subprocesses).

Design (see docs/ARCHITECTURE.md §4):
  - OPTIONAL & PLUGGABLE ("by choose"): the user freely picks the model
    and API for the planner and for the workers.
  - AUTO-ON: `orchestrator_model` set (and enabled) -> the task is split,
    parallel workers are spawned.
  - AUTO-OFF: `orchestrator_model` empty / enabled=false -> 100%
    single-agent behavior, as before. No default change.
  - Planner DEAD (timeout/error): OrchestratorError -> the caller
    (loop.main) falls back to single-agent + warning. The run does NOT crash.
  - Custom API keys ONLY via env var; never hardcoded.
  - Model rules are policy-driven (providers.check_model_allowed) — no
    hardcoded allow/forbid lists here.

Security:
  - Depth guard: workers (env _ELIEVE_WORKER=1) may NOT run the
    orchestrator -> OrchestratorError. Prevents recursive explosions.
  - Every worker: its own outdir, its own max_steps, a limited toolset
    (--no-exec flag for read-only mode), its own progress.json.
  - One worker failing does not fail the others (failure isolation);
    the merge records every worker's status.

API:
    orch = Orchestrator.from_config(orch_cfg, task=..., outdir=...,
                                    model=..., provider_cfg=...,
                                    model_policy=...)
    out_md = orch.run(task, outdir)   # -> merged OUT.md path
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from .providers import (
    ProviderConfig,
    check_model_allowed,
    post_chat_completions,
)

WORKER_ENV_FLAG = "_ELIEVE_WORKER"

DEFAULT_MAX_WORKERS = 4
DEFAULT_WORKER_MAX_STEPS = 20
DEFAULT_PLAN_TIMEOUT_S = 120
DEFAULT_WORKER_TIMEOUT_S = 1800
DEFAULT_MAX_CONSECUTIVE_PLAN_FAILURES = 1  # one failed plan -> fallback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLAN_SYSTEM_PROMPT = """You are a planner for a team of AI agents. Task: split the TASK
below into INDEPENDENT subtasks (runnable in parallel without waiting on
each other).

Rules:
- At most {max_workers} subtasks. Fewer when the task is genuinely small.
- Each subtask: a short title, a self-contained task description (complete:
  target, scope, what to look for — the worker sees no other context),
  and readonly=true when the subtask only needs reading/grepping
  (no execution).
- Output ONLY valid JSON: an array of objects
  [{"title": "...", "task": "...", "readonly": true|false}]
  No explanation outside the JSON.
"""


class OrchestratorError(Exception):
    """Orchestrator failure (dead planner, depth guard, invalid config).

    The caller (elieve.loop main) catches this and falls back to
    single-agent — the run must not crash because of it.
    """


def is_orchestrator_active(cfg):
    """True when the orchestrator is requested: enabled and model set."""
    cfg = cfg or {}
    return bool(cfg.get("enabled", True)) and bool(
        (cfg.get("orchestrator_model") or "").strip()
    )


def _extract_json_array(text):
    """Grab the first JSON array from text (tolerates code fences)."""
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    candidate = m.group(1) if m else text
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start < 0 or end <= start:
        raise OrchestratorError("planner response contains no JSON array.")
    try:
        data = json.loads(candidate[start : end + 1])
    except Exception as e:
        raise OrchestratorError(f"subtask JSON invalid: {e}")
    if not isinstance(data, list):
        raise OrchestratorError("planner response is not a JSON array.")
    return data


class Orchestrator:
    """Multi-worker ElieveLoop coordinator (phase 4)."""

    def __init__(self, orchestrator_model, provider_cfg=None,
                 model_policy=None, mandor_provider=None,
                 mandor_policy=None, worker_model="", max_workers=4,
                 worker_max_steps=20, worker_readonly_default=False,
                 worker_timeout_s=DEFAULT_WORKER_TIMEOUT_S,
                 plan_timeout_s=DEFAULT_PLAN_TIMEOUT_S,
                 compaction_cfg=None, memory_cfg=None, permissions_cfg=None,
                 system_prompt=None, language="en", workspace_root=None,
                 plan_fn=None, spawn_fn=None,
                 plan_first=False, plan_model="", recon_plan_fn=None):
        raw = (orchestrator_model or "").strip()
        if not raw:
            raise OrchestratorError(
                "orchestrator_model is empty — orchestrator is off."
            )
        # The main provider serves the workers; the planner may use a
        # dedicated provider (mandor_provider) or fall back to the main one.
        self.provider_cfg = (provider_cfg if provider_cfg is not None
                             else ProviderConfig())
        self.model_policy = dict(model_policy or {})
        self.mandor_provider = (mandor_provider if mandor_provider is not None
                                else self.provider_cfg)
        self.mandor_policy = (dict(mandor_policy) if mandor_policy is not None
                              else self.model_policy)
        # Policy-driven model validation (no hardcoded rules here).
        try:
            self.orchestrator_model = check_model_allowed(
                raw, self.mandor_policy)
        except ValueError as e:
            raise OrchestratorError(str(e))
        self.plan_timeout_s = int(plan_timeout_s)
        self.worker_model = (worker_model or "").strip()
        self.max_workers = max(1, int(max_workers))
        self.worker_max_steps = max(1, int(worker_max_steps))
        self.worker_readonly_default = bool(worker_readonly_default)
        self.worker_timeout_s = int(worker_timeout_s)
        self.compaction_cfg = compaction_cfg
        self.memory_cfg = memory_cfg
        self.permissions_cfg = permissions_cfg
        self.system_prompt = system_prompt
        self.language = (language or "en").strip().lower() or "en"
        self.workspace_root = workspace_root
        # Injectable for unit tests (no network/subprocess).
        self._plan_fn = plan_fn
        self._spawn_fn = spawn_fn
        # Gap 6 — recon gate: plan read-only sebelum worker di-spawn.
        # plan_first=false/absen -> perilaku lama, tanpa perubahan.
        self.plan_first = bool(plan_first)
        self.plan_model = (plan_model or "").strip()
        self._recon_plan_fn = recon_plan_fn

    @classmethod
    def from_config(cls, orch_cfg, *, task, outdir, model, provider_cfg,
                    model_policy=None, workspace_root=None, language="en",
                    compaction_cfg=None, memory_cfg=None, permissions_cfg=None,
                    system_prompt=None, plan_fn=None, spawn_fn=None,
                    recon_plan_fn=None):
        """Build from the `orchestrator:` YAML block + runtime values.

        The planner normally shares the main provider; an `orchestrator:`
        sub-block `provider:` (or the legacy `orchestrator_api_base` /
        `orchestrator_api_key_env` keys) may dedicate a different endpoint
        to the planner.
        """
        orch_cfg = orch_cfg or {}
        mandor_provider = None
        mandor_policy = model_policy
        sub = orch_cfg.get("provider")
        if isinstance(sub, dict) and sub:
            mandor_provider = ProviderConfig.from_dict(sub)
            mandor_policy = orch_cfg.get("model_policy", model_policy)
        else:
            legacy_base = (orch_cfg.get("orchestrator_api_base") or "").strip()
            legacy_env = (orch_cfg.get("orchestrator_api_key_env") or "").strip()
            if legacy_base:
                # Legacy keys: dedicated custom endpoint for the planner.
                if not legacy_env:
                    raise OrchestratorError(
                        "orchestrator_api_base is set but "
                        "orchestrator_api_key_env is empty — custom keys "
                        "must come via env var.")
                if not os.environ.get(legacy_env):
                    raise OrchestratorError(
                        f"env var {legacy_env!r} is empty/not set.")
                mandor_provider = ProviderConfig(
                    base_url=legacy_base,
                    api_key_env=legacy_env,
                    model=(orch_cfg.get("orchestrator_model") or "").strip(),
                    timeout_s=orch_cfg.get("plan_timeout_s",
                                           DEFAULT_PLAN_TIMEOUT_S),
                )
        return cls(
            orchestrator_model=orch_cfg.get("orchestrator_model", ""),
            provider_cfg=provider_cfg,
            model_policy=model_policy,
            mandor_provider=mandor_provider,
            mandor_policy=mandor_policy,
            worker_model=orch_cfg.get("worker_model", "") or model,
            max_workers=orch_cfg.get("max_workers", DEFAULT_MAX_WORKERS),
            worker_max_steps=orch_cfg.get(
                "worker_max_steps", DEFAULT_WORKER_MAX_STEPS),
            worker_readonly_default=orch_cfg.get(
                "worker_readonly_default", False),
            worker_timeout_s=orch_cfg.get(
                "worker_timeout_s", DEFAULT_WORKER_TIMEOUT_S),
            plan_timeout_s=orch_cfg.get(
                "plan_timeout_s", DEFAULT_PLAN_TIMEOUT_S),
            compaction_cfg=compaction_cfg,
            memory_cfg=memory_cfg,
            permissions_cfg=permissions_cfg,
            system_prompt=system_prompt,
            language=language,
            workspace_root=workspace_root,
            plan_fn=plan_fn,
            spawn_fn=spawn_fn,
            # Gap 6 — recon gate (default off: perilaku lama tanpa perubahan).
            plan_first=bool(orch_cfg.get("plan_first", False)),
            plan_model=(orch_cfg.get("plan_model") or "").strip(),
            recon_plan_fn=recon_plan_fn,
        )

    # -- planning ----------------------------------------------------

    def _call_mandor(self, messages, timeout):
        payload = {
            "model": self.orchestrator_model,
            "messages": messages,
            "stream": False,
        }
        try:
            status, text = post_chat_completions(
                self.mandor_provider, payload, timeout=timeout)
        except Exception as e:
            raise OrchestratorError(f"planner unreachable: {e}")
        if status == 429:
            raise OrchestratorError("planner rate-limited (HTTP 429).")
        if status >= 400:
            raise OrchestratorError(f"planner HTTP {status}: {text[:300]}")
        try:
            data = json.loads(text)
            return data["choices"][0]["message"].get("content") or ""
        except Exception as e:
            raise OrchestratorError(f"planner response could not be parsed: {e}")

    def plan(self, task):
        """Split the task into independent subtasks (at most max_workers).

        Returns a list of {"title", "task", "readonly"}. Raises
        OrchestratorError when the planner is dead / the response invalid.
        """
        if self._plan_fn is not None:
            try:
                subtasks = self._plan_fn(task)
            except OrchestratorError:
                raise
            except Exception as e:
                raise OrchestratorError(f"planning failed: {e}")
        else:
            content = self._call_mandor(
                [
                    {"role": "system",
                     "content": PLAN_SYSTEM_PROMPT.format(
                         max_workers=self.max_workers)},
                    {"role": "user", "content": task},
                ],
                self.plan_timeout_s,
            )
            subtasks = _extract_json_array(content)
        cleaned = []
        for s in subtasks[: self.max_workers]:
            if not isinstance(s, dict):
                continue
            title = str(s.get("title") or "subtask").strip()[:120]
            subtask_text = str(s.get("task") or "").strip()
            if not subtask_text:
                continue
            cleaned.append({
                "title": title,
                "task": subtask_text,
                "readonly": bool(s.get("readonly",
                                       self.worker_readonly_default)),
            })
        if not cleaned:
            raise OrchestratorError(
                "planner produced no valid subtasks."
            )
        return cleaned

    # -- recon gate (Gap 6) --------------------------------------------

    def _run_recon_gate(self, task, outdir):
        """Fase plan read-only SEBELUM worker mahal di-spawn.

        Memakai mekanisme plan yang sama dengan CLI --plan
        (elieve.planmode.run_plan): toolset tanpa exec, menulis PLAN.md.
        Worker baru di-spawn setelah PLAN.md ada. Gagal -> OrchestratorError
        (caller fallback ke single-agent; run tidak crash).

        Returns dict info recon untuk dicatat di progress.json.
        """
        from .planmode import run_plan
        recon_outdir = os.path.join(outdir, "recon-plan")
        recon_model = None
        plan_md = None
        if self._recon_plan_fn is not None:
            # Injectable untuk unit test (tanpa network).
            plan_md = self._recon_plan_fn(task, recon_outdir)
        else:
            raw = (self.plan_model or self.worker_model
                   or self.orchestrator_model)
            try:
                recon_model = check_model_allowed(raw, self.model_policy)
            except ValueError as e:
                raise OrchestratorError(
                    f"recon gate: model plan ditolak policy: {e}")
            print(f"[orchestrator] recon gate: plan read-only via "
                  f"{recon_model} ...", flush=True)
            rc = run_plan(
                task, recon_outdir, recon_model,
                provider_cfg=self.provider_cfg,
                model_policy=self.model_policy,
                lang=self.language,
                workspace_root=self.workspace_root,
                compaction_cfg=self.compaction_cfg,
                memory_cfg=self.memory_cfg,
                permissions_cfg=self.permissions_cfg,
                system_prompt=self.system_prompt,
            )
            if rc == 0:
                plan_md = os.path.join(recon_outdir, "PLAN.md")
        if not plan_md or not os.path.isfile(plan_md):
            raise OrchestratorError(
                "recon gate: fase plan read-only gagal "
                "(PLAN.md tidak ada) — worker tidak di-spawn.")
        # Bukti gate lolos di root outdir: worker di-spawn SETELAH ini ada.
        dest = os.path.join(outdir, "PLAN.md")
        shutil.copy(plan_md, dest)
        print(f"[orchestrator] recon gate lolos: {dest}", flush=True)
        return {"outdir": recon_outdir, "plan_md": dest,
                "model": recon_model}

    # -- worker spawn --------------------------------------------------

    def _worker_config_path(self, worker_outdir):
        """Write worker-config.yaml (best-effort; needs PyYAML).

        The provider / model_policy / workspace blocks are forwarded so a
        worker runs against ANY provider — not just a hardcoded default.
        """
        cfg = {
            "model": self.worker_model,
            "max_steps": self.worker_max_steps,
            # Workers must not become planners (defense in depth; the
            # primary depth guard is env _ELIEVE_WORKER=1).
            "orchestrator": {"enabled": False, "orchestrator_model": ""},
            "provider": self.provider_cfg.to_dict(),
            "model_policy": {
                "allow": list(self.model_policy.get("allow") or []),
                "forbid": list(self.model_policy.get("forbid") or []),
            },
            "language": self.language,
        }
        if self.workspace_root:
            cfg["workspace_root"] = self.workspace_root
        if self.compaction_cfg is not None:
            cfg["compaction"] = self.compaction_cfg
        if self.memory_cfg is not None:
            cfg["memory"] = self.memory_cfg
        if self.permissions_cfg is not None:
            cfg["permissions"] = self.permissions_cfg
        try:
            import yaml  # noqa
        except ImportError:
            return None
        path = os.path.join(worker_outdir, "worker-config.yaml")
        os.makedirs(worker_outdir, exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        return path

    def _spawn_worker(self, index, subtask, worker_outdir):
        """Run one worker as a subprocess; return a result dict."""
        if self._spawn_fn is not None:
            return self._spawn_fn(index, subtask, worker_outdir)
        os.makedirs(worker_outdir, exist_ok=True)
        cfg_path = self._worker_config_path(worker_outdir)
        cmd = [
            sys.executable, "-m", "elieve.loop",
            "--task", subtask["task"],
            "--outdir", worker_outdir,
            "--model", self.worker_model,
            "--max-steps", str(self.worker_max_steps),
        ]
        if cfg_path:
            cmd += ["--config", cfg_path]
        if subtask.get("readonly"):
            cmd.append("--no-exec")
        env = dict(os.environ)
        env[WORKER_ENV_FLAG] = "1"
        started = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, env=env,
                capture_output=True, text=True,
                timeout=self.worker_timeout_s,
            )
            status = "done" if proc.returncode == 0 else "error"
            note = (proc.stderr or "")[-500:] if proc.returncode != 0 else ""
            return {
                "index": index, "title": subtask["title"],
                "status": status, "outdir": worker_outdir,
                "returncode": proc.returncode, "note": note,
                "elapsed_s": round(time.time() - started, 1),
            }
        except subprocess.TimeoutExpired:
            return {
                "index": index, "title": subtask["title"],
                "status": "timeout", "outdir": worker_outdir,
                "returncode": None,
                "note": f"worker timeout after {self.worker_timeout_s}s",
                "elapsed_s": round(time.time() - started, 1),
            }
        except Exception as e:
            return {
                "index": index, "title": subtask["title"],
                "status": "error", "outdir": worker_outdir,
                "returncode": None, "note": str(e)[:300],
                "elapsed_s": round(time.time() - started, 1),
            }

    # -- merge ---------------------------------------------------------

    def _read_worker_out(self, worker_outdir):
        path = os.path.join(worker_outdir, "OUT.md")
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ""

    def merge(self, task, results, outdir, recon=None):
        """Merge every worker's OUT.md into one report; write OUT.md +
        progress.json in outdir. Returns the merged OUT.md path."""
        ts = datetime.now(timezone.utc).isoformat()
        ok = sum(1 for r in results if r["status"] == "done")
        lines = [
            "# Elieve Orchestrator — merged report",
            "",
            f"- Task: {task}",
            f"- Workers: {ok}/{len(results)} succeeded",
            f"- Time: {ts}",
            "",
            "---",
            "",
        ]
        for r in results:
            lines.append(f"## Worker {r['index']}: {r['title']} "
                         f"— {r['status']}")
            lines.append("")
            if r["status"] != "done" and r.get("note"):
                lines.append(f"> Failure note: {r['note']}")
                lines.append("")
            body = self._read_worker_out(r["outdir"]).strip()
            if body:
                # Strip the worker's standard elieve header for brevity.
                body = re.sub(r"^# Elieve — hasil\n\n(- .*\n)+\n---\n\n",
                              "", body, count=1)
                lines.append(body)
            else:
                lines.append("(no OUT.md from this worker)")
            lines.append("")
            lines.append("---")
            lines.append("")
        os.makedirs(outdir, exist_ok=True)
        out_md = os.path.join(outdir, "OUT.md")
        with open(out_md, "w") as f:
            f.write("\n".join(lines))
        prog = {
            "task": task,
            "mode": "orchestrator",
            "orchestrator_model": self.orchestrator_model,
            "worker_model": self.worker_model,
            "workers": [
                {k: r.get(k) for k in
                 ("index", "title", "status", "outdir",
                  "returncode", "note", "elapsed_s")}
                for r in results
            ],
            "status": "done" if ok == len(results) and results else "partial",
            "updated_at": ts,
        }
        if recon:
            prog["recon_plan"] = {
                "outdir": recon.get("outdir"),
                "plan_md": recon.get("plan_md"),
                "model": recon.get("model"),
            }
        with open(os.path.join(outdir, "progress.json"), "w") as f:
            json.dump(prog, f, indent=2, ensure_ascii=False)
        return out_md

    # -- main ----------------------------------------------------------

    def run(self, task, outdir):
        """Run the task via parallel workers; return the merged OUT.md path.

        Raises OrchestratorError on: the depth guard tripping, or the
        planner dying during planning. The caller catches it -> fallback
        to single-agent.
        """
        if os.environ.get(WORKER_ENV_FLAG) == "1":
            raise OrchestratorError(
                "depth guard: workers may not run the orchestrator."
            )
        os.makedirs(outdir, exist_ok=True)
        # Gap 6 — recon gate: fase plan read-only dulu; worker di-spawn
        # SETELAH PLAN.md ada. Gagal -> OrchestratorError -> caller
        # fallback single-agent. plan_first=false/absen -> lewati
        # (perilaku lama, tanpa perubahan).
        recon = None
        if self.plan_first:
            recon = self._run_recon_gate(task, outdir)
        print(f"[orchestrator] planning via {self.orchestrator_model} ...",
              flush=True)
        subtasks = self.plan(task)
        print(f"[orchestrator] {len(subtasks)} subtasks:", flush=True)
        for i, s in enumerate(subtasks):
            ro = "readonly" if s["readonly"] else "full-tools"
            print(f"[orchestrator]   w{i}: {s['title']} [{ro}]", flush=True)

        workers_dir = os.path.join(outdir, "workers")
        os.makedirs(workers_dir, exist_ok=True)
        results = [None] * len(subtasks)

        def _one(i):
            wdir = os.path.join(workers_dir, f"w{i}")
            print(f"[orchestrator] spawn w{i} -> {wdir}", flush=True)
            return self._spawn_worker(i, subtasks[i], wdir)

        # Failure isolation: one worker failing must not take down others.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers) as pool:
            futs = {pool.submit(_one, i): i for i in range(len(subtasks))}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # one worker must never
                    results[i] = {      # take down the others
                        "index": i, "title": subtasks[i]["title"],
                        "status": "error",
                        "outdir": os.path.join(workers_dir, f"w{i}"),
                        "returncode": None, "note": str(e)[:300],
                        "elapsed_s": 0,
                    }
                r = results[i]
                print(f"[orchestrator] w{i} finished: {r['status']}",
                      flush=True)

        out_md = self.merge(task, results, outdir, recon=recon)
        print(f"[orchestrator] merged report: {out_md}", flush=True)
        return out_md
