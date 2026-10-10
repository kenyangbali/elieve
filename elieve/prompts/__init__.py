"""elieve.prompts — system prompts: generic default + opt-in profiles.

Use :func:`get_system_prompt` to fetch a prompt::

    get_system_prompt("en")                    # generic default (English)
    get_system_prompt("id", profile="hunter")  # bug-hunter profile (Indonesian)

``profile`` selects a persona preset:

- ``"default"`` — neutral general-purpose agent (the default).
- ``"hunter"`` — security bug-hunter persona (strict evidence rules).

Unknown profiles fall back to ``"default"``; unknown language codes fall
back to English. The ``{WORKSPACE_ROOT}`` placeholder is replaced with the
given workspace root (``"./workspace"`` when omitted).

Backward compatible: ``get_system_prompt(lang)`` behaves exactly as
before, returning the (now generic) default prompt.
"""

from .en import SYSTEM_PROMPT as EN_SYSTEM_PROMPT
from .hunter import HUNTER_SYSTEM_PROMPT_EN, HUNTER_SYSTEM_PROMPT_ID
from .id import SYSTEM_PROMPT as ID_SYSTEM_PROMPT

_PROFILES = {
    "default": {"en": EN_SYSTEM_PROMPT, "id": ID_SYSTEM_PROMPT},
    "hunter": {"en": HUNTER_SYSTEM_PROMPT_EN, "id": HUNTER_SYSTEM_PROMPT_ID},
}

__all__ = [
    "get_system_prompt",
    "EN_SYSTEM_PROMPT",
    "ID_SYSTEM_PROMPT",
    "HUNTER_SYSTEM_PROMPT_EN",
    "HUNTER_SYSTEM_PROMPT_ID",
]


def get_system_prompt(lang="en", workspace_root=None, profile="default"):
    """Return the system prompt for ``lang`` and ``profile``.

    Args:
        lang: ``"en"`` or ``"id"`` (anything else falls back to ``"en"``).
        workspace_root: substituted into the ``{WORKSPACE_ROOT}``
            placeholder; defaults to ``"./workspace"`` when omitted.
        profile: ``"default"`` (generic agent) or ``"hunter"``
            (bug-hunter persona). Unknown values fall back to ``"default"``.
    """
    presets = _PROFILES.get((profile or "default").strip().lower(),
                            _PROFILES["default"])
    template = presets.get((lang or "en").strip().lower(), presets["en"])
    root = workspace_root if workspace_root else "./workspace"
    return template.replace("{WORKSPACE_ROOT}", str(root))
