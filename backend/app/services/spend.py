"""Persistent model-spend ledger and caps (gate 3).

The client's own counters bound one run. They do not bound a month: a process
that restarts, or a series of separate runs, starts each time at zero, so a
daily or monthly rupee cap written as an in-memory counter is not a cap at all.
This module keeps the spend in the database and refuses the next call when the
day or the month is already spent.

Three rules, each deliberate:

1. **Disabled by default.** With `LLM_SPEND_CAPS_ENABLED` unset the ledger is
   never opened, so unit tests and a machine without a database behave exactly
   as before.
2. **Fail closed.** If caps are enabled but no price is configured, or the
   ledger cannot be read, the call is refused rather than assumed free. A cap
   that silently stops working is worse than no cap, because the report still
   claims a budget.
3. **Reserve before HTTP.** A short serialized transaction stores an upper
   estimate before each attempt; successful usage settles that row. Unknown
   outcomes keep their reservation across restarts. A zero-token row with a
   positive cost is an unsettled conservative estimate, not a billed total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4

from sqlalchemy import func, select, text, update

from app.core.config import get_settings
from app.repositories.db import session_scope
from app.repositories.schema import llm_spend


class SpendCapReached(Exception):
    """The configured day or month budget is spent.

    Separate from a provider refusal: nobody declined the call, the operator's
    own cap stopped it. A report has to be able to say which of the two it was.
    """


class SpendLedgerUnavailable(Exception):
    """The ledger could not be read or written, so a capped run must not start."""


@dataclass(frozen=True)
class SpendTotals:
    day_inr: float
    month_inr: float
    calls_today: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def day_start(moment: Optional[datetime] = None) -> datetime:
    moment = moment or _now()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(moment: Optional[datetime] = None) -> datetime:
    moment = moment or _now()
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def call_cost_inr(prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    """What one call costs at the configured price, or None when unpriced.

    A price is a deployment fact - the provider's tariff in the operator's
    currency - so it comes from the environment and is never a constant here.
    """
    settings = get_settings()
    price_in = settings.llm_cost_input_per_mtok_inr
    price_out = settings.llm_cost_output_per_mtok_inr
    if price_in is None or price_out is None:
        return None
    return (
        (max(0, prompt_tokens) / 1_000_000) * float(price_in)
        + (max(0, completion_tokens) / 1_000_000) * float(price_out)
    )


def read_totals(session, moment: Optional[datetime] = None) -> SpendTotals:
    """Spend so far today and this month, from the stored rows."""
    moment = moment or _now()
    day_rows = session.execute(
        select(func.coalesce(func.sum(llm_spend.c.cost_inr), 0), func.count())
        .where(llm_spend.c.occurred_at >= day_start(moment))
    ).one()
    month_total = session.execute(
        select(func.coalesce(func.sum(llm_spend.c.cost_inr), 0))
        .where(llm_spend.c.occurred_at >= month_start(moment))
    ).scalar_one()
    return SpendTotals(
        day_inr=float(day_rows[0] or 0.0),
        month_inr=float(month_total or 0.0),
        calls_today=int(day_rows[1] or 0),
    )


class SpendLedger:
    """The database-backed cap a client asks before each call."""

    def __init__(self, *, session_factory=session_scope):
        self._session_factory = session_factory

    @staticmethod
    def enabled() -> bool:
        return bool(get_settings().llm_spend_caps_enabled)

    def check(self) -> None:
        """Refuse the next call when the day or month budget is spent."""
        if not self.enabled():
            return
        settings = get_settings()
        daily = float(settings.llm_daily_cap_inr or 0)
        monthly = float(settings.llm_monthly_cap_inr or 0)
        if daily <= 0 and monthly <= 0:
            raise SpendLedgerUnavailable(
                "spend caps are enabled but neither LLM_DAILY_CAP_INR nor "
                "LLM_MONTHLY_CAP_INR is set"
            )
        if call_cost_inr(1, 1) is None:
            raise SpendLedgerUnavailable(
                "spend caps are enabled but no price is configured; set "
                "LLM_COST_INPUT_PER_MTOK_INR and LLM_COST_OUTPUT_PER_MTOK_INR"
            )
        try:
            with self._session_factory() as session:
                totals = read_totals(session)
        except Exception as exc:  # noqa: BLE001 - a cap that cannot read must not allow
            raise SpendLedgerUnavailable(
                f"the spend ledger could not be read ({type(exc).__name__})"
            ) from None
        if daily > 0 and totals.day_inr >= daily:
            raise SpendCapReached(
                f"the daily cap of INR {daily:g} is spent "
                f"(INR {totals.day_inr:.2f} today, {totals.calls_today} calls)"
            )
        if monthly > 0 and totals.month_inr >= monthly:
            raise SpendCapReached(
                f"the monthly cap of INR {monthly:g} is spent "
                f"(INR {totals.month_inr:.2f} this month)"
            )

    def reserve(self, *, model, purpose, input_bound, output_bound):
        """Atomically reserve the next request's upper estimate across processes."""
        if not self.enabled():
            return None
        settings = get_settings()
        if settings.llm_daily_cap_inr <= 0 and settings.llm_monthly_cap_inr <= 0:
            raise SpendLedgerUnavailable('spend caps are enabled but no positive daily or monthly cap is configured')
        cost = call_cost_inr(input_bound, output_bound)
        if cost is None:
            raise SpendLedgerUnavailable('no price is configured for the next call')
        cost = Decimal(str(cost)).quantize(Decimal('.0001'), rounding=ROUND_CEILING)
        identifier = uuid4()
        try:
            with self._session_factory() as session:
                session.execute(text('LOCK TABLE llm_spend IN SHARE ROW EXCLUSIVE MODE'))
                totals = read_totals(session)
                for period, total, cap in [('daily',totals.day_inr,settings.llm_daily_cap_inr),
                                           ('monthly',totals.month_inr,settings.llm_monthly_cap_inr)]:
                    if cap > 0 and Decimal(str(total)) + cost > Decimal(str(cap)):
                        raise SpendCapReached(f'the {period} INR cap would be exceeded by the next call reservation')
                session.execute(llm_spend.insert().values(id=identifier,model=model,purpose=purpose,
                    prompt_tokens=0,completion_tokens=0,cost_inr=cost))
        except SpendCapReached:
            raise
        except Exception as exc:
            raise SpendLedgerUnavailable(f'the spend reservation failed ({type(exc).__name__})') from None
        return identifier

    def settle(self, identifier, *, prompt_tokens, completion_tokens):
        """Replace a reservation with usage; unknown requests keep their bound."""
        cost = call_cost_inr(prompt_tokens, completion_tokens)
        if cost is None:
            raise SpendLedgerUnavailable('the answered call has no configured price')
        cost = Decimal(str(cost)).quantize(Decimal('.0001'), rounding=ROUND_CEILING)
        try:
            with self._session_factory() as session:
                session.execute(update(llm_spend).where(llm_spend.c.id==identifier).values(
                    prompt_tokens=prompt_tokens,completion_tokens=completion_tokens,cost_inr=cost))
        except Exception as exc:
            raise SpendLedgerUnavailable(f'the spend usage could not be settled ({type(exc).__name__})') from None

    def record(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        purpose: str = "text",
        run_id=None,
    ) -> float:
        """Write one billable call; returns the cost it was charged at."""
        if not self.enabled():
            return 0.0
        cost = call_cost_inr(prompt_tokens, completion_tokens)
        if cost is None:
            raise SpendLedgerUnavailable(
                "no price is configured, so a call cannot be recorded against a cap"
            )
        try:
            with self._session_factory() as session:
                session.execute(
                    llm_spend.insert().values(
                        model=model,
                        purpose=purpose,
                        prompt_tokens=int(prompt_tokens),
                        completion_tokens=int(completion_tokens),
                        cost_inr=cost,
                        run_id=run_id,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - an unrecorded call must stop the run
            raise SpendLedgerUnavailable(
                f"the spend ledger could not be written ({type(exc).__name__})"
            ) from None
        return cost

    def totals(self) -> SpendTotals:
        """What has been spent so far, for a report or a health check."""
        with self._session_factory() as session:
            return read_totals(session)
