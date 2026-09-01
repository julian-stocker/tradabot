"""Turning a retrieved SEC document into text that evidence can point into.

Determinism is the requirement
------------------------------
An evidence excerpt is stored as a character range into the normalised text. If
normalisation were not byte-for-byte reproducible, every re-parse would move
the offsets and old evidence would quietly point at the wrong words. So the
transformation is fixed, versioned by
:data:`~app.research_intelligence.documents.EXTRACTION_VERSION`, and does
nothing locale- or time-dependent.

The document is data, never instructions
----------------------------------------
Filing text is untrusted. It is never evaluated, never used to build a
filesystem path, never interpolated into SQL, and never followed: hyperlinks
inside an exhibit are stripped with every other tag, and the only URLs this
package fetches are ones it constructed itself from SEC filing metadata under
the filing's own accession directory. An exhibit containing
``<a href="http://evil.example/x">`` yields the anchor's text and nothing else.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Final

SUPPORTED_CONTENT: Final[tuple[str, ...]] = ("text/html", "text/plain", "text/xml")
"""What this phase can turn into text. PDF is deliberately absent: none of the
measured validation filings served one for a narrative exhibit, and adding a
parser -- or worse, OCR -- for a case that did not arise would be building
against an imagined requirement."""

_SCRIPT = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BLOCK = re.compile(r"(?i)<(br|/tr|/p|/div|/h[1-6]|/li)[^>]*>")
_CELL = re.compile(r"(?i)</t[dh]>")
_TAG = re.compile(r"<[^>]+>")
_TABS = re.compile("[ ]*\\t[ \\t]*")
_SPACES = re.compile("[ ]{2,}")
_NEWLINES = re.compile(r"\n{2,}")


def content_hash(raw: bytes) -> str:
    """SHA-256 of exactly the bytes SEC served."""
    return hashlib.sha256(raw).hexdigest()


def supported(content_type: str | None) -> bool:
    if not content_type:
        return False
    return any(content_type.lower().startswith(c) for c in SUPPORTED_CONTENT)


def to_text(raw: bytes, content_type: str | None) -> str:
    """Normalised text. Deterministic for a given input.

    Block-level tags become newlines and table cells become tabs, so a table's
    row structure survives flattening well enough for a reader to locate an
    excerpt. That structure is *not* trusted for meaning -- see
    :mod:`app.research_intelligence.facts`, which refuses to read column
    semantics out of flattened tables.
    """
    text = raw.decode("utf-8", "ignore")
    if content_type and content_type.lower().startswith("text/plain"):
        return _normalise(text)
    text = _SCRIPT.sub(" ", text)
    text = _BLOCK.sub("\n", text)
    text = _CELL.sub("\t", text)
    text = _TAG.sub(" ", text)
    return _normalise(html.unescape(text))


def _normalise(text: str) -> str:
    # ``&nbsp;`` is pervasive in filing HTML and unescapes to U+00A0. Left in
    # place it would make two visually identical texts hash differently.
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    # Tab runs collapse *before* space runs, and survive them: a tab marks a
    # table-cell boundary, and the fact extractor discards any sentence
    # carrying one. Letting a tab be swallowed as whitespace would feed table
    # fragments to a parser that cannot read tables.
    text = _TABS.sub("\t", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NEWLINES.sub("\n", text).strip()


def excerpt(text: str, start: int, end: int, *, pad: int = 0) -> str:
    """The evidence span, optionally with a little context around it."""
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return text[lo:hi].strip()
