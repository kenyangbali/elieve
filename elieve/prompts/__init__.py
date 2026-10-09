"""elieve.prompts — default system prompts (English + Indonesian).

Use :func:`get_system_prompt` to fetch the default prompt for a language
code (``"en"`` | ``"id"``). Unknown codes fall back to English. The
``{WORKSPACE_ROOT}`` placeholder is replaced with the given workspace
root (``"./workspace"`` when omitted).
"""

from .en import SYSTEM_PROMPT as EN_SYSTEM_PROMPT
from .id import SYSTEM_PROMPT as ID_SYSTEM_PROMPT

_PROMPTS = {"en": EN_SYSTEM_PROMPT, "id": ID_SYSTEM_PROMPT}

__all__ = ["get_system_prompt", "EN_SYSTEM_PROMPT", "ID_SYSTEM_PROMPT"]


def get_system_prompt(lang="en", workspace_root=None):
    """Return the default system prompt for ``lang``.

    Args:
        lang: ``"en"`` or ``"id"`` (anything else falls back to ``"en"``).
        workspace_root: substituted into the ``{WORKSPACE_ROOT}``
            placeholder; defaults to ``"./workspace"`` when omitted.
    """
    template = _PROMPTS.get((lang or "en").strip().lower(), _PROMPTS["en"])
    root = workspace_root if workspace_root else "./workspace"
    return template.replace("{WORKSPACE_ROOT}", str(root))
