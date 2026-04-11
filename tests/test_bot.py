import asyncio
import contextlib
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot import TrendBot6
from src.binance_private_stream import BinancePrivateUserStream
from src.binance_market_data import BinancePublicMarketStream
from src.exchange_errors import ExchangeAPIError
from src.binance_rest import BinanceRestClient
from src.config import BotConfig
from src.models import Balance, InstrumentMeta
from src.models import BookLevel, BookSnapshot
from src.utils import build_cl_ord_id, now_ms


class StubJournal:
    def __init__(self):
        self.events = []

    def append(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


def make_book(*, bid: str, ask: str, ts_ms: int) -> BookSnapshot:
    return BookSnapshot(
        ts_ms=ts_ms,
        received_ms=ts_ms,
        bids=[BookLevel(price=Decimal(bid), size=Decimal("100000"))],
        asks=[BookLevel(price=Decimal(ask), size=Decimal("100000"))],
    )


def test_public_reconnect_resyncs_without_immediate_cancel(tmp_path):
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
        await bot._on_reconnect("public_books5")
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    assert cancel_calls == []
    assert bot.state.resync_required is True
    assert bot.state.resync_reason == "public_books5 reconnected"
    assert bot.state.pause_until_ms > 0
    assert ("reconnect", {"stream": "public_books5"}) in bot.journal.events
    assert (
        "public_reconnect_resync_only",
        {
            "stream": "public_books5",
            "immediate_cancel": False,
            "configured_cancel_on_public_reconnect": False,
        },
    ) in bot.journal.events


def test_private_account_update_refreshes_stream_activity(tmp_path):
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    previous_activity_ms = now_ms() - 60_000
    bot.state.mark_stream_activity("private_user", previous_activity_ms)

    try:
        asyncio.run(
            bot._on_account(
                {
                    "details": [
                        {
                            "ccy": "USDC",
                            "cashBal": "10",
                            "availBal": "10",
                        }
                    ]
                }
            )
        )
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.stream_last_activity_ms["private_user"] > previous_activity_ms


def test_profit_density_window_prefers_sqlite_over_journal_scan(tmp_path):
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.live.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.live.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    Path(config.telemetry.journal_path).write_text("{not-json}\n", encoding="utf-8")
    conn = sqlite3.connect(config.telemetry.sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                event TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                runtime_state TEXT,
                run_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        payload_buy = {
            "reason_bucket": "entry",
            "order": {
                "cl_ord_id": "buy-1",
                "side": "buy",
                "price": "1.0000",
                "filled_size": "100",
            },
            "raw": {"fillPx": "1.0000"},
        }
        payload_sell = {
            "reason_bucket": "entry",
            "order": {
                "cl_ord_id": "sell-1",
                "side": "sell",
                "price": "1.0001",
                "filled_size": "100",
            },
            "raw": {"fillPx": "1.0001"},
        }
        conn.execute(
            "INSERT INTO audit_events (ts_ms, event, payload_json) VALUES (?, ?, ?)",
            (now_ms() - 1_000, "order_update", json.dumps(payload_buy)),
        )
        conn.execute(
            "INSERT INTO audit_events (ts_ms, event, payload_json) VALUES (?, ?, ?)",
            (now_ms(), "order_update", json.dumps(payload_sell)),
        )
        conn.commit()
    finally:
        conn.close()

    bot = TrendBot6(config)

    try:
        turnover, realized = bot._compute_profit_density_window(reason_bucket="entry", window_minutes=60)
        by_side = bot._compute_profit_density_window_by_opening_side(reason_bucket="entry", window_minutes=60)
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert turnover == Decimal("200.0100")
    assert realized == Decimal("0.0100")
    assert by_side["buy"] == (Decimal("100.0000"), Decimal("0.0100"))
    assert by_side["sell"] == (Decimal("100.0100"), Decimal("0"))


def test_bot_refresh_fee_uses_runtime_fee_without_zero_fee_override(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.risk.zero_fee_instruments = ["USDC-USDT"]

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    async def fake_fetch_trade_fee(inst_type: str, inst_id: str) -> dict:
        assert inst_type == "SPOT"
        assert inst_id == "USDC-USDT"
        return {
            "maker": Decimal("0.0005"),
            "taker": Decimal("0.0010"),
            "feeType": "level_based",
        }

    bot.rest.fetch_trade_fee = fake_fetch_trade_fee  # type: ignore[method-assign]

    try:
        asyncio.run(bot._refresh_fee(force=True))
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.fee_snapshot is not None
    assert bot.state.fee_snapshot.maker == Decimal("0.0005")
    assert bot.state.fee_snapshot.taker == Decimal("0.0010")
    assert bot.state.fee_snapshot.effective_maker == Decimal("0.0005")
    assert bot.state.fee_snapshot.effective_taker == Decimal("0.0010")
    assert bot.state.fee_snapshot.zero_fee_override is False


def test_live_market_gate_blocks_observe_only_pair(tmp_path):
    config = BotConfig(mode="live")
    config.trading.inst_id = "DAI-USDT"
    config.trading.base_ccy = "DAI"
    config.trading.quote_ccy = "USDT"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    try:
        allowed = bot._check_live_market_gate()
    finally:
        bot.audit_store.close()

    assert allowed is False
    assert bot.state.runtime_state == "STOPPED"
    assert bot.state.runtime_reason == "observe-only instrument blocked in live mode: DAI-USDT"
    assert (
        "startup_market_gate_blocked",
        {
            "inst_id": "DAI-USDT",
            "reason": "observe-only instrument blocked in live mode: DAI-USDT",
            "live_allowed_instruments": ["USDC-USDT", "USDG-USDT"],
            "observe_only_instruments": ["DAI-USDT", "PYUSD-USDT"],
        },
    ) in bot.journal.events


def test_live_market_gate_allows_core_pair(tmp_path):
    config = BotConfig(mode="live")
    config.trading.inst_id = "USDG-USDT"
    config.trading.base_ccy = "USDG"
    config.trading.quote_ccy = "USDT"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)

    try:
        allowed = bot._check_live_market_gate()
    finally:
        bot.audit_store.close()

    assert allowed is True
    assert bot.state.runtime_state == "INIT"


def test_live_budget_gate_blocks_when_instance_budget_exceeds_account_balance(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.trading.budget_base_total = Decimal("60000")
    config.trading.budget_quote_total = Decimal("5000")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    try:
        allowed = bot._check_live_budget_gate()
    finally:
        bot.audit_store.close()

    assert allowed is False
    assert bot.state.runtime_state == "STOPPED"
    assert "instance budget exceeds account balance" in bot.state.runtime_reason
    assert (
        "startup_budget_gate_blocked",
        {
            "inst_id": "USDC-USDT",
            "reason": "instance budget exceeds account balance: USDC budget 60000 exceeds exchange total 50000",
            "budget_base_total": Decimal("60000"),
            "budget_quote_total": Decimal("5000"),
            "exchange_base_total": Decimal("50000"),
            "exchange_quote_total": Decimal("50000"),
        },
    ) in bot.journal.events


def test_live_budget_gate_clamps_binance_main_leg_budget_to_exchange_balance(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.trading.budget_base_total = Decimal("1200")
    config.trading.budget_quote_total = Decimal("900")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("590"), available=Decimal("590")),
            "USDT": Balance(ccy="USDT", total=Decimal("5374.05183473"), available=Decimal("5374.05183473")),
        }
    )

    try:
        allowed = bot._check_live_budget_gate()
    finally:
        bot.audit_store.close()

    assert allowed is True
    assert bot.state.runtime_state == "INIT"
    assert bot.state.balance_budget_caps["USD1"] == Decimal("590")
    assert bot.state.budget_total_balance("USD1") == Decimal("590")
    assert any(event == "startup_budget_gate_clamped" for event, _ in bot.journal.events)


def test_live_budget_gate_clamps_binance_main_leg_even_when_startup_recovery_is_enabled(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.trading.budget_base_total = Decimal("590")
    config.trading.budget_quote_total = Decimal("900")
    config.risk.startup_recovery_enabled = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("500"), available=Decimal("410"), frozen=Decimal("90")),
            "USDT": Balance(ccy="USDT", total=Decimal("5764.00083473"), available=Decimal("5464.06083473"), frozen=Decimal("299.94")),
        }
    )

    try:
        allowed = bot._check_live_budget_gate()
    finally:
        bot.audit_store.close()

    assert allowed is True
    assert bot.state.startup_recovery_side == ""
    assert bot.state.balance_budget_caps["USD1"] == Decimal("500")
    assert any(event == "startup_budget_gate_clamped" for event, _ in bot.journal.events)


def test_live_budget_gate_allows_release_only_instance_to_idle_with_low_inventory(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDC"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDC"
    config.trading.budget_base_total = Decimal("500")
    config.trading.budget_quote_total = Decimal("500")
    config.strategy.release_only_mode = True
    config.strategy.release_only_base_buffer = Decimal("150")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("0"), available=Decimal("0")),
            "USDC": Balance(ccy="USDC", total=Decimal("0"), available=Decimal("0")),
        }
    )

    try:
        allowed = bot._check_live_budget_gate()
    finally:
        bot.audit_store.close()

    assert allowed is True
    assert bot.state.runtime_state == "INIT"
    assert (
        "startup_budget_gate_release_only_bypassed",
        {
            "inst_id": "USD1-USDC",
            "budget_base_total": Decimal("500"),
            "budget_quote_total": Decimal("500"),
            "exchange_base_total": Decimal("0"),
            "exchange_quote_total": Decimal("0"),
        },
    ) in bot.journal.events


def test_live_budget_gate_allows_sell_side_startup_recovery_when_quote_budget_is_blocked(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USDC-USDT"
    config.trading.base_ccy = "USDC"
    config.trading.quote_ccy = "USDT"
    config.trading.budget_base_total = Decimal("3200")
    config.trading.budget_quote_total = Decimal("3000")
    config.risk.startup_recovery_enabled = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("7134.83"), available=Decimal("7134.83")),
            "USDT": Balance(ccy="USDT", total=Decimal("167.71"), available=Decimal("167.71")),
        }
    )
    bot.state.live_position_lots.append(
        bot.state._parse_strategy_lot(
            {"qty": "2899", "price": "1.0001", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )

    try:
        allowed = bot._check_live_budget_gate()
    finally:
        bot.audit_store.close()

    assert allowed is True
    assert bot.state.startup_recovery_side == "sell"
    assert any(event == "startup_budget_gate_recovery_bypassed" for event, _ in bot.journal.events)


def test_release_bot_writes_route_ledger_event_on_release_fill(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDC"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDC"
    config.strategy.release_only_mode = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.shared_route_ledger_path = str(tmp_path / "route_ledger.jsonl")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDC",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDC",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    bot.state.set_order_reason(cl_ord_id="sell1", reason="release_external_long")

    async def run():
        await bot._on_order(
            {
                "instId": "USD1-USDC",
                "side": "sell",
                "ordId": "1",
                "clOrdId": "sell1",
                "px": "0.9996",
                "sz": "250",
                "accFillSz": "250",
                "fillPx": "0.9996",
                "state": "filled",
                "cTime": "1",
                "uTime": "2",
            }
        )
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    ledger = (tmp_path / "route_ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(ledger[-1])
    assert record["payload"]["asset"] == "USD1"
    assert record["payload"]["source_inst_id"] == "USD1-USDC"
    assert record["payload"]["fill_size"] == "250"


def test_release_only_bot_stops_when_inventory_is_fully_collected(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.strategy.release_only_mode = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDT",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    bot.state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("0"), available=Decimal("0")),
            "USDT": Balance(ccy="USDT", total=Decimal("1000"), available=Decimal("1000")),
        }
    )
    bot.state.initial_external_base_inventory = Decimal("410")
    bot.state.external_base_inventory_remaining = Decimal("0")

    try:
        stopped = bot._maybe_stop_completed_release_only_run()
    finally:
        bot.audit_store.close()

    assert stopped is True
    assert bot.state.runtime_state == "STOPPED"
    assert bot.state.runtime_reason == "release inventory fully collected"
    assert (
        "release_only_completed",
        {
            "inst_id": "USD1-USDT",
            "base_ccy": "USD1",
            "remaining_base": Decimal("0"),
            "strategy_position_base": Decimal("0"),
        },
    ) in bot.journal.events


def test_bot_stops_on_restricted_location_stream_error(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    try:
        asyncio.run(bot._on_stream_error("public_books5", RuntimeError("server rejected WebSocket connection: HTTP 451")))
    finally:
        bot.audit_store.close()

    assert bot.state.runtime_state == "STOPPED"
    assert bot.state.runtime_reason == "binance restricted location"
    assert any(event == "exchange_restricted_stop" for event, _ in bot.journal.events)


def test_bot_run_stops_on_restricted_location_tick_error(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.risk.cancel_managed_orders_on_shutdown = False
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    async def fake_bootstrap() -> None:
        bot.state.set_runtime_state("READY", "bootstrapped")

    async def fake_tick() -> None:
        raise ExchangeAPIError(
            path="/api/v3/account",
            msg="Service unavailable from a restricted location according to 'b. Eligibility'",
            status_code=451,
        )

    bot._bootstrap = fake_bootstrap  # type: ignore[method-assign]
    bot._tick = fake_tick  # type: ignore[method-assign]

    try:
        asyncio.run(bot.run())
    finally:
        bot.audit_store.close()

    assert bot.state.runtime_state == "STOPPED"
    assert bot.state.runtime_reason == "binance restricted location"
    assert any(event == "tick_error" for event, _ in bot.journal.events)
    assert any(event == "exchange_restricted_stop" for event, _ in bot.journal.events)


def test_bot_consumes_route_ledger_and_reduces_matching_long_inventory(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.shared_route_ledger_path = str(tmp_path / "route_ledger.jsonl")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDT",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    bot.state.live_position_lots.append(
        bot.state._parse_strategy_lot(
            {"qty": "800", "price": "0.9994", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    (tmp_path / "route_ledger.jsonl").write_text(
        json.dumps(
            {
                "ts_ms": 1,
                "payload": {
                    "asset": "USD1",
                    "source_inst_id": "USD1-USDC",
                    "side": "sell",
                    "fill_size": "250",
                    "fill_price": "0.9996",
                    "reason": "release_external_long",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        bot._consume_route_ledger_events()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.strategy_position_base() == Decimal("550")
    assert bot.state.live_realized_pnl_quote == Decimal("0.05")
    assert any(event == "triangle_route_ledger_applied" for event, _ in bot.journal.events)


def test_bot_refreshes_entry_profit_density_signal_from_recent_journal(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.strategy.entry_profit_density_enabled = True
    config.strategy.entry_profit_density_window_minutes = 60
    config.strategy.entry_profit_density_soft_per10k = Decimal("0.15")
    config.strategy.entry_profit_density_hard_per10k = Decimal("0.05")
    config.strategy.entry_profit_density_soft_size_factor = Decimal("0.70")
    config.strategy.entry_profit_density_hard_size_factor = Decimal("0.40")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.live.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    journal_path = Path(config.telemetry.journal_path)
    current_ts = now_ms()
    journal_path.write_text(
        "\n".join(
            [
                json.dumps({"ts_ms": current_ts - 1_000, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "e1", "side": "buy", "price": "1", "filled_size": "1000"}, "raw": {"fillPx": "1"}, "reason": "join_best_bid", "reason_bucket": "entry"}}),
                json.dumps({"ts_ms": current_ts, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "e2", "side": "sell", "price": "1.00001", "filled_size": "1000"}, "raw": {"fillPx": "1.00001"}, "reason": "join_best_ask", "reason_bucket": "entry"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bot = TrendBot6(config)
    bot.state.set_instrument(
        InstrumentMeta(
            inst_id="USDC-USDT",
            inst_type="SPOT",
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )

    try:
        bot._refresh_entry_profit_density_signal()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.entry_profit_density_per10k is not None
    assert bot.state.entry_profit_density_per10k < Decimal("0.15")
    assert bot.state.entry_profit_density_size_factor == Decimal("0.40")


def test_bot_refreshes_side_specific_entry_profit_density_signal_from_recent_journal(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.strategy.entry_profit_density_enabled = True
    config.strategy.entry_profit_density_window_minutes = 60
    config.strategy.entry_profit_density_soft_per10k = Decimal("0.15")
    config.strategy.entry_profit_density_hard_per10k = Decimal("0.05")
    config.strategy.entry_profit_density_soft_size_factor = Decimal("0.70")
    config.strategy.entry_profit_density_hard_size_factor = Decimal("0.40")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.live.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    journal_path = Path(config.telemetry.journal_path)
    current_ts = now_ms()
    journal_path.write_text(
        "\n".join(
            [
                json.dumps({"ts_ms": current_ts - 3_000, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "buy-open", "side": "buy", "price": "1.0001", "filled_size": "1000"}, "raw": {"fillPx": "1.0001"}, "reason": "join_best_bid", "reason_bucket": "entry"}}),
                json.dumps({"ts_ms": current_ts - 2_000, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "sell-close-buy", "side": "sell", "price": "1.0000", "filled_size": "1000"}, "raw": {"fillPx": "1.0000"}, "reason": "join_best_ask", "reason_bucket": "entry"}}),
                json.dumps({"ts_ms": current_ts - 1_000, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "sell-open", "side": "sell", "price": "1.0001", "filled_size": "1000"}, "raw": {"fillPx": "1.0001"}, "reason": "join_best_ask", "reason_bucket": "entry"}}),
                json.dumps({"ts_ms": current_ts, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "buy-close-sell", "side": "buy", "price": "1.0000", "filled_size": "1000"}, "raw": {"fillPx": "1.0000"}, "reason": "join_best_bid", "reason_bucket": "entry"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bot = TrendBot6(config)

    try:
        bot._refresh_entry_profit_density_signal()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.entry_profit_density_per10k == Decimal("0")
    assert bot.state.entry_profit_density_size_factor == Decimal("0.40")
    assert bot.state.entry_profit_density_per10k_for_side("buy") < Decimal("0")
    assert bot.state.entry_profit_density_size_factor_for_side("buy") == Decimal("0.40")
    assert bot.state.entry_profit_density_per10k_for_side("sell") > Decimal("0.15")
    assert bot.state.entry_profit_density_size_factor_for_side("sell") == Decimal("1")


def test_bot_refreshes_rebalance_profit_density_signal_from_recent_journal(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.strategy.rebalance_profit_density_enabled = True
    config.strategy.rebalance_profit_density_window_minutes = 60
    config.strategy.rebalance_profit_density_soft_per10k = Decimal("0.15")
    config.strategy.rebalance_profit_density_hard_per10k = Decimal("0.05")
    config.strategy.rebalance_profit_density_soft_size_factor = Decimal("0.70")
    config.strategy.rebalance_profit_density_hard_size_factor = Decimal("0.40")
    config.strategy.rebalance_profit_density_soft_extra_ticks = 1
    config.strategy.rebalance_profit_density_hard_extra_ticks = 2
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.live.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    journal_path = Path(config.telemetry.journal_path)
    current_ts = now_ms()
    journal_path.write_text(
        "\n".join(
            [
                json.dumps({"ts_ms": current_ts - 3_700_000, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "r1", "side": "buy", "price": "1", "filled_size": "1000"}, "raw": {"fillPx": "1"}, "reason": "rebalance_open_short", "reason_bucket": "rebalance"}}),
                json.dumps({"ts_ms": current_ts, "run_id": "run1", "event": "order_update", "payload": {"order": {"cl_ord_id": "r2", "side": "sell", "price": "1.000001", "filled_size": "1000"}, "raw": {"fillPx": "1.000001"}, "reason": "rebalance_open_long", "reason_bucket": "rebalance"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bot = TrendBot6(config)

    try:
        bot._refresh_rebalance_profit_density_signal()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.rebalance_profit_density_per10k is not None
    assert bot.state.rebalance_profit_density_per10k < Decimal("0.05")
    assert bot.state.rebalance_profit_density_size_factor == Decimal("0.40")
    assert bot.state.rebalance_profit_density_extra_ticks == 2


def test_release_bot_reads_shared_inventory_from_companion_state(tmp_path):
    companion_state = tmp_path / "usd1usdt_state.json"
    companion_state.write_text(
        json.dumps(
            {
                "instrument": {"inst_id": "USD1-USDT", "base_ccy": "USD1", "quote_ccy": "USDT"},
                "strategy_position_base": "935",
                "triangle_exit_route_choice": {
                    "primary_route": "sell_usd1usdc_then_sell_usdcusdt",
                    "backup_route": "direct_sell_usd1usdt",
                    "improvement_bp": "0.30",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDC"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDC"
    config.strategy.release_only_mode = True
    config.strategy.release_only_shared_state_paths = [str(companion_state)]
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)

    try:
        bot._refresh_shared_release_inventory()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.shared_release_inventory_base == Decimal("935")


def test_release_bot_ignores_companion_state_when_direct_route_is_preferred(tmp_path):
    companion_state = tmp_path / "usd1usdt_state.json"
    companion_state.write_text(
        json.dumps(
            {
                "instrument": {"inst_id": "USD1-USDT", "base_ccy": "USD1", "quote_ccy": "USDT"},
                "strategy_position_base": "935",
                "triangle_exit_route_choice": {
                    "primary_route": "direct_sell_usd1usdt",
                    "backup_route": "sell_usd1usdc_then_sell_usdcusdt",
                    "improvement_bp": "0.30",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDC"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDC"
    config.strategy.release_only_mode = True
    config.strategy.release_only_shared_state_paths = [str(companion_state)]
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)

    try:
        bot._refresh_shared_release_inventory()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.shared_release_inventory_base == Decimal("0")


def test_bot_refreshes_triangle_route_diagnostics_and_journals_change(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.strategy.triangle_routing_enabled = True
    config.strategy.triangle_strict_dual_exit_edge_bp = Decimal("0.10")
    config.strategy.triangle_best_exit_edge_bp = Decimal("0.20")
    config.strategy.triangle_max_worst_exit_loss_bp = Decimal("0.50")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    current_ts = now_ms()
    bot.state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDT",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    bot.state.set_book(make_book(bid="0.9997", ask="0.9998", ts_ms=current_ts))
    bot.state.set_triangle_route_snapshot(
        {
            "checked_at_ms": current_ts,
            "quotes": {
                "USDC-USDT": {"bid": Decimal("1.0001"), "ask": Decimal("1.0002")},
                "USD1-USDT": {"bid": Decimal("0.9997"), "ask": Decimal("0.9998")},
                "USD1-USDC": {"bid": Decimal("0.9999"), "ask": Decimal("1.0000")},
            },
        }
    )
    bot.state.set_triangle_exit_route_choice(
        {
            "direction": "sell",
            "primary_route": "sell_usd1usdc_then_sell_usdcusdt",
            "backup_route": "direct_sell_usd1usdt",
            "improvement_bp": Decimal("0.40"),
        }
    )
    bot.state.live_position_lots.append(
        bot.state._parse_strategy_lot({"qty": "800", "price": "0.9996", "ts_ms": 1, "cl_ord_id": "lot1"})
    )

    try:
        bot._refresh_triangle_route_diagnostics()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    diagnostics = bot.state.triangle_route_diagnostics
    assert diagnostics is not None
    assert diagnostics["snapshot_status"] == "ready"
    assert diagnostics["route_status"] == "indirect_preferred"
    assert diagnostics["entry_buy_gate_status"] == "allowed"
    assert diagnostics["entry_buy_gate_reason"] in {"strict_edge_ok", "best_edge_ok"}
    assert any(event == "triangle_route_diagnostics" for event, _ in bot.journal.events)


def test_refresh_triangle_route_snapshot_uses_best_bid_ask_for_auxiliary_pairs(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.strategy.triangle_routing_enabled = True
    config.strategy.triangle_route_refresh_interval_seconds = 0
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.state.set_book(make_book(bid="0.9997", ask="0.9998", ts_ms=1))
    calls: list[list[str]] = []

    async def fake_fetch_best_bid_ask_many(inst_ids: list[str]):
        calls.append(inst_ids)
        return {
            "USDC-USDT": make_book(bid="1.0001", ask="1.0002", ts_ms=2),
            "USD1-USDC": make_book(bid="0.9999", ask="1.0000", ts_ms=2),
        }

    async def fake_fetch_order_book(inst_id: str, depth: int):
        raise AssertionError(f"unexpected depth fetch for {inst_id} depth={depth}")

    bot.rest.fetch_best_bid_ask_many = fake_fetch_best_bid_ask_many  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]

    try:
        asyncio.run(bot._refresh_triangle_route_snapshot_if_due())
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(bot.rest.close())
        bot.audit_store.close()

    snapshot = bot.state.triangle_route_snapshot
    assert snapshot is not None
    assert calls == [["USDC-USDT", "USD1-USDC"]]
    assert snapshot["quotes"]["USD1-USDT"]["bid"] == Decimal("0.9997")
    assert snapshot["quotes"]["USDC-USDT"]["ask"] == Decimal("1.0002")
    assert snapshot["quotes"]["USD1-USDC"]["bid"] == Decimal("0.9999")


def test_refresh_triangle_route_snapshot_backs_off_after_failure(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.strategy.triangle_routing_enabled = True
    config.strategy.triangle_route_refresh_interval_seconds = 5
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_book(make_book(bid="0.9997", ask="0.9998", ts_ms=1))
    calls: list[list[str]] = []

    async def fake_fetch_best_bid_ask_many(inst_ids: list[str]):
        calls.append(inst_ids)
        raise RuntimeError(f"down:{','.join(inst_ids)}")

    bot.rest.fetch_best_bid_ask_many = fake_fetch_best_bid_ask_many  # type: ignore[method-assign]

    try:
        asyncio.run(bot._refresh_triangle_route_snapshot_if_due())
        asyncio.run(bot._refresh_triangle_route_snapshot_if_due())
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert calls == [["USDC-USDT", "USD1-USDC"]]
    assert any(event == "triangle_route_refresh_error" for event, _ in bot.journal.events)
    assert bot._last_triangle_route_refresh_ms > 0


def test_refresh_triangle_route_snapshot_batches_auxiliary_pairs_for_binance(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.strategy.triangle_routing_enabled = True
    config.strategy.triangle_route_refresh_interval_seconds = 0
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.state.set_book(make_book(bid="0.9997", ask="0.9998", ts_ms=1))
    calls: list[list[str]] = []

    async def fake_fetch_best_bid_ask_many(inst_ids: list[str]):
        calls.append(inst_ids)
        return {
            "USDC-USDT": make_book(bid="1.0001", ask="1.0002", ts_ms=2),
            "USD1-USDC": make_book(bid="0.9999", ask="1.0000", ts_ms=2),
        }

    async def fake_fetch_best_bid_ask(inst_id: str):
        raise AssertionError(f"unexpected single fetch for {inst_id}")

    async def fake_fetch_order_book(inst_id: str, depth: int):
        raise AssertionError(f"unexpected depth fetch for {inst_id} depth={depth}")

    bot.rest.fetch_best_bid_ask_many = fake_fetch_best_bid_ask_many  # type: ignore[method-assign]
    bot.rest.fetch_best_bid_ask = fake_fetch_best_bid_ask  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]

    try:
        asyncio.run(bot._refresh_triangle_route_snapshot_if_due())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    snapshot = bot.state.triangle_route_snapshot
    assert snapshot is not None
    assert calls == [["USDC-USDT", "USD1-USDC"]]
    assert snapshot["quotes"]["USD1-USDT"]["ask"] == Decimal("0.9998")
    assert snapshot["quotes"]["USDC-USDT"]["bid"] == Decimal("1.0001")
    assert snapshot["quotes"]["USD1-USDC"]["ask"] == Decimal("1.0000")


def test_refresh_triangle_exit_route_choice_uses_configured_indirect_threshold(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.strategy.triangle_routing_enabled = True
    config.strategy.triangle_prefer_indirect_min_improvement_bp = Decimal("8")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDT",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    bot.state.set_book(make_book(bid="0.9995", ask="0.9996", ts_ms=1))
    bot.state.live_position_lots.append(
        bot.state._parse_strategy_lot({"qty": "800", "price": "0.9994", "ts_ms": 1, "cl_ord_id": "lot1"})
    )
    bot.state.set_triangle_route_snapshot(
        {
            "checked_at_ms": now_ms(),
            "quotes": {
                "USDC-USDT": {"bid": Decimal("1.0006"), "ask": Decimal("1.0007")},
                "USD1-USDT": {"bid": Decimal("0.9995"), "ask": Decimal("0.9996")},
                "USD1-USDC": {"bid": Decimal("0.9998"), "ask": Decimal("0.9999")},
            },
        }
    )

    try:
        bot._refresh_triangle_exit_route_choice()
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.triangle_exit_route_choice is not None
    assert bot.state.triangle_exit_route_choice["primary_route"] == "direct_sell_usd1usdt"


def test_binance_bot_uses_binance_clients(monkeypatch, tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.exchange.binance_env = "testnet"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.stop_request_path = str(tmp_path / "stop.request")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    async def fake_sync_time_offset():
        return None

    async def fake_fetch_instrument(inst_id: str, inst_type: str):
        return InstrumentMeta(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
            state="live",
        )

    async def fake_fetch_order_book(inst_id: str, depth: int):
        return make_book(bid="1.0000", ask="1.0001", ts_ms=1)

    async def fake_fetch_balances(ccys):
        return {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }

    async def fake_fetch_trade_fee(inst_type: str, inst_id: str):
        return {"maker": Decimal("0"), "taker": Decimal("0"), "feeType": ""}

    async def fake_bootstrap_pending_orders():
        return None

    async def fake_stream_start(self):
        return None

    bot.rest.sync_time_offset = fake_sync_time_offset  # type: ignore[method-assign]
    bot.rest.fetch_instrument = fake_fetch_instrument  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]
    bot.rest.fetch_trade_fee = fake_fetch_trade_fee  # type: ignore[method-assign]
    bot.executor.bootstrap_pending_orders = fake_bootstrap_pending_orders  # type: ignore[method-assign]
    monkeypatch.setattr("src.binance_market_data.BinancePublicMarketStream.start", fake_stream_start)
    monkeypatch.setattr("src.binance_private_stream.BinancePrivateUserStream.start", fake_stream_start)

    try:
        asyncio.run(bot._bootstrap())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert isinstance(bot.rest, BinanceRestClient)
    assert isinstance(bot.public_stream, BinancePublicMarketStream)
    assert isinstance(bot.private_stream, BinancePrivateUserStream)
    assert bot.executor.trade_client is bot.rest


def test_binance_bootstrap_refreshes_pending_orders_after_startup_cleanup(monkeypatch, tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.exchange.binance_env = "testnet"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.stop_request_path = str(tmp_path / "stop.request")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    pending_calls = {"count": 0}
    cancel_calls: list[str] = []
    buy_id = build_cl_ord_id(config.managed_prefix, "buy")
    sell_id = build_cl_ord_id(config.managed_prefix, "sell")

    async def fake_sync_time_offset():
        return None

    async def fake_fetch_instrument(inst_id: str, inst_type: str):
        return InstrumentMeta(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
            state="live",
        )

    async def fake_fetch_order_book(inst_id: str, depth: int):
        return make_book(bid="1.0000", ask="1.0001", ts_ms=1)

    async def fake_fetch_balances(ccys):
        return {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }

    async def fake_fetch_trade_fee(inst_type: str, inst_id: str):
        return {"maker": Decimal("0"), "taker": Decimal("0"), "feeType": ""}

    async def fake_list_pending_orders(inst_id: str, inst_type: str):
        pending_calls["count"] += 1
        if pending_calls["count"] == 1:
            return [
                {
                    "instId": inst_id,
                    "side": "buy",
                    "ordId": "b1",
                    "clOrdId": buy_id,
                    "px": "0.9998",
                    "sz": "500",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "1",
                },
                {
                    "instId": inst_id,
                    "side": "sell",
                    "ordId": "s1",
                    "clOrdId": sell_id,
                    "px": "1.0002",
                    "sz": "500",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "1",
                },
            ]
        return []

    async def fake_cancel_order(*, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None, req_id: str | None = None, inst_id_code: str | None = None):
        del inst_id, ord_id, req_id, inst_id_code
        cancel_calls.append(str(cl_ord_id or ""))
        return {}

    async def fake_stream_start(self):
        return None

    bot.rest.sync_time_offset = fake_sync_time_offset  # type: ignore[method-assign]
    bot.rest.fetch_instrument = fake_fetch_instrument  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]
    bot.rest.fetch_trade_fee = fake_fetch_trade_fee  # type: ignore[method-assign]
    bot.rest.list_pending_orders = fake_list_pending_orders  # type: ignore[method-assign]
    bot.rest.cancel_order = fake_cancel_order  # type: ignore[method-assign]
    monkeypatch.setattr("src.binance_market_data.BinancePublicMarketStream.start", fake_stream_start)
    monkeypatch.setattr("src.binance_private_stream.BinancePrivateUserStream.start", fake_stream_start)

    try:
        asyncio.run(bot._bootstrap())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert cancel_calls == [buy_id, sell_id]
    assert pending_calls["count"] == 2
    assert bot.state.live_orders == {}


def test_okx_bootstrap_reconciles_startup_cleanup_until_pending_orders_clear(monkeypatch, tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "okx"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.stop_request_path = str(tmp_path / "stop.request")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    pending_calls = {"count": 0}
    cancel_calls: list[str] = []
    sleep_calls: list[float] = []
    sell_id = build_cl_ord_id(config.managed_prefix, "sell")

    async def fake_sync_time_offset():
        return None

    async def fake_fetch_instrument(inst_id: str, inst_type: str):
        return InstrumentMeta(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
            inst_id_code="648",
            state="live",
        )

    async def fake_fetch_order_book(inst_id: str, depth: int):
        return make_book(bid="1.0000", ask="1.0001", ts_ms=1)

    async def fake_fetch_balances(ccys):
        return {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }

    async def fake_fetch_trade_fee(inst_type: str, inst_id: str):
        return {"maker": Decimal("0"), "taker": Decimal("0"), "feeType": ""}

    async def fake_list_pending_orders(inst_id: str, inst_type: str):
        pending_calls["count"] += 1
        if pending_calls["count"] < 3:
            return [
                {
                    "instId": inst_id,
                    "side": "sell",
                    "ordId": "s1",
                    "clOrdId": sell_id,
                    "px": "1.0002",
                    "sz": "500",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "1",
                }
            ]
        return []

    async def fake_cancel_order(*, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None, req_id: str | None = None, inst_id_code: str | None = None):
        del inst_id, ord_id, req_id, inst_id_code
        cancel_calls.append(str(cl_ord_id or ""))
        return {}

    async def fake_stream_start(self):
        return None

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)

    bot.rest.sync_time_offset = fake_sync_time_offset  # type: ignore[method-assign]
    bot.rest.fetch_instrument = fake_fetch_instrument  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]
    bot.rest.fetch_trade_fee = fake_fetch_trade_fee  # type: ignore[method-assign]
    bot.rest.list_pending_orders = fake_list_pending_orders  # type: ignore[method-assign]
    bot.rest.cancel_order = fake_cancel_order  # type: ignore[method-assign]
    monkeypatch.setattr("src.market_data.PublicBookStream.start", fake_stream_start)
    monkeypatch.setattr("src.private_stream.PrivateUserStream.start", fake_stream_start)
    monkeypatch.setattr("src.executor.asyncio.sleep", fake_sleep)

    try:
        asyncio.run(bot._bootstrap())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert cancel_calls == [sell_id]
    assert pending_calls["count"] == 3
    assert sleep_calls == [0.2]
    assert bot.state.live_orders == {}


def test_binance_bootstrap_cleans_managed_orders_before_budget_gate_blocks(monkeypatch, tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.exchange.binance_env = "testnet"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.stop_request_path = str(tmp_path / "stop.request")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    pending_calls = {"count": 0}
    cancel_calls: list[str] = []
    buy_id = build_cl_ord_id(config.managed_prefix, "buy")

    async def fake_sync_time_offset():
        return None

    async def fake_fetch_instrument(inst_id: str, inst_type: str):
        return InstrumentMeta(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
            state="live",
        )

    async def fake_fetch_order_book(inst_id: str, depth: int):
        return make_book(bid="1.0000", ask="1.0001", ts_ms=1)

    async def fake_fetch_balances(ccys):
        return {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }

    async def fake_list_pending_orders(inst_id: str, inst_type: str):
        pending_calls["count"] += 1
        if pending_calls["count"] == 1:
            return [
                {
                    "instId": inst_id,
                    "side": "buy",
                    "ordId": "b1",
                    "clOrdId": buy_id,
                    "px": "0.9998",
                    "sz": "500",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "1",
                }
            ]
        return []

    async def fake_cancel_order(*, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None, req_id: str | None = None, inst_id_code: str | None = None):
        del inst_id, ord_id, req_id, inst_id_code
        cancel_calls.append(str(cl_ord_id or ""))
        return {}

    bot.rest.sync_time_offset = fake_sync_time_offset  # type: ignore[method-assign]
    bot.rest.fetch_instrument = fake_fetch_instrument  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]
    bot.rest.list_pending_orders = fake_list_pending_orders  # type: ignore[method-assign]
    bot.rest.cancel_order = fake_cancel_order  # type: ignore[method-assign]
    bot._check_live_budget_gate = lambda: False  # type: ignore[method-assign]

    try:
        asyncio.run(bot._bootstrap())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert cancel_calls == [buy_id]
    assert pending_calls["count"] == 2
    assert bot.state.live_orders == {}


def test_simulated_live_bootstrap_keeps_private_ws_routing_for_usdc(monkeypatch, tmp_path):
    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    async def fake_sync_time_offset():
        return None

    async def fake_fetch_instrument(inst_id: str, inst_type: str):
        return InstrumentMeta(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy="USDC",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
            inst_id_code="648",
            state="live",
        )

    async def fake_fetch_order_book(inst_id: str, depth: int):
        return make_book(bid="1.0000", ask="1.0001", ts_ms=1)

    async def fake_fetch_balances(ccys):
        return {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }

    async def fake_fetch_trade_fee(inst_type: str, inst_id: str):
        return {"maker": Decimal("0"), "taker": Decimal("0"), "feeType": ""}

    async def fake_stream_start(self):
        return None

    bot.rest.sync_time_offset = fake_sync_time_offset  # type: ignore[method-assign]
    bot.rest.fetch_instrument = fake_fetch_instrument  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]
    bot.rest.fetch_trade_fee = fake_fetch_trade_fee  # type: ignore[method-assign]
    bot.executor.bootstrap_pending_orders = fake_sync_time_offset  # type: ignore[method-assign]
    monkeypatch.setattr("src.market_data.PublicBookStream.start", fake_stream_start)
    monkeypatch.setattr("src.private_stream.PrivateUserStream.start", fake_stream_start)

    try:
        asyncio.run(bot._bootstrap())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.private_stream is not None
    assert bot.executor.trade_client is bot.private_stream
    assert (
        "simulated_trade_routing",
        {
            "trade_client": "private_ws",
            "reason": "simulated instrument allows private ws trading",
            "inst_id": "USDC-USDT",
        },
    ) in bot.journal.events


def test_simulated_live_bootstrap_keeps_rest_trade_routing_for_usdg(monkeypatch, tmp_path):
    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.trading.inst_id = "USDG-USDT"
    config.trading.base_ccy = "USDG"
    config.trading.quote_ccy = "USDT"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    async def fake_sync_time_offset():
        return None

    async def fake_fetch_instrument(inst_id: str, inst_type: str):
        return InstrumentMeta(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy="USDG",
            quote_ccy="USDT",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
            inst_id_code="213767",
            state="live",
        )

    async def fake_fetch_order_book(inst_id: str, depth: int):
        return make_book(bid="1.0000", ask="1.0001", ts_ms=1)

    async def fake_fetch_balances(ccys):
        return {
            "USDG": Balance(ccy="USDG", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }

    async def fake_fetch_trade_fee(inst_type: str, inst_id: str):
        return {"maker": Decimal("0"), "taker": Decimal("0"), "feeType": ""}

    async def fake_stream_start(self):
        return None

    bot.rest.sync_time_offset = fake_sync_time_offset  # type: ignore[method-assign]
    bot.rest.fetch_instrument = fake_fetch_instrument  # type: ignore[method-assign]
    bot.rest.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]
    bot.rest.fetch_trade_fee = fake_fetch_trade_fee  # type: ignore[method-assign]
    bot.executor.bootstrap_pending_orders = fake_sync_time_offset  # type: ignore[method-assign]
    monkeypatch.setattr("src.market_data.PublicBookStream.start", fake_stream_start)
    monkeypatch.setattr("src.private_stream.PrivateUserStream.start", fake_stream_start)

    try:
        asyncio.run(bot._bootstrap())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.private_stream is not None
    assert bot.executor.trade_client is bot.rest
    assert (
        "simulated_trade_routing",
        {
            "trade_client": "rest",
            "reason": "configured simulated instrument uses rest trading fallback",
            "inst_id": "USDG-USDT",
        },
    ) in bot.journal.events


def test_on_order_ignores_foreign_instrument_updates(tmp_path):
    config = BotConfig(mode="live")
    config.trading.inst_id = "USDC-USDT"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    try:
        asyncio.run(
            bot._on_order(
                {
                    "instId": "USDG-USDT",
                    "side": "buy",
                    "ordId": "foreign-1",
                    "clOrdId": "bot7mb123",
                    "px": "1",
                    "sz": "1000",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "1",
                }
            )
        )
    finally:
        bot.audit_store.close()

    assert bot.state.live_orders == {}
    assert (
        "order_update_ignored_foreign_inst",
        {
            "inst_id": "USDG-USDT",
            "expected_inst_id": "USDC-USDT",
            "cl_ord_id": "bot7mb123",
        },
    ) in bot.journal.events


def test_bot_detects_stop_request_file_and_marks_stopped(tmp_path):
    stop_path = tmp_path / "stop.request"
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.telemetry.stop_request_path = str(stop_path)

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    stop_path.write_text("stop", encoding="utf-8")

    try:
        stopped = bot._check_stop_request()
    finally:
        bot.audit_store.close()

    assert stopped is True
    assert bot.state.runtime_state == "STOPPED"
    assert "stop requested" in bot.state.runtime_reason
    assert (
        "stop_requested",
        {
            "inst_id": "USDC-USDT",
            "path": str(stop_path),
        },
    ) in bot.journal.events


def test_shutdown_preserves_existing_startup_gate_reason(tmp_path):
    config = BotConfig(mode="live")
    config.trading.inst_id = "DAI-USDT"
    config.trading.base_ccy = "DAI"
    config.trading.quote_ccy = "USDT"
    config.risk.cancel_managed_orders_on_shutdown = False
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)

    try:
        assert bot._check_live_market_gate() is False
        asyncio.run(bot.shutdown())
    finally:
        bot.audit_store.close()

    assert bot.state.runtime_state == "STOPPED"
    assert bot.state.runtime_reason == "observe-only instrument blocked in live mode: DAI-USDT"


def test_bot_restores_persisted_accounting_on_init(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "initial_nav_quote": "25016.5929021004943",
                "live_realized_pnl_quote": "2.2",
                "observed_fill_count": 4,
                "live_position_lots": [
                    {"qty": "-1693.170218", "price": "1.0001", "ts_ms": 1234, "cl_ord_id": "bot6ms1"}
                ],
                "initial_external_base_inventory": "23008.69913",
                "external_base_inventory_remaining": "21021.301162",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(state_path)

    bot = TrendBot6(config)
    try:
        assert bot.state.initial_nav_quote == Decimal("25016.5929021004943")
        assert bot.state.live_realized_pnl_quote == Decimal("2.2")
        assert bot.state.observed_fill_count == 4
        assert bot.state.strategy_position_base() == Decimal("-1693.170218")
        journal_text = Path(config.telemetry.journal_path).read_text(encoding="utf-8")
        assert '"event": "state_restored"' in journal_text
    finally:
        bot.audit_store.close()


def test_consistency_resync_cancels_only_offending_managed_orders(tmp_path):
    config = BotConfig(mode="live")
    config.risk.cancel_managed_on_consistency_failure = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    selective_calls: list[tuple[tuple[str, ...], str]] = []
    all_cancel_calls: list[str] = []

    async def fake_cancel_managed_orders(*, cl_ord_ids, reason: str) -> None:
        selective_calls.append((tuple(cl_ord_ids), reason))

    async def fake_cancel_all_managed_orders(*, reason: str) -> None:
        all_cancel_calls.append(reason)

    bot.executor.cancel_managed_orders = fake_cancel_managed_orders  # type: ignore[method-assign]
    bot.executor.cancel_all_managed_orders = fake_cancel_all_managed_orders  # type: ignore[method-assign]

    bot.state.set_instrument(
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
    bot.state.set_book(make_book(bid="0.9999", ask="1.0000", ts_ms=1))
    bot.state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    bad_sell_id = build_cl_ord_id("bot6", "sell")
    good_buy_id = build_cl_ord_id("bot6", "buy")
    bot.state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": bad_sell_id,
            "px": "0.9999",
            "sz": "1000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="test",
    )
    bot.state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "2",
            "clOrdId": good_buy_id,
            "px": "0.9998",
            "sz": "1000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="test",
    )

    try:
        first_ok = asyncio.run(bot._run_consistency_check(context="resync", stop_on_failure=False))
        second_ok = asyncio.run(bot._run_consistency_check(context="resync", stop_on_failure=False))
    finally:
        bot.audit_store.close()

    assert first_ok is False
    assert second_ok is False
    assert selective_calls == [((bad_sell_id,), "consistency_failure:resync")]
    assert all_cancel_calls == []


def test_consistency_resync_debounce_clears_after_success(tmp_path):
    config = BotConfig(mode="live")
    config.risk.cancel_managed_on_consistency_failure = True
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    selective_calls: list[tuple[tuple[str, ...], str]] = []

    async def fake_cancel_managed_orders(*, cl_ord_ids, reason: str) -> None:
        selective_calls.append((tuple(cl_ord_ids), reason))

    bot.executor.cancel_managed_orders = fake_cancel_managed_orders  # type: ignore[method-assign]

    bot.state.set_instrument(
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
    bot.state.set_book(make_book(bid="0.9999", ask="1.0000", ts_ms=1))
    bot.state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    bad_sell_id = build_cl_ord_id("bot6", "sell")
    bot.state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": bad_sell_id,
            "px": "0.9999",
            "sz": "1000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="test",
    )

    try:
        first_ok = asyncio.run(bot._run_consistency_check(context="resync", stop_on_failure=False))
        bot.state.set_book(make_book(bid="0.9998", ask="0.9999", ts_ms=2))
        cleared_ok = asyncio.run(bot._run_consistency_check(context="resync", stop_on_failure=False))
        bot.state.set_book(make_book(bid="0.9999", ask="1.0000", ts_ms=3))
        second_first_ok = asyncio.run(bot._run_consistency_check(context="resync", stop_on_failure=False))
    finally:
        bot.audit_store.close()

    assert first_ok is False
    assert cleared_ok is True
    assert second_first_ok is False
    assert selective_calls == []


def test_run_logs_tick_error_repr_and_traceback(tmp_path):
    config = BotConfig(mode="shadow")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()

    async def fake_bootstrap() -> None:
        bot.state.set_runtime_state("READY", "test bootstrap")

    async def fake_tick() -> None:
        raise Exception()

    async def fake_sleep(_: float) -> None:
        bot.state.set_runtime_state("STOPPED", "done")

    async def fake_shutdown() -> None:
        return None

    original_sleep = asyncio.sleep
    bot._bootstrap = fake_bootstrap  # type: ignore[method-assign]
    bot._tick = fake_tick  # type: ignore[method-assign]
    bot.shutdown = fake_shutdown  # type: ignore[method-assign]
    asyncio.sleep = fake_sleep  # type: ignore[assignment]

    try:
        asyncio.run(bot.run())
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
        bot.audit_store.close()

    tick_errors = [payload for event, payload in bot.journal.events if event == "tick_error"]
    assert len(tick_errors) == 1
    assert tick_errors[0]["error"] == ""
    assert tick_errors[0]["error_repr"] == "Exception()"
    assert tick_errors[0]["error_type"] == "Exception"
    assert "fake_tick" in tick_errors[0]["traceback"]
    assert bot.state.resync_reason == "tick failure: Exception()"


def test_refresh_balances_if_due_logs_and_skips_transient_failure(tmp_path):
    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("100"), available=Decimal("100")),
            "USDT": Balance(ccy="USDT", total=Decimal("200"), available=Decimal("200")),
        }
    )

    async def fake_fetch_balances(_: list[str]):
        raise RuntimeError("balance api down")

    bot.rest.fetch_balances = fake_fetch_balances  # type: ignore[method-assign]

    try:
        asyncio.run(bot._refresh_balances_if_due())
    finally:
        asyncio.run(bot.rest.close())
        bot.audit_store.close()

    assert bot.state.balances["USDC"].total == Decimal("100")
    assert bot.state.balances["USDT"].total == Decimal("200")
    assert bot.state.resync_required is False
    assert bot.state.pause_until_ms == 0
    assert bot._last_balance_poll_ms > 0
    assert (
        "balance_refresh_error",
        {
            "inst_id": "USDC-USDT",
            "base_ccy": "USDC",
            "quote_ccy": "USDT",
            "error_type": "RuntimeError",
            "error": "balance api down",
        },
    ) in bot.journal.events


def test_on_book_triggers_debounced_quote_cycle_when_top_price_changes(tmp_path):
    config = BotConfig(mode="shadow")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.trading.book_requote_debounce_ms = 20

    bot = TrendBot6(config)
    calls: list[tuple[str, bool]] = []
    bot.state.set_book(make_book(bid="1.0000", ask="1.0001", ts_ms=1))

    async def fake_run_quote_cycle(*, trigger: str, include_maintenance: bool) -> None:
        calls.append((trigger, include_maintenance))

    bot._run_quote_cycle = fake_run_quote_cycle  # type: ignore[method-assign]

    async def run() -> None:
        await bot._on_book(make_book(bid="0.9999", ask="1.0000", ts_ms=2))
        await asyncio.sleep(0.06)
        await bot._stop_book_requote_worker()
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    assert calls == [("book_top_price_changed", False)]


def test_on_book_debounce_coalesces_multiple_price_updates(tmp_path):
    config = BotConfig(mode="shadow")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.trading.book_requote_debounce_ms = 20

    bot = TrendBot6(config)
    calls: list[tuple[str, bool]] = []
    bot.state.set_book(make_book(bid="1.0000", ask="1.0001", ts_ms=1))

    async def fake_run_quote_cycle(*, trigger: str, include_maintenance: bool) -> None:
        calls.append((trigger, include_maintenance))

    bot._run_quote_cycle = fake_run_quote_cycle  # type: ignore[method-assign]

    async def run() -> None:
        await bot._on_book(make_book(bid="0.9999", ask="1.0000", ts_ms=2))
        await bot._on_book(make_book(bid="0.9998", ask="0.9999", ts_ms=3))
        await asyncio.sleep(0.06)
        await bot._stop_book_requote_worker()
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    assert calls == [("book_top_price_changed", False)]


def test_on_book_ignores_same_top_prices(tmp_path):
    config = BotConfig(mode="shadow")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")
    config.trading.book_requote_debounce_ms = 20

    bot = TrendBot6(config)
    calls: list[tuple[str, bool]] = []
    bot.state.set_book(make_book(bid="1.0000", ask="1.0001", ts_ms=1))

    async def fake_run_quote_cycle(*, trigger: str, include_maintenance: bool) -> None:
        calls.append((trigger, include_maintenance))

    bot._run_quote_cycle = fake_run_quote_cycle  # type: ignore[method-assign]

    async def run() -> None:
        await bot._on_book(make_book(bid="1.0000", ask="1.0001", ts_ms=2))
        await asyncio.sleep(0.06)
        await bot._stop_book_requote_worker()
        await bot.rest.close()

    try:
        asyncio.run(run())
    finally:
        bot.audit_store.close()

    assert calls == []


def test_bot_logs_ws_amend_failure_and_clears_pending(tmp_path):
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_instrument(
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
    bot.state.set_book(make_book(bid="0.9998", ask="0.9999", ts_ms=1))
    buy_id = build_cl_ord_id("bot6", "buy")
    bot.state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "0.9998",
            "sz": "10000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="test",
    )
    bot.state.register_pending_amend(
        cl_ord_id=buy_id,
        ord_id="b1",
        side="buy",
        reason="join_best_bid",
        previous_price=Decimal("0.9998"),
        previous_size=Decimal("10000"),
        previous_remaining_size=Decimal("10000"),
        target_price=Decimal("0.9999"),
        target_size=Decimal("10000"),
        target_remaining_size=Decimal("10000"),
        filled_size=Decimal("0"),
    )

    try:
        asyncio.run(
            bot._on_order(
                {
                    "instId": "USDC-USDT",
                    "side": "buy",
                    "ordId": "b1",
                    "clOrdId": buy_id,
                    "px": "0.9998",
                    "sz": "10000",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "2",
                    "code": "51511",
                    "msg": "",
                    "amendResult": "-1",
                }
            )
        )
    finally:
        bot.audit_store.close()

    assert bot.state.pending_amend(buy_id) is None
    assert bot.state.live_orders[buy_id].price == Decimal("0.9998")
    assert any(event == "amend_order_error" for event, _ in bot.journal.events)


def test_bot_logs_ws_amend_success_and_clears_pending(tmp_path):
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_instrument(
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
    bot.state.set_book(make_book(bid="0.9998", ask="0.9999", ts_ms=1))
    buy_id = build_cl_ord_id("bot6", "buy")
    bot.state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "0.9998",
            "sz": "10000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="test",
    )
    bot.state.register_pending_amend(
        cl_ord_id=buy_id,
        ord_id="b1",
        side="buy",
        reason="join_best_bid",
        previous_price=Decimal("0.9998"),
        previous_size=Decimal("10000"),
        previous_remaining_size=Decimal("10000"),
        target_price=Decimal("0.9999"),
        target_size=Decimal("10000"),
        target_remaining_size=Decimal("10000"),
        filled_size=Decimal("0"),
    )

    try:
        asyncio.run(
            bot._on_order(
                {
                    "instId": "USDC-USDT",
                    "side": "buy",
                    "ordId": "b1",
                    "clOrdId": buy_id,
                    "px": "0.9999",
                    "sz": "10000",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "2",
                    "code": "0",
                    "msg": "",
                    "amendResult": "0",
                }
            )
        )
    finally:
        bot.audit_store.close()

    assert bot.state.pending_amend(buy_id) is None
    assert bot.state.live_orders[buy_id].price == Decimal("0.9999")
    assert any(event == "amend_order" for event, _ in bot.journal.events)


def test_bot_cancels_rebalance_order_when_ws_amend_fails(tmp_path):
    config = BotConfig(mode="live")
    config.telemetry.sqlite_enabled = False
    config.telemetry.journal_path = str(tmp_path / "journal.jsonl")
    config.telemetry.sqlite_path = str(tmp_path / "audit.db")
    config.telemetry.state_path = str(tmp_path / "state.json")

    bot = TrendBot6(config)
    bot.journal = StubJournal()
    bot.state.set_instrument(
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
    bot.state.set_book(make_book(bid="0.9998", ask="0.9999", ts_ms=1))
    sell_id = build_cl_ord_id("bot6", "sell")
    bot.state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0001",
            "sz": "10000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="test",
    )
    bot.state.register_pending_amend(
        cl_ord_id=sell_id,
        ord_id="s1",
        side="sell",
        reason="rebalance_open_long",
        previous_price=Decimal("1.0001"),
        previous_size=Decimal("10000"),
        previous_remaining_size=Decimal("10000"),
        target_price=Decimal("1.0000"),
        target_size=Decimal("10000"),
        target_remaining_size=Decimal("10000"),
        filled_size=Decimal("0"),
    )
    cancel_calls: list[tuple[str, str, bool]] = []

    async def fake_cancel(order, *, reason: str, ignore_cooldown: bool = False) -> None:
        cancel_calls.append((order.cl_ord_id, reason, ignore_cooldown))

    bot.executor._cancel_order = fake_cancel  # type: ignore[method-assign]

    try:
        asyncio.run(
            bot._on_order(
                {
                    "instId": "USDC-USDT",
                    "side": "sell",
                    "ordId": "s1",
                    "clOrdId": sell_id,
                    "px": "1.0001",
                    "sz": "10000",
                    "accFillSz": "0",
                    "state": "live",
                    "cTime": "1",
                    "uTime": "2",
                    "code": "51511",
                    "msg": "",
                    "amendResult": "-1",
                }
            )
        )
    finally:
        bot.audit_store.close()

    assert cancel_calls == [(sell_id, "reprice_or_ttl", True)]
    assert any(event == "amend_rebalance_fallback_cancel" for event, _ in bot.journal.events)
