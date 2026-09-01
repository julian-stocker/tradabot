"""A small, polite, read-only HTTP client for SEC EDGAR.

Scope
-----
Three endpoints, all public, all free, none authenticated:

``company_tickers``
    ticker -> CIK, the only way to turn a symbol into an EDGAR identity.
``companyfacts``
    every XBRL fact a company has ever filed, with the accession that published
    each one. This is what makes point-in-time queries possible at all.
``submissions``
    filing metadata, and the only source of ``acceptanceDateTime`` -- the moment
    a document actually became public, which companyfacts does not carry.

No credentials
--------------
EDGAR requires no key. It requires a descriptive ``User-Agent`` with a contact
address, which is a courtesy identifier and not a secret; there is nothing here
to leak and nothing is read from the credential settings.

Rate limiting
-------------
SEC asks for no more than 10 requests per second. This client enforces the gap
itself rather than trusting call sites to sleep, because a caller that forgets
gets the whole project blocked, not just its own run.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Final

from app.core.logging import get_logger

logger = get_logger(__name__)

TICKERS_URL: Final = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL: Final = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL: Final = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_FILE_URL: Final = "https://data.sec.gov/submissions/{name}"

DEFAULT_USER_AGENT: Final = "tradabot research contact-via-repository"
"""Overridden with ``TRADABOT_SEC_USER_AGENT``. SEC asks that this identify the
requester; it is not a credential, and no secret is ever placed here."""

_MIN_INTERVAL: Final = 0.11
"""Seconds between requests. Slightly above SEC's 10/s ceiling, on purpose."""

_RETRY_STATUS: Final = frozenset({429, 500, 502, 503, 504})
_NOT_FOUND: Final = 404


class EdgarUnavailableError(RuntimeError):
    """EDGAR could not be reached, or refused. Carries no response body."""


class EdgarClient:
    """Rate-limited reader for the three public EDGAR endpoints.

    Args:
        user_agent: contact string sent to SEC. Defaults to the environment
            override, then to :data:`DEFAULT_USER_AGENT`.
        timeout: per-request timeout in seconds.
        retries: attempts for a transient status before giving up.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout: float = 60.0,
        retries: int = 3,
    ) -> None:
        self._agent = user_agent or os.environ.get("TRADABOT_SEC_USER_AGENT", DEFAULT_USER_AGENT)
        self._timeout = timeout
        self._retries = max(1, retries)
        self._last = 0.0

    # ------------------------------------------------------------------ http
    def _get(self, url: str) -> dict[str, Any]:
        last_error = "unknown"
        for attempt in range(self._retries):
            gap = _MIN_INTERVAL - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self._agent,
                    "Accept-Encoding": "gzip, deflate",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    parsed: dict[str, Any] = json.loads(raw)
                    return parsed
            except urllib.error.HTTPError as exc:
                if exc.code == _NOT_FOUND:
                    # A company with no XBRL filings is an ordinary outcome, not
                    # a fault, and must not cost three retries each time.
                    msg = "not found"
                    raise EdgarUnavailableError(msg) from None
                last_error = f"HTTP {exc.code}"
                if exc.code not in _RETRY_STATUS:
                    break
            except Exception as exc:
                last_error = type(exc).__name__
            time.sleep(_MIN_INTERVAL * (2**attempt))
        raise EdgarUnavailableError(last_error)

    # ------------------------------------------------------------ endpoints
    def company_tickers(self) -> dict[str, int]:
        """Ticker -> CIK for every EDGAR filer.

        Symbols are upper-cased and dots normalised to dashes, because EDGAR
        writes ``BRK-B`` where market data writes ``BRK.B`` and a lookup that
        misses on punctuation looks exactly like a company with no filings.
        """
        payload = self._get(TICKERS_URL)
        out: dict[str, int] = {}
        for entry in payload.values():
            ticker = str(entry.get("ticker", "")).upper().replace(".", "-")
            if ticker:
                out.setdefault(ticker, int(entry["cik_str"]))
        return out

    def companyfacts(self, cik: int) -> dict[str, Any]:
        """Every XBRL fact for one filer."""
        return self._get(COMPANYFACTS_URL.format(cik=cik))

    def profile(self, cik: int) -> dict[str, str]:
        """Entity name and SIC classification for one filer.

        The SIC code is the SEC's own classification of what the company does.
        It is the only sector signal Tradabot has that covers every filer it
        ingests, it costs nothing extra, and it is the difference between
        refusing to read a bank's balance sheet and reporting that Wells Fargo
        carries an acceptable amount of debt.
        """
        payload = self._get(SUBMISSIONS_URL.format(cik=cik))
        return {
            "name": str(payload.get("name") or ""),
            "sic": str(payload.get("sic") or ""),
            "sic_description": str(payload.get("sicDescription") or ""),
            "country": str(
                (payload.get("addresses") or {}).get("business", {}).get("stateOrCountry") or ""
            ),
        }

    def acceptance_times(self, cik: int) -> dict[str, str]:
        """Accession -> acceptance timestamp for one filer.

        The submissions endpoint holds the most recent 1,000 filings inline and
        spills the rest into extra files. Both are read: a company that files
        often would otherwise lose acceptance times for exactly the older
        history the Advisor uses for its valuation percentiles.
        """
        payload = self._get(SUBMISSIONS_URL.format(cik=cik))
        filings = payload.get("filings", {})
        out: dict[str, str] = {}
        _collect_acceptance(filings.get("recent", {}), out)
        for extra in filings.get("files", []) or []:
            name = str(extra.get("name", ""))
            if not name:
                continue
            try:
                more = self._get(SUBMISSIONS_FILE_URL.format(name=name))
            except EdgarUnavailableError as exc:
                # Missing older acceptance times degrade provenance detail; they
                # never invalidate a fact, whose filing date is already known.
                logger.warning("submissions overflow unavailable", file=name, reason=str(exc))
                continue
            _collect_acceptance(more, out)
        return out


def _collect_acceptance(block: dict[str, Any], into: dict[str, str]) -> None:
    accessions = block.get("accessionNumber") or []
    accepted = block.get("acceptanceDateTime") or []
    for accession, when in zip(accessions, accepted, strict=False):
        if accession and when:
            into[str(accession)] = str(when)
