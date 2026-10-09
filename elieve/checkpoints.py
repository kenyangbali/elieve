#!/usr/bin/env python3
"""elieve.checkpoints — Gap 4 (docs/GAP-AUDIT.md G4): checkpoints / resume.

Claude Code menyimpan checkpoint tiap prompt dan bisa `--resume` sesi
lama. Elieve tiap run mulai dari nol; `progress.json` hanya menyimpan
step terakhir, bukan percakapan. Modul ini menutup gap itu:

- save_checkpoint(outdir, step, messages, state): snapshot percakapan
  (messages) + nomor step + ringkasan state ke
  <outdir>/checkpoints/ckpt-<step>.json, tulis ATOMIK (tmp + rename)
  agar file setengah-tulis tidak pernah terlihat.
- load_checkpoint(outdir, step=None): muat checkpoint; step=None =
  snapshot terakhir. File corrupt / tidak ada -> CheckpointError dengan
  pesan JELAS (bukan traceback misterius).
- list_checkpoints(outdir): daftar snapshot yang ada, urut naik per step.

Loop (elieve/loop.py) memakai modul ini untuk:
  1. menyimpan checkpoint tiap N step (config `checkpoints.every_n_steps`,
     default 10; `enabled` default true);
  2. menyimpan checkpoint tambahan saat PreCompact hook akan memampatkan
     konteks (titik waktu yang sama dengan hook — tanpa mekanisme
     duplikat, cukup panggil save_checkpoint di jalur itu);
  3. CLI --resume / --list-checkpoints.

Isi snapshot (JSON):
  {
    "version": 1,
    "step": <int>,                    # step terakhir yang selesai
    "saved_at": <ISO 8601 UTC>,
    "task": "...",                    # dari state["task"] bila ada
    "model": "...",                   # dari state["model"] bila ada
    "messages": [ ... ],              # riwayat percakapan lengkap
    "state": {                        # ringkasan state (bebas dibentuk loop)
      "usage": {...},                 # elieve.accounting.UsageTracker.to_dict()
      "tasks": [ ... ],               # elieve.tasks.TaskList.list()
      "max_steps": 40,
    },
  }

Resume = lanjut dari step+messages snapshot (bukan mulai dari nol).
API key tidak disentuh modul ini.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone

CHECKPOINT_DIRNAME = "checkpoints"
CHECKPOINT_PREFIX = "ckpt-"
CHECKPOINT_SUFFIX = ".json"
CHECKPOINT_VERSION = 1

DEFAULT_ENABLED = True
DEFAULT_EVERY_N_STEPS = 10

_CKPT_RE = re.compile(r"^ckpt-(\d+)\.json$")


class CheckpointError(Exception):
    """Error checkpoints yang jelas: tidak ada / corrupt / tidak valid."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def checkpoint_dir(outdir):
    """Path direktori <outdir>/checkpoints."""
    return os.path.join(outdir, CHECKPOINT_DIRNAME)


def _atomic_write_json(path, record):
    """Tulis JSON secara atomik: file tmp di dir yang sama + os.replace."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=d, prefix=".ckpt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_checkpoint(outdir, step, messages, state=None):
    """Simpan snapshot percakapan; return path file checkpoint.

    step: nomor step terakhir yang selesai (int >= 0).
    messages: daftar dict message (system/user/assistant/tool) — harus
      JSON-serializable.
    state: dict ringkasan state (usage, tasks, task, model, ...) atau None.
    Melempar CheckpointError bila step/messages tidak valid, dan
    OSError/CheckpointError bila tulis gagal.
    """
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        raise CheckpointError(
            f"save_checkpoint: step harus int >= 0, dapat: {step!r}")
    if not isinstance(messages, list) or not all(
            isinstance(m, dict) for m in messages):
        raise CheckpointError(
            "save_checkpoint: messages harus daftar dict message.")
    state = dict(state or {})
    record = {
        "version": CHECKPOINT_VERSION,
        "step": step,
        "saved_at": _utcnow(),
        "task": state.get("task"),
        "model": state.get("model"),
        "messages": messages,
        "state": state,
    }
    path = os.path.join(
        checkpoint_dir(outdir),
        f"{CHECKPOINT_PREFIX}{step}{CHECKPOINT_SUFFIX}",
    )
    try:
        _atomic_write_json(path, record)
    except OSError as e:
        raise CheckpointError(
            f"save_checkpoint: gagal menulis {path}: {e}")
    return path


def _scan(outdir):
    """Daftar (step:int, path:str) dari file ckpt-*.json yang valid namanya."""
    d = checkpoint_dir(outdir)
    found = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for name in names:
        m = _CKPT_RE.match(name)
        if m:
            found.append((int(m.group(1)), os.path.join(d, name)))
    found.sort(key=lambda t: t[0])
    return found


def list_checkpoints(outdir):
    """Daftar snapshot yang ada: [{'step', 'path'}] urut naik per step."""
    return [{"step": step, "path": path} for step, path in _scan(outdir)]


def load_checkpoint(outdir, step=None):
    """Muat checkpoint; return dict record (lihat docstring modul).

    step=None -> snapshot dengan step terbesar (terakhir).
    Melempar CheckpointError (pesan jelas) bila:
      - tidak ada checkpoint sama sekali di <outdir>/checkpoints/;
      - step yang diminta tidak ditemukan;
      - file bukan JSON valid / strukturnya tidak valid (corrupt).
    Tidak pernah membiarkan JSONDecodeError mentah lolos.
    """
    found = _scan(outdir)
    if not found:
        raise CheckpointError(
            f"tidak ada checkpoint di {checkpoint_dir(outdir)} "
            f"(direktori kosong atau belum pernah ada run "
            f"dengan checkpoint aktif di outdir ini).")
    if step is None:
        step, path = found[-1]
    else:
        match = [p for s, p in found if s == step]
        if not match:
            have = ", ".join(str(s) for s, _ in found)
            raise CheckpointError(
                f"checkpoint step {step} tidak ditemukan di "
                f"{checkpoint_dir(outdir)}; yang ada: step {have}.")
        path = match[0]
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except json.JSONDecodeError as e:
        raise CheckpointError(
            f"checkpoint CORRUPT (bukan JSON valid): {path}: {e}. "
            f"Hapus file ini bila ingin run baru, atau pakai "
            f"--resume dengan outdir lain.")
    except OSError as e:
        raise CheckpointError(
            f"checkpoint tidak bisa dibaca: {path}: {e}")
    problems = _validate_record(record)
    if problems:
        raise CheckpointError(
            f"checkpoint CORRUPT (struktur tidak valid): {path}: "
            + "; ".join(problems))
    return record


def _validate_record(record):
    """Return daftar masalah struktur; [] bila valid."""
    problems = []
    if not isinstance(record, dict):
        return [f"root harus object, dapat {type(record).__name__}"]
    ver = record.get("version")
    if ver != CHECKPOINT_VERSION:
        problems.append(
            f"version tidak didukung: {ver!r} "
            f"(modul ini versi {CHECKPOINT_VERSION})")
    step = record.get("step")
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        problems.append(f"'step' harus int >= 0, dapat: {step!r}")
    msgs = record.get("messages")
    if not isinstance(msgs, list) or not all(
            isinstance(m, dict) for m in msgs):
        problems.append("'messages' harus daftar dict message")
    if not isinstance(record.get("state"), dict):
        problems.append("'state' harus object")
    return problems


def should_save(step, every_n_steps):
    """True bila checkpoint periodik jatuh tempo di step ini."""
    try:
        n = int(every_n_steps)
    except (TypeError, ValueError):
        return False
    return n > 0 and step > 0 and step % n == 0
