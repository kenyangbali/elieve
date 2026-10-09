"""Gap 6 — Plan mode: riset read-only, tulis PLAN.md, berhenti.

Dipakai dua arah:
  1. CLI ``--plan`` (elieve.loop main): run single-agent read-only.
  2. Orchestrator "recon gate" (``orchestrator.plan_first: true``):
     fase plan read-only SEBELUM worker mahal di-spawn.

Mekanisme: ElieveLoop biasa dengan ``no_exec=True`` (toolset tanpa
``exec`` — mekanisme read-only yang SUDAH ada, tidak diduplikasi) +
system-prompt addendum yang memaksa model meriset saja. Setelah loop
selesai, jawaban akhir dinormalisasi menjadi ``<outdir>/PLAN.md`` dengan
bagian-bagian wajib, lalu run BERHENTI (return code loop). TIDAK ADA
fase eksekusi setelah rencana.

Keamanan read-only:
  - ``exec`` dihapus dari dispatch DAN tool schemas -> model bahkan
    tidak melihat tool-nya;
  - bila model tetap mencoba memanggil ``exec`` (atau via fallback
    ```tool block), loop mengembalikan observasi "unknown tool: exec"
    — bukan crash;
  - mode plan tidak me-load MCP (tool eksternal bisa mengeksekusi).

API key tidak pernah di-hardcode di sini; ElieveLoop menyelesaikannya
via providers.resolve_api_key seperti biasa.
"""

import json
import os
import re
from datetime import datetime, timezone

from .loop import ElieveLoop
from .prompts import get_system_prompt
from .providers import check_model_allowed

# Bagian-bagian wajib PLAN.md per bahasa. Addendum system prompt memaksa
# model memakai header ## yang persis sama; writer di bawah memastikannya
# secara deterministik (bagian yang hilang ditandai, bukan dikarang).
PLAN_SECTIONS = {
    "id": ["Tujuan", "Permukaan yang dipetakan",
           "Langkah rencana", "Estimasi"],
    "en": ["Goal", "Mapped surface", "Plan steps", "Estimate"],
}

PLAN_ADDENDUM = {
    "id": (
        "\n\nMODE RENCANA (PLAN MODE — read-only):\n"
        "1. Kamu sedang MERISET, bukan mengeksekusi. Tool `exec` TIDAK "
        "tersedia di sesi ini — jangan pernah memanggil atau memintanya.\n"
        "2. Petakan permukaan target hanya dengan read_file, list_dir, "
        "grep (dan remember bila perlu mencatat fakta).\n"
        "3. Jawaban akhirmu ADALAH rencananya. Tulis dengan TEPAT "
        "bagian-bagian ini (header ##):\n"
        "   ## Tujuan\n"
        "   ## Permukaan yang dipetakan\n"
        "   ## Langkah rencana\n"
        "   ## Estimasi\n"
        "   Isi tiap bagian dengan konkret: file/endpoint/entry point yang "
        "relevan, urutan langkah yang bisa dieksekusi nanti, dan estimasi "
        "usaha per langkah.\n"
        "4. Jangan mengubah file apa pun. Jangan menjalankan perintah apa pun."
    ),
    "en": (
        "\n\nPLAN MODE (read-only):\n"
        "1. You are RESEARCHING, not executing. The `exec` tool is NOT "
        "available in this session — never call or ask for it.\n"
        "2. Map the target surface using only read_file, list_dir, grep "
        "(and remember to record facts).\n"
        "3. Your final answer IS the plan. Write it with EXACTLY these "
        "sections (## headers):\n"
        "   ## Goal\n"
        "   ## Mapped surface\n"
        "   ## Plan steps\n"
        "   ## Estimate\n"
        "   Be concrete in each section: relevant files/endpoints/entry "
        "points, an ordered list of steps to execute later, and an effort "
        "estimate per step.\n"
        "4. Do not modify any file. Do not run any command."
    ),
}

_MISSING_NOTE = {
    "id": "(bagian ini tidak dihasilkan model pada fase riset)",
    "en": "(this section was not produced by the model during research)",
}

_OUT_HEADER_RE = re.compile(
    r"^# Elieve — hasil\n\n(?:- .*\n)+\n---\n\n", re.S)


def _lang(lang):
    return "id" if (lang or "").strip().lower() == "id" else "en"


def resolve_plan_model(plan_cfg, main_model, model_policy=None):
    """Model efektif fase plan: ``plan.plan_model`` atau model utama.

    Validasi via policy (check_model_allowed) — aturan ag/* vs
    bns/*/oc/* datang dari config, bukan hardcode di sini.
    Raises ValueError bila kosong atau ditolak policy.
    """
    raw = (((plan_cfg or {}).get("plan_model") or "").strip()
           or (main_model or "").strip())
    if not raw:
        raise ValueError(
            "no plan model: set plan.plan_model in the config or pass "
            "a main model.")
    return check_model_allowed(raw, model_policy or {})


def _read_final_answer(outdir):
    """Ambil jawaban akhir dari OUT.md (header standar di-strip)."""
    try:
        with open(os.path.join(outdir, "OUT.md")) as f:
            body = f.read()
    except OSError:
        return ""
    return _OUT_HEADER_RE.sub("", body, count=1).strip()


def _read_progress(outdir):
    """(status, step) dari progress.json; best effort."""
    try:
        with open(os.path.join(outdir, "progress.json")) as f:
            prog = json.load(f)
        return str(prog.get("status") or "?"), int(prog.get("step") or 0)
    except Exception:
        return "?", 0


def _ensure_sections(body, sections, lang):
    """Pastikan tiap section wajib ada sebagai header ##.

    Model yang nurut sudah menulisnya (lihat addendum). Bila ada yang
    hilang, tambahkan header + penanda jujur — TIDAK dikarang isinya.
    """
    out = (body or "").strip() or "(model tidak memberi jawaban akhir)"
    missing = []
    for sec in sections:
        if not re.search(r"(?m)^#{1,3}\s*" + re.escape(sec) + r"\s*$",
                         out, re.IGNORECASE):
            missing.append(sec)
    for sec in missing:
        out += ("\n\n## " + sec + "\n\n"
                + _MISSING_NOTE[_lang(lang)])
    return out


def write_plan_md(outdir, task, model, final_text, lang="en",
                  status="done", step=0):
    """Tulis <outdir>/PLAN.md; kembalikan path-nya."""
    lg = _lang(lang)
    sections = PLAN_SECTIONS[lg]
    title = ("# Rencana — elieve plan mode"
             if lg == "id" else "# Plan — elieve plan mode")
    header = "\n".join([
        title,
        "",
        f"- Task: {task}",
        f"- Model: {model}",
        f"- Status loop: {status}",
        f"- Step: {step}",
        "- Mode: read-only (tanpa exec)",
        f"- Waktu: {datetime.now(timezone.utc).isoformat()}",
        "",
        "---",
        "",
    ])
    body = _ensure_sections(final_text, sections, lg)
    path = os.path.join(outdir, "PLAN.md")
    os.makedirs(outdir, exist_ok=True)
    with open(path, "w") as f:
        f.write(header + body + "\n")
    return path


def run_plan(task, outdir, model, provider_cfg=None, model_policy=None,
             max_steps=30, lang="en", workspace_root=None,
             system_prompt=None, compaction_cfg=None, memory_cfg=None,
             permissions_cfg=None, hooks_cfg=None, tasks_cfg=None,
             accounting_cfg=None, checkpoints_cfg=None,
             resume_record=None):
    """Jalankan fase plan read-only; tulis PLAN.md; kembalikan exit code.

    Ini "mekanisme plan yang sama" dipakai CLI --plan dan recon gate
    orchestrator. ``model`` diasumsikan sudah lolos resolve_plan_model /
    check_model_allowed oleh pemanggil.

    Catatan: pemanggil WAJIB sudah memanggil
    ``elieve.tools.configure_roots(workspace_root)`` sebelumnya
    (seperti main() lakukan) agar tool file tahu sandbox root.
    """
    outdir = os.path.abspath(outdir)
    lg = _lang(lang)
    base = (system_prompt if system_prompt is not None
            else get_system_prompt(lg, workspace_root=workspace_root))
    prompt = base + PLAN_ADDENDUM[lg]
    loop = ElieveLoop(
        task=task,
        outdir=outdir,
        model=model,
        max_steps=max(1, int(max_steps)),
        system_prompt=prompt,
        compaction_cfg=compaction_cfg,
        memory_cfg=memory_cfg,
        permissions_cfg=permissions_cfg,
        no_exec=True,  # paksa read-only via mekanisme yang sudah ada
        hooks_cfg=hooks_cfg,
        tasks_cfg=tasks_cfg,
        accounting_cfg=accounting_cfg,
        provider_cfg=provider_cfg,
        model_policy=model_policy,
        checkpoints_cfg=checkpoints_cfg,
        resume_record=resume_record,
    )
    rc = loop.run()
    final = _read_final_answer(outdir)
    status, step = _read_progress(outdir)
    plan_md = write_plan_md(outdir, task=task, model=loop.model,
                            final_text=final, lang=lg,
                            status=status, step=step)
    print(f"[plan] PLAN.md ditulis: {plan_md} (rc={rc})", flush=True)
    return rc
