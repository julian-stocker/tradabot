"""Three paper accounts, one strategy. The isolation has to be provable.

The whole experiment rests on capital being the only difference between the
accounts. That makes two failure modes catastrophic and invisible: credentials
crossing between accounts, and one account's state leaking into another's
sizing. Both would produce a perfectly plausible results table.

Every credential here is fake.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.broker import paper_accounts
from app.broker.paper_accounts import (
    PAPER_BASE_URL_DEFAULT,
    PaperAccountCredentials,
    PaperAccountRegistry,
    PaperAccountSlot,
    PaperAccountState,
    PaperExecutionRefusedError,
    assert_may_submit,
    classify_endpoint,
    scoped_idempotency_key,
    verify_account,
    verify_all,
)

FAKE_ENV = {
    "ALPACA_PAPER_1K_API_KEY": "PKFAKE1K",
    "ALPACA_PAPER_1K_API_SECRET": "s1k",
    "ALPACA_PAPER_3K_API_KEY": "PKFAKE3K",
    "ALPACA_PAPER_3K_API_SECRET": "s3k",
    "ALPACA_PAPER_10K_API_KEY": "PKFAKE10K",
    "ALPACA_PAPER_10K_API_SECRET": "s10k",
}


class FakeAccount:
    def __init__(
        self,
        number="PA123",
        equity="1000",
        cash="1000",
        buying="1000",
        status="ACTIVE",
        blocked=False,
    ):
        self.account_number = number
        self.equity, self.cash, self.buying_power = equity, cash, buying
        self.status, self.currency = status, "USD"
        self.trading_blocked = blocked
        self.account_blocked = blocked


class FakeClient:
    def __init__(self, account=None, positions=(), orders=(), raises=False):
        self._a, self._p, self._o, self._raises = (
            account or FakeAccount(),
            positions,
            orders,
            raises,
        )

    def get_account(self):
        if self._raises:
            raise RuntimeError("unauthorized")
        return self._a

    def get_all_positions(self):
        return list(self._p)

    def get_orders(self):
        return list(self._o)


def registry(env=None, enabled=False):
    source = dict(env if env is not None else FAKE_ENV)
    source["ALPACA_PAPER_EXECUTION_ENABLED"] = "true" if enabled else "false"
    return PaperAccountRegistry.load(env=source)


def state(slot=PaperAccountSlot.PAPER_1K, **kw):
    base = {
        "slot": slot,
        "account_number": "PA1",
        "is_paper": True,
        "status": "ACTIVE",
        "currency": "USD",
        "cash": Decimal("1000"),
        "equity": Decimal("1000"),
        "buying_power": Decimal("1000"),
        "trading_blocked": False,
        "account_blocked": False,
        "open_positions": 0,
        "open_orders": 0,
    }
    base.update(kw)
    return PaperAccountState(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Registry and credential isolation
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_all_three_slots_resolve_independently(self) -> None:
        reg = registry()
        assert set(reg.accounts) == set(PaperAccountSlot)
        assert reg.missing == ()

    def test_credentials_never_cross_between_accounts(self) -> None:
        """**The gate.** One shared key would route one account into another."""
        reg = registry()
        keys = {slot: reg.credentials(slot).api_key.get_secret_value() for slot in PaperAccountSlot}
        assert len(set(keys.values())) == 3

    def test_a_missing_slot_raises_rather_than_falling_back(self) -> None:
        """**The gate.** Falling back to another slot's key is the catastrophe."""
        partial = {k: v for k, v in FAKE_ENV.items() if "10K" not in k}
        reg = registry(partial)
        assert reg.missing == (PaperAccountSlot.PAPER_10K,)
        with pytest.raises(KeyError, match="PAPER_10K"):
            reg.credentials(PaperAccountSlot.PAPER_10K)

    def test_both_naming_conventions_are_accepted(self) -> None:
        canonical = {
            "TRADABOT_ALPACA_PAPER__1K_API_KEY": "PKX",
            "TRADABOT_ALPACA_PAPER__1K_API_SECRET": "sx",
        }
        reg = registry(canonical)
        assert reg.credentials(PaperAccountSlot.PAPER_1K).source_variable.startswith("TRADABOT_")

    def test_execution_is_disabled_unless_explicitly_enabled(self) -> None:
        assert registry().execution_enabled is False
        assert registry(enabled=True).execution_enabled is True

    def test_the_default_endpoint_is_the_paper_endpoint(self) -> None:
        assert classify_endpoint(PAPER_BASE_URL_DEFAULT) == "PAPER"
        assert classify_endpoint("https://api.alpaca.markets") == "LIVE"
        assert classify_endpoint("https://example.test") == "UNKNOWN"


# ---------------------------------------------------------------------------
# Secrets never surface
# ---------------------------------------------------------------------------
class TestSecrecy:
    def test_credentials_do_not_render_their_values(self) -> None:
        """**The gate.** A credential in a traceback is a leaked credential."""
        creds = registry().credentials(PaperAccountSlot.PAPER_1K)
        for rendering in (repr(creds), str(creds), repr(registry())):
            assert "PKFAKE1K" not in rendering
            assert "s1k" not in rendering

    def test_the_registry_logs_names_never_values(self) -> None:
        source = inspect.getsource(paper_accounts.PaperAccountRegistry.load)
        assert "get_secret_value" not in source

    def test_verification_state_carries_no_credential_field(self) -> None:
        fields = set(PaperAccountState.__dataclass_fields__)
        for forbidden in ("api_key", "api_secret", "secret", "token", "credentials"):
            assert forbidden not in fields

    def test_the_module_never_persists_a_credential(self) -> None:
        source = inspect.getsource(paper_accounts).lower()
        for forbidden in ("insert into", "session.add", "commit()"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# Verification fails closed
# ---------------------------------------------------------------------------
class TestVerification:
    def creds(self, slot=PaperAccountSlot.PAPER_1K):
        return PaperAccountCredentials(
            slot,
            *(registry().credentials(slot).api_key, registry().credentials(slot).api_secret),
            "X",
        )

    def test_a_live_endpoint_is_refused_before_any_credential_is_used(self) -> None:
        """**The gate.** No key is ever sent to the live endpoint."""
        result = verify_account(self.creds(), base_url="https://api.alpaca.markets")
        assert not result.verified_paper
        assert "live endpoint" in (result.error or "")

    def test_an_authentication_failure_is_a_refusal_not_an_exception(self) -> None:
        result = verify_account(
            self.creds(), base_url=PAPER_BASE_URL_DEFAULT, client=FakeClient(raises=True)
        )
        assert not result.can_execute
        assert result.error is not None

    def test_a_paper_account_number_is_recognised(self) -> None:
        result = verify_account(
            self.creds(),
            base_url=PAPER_BASE_URL_DEFAULT,
            client=FakeClient(FakeAccount(number="PA9")),
        )
        assert result.verified_paper
        assert result.can_execute

    @pytest.mark.parametrize("kwargs", [{"status": "INACTIVE"}, {"blocked": True}, {"equity": "0"}])
    def test_any_unhealthy_account_cannot_execute(self, kwargs: dict[str, object]) -> None:
        result = verify_account(
            self.creds(), base_url=PAPER_BASE_URL_DEFAULT, client=FakeClient(FakeAccount(**kwargs))
        )  # type: ignore[arg-type]
        assert not result.can_execute

    def test_fractional_capability_is_none_when_unreported_not_true(self) -> None:
        """An assumed capability produces an order the venue rejects."""
        result = verify_account(
            self.creds(), base_url=PAPER_BASE_URL_DEFAULT, client=FakeClient(FakeAccount())
        )
        assert result.fractional_capable is None

    def test_one_failing_account_does_not_affect_the_others(self) -> None:
        """**The gate.** A partial outage must never reroute decisions."""
        report = verify_all(
            registry(),
            clients={
                PaperAccountSlot.PAPER_1K: FakeClient(FakeAccount(number="PA1")),
                PaperAccountSlot.PAPER_3K: FakeClient(raises=True),
                PaperAccountSlot.PAPER_10K: FakeClient(FakeAccount(number="PA10")),
            },
        )
        assert report.states[PaperAccountSlot.PAPER_1K].can_execute
        assert not report.states[PaperAccountSlot.PAPER_3K].can_execute
        assert report.states[PaperAccountSlot.PAPER_10K].can_execute
        assert set(report.executable_slots) == {
            PaperAccountSlot.PAPER_1K,
            PaperAccountSlot.PAPER_10K,
        }

    def test_the_module_exposes_no_order_path(self) -> None:
        source = inspect.getsource(paper_accounts).lower()
        for forbidden in ("submit_order", "place_order", "market_order_request", "cancel_order"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# The submission boundary
# ---------------------------------------------------------------------------
class TestSubmissionGate:
    def ok(self, **kw):
        base = {
            "registry": registry(enabled=True),
            "state": state(),
            "risk_permits": True,
            "quantity": Decimal("5"),
            "idempotency_key": "entry:PAPER_1K:1",
            "already_submitted": False,
        }
        base.update(kw)
        return base

    def test_a_fully_valid_request_passes_every_gate(self) -> None:
        assert_may_submit(**self.ok())

    def test_execution_disabled_refuses(self) -> None:
        """**The gate.** The default configuration cannot place an order."""
        with pytest.raises(PaperExecutionRefusedError, match="disabled"):
            assert_may_submit(**self.ok(registry=registry(enabled=False)))

    def test_a_risk_rejection_refuses(self) -> None:
        with pytest.raises(PaperExecutionRefusedError, match="risk gate"):
            assert_may_submit(**self.ok(risk_permits=False))

    def test_an_unverified_account_refuses(self) -> None:
        with pytest.raises(PaperExecutionRefusedError, match="not executable"):
            assert_may_submit(**self.ok(state=state(is_paper=False)))

    def test_a_zero_quantity_refuses(self) -> None:
        with pytest.raises(PaperExecutionRefusedError, match="sizing produced"):
            assert_may_submit(**self.ok(quantity=Decimal("0")))

    def test_a_duplicate_decision_refuses(self) -> None:
        with pytest.raises(PaperExecutionRefusedError, match="already used"):
            assert_may_submit(**self.ok(already_submitted=True))

    def test_a_non_paper_endpoint_refuses(self) -> None:
        reg = PaperAccountRegistry.load(
            env={
                **FAKE_ENV,
                "ALPACA_PAPER_EXECUTION_ENABLED": "true",
                "ALPACA_PAPER_BASE_URL": "https://api.alpaca.markets",
            }
        )
        with pytest.raises(PaperExecutionRefusedError, match="not positively identified"):
            assert_may_submit(**self.ok(registry=reg))


# ---------------------------------------------------------------------------
# Idempotency: the Phase 11.4 sharp edge, under three accounts
# ---------------------------------------------------------------------------
class TestIdempotencyScoping:
    def test_the_same_candidate_yields_a_distinct_key_per_account(self) -> None:
        """**The gate.** Phase 11.4 found ``idempotency_key`` globally unique and
        unfiltered by profile. Three accounts acting on one frozen candidate
        would collide, and the second account would silently appear to have
        traded when it had not."""
        keys = {scoped_idempotency_key(slot, 42) for slot in PaperAccountSlot}
        assert len(keys) == 3

    def test_keys_stay_deterministic_rather_than_random(self) -> None:
        """Randomising would break replay idempotency, which is the point of it."""
        assert scoped_idempotency_key(PaperAccountSlot.PAPER_1K, 7) == "entry:PAPER_1K:7"
        assert scoped_idempotency_key(PaperAccountSlot.PAPER_1K, 7) == scoped_idempotency_key(
            PaperAccountSlot.PAPER_1K, 7
        )

    def test_different_decisions_never_share_a_key_within_an_account(self) -> None:
        keys = {scoped_idempotency_key(PaperAccountSlot.PAPER_3K, i) for i in range(50)}
        assert len(keys) == 50


# ---------------------------------------------------------------------------
# One strategy, three accounts
# ---------------------------------------------------------------------------
class TestSingleStrategy:
    def test_no_per_account_opportunity_rule_exists(self) -> None:
        """**The gate.** MATCH_B_1K would make capital an alpha input."""
        source = inspect.getsource(paper_accounts)
        for forbidden in ("MATCH_B_1K", "MATCH_B_3K", "MATCH_B_10K"):
            assert forbidden not in source

    def test_the_account_module_contains_no_strategy_or_sizing(self) -> None:
        source = inspect.getsource(paper_accounts).lower()
        for forbidden in ("xs_rank", "atr", "def size_", "risk_per_trade", "order_fee"):
            assert forbidden not in source

    def test_match_b_remains_frozen(self) -> None:
        from app.research.phase12_1 import MATCH_B_DEFINITION

        assert MATCH_B_DEFINITION["version"] == "match-b-v1"
        assert MATCH_B_DEFINITION["relative_strength"] == "xs_rank_ret_20d >= 0.90"
        assert MATCH_B_DEFINITION["sector_positive"] == "sector_etf_ret_20d > 0"
        assert MATCH_B_DEFINITION["movement_sufficiency"] == "movement_to_cost >= 8.0"
        assert MATCH_B_DEFINITION["horizon_sessions"] == 3

    def test_options_data_cannot_reach_a_paper_decision(self) -> None:
        source = inspect.getsource(paper_accounts).lower()
        for forbidden in ("implied_volatility", "option_surface", "option_quote", "iv_30d"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# No leverage, long only -- the account-comparability fix
# ---------------------------------------------------------------------------
class TestEffectiveCapital:
    def test_margin_buying_power_never_raises_deployable_capital(self) -> None:
        """**The gate.** Two of the three accounts carry 4x buying power. Sizing
        against it would compare capital *and* leverage, and no difference could
        then be attributed to either."""
        margin = state(equity=Decimal("3000"), cash=Decimal("3000"), buying_power=Decimal("12000"))
        capital = paper_accounts.effective_capital(margin)
        assert capital.max_exposure == Decimal("3000")
        assert capital.usable_cash == Decimal("3000")
        assert capital.leverage_withheld == Decimal("9000")
        assert capital.leverage_ratio == Decimal("1")

    def test_a_cash_account_is_unaffected_by_the_cap(self) -> None:
        cash = state(equity=Decimal("1000"), cash=Decimal("1000"), buying_power=Decimal("1000"))
        capital = paper_accounts.effective_capital(cash)
        assert capital.max_exposure == Decimal("1000")
        assert capital.leverage_withheld == Decimal("0")

    def test_leverage_ratio_never_exceeds_one_at_any_multiplier(self) -> None:
        for multiplier in (1, 2, 4, 10):
            capped = paper_accounts.effective_capital(
                state(
                    equity=Decimal("1000"),
                    cash=Decimal("1000"),
                    buying_power=Decimal(1000 * multiplier),
                )
            )
            assert capped.leverage_ratio <= Decimal("1")

    def test_capital_scales_only_with_equity(self) -> None:
        """The experiment's independent variable, isolated."""
        exposures = [
            paper_accounts.effective_capital(
                state(equity=Decimal(e), cash=Decimal(e), buying_power=Decimal(e) * 4)
            ).max_exposure
            for e in ("1000", "3000", "10000")
        ]
        assert exposures == [Decimal("1000"), Decimal("3000"), Decimal("10000")]

    def test_settled_cash_bounds_usable_capital(self) -> None:
        """Equity can exceed cash while positions are open; sizing follows cash."""
        partial = state(equity=Decimal("1000"), cash=Decimal("250"), buying_power=Decimal("4000"))
        assert paper_accounts.effective_capital(partial).usable_cash == Decimal("250")


class TestLongOnly:
    def test_a_short_is_refused_even_though_the_broker_permits_it(self) -> None:
        """**The gate.** PAPER_3K and PAPER_10K have shorting enabled; nothing in
        tradabot has ever validated a short."""
        with pytest.raises(PaperExecutionRefusedError, match="long only"):
            paper_accounts.assert_long_only("SHORT")

    @pytest.mark.parametrize("side", ["LONG", "long", "BUY"])
    def test_long_sides_are_accepted(self, side: str) -> None:
        paper_accounts.assert_long_only(side)

    def test_the_submission_gate_refuses_a_short(self) -> None:
        with pytest.raises(PaperExecutionRefusedError, match="long only"):
            assert_may_submit(
                registry=registry(enabled=True),
                state=state(),
                risk_permits=True,
                quantity=Decimal("1"),
                idempotency_key="k",
                already_submitted=False,
                side="SHORT",
            )

    def test_the_submission_gate_refuses_a_leveraged_notional(self) -> None:
        capital = paper_accounts.effective_capital(
            state(equity=Decimal("1000"), cash=Decimal("1000"), buying_power=Decimal("4000"))
        )
        with pytest.raises(PaperExecutionRefusedError, match="no-leverage cap"):
            assert_may_submit(
                registry=registry(enabled=True),
                state=state(),
                risk_permits=True,
                quantity=Decimal("1"),
                idempotency_key="k",
                already_submitted=False,
                notional=Decimal("2500"),
                capital=capital,
            )
