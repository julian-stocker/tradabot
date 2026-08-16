"""What a paper account actually represents.

The claim being avoided
-----------------------
An Alpaca paper account shows US-listed positions held in that one account. It
does not show holdings at another broker, a pension, property, cash outside the
account, or anything not listed in the US. A message that says "your portfolio
is 41% technology" when the account is one slice of someone's holdings states a
true number about a false subject, and the reader has no way to tell.

So every portfolio message states its coverage, always. There is no silent case.

Declared, never inferred
------------------------
Coverage is read from configuration. It cannot be derived from position data:
whether an account is someone's entire exposure is a fact about their
circumstances, and no amount of holdings tells you what is missing. Nothing here
estimates an unknown weight or invents a position it cannot see.

The default is the honest one
-----------------------------
With nothing configured, the label is ``ALPACA ACCOUNT ONLY`` -- accurate,
unglamorous, and it makes no claim about total wealth. Silence would default to
the reader's assumption, which is usually the wrong one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Final

ENV_PREFIX: Final = "TRADABOT_COVERAGE_"


class Coverage(StrEnum):
    """How much of a person's holdings one account represents."""

    FULL = "FULL_PORTFOLIO"
    PARTIAL = "PARTIAL_PORTFOLIO"
    US_ONLY = "US_ONLY_VIEW"
    ACCOUNT_ONLY = "ALPACA_ACCOUNT_ONLY"
    """The default. Says what is certainly true and claims nothing further."""


_TEXT: Final[dict[Coverage, str]] = {
    Coverage.FULL: "FULL — configured as the complete portfolio",
    Coverage.PARTIAL: "PARTIAL — this account is one part of a larger portfolio",
    Coverage.US_ONLY: "PARTIAL — US-listed holdings only",
    Coverage.ACCOUNT_ONLY: (
        "ALPACA ACCOUNT ONLY — this account's positions; not a view of total holdings"
    ),
}


def resolve(account: str, env: Mapping[str, str] | None = None) -> tuple[Coverage, str]:
    """The coverage state and display text for one account.

    Args:
        account: slot name, e.g. ``PAPER_3K``.
        env: environment mapping. Defaults to ``os.environ``.

    An unrecognised configured value is treated as free text on top of
    ``PARTIAL``: someone who wrote something specific meant to qualify the
    account, and reading it as "full portfolio" would be the one wrong answer.
    """
    source = env if env is not None else os.environ
    raw = str(source.get(f"{ENV_PREFIX}{account.upper()}", "")).strip()
    if not raw:
        return Coverage.ACCOUNT_ONLY, _TEXT[Coverage.ACCOUNT_ONLY]
    try:
        state = Coverage(raw.upper().replace(" ", "_"))
    except ValueError:
        return Coverage.PARTIAL, f"PARTIAL — {raw}"
    return state, _TEXT[state]


def label(account: str, env: Mapping[str, str] | None = None) -> str:
    """Display text only. Never empty."""
    return resolve(account, env)[1]


def is_partial(account: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether this account is known not to be the whole picture."""
    return resolve(account, env)[0] is not Coverage.FULL
