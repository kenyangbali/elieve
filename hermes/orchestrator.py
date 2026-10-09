"""Fase 4 — multi-agent orchestration (implementasi penuh).

Masalah yang diselesaikan: satu agent untuk audit besar = lambat dan
tunnel vision. Orchestrator memecah task jadi subtask independen lalu
menjalankan N worker HermesLoop paralel (subprocess terisolasi).

Desain (lihat docs/ARCHITECTURE.md §4), keputusan Bayu 2026-10-09
(pola sama seperti classifier Fase 3):
  - OPSIONAL & PLUGGABLE ("by choose"): user bebas pilih model + API
    khusus untuk mandor maupun worker.
  - AUTO-ON: `orchestrator_model` diisi (dan enabled) -> task dipecah,
    worker paralel di-spawn.
  - AUTO-OFF: `orchestrator_model` kosong / enabled=false -> perilaku
    100% single-agent seperti sebelumnya. Tidak ada perubahan default.
  - Mandor MATI (timeout/error): OrchestratorError -> pemanggil
    (loop.main) fallback ke single-agent + warning. Run TIDAK crash.
  - API key custom HANYA via env var; tidak pernah di-hardcode.

Keamanan:
  - Depth guard: worker (env _HERMES_WORKER=1) DILARANG menjalankan
    orchestrator -> OrchestratorError. Mencegah ledakan rekursif.
  - Via 9router HANYA model ag/*; bns/*/oc/* DITOLAK di kode.
  - Tiap worker: outdir sendiri, max_steps sendiri, toolset terbatas
    (flag --no-exec untuk mode baca-saja), progress.json sendiri.
  - Kegagalan satu worker tidak menggagalkan worker lain (failure
    isolation); hasil merge mencatat status tiap worker.

API:
    orch = Orchestrator.from_config(orch_cfg, task=..., outdir=...,
                                    model=..., api_key=...)
    out_md = orch.run(task, outdir)   # -> path OUT.md gabungan
"""

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

WORKER_ENV_FLAG = "_HERMES_WORKER"

FORBIDDEN_PREFIXES = ("bns/", "oc/")

DEFAULT_MAX_WORKERS = 4
DEFAULT_WORKER_MAX_STEPS = 20
DEFAULT_PLAN_TIMEOUT_S = 120
DEFAULT_WORKER_TIMEOUT_S = 1800
DEFAULT_MAX_CONSECUTIVE_PLAN_FAILURES = 1  # gagal plan sekali -> fallback

NINE_ROUTER_URL = "http://127.0.0.1:20128/v1/chat/completions"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLAN_SYSTEM_PROMPT = """Kamu perencana untuk tim bug-hunter. Tugas: pecah TASK
berikut menjadi subtask yang INDEPENDEN (bisa dikerjakan paralel tanpa
saling menunggu).

Aturan:
- Maksimal {max_workers} subtask. Lebih sedikit bila task memang kecil.
- Tiap subtask: judul singkat, deskripsi task mandiri (lengkap: target,
  scope, apa yang dicari — worker tidak melihat konteks lain),
  dan readonly=true bila subtask hanya butuh baca/grep (tanpa eksekusi).
- Output HANYA JSON valid: array of object
  [{"title": "...", "task": "...", "readonly": true|false}]
  Tanpa penjelasan lain di luar JSON.
"""


class OrchestratorError(Exception):
    """Kegagalan orchestrator (mandor mati, depth guard, config invalid).

    Pemanggil (hermes.loop main) menangkap ini lalu fallback ke
    single-agent — run tidak boleh crash karenanya.
    """


def is_orchestrator_active(cfg):
    """True bila orchestrator diminta aktif: enabled dan model terisi."""
    cfg = cfg or {}
    return bool(cfg.get("enabled", True)) and bool(
        (cfg.get("orchestrator_model") or "").strip()
    )


def _validate_9router_model(model, role):
    """Via 9router HANYA ag/*. bns/*/oc/* DITOLAK."""
    low = (model or "").strip().lower()
    for prefix in FORBIDDEN_PREFIXES:
        if low.startswith(prefix):
            raise OrchestratorError(
                f"{role} '{model}' DILARANG — jangan sentuh kuota bns/* atau oc/*."
            )
    if not low.startswith("ag/"):
        raise OrchestratorError(
            f"{role} harus model ag/* bila via 9router (dapat '{model}')."
        )
    return model.strip()


def _post_json(url, headers, payload, timeout):
    """POST JSON minimal tanpa dependensi (duplikat kecil dari loop.py
    agar orchestrator.py tidak import loop — menghindari circular import)."""
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


def _extract_json_array(text):
    """Ambil array JSON pertama dari teks (tahan code fence)."""
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    candidate = m.group(1) if m else text
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start < 0 or end <= start:
        raise OrchestratorError("respons mandor tidak memuat array JSON.")
    try:
        data = json.loads(candidate[start : end + 1])
    except Exception as e:
        raise OrchestratorError(f"JSON subtask tidak valid: {e}")
    if not isinstance(data, list):
        raise OrchestratorError("respons mandor bukan array JSON.")
    return data


class Orchestrator:
    """Koordinator multi-worker HermesLoop (fase 4)."""

    def __init__(self, orchestrator_model, api_key, api_base="",
                 api_key_env="", worker_model="", max_workers=4,
                 worker_max_steps=20, worker_readonly_default=False,
                 worker_timeout_s=DEFAULT_WORKER_TIMEOUT_S,
                 plan_timeout_s=DEFAULT_PLAN_TIMEOUT_S,
                 compaction_cfg=None, memory_cfg=None, permissions_cfg=None,
                 system_prompt=None, plan_fn=None, spawn_fn=None):
        self.orchestrator_model = (orchestrator_model or "").strip()
        if not self.orchestrator_model:
            raise OrchestratorError(
                "orchestrator_model kosong — orchestrator tidak aktif."
            )
        self.api_base = (api_base or "").strip()
        if self.api_base:
            # API custom (OpenAI-compatible) pilihan user; model bebas.
            self.api_url = self.api_base.rstrip("/") + "/chat/completions"
            key_env = (api_key_env or "").strip()
            if not key_env:
                raise OrchestratorError(
                    "orchestrator_api_base diisi tapi orchestrator_api_key_env "
                    "kosong — key custom wajib via env var."
                )
            self.api_key = os.environ.get(key_env) or ""
            if not self.api_key:
                raise OrchestratorError(
                    f"env var '{key_env}' kosong/belum di-set."
                )
        else:
            # Via 9router: model mandor WAJIB ag/*.
            _validate_9router_model(self.orchestrator_model, "orchestrator_model")
            self.api_url = NINE_ROUTER_URL
            self.api_key = api_key
        self.worker_model = (worker_model or "").strip()
        self.max_workers = max(1, int(max_workers))
        self.worker_max_steps = max(1, int(worker_max_steps))
        self.worker_readonly_default = bool(worker_readonly_default)
        self.worker_timeout_s = int(worker_timeout_s)
        self.plan_timeout_s = int(plan_timeout_s)
        self.compaction_cfg = compaction_cfg
        self.memory_cfg = memory_cfg
        self.permissions_cfg = permissions_cfg
        self.system_prompt = system_prompt
        # Injectable untuk unit test (tanpa network/subprocess).
        self._plan_fn = plan_fn
        self._spawn_fn = spawn_fn

    @classmethod
    def from_config(cls, orch_cfg, *, task, outdir, model, api_key,
                    compaction_cfg=None, memory_cfg=None, permissions_cfg=None,
                    system_prompt=None, plan_fn=None, spawn_fn=None):
        """Bangun dari blok `orchestrator:` di YAML + nilai runtime."""
        orch_cfg = orch_cfg or {}
        return cls(
            orchestrator_model=orch_cfg.get("orchestrator_model", ""),
            api_key=api_key,
            api_base=orch_cfg.get("orchestrator_api_base", ""),
            api_key_env=orch_cfg.get("orchestrator_api_key_env", ""),
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
            plan_fn=plan_fn,
            spawn_fn=spawn_fn,
        )

    # -- planning ----------------------------------------------------

    def _call_mandor(self, messages, timeout):
        payload = {
            "model": self.orchestrator_model,
            "messages": messages,
            "stream": False,
        }
        try:
            status, text = _post_json(
                self.api_url,
                {"Authorization": "Bearer " + self.api_key,
                 "Content-Type": "application/json"},
                payload,
                timeout,
            )
        except Exception as e:
            raise OrchestratorError(f"mandor tidak terjangkau: {e}")
        if status == 429:
            raise OrchestratorError("mandor rate-limited (HTTP 429).")
        if status >= 400:
            raise OrchestratorError(f"mandor HTTP {status}: {text[:300]}")
        try:
            data = json.loads(text)
            return data["choices"][0]["message"].get("content") or ""
        except Exception as e:
            raise OrchestratorError(f"respons mandor tidak bisa di-parse: {e}")

    def plan(self, task):
        """Pecah task jadi daftar subtask independen (maks max_workers).

        Kembalikan list [{"title","task","readonly"}]. Melempar
        OrchestratorError bila mandor mati / respons invalid.
        """
        if self._plan_fn is not None:
            try:
                subtasks = self._plan_fn(task)
            except OrchestratorError:
                raise
            except Exception as e:
                raise OrchestratorError(f"planning gagal: {e}")
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
                "mandor tidak menghasilkan subtask yang valid."
            )
        return cleaned

    # -- worker spawn --------------------------------------------------

    def _worker_config_path(self, worker_outdir):
        """Tulis worker-config.yaml (best-effort; butuh PyYAML)."""
        cfg = {
            "model": self.worker_model,
            "max_steps": self.worker_max_steps,
            # Worker tidak boleh jadi mandor lagi (defense in depth;
            # depth guard utama via env _HERMES_WORKER=1).
            "orchestrator": {"enabled": False, "orchestrator_model": ""},
        }
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
        """Jalankan satu worker sebagai subprocess; kembalikan result dict."""
        if self._spawn_fn is not None:
            return self._spawn_fn(index, subtask, worker_outdir)
        os.makedirs(worker_outdir, exist_ok=True)
        cfg_path = self._worker_config_path(worker_outdir)
        cmd = [
            sys.executable, "-m", "hermes.loop",
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
                "note": f"worker timeout setelah {self.worker_timeout_s}s",
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

    def merge(self, task, results, outdir):
        """Gabung OUT.md tiap worker jadi satu laporan; tulis OUT.md +
        progress.json di outdir. Kembalikan path OUT.md gabungan."""
        ts = datetime.now(timezone.utc).isoformat()
        ok = sum(1 for r in results if r["status"] == "done")
        lines = [
            "# Hermes Orchestrator — laporan gabungan",
            "",
            f"- Task: {task}",
            f"- Worker: {ok}/{len(results)} sukses",
            f"- Waktu: {ts}",
            "",
            "---",
            "",
        ]
        for r in results:
            lines.append(f"## Worker {r['index']}: {r['title']} "
                         f"— {r['status']}")
            lines.append("")
            if r["status"] != "done" and r.get("note"):
                lines.append(f"> Catatan kegagalan: {r['note']}")
                lines.append("")
            body = self._read_worker_out(r["outdir"]).strip()
            if body:
                # Buang header hermes standar worker agar ringkas.
                body = re.sub(r"^# Hermes — hasil\n\n(- .*\n)+\n---\n\n",
                              "", body, count=1)
                lines.append(body)
            else:
                lines.append("(tidak ada OUT.md dari worker ini)")
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
        with open(os.path.join(outdir, "progress.json"), "w") as f:
            json.dump(prog, f, indent=2, ensure_ascii=False)
        return out_md

    # -- main ----------------------------------------------------------

    def run(self, task, outdir):
        """Jalankan task via worker paralel; kembalikan path OUT.md gabungan.

        Melempar OrchestratorError bila: depth guard terpicu, atau mandor
        mati saat planning. Pemanggil menangkapnya -> fallback single-agent.
        """
        if os.environ.get(WORKER_ENV_FLAG) == "1":
            raise OrchestratorError(
                "depth guard: worker dilarang menjalankan orchestrator."
            )
        os.makedirs(outdir, exist_ok=True)
        print(f"[orchestrator] planning via {self.orchestrator_model} ...",
              flush=True)
        subtasks = self.plan(task)
        print(f"[orchestrator] {len(subtasks)} subtask:", flush=True)
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

        # Failure isolation: satu worker gagal -> yang lain tetap jalan.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers) as pool:
            futs = {pool.submit(_one, i): i for i in range(len(subtasks))}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # jangan biarkan satu worker
                    results[i] = {      # menjatuhkan yang lain
                        "index": i, "title": subtasks[i]["title"],
                        "status": "error",
                        "outdir": os.path.join(workers_dir, f"w{i}"),
                        "returncode": None, "note": str(e)[:300],
                        "elapsed_s": 0,
                    }
                r = results[i]
                print(f"[orchestrator] w{i} selesai: {r['status']}",
                      flush=True)

        out_md = self.merge(task, results, outdir)
        print(f"[orchestrator] laporan gabungan: {out_md}", flush=True)
        return out_md
