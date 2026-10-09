#!/usr/bin/env python3
"""Gap 2 (docs/GAP-AUDIT.md G7) — structured task tracking ala TodoWrite.

Masalah: `progress.json` hanya menghitung step, bukan daftar tugas.
Model tidak punya checklist terstruktur untuk run panjang — status kerja
("sudah sampai mana") gampang hilang saat konteks dipadatkan.

Desain:
  - `TaskList(path)`: daftar task {title, status} dengan status
    pending / in_progress / completed. Persist OTOMATIS ke
    `<outdir>/tasks.json` setiap mutasi, tulis ATOMIK (file tmp + rename)
    agar crash di tengah tulis tidak merusak file.
  - API: add(title), set_status(index|title, status), list(), summary().

  PIN DARI COMPACTION — daftar task HARUS survive micro_compact &
  pipeline threshold. Dipakai TIGA lapis pertahanan (pilih yang paling
  bersih, tanpa metadata eksotis):

  1. **Ringkasan di system prompt tiap turn.** HermesLoop menyuntik
     `summary()` (<300 char) sebagai blok "## Daftar task" di system
     prompt SEBELUM setiap panggilan model. System prompt masuk
     `prefix_len` yang TIDAK PERNAH disentuh compaction (Lapis 4).
     Karena disuntik ulang tiap turn dari TaskList yang hidup, state
     task selalu segar walau riwayat tengah dipotong.

  2. **Hasil tool `task_update` dikecualikan dari pemotongan.**
     `hermes/compaction.py` mengenali tool hasil lewat nama tool
     (`STATEFUL_TOOL_NAMES = {"task_update"}`): pesan `role: tool` dengan
     nama itu TIDAK PERNAH di-offload oleh micro_compact maupun di-mask
     oleh mask_observations. Hasil tool ini kecil (ringkasan mutasi),
     jadi pengecualian tidak membebani konteks.

  3. **Ground truth di disk.** tasks.json adalah sumber kebenaran;
     loop bisa membangun ulang TaskList dari path kapan saja.

  Tanpa LLM di jalur ini: semua operasi murni Python deterministik.
  Implementasi original.
"""

import json
import logging
import os
import tempfile

log = logging.getLogger("hermes.tasks")

STATUSES = ("pending", "in_progress", "completed")

DEFAULT_MAX_TASKS = 64
DEFAULT_SUMMARY_MAX_CHARS = 300


def _normalize_title(title):
    return " ".join(str(title or "").split())


class TaskList:
    """Kelola daftar task dengan persist otomatis ke tasks.json.

    path: file json (biasanya "<outdir>/tasks.json").
    max_tasks: batas jumlah task; add() di atas batas menolak (ValueError).
    """

    def __init__(self, path, max_tasks=DEFAULT_MAX_TASKS):
        self.path = path
        self.max_tasks = max(1, int(max_tasks or DEFAULT_MAX_TASKS))
        self._tasks = []
        self._loaded = False

    # -- persistensi ---------------------------------------------------

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("tasks: %s rusak/tidak terbaca (%s) — mulai kosong",
                        self.path, e)
            return
        if isinstance(data, dict):
            data = data.get("tasks") or []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                title = _normalize_title(item.get("title"))
                status = item.get("status")
                if title and status in STATUSES:
                    self._tasks.append({"title": title, "status": status})

    def _save(self):
        """Tulis atomik: tmp file + os.replace. Tidak pernah raise."""
        self._ensure_loaded()
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            log.warning("tasks: gagal buat direktori %s: %s", directory, e)
            return
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir=directory, prefix=".tasks-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"tasks": self._tasks}, f,
                          indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            tmp = None
        except OSError as e:
            log.warning("tasks: gagal tulis %s: %s", self.path, e)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- mutasi --------------------------------------------------------

    def add(self, title):
        """Tambah task baru (status pending). Return index task baru.

        ValueError bila judul kosong, duplikat, atau daftar penuh.
        """
        self._ensure_loaded()
        title = _normalize_title(title)
        if not title:
            raise ValueError("judul task tidak boleh kosong.")
        norm = title.lower()
        for t in self._tasks:
            if t["title"].lower() == norm:
                raise ValueError(f"task '{title}' sudah ada di daftar.")
        if len(self._tasks) >= self.max_tasks:
            raise ValueError(
                f"daftar task penuh ({self.max_tasks} task) — "
                f"selesaikan/hapus dulu sebelum menambah."
            )
        self._tasks.append({"title": title, "status": "pending"})
        self._save()
        return len(self._tasks) - 1

    def _resolve(self, target):
        """target: index int ATAU judul (str). Return index. ValueError bila
        tidak ketemu."""
        self._ensure_loaded()
        if isinstance(target, int) and not isinstance(target, bool):
            idx = target
        elif isinstance(target, str) and target.strip().lstrip("-").isdigit():
            idx = int(target.strip())
        else:
            norm = _normalize_title(target).lower()
            for i, t in enumerate(self._tasks):
                if t["title"].lower() == norm:
                    return i
            raise ValueError(f"task tidak ditemukan: {target!r}")
        if not 0 <= idx < len(self._tasks):
            raise ValueError(
                f"index task {idx} di luar rentang (0..{len(self._tasks) - 1})."
            )
        return idx

    def set_status(self, target, status):
        """Ubah status task. target = index atau judul.

        ValueError bila status invalid atau target tidak ketemu.
        """
        status = str(status or "").strip().lower()
        if status not in STATUSES:
            raise ValueError(
                f"status '{status}' tidak valid. "
                f"Status valid: {', '.join(STATUSES)}"
            )
        idx = self._resolve(target)
        self._tasks[idx]["status"] = status
        self._save()
        return dict(self._tasks[idx])

    # -- baca ----------------------------------------------------------

    def list(self):
        """Kembalikan salinan daftar task [{title, status}]."""
        self._ensure_loaded()
        return [dict(t) for t in self._tasks]

    def summary(self, max_chars=DEFAULT_SUMMARY_MAX_CHARS):
        """Ringkasan 1 baris: "3/8 selesai, aktif: <judul>".

        Kosong ("") bila belum ada task. Dipotong ke max_chars.
        """
        self._ensure_loaded()
        total = len(self._tasks)
        if total == 0:
            return ""
        done = sum(1 for t in self._tasks if t["status"] == "completed")
        active = next(
            (t["title"] for t in self._tasks
             if t["status"] == "in_progress"),
            None,
        )
        parts = [f"{done}/{total} selesai"]
        if active:
            parts.append(f"aktif: {active}")
        text = ", ".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars - 1].rstrip() + "…"
        return text

    def format_list(self):
        """Render daftar untuk dibaca model/manusia."""
        self._ensure_loaded()
        if not self._tasks:
            return "(belum ada task)"
        lines = []
        for i, t in enumerate(self._tasks):
            lines.append(f"{i}. [{t['status']}] {t['title']}")
        return "\n".join(lines)
