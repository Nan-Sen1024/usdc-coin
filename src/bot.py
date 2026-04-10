from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .audit_store import SQLiteAuditStore
from .binance_market_data import BinancePublicMarketStream
from .binance_private_stream import BinancePrivateUserStream
from .binance_rest import BinanceRestClient
from .config import BotConfig
from .consistency import StateConsistencyChecker
from .executor import JournalWriter, OrderExecutor
from .market_gate import evaluate_market_gate
from .market_data import PublicBookStream
from .models import FeeSnapshot
from .okx_rest import OKXRestClient
from .private_stream import PrivateUserStream
from .reason_attribution import classify_reason_bucket
from .risk import RiskManager
from .route_ledger import append_route_ledger_event, read_route_ledger_events
from .shadow import ShadowFillSimulator
from .state import BotState
from .status_panel import TerminalStatusPanel
from .strategy import MicroMakerStrategy
from .triangle_routing import SUPPORTED_TRIANGLE_PAIRS, build_triangle_quote_snapshot, compute_dual_exit_metrics, compute_inventory_route_choice
from .utils import now_ms, to_jsonable

logger = logging.getLogger(__name__)


class TrendBot6:
    def __init__(self, config: BotConfig):
        self.config = config
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.state = BotState(managed_prefix=config.managed_prefix, state_path=config.telemetry.state_path)
        self.state.configure_release_tracking(enabled=config.strategy.release_only_mode)
        self.state.configure_balance_budgets(
            base_ccy=config.trading.base_ccy,
            quote_ccy=config.trading.quote_ccy,
            base_total=config.trading.budget_base_total,
            quote_total=config.trading.budget_quote_total,
        )
        restored_state = self.state.load_persisted_accounting()
        self.audit_store = SQLiteAuditStore(
            config.telemetry.sqlite_path,
            enabled=config.telemetry.sqlite_enabled,
        )
        self.audit_store.open()
        self.journal = JournalWriter(
            config.telemetry.journal_path,
            sqlite_store=self.audit_store,
            runtime_state_getter=lambda: self.state.runtime_state,
            run_id=self.run_id,
        )
        if restored_state:
            self.journal.append("state_restored", restored_state)
        self.rest = self._build_rest_client(config)
        self.risk = RiskManager(config.risk, config.trading, mode=config.mode)
        self.strategy = MicroMakerStrategy(
            config.strategy,
            config.trading,
            max_orders_per_side=config.risk.max_managed_orders_per_side,
        )
        self.shadow_simulator = (
            ShadowFillSimulator(state=self.state, trading=config.trading, config=config.shadow, journal=self.journal)
            if config.mode == "shadow"
            else None
        )
        self.executor = OrderExecutor(
            rest=self.rest,
            state=self.state,
            config=config,
            journal=self.journal,
            shadow_simulator=self.shadow_simulator,
        )
        self.consistency_checker = StateConsistencyChecker(
            risk=config.risk,
            trading=config.trading,
            managed_prefix=config.managed_prefix,
        )
        self.status_panel = TerminalStatusPanel(
            config=config.telemetry,
            mode=config.mode,
            simulated=config.exchange.simulated,
            live_allowed_instruments=tuple(config.risk.live_allowed_instruments),
            observe_only_instruments=tuple(config.risk.observe_only_instruments),
            release_only_mode=config.strategy.release_only_mode,
            release_only_base_buffer=config.strategy.release_only_base_buffer,
        )
        self.public_stream: PublicBookStream | None = None
        self.private_stream: PrivateUserStream | None = None
        self._last_balance_poll_ms = 0
        self._last_snapshot_ms = 0
        self._last_resync_attempt_ms = 0
        self._last_triangle_route_refresh_ms = 0
        self._quote_cycle_lock = asyncio.Lock()
        self._book_requote_event = asyncio.Event()
        self._book_requote_task: asyncio.Task | None = None
        self._last_book_requote_signal_ms = 0
        self._last_book_requote_reason = "book_top_price_changed"
        self.stop_request_path = Path(config.telemetry.stop_request_path)
        self.shared_route_ledger_path = Path(config.telemetry.shared_route_ledger_path)
        self._last_triangle_route_diagnostics_json = ""
        self._clear_stale_stop_request()

    async def run(self) -> None:
        logger.info("Trend Bot 6 starting in [%s] mode", self.config.mode)
        await self._bootstrap()
        try:
            while self.state.runtime_state != "STOPPED":
                try:
                    if self._check_stop_request():
                        continue
                    await self._tick()
                except Exception as exc:
                    logger.exception("Tick failed: %s", exc)
                    error_message = self._exception_message(exc)
                    self.journal.append(
                        "tick_error",
                        {
                            "error": str(exc),
                            "error_repr": repr(exc),
                            "error_type": type(exc).__name__,
                            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                        },
                    )
                    if self._stop_for_binance_restricted_location(source="tick", exc=exc):
                        continue
                    self.state.request_resync(f"tick failure: {error_message}")
                    self.state.set_pause(
                        reason=f"tick failure: {error_message}",
                        duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
                    )
                await asyncio.sleep(self.config.trading.loop_interval_seconds)
        finally:
            await self._stop_book_requote_worker()
            await self.shutdown()

    async def shutdown(self) -> None:
        await self._stop_book_requote_worker()
        shutdown_reason = (
            self.state.runtime_reason
            if self.state.runtime_state == "STOPPED" and self.state.runtime_reason and self.state.runtime_reason != "booting"
            else "shutdown"
        )
        self.state.set_runtime_state("STOPPED", shutdown_reason)
        logger.info("Shutting down Trend Bot 6")
        if self.config.mode == "live" and self.config.risk.cancel_managed_orders_on_shutdown:
            try:
                await self.executor.cancel_all_managed_orders(reason="shutdown")
            except Exception as exc:
                logger.warning("Failed to cancel on shutdown: %s", exc)
        if self.private_stream:
            await self.private_stream.stop()
        if self.public_stream:
            await self.public_stream.stop()
        self.state.persist()
        self.audit_store.close()
        await self.rest.close()
        self._clear_stop_request_file()

    async def _bootstrap(self) -> None:
        self.state.set_runtime_state("INIT", "bootstrap")
        self.state.set_stream_status("public_books5", False)
        self.state.set_stream_status("private_user", self.config.mode != "live")

        await self.rest.sync_time_offset()
        await self._refresh_instrument(force=True)
        if not self._check_live_market_gate():
            return

        book = await self.rest.fetch_order_book(self.config.trading.inst_id, self.config.trading.bootstrap_depth)
        self.state.set_book(book)
        self.journal.append("bootstrap_instrument", {"instrument": self.state.instrument})
        self.journal.append("bootstrap_book", {"book": book})

        if self.config.mode == "live":
            balances = await self.rest.fetch_balances([self.config.trading.base_ccy, self.config.trading.quote_ccy])
            self.state.set_balances(balances)
            await self.executor.bootstrap_pending_orders()
            if not self._check_live_budget_gate():
                return
            await self._refresh_fee(force=True)
            if not await self._run_consistency_check(context="bootstrap", stop_on_failure=True):
                return
        else:
            self.state.seed_shadow_balances(
                base_ccy=self.config.trading.base_ccy,
                quote_ccy=self.config.trading.quote_ccy,
                base_balance=self.config.trading.shadow_base_balance,
                quote_balance=self.config.trading.shadow_quote_balance,
            )
            self.state.set_runtime_state("READY", "shadow bootstrap complete")

        self.public_stream = self._build_public_stream()
        await self.public_stream.start()

        if self.config.mode == "live":
            self.private_stream = self._build_private_stream()
            trade_client_name = "private_ws"
            if self._prefer_rest_trade_routing():
                trade_client_name = "rest"
            if self.config.exchange.simulated:
                self.journal.append(
                    "simulated_trade_routing",
                    {
                        "trade_client": trade_client_name,
                        "reason": (
                            "configured simulated instrument uses rest trading fallback"
                            if trade_client_name == "rest"
                            else "simulated instrument allows private ws trading"
                        ),
                        "inst_id": self.config.trading.inst_id,
                    },
                )
            if trade_client_name == "private_ws":
                self.executor.attach_trade_client(self.private_stream)
            await self.private_stream.start()

    @staticmethod
    def _build_rest_client(config: BotConfig):
        if config.exchange.name == "binance":
            return BinanceRestClient(config.exchange)
        return OKXRestClient(config.exchange)

    def _build_public_stream(self):
        if self.config.exchange.name == "binance":
            return BinancePublicMarketStream(
                url=self.config.exchange.public_ws_url,
                inst_id=self.config.trading.inst_id,
                on_book=self._on_book,
                on_trade=self._on_trade,
                on_reconnect=self._on_reconnect,
                on_status=self._on_stream_status,
                on_error=self._on_stream_error,
                on_activity=self._on_stream_activity,
                subscribe_trades=True,
            )
        return PublicBookStream(
            url=self.config.exchange.public_ws_url,
            inst_id=self.config.trading.inst_id,
            on_book=self._on_book,
            on_trade=self._on_trade,
            on_reconnect=self._on_reconnect,
            on_status=self._on_stream_status,
            on_error=self._on_stream_error,
            on_activity=self._on_stream_activity,
            subscribe_trades=True,
        )

    def _build_private_stream(self):
        if self.config.exchange.name == "binance":
            return BinancePrivateUserStream(
                url=self.config.exchange.private_ws_url,
                rest=self.rest,
                inst_id=self.config.trading.inst_id,
                on_order=self._on_order,
                on_account=self._on_account,
                on_reconnect=self._on_reconnect,
                on_status=self._on_stream_status,
                on_error=self._on_stream_error,
            )
        return PrivateUserStream(
            url=self.config.exchange.private_ws_url,
            signer=self.rest.signer,
            time_offset_ms=self.rest.time_offset_ms,
            inst_type=self.config.trading.inst_type,
            on_order=self._on_order,
            on_account=self._on_account,
            on_reconnect=self._on_reconnect,
            on_status=self._on_stream_status,
            on_error=self._on_stream_error,
            on_activity=self._on_stream_activity,
        )

    async def _tick(self) -> None:
        await self._run_quote_cycle(trigger="loop", include_maintenance=True)

    async def _run_quote_cycle(self, *, trigger: str, include_maintenance: bool) -> None:
        async with self._quote_cycle_lock:
            if include_maintenance:
                await self._refresh_balances_if_due()
                self._refresh_startup_recovery_mode()
                self._refresh_entry_profit_density_signal()
                self._refresh_rebalance_profit_density_signal()
                await self._refresh_instrument(force=False)
                await self._refresh_fee(force=False)
                await self._refresh_triangle_route_snapshot_if_due()
                self._refresh_shared_release_inventory()
                self._consume_route_ledger_events()
                await self._maybe_resync()
                if self._maybe_stop_completed_release_only_run():
                    return
            self.state.clear_pause_if_elapsed()
            self._refresh_triangle_exit_route_choice()
            self._refresh_triangle_route_diagnostics()

            risk_status = self.risk.evaluate(self.state)
            decision = self.strategy.decide(self.state, risk_status)
            self._update_runtime_state(risk_status, decision)
            self.journal.append(
                "decision",
                {
                    "runtime_state": self.state.runtime_state,
                    "trigger": trigger,
                    "risk": risk_status,
                    "decision": decision,
                    "balances": self.state.balances,
                    "live_orders": self.state.live_orders,
                    "fee_snapshot": self.state.fee_snapshot,
                },
            )
            await self.executor.reconcile(decision, risk_status=risk_status)
            self.status_panel.maybe_render(state=self.state, risk_status=risk_status, decision=decision)

            loop_ms = now_ms()
            if include_maintenance and loop_ms - self._last_snapshot_ms >= int(self.config.telemetry.snapshot_interval_seconds * 1000):
                self.state.persist()
                self._last_snapshot_ms = loop_ms

    def _maybe_stop_completed_release_only_run(self) -> bool:
        if not self.config.strategy.release_only_mode:
            return False
        instrument = self.state.instrument
        if instrument is None:
            return False
        if self.state.bot_orders():
            return False
        if self.state.strategy_position_base() >= instrument.min_size:
            return False
        if self.state.total_balance(instrument.base_ccy) >= instrument.min_size:
            return False
        if self.state.external_base_inventory_remaining >= instrument.min_size:
            return False
        reason = "release inventory fully collected"
        self.journal.append(
            "release_only_completed",
            {
                "inst_id": self.config.trading.inst_id,
                "base_ccy": instrument.base_ccy,
                "remaining_base": self.state.external_base_inventory_remaining,
                "strategy_position_base": self.state.strategy_position_base(),
            },
        )
        self.state.set_runtime_state("STOPPED", reason)
        return True

    async def _refresh_balances_if_due(self) -> None:
        if self.config.mode != "live":
            return
        loop_ms = now_ms()
        if loop_ms - self._last_balance_poll_ms < int(self.config.trading.balance_poll_interval_seconds * 1000):
            return
        try:
            balances = await self.rest.fetch_balances([self.config.trading.base_ccy, self.config.trading.quote_ccy])
        except Exception as exc:
            self._last_balance_poll_ms = loop_ms
            logger.warning("Balance refresh failed for %s: %r", self.config.trading.inst_id, exc)
            self.journal.append(
                "balance_refresh_error",
                {
                    "inst_id": self.config.trading.inst_id,
                    "base_ccy": self.config.trading.base_ccy,
                    "quote_ccy": self.config.trading.quote_ccy,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return
        self.state.set_balances(balances)
        self._last_balance_poll_ms = loop_ms

    async def _refresh_instrument(self, *, force: bool) -> None:
        loop_ms = now_ms()
        if not force and loop_ms - self.state.last_instrument_check_ms < int(self.config.risk.instrument_poll_interval_seconds * 1000):
            return
        instrument = await self.rest.fetch_instrument(self.config.trading.inst_id, self.config.trading.inst_type)
        self.state.set_instrument(instrument)

    async def _refresh_fee(self, *, force: bool) -> None:
        if self.config.mode != "live":
            return
        loop_ms = now_ms()
        if not force and loop_ms - self.state.last_fee_check_ms < int(self.config.risk.fee_poll_interval_seconds * 1000):
            return
        fee_data = await self.rest.fetch_trade_fee(self.config.trading.inst_type, self.config.trading.inst_id)
        snapshot = FeeSnapshot(
            inst_type=self.config.trading.inst_type,
            inst_id=self.config.trading.inst_id,
            maker=fee_data["maker"],
            taker=fee_data["taker"],
            effective_maker=fee_data["maker"],
            effective_taker=fee_data["taker"],
            checked_at_ms=loop_ms,
            fee_type=fee_data.get("feeType", ""),
            zero_fee_override=False,
        )
        self.state.set_fee_snapshot(snapshot)
        self.journal.append("fee_snapshot", {"snapshot": snapshot})

    async def _refresh_triangle_route_snapshot_if_due(self) -> None:
        if self.config.exchange.name != "binance":
            return
        if not self.config.strategy.triangle_routing_enabled:
            return
        if self.config.trading.inst_id not in {"USDC-USDT", "USD1-USDT"}:
            return
        loop_ms = now_ms()
        interval_ms = int(max(self.config.strategy.triangle_route_refresh_interval_seconds, 0) * 1000)
        if interval_ms > 0 and loop_ms - self._last_triangle_route_refresh_ms < interval_ms:
            return

        quotes: dict[str, dict[str, Decimal]] = {}
        current_book = self.state.book
        if (
            current_book
            and self.config.trading.inst_id in SUPPORTED_TRIANGLE_PAIRS
            and current_book.best_bid
            and current_book.best_ask
        ):
            quotes[self.config.trading.inst_id] = {
                "bid": current_book.best_bid.price,
                "ask": current_book.best_ask.price,
            }

        auxiliary_inst_ids = [inst_id for inst_id in SUPPORTED_TRIANGLE_PAIRS if inst_id != self.config.trading.inst_id]
        if self.config.exchange.name == "binance" and hasattr(self.rest, "fetch_best_bid_ask_many"):
            try:
                books = await self.rest.fetch_best_bid_ask_many(auxiliary_inst_ids)
            except Exception as exc:
                self._last_triangle_route_refresh_ms = loop_ms
                self.journal.append(
                    "triangle_route_refresh_error",
                    {
                        "inst_id": self.config.trading.inst_id,
                        "reference_inst_id": ",".join(auxiliary_inst_ids),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                return
            for inst_id, book in books.items():
                if book.best_bid and book.best_ask:
                    quotes[inst_id] = {
                        "bid": book.best_bid.price,
                        "ask": book.best_ask.price,
                    }
        else:
            for inst_id in auxiliary_inst_ids:
                try:
                    if self.config.exchange.name == "binance" and hasattr(self.rest, "fetch_best_bid_ask"):
                        book = await self.rest.fetch_best_bid_ask(inst_id)
                    else:
                        book = await self.rest.fetch_order_book(inst_id, depth=1)
                except Exception as exc:
                    self._last_triangle_route_refresh_ms = loop_ms
                    self.journal.append(
                        "triangle_route_refresh_error",
                        {
                            "inst_id": self.config.trading.inst_id,
                            "reference_inst_id": inst_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    return
                if book.best_bid and book.best_ask:
                    quotes[inst_id] = {
                        "bid": book.best_bid.price,
                        "ask": book.best_ask.price,
                    }

        self.state.set_triangle_route_snapshot(build_triangle_quote_snapshot(quotes, checked_at_ms=loop_ms))
        self._last_triangle_route_refresh_ms = loop_ms

    def _refresh_triangle_exit_route_choice(self) -> None:
        if self.config.exchange.name != "binance":
            self.state.set_triangle_exit_route_choice(None)
            return
        if not self.config.strategy.triangle_routing_enabled:
            self.state.set_triangle_exit_route_choice(None)
            return
        if self.config.trading.inst_id not in {"USDC-USDT", "USD1-USDT"}:
            self.state.set_triangle_exit_route_choice(None)
            return
        book = self.state.book
        if not book or not book.best_bid or not book.best_ask:
            self.state.set_triangle_exit_route_choice(None)
            return
        choice = compute_inventory_route_choice(
            inst_id=self.config.trading.inst_id,
            position_base=self.state.strategy_position_base(),
            current_bid=book.best_bid.price,
            current_ask=book.best_ask.price,
            snapshot=self.state.triangle_route_snapshot,
            indirect_leg_penalty_bp=self.config.strategy.triangle_indirect_leg_penalty_bp,
            prefer_indirect_min_improvement_bp=self.config.strategy.triangle_prefer_indirect_min_improvement_bp,
        )
        self.state.set_triangle_exit_route_choice(choice)

    def _refresh_triangle_route_diagnostics(self) -> None:
        diagnostics = self._build_triangle_route_diagnostics()
        self.state.set_triangle_route_diagnostics(diagnostics)
        normalized = json.dumps(to_jsonable(diagnostics or {}), sort_keys=True, ensure_ascii=False)
        if normalized == self._last_triangle_route_diagnostics_json:
            return
        self._last_triangle_route_diagnostics_json = normalized
        if diagnostics is not None:
            self.journal.append("triangle_route_diagnostics", {"diagnostics": diagnostics})

    def _build_triangle_route_diagnostics(self) -> dict[str, object] | None:
        if self.config.exchange.name != "binance":
            return None
        if not self.config.strategy.triangle_routing_enabled:
            return None

        inst_id = self.config.trading.inst_id
        position_base = self.state.strategy_position_base()
        snapshot = self.state.triangle_route_snapshot if isinstance(self.state.triangle_route_snapshot, dict) else None
        now_ref = now_ms()
        snapshot_checked_at_ms = int(snapshot.get("checked_at_ms") or 0) if snapshot else 0
        snapshot_age_ms = max(now_ref - snapshot_checked_at_ms, 0) if snapshot_checked_at_ms > 0 else None
        snapshot_status = "missing"
        snapshot_ready = False
        if snapshot is not None:
            snapshot_status = "ready"
            if (
                snapshot_checked_at_ms > 0
                and snapshot_age_ms is not None
                and snapshot_age_ms > max(self.config.strategy.triangle_snapshot_stale_ms, 0)
            ):
                snapshot_status = "stale"
            else:
                snapshot_ready = True

        route_status = "not_applicable"
        choice = self.state.triangle_exit_route_choice if isinstance(self.state.triangle_exit_route_choice, dict) else None
        if inst_id in {"USDC-USDT", "USD1-USDT"}:
            if not snapshot_ready:
                route_status = f"snapshot_{snapshot_status}"
            elif position_base == 0:
                route_status = "flat_position"
            elif not choice:
                route_status = "choice_unavailable"
            else:
                primary_route = str(choice.get("primary_route") or "")
                route_status = "indirect_preferred" if primary_route and not primary_route.startswith("direct_") else "direct_preferred"

        book = self.state.book
        entry_gate_status = "not_applicable"
        entry_gate_reason = "unsupported_inst"
        metrics: dict[str, Decimal] | None = None
        if inst_id in {"USDC-USDT", "USD1-USDT"}:
            if not book or not book.best_bid:
                entry_gate_status = "not_ready"
                entry_gate_reason = "book_missing"
            elif not snapshot_ready:
                entry_gate_status = "not_ready"
                entry_gate_reason = f"snapshot_{snapshot_status}"
            else:
                metrics = compute_dual_exit_metrics(
                    inst_id=inst_id,
                    entry_buy_price=book.best_bid.price,
                    snapshot=snapshot,
                    indirect_leg_penalty_bp=self.config.strategy.triangle_indirect_leg_penalty_bp,
                )
                if metrics is None:
                    entry_gate_status = "not_ready"
                    entry_gate_reason = "metrics_unavailable"
                else:
                    strict_edge = metrics["strict_dual_exit_edge_bp"]
                    best_edge = metrics["best_exit_edge_bp"]
                    strict_ok = strict_edge >= self.config.strategy.triangle_strict_dual_exit_edge_bp
                    best_ok = (
                        best_edge >= self.config.strategy.triangle_best_exit_edge_bp
                        and strict_edge >= -self.config.strategy.triangle_max_worst_exit_loss_bp
                    )
                    if strict_ok:
                        entry_gate_status = "allowed"
                        entry_gate_reason = "strict_edge_ok"
                    elif best_ok:
                        entry_gate_status = "allowed"
                        entry_gate_reason = "best_edge_ok"
                    elif strict_edge < -self.config.strategy.triangle_max_worst_exit_loss_bp:
                        entry_gate_status = "blocked"
                        entry_gate_reason = "worst_exit_loss_too_large"
                    elif best_edge < self.config.strategy.triangle_best_exit_edge_bp:
                        entry_gate_status = "blocked"
                        entry_gate_reason = "best_exit_edge_too_low"
                    else:
                        entry_gate_status = "blocked"
                        entry_gate_reason = "strict_exit_edge_too_low"

        diagnostics: dict[str, object] = {
            "inst_id": inst_id,
            "snapshot_status": snapshot_status,
            "snapshot_age_ms": snapshot_age_ms,
            "position_base": position_base,
            "route_status": route_status,
            "entry_buy_gate_status": entry_gate_status,
            "entry_buy_gate_reason": entry_gate_reason,
            "primary_route": str(choice.get("primary_route") or "") if choice else "",
            "backup_route": str(choice.get("backup_route") or "") if choice else "",
            "direction": str(choice.get("direction") or "") if choice else "",
            "improvement_bp": Decimal(str(choice.get("improvement_bp") or "0")) if choice else None,
        }
        if metrics is not None:
            diagnostics.update(metrics)
        return diagnostics

    def _refresh_shared_release_inventory(self) -> None:
        if not self.config.strategy.release_only_mode:
            self.state.set_shared_release_inventory_base(Decimal("0"))
            self.state.set_shared_release_inventory_improvement_bp(Decimal("0"))
            return
        total_shared = Decimal("0")
        best_improvement = Decimal("0")
        for raw_path in self.config.strategy.release_only_shared_state_paths:
            path = Path(raw_path)
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            instrument = payload.get("instrument") or {}
            if str(instrument.get("base_ccy") or "") != self.config.trading.base_ccy:
                continue
            position = Decimal(str(payload.get("strategy_position_base") or "0"))
            if position <= 0:
                continue
            route_choice = payload.get("triangle_exit_route_choice") or {}
            primary_route = str(route_choice.get("primary_route") or "")
            improvement_bp = Decimal(str(route_choice.get("improvement_bp") or "0"))
            if primary_route.startswith("direct_") or not primary_route:
                continue
            if position < self.config.strategy.release_only_shared_inventory_min_base:
                continue
            if improvement_bp < self.config.strategy.release_only_shared_inventory_min_improvement_bp:
                continue
            total_shared += position
            best_improvement = max(best_improvement, improvement_bp)
        self.state.set_shared_release_inventory_base(total_shared)
        self.state.set_shared_release_inventory_improvement_bp(best_improvement)

    def _check_live_market_gate(self) -> bool:
        if self.config.mode != "live":
            return True
        market_gate = evaluate_market_gate(
            inst_id=self.config.trading.inst_id,
            live_allowed_instruments=self.config.risk.live_allowed_instruments,
            observe_only_instruments=self.config.risk.observe_only_instruments,
        )
        if market_gate.live_allowed:
            return True

        logger.warning("Startup market gate blocked live run for %s: %s", market_gate.inst_id, market_gate.reason)
        self.journal.append(
            "startup_market_gate_blocked",
            {
                "inst_id": market_gate.inst_id,
                "reason": market_gate.reason,
                "live_allowed_instruments": list(market_gate.live_allowed_instruments),
                "observe_only_instruments": list(market_gate.observe_only_instruments),
            },
        )
        self.state.set_runtime_state("STOPPED", market_gate.reason)
        return False

    def _check_live_budget_gate(self) -> bool:
        if self.config.mode != "live":
            return True
        errors = self.state.validate_configured_budgets(
            ccys=(self.config.trading.base_ccy, self.config.trading.quote_ccy),
        )
        if not errors:
            return True
        if self.config.strategy.release_only_mode:
            self.journal.append(
                "startup_budget_gate_release_only_bypassed",
                {
                    "inst_id": self.config.trading.inst_id,
                    "budget_base_total": self.config.trading.budget_base_total,
                    "budget_quote_total": self.config.trading.budget_quote_total,
                    "exchange_base_total": self.state.exchange_total_balance(self.config.trading.base_ccy),
                    "exchange_quote_total": self.state.exchange_total_balance(self.config.trading.quote_ccy),
                },
            )
            return True
        if self._can_enter_startup_recovery(errors):
            self.state.set_startup_recovery_side("sell")
            self.journal.append(
                "startup_budget_gate_recovery_bypassed",
                {
                    "inst_id": self.config.trading.inst_id,
                    "recovery_side": "sell",
                    "reason": "; ".join(errors),
                    "strategy_position_base": self.state.strategy_position_base(),
                    "exchange_base_total": self.state.exchange_total_balance(self.config.trading.base_ccy),
                    "exchange_quote_total": self.state.exchange_total_balance(self.config.trading.quote_ccy),
                },
            )
            return True
        if self._can_clamp_startup_budget(errors):
            base_ccy = self.config.trading.base_ccy
            quote_ccy = self.config.trading.quote_ccy
            clamped_base = min(
                self.config.trading.budget_base_total,
                self.state.exchange_total_balance(base_ccy),
            )
            clamped_quote = min(
                self.config.trading.budget_quote_total,
                self.state.exchange_total_balance(quote_ccy),
            )
            self.config.trading.budget_base_total = clamped_base
            self.config.trading.budget_quote_total = clamped_quote
            self.state.configure_balance_budgets(
                base_ccy=base_ccy,
                quote_ccy=quote_ccy,
                base_total=clamped_base,
                quote_total=clamped_quote,
            )
            self.journal.append(
                "startup_budget_gate_clamped",
                {
                    "inst_id": self.config.trading.inst_id,
                    "reason": "; ".join(errors),
                    "budget_base_total": clamped_base,
                    "budget_quote_total": clamped_quote,
                    "exchange_base_total": self.state.exchange_total_balance(base_ccy),
                    "exchange_quote_total": self.state.exchange_total_balance(quote_ccy),
                },
            )
            return True

        reason = f"instance budget exceeds account balance: {'; '.join(errors)}"
        logger.warning("Startup budget gate blocked live run for %s: %s", self.config.trading.inst_id, reason)
        self.journal.append(
            "startup_budget_gate_blocked",
            {
                "inst_id": self.config.trading.inst_id,
                "reason": reason,
                "budget_base_total": self.config.trading.budget_base_total,
                "budget_quote_total": self.config.trading.budget_quote_total,
                "exchange_base_total": self.state.exchange_total_balance(self.config.trading.base_ccy),
                "exchange_quote_total": self.state.exchange_total_balance(self.config.trading.quote_ccy),
            },
        )
        self.state.set_runtime_state("STOPPED", reason)
        return False

    def _can_clamp_startup_budget(self, errors: tuple[str, ...]) -> bool:
        if self.config.exchange.name != "binance":
            return False
        if self.config.strategy.release_only_mode:
            return False
        if not errors:
            return False
        return True

    def _can_enter_startup_recovery(self, errors: tuple[str, ...]) -> bool:
        if not self.config.risk.startup_recovery_enabled:
            return False
        position = self.state.strategy_position_base()
        if position <= 0:
            return False
        base_total = self.state.exchange_total_balance(self.config.trading.base_ccy)
        if base_total < position:
            return False
        quote_ccy = self.config.trading.quote_ccy
        if not errors:
            return False
        if not all(error.startswith(f"{quote_ccy} ") for error in errors):
            return False
        return True

    def _refresh_startup_recovery_mode(self) -> None:
        if self.state.startup_recovery_side != "sell":
            return
        quote_budget = self.state.balance_budget_caps.get(self.config.trading.quote_ccy, Decimal("0"))
        position = self.state.strategy_position_base()
        quote_total = self.state.exchange_total_balance(self.config.trading.quote_ccy)
        if position <= 0 or (quote_budget > 0 and quote_total >= quote_budget):
            self.state.set_startup_recovery_side(None)
            self.journal.append(
                "startup_recovery_cleared",
                {
                    "inst_id": self.config.trading.inst_id,
                    "strategy_position_base": position,
                    "exchange_quote_total": quote_total,
                    "quote_budget": quote_budget,
                },
            )

    def _refresh_entry_profit_density_signal(self) -> None:
        if not self.config.strategy.entry_profit_density_enabled:
            self.state.set_entry_profit_density(per10k=None, size_factor=Decimal("1"))
            for side in ("buy", "sell"):
                self.state.set_entry_profit_density_by_side(side=side, per10k=None, size_factor=Decimal("1"))
            return
        turnover, realized = self._compute_profit_density_window(reason_bucket="entry", window_minutes=self.config.strategy.entry_profit_density_window_minutes)
        by_side = self._compute_profit_density_window_by_opening_side(
            reason_bucket="entry",
            window_minutes=self.config.strategy.entry_profit_density_window_minutes,
        )

        per10k = None
        size_factor = Decimal("1")
        if turnover > 0:
            per10k = (realized / turnover) * Decimal("10000")
            size_factor = self._entry_profit_density_size_factor_for_per10k(per10k)
        self.state.set_entry_profit_density(per10k=per10k, size_factor=size_factor)
        for side in ("buy", "sell"):
            side_turnover, side_realized = by_side[side]
            side_per10k = None
            side_size_factor = Decimal("1")
            if side_turnover > 0:
                side_per10k = (side_realized / side_turnover) * Decimal("10000")
                side_size_factor = self._entry_profit_density_size_factor_for_per10k(side_per10k)
            self.state.set_entry_profit_density_by_side(side=side, per10k=side_per10k, size_factor=side_size_factor)

    def _refresh_rebalance_profit_density_signal(self) -> None:
        if not self.config.strategy.rebalance_profit_density_enabled:
            self.state.set_rebalance_profit_density(per10k=None, size_factor=Decimal("1"), extra_ticks=0)
            return
        turnover, realized = self._compute_profit_density_window(
            reason_bucket="rebalance",
            window_minutes=self.config.strategy.rebalance_profit_density_window_minutes,
        )

        per10k = None
        size_factor = Decimal("1")
        extra_ticks = 0
        if turnover > 0:
            per10k = (realized / turnover) * Decimal("10000")
            if per10k <= self.config.strategy.rebalance_profit_density_hard_per10k:
                size_factor = self.config.strategy.rebalance_profit_density_hard_size_factor
                extra_ticks = self.config.strategy.rebalance_profit_density_hard_extra_ticks
            elif per10k <= self.config.strategy.rebalance_profit_density_soft_per10k:
                size_factor = self.config.strategy.rebalance_profit_density_soft_size_factor
                extra_ticks = self.config.strategy.rebalance_profit_density_soft_extra_ticks
        self.state.set_rebalance_profit_density(per10k=per10k, size_factor=size_factor, extra_ticks=extra_ticks)

    def _compute_profit_density_window(self, *, reason_bucket: str, window_minutes: int) -> tuple[Decimal, Decimal]:
        cutoff_ms = 0
        if window_minutes > 0:
            cutoff_ms = now_ms() - (int(window_minutes) * 60 * 1000)

        turnover = Decimal("0")
        realized = Decimal("0")
        lots: list[dict[str, Decimal]] = []
        prev_filled_by_order: dict[str, Decimal] = {}
        for record_ts_ms, payload in self._load_profit_density_order_updates(reason_bucket=reason_bucket, cutoff_ms=cutoff_ms):
            order = payload.get("order") or {}
            cl_ord_id = str(
                order.get("cl_ord_id")
                or order.get("clOrdId")
                or order.get("ord_id")
                or order.get("ordId")
                or ""
            )
            filled_size = Decimal(str(order.get("filled_size") or "0"))
            previous_filled = prev_filled_by_order.get(cl_ord_id, Decimal("0")) if cl_ord_id else Decimal("0")
            if cl_ord_id:
                prev_filled_by_order[cl_ord_id] = filled_size
            fill_delta = filled_size - previous_filled
            if fill_delta <= 0:
                continue

            raw = payload.get("raw") or {}
            fill_price = Decimal(str(raw.get("fillPx") or order.get("price") or "0"))
            side = str(order.get("side") or "")
            if fill_price <= 0 or side not in {"buy", "sell"}:
                continue

            in_window = cutoff_ms <= 0 or record_ts_ms <= 0 or record_ts_ms >= cutoff_ms
            if in_window:
                turnover += fill_delta * fill_price

            remaining = fill_delta
            if side == "buy":
                while remaining > 0 and lots and lots[0]["qty"] < 0:
                    lot = lots[0]
                    matched = min(remaining, -lot["qty"])
                    if in_window:
                        realized += matched * (lot["price"] - fill_price)
                    lot["qty"] += matched
                    remaining -= matched
                    if lot["qty"] == 0:
                        lots.pop(0)
                if remaining > 0:
                    lots.append({"qty": remaining, "price": fill_price})
            else:
                while remaining > 0 and lots and lots[0]["qty"] > 0:
                    lot = lots[0]
                    matched = min(remaining, lot["qty"])
                    if in_window:
                        realized += matched * (fill_price - lot["price"])
                    lot["qty"] -= matched
                    remaining -= matched
                    if lot["qty"] == 0:
                        lots.pop(0)
                if remaining > 0:
                    lots.append({"qty": -remaining, "price": fill_price})
        return turnover, realized

    def _compute_profit_density_window_by_opening_side(
        self,
        *,
        reason_bucket: str,
        window_minutes: int,
    ) -> dict[str, tuple[Decimal, Decimal]]:
        empty = {"buy": (Decimal("0"), Decimal("0")), "sell": (Decimal("0"), Decimal("0"))}

        cutoff_ms = 0
        if window_minutes > 0:
            cutoff_ms = now_ms() - (int(window_minutes) * 60 * 1000)

        turnover_by_side = {"buy": Decimal("0"), "sell": Decimal("0")}
        realized_by_side = {"buy": Decimal("0"), "sell": Decimal("0")}
        lots: list[dict[str, Decimal | str]] = []
        prev_filled_by_order: dict[str, Decimal] = {}
        for record_ts_ms, payload in self._load_profit_density_order_updates(reason_bucket=reason_bucket, cutoff_ms=cutoff_ms):
            order = payload.get("order") or {}
            cl_ord_id = str(
                order.get("cl_ord_id")
                or order.get("clOrdId")
                or order.get("ord_id")
                or order.get("ordId")
                or ""
            )
            filled_size = Decimal(str(order.get("filled_size") or "0"))
            previous_filled = prev_filled_by_order.get(cl_ord_id, Decimal("0")) if cl_ord_id else Decimal("0")
            if cl_ord_id:
                prev_filled_by_order[cl_ord_id] = filled_size
            fill_delta = filled_size - previous_filled
            if fill_delta <= 0:
                continue

            raw = payload.get("raw") or {}
            fill_price = Decimal(str(raw.get("fillPx") or order.get("price") or "0"))
            side = str(order.get("side") or "")
            if fill_price <= 0 or side not in {"buy", "sell"}:
                continue

            in_window = cutoff_ms <= 0 or record_ts_ms <= 0 or record_ts_ms >= cutoff_ms
            if in_window:
                turnover_by_side[side] += fill_delta * fill_price

            remaining = fill_delta
            if side == "buy":
                while remaining > 0 and lots and Decimal(lots[0]["qty"]) < 0:
                    lot = lots[0]
                    matched = min(remaining, -Decimal(lot["qty"]))
                    if in_window:
                        realized_by_side[str(lot["opening_side"])] += matched * (Decimal(lot["price"]) - fill_price)
                    lot["qty"] = Decimal(lot["qty"]) + matched
                    remaining -= matched
                    if Decimal(lot["qty"]) == 0:
                        lots.pop(0)
                if remaining > 0:
                    lots.append({"qty": remaining, "price": fill_price, "opening_side": side})
            else:
                while remaining > 0 and lots and Decimal(lots[0]["qty"]) > 0:
                    lot = lots[0]
                    matched = min(remaining, Decimal(lot["qty"]))
                    if in_window:
                        realized_by_side[str(lot["opening_side"])] += matched * (fill_price - Decimal(lot["price"]))
                    lot["qty"] = Decimal(lot["qty"]) - matched
                    remaining -= matched
                    if Decimal(lot["qty"]) == 0:
                        lots.pop(0)
                if remaining > 0:
                    lots.append({"qty": -remaining, "price": fill_price, "opening_side": side})

        return {
            "buy": (turnover_by_side["buy"], realized_by_side["buy"]),
            "sell": (turnover_by_side["sell"], realized_by_side["sell"]),
        }

    def _load_profit_density_order_updates(
        self,
        *,
        reason_bucket: str,
        cutoff_ms: int,
    ) -> list[tuple[int, dict[str, object]]]:
        sqlite_path = Path(self.config.telemetry.sqlite_path)
        if sqlite_path.exists():
            conn = sqlite3.connect(str(sqlite_path))
            try:
                sql = "SELECT ts_ms, payload_json FROM audit_events WHERE event = ?"
                params: list[object] = ["order_update"]
                if cutoff_ms > 0:
                    sql += " AND ts_ms >= ?"
                    params.append(int(cutoff_ms))
                sql += " ORDER BY id"
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
            updates: list[tuple[int, dict[str, object]]] = []
            for ts_ms, payload_json in rows:
                try:
                    payload = json.loads(payload_json)
                except Exception:
                    continue
                if str((payload or {}).get("reason_bucket") or "") != reason_bucket:
                    continue
                updates.append((int(ts_ms), payload))
            return updates

        journal_path = Path(self.config.telemetry.journal_path)
        if not journal_path.exists():
            return []
        updates: list[tuple[int, dict[str, object]]] = []
        with journal_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("event") != "order_update":
                    continue
                payload = record.get("payload") or {}
                if str(payload.get("reason_bucket") or "") != reason_bucket:
                    continue
                record_ts_ms = int(record.get("ts_ms") or 0)
                if cutoff_ms > 0 and record_ts_ms > 0 and record_ts_ms < cutoff_ms:
                    continue
                updates.append((record_ts_ms, payload))
        return updates

    def _entry_profit_density_size_factor_for_per10k(self, per10k: Decimal) -> Decimal:
        if per10k <= self.config.strategy.entry_profit_density_hard_per10k:
            return self.config.strategy.entry_profit_density_hard_size_factor
        if per10k <= self.config.strategy.entry_profit_density_soft_per10k:
            return self.config.strategy.entry_profit_density_soft_size_factor
        return Decimal("1")

    def _prefer_rest_trade_routing(self) -> bool:
        if self.config.exchange.name == "binance":
            return True
        return self.config.exchange.simulated and self.config.trading.inst_id in self.config.risk.simulated_rest_trade_instruments

    def _check_stop_request(self) -> bool:
        if not self.stop_request_path.exists():
            return False
        self.journal.append(
            "stop_requested",
            {
                "inst_id": self.config.trading.inst_id,
                "path": str(self.stop_request_path),
            },
        )
        self.state.set_runtime_state("STOPPED", f"stop requested: {self.stop_request_path.name}")
        return True

    def _clear_stale_stop_request(self) -> None:
        if not self.stop_request_path.exists():
            return
        try:
            self.stop_request_path.unlink()
        except OSError:
            logger.warning("Failed to clear stale stop request file: %s", self.stop_request_path)

    def _clear_stop_request_file(self) -> None:
        if not self.stop_request_path.exists():
            return
        try:
            self.stop_request_path.unlink()
        except OSError:
            logger.warning("Failed to remove stop request file during shutdown: %s", self.stop_request_path)

    async def _maybe_resync(self) -> None:
        if self.config.mode != "live" or not self.state.resync_required:
            return
        loop_ms = now_ms()
        if loop_ms - self._last_resync_attempt_ms < 1000:
            return
        self._last_resync_attempt_ms = loop_ms
        try:
            balances = await self.rest.fetch_balances([self.config.trading.base_ccy, self.config.trading.quote_ccy])
            self.state.set_balances(balances)
            await self.executor.reload_pending_orders()
            book = await self.rest.fetch_order_book(self.config.trading.inst_id, self.config.trading.bootstrap_depth)
            self.state.set_book(book)
            if not await self._run_consistency_check(context="resync", stop_on_failure=False):
                return
            self.state.clear_resync()
            if self.state.runtime_state == "PAUSED":
                self.state.set_runtime_state("READY", "resync complete")
            self.journal.append("resync_complete", {"reason": "state reloaded"})
        except Exception as exc:
            self.state.set_pause(
                reason=f"resync retry after failure: {exc}",
                duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
            )
            self.journal.append("resync_error", {"error": str(exc)})

    def _update_runtime_state(self, risk_status, decision) -> None:
        target_state = risk_status.runtime_state
        target_reason = risk_status.reason
        if risk_status.ok and target_state == "READY":
            target_state = "QUOTING" if (decision.bid or decision.ask) else "READY"
            target_reason = decision.reason
        elif risk_status.ok and target_state == "REDUCE_ONLY":
            target_reason = decision.reason
        self.state.set_runtime_state(target_state, target_reason)

    async def _run_consistency_check(self, *, context: str, stop_on_failure: bool) -> bool:
        report = self.consistency_checker.check(self.state)
        self.state.record_consistency_result(report.ok, report.reason)
        self.journal.append("consistency_check", {"context": context, "report": report})
        if report.ok:
            if context == "resync":
                self.state.clear_resync_passive_violations()
            return True

        offending_order_ids = report.offending_managed_orders
        if context == "resync" and offending_order_ids:
            offending_order_ids = self.state.note_resync_passive_violations(report.offending_managed_orders)
            if not offending_order_ids:
                self.state.request_resync(f"{context} consistency failed: {report.reason}")
                self.state.set_pause(
                    reason=f"{context} passive price check deferred: {report.reason}",
                    duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
                )
                self.journal.append(
                    "consistency_check_deferred",
                    {
                        "context": context,
                        "reason": report.reason,
                        "offending_managed_orders": report.offending_managed_orders,
                    },
                )
                return False

        if self.config.mode == "live" and report.cancel_managed and self.config.risk.cancel_managed_on_consistency_failure:
            if offending_order_ids:
                await self.executor.cancel_managed_orders(
                    cl_ord_ids=offending_order_ids,
                    reason=f"consistency_failure:{context}",
                )
            else:
                await self.executor.cancel_all_managed_orders(reason=f"consistency_failure:{context}")

        if stop_on_failure or self.state.consecutive_consistency_failures >= self.config.risk.max_consistency_failures:
            self.state.set_runtime_state("STOPPED", f"consistency failure: {report.reason}")
            return False

        self.state.request_resync(f"{context} consistency failed: {report.reason}")
        self.state.set_pause(
            reason=f"{context} consistency failed: {report.reason}",
            duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
        )
        return False

    @staticmethod
    def _exception_message(exc: Exception) -> str:
        text = str(exc).strip()
        if text:
            return f"{type(exc).__name__}: {text}"
        return repr(exc)

    async def _on_book(self, book) -> None:
        previous_book = self.state.book
        self.state.set_book(book)
        self.state.evaluate_fill_markouts()
        toxic_events = self.state.evaluate_toxic_flow(
            min_observation_ms=max(int(self.config.strategy.toxic_flow_min_observation_ms), 0),
            max_observation_ms=max(int(self.config.strategy.toxic_flow_max_observation_ms), 0),
            adverse_ticks=max(int(self.config.strategy.toxic_flow_adverse_ticks), 0),
            cooldown_ms=int(max(self.config.strategy.toxic_flow_cooldown_seconds, 0) * 1000),
        )
        for event in toxic_events:
            self.journal.append("toxic_flow_cooldown", event)
        if self.shadow_simulator:
            await self.shadow_simulator.on_book(book)
        reason = self._book_requote_reason(previous_book, book)
        if reason:
            self._signal_book_requote(reason)

    async def _on_trade(self, trade) -> None:
        self.state.set_last_market_trade(trade)
        if self.shadow_simulator:
            await self.shadow_simulator.on_trade(trade)

    async def _on_order(self, payload: dict) -> None:
        self.state.mark_stream_activity("private_user")
        normalized = dict(payload)
        inst_id = str(normalized.get("instId") or "")
        if inst_id and inst_id != self.config.trading.inst_id:
            self.journal.append(
                "order_update_ignored_foreign_inst",
                {
                    "inst_id": inst_id,
                    "expected_inst_id": self.config.trading.inst_id,
                    "cl_ord_id": normalized.get("clOrdId") or normalized.get("ordId") or "",
                },
            )
            return
        for key in ("cTime", "uTime", "fillTime"):
            value = normalized.get(key)
            if value not in (None, "", "0"):
                normalized[key] = str(self._exchange_ms_to_local_ms(int(value)))
        previous_order = self.state.live_orders.get(str(normalized.get("clOrdId") or normalized.get("ordId") or ""))
        previous_filled = previous_order.filled_size if previous_order is not None else Decimal("0")
        order = self.state.apply_order_update(normalized, source="ws_order")
        fill_delta = order.filled_size - previous_filled
        if (
            fill_delta > 0
            and self.config.strategy.release_only_mode
            and order.side == "sell"
            and self.state.order_reason(order.cl_ord_id) == "release_external_long"
        ):
            fill_price = Decimal(str(normalized.get("fillPx") or normalized.get("px") or "0"))
            if fill_price > 0:
                append_route_ledger_event(
                    self.shared_route_ledger_path,
                    {
                        "asset": self.config.trading.base_ccy,
                        "source_inst_id": self.config.trading.inst_id,
                        "side": order.side,
                        "fill_size": fill_delta,
                        "fill_price": fill_price,
                        "reason": "release_external_long",
                    },
                )
        amend_resolution = self.state.resolve_pending_amend_update(payload=normalized, order=order)
        if amend_resolution is not None:
            event, event_payload = amend_resolution
            self.journal.append(event, event_payload)
            await self._handle_amend_resolution(order=order, event=event, event_payload=event_payload)
        self.journal.append(
            "order_update",
            {
                "order": order,
                "raw": payload,
                "reason": self.state.order_reason(order.cl_ord_id) or "",
                "reason_bucket": self.state.order_reason_bucket(order.cl_ord_id),
            },
        )

    async def _handle_amend_resolution(self, *, order, event: str, event_payload: dict[str, object]) -> None:
        if event != "amend_order_error":
            return
        if order.is_terminal or order.cancel_requested:
            return
        event_reason = str(event_payload.get("reason") or self.state.order_reason(order.cl_ord_id) or "")
        reason_bucket = classify_reason_bucket(event_reason)
        if reason_bucket not in {"rebalance", "secondary"}:
            return
        self.journal.append(
            "amend_rebalance_fallback_cancel",
            {
                "cl_ord_id": order.cl_ord_id,
                "ord_id": order.ord_id,
                "side": order.side,
                "reason": event_reason,
                "reason_bucket": reason_bucket,
                "fallback_reason": "reprice_or_ttl",
            },
        )
        await self.executor._cancel_order(order, reason="reprice_or_ttl", ignore_cooldown=True)

    async def _on_account(self, payload: dict) -> None:
        self.state.mark_stream_activity("private_user")
        self.state.apply_account_update(payload)
        self.journal.append("account_update", payload)

    def _consume_route_ledger_events(self) -> None:
        if self.config.strategy.release_only_mode:
            return
        if not self.state.instrument:
            return
        new_offset, events = read_route_ledger_events(
            self.shared_route_ledger_path,
            offset=self.state.route_ledger_offset,
        )
        for event in events:
            payload = event.get("payload") or {}
            if str(payload.get("asset") or "") != self.config.trading.base_ccy:
                continue
            if str(payload.get("source_inst_id") or "") == self.config.trading.inst_id:
                continue
            fill_size = Decimal(str(payload.get("fill_size") or "0"))
            fill_price = Decimal(str(payload.get("fill_price") or "0"))
            matched = self.state.apply_external_release_fill(fill_size=fill_size, fill_price=fill_price)
            if matched > 0:
                self.journal.append(
                    "triangle_route_ledger_applied",
                    {
                        "source_inst_id": payload.get("source_inst_id") or "",
                        "asset": payload.get("asset") or "",
                        "matched_size": matched,
                        "fill_price": fill_price,
                    },
                )
        self.state.set_route_ledger_offset(new_offset)

    async def _on_stream_status(self, stream_name: str, connected: bool) -> None:
        self.state.set_stream_status(stream_name, connected)
        self.journal.append("stream_status", {"stream": stream_name, "connected": connected})
        if self.state.runtime_state == "STOPPED":
            return
        if self.config.mode == "live" and not connected:
            self.state.request_resync(f"{stream_name} disconnected")
            self.state.set_pause(
                reason=f"{stream_name} disconnected",
                duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
            )

    async def _on_stream_error(self, stream_name: str, exc: Exception) -> None:
        self.journal.append(
            "stream_error",
            {
                "stream": stream_name,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        self._stop_for_binance_restricted_location(source=f"stream:{stream_name}", exc=exc)

    async def _on_stream_activity(self, stream_name: str, activity: str) -> None:
        del activity
        self.state.mark_stream_activity(stream_name)

    def _exchange_ms_to_local_ms(self, exchange_ms: int) -> int:
        return int(exchange_ms - self.rest.time_offset_ms)

    def _is_binance_restricted_location_error(self, exc: Exception) -> bool:
        if self.config.exchange.name != "binance":
            return False
        status_code = getattr(exc, "status_code", None)
        if status_code == 451:
            return True
        message = " ".join(part for part in (str(exc), repr(exc)) if part).lower()
        return "restricted location" in message or "eligibility" in message or "http 451" in message

    def _stop_for_binance_restricted_location(self, *, source: str, exc: Exception) -> bool:
        if not self._is_binance_restricted_location_error(exc):
            return False
        self.state.set_runtime_state("STOPPED", "binance restricted location")
        self.journal.append(
            "exchange_restricted_stop",
            {
                "source": source,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return True

    async def _on_reconnect(self, stream_name: str) -> None:
        if self.state.runtime_state == "STOPPED":
            return
        logger.warning("Reconnect detected: %s", stream_name)
        self.state.mark_reconnect()
        self.state.request_resync(f"{stream_name} reconnected")
        self.state.set_pause(
            reason=f"{stream_name} reconnected",
            duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
        )
        self.journal.append("reconnect", {"stream": stream_name})
        if self.config.mode != "live":
            return

        cancel_reason = self._managed_order_cancel_reason_for_reconnect(stream_name)
        if cancel_reason is not None:
            self.journal.append(
                "reconnect_cancel_all_managed_orders",
                {
                    "stream": stream_name,
                    "cancel_reason": cancel_reason,
                },
            )
            await self.executor.cancel_all_managed_orders(reason=cancel_reason)
            return

        if stream_name == "public_books5":
            self.journal.append(
                "public_reconnect_resync_only",
                {
                    "stream": stream_name,
                    "immediate_cancel": False,
                    "configured_cancel_on_public_reconnect": self.config.risk.cancel_managed_orders_on_public_reconnect,
                },
            )

    def _managed_order_cancel_reason_for_reconnect(self, stream_name: str) -> str | None:
        if stream_name == "private_user":
            return "private_reconnect"
        if stream_name == "public_books5" and self.config.risk.cancel_managed_orders_on_public_reconnect:
            return "public_reconnect"
        return None

    def _book_requote_reason(self, previous_book, current_book) -> str | None:
        if not self.config.trading.event_driven_requote:
            return None
        if previous_book is None:
            return None

        previous_best_bid = previous_book.best_bid.price if previous_book.best_bid else None
        previous_best_ask = previous_book.best_ask.price if previous_book.best_ask else None
        current_best_bid = current_book.best_bid.price if current_book.best_bid else None
        current_best_ask = current_book.best_ask.price if current_book.best_ask else None

        if previous_best_bid == current_best_bid and previous_best_ask == current_best_ask:
            return None
        return "book_top_price_changed"

    def _signal_book_requote(self, reason: str) -> None:
        if self.state.runtime_state == "STOPPED":
            return
        self._last_book_requote_reason = reason
        self._last_book_requote_signal_ms = now_ms()
        self._book_requote_event.set()
        self._ensure_book_requote_worker()

    def _ensure_book_requote_worker(self) -> None:
        if self._book_requote_task and not self._book_requote_task.done():
            return
        self._book_requote_task = asyncio.create_task(self._book_requote_worker())

    async def _stop_book_requote_worker(self) -> None:
        if not self._book_requote_task:
            return
        self._book_requote_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._book_requote_task
        self._book_requote_task = None
        self._book_requote_event.clear()

    async def _book_requote_worker(self) -> None:
        try:
            while self.state.runtime_state != "STOPPED":
                await self._book_requote_event.wait()
                while self.state.runtime_state != "STOPPED":
                    due_ms = self._last_book_requote_signal_ms + self.config.trading.book_requote_debounce_ms
                    delay_ms = due_ms - now_ms()
                    if delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000)
                        continue

                    observed_signal_ms = self._last_book_requote_signal_ms
                    trigger = self._last_book_requote_reason
                    self._book_requote_event.clear()
                    await self._run_quote_cycle(trigger=trigger, include_maintenance=False)
                    if observed_signal_ms == self._last_book_requote_signal_ms and not self._book_requote_event.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Book reprice failed: %s", exc)
            self.journal.append("book_requote_error", {"error": str(exc)})
            self.state.request_resync(f"book reprice failure: {exc}")
            self.state.set_pause(
                reason=f"book reprice failure: {exc}",
                duration_ms=int(self.config.risk.pause_after_reconnect_seconds * 1000),
            )
