from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Balance, BookLevel, BookSnapshot, InstrumentMeta
from src.state import BotState
from src.utils import build_cl_ord_id


def test_initial_nav_waits_for_balances():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
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
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            received_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("1000"))],
        )
    )

    assert state.initial_nav_quote is None
    assert state.shadow_base_cost_quote is None

    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    assert state.initial_nav_quote == Decimal("99997.5")
    assert state.strategy_nav_peak_quote == Decimal("99997.5")
    assert state.account_total_eq_peak_quote == Decimal("99997.5")
    assert state.shadow_base_cost_quote == Decimal("49997.5")


def test_budget_caps_effective_balances_without_losing_exchange_snapshot():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.configure_balance_budgets(
        base_ccy="USDC",
        quote_ccy="USDT",
        base_total=Decimal("12000"),
        quote_total=Decimal("8000"),
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("40000"), frozen=Decimal("10000")),
        }
    )

    assert state.exchange_total_balance("USDC") == Decimal("50000")
    assert state.exchange_total_balance("USDT") == Decimal("50000")
    assert state.budget_total_balance("USDC") == Decimal("12000")
    assert state.budget_total_balance("USDT") == Decimal("8000")
    assert state.total_balance("USDC") == Decimal("12000")
    assert state.free_balance("USDC") == Decimal("12000")
    assert state.total_balance("USDT") == Decimal("8000")
    assert state.free_balance("USDT") == Decimal("8000")


def test_budgeted_balances_track_local_fills_then_clip_to_exchange_constraints():
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
    state.configure_balance_budgets(
        base_ccy="USDC",
        quote_ccy="USDT",
        base_total=Decimal("10000"),
        quote_total=Decimal("10000"),
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1",
            "fillPx": "1",
            "sz": "4000",
            "accFillSz": "4000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    assert state.budget_total_balance("USDT") == Decimal("6000")
    assert state.total_balance("USDT") == Decimal("6000")
    assert state.budget_total_balance("USDC") == Decimal("14000")
    assert state.total_balance("USDC") == Decimal("14000")

    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("12000"), available=Decimal("12000")),
            "USDT": Balance(ccy="USDT", total=Decimal("7000"), available=Decimal("7000")),
        }
    )

    assert state.budget_total_balance("USDC") == Decimal("14000")
    assert state.total_balance("USDC") == Decimal("12000")
    assert state.budget_total_balance("USDT") == Decimal("6000")
    assert state.total_balance("USDT") == Decimal("6000")


def test_live_pnl_tracks_realized_and_unrealized_from_managed_fills():
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
            ts_ms=1,
            received_ms=1,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    assert state.strategy_position_base() == Decimal("10000")
    assert state.live_realized_pnl_quote == Decimal("0")
    assert state.live_unrealized_pnl_quote() == Decimal("0.5")
    assert state.last_trade is not None
    assert state.last_trade.order_price == Decimal("1")
    assert state.last_trade.price == Decimal("1")

    sell_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "2",
            "clOrdId": sell_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "3",
            "uTime": "4",
        },
        source="test",
    )

    assert state.strategy_position_base() == Decimal("0")
    assert state.live_realized_pnl_quote == Decimal("1")
    assert state.live_unrealized_pnl_quote() == Decimal("0")


def test_state_tracks_strategy_and_account_drawdowns_separately():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
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
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            received_ms=1,
            bids=[BookLevel(price=Decimal("1.0000"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }
    )
    state.apply_account_update(
        {
            "totalEq": "33010",
            "details": [
                {"ccy": "USDC", "cashBal": "10000", "availBal": "10000"},
                {"ccy": "USDT", "cashBal": "10000", "availBal": "10000"},
            ],
        }
    )

    state.set_book(
        BookSnapshot(
            ts_ms=2,
            received_ms=2,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("1000"))],
        )
    )
    state.apply_account_update(
        {
            "totalEq": "33001.8",
            "details": [
                {"ccy": "USDC", "cashBal": "10000", "availBal": "10000"},
                {"ccy": "USDT", "cashBal": "10000", "availBal": "10000"},
            ],
        }
    )

    assert state.strategy_nav_peak_quote == Decimal("20000.5")
    assert state.strategy_drawdown_quote() == Decimal("-1")
    assert state.account_equity_quote() == Decimal("33001.8")
    assert state.account_total_eq_peak_quote == Decimal("33010")
    assert state.account_drawdown_quote() == Decimal("-8.2")


def test_state_marks_side_toxic_flow_cooldown_after_adverse_fill(monkeypatch):
    fill_ts = 1_700_000_000_000
    monkeypatch.setattr("src.state.now_ms", lambda: fill_ts + 500)
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
            ts_ms=1,
            received_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    sell_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": sell_id,
            "px": "1.0000",
            "fillPx": "1.0000",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": str(fill_ts),
            "uTime": str(fill_ts),
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            received_ms=2,
            bids=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0002"), size=Decimal("1000"))],
        )
    )

    events = state.evaluate_toxic_flow(
        min_observation_ms=300,
        max_observation_ms=1000,
        adverse_ticks=1,
        cooldown_ms=2000,
    )

    assert len(events) == 1
    assert events[0]["side"] == "sell"
    assert state.is_toxic_flow_side_cooling_down("sell")
    assert state.last_trade is not None
    assert state.last_trade.order_price == Decimal("1.0000")
    assert state.last_trade.price == Decimal("1.0000")


def test_managed_buy_fill_updates_local_balances_immediately():
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
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("40000"), frozen=Decimal("10000")),
        }
    )

    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "4000",
            "state": "partially_filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    assert state.total_balance("USDT") == Decimal("46000")
    assert state.free_balance("USDT") == Decimal("40000")
    assert state.balances["USDT"].frozen == Decimal("6000")
    assert state.total_balance("USDC") == Decimal("54000")
    assert state.free_balance("USDC") == Decimal("54000")


def test_managed_sell_fill_consumes_frozen_base_before_available():
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
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("45000"), frozen=Decimal("5000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    sell_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": sell_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "5000",
            "accFillSz": "4000",
            "state": "partially_filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    assert state.total_balance("USDC") == Decimal("46000")
    assert state.free_balance("USDC") == Decimal("45000")
    assert state.balances["USDC"].frozen == Decimal("1000")
    assert state.total_balance("USDT") == Decimal("54000.4")
    assert state.free_balance("USDT") == Decimal("54000.4")


def test_load_persisted_accounting_restores_live_state(tmp_path):
    state_path = tmp_path / "state.json"
    state = BotState(managed_prefix="bot6", state_path=str(state_path))
    state.configure_balance_budgets(
        base_ccy="USDC",
        quote_ccy="USDT",
        base_total=Decimal("12000"),
        quote_total=Decimal("12000"),
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("8800"), available=Decimal("8800")),
            "USDT": Balance(ccy="USDT", total=Decimal("15202.078"), available=Decimal("15202.078")),
        }
    )
    state._adjust_balance("USDT", total_delta=Decimal("3202.078"), available_delta=Decimal("3202.078"))
    state.initial_nav_quote = Decimal("25000")
    state.strategy_nav_peak_quote = Decimal("25006")
    state.account_total_eq_quote = Decimal("33010.5")
    state.account_total_eq_peak_quote = Decimal("33018.2")
    state.live_realized_pnl_quote = Decimal("2.2")
    state.observed_fill_count = 4
    state.observed_fill_volume_quote = Decimal("2888.8692150357")
    state.initial_external_base_inventory = Decimal("23008.69913")
    state.external_base_inventory_remaining = Decimal("21021.301162")
    state.set_triangle_route_diagnostics(
        {
            "snapshot_status": "ready",
            "route_status": "indirect_preferred",
            "entry_buy_gate_status": "allowed",
        }
    )
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "-1693.170218", "price": "1.0001", "ts_ms": 1234, "cl_ord_id": "bot6ms1"}
        )
    )
    state.persist()

    restored = BotState(managed_prefix="bot6", state_path=str(state_path))
    summary = restored.load_persisted_accounting()

    assert summary is not None
    assert restored.initial_nav_quote == Decimal("25000")
    assert restored.strategy_nav_peak_quote == Decimal("25006")
    assert restored.account_total_eq_quote == Decimal("33010.5")
    assert restored.account_total_eq_peak_quote == Decimal("33018.2")
    assert restored.live_realized_pnl_quote == Decimal("2.2")
    assert restored.observed_fill_count == 4
    assert restored.observed_fill_volume_quote == Decimal("2888.8692150357")
    assert restored.initial_external_base_inventory == Decimal("23008.69913")
    assert restored.external_base_inventory_remaining == Decimal("21021.301162")
    assert restored.budget_total_balance("USDC") == Decimal("8800")
    assert restored.budget_total_balance("USDT") == Decimal("15202.078")
    assert restored.triangle_route_diagnostics == {
        "snapshot_status": "ready",
        "route_status": "indirect_preferred",
        "entry_buy_gate_status": "allowed",
    }
    assert restored.strategy_position_base() == Decimal("-1693.170218")


def test_restored_budget_balances_survive_fresh_exchange_snapshot(tmp_path):
    state_path = tmp_path / "state.json"
    state = BotState(managed_prefix="bot6", state_path=str(state_path))
    state.configure_balance_budgets(
        base_ccy="USDC",
        quote_ccy="USDT",
        base_total=Decimal("12000"),
        quote_total=Decimal("12000"),
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("8800"), available=Decimal("8800")),
            "USDT": Balance(ccy="USDT", total=Decimal("11500"), available=Decimal("11500")),
        }
    )
    state._adjust_balance("USDT", total_delta=Decimal("2500"), available_delta=Decimal("2500"))
    state.persist()

    restored = BotState(managed_prefix="bot6", state_path=str(state_path))
    restored.configure_balance_budgets(
        base_ccy="USDC",
        quote_ccy="USDT",
        base_total=Decimal("12000"),
        quote_total=Decimal("12000"),
    )
    restored.load_persisted_accounting()
    restored.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("18730.260263"), available=Decimal("15730.260263"), frozen=Decimal("3000")),
            "USDT": Balance(ccy="USDT", total=Decimal("60433.131041030174"), available=Decimal("59832.83104103017"), frozen=Decimal("600.3")),
        }
    )

    assert restored.budget_total_balance("USDC") == Decimal("8800")
    assert restored.budget_total_balance("USDT") == Decimal("14000")
    assert restored.total_balance("USDC") == Decimal("8800")
    assert restored.total_balance("USDT") == Decimal("14000")


def test_release_tracking_sell_fill_consumes_external_inventory_before_opening_short():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDC",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDC",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    state.configure_release_tracking(enabled=True)
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            received_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("1000"), available=Decimal("1000")),
            "USDC": Balance(ccy="USDC", total=Decimal("1000"), available=Decimal("1000")),
        }
    )

    sell_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USD1-USDC",
            "side": "sell",
            "ordId": "1",
            "clOrdId": sell_id,
            "px": "1.0000",
            "fillPx": "1.0000",
            "sz": "400",
            "accFillSz": "400",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    assert state.external_base_inventory_remaining == Decimal("600")
    assert state.strategy_position_base() == Decimal("0")
    assert list(state.live_position_lots) == []


def test_release_tracking_clamps_external_inventory_to_actual_base_balance():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
        InstrumentMeta(
            inst_id="USD1-USDC",
            inst_type="SPOT",
            base_ccy="USD1",
            quote_ccy="USDC",
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    state.configure_release_tracking(enabled=True)
    state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("1000"), available=Decimal("1000")),
            "USDC": Balance(ccy="USDC", total=Decimal("1000"), available=Decimal("1000")),
        }
    )

    state.external_base_inventory_remaining = Decimal("900")
    state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("250"), available=Decimal("250")),
            "USDC": Balance(ccy="USDC", total=Decimal("1750"), available=Decimal("1750")),
        }
    )

    assert state.external_base_inventory_remaining == Decimal("250")


def test_apply_external_release_fill_consumes_positive_lots_and_realizes_pnl():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "600", "price": "0.9994", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "400", "price": "0.9995", "ts_ms": 2, "cl_ord_id": "lot2"}
        )
    )

    matched = state.apply_external_release_fill(
        fill_size=Decimal("700"),
        fill_price=Decimal("0.9996"),
    )

    assert matched == Decimal("700")
    assert state.strategy_position_base() == Decimal("300")
    assert state.live_realized_pnl_quote == Decimal("0.13")


def test_replace_live_orders_does_not_double_count_partial_fill_on_resync():
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
            ts_ms=1,
            received_ms=1,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50000"), available=Decimal("50000")),
            "USDT": Balance(ccy="USDT", total=Decimal("50000"), available=Decimal("50000")),
        }
    )

    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    sell_id = build_cl_ord_id("bot6", "sell")
    partial_payload = {
        "instId": "USDC-USDT",
        "side": "sell",
        "ordId": "2",
        "clOrdId": sell_id,
        "px": "1.0001",
        "fillPx": "1.0001",
        "sz": "10000",
        "accFillSz": "5806.096684",
        "state": "partially_filled",
        "cTime": "3",
        "uTime": "4",
    }
    state.apply_order_update(partial_payload, source="ws_order")

    assert state.strategy_position_base() == Decimal("4193.903316")

    state.replace_live_orders([partial_payload], source="rest_sync")

    assert state.strategy_position_base() == Decimal("4193.903316")
    assert sell_id in state.live_orders
    assert state.live_orders[sell_id].filled_size == Decimal("5806.096684")


def test_pending_amend_req_id_mismatch_does_not_clear_newer_pending():
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
    buy_id = build_cl_ord_id("bot6", "buy")
    order = state.apply_order_update(
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
    state.register_pending_amend(
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
        req_id="newer_req",
    )

    resolution = state.resolve_pending_amend_update(
        payload={"code": "0", "amendResult": "0", "reqId": "older_req"},
        order=order,
    )

    assert resolution is None
    assert state.pending_amend(buy_id) is not None
    assert state.pending_amend(buy_id)["req_id"] == "newer_req"


def test_apply_order_update_preserves_side_when_okx_ws_omits_side_on_amend():
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

    sell_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0003",
            "sz": "1200",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="ws_order",
    )

    updated = state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0003",
            "sz": "2000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "2",
            "reqId": "req-amend",
            "amendResult": "0",
            "code": "0",
            "msg": "",
        },
        source="ws_order",
    )

    assert updated.side == "sell"
    assert [order.cl_ord_id for order in state.bot_orders("sell")] == [sell_id]


def test_managed_fill_timestamps_persist_by_reason_bucket(tmp_path):
    state_path = tmp_path / "state.json"
    state = BotState(managed_prefix="bot6", state_path=str(state_path))
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
            ts_ms=1_700_000_000_000,
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

    sell_id = build_cl_ord_id("bot6", "sell")
    state.set_order_reason(cl_ord_id=sell_id, reason="rebalance_open_long")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "1000",
            "accFillSz": "1000",
            "state": "filled",
            "cTime": "1700000001000",
            "uTime": "1700000001000",
        },
        source="test",
    )

    assert state.last_managed_fill_ts_ms(side="sell", reason_bucket="rebalance") == 1_700_000_001_000
    assert state.managed_fill_age_ms(side="sell", reason_bucket="rebalance", reference_ms=1_700_000_061_000) == 60_000

    state.persist()

    restored = BotState(managed_prefix="bot6", state_path=str(state_path))
    restored.load_persisted_accounting()

    assert restored.last_managed_fill_ts_ms(side="sell", reason_bucket="rebalance") == 1_700_000_001_000
