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

from .tools import DISPATCH, TOOL_SCHEMAS, ToolError
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
- Gunakan function call yang tersedia: read_file, list_dir, grep, exec.
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


def call_model(messages, model, api_key):
    """Kirim chat completion; kembalikan (message, usage).

    `usage` berisi prompt_tokens/completion_tokens bila provider
    memberikannya, else dict kosong (pemanggil pakai estimasi chars/4).
    """
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
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


def _fallback_tool_call(text: str):
    """Cadangan bila model tidak pakai native function calling:
    blok ```tool {"name": ..., "arguments": {...}}```"""
    m = re.search(r"```tool\s*\n(\{.*?\})\s*```", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except Exception:
        return None
    name = d.get("name")
    if name in DISPATCH:
        return name, d.get("arguments") or {}
    return None


class HermesLoop:
    """Satu sesi ReAct: kirim task, iterasi tool call sampai jawaban akhir."""

    def __init__(self, task, outdir, model=DEFAULT_MODEL, max_steps=40,
                 system_prompt=SYSTEM_PROMPT, compaction_cfg=None):
        self.task = task
        self.outdir = outdir
        self.model = validate_model(model)
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.api_key = get_api_key()
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

    # -- tool dispatch -----------------------------------------------

    def _run_tool(self, name, args):
        try:
            return str(DISPATCH[name](**args))
        except TypeError as e:
            return f"argumen salah untuk {name}: {e}"
        except ToolError as e:
            return f"TOOL DITOLAK: {e}"
        except Exception as e:
            return f"tool error ({name}): {e}"

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
        out, actions = apply_threshold_pipeline(
            messages,
            prompt_tokens,
            context_limit=int(self.compaction["context_limit"]),
            thresholds=self.compaction["thresholds"],
            recency_window=int(self.compaction["recency_window"]),
            prefix_len=int(self.compaction["prefix_len"]),
            summarizer=self._summarize_for_compaction,
        )
        return out, actions

    # -- main --------------------------------------------------------

    def run(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        messages = [
            {"role": "system", "content": self.system_prompt},
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
            if step > 1:
                time.sleep(MODEL_CALL_DELAY_S)
            print(
                f"[hermes] step {step}/{self.max_steps} -> {self.model} ...",
                flush=True,
            )
            # Lapis 1: microcompaction tiap turn SEBELUM request (tanpa LLM)
            messages = self._maybe_micro_compact(messages)
            try:
                msg, usage = call_model(messages, self.model, self.api_key)
            except RateLimited as e:
                prog.update(step=step, status="rate_limited", note=str(e))
                self._write_progress(prog)
                self._write_out(
                    "BERHENTI: rate limited (HTTP 429). Progress tersimpan di "
                    "progress.json — lanjutkan manual bila kuota pulih.",
                    "rate_limited",
                )
                print(f"[hermes] {e}", flush=True)
                return 0
            except Exception as e:
                prog.update(step=step, status="error", note=str(e)[:300])
                self._write_progress(prog)
                print(f"[hermes] ERROR: {e}", flush=True)
                return 1

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
                    if name not in DISPATCH:
                        result = f"tool tidak dikenal: {name}"
                    else:
                        print(
                            f"[hermes]   tool: {name} {str(args)[:120]}",
                            flush=True,
                        )
                        result = self._run_tool(name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": name,
                            "content": str(result),
                        }
                    )
                    prog["last_tool"] = name
                prog.update(step=step, status="running")
                self._write_progress(prog)
                continue

            content = msg.get("content") or ""
            fb = _fallback_tool_call(content)
            if fb:
                name, args = fb
                print(f"[hermes]   tool (fallback): {name}", flush=True)
                result = self._run_tool(name, args)
                messages.append(
                    {
                        "role": "user",
                        "content": f"[hasil tool {name}]\n{result}\nLanjutkan task.",
                    }
                )
                prog.update(step=step, status="running", last_tool=name)
                self._write_progress(prog)
                continue

            # jawaban akhir — tidak ada tool call
            prog.update(step=step, status="done")
            self._write_progress(prog)
            self._write_out(content, "done")
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
    args = ap.parse_args(argv)

    cfg = load_config(args.config) if args.config else {}
    loop = HermesLoop(
        task=args.task,
        outdir=args.outdir,
        model=cfg.get("model", args.model),
        max_steps=int(cfg.get("max_steps", args.max_steps)),
        system_prompt=args.system_prompt or cfg.get("system_prompt", SYSTEM_PROMPT),
        compaction_cfg=cfg.get("compaction"),
    )
    return loop.run()


if __name__ == "__main__":
    sys.exit(main())
