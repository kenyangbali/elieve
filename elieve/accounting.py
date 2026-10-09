#!/usr/bin/env python3
"""elieve.accounting — Gap 3: akuntansi token & estimasi biaya per run.

- UsageTracker : akumulasi prompt/completion/total tokens per model per run.
- Estimasi biaya: tabel harga USD per 1K token dari blok `accounting:` di
  config YAML (prices). Harga yang tak dikenal -> biaya 0 + warning sekali
  (bukan crash).
- Persist: <outdir>/usage.json ditulis tiap akhir run + berkala tiap
  10 step (best effort).
- Guardrail:
  - Warning saat prompt_tokens terakhir >= context_warn_pct% dari
    context_limit (default 80%). Tanpa usage asli -> fallback chars/4.
  - run_cost_cap (USD, default 0 = nonaktif): bila estimasi biaya run
    mencapai cap -> loop berhenti rapi (status cost_capped), bukan crash.

API key tidak disentuh modul ini (baca key tetap di elieve/loop.py).
"""

import json
import os
import sys
from datetime import datetime, timezone

USAGE_FILENAME = "usage.json"
DEFAULT_CONTEXT_WARN_PCT = 80
PERIODIC_SAVE_EVERY_STEPS = 10

# Format harga: {"nama-model": {"input_per_1k": USD, "output_per_1k": USD}}.
# Nilai contoh = PLACEHOLDER; sesuaikan dengan harga aktual provider.
PLACEHOLDER_PRICES = {
    "ag/claude-opus-4-6-thinking": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    "ag/gemini-3.1-pro": {"input_per_1k": 0.0025, "output_per_1k": 0.01},
    "ag/gemini-3-flash": {"input_per_1k": 0.0005, "output_per_1k": 0.002},
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class UsageTracker:
    """Akumulasi usage per panggilan model, per model, per run."""

    def __init__(self):
        # model -> {"prompt_tokens", "completion_tokens", "total_tokens",
        #           "calls", "estimated_calls"}
        self._models = {}

    def _bucket(self, model):
        if model not in self._models:
            self._models[model] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "estimated_calls": 0,
            }
        return self._models[model]

    def record(self, model, usage, estimated=False):
        """Catat satu panggilan model.

        `usage`: dict, boleh sebagian — kunci yang dikenal:
        prompt_tokens / completion_tokens / total_tokens. Kunci hilang
        dianggap 0. Bila total_tokens hilang tapi prompt+completion ada,
        total dihitung dari keduanya. `estimated=True` menandai angka
        berasal dari estimasi (bukan usage asli provider).
        """
        usage = usage or {}
        b = self._bucket(model)
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        t = usage.get("total_tokens")
        t = int(t) if t is not None else p + c
        b["prompt_tokens"] += max(p, 0)
        b["completion_tokens"] += max(c, 0)
        b["total_tokens"] += max(t, 0)
        b["calls"] += 1
        if estimated:
            b["estimated_calls"] += 1
        return b

    def totals(self):
        """{'models': {model: {...}}, 'grand': {...}} — salinan angka."""
        models = {m: dict(v) for m, v in self._models.items()}
        grand = {"prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "calls": 0, "estimated_calls": 0}
        for v in models.values():
            for k in grand:
                grand[k] += v[k]
        return {"models": models, "grand": grand}

    def to_dict(self):
        return self.totals()

    def reset(self):
        self._models.clear()


def estimate_cost_for(tokens, price):
    """Biaya USD untuk satu bucket token berdasar satu entri harga."""
    if not price:
        return 0.0
    inp = float(price.get("input_per_1k") or 0)
    outp = float(price.get("output_per_1k") or 0)
    return (tokens.get("prompt_tokens", 0) / 1000.0) * inp \
        + (tokens.get("completion_tokens", 0) / 1000.0) * outp


class Accounting:
    """Controller Gap 3: tracker + harga + guardrail + persist + laporan.

    cfg (blok `accounting:` di YAML):
      enabled: true
      prices: {model: {input_per_1k, output_per_1k}}  (USD per 1K token)
      run_cost_cap: 0.0        # USD; 0 = nonaktif
      context_warn_pct: 80     # warning konteks >= N% dari context_limit
    context_limit: diambil dari config compaction (di-pass loop).
    """

    def __init__(self, cfg=None, outdir=".", context_limit=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.prices = dict(cfg.get("prices") or {})
        self.run_cost_cap = float(cfg.get("run_cost_cap", 0) or 0)
        self.context_warn_pct = float(
            cfg.get("context_warn_pct", DEFAULT_CONTEXT_WARN_PCT))
        self.context_limit = context_limit
        self.outdir = outdir
        self.tracker = UsageTracker()
        self._warned_context = False
        self._warned_unknown_price = set()

    # -- biaya -------------------------------------------------------

    def estimated_cost(self, prices=None):
        """{'per_model': {model: usd}, 'total_usd': usd,
        'unknown_prices': [model]}.

        Model tanpa entri harga -> biaya 0 + warning SEKALI (bukan crash).
        """
        prices = self.prices if prices is None else (prices or {})
        per_model = {}
        unknown = []
        for model, tokens in self.tracker.totals()["models"].items():
            price = prices.get(model)
            if price is None:
                unknown.append(model)
                per_model[model] = 0.0
                if model not in self._warned_unknown_price:
                    self._warned_unknown_price.add(model)
                    print(
                        f"[accounting] peringatan: tidak ada harga untuk "
                        f"model '{model}' — biaya dihitung 0. Tambahkan ke "
                        f"blok accounting.prices di config.",
                        flush=True,
                    )
            else:
                per_model[model] = round(estimate_cost_for(tokens, price), 6)
        return {
            "per_model": per_model,
            "total_usd": round(sum(per_model.values()), 6),
            "unknown_prices": unknown,
        }

    # -- guardrail ----------------------------------------------------

    def check_context_warning(self, prompt_tokens):
        """True bila pemakaian konteks >= context_warn_pct% (warning sekali).

        `prompt_tokens`: angka terakhir dari provider (atau estimasi).
        Tanpa context_limit -> tidak ada warning (return False).
        """
        if not self.context_limit or self._warned_context:
            return False
        try:
            pct = (float(prompt_tokens) / float(self.context_limit)) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        if pct >= self.context_warn_pct:
            self._warned_context = True
            print(
                f"[accounting] PERINGATAN: pemakaian konteks "
                f"~{pct:.0f}% (>= {self.context_warn_pct:g}%) dari "
                f"context_limit {self.context_limit} token "
                f"(prompt_tokens terakhir: {prompt_tokens}).",
                flush=True,
            )
            return True
        return False

    @property
    def context_warned(self):
        return self._warned_context

    def cap_reached(self):
        """True bila run_cost_cap > 0 dan estimasi biaya run >= cap."""
        if self.run_cost_cap <= 0:
            return False
        return self.estimated_cost()["total_usd"] >= self.run_cost_cap

    def cap_message(self):
        total = self.estimated_cost()["total_usd"]
        return (
            f"budget tercapai: estimasi biaya run ${total:.4f} USD mencapai "
            f"run_cost_cap ${self.run_cost_cap:.2f} USD — run dihentikan "
            f"dengan rapi (bukan crash). Rincian token di usage.json."
        )

    # -- persist + laporan ---------------------------------------------

    def usage_path(self):
        return os.path.join(self.outdir, USAGE_FILENAME)

    def save(self):
        """Tulis usage.json; kembalikan path. Best effort (boleh gagal)."""
        cost = self.estimated_cost()
        payload = {
            "models": self.tracker.totals()["models"],
            "grand": self.tracker.totals()["grand"],
            "estimated_cost_usd": cost["per_model"],
            "total_estimated_cost_usd": cost["total_usd"],
            "unknown_prices": cost["unknown_prices"],
            "run_cost_cap": self.run_cost_cap,
            "context_limit": self.context_limit,
            "context_warn_pct": self.context_warn_pct,
            "updated_at": _now_iso(),
        }
        path = self.usage_path()
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return path

    def maybe_periodic_save(self, step):
        """Best effort: simpan tiap PERIODIC_SAVE_EVERY_STEPS step."""
        if step and step % PERIODIC_SAVE_EVERY_STEPS == 0:
            try:
                self.save()
            except Exception as e:  # jangan crash-kan run
                print(f"[accounting] gagal simpan berkala usage.json: {e}",
                      flush=True)

    def summary_text(self):
        """Ringkasan akhir run untuk stdout."""
        t = self.tracker.totals()
        cost = self.estimated_cost()
        lines = ["", "===== Ringkasan pemakaian (Gap 3) ====="]
        if not t["models"]:
            lines.append("(tidak ada panggilan model tercatat)")
        for model, v in sorted(t["models"].items()):
            est = " (estimasi)" if v["estimated_calls"] else ""
            lines.append(
                f"- {model}{est}: prompt={v['prompt_tokens']:,} "
                f"completion={v['completion_tokens']:,} "
                f"total={v['total_tokens']:,} "
                f"calls={v['calls']} "
                f"biaya~${cost['per_model'][model]:.4f} USD"
            )
        g = t["grand"]
        lines.append(
            f"TOTAL: prompt={g['prompt_tokens']:,} "
            f"completion={g['completion_tokens']:,} "
            f"total={g['total_tokens']:,} "
            f"calls={g['calls']} "
            f"=> estimasi biaya ${cost['total_usd']:.4f} USD"
        )
        if cost["unknown_prices"]:
            lines.append(
                "harga tak dikenal (biaya=0): "
                + ", ".join(cost["unknown_prices"])
            )
        lines.append("=======================================")
        return "\n".join(lines)
