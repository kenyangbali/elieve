"""elieve.prompts — intentionally empty.

elieve ("Hermes, Anthropic-style") ships with NO built-in system prompt,
persona, soul.md, rules, or modes. The framework is blank by design: the
operator brings their own prompt — or runs with none at all.

:func:`get_system_prompt` is kept for backward compatibility and ALWAYS
returns ``""``. Supply a prompt explicitly via the ``system_prompt``
config key or the ``--system-prompt`` CLI flag. When nothing is
supplied, the loop sends NO system message at all.
"""

__all__ = ["get_system_prompt"]


def get_system_prompt(lang="en", workspace_root=None, profile="default"):
    """Return the built-in system prompt: always ``""``.

    All arguments are accepted for backward compatibility and ignored —
    there is no default persona, in any language, under any profile.
    """
    return ""
