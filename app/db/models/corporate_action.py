"""Corporate-action ORM model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import Money, UTCDateTime
from app.domain.enums import CorporateActionType

RATIO_PRECISION = 18
RATIO_SCALE = 6
AMOUNT_PRECISION = 18
AMOUNT_SCALE = 6


class CorporateActionRow(Base, TimestampMixin):
    """A corporate action affecting one instrument.

    **One table for every action type**, discriminated by :attr:`action_type`,
    with nullable type-specific columns guarded by CHECK constraints. Recording a
    spin-off or a symbol change therefore needs no migration -- only the
    adjustment rule that interprets it.

    The alternative (a table per type) multiplies joins for a query as ordinary as
    "every action for this instrument, in order", which is exactly what the
    adjustment layer asks on every read.
    """

    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[CorporateActionType] = mapped_column(
        Enum(CorporateActionType, native_enum=False, length=24, validate_strings=True),
        nullable=False,
    )
    effective_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        doc=(
            "Split: first instant the new shares trade. Cash dividend: the "
            "ex-dividend instant. In both cases the moment the series changes."
        ),
    )
    payment_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, doc="Dividend payment date, when known."
    )

    from_shares: Mapped[Decimal | None] = mapped_column(
        Money(RATIO_PRECISION, RATIO_SCALE),
        nullable=True,
        doc="Shares held before the split. 2-for-1 is from_shares=1, to_shares=2.",
    )
    to_shares: Mapped[Decimal | None] = mapped_column(
        Money(RATIO_PRECISION, RATIO_SCALE),
        nullable=True,
        doc="Shares held after the split.",
    )

    cash_amount: Mapped[Decimal | None] = mapped_column(
        Money(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=True,
        doc="Dividend per share, in `currency`.",
    )
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="unknown",
        doc="Provider that reported the action, so a disputed entry is traceable.",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="Provider's own identifier for the action."
    )

    __table_args__ = (
        # Natural key: one instrument cannot have two actions of the same type
        # effective at the same instant. Makes ingestion idempotent.
        UniqueConstraint(
            "instrument_id",
            "action_type",
            "effective_at",
            name="instrument_id_action_type_effective_at",
        ),
        CheckConstraint(
            "action_type <> 'SPLIT' OR "
            "(from_shares IS NOT NULL AND to_shares IS NOT NULL "
            " AND from_shares > 0 AND to_shares > 0)",
            name="split_requires_ratio",
        ),
        CheckConstraint(
            "action_type <> 'CASH_DIVIDEND' OR "
            "(cash_amount IS NOT NULL AND cash_amount > 0 AND currency IS NOT NULL)",
            name="dividend_requires_amount",
        ),
        CheckConstraint(
            "payment_at IS NULL OR payment_at >= effective_at",
            name="payment_after_effective",
        ),
        CheckConstraint("currency IS NULL OR length(currency) = 3", name="currency_iso4217"),
        # The adjustment layer always reads "all actions for instrument X,
        # optionally up to time T, in chronological order".
        Index("ix_corporate_actions_instrument_id_effective_at", "instrument_id", "effective_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<CorporateAction {self.action_type} instrument={self.instrument_id} "
            f"{self.effective_at.isoformat()}>"
        )
