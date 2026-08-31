"""Canonicalize a raw NER person span into a comparable name (or reject it)."""

from __future__ import annotations

import re

_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "sir",
    "ceo",
    "cfo",
    "coo",
    "cto",
    "chairman",
    "president",
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z.'-]*")
_MIN_LEN = 4
_MAX_LEN = 40
_MIN_TOKENS = 2


def canonical(raw: str) -> str | None:
    """Return a canonical ``First Last`` form, or ``None`` if the span is unusable."""
    tokens = _TOKEN_RE.findall(raw or "")
    tokens = [t for t in tokens if t.lower().strip(".") not in _HONORIFICS]
    tokens = [t for t in tokens if len(t.strip(".'-")) > 1 or t.endswith(".")]
    if len(tokens) < _MIN_TOKENS:
        return None
    name = " ".join(t.capitalize() if t.isupper() or t.islower() else t for t in tokens)
    name = re.sub(r"\s+", " ", name).strip()
    if not (_MIN_LEN <= len(name) <= _MAX_LEN):
        return None
    return name
