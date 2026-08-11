"""Console backend.

Renders the *same* :class:`NotificationMessage` a Discord channel would receive,
to the structured log. Not a stub: it shares the formatter, so what you see
locally is what would be posted, and a formatting bug is visible without a
webhook.

It also makes the whole notification path exercisable in tests and in CI with no
network and no configuration -- which is the difference between a delivery layer
that is tested and one that is merely written.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.notifications.models import DeliveryResult, NotificationMessage

logger = get_logger(__name__)

BACKEND_NAME = "console"


class ConsoleNotifier:
    """Writes notifications to the log. Never fails."""

    name = BACKEND_NAME

    def __init__(self, *, max_characters: int = 1900) -> None:
        self._max_characters = max_characters

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Log the rendered message.

        Truncated to the same limit a real transport would apply, so the console
        shows what Discord would show rather than a fuller version that hides a
        truncation problem until it reaches production.
        """
        logger.info(
            "notification",
            category=message.category.value,
            severity=message.severity.value,
            event_type=message.event_type.value,
            body=message.rendered(self._max_characters),
        )
        return DeliveryResult(backend=BACKEND_NAME, delivered=True)
