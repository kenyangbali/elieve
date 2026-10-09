"""Fase 2 — MEMORY.md + autoDream (implementasi penuh).

Masalah: tiap sesi Hermes mulai dari nol; pelajaran run sebelumnya hilang.

Desain (docs/ARCHITECTURE.md §2):
  - `MEMORY.md` per outdir: butir SINGKAT (maks ~150 karakter),
    format `- <fakta> (YYYY-MM-DD)`. BUKAN arsip lengkap.
  - `remember(fakta)`: tulis butir baru kapan saja (via tool agent).
  - `recall()`: seluruh isi sebagai konteks tambahan di awal run.
  - `tidy(api_key)`: autoDream berkala — gabung duplikat, hapus yang
    basi/kontradiktif, rapikan format, memakai model murah ag/*.
  - **Filter rahasia**: API key, token, kredensial TIDAK PERNAH ditulis —
    butir yang cocok pola rahasia DITOLAK + peringatan.

Implementasi original.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import date

from .compaction import _validate_ag_model

DEFAULT_MAX_FACT_CHARS = 150
DEFAULT_SUMMARIZER_MODEL = "ag/gemini-3-flash"
DEFAULT_TIDY_EVERY_RUNS = 5

# (pola regex, label) — butir yang cocok DITOLAK, tidak pernah ditulis.
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
    """Kembalikan label pola rahasia bila cocok, else None."""
    for rx, label in _COMPILED_SECRETS:
        if rx.search(text or ""):
            return label
    return None


def _warn(msg):
    sys.stderr.write(f"[memory] PERINGATAN: {msg}\n")


def _normalize_body(body):
    """Normalisasi untuk deteksi duplikat: lowercase, spasi tunggal,
    tanggal akhir dibuang."""
    body = " ".join((body or "").split()).lower()
    return _DATE_RE.sub("", body).strip()


def _smart_truncate(text, limit):
    """Potong di batas kata; tambah '...' bila dipotong."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip() + "..."


def _read_bullets(path):
    """Baca butir '- ...' dari file; [] bila file tak ada."""
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
    """Ingatan lintas sesi berbasis file markdown (fase 2)."""

    def __init__(self, path, max_fact_chars=DEFAULT_MAX_FACT_CHARS):
        self.path = path
        self.max_fact_chars = int(max_fact_chars or DEFAULT_MAX_FACT_CHARS)

    # -- tulis / baca -------------------------------------------------

    def remember(self, fact: str) -> bool:
        """Simpan satu butir ingatan. True bila tersimpan.

        Ditolak (False + peringatan): kosong, duplikat persis, atau
        mengandung pola rahasia (TIDAK PERNAH ditulis ke file).
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
        """Seluruh isi MEMORY.md sebagai teks konteks; '' bila belum ada."""
        bullets = _read_bullets(self.path)
        return "\n".join(bullets).strip()

    # -- autoDream ----------------------------------------------------

    def tidy(self, api_key, model=DEFAULT_SUMMARIZER_MODEL, post_fn=None):
        """Rapikan MEMORY.md via model murah: gabung duplikat, hapus yang
        basi/kontradiktif, rapikan format. `post_fn(payload, api_key)`
        injectable untuk test (tanpa network di unit test).

        Mengembalikan isi baru (str). Butir hasil yang lolos filter
        rahasia saja yang ditulis.
        """
        model = _validate_ag_model(model)
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
        post = post_fn or _default_post
        content = post(payload, api_key) or ""

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


def _default_post(payload, api_key, url=None, timeout=120):
    """POST chat completion minimal (stdlib saja); kembalikan content str."""
    url = url or "http://127.0.0.1:20128/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"tidy HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except Exception as e:
        raise RuntimeError(f"tidy gagal: {e}")
    try:
        return data["choices"][0]["message"].get("content") or ""
    except Exception as e:
        raise RuntimeError(f"respon tidy tidak bisa di-parse: {e}")
