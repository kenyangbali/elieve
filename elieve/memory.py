"""Phase 2 — MEMORY.md + autoDream (full implementation).

Problem: every Elieve session starts from zero; lessons from previous
runs are lost.

Design (docs/ARCHITECTURE.md §2):
  - `MEMORY.md` per outdir: SHORT bullets (max ~150 chars),
    format `- <fact> (YYYY-MM-DD)`. NOT a full archive.
  - `remember(fact)`: write a new bullet any time (via the agent tool).
  - `recall()`: the whole content as extra context at run start.
  - `tidy(api_key)`: periodic autoDream — merge duplicates, drop
    stale/contradictory ones, tidy the format, using a cheap model.
  - **Secret filter**: API keys, tokens, credentials are NEVER written —
    bullets matching a secret pattern are REJECTED + warned.

Original implementation.
"""

import json
import os
import re
import sys
from datetime import date

from .providers import (
    ProviderConfig,
    check_model_allowed,
    post_chat_completions,
)

DEFAULT_MAX_FACT_CHARS = 150
DEFAULT_SUMMARIZER_MODEL = "ag/gemini-3-flash"
DEFAULT_TIDY_EVERY_RUNS = 5

# (regex, label) — matching bullets are REJECTED, never written.
SECRET_PATTERNS = [
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub personal access token"),
    (r"github_pat_[A-Za-z0-9_]{10,}", "GitHub fine-grained PAT"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub OAuth token"),
    (r"sk-[A-Za-z0-9]{10,}", "API key gaya sk-"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key ID"),
    (r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{10,}", "Bearer token"),
    (r"api[_-]?key\s*[:=]\s*\S+", "api_key assignment"),
    (r"secret\s*[:=]\s*\S+", "secret assignment"),
    (r"password\s*[:=]\s*\S+", "password assignment"),
    (r"passwd\s*[:=]\s*\S+", "passwd assignment"),
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
     "private key PEM"),
]

_COMPILED_SECRETS = [(re.compile(p), label) for p, label in SECRET_PATTERNS]

_DATE_RE = re.compile(r"\s*\(\d{4}-\d{2}-\d{2}\)\s*$")


def contains_secret(text):
    """Return the matching secret-pattern label, else None."""
    for rx, label in _COMPILED_SECRETS:
        if rx.search(text or ""):
            return label
    return None


def _warn(msg):
    sys.stderr.write(f"[memory] PERINGATAN: {msg}\n")


def _normalize_body(body):
    """Normalize for duplicate detection: lowercase, single spaces,
    trailing date stripped."""
    body = " ".join((body or "").split()).lower()
    return _DATE_RE.sub("", body).strip()


def _smart_truncate(text, limit):
    """Cut at a word boundary; append '...' when cut."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip() + "..."


def _read_bullets(path):
    """Read '- ...' bullets from a file; [] when the file is absent."""
    if not os.path.isfile(path):
        return []
    bullets = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("- ") and line[3:].strip():
                bullets.append(line)
    return bullets


class AgentMemory:
    """File-backed cross-session markdown memory (phase 2)."""

    def __init__(self, path, max_fact_chars=DEFAULT_MAX_FACT_CHARS):
        self.path = path
        self.max_fact_chars = int(max_fact_chars or DEFAULT_MAX_FACT_CHARS)

    # -- tulis / baca -------------------------------------------------

    def remember(self, fact: str) -> bool:
        """Store one memory bullet. True when stored.

        Rejected (False + warning): empty, exact duplicate, or matching
        a secret pattern (NEVER written to file).
        """
        body = " ".join((fact or "").split())
        if not body:
            return False
        secret = contains_secret(body)
        if secret:
            _warn(
                f"butir ingatan DITOLAK (mengandung {secret}); "
                "rahasia tidak pernah ditulis ke MEMORY.md."
            )
            return False
        body = _smart_truncate(body, self.max_fact_chars)
        norm = _normalize_body(body)
        for b in _read_bullets(self.path):
            if _normalize_body(b[2:]) == norm:
                return False  # duplikat persis
        entry = f"- {body} ({date.today().isoformat()})\n"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".",
                    exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry)
        return True

    def recall(self) -> str:
        """Whole MEMORY.md content as context text; '' when absent."""
        bullets = _read_bullets(self.path)
        return "\n".join(bullets).strip()

    # -- autoDream ----------------------------------------------------

    def tidy(self, api_key, model=DEFAULT_SUMMARIZER_MODEL, post_fn=None,
               model_policy=None, provider_cfg=None):
        """Tidy MEMORY.md via a cheap model: merge duplicates, drop
        stale/contradictory ones, tidy the format.
        `post_fn(payload, api_key)` is injectable for tests (no network
        in unit tests); the default POSTs via the configured provider.

        Returns the new content (str). Only bullets passing the secret
        filter are written. `model_policy` is enforced via
        providers.check_model_allowed (policy-driven; no hardcoded
        model rules here).
        """
        model = check_model_allowed(model, model_policy)
        bullets = _read_bullets(self.path)
        if len(bullets) < 2:
            return "\n".join(bullets).strip()

        prompt = (
            "Kamu perapi ingatan. Di bawah ini butir-butir MEMORY.md "
            '(format "- <fakta> (YYYY-MM-DD)").\n'
            "Tugas:\n"
            "1. Gabungkan butir duplikat/mirip jadi satu butir paling jelas.\n"
            "2. Hapus butir yang basi atau kontradiktif (bila kontradiksi, "
            "pertahankan yang tanggalnya lebih baru).\n"
            "3. Tiap butir MAKS ~150 karakter, format tetap "
            '"- <fakta> (YYYY-MM-DD)" (pertahankan tanggal asli).\n'
            "4. JANGAN PERNAH menulis API key, token, password, atau "
            "kredensial — bila ada di input, BUANG butirnya.\n"
            "Keluarkan HANYA daftar butir, satu per baris, tanpa penjelasan.\n\n"
            + "\n".join(bullets)
        )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if post_fn is not None:
            content = post_fn(payload, api_key) or ""
        else:
            cfg = (provider_cfg if provider_cfg is not None
                   else ProviderConfig())
            status, text = post_chat_completions(
                cfg, payload, api_key=api_key)
            if status >= 400:
                raise RuntimeError(f"tidy HTTP {status}: {text[:300]}")
            try:
                data = json.loads(text)
                content = data["choices"][0]["message"].get("content") or ""
            except Exception as e:
                raise RuntimeError(
                    f"tidy response could not be parsed: {e}")

        new_bullets = []
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            body = _smart_truncate(line[2:].strip(), self.max_fact_chars)
            if not body or contains_secret(body):
                if body:
                    _warn("hasil tidy mengandung pola rahasia — butir dibuang.")
                continue
            new_bullets.append(f"- {body}")
        if not new_bullets:
            _warn("tidy tidak menghasilkan butir valid — file tidak diubah.")
            return "\n".join(bullets).strip()

        # dedupe hasil akhir (jaga-jaga model mengulang)
        seen, final = set(), []
        for b in new_bullets:
            n = _normalize_body(b[2:])
            if n not in seen:
                seen.add(n)
                final.append(b)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(final) + "\n")
        return "\n".join(final)


