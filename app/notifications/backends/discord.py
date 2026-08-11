"""Discord webhook delivery.

The only module that knows Discord's protocol -- its payload shape, its 2000
character limit, its rate-limit headers. Everything upstream deals in
:class:`NotificationMessage`.

**A webhook URL is a bearer credential.** Anyone holding one can post to that
channel as tradabot. So the URL is never logged, never returned, never placed in
an exception message, and never written to disk. Errors identify the *channel*,
which is what an operator needs, and nothing about the URL that reaches it.

**Delivery never raises.** Every failure becomes a :class:`DeliveryResult` with
``delivered=False``. This is not defensive habit -- it is the property the whole
phase depends on: a paper trade must stay persisted when Discord is down, and an
exception escaping this module is exactly how it would not.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from typing import Any, Final

import httpx

from app.core.config import DiscordSettings
from app.core.events import EventCategory
from app.core.logging import get_logger
from app.core.redaction import redact, safe_message
from app.notifications.models import DeliveryResult, NotificationMessage

logger = get_logger(__name__)

BACKEND_NAME = "discord"

HTTP_TOO_MANY_REQUESTS: Final = 429
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# Discord's own hard cap. The configured limit sits below it; this is the wall.
DISCORD_CONTENT_LIMIT: Final = 2000


class DiscordWebhookNotifier:
    """Posts messages to per-category Discord webhooks.

    Args:
        settings: webhooks and retry policy.
        client: injected HTTP client, for tests. One is created per send when
            absent, which is fine for a notification's traffic profile and avoids
            owning a connection pool this class has no lifecycle hook to close.
    """

    name = BACKEND_NAME

    def __init__(
        self,
        settings: DiscordSettings,
        *,
        client: httpx.AsyncClient | None = None,
        max_characters: int = 1900,
    ) -> None:
        self._settings = settings
        self._client = client
        self._max_characters = min(max_characters, DISCORD_CONTENT_LIMIT)
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        """Last failure. Never contains a webhook URL."""
        return self._last_error

    def webhook_for(self, category: EventCategory) -> str | None:
        """The configured webhook for a category, or None.

        Returning ``None`` rather than raising lets an operator configure the
        channels they care about and leave the rest empty, which is a reasonable
        thing to want and not an error.
        """
        secret = {
            EventCategory.MARKET: self._settings.market_webhook,
            EventCategory.PAPER_TRADE: self._settings.trades_webhook,
            EventCategory.PERFORMANCE: self._settings.performance_webhook,
            EventCategory.SYSTEM: self._settings.system_webhook,
        }[category]
        url = secret.get_secret_value().strip()
        return url or None

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Deliver one message, retrying what is worth retrying."""
        url = self.webhook_for(message.category)
        if url is None:
            # Not a failure: nothing is configured for this channel.
            return DeliveryResult(
                backend=BACKEND_NAME,
                delivered=False,
                error=f"no webhook configured for category '{message.category.value}'",
                attempts=0,
            )

        payload: dict[str, Any] = {
            "content": message.rendered(self._max_characters),
            "username": self._settings.username,
            # Suppress @everyone/@here even if a formatter or a symbol name ever
            # produces one. A monitoring channel must not be able to ping a room.
            "allowed_mentions": {"parse": []},
        }
        return await self._post(url, payload, message.category)

    async def _post(
        self, url: str, payload: Mapping[str, Any], category: EventCategory
    ) -> DeliveryResult:
        """POST with bounded retries and exponential backoff plus jitter.

        Retries 429 and the transient 5xx family. A 4xx other than 429 means the
        request itself is wrong -- a revoked webhook, a malformed payload -- and
        repeating it just delays the operator learning that.
        """
        attempts = self._settings.max_retries + 1
        delay = self._settings.backoff_base_seconds
        last_status: int | None = None
        last_error: str | None = None
        last_response: httpx.Response | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._request(url, payload)
            except (httpx.HTTPError, TimeoutError) as exc:
                # `safe_message` in case a transport error echoes the URL back.
                last_error = safe_message(exc)
                last_status = None
                last_response = None
            else:
                last_response = response
                last_status = response.status_code
                if response.is_success:
                    self._last_error = None
                    return DeliveryResult(
                        backend=BACKEND_NAME,
                        delivered=True,
                        status_code=last_status,
                        attempts=attempt,
                    )
                last_error = f"HTTP {last_status}"
                if last_status not in _RETRYABLE_STATUS:
                    break

            if attempt == attempts:
                break
            await asyncio.sleep(self._backoff(delay, last_status, last_response))
            delay *= 2

        error = f"{category.value}: {redact(last_error or 'delivery failed')}"
        self._last_error = error
        logger.warning(
            "discord delivery failed",
            category=category.value,
            status=last_status,
            attempts=attempts,
            error=redact(last_error or ""),
        )
        return DeliveryResult(
            backend=BACKEND_NAME,
            delivered=False,
            status_code=last_status,
            error=error,
            attempts=attempts,
        )

    def _backoff(self, delay: float, status: int | None, response: httpx.Response | None) -> float:
        """How long to wait before the next attempt.

        Discord's 429 carries ``Retry-After``, and it is authoritative: it knows
        when it will accept traffic again, and ignoring it is how a client earns
        a longer ban. Otherwise exponential with full jitter, so several
        simultaneous notifications do not retry in lockstep.
        """
        if status == HTTP_TOO_MANY_REQUESTS and response is not None:
            retry_after = _retry_after(response)
            if retry_after is not None:
                return min(retry_after, self._settings.backoff_max_seconds)
        return random.uniform(0, min(delay, self._settings.backoff_max_seconds))

    async def _request(self, url: str, payload: Mapping[str, Any]) -> httpx.Response:
        timeout = self._settings.request_timeout_seconds
        if self._client is not None:
            return await self._client.post(url, json=payload, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, json=payload)


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait, from the header or the JSON body.

    Discord sends ``Retry-After`` as a header and also ``retry_after`` in a JSON
    body; the header is checked first and the body is a fallback for the cases
    where it is absent.
    """
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if raw is None:
        try:
            body = response.json()
        except ValueError:
            return None
        raw = body.get("retry_after") if isinstance(body, dict) else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None
