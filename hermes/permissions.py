"""Fase 3 — permission gate 2 tahap + lapisan 0 regex (implementasi penuh).

Masalah: deny-list regex v1 (hermes/tools/exec.py: DENY_RULES) tidak paham
konteks dan rapuh terhadap variasi penulisan. Butuh penilaian semantik.

Desain (docs/ARCHITECTURE.md §3 + keputusan Bayu 2026-10-09):
  - Lapisan 0: deny-list regex v1, dipakai ulang, fail-closed.
  - Tahap 1 "kilat": model kecil/murah menjawab ALLOW/DENY/UNSURE dalam
    <64 token, timeout singkat. OPSIONAL & PLUGGABLE ("by choose"):
    user bebas pilih model + API khusus untuk classifier.
  - Tahap 2: HANYA bila tahap 1 UNSURE — model utama menimbang maksud
    perintah lalu putuskan allow / deny / ask.
  - AUTO-ON: `classifier_model` diisi -> gate aktif.
    AUTO-OFF: `classifier_model` kosong -> classifier di-skip total,
    proteksi tetap via regex lapisan 0.
  - Model MATI (timeout/error): fallback per-call ke verdict lapisan 0
    (fail-closed); 3 gagal beruntun -> classifier auto-disable untuk
    sisa run. Run TIDAK BOLEH crash gara-gara classifier.
  - API key custom HANYA via env var (`classifier_api_key_env` = NAMA
    env var). Tidak ada secret yang di-hardcode / di-commit.

Semua verdict dicatat ke audit log JSONL (<outdir>/permission_audit.jsonl).
Implementasi original, tidak menyalin kode dari mana pun.
"""

import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone

from .tools.exec import _exec_allowed

log = logging.getLogger("hermes.permissions")

NINEROUTER_URL = "http://127.0.0.1:20128/v1/chat/completions"
FORBIDDEN_PREFIXES = ("bns/", "oc/")   # kuota terlarang — ditolak di kode

DEFAULT_KILAT_MAX_TOKENS = 32          # wajib <64
DEFAULT_KILAT_TIMEOUT_S = 10
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
STAGE2_MAX_TOKENS = 256
STAGE2_TIMEOUT_S = 60

KILAT_SYSTEM = (
    "Kamu pemeriksa keamanan cepat untuk tool-call sebuah agen. "
    "Jawab SELALU diawali tepat satu kata: ALLOW, DENY, atau UNSURE, "
    "lalu alasan sangat singkat (maks 1 kalimat).\n"
    "DENY bila aksi: menghapus/merusak data, mengubah sistem/host, "
    "menonaktifkan proteksi, mengunduh lalu mengeksekusi kode remote "
    "(mis. curl/wget | sh), mengirim data/kredensial keluar (exfiltrate), "
    "atau keluar dari area izin (/home/hatch/workspace dan /tmp).\n"
    "ALLOW bila aksi jelas aman: baca file, list direktori, grep, "
    "atau perintah shell yang tidak destruktif.\n"
    "UNSURE bila ragu."
)

STAGE2_SYSTEM = (
    "Kamu penimbang izin tahap akhir untuk tool-call sebuah agen bug bounty. "
    "Tahap cepat ragu, jadi kamu yang memutuskan. "
    "Jawab SELALU diawali tepat satu kata: ALLOW, DENY, atau ASK, "
    "lalu alasan singkat (maks 2 kalimat).\n"
    "DENY bila aksi destruktif, keluar izin, atau exfiltrate data. "
    "ASK bila aksi berpotensi berisiko tapi maksudnya belum jelas "
    "dan butuh konfirmasi manusia. Selain itu ALLOW."
)


def _validate_ag_model(model: str) -> str:
    """Classifier via 9router HANYA boleh ag/*. bns/*/oc/* DITOLAK."""
    m = (model or "").strip()
    low = m.lower()
    for prefix in FORBIDDEN_PREFIXES:
        if low.startswith(prefix):
            raise ValueError(
                f"model classifier '{model}' DILARANG — jangan sentuh "
                f"kuota bns/* atau oc/*."
            )
    if not low.startswith("ag/"):
        raise ValueError(
            f"model classifier via 9router harus ag/* (dapat '{model}')."
        )
    return m


def _post_json(url, payload, api_key, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _parse_verdict(text, allowed):
    """Ambil kata pertama; petakan ke salah satu verdict yang diizinkan."""
    first = (text or "").strip().split()
    word = first[0].upper().rstrip(".,:;") if first else ""
    return word if word in allowed else "UNSURE"


class PermissionGate:
    """Gerbang izin: lapisan 0 (regex) + classifier kilat + reasoning.

    Contoh:
        gate = PermissionGate(
            cfg={"classifier_model": "ag/gemini-3-flash"},
            deep_model="ag/claude-opus-4-6-thinking",
            main_api_key="<key 9router>",
            audit_path="/run/outdir/permission_audit.jsonl",
        )
        verdict, reason = gate.check("exec", {"command": "rm -rf /tmp/x"})
        # verdict: "allow" | "deny" | "ask"
    """

    def __init__(self, cfg=None, deep_model=None, main_api_key="",
                 audit_path=None, post_fn=None):
        cfg = dict(cfg or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.classifier_model = (cfg.get("classifier_model") or "").strip()
        self.api_base = (cfg.get("classifier_api_base") or "").strip()
        self.api_key_env = (cfg.get("classifier_api_key_env") or "").strip()
        self.kilat_max_tokens = min(
            int(cfg.get("kilat_max_tokens", DEFAULT_KILAT_MAX_TOKENS)), 63)
        self.kilat_timeout_s = int(
            cfg.get("kilat_timeout_s", DEFAULT_KILAT_TIMEOUT_S))
        self.max_failures = int(
            cfg.get("max_consecutive_failures",
                    DEFAULT_MAX_CONSECUTIVE_FAILURES))
        self.deep_model = (deep_model or "").strip()
        self.main_api_key = main_api_key or ""
        self.audit_path = audit_path
        self.post_fn = post_fn  # injectable untuk test

        self._off_logged = False
        self._classifier_dead = False
        self._consec_failures = 0

        # Validasi awal hanya bila classifier dipakai via 9router.
        if self._classifier_configured() and not self.api_base:
            _validate_ag_model(self.classifier_model)

    # -- status --------------------------------------------------------

    def _classifier_configured(self):
        return self.enabled and bool(self.classifier_model)

    def classifier_active(self):
        """True bila tahap 1 kilat boleh dipakai saat ini."""
        return (self._classifier_configured()
                and not self._classifier_dead)

    # -- lapisan 0 -----------------------------------------------------

    def _layer0(self, tool_name, args):
        """Deny-list regex v1 (hanya relevan untuk exec)."""
        if tool_name == "exec":
            cmd = (args or {}).get("command") or ""
            ok, reason = _exec_allowed(str(cmd))
            if not ok:
                return "deny", f"lapisan-0 regex: {reason}"
        return "allow", "lapisan-0: lolos"

    # -- pemanggilan LLM -------------------------------------------------

    def _resolve_classifier_key(self):
        """Key classifier: env var custom, atau key 9router bila api_base kosong."""
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                return key
            # env diminta tapi kosong -> seperti model mati (fail-closed)
            raise RuntimeError(
                f"env var '{self.api_key_env}' kosong/tidak ada "
                f"padahal classifier_api_key_env diisi."
            )
        if not self.api_base:
            if not self.main_api_key:
                raise RuntimeError("API key 9router tidak tersedia.")
            return self.main_api_key
        raise RuntimeError(
            "classifier_api_base diisi tapi classifier_api_key_env kosong — "
            "key custom wajib via env var."
        )

    def _call_classifier(self, system, user_content, max_tokens, timeout):
        """Satu panggilan ke model classifier; kembalikan teks jawaban."""
        url = self.api_base or NINEROUTER_URL
        key = self._resolve_classifier_key()
        payload = {
            "model": self.classifier_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        }
        post = self.post_fn or _post_json
        status, text = post(url, payload, key, timeout)
        if status >= 400:
            raise RuntimeError(f"classifier HTTP {status}: {text[:200]}")
        try:
            data = json.loads(text)
            content = data["choices"][0]["message"].get("content") or ""
        except Exception as e:
            raise RuntimeError(f"respon classifier tidak bisa di-parse: {e}")
        return content.strip()

    def _stage1(self, tool_name, args):
        """Tahap kilat: ALLOW / DENY / UNSURE."""
        user_content = (
            f"Tool: {tool_name}\n"
            f"Argumen: {json.dumps(args or {}, ensure_ascii=False)[:800]}"
        )
        text = self._call_classifier(
            KILAT_SYSTEM, user_content,
            self.kilat_max_tokens, self.kilat_timeout_s)
        verdict = _parse_verdict(text, {"ALLOW", "DENY", "UNSURE"})
        return verdict.lower(), f"kilat: {text[:200]}"

    def _stage2(self, tool_name, args, stage1_reason):
        """Tahap reasoning via model utama; ALLOW / DENY / ASK."""
        if not self.deep_model:
            raise RuntimeError("deep_model kosong — tahap 2 tidak bisa jalan.")
        user_content = (
            f"Tool: {tool_name}\n"
            f"Argumen: {json.dumps(args or {}, ensure_ascii=False)[:1200]}\n"
            f"Catatan tahap kilat: {stage1_reason[:300]}"
        )
        # Tahap 2 selalu via 9router dengan model utama (ag/* tervalidasi loop).
        payload = {
            "model": self.deep_model,
            "messages": [
                {"role": "system", "content": STAGE2_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": STAGE2_MAX_TOKENS,
            "stream": False,
        }
        post = self.post_fn or _post_json
        status, text = post(NINEROUTER_URL, payload, self.main_api_key,
                            STAGE2_TIMEOUT_S)
        if status >= 400:
            raise RuntimeError(f"tahap-2 HTTP {status}: {text[:200]}")
        try:
            data = json.loads(text)
            content = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            raise RuntimeError(f"respon tahap-2 tidak bisa di-parse: {e}")
        verdict = _parse_verdict(content, {"ALLOW", "DENY", "ASK"})
        if verdict == "UNSURE":
            # tak ter-parse -> paling aman: minta konfirmasi manusia
            verdict = "ASK"
        # ASK diperlakukan sebagai penolakan lunak (loop non-interaktif).
        return verdict.lower(), f"tahap-2: {content[:200]}"

    # -- audit -----------------------------------------------------------

    def _audit(self, entry):
        if not self.audit_path:
            return
        try:
            parent = os.path.dirname(self.audit_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            entry["ts"] = datetime.now(timezone.utc).isoformat()
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("audit log gagal ditulis: %s", e)

    # -- pintu utama -------------------------------------------------------

    def check(self, tool_name, args):
        """Nilai satu tool call.

        Kembalikan (verdict, reason); verdict salah satu dari
        "allow" | "deny" | "ask". TIDAK PERNAH melempar — kegagalan
        internal jatuh ke lapisan 0 (fail-closed).
        """
        args = args or {}
        audit = {
            "tool": tool_name,
            "args_summary": str(args)[:300],
            "layer0": None,
            "stage1": None,
            "stage2": None,
            "verdict": None,
            "reason": None,
        }
        try:
            return self._check_inner(tool_name, args, audit)
        except Exception as e:  # noqa: BLE001 - gate tidak boleh crash
            log.warning("permission gate error (%s) -> fallback lapisan-0: %s",
                        tool_name, e)
            v0, r0 = self._layer0(tool_name, args)
            audit["layer0"] = v0
            audit["verdict"] = v0
            audit["reason"] = f"gate error, fallback lapisan-0: {r0} [{e}]"
            self._audit(audit)
            return v0, audit["reason"]

    def _check_inner(self, tool_name, args, audit):
        # Lapisan 0 dulu: gratis, deterministik, fail-closed.
        v0, r0 = self._layer0(tool_name, args)
        audit["layer0"] = v0
        if v0 == "deny":
            audit["verdict"] = "deny"
            audit["reason"] = r0
            self._audit(audit)
            return "deny", r0

        if not self.classifier_active():
            if self._classifier_configured() and not self._off_logged:
                # auto-off karena model kosong/mati — log sekali saja
                self._off_logged = True
                msg = ("permission gate: classifier OFF "
                       "(model kosong / dinonaktifkan / auto-disable) — "
                       "hanya regex lapisan-0 yang aktif.")
                print(f"[hermes] {msg}", file=sys.stderr, flush=True)
                log.warning(msg)
            audit["verdict"] = "allow"
            audit["reason"] = "classifier off — lapisan-0 lolos"
            self._audit(audit)
            return "allow", audit["reason"]

        # Tahap 1 kilat.
        try:
            v1, r1 = self._stage1(tool_name, args)
        except Exception as e:
            self._register_failure(e)
            # fallback per-call ke lapisan 0 (sudah lolos di atas -> allow)
            audit["stage1"] = f"error: {e}"
            audit["verdict"] = "allow"
            audit["reason"] = (f"classifier mati ({e}); "
                               "fallback lapisan-0 (lolos)")
            self._audit(audit)
            return "allow", audit["reason"]
        self._consec_failures = 0
        audit["stage1"] = f"{v1}: {r1[:120]}"

        if v1 == "allow":
            audit["verdict"] = "allow"
            audit["reason"] = r1
            self._audit(audit)
            return "allow", r1
        if v1 == "deny":
            audit["verdict"] = "deny"
            audit["reason"] = r1
            self._audit(audit)
            return "deny", r1

        # Tahap 2 — hanya bila tahap 1 UNSURE.
        try:
            v2, r2 = self._stage2(tool_name, args, r1)
        except Exception as e:
            self._register_failure(e)
            audit["stage2"] = f"error: {e}"
            audit["verdict"] = "allow"
            audit["reason"] = (f"tahap-2 mati ({e}); "
                               "fallback lapisan-0 (lolos)")
            self._audit(audit)
            return "allow", audit["reason"]
        self._consec_failures = 0
        audit["stage2"] = f"{v2}: {r2[:120]}"
        audit["verdict"] = v2
        audit["reason"] = r2
        self._audit(audit)
        return v2, r2

    def _register_failure(self, e):
        self._consec_failures += 1
        log.warning("classifier gagal (%s/%s): %s",
                    self._consec_failures, self.max_failures, e)
        if self._consec_failures >= max(1, self.max_failures):
            self._classifier_dead = True
            msg = (f"permission gate: classifier AUTO-DISABLE setelah "
                   f"{self._consec_failures} gagal beruntun — sisa run "
                   f"hanya pakai regex lapisan-0.")
            print(f"[hermes] {msg}", file=sys.stderr, flush=True)
            log.warning(msg)
