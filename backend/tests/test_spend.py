"""Persistent spend caps: the ledger refuses before the call, and records after it.

Every number here is synthetic. Nothing in this file talks to a provider.
"""

from __future__ import annotations

import pytest
from contextlib import nullcontext

from app.core.config import Settings, get_settings
from app.services import spend
from app.services.llm import LLMBudgetExceeded, LLMClient
from app.services.spend import SpendCapReached, SpendLedger, SpendLedgerUnavailable


def _settings(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # A ledger test must never inherit a real tariff or cap from the developer's
    # .env: the assertions below are about arithmetic, not about a deployment.
    for key in (
        "LLM_SPEND_CAPS_ENABLED",
        "LLM_SPEND_TRACKING_ENABLED",
        "LLM_DAILY_CAP_INR",
        "LLM_MONTHLY_CAP_INR",
        "LLM_COST_INPUT_PER_MTOK_INR",
        "LLM_COST_OUTPUT_PER_MTOK_INR",
    ):
        # Removed, not blanked: an empty string is not a valid boolean or
        # number, and the point is to test the defaults, not a parse failure.
        monkeypatch.delenv(key, raising=False)
    # delenv cannot stop pydantic reading the developer's .env file itself.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cost_uses_the_configured_price(monkeypatch):
    _settings(
        monkeypatch,
        LLM_COST_INPUT_PER_MTOK_INR=100,
        LLM_COST_OUTPUT_PER_MTOK_INR=400,
    )
    # 1M in at 100 plus 0.5M out at 400 = 100 + 200.
    assert spend.call_cost_inr(1_000_000, 500_000) == pytest.approx(300.0)


def test_an_unpriced_call_has_no_cost(monkeypatch):
    _settings(monkeypatch, LLM_COST_INPUT_PER_MTOK_INR=100)
    assert spend.call_cost_inr(10, 10) is None


def test_the_ledger_is_off_by_default_and_never_touches_the_database(monkeypatch):
    """A machine without a database must behave exactly as before."""
    _settings(monkeypatch, LLM_SPEND_CAPS_ENABLED="false")

    def explode():  # pragma: no cover - a call here is the failure
        raise AssertionError("the disabled ledger opened a session")

    ledger = SpendLedger(session_factory=explode)
    ledger.check()
    assert ledger.record(model="synthetic", purpose="text",
                         prompt_tokens=10, completion_tokens=5) == 0.0


def test_enabled_caps_without_a_price_refuse_the_call(monkeypatch):
    _settings(monkeypatch, LLM_SPEND_CAPS_ENABLED="true", LLM_DAILY_CAP_INR=10)
    with pytest.raises(SpendLedgerUnavailable, match="no price"):
        SpendLedger(session_factory=lambda: None).check()


def test_enabled_caps_without_a_limit_refuse_the_call(monkeypatch):
    _settings(
        monkeypatch,
        LLM_SPEND_CAPS_ENABLED="true",
        LLM_COST_INPUT_PER_MTOK_INR=100,
        LLM_COST_OUTPUT_PER_MTOK_INR=400,
    )
    with pytest.raises(SpendLedgerUnavailable, match="neither"):
        SpendLedger(session_factory=lambda: None).check()


def test_a_spent_day_cap_refuses_before_any_work(monkeypatch):
    _settings(
        monkeypatch,
        LLM_SPEND_CAPS_ENABLED="true",
        LLM_DAILY_CAP_INR=10,
        LLM_MONTHLY_CAP_INR=1000,
        LLM_COST_INPUT_PER_MTOK_INR=100,
        LLM_COST_OUTPUT_PER_MTOK_INR=400,
    )
    monkeypatch.setattr(
        spend, "read_totals",
        lambda session: spend.SpendTotals(day_inr=10.0, month_inr=12.0, calls_today=4),
    )
    with pytest.raises(SpendCapReached, match="daily cap"):
        SpendLedger(session_factory=lambda: nullcontext(None)).check()


def test_a_spent_month_cap_refuses_before_any_work(monkeypatch):
    _settings(
        monkeypatch,
        LLM_SPEND_CAPS_ENABLED="true",
        LLM_DAILY_CAP_INR=1000,
        LLM_MONTHLY_CAP_INR=50,
        LLM_COST_INPUT_PER_MTOK_INR=100,
        LLM_COST_OUTPUT_PER_MTOK_INR=400,
    )
    monkeypatch.setattr(
        spend, "read_totals",
        lambda session: spend.SpendTotals(day_inr=1.0, month_inr=50.0, calls_today=1),
    )
    with pytest.raises(SpendCapReached, match="monthly cap"):
        SpendLedger(session_factory=lambda: nullcontext(None)).check()


def test_the_client_turns_a_cap_into_a_budget_stop(monkeypatch):
    """A cap is a stop, not an extraction failure: silence and refusal differ."""
    _settings(monkeypatch, LLM_MODEL="synthetic-model",
              LLM_API_KEY="synthetic-key",
              LLM_BASE_URL="https://synthetic.example.test/v1")

    class Reached:
        def reserve(self, **kwargs):
            raise SpendCapReached("the daily cap of INR 10 is spent")

        def record(self, **_kwargs):  # pragma: no cover - never reached
            raise AssertionError("a refused call must not be recorded")

    client = LLMClient()
    client.ledger = Reached()
    with pytest.raises(LLMBudgetExceeded, match="daily cap"):
        client.chat("synthetic system", "synthetic user")


def test_the_client_stops_when_the_ledger_cannot_be_enforced(monkeypatch):
    _settings(monkeypatch, LLM_MODEL="synthetic-model",
              LLM_API_KEY="synthetic-key",
              LLM_BASE_URL="https://synthetic.example.test/v1")

    class Unreadable:
        def reserve(self, **kwargs):
            raise SpendLedgerUnavailable("the spend ledger could not be read")

        def record(self, **_kwargs):  # pragma: no cover
            raise AssertionError("unreachable")

    client = LLMClient()
    client.ledger = Unreadable()
    with pytest.raises(LLMBudgetExceeded, match="could not be enforced"):
        client.chat("synthetic system", "synthetic user")



def test_tracking_without_caps_records_even_when_old_caps_are_spent(monkeypatch):
    _settings(monkeypatch,LLM_SPEND_CAPS_ENABLED='false',LLM_SPEND_TRACKING_ENABLED='true',
              LLM_DAILY_CAP_INR=1,LLM_MONTHLY_CAP_INR=1,
              LLM_COST_INPUT_PER_MTOK_INR=100,LLM_COST_OUTPUT_PER_MTOK_INR=400)
    monkeypatch.setattr(spend,'read_totals',lambda session:spend.SpendTotals(100,100,10))
    statements=[]
    class Session:
        def execute(self,statement):statements.append(statement)
    ledger=SpendLedger(session_factory=lambda:nullcontext(Session()))
    ledger.check()
    identifier=ledger.reserve(model='synthetic',purpose='matching',input_bound=100,output_bound=50)
    assert identifier is not None
    ledger.settle(identifier,prompt_tokens=80,completion_tokens=20)
    assert len(statements)==3 and statements[1].is_insert and statements[2].is_update


def test_tracking_without_caps_still_requires_a_price(monkeypatch):
    _settings(monkeypatch,LLM_SPEND_CAPS_ENABLED='false',LLM_SPEND_TRACKING_ENABLED='true')
    with pytest.raises(SpendLedgerUnavailable,match='price'):
        SpendLedger(session_factory=lambda:None).check()
