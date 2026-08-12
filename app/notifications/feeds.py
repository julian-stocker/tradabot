"""Semantic destinations for opportunity messages.

Today every market message goes to one channel. That works while the only thing
being said is "this qualified", and stops working the moment WATCH and EXIT
messages join them -- a channel carrying "developing setup", "qualified bullish
setup" and "your thesis just broke" is three feeds wearing one name.

This fixes the *vocabulary* now so the split is configuration later. Each
opportunity carries a :class:`FeedKey`; the backend resolves it to a webhook if
one is configured and **falls back to the existing market channel if not**. No
new webhook is required, `.env` is untouched, and current behaviour is
unchanged.

The distinction that matters
----------------------------
``SELL_EXIT`` is deliberately not called "sell". Three different things get
conflated under that word and only one of them is currently supported:

* **exit a long thesis** -- the setup that justified the position broke;
* **take profit** -- a target or rule closed it;
* **open a short** -- an independent bearish opportunity.

The first two are instructions about a position you hold. The third is a new
position in the opposite direction. Production is long-only, so tradabot emits
the first two and **must never imply the third**.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from app.scanner.enums import SignalLifecycle


class FeedKey(StrEnum):
    """Where an opportunity message belongs, semantically."""

    WATCH = "watch-opportunities"
    """A developing setup below the qualification threshold. Not a trade signal."""

    BUY = "buy-opportunities"
    """A qualified or strong bullish setup. **A label, not an instruction.**"""

    SELL_EXIT = "sell-exit-signals"
    """A long thesis invalidated, or a take-profit-style exit. Never a short."""


TRENDS_ROUTING_KEY: Final = "market-trends"
STATUS_ROUTING_KEY: Final = "status"
"""Dedicated destinations that must **never** fall back to another channel.

Deliberately not :class:`FeedKey` members. A feed key falls back to the market
channel when unconfigured, which is right for opportunity messages -- they belong
in a signals channel either way. It is wrong for both of these:

* descriptive trend text in #market-signals would read as a recommendation,
  which is the one thing :mod:`app.notifications.trends` exists to prevent;
* a status dashboard posted into a signals channel would be edited in place
  fifteen minutes later, silently rewriting a message someone was reading.

So an unconfigured webhook here means **silence**, and silence is the safe
outcome. :meth:`DiscordWebhookNotifier.webhook_for` implements this by returning
``None`` for any routing key that is not a feed key.
"""


WATCH_STATUS: Final = "NOT_IMPLEMENTED"
"""WATCH has no defensible policy yet, and inventing one would be worse than none.

The obvious definition -- "score just below 75" -- is precisely what the phase-5.6
research argues against: the 70-75 band is the *worst* in the dataset (49.0%
positive at 1d, 47.9% at 5d, both under the 51.6% baseline). Promoting exactly
that band to its own channel would be promoting the weakest evidence available.

The other candidates -- improving multi-timeframe agreement, a developing
breakout, a volume setup -- were all measured in phase 5.6 and none of them
discriminates: every component sits within about +/-1.5pp of the base rate, and
volume confirmation and breakout confirmation are the two that add *least*.

So the routing is ready and the policy is not. :func:`classify` returns ``None``
for anything below the threshold rather than manufacturing a WATCH, and the
threshold itself is untouched.
"""


def classify(lifecycle: SignalLifecycle, direction: int) -> FeedKey | None:
    """The feed an opportunity belongs to, or ``None`` for no message.

    Deliberately total and deliberately small: every lifecycle state maps to one
    outcome, so a new state cannot silently fall into the wrong feed.
    """
    if lifecycle in _BUY_STATES and direction > 0:
        return FeedKey.BUY
    if lifecycle in _EXIT_STATES:
        return FeedKey.SELL_EXIT
    return None


_BUY_STATES: Final[frozenset[SignalLifecycle]] = frozenset(
    {SignalLifecycle.QUALIFIED, SignalLifecycle.STRONG}
)

_EXIT_STATES: Final[frozenset[SignalLifecycle]] = frozenset(
    {SignalLifecycle.WEAKENED, SignalLifecycle.INVALIDATED}
)
"""Exit signals are about a thesis that has broken, not a new bearish position.

``EXPIRED`` is absent on purpose: a setup ageing out is bookkeeping, not news.
Announcing it would put a message in a channel for something that, by definition,
stopped being interesting.
"""


HEADLINES: Final[dict[FeedKey, str]] = {
    FeedKey.WATCH: "WATCH — developing setup",
    FeedKey.BUY: "BULLISH OPPORTUNITY",
    FeedKey.SELL_EXIT: "EXIT SIGNAL — long thesis weakened",
}
"""Human-facing wording.

"BULLISH OPPORTUNITY", never "BUY". The distinction is not pedantry: tradabot
places no orders, has no view of the reader's portfolio, and its own research
puts the edge at a few percentage points on 57 episodes. Language implying
certainty would be claiming something the numbers do not support.
"""
