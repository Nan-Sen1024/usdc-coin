from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import BotConfig
from .market_gate import evaluate_market_gate
from .reason_attribution import classify_reason_bucket, realized_per_10k_turnover
from .utils import decimal_to_str


@dataclass(frozen=True)
class ActionProfitabilitySummary:
    action_class: str
    fill_count: int
    turnover_quote: Decimal
    realized_pnl_quote: Decimal
    realized_per_10k_turnover: Decimal | None
    gross_spread_capture_quote: Decimal
    gross_spread_capture_per_10k: Decimal | None
    markout_300ms: Decimal | None
    markout_1000ms: Decimal | None
    markout_2000ms: Decimal | None


@dataclass(frozen=True)
class ProfitabilityReport:
    title: str
    run_id: str | None
    start_ts_ms: int | None
    end_ts_ms: int | None
    event_count: int
    fill_count: int
    turnover_quote: Decimal
    realized_pnl_quote: Decimal
    realized_per_10k_turnover: Decimal | None
    gross_spread_capture_quote: Decimal
    gross_spread_capture_per_10k: Decimal | None
    roundtrip_closure_rate: Decimal | None
    cancel_after_terminal_rate: Decimal | None
    same_price_amend_rate: Decimal | None
    action_summaries: list[ActionProfitabilitySummary]
    largest_turnover_action_class: str | None
    worst_realized_action_class: str | None
    markout_is_approximate: bool


def render_audit_summary(config: BotConfig, *, run_id: str | None = None) -> str:
    lines: list[str] = []

    snapshot_lines = _render_snapshot_section(config)
    if snapshot_lines:
        lines.extend(snapshot_lines)

    latest_run = run_id or _latest_run_id(config.telemetry.sqlite_path)
    if latest_run:
        if lines:
            lines.append("")
        lines.extend(
            _render_run_section(
                config.telemetry.sqlite_path,
                latest_run,
                title="最新运行",
                default_base_ccy=config.trading.base_ccy,
            )
        )

    filled_run = _latest_run_with_fills(config.telemetry.sqlite_path, exclude_run_id=latest_run)
    if filled_run:
        if lines:
            lines.append("")
        lines.extend(
            _render_run_section(
                config.telemetry.sqlite_path,
                filled_run,
                title="最近一次有成交的运行",
                default_base_ccy=config.trading.base_ccy,
            )
        )

    if not lines:
        return "未找到快照或审计数据。"
    return "\n".join(lines)


def _render_snapshot_section(config: BotConfig) -> list[str]:
    snapshot_path = Path(config.telemetry.state_path)
    if not snapshot_path.exists():
        return []

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    instrument = payload.get("instrument") or {}
    balances = payload.get("balances") or {}
    book = payload.get("book") or {}
    runtime_state = payload.get("runtime_state") or "-"
    runtime_reason = payload.get("runtime_reason") or "-"
    initial_nav = _optional_decimal(payload.get("initial_nav_quote"))
    current_inst_id = instrument.get("inst_id") or config.trading.inst_id
    base_ccy = instrument.get("base_ccy") or config.trading.base_ccy
    quote_ccy = instrument.get("quote_ccy") or config.trading.quote_ccy
    market_gate = evaluate_market_gate(
        inst_id=str(current_inst_id),
        live_allowed_instruments=config.risk.live_allowed_instruments,
        observe_only_instruments=config.risk.observe_only_instruments,
    )

    best_bid = _extract_price(book.get("bids"), 0)
    best_ask = _extract_price(book.get("asks"), 0)
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / Decimal("2")
    elif best_bid is not None:
        mid = best_bid
    elif best_ask is not None:
        mid = best_ask

    base_total = _balance_total(balances, base_ccy)
    quote_total = _balance_total(balances, quote_ccy)
    nav = base_total * mid + quote_total if mid is not None else None
    pnl = nav - initial_nav if nav is not None and initial_nav is not None else None
    inventory_ratio = (base_total * mid / nav) if nav is not None and mid is not None and nav > 0 else None
    live_realized = _optional_decimal(payload.get("live_realized_pnl_quote"))
    live_unrealized = _optional_decimal(payload.get("live_unrealized_pnl_quote"))
    strategy_position_base = _optional_decimal(payload.get("strategy_position_base"))
    initial_external_base_inventory = _optional_decimal(payload.get("initial_external_base_inventory"))
    external_base_inventory_remaining = _optional_decimal(payload.get("external_base_inventory_remaining"))
    triangle_exit_route_choice = payload.get("triangle_exit_route_choice") or {}

    mode_text = "OKX模拟盘" if config.mode == "live" and config.exchange.simulated else ("实盘" if config.mode == "live" else "影子模拟")
    lines = [
        "当前快照",
        f"- 模式={mode_text}",
        f"- 状态={_translate_reason(runtime_state)} | 原因={_translate_reason(runtime_reason)}",
        f"- 当前盘口: 买一={_fmt(best_bid)} 卖一={_fmt(best_ask)} 中间价={_fmt(mid)}",
        f"- {base_ccy}: 总额={_fmt(base_total)}",
        f"- {quote_ccy}: 总额={_fmt(quote_total)}",
        f"- 策略净值(U)={_fmt(nav)} | 本轮盈亏(U)={_fmt_signed(pnl)} | 库存占比={_fmt_pct(inventory_ratio)}",
        f"- 已观测成交次数={payload.get('observed_fill_count', 0)} | 已观测成交额(U)={_fmt(_optional_decimal(payload.get('observed_fill_volume_quote')))}",
    ]
    lines.insert(3, f"- market_gate={'allowed' if market_gate.live_allowed else 'blocked'} | current_inst={current_inst_id} | role={market_gate.role}")
    if config.strategy.release_only_mode:
        release_buffer = Decimal(str(config.strategy.release_only_base_buffer))
        released = None
        if initial_external_base_inventory is not None and external_base_inventory_remaining is not None:
            released = max(initial_external_base_inventory - external_base_inventory_remaining, Decimal("0"))
        releasable = Decimal("0")
        if external_base_inventory_remaining is not None:
            releasable = max(min(external_base_inventory_remaining, base_total) - max(release_buffer, Decimal("0")), Decimal("0"))
        lines.append(
            "- 释放模式: "
            f"初始外部库存={_fmt(initial_external_base_inventory)} "
            f"当前剩余={_fmt(external_base_inventory_remaining)} "
            f"已释放={_fmt(released)} "
            f"保留量={_fmt(release_buffer)} "
            f"当前可释放={_fmt(releasable)}"
        )
    if live_realized is not None or live_unrealized is not None or strategy_position_base is not None:
        lines.append(
            f"- 已实现(U)={_fmt_signed(live_realized)} | 库存浮盈(U)={_fmt_signed(live_unrealized)} | 待回补仓位({base_ccy})={_fmt_signed(strategy_position_base)}"
        )
    if isinstance(triangle_exit_route_choice, dict) and triangle_exit_route_choice:
        lines.append(
            "- 路由建议: "
            f"主路={triangle_exit_route_choice.get('primary_route') or '-'} "
            f"备路={triangle_exit_route_choice.get('backup_route') or '-'} "
            f"方向={triangle_exit_route_choice.get('direction') or '-'} "
            f"主参考价={_fmt(_optional_decimal(triangle_exit_route_choice.get('primary_reference_price')))} "
            f"备参考价={_fmt(_optional_decimal(triangle_exit_route_choice.get('backup_reference_price')))} "
            f"改善bp={_fmt(_optional_decimal(triangle_exit_route_choice.get('improvement_bp')))}"
        )
    return lines


def _render_run_section(sqlite_path: str, run_id: str, *, title: str, default_base_ccy: str | None = None) -> list[str]:
    events = _load_run_events(sqlite_path, run_id)
    if not events:
        return [title, f"- run_id={run_id}", "- 未找到事件"]

    counts = Counter(event for _, event, _ in events)
    cancel_reasons: Counter[str] = Counter()
    decision_reasons: Counter[str] = Counter()
    fills_by_order: dict[str, dict[str, Any]] = {}
    release_fill_count = 0
    release_fill_base = Decimal("0")
    release_fill_quote = Decimal("0")

    for _, event, payload in events:
        if event == "cancel_order":
            cancel_reasons[str(payload.get("reason_zh") or _translate_reason(str(payload.get("reason") or "-")))] += 1
            continue
        if event == "decision":
            reason = ((payload.get("decision") or {}).get("reason")) or "-"
            decision_reasons[str(reason)] += 1
            continue
        if event != "order_update":
            continue

        order = payload.get("order") or {}
        filled_size = _optional_decimal(order.get("filled_size"))
        if filled_size is None or filled_size <= 0:
            continue

        cl_ord_id = str(order.get("cl_ord_id") or order.get("ord_id") or f"unknown-{len(fills_by_order)}")
        previous = fills_by_order.get(cl_ord_id)
        if previous is not None and filled_size <= previous["filled_size"]:
            continue

        fills_by_order[cl_ord_id] = {
            "side": str(order.get("side") or "-"),
            "price": _optional_decimal(order.get("price")) or Decimal("0"),
            "filled_size": filled_size,
            "state": str(order.get("state") or "-"),
            "reason": str(payload.get("reason") or ""),
            "reason_bucket": str(payload.get("reason_bucket") or ""),
        }

    buy_count = 0
    sell_count = 0
    buy_notional = Decimal("0")
    sell_notional = Decimal("0")
    buy_size = Decimal("0")
    sell_size = Decimal("0")
    for fill in fills_by_order.values():
        notional = fill["filled_size"] * fill["price"]
        if fill["side"] == "buy":
            buy_count += 1
            buy_notional += notional
            buy_size += fill["filled_size"]
        elif fill["side"] == "sell":
            sell_count += 1
            sell_notional += notional
            sell_size += fill["filled_size"]
        if fill.get("reason_bucket") == "release" or str(fill.get("reason") or "").startswith("release"):
            release_fill_count += 1
            release_fill_base += fill["filled_size"]
            release_fill_quote += notional

    start_ms = events[0][0]
    end_ms = events[-1][0]
    duration_seconds = Decimal(end_ms - start_ms) / Decimal("1000")

    roundtrip_pnl: Decimal | None = None
    if buy_size > 0 and sell_size > 0 and buy_size == sell_size:
        roundtrip_pnl = sell_notional - buy_notional

    lines = [
        title,
        f"- run_id={run_id}",
        f"- 时长={_fmt_seconds(duration_seconds)} | 事件数={len(events)}",
        (
            f"- 下单={counts.get('place_order', 0)} "
            f"撤单={counts.get('cancel_order', 0)} "
            f"订单回报={counts.get('order_update', 0)} "
            f"成交订单={buy_count + sell_count}"
        ),
        (
            f"- 买入成交={buy_count}笔/{_fmt(buy_notional)}U "
            f"卖出成交={sell_count}笔/{_fmt(sell_notional)}U "
            f"往返价差毛收益估算(U)={_fmt_signed(roundtrip_pnl)}"
        ),
    ]
    if release_fill_count > 0:
        order_ccy = _infer_run_base_ccy(events) or default_base_ccy or "BASE"
        lines.append(f"- 释放成交={release_fill_count}笔/{_fmt(release_fill_base)}{order_ccy}/{_fmt(release_fill_quote)}U")
    if roundtrip_pnl is None and (buy_count or sell_count):
        lines.append("- 说明: 当前只有单边成交，或买卖数量未配平，暂不把成交额差额当成利润")
    if cancel_reasons:
        translated = "，".join(f"{reason} {count}" for reason, count in cancel_reasons.most_common())
        lines.append(f"- 撤单主因: {translated}")
    if decision_reasons:
        translated = "，".join(f"{_translate_reason(reason)} {count}" for reason, count in decision_reasons.most_common(3))
        lines.append(f"- 决策主因: {translated}")
    return lines


def _infer_run_base_ccy(events: list[tuple[int, str, dict[str, Any]]]) -> str | None:
    for _, event, payload in events:
        if event != "order_update":
            continue
        order = payload.get("order") or {}
        inst_id = str(order.get("inst_id") or order.get("instId") or "")
        if "-" in inst_id:
            return inst_id.split("-", 1)[0]
    return None


def _load_run_events(sqlite_path: str, run_id: str) -> list[tuple[int, str, dict[str, Any]]]:
    path = Path(sqlite_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT ts_ms, event, payload_json FROM audit_events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(int(ts_ms), str(event), json.loads(payload_json)) for ts_ms, event, payload_json in rows]


def _latest_run_id(sqlite_path: str) -> str | None:
    path = Path(sqlite_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT run_id FROM audit_events WHERE run_id IS NOT NULL GROUP BY run_id ORDER BY MAX(ts_ms) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row and row[0] else None


def _latest_run_with_fills(sqlite_path: str, *, exclude_run_id: str | None = None) -> str | None:
    path = Path(sqlite_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        run_rows = conn.execute(
            "SELECT run_id FROM audit_events WHERE run_id IS NOT NULL GROUP BY run_id ORDER BY MAX(ts_ms) DESC"
        ).fetchall()
        for (run_id,) in run_rows:
            if not run_id or run_id == exclude_run_id:
                continue
            has_fill = conn.execute(
                """
                SELECT 1
                FROM audit_events
                WHERE run_id = ?
                  AND event = 'order_update'
                  AND CAST(json_extract(payload_json, '$.order.filled_size') AS REAL) > 0
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if has_fill:
                return str(run_id)
    finally:
        conn.close()
    return None


def _extract_price(levels: Any, index: int) -> Decimal | None:
    if not isinstance(levels, list) or len(levels) <= index:
        return None
    level = levels[index] or {}
    return _optional_decimal(level.get("price"))


def _balance_total(balances: dict[str, Any], ccy: str) -> Decimal:
    payload = balances.get(ccy) or {}
    return _optional_decimal(payload.get("total")) or Decimal("0")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _fmt(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return decimal_to_str(value)


def _fmt_signed(value: Decimal | None) -> str:
    if value is None:
        return "-"
    prefix = "+" if value > 0 else ""
    return prefix + decimal_to_str(value)


def _fmt_pct(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return decimal_to_str(value * Decimal("100")) + "%"


def _fmt_seconds(value: Decimal) -> str:
    if value < 60:
        return decimal_to_str(value) + "秒"
    minutes = int(value // Decimal("60"))
    seconds = value - Decimal(minutes) * Decimal("60")
    return f"{minutes}分{decimal_to_str(seconds)}秒"


def _translate_reason(value: str) -> str:
    mapping = {
        "INIT": "初始化",
        "READY": "就绪",
        "QUOTING": "报价中",
        "PAUSED": "暂停",
        "STOPPED": "停止",
        "REDUCE_ONLY": "仅减仓",
        "ok": "正常",
        "two_sided": "双边报价",
        "inventory_low_bid_only": "库存偏低，只挂买单",
        "inventory_high_ask_only": "库存偏高，只挂卖单",
        "fill_rebalance_buy_only": "成交后回补，只挂买单",
        "fill_rebalance_sell_only": "成交后回补，只挂卖单",
        "strict_cycle_buy_only": "严格交替：本轮只挂买单",
        "strict_cycle_sell_only": "严格交替：本轮只挂卖单",
        "streams not ready": "流未就绪",
        "too many place failures": "下单失败次数过多",
        "too many reconnects in 5m": "5分钟内重连次数过多",
        "reduce_only_inventory_high": "库存过高，仅减仓",
        "reduce_only_inventory_low": "库存过低，仅减仓",
        "inventory/balance blocks both sides": "余额或库存限制，双边都不能挂",
        "missing market bootstrap": "缺少启动行情",
        "shutdown": "程序关闭",
        "booting": "启动中",
        "reprice_or_ttl": "超时或需要重挂",
        "side_disabled": "该侧当前禁挂",
        "-": "-",
    }
    if value in mapping:
        return mapping[value]
    if str(value).startswith("observe-only instrument blocked in live mode:"):
        return "观察池交易对禁止 live 启动:" + str(value).split(":", 1)[1]
    if str(value).startswith("instrument not approved for live mode:"):
        return "未列入 live 允许池:" + str(value).split(":", 1)[1]
    prefix_mapping = {
        "stale book:": "盘口过旧:",
        "pause active:": "暂停中:",
        "place failure cooldown:": "下单失败冷却中:",
        "cancel failure cooldown:": "撤单失败冷却中:",
        "spread too tight:": "价差过小:",
        "visible depth too thin:": "可见深度不足:",
        "peg deviation too high:": "脱锚偏离过大:",
        "daily loss limit hit:": "触发日内亏损限制:",
        "resync required:": "需要重同步:",
    }
    for prefix, translated in prefix_mapping.items():
        if str(value).startswith(prefix):
            return translated + str(value)[len(prefix):]
    return value


def render_profit_density_report(config: BotConfig, *, window_hours: int = 24) -> str:
    sections = [
        _render_profitability_section(
            config=config,
            title=f"最近{window_hours}小时(相对最新事件)",
            since_ts_ms=_since_ts_ms_for_window(config.telemetry.sqlite_path, window_hours=window_hours),
        )
    ]

    latest_run = _latest_run_id(config.telemetry.sqlite_path)
    if latest_run:
        sections.append(
            _render_profitability_section(
                config=config,
                title="最新运行",
                run_id=latest_run,
            )
        )

    latest_filled_run = _latest_run_with_fills(config.telemetry.sqlite_path, exclude_run_id=latest_run)
    if latest_filled_run:
        sections.append(
            _render_profitability_section(
                config=config,
                title="最近一次有成交的运行",
                run_id=latest_filled_run,
            )
        )

    header = [
        "Profit Density Report",
        f"- config_inst={config.trading.inst_id}",
        f"- sqlite={config.telemetry.sqlite_path}",
        f"- journal={config.telemetry.journal_path}",
        f"- state={config.telemetry.state_path}",
    ]
    return "\n".join(header + [""] + sections)


def build_profitability_report(
    config: BotConfig,
    *,
    title: str,
    run_id: str | None = None,
    since_ts_ms: int | None = None,
) -> ProfitabilityReport:
    events = _load_filtered_events(
        config.telemetry.sqlite_path,
        run_id=run_id,
        since_ts_ms=since_ts_ms,
    )
    if not events:
        return ProfitabilityReport(
            title=title,
            run_id=run_id,
            start_ts_ms=None,
            end_ts_ms=None,
            event_count=0,
            fill_count=0,
            turnover_quote=Decimal("0"),
            realized_pnl_quote=Decimal("0"),
            realized_per_10k_turnover=None,
            gross_spread_capture_quote=Decimal("0"),
            gross_spread_capture_per_10k=None,
            roundtrip_closure_rate=None,
            cancel_after_terminal_rate=None,
            same_price_amend_rate=None,
            action_summaries=[],
            largest_turnover_action_class=None,
            worst_realized_action_class=None,
            markout_is_approximate=Path(config.telemetry.state_path).exists(),
        )

    analysis = _analyze_profitability_events(events)
    markout_summary = _load_markout_by_action_class(config.telemetry.state_path)

    action_classes = sorted(set(analysis["turnover_by_action"]) | set(analysis["pnl_by_action"]) | set(markout_summary))
    action_summaries: list[ActionProfitabilitySummary] = []
    for action_class in action_classes:
        turnover = Decimal(analysis["turnover_by_action"].get(action_class, Decimal("0")))
        realized = Decimal(analysis["pnl_by_action"].get(action_class, Decimal("0")))
        gross = Decimal(analysis["gross_spread_by_action"].get(action_class, Decimal("0")))
        markout = markout_summary.get(action_class) or {}
        action_summaries.append(
            ActionProfitabilitySummary(
                action_class=action_class,
                fill_count=int(analysis["fill_count_by_action"].get(action_class, 0)),
                turnover_quote=turnover,
                realized_pnl_quote=realized,
                realized_per_10k_turnover=realized_per_10k_turnover(
                    realized_pnl_quote=realized,
                    turnover_quote=turnover,
                ),
                gross_spread_capture_quote=gross,
                gross_spread_capture_per_10k=realized_per_10k_turnover(
                    realized_pnl_quote=gross,
                    turnover_quote=turnover,
                ),
                markout_300ms=_optional_decimal((markout.get("300") or {}).get("avg_adverse_ticks")) if markout.get("300") else None,
                markout_1000ms=_optional_decimal((markout.get("1000") or {}).get("avg_adverse_ticks")) if markout.get("1000") else None,
                markout_2000ms=_optional_decimal((markout.get("2000") or {}).get("avg_adverse_ticks")) if markout.get("2000") else None,
            )
        )
    action_summaries.sort(key=lambda item: item.turnover_quote, reverse=True)

    largest_turnover = action_summaries[0].action_class if action_summaries else None
    worst_realized = None
    non_zero_realized = [item for item in action_summaries if item.turnover_quote > 0]
    if non_zero_realized:
        worst_realized = min(non_zero_realized, key=lambda item: item.realized_pnl_quote).action_class

    start_ts_ms = events[0][0]
    end_ts_ms = events[-1][0]
    turnover_quote = Decimal(analysis["turnover_quote"])
    realized_pnl_quote = Decimal(analysis["realized_pnl_quote"])
    gross_spread_capture_quote = Decimal(analysis["gross_spread_capture_quote"])
    return ProfitabilityReport(
        title=title,
        run_id=run_id,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        event_count=len(events),
        fill_count=int(analysis["fill_count"]),
        turnover_quote=turnover_quote,
        realized_pnl_quote=realized_pnl_quote,
        realized_per_10k_turnover=realized_per_10k_turnover(
            realized_pnl_quote=realized_pnl_quote,
            turnover_quote=turnover_quote,
        ),
        gross_spread_capture_quote=gross_spread_capture_quote,
        gross_spread_capture_per_10k=realized_per_10k_turnover(
            realized_pnl_quote=gross_spread_capture_quote,
            turnover_quote=turnover_quote,
        ),
        roundtrip_closure_rate=_safe_ratio(
            Decimal(analysis["matched_turnover_quote"]),
            turnover_quote,
        ),
        cancel_after_terminal_rate=_safe_ratio(
            Decimal(analysis["cancel_after_terminal_count"]),
            Decimal(analysis["cancel_attempt_count"]),
        ),
        same_price_amend_rate=_safe_ratio(
            Decimal(analysis["same_price_amend_count"]),
            Decimal(analysis["amend_count"]),
        ),
        action_summaries=action_summaries,
        largest_turnover_action_class=largest_turnover,
        worst_realized_action_class=worst_realized,
        markout_is_approximate=bool(markout_summary),
    )


def _render_profitability_section(
    *,
    config: BotConfig,
    title: str,
    run_id: str | None = None,
    since_ts_ms: int | None = None,
) -> str:
    report = build_profitability_report(
        config,
        title=title,
        run_id=run_id,
        since_ts_ms=since_ts_ms,
    )
    lines = [title]
    if report.run_id:
        lines.append(f"- run_id={report.run_id}")
    if report.event_count == 0:
        lines.append("- 无事件")
        return "\n".join(lines)

    lines.extend(
        [
            f"- 时间范围={_fmt_ts_ms(report.start_ts_ms)} -> {_fmt_ts_ms(report.end_ts_ms)}",
            f"- 事件数={report.event_count} | 成交次数={report.fill_count}",
            f"- 成交额(U)={_fmt(report.turnover_quote)} | 已实现(U)={_fmt_signed(report.realized_pnl_quote)} | 已实现/1万成交={_fmt(report.realized_per_10k_turnover)}",
            f"- 毛价差捕获(U,近似)={_fmt_signed(report.gross_spread_capture_quote)} | 毛价差/1万成交(近似)={_fmt(report.gross_spread_capture_per_10k)}",
            f"- 往返闭环率={_fmt_pct(report.roundtrip_closure_rate)} | 终态撤单率={_fmt_pct(report.cancel_after_terminal_rate)} | 同价改单率={_fmt_pct(report.same_price_amend_rate)}",
            f"- 最大成交动作={report.largest_turnover_action_class or '-'} | 最差已实现动作={report.worst_realized_action_class or '-'}",
        ]
    )
    if report.markout_is_approximate:
        lines.append("- markout_after_fill_by_reason_bucket 使用 state snapshot 汇总，属于近似值，不保证与窗口或 run 严格对齐")
    if report.action_summaries:
        lines.append("- 动作归因:")
        for item in report.action_summaries:
            lines.append(
                "  - "
                f"{item.action_class}: fills={item.fill_count} "
                f"turnover={_fmt(item.turnover_quote)}U "
                f"realized={_fmt_signed(item.realized_pnl_quote)}U "
                f"per10k={_fmt(item.realized_per_10k_turnover)} "
                f"gross_per10k~={_fmt(item.gross_spread_capture_per_10k)} "
                f"markout300={_fmt(item.markout_300ms)} "
                f"markout1000={_fmt(item.markout_1000ms)} "
                f"markout2000={_fmt(item.markout_2000ms)}"
            )
    return "\n".join(lines)


def _load_filtered_events(
    sqlite_path: str,
    *,
    run_id: str | None = None,
    since_ts_ms: int | None = None,
) -> list[tuple[int, str, dict[str, Any]]]:
    path = Path(sqlite_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        sql = "SELECT ts_ms, event, payload_json FROM audit_events"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if since_ts_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(since_ts_ms))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [(int(ts_ms), str(event), json.loads(payload_json)) for ts_ms, event, payload_json in rows]


def _since_ts_ms_for_window(sqlite_path: str, *, window_hours: int) -> int | None:
    path = Path(sqlite_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT MAX(ts_ms) FROM audit_events").fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    return int(row[0]) - max(int(window_hours), 0) * 60 * 60 * 1000


def _analyze_profitability_events(events: list[tuple[int, str, dict[str, Any]]]) -> dict[str, Any]:
    latest_intents_by_side: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    reason_by_order: dict[str, str] = {}
    prev_filled_by_order: dict[str, Decimal] = {}
    lots: list[dict[str, Any]] = []
    fill_count_by_action: Counter[str] = Counter()
    turnover_by_action: dict[str, Decimal] = Counter()  # type: ignore[assignment]
    pnl_by_action: dict[str, Decimal] = Counter()  # type: ignore[assignment]
    gross_spread_by_action: dict[str, Decimal] = Counter()  # type: ignore[assignment]

    fill_count = 0
    turnover_quote = Decimal("0")
    realized_pnl_quote = Decimal("0")
    gross_spread_capture_quote = Decimal("0")
    matched_turnover_quote = Decimal("0")
    cancel_attempt_count = 0
    cancel_after_terminal_count = 0
    amend_count = 0
    same_price_amend_count = 0

    for _, event, payload in events:
        if event == "decision":
            latest_intents_by_side = _extract_decision_intents(payload)
            continue

        if event in {"place_order", "shadow_quote"}:
            cl_ord_id = str(payload.get("clOrdId") or payload.get("cl_ord_id") or "")
            if cl_ord_id:
                reason = str(payload.get("reason") or "")
                if not reason:
                    side = str(payload.get("side") or "")
                    price = _optional_decimal(payload.get("px") or payload.get("price")) or Decimal("0")
                    size = _optional_decimal(payload.get("sz") or payload.get("base_size")) or Decimal("0")
                    reason = _infer_reason_from_decision(
                        side=side,
                        price=price,
                        size=size,
                        intents=latest_intents_by_side.get(side, []),
                    )
                if reason:
                    reason_by_order[cl_ord_id] = reason
            continue

        if event in {"amend_order_submitted", "shadow_amend_order"}:
            amend_count += 1
            old_price = _optional_decimal(payload.get("old_price"))
            new_price = _optional_decimal(payload.get("new_price"))
            if old_price is not None and new_price is not None and old_price == new_price:
                same_price_amend_count += 1
            cl_ord_id = str(payload.get("cl_ord_id") or payload.get("clOrdId") or "")
            reason = str(payload.get("reason") or "")
            if cl_ord_id and reason:
                reason_by_order[cl_ord_id] = reason
            continue

        if event in {"cancel_order", "cancel_order_terminal", "shadow_cancel"}:
            cancel_attempt_count += 1
            if event == "cancel_order_terminal":
                cancel_after_terminal_count += 1
            continue

        if event != "order_update":
            continue

        order = payload.get("order") or {}
        cl_ord_id = str(order.get("cl_ord_id") or order.get("clOrdId") or "")
        if not cl_ord_id:
            continue
        reason = str(payload.get("reason") or reason_by_order.get(cl_ord_id) or "")
        if reason:
            reason_by_order[cl_ord_id] = reason
        action_class = _classify_action_class(reason)

        filled_size = _optional_decimal(order.get("filled_size")) or Decimal("0")
        prev_filled = prev_filled_by_order.get(cl_ord_id, Decimal("0"))
        fill_delta = filled_size - prev_filled
        prev_filled_by_order[cl_ord_id] = filled_size
        if fill_delta <= 0:
            continue

        raw = payload.get("raw") or {}
        fill_price = _optional_decimal(raw.get("fillPx") or order.get("price")) or Decimal("0")
        side = str(order.get("side") or "")
        fill_turnover = fill_delta * fill_price

        fill_count += 1
        turnover_quote += fill_turnover
        fill_count_by_action[action_class] += 1
        turnover_by_action[action_class] += fill_turnover

        remaining = fill_delta
        if side == "buy":
            while remaining > 0 and lots and Decimal(lots[0]["qty"]) < 0:
                lot = lots[0]
                lot_qty = -Decimal(lot["qty"])
                matched = min(remaining, lot_qty)
                pnl = matched * (Decimal(lot["price"]) - fill_price)
                matched_turnover_quote += matched * (Decimal(lot["price"]) + fill_price)
                pnl_by_action[action_class] += pnl
                gross_spread_by_action[action_class] += pnl
                realized_pnl_quote += pnl
                gross_spread_capture_quote += pnl
                lot["qty"] = Decimal(lot["qty"]) + matched
                remaining -= matched
                if Decimal(lot["qty"]) == 0:
                    lots.pop(0)
            if remaining > 0:
                lots.append({"qty": remaining, "price": fill_price, "action_class": action_class})
        elif side == "sell":
            while remaining > 0 and lots and Decimal(lots[0]["qty"]) > 0:
                lot = lots[0]
                lot_qty = Decimal(lot["qty"])
                matched = min(remaining, lot_qty)
                pnl = matched * (fill_price - Decimal(lot["price"]))
                matched_turnover_quote += matched * (Decimal(lot["price"]) + fill_price)
                pnl_by_action[action_class] += pnl
                gross_spread_by_action[action_class] += pnl
                realized_pnl_quote += pnl
                gross_spread_capture_quote += pnl
                lot["qty"] = Decimal(lot["qty"]) - matched
                remaining -= matched
                if Decimal(lot["qty"]) == 0:
                    lots.pop(0)
            if remaining > 0:
                lots.append({"qty": -remaining, "price": fill_price, "action_class": action_class})

    return {
        "fill_count": fill_count,
        "turnover_quote": turnover_quote,
        "realized_pnl_quote": realized_pnl_quote,
        "gross_spread_capture_quote": gross_spread_capture_quote,
        "matched_turnover_quote": matched_turnover_quote,
        "cancel_attempt_count": cancel_attempt_count,
        "cancel_after_terminal_count": cancel_after_terminal_count,
        "amend_count": amend_count,
        "same_price_amend_count": same_price_amend_count,
        "fill_count_by_action": fill_count_by_action,
        "turnover_by_action": turnover_by_action,
        "pnl_by_action": pnl_by_action,
        "gross_spread_by_action": gross_spread_by_action,
    }


def _load_markout_by_action_class(state_path: str) -> dict[str, dict[str, dict[str, Any]]]:
    path = Path(state_path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    by_reason = payload.get("fill_markout_summary_by_reason")
    if not isinstance(by_reason, dict):
        return {}

    aggregated: dict[str, dict[str, dict[str, Decimal | int]]] = {}
    for reason_bucket, per_window in by_reason.items():
        action_class = _classify_action_class(str(reason_bucket or ""))
        action_windows = aggregated.setdefault(action_class, {})
        if not isinstance(per_window, dict):
            continue
        for window_ms, window_payload in per_window.items():
            if not isinstance(window_payload, dict):
                continue
            samples = int(window_payload.get("samples") or 0)
            avg = _optional_decimal(window_payload.get("avg_adverse_ticks"))
            window = action_windows.setdefault(str(window_ms), {"samples": 0, "weighted_sum": Decimal("0")})
            window["samples"] = int(window["samples"]) + samples
            if avg is not None and samples > 0:
                window["weighted_sum"] = Decimal(window["weighted_sum"]) + avg * Decimal(samples)

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for action_class, per_window in aggregated.items():
        result[action_class] = {}
        for window_ms, stats in per_window.items():
            sample_count = int(stats["samples"])
            avg = None
            if sample_count > 0:
                avg = Decimal(stats["weighted_sum"]) / Decimal(sample_count)
            result[action_class][window_ms] = {
                "samples": sample_count,
                "avg_adverse_ticks": avg,
            }
    return result


def _classify_action_class(reason: str | None) -> str:
    value = str(reason or "").strip()
    bucket = classify_reason_bucket(value)
    if bucket == "release" or value.startswith("release"):
        return "release"
    if bucket in {"rebalance", "secondary"} or value.startswith("fill_rebalance") or "rebalance" in value:
        return "rebalance"
    if "resync" in value or "reconnect" in value or value.startswith("startup_recovery"):
        return "resync"
    if bucket in {"entry", "strict_cycle"} or value.startswith("join_") or value.startswith("inventory_") or value.endswith("_bid_only") or value.endswith("_ask_only"):
        return "entry"
    if value:
        return "protective"
    return "unknown"


def _extract_decision_intents(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    decision = (payload or {}).get("decision") or {}
    result: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    for side_key, normalized_side in (("bid_layers", "buy"), ("ask_layers", "sell")):
        for layer in decision.get(side_key) or []:
            if not isinstance(layer, dict):
                continue
            result[normalized_side].append(
                {
                    "reason": str(layer.get("reason") or ""),
                    "price": _optional_decimal(layer.get("price")) or Decimal("0"),
                    "base_size": _optional_decimal(layer.get("base_size")) or Decimal("0"),
                }
            )
    return result


def _infer_reason_from_decision(*, side: str, price: Decimal, size: Decimal, intents: list[dict[str, Any]]) -> str:
    for intent in intents:
        if intent["price"] == price and intent["base_size"] == size:
            return str(intent["reason"] or "")
    for intent in intents:
        if intent["price"] == price:
            return str(intent["reason"] or "")
    return ""


def _fmt_ts_ms(value: int | None) -> str:
    if value is None:
        return "-"
    return str(value)


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return numerator / denominator
