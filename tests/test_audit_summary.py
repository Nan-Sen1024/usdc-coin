from pathlib import Path
import json
import sys
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit_store import SQLiteAuditStore
from src.audit_summary import build_profitability_report, render_audit_summary
from src.config import BotConfig


def test_render_audit_summary_outputs_chinese_sections(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    store = SQLiteAuditStore(str(db_path))
    store.open()
    store.append_event(
        ts_ms=1000,
        event="bootstrap_book",
        payload={
            "book": {
                "bids": [{"price": "1", "size": "1000"}],
                "asks": [{"price": "1.0001", "size": "1000"}],
            }
        },
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=1100,
        event="decision",
        payload={"decision": {"reason": "inventory_low_bid_only"}},
        run_id="run-fill",
    )
    store.append_event(ts_ms=1200, event="place_order", payload={"clOrdId": "buy1"}, run_id="run-fill")
    store.append_event(
        ts_ms=1300,
        event="order_update",
        payload={"order": {"cl_ord_id": "buy1", "side": "buy", "price": "1", "filled_size": "10000", "state": "filled"}},
        run_id="run-fill",
    )
    store.append_event(ts_ms=1400, event="place_order", payload={"clOrdId": "sell1"}, run_id="run-fill")
    store.append_event(
        ts_ms=1500,
        event="order_update",
        payload={"order": {"cl_ord_id": "sell1", "side": "sell", "price": "1", "filled_size": "10000", "state": "filled"}},
        run_id="run-fill",
    )
    store.append_event(ts_ms=1600, event="cancel_order", payload={"reason": "reprice_or_ttl", "reason_zh": "改价或超时重挂"}, run_id="run-fill")
    store.append_event(
        ts_ms=2000,
        event="decision",
        payload={"decision": {"reason": "inventory_low_bid_only"}},
        run_id="run-latest",
    )
    store.append_event(ts_ms=2100, event="place_order", payload={"clOrdId": "latest-buy"}, run_id="run-latest")
    store.append_event(ts_ms=2200, event="cancel_order", payload={"reason": "shutdown", "reason_zh": "程序关闭"}, run_id="run-latest")
    store.close()

    snapshot_path.write_text(
        json.dumps(
            {
                "instrument": {"base_ccy": "USDC", "quote_ccy": "USDT"},
                "book": {
                    "bids": [{"price": "1", "size": "1000"}],
                    "asks": [{"price": "1.0001", "size": "1000"}],
                },
                "balances": {
                    "USDC": {"total": "11110.100850395593"},
                    "USDT": {"total": "17562.853776016487"},
                },
                "runtime_state": "STOPPED",
                "runtime_reason": "shutdown",
                "initial_nav_quote": "28673.51013145459977965",
                "observed_fill_count": 2,
                "observed_fill_volume_quote": "20000",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    text = render_audit_summary(config)

    assert "当前快照" in text
    assert "模式=OKX模拟盘" in text
    assert "最新运行" in text
    assert "最近一次有成交的运行" in text
    assert "往返价差毛收益估算(U)=0" in text
    assert "撤单主因: 改价或超时重挂 1" in text


def test_render_audit_summary_shows_market_gate_snapshot(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    snapshot_path.write_text(
        json.dumps(
            {
                "instrument": {"inst_id": "DAI-USDT", "base_ccy": "DAI", "quote_ccy": "USDT"},
                "book": {
                    "bids": [{"price": "1", "size": "1000"}],
                    "asks": [{"price": "1.0001", "size": "1000"}],
                },
                "balances": {
                    "DAI": {"total": "10000"},
                    "USDT": {"total": "10000"},
                },
                "runtime_state": "STOPPED",
                "runtime_reason": "observe-only instrument blocked in live mode: DAI-USDT",
                "initial_nav_quote": "20000",
                "observed_fill_count": 0,
                "observed_fill_volume_quote": "0",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.trading.inst_id = "DAI-USDT"
    config.trading.base_ccy = "DAI"
    config.trading.quote_ccy = "USDT"
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    text = render_audit_summary(config)

    assert "current_inst=DAI-USDT" in text
    assert "market_gate=blocked" in text
    assert "role=observe_only" in text


def test_render_audit_summary_translates_strict_cycle_reasons(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    store = SQLiteAuditStore(str(db_path))
    store.open()
    store.append_event(
        ts_ms=1000,
        event="decision",
        payload={"decision": {"reason": "strict_cycle_buy_only"}},
        run_id="run-latest",
    )
    store.close()

    snapshot_path.write_text(
        json.dumps(
            {
                "instrument": {"base_ccy": "USDC", "quote_ccy": "USDT"},
                "book": {
                    "bids": [{"price": "1", "size": "1000"}],
                    "asks": [{"price": "1.0001", "size": "1000"}],
                },
                "balances": {
                    "USDC": {"total": "10000"},
                    "USDT": {"total": "10000"},
                },
                "runtime_state": "QUOTING",
                "runtime_reason": "strict_cycle_sell_only",
                "initial_nav_quote": "20000",
                "observed_fill_count": 0,
                "observed_fill_volume_quote": "0",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.exchange.simulated = True
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    text = render_audit_summary(config)

    assert "原因=严格交替：本轮只挂卖单" in text
    assert "严格交替：本轮只挂买单 1" in text


def test_render_audit_summary_shows_release_only_snapshot_details(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    snapshot_path.write_text(
        json.dumps(
            {
                "instrument": {"inst_id": "USD1-USDC", "base_ccy": "USD1", "quote_ccy": "USDC"},
                "book": {
                    "bids": [{"price": "0.9994", "size": "1000"}],
                    "asks": [{"price": "0.9995", "size": "1000"}],
                },
                "balances": {
                    "USD1": {"total": "900"},
                    "USDC": {"total": "800"},
                },
                "runtime_state": "QUOTING",
                "runtime_reason": "release_external_sell_only",
                "initial_nav_quote": "1699.45",
                "observed_fill_count": 0,
                "observed_fill_volume_quote": "0",
                "initial_external_base_inventory": "1200",
                "external_base_inventory_remaining": "900",
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
    config.strategy.release_only_base_buffer = 150
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    text = render_audit_summary(config)

    assert "释放模式:" in text
    assert "初始外部库存=1200" in text
    assert "当前剩余=900" in text
    assert "已释放=300" in text
    assert "保留量=150" in text
    assert "当前可释放=750" in text


def test_render_audit_summary_shows_release_fill_totals_in_run_section(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    store = SQLiteAuditStore(str(db_path))
    store.open()
    store.append_event(
        ts_ms=1000,
        event="decision",
        payload={"decision": {"reason": "release_external_sell_only"}},
        run_id="run-release",
    )
    store.append_event(
        ts_ms=1100,
        event="order_update",
        payload={
            "order": {"cl_ord_id": "sell-release", "side": "sell", "price": "0.9995", "filled_size": "500", "state": "filled"},
            "reason": "release_external_long",
            "reason_bucket": "release",
        },
        run_id="run-release",
    )
    store.close()

    snapshot_path.write_text(
        json.dumps(
            {
                "instrument": {"inst_id": "USD1-USDC", "base_ccy": "USD1", "quote_ccy": "USDC"},
                "book": {
                    "bids": [{"price": "0.9994", "size": "1000"}],
                    "asks": [{"price": "0.9995", "size": "1000"}],
                },
                "balances": {
                    "USD1": {"total": "900"},
                    "USDC": {"total": "800"},
                },
                "runtime_state": "QUOTING",
                "runtime_reason": "release_external_sell_only",
                "initial_nav_quote": "1699.45",
                "observed_fill_count": 1,
                "observed_fill_volume_quote": "499.75",
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
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    text = render_audit_summary(config, run_id="run-release")

    assert "释放成交=1笔/500USD1/499.75U" in text


def test_render_audit_summary_shows_triangle_route_choice(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    snapshot_path.write_text(
        json.dumps(
            {
                "instrument": {"inst_id": "USD1-USDT", "base_ccy": "USD1", "quote_ccy": "USDT"},
                "book": {
                    "bids": [{"price": "0.9995", "size": "1000"}],
                    "asks": [{"price": "0.9996", "size": "1000"}],
                },
                "balances": {
                    "USD1": {"total": "1200"},
                    "USDT": {"total": "900"},
                },
                "runtime_state": "QUOTING",
                "runtime_reason": "fill_rebalance_sell_only",
                "initial_nav_quote": "2099.52",
                "observed_fill_count": 0,
                "observed_fill_volume_quote": "0",
                "triangle_exit_route_choice": {
                    "primary_route": "direct_sell_usd1usdt",
                    "backup_route": "sell_usd1usdc_then_sell_usdcusdt",
                    "direction": "sell",
                    "primary_reference_price": "0.9996",
                    "backup_reference_price": "0.99949986",
                    "improvement_bp": "0.8",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.exchange.name = "binance"
    config.trading.inst_id = "USD1-USDT"
    config.trading.base_ccy = "USD1"
    config.trading.quote_ccy = "USDT"
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    text = render_audit_summary(config)

    assert "路由建议:" in text
    assert "主路=direct_sell_usd1usdt" in text
    assert "备路=sell_usd1usdc_then_sell_usdcusdt" in text
    assert "方向=sell" in text


def test_build_profitability_report_minimal_metrics(tmp_path):
    db_path = tmp_path / "audit.db"
    snapshot_path = tmp_path / "state_snapshot.json"

    store = SQLiteAuditStore(str(db_path))
    store.open()
    store.append_event(
        ts_ms=1000,
        event="decision",
        payload={
            "decision": {
                "bid_layers": [{"reason": "join_best_bid", "price": "1", "base_size": "100"}],
                "ask_layers": [{"reason": "rebalance_open_long", "price": "0.9999", "base_size": "100"}],
            }
        },
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=1100,
        event="place_order",
        payload={"clOrdId": "buy1", "side": "buy", "px": "1", "sz": "100", "reason": "join_best_bid"},
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=1200,
        event="order_update",
        payload={
            "order": {"cl_ord_id": "buy1", "side": "buy", "price": "1", "filled_size": "100", "state": "filled"},
            "raw": {"fillPx": "1"},
            "reason": "join_best_bid",
        },
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=1300,
        event="amend_order_submitted",
        payload={"cl_ord_id": "sell1", "reason": "rebalance_open_long", "old_price": "0.9999", "new_price": "0.9999"},
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=1400,
        event="cancel_order_terminal",
        payload={"cl_ord_id": "sell1", "reason": "reprice_or_ttl"},
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=1500,
        event="order_update",
        payload={
            "order": {"cl_ord_id": "sell1", "side": "sell", "price": "0.9999", "filled_size": "100", "state": "filled"},
            "raw": {"fillPx": "0.9999"},
            "reason": "rebalance_open_long",
        },
        run_id="run-fill",
    )
    store.append_event(
        ts_ms=2000,
        event="decision",
        payload={"decision": {"reason": "inventory_low_bid_only"}},
        run_id="run-latest",
    )
    store.close()

    snapshot_path.write_text(
        json.dumps(
            {
                "fill_markout_summary_by_reason": {
                    "entry": {"300": {"samples": 2, "avg_adverse_ticks": "0.3"}},
                    "rebalance": {"300": {"samples": 1, "avg_adverse_ticks": "1.2"}},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = BotConfig(mode="live")
    config.telemetry.sqlite_path = str(db_path)
    config.telemetry.state_path = str(snapshot_path)

    report = build_profitability_report(config, title="latest filled", run_id="run-fill")

    assert report.fill_count == 2
    assert report.turnover_quote == Decimal("199.99")
    assert report.realized_pnl_quote == Decimal("-0.01")
    assert report.cancel_after_terminal_rate == Decimal("1")
    assert report.same_price_amend_rate == Decimal("1")
    assert report.largest_turnover_action_class == "entry"
    assert report.worst_realized_action_class == "rebalance"
    assert any(item.action_class == "entry" for item in report.action_summaries)
    assert any(item.action_class == "rebalance" for item in report.action_summaries)
