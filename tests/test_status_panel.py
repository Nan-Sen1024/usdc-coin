from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import TelemetryConfig
from src.models import Balance, BookLevel, BookSnapshot, InstrumentMeta, OrderIntent, QuoteDecision, RiskStatus, TradeTick
from src.state import BotState
from src.status_panel import TerminalStatusPanel
from src.utils import build_cl_ord_id


def test_status_panel_builds_readable_snapshot():
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
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("50010"), available=Decimal("40010"), frozen=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("49990"), available=Decimal("49990")),
        }
    )
    state.runtime_state = "QUOTING"
    state.runtime_reason = "two_sided"
    state.shadow_realized_pnl_quote = Decimal("1.25")
    state.shadow_fill_count = 3
    state.set_last_trade(TradeTick(ts_ms=11, received_ms=11, price=Decimal("1"), size=Decimal("100"), side="sell"))
    state.set_last_market_trade(TradeTick(ts_ms=12, received_ms=12, price=Decimal("1.0001"), size=Decimal("50"), side="buy", trade_id="mkt-1"))
    state.apply_order_update(
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "",
            "clOrdId": build_cl_ord_id("bot6", "buy"),
            "px": "0.9999",
            "sz": "10000",
            "accFillSz": "0",
            "state": "live",
            "cTime": "1",
            "uTime": "1",
        },
        source="shadow_place",
    ).queue_ahead_size = Decimal("250000")

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="shadow",
    )
    decision = QuoteDecision(
        reason="two_sided",
        bid=OrderIntent(side="buy", price=Decimal("0.9999"), quote_notional=Decimal("10000"), reason="join_best_bid"),
        ask=OrderIntent(side="sell", price=Decimal("1.0000"), quote_notional=Decimal("10000"), reason="join_best_ask"),
        inventory_ratio=Decimal("0.5"),
        spread_ticks=Decimal("1"),
    )
    risk = RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "状态=报价中" in text
    assert "原因=双边报价" in text
    assert "成交次数=3" in text
    assert "策略回撤(U)=0" in text
    assert "账户回撤(U)=0" in text
    assert "已实现(U)=+1.25" in text
    assert "市场成交 | 方向=买 价格=1.0001 数量=50 成交ID=mkt-1" in text
    assert "买单 价格=0.9999 数量=10000" in text
    assert "买单 价格=0.9999 目标金额=10000U" in text
    assert "最近成交 | 方向=卖 委托价=- 成交价=1 数量=100" in text


def test_status_panel_marks_demo_live_mode_in_chinese():
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
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }
    )
    state.runtime_state = "QUOTING"
    state.runtime_reason = "inventory_low_bid_only"
    state.set_last_market_trade(TradeTick(ts_ms=11, received_ms=11, price=Decimal("1"), size=Decimal("123"), side="sell", trade_id="market-2"))

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
        simulated=True,
    )
    decision = QuoteDecision(reason="inventory_low_bid_only", bid=None, ask=None)
    risk = RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "模式=OKX模拟盘" in text
    assert "本轮盈亏(U)=" in text
    assert "账户权益(U)=" in text
    assert "市场成交 | 方向=卖 价格=1 数量=123 成交ID=market-2" in text


def test_status_panel_shows_live_realized_and_unrealized_pnl():
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
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
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
    state.runtime_state = "QUOTING"
    state.runtime_reason = "fill_rebalance_sell_only"

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
        simulated=True,
    )
    decision = QuoteDecision(
        reason="fill_rebalance_sell_only",
        ask=OrderIntent(side="sell", price=Decimal("1.0001"), quote_notional=Decimal("10001"), reason="rebalance_open_long", base_size=Decimal("10000")),
    )
    risk = RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "已实现(U)=0" in text
    assert "库存浮盈(U)=+0.5" in text
    assert "待回补仓位(USDC)=+10000" in text
    assert "策略回撤(U)=0" in text
    assert "成交后回补，只挂卖单" in text
    assert f"最近成交 | 方向=买 委托价=1 成交价=1 数量=10000 订单号={buy_id}" in text


def test_status_panel_shows_separate_strategy_and_account_drawdowns():
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
            ts_ms=10,
            received_ms=10,
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
            ts_ms=11,
            received_ms=11,
            bids=[BookLevel(price=Decimal("0.9999"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0000"), size=Decimal("1000"))],
        )
    )
    state.apply_account_update(
        {
            "totalEq": "33001.3",
            "details": [
                {"ccy": "USDC", "cashBal": "10000", "availBal": "10000"},
                {"ccy": "USDT", "cashBal": "10000", "availBal": "10000"},
            ],
        }
    )
    state.runtime_state = "QUOTING"
    state.runtime_reason = "sell_drought_rebalance_sell_only"

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
        simulated=False,
    )
    decision = QuoteDecision(reason="sell_drought_rebalance_sell_only", bid=None, ask=None)
    risk = RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "策略峰值(U)=20000.5" in text
    assert "策略回撤(U)=-1" in text
    assert "账户权益(U)=33001.3" in text
    assert "账户峰值(U)=33010" in text
    assert "账户回撤(U)=-8.7" in text


def test_status_panel_shows_market_gate_role():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
        InstrumentMeta(
            inst_id="USDG-USDT",
            inst_type="SPOT",
            base_ccy="USDG",
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
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("1.0000"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDG": Balance(ccy="USDG", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }
    )
    state.runtime_state = "READY"
    state.runtime_reason = "ok"

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
        live_allowed_instruments=("USDC-USDT", "USDG-USDT"),
        observe_only_instruments=("DAI-USDT", "PYUSD-USDT"),
    )
    decision = QuoteDecision(reason="two_sided", bid=None, ask=None, inventory_ratio=Decimal("0.5"), spread_ticks=Decimal("1"))
    risk = RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "market_gate |" in text
    assert "current=USDG-USDT" in text
    assert "role=core" in text


def test_status_panel_translates_strict_cycle_reasons():
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
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("1"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("1.0001"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USDC": Balance(ccy="USDC", total=Decimal("10000"), available=Decimal("10000")),
            "USDT": Balance(ccy="USDT", total=Decimal("10000"), available=Decimal("10000")),
        }
    )
    state.runtime_state = "QUOTING"
    state.runtime_reason = "strict_cycle_sell_only"

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
        simulated=True,
    )
    decision = QuoteDecision(
        reason="strict_cycle_buy_only",
        bid=OrderIntent(side="buy", price=Decimal("1"), quote_notional=Decimal("10000"), reason="join_best_bid"),
        ask=None,
    )
    risk = RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=False)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "原因=严格交替：本轮只挂卖单" in text
    assert "决策 | 原因=严格交替：本轮只挂买单" in text


def test_status_panel_shows_release_only_status():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
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
    state.configure_release_tracking(enabled=True)
    state.set_book(
        BookSnapshot(
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("0.9994"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("0.9995"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("1200"), available=Decimal("1200")),
            "USDC": Balance(ccy="USDC", total=Decimal("800"), available=Decimal("800")),
        }
    )
    state.external_base_inventory_remaining = Decimal("900")
    state.runtime_state = "QUOTING"
    state.runtime_reason = "release_external_sell_only"

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
        simulated=False,
        release_only_mode=True,
        release_only_base_buffer=Decimal("150"),
    )
    decision = QuoteDecision(
        reason="release_external_sell_only",
        ask=OrderIntent(
            side="sell",
            price=Decimal("0.9995"),
            quote_notional=Decimal("499.75"),
            reason="release_external_long",
            base_size=Decimal("500"),
        ),
    )
    risk = RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "释放 |" in text
    assert "模式=是" in text
    assert "外部库存剩余=900" in text
    assert "保留量=150" in text
    assert "可释放=750" in text
    assert "当前动作=释放中" in text


def test_status_panel_shows_triangle_route_status():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
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
    state.set_book(
        BookSnapshot(
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("1200"), available=Decimal("1200")),
            "USDT": Balance(ccy="USDT", total=Decimal("900"), available=Decimal("900")),
        }
    )
    state.set_triangle_exit_route_choice(
        {
            "primary_route": "direct_sell_usd1usdt",
            "backup_route": "sell_usd1usdc_then_sell_usdcusdt",
            "direction": "sell",
            "primary_reference_price": Decimal("0.9996"),
            "backup_reference_price": Decimal("0.99949986"),
            "improvement_bp": Decimal("0.8"),
        }
    )

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
    )
    decision = QuoteDecision(reason="fill_rebalance_sell_only", ask=None)
    risk = RiskStatus(ok=True, reason="ok", allow_bid=False, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "路由 |" in text
    assert "主路=direct_sell_usd1usdt" in text
    assert "备路=sell_usd1usdc_then_sell_usdcusdt" in text
    assert "方向=sell" in text
    assert "主参考价=0.9996" in text


def test_status_panel_shows_triangle_route_diagnostics_without_choice():
    state = BotState(managed_prefix="bot6", state_path="data/test_state.json")
    state.set_instrument(
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
    state.set_book(
        BookSnapshot(
            ts_ms=10,
            received_ms=10,
            bids=[BookLevel(price=Decimal("0.9995"), size=Decimal("1000"))],
            asks=[BookLevel(price=Decimal("0.9996"), size=Decimal("1000"))],
        )
    )
    state.set_balances(
        {
            "USD1": Balance(ccy="USD1", total=Decimal("1200"), available=Decimal("1200")),
            "USDT": Balance(ccy="USDT", total=Decimal("900"), available=Decimal("900")),
        }
    )
    state.set_triangle_route_diagnostics(
        {
            "snapshot_status": "ready",
            "snapshot_age_ms": 120,
            "route_status": "flat_position",
            "entry_buy_gate_status": "allowed",
            "entry_buy_gate_reason": "strict_edge_ok",
        }
    )

    panel = TerminalStatusPanel(
        config=TelemetryConfig(status_panel_enabled=True, status_panel_render_non_interactive=True),
        mode="live",
    )
    decision = QuoteDecision(reason="two_sided", ask=None)
    risk = RiskStatus(ok=True, reason="ok", allow_bid=True, allow_ask=True)

    text = panel.build_text(state=state, risk_status=risk, decision=decision)

    assert "路由 |" in text
    assert "快照=ready" in text
    assert "库存路由状态=flat_position" in text
    assert "入口买入=allowed" in text
    assert "入口原因=strict_edge_ok" in text
