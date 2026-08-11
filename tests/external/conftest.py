"""Opt-in tests that hit a live provider.

**Skipped by default.** Every other test in this repository is offline and
deterministic; these are neither. They need credentials, they cost API quota, and
they fail when a third party has an outage -- so they must never run as part of
``make test``, where a red bar has to mean "our code broke".

Run them deliberately::

    TRADABOT_RUN_EXTERNAL_TESTS=1 pytest tests/external -v

or ``make smoke-real-data``.

Both gates must pass: the opt-in flag *and* configured credentials. Missing
credentials skip rather than fail -- an absent key is a local setup fact, not a
defect in the code under test.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings, get_settings

OPT_IN_VARIABLE = "TRADABOT_RUN_EXTERNAL_TESTS"


def _opted_in() -> bool:
    return os.environ.get(OPT_IN_VARIABLE, "").strip().lower() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """Real settings from the environment, skipping when this is not a live run."""
    if not _opted_in():
        pytest.skip(f"external tests are opt-in; set {OPT_IN_VARIABLE}=1 to run them")

    settings = get_settings()
    if settings.market_data_provider == "mock":
        pytest.skip("TRADABOT_MARKET_DATA_PROVIDER is `mock`; nothing external to test")
    if not settings.alpaca.is_configured:
        # Reports *that* credentials are missing, never anything about their value.
        pytest.skip("no provider credentials configured in the environment")
    return settings
