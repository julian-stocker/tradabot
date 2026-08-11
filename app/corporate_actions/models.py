"""Corporate-action domain models.

A single :class:`CorporateAction` type carries every action kind, discriminated by
:class:`~app.domain.enums.CorporateActionType`, with type-specific fields
validated per kind. The alternative -- a class per action type -- would force a
schema migration for every new kind, which is exactly what the design brief rules
out.

Split ratios are stored as an explicit ``from``/``to`` share pair rather than a
single float. A 3-for-2 split is ``from_shares=2, to_shares=3``; storing 1.5
loses the fact that it was a 3:2, and a 1-for-3 reverse split becomes
0.3333333... which is not exactly representable and compounds badly across
multiple actions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.time import ensure_utc
from app.domain.enums import CorporateActionType


class CorporateAction(BaseModel):
    """One corporate action affecting one instrument.

    Attributes:
        effective_at: for a split, the instant the new shares begin trading; for a
            cash dividend, the **ex-dividend** instant. In both cases it is the
            moment the price series changes character, which is what the
            adjustment layer keys on.
        payment_at: dividend payment date, when known. Recorded for cash-flow
            modelling; irrelevant to price adjustment.
        from_shares / to_shares: split ratio. 2-for-1 is ``from_shares=1,
            to_shares=2``.
        cash_amount: dividend amount per share, in :attr:`currency`.
        source: provider identifier, so a disputed action can be traced.
        external_id: the provider's own id for the action, for deduplication.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    action_type: CorporateActionType
    effective_at: datetime

    payment_at: datetime | None = None
    from_shares: Decimal | None = Field(default=None, gt=0)
    to_shares: Decimal | None = Field(default=None, gt=0)
    cash_amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    source: str = Field(default="unknown", max_length=32)
    external_id: str | None = Field(default=None, max_length=64)

    @field_validator("symbol", "currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("effective_at", "payment_at")
    @classmethod
    def _normalise_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _check_fields_match_type(self) -> CorporateAction:
        """Reject actions missing the fields their type requires.

        A split with no ratio, or a dividend with no amount, is unusable data --
        and much easier to diagnose here than as a silently skipped adjustment
        three layers downstream.
        """
        if self.action_type is CorporateActionType.SPLIT:
            if self.from_shares is None or self.to_shares is None:
                msg = (
                    f"SPLIT for {self.symbol} at {self.effective_at.isoformat()} "
                    f"requires both from_shares and to_shares"
                )
                raise ValueError(msg)
            if self.cash_amount is not None:
                msg = "SPLIT must not carry a cash_amount"
                raise ValueError(msg)

        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            if self.cash_amount is None:
                msg = (
                    f"CASH_DIVIDEND for {self.symbol} at "
                    f"{self.effective_at.isoformat()} requires a cash_amount"
                )
                raise ValueError(msg)
            if self.currency is None:
                msg = "CASH_DIVIDEND requires a currency"
                raise ValueError(msg)
            if self.from_shares is not None or self.to_shares is not None:
                msg = "CASH_DIVIDEND must not carry a split ratio"
                raise ValueError(msg)

        if self.payment_at is not None and self.payment_at < self.effective_at:
            msg = (
                f"payment_at ({self.payment_at.isoformat()}) precedes effective_at "
                f"({self.effective_at.isoformat()}); a dividend cannot pay before it goes ex"
            )
            raise ValueError(msg)

        return self

    @property
    def split_ratio(self) -> Decimal:
        """Shares held after the action per share held before.

        2-for-1 returns 2; a 1-for-10 reverse split returns ``0.1``. Returns 1 for
        actions that do not change the share count, so callers can multiply
        unconditionally.
        """
        if self.from_shares is None or self.to_shares is None:
            return Decimal(1)
        return self.to_shares / self.from_shares

    @property
    def is_reverse_split(self) -> bool:
        return self.action_type is CorporateActionType.SPLIT and self.split_ratio < 1

    def describe(self) -> str:
        """Human-readable one-liner, for logs and API explanations."""
        when = self.effective_at.date().isoformat()
        if self.action_type is CorporateActionType.SPLIT:
            kind = "reverse split" if self.is_reverse_split else "split"
            return f"{_plain(self.to_shares)}-for-{_plain(self.from_shares)} {kind} on {when}"
        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            return f"{self.cash_amount} {self.currency} cash dividend, ex {when}"
        return f"{self.action_type.value} on {when}"


def _plain(value: Decimal | None) -> str:
    """Render a Decimal without trailing zeros.

    A ratio loaded from ``NUMERIC(18, 6)`` arrives as ``4.000000``. Unlike a
    float, ``:g`` does not normalise a Decimal, so "4-for-1" would read
    "4.000000-for-1.000000".
    """
    if value is None:
        return "?"
    normalised = value.normalize()
    # normalize() renders small integers in exponent form (1E+1); undo that.
    return f"{normalised:f}"
