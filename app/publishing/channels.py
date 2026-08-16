"""Which channel a monitored change belongs in.

One rule, applied consistently: **a message goes to the audience whose decisions
it concerns.** A sector move concerns everyone watching the market; a weight
change in PAPER_3K concerns exactly one account.

That is why portfolio events never reach market-signals. Three accounts run the
same strategy at three capital tiers, so "NVDA moved to 18% of equity" is
meaningless without knowing whose equity — and posting it to a shared channel
would produce a true statement that a reader would misattribute.

No fallback between paper slots
-------------------------------
An unconfigured paper channel means **no message for that slot**. It does not
mean "post it to the next one along". :func:`~app.core.webhooks.WebhookRegistry.paper_webhook`
already refuses to fall back; this module never works around that.

Routing depends on the event's scope and kind, both of which come from the
monitoring engine and are persistent properties of the subject. It never depends
on what a message happens to say, because wording changes and routing should not.
"""

from __future__ import annotations

from typing import Final

from app.core.webhooks import WebhookChannel
from app.monitoring.schemas import PORTFOLIO_KINDS, ChangeEvent, EventKind, ScopeKind

MARKET_SIGNALS: Final = WebhookChannel.MARKET
"""#market-signals — material market and company alerts."""

MARKET_TRENDS: Final = WebhookChannel.TRENDS
"""#market-trends — the weekly newsletter, and nothing else."""

SYSTEM: Final = WebhookChannel.SYSTEM
"""#system — transport health, delivery failures, recovery notices. These are
infrastructure facts and must not dilute an alert channel."""

STATUS: Final = WebhookChannel.STATUS
"""#status — the existing dashboard heartbeat, unchanged by this phase."""

PAPER_CHANNELS: Final[dict[str, WebhookChannel]] = {
    "PAPER_1K": WebhookChannel.PAPER_1K,
    "PAPER_3K": WebhookChannel.PAPER_3K,
    "PAPER_10K": WebhookChannel.PAPER_10K,
}

MARKET_SIGNAL_KINDS: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.MARKET_REGIME_CHANGE,
        EventKind.SECTOR_MOVE,
        EventKind.UNUSUAL_VOLUME,
        EventKind.UNUSUAL_VOLATILITY,
        EventKind.RELATIVE_STRENGTH_CHANGE,
        EventKind.NEW_SEC_FILING,
        EventKind.FUNDAMENTAL_CHANGE,
        EventKind.VALUATION_STATE_CHANGE,
        EventKind.COMPANY_CONFIDENCE_CHANGE,
        EventKind.DATA_HEALTH_CHANGE,
    }
)
"""Everything market-signals may carry. Deliberately the complement of
:data:`~app.monitoring.schemas.PORTFOLIO_KINDS`; a test asserts the two do not
overlap, so a new event kind cannot quietly become routable to both."""


def channel_for(event: ChangeEvent) -> WebhookChannel | None:
    """The destination for one event, or ``None`` when it has no home.

    ``None`` is returned rather than a default channel: an event kind nobody has
    assigned a destination to should go nowhere and be visible as unrouted, not
    land in whichever channel happened to be the fallback.
    """
    if event.scope.kind is ScopeKind.PORTFOLIO or event.kind in PORTFOLIO_KINDS:
        account = event.scope.account
        return PAPER_CHANNELS.get(account) if account else None
    if event.kind in MARKET_SIGNAL_KINDS:
        return MARKET_SIGNALS
    return None


def paper_channel(account: str) -> WebhookChannel | None:
    """One account's channel, and only that account's."""
    return PAPER_CHANNELS.get(account.upper())
