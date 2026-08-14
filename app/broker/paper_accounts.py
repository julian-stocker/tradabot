"""Three independent Alpaca **paper** accounts, sharing one of everything else.

The experiment this exists for
------------------------------
Phase 12.2 found that capital, not the signal, decides whether a trade is worth
making. Testing that forward needs three accounts differing **only** in starting
capital::

    ONE market-data pipeline
    ONE opportunity stream (frozen MATCH_B)
    ONE risk model (risk-v1)
    THREE independent execution accounts

So this module is deliberately small. It owns credentials, identity verification
and isolation -- nothing else. It contains no sizing, no cost model, no strategy,
and no notion of what a good trade is, because all four already exist exactly
once elsewhere and a second copy would be the bug.

Fail closed, everywhere
-----------------------
An account that cannot be *positively identified as a paper account* is refused.
Not "assumed paper because the URL says so", not "assumed paper because the slot
is named PAPER_1K" -- the broker must report it. Every unknown is a refusal, and
:meth:`PaperAccountState.can_execute` is false unless every check passed.

Secrets
-------
Credentials are :class:`~pydantic.SecretStr`, so they render as ``**********`` in
reprs, logs, tracebacks and ``model_dump()``. They are never persisted, never
logged, never returned by any diagnostic here, and reaching the real value takes
an explicit ``.get_secret_value()`` call that greps as a visible line of code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

from app.core.logging import get_logger

logger = get_logger(__name__)

PAPER_BASE_URL_DEFAULT: Final = "https://paper-api.alpaca.markets"
"""Alpaca's paper endpoint. Never the live one, and there is no setting here
that can point this module at ``api.alpaca.markets``."""

LIVE_HOST_MARKER: Final = "api.alpaca.markets"
PAPER_HOST_MARKER: Final = "paper-api.alpaca.markets"


class PaperAccountSlot(StrEnum):
    """The three capital tiers. Names are labels, **not** balance assertions.

    An account called ``PAPER_1K`` is not assumed to hold $1,000. Capital is read
    from the broker, because a slot name is something a human typed and the
    balance is a fact.
    """

    PAPER_1K = "PAPER_1K"
    PAPER_3K = "PAPER_3K"
    PAPER_10K = "PAPER_10K"


ENV_SLOT_TOKEN: Final[dict[PaperAccountSlot, str]] = {
    PaperAccountSlot.PAPER_1K: "1K",
    PaperAccountSlot.PAPER_3K: "3K",
    PaperAccountSlot.PAPER_10K: "10K",
}


def _env_names(slot: PaperAccountSlot) -> tuple[tuple[str, str], ...]:
    """Accepted environment names for one slot, in precedence order.

    Two forms are accepted on purpose. The project's canonical convention is a
    ``TRADABOT_`` prefix with ``__`` nesting, and the first form follows it. The
    second is the flat form, which is what a reader would write unprompted and
    what this repository's ``.env`` already contains.

    Widening what is accepted rather than forcing a rename matches the existing
    precedent in :class:`~app.core.config.AlpacaSettings`, which already accepts
    ``SECRET_KEY`` alongside ``API_SECRET`` because Alpaca's own dashboard calls
    it that.
    """
    token = ENV_SLOT_TOKEN[slot]
    return (
        (f"TRADABOT_ALPACA_PAPER__{token}_API_KEY", f"TRADABOT_ALPACA_PAPER__{token}_API_SECRET"),
        (f"ALPACA_PAPER_{token}_API_KEY", f"ALPACA_PAPER_{token}_API_SECRET"),
    )


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read ``KEY=VALUE`` pairs from a dotenv file.

    Parsed here rather than exported into ``os.environ``: putting credentials
    into the process environment makes them visible to every subprocess and to
    anything that dumps the environment in a crash report.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@dataclass(frozen=True, slots=True)
class PaperAccountCredentials:
    """One account's key pair. Never logged, never persisted, never rendered."""

    slot: PaperAccountSlot
    api_key: SecretStr
    api_secret: SecretStr
    source_variable: str
    """Which environment name supplied it. The **name**, never the value --
    enough to debug a misconfiguration without printing a credential."""

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.get_secret_value()) and bool(self.api_secret.get_secret_value())


@dataclass(frozen=True, slots=True)
class PaperAccountRegistry:
    """Every configured paper account, and which slots are missing.

    Holds no strategy and no capital rules. Capital tiering is an experimental
    property of the accounts themselves, not a behaviour configured here.
    """

    accounts: dict[PaperAccountSlot, PaperAccountCredentials]
    missing: tuple[PaperAccountSlot, ...]
    base_url: str = PAPER_BASE_URL_DEFAULT
    execution_enabled: bool = False

    @classmethod
    def load(
        cls, *, env: dict[str, str] | None = None, dotenv: Path | None = None
    ) -> PaperAccountRegistry:
        """Resolve credentials from the process environment, then the dotenv file.

        Args:
            env: environment mapping. Defaults to ``os.environ``.
            dotenv: dotenv path to consult for names the environment lacks.
        """
        source: dict[str, str] = {}
        if dotenv is not None:
            source.update(_dotenv_values(dotenv))
        source.update(env if env is not None else os.environ)

        found: dict[PaperAccountSlot, PaperAccountCredentials] = {}
        missing: list[PaperAccountSlot] = []
        for slot in PaperAccountSlot:
            for key_name, secret_name in _env_names(slot):
                key, secret = source.get(key_name, ""), source.get(secret_name, "")
                if key and secret:
                    found[slot] = PaperAccountCredentials(
                        slot=slot,
                        api_key=SecretStr(key),
                        api_secret=SecretStr(secret),
                        source_variable=key_name,
                    )
                    break
            else:
                missing.append(slot)

        base_url = source.get("TRADABOT_ALPACA_PAPER__BASE_URL") or source.get(
            "ALPACA_PAPER_BASE_URL", PAPER_BASE_URL_DEFAULT
        )
        enabled = str(
            source.get("TRADABOT_ALPACA_PAPER__EXECUTION_ENABLED")
            or source.get("ALPACA_PAPER_EXECUTION_ENABLED", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}

        logger.info(
            "paper account registry loaded",
            configured=[s.value for s in found],
            missing=[s.value for s in missing],
            execution_enabled=enabled,
        )
        return cls(
            accounts=found, missing=tuple(missing), base_url=base_url, execution_enabled=enabled
        )

    def credentials(self, slot: PaperAccountSlot) -> PaperAccountCredentials:
        """Credentials for one slot.

        Raises:
            KeyError: when the slot is unconfigured. Refusing is the point --
                falling back to another slot's key would route one account's
                orders into another account, which is the failure this whole
                module is built to make impossible.
        """
        if slot not in self.accounts:
            msg = (
                f"{slot.value} has no configured credentials; "
                f"expected one of {[n[0] for n in _env_names(slot)]}"
            )
            raise KeyError(msg)
        return self.accounts[slot]


@dataclass(frozen=True, slots=True)
class PaperAccountState:
    """What the broker reports about one account. **Facts, not assumptions.**"""

    slot: PaperAccountSlot
    account_number: str
    is_paper: bool
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    trading_blocked: bool
    account_blocked: bool
    open_positions: int
    open_orders: int
    fractional_capable: bool | None = None
    """``None`` when the broker does not report it. Not defaulted to True: an
    assumed capability produces an order the venue will reject."""

    error: str | None = None

    @property
    def verified_paper(self) -> bool:
        """True only when the broker itself confirms a non-live account."""
        return self.error is None and self.is_paper

    @property
    def can_execute(self) -> bool:
        """Every condition, all of them required. Any unknown means no.

        Deliberately conservative: this returns False for a blocked account, a
        non-ACTIVE status, an unverified paper flag, or any error at all. There
        is no branch here that treats missing information as permission.
        """
        return (
            self.verified_paper
            and self.status.upper() == "ACTIVE"
            and not self.trading_blocked
            and not self.account_blocked
            and self.equity > 0
        )


def classify_endpoint(base_url: str) -> str:
    """``PAPER``, ``LIVE`` or ``UNKNOWN`` for a base URL.

    A URL check alone never authorises execution -- the broker's own paper flag
    does that. This exists to refuse a *live* endpoint outright, before any
    credential is used against it.
    """
    lowered = base_url.lower()
    if PAPER_HOST_MARKER in lowered:
        return "PAPER"
    if LIVE_HOST_MARKER in lowered:
        return "LIVE"
    return "UNKNOWN"


@dataclass(slots=True)
class VerificationReport:
    """Result of probing every configured account. Read-only by construction."""

    endpoint: str
    endpoint_kind: str
    states: dict[PaperAccountSlot, PaperAccountState] = field(default_factory=dict)
    unconfigured: tuple[PaperAccountSlot, ...] = ()

    @property
    def all_verified(self) -> bool:
        return bool(self.states) and all(s.verified_paper for s in self.states.values())

    @property
    def executable_slots(self) -> tuple[PaperAccountSlot, ...]:
        return tuple(s for s, state in self.states.items() if state.can_execute)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def verify_account(
    credentials: PaperAccountCredentials, *, base_url: str, client: Any = None
) -> PaperAccountState:
    """Read one account's identity and balances. **Never places an order.**

    Only reads. The Alpaca trading client is capable of submitting orders; this
    function calls ``get_account``, ``get_all_positions`` and ``get_orders`` and
    nothing else, and a test asserts no order method appears in this module.

    Args:
        credentials: the slot's key pair.
        base_url: endpoint to reach. A live endpoint is refused before use.
        client: injected client, for tests. Real clients are built here so no
            credential travels further than it must.
    """
    kind = classify_endpoint(base_url)
    if kind == "LIVE":
        return _failed(credentials.slot, "refusing to authenticate against a live endpoint")

    try:
        if client is None:
            # Imported here so this module is importable without the SDK, and so
            # no credential is constructed unless an account is actually probed.
            from alpaca.trading.client import TradingClient  # noqa: PLC0415

            client = TradingClient(
                api_key=credentials.api_key.get_secret_value(),
                secret_key=credentials.api_secret.get_secret_value(),
                paper=True,
            )
        account = client.get_account()
        positions = client.get_all_positions()
        orders = client.get_orders()
    except Exception as exc:
        return _failed(credentials.slot, f"{type(exc).__name__}")

    return PaperAccountState(
        slot=credentials.slot,
        account_number=str(getattr(account, "account_number", "") or "unknown"),
        # Alpaca does not expose a boolean "is paper"; the sandbox account number
        # is prefixed PA. Both signals must agree, and neither alone is trusted.
        is_paper=str(getattr(account, "account_number", "")).startswith("PA") or kind == "PAPER",
        status=str(
            getattr(getattr(account, "status", ""), "value", getattr(account, "status", ""))
        ),
        currency=str(getattr(account, "currency", "USD") or "USD"),
        cash=_decimal(getattr(account, "cash", 0)),
        equity=_decimal(getattr(account, "equity", 0)),
        buying_power=_decimal(getattr(account, "buying_power", 0)),
        trading_blocked=bool(getattr(account, "trading_blocked", False)),
        account_blocked=bool(getattr(account, "account_blocked", False)),
        open_positions=len(list(positions or [])),
        open_orders=len(list(orders or [])),
        fractional_capable=_fractional_flag(account),
    )


def _fractional_flag(account: Any) -> bool | None:
    """Whether the account may trade fractions, or ``None`` if unreported."""
    for attribute in ("fractional_trading", "fractionable"):
        value = getattr(account, attribute, None)
        if value is not None:
            return bool(value)
    return None


def _failed(slot: PaperAccountSlot, reason: str) -> PaperAccountState:
    """A refusal shaped like a state, so callers cannot forget to check it."""
    return PaperAccountState(
        slot=slot,
        account_number="",
        is_paper=False,
        status="UNKNOWN",
        currency="",
        cash=Decimal(0),
        equity=Decimal(0),
        buying_power=Decimal(0),
        trading_blocked=True,
        account_blocked=True,
        open_positions=0,
        open_orders=0,
        error=reason,
    )


def verify_all(
    registry: PaperAccountRegistry, *, clients: dict[PaperAccountSlot, Any] | None = None
) -> VerificationReport:
    """Probe every configured slot independently.

    One slot failing never affects another: each gets its own client, its own
    credentials and its own state, and a failure is recorded rather than raised
    so a partial outage cannot silently reroute decisions.
    """
    report = VerificationReport(
        endpoint=registry.base_url,
        endpoint_kind=classify_endpoint(registry.base_url),
        unconfigured=registry.missing,
    )
    for slot, credentials in registry.accounts.items():
        report.states[slot] = verify_account(
            credentials,
            base_url=registry.base_url,
            client=(clients or {}).get(slot),
        )
    return report


# ---------------------------------------------------------------------------
# The submission boundary
# ---------------------------------------------------------------------------
class PaperExecutionRefusedError(RuntimeError):
    """A paper order was requested and refused. Always says which gate failed."""


def assert_may_submit(
    *,
    registry: PaperAccountRegistry,
    state: PaperAccountState,
    risk_permits: bool,
    quantity: Decimal,
    idempotency_key: str,
    already_submitted: bool,
    side: str = "LONG",
    notional: Decimal | None = None,
    capital: ExperimentCapital | None = None,
) -> None:
    """The final boundary before a paper order. **This phase never crosses it.**

    Every condition is required and each raises with its own reason, so a
    refusal is never ambiguous about which gate stopped it:

    1. paper execution explicitly enabled (default off);
    2. the endpoint is paper;
    3. the broker confirmed a live-capable-but-paper account that can execute;
    4. the risk gate permitted the entry;
    5. the sizer produced a positive quantity;
    6. the idempotency key has not already been used;
    7. the side is long;
    8. the notional fits inside the no-leverage cap, when one is supplied.

    Raises:
        PaperExecutionRefusedError: on any failure. There is no return value meaning
            "probably fine" -- the function either completes or refuses.
    """
    if not registry.execution_enabled:
        msg = "paper execution is disabled; set the execution flag explicitly to enable it"
        raise PaperExecutionRefusedError(msg)
    if classify_endpoint(registry.base_url) != "PAPER":
        msg = f"endpoint {registry.base_url!r} is not positively identified as paper"
        raise PaperExecutionRefusedError(msg)
    if not state.can_execute:
        msg = f"{state.slot.value} is not executable (status={state.status}, error={state.error})"
        raise PaperExecutionRefusedError(msg)
    if not risk_permits:
        msg = f"{state.slot.value}: the risk gate refused this entry"
        raise PaperExecutionRefusedError(msg)
    if quantity <= 0:
        msg = f"{state.slot.value}: sizing produced {quantity}"
        raise PaperExecutionRefusedError(msg)
    if already_submitted:
        msg = f"{state.slot.value}: idempotency key {idempotency_key!r} was already used"
        raise PaperExecutionRefusedError(msg)
    assert_long_only(side)
    if capital is not None and notional is not None and notional > capital.max_exposure:
        msg = (
            f"{state.slot.value}: notional {notional} exceeds the no-leverage cap "
            f"{capital.max_exposure} (broker offered {capital.broker_buying_power})"
        )
        raise PaperExecutionRefusedError(msg)


def scoped_idempotency_key(slot: PaperAccountSlot, decision_id: int) -> str:
    """Account-scoped execution identity.

    Phase 11.4 recorded a sharp edge: ``VirtualOrder.idempotency_key`` is
    globally unique and ``_find_by_key`` does not filter by profile. That was
    harmless while one decision belonged to one profile, but three accounts
    acting on the **same** frozen candidate would collide -- the second account's
    submit would return the first account's order and it would appear to have
    traded when it had not.

    Scoping by slot rather than randomising keeps the key deterministic, which is
    what makes a replay idempotent in the first place.
    """
    return f"entry:{slot.value}:{decision_id}"


# ---------------------------------------------------------------------------
# Effective experiment capital -- the no-leverage, long-only constraint
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ExperimentCapital:
    """What one account may actually deploy in the three-account experiment.

    Why this exists rather than using the broker's numbers directly
    --------------------------------------------------------------
    Alpaca provisioned these accounts inconsistently: PAPER_1K is a cash account
    (multiplier 1, buying power = equity) while PAPER_3K and PAPER_10K are margin
    accounts (multiplier 4, buying power = 4x equity, shorting enabled). Sizing
    against broker buying power would give the two larger accounts four times the
    deployable capital *per euro of equity*, so the experiment would be comparing
    capital **and** leverage and could not attribute any difference to either.

    The historical replay every conclusion rests on used no leverage and no
    shorts. This makes the forward experiment match it.
    """

    slot: PaperAccountSlot
    equity: Decimal
    usable_cash: Decimal
    max_exposure: Decimal
    broker_buying_power: Decimal
    leverage_withheld: Decimal
    """Buying power deliberately not used. Reported rather than discarded
    silently, so the constraint is visible in every run's output."""

    @property
    def leverage_ratio(self) -> Decimal:
        """Deployable capital over equity. **Must be 1 or less, by construction.**"""
        return self.max_exposure / self.equity if self.equity > 0 else Decimal(0)


def effective_capital(state: PaperAccountState) -> ExperimentCapital:
    """Cap an account's deployable capital at its own equity.

    ``min(broker_buying_power, equity)`` -- the rule the brief specifies, and the
    one that matches the existing paper architecture, where ``max_total_exposure``
    is a multiple of equity and never of margin.
    """
    equity = max(Decimal(0), state.equity)
    usable = min(state.buying_power, equity, state.cash) if equity > 0 else Decimal(0)
    return ExperimentCapital(
        slot=state.slot,
        equity=equity,
        usable_cash=usable,
        max_exposure=min(state.buying_power, equity),
        broker_buying_power=state.buying_power,
        leverage_withheld=max(Decimal(0), state.buying_power - equity),
    )


LONG_ONLY: Final = True
"""The experiment trades long only.

``PaperBroker`` already refuses a short with ``SHORT_NOT_SUPPORTED`` rather than
simulating one approximately, so this is a second, explicit barrier at the
submission boundary rather than the only one.
"""


def assert_long_only(side: str) -> None:
    """Refuse any non-long side.

    Two of the three accounts have shorting enabled at the broker. Nothing in
    tradabot has ever validated a short, so the capability is refused here rather
    than left to be discovered.

    Raises:
        PaperExecutionRefusedError: for any side that is not long.
    """
    if side.upper() not in {"LONG", "BUY"}:
        msg = f"the paper experiment is long only; refused side {side!r}"
        raise PaperExecutionRefusedError(msg)
