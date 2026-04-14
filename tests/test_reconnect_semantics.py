import asyncio
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot import TrendBot6
from src.config import BotConfig, RiskConfig, TradingConfig
from src.executor import OrderExecutor
from src.models import Balance, BookLevel, BookSnapshot, InstrumentMeta, LiveOrder, RiskStatus
from src.okx_auth import OKXSigner
from src.private_stream import PrivateUserStream
from src.state import BotState
from src.utils import now_ms


class StubJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def append(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class StubRest:
    async def cancel_order(self, **kwargs):
        return None

    async def place_limit_order(self, **kwargs):
        return {"ordId": "fake_ord_123"}

    async def close(self):
        return None


async def _noop_private_payload(payload):
    del payload


def make_state() -> BotState:
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
        InstrumentMeta(
            inst_id="USDC-USDT",
            inst_type="SPOT",
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    state.set_book(
        BookSnapshot(
            ts_ms=now_ms(),
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )
    state.set_stream_status("public_books5", True)
    state.set_stream_status("private_user", True)
    return state


def inject_live_order(state: BotState, *, side: str = "sell") -> LiveOrder:
    order = LiveOrder(
        inst_id="USDC-USDT",
        side=side,
        ord_id="fake_ord",
        cl_ord_id=f"bot6-{side}",
        price=Decimal("1.0001") if side == "sell" else Decimal("0.9999"),
        size=Decimal("10000"),
        filled_size=Decimal("0"),
        state="live",
        created_at_ms=now_ms() - 5_000,
        updated_at_ms=now_ms(),
        source="test",
    )
    state.live_orders[order.cl_ord_id] = order
    return order


def make_executor(*, cancel_orders_on_stale_book: bool = False, cancel_on_public_reconnect: bool = False) -> tuple[OrderExecutor, BotState]:
    config = BotConfig(mode="live")
    config.risk.cancel_orders_on_stale_book = cancel_orders_on_stale_book
    config.risk.cancel_managed_orders_on_public_reconnect = cancel_on_public_reconnect
    state = make_state()
    executor = OrderExecutor(
        rest=StubRest(),
        state=state,
        config=config,
        journal=StubJournal(),
    )
    return executor, state


def test_public_reconnect_cancels_managed_orders_when_configured(tmp_path):
    config = BotConfig(mode="live")
    config.risk.cancel_managed_orders_on_public_reconnect = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    cancel_calls: list[str] = []

    async def fake_cancel_all_managed_orders(*, reason: str) -> None:
        cancel_calls.append(reason)

    bot.executor.cancel_all_managed_orders = fake_cancel_all_managed_orders  # type: ignore[method-assign]

    async def run() -> None:
        await bot._on_reconnect("public_books5")
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    assert cancel_calls == ["public_reconnect"]
    assert (
        "reconnect_cancel_all_managed_orders",
        {
            "stream": "public_books5",
            "cancel_reason": "public_reconnect",
        },
    ) in bot.journal.events


def test_private_reconnect_always_cancels_managed_orders(tmp_path):
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    cancel_calls: list[str] = []

    async def fake_cancel_all_managed_orders(*, reason: str) -> None:
        cancel_calls.append(reason)

    bot.executor.cancel_all_managed_orders = fake_cancel_all_managed_orders  # type: ignore[method-assign]

    async def run() -> None:
        await bot._on_reconnect("private_user")
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    assert cancel_calls == ["private_reconnect"]
    assert (
        "reconnect_cancel_all_managed_orders",
        {
            "stream": "private_user",
            "cancel_reason": "private_reconnect",
        },
    ) in bot.journal.events


def test_executor_falls_back_to_rest_when_private_ws_is_not_authenticated():
    executor, _ = make_executor()
    stream = PrivateUserStream(
        url="wss://example.invalid/ws/private",
        signer=OKXSigner(api_key="k", secret_key="s", passphrase="p"),
        time_offset_ms=0,
        inst_type="SPOT",
        on_order=_noop_private_payload,
        on_account=_noop_private_payload,
    )
    stream.ws = object()
    executor.attach_trade_client(stream)

    assert executor._trade_client() is executor.rest


def test_risk_blocks_stale_private_stream_in_live_mode():
    from src.risk import RiskManager

    risk = RiskManager(RiskConfig(stale_book_ms=1), TradingConfig(), mode="live")
    state = make_state()
    state.mark_stream_activity("private_user", now_ms() - 10)

    status = risk.evaluate(state)

    assert status.ok is False
    assert "stale private stream" in status.reason
    assert status.runtime_state == "PAUSED"


def test_executor_does_not_keep_orders_during_stale_public_when_cancel_enabled():
    executor, state = make_executor(cancel_orders_on_stale_book=True)
    order = inject_live_order(state)

    keep = executor._should_keep_order_without_intent(
        primary=order,
        risk_status=RiskStatus(
            ok=False,
            reason="stale public stream: 1500ms",
            allow_bid=False,
            allow_ask=False,
            runtime_state="PAUSED",
        ),
    )

    assert keep is False


def test_executor_does_not_keep_orders_during_stale_private_stream():
    executor, state = make_executor()
    order = inject_live_order(state)

    keep = executor._should_keep_order_without_intent(
        primary=order,
        risk_status=RiskStatus(
            ok=False,
            reason="stale private stream: 1500ms",
            allow_bid=False,
            allow_ask=False,
            runtime_state="PAUSED",
        ),
    )

    assert keep is False


def test_executor_does_not_keep_orders_during_public_reconnect_resync_when_cancel_enabled():
    executor, state = make_executor(cancel_on_public_reconnect=True)
    order = inject_live_order(state)

    keep = executor._should_keep_order_without_intent(
        primary=order,
        risk_status=RiskStatus(
            ok=False,
            reason="resync required: public_books5 reconnected",
            allow_bid=False,
            allow_ask=False,
            runtime_state="PAUSED",
        ),
    )

    assert keep is False
