"""elieve — modular AI agent framework.

Baseline v1: a ReAct loop with function calling. The advanced architecture
patterns (compaction, memory, permission classifier, orchestrator) are
designed in docs/ARCHITECTURE.md and implemented incrementally per phase.
"""

__version__ = "0.1.0"


def __getattr__(name):
    # Lazy import so `python3 -m elieve.loop` does not trigger a
    # double-module RuntimeWarning when the package __init__ runs.
    if name == "ElieveLoop":
        from .loop import ElieveLoop

        return ElieveLoop
    raise AttributeError(f"module 'elieve' has no attribute {name!r}")


__all__ = ["ElieveLoop", "__version__"]
