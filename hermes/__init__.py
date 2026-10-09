"""hermes-agent — framework agent AI modular.

Baseline v1: ReAct loop dengan function calling (port rapi dari
hermes-hunter/hunter.py). Pola arsitektur lanjutan (compaction, memory,
permission classifier, orchestrator) didesain di docs/ARCHITECTURE.md
dan diimplementasi bertahap per fase.
"""

__version__ = "0.1.0"


def __getattr__(name):
    # Lazy import agar `python3 -m hermes.loop` tidak memicu
    # RuntimeWarning modul-ganda saat package __init__ dieksekusi.
    if name == "HermesLoop":
        from .loop import HermesLoop

        return HermesLoop
    raise AttributeError(f"module 'hermes' has no attribute {name!r}")


__all__ = ["HermesLoop", "__version__"]
