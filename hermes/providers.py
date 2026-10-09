"""hermes.providers — OpenAI-compatible provider abstraction.

A ProviderConfig describes *where* and *how* to call a chat-completions
endpoint. It never carries a raw secret: API keys are resolved at runtime,
either from a named environment variable or from a special key source.

Key sources (``resolve_api_key``):
  - ``key_provider="9router"``: read from the local 9router sqlite DB
    (``~/.9router/db/data.sqlite``, table ``apiKeys``,
    row ``name='Default Key'``);
  - otherwise: ``os.environ[api_key_env]`` where ``api_key_env`` is the
    *name* of the variable (a clear error is raised when it is missing).

Keys are never printed, logged, or written to disk by this module.

Model allow/forbid policy (``check_model_allowed``):
  - ``policy`` is ``{"allow": [...], "forbid": [...]}``;
    missing or empty lists mean unrestricted;
  - entries support a trailing ``*`` prefix wildcard (e.g. ``"ag/*"``);
  - ``forbid`` wins over ``allow``;
  - violations raise ``ValueError``; on success the normalized
    (stripped) model name is returned.
"""

import json
import os
import sqlite3

NINE_ROUTER_DB = os.path.expanduser("~/.9router/db/data.sqlite")
NINE_ROUTER_DEFAULT_KEY_NAME = "Default Key"


class ProviderError(Exception):
    """Provider misconfiguration or transport failure."""


class ProviderKeyError(ProviderError):
    """The API key could not be resolved.

    Never carries the key value — only describes *where* it was
    expected to come from.
    """


class ProviderConfig:
    """Connection description for one OpenAI-compatible provider.

    Args:
        base_url: e.g. ``"https://api.openai.com/v1"`` (``/chat/completions``
            is appended by :func:`post_chat_completions`).
        api_key_env: *name* of the environment variable holding the key.
            Only used when ``key_provider`` is None.
        model: default model name for this provider.
        timeout_s: default HTTP timeout in seconds.
        key_provider: optional special key source; currently only
            ``"9router"`` (local 9router sqlite DB) is supported.
    """

    def __init__(self, base_url="", api_key_env="", model="", timeout_s=180,
                 key_provider=None):
        self.base_url = (base_url or "").strip()
        self.api_key_env = (api_key_env or "").strip()
        self.model = (model or "").strip()
        self.timeout_s = 180 if timeout_s is None else int(timeout_s)
        self.key_provider = (key_provider or "").strip() or None

    @classmethod
    def from_dict(cls, d):
        """Build from a ``provider:`` config block (dict or None)."""
        d = d or {}
        return cls(
            base_url=d.get("base_url") or "",
            api_key_env=d.get("api_key_env") or "",
            model=d.get("model") or "",
            timeout_s=d.get("timeout_s", 180),
            key_provider=d.get("key_provider"),
        )

    def to_dict(self):
        """Serialize back to a ``provider:`` config block (no secrets)."""
        return {
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "model": self.model,
            "timeout_s": self.timeout_s,
            "key_provider": self.key_provider,
        }

    def chat_url(self):
        """Full ``/chat/completions`` URL; raises when unconfigured."""
        if not self.base_url:
            raise ProviderError("provider base_url is not configured.")
        return self.base_url.rstrip("/") + "/chat/completions"

    def __repr__(self):
        # api_key_env is only a variable NAME — safe to show.
        return (
            f"ProviderConfig(base_url={self.base_url!r}, "
            f"api_key_env={self.api_key_env!r}, model={self.model!r}, "
            f"timeout_s={self.timeout_s}, "
            f"key_provider={self.key_provider!r})"
        )


def _read_9router_default_key():
    """Read the ``'Default Key'`` row from the local 9router sqlite DB."""
    try:
        con = sqlite3.connect(NINE_ROUTER_DB)
        try:
            row = con.execute(
                "SELECT key FROM apiKeys WHERE name=? LIMIT 1",
                (NINE_ROUTER_DEFAULT_KEY_NAME,),
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        raise ProviderKeyError(
            f"cannot read 9router DB ({NINE_ROUTER_DB}): {e}")
    if not row or not row[0]:
        raise ProviderKeyError(
            f"row name={NINE_ROUTER_DEFAULT_KEY_NAME!r} missing/empty "
            "in 9router apiKeys table.")
    return row[0]


def resolve_api_key(cfg):
    """Resolve the API key for ``cfg``.

    The key value itself is never printed, logged, or written anywhere.
    Raises :class:`ProviderKeyError` with a *where-to-fix* message.
    """
    cfg = cfg if cfg is not None else ProviderConfig()
    if cfg.key_provider == "9router":
        return _read_9router_default_key()
    if cfg.key_provider:
        raise ProviderKeyError(
            f"unknown key_provider {cfg.key_provider!r} "
            "(supported: '9router', or None for env var).")
    if not cfg.api_key_env:
        raise ProviderKeyError(
            "no key source configured: set key_provider or api_key_env "
            "(the NAME of an env var holding the key).")
    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise ProviderKeyError(
            f"env var {cfg.api_key_env!r} is empty/not set.")
    return key


def _pattern_matches(model_low, pattern):
    pat = (pattern or "").strip().lower()
    if not pat:
        return False
    if pat.endswith("*"):
        return model_low.startswith(pat[:-1])
    return model_low == pat


def check_model_allowed(model, policy):
    """Validate a model name against an allow/forbid policy.

    ``policy`` is ``{"allow": [...], "forbid": [...]}``; missing or empty
    lists mean unrestricted. Entries support a trailing ``*`` prefix
    wildcard. ``forbid`` wins over ``allow``.

    Returns the normalized (stripped) model name; raises ``ValueError``
    on violation (or when the name is empty).
    """
    m = (model or "").strip()
    if not m:
        raise ValueError("model name is empty.")
    policy = policy or {}
    allow = policy.get("allow") or []
    forbid = policy.get("forbid") or []
    low = m.lower()
    for pat in forbid:
        if _pattern_matches(low, pat):
            raise ValueError(
                f"model {m!r} is forbidden by model_policy "
                f"(matched {str(pat).strip()!r}).")
    if allow:
        if not any(_pattern_matches(low, p) for p in allow):
            raise ValueError(
                f"model {m!r} is not in the model_policy allow list.")
    return m


def _post_json_requests(url, headers, payload, timeout):
    import requests  # local import: optional dependency

    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    return r.status_code, r.text


def _post_json_urllib(url, headers, payload, timeout):
    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post_chat_completions(cfg, payload, api_key=None, timeout=None):
    """POST ``payload`` to ``{base_url}/chat/completions`` with Bearer auth.

    Returns ``(status_code, response_text)``. Uses ``requests`` when
    available, stdlib ``urllib`` otherwise. Raises :class:`ProviderError`
    on misconfiguration (e.g. missing ``base_url``); transport errors
    propagate to the caller. The key is passed only as an HTTP header —
    never logged.
    """
    cfg = cfg if cfg is not None else ProviderConfig()
    url = cfg.chat_url()  # raises ProviderError when base_url is missing
    key = api_key if api_key is not None else resolve_api_key(cfg)
    headers = {"Authorization": "Bearer " + key,
               "Content-Type": "application/json"}
    t = cfg.timeout_s if timeout is None else timeout
    try:
        return _post_json_requests(url, headers, payload, t)
    except ImportError:
        return _post_json_urllib(url, headers, payload, t)


__all__ = [
    "ProviderConfig",
    "ProviderError",
    "ProviderKeyError",
    "check_model_allowed",
    "post_chat_completions",
    "resolve_api_key",
]
