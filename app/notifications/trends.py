"""Market activity worth looking at. **Not recommendations.**

The distinction is the entire point of this module. Phase 5.8 classified the
score as PROMISING_BUT_INSUFFICIENT, so tradabot may not tell anyone to buy
anything. It can still say "NVDA moved 4% on twice its usual volume", which is a
statement about what happened, not about what to do.

So no message from here ever contains BUY, SELL, WATCH, ENTRY or EXIT, and none
is gated on the 75/85 thresholds -- a 4% move on heavy volume is notable whether
or not the scanner liked the setup. :func:`assert_no_recommendation_language`
enforces that at the boundary rather than trusting every future formatter.

Volume, not opinion
-------------------
Fifty-two symbols scanned four times an hour is 4,800 observations a day. The
noise policy is therefore stricter than the detection policy: an event fires once
when it appears, stays silent while it persists, and only speaks again when it
materially changes. Healthy silence is the normal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from app.scanner.enums import SessionPhase

FORBIDDEN_WORDS: Final[tuple[str, ...]] = (
    "buy",
    "sell",
    "entry",
    "exit",
    "watch",
    "target",
    "recommend",
)
"""Words a market-observation message must never contain.

Checked, not merely documented. A trends channel that drifts into advice is
worse than no channel: it carries the authority of automation without the
evidence to support it.
"""

DISCLAIMER: Final = "Market observation only — not a trade recommendation."


class TrendEvent(StrEnum):
    """Why a symbol is worth a glance."""

    STRONG_MOVE_UP = "STRONG_MOVE_UP"
    STRONG_MOVE_DOWN = "STRONG_MOVE_DOWN"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    TREND_FLIP = "TREND_FLIP"

    @property
    def is_directional(self) -> bool:
        return self in {
            TrendEvent.STRONG_MOVE_UP,
            TrendEvent.STRONG_MOVE_DOWN,
            TrendEvent.BREAKOUT,
            TrendEvent.BREAKDOWN,
        }


# Thresholds are engineering assumptions about what a human would look up from
# their desk for -- deliberately not derived from outcome labels, because these
# select what is *interesting*, not what is profitable.
MOVE_PCT: Final = 3.0
"""A daily move this large is unusual for a large-cap."""

VOLUME_MULTIPLE: Final = 2.0
"""Twice the 20-period average. Below this, 'heavy volume' is ordinary variation."""

VOLATILITY_PCT: Final = 45.0
"""Annualised. The phase-5.6 attribution put 35-60% in the top two buckets."""

COOLDOWN: Final = timedelta(hours=4)
"""Minimum gap before the same symbol+event may fire again.

Long on purpose. A stock up 4% at 15:00 is still up 4% at 16:00, and that is not
new information.
"""

MATERIAL_CHANGE_PCT: Final = 2.0
"""How much further a move must extend to be worth repeating inside a cooldown."""


@dataclass(frozen=True, slots=True)
class TrendState:
    """What was last announced for one symbol+event."""

    key: str
    last_notified_at: datetime | None = None
    last_value: float | None = None


@dataclass(frozen=True, slots=True)
class TrendSignal:
    """One notable observation, ready to render."""

    symbol: str
    event: TrendEvent
    value: float
    headline: str
    detail: str = ""

    @property
    def key(self) -> str:
        return f"trend:{self.symbol}:{self.event.value}"


def detect(
    *,
    symbol: str,
    change_1d_pct: float | None = None,
    change_5d_pct: float | None = None,
    relative_volume: float | None = None,
    volatility: float | None = None,
    structure_state: str | None = None,
    trend_flip: bool = False,
) -> list[TrendSignal]:
    """Every notable thing about one symbol right now.

    Purely descriptive: each branch reports something that already happened.
    Nothing here forecasts, and nothing consults the signal score -- a heavy
    -volume 4% move is worth seeing whether or not the setup qualified.
    """
    found: list[TrendSignal] = []

    if change_1d_pct is not None and abs(change_1d_pct) >= MOVE_PCT:
        up = change_1d_pct > 0
        found.append(
            TrendSignal(
                symbol=symbol,
                event=TrendEvent.STRONG_MOVE_UP if up else TrendEvent.STRONG_MOVE_DOWN,
                value=change_1d_pct,
                headline=f"{change_1d_pct:+.1f}% today",
                detail=f"5d {change_5d_pct:+.1f}%" if change_5d_pct is not None else "",
            )
        )

    if relative_volume is not None and relative_volume >= VOLUME_MULTIPLE:
        found.append(
            TrendSignal(
                symbol=symbol,
                event=TrendEvent.VOLUME_SPIKE,
                value=relative_volume,
                headline=f"volume {relative_volume:.1f}x average",
            )
        )

    if volatility is not None and volatility * 100 >= VOLATILITY_PCT:
        found.append(
            TrendSignal(
                symbol=symbol,
                event=TrendEvent.VOLATILITY_EXPANSION,
                value=volatility * 100,
                headline=f"volatility {volatility * 100:.0f}% annualised",
            )
        )

    if structure_state == "BREAKOUT":
        found.append(TrendSignal(symbol, TrendEvent.BREAKOUT, 0.0, "broke above its recent range"))
    elif structure_state == "BREAKDOWN":
        found.append(TrendSignal(symbol, TrendEvent.BREAKDOWN, 0.0, "broke below its recent range"))

    if trend_flip:
        found.append(TrendSignal(symbol, TrendEvent.TREND_FLIP, 0.0, "trend direction changed"))

    return found


def should_notify(signal: TrendSignal, state: TrendState | None, *, now: datetime) -> bool:
    """Whether this observation is *new*.

    Three rules, and the second is the one that keeps the channel readable:

    1. never seen -> notify;
    2. inside the cooldown -> silent, **unless** the move extended materially;
    3. past the cooldown -> notify again.

    A persisting condition is not news. The same 4% move re-announced every scan
    is exactly the failure the market-signals channel already had.
    """
    if state is None or state.last_notified_at is None:
        return True

    elapsed = now - state.last_notified_at
    if elapsed >= COOLDOWN:
        return True

    if state.last_value is None:
        return False
    return abs(signal.value) - abs(state.last_value) >= MATERIAL_CHANGE_PCT


def session_allows_trends(
    session: SessionPhase, *, conservative_extended: bool = True
) -> tuple[bool, str]:
    """Whether the market session permits trend messages.

    Closed markets produce nothing new, so periodic messages there are pure
    noise -- the same conclusion the market-signals fix reached.

    Extended hours are suppressed by default for a data reason rather than a
    caution reason: IEX prints thinly outside the session, and a "volume spike"
    computed from a handful of trades is an artefact of the feed. Phase 4
    measured 883-1118 bps spreads on mega-caps after the close.
    """
    if session in (SessionPhase.CLOSED, SessionPhase.WEEKEND, SessionPhase.HOLIDAY):
        return False, f"market {session.value.lower()}"
    if session is not SessionPhase.REGULAR and conservative_extended:
        return False, f"{session.value.lower()} -- IEX prints too thin to read"
    return True, "regular session"


def rank(signals: list[TrendSignal], *, limit: int = 5) -> list[TrendSignal]:
    """The few most notable, strongest first.

    A ranked list of five is a glance. Fifty-two is a spreadsheet, and nobody
    reads a spreadsheet on a phone.
    """
    return sorted(signals, key=lambda s: abs(s.value), reverse=True)[:limit]


def assert_no_recommendation_language(text: str) -> None:
    """Guard the boundary between observation and advice.

    Enforced rather than trusted: the research says the score is not validated
    for recommendations, so a formatter that quietly introduces "watch" would be
    making a claim the evidence does not support.
    """
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        if word in lowered:
            msg = f"trend message contains recommendation language: {word!r}"
            raise ValueError(msg)


def build_payload(signals: list[TrendSignal], *, context: dict[str, Any]) -> dict[str, Any]:
    """The event payload for a ranked trend summary."""
    return {
        "title": "📈 MARKET ACTIVITY",
        "movers": [
            {
                "symbol": signal.symbol,
                "event": signal.event.value,
                "headline": signal.headline,
                "detail": signal.detail,
            }
            for signal in signals
        ],
        "disclaimer": DISCLAIMER,
        **context,
    }
