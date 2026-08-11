"""Masking credential-shaped text before it is written or returned.

Lives in ``core`` because more than one boundary needs it. The Alpaca provider
redacts its own exception messages, but a provider is not the last place a secret
can escape: an error string travels into a log line, an event payload and an API
response, and each of those is somewhere a key can end up permanently.

This is **defence in depth, not the primary control.** The primary control is not
putting secrets in strings: they are held in ``SecretStr``, never interpolated
into messages, never returned by an endpoint. Redaction catches what a third-party
SDK does with its own error text, which we do not control.

It is pattern-based, so it is necessarily incomplete -- a secret in a shape nobody
anticipated passes through. Never rely on it to make an otherwise-unsafe value
safe to publish.
"""

from __future__ import annotations

import re
from typing import Final

MASK: Final = "***REDACTED***"

# An `Authorization` value is masked to end-of-line: in `Bearer <token>` the secret
# is the *second* word, so masking only the next token leaks it.
_AUTHORIZATION: Final = re.compile(r"(?i)(authorization\s*[:=]\s*)(.*)")

_KEYED: Final = (
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*[\"']?)([A-Za-z0-9_\-]{6,})"),
    re.compile(r"(?i)(secret[_a-z]*\s*[:=]\s*[\"']?)([A-Za-z0-9_\-]{6,})"),
    re.compile(r"(?i)(token\s*[:=]\s*[\"']?)([A-Za-z0-9_\-]{6,})"),
    re.compile(r"(?i)(password\s*[:=]\s*[\"']?)(\S{4,})"),
)

# Alpaca keys look like PK... / AK..., masked wherever they appear even unlabelled.
_KEY_SHAPED: Final = re.compile(r"\b[AP]K[A-Z0-9]{10,}\b")


def redact(text: str) -> str:
    """Mask values that look like credentials."""
    redacted = _AUTHORIZATION.sub(rf"\1{MASK}", text)
    for pattern in _KEYED:
        redacted = pattern.sub(rf"\1{MASK}", redacted)
    return _KEY_SHAPED.sub(MASK, redacted)


def first_line(text: str, limit: int = 300) -> str:
    """The first line, truncated.

    Multi-line provider errors often carry a request dump on the later lines --
    which is where the interesting failure is *not*, and where a credential often
    is.
    """
    return text.strip().splitlines()[0][:limit] if text.strip() else ""


def safe_message(exc: BaseException) -> str:
    """An exception rendered for a log line, an event or an API response."""
    return first_line(redact(str(exc)))
