"""Fase 1 — context compaction: pipeline 4 lapis (implementasi penuh).

Masalah: riwayat `messages` di HermesLoop tumbuh tanpa batas sampai
max_steps. Tiap turn mengirim ulang seluruh riwayat = token input membengkak.

Desain (docs/PHASE1-COMPACTION.md):
  Lapis 1 — microcompaction per-turn, murni operasi string, tanpa LLM.
  Lapis 2 — threshold pipeline progresif berdasar prompt_tokens vs limit.
  Lapis 3 — full compaction via LLM murah (ag/gemini-3-flash), jarang.
  Lapis 4 — prefix (system + pesan awal) TIDAK PERNAH diubah/di-reorder.

Struktur messages yang dipakai:
  system            -> {"role": "system", "content": str}
  user (task awal)  -> {"role": "user", "content": str}
  assistant         -> {"role": "assistant", "content": str|None,
                        "tool_calls": [{id, function:{name, arguments}}]}
  tool              -> {"role": "tool", "tool_call_id": str,
                        "name": str, "content": str}

Semua fungsi di sini MENGEMBALIKAN list baru — input tidak pernah dimutasi.
Implementasi original, tidak menyalin kode dari mana pun.
"""

import json
import logging
import time
import urllib.request

log = logging.getLogger("hermes.compaction")

API_URL = "http://127.0.0.1:20128/v1/chat/completions"

# -- konstanta -------------------------------------------------------
OFFLOADED_FMT = "[offloaded: {summary}]"   # Lapis 1: pointer hasil tool lama
MASKED_PTR = "[offloaded to scratch]"      # Lapis 2 (80%): masking observasi
PRUNED_PTR = "[pruned]"                    # Lapis 2 (85%): pruning cepat
COMPACTED_MARK = "[COMPACTED]"             # Lapis 3: pesan ringkasan LLM

DEFAULT_CONTEXT_LIMIT = 64000
DEFAULT_RECENCY_WINDOW = 5
DEFAULT_MAX_TOOL_CHARS = 2000
DEFAULT_THRESHOLDS = [70, 80, 85, 90, 95]
DEFAULT_SUMMARIZER_MODEL = "ag/gemini-3-flash"
DEFAULT_KEEP_RECENT = 5
DEFAULT_PREFIX_LEN = 2          # system prompt + pesan task awal
DEFAULT_MAX_AGE_HOURS = 24

FORBIDDEN_PREFIXES = ("bns/", "oc/")   # kuota terlarang — ditolak di kode

CHARS_PER_TOKEN = 4             # heuristik estimasi bila usage tak tersedia


# -- util dasar ------------------------------------------------------

def estimate_tokens(messages) -> int:
    """Estimasi token kasar: chars/4 + overhead per pesan.

    Dipakai bila respons API tidak menyertakan `usage`.
    """
    total = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // CHARS_PER_TOKEN
        total += 8  # overhead role / struktur pesan
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += len(json.dumps(fn, ensure_ascii=False)) // CHARS_PER_TOKEN
    return total


def _one_line_summary(content, limit=120) -> str:
    """Ringkasan 1 baris untuk pointer offload: baris pertama non-kosong."""
    text = " ".join(str(content).split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text or "(hasil kosong)"


def _split_turns(body):
    """Kelompokkan body (di luar prefix) menjadi turn.

    Satu turn diawali pesan assistant dan mencakup pesan-pesan tool/user
    sesudahnya sampai assistant berikutnya.
    """
    turns, cur = [], []
    for m in body:
        if m.get("role") == "assistant" and cur:
            turns.append(cur)
            cur = []
        cur.append(m)
    if cur:
        turns.append(cur)
    return turns


def _active_tool_call_ids(turns) -> set:
    """tool_call_id yang dirujuk turn terakhir = 'masih aktif'."""
    ids = set()
    if not turns:
        return ids
    for m in turns[-1]:
        for tc in m.get("tool_calls") or []:
            tid = tc.get("id")
            if tid:
                ids.add(tid)
    return ids


def _protected_global_idx(body_len, turns, recency_window, prefix_len) -> set:
    """Index global pesan yang masuk jendela recency (tidak boleh disentuh)."""
    keep = turns[-recency_window:] if recency_window > 0 else []
    protected = set()
    # petakan ulang turn -> index body
    idx = 0
    for t in turns:
        for _ in t:
            if t in keep:
                protected.add(prefix_len + idx)
            idx += 1
    return protected


def _validate_ag_model(model: str) -> str:
    """Summarizer HANYA boleh ag/*. bns/*/oc/* DITOLAK (ValueError)."""
    m = (model or "").strip()
    low = m.lower()
    for prefix in FORBIDDEN_PREFIXES:
        if low.startswith(prefix):
            raise ValueError(
                f"model '{model}' DILARANG — jangan sentuh kuota bns/* atau oc/*."
            )
    if not low.startswith("ag/"):
        raise ValueError(
            f"model summarizer harus ag/* (dapat '{model}')."
        )
    return m


# -- Lapis 1: microcompaction ----------------------------------------

def micro_compact(messages, recency_window=DEFAULT_RECENCY_WINDOW,
                  max_tool_chars=DEFAULT_MAX_TOOL_CHARS,
                  prefix_len=DEFAULT_PREFIX_LEN,
                  max_age_hours=DEFAULT_MAX_AGE_HOURS):
    """Potong hasil tool lama jadi pointer. Murni string ops, tanpa LLM.

    Aturan:
      - prefix (system + pesan awal) TIDAK PERNAH disentuh;
      - N turn terakhir (recency_window) TIDAK disentuh;
      - hasil tool yang tool_call_id-nya dirujuk turn terakhir ("aktif")
        TIDAK disentuh;
      - pesan berumur > max_age_hours (bila membawa field "ts" epoch)
        di zona tengah di-drop.
    Return: list messages BARU.
    """
    if not messages:
        return []
    out = [dict(m) for m in messages]  # salinan dangkal per pesan
    prefix_end = min(prefix_len, len(out))
    body = out[prefix_end:]
    if not body:
        return out

    turns = _split_turns(body)
    protected = _protected_global_idx(len(body), turns, recency_window,
                                      prefix_end)
    active_ids = _active_tool_call_ids(turns)
    now = time.time()
    drop_idx = set()

    for i, m in enumerate(body):
        gi = prefix_end + i
        if gi in protected:
            continue
        # time-based clearing: hanya untuk pesan ber-ts di zona tengah
        ts = m.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            if (now - ts) > max_age_hours * 3600:
                drop_idx.add(gi)
                continue
        if m.get("role") != "tool":
            continue
        if m.get("tool_call_id") in active_ids:
            continue
        content = m.get("content") or ""
        if len(content) > max_tool_chars:
            out[gi] = dict(m, content=OFFLOADED_FMT.format(
                summary=_one_line_summary(content)))

    if drop_idx:
        out = [m for gi, m in enumerate(out) if gi not in drop_idx]
    return out


# -- Lapis 2: threshold pipeline -------------------------------------

def mask_observations(messages, recency_window=DEFAULT_RECENCY_WINDOW,
                      prefix_len=DEFAULT_PREFIX_LEN,
                      marker=MASKED_PTR):
    """80%: hasil tool lama di luar recency window -> marker.

    Metadata (tool_call_id, name) dipertahankan agar pasangan
    tool_calls tetap valid.
    """
    out = [dict(m) for m in messages]
    prefix_end = min(prefix_len, len(out))
    body = out[prefix_end:]
    turns = _split_turns(body)
    protected = _protected_global_idx(len(body), turns, recency_window,
                                      prefix_end)
    active_ids = _active_tool_call_ids(turns)
    for i, m in enumerate(body):
        gi = prefix_end + i
        if gi in protected:
            continue
        if m.get("role") != "tool":
            continue
        if m.get("tool_call_id") in active_ids:
            continue
        if m.get("content") != marker:
            out[gi] = dict(m, content=marker)
    return out


def prune_middle(messages, recency_window=DEFAULT_RECENCY_WINDOW,
                 prefix_len=DEFAULT_PREFIX_LEN,
                 max_user_chars=500):
    """85% fast pruning, jalan dari tengah:
      - pesan assistant di zona tengah: content teks dikosongkan
        (tool_calls DIPERTAHANKAN agar pairing tool tetap valid);
      - pesan user di zona tengah: dipotong ke max_user_chars.
    """
    out = [dict(m) for m in messages]
    prefix_end = min(prefix_len, len(out))
    body = out[prefix_end:]
    turns = _split_turns(body)
    protected = _protected_global_idx(len(body), turns, recency_window,
                                      prefix_end)
    for i, m in enumerate(body):
        gi = prefix_end + i
        if gi in protected:
            continue
        role = m.get("role")
        if role == "assistant":
            if m.get("content"):
                nm = dict(m, content="")
                out[gi] = nm
        elif role == "user":
            c = m.get("content") or ""
            if len(c) > max_user_chars:
                out[gi] = dict(m, content=c[:max_user_chars] + "…[pruned]")
    return out


def apply_threshold_pipeline(messages, prompt_tokens,
                             context_limit=DEFAULT_CONTEXT_LIMIT,
                             thresholds=DEFAULT_THRESHOLDS,
                             recency_window=DEFAULT_RECENCY_WINDOW,
                             prefix_len=DEFAULT_PREFIX_LEN,
                             summarizer=None):
    """Pipeline progresif berdasar rasio prompt_tokens / context_limit.

    thresholds = [t70, t80, t85, t90, t95] (persen).
      70% -> warning saja (log + catat aksi);
      80% -> observation masking (recency_window penuh);
      85% -> masking + fast pruning mundur;
      90% -> aggressive masking (recency_window 5 -> 2);
      95% -> full compaction via `summarizer` (callable
             messages -> messages_baru). Bila summarizer None, fallback
             ke aggressive masking agar loop tidak crash.

    Return: (messages_baru, actions: [str]).
    Prefix tidak pernah diubah di semua aksi.
    """
    t = sorted(thresholds or DEFAULT_THRESHOLDS)
    ratio = (prompt_tokens / context_limit * 100.0) if context_limit else 0.0
    actions = []
    out = messages

    if ratio >= t[4]:
        actions.append(f"full_compaction@{ratio:.1f}%")
        log.warning("compaction: %.1f%% dari limit — full compaction", ratio)
        if summarizer is not None:
            return summarizer(out), actions
        actions.append("summarizer_missing:fallback_aggressive")
        log.warning("compaction: summarizer tidak tersedia — fallback agresif")
        out = mask_observations(out, recency_window=2, prefix_len=prefix_len)
        actions.append("aggressive_masking(window=2)")
        return out, actions
    if ratio >= t[3]:
        actions.append(f"aggressive_masking(window=2)@{ratio:.1f}%")
        log.warning("compaction: %.1f%% — aggressive masking", ratio)
        return mask_observations(out, recency_window=2,
                                 prefix_len=prefix_len), actions
    if ratio >= t[2]:
        actions.append(f"mask+prune@{ratio:.1f}%")
        log.warning("compaction: %.1f%% — masking + fast pruning", ratio)
        out = mask_observations(out, recency_window=recency_window,
                                prefix_len=prefix_len)
        out = prune_middle(out, recency_window=recency_window,
                           prefix_len=prefix_len)
        return out, actions
    if ratio >= t[1]:
        actions.append(f"observation_masking@{ratio:.1f}%")
        log.warning("compaction: %.1f%% — observation masking", ratio)
        return mask_observations(out, recency_window=recency_window,
                                 prefix_len=prefix_len), actions
    if ratio >= t[0]:
        actions.append(f"warning@{ratio:.1f}%")
        log.warning("compaction: %.1f%% dari limit — mendekati ambang", ratio)
    return out, actions


# -- Lapis 3: full compaction via LLM --------------------------------

SUMMARIZER_SYSTEM = (
    "Kamu peringkas status kerja untuk agent otonom. Tugasmu: baca riwayat "
    "kerja lalu tulis STATUS KERJA yang padat dan faktual."
)

SUMMARIZER_INSTRUCTION = """Ringkas riwayat kerja agent berikut menjadi STATUS KERJA.
WAJIB memuat 4 bagian ini:
1. Keputusan yang sudah dibuat
2. Issue / pertanyaan yang masih terbuka
3. State implementasi saat ini
4. File yang sedang dikerjakan

Aturan: tulis padat dengan bullet, bahasa Indonesia, hanya fakta dari
riwayat (jangan mengarang). Maksimal ~40 baris.

--- RIWAYAT KERJA ---
{history}
--- AKHIR RIWAYAT ---"""


def _serialize_for_summary(middle, max_chars=600):
    """Serialisasi zona tengah jadi teks untuk summarizer."""
    lines = []
    for m in middle:
        role = m.get("role")
        content = (m.get("content") or "")
        if isinstance(content, str) and len(content) > max_chars:
            content = content[:max_chars] + "…"
        if role == "assistant":
            names = [((tc.get("function") or {}).get("name") or "?")
                     for tc in (m.get("tool_calls") or [])]
            tag = f" [tool_calls: {', '.join(names)}]" if names else ""
            lines.append(f"assistant: {content}{tag}")
        elif role == "tool":
            lines.append(f"tool({m.get('name')}): {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _post_json(url, payload, api_key, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def full_compact(messages, model, api_key, keep_recent=DEFAULT_KEEP_RECENT,
                 prefix_len=DEFAULT_PREFIX_LEN, post_fn=None):
    """Ringkas zona tengah via LLM murah; kembalikan pesan baru.

    Hasil: prefix (verbatim) + SATU pesan user "[COMPACTED] <ringkasan>"
    + keep_recent turn terakhir (verbatim).
    `post_fn(url, payload, api_key, timeout) -> (status, text)` bisa
    di-inject untuk testing; default POST ke 9router.
    """
    model = _validate_ag_model(model)
    if not messages:
        return []
    prefix_end = min(prefix_len, len(messages))
    prefix = [dict(m) for m in messages[:prefix_end]]
    body = messages[prefix_end:]
    turns = _split_turns(body)

    recent_turns = turns[-keep_recent:] if keep_recent > 0 else []
    n_recent = len(recent_turns)
    middle_turns = turns[:len(turns) - n_recent] if n_recent else turns
    middle = [m for t in middle_turns for m in t]
    recent = [dict(m) for t in recent_turns for m in t]

    if not middle:
        # tidak ada yang bisa diringkas — kembalikan apa adanya
        return [dict(m) for m in messages]

    history_text = _serialize_for_summary(middle)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUMMARIZER_SYSTEM},
            {"role": "user",
             "content": SUMMARIZER_INSTRUCTION.format(history=history_text)},
        ],
        "stream": False,
    }
    post = post_fn or _post_json
    status, text = post(API_URL, payload, api_key, 120)
    if status >= 400:
        raise RuntimeError(f"summarizer HTTP {status}: {text[:300]}")
    try:
        data = json.loads(text)
        summary = data["choices"][0]["message"].get("content") or ""
    except Exception as e:
        raise RuntimeError(f"respon summarizer tidak bisa di-parse: {e}")
    summary = summary.strip() or "(ringkasan kosong)"

    boundary = {"role": "user",
                "content": f"{COMPACTED_MARK} {summary}"}
    return prefix + [boundary] + recent


# -- Lapis 4: cache preservation -------------------------------------
# Diimplementasikan secara struktural: SEMUA fungsi di atas menerima
# `prefix_len` (default 2 = system prompt + pesan task awal) dan tidak
# pernah mengubah, me-reorder, atau menghapus messages[:prefix_len].
# Test: tests/test_compaction.py::TestPrefixPreservation.


class ContextCompactor:
    """Pembungkus stateful di atas fungsi-fungsi lapis (kompat stub lama)."""

    def __init__(self, cache_model=DEFAULT_SUMMARIZER_MODEL,
                 threshold_ratio=0.7, keep_last_turns=6,
                 context_limit=DEFAULT_CONTEXT_LIMIT,
                 recency_window=DEFAULT_RECENCY_WINDOW,
                 max_tool_chars=DEFAULT_MAX_TOOL_CHARS,
                 prefix_len=DEFAULT_PREFIX_LEN):
        self.cache_model = _validate_ag_model(cache_model)
        self.threshold_ratio = threshold_ratio
        self.keep_last_turns = keep_last_turns
        self.context_limit = context_limit
        self.recency_window = recency_window
        self.max_tool_chars = max_tool_chars
        self.prefix_len = prefix_len

    def maybe_compact(self, messages, api_key=None, prompt_tokens=None,
                      summarizer=None):
        """Satu pintu: micro per-turn + pipeline threshold bila ada usage.

        Tanpa prompt_tokens hanya micro_compact yang jalan (tanpa LLM).
        """
        out = micro_compact(messages, recency_window=self.recency_window,
                            max_tool_chars=self.max_tool_chars,
                            prefix_len=self.prefix_len)
        if prompt_tokens is None:
            return out, ["micro"]
        if summarizer is None and api_key:
            model = self.cache_model

            def summarizer(msgs, _m=model, _k=api_key):
                return full_compact(msgs, _m, _k,
                                    keep_recent=self.keep_last_turns,
                                    prefix_len=self.prefix_len)

        return apply_threshold_pipeline(
            out, prompt_tokens,
            context_limit=self.context_limit,
            recency_window=self.recency_window,
            prefix_len=self.prefix_len,
            summarizer=summarizer)
