"""Application configuration.

Coding rule 14: *financial assumptions must be configurable rather than hidden
constants*. Every number that encodes a market or broker assumption -- fee levels,
slippage, signal weights, classification thresholds -- is declared here and is
overridable through the environment.

Nested settings use the ``__`` delimiter, e.g.::

    TRADABOT_COSTS__ORDER_FEE=0.99
    TRADABOT_SIGNALS__WEIGHTS__MOMENTUM=0.30
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError

_PORTFOLIO_WEBHOOK_KEY = re.compile(r"paper_(?P<suffix>[a-z0-9_]+)_webhook")

WEIGHT_SUM_TOLERANCE = 1e-6


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class CostSettings(BaseModel):
    """Broker cost assumptions.

    Baseline placeholders modelled loosely on a European retail neobroker
    (flat per-order fee, no percentage commission). They are **not** calibrated
    against any real broker and must be re-measured against fills in phase 5.

    tradabot never integrates unofficial broker APIs; these are configuration
    values you enter yourself.
    """

    order_fee: Decimal = Field(
        default=Decimal("1.00"),
        ge=0,
        description="Fixed fee charged per order, in account currency.",
    )
    variable_fee_rate: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
        description="Commission as a fraction of notional (0.0005 == 5 bps).",
    )
    slippage_spread_multiple: Decimal = Field(
        default=Decimal("0.5"),
        ge=0,
        description=(
            "Assumed adverse slippage per side, expressed as a multiple of the "
            "half-spread. 0.0 = fills exactly at the quoted touch; 1.0 = one full "
            "extra half-spread of adverse selection."
        ),
    )
    default_spread_bps: float = Field(
        default=10.0,
        ge=0,
        description="Fallback spread in basis points when no quote is available.",
    )
    min_order_notional: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Smallest order the broker accepts; 0 disables the check.",
    )


class AlpacaSettings(BaseModel):
    """Alpaca market-data credentials and feed selection.

    Credentials are :class:`~pydantic.SecretStr`, so they render as ``**********``
    in reprs, logs, tracebacks and ``model_dump()`` output. Getting at the real
    value requires an explicit ``.get_secret_value()`` call, which is greppable --
    an accidental leak becomes a visible line of code rather than a silent one.

    tradabot uses Alpaca for **market data only**. No trading endpoint is
    configured, and no order-placing credential is ever requested.
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr = Field(default=SecretStr(""), description="ALPACA_API_KEY.")
    api_secret: SecretStr = Field(default=SecretStr(""), description="ALPACA_API_SECRET.")

    feed: Literal["iex", "sip", "delayed_sip"] = Field(
        default="iex",
        description=(
            "Data feed. 'iex' is free but covers only IEX volume -- roughly 2-3% "
            "of consolidated tape, so its bars and spreads are NOT the whole "
            "market. 'sip' is the consolidated tape and requires a paid "
            "subscription. Which one produced a bar matters, so it is recorded."
        ),
    )
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(
        default=4,
        ge=0,
        le=10,
        description="Bounded. An unbounded retry loop turns an outage into a hang.",
    )
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    max_bars_per_request: int = Field(default=10_000, ge=1, le=10_000)

    @model_validator(mode="before")
    @classmethod
    def _accept_alpaca_naming(cls, values: Any) -> Any:
        """Accept ``SECRET_KEY`` as well as ``API_SECRET``.

        Alpaca's own dashboard and documentation call the pair "API Key ID" and
        "Secret Key", so ``TRADABOT_ALPACA__SECRET_KEY`` is what someone
        following their instructions writes. Rejecting it produced the worst
        possible symptom: the key was read, the secret silently was not, and
        `is_configured` reported False with both variables visibly present in
        `.env`.

        ``api_secret`` stays canonical -- this only widens what is accepted.
        """
        if isinstance(values, dict) and "secret_key" in values:
            alias = values.pop("secret_key")
            values.setdefault("api_secret", alias)
        return values

    @property
    def is_configured(self) -> bool:
        """Whether both credentials are present.

        Checked before constructing a client so a missing key produces a clear
        configuration error rather than an opaque 401 from the provider.
        """
        return bool(self.api_key.get_secret_value()) and bool(self.api_secret.get_secret_value())


class MarketDataSettings(BaseModel):
    """Provider-independent market-data behaviour."""

    model_config = ConfigDict(frozen=True)

    watchlist: tuple[str, ...] = Field(
        default=("AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA"),
        description=(
            "Symbols to synchronise. **Development examples, not investment "
            "recommendations.** Configuration, not strategy: nothing in the engine "
            "reads this list to decide anything."
        ),
    )
    default_exchange: str = Field(
        default="XNAS", description="Exchange MIC assumed when a provider omits one."
    )
    max_quote_age_seconds: int = Field(
        default=900,
        ge=0,
        description="Above this a quote is reported stale by the health endpoint.",
    )
    treat_provider_quote_as_executable: bool = Field(
        default=True,
        description=(
            "Whether a provider quote may stand in for a broker's executable "
            "quote in the paper broker. Market data from a consolidated tape is "
            "NOT the price a retail broker would fill you at -- see "
            "docs/market-data.md. True is the best available approximation, and "
            "it is a stated assumption rather than a hidden equivalence."
        ),
    )

    @field_validator("watchlist", mode="before")
    @classmethod
    def _parse_watchlist(cls, value: object) -> object:
        """Accept a comma-separated string from the environment."""
        if isinstance(value, str):
            return tuple(s.strip().upper() for s in value.split(",") if s.strip())
        return value


class ScannerSettings(BaseModel):
    """Continuous scanner behaviour.

    Intervals are **declared, not enforced**: nothing here sleeps or loops. They
    document the cadence an external scheduler should invoke the CLI at, and are
    read by `scanner status` so the configured intent and the actual behaviour
    can be compared. Putting a scheduler inside the domain would make the cadence
    untestable and the process unkillable mid-cycle.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=True, description="Master switch for scan cycles.")

    scan_interval_minutes: int = Field(
        default=15, ge=1, description="Intended gap between full signal scans."
    )
    market_sync_interval_minutes: int = Field(
        default=5, ge=1, description="Intended gap between incremental data syncs."
    )
    overview_interval_minutes: int = Field(
        default=60, ge=1, description="Intended gap between market overviews."
    )

    top_candidates: int = Field(
        default=5, ge=1, le=50, description="Candidates in an overview or ranking."
    )

    max_data_age_minutes: int = Field(
        default=30,
        ge=1,
        description=(
            "Newest bar older than this makes an evaluation non-actionable. It is "
            "still persisted -- with its data-quality state -- because a stale "
            "observation is a real observation about the feed."
        ),
    )

    signal_expiry_hours: int = Field(
        default=48,
        ge=1,
        description=(
            "An active signal not seen for this long EXPIRES. Deliberately "
            "distinct from invalidation: 'we stopped looking' is not 'it stopped "
            "being true', and conflating them would poison future labels."
        ),
    )

    lease_seconds: int = Field(
        default=900,
        ge=30,
        description=(
            "How long a scan lease is held. Long enough for a slow cycle, short "
            "enough that a killed process does not lock the scanner for an hour."
        ),
    )

    require_regular_session: bool = Field(
        default=True,
        description=(
            "Only qualify NEW signals during the regular session. The free IEX "
            "feed's extended-hours spreads and volume read very differently, and "
            "a scanner qualifying on them would be measuring the feed. "
            "Evaluations are still recorded outside the session."
        ),
    )

    max_symbols_per_cycle: int = Field(
        default=100,
        ge=1,
        le=500,
        description=(
            "Hard ceiling on one cycle. A guard rail, not a target -- the "
            "multiple-comparisons hazard grows with universe size "
            "(see app/scanner/models.py)."
        ),
    )


class DiscordSettings(BaseModel):
    """Discord webhook delivery.

    Four webhooks, one per channel, because Discord scopes a webhook to a
    channel. They are **secrets**: a webhook URL is a bearer credential -- anyone
    holding one can post to that channel -- so they are `SecretStr`, never
    logged, never returned by an endpoint, and never written to `.env.example`.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(
        default=False,
        description=(
            "Off by default. tradabot must run, test and demo with no Discord "
            "server anywhere; delivery is monitoring, not a dependency."
        ),
    )

    market_webhook: SecretStr = Field(default=SecretStr(""), description="#market-signals.")
    performance_webhook: SecretStr = Field(default=SecretStr(""), description="#performance.")
    system_webhook: SecretStr = Field(default=SecretStr(""), description="#tradabot-system.")

    trades_webhook: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Legacy single paper-trade channel. Used only as a fallback when a "
            "portfolio has no destination of its own, so an existing setup keeps "
            "working while portfolio channels are configured."
        ),
    )

    portfolio_webhooks: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Routing key -> webhook, e.g. {'paper-100': ...}. Populated from any "
            "TRADABOT_DISCORD__PAPER_<N>_WEBHOOK variable by a generic rule, so "
            "adding a portfolio is one environment variable and no code change."
        ),
    )

    request_timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Bounded. A notification is worth retrying, never worth hanging for.",
    )
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=15.0, gt=0)

    username: str = Field(default="tradabot", description="Display name on posted messages.")

    @model_validator(mode="before")
    @classmethod
    def _collect_portfolio_webhooks(cls, values: Any) -> Any:
        """Fold ``PAPER_<N>_WEBHOOK`` variables into :attr:`portfolio_webhooks`.

        Generic on purpose. The rule is "any key matching ``paper_<something>_
        webhook`` becomes routing key ``paper-<something>``", so a fourth
        portfolio is a new environment variable and nothing else. Naming the
        three current portfolios as explicit fields would mean editing this class
        -- and then the routing code, and then the tests -- every time one is
        added, which is exactly the coupling Part B forbids.
        """
        if not isinstance(values, dict):
            return values

        collected: dict[str, Any] = dict(values.get("portfolio_webhooks") or {})
        for key in list(values):
            match = _PORTFOLIO_WEBHOOK_KEY.fullmatch(str(key).lower())
            if match is None:
                continue
            routing_key = f"paper-{match.group('suffix').replace('_', '-')}"
            collected.setdefault(routing_key, values.pop(key))

        if collected:
            values["portfolio_webhooks"] = collected
        return values

    def webhook_for_portfolio(self, routing_key: str) -> SecretStr | None:
        """The destination for one portfolio, or the legacy channel, or None.

        Falls back to ``trades_webhook`` so an installation that has not yet
        configured per-portfolio channels keeps delivering rather than silently
        going quiet.
        """
        secret = self.portfolio_webhooks.get(routing_key)
        if secret is not None and secret.get_secret_value().strip():
            return secret
        legacy = self.trades_webhook.get_secret_value().strip()
        return self.trades_webhook if legacy else None

    @property
    def configured_portfolios(self) -> frozenset[str]:
        """Routing keys with a destination. Names only, never values."""
        return frozenset(
            key
            for key, secret in self.portfolio_webhooks.items()
            if secret.get_secret_value().strip()
        )

    @property
    def configured_categories(self) -> frozenset[str]:
        """Categories with a webhook set. Never reveals the values."""
        named = frozenset(
            name
            for name, secret in (
                ("market", self.market_webhook),
                ("paper_trade", self.trades_webhook),
                ("performance", self.performance_webhook),
                ("system", self.system_webhook),
            )
            if secret.get_secret_value().strip()
        )
        return named | self.configured_portfolios

    @property
    def is_configured(self) -> bool:
        """Whether at least one channel can be delivered to."""
        return bool(self.configured_categories)

    def missing_portfolio_destinations(self, routing_keys: Sequence[str]) -> list[str]:
        """Which of ``routing_keys`` have nowhere to go.

        Used by the operations check. Reports **names**, never values -- an
        operator needs to know which channel is unconfigured, not what the
        configured ones point at.
        """
        return [key for key in routing_keys if self.webhook_for_portfolio(key) is None]


class NotificationSettings(BaseModel):
    """When an event is worth telling a human about.

    **These thresholds control notification volume, nothing else.** A signal that
    fails to clear them is still computed, still scored and still persisted --
    see docs/notifications.md. Filtering what reaches Discord must never filter
    what reaches the database, because the database is the future ML dataset.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=True, description="Master switch for all backends.")
    console: bool = Field(
        default=False,
        description="Echo notifications to the log. Useful locally; independent of Discord.",
    )

    signal_threshold: float = Field(
        default=75.0,
        ge=0.0,
        le=100.0,
        description=(
            "Score at or above which a signal is worth announcing. An engineering "
            "default chosen to keep volume low -- NOT a claim that 75 is "
            "meaningful. Nothing in the scoring model treats it as special."
        ),
    )
    strong_signal_threshold: float = Field(
        default=85.0, ge=0.0, le=100.0, description="Above this, an upgrade is announced."
    )
    signal_cooldown_minutes: int = Field(
        default=60,
        ge=0,
        description="Minimum gap between notifications about the same subject.",
    )
    minimum_score_change: float = Field(
        default=5.0,
        ge=0.0,
        description=(
            "A re-notification needs at least this much movement. Without it, a "
            "score oscillating around a threshold notifies on every scan."
        ),
    )

    overview_size: int = Field(
        default=5, ge=1, le=25, description="Opportunities in a market overview."
    )
    max_message_characters: int = Field(
        default=1900,
        ge=200,
        le=2000,
        description=(
            "Discord's hard limit is 2000. The margin absorbs the wrapper so a "
            "long list of reasons truncates rather than failing delivery."
        ),
    )

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> NotificationSettings:
        if self.strong_signal_threshold < self.signal_threshold:
            msg = (
                f"strong_signal_threshold ({self.strong_signal_threshold}) must be at "
                f"least signal_threshold ({self.signal_threshold}); otherwise a signal "
                f"would be 'strong' before it qualified at all"
            )
            raise ValueError(msg)
        return self


class SignalWeights(BaseModel):
    """Weights of the baseline scoring components.

    **These are heuristics, not optimised parameters.** They were chosen to be
    plausible and legible, and carry no statistical backing whatsoever. Phase 4
    (backtesting) exists precisely to replace them with validated values.
    """

    momentum: float = Field(default=0.25, ge=0, le=1)
    volume: float = Field(default=0.25, ge=0, le=1)
    trend: float = Field(default=0.20, ge=0, le=1)
    volatility: float = Field(default=0.10, ge=0, le=1)
    regime: float = Field(default=0.10, ge=0, le=1)
    spread: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> SignalWeights:
        total = sum(self.as_mapping().values())
        if not math.isclose(total, 1.0, abs_tol=WEIGHT_SUM_TOLERANCE):
            msg = f"signal component weights must sum to 1.0, got {total:.6f}"
            raise ValueError(msg)
        return self

    def as_mapping(self) -> dict[str, float]:
        """Component name -> weight, in declaration order."""
        return {
            "momentum": self.momentum,
            "volume": self.volume,
            "trend": self.trend,
            "volatility": self.volatility,
            "regime": self.regime,
            "spread": self.spread,
        }


class SignalSettings(BaseModel):
    """Signal engine configuration."""

    weights: SignalWeights = Field(default_factory=SignalWeights)
    bullish_threshold: float = Field(
        default=20.0,
        ge=0,
        le=100,
        description="Absolute score at which NEUTRAL becomes BULLISH/BEARISH.",
    )
    strong_bullish_threshold: float = Field(
        default=55.0,
        ge=0,
        le=100,
        description="Absolute score at which a signal becomes STRONG_*.",
    )
    min_bars: int = Field(
        default=60,
        ge=2,
        description="Minimum candle history required before a signal is produced.",
    )
    expected_move_capture_ratio: float = Field(
        default=0.25,
        gt=0,
        le=1,
        description=(
            "Fraction of the typical horizon RANGE that a full-conviction signal is "
            "assumed to capture as directional drift. ATR-based range scaling measures "
            "how far price wanders, not how far it trends, so claiming the whole range "
            "as expected edge would overstate it several-fold. 0.25 is a deliberately "
            "conservative placeholder with no empirical basis -- it is the single most "
            "important number to calibrate in phase 7, because the entire net-edge "
            "filter is proportional to it."
        ),
    )

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> SignalSettings:
        if self.strong_bullish_threshold <= self.bullish_threshold:
            msg = (
                f"strong_bullish_threshold ({self.strong_bullish_threshold}) must be "
                f"greater than bullish_threshold ({self.bullish_threshold})"
            )
            raise ValueError(msg)
        return self


class Settings(BaseSettings):
    """Root application settings, loaded from environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="TRADABOT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Application ------------------------------------------------------
    env: Environment = Environment.DEVELOPMENT
    debug: bool = False
    app_name: str = "tradabot"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_prefix: str = "/api/v1"

    # --- Logging ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # --- Database ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://tradabot:tradabot@localhost:5432/tradabot"
    db_echo: bool = False
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    # --- Market data ------------------------------------------------------
    market_data_provider: str = Field(
        default="mock",
        description="'mock' or 'alpaca'. The mock provider is never removed.",
    )
    mock_seed: int = 1337
    alpaca: AlpacaSettings = Field(default_factory=AlpacaSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)

    # --- Domain assumptions ----------------------------------------------
    costs: CostSettings = Field(default_factory=CostSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)

    # --- Notifications ----------------------------------------------------
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)

    # --- Scanner ----------------------------------------------------------
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Reject sync drivers early.

        A sync driver inside an async engine fails at request time with a confusing
        greenlet error; failing at startup with a clear message is cheaper.
        """
        if "+asyncpg" not in value and "+aiosqlite" not in value:
            scheme = value.partition("://")[0]
            msg = (
                f"database_url must use an async driver "
                f"(postgresql+asyncpg:// or sqlite+aiosqlite://), got: {scheme}"
            )
            raise ValueError(msg)
        return value

    @property
    def is_production(self) -> bool:
        return self.env is Environment.PRODUCTION

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that ``.env`` is read once. Tests override the FastAPI dependency
    rather than mutating this cache; ``get_settings.cache_clear()`` is available
    for the rare case where an env change must be picked up.
    """
    try:
        return Settings()
    except Exception as exc:
        raise ConfigurationError(f"invalid tradabot configuration: {exc}") from exc
