"""hermes-agent — modular AI agent framework.

Baseline v1: a ReAct loop with function calling. The advanced architecture
patterns (compaction, memory, permission classifier, orchestrator) are
designed in docs/ARCHITECTURE.md and implemented incrementally per phase.
"""

__version__ = "0.1.0"


def __getattr__(name):
    # Lazy import so `python3 -m hermes.loop` does not trigger a
    # double-module RuntimeWarning when the package __init__ runs.
    if name == "HermesLoop":
        from .loop import HermesLoop

        return HermesLoop
    raise AttributeError(f"module 'hermes' has no attribute {name!r}")


__all__ = ["HermesLoop", "__version__"]
