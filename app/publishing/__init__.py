"""Discord presentation and transport.

Turns what :mod:`app.monitoring` decided was worth reporting into messages, and
delivers them to the channels resolved by :mod:`app.core.webhooks`.

**Presentation only.** Materiality, deduplication, cooldowns, ranking and weekly
aggregation belong to the monitoring engine and are not reimplemented here, so
changing a threshold in one place changes every channel.

**Output-only.** Nothing in this package can reach a broker, place an order or
alter monitoring state incorrectly, and a delivery failure is returned rather
than raised -- a Discord outage must never become a trading or analysis outage.
"""

from app.publishing.channels import (
    MARKET_SIGNALS,
    MARKET_TRENDS,
    PAPER_CHANNELS,
    STATUS,
    SYSTEM,
    channel_for,
    paper_channel,
)
from app.publishing.coverage import Coverage, is_partial
from app.publishing.coverage import label as coverage_label
from app.publishing.ledger import (
    DeliveryLedger,
    DeliveryRecord,
    DeliveryStatus,
    event_id,
    observation_date,
)
from app.publishing.publisher import Publisher, PublishOutcome

__all__ = [
    "MARKET_SIGNALS",
    "MARKET_TRENDS",
    "PAPER_CHANNELS",
    "STATUS",
    "SYSTEM",
    "Coverage",
    "DeliveryLedger",
    "DeliveryRecord",
    "DeliveryStatus",
    "PublishOutcome",
    "Publisher",
    "channel_for",
    "coverage_label",
    "event_id",
    "is_partial",
    "observation_date",
    "paper_channel",
]
