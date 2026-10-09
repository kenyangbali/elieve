"""Phase 1 — context compaction: 4-layer pipeline (full implementation).

Problem: the `messages` history in HermesLoop grows unbounded until
max_steps. Re-sending the whole history every turn bloats input tokens.

Design (docs/PHASE1-COMPACTION.md):
  Layer 1 — per-turn microcompaction, pure string ops, no LLM.
  Layer 2 — progressive threshold pipeline based on prompt_tokens vs limit.
  Layer 3 — full compaction via a cheap LLM, rarely.
  Layer 4 — the prefix (system + initial messages) is NEVER modified.

Message shapes used here:
  system            -> {"role": "system", "content": str}
  user (initial)    -> {"role": "user", "content": str}
  assistant         -> {"role": "assistant", "content": str|None,
                        "tool_calls": [{id, function:{name, arguments}}]}
  tool              -> {"role": "tool", "tool_call_id": str,
                        "name": str, "content": str}

Every function here RETURNS a new list — inputs are never mutated.
Original implementation.
"""

import json
import logging
import time

from .providers import (
    ProviderConfig,
    check_model_allowed,
    post_chat_completions,
)

log = logging.getLogger("hermes.compaction")

# -- constants -------------------------------------------------------
OFFLOADED_FMT = "[offloaded: {summary}]"   # Layer 1: old tool-result pointer
MASKED_PTR = "[offloaded to scratch]"      # Layer 2 (80%): observation masking
PRUNED_PTR = "[pruned]"                    # Layer 2 (85%): fast pruning
COMPACTED_MARK = "[COMPACTED]"             # Layer 3: LLM summary message

DEFAULT_CONTEXT_LIMIT = 64000
DEFAULT_RECENCY_WINDOW = 5
DEFAULT_MAX_TOOL_CHARS = 2000
DEFAULT_THRESHOLDS = [70, 80, 85, 90, 95]
DEFAULT_SUMMARIZER_MODEL = "ag/gemini-3-flash"
DEFAULT_KEEP_RECENT = 5
DEFAULT_PREFIX_LEN = 2          # system prompt + initial task message
DEFAULT_MAX_AGE_HOURS = 24

# Gap 2 — tools whose results carry STATE (not just observations).
# Messages from these tools are excluded from micro_compact trimming and
# observation masking: task state must survive compaction.
# Ground truth stays in tasks.json; a fresh summary is injected into the
# system prompt every turn by HermesLoop ("## Daftar task"). See hermes/tasks.py.
STATEFUL_TOOL_NAMES = frozenset({"task_update"})

CHARS_PER_TOKEN = 4             # token-estimate heuristic when usage is absent


# -- basic utils ------------------------------------------------------

def estimate_tokens(messages) -> int:
    """Rough token estimate: chars/4 + per-message overhead.

    Used when the API response omits `usage`.
    """
    total = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // CHARS_PER_TOKEN
        total += 8  # role / message-structure overhead
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += len(json.dumps(fn, ensure_ascii=False)) // CHARS_PER_TOKEN
    return total


def _one_line_summary(content, limit=120) -> str:
    """One-line summary for an offload pointer: first non-empty line."""
    text = " ".join(str(content).split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text or "(hasil kosong)"


def _split_turns(body):
    """Group the body (outside the prefix) into turns.

    One turn starts at an assistant message and covers the tool/user
    messages after it until the next assistant message.
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
    """tool_call_ids referenced by the last turn = 'still active'."""
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
    """Global indexes of messages inside the recency window (untouchable)."""
    keep = turns[-recency_window:] if recency_window > 0 else []
    protected = set()
    # remap turn -> body index
    idx = 0
    for t in turns:
        for _ in t:
            if t in keep:
                protected.add(prefix_len + idx)
            idx += 1
    return protected


# -- Layer 1: microcompaction ----------------------------------------

def micro_compact(messages, recency_window=DEFAULT_RECENCY_WINDOW,
                  max_tool_chars=DEFAULT_MAX_TOOL_CHARS,
                  prefix_len=DEFAULT_PREFIX_LEN,
                  max_age_hours=DEFAULT_MAX_AGE_HOURS):
    """Trim old tool results into pointers. Pure string ops, no LLM.

    Rules:
      - the prefix (system + initial messages) is NEVER touched;
      - the last N turns (recency_window) are untouched;
      - tool results whose tool_call_id is referenced by the last turn
        ("active") are untouched;
      - messages older than max_age_hours (when carrying an epoch "ts"
        field) in the middle zone are dropped.
    Returns: a NEW messages list.
    """
    if not messages:
        return []
    out = [dict(m) for m in messages]  # shallow copy per message
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
        # time-based clearing: only for ts-bearing messages in the middle zone
        ts = m.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            if (now - ts) > max_age_hours * 3600:
                drop_idx.add(gi)
                continue
        if m.get("role") != "tool":
            continue
        if m.get("tool_call_id") in active_ids:
            continue
        # Gap 2: state-carrying tool results must not be trimmed.
        if m.get("name") in STATEFUL_TOOL_NAMES:
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
    """80%: old tool results outside the recency window -> marker.

    Metadata (tool_call_id, name) is kept so tool_calls pairing stays valid.
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
        # Gap 2: state-carrying tool results must not be masked.
        if m.get("name") in STATEFUL_TOOL_NAMES:
            continue
        if m.get("content") != marker:
            out[gi] = dict(m, content=marker)
    return out


def prune_middle(messages, recency_window=DEFAULT_RECENCY_WINDOW,
                 prefix_len=DEFAULT_PREFIX_LEN,
                 max_user_chars=500):
    """85% fast pruning, working from the middle:
      - assistant messages in the middle zone: text content emptied
        (tool_calls KEPT so tool pairing stays valid);
      - user messages in the middle zone: cut to max_user_chars.
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
    """Progressive pipeline based on the prompt_tokens / context_limit ratio.

    thresholds = [t70, t80, t85, t90, t95] (percent).
      70% -> warning only (log + record action);
      80% -> observation masking (full recency_window);
      85% -> masking + backward fast pruning;
      90% -> aggressive masking (recency_window 5 -> 2);
      95% -> full compaction via `summarizer` (a callable
             messages -> new_messages). When summarizer is None, fall back
             to aggressive masking so the loop never crashes.

    Returns: (new_messages, actions: [str]).
    The prefix is never modified by any action.
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
    """Serialize the middle zone to text for the summarizer."""
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


def _default_post(provider_cfg, url, payload, api_key, timeout=120):
    """Default POST for the summarizer: via the configured provider."""
    cfg = provider_cfg if provider_cfg is not None else ProviderConfig()
    return post_chat_completions(cfg, payload, api_key=api_key,
                                 timeout=timeout)


def full_compact(messages, model, api_key, keep_recent=DEFAULT_KEEP_RECENT,
                 prefix_len=DEFAULT_PREFIX_LEN, post_fn=None,
                 model_policy=None, provider_cfg=None):
    """Summarize the middle zone via a cheap LLM; return the new messages.

    Result: prefix (verbatim) + ONE user message "[COMPACTED] <summary>"
    + keep_recent latest turns (verbatim).
    `post_fn(url, payload, api_key, timeout) -> (status, text)` can be
    injected for testing; the default POSTs via the configured provider.
    `model_policy` is enforced via providers.check_model_allowed
    (policy-driven; no hardcoded model rules here).
    """
    model = check_model_allowed(model, model_policy)
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
        # nothing to summarize — return as-is
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
    post = post_fn or (lambda url, payload, api_key, timeout=120:
                       _default_post(provider_cfg, url, payload, api_key,
                                     timeout))
    cfg = provider_cfg if provider_cfg is not None else ProviderConfig()
    url = cfg.base_url.rstrip("/") + "/chat/completions" if cfg.base_url else ""
    status, text = post(url, payload, api_key, 120)
    if status >= 400:
        raise RuntimeError(f"summarizer HTTP {status}: {text[:300]}")
    try:
        data = json.loads(text)
        summary = data["choices"][0]["message"].get("content") or ""
    except Exception as e:
        raise RuntimeError(f"summarizer response could not be parsed: {e}")
    summary = summary.strip() or "(empty summary)"

    boundary = {"role": "user",
                "content": f"{COMPACTED_MARK} {summary}"}
    return prefix + [boundary] + recent


# -- Layer 4: cache preservation -------------------------------------
# Implemented structurally: EVERY function above accepts `prefix_len`
# (default 2 = system prompt + initial task message) and never modifies,
# reorders, or deletes messages[:prefix_len].
# Test: tests/test_compaction.py::TestPrefixPreservation.


class ContextCompactor:
    """Stateful wrapper over the layer functions (legacy compat stub)."""

    def __init__(self, cache_model=DEFAULT_SUMMARIZER_MODEL,
                 threshold_ratio=0.7, keep_last_turns=6,
                 context_limit=DEFAULT_CONTEXT_LIMIT,
                 recency_window=DEFAULT_RECENCY_WINDOW,
                 max_tool_chars=DEFAULT_MAX_TOOL_CHARS,
                 prefix_len=DEFAULT_PREFIX_LEN,
                 model_policy=None, provider_cfg=None):
        self.cache_model = check_model_allowed(cache_model, model_policy)
        self.model_policy = dict(model_policy or {})
        self.provider_cfg = provider_cfg
        self.threshold_ratio = threshold_ratio
        self.keep_last_turns = keep_last_turns
        self.context_limit = context_limit
        self.recency_window = recency_window
        self.max_tool_chars = max_tool_chars
        self.prefix_len = prefix_len

    def maybe_compact(self, messages, api_key=None, prompt_tokens=None,
                      summarizer=None):
        """Single entry: per-turn micro + threshold pipeline when usage exists.

        Without prompt_tokens only micro_compact runs (no LLM).
        """
        out = micro_compact(messages, recency_window=self.recency_window,
                            max_tool_chars=self.max_tool_chars,
                            prefix_len=self.prefix_len)
        if prompt_tokens is None:
            return out, ["micro"]
        if summarizer is None and api_key:
            model = self.cache_model
            policy = self.model_policy
            prov = self.provider_cfg

            def summarizer(msgs, _m=model, _k=api_key):
                return full_compact(msgs, _m, _k,
                                    keep_recent=self.keep_last_turns,
                                    prefix_len=self.prefix_len,
                                    model_policy=policy,
                                    provider_cfg=prov)

        return apply_threshold_pipeline(
            out, prompt_tokens,
            context_limit=self.context_limit,
            recency_window=self.recency_window,
            prefix_len=self.prefix_len,
            summarizer=summarizer)


# -- hook lifecycle integration (Gap 1, docs/GAP-AUDIT.md G1) -----------
# The functions above MUST stay pure (no side effects) so they can be
# tested deterministically; the loop fires PreCompact/PostCompact hooks
# around pipeline calls via the helpers below (None-safe).

def fire_pre_compact(runner, ctx):
    """Fire the PreCompact hook before compressing. runner=None -> no-op."""
    if runner is None:
        return {}
    try:
        return runner.pre_compact(ctx or {})
    except Exception as e:
        log.warning("PreCompact failed (run continues): %s", e)
        return {"error": str(e)}


def fire_post_compact(runner, ctx, info=None):
    """Fire the PostCompact hook after compressing. runner=None -> no-op."""
    if runner is None:
        return {}
    try:
        merged = dict(ctx or {})
        if info:
            merged.update(info)
        return runner.post_compact(merged)
    except Exception as e:
        log.warning("PostCompact gagal (run lanjut): %s", e)
        return {"error": str(e)}
