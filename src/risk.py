from __future__ import annotations

from decimal import Decimal

from .config import RiskConfig, TradingConfig
from .models import RiskStatus
from .state import BotState
from .utils import now_ms


class RiskManager:
    def __init__(self, config: RiskConfig, trading: TradingConfig, *, mode: str):
        self.config = config
        self.trading = trading
        self.mode = mode

    def evaluate(self, state: BotState) -> RiskStatus:
        if state.runtime_state == "STOPPED":
            return RiskStatus(ok=False, reason=state.runtime_reason or "stopped", allow_bid=False, allow_ask=False, runtime_state="STOPPED")

        if self.mode == "live" and not state.streams_ready(
            require_public=self.config.require_public_stream_ready,
            require_private=self.config.require_private_stream_ready,
        ):
            return RiskStatus(ok=False, reason="streams not ready", allow_bid=False, allow_ask=False, runtime_state="INIT")

        if state.resync_required:
            return RiskStatus(ok=False, reason=f"resync required: {state.resync_reason}", allow_bid=False, allow_ask=False, runtime_state="PAUSED")

        if state.is_pause_active():
            remaining_ms = max(state.pause_until_ms - now_ms(), 0)
            return RiskStatus(ok=False, reason=f"pause active: {remaining_ms}ms", allow_bid=False, allow_ask=False, runtime_state="PAUSED")

        if not state.instrument or not state.book:
            return RiskStatus(ok=False, reason="missing market bootstrap", allow_bid=False, allow_ask=False, runtime_state="INIT")

        if str(state.instrument.state).lower() != "live":
            return RiskStatus(
                ok=False,
                reason=f"instrument state is {state.instrument.state}",
                allow_bid=False,
                allow_ask=False,
                runtime_state="STOPPED",
            )

        public_stream_age_ms = state.stream_activity_age_ms("public_books5")
        if public_stream_age_ms is None:
            book_age_ms = now_ms() - state.book.last_update_ms
            if book_age_ms > self.config.stale_book_ms:
                return RiskStatus(
                    ok=False,
                    reason=f"stale book: {book_age_ms}ms",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="PAUSED",
                )
        elif public_stream_age_ms > self.config.stale_book_ms:
            return RiskStatus(
                ok=False,
                reason=f"stale public stream: {public_stream_age_ms}ms",
                allow_bid=False,
                allow_ask=False,
                runtime_state="PAUSED",
            )

        if self.mode == "live" and self.config.require_private_stream_ready:
            private_stream_age_ms = state.stream_activity_age_ms("private_user")
            if private_stream_age_ms is not None and private_stream_age_ms > self.config.stale_book_ms:
                return RiskStatus(
                    ok=False,
                    reason=f"stale private stream: {private_stream_age_ms}ms",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="PAUSED",
                )

        reconnect_count = state.reconnect_count_5m()
        if reconnect_count > self.config.max_reconnects_per_5m and not state.streams_ready(
            require_public=self.config.require_public_stream_ready,
            require_private=self.config.require_private_stream_ready,
        ):
            return RiskStatus(ok=False, reason="too many reconnects in 5m", allow_bid=False, allow_ask=False, runtime_state="PAUSED")

        if state.consecutive_place_failures >= self.config.max_consecutive_place_failures:
            remaining_ms = state.place_failure_cooldown_remaining_ms(self.config.place_failure_cooldown_seconds)
            if remaining_ms > 0:
                return RiskStatus(
                    ok=False,
                    reason=f"place failure cooldown: {remaining_ms}ms",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="PAUSED",
                )
            state.reset_place_failures()

        if state.consecutive_cancel_failures >= self.config.max_consecutive_cancel_failures:
            remaining_ms = state.cancel_failure_cooldown_remaining_ms(self.config.cancel_failure_cooldown_seconds)
            if remaining_ms > 0:
                return RiskStatus(
                    ok=False,
                    reason=f"cancel failure cooldown: {remaining_ms}ms",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="PAUSED",
                )
            state.reset_cancel_failures()

        if (
            self.config.realized_loss_shutdown_quote > 0
            and state.live_realized_pnl_quote <= -self.config.realized_loss_shutdown_quote
        ):
            return RiskStatus(
                ok=False,
                reason=f"realized loss limit hit: {state.live_realized_pnl_quote}",
                allow_bid=False,
                allow_ask=False,
                runtime_state="STOPPED",
            )

        if state.book.mid is not None and self.config.peg_reference_price > 0:
            deviation_bps = (abs(state.book.mid - self.config.peg_reference_price) / self.config.peg_reference_price) * Decimal("10000")
            if deviation_bps >= self.config.max_mid_deviation_bps:
                return RiskStatus(
                    ok=False,
                    reason=f"peg deviation too high: {deviation_bps}bps",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="PAUSED",
                )

        if self.mode == "live" and self.config.enforce_effective_fee_gate and state.fee_snapshot:
            if state.fee_snapshot.effective_maker > self.config.max_effective_maker_fee_rate:
                return RiskStatus(
                    ok=False,
                    reason=f"maker fee too high: {state.fee_snapshot.effective_maker}",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="STOPPED",
                )
            if state.fee_snapshot.effective_taker > self.config.max_effective_taker_fee_rate:
                return RiskStatus(
                    ok=False,
                    reason=f"taker fee too high: {state.fee_snapshot.effective_taker}",
                    allow_bid=False,
                    allow_ask=False,
                    runtime_state="STOPPED",
                )

        allow_bid = state.free_balance(self.trading.quote_ccy) > self.config.min_free_quote_buffer
        allow_ask = state.free_balance(self.trading.base_ccy) > self.config.min_free_base_buffer

        if state.startup_recovery_side == "sell":
            allow_bid = False
            if allow_ask:
                return RiskStatus(
                    ok=True,
                    reason="startup_recovery_sell_only",
                    allow_bid=False,
                    allow_ask=True,
                    runtime_state="REDUCE_ONLY",
                )
        if state.startup_recovery_side == "buy":
            allow_ask = False
            if allow_bid:
                return RiskStatus(
                    ok=True,
                    reason="startup_recovery_buy_only",
                    allow_bid=True,
                    allow_ask=False,
                    runtime_state="REDUCE_ONLY",
                )

        strategy_position = state.strategy_position_base()
        min_position_size = state.instrument.min_size if state.instrument else Decimal("0")
        inventory_ratio = state.inventory_ratio()
        if inventory_ratio is not None:
            hard_upper = max(self.config.hard_inventory_upper_pct, Decimal("0"))
            hard_lower = min(max(self.config.hard_inventory_lower_pct, Decimal("0")), Decimal("1"))
            if strategy_position >= min_position_size and inventory_ratio >= hard_upper:
                allow_bid = False
                if allow_ask:
                    return RiskStatus(
                        ok=True,
                        reason="reduce_only_inventory_high",
                        allow_bid=False,
                        allow_ask=True,
                        runtime_state="REDUCE_ONLY",
                    )
            if strategy_position <= -min_position_size and inventory_ratio <= hard_lower:
                allow_ask = False
                if allow_bid:
                    return RiskStatus(
                        ok=True,
                        reason="reduce_only_inventory_low",
                        allow_bid=True,
                        allow_ask=False,
                        runtime_state="REDUCE_ONLY",
                    )

        if strategy_position >= min_position_size and not allow_ask:
            return RiskStatus(
                ok=False,
                reason="bot long rebalance blocked",
                allow_bid=False,
                allow_ask=False,
                runtime_state="PAUSED",
            )
        if strategy_position <= -min_position_size and not allow_bid:
            return RiskStatus(
                ok=False,
                reason="bot short rebalance blocked",
                allow_bid=False,
                allow_ask=False,
                runtime_state="PAUSED",
            )

        if not allow_bid and not allow_ask:
            return RiskStatus(ok=False, reason="inventory/balance blocks both sides", allow_bid=False, allow_ask=False, runtime_state="PAUSED")

        return RiskStatus(ok=True, reason="ok", allow_bid=allow_bid, allow_ask=allow_ask, runtime_state="READY")
