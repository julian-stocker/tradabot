"""UTC-only time helpers.

Rule 7 of the project coding standard: *the application uses UTC internally*.
Naive datetimes are rejected rather than silently assumed to be UTC -- silently
assuming is how off-by-one-session bugs get into backtests.

Localisation to an exchange timezone is strictly a presentation concern and
belongs in the UI layer, never in feature or signal calculations.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    Injected as a callable (see ``app.api.deps``) so tests can freeze time
    without monkeypatching the stdlib.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC.

    Raises:
        ValueError: if ``value`` is naive. We refuse to guess a timezone.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = (
            f"naive datetime {value!r} is not accepted; tradabot works in UTC and "
            f"will not guess a timezone"
        )
        raise ValueError(msg)
    return value.astimezone(UTC)


def is_aware(value: datetime) -> bool:
    """True if ``value`` carries a usable timezone offset."""
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None
