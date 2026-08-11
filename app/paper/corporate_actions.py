"""Corporate actions applied to open paper positions.

Resolves the phase 3 known risk. Phase 2 taught the *price series* about splits;
this teaches *held positions* about them.

The problem
-----------
A position of 10 shares at 100 is worth 1000. After a 2-for-1 split it is 20
shares at 50 -- still 1000. Nothing happened economically. But a simulator that
adjusts the price series and not the position sees its 10 shares reprice from 100
to 50 and books a 50% loss that never occurred.

The rule
--------
**Economic value is preserved exactly.**

    quantity    x= ratio
    entry price /= ratio
    stop        /= ratio
    take profit /= ratio

so ``quantity x entry_price`` is unchanged. The ratio comes from the domain's
exact ``from_shares``/``to_shares`` pair, never a float, so a 3-for-2 split stays
3:2 and does not compound rounding across successive actions.

**Exactness caveat.** Value is preserved exactly whenever the adjusted price is
representable at the storage scale -- every 2-for-1, 4-for-1, 10-for-1 and reverse
split. A 3-for-2 leaves 100/1.5 = 66.666... which quantises to six decimals, so
the reconstructed value differs by a fraction of a cent on a 1000 position. That
residue is inherent to storing a price at finite precision; it is bounded by the
price quantum and does not accumulate, because each adjustment starts from the
stored entry rather than from a running product.

Stops and targets are adjusted for the same reason: a stop at 96 against a 100
entry is a 4% stop. Leaving it at 96 after a 2-for-1 split turns it into a stop
92% below the new price -- effectively deleting the risk control at the exact
moment the numbers look most confusing.

Scope
-----
Splits only, including reverse splits (the arithmetic is identical for a ratio
below 1). Cash dividends do not change a position's share count and are *not*
credited as cash: modelling dividend income requires payment dates, withholding
tax and currency handling, and a half-implementation would quietly overstate
returns. Mergers and spin-offs are out of scope and are refused loudly rather
than approximated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.time import ensure_utc
from app.corporate_actions.models import CorporateAction
from app.db.models import VirtualPosition
from app.domain.enums import CorporateActionType, PositionStatus

logger = get_logger(__name__)

QUANTITY_EXPONENT = Decimal("0.00000001")
PRICE_EXPONENT = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class PositionAdjustment:
    """One position's before/after, for the audit trail."""

    position_id: int
    simulation_profile_id: int
    ratio: Decimal
    quantity_before: Decimal
    quantity_after: Decimal
    entry_before: Decimal
    entry_after: Decimal
    stop_before: Decimal | None
    stop_after: Decimal | None
    target_before: Decimal | None
    target_after: Decimal | None

    @property
    def value_before(self) -> Decimal:
        return self.quantity_before * self.entry_before

    @property
    def value_after(self) -> Decimal:
        return self.quantity_after * self.entry_after

    def describe(self) -> str:
        return (
            f"position {self.position_id}: {self.quantity_before} @ "
            f"{self.entry_before} -> {self.quantity_after} @ {self.entry_after} "
            f"(ratio {self.ratio})"
        )


def apply_split_to_position(position: VirtualPosition, ratio: Decimal) -> PositionAdjustment:
    """Adjust one open position for a split, preserving economic value.

    Mutates ``position`` in place and returns what changed.

    Raises:
        ValueError: on a non-positive ratio, which would produce a nonsensical
            position rather than an adjusted one.
    """
    if ratio <= 0:
        msg = f"split ratio must be positive, got {ratio}"
        raise ValueError(msg)

    quantity_before = position.quantity
    entry_before = position.average_entry_price
    stop_before = position.stop_loss
    target_before = position.take_profit

    position.quantity = (quantity_before * ratio).quantize(QUANTITY_EXPONENT)
    position.average_entry_price = (entry_before / ratio).quantize(PRICE_EXPONENT)
    position.stop_loss = (
        (stop_before / ratio).quantize(PRICE_EXPONENT) if stop_before is not None else None
    )
    position.take_profit = (
        (target_before / ratio).quantize(PRICE_EXPONENT) if target_before is not None else None
    )

    # Marks and excursion trackers are prices too; leaving them unscaled would
    # make the next valuation compare a post-split quantity against a pre-split
    # price.
    if position.current_mark_price is not None:
        position.current_mark_price = (position.current_mark_price / ratio).quantize(PRICE_EXPONENT)
    if position.highest_price_seen is not None:
        position.highest_price_seen = (position.highest_price_seen / ratio).quantize(PRICE_EXPONENT)
    if position.lowest_price_seen is not None:
        position.lowest_price_seen = (position.lowest_price_seen / ratio).quantize(PRICE_EXPONENT)

    return PositionAdjustment(
        position_id=position.id,
        simulation_profile_id=position.simulation_profile_id,
        ratio=ratio,
        quantity_before=quantity_before,
        quantity_after=position.quantity,
        entry_before=entry_before,
        entry_after=position.average_entry_price,
        stop_before=stop_before,
        stop_after=position.stop_loss,
        target_before=target_before,
        target_after=position.take_profit,
    )


class PositionCorporateActionService:
    """Applies corporate actions to open positions across every profile."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def apply_actions(
        self,
        *,
        instrument_id: int,
        actions: Sequence[CorporateAction],
        as_of: datetime,
    ) -> list[PositionAdjustment]:
        """Apply every split effective at or before ``as_of`` to open positions.

        A split is applied to a position only when it is effective **after** both:

        * the position's entry -- a position opened after the split was already
          bought at post-split prices, and adjusting it would halve a holding that
          was never doubled; and
        * ``corporate_actions_applied_through`` -- what this position has already
          been adjusted for.

        The second condition is what makes the call idempotent. Re-importing a
        provider's full action history is routine (it is how new actions arrive),
        so an adjustment that fired again on every import would repeatedly halve
        the same position until it rounded to nothing.

        Cash dividends are skipped -- see the module docstring.
        """
        as_of = ensure_utc(as_of)
        splits = sorted(
            (
                action
                for action in actions
                if action.action_type is CorporateActionType.SPLIT and action.effective_at <= as_of
            ),
            key=lambda a: a.effective_at,
        )
        if not splits:
            return []

        adjustments: list[PositionAdjustment] = []
        for split in splits:
            for position in await self._unadjusted_positions(
                instrument_id=instrument_id, moment=split.effective_at
            ):
                adjustment = apply_split_to_position(position, split.split_ratio)
                position.corporate_actions_applied_through = split.effective_at
                adjustments.append(adjustment)
                logger.info(
                    "adjusted open position for split",
                    action=split.describe(),
                    **_log_fields(adjustment),
                )

        if adjustments:
            await self._session.flush()
        return adjustments

    async def mark_applied_through(self, *, instrument_id: int, moment: datetime) -> int:
        """Record that positions are current with actions up to ``moment``.

        Called for positions opened *after* the last known action, so that a later
        import does not treat historical splits as new for them. Returns how many
        positions were stamped.
        """
        moment = ensure_utc(moment)
        positions = await self._open_positions(instrument_id)
        stamped = 0
        for position in positions:
            if (
                position.corporate_actions_applied_through is None
                or position.corporate_actions_applied_through < moment
            ):
                position.corporate_actions_applied_through = moment
                stamped += 1
        if stamped:
            await self._session.flush()
        return stamped

    async def _unadjusted_positions(
        self, *, instrument_id: int, moment: datetime
    ) -> Sequence[VirtualPosition]:
        """Open positions that predate ``moment`` and have not been adjusted for it."""
        return [
            position
            for position in await self._open_positions(instrument_id)
            if position.entry_timestamp < moment
            and (
                position.corporate_actions_applied_through is None
                or position.corporate_actions_applied_through < moment
            )
        ]

    async def _open_positions(self, instrument_id: int) -> Sequence[VirtualPosition]:
        stmt = (
            select(VirtualPosition)
            .where(
                VirtualPosition.instrument_id == instrument_id,
                VirtualPosition.status == PositionStatus.OPEN,
            )
            .order_by(VirtualPosition.id)
        )
        return (await self._session.execute(stmt)).scalars().all()


def _log_fields(adjustment: PositionAdjustment) -> dict[str, object]:
    return {
        "position_id": adjustment.position_id,
        "profile_id": adjustment.simulation_profile_id,
        "ratio": str(adjustment.ratio),
        "quantity": f"{adjustment.quantity_before} -> {adjustment.quantity_after}",
        "entry": f"{adjustment.entry_before} -> {adjustment.entry_after}",
    }
