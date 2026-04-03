from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import StrategyConfig, TradingConfig
from src.models import Balance, BookLevel, BookSnapshot, InstrumentMeta, RiskStatus
from src.state import BotState
from src.strategy import MicroMakerStrategy
from src.triangle_routing import build_triangle_quote_snapshot
from src.utils import build_cl_ord_id


def build_state(base_balance: str, quote_balance: str, depth_size: str = "100000") -> BotState:
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
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal(depth_size))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal(depth_size))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal(base_balance), available=Decimal(base_balance)),
            "USDT": Balance(ccy="USDT", total=Decimal(quote_balance), available=Decimal(quote_balance)),
        }
    )
    return state


def build_state_for_inst(inst_id: str, base_balance: str, quote_balance: str, *, best_bid: str, best_ask: str, depth_size: str = "100000") -> BotState:
    base_ccy, quote_ccy = inst_id.split("-")
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
        InstrumentMeta(
            inst_id=inst_id,
            inst_type="SPOT",
            base_ccy=base_ccy,
            quote_ccy=quote_ccy,
            tick_size=Decimal("0.0001"),
            lot_size=Decimal("0.000001"),
            min_size=Decimal("1"),
            max_market_amount=Decimal("1000000"),
            max_limit_amount=Decimal("20000000"),
        )
    )
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal(best_bid), size=Decimal(depth_size))],
            asks=[BookLevel(price=Decimal(best_ask), size=Decimal(depth_size))],
        )
    )
    state.set_balances(
        {
            base_ccy: Balance(ccy=base_ccy, total=Decimal(base_balance), available=Decimal(base_balance)),
            quote_ccy: Balance(ccy=quote_ccy, total=Decimal(quote_balance), available=Decimal(quote_balance)),
        }
    )
    return state


def test_strategy_quotes_both_sides_near_target_inventory():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig())
    decision = strategy.decide(
        build_state("50000", "50000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )
    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.reason == "two_sided"


def test_strategy_can_emit_two_layers_per_side_when_enabled():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(), max_orders_per_side=2)
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert len(decision.bid_layers) == 2
    assert len(decision.ask_layers) == 2
    assert decision.bid_layers[0].price == Decimal("0.9998")
    assert decision.bid_layers[1].price == Decimal("0.9997")
    assert decision.ask_layers[0].price == Decimal("1.0000")
    assert decision.ask_layers[1].price == Decimal("1.0001")
    assert decision.bid_layers[1].reason == "join_second_bid"
    assert decision.ask_layers[1].reason == "join_second_ask"


def test_strategy_join_second_layers_require_configured_edge():
    strategy = MicroMakerStrategy(
        StrategyConfig(secondary_entry_layer_min_edge_ticks=4),
        TradingConfig(),
        max_orders_per_side=2,
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert len(decision.bid_layers) == 1
    assert len(decision.ask_layers) == 1


def test_strategy_disables_second_entry_layer_when_flag_is_off():
    strategy = MicroMakerStrategy(
        StrategyConfig(secondary_layers_enabled=False),
        TradingConfig(),
        max_orders_per_side=2,
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert len(decision.bid_layers) == 1
    assert len(decision.ask_layers) == 1


def test_state_records_adverse_fill_markouts_across_observation_windows():
    state = build_state("50000", "50000")
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1.0000",
            "fillPx": "1.0000",
            "sz": "1000",
            "accFillSz": "1000",
            "state": "filled",
            "cTime": "1000",
            "uTime": "1000",
        },
        source="test",
    )

    state.set_book(
        BookSnapshot(
            ts_ms=1300,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
        )
    )
    state.evaluate_fill_markouts(reference_ms=1300)
    assert state.average_adverse_fill_markout_ticks(side="buy", window_ms=300) == Decimal("1.5")
    assert state.adverse_fill_markout_sample_count(side="buy", window_ms=1000) == 0

    state.set_book(
        BookSnapshot(
            ts_ms=2000,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    state.evaluate_fill_markouts(reference_ms=2000)
    assert state.average_adverse_fill_markout_ticks(side="buy", window_ms=1000) == Decimal("0.5")


def test_strategy_scales_entry_size_only_when_spread_is_favorable():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            favorable_size_spread_ticks=2,
            favorable_size_multiplier=Decimal("1.5"),
        ),
        TradingConfig(entry_base_size=Decimal("5000"), quote_size=Decimal("5000")),
        max_orders_per_side=2,
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "two_sided"
    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("7500")
    assert decision.ask.base_size == Decimal("7500")
    assert decision.bid_layers[1].base_size == Decimal("7500")
    assert decision.ask_layers[1].base_size == Decimal("7500")


def test_strategy_does_not_scale_entry_size_when_spread_is_one_tick():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            favorable_size_spread_ticks=2,
            favorable_size_multiplier=Decimal("1.5"),
        ),
        TradingConfig(entry_base_size=Decimal("5000"), quote_size=Decimal("5000")),
    )

    decision = strategy.decide(
        build_state("50000", "50000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "two_sided"
    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("5000")
    assert decision.ask.base_size == Decimal("5000")


def test_strategy_does_not_emit_second_entry_layer_when_spread_is_one_tick():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(), max_orders_per_side=2)
    decision = strategy.decide(
        build_state("50000", "50000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert len(decision.bid_layers) == 1
    assert len(decision.ask_layers) == 1
    assert decision.bid_layers[0].reason == "join_best_bid"
    assert decision.ask_layers[0].reason == "join_best_ask"


def test_release_only_strategy_emits_ask_only_when_external_base_exceeds_buffer():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            release_only_mode=True,
            release_only_base_buffer=Decimal("200"),
        ),
        TradingConfig(entry_base_size=Decimal("500"), quote_size=Decimal("500")),
    )
    state = build_state("1500", "500")
    state.external_base_inventory_remaining = Decimal("1200")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "release_external_sell_only"
    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.reason == "release_external_long"
    assert decision.ask.base_size == Decimal("500")


def test_release_only_strategy_stays_idle_below_external_base_buffer():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            release_only_mode=True,
            release_only_base_buffer=Decimal("1200"),
        ),
        TradingConfig(entry_base_size=Decimal("500"), quote_size=Decimal("500")),
    )
    state = build_state("1500", "500")
    state.external_base_inventory_remaining = Decimal("1100")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "release_only_idle"
    assert decision.bid is None
    assert decision.ask is None


def test_release_only_strategy_can_expand_sell_size_with_shared_inventory():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            release_only_mode=True,
            release_only_base_buffer=Decimal("150"),
        ),
        TradingConfig(inst_id="USD1-USDC", base_ccy="USD1", quote_ccy="USDC", entry_base_size=Decimal("250"), quote_size=Decimal("250")),
    )
    state = build_state_for_inst("USD1-USDC", "400", "250", best_bid="0.9995", best_ask="0.9996")
    state.external_base_inventory_remaining = Decimal("400")
    state.shared_release_inventory_base = Decimal("350")
    state.shared_release_inventory_improvement_bp = Decimal("0.30")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.ask.reason == "release_external_long"
    assert decision.ask.base_size == Decimal("600")


def test_release_only_strategy_ignores_shared_inventory_when_improvement_is_too_small():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            release_only_mode=True,
            release_only_base_buffer=Decimal("150"),
            release_only_shared_inventory_min_improvement_bp=Decimal("0.50"),
        ),
        TradingConfig(inst_id="USD1-USDC", base_ccy="USD1", quote_ccy="USDC", entry_base_size=Decimal("250"), quote_size=Decimal("250")),
    )
    state = build_state_for_inst("USD1-USDC", "400", "250", best_bid="0.9995", best_ask="0.9996")
    state.external_base_inventory_remaining = Decimal("400")
    state.shared_release_inventory_base = Decimal("350")
    state.shared_release_inventory_improvement_bp = Decimal("0.10")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("250")


def test_strategy_suppresses_direct_sell_rebalance_when_indirect_route_is_preferred():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            triangle_routing_enabled=True,
            triangle_prefer_indirect_min_improvement_bp=Decimal("0.10"),
            triangle_indirect_handoff_enabled=True,
        ),
        TradingConfig(inst_id="USD1-USDT", base_ccy="USD1", quote_ccy="USDT", entry_base_size=Decimal("800"), quote_size=Decimal("800")),
    )
    state = build_state_for_inst("USD1-USDT", "1200", "900", best_bid="0.9995", best_ask="0.9996")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "600", "price": "0.9994", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.set_triangle_exit_route_choice(
        {
            "primary_route": "sell_usd1usdc_then_sell_usdcusdt",
            "backup_route": "direct_sell_usd1usdt",
            "direction": "sell",
            "primary_reference_price": Decimal("0.9997"),
            "backup_reference_price": Decimal("0.9996"),
            "improvement_bp": Decimal("0.30"),
        }
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True),
    )

    assert decision.ask is None
    assert decision.reason == "route_indirect_release_only"


def test_strategy_suppresses_direct_sell_rebalance_for_usdc_when_indirect_route_is_preferred():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            triangle_routing_enabled=True,
            triangle_prefer_indirect_min_improvement_bp=Decimal("0.10"),
            triangle_direct_sell_floor_enabled=True,
        ),
        TradingConfig(inst_id="USDC-USDT", base_ccy="USDC", quote_ccy="USDT", entry_base_size=Decimal("1000"), quote_size=Decimal("1000")),
    )
    state = build_state_for_inst("USDC-USDT", "3200", "3000", best_bid="1.0002", best_ask="1.0003")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "600", "price": "1.0001", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.set_triangle_exit_route_choice(
        {
            "primary_route": "buy_usd1usdc_then_sell_usd1usdt",
            "backup_route": "direct_sell_usdcusdt",
            "direction": "sell",
            "primary_reference_price": Decimal("1.0005"),
            "backup_reference_price": Decimal("1.0003"),
            "improvement_bp": Decimal("0.20"),
        }
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.ask.reason == "rebalance_open_long"
    assert decision.ask.price == Decimal("1.0005")


def test_strategy_caps_direct_rebalance_buy_price_when_indirect_buy_is_cheaper():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            triangle_routing_enabled=True,
            triangle_direct_buy_ceiling_enabled=True,
        ),
        TradingConfig(inst_id="USD1-USDT", base_ccy="USD1", quote_ccy="USDT", entry_base_size=Decimal("800"), quote_size=Decimal("800")),
    )
    state = build_state_for_inst("USD1-USDT", "1200", "900", best_bid="0.9994", best_ask="0.9995")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "-600", "price": "0.9998", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.set_triangle_exit_route_choice(
        {
            "primary_route": "buy_usdcusdt_then_buy_usd1usdc",
            "backup_route": "direct_buy_usd1usdt",
            "direction": "buy",
            "primary_reference_price": Decimal("0.9992"),
            "backup_reference_price": Decimal("0.9994"),
            "improvement_bp": Decimal("0.20"),
        }
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=False),
    )

    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_open_short"
    assert decision.bid.price == Decimal("0.9992")


def test_strategy_suppresses_direct_sell_rebalance_only_when_handoff_is_enabled():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            triangle_routing_enabled=True,
            triangle_prefer_indirect_min_improvement_bp=Decimal("0.10"),
            triangle_indirect_handoff_enabled=True,
        ),
        TradingConfig(inst_id="USD1-USDT", base_ccy="USD1", quote_ccy="USDT", entry_base_size=Decimal("800"), quote_size=Decimal("800")),
    )
    state = build_state_for_inst("USD1-USDT", "1200", "900", best_bid="0.9995", best_ask="0.9996")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "600", "price": "0.9994", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.set_triangle_exit_route_choice(
        {
            "primary_route": "sell_usd1usdc_then_sell_usdcusdt",
            "backup_route": "direct_sell_usd1usdt",
            "direction": "sell",
            "primary_reference_price": Decimal("0.9997"),
            "backup_reference_price": Decimal("0.9996"),
            "improvement_bp": Decimal("0.30"),
        }
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True),
    )

    assert decision.ask is None
    assert decision.reason == "route_indirect_release_only"


def test_strategy_triangle_route_gate_blocks_low_quality_usd1_buy_entry():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            triangle_routing_enabled=True,
            triangle_strict_dual_exit_edge_bp=Decimal("0.15"),
            triangle_best_exit_edge_bp=Decimal("0.75"),
            triangle_max_worst_exit_loss_bp=Decimal("0.10"),
            triangle_indirect_leg_penalty_bp=Decimal("0.20"),
        ),
        TradingConfig(inst_id="USD1-USDT", base_ccy="USD1", quote_ccy="USDT", entry_base_size=Decimal("800"), quote_size=Decimal("800")),
    )
    state = build_state_for_inst("USD1-USDT", "1200", "1200", best_bid="0.9995", best_ask="0.9996")
    state.set_triangle_route_snapshot(
        build_triangle_quote_snapshot(
            {
                "USDC-USDT": {"bid": Decimal("1.0002"), "ask": Decimal("1.0003")},
                "USD1-USDT": {"bid": Decimal("0.9995"), "ask": Decimal("0.9996")},
                "USD1-USDC": {"bid": Decimal("0.9989"), "ask": Decimal("0.9990")},
            },
            checked_at_ms=9999999999999,
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.reason == "inventory_high_ask_only"


def test_strategy_triangle_route_gate_allows_high_quality_usd1_buy_entry():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            triangle_routing_enabled=True,
            triangle_strict_dual_exit_edge_bp=Decimal("0.15"),
            triangle_best_exit_edge_bp=Decimal("0.75"),
            triangle_max_worst_exit_loss_bp=Decimal("1.25"),
            triangle_indirect_leg_penalty_bp=Decimal("0.20"),
        ),
        TradingConfig(inst_id="USD1-USDT", base_ccy="USD1", quote_ccy="USDT", entry_base_size=Decimal("800"), quote_size=Decimal("800")),
    )
    state = build_state_for_inst("USD1-USDT", "1200", "1200", best_bid="0.9995", best_ask="0.9996")
    state.set_triangle_route_snapshot(
        build_triangle_quote_snapshot(
            {
                "USDC-USDT": {"bid": Decimal("1.0002"), "ask": Decimal("1.0003")},
                "USD1-USDT": {"bid": Decimal("0.9995"), "ask": Decimal("0.9996")},
                "USD1-USDC": {"bid": Decimal("0.9993"), "ask": Decimal("0.9994")},
            },
            checked_at_ms=9999999999999,
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None


def test_strategy_does_not_scale_overlay_size_while_rebalancing_even_if_spread_is_favorable():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            favorable_size_spread_ticks=2,
            favorable_size_multiplier=Decimal("1.5"),
        ),
        TradingConfig(entry_base_size=Decimal("10000"), quote_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "fill_rebalance_sell_biased"
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.base_size == Decimal("500.0000")


def test_strategy_uses_bot_position_to_bias_secondary_side_even_when_account_inventory_disagrees():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(entry_base_size=Decimal("5000"), quote_size=Decimal("5000")))
    state = build_state("70000", "30000")
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "2500",
            "accFillSz": "2500",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "fill_rebalance_buy_biased"
    assert decision.ask is not None
    assert decision.ask.reason == "rebalance_secondary_ask"
    assert decision.ask.base_size == Decimal("375.00000")


def test_strategy_disables_secondary_rebalance_layer_when_flag_is_off():
    strategy = MicroMakerStrategy(
        StrategyConfig(secondary_layers_enabled=False),
        TradingConfig(entry_base_size=Decimal("5000"), quote_size=Decimal("5000")),
    )
    state = build_state("70000", "30000")
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "2500",
            "accFillSz": "2500",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.reason == "fill_rebalance_buy_only"
    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_open_short"
    assert decision.ask is None


def test_strategy_strict_cycle_starts_with_buy_only():
    strategy = MicroMakerStrategy(StrategyConfig(strict_alternating_sides=True), TradingConfig(entry_base_size=Decimal("10000")))
    decision = strategy.decide(
        build_state("50000", "50000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )
    assert decision.bid is not None
    assert decision.ask is None
    assert decision.bid.base_size == Decimal("10000")
    assert decision.reason == "strict_cycle_buy_only"


def test_strategy_strict_cycle_scales_down_buy_entry_when_recent_buy_markout_is_adverse():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            strict_alternating_sides=True,
            entry_markout_window_ms=1000,
            entry_markout_trigger_samples=1,
            entry_markout_adverse_threshold_ticks=Decimal("1"),
            entry_markout_penalty_size_factor=Decimal("0.50"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state._record_markout_sample(side="buy", window_ms=1000, adverse_ticks=Decimal("1.5"))

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is None
    assert decision.bid.base_size == Decimal("5000")
    assert decision.reason == "strict_cycle_buy_only"


def test_strategy_can_use_fixed_entry_base_size_for_two_sided_quotes():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(entry_base_size=Decimal("10000")))
    decision = strategy.decide(
        build_state("50000", "50000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("10000")
    assert decision.bid.quote_notional == Decimal("9999")
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.quote_notional == Decimal("10000")
    assert decision.reason == "two_sided"


def test_strategy_scales_down_buy_entry_when_recent_buy_markout_is_adverse():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            entry_markout_window_ms=1000,
            entry_markout_trigger_samples=1,
            entry_markout_adverse_threshold_ticks=Decimal("1"),
            entry_markout_penalty_size_factor=Decimal("0.50"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state._record_markout_sample(side="buy", window_ms=1000, adverse_ticks=Decimal("1.5"))

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("5000")
    assert decision.ask.base_size == Decimal("10000")


def test_strategy_scales_down_entry_when_profit_density_is_weak():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            entry_profit_density_enabled=True,
            entry_profit_density_soft_per10k=Decimal("0.15"),
            entry_profit_density_hard_per10k=Decimal("0.05"),
            entry_profit_density_soft_size_factor=Decimal("0.70"),
            entry_profit_density_hard_size_factor=Decimal("0.40"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.entry_profit_density_per10k = Decimal("0.04")
    state.entry_profit_density_size_factor = Decimal("0.40")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("4000")
    assert decision.ask.base_size == Decimal("4000")


def test_strategy_blocks_entry_buy_when_profit_density_is_hard_weak_under_rebalance_sell_pressure():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            entry_profit_density_enabled=True,
            entry_profit_density_soft_per10k=Decimal("0.15"),
            entry_profit_density_hard_per10k=Decimal("0.05"),
            entry_profit_density_soft_size_factor=Decimal("0.70"),
            entry_profit_density_hard_size_factor=Decimal("0.40"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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
    state.entry_profit_density_per10k = Decimal("0.04")
    state.entry_profit_density_size_factor = Decimal("0.40")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.reason == "rebalance_open_long"
    assert decision.ask.base_size == Decimal("10000")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_keeps_entry_size_when_profit_density_is_healthy():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            entry_profit_density_enabled=True,
            entry_profit_density_soft_per10k=Decimal("0.15"),
            entry_profit_density_hard_per10k=Decimal("0.05"),
            entry_profit_density_soft_size_factor=Decimal("0.70"),
            entry_profit_density_hard_size_factor=Decimal("0.40"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.entry_profit_density_per10k = Decimal("0.30")
    state.entry_profit_density_size_factor = Decimal("1")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("10000")
    assert decision.ask.base_size == Decimal("10000")


def test_strategy_entry_markout_penalty_does_not_bleed_into_secondary_rebalance():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_secondary_size_factor=Decimal("0.25"),
            mild_skew_size_factor=Decimal("0"),
            secondary_min_positive_edge_ticks=1,
            secondary_full_size_edge_ticks=1,
            entry_markout_window_ms=1000,
            entry_markout_trigger_samples=1,
            entry_markout_adverse_threshold_ticks=Decimal("1"),
            entry_markout_penalty_size_factor=Decimal("0.50"),
            secondary_markout_window_ms=0,
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state._record_markout_sample(side="buy", window_ms=1000, adverse_ticks=Decimal("1.5"))
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True))

    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.base_size == Decimal("2500.00000")


def test_strategy_severe_buy_markout_disables_second_bid_layer_and_scales_entry_more():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            entry_markout_window_ms=1000,
            entry_markout_trigger_samples=1,
            entry_markout_adverse_threshold_ticks=Decimal("1"),
            entry_markout_penalty_size_factor=Decimal("0.50"),
            toxicity_severe_extra_ticks=Decimal("1"),
            toxicity_severe_size_factor=Decimal("0.25"),
            toxicity_disable_second_entry_layer=True,
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
        max_orders_per_side=2,
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    state._record_markout_sample(side="buy", window_ms=1000, adverse_ticks=Decimal("2.5"))

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.base_size == Decimal("2500")
    assert len(decision.bid_layers) == 1
    assert len(decision.ask_layers) == 2


def test_strategy_turns_into_ask_only_when_inventory_high():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig())
    decision = strategy.decide(
        build_state("80000", "20000"),
        RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )
    assert decision.bid is None
    assert decision.ask is not None
    assert decision.reason == "inventory_high_ask_only"


def test_strategy_rebalances_buy_after_selling_startup_inventory():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig())
    state = build_state("10000", "10000")
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert state.strategy_position_base() == Decimal("-10000")
    assert decision.bid is not None
    assert decision.ask is None
    assert decision.bid.base_size == Decimal("10000")
    assert decision.reason == "fill_rebalance_buy_only"


def test_strategy_strict_cycle_sells_only_after_buy_fill():
    strategy = MicroMakerStrategy(StrategyConfig(strict_alternating_sides=True), TradingConfig(entry_base_size=Decimal("10000")))
    state = build_state("50000", "50000")
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_strict_cycle_returns_to_sell_only_after_short_is_closed():
    strategy = MicroMakerStrategy(StrategyConfig(strict_alternating_sides=True), TradingConfig(entry_base_size=Decimal("10000")))
    state = build_state("50000", "50000")
    sell_id = build_cl_ord_id("bot6", "sell")
    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": sell_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "2",
            "clOrdId": buy_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "3",
            "uTime": "4",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.reason == "strict_cycle_sell_only"


def test_strategy_inventory_high_normal_sell_respects_price_floor():
    strategy = MicroMakerStrategy(
        StrategyConfig(normal_sell_price_floor=Decimal("1")),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("80000", "20000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.price == Decimal("1")
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.quote_notional == Decimal("10000")
    assert decision.reason == "inventory_high_ask_only"


def test_strategy_two_sided_quote_does_not_apply_sell_price_floor_while_bid_is_live():
    strategy = MicroMakerStrategy(
        StrategyConfig(normal_sell_price_floor=Decimal("1.0001")),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.ask.price == Decimal("0.9999")


def test_strategy_two_sided_quote_applies_buy_price_cap():
    strategy = MicroMakerStrategy(
        StrategyConfig(normal_buy_price_cap=Decimal("1")),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0003"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.price == Decimal("1")
    assert decision.bid.base_size == Decimal("10000")


def test_strategy_blocks_when_visible_depth_is_too_thin():
    strategy = MicroMakerStrategy(StrategyConfig(min_visible_depth_multiplier=Decimal("3")), TradingConfig(quote_size=Decimal("10000")))
    decision = strategy.decide(
        build_state("50000", "50000", depth_size="1000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )
    assert decision.bid is None
    assert decision.ask is None
    assert "visible depth too thin" in decision.reason


def test_strategy_uses_soft_band_to_reduce_bid_and_keep_two_sided_quotes():
    strategy = MicroMakerStrategy(StrategyConfig(account_inventory_skew_enabled=True), TradingConfig())
    decision = strategy.decide(
        build_state("70000", "30000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )
    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.price == Decimal("0.9999")
    assert decision.bid.quote_notional == Decimal("5000")
    assert decision.reason == "two_sided"


def test_strategy_keeps_full_two_sided_quotes_inside_configured_soft_band():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            account_inventory_skew_enabled=True,
            inventory_soft_lower_pct=Decimal("0.40"),
            inventory_soft_upper_pct=Decimal("0.60"),
            mild_skew_threshold_pct=Decimal("0.03"),
            mild_skew_size_factor=Decimal("0.50"),
        ),
        TradingConfig(),
    )

    decision = strategy.decide(
        build_state("58000", "42000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.price == Decimal("0.9999")
    assert decision.bid.quote_notional == Decimal("10000")
    assert decision.ask.price == Decimal("1.0000")
    assert decision.ask.quote_notional == Decimal("10000")
    assert decision.reason == "two_sided"


def test_strategy_starts_reducing_bid_after_crossing_soft_upper_band():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            account_inventory_skew_enabled=True,
            inventory_soft_lower_pct=Decimal("0.40"),
            inventory_soft_upper_pct=Decimal("0.60"),
            mild_skew_threshold_pct=Decimal("0.03"),
            mild_skew_size_factor=Decimal("0.50"),
        ),
        TradingConfig(),
    )

    decision = strategy.decide(
        build_state("63000", "37000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.price == Decimal("0.9999")
    assert Decimal("5000") < decision.bid.quote_notional < Decimal("5100")
    assert decision.ask.price == Decimal("1.0000")
    assert decision.ask.quote_notional == Decimal("10000")
    assert decision.reason == "two_sided"


def test_strategy_starts_reducing_ask_after_crossing_soft_lower_band():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            account_inventory_skew_enabled=True,
            inventory_soft_lower_pct=Decimal("0.40"),
            inventory_soft_upper_pct=Decimal("0.60"),
            mild_skew_threshold_pct=Decimal("0.03"),
            mild_skew_size_factor=Decimal("0.50"),
        ),
        TradingConfig(),
    )

    decision = strategy.decide(
        build_state("37000", "63000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.price == Decimal("0.9999")
    assert decision.bid.quote_notional == Decimal("10000")
    assert decision.ask.price == Decimal("1.0000")
    assert decision.ask.quote_notional == Decimal("5000")
    assert decision.reason == "two_sided"


def test_strategy_can_fully_suppress_ask_when_account_inventory_is_too_low():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            account_inventory_skew_enabled=True,
            inventory_soft_lower_pct=Decimal("0.45"),
            inventory_soft_upper_pct=Decimal("0.55"),
            mild_skew_threshold_pct=Decimal("0.02"),
            mild_skew_size_factor=Decimal("1"),
        ),
        TradingConfig(),
    )

    decision = strategy.decide(
        build_state("42000", "58000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.quote_notional == Decimal("10000")
    assert decision.ask.quote_notional == Decimal("0")
    assert decision.reason == "two_sided"


def test_strategy_can_fully_suppress_bid_when_account_inventory_is_too_high():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            account_inventory_skew_enabled=True,
            inventory_soft_lower_pct=Decimal("0.45"),
            inventory_soft_upper_pct=Decimal("0.55"),
            mild_skew_threshold_pct=Decimal("0.02"),
            mild_skew_size_factor=Decimal("1"),
        ),
        TradingConfig(),
    )

    decision = strategy.decide(
        build_state("58000", "42000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.quote_notional == Decimal("0")
    assert decision.ask.quote_notional == Decimal("10000")
    assert decision.reason == "two_sided"


def test_strategy_still_uses_price_skew_when_spread_exceeds_one_tick():
    strategy = MicroMakerStrategy(StrategyConfig(account_inventory_skew_enabled=True), TradingConfig())
    state = build_state("70000", "30000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.price == Decimal("0.9997")
    assert decision.ask.price == Decimal("0.9999")
    assert decision.reason == "two_sided"


def test_strategy_ignores_account_inventory_skew_by_default_when_bot_is_flat():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig())

    decision = strategy.decide(
        build_state("70000", "30000"),
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is not None
    assert decision.bid.quote_notional == Decimal("10000")
    assert decision.ask.quote_notional == Decimal("10000")
    assert decision.bid.price == Decimal("0.9999")
    assert decision.ask.price == Decimal("1.0000")
    assert decision.reason == "two_sided"


def test_strategy_uses_exact_fill_size_to_rebalance_open_long():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig())
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True))

    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.price == Decimal("0.9999")
    assert decision.bid.quote_notional == Decimal("500.0000")
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.quote_notional == Decimal("10001")
    assert decision.reason == "fill_rebalance_sell_biased"


def test_strategy_secondary_bid_scales_down_when_edge_is_thin():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_secondary_size_factor=Decimal("0.25"),
            secondary_min_positive_edge_ticks=1,
            secondary_full_size_edge_ticks=2,
            secondary_thin_edge_size_factor=Decimal("0.50"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True))

    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.base_size == Decimal("1250.00000")
    assert decision.reason == "fill_rebalance_sell_biased"


def test_strategy_secondary_bid_is_suppressed_when_edge_is_below_threshold():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_secondary_size_factor=Decimal("0.25"),
            secondary_min_positive_edge_ticks=3,
            secondary_full_size_edge_ticks=3,
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True))

    assert decision.ask is not None
    assert decision.bid is None
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_secondary_bid_is_suppressed_when_recent_buy_markout_is_adverse():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_secondary_size_factor=Decimal("0.25"),
            rebalance_secondary_price_offset_ticks=0,
            secondary_min_positive_edge_ticks=1,
            secondary_full_size_edge_ticks=2,
            secondary_markout_window_ms=1000,
            secondary_markout_trigger_samples=1,
            secondary_markout_adverse_threshold_ticks=Decimal("1"),
            secondary_markout_penalty_edge_ticks=1,
            secondary_markout_penalty_size_factor=Decimal("0.50"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1000",
            "uTime": "1000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2000,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
        )
    )
    state.evaluate_fill_markouts(reference_ms=2000)

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True))

    assert decision.ask is not None
    assert decision.bid is None
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_sell_respects_min_profit_ticks():
    strategy = MicroMakerStrategy(StrategyConfig(rebalance_min_profit_ticks=1), TradingConfig())
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"))

    assert decision.ask is not None
    assert decision.ask.price == Decimal("1.0001")
    assert decision.ask.quote_notional == Decimal("10001")


def test_strategy_rebalance_sell_ignores_normal_sell_floor_when_trade_already_has_one_tick_profit():
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, normal_sell_price_floor=Decimal("1.0001")),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"))

    assert decision.ask is not None
    assert decision.ask.price == Decimal("1")
    assert decision.ask.quote_notional == Decimal("10000")


def test_strategy_rebalance_sell_can_allow_flat_exit_when_profit_ticks_zero():
    strategy = MicroMakerStrategy(StrategyConfig(rebalance_min_profit_ticks=0), TradingConfig())
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(state, RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"))

    assert decision.ask is not None
    assert decision.ask.price == Decimal("1")


def test_strategy_rebalance_buy_downgrades_to_flat_reload_after_timeout(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is not None
    assert decision.bid.price == Decimal("0.9999")
    assert decision.ask is None
    assert decision.reason == "fill_rebalance_buy_only"


def test_strategy_rebalance_buy_downgrades_only_after_timeout_and_top_change(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is not None
    assert decision.bid.price == Decimal("1")
    assert decision.ask is None
    assert decision.reason == "fill_rebalance_buy_only"


def test_strategy_rebalance_sell_downgrades_to_flat_reload_after_timeout(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.price == Decimal("1.0001")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_sell_downgrades_only_after_timeout_and_top_change(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.price == Decimal("0.9999")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_timeout_uses_lot_age_not_last_fill(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.last_fill_ms = 1_700_000_300_000

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.price == Decimal("1.0001")


def test_strategy_rebalance_sell_does_not_move_inside_spread_after_timeout(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.price == Decimal("1.0001")


def test_strategy_rebalance_buy_does_not_move_inside_spread_after_timeout(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_300_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=120),
        TradingConfig(),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is not None
    assert decision.bid.price == Decimal("0.9999")


def test_strategy_rebalance_sell_moves_passively_inside_spread_after_timeout_and_book_change(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=5),
        TradingConfig(order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "0.9996",
            "fillPx": "0.9996",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9997"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.price == Decimal("0.9998")


def test_strategy_rebalance_buy_moves_passively_inside_spread_after_timeout_and_book_change(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=5),
        TradingConfig(order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0003"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1.0002",
            "fillPx": "1.0002",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is not None
    assert decision.bid.price == Decimal("1.0001")


def test_strategy_rebalance_buy_can_keep_small_passive_ask_when_inventory_stays_above_target():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(entry_base_size=Decimal("10000")))
    state = build_state("28000", "5000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.bid.base_size == Decimal("10000")
    assert decision.bid.price == Decimal("0.9999")
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("500.0000")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.reason == "fill_rebalance_buy_biased"


def test_strategy_rebalance_sell_uses_profitable_fifo_tranche_before_full_position():
    strategy = MicroMakerStrategy(StrategyConfig(rebalance_min_profit_ticks=1), TradingConfig(entry_base_size=Decimal("5000")))
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9997"), size=Decimal("100000"))],
        )
    )
    first_buy = build_cl_ord_id("bot6", "buy")
    second_buy = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": first_buy,
            "px": "0.9995",
            "fillPx": "0.9995",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "2",
            "clOrdId": second_buy,
            "px": "0.9998",
            "fillPx": "0.9998",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "3",
            "uTime": "4",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("5000")
    assert decision.ask.price == Decimal("0.9997")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_sell_scales_down_when_rebalance_profit_density_is_weak():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_profit_density_enabled=True,
            rebalance_profit_density_soft_size_factor=Decimal("0.50"),
            rebalance_profit_density_soft_extra_ticks=1,
        ),
        TradingConfig(entry_base_size=Decimal("5000")),
    )
    state = build_state("50000", "50000")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "10000", "price": "0.9995", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.rebalance_profit_density_size_factor = Decimal("0.50")
    state.rebalance_profit_density_extra_ticks = 1

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("5000")


def test_strategy_rebalance_buy_respects_route_aware_ceiling_and_extra_ticks():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            triangle_routing_enabled=True,
            triangle_direct_buy_ceiling_enabled=True,
            rebalance_profit_density_enabled=True,
            rebalance_profit_density_soft_extra_ticks=1,
        ),
        TradingConfig(inst_id="USD1-USDT", base_ccy="USD1", quote_ccy="USDT", entry_base_size=Decimal("800"), quote_size=Decimal("800")),
    )
    state = build_state_for_inst("USD1-USDT", "1200", "900", best_bid="0.9994", best_ask="0.9995")
    state.live_position_lots.append(
        state._parse_strategy_lot(
            {"qty": "-600", "price": "0.9998", "ts_ms": 1, "cl_ord_id": "lot1"}
        )
    )
    state.set_triangle_exit_route_choice(
        {
            "primary_route": "buy_usdcusdt_then_buy_usd1usdc",
            "backup_route": "direct_buy_usd1usdt",
            "direction": "buy",
            "primary_reference_price": Decimal("0.9992"),
            "backup_reference_price": Decimal("0.9994"),
            "improvement_bp": Decimal("0.20"),
        }
    )
    state.rebalance_profit_density_size_factor = Decimal("1")
    state.rebalance_profit_density_extra_ticks = 1

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=False),
    )

    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_open_short"
    assert decision.bid.price == Decimal("0.9992")


def test_strategy_rebalance_sell_uses_competitive_chunk_when_zero_profitable_prefix_after_reload(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=5),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )
    first_buy = build_cl_ord_id("bot6", "buy")
    second_buy = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": first_buy,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "2",
            "clOrdId": second_buy,
            "px": "0.9998",
            "fillPx": "0.9998",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000001000",
            "uTime": "1700000001000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9997"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("3750.00")
    assert decision.ask.price == Decimal("1.0000")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_sell_uses_smaller_aged_chunk_before_release(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_drift_ticks=5,
            rebalance_max_order_age_seconds=60,
            rebalance_release_size_factor=Decimal("0.50"),
        ),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )
    first_buy = build_cl_ord_id("bot6", "buy")
    second_buy = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": first_buy,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "2",
            "clOrdId": second_buy,
            "px": "0.9998",
            "fillPx": "0.9998",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000001000",
            "uTime": "1700000001000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("2500.00")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_buy_uses_profitable_fifo_tranche_before_full_position():
    strategy = MicroMakerStrategy(StrategyConfig(rebalance_min_profit_ticks=1), TradingConfig(entry_base_size=Decimal("5000")))
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0003"), size=Decimal("100000"))],
        )
    )
    first_sell = build_cl_ord_id("bot6", "sell")
    second_sell = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": first_sell,
            "px": "1.0003",
            "fillPx": "1.0003",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "2",
            "clOrdId": second_sell,
            "px": "1.0000",
            "fillPx": "1.0000",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "3",
            "uTime": "4",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is None
    assert decision.bid is not None
    assert decision.bid.base_size == Decimal("5000")
    assert decision.bid.price == Decimal("1.0002")
    assert decision.reason == "fill_rebalance_buy_only"


def test_strategy_rebalance_buy_uses_competitive_chunk_when_zero_profitable_prefix_after_reload(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(rebalance_min_profit_ticks=1, rebalance_reload_timeout_seconds=5),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
        )
    )
    first_sell = build_cl_ord_id("bot6", "sell")
    second_sell = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": first_sell,
            "px": "0.9995",
            "fillPx": "0.9995",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "2",
            "clOrdId": second_sell,
            "px": "1.0002",
            "fillPx": "1.0002",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000001000",
            "uTime": "1700000001000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0003"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_low", allow_bid=True, allow_ask=False, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is not None
    assert decision.bid.base_size == Decimal("3750.00")
    assert decision.bid.price == Decimal("0.9996")
    assert decision.reason == "fill_rebalance_buy_only"


def test_strategy_release_sell_only_uses_excess_inventory_chunk(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_max_order_age_seconds=5,
            rebalance_release_excess_only=True,
            rebalance_release_max_negative_ticks=1,
        ),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1.0002",
            "fillPx": "1.0002",
            "sz": "5407.775",
            "accFillSz": "5407.775",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("407.775")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_sell_drought_release_sell_adds_one_extra_negative_tick_on_first_window(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_001_811_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.60"),
            sell_drought_rebalance_window_seconds=1800,
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=120,
            rebalance_max_order_age_seconds=12,
            rebalance_release_excess_only=True,
            rebalance_release_max_negative_ticks=1,
            rebalance_release_depth_levels=1,
            rebalance_release_depth_fraction=Decimal("0.10"),
            rebalance_release_depth_step_bonus=Decimal("0.05"),
        ),
        TradingConfig(entry_base_size=Decimal("2000"), quote_size=Decimal("2000"), order_ttl_seconds=6),
    )
    state = build_state("14747.3273", "9252.6727")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("1.0004"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0005"), size=Decimal("100000"))],
        )
    )

    buy_id = build_cl_ord_id("bot6", "buy")
    state.set_order_reason(cl_ord_id=buy_id, reason="join_best_bid")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "1.0005",
            "fillPx": "1.0005",
            "sz": "2846.24231",
            "accFillSz": "2846.24231",
            "state": "filled",
            "cTime": "1699998150000",
            "uTime": "1699998150000",
        },
        source="test",
    )

    sell_id = build_cl_ord_id("bot6", "sell")
    state.set_order_reason(cl_ord_id=sell_id, reason="rebalance_open_long")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0004",
            "fillPx": "1.0004",
            "sz": "100",
            "accFillSz": "100",
            "state": "filled",
            "cTime": "1700000010000",
            "uTime": "1700000010000",
        },
        source="test",
    )

    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0003"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.reason == "rebalance_open_long"
    assert decision.ask.price == Decimal("1.0003")
    assert decision.ask.base_size == Decimal("746.24231")
    assert decision.reason == "sell_drought_rebalance_sell_only"


def test_strategy_sell_drought_release_sell_scales_to_three_extra_ticks_for_aged_inventory(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_007_200_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.60"),
            sell_drought_rebalance_window_seconds=1800,
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=120,
            rebalance_max_order_age_seconds=12,
            rebalance_release_excess_only=True,
            rebalance_release_max_negative_ticks=1,
            rebalance_release_depth_levels=1,
            rebalance_release_depth_fraction=Decimal("0.10"),
            rebalance_release_depth_step_bonus=Decimal("0.05"),
        ),
        TradingConfig(entry_base_size=Decimal("2000"), quote_size=Decimal("2000"), order_ttl_seconds=6),
    )
    state = build_state("14747.3273", "9252.6727")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("1.0004"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0005"), size=Decimal("100000"))],
        )
    )

    buy_id = build_cl_ord_id("bot6", "buy")
    state.set_order_reason(cl_ord_id=buy_id, reason="join_best_bid")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "1.0005",
            "fillPx": "1.0005",
            "sz": "2846.24231",
            "accFillSz": "2846.24231",
            "state": "filled",
            "cTime": "1699998150000",
            "uTime": "1699998150000",
        },
        source="test",
    )

    sell_id = build_cl_ord_id("bot6", "sell")
    state.set_order_reason(cl_ord_id=sell_id, reason="rebalance_open_long")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0004",
            "fillPx": "1.0004",
            "sz": "100",
            "accFillSz": "100",
            "state": "filled",
            "cTime": "1700000010000",
            "uTime": "1700000010000",
        },
        source="test",
    )

    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.reason == "rebalance_open_long"
    assert decision.ask.price == Decimal("1.0001")
    assert decision.ask.base_size == Decimal("746.24231")
    assert decision.reason == "sell_drought_rebalance_sell_only"


def test_strategy_drought_release_extra_negative_ticks_caps_at_three_for_both_sides(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_007_200_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.60"),
            sell_drought_rebalance_window_seconds=1800,
        ),
        TradingConfig(entry_base_size=Decimal("1000"), quote_size=Decimal("1000"), order_ttl_seconds=6),
    )

    sell_state = build_state("18000", "7000")
    sell_buy_id = build_cl_ord_id("bot6", "buy")
    sell_state.set_order_reason(cl_ord_id=sell_buy_id, reason="join_best_bid")
    sell_state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": sell_buy_id,
            "px": "1.0005",
            "fillPx": "1.0005",
            "sz": "2000",
            "accFillSz": "2000",
            "state": "filled",
            "cTime": "1700000020000",
            "uTime": "1700000020000",
        },
        source="test",
    )
    sell_sell_id = build_cl_ord_id("bot6", "sell")
    sell_state.set_order_reason(cl_ord_id=sell_sell_id, reason="rebalance_open_long")
    sell_state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_sell_id,
            "px": "1.0004",
            "fillPx": "1.0004",
            "sz": "1000",
            "accFillSz": "1000",
            "state": "filled",
            "cTime": "1700000010000",
            "uTime": "1700000010000",
        },
        source="test",
    )
    assert strategy._drought_release_extra_negative_ticks(
        state=sell_state,
        side="sell",
        rebalance_base=Decimal("1000"),
        rebalance_mode="release",
    ) == 3

    buy_state = build_state("7000", "18000")
    buy_sell_id = build_cl_ord_id("bot6", "sell")
    buy_state.set_order_reason(cl_ord_id=buy_sell_id, reason="join_best_ask")
    buy_state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s2",
            "clOrdId": buy_sell_id,
            "px": "0.9995",
            "fillPx": "0.9995",
            "sz": "2000",
            "accFillSz": "2000",
            "state": "filled",
            "cTime": "1700000020000",
            "uTime": "1700000020000",
        },
        source="test",
    )
    buy_buy_id = build_cl_ord_id("bot6", "buy")
    buy_state.set_order_reason(cl_ord_id=buy_buy_id, reason="rebalance_open_short")
    buy_state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b2",
            "clOrdId": buy_buy_id,
            "px": "0.9996",
            "fillPx": "0.9996",
            "sz": "1000",
            "accFillSz": "1000",
            "state": "filled",
            "cTime": "1700000010000",
            "uTime": "1700000010000",
        },
        source="test",
    )
    assert strategy._drought_release_extra_negative_ticks(
        state=buy_state,
        side="buy",
        rebalance_base=Decimal("1000"),
        rebalance_mode="release",
    ) == 3


def test_strategy_drought_release_extra_negative_ticks_stays_zero_when_guard_is_inactive(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_030_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.58"),
            sell_drought_rebalance_window_seconds=60,
        ),
        TradingConfig(entry_base_size=Decimal("1000"), quote_size=Decimal("1000")),
    )
    state = build_state("18000", "7000")

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

    buy_id = build_cl_ord_id("bot6", "buy")
    state.set_order_reason(cl_ord_id=buy_id, reason="join_best_bid")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "2000",
            "accFillSz": "2000",
            "state": "filled",
            "cTime": "1700000002000",
            "uTime": "1700000002000",
        },
        source="test",
    )

    assert strategy._drought_release_extra_negative_ticks(
        state=state,
        side="sell",
        rebalance_base=Decimal("1000"),
        rebalance_mode="release",
    ) == 0


def test_strategy_release_sell_respects_thin_bid_depth_budget(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_max_order_age_seconds=5,
            min_visible_depth_multiplier=Decimal("0"),
            rebalance_release_excess_only=True,
            rebalance_release_max_negative_ticks=1,
            rebalance_release_depth_levels=1,
            rebalance_release_depth_fraction=Decimal("0.10"),
            rebalance_release_depth_step_bonus=Decimal("0"),
        ),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1.0002",
            "fillPx": "1.0002",
            "sz": "5407.775",
            "accFillSz": "5407.775",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("100")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_release_sell_skips_when_bid_depth_budget_is_below_min_size(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_max_order_age_seconds=5,
            min_visible_depth_multiplier=Decimal("0"),
            rebalance_release_excess_only=True,
            rebalance_release_max_negative_ticks=1,
            rebalance_release_depth_levels=1,
            rebalance_release_depth_fraction=Decimal("0.10"),
            rebalance_release_depth_step_bonus=Decimal("0"),
        ),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1.0002",
            "fillPx": "1.0002",
            "sz": "5407.775",
            "accFillSz": "5407.775",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("5"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is None
    assert decision.reason == "reduce_only_inventory_high"


def test_strategy_release_sell_keeps_core_inventory_at_cost_floor_when_no_excess(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_max_order_age_seconds=5,
            rebalance_release_excess_only=True,
            rebalance_release_max_negative_ticks=1,
        ),
        TradingConfig(entry_base_size=Decimal("5000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
            "px": "1.0002",
            "fillPx": "1.0002",
            "sz": "5000",
            "accFillSz": "5000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("5000")
    assert decision.ask.price == Decimal("1.0002")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_suppresses_secondary_sell_side_during_toxic_flow_cooldown(monkeypatch):
    fill_ts = 1_700_000_000_000
    monkeypatch.setattr("src.state.now_ms", lambda: fill_ts + 500)
    monkeypatch.setattr("src.strategy.now_ms", lambda: fill_ts + 500)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            toxic_flow_min_observation_ms=300,
            toxic_flow_max_observation_ms=1000,
            toxic_flow_adverse_ticks=1,
            toxic_flow_cooldown_seconds=2,
        ),
        TradingConfig(entry_base_size=Decimal("5000")),
    )
    state = build_state("50000", "50000")
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
            bids=[BookLevel(price=Decimal("1.0002"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0003"), size=Decimal("100000"))],
        )
    )
    state.evaluate_toxic_flow(
        min_observation_ms=300,
        max_observation_ms=1000,
        adverse_ticks=1,
        cooldown_ms=2000,
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.ask is None
    assert decision.reason == "fill_rebalance_buy_only"


def test_strategy_suppresses_secondary_buy_side_during_toxic_flow_cooldown(monkeypatch):
    fill_ts = 1_700_000_000_000
    monkeypatch.setattr("src.state.now_ms", lambda: fill_ts + 500)
    monkeypatch.setattr("src.strategy.now_ms", lambda: fill_ts + 500)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            toxic_flow_min_observation_ms=300,
            toxic_flow_max_observation_ms=1000,
            toxic_flow_adverse_ticks=1,
            toxic_flow_cooldown_seconds=2,
        ),
        TradingConfig(entry_base_size=Decimal("5000")),
    )
    state = build_state("50000", "50000")
    buy_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": buy_id,
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
            bids=[BookLevel(price=Decimal("0.9997"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
        )
    )
    state.evaluate_toxic_flow(
        min_observation_ms=300,
        max_observation_ms=1000,
        adverse_ticks=1,
        cooldown_ms=2000,
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_rebalance_sell_can_keep_small_passive_bid_when_inventory_stays_below_target():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(entry_base_size=Decimal("10000")))
    state = build_state("5000", "28000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.bid is not None
    assert decision.bid.base_size == Decimal("500.0000")
    assert decision.bid.price == Decimal("0.9998")
    assert decision.reason == "fill_rebalance_sell_biased"


def test_strategy_rebalance_sell_keeps_overlay_when_account_inventory_is_near_target():
    strategy = MicroMakerStrategy(StrategyConfig(), TradingConfig(entry_base_size=Decimal("10000")))
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
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

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.base_size == Decimal("500.0000")
    assert decision.bid.price == Decimal("0.9998")
    assert decision.reason == "fill_rebalance_sell_biased"


def test_strategy_release_mode_keeps_small_overlay_when_move_is_not_adverse(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_max_order_age_seconds=6,
            rebalance_secondary_size_factor=Decimal("0.25"),
            rebalance_overlay_floor_factor=Decimal("0.10"),
        ),
        TradingConfig(entry_base_size=Decimal("10000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1.0000",
            "fillPx": "1.0000",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.base_size == Decimal("500.0000")
    assert decision.reason == "fill_rebalance_sell_biased"


def test_strategy_release_mode_keeps_floor_overlay_when_market_moves_against_position(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_013_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            rebalance_min_profit_ticks=1,
            rebalance_reload_timeout_seconds=5,
            rebalance_max_order_age_seconds=6,
            rebalance_secondary_size_factor=Decimal("0.25"),
            rebalance_overlay_floor_factor=Decimal("0.10"),
        ),
        TradingConfig(entry_base_size=Decimal("10000"), order_ttl_seconds=6),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=1,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1.0000",
            "fillPx": "1.0000",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )
    state.set_book(
        BookSnapshot(
            ts_ms=2,
            bids=[BookLevel(price=Decimal("0.9997"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("0.9998"), size=Decimal("100000"))],
        )
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_secondary_bid"
    assert decision.bid.base_size == Decimal("200.00000")
    assert decision.reason == "fill_rebalance_sell_biased"


def test_strategy_inventory_review_does_not_push_secondary_ask_farther_away(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_012_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(),
        TradingConfig(entry_base_size=Decimal("10000"), order_ttl_seconds=6),
    )
    state = build_state("28000", "5000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "sell")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.bid.base_size == Decimal("10000")
    assert decision.bid.price == Decimal("0.9999")
    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("500.0000")
    assert decision.ask.price == Decimal("1.0001")


def test_strategy_inventory_review_does_not_push_secondary_bid_farther_away(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_012_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(),
        TradingConfig(entry_base_size=Decimal("10000"), order_ttl_seconds=6),
    )
    state = build_state("5000", "28000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1700000000000",
            "uTime": "1700000000000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.ask is not None
    assert decision.ask.base_size == Decimal("10000")
    assert decision.ask.price == Decimal("1.0001")
    assert decision.bid is not None
    assert decision.bid.base_size == Decimal("500.0000")
    assert decision.bid.price == Decimal("0.9998")


def test_strategy_strict_cycle_sell_leg_ignores_normal_sell_floor_when_buy_fill_already_has_one_tick_profit():
    strategy = MicroMakerStrategy(
        StrategyConfig(
            strict_alternating_sides=True,
            rebalance_min_profit_ticks=1,
            normal_sell_price_floor=Decimal("1.0001"),
        ),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("50000", "50000")
    state.set_book(
        BookSnapshot(
            ts_ms=9999999999999,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("100000"))],
            asks=[BookLevel(price=Decimal("1"), size=Decimal("100000"))],
        )
    )
    cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "1",
            "clOrdId": cl_ord_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.price == Decimal("1")
    assert decision.ask.base_size == Decimal("10000")
    assert decision.reason == "fill_rebalance_sell_only"


def test_strategy_ignores_sub_min_rebalance_dust_and_keeps_normal_ask():
    strategy = MicroMakerStrategy(
        StrategyConfig(normal_sell_price_floor=Decimal("1")),
        TradingConfig(entry_base_size=Decimal("10000")),
    )
    state = build_state("20230.742424", "4785.963127427094")
    sell_cl_ord_id = build_cl_ord_id("bot6", "sell")
    buy_cl_ord_id = build_cl_ord_id("bot6", "buy")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "1",
            "clOrdId": sell_cl_ord_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "9999.000099",
            "accFillSz": "9999.000099",
            "state": "filled",
            "cTime": "1",
            "uTime": "2",
        },
        source="test",
    )
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "2",
            "clOrdId": buy_cl_ord_id,
            "px": "1",
            "fillPx": "1",
            "sz": "10000",
            "accFillSz": "10000",
            "state": "filled",
            "cTime": "3",
            "uTime": "4",
        },
        source="test",
    )

    assert state.strategy_position_base() == Decimal("0.999901")

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="reduce_only_inventory_high", allow_bid=False, allow_ask=True, runtime_state="REDUCE_ONLY"),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.reason == "join_best_ask"
    assert decision.ask.price == Decimal("1")
    assert decision.ask.base_size == Decimal("10000")
    assert decision.reason == "inventory_high_ask_only"


def test_strategy_sell_drought_guard_suppresses_entry_buy_and_keeps_rebalance_sell(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_061_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.58"),
            sell_drought_rebalance_window_seconds=60,
        ),
        TradingConfig(entry_base_size=Decimal("1000"), quote_size=Decimal("1000")),
    )
    state = build_state("18000", "7000")

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

    buy_id = build_cl_ord_id("bot6", "buy")
    state.set_order_reason(cl_ord_id=buy_id, reason="join_best_bid")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "2000",
            "accFillSz": "2000",
            "state": "filled",
            "cTime": "1700000002000",
            "uTime": "1700000002000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is None
    assert decision.ask is not None
    assert decision.ask.reason == "rebalance_open_long"
    assert decision.reason == "sell_drought_rebalance_sell_only"


def test_strategy_sell_drought_guard_does_not_block_recent_rebalance_sell(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_030_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.58"),
            sell_drought_rebalance_window_seconds=60,
        ),
        TradingConfig(entry_base_size=Decimal("1000"), quote_size=Decimal("1000")),
    )
    state = build_state("18000", "7000")

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

    buy_id = build_cl_ord_id("bot6", "buy")
    state.set_order_reason(cl_ord_id=buy_id, reason="join_best_bid")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
            "px": "0.9999",
            "fillPx": "0.9999",
            "sz": "2000",
            "accFillSz": "2000",
            "state": "filled",
            "cTime": "1700000002000",
            "uTime": "1700000002000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.bid.reason == "join_best_bid"
    assert decision.ask is not None


def test_strategy_buy_drought_guard_suppresses_entry_sell_and_keeps_rebalance_buy(monkeypatch):
    monkeypatch.setattr("src.strategy.now_ms", lambda: 1_700_000_061_000)
    strategy = MicroMakerStrategy(
        StrategyConfig(
            secondary_layers_enabled=False,
            sell_drought_guard_enabled=True,
            sell_drought_inventory_ratio_pct=Decimal("0.58"),
            sell_drought_rebalance_window_seconds=60,
        ),
        TradingConfig(entry_base_size=Decimal("1000"), quote_size=Decimal("1000")),
    )
    state = build_state("7000", "18000")

    buy_id = build_cl_ord_id("bot6", "buy")
    state.set_order_reason(cl_ord_id=buy_id, reason="rebalance_open_short")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "b1",
            "clOrdId": buy_id,
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

    sell_id = build_cl_ord_id("bot6", "sell")
    state.set_order_reason(cl_ord_id=sell_id, reason="join_best_ask")
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "sell",
            "ordId": "s1",
            "clOrdId": sell_id,
            "px": "1.0001",
            "fillPx": "1.0001",
            "sz": "2000",
            "accFillSz": "2000",
            "state": "filled",
            "cTime": "1700000002000",
            "uTime": "1700000002000",
        },
        source="test",
    )

    decision = strategy.decide(
        state,
        RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True),
    )

    assert decision.bid is not None
    assert decision.bid.reason == "rebalance_open_short"
    assert decision.ask is None
    assert decision.reason == "buy_drought_rebalance_buy_only"
