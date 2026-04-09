from __future__ import annotations

from collections import deque
from decimal import Decimal
import json
from pathlib import Path

from .reason_attribution import classify_reason_bucket
from .models import Balance, BookSnapshot, FeeSnapshot, InstrumentMeta, LiveOrder, StrategyLot, TradeTick
from .utils import is_managed_cl_ord_id, now_ms, parse_decimal, quantize_down, quantize_up, to_jsonable

MARKOUT_WINDOWS_MS = (300, 1000, 2000)
MARKOUT_HISTORY_SIZE = 64


class BotState:
    def __init__(self, *, managed_prefix: str, state_path: str):
        self.managed_prefix = managed_prefix
        self.state_path = Path(state_path)
        self.instrument: InstrumentMeta | None = None
        self.book: BookSnapshot | None = None
        self.exchange_balances: dict[str, Balance] = {}
        self.budget_balances: dict[str, Balance] = {}
        self.balance_budget_caps: dict[str, Decimal] = {}
        self.balances: dict[str, Balance] = {}
        self.live_orders: dict[str, LiveOrder] = {}
        self.reconnect_events: deque[int] = deque()
        self.initial_nav_quote: Decimal | None = None
        self.strategy_nav_peak_quote: Decimal | None = None
        self.account_total_eq_quote: Decimal | None = None
        self.account_total_eq_peak_quote: Decimal | None = None
        self.last_fill_ms: int | None = None
        self.runtime_state = "INIT"
        self.runtime_reason = "booting"
        self.startup_recovery_side = ""
        self.pause_until_ms = 0
        self.stream_status = {"public_books5": False, "private_user": False}
        self.stream_last_activity_ms = {"public_books5": 0, "private_user": 0}
        self.consecutive_place_failures = 0
        self.consecutive_cancel_failures = 0
        self.last_place_failure_ms = 0
        self.last_cancel_failure_ms = 0
        self.resync_required = False
        self.resync_reason = ""
        self.fee_snapshot: FeeSnapshot | None = None
        self.last_trade: TradeTick | None = None
        self.last_market_trade: TradeTick | None = None
        self.last_fee_check_ms = 0
        self.last_instrument_check_ms = 0
        self.last_consistency_check_ms = 0
        self.last_consistency_ok = False
        self.last_consistency_reason = ""
        self.consecutive_consistency_failures = 0
        self.shadow_realized_pnl_quote = Decimal("0")
        self.shadow_base_cost_quote: Decimal | None = None
        self.shadow_fill_count = 0
        self.shadow_fill_volume_quote = Decimal("0")
        self.observed_fill_count = 0
        self.observed_fill_volume_quote = Decimal("0")
        self.live_realized_pnl_quote = Decimal("0")
        self.entry_profit_density_per10k: Decimal | None = None
        self.entry_profit_density_size_factor: Decimal = Decimal("1")
        self.entry_profit_density_per10k_by_side: dict[str, Decimal | None] = {"buy": None, "sell": None}
        self.entry_profit_density_size_factor_by_side: dict[str, Decimal] = {"buy": Decimal("1"), "sell": Decimal("1")}
        self.rebalance_profit_density_per10k: Decimal | None = None
        self.rebalance_profit_density_size_factor: Decimal = Decimal("1")
        self.rebalance_profit_density_extra_ticks: int = 0
        self.live_position_lots: deque[StrategyLot] = deque()
        self.initial_external_base_inventory: Decimal | None = None
        self.external_base_inventory_remaining: Decimal = Decimal("0")
        self.release_tracking_enabled = False
        self.shared_release_inventory_base = Decimal("0")
        self.shared_release_inventory_improvement_bp = Decimal("0")
        self.triangle_route_snapshot: dict[str, object] | None = None
        self.triangle_exit_route_choice: dict[str, object] | None = None
        self.triangle_route_diagnostics: dict[str, object] | None = None
        self.route_ledger_offset = 0
        self._resync_passive_violation_counts: dict[str, int] = {}
        self._pending_amendments: dict[str, dict[str, object]] = {}
        self._pending_toxic_flow_fills: deque[TradeTick] = deque()
        self._toxic_flow_cooldown_until_ms: dict[str, int] = {"buy": 0, "sell": 0}
        self._pending_fill_markouts: deque[dict[str, object]] = deque()
        self._order_reasons: dict[str, dict[str, str]] = {}
        self._last_managed_fill_ts_ms_by_side_bucket: dict[str, int] = {}
        self._fill_markout_samples: dict[str, dict[int, deque[Decimal]]] = {
            side: {window_ms: deque() for window_ms in MARKOUT_WINDOWS_MS}
            for side in ("buy", "sell")
        }
        self._fill_markout_samples_by_reason: dict[str, dict[int, deque[Decimal]]] = {}

    def set_instrument(self, instrument: InstrumentMeta) -> None:
        self.instrument = instrument
        self.last_instrument_check_ms = now_ms()
        self._init_external_inventory_if_possible()
        self._clamp_external_inventory_to_balance()

    def configure_release_tracking(self, *, enabled: bool) -> None:
        self.release_tracking_enabled = bool(enabled)
        self._clamp_external_inventory_to_balance()

    def set_triangle_route_snapshot(self, snapshot: dict[str, object] | None) -> None:
        self.triangle_route_snapshot = dict(snapshot) if snapshot is not None else None

    def set_triangle_exit_route_choice(self, choice: dict[str, object] | None) -> None:
        self.triangle_exit_route_choice = dict(choice) if choice is not None else None

    def set_triangle_route_diagnostics(self, diagnostics: dict[str, object] | None) -> None:
        self.triangle_route_diagnostics = dict(diagnostics) if diagnostics is not None else None

    def set_route_ledger_offset(self, offset: int) -> None:
        self.route_ledger_offset = max(int(offset), 0)

    def set_startup_recovery_side(self, side: str | None) -> None:
        normalized = str(side or "").lower()
        if normalized not in {"", "buy", "sell"}:
            normalized = ""
        self.startup_recovery_side = normalized

    def set_shared_release_inventory_base(self, value: Decimal) -> None:
        self.shared_release_inventory_base = max(value, Decimal("0"))

    def set_shared_release_inventory_improvement_bp(self, value: Decimal) -> None:
        self.shared_release_inventory_improvement_bp = max(value, Decimal("0"))

    def set_entry_profit_density(self, *, per10k: Decimal | None, size_factor: Decimal) -> None:
        self.entry_profit_density_per10k = per10k
        self.entry_profit_density_size_factor = min(max(size_factor, Decimal("0")), Decimal("1"))

    def set_entry_profit_density_by_side(self, *, side: str, per10k: Decimal | None, size_factor: Decimal) -> None:
        normalized = str(side or "").lower()
        if normalized not in {"buy", "sell"}:
            return
        self.entry_profit_density_per10k_by_side[normalized] = per10k
        self.entry_profit_density_size_factor_by_side[normalized] = min(max(size_factor, Decimal("0")), Decimal("1"))

    def entry_profit_density_per10k_for_side(self, side: str) -> Decimal | None:
        normalized = str(side or "").lower()
        if normalized in self.entry_profit_density_per10k_by_side:
            per10k = self.entry_profit_density_per10k_by_side[normalized]
            if per10k is not None:
                return per10k
        return self.entry_profit_density_per10k

    def entry_profit_density_size_factor_for_side(self, side: str) -> Decimal:
        normalized = str(side or "").lower()
        if normalized in self.entry_profit_density_size_factor_by_side and self.entry_profit_density_per10k_by_side.get(normalized) is not None:
            return self.entry_profit_density_size_factor_by_side[normalized]
        return self.entry_profit_density_size_factor

    def set_rebalance_profit_density(self, *, per10k: Decimal | None, size_factor: Decimal, extra_ticks: int) -> None:
        self.rebalance_profit_density_per10k = per10k
        self.rebalance_profit_density_size_factor = min(max(size_factor, Decimal("0")), Decimal("1"))
        self.rebalance_profit_density_extra_ticks = max(int(extra_ticks), 0)

    def set_book(self, book: BookSnapshot) -> None:
        self.book = book
        self._init_nav_if_possible()
        self._init_shadow_cost_if_possible()

    def configure_balance_budgets(
        self,
        *,
        base_ccy: str,
        quote_ccy: str,
        base_total: Decimal,
        quote_total: Decimal,
    ) -> None:
        self._set_balance_budget_cap(base_ccy, base_total)
        self._set_balance_budget_cap(quote_ccy, quote_total)
        for ccy in (base_ccy, quote_ccy):
            source = self.exchange_balances.get(ccy) or self.budget_balances.get(ccy) or self.balances.get(ccy)
            if source is not None:
                self.budget_balances[ccy] = self._budget_seed_balance(ccy, source)
        self._refresh_effective_balances()

    def set_balances(self, balances: dict[str, Balance]) -> None:
        for ccy, balance in balances.items():
            self.exchange_balances[ccy] = Balance(
                ccy=ccy,
                total=balance.total,
                available=balance.available,
                frozen=balance.frozen,
            )
            if ccy not in self.balance_budget_caps:
                self.budget_balances[ccy] = Balance(
                    ccy=ccy,
                    total=balance.total,
                    available=balance.available,
                    frozen=balance.frozen,
                )
            elif ccy not in self.budget_balances:
                self.budget_balances[ccy] = self._budget_seed_balance(ccy, balance)
        self._refresh_effective_balances(currencies=tuple(balances.keys()))
        self._init_nav_if_possible()
        self._init_shadow_cost_if_possible()
        self._init_external_inventory_if_possible()
        self._clamp_external_inventory_to_balance()

    def seed_shadow_balances(self, *, base_ccy: str, quote_ccy: str, base_balance: Decimal, quote_balance: Decimal) -> None:
        self.budget_balances[base_ccy] = self._budget_seed_balance(
            base_ccy,
            Balance(ccy=base_ccy, total=base_balance, available=base_balance),
        )
        self.budget_balances[quote_ccy] = self._budget_seed_balance(
            quote_ccy,
            Balance(ccy=quote_ccy, total=quote_balance, available=quote_balance),
        )
        self._refresh_effective_balances(currencies=(base_ccy, quote_ccy))
        self._init_nav_if_possible()
        self._init_shadow_cost_if_possible()

    def set_fee_snapshot(self, snapshot: FeeSnapshot) -> None:
        self.fee_snapshot = snapshot
        self.last_fee_check_ms = snapshot.checked_at_ms

    def set_last_trade(self, trade: TradeTick) -> None:
        self.last_trade = trade

    def set_last_market_trade(self, trade: TradeTick) -> None:
        self.last_market_trade = trade

    def load_persisted_accounting(self) -> dict[str, int | str] | None:
        if not self.state_path.exists():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

        self.initial_nav_quote = self._optional_decimal(payload.get("initial_nav_quote"))
        self.strategy_nav_peak_quote = self._optional_decimal(payload.get("strategy_nav_peak_quote"))
        self.account_total_eq_quote = self._optional_decimal(payload.get("account_total_eq_quote"))
        self.account_total_eq_peak_quote = self._optional_decimal(payload.get("account_total_eq_peak_quote"))
        self.last_fill_ms = self._optional_int(payload.get("last_fill_ms"))
        self.startup_recovery_side = str(payload.get("startup_recovery_side") or "").lower()
        self.shadow_realized_pnl_quote = parse_decimal(payload.get("shadow_realized_pnl_quote") or "0")
        self.shadow_base_cost_quote = self._optional_decimal(payload.get("shadow_base_cost_quote"))
        self.shadow_fill_count = self._optional_int(payload.get("shadow_fill_count")) or 0
        self.shadow_fill_volume_quote = parse_decimal(payload.get("shadow_fill_volume_quote") or "0")
        self.observed_fill_count = self._optional_int(payload.get("observed_fill_count")) or 0
        self.observed_fill_volume_quote = parse_decimal(payload.get("observed_fill_volume_quote") or "0")
        self.live_realized_pnl_quote = parse_decimal(payload.get("live_realized_pnl_quote") or "0")
        self.entry_profit_density_per10k = self._optional_decimal(payload.get("entry_profit_density_per10k"))
        self.entry_profit_density_size_factor = parse_decimal(payload.get("entry_profit_density_size_factor") or "1")
        self.entry_profit_density_per10k_by_side = self._restore_optional_decimal_by_side(
            payload.get("entry_profit_density_per10k_by_side"),
            fallback=self.entry_profit_density_per10k,
        )
        self.entry_profit_density_size_factor_by_side = self._restore_decimal_by_side(
            payload.get("entry_profit_density_size_factor_by_side"),
            fallback=self.entry_profit_density_size_factor,
        )
        self.rebalance_profit_density_per10k = self._optional_decimal(payload.get("rebalance_profit_density_per10k"))
        self.rebalance_profit_density_size_factor = parse_decimal(payload.get("rebalance_profit_density_size_factor") or "1")
        self.rebalance_profit_density_extra_ticks = int(payload.get("rebalance_profit_density_extra_ticks") or 0)
        self.initial_external_base_inventory = self._optional_decimal(payload.get("initial_external_base_inventory"))
        self.external_base_inventory_remaining = parse_decimal(payload.get("external_base_inventory_remaining") or "0")
        self.shared_release_inventory_base = parse_decimal(payload.get("shared_release_inventory_base") or "0")
        self.shared_release_inventory_improvement_bp = parse_decimal(payload.get("shared_release_inventory_improvement_bp") or "0")
        restored_budget_balances = self._restore_balance_map(payload, "budget_balances")
        if restored_budget_balances:
            self.budget_balances = restored_budget_balances
        restored_effective_balances = self._restore_balance_map(payload, "balances")
        if restored_effective_balances:
            self.balances = restored_effective_balances
        restored_exchange_balances = self._restore_balance_map(payload, "exchange_balances")
        if restored_exchange_balances:
            self.exchange_balances = restored_exchange_balances
        if restored_budget_balances or restored_exchange_balances:
            self._refresh_effective_balances()
        self._refresh_peak_metrics()
        fill_ts_payload = payload.get("last_managed_fill_ts_ms_by_side_bucket")
        restored_fill_ts: dict[str, int] = {}
        if isinstance(fill_ts_payload, dict):
            for key, value in fill_ts_payload.items():
                try:
                    resolved = int(value)
                except (TypeError, ValueError):
                    continue
                if resolved > 0:
                    restored_fill_ts[str(key)] = resolved
        self._last_managed_fill_ts_ms_by_side_bucket = restored_fill_ts
        triangle_snapshot = payload.get("triangle_route_snapshot")
        if isinstance(triangle_snapshot, dict):
            self.triangle_route_snapshot = triangle_snapshot
        triangle_choice = payload.get("triangle_exit_route_choice")
        if isinstance(triangle_choice, dict):
            self.triangle_exit_route_choice = triangle_choice
        triangle_diagnostics = payload.get("triangle_route_diagnostics")
        if isinstance(triangle_diagnostics, dict):
            self.triangle_route_diagnostics = triangle_diagnostics
        self.route_ledger_offset = int(payload.get("route_ledger_offset") or 0)
        cooldown_payload = payload.get("toxic_flow_cooldown_until_ms") or {}
        now_ref = now_ms()
        self._toxic_flow_cooldown_until_ms = {
            "buy": max(self._optional_int(cooldown_payload.get("buy")) or 0, 0),
            "sell": max(self._optional_int(cooldown_payload.get("sell")) or 0, 0),
        }
        for side, until_ms in list(self._toxic_flow_cooldown_until_ms.items()):
            if until_ms <= now_ref:
                self._toxic_flow_cooldown_until_ms[side] = 0

        restored_lots: deque[StrategyLot] = deque()
        for item in payload.get("live_position_lots", []):
            lot = self._parse_strategy_lot(item)
            if lot is not None:
                restored_lots.append(lot)
        self.live_position_lots = restored_lots

        last_trade = self._parse_trade_tick(payload.get("last_trade"))
        if last_trade is not None:
            self.last_trade = last_trade
        last_market_trade = self._parse_trade_tick(payload.get("last_market_trade"))
        if last_market_trade is not None:
            self.last_market_trade = last_market_trade

        return {
            "live_lot_count": len(self.live_position_lots),
            "live_realized_pnl_quote": str(self.live_realized_pnl_quote),
            "observed_fill_count": self.observed_fill_count,
        }

    def apply_account_update(self, payload: dict) -> None:
        total_eq = self._optional_decimal(payload.get("totalEq"))
        if total_eq is not None and total_eq > 0:
            self.account_total_eq_quote = total_eq
        for item in payload.get("details", []):
            ccy = item.get("ccy")
            if not ccy:
                continue
            total = parse_decimal(item.get("cashBal") or item.get("eq") or "0")
            available = parse_decimal(item.get("availBal") or item.get("availEq") or total)
            frozen = max(total - available, Decimal("0"))
            exchange_balance = Balance(ccy=ccy, total=total, available=available, frozen=frozen)
            self.exchange_balances[ccy] = exchange_balance
            if ccy not in self.balance_budget_caps:
                self.budget_balances[ccy] = exchange_balance
            elif ccy not in self.budget_balances:
                self.budget_balances[ccy] = self._budget_seed_balance(ccy, exchange_balance)
        self._refresh_effective_balances(currencies=tuple(item.get("ccy") for item in payload.get("details", []) if item.get("ccy")))
        self._init_nav_if_possible()
        self._init_external_inventory_if_possible()
        self._clamp_external_inventory_to_balance()

    def replace_live_orders(self, payloads: list[dict], *, source: str = "rest_sync") -> None:
        seen: set[str] = set()
        for payload in payloads:
            cl_ord_id = payload.get("clOrdId") or payload.get("ordId") or ""
            seen.add(str(cl_ord_id))
            self.apply_order_update(payload, source=source)
        for cl_ord_id in list(self.live_orders.keys()):
            if cl_ord_id not in seen:
                self.live_orders.pop(cl_ord_id, None)

    def apply_order_update(self, payload: dict, *, source: str = "ws") -> LiveOrder:
        cl_ord_id = payload.get("clOrdId") or payload.get("ordId") or ""
        state = str(payload.get("state") or "live").lower()
        previous = self.live_orders.get(cl_ord_id)
        side = str(payload.get("side") or "")
        if not side and previous is not None:
            side = previous.side
        order = LiveOrder(
            inst_id=payload.get("instId", self.instrument.inst_id if self.instrument else ""),
            side=side,
            ord_id=str(payload.get("ordId") or ""),
            cl_ord_id=cl_ord_id,
            price=parse_decimal(payload.get("px") or "0"),
            size=parse_decimal(payload.get("sz") or "0"),
            filled_size=parse_decimal(payload.get("accFillSz") or payload.get("fillSz") or "0"),
            state=state,
            created_at_ms=int(payload.get("cTime") or now_ms()),
            updated_at_ms=int(payload.get("uTime") or now_ms()),
            source=source,
            cancel_requested=previous.cancel_requested if previous else False,
        )
        previous_filled = previous.filled_size if previous else Decimal("0")
        fill_delta = order.filled_size - previous_filled
        if fill_delta > 0:
            fill_price = parse_decimal(payload.get("fillPx") or payload.get("avgPx") or payload.get("px") or "0")
            self.observed_fill_count += 1
            if fill_price > 0:
                self.observed_fill_volume_quote += fill_delta * fill_price
                if is_managed_cl_ord_id(cl_ord_id, self.managed_prefix):
                    self._record_managed_fill_timestamp(
                        side=order.side,
                        reason_bucket=self.order_reason_bucket(cl_ord_id),
                        ts_ms=order.updated_at_ms,
                    )
                    self._apply_live_fill_balance_effect(
                        side=order.side,
                        fill_size=fill_delta,
                        fill_price=fill_price,
                    )
                    self._record_live_fill(
                        side=order.side,
                        fill_size=fill_delta,
                        fill_price=fill_price,
                        fill_ts_ms=order.updated_at_ms,
                        cl_ord_id=cl_ord_id,
                        order_price=order.price,
                    )
        if state == "filled":
            self.last_fill_ms = order.updated_at_ms
        if order.is_terminal:
            self.live_orders.pop(cl_ord_id, None)
            self._pending_amendments.pop(cl_ord_id, None)
        else:
            self.live_orders[cl_ord_id] = order
        return order

    def register_pending_amend(
        self,
        *,
        cl_ord_id: str,
        ord_id: str,
        side: str,
        reason: str,
        previous_price: Decimal,
        previous_size: Decimal,
        previous_remaining_size: Decimal,
        target_price: Decimal,
        target_size: Decimal,
        target_remaining_size: Decimal,
        filled_size: Decimal,
        target_exchange_size: Decimal | None = None,
        target_exchange_filled_size: Decimal | None = None,
        req_id: str | None = None,
    ) -> None:
        self._pending_amendments[cl_ord_id] = {
            "cl_ord_id": cl_ord_id,
            "ord_id": ord_id,
            "side": side,
            "reason": reason,
            "previous_price": previous_price,
            "previous_size": previous_size,
            "previous_remaining_size": previous_remaining_size,
            "target_price": target_price,
            "target_size": target_size,
            "target_remaining_size": target_remaining_size,
            "filled_size": filled_size,
            "target_exchange_size": target_exchange_size if target_exchange_size is not None else target_size,
            "target_exchange_filled_size": target_exchange_filled_size if target_exchange_filled_size is not None else filled_size,
            "req_id": req_id or "",
            "requested_at_ms": now_ms(),
        }

    def pending_amend(self, cl_ord_id: str) -> dict[str, object] | None:
        pending = self._pending_amendments.get(cl_ord_id)
        if pending is None:
            return None
        return dict(pending)

    def has_pending_amend(self, cl_ord_id: str) -> bool:
        return cl_ord_id in self._pending_amendments

    def clear_pending_amend(self, cl_ord_id: str) -> dict[str, object] | None:
        return self._pending_amendments.pop(cl_ord_id, None)

    def update_pending_amend_identity(
        self,
        *,
        previous_cl_ord_id: str,
        cl_ord_id: str,
        ord_id: str,
    ) -> None:
        pending = self._pending_amendments.pop(previous_cl_ord_id, None)
        if pending is None:
            return
        pending["cl_ord_id"] = cl_ord_id
        pending["ord_id"] = ord_id
        self._pending_amendments[cl_ord_id] = pending

    def set_order_reason(self, *, cl_ord_id: str, reason: str | None) -> None:
        if not cl_ord_id:
            return
        resolved = str(reason or "")
        self._order_reasons[cl_ord_id] = {
            "reason": resolved,
            "bucket": classify_reason_bucket(resolved),
        }

    def order_reason(self, cl_ord_id: str) -> str | None:
        payload = self._order_reasons.get(cl_ord_id)
        if payload is None:
            return None
        return payload.get("reason") or None

    def order_reason_bucket(self, cl_ord_id: str) -> str:
        payload = self._order_reasons.get(cl_ord_id)
        if payload is None:
            return "unknown"
        return payload.get("bucket") or "unknown"

    @staticmethod
    def _managed_fill_ts_key(*, side: str, reason_bucket: str) -> str:
        return f"{str(reason_bucket or 'unknown')}|{str(side or '')}"

    def _record_managed_fill_timestamp(self, *, side: str, reason_bucket: str, ts_ms: int) -> None:
        if ts_ms <= 0 or side not in {"buy", "sell"}:
            return
        key = self._managed_fill_ts_key(side=side, reason_bucket=reason_bucket)
        self._last_managed_fill_ts_ms_by_side_bucket[key] = ts_ms

    def last_managed_fill_ts_ms(self, *, side: str, reason_bucket: str) -> int | None:
        key = self._managed_fill_ts_key(side=side, reason_bucket=reason_bucket)
        value = self._last_managed_fill_ts_ms_by_side_bucket.get(key)
        if value is None or value <= 0:
            return None
        return value

    def managed_fill_age_ms(
        self,
        *,
        side: str,
        reason_bucket: str,
        reference_ms: int | None = None,
    ) -> int | None:
        last_ts = self.last_managed_fill_ts_ms(side=side, reason_bucket=reason_bucket)
        if last_ts is None:
            return None
        now_ref = reference_ms if reference_ms is not None else now_ms()
        return max(now_ref - last_ts, 0)

    def resolve_pending_amend_update(self, *, payload: dict, order: LiveOrder) -> tuple[str, dict[str, object]] | None:
        pending = self._pending_amendments.get(order.cl_ord_id)
        if pending is None:
            return None

        pending_req_id = str(pending.get("req_id") or "")
        payload_req_id = str(payload.get("reqId") or "")
        if pending_req_id and payload_req_id and payload_req_id != pending_req_id:
            return None

        code = str(payload.get("code") or "0")
        amend_result = str(payload.get("amendResult") or "")
        if code != "0" or amend_result not in {"", "0"}:
            self._pending_amendments.pop(order.cl_ord_id, None)
            return (
                "amend_order_error",
                {
                    "cl_ord_id": pending["cl_ord_id"],
                    "ord_id": pending["ord_id"],
                    "side": pending["side"],
                    "reason": pending["reason"],
                    "old_price": pending["previous_price"],
                    "new_price": pending["target_price"],
                    "old_size": pending["previous_size"],
                    "new_size": pending["target_size"],
                    "old_remaining_size": pending["previous_remaining_size"],
                    "new_remaining_size": pending["target_remaining_size"],
                    "filled_size": order.filled_size,
                    "exchange_price": order.price,
                    "exchange_size": order.size,
                    "exchange_state": order.state,
                    "error_source": "ws_order_update",
                    "req_id": pending_req_id,
                    "okx": {
                        "code": code,
                        "msg": str(payload.get("msg") or ""),
                        "amendResult": amend_result,
                    },
                },
            )

        target_price = pending["target_price"]
        target_size = pending["target_size"]
        target_exchange_size = pending.get("target_exchange_size", target_size)
        target_exchange_filled_size = pending.get("target_exchange_filled_size", pending["filled_size"])
        if amend_result == "0" or (
            order.price == target_price
            and order.size == target_exchange_size
            and order.filled_size == target_exchange_filled_size
        ):
            self._pending_amendments.pop(order.cl_ord_id, None)
            return (
                "amend_order",
                {
                    "cl_ord_id": pending["cl_ord_id"],
                    "ord_id": pending["ord_id"],
                    "side": pending["side"],
                    "reason": pending["reason"],
                    "old_price": pending["previous_price"],
                    "new_price": order.price,
                    "old_size": pending["previous_size"],
                    "new_size": order.size,
                    "old_remaining_size": pending["previous_remaining_size"],
                    "new_remaining_size": order.remaining_size,
                    "filled_size": order.filled_size,
                    "confirmed_by_ws": True,
                    "req_id": pending_req_id,
                },
            )
        return None

    def bot_orders(self, side: str | None = None) -> list[LiveOrder]:
        orders = [order for order in self.live_orders.values() if is_managed_cl_ord_id(order.cl_ord_id, self.managed_prefix)]
        if side:
            orders = [order for order in orders if order.side == side]
        return sorted(orders, key=lambda item: item.created_at_ms)

    def reserve_shadow_order(self, order: LiveOrder) -> None:
        if not self.instrument:
            raise RuntimeError("instrument missing for shadow reserve")
        if order.side == "buy":
            reserved_quote = order.remaining_size * order.price
            self._adjust_balance(self.instrument.quote_ccy, available_delta=-reserved_quote, frozen_delta=reserved_quote)
            return
        if order.side == "sell":
            self._adjust_balance(self.instrument.base_ccy, available_delta=-order.remaining_size, frozen_delta=order.remaining_size)
            return
        raise ValueError(f"unsupported side for shadow reserve: {order.side}")

    def release_shadow_order(self, order: LiveOrder) -> None:
        if not self.instrument:
            raise RuntimeError("instrument missing for shadow release")
        if order.remaining_size <= 0:
            return
        if order.side == "buy":
            reserved_quote = order.remaining_size * order.price
            self._adjust_balance(self.instrument.quote_ccy, available_delta=reserved_quote, frozen_delta=-reserved_quote)
            return
        if order.side == "sell":
            self._adjust_balance(self.instrument.base_ccy, available_delta=order.remaining_size, frozen_delta=-order.remaining_size)
            return
        raise ValueError(f"unsupported side for shadow release: {order.side}")

    def apply_shadow_fill(self, order: LiveOrder, *, fill_size: Decimal, fill_price: Decimal, fill_ts_ms: int) -> None:
        if not self.instrument:
            raise RuntimeError("instrument missing for shadow fill")
        if fill_size <= 0:
            return
        fill_size = min(fill_size, order.remaining_size)
        if fill_size <= 0:
            return

        self._init_shadow_cost_if_possible()
        quote_amount = fill_size * fill_price
        base_ccy = self.instrument.base_ccy
        quote_ccy = self.instrument.quote_ccy

        if order.side == "buy":
            self._adjust_balance(quote_ccy, total_delta=-quote_amount, frozen_delta=-quote_amount)
            self._adjust_balance(base_ccy, total_delta=fill_size, available_delta=fill_size)
            self.shadow_base_cost_quote = (self.shadow_base_cost_quote or Decimal("0")) + quote_amount
        elif order.side == "sell":
            base_before = self.total_balance(base_ccy)
            average_cost = fill_price
            if base_before > 0 and self.shadow_base_cost_quote is not None:
                average_cost = self.shadow_base_cost_quote / base_before
            realized = quote_amount - (average_cost * fill_size)
            self.shadow_realized_pnl_quote += realized
            self._adjust_balance(base_ccy, total_delta=-fill_size, frozen_delta=-fill_size)
            self._adjust_balance(quote_ccy, total_delta=quote_amount, available_delta=quote_amount)
            if self.shadow_base_cost_quote is not None:
                self.shadow_base_cost_quote -= average_cost * fill_size
                if self.shadow_base_cost_quote < 0:
                    self.shadow_base_cost_quote = Decimal("0")
        else:
            raise ValueError(f"unsupported side for shadow fill: {order.side}")

        order.filled_size += fill_size
        order.updated_at_ms = fill_ts_ms
        order.queue_ahead_size = Decimal("0")
        self.last_fill_ms = fill_ts_ms
        self.last_trade = TradeTick(
            ts_ms=fill_ts_ms,
            received_ms=fill_ts_ms,
            price=fill_price,
            size=fill_size,
            side=order.side,
            trade_id=order.cl_ord_id,
            order_price=order.price,
        )
        self.shadow_fill_count += 1
        self.shadow_fill_volume_quote += quote_amount

        if order.remaining_size <= 0:
            order.state = "filled"
            self.live_orders.pop(order.cl_ord_id, None)
        else:
            self.live_orders[order.cl_ord_id] = order

    def set_stream_status(self, stream_name: str, connected: bool) -> None:
        self.stream_status[stream_name] = connected
        if connected:
            self.mark_stream_activity(stream_name)

    def mark_stream_activity(self, stream_name: str, activity_ms: int | None = None) -> None:
        self.stream_last_activity_ms[stream_name] = activity_ms if activity_ms is not None else now_ms()

    def stream_activity_age_ms(self, stream_name: str, *, reference_ms: int | None = None) -> int | None:
        last_activity_ms = self.stream_last_activity_ms.get(stream_name, 0)
        if last_activity_ms <= 0:
            return None
        now_ref = reference_ms if reference_ms is not None else now_ms()
        return max(now_ref - last_activity_ms, 0)

    def streams_ready(self, *, require_public: bool, require_private: bool) -> bool:
        public_ok = self.stream_status.get("public_books5", False) if require_public else True
        private_ok = self.stream_status.get("private_user", False) if require_private else True
        return public_ok and private_ok

    def set_runtime_state(self, runtime_state: str, reason: str = "") -> None:
        self.runtime_state = runtime_state
        if reason:
            self.runtime_reason = reason

    def set_pause(self, *, reason: str, duration_ms: int) -> None:
        self.runtime_state = "PAUSED"
        self.runtime_reason = reason
        self.pause_until_ms = max(self.pause_until_ms, now_ms() + max(duration_ms, 0))

    def clear_pause_if_elapsed(self) -> None:
        if self.runtime_state == "PAUSED" and not self.resync_required and self.pause_until_ms and now_ms() >= self.pause_until_ms:
            self.runtime_state = "READY"
            self.runtime_reason = "pause elapsed"
            self.pause_until_ms = 0

    def is_pause_active(self) -> bool:
        return self.runtime_state == "PAUSED" and now_ms() < self.pause_until_ms

    def request_resync(self, reason: str) -> None:
        self.resync_required = True
        self.resync_reason = reason

    def clear_resync(self) -> None:
        self.resync_required = False
        self.resync_reason = ""
        self.clear_resync_passive_violations()

    def note_resync_passive_violations(self, order_ids: tuple[str, ...]) -> tuple[str, ...]:
        updated_counts: dict[str, int] = {}
        escalated: list[str] = []
        for order_id in order_ids:
            count = self._resync_passive_violation_counts.get(order_id, 0) + 1
            updated_counts[order_id] = count
            if count >= 2:
                escalated.append(order_id)
        self._resync_passive_violation_counts = updated_counts
        return tuple(escalated)

    def clear_resync_passive_violations(self) -> None:
        self._resync_passive_violation_counts.clear()

    def mark_reconnect(self) -> None:
        current = now_ms()
        self.reconnect_events.append(current)
        threshold = current - 300_000
        while self.reconnect_events and self.reconnect_events[0] < threshold:
            self.reconnect_events.popleft()

    def reconnect_count_5m(self) -> int:
        threshold = now_ms() - 300_000
        while self.reconnect_events and self.reconnect_events[0] < threshold:
            self.reconnect_events.popleft()
        return len(self.reconnect_events)

    def record_place_result(self, success: bool) -> None:
        if success:
            self.consecutive_place_failures = 0
            self.last_place_failure_ms = 0
            return
        self.consecutive_place_failures += 1
        self.last_place_failure_ms = now_ms()

    def record_cancel_result(self, success: bool) -> None:
        if success:
            self.consecutive_cancel_failures = 0
            self.last_cancel_failure_ms = 0
            return
        self.consecutive_cancel_failures += 1
        self.last_cancel_failure_ms = now_ms()

    def reset_place_failures(self) -> None:
        self.consecutive_place_failures = 0
        self.last_place_failure_ms = 0

    def reset_cancel_failures(self) -> None:
        self.consecutive_cancel_failures = 0
        self.last_cancel_failure_ms = 0

    def place_failure_cooldown_remaining_ms(self, cooldown_seconds: float) -> int:
        if self.consecutive_place_failures <= 0 or self.last_place_failure_ms <= 0:
            return 0
        remaining = int(cooldown_seconds * 1000) - (now_ms() - self.last_place_failure_ms)
        return max(remaining, 0)

    def cancel_failure_cooldown_remaining_ms(self, cooldown_seconds: float) -> int:
        if self.consecutive_cancel_failures <= 0 or self.last_cancel_failure_ms <= 0:
            return 0
        remaining = int(cooldown_seconds * 1000) - (now_ms() - self.last_cancel_failure_ms)
        return max(remaining, 0)

    def record_consistency_result(self, ok: bool, reason: str) -> None:
        self.last_consistency_check_ms = now_ms()
        self.last_consistency_ok = ok
        self.last_consistency_reason = reason
        self.consecutive_consistency_failures = 0 if ok else self.consecutive_consistency_failures + 1

    def exchange_free_balance(self, ccy: str) -> Decimal:
        return self.exchange_balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"))).available

    def exchange_total_balance(self, ccy: str) -> Decimal:
        return self.exchange_balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"))).total

    def budget_free_balance(self, ccy: str) -> Decimal:
        return self.budget_balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"))).available

    def budget_total_balance(self, ccy: str) -> Decimal:
        return self.budget_balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"))).total

    def validate_configured_budgets(self, *, ccys: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        errors: list[str] = []
        for ccy in ccys:
            configured = self.balance_budget_caps.get(ccy)
            if configured is None or configured <= 0:
                continue
            exchange_total = self.exchange_total_balance(ccy)
            if exchange_total <= 0:
                errors.append(f"{ccy} budget configured but exchange balance missing")
                continue
            if configured > exchange_total:
                errors.append(f"{ccy} budget {configured} exceeds exchange total {exchange_total}")
        return tuple(errors)

    def free_balance(self, ccy: str) -> Decimal:
        return self.balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"))).available

    def total_balance(self, ccy: str) -> Decimal:
        return self.balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"))).total

    def strategy_position_base(self) -> Decimal:
        return sum((lot.qty for lot in self.live_position_lots), Decimal("0"))

    def external_release_base_size(self, *, base_buffer: Decimal) -> Decimal:
        if not self.instrument:
            return Decimal("0")
        available = min(self.external_base_inventory_remaining, self.total_balance(self.instrument.base_ccy))
        releasable = max(available - max(base_buffer, Decimal("0")), Decimal("0"))
        return quantize_down(releasable, self.instrument.lot_size)

    def total_release_base_size(self, *, base_buffer: Decimal) -> Decimal:
        if not self.instrument:
            return Decimal("0")
        own = self.external_release_base_size(base_buffer=base_buffer)
        shared = quantize_down(max(self.shared_release_inventory_base, Decimal("0")), self.instrument.lot_size)
        return own + shared

    def apply_external_release_fill(self, *, fill_size: Decimal, fill_price: Decimal) -> Decimal:
        if fill_size <= 0 or fill_price <= 0:
            return Decimal("0")
        remaining = fill_size
        matched_total = Decimal("0")
        while remaining > 0 and self.live_position_lots and self.live_position_lots[0].qty > 0:
            lot = self.live_position_lots[0]
            matched = min(remaining, lot.qty)
            self.live_realized_pnl_quote += matched * (fill_price - lot.price)
            lot.qty -= matched
            remaining -= matched
            matched_total += matched
            if lot.qty == 0:
                self.live_position_lots.popleft()
        return matched_total

    def toxic_flow_cooldown_remaining_ms(self, side: str, *, reference_ms: int | None = None) -> int:
        until_ms = self._toxic_flow_cooldown_until_ms.get(side, 0)
        if until_ms <= 0:
            return 0
        now_ref = reference_ms if reference_ms is not None else now_ms()
        return max(until_ms - now_ref, 0)

    def is_toxic_flow_side_cooling_down(self, side: str, *, reference_ms: int | None = None) -> bool:
        return self.toxic_flow_cooldown_remaining_ms(side, reference_ms=reference_ms) > 0

    def evaluate_fill_markouts(self, *, reference_ms: int | None = None) -> None:
        if not self.instrument or not self.book or self.book.mid is None:
            return
        tick_size = self.instrument.tick_size
        if tick_size <= 0:
            return

        now_ref = reference_ms if reference_ms is not None else now_ms()
        remaining: deque[dict[str, object]] = deque()
        while self._pending_fill_markouts:
            pending = self._pending_fill_markouts.popleft()
            fill_ts_ms = int(pending.get("ts_ms") or 0)
            fill_price = pending.get("price")
            side = str(pending.get("side") or "")
            observed_windows = pending.get("observed_windows")
            if (
                fill_ts_ms <= 0
                or not isinstance(fill_price, Decimal)
                or side not in {"buy", "sell"}
                or not isinstance(observed_windows, set)
            ):
                continue

            age_ms = max(now_ref - fill_ts_ms, 0)
            for window_ms in MARKOUT_WINDOWS_MS:
                if window_ms in observed_windows or age_ms < window_ms:
                    continue
                adverse_ticks = self._adverse_markout_ticks(
                    side=side,
                    fill_price=fill_price,
                    mark_price=self.book.mid,
                    tick_size=tick_size,
                )
                self._record_markout_sample(
                    side=side,
                    window_ms=window_ms,
                    adverse_ticks=adverse_ticks,
                    reason_bucket=str(pending.get("reason_bucket") or "unknown"),
                )
                observed_windows.add(window_ms)

            if len(observed_windows) < len(MARKOUT_WINDOWS_MS):
                remaining.append(pending)
        self._pending_fill_markouts = remaining

    def evaluate_toxic_flow(
        self,
        *,
        min_observation_ms: int,
        max_observation_ms: int,
        adverse_ticks: int,
        cooldown_ms: int,
        reference_ms: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        if adverse_ticks <= 0 or cooldown_ms <= 0:
            return ()
        if not self.instrument or not self.book or self.book.mid is None:
            return ()
        tick_size = self.instrument.tick_size
        if tick_size <= 0:
            return ()

        now_ref = reference_ms if reference_ms is not None else now_ms()
        remaining: deque[TradeTick] = deque()
        events: list[dict[str, object]] = []
        while self._pending_toxic_flow_fills:
            fill = self._pending_toxic_flow_fills.popleft()
            age_ms = max(now_ref - fill.ts_ms, 0)
            if age_ms < min_observation_ms:
                remaining.append(fill)
                continue
            if age_ms > max_observation_ms:
                continue

            adverse_move = Decimal("0")
            if fill.side == "buy":
                adverse_move = fill.price - self.book.mid
            elif fill.side == "sell":
                adverse_move = self.book.mid - fill.price
            adverse_move = max(adverse_move, Decimal("0"))
            adverse_move_ticks = int(adverse_move / tick_size)
            if adverse_move_ticks < adverse_ticks:
                remaining.append(fill)
                continue

            cooldown_until_ms = now_ref + cooldown_ms
            self._toxic_flow_cooldown_until_ms[fill.side] = max(
                self._toxic_flow_cooldown_until_ms.get(fill.side, 0),
                cooldown_until_ms,
            )
            events.append(
                {
                    "side": fill.side,
                    "fill_ts_ms": fill.ts_ms,
                    "fill_price": fill.price,
                    "current_mid": self.book.mid,
                    "adverse_ticks": adverse_move_ticks,
                    "cooldown_until_ms": self._toxic_flow_cooldown_until_ms[fill.side],
                }
            )

        self._pending_toxic_flow_fills = remaining
        return tuple(events)

    def average_adverse_fill_markout_ticks(self, *, side: str, window_ms: int) -> Decimal | None:
        samples = self._fill_markout_samples.get(side, {}).get(window_ms)
        if not samples:
            return None
        return sum(samples, Decimal("0")) / Decimal(len(samples))

    def adverse_fill_markout_sample_count(self, *, side: str, window_ms: int) -> int:
        samples = self._fill_markout_samples.get(side, {}).get(window_ms)
        return len(samples) if samples is not None else 0

    def adverse_fill_markout_level(
        self,
        *,
        side: str,
        window_ms: int,
        trigger_samples: int,
        threshold_ticks: Decimal,
        severe_extra_ticks: Decimal = Decimal("1"),
    ) -> int:
        if window_ms <= 0 or trigger_samples <= 0 or threshold_ticks <= 0:
            return 0
        if self.adverse_fill_markout_sample_count(side=side, window_ms=window_ms) < trigger_samples:
            return 0
        adverse_ticks = self.average_adverse_fill_markout_ticks(side=side, window_ms=window_ms)
        if adverse_ticks is None or adverse_ticks < threshold_ticks:
            return 0
        severe_threshold = threshold_ticks + max(severe_extra_ticks, Decimal("0"))
        if adverse_ticks >= severe_threshold:
            return 2
        return 1

    def fill_markout_summary(self) -> dict[str, dict[str, dict[str, object]]]:
        summary: dict[str, dict[str, dict[str, object]]] = {}
        for side, per_window in self._fill_markout_samples.items():
            side_summary: dict[str, dict[str, object]] = {}
            for window_ms, samples in per_window.items():
                avg = None
                if samples:
                    avg = sum(samples, Decimal("0")) / Decimal(len(samples))
                side_summary[str(window_ms)] = {
                    "samples": len(samples),
                    "avg_adverse_ticks": avg,
                }
            summary[side] = side_summary
        return summary

    def fill_markout_summary_by_reason(self) -> dict[str, dict[str, dict[str, object]]]:
        summary: dict[str, dict[str, dict[str, object]]] = {}
        for reason_bucket, per_window in self._fill_markout_samples_by_reason.items():
            bucket_summary: dict[str, dict[str, object]] = {}
            for window_ms, samples in per_window.items():
                avg = None
                if samples:
                    avg = sum(samples, Decimal("0")) / Decimal(len(samples))
                bucket_summary[str(window_ms)] = {
                    "samples": len(samples),
                    "avg_adverse_ticks": avg,
                }
            summary[reason_bucket] = bucket_summary
        return summary

    def rebalance_base_size(self, side: str) -> Decimal:
        position = self.strategy_position_base()
        if side == "sell" and position > 0:
            return position
        if side == "buy" and position < 0:
            return -position
        return Decimal("0")

    def oldest_rebalance_lot_age_ms(self, side: str, *, reference_ms: int | None = None) -> int | None:
        if side == "sell":
            lot_timestamps = [lot.ts_ms for lot in self.live_position_lots if lot.qty > 0 and lot.ts_ms >= 1_000_000_000_000]
        elif side == "buy":
            lot_timestamps = [lot.ts_ms for lot in self.live_position_lots if lot.qty < 0 and lot.ts_ms >= 1_000_000_000_000]
        else:
            return None
        if not lot_timestamps:
            return None
        now_ref = reference_ms if reference_ms is not None else now_ms()
        return max(now_ref - min(lot_timestamps), 0)

    def oldest_rebalance_lot(self, side: str) -> StrategyLot | None:
        candidates: list[StrategyLot]
        if side == "sell":
            candidates = [lot for lot in self.live_position_lots if lot.qty > 0 and lot.ts_ms >= 1_000_000_000_000]
        elif side == "buy":
            candidates = [lot for lot in self.live_position_lots if lot.qty < 0 and lot.ts_ms >= 1_000_000_000_000]
        else:
            return None
        if not candidates:
            return None
        return min(candidates, key=lambda lot: lot.ts_ms)

    def min_rebalance_sell_price(self, base_size: Decimal, *, tick_size: Decimal, profit_ticks: int) -> Decimal | None:
        if base_size <= 0:
            return None
        remaining = base_size
        max_cost: Decimal | None = None
        for lot in self.live_position_lots:
            if lot.qty <= 0:
                continue
            matched = min(remaining, lot.qty)
            if matched <= 0:
                continue
            max_cost = lot.price if max_cost is None else max(max_cost, lot.price)
            remaining -= matched
            if remaining <= 0:
                break
        if max_cost is None:
            return None
        target = max_cost + tick_size * Decimal(profit_ticks)
        return quantize_up(target, tick_size)

    def max_rebalance_buy_price(self, base_size: Decimal, *, tick_size: Decimal, profit_ticks: int) -> Decimal | None:
        if base_size <= 0:
            return None
        remaining = base_size
        min_open_price: Decimal | None = None
        for lot in self.live_position_lots:
            if lot.qty >= 0:
                continue
            matched = min(remaining, -lot.qty)
            if matched <= 0:
                continue
            min_open_price = lot.price if min_open_price is None else min(min_open_price, lot.price)
            remaining -= matched
            if remaining <= 0:
                break
        if min_open_price is None:
            return None
        target = min_open_price - tick_size * Decimal(profit_ticks)
        return quantize_down(target, tick_size)

    def profitable_rebalance_sell_size(self, sell_price: Decimal, *, tick_size: Decimal, profit_ticks: int) -> Decimal:
        if sell_price <= 0:
            return Decimal("0")
        required_edge = tick_size * Decimal(max(profit_ticks, 0))
        profitable_size = Decimal("0")
        matched_prefix = False
        for lot in self.live_position_lots:
            if lot.qty <= 0:
                if matched_prefix:
                    break
                continue
            matched_prefix = True
            required_price = quantize_up(lot.price + required_edge, tick_size)
            if sell_price < required_price:
                break
            profitable_size += lot.qty
        return profitable_size

    def profitable_rebalance_buy_size(self, buy_price: Decimal, *, tick_size: Decimal, profit_ticks: int) -> Decimal:
        if buy_price <= 0:
            return Decimal("0")
        required_edge = tick_size * Decimal(max(profit_ticks, 0))
        profitable_size = Decimal("0")
        matched_prefix = False
        for lot in self.live_position_lots:
            if lot.qty >= 0:
                if matched_prefix:
                    break
                continue
            matched_prefix = True
            required_price = quantize_down(lot.price - required_edge, tick_size)
            if buy_price > required_price:
                break
            profitable_size += -lot.qty
        return profitable_size

    def inventory_ratio(self) -> Decimal | None:
        if not self.instrument or not self.book or not self.book.mid:
            return None
        base_total = self.total_balance(self.instrument.base_ccy)
        quote_total = self.total_balance(self.instrument.quote_ccy)
        nav = base_total * self.book.mid + quote_total
        if nav <= 0:
            return None
        return (base_total * self.book.mid) / nav

    def nav_quote(self) -> Decimal | None:
        if not self.instrument or not self.book or not self.book.mid:
            return None
        base_total = self.total_balance(self.instrument.base_ccy)
        quote_total = self.total_balance(self.instrument.quote_ccy)
        return base_total * self.book.mid + quote_total

    def exchange_nav_quote(self) -> Decimal | None:
        if not self.instrument or not self.book or not self.book.mid:
            return None
        base_total = self.exchange_total_balance(self.instrument.base_ccy)
        quote_total = self.exchange_total_balance(self.instrument.quote_ccy)
        return base_total * self.book.mid + quote_total

    def account_equity_quote(self) -> Decimal | None:
        if self.account_total_eq_quote is not None:
            return self.account_total_eq_quote
        return self.exchange_nav_quote()

    def daily_pnl_quote(self) -> Decimal | None:
        nav = self.nav_quote()
        if nav is None or self.initial_nav_quote is None:
            return None
        return nav - self.initial_nav_quote

    def strategy_drawdown_quote(self) -> Decimal | None:
        nav = self.nav_quote()
        if nav is None or self.strategy_nav_peak_quote is None:
            return None
        return nav - self.strategy_nav_peak_quote

    def account_drawdown_quote(self) -> Decimal | None:
        equity = self.account_equity_quote()
        if equity is None or self.account_total_eq_peak_quote is None:
            return None
        return equity - self.account_total_eq_peak_quote

    def live_unrealized_pnl_quote(self) -> Decimal | None:
        if not self.book or self.book.mid is None:
            return None
        return sum(((self.book.mid - lot.price) * lot.qty for lot in self.live_position_lots), Decimal("0"))

    def live_total_pnl_quote(self) -> Decimal | None:
        unrealized = self.live_unrealized_pnl_quote()
        if unrealized is None:
            return None
        return self.live_realized_pnl_quote + unrealized

    def shadow_unrealized_pnl_quote(self) -> Decimal | None:
        total_pnl = self.daily_pnl_quote()
        if total_pnl is None:
            return None
        return total_pnl - self.shadow_realized_pnl_quote

    def persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "instrument": self.instrument,
            "book": self.book,
            "balances": self.balances,
            "exchange_balances": self.exchange_balances,
            "budget_balances": self.budget_balances,
            "live_orders": self.live_orders,
            "initial_nav_quote": self.initial_nav_quote,
            "strategy_nav_peak_quote": self.strategy_nav_peak_quote,
            "account_total_eq_quote": self.account_total_eq_quote,
            "account_total_eq_peak_quote": self.account_total_eq_peak_quote,
            "last_fill_ms": self.last_fill_ms,
            "startup_recovery_side": self.startup_recovery_side,
            "runtime_state": self.runtime_state,
            "runtime_reason": self.runtime_reason,
            "pause_until_ms": self.pause_until_ms,
            "stream_status": self.stream_status,
            "stream_last_activity_ms": self.stream_last_activity_ms,
            "consecutive_place_failures": self.consecutive_place_failures,
            "consecutive_cancel_failures": self.consecutive_cancel_failures,
            "last_place_failure_ms": self.last_place_failure_ms,
            "last_cancel_failure_ms": self.last_cancel_failure_ms,
            "resync_required": self.resync_required,
            "resync_reason": self.resync_reason,
            "fee_snapshot": self.fee_snapshot,
            "last_trade": self.last_trade,
            "last_market_trade": self.last_market_trade,
            "last_fee_check_ms": self.last_fee_check_ms,
            "last_instrument_check_ms": self.last_instrument_check_ms,
            "last_consistency_check_ms": self.last_consistency_check_ms,
            "last_consistency_ok": self.last_consistency_ok,
            "last_consistency_reason": self.last_consistency_reason,
            "consecutive_consistency_failures": self.consecutive_consistency_failures,
            "shadow_realized_pnl_quote": self.shadow_realized_pnl_quote,
            "shadow_base_cost_quote": self.shadow_base_cost_quote,
            "shadow_fill_count": self.shadow_fill_count,
            "shadow_fill_volume_quote": self.shadow_fill_volume_quote,
            "observed_fill_count": self.observed_fill_count,
            "observed_fill_volume_quote": self.observed_fill_volume_quote,
            "live_realized_pnl_quote": self.live_realized_pnl_quote,
            "entry_profit_density_per10k": self.entry_profit_density_per10k,
            "entry_profit_density_size_factor": self.entry_profit_density_size_factor,
            "entry_profit_density_per10k_by_side": self.entry_profit_density_per10k_by_side,
            "entry_profit_density_size_factor_by_side": self.entry_profit_density_size_factor_by_side,
            "rebalance_profit_density_per10k": self.rebalance_profit_density_per10k,
            "rebalance_profit_density_size_factor": self.rebalance_profit_density_size_factor,
            "rebalance_profit_density_extra_ticks": self.rebalance_profit_density_extra_ticks,
            "live_unrealized_pnl_quote": self.live_unrealized_pnl_quote(),
            "live_total_pnl_quote": self.live_total_pnl_quote(),
            "strategy_drawdown_quote": self.strategy_drawdown_quote(),
            "account_equity_quote": self.account_equity_quote(),
            "account_drawdown_quote": self.account_drawdown_quote(),
            "strategy_position_base": self.strategy_position_base(),
            "live_position_lots": list(self.live_position_lots),
            "initial_external_base_inventory": self.initial_external_base_inventory,
            "external_base_inventory_remaining": self.external_base_inventory_remaining,
            "shared_release_inventory_base": self.shared_release_inventory_base,
            "shared_release_inventory_improvement_bp": self.shared_release_inventory_improvement_bp,
            "last_managed_fill_ts_ms_by_side_bucket": self._last_managed_fill_ts_ms_by_side_bucket,
            "triangle_route_snapshot": self.triangle_route_snapshot,
            "triangle_exit_route_choice": self.triangle_exit_route_choice,
            "triangle_route_diagnostics": self.triangle_route_diagnostics,
            "route_ledger_offset": self.route_ledger_offset,
            "toxic_flow_cooldown_until_ms": self._toxic_flow_cooldown_until_ms,
            "fill_markout_summary": self.fill_markout_summary(),
            "fill_markout_summary_by_reason": self.fill_markout_summary_by_reason(),
        }
        self.state_path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")

    def _init_nav_if_possible(self) -> None:
        if self._has_balance_snapshot():
            nav = self.nav_quote()
            if self.initial_nav_quote is None and nav is not None and nav > 0:
                self.initial_nav_quote = nav
        self._refresh_peak_metrics()

    def _init_shadow_cost_if_possible(self) -> None:
        if self.shadow_base_cost_quote is None and self.instrument and self.book and self.book.mid is not None and self._has_balance_snapshot():
            self.shadow_base_cost_quote = self.total_balance(self.instrument.base_ccy) * self.book.mid

    def _refresh_peak_metrics(self) -> None:
        nav = self.nav_quote()
        if nav is not None and nav > 0:
            baseline = self.initial_nav_quote if self.initial_nav_quote is not None and self.initial_nav_quote > 0 else nav
            candidate = nav if nav > baseline else baseline
            if self.strategy_nav_peak_quote is None or candidate > self.strategy_nav_peak_quote:
                self.strategy_nav_peak_quote = candidate
        account_equity = self.account_equity_quote()
        if account_equity is not None and account_equity > 0:
            if self.account_total_eq_peak_quote is None or account_equity > self.account_total_eq_peak_quote:
                self.account_total_eq_peak_quote = account_equity

    @staticmethod
    def _optional_decimal(value) -> Decimal | None:
        if value in (None, "", "null"):
            return None
        return parse_decimal(value)

    @staticmethod
    def _optional_int(value) -> int | None:
        if value in (None, "", "null"):
            return None
        return int(value)

    def _restore_optional_decimal_by_side(self, raw, *, fallback: Decimal | None) -> dict[str, Decimal | None]:
        restored: dict[str, Decimal | None] = {"buy": fallback, "sell": fallback}
        if not isinstance(raw, dict):
            return restored
        for side in ("buy", "sell"):
            restored[side] = self._optional_decimal(raw.get(side))
        return restored

    def _restore_decimal_by_side(self, raw, *, fallback: Decimal) -> dict[str, Decimal]:
        restored: dict[str, Decimal] = {"buy": fallback, "sell": fallback}
        if not isinstance(raw, dict):
            return restored
        for side in ("buy", "sell"):
            restored[side] = min(max(parse_decimal(raw.get(side) or fallback), Decimal("0")), Decimal("1"))
        return restored

    @staticmethod
    def _parse_trade_tick(value) -> TradeTick | None:
        if not isinstance(value, dict):
            return None
        try:
            return TradeTick(
                ts_ms=int(value.get("ts_ms") or 0),
                price=parse_decimal(value.get("price") or "0"),
                size=parse_decimal(value.get("size") or "0"),
                side=str(value.get("side") or ""),
                received_ms=BotState._optional_int(value.get("received_ms")),
                trade_id=str(value.get("trade_id") or "") or None,
                order_price=BotState._optional_decimal(value.get("order_price")),
            )
        except (TypeError, ValueError, ArithmeticError):
            return None

    @staticmethod
    def _parse_balance(value) -> Balance | None:
        if not isinstance(value, dict):
            return None
        ccy = str(value.get("ccy") or "")
        if not ccy:
            return None
        try:
            return Balance(
                ccy=ccy,
                total=parse_decimal(value.get("total") or "0"),
                available=parse_decimal(value.get("available") or "0"),
                frozen=parse_decimal(value.get("frozen") or "0"),
            )
        except (TypeError, ValueError, ArithmeticError):
            return None

    def _restore_balance_map(self, payload: dict, key: str) -> dict[str, Balance]:
        raw = payload.get(key)
        if not isinstance(raw, dict):
            return {}
        restored: dict[str, Balance] = {}
        for ccy, item in raw.items():
            balance = self._parse_balance(item)
            if balance is None:
                continue
            restored[str(ccy)] = balance
        return restored

    @staticmethod
    def _parse_strategy_lot(value) -> StrategyLot | None:
        if not isinstance(value, dict):
            return None
        try:
            return StrategyLot(
                qty=parse_decimal(value.get("qty") or "0"),
                price=parse_decimal(value.get("price") or "0"),
                ts_ms=int(value.get("ts_ms") or 0),
                cl_ord_id=str(value.get("cl_ord_id") or ""),
                reference_best_bid=BotState._optional_decimal(value.get("reference_best_bid")),
                reference_best_ask=BotState._optional_decimal(value.get("reference_best_ask")),
            )
        except (TypeError, ValueError, ArithmeticError):
            return None

    def _init_external_inventory_if_possible(self) -> None:
        if self.initial_external_base_inventory is not None or not self.instrument or not self._has_balance_snapshot():
            return
        self.initial_external_base_inventory = self.total_balance(self.instrument.base_ccy)
        self.external_base_inventory_remaining = self.initial_external_base_inventory

    def _clamp_external_inventory_to_balance(self) -> None:
        if not self.release_tracking_enabled or not self.instrument or not self._has_balance_snapshot():
            return
        base_total = self.total_balance(self.instrument.base_ccy)
        self.external_base_inventory_remaining = min(self.external_base_inventory_remaining, base_total)
        if self.external_base_inventory_remaining < 0:
            self.external_base_inventory_remaining = Decimal("0")

    def mark_cancel_requested(self, cl_ord_id: str) -> None:
        order = self.live_orders.get(cl_ord_id)
        if not order:
            return
        order.cancel_requested = True
        order.updated_at_ms = now_ms()

    def _apply_live_fill_balance_effect(self, *, side: str, fill_size: Decimal, fill_price: Decimal) -> None:
        if fill_size <= 0 or fill_price <= 0 or not self.instrument or not self._has_balance_snapshot():
            return

        quote_amount = fill_size * fill_price
        base_ccy = self.instrument.base_ccy
        quote_ccy = self.instrument.quote_ccy

        if side == "buy":
            self._consume_balance(ccy=quote_ccy, amount=quote_amount)
            self._adjust_balance(base_ccy, total_delta=fill_size, available_delta=fill_size)
            return
        if side == "sell":
            self._consume_balance(ccy=base_ccy, amount=fill_size)
            self._adjust_balance(quote_ccy, total_delta=quote_amount, available_delta=quote_amount)
            return
        raise ValueError(f"unsupported side for live fill balance effect: {side}")

    def _consume_balance(self, *, ccy: str, amount: Decimal) -> None:
        if amount <= 0:
            return
        current = self.budget_balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"), frozen=Decimal("0")))
        consumed_frozen = min(current.frozen, amount)
        consumed_available = amount - consumed_frozen
        self._adjust_balance(
            ccy,
            total_delta=-amount,
            available_delta=-consumed_available,
            frozen_delta=-consumed_frozen,
        )

    def _adjust_balance(
        self,
        ccy: str,
        *,
        total_delta: Decimal = Decimal("0"),
        available_delta: Decimal = Decimal("0"),
        frozen_delta: Decimal = Decimal("0"),
    ) -> None:
        current = self.budget_balances.get(ccy, Balance(ccy=ccy, total=Decimal("0"), available=Decimal("0"), frozen=Decimal("0")))
        total = current.total + total_delta
        available = current.available + available_delta
        frozen = current.frozen + frozen_delta
        if total < 0:
            total = Decimal("0")
        if available < 0:
            available = Decimal("0")
        if frozen < 0:
            frozen = Decimal("0")
        self.budget_balances[ccy] = Balance(ccy=ccy, total=total, available=available, frozen=frozen)
        if ccy in self.exchange_balances:
            exchange_current = self.exchange_balances[ccy]
            exchange_total = exchange_current.total + total_delta
            exchange_available = exchange_current.available + available_delta
            exchange_frozen = exchange_current.frozen + frozen_delta
            if exchange_total < 0:
                exchange_total = Decimal("0")
            if exchange_available < 0:
                exchange_available = Decimal("0")
            if exchange_frozen < 0:
                exchange_frozen = Decimal("0")
            self.exchange_balances[ccy] = Balance(
                ccy=ccy,
                total=exchange_total,
                available=exchange_available,
                frozen=exchange_frozen,
            )
        self._refresh_effective_balances(currencies=(ccy,))
        self._refresh_peak_metrics()

    def _has_balance_snapshot(self) -> bool:
        if not self.instrument:
            return False
        return self.instrument.base_ccy in self.balances and self.instrument.quote_ccy in self.balances

    def _set_balance_budget_cap(self, ccy: str, total: Decimal) -> None:
        if total > 0:
            self.balance_budget_caps[ccy] = total
            return
        self.balance_budget_caps.pop(ccy, None)

    def _budget_seed_balance(self, ccy: str, source: Balance) -> Balance:
        budget_cap = self.balance_budget_caps.get(ccy)
        if budget_cap is None or budget_cap <= 0:
            return Balance(ccy=ccy, total=source.total, available=source.available, frozen=source.frozen)
        total = min(source.total, budget_cap)
        available = min(source.available, total)
        frozen = max(total - available, Decimal("0"))
        return Balance(ccy=ccy, total=total, available=available, frozen=frozen)

    def _ensure_budget_balance(self, ccy: str) -> None:
        if ccy in self.budget_balances:
            return
        source = self.exchange_balances.get(ccy) or self.balances.get(ccy) or Balance(
            ccy=ccy,
            total=Decimal("0"),
            available=Decimal("0"),
            frozen=Decimal("0"),
        )
        self.budget_balances[ccy] = self._budget_seed_balance(ccy, source)

    def _refresh_effective_balances(self, *, currencies: tuple[str, ...] | list[str] | None = None) -> None:
        if currencies is None:
            keys = set(self.exchange_balances) | set(self.budget_balances) | set(self.balances)
        else:
            keys = {ccy for ccy in currencies if ccy}
        for ccy in keys:
            budget = self.budget_balances.get(ccy)
            exchange = self.exchange_balances.get(ccy)
            if budget is None and exchange is None:
                self.balances.pop(ccy, None)
                continue
            if budget is None:
                self.balances[ccy] = exchange
                continue
            if exchange is None:
                self.balances[ccy] = budget
                continue
            total = min(budget.total, exchange.total)
            available = min(budget.available, exchange.available, total)
            frozen = max(total - available, Decimal("0"))
            self.balances[ccy] = Balance(ccy=ccy, total=total, available=available, frozen=frozen)

    def _record_live_fill(
        self,
        *,
        side: str,
        fill_size: Decimal,
        fill_price: Decimal,
        fill_ts_ms: int,
        cl_ord_id: str,
        order_price: Decimal,
    ) -> None:
        if fill_size <= 0 or fill_price <= 0:
            return

        reference_best_bid = self.book.best_bid.price if self.book and self.book.best_bid else None
        reference_best_ask = self.book.best_ask.price if self.book and self.book.best_ask else None

        remaining = fill_size
        if side == "buy":
            while remaining > 0 and self.live_position_lots and self.live_position_lots[0].qty < 0:
                lot = self.live_position_lots[0]
                matched = min(remaining, -lot.qty)
                self.live_realized_pnl_quote += matched * (lot.price - fill_price)
                lot.qty += matched
                remaining -= matched
                if lot.qty == 0:
                    self.live_position_lots.popleft()
            if remaining > 0:
                self.live_position_lots.append(
                    StrategyLot(
                        qty=remaining,
                        price=fill_price,
                        ts_ms=fill_ts_ms,
                        cl_ord_id=cl_ord_id,
                        reference_best_bid=reference_best_bid,
                        reference_best_ask=reference_best_ask,
                    )
                )
        elif side == "sell":
            while remaining > 0 and self.live_position_lots and self.live_position_lots[0].qty > 0:
                lot = self.live_position_lots[0]
                matched = min(remaining, lot.qty)
                self.live_realized_pnl_quote += matched * (fill_price - lot.price)
                lot.qty -= matched
                remaining -= matched
                if lot.qty == 0:
                    self.live_position_lots.popleft()
            if remaining > 0 and self.release_tracking_enabled and self.external_base_inventory_remaining > 0:
                released = min(remaining, self.external_base_inventory_remaining)
                self.external_base_inventory_remaining -= released
                remaining -= released
            if remaining > 0:
                self.live_position_lots.append(
                    StrategyLot(
                        qty=-remaining,
                        price=fill_price,
                        ts_ms=fill_ts_ms,
                        cl_ord_id=cl_ord_id,
                        reference_best_bid=reference_best_bid,
                        reference_best_ask=reference_best_ask,
                    )
                )
        else:
            raise ValueError(f"unsupported side for live fill: {side}")

        self.last_fill_ms = fill_ts_ms
        self.last_trade = TradeTick(
            ts_ms=fill_ts_ms,
            received_ms=fill_ts_ms,
            price=fill_price,
            size=fill_size,
            side=side,
            trade_id=cl_ord_id,
            order_price=order_price,
        )
        self._pending_toxic_flow_fills.append(
            TradeTick(
                ts_ms=fill_ts_ms,
                received_ms=fill_ts_ms,
                price=fill_price,
                size=fill_size,
                side=side,
                trade_id=cl_ord_id,
                order_price=order_price,
            )
        )
        self._pending_fill_markouts.append(
            {
                "ts_ms": fill_ts_ms,
                "price": fill_price,
                "side": side,
                "reason": self.order_reason(cl_ord_id) or "",
                "reason_bucket": self.order_reason_bucket(cl_ord_id),
                "observed_windows": set(),
            }
        )

    @staticmethod
    def _adverse_markout_ticks(*, side: str, fill_price: Decimal, mark_price: Decimal, tick_size: Decimal) -> Decimal:
        if tick_size <= 0:
            return Decimal("0")
        adverse_move = Decimal("0")
        if side == "buy":
            adverse_move = fill_price - mark_price
        elif side == "sell":
            adverse_move = mark_price - fill_price
        if adverse_move <= 0:
            return Decimal("0")
        return adverse_move / tick_size

    def _record_markout_sample(self, *, side: str, window_ms: int, adverse_ticks: Decimal, reason_bucket: str = "unknown") -> None:
        side_samples = self._fill_markout_samples.get(side)
        if side_samples is None:
            return
        samples = side_samples.get(window_ms)
        if samples is None:
            return
        samples.append(max(adverse_ticks, Decimal("0")))
        while len(samples) > MARKOUT_HISTORY_SIZE:
            samples.popleft()
        bucket_samples = self._fill_markout_samples_by_reason.setdefault(
            reason_bucket,
            {bucket_window: deque() for bucket_window in MARKOUT_WINDOWS_MS},
        )
        bucket_window_samples = bucket_samples.setdefault(window_ms, deque())
        bucket_window_samples.append(max(adverse_ticks, Decimal("0")))
        while len(bucket_window_samples) > MARKOUT_HISTORY_SIZE:
            bucket_window_samples.popleft()
