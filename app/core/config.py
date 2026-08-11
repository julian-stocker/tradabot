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
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError

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
    market_data_provider: str = "mock"
    mock_seed: int = 1337

    # --- Domain assumptions ----------------------------------------------
    costs: CostSettings = Field(default_factory=CostSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)

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
