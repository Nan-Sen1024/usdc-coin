from __future__ import annotations

import sys
from decimal import Decimal

from .config import TelemetryConfig
from .market_gate import evaluate_market_gate, normalize_instruments
from .models import QuoteDecision, RiskStatus
from .state import BotState
from .utils import decimal_to_str, now_ms


class TerminalStatusPanel:
    def __init__(
        self,
        *,
        config: TelemetryConfig,
        mode: str,
        simulated: bool = False,
        stream=None,
        live_allowed_instruments: tuple[str, ...] | list[str] | None = None,
        observe_only_instruments: tuple[str, ...] | list[str] | None = None,
        release_only_mode: bool = False,
        release_only_base_buffer: Decimal = Decimal("0"),
    ):
        self.config = config
        self.mode = mode
        self.simulated = simulated
        self.stream = stream or sys.stdout
        self._last_render_ms = 0
        self.live_allowed_instruments = normalize_instruments(live_allowed_instruments)
        self.observe_only_instruments = normalize_instruments(observe_only_instruments)
        self.release_only_mode = release_only_mode
        self.release_only_base_buffer = release_only_base_buffer

    def maybe_render(self, *, state: BotState, risk_status: RiskStatus, decision: QuoteDecision) -> None:
        if not self.config.status_panel_enabled:
            return
        interactive = getattr(self.stream, "isatty", lambda: False)()
        if not interactive and not self.config.status_panel_render_non_interactive:
            return
        interval_ms = max(int(self.config.status_panel_interval_seconds * 1000), 0)
        current = now_ms()
        if interval_ms and current - self._last_render_ms < interval_ms:
            return
        self._last_render_ms = current

        payload = self.build_text(state=state, risk_status=risk_status, decision=decision)
        prefix = "\x1b[2J\x1b[H" if interactive and self.config.status_panel_clear_screen else ""
        self.stream.write(prefix + payload + "\n")
        self.stream.flush()

    def build_text(self, *, state: BotState, risk_status: RiskStatus, decision: QuoteDecision) -> str:
        book = state.book
        bid = book.best_bid.price if book and book.best_bid else None
        ask = book.best_ask.price if book and book.best_ask else None
        spread_ticks = decision.spread_ticks if decision.spread_ticks else Decimal("0")
        book_age_ms = now_ms() - book.last_update_ms if book else -1
        inventory_ratio = state.inventory_ratio()
        nav = state.nav_quote()
        day_pnl = state.daily_pnl_quote()
        strategy_peak = state.strategy_nav_peak_quote
        strategy_drawdown = state.strategy_drawdown_quote()
        account_equity = state.account_equity_quote()
        account_peak = state.account_total_eq_peak_quote
        account_drawdown = state.account_drawdown_quote()
        shadow_unrealized = state.shadow_unrealized_pnl_quote()
        live_realized = state.live_realized_pnl_quote
        live_unrealized = state.live_unrealized_pnl_quote()
        live_position_base = state.strategy_position_base()
        fill_count = state.shadow_fill_count if self.mode == "shadow" else state.observed_fill_count
        fill_volume = state.shadow_fill_volume_quote if self.mode == "shadow" else state.observed_fill_volume_quote
        base_ccy = state.instrument.base_ccy if state.instrument else "BASE"

        pnl_parts = [
            f"策略净值(U)={self._fmt_dec(nav)}",
            f"本轮盈亏(U)={self._fmt_signed(day_pnl)}",
            f"成交次数={fill_count}",
        ]
        drawdown_parts = [
            f"策略峰值(U)={self._fmt_dec(strategy_peak)}",
            f"策略回撤(U)={self._fmt_signed(strategy_drawdown)}",
            f"账户权益(U)={self._fmt_dec(account_equity)}",
            f"账户峰值(U)={self._fmt_dec(account_peak)}",
            f"账户回撤(U)={self._fmt_signed(account_drawdown)}",
        ]
        if self.mode == "shadow":
            pnl_parts.append(f"已实现(U)={self._fmt_signed(state.shadow_realized_pnl_quote)}")
            pnl_parts.append(f"未实现(U)={self._fmt_signed(shadow_unrealized)}")
        else:
            pnl_parts.append(f"已实现(U)={self._fmt_signed(live_realized)}")
            pnl_parts.append(f"库存浮盈(U)={self._fmt_signed(live_unrealized)}")
            pnl_parts.append(f"待回补仓位({base_ccy})={self._fmt_signed(live_position_base)}")
            pnl_parts.append(f"成交估算额(U)={self._fmt_dec(fill_volume)}")

        lines = [
            (
                f"trend_bot_6 | 模式={self._translate_mode(self.mode, self.simulated)} | "
                f"状态={self._translate_reason(state.runtime_state)} | "
                f"原因={self._translate_reason(state.runtime_reason or '-')}"
            ),
            (
                "行情 | "
                f"买一={self._fmt_dec(bid)} 卖一={self._fmt_dec(ask)} "
                f"价差tick={self._fmt_dec(spread_ticks)} 盘口年龄毫秒={book_age_ms}"
            ),
            f"市场成交 | {self._fmt_market_trade(state)}",
            (
                "流状态 | "
                f"公共={self._fmt_bool(state.stream_status.get('public_books5', False))} "
                f"私有={self._fmt_bool(state.stream_status.get('private_user', False))} "
                f"待重同步={self._fmt_bool(state.resync_required)}"
            ),
            (
                "余额 | "
                f"{self._fmt_balance(state, state.instrument.base_ccy if state.instrument else 'BASE')} | "
                f"{self._fmt_balance(state, state.instrument.quote_ccy if state.instrument else 'QUOTE')}"
            ),
        ]
        if self.release_only_mode:
            lines.append(self._fmt_release_status(state=state, decision=decision))
        if state.triangle_route_diagnostics or state.triangle_exit_route_choice:
            lines.append(self._fmt_triangle_route_status(state=state))
        lines.extend(
            [
                "盈亏 | " + " ".join(pnl_parts),
                "回撤 | " + " ".join(drawdown_parts),
                (
                    "策略 | "
                    f"库存占比={self._fmt_pct(inventory_ratio)} "
                    f"允许买单={self._fmt_bool(risk_status.allow_bid)} 允许卖单={self._fmt_bool(risk_status.allow_ask)} "
                    f"风控={self._translate_reason(risk_status.reason)}"
                ),
                (
                    "决策 | "
                    f"原因={self._translate_reason(decision.reason)} "
                    f"买单={self._fmt_intents(decision.bid_layers)} "
                    f"卖单={self._fmt_intents(decision.ask_layers)}"
                ),
                f"挂单 | {self._fmt_orders(state)}",
                f"最近成交 | {self._fmt_trade(state)}",
            ]
        )
        current_inst_id = state.instrument.inst_id if state.instrument else "-"
        if self.mode == "live" and current_inst_id != "-":
            market_gate = evaluate_market_gate(
                inst_id=current_inst_id,
                live_allowed_instruments=self.live_allowed_instruments,
                observe_only_instruments=self.observe_only_instruments,
            )
            lines.insert(
                2,
                f"market_gate | current={current_inst_id} role={market_gate.role} live={self._fmt_bool(market_gate.live_allowed)}",
            )
        return "\n".join(lines)

    def _fmt_release_status(self, *, state: BotState, decision: QuoteDecision) -> str:
        buffer = max(self.release_only_base_buffer, Decimal("0"))
        releasable = state.external_release_base_size(base_buffer=buffer)
        action = "待机"
        if decision.reason == "release_external_sell_only":
            action = "释放中"
        elif any(getattr(intent, "reason", "") == "release_external_long" for intent in decision.ask_layers):
            action = "释放中"
        return (
            "释放 | "
            f"模式={self._fmt_bool(self.release_only_mode)} "
            f"外部库存剩余={self._fmt_dec(state.external_base_inventory_remaining)} "
            f"保留量={self._fmt_dec(buffer)} "
            f"可释放={self._fmt_dec(releasable)} "
            f"当前动作={action}"
        )

    def _fmt_triangle_route_status(self, *, state: BotState) -> str:
        diagnostics = state.triangle_route_diagnostics or {}
        choice = state.triangle_exit_route_choice or {}
        return (
            "路由 | "
            f"快照={diagnostics.get('snapshot_status') or '-'} "
            f"快照年龄毫秒={diagnostics.get('snapshot_age_ms') if diagnostics.get('snapshot_age_ms') is not None else '-'} "
            f"库存路由状态={diagnostics.get('route_status') or '-'} "
            f"入口买入={diagnostics.get('entry_buy_gate_status') or '-'} "
            f"入口原因={diagnostics.get('entry_buy_gate_reason') or '-'} "
            f"主路={choice.get('primary_route') or '-'} "
            f"备路={choice.get('backup_route') or '-'} "
            f"方向={choice.get('direction') or '-'} "
            f"主参考价={self._fmt_dec(choice.get('primary_reference_price'))} "
            f"备参考价={self._fmt_dec(choice.get('backup_reference_price'))} "
            f"改善bp={self._fmt_dec(choice.get('improvement_bp'))}"
        )

    def _fmt_balance(self, state: BotState, ccy: str) -> str:
        balance = state.balances.get(ccy)
        if not balance:
            return f"{ccy}: 总额=0 可用=0 冻结=0"
        return (
            f"{ccy}: 总额={self._fmt_dec(balance.total)} "
            f"可用={self._fmt_dec(balance.available)} "
            f"冻结={self._fmt_dec(balance.frozen)}"
        )

    def _fmt_orders(self, state: BotState) -> str:
        orders = state.bot_orders()
        if not orders:
            return "无"
        parts = []
        for order in orders:
            parts.append(
                (
                    f"{self._translate_side(order.side)}单 "
                    f"价格={self._fmt_dec(order.price)} "
                    f"数量={self._fmt_dec(order.remaining_size)} "
                    f"约={self._fmt_dec(order.remaining_size * order.price)}U "
                    f"排队前手={self._fmt_dec(order.queue_ahead_size)} "
                    f"年龄毫秒={max(now_ms() - order.created_at_ms, 0)}"
                )
            )
        return " | ".join(parts)

    def _fmt_trade(self, state: BotState) -> str:
        trade = state.last_trade
        if not trade:
            return "无"
        age_ms = max(now_ms() - trade.last_update_ms, 0)
        parts = [
            f"方向={self._translate_side(trade.side)}",
            f"委托价={self._fmt_dec(trade.order_price) if trade.order_price is not None else '-'}",
            f"成交价={self._fmt_dec(trade.price)}",
            f"数量={self._fmt_dec(trade.size)}",
        ]
        if trade.trade_id:
            parts.append(f"订单号={trade.trade_id}")
        parts.append(f"年龄毫秒={age_ms}")
        return " ".join(parts)

    def _fmt_market_trade(self, state: BotState) -> str:
        trade = state.last_market_trade
        if not trade:
            return "无"
        age_ms = max(now_ms() - trade.last_update_ms, 0)
        parts = [
            f"方向={self._translate_side(trade.side)}",
            f"价格={self._fmt_dec(trade.price)}",
            f"数量={self._fmt_dec(trade.size)}",
        ]
        if trade.trade_id:
            parts.append(f"成交ID={trade.trade_id}")
        parts.append(f"年龄毫秒={age_ms}")
        return " ".join(parts)

    def _fmt_intent(self, intent) -> str:
        if intent is None:
            return "-"
        if intent.base_size is not None:
            return (
                f"{self._translate_side(intent.side)}单 价格={self._fmt_dec(intent.price)} "
                f"数量={self._fmt_dec(intent.base_size)} 约{self._fmt_dec(intent.quote_notional)}U"
            )
        return (
            f"{self._translate_side(intent.side)}单 价格={self._fmt_dec(intent.price)} "
            f"目标金额={self._fmt_dec(intent.quote_notional)}U"
        )

    def _fmt_intents(self, intents) -> str:
        if not intents:
            return "-"
        return " | ".join(self._fmt_intent(intent) for intent in intents)

    @staticmethod
    def _fmt_dec(value: Decimal | None) -> str:
        if value is None:
            return "-"
        return decimal_to_str(value)

    @staticmethod
    def _fmt_pct(value: Decimal | None) -> str:
        if value is None:
            return "-"
        return decimal_to_str(value * Decimal("100")) + "%"

    @staticmethod
    def _fmt_signed(value: Decimal | None) -> str:
        if value is None:
            return "-"
        prefix = "+" if value > 0 else ""
        return prefix + decimal_to_str(value)

    @staticmethod
    def _fmt_bool(value: bool) -> str:
        return "是" if value else "否"

    @staticmethod
    def _translate_mode(value: str, simulated: bool = False) -> str:
        if value == "live" and simulated:
            return "OKX模拟盘"
        return {"live": "实盘", "shadow": "影子模拟"}.get(value, value)

    @staticmethod
    def _translate_side(value: str) -> str:
        return {"buy": "买", "sell": "卖"}.get(value, value or "-")

    @staticmethod
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
            "release_external_sell_only": "释放模式：只挂卖单",
            "release_only_idle": "释放模式待机",
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
