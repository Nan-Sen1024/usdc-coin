from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .audit_store import SQLiteAuditStore
from .config import BotConfig
from .exchange_errors import ExchangeAPIError
from .log_labels import summarize_okx_error, translate_reason
from .models import InstrumentMeta, LiveOrder, QuoteDecision, RiskStatus
from .okx_rest import OKXAPIError, OKXRestClient
from .reason_attribution import classify_reason_bucket
from .state import BotState
from .utils import build_cl_ord_id, build_req_id, decimal_to_str, is_managed_cl_ord_id, now_ms, passive_edge_ticks, quantize_down, to_jsonable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .shadow import ShadowFillSimulator


class JournalWriter:
    def __init__(
        self,
        path: str,
        *,
        sqlite_store: SQLiteAuditStore | None = None,
        runtime_state_getter: Callable[[], str | None] | None = None,
        run_id: str | None = None,
    ):
        self.path = Path(path)
        self.sqlite_store = sqlite_store
        self.runtime_state_getter = runtime_state_getter
        self.run_id = run_id

    def append(self, event: str, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ts_ms = now_ms()
        runtime_state = self.runtime_state_getter() if self.runtime_state_getter else None
        record = {
            "ts_ms": ts_ms,
            "event": event,
            "runtime_state": runtime_state,
            "run_id": self.run_id,
            "payload": to_jsonable(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.sqlite_store:
            self.sqlite_store.append_event(
                ts_ms=ts_ms,
                event=event,
                payload=payload,
                runtime_state=runtime_state,
                run_id=self.run_id,
            )


class OrderExecutor:
    def __init__(
        self,
        *,
        rest,
        state: BotState,
        config: BotConfig,
        journal: JournalWriter,
        shadow_simulator: "ShadowFillSimulator | None" = None,
    ):
        self.rest = rest
        self.state = state
        self.config = config
        self.journal = journal
        self.shadow_simulator = shadow_simulator
        self._last_action_by_side: dict[str, int] = {}
        self.trade_client = rest

    @staticmethod
    def _exception_message(exc: BaseException) -> str:
        text = str(exc).strip()
        if text:
            return text
        return type(exc).__name__

    def attach_trade_client(self, trade_client) -> None:
        self.trade_client = trade_client

    async def bootstrap_pending_orders(self) -> None:
        await self._sync_pending_orders(
            fail_on_foreign=self.config.risk.fail_on_foreign_pending_orders,
            cancel_managed=self.config.risk.cancel_managed_orders_on_startup,
        )
        if self.config.risk.cancel_managed_orders_on_startup:
            await self._wait_for_startup_cleanup_reconciliation()

    async def reload_pending_orders(self) -> None:
        await self._sync_pending_orders(fail_on_foreign=False, cancel_managed=False)

    async def _wait_for_startup_cleanup_reconciliation(self) -> None:
        """Reconcile local order state against exchange truth before streams come online."""
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            await self.reload_pending_orders()
            if not any(order.cancel_requested for order in self.state.bot_orders()):
                return
            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(0.2)

    async def reconcile(self, decision: QuoteDecision, risk_status: RiskStatus | None = None) -> None:
        if await self._try_batch_cross_side_amend(decision, risk_status=risk_status):
            return
        await self._reconcile_side("buy", decision.bid_layers, risk_status=risk_status)
        await self._reconcile_side("sell", decision.ask_layers, risk_status=risk_status)

    async def cancel_all_managed_orders(self, *, reason: str) -> None:
        orders = [order for order in list(self.state.bot_orders()) if not order.cancel_requested]
        if await self._batch_cancel_orders(orders=orders, reason=reason):
            return
        for order in orders:
            await self._cancel_order(order, reason=reason, ignore_cooldown=True)

    async def cancel_managed_orders(self, *, cl_ord_ids: tuple[str, ...] | list[str], reason: str) -> None:
        orders: list[LiveOrder] = []
        for cl_ord_id in cl_ord_ids:
            order = self.state.live_orders.get(cl_ord_id)
            if order is None:
                continue
            if not is_managed_cl_ord_id(order.cl_ord_id, self.config.managed_prefix):
                continue
            if order.cancel_requested:
                continue
            orders.append(order)
        if await self._batch_cancel_orders(orders=orders, reason=reason):
            return
        for order in orders:
            await self._cancel_order(order, reason=reason, ignore_cooldown=True)

    async def _sync_pending_orders(self, *, fail_on_foreign: bool, cancel_managed: bool) -> None:
        orders = await self.rest.list_pending_orders(
            inst_id=self.config.trading.inst_id,
            inst_type=self.config.trading.inst_type,
        )
        self.state.replace_live_orders(orders, source="rest_sync")
        foreign = [
            order.cl_ord_id or order.ord_id
            for order in self.state.live_orders.values()
            if not is_managed_cl_ord_id(order.cl_ord_id, self.config.managed_prefix)
        ]
        if foreign and fail_on_foreign:
            raise RuntimeError(f"Foreign pending orders detected: {foreign}")
        if cancel_managed:
            await self.cancel_all_managed_orders(reason="startup_cleanup")

    async def _reconcile_side(self, side: str, intents, *, risk_status: RiskStatus | None = None) -> None:
        live_orders = self.state.bot_orders(side)
        targets = self._resolved_targets_for_side(side=side, intents=intents)

        if not targets:
            for order in live_orders:
                if order.cancel_requested:
                    continue
                if self._should_keep_order_without_intent(primary=order, risk_status=risk_status):
                    continue
                await self._cancel_order(order, reason="side_disabled")
                return
            return

        unmatched_orders = list(live_orders)
        unmatched_targets = list(targets)

        remaining_targets: list[tuple[object, Decimal]] = []
        for intent, base_size in unmatched_targets:
            matched = self._pop_first_matching_order(
                orders=unmatched_orders,
                intent=intent,
                base_size=base_size,
                matcher=self._same_live_order_target,
            )
            if matched is None:
                remaining_targets.append((intent, base_size))
        unmatched_targets = remaining_targets

        remaining_targets = []
        for intent, base_size in unmatched_targets:
            matched = self._pop_first_matching_order(
                orders=unmatched_orders,
                intent=intent,
                base_size=base_size,
                matcher=self._should_keep_existing_order,
            )
            if matched is None:
                remaining_targets.append((intent, base_size))
        unmatched_targets = remaining_targets

        if any(order.cancel_requested for order in unmatched_orders):
            return

        if unmatched_orders and unmatched_targets:
            order = unmatched_orders[0]
            intent, base_size = unmatched_targets[0]
            if not self._cooldown_ok(side):
                return
            if await self._amend_order(primary=order, intent=intent, base_size=base_size):
                return
            await self._cancel_order(order, reason="reprice_or_ttl")
            return

        if unmatched_orders:
            await self._cancel_order(unmatched_orders[0], reason="duplicate_side_order")
            return

        if not unmatched_targets:
            return
        intent, base_size = unmatched_targets[0]

        if not self._cooldown_ok(side):
            return

        if self.config.mode == "shadow":
            cl_ord_id = build_cl_ord_id(self.config.managed_prefix, side)
            payload = {
                "instId": self.config.trading.inst_id,
                "side": side,
                "ordId": "",
                "clOrdId": cl_ord_id,
                "px": decimal_to_str(intent.price),
                "sz": decimal_to_str(base_size),
                "accFillSz": "0",
                "state": "live",
                "cTime": str(now_ms()),
                "uTime": str(now_ms()),
            }
            self.state.set_order_reason(cl_ord_id=cl_ord_id, reason=intent.reason)
            order = self.state.apply_order_update(payload, source="shadow_place")
            if self.shadow_simulator:
                self.shadow_simulator.on_order_placed(order)
            self.state.record_place_result(True)
            self.journal.append(
                "shadow_quote",
                {
                    "cl_ord_id": cl_ord_id,
                    "side": side,
                    "price": intent.price,
                    "base_size": base_size,
                    "quote_notional": base_size * intent.price,
                    "reason": intent.reason,
                    "reason_bucket": classify_reason_bucket(intent.reason),
                },
            )
            self._last_action_by_side[side] = now_ms()
            return

        cl_ord_id = build_cl_ord_id(self.config.managed_prefix, side)
        req_id = build_req_id(self.config.managed_prefix, f"pl{side}")
        self.state.set_order_reason(cl_ord_id=cl_ord_id, reason=intent.reason)
        try:
            response = await self._trade_client().place_limit_order(
                inst_id=self.config.trading.inst_id,
                side=side,
                price=intent.price,
                size=base_size,
                cl_ord_id=cl_ord_id,
                post_only=self.config.trading.post_only,
                req_id=req_id,
                inst_id_code=self._trade_inst_id_code(),
            )
        except (Exception, asyncio.CancelledError) as exc:
            self.state.record_place_result(False)
            error_text = self._exception_message(exc)
            payload = {
                "side": side,
                "req_id": req_id,
                "reason": error_text,
                "error_type": type(exc).__name__,
                "reason_zh": "下单失败",
                "side_zh": "买单" if side == "buy" else "卖单",
            }
            if isinstance(exc, ExchangeAPIError):
                payload["okx"] = exc.to_dict()
                payload["okx_zh"] = summarize_okx_error(payload["okx"])
            self.journal.append("place_order_error", payload)
            logger.warning("下单失败 | %s | %s", payload["side_zh"], payload.get("okx_zh") or payload["reason"])
            return

        payload = {
            "instId": self.config.trading.inst_id,
            "side": side,
            "ordId": response.get("ordId", ""),
            "clOrdId": cl_ord_id,
            "reqId": req_id,
            "px": decimal_to_str(intent.price),
            "sz": decimal_to_str(base_size),
            "accFillSz": "0",
            "state": "live",
            "cTime": str(now_ms()),
            "uTime": str(now_ms()),
            "reason": intent.reason,
            "reason_bucket": classify_reason_bucket(intent.reason),
        }
        self.state.apply_order_update(payload, source="rest_place")
        self.state.record_place_result(True)
        self.journal.append("place_order", payload)
        self._last_action_by_side[side] = now_ms()

    async def _amend_order(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        trade_client = self._trade_client()
        amend_order = getattr(trade_client, "amend_order", None)
        if self.config.mode != "shadow" and not callable(amend_order):
            return False

        new_total_size = primary.filled_size + base_size
        req_id = build_req_id(self.config.managed_prefix, f"am{primary.side}")
        update_payload = {
            "instId": primary.inst_id,
            "side": primary.side,
            "ordId": primary.ord_id,
            "clOrdId": primary.cl_ord_id,
            "reqId": req_id,
            "px": decimal_to_str(intent.price),
            "sz": decimal_to_str(new_total_size),
            "accFillSz": decimal_to_str(primary.filled_size),
            "state": primary.state,
            "cTime": str(primary.created_at_ms),
            "uTime": str(now_ms()),
        }
        journal_payload = {
            "cl_ord_id": primary.cl_ord_id,
            "ord_id": primary.ord_id,
            "side": primary.side,
            "reason": intent.reason,
            "reason_bucket": classify_reason_bucket(intent.reason),
            "old_price": primary.price,
            "new_price": intent.price,
            "old_size": primary.size,
            "new_size": new_total_size,
            "filled_size": primary.filled_size,
            "old_remaining_size": primary.remaining_size,
            "new_remaining_size": base_size,
            "req_id": req_id,
        }

        if self.config.mode == "shadow":
            self.state.set_order_reason(cl_ord_id=primary.cl_ord_id, reason=intent.reason)
            amended_order = self.state.apply_order_update(update_payload, source="shadow_amend")
            if self.shadow_simulator:
                self.shadow_simulator.on_order_amended(primary, amended_order)
            self.journal.append("shadow_amend_order", journal_payload)
            self._last_action_by_side[primary.side] = now_ms()
            return True

        pending_cl_ord_id = str(update_payload["clOrdId"])
        pending_ord_id = str(update_payload["ordId"])
        target_exchange_size = base_size if self.config.exchange.name == "binance" else new_total_size
        target_exchange_filled_size = Decimal("0") if self.config.exchange.name == "binance" else primary.filled_size
        self.state.register_pending_amend(
            cl_ord_id=pending_cl_ord_id,
            ord_id=pending_ord_id,
            side=primary.side,
            reason=intent.reason,
            previous_price=primary.price,
            previous_size=primary.size,
            previous_remaining_size=primary.remaining_size,
            target_price=intent.price,
            target_size=new_total_size,
            target_remaining_size=base_size,
            filled_size=primary.filled_size,
            target_exchange_size=target_exchange_size,
            target_exchange_filled_size=target_exchange_filled_size,
            req_id=req_id,
        )
        self.state.set_order_reason(cl_ord_id=pending_cl_ord_id, reason=intent.reason)
        try:
            response = await amend_order(
                inst_id=primary.inst_id,
                side=primary.side,
                ord_id=primary.ord_id or None,
                cl_ord_id=primary.cl_ord_id or None,
                new_price=intent.price,
                new_size=new_total_size,
                filled_size=primary.filled_size,
                post_only=self.config.trading.post_only,
                cxl_on_fail=False,
                req_id=req_id,
                inst_id_code=self._trade_inst_id_code(),
            )
        except (Exception, asyncio.CancelledError) as exc:
            error_text = self._exception_message(exc)
            payload = {
                "cl_ord_id": primary.cl_ord_id,
                "ord_id": primary.ord_id,
                "side": primary.side,
                "reason": intent.reason,
                "req_id": req_id,
                "error_type": type(exc).__name__,
                "old_price": primary.price,
                "new_price": intent.price,
                "old_size": primary.size,
                "new_size": new_total_size,
                "error": error_text,
            }
            self.state.clear_pending_amend(pending_cl_ord_id)
            if isinstance(exc, ExchangeAPIError):
                payload["okx"] = exc.to_dict()
                payload["okx_zh"] = summarize_okx_error(payload["okx"])
            self.journal.append("amend_order_error", payload)
            logger.warning("改单失败 | %s | %s", primary.side, payload.get("okx_zh") or payload["error"])
            return False

        if response.get("ordId"):
            update_payload["ordId"] = str(response["ordId"])
        if response.get("clOrdId"):
            update_payload["clOrdId"] = str(response["clOrdId"])
        journal_payload["cl_ord_id"] = str(update_payload["clOrdId"])
        journal_payload["ord_id"] = str(update_payload["ordId"])
        identity_changed = (
            str(update_payload["clOrdId"]) != pending_cl_ord_id
            or str(update_payload["ordId"]) != pending_ord_id
        )
        self.state.update_pending_amend_identity(
            previous_cl_ord_id=pending_cl_ord_id,
            cl_ord_id=str(update_payload["clOrdId"]),
            ord_id=str(update_payload["ordId"]),
        )
        self.state.set_order_reason(cl_ord_id=str(update_payload["clOrdId"]), reason=intent.reason)
        if identity_changed:
            self._adopt_amend_replacement_order(
                primary=primary,
                cl_ord_id=str(update_payload["clOrdId"]),
                ord_id=str(update_payload["ordId"]),
                price=intent.price,
                size=target_exchange_size,
                filled_size=target_exchange_filled_size,
            )
        self.journal.append("amend_order_submitted", journal_payload)
        self._last_action_by_side[primary.side] = now_ms()
        return True

    def _adopt_amend_replacement_order(
        self,
        *,
        primary: LiveOrder,
        cl_ord_id: str,
        ord_id: str,
        price: Decimal,
        size: Decimal,
        filled_size: Decimal,
    ) -> None:
        self.state.live_orders.pop(primary.cl_ord_id, None)
        replacement_payload = {
            "instId": primary.inst_id,
            "side": primary.side,
            "ordId": ord_id,
            "clOrdId": cl_ord_id,
            "px": decimal_to_str(price),
            "sz": decimal_to_str(size),
            "accFillSz": decimal_to_str(filled_size),
            "state": "live",
            "cTime": str(now_ms()),
            "uTime": str(now_ms()),
        }
        replacement = self.state.apply_order_update(replacement_payload, source="rest_amend_replace")
        if primary.cancel_requested:
            self.state.mark_cancel_requested(replacement.cl_ord_id)

    async def _cancel_order(self, order: LiveOrder, *, reason: str, ignore_cooldown: bool = False) -> None:
        if not ignore_cooldown and not self._cooldown_ok(order.side):
            return
        if self.config.mode == "shadow":
            if self.shadow_simulator:
                self.shadow_simulator.on_order_canceled(order, reason=reason)
            self.journal.append(
                "shadow_cancel",
                {
                    "side": order.side,
                    "cl_ord_id": order.cl_ord_id,
                    "reason": reason,
                    "reason_zh": translate_reason(reason),
                },
            )
            self.state.live_orders.pop(order.cl_ord_id, None)
            self.state.record_cancel_result(True)
            self._last_action_by_side[order.side] = now_ms()
            return
        req_id = build_req_id(self.config.managed_prefix, f"cx{order.side}")
        try:
            await self._trade_client().cancel_order(
                inst_id=order.inst_id,
                ord_id=order.ord_id or None,
                cl_ord_id=order.cl_ord_id or None,
                req_id=req_id,
                inst_id_code=self._trade_inst_id_code(),
            )
        except (Exception, asyncio.CancelledError) as exc:
            error_text = self._exception_message(exc)
            if self._is_benign_terminal_cancel_error(exc):
                payload = {
                    "cl_ord_id": order.cl_ord_id,
                    "ord_id": order.ord_id,
                    "req_id": req_id,
                    "reason": reason,
                    "reason_zh": translate_reason(reason),
                    "error": error_text,
                    "error_type": type(exc).__name__,
                }
                if isinstance(exc, ExchangeAPIError):
                    payload["okx"] = exc.to_dict()
                    payload["okx_zh"] = summarize_okx_error(payload["okx"])
                self.state.live_orders.pop(order.cl_ord_id, None)
                self.state.record_cancel_result(True)
                self.journal.append("cancel_order_terminal", payload)
                logger.info("撤单已无须执行 | %s | %s", translate_reason(reason), payload.get("okx_zh") or payload["error"])
                self._last_action_by_side[order.side] = now_ms()
                return
            self.state.record_cancel_result(False)
            payload = {
                "cl_ord_id": order.cl_ord_id,
                "ord_id": order.ord_id,
                "req_id": req_id,
                "reason": reason,
                "reason_zh": translate_reason(reason),
                "error": error_text,
                "error_type": type(exc).__name__,
            }
            if isinstance(exc, ExchangeAPIError):
                payload["okx"] = exc.to_dict()
                payload["okx_zh"] = summarize_okx_error(payload["okx"])
            self.journal.append(
                "cancel_order_error",
                payload,
            )
            logger.warning("撤单失败 | %s | %s", translate_reason(reason), payload.get("okx_zh") or payload["error"])
            return
        self.state.record_cancel_result(True)
        self.journal.append(
            "cancel_order",
            {
                "cl_ord_id": order.cl_ord_id,
                "ord_id": order.ord_id,
                "req_id": req_id,
                "reason": reason,
                "reason_zh": translate_reason(reason),
            },
        )
        self.state.mark_cancel_requested(order.cl_ord_id)
        self._last_action_by_side[order.side] = now_ms()

    def _trade_client(self):
        client = self.trade_client
        ready = getattr(client, "trade_ready", None)
        if client is not self.rest and callable(ready):
            try:
                if ready():
                    return client
            except Exception:
                pass
        return self.rest

    def _trade_inst_id_code(self) -> str | None:
        if self.config.exchange.name != "okx" or not self.state.instrument:
            return None
        inst_id_code = str(self.state.instrument.inst_id_code or "")
        return inst_id_code or None

    async def _batch_cancel_orders(self, *, orders: list[LiveOrder], reason: str) -> bool:
        if self.config.mode == "shadow" or len(orders) < 2:
            return False
        trade_client = self._trade_client()
        batch_cancel_orders = getattr(trade_client, "batch_cancel_orders", None)
        if trade_client is self.rest or not callable(batch_cancel_orders):
            return False
        inst_id_code = self._trade_inst_id_code()
        request_id = build_req_id(self.config.managed_prefix, "bcxl")
        payloads = []
        for order in orders:
            payload = {"instIdCode": inst_id_code} if inst_id_code else {"instId": order.inst_id}
            if order.ord_id:
                payload["ordId"] = order.ord_id
            if order.cl_ord_id:
                payload["clOrdId"] = order.cl_ord_id
            payloads.append(payload)
        try:
            results = await batch_cancel_orders(orders=payloads, request_id=request_id)
        except (Exception, asyncio.CancelledError) as exc:
            self.journal.append(
                "batch_cancel_order_error",
                {
                    "req_id": request_id,
                    "reason": reason,
                    "reason_zh": translate_reason(reason),
                    "orders": [{"cl_ord_id": order.cl_ord_id, "ord_id": order.ord_id} for order in orders],
                    "error": self._exception_message(exc),
                    "error_type": type(exc).__name__,
                },
            )
            logger.warning("批量撤单失败 | %s | %s", translate_reason(reason), self._exception_message(exc))
            return True

        results_by_cl_ord_id = {str(item.get("clOrdId") or ""): item for item in results}
        results_by_ord_id = {str(item.get("ordId") or ""): item for item in results}
        for order in orders:
            item = results_by_cl_ord_id.get(order.cl_ord_id) or results_by_ord_id.get(order.ord_id or "")
            if item is None:
                continue
            if str(item.get("sCode") or "0") != "0":
                self.journal.append(
                    "cancel_order_error",
                    {
                        "cl_ord_id": order.cl_ord_id,
                        "ord_id": order.ord_id,
                        "req_id": request_id,
                        "reason": reason,
                        "reason_zh": translate_reason(reason),
                        "okx": {"code": str(item.get("sCode") or ""), "msg": str(item.get("sMsg") or "")},
                        "error": str(item.get("sMsg") or "batch cancel order failed"),
                    },
                )
                continue
            self.state.record_cancel_result(True)
            self.journal.append(
                "cancel_order",
                {
                    "cl_ord_id": order.cl_ord_id,
                    "ord_id": order.ord_id,
                    "req_id": request_id,
                    "reason": reason,
                    "reason_zh": translate_reason(reason),
                },
            )
            self.state.mark_cancel_requested(order.cl_ord_id)
            self._last_action_by_side[order.side] = now_ms()
        return True

    def _single_amend_candidate_for_side(self, *, side: str, intents):
        live_orders = self.state.bot_orders(side)
        targets = self._resolved_targets_for_side(side=side, intents=intents)
        if len(live_orders) != 1 or len(targets) != 1:
            return None
        order = live_orders[0]
        intent, base_size = targets[0]
        if order.cancel_requested or not self._cooldown_ok(side):
            return None
        if self._same_live_order_target(primary=order, intent=intent, base_size=base_size):
            return None
        if self._should_keep_existing_order(primary=order, intent=intent, base_size=base_size):
            return None
        return order, intent, base_size

    async def _try_batch_cross_side_amend(self, decision: QuoteDecision, *, risk_status: RiskStatus | None = None) -> bool:
        del risk_status
        if self.config.mode == "shadow":
            return False
        trade_client = self._trade_client()
        batch_amend_orders = getattr(trade_client, "batch_amend_orders", None)
        if trade_client is self.rest or not callable(batch_amend_orders):
            return False
        buy_candidate = self._single_amend_candidate_for_side(side="buy", intents=decision.bid_layers)
        sell_candidate = self._single_amend_candidate_for_side(side="sell", intents=decision.ask_layers)
        if buy_candidate is None or sell_candidate is None:
            return False

        inst_id_code = self._trade_inst_id_code()
        request_id = build_req_id(self.config.managed_prefix, "bamd")
        batch_payloads: list[dict] = []
        journal_payloads: list[dict] = []
        candidates = [buy_candidate, sell_candidate]
        for order, intent, base_size in candidates:
            new_total_size = order.filled_size + base_size
            req_id = build_req_id(self.config.managed_prefix, f"am{order.side}")
            self.state.register_pending_amend(
                cl_ord_id=order.cl_ord_id,
                ord_id=order.ord_id,
                side=order.side,
                reason=intent.reason,
                previous_price=order.price,
                previous_size=order.size,
                previous_remaining_size=order.remaining_size,
                target_price=intent.price,
                target_size=new_total_size,
                target_remaining_size=base_size,
                filled_size=order.filled_size,
                req_id=req_id,
            )
            payload = {"instIdCode": inst_id_code} if inst_id_code else {"instId": order.inst_id}
            if order.ord_id:
                payload["ordId"] = order.ord_id
            if order.cl_ord_id:
                payload["clOrdId"] = order.cl_ord_id
            payload["newPx"] = decimal_to_str(intent.price)
            payload["newSz"] = decimal_to_str(new_total_size)
            payload["cxlOnFail"] = "false"
            payload["reqId"] = req_id
            batch_payloads.append(payload)
            journal_payloads.append(
                {
                    "cl_ord_id": order.cl_ord_id,
                    "ord_id": order.ord_id,
                    "side": order.side,
                    "reason": intent.reason,
                    "old_price": order.price,
                    "new_price": intent.price,
                    "old_size": order.size,
                    "new_size": new_total_size,
                    "filled_size": order.filled_size,
                    "old_remaining_size": order.remaining_size,
                    "new_remaining_size": base_size,
                    "req_id": req_id,
                }
            )
        try:
            results = await batch_amend_orders(orders=batch_payloads, request_id=request_id)
        except (Exception, asyncio.CancelledError) as exc:
            for order, _, _ in candidates:
                self.state.clear_pending_amend(order.cl_ord_id)
            self.journal.append(
                "batch_amend_order_error",
                {
                    "req_id": request_id,
                    "orders": journal_payloads,
                    "error": self._exception_message(exc),
                    "error_type": type(exc).__name__,
                },
            )
            logger.warning("批量改单失败 | %s", self._exception_message(exc))
            return True

        results_by_cl_ord_id = {str(item.get("clOrdId") or ""): item for item in results}
        results_by_ord_id = {str(item.get("ordId") or ""): item for item in results}
        for payload in journal_payloads:
            item = results_by_cl_ord_id.get(payload["cl_ord_id"]) or results_by_ord_id.get(payload["ord_id"] or "")
            if item is None:
                continue
            if str(item.get("sCode") or "0") != "0":
                self.state.clear_pending_amend(payload["cl_ord_id"])
                self.journal.append(
                    "amend_order_error",
                    {
                        **payload,
                        "okx": {"code": str(item.get("sCode") or ""), "msg": str(item.get("sMsg") or "")},
                        "error": str(item.get("sMsg") or "batch amend order failed"),
                    },
                )
                continue
            resolved_cl_ord_id = str(item.get("clOrdId") or payload["cl_ord_id"])
            resolved_ord_id = str(item.get("ordId") or payload["ord_id"] or "")
            self.state.update_pending_amend_identity(
                previous_cl_ord_id=payload["cl_ord_id"],
                cl_ord_id=resolved_cl_ord_id,
                ord_id=resolved_ord_id,
            )
            payload["cl_ord_id"] = resolved_cl_ord_id
            payload["ord_id"] = resolved_ord_id
            self.journal.append("amend_order_submitted", payload)
            self._last_action_by_side[payload["side"]] = now_ms()
        return True

    def _cooldown_ok(self, side: str) -> bool:
        last = self._last_action_by_side.get(side, 0)
        return (now_ms() - last) >= int(self.config.trading.action_cooldown_seconds * 1000)

    def _normalize_side_intents(self, *, side: str, intents) -> list:
        if intents is None:
            return []
        if isinstance(intents, (list, tuple)):
            normalized = [intent for intent in intents if intent is not None and getattr(intent, "side", None) == side]
        else:
            normalized = [intents] if getattr(intents, "side", None) == side else []
        max_orders = max(int(self.config.risk.max_managed_orders_per_side), 1)
        return normalized[:max_orders]

    def _resolved_targets_for_side(self, *, side: str, intents) -> list[tuple[object, Decimal]]:
        instrument = self.state.instrument
        if instrument is None:
            return []

        normalized = self._normalize_side_intents(side=side, intents=intents)
        if not normalized:
            return []

        remaining_budget = self._available_side_budget(side=side, live_orders=self.state.bot_orders(side))
        resolved: list[tuple[object, Decimal]] = []
        for intent in normalized:
            if intent.base_size is not None:
                desired = quantize_down(intent.base_size, instrument.lot_size)
            else:
                desired = self._base_size_for_intent(intent.price, intent.quote_notional, instrument)
            max_placeable = self._max_base_size_from_budget(
                side=side,
                price=intent.price,
                instrument=instrument,
                budget=remaining_budget,
            )
            base_size = min(desired, max_placeable)
            if base_size < instrument.min_size:
                self.journal.append(
                    "skip_order",
                    {
                        "side": side,
                        "reason": "size_below_min",
                        "reason_zh": translate_reason("size_below_min"),
                        "base_size": base_size,
                    },
                )
                continue
            resolved.append((intent, base_size))
            if side == "buy":
                remaining_budget = max(remaining_budget - (base_size * intent.price), Decimal("0"))
            else:
                remaining_budget = max(remaining_budget - base_size, Decimal("0"))
        return resolved

    def _available_side_budget(self, *, side: str, live_orders: list[LiveOrder]) -> Decimal:
        if not self.state.instrument:
            return Decimal("0")
        active_orders = [order for order in live_orders if not order.cancel_requested]
        if side == "buy":
            free_quote = self.state.free_balance(self.state.instrument.quote_ccy) - self.config.risk.min_free_quote_buffer
            reusable_quote = sum(order.remaining_size * order.price for order in active_orders)
            return max(free_quote, Decimal("0")) + reusable_quote
        if side == "sell":
            free_base = self.state.free_balance(self.state.instrument.base_ccy) - self.config.risk.min_free_base_buffer
            reusable_base = sum(order.remaining_size for order in active_orders)
            return max(free_base, Decimal("0")) + reusable_base
        return Decimal("0")

    @staticmethod
    def _max_base_size_from_budget(
        *,
        side: str,
        price: Decimal,
        instrument: InstrumentMeta,
        budget: Decimal,
    ) -> Decimal:
        if budget <= 0:
            return Decimal("0")
        if side == "buy":
            return quantize_down(budget / price, instrument.lot_size)
        if side == "sell":
            return quantize_down(budget, instrument.lot_size)
        return Decimal("0")

    @staticmethod
    def _pop_first_matching_order(*, orders: list[LiveOrder], intent, base_size: Decimal, matcher: Callable[..., bool]) -> LiveOrder | None:
        for index, order in enumerate(orders):
            if matcher(primary=order, intent=intent, base_size=base_size):
                return orders.pop(index)
        return None

    def _should_keep_order_without_intent(self, *, primary: LiveOrder, risk_status: RiskStatus | None) -> bool:
        if risk_status is None:
            return False
        if self._should_withdraw_managed_order_for_risk(risk_status):
            return False
        if self.state.has_pending_amend(primary.cl_ord_id):
            return True
        if risk_status.runtime_state in {"INIT", "PAUSED"}:
            return True
        if self._is_stale_market_data_reason(risk_status.reason) and not self.config.risk.cancel_orders_on_stale_book:
            return True
        if primary.filled_size > 0 and risk_status.ok and self._side_allowed_by_risk(primary.side, risk_status):
            return True
        return False

    def _should_withdraw_managed_order_for_risk(self, risk_status: RiskStatus) -> bool:
        reason = risk_status.reason
        if reason.startswith("stale public stream:"):
            return self.config.risk.cancel_orders_on_stale_book
        if reason.startswith("stale private stream:"):
            return True
        if reason.startswith("resync required: private_user reconnected"):
            return True
        if reason.startswith("resync required: public_books5 reconnected"):
            return self.config.risk.cancel_managed_orders_on_public_reconnect
        return False

    @staticmethod
    def _is_stale_market_data_reason(reason: str) -> bool:
        return reason.startswith("stale public stream:")

    def _should_keep_existing_order(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if self._same_live_order_target(primary=primary, intent=intent, base_size=base_size):
            return True
        if self.state.has_pending_amend(primary.cl_ord_id):
            return True
        if self._should_suppress_same_price_amend(primary=primary, intent=intent, base_size=base_size):
            return True
        if self._should_preserve_partial_fill_with_same_price(primary=primary, intent=intent, base_size=base_size):
            return True
        if self._rebalance_order_requires_refresh(primary=primary, intent=intent):
            return False
        if self._should_preserve_entry_queue(primary=primary, intent=intent, base_size=base_size):
            return True
        if self._should_preserve_rebalance_queue(primary=primary, intent=intent, base_size=base_size):
            return True
        return False

    @staticmethod
    def _same_live_order_target(*, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if primary.side != intent.side:
            return False
        if primary.price != intent.price:
            return False
        return primary.remaining_size == base_size

    def _should_suppress_same_price_amend(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if primary.side != intent.side:
            return False
        if primary.price != intent.price:
            return False
        current_remaining = primary.remaining_size
        if current_remaining <= 0:
            return False
        size_delta = abs(current_remaining - base_size)
        if size_delta <= 0:
            return True
        min_delta_base = max(self.config.trading.same_price_amend_min_remaining_change_base, Decimal("0"))
        min_delta_ratio = max(self.config.trading.same_price_amend_min_remaining_change_ratio, Decimal("0"))
        threshold = max(min_delta_base, current_remaining * min_delta_ratio)
        return size_delta < threshold

    @staticmethod
    def _side_allowed_by_risk(side: str, risk_status: RiskStatus) -> bool:
        if side == "buy":
            return risk_status.allow_bid
        if side == "sell":
            return risk_status.allow_ask
        return False

    def _should_preserve_partial_fill_with_same_price(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if primary.filled_size <= 0:
            return False
        if primary.side != intent.side:
            return False
        if primary.price != intent.price:
            return False
        if primary.remaining_size <= 0:
            return False
        return primary.remaining_size <= base_size

    def _should_preserve_entry_queue(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if not self.config.strategy.preserve_entry_queue:
            return False
        if not self.state.instrument or not self.state.book:
            return False
        if not self._entry_queue_preserve_allowed(side=primary.side):
            return False
        if primary.side != intent.side:
            return False
        if primary.remaining_size <= 0:
            return False
        if not self._entry_order_matches_requested_exposure(primary=primary, intent=intent, base_size=base_size):
            return False
        if intent.reason not in {"join_best_bid", "join_best_ask", "join_second_bid", "join_second_ask"}:
            return False
        min_spread = self.state.instrument.tick_size * Decimal(self.config.strategy.min_spread_ticks)
        if intent.reason in {"join_best_bid", "join_second_bid"}:
            best_ask = self.state.book.best_ask.price if self.state.book.best_ask else None
            if best_ask is None:
                return False
            if primary.price < intent.price:
                return False
            return (best_ask - primary.price) >= min_spread
        if intent.reason in {"join_best_ask", "join_second_ask"}:
            best_bid = self.state.book.best_bid.price if self.state.book.best_bid else None
            if best_bid is None:
                return False
            if primary.price > intent.price:
                return False
            return (primary.price - best_bid) >= min_spread
        return False

    def _entry_queue_preserve_allowed(self, *, side: str) -> bool:
        strategy_position = self.state.strategy_position_base()
        if strategy_position > 0:
            return side != "buy"
        if strategy_position < 0:
            return side != "sell"
        if not self.config.strategy.account_inventory_skew_enabled:
            return True

        inventory_ratio = self.state.inventory_ratio()
        if inventory_ratio is None:
            return True
        soft_lower = min(self.config.strategy.inventory_soft_lower_pct, self.config.strategy.inventory_target_pct)
        soft_upper = max(self.config.strategy.inventory_soft_upper_pct, self.config.strategy.inventory_target_pct)
        if inventory_ratio > soft_upper and side == "buy":
            return False
        if inventory_ratio < soft_lower and side == "sell":
            return False
        return True

    def _entry_order_matches_requested_exposure(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if intent.base_size is not None:
            return primary.remaining_size == base_size
        if not self.state.instrument:
            return False
        tolerance = max(primary.price, intent.price) * self.state.instrument.lot_size
        current_quote_notional = primary.remaining_size * primary.price
        return abs(current_quote_notional - intent.quote_notional) <= tolerance

    def _should_preserve_rebalance_queue(self, *, primary: LiveOrder, intent, base_size: Decimal) -> bool:
        if not self.config.strategy.preserve_rebalance_queue:
            return False
        if not self.state.instrument or not self.state.book:
            return False
        if self._rebalance_order_requires_refresh(primary=primary, intent=intent):
            return False
        if primary.remaining_size != base_size:
            return False
        if intent.reason == "rebalance_open_long":
            floor = self.state.min_rebalance_sell_price(
                base_size,
                tick_size=self.state.instrument.tick_size,
                profit_ticks=self.config.strategy.rebalance_min_profit_ticks,
            )
            if floor is None:
                return False
            return primary.side == "sell" and primary.price <= intent.price and primary.price >= floor
        if intent.reason == "rebalance_open_short":
            cap = self.state.max_rebalance_buy_price(
                base_size,
                tick_size=self.state.instrument.tick_size,
                profit_ticks=self.config.strategy.rebalance_min_profit_ticks,
            )
            if cap is None:
                return False
            return primary.side == "buy" and primary.price >= intent.price and primary.price <= cap
        min_spread = self.state.instrument.tick_size * Decimal(self.config.strategy.min_spread_ticks)
        if intent.reason == "rebalance_secondary_ask":
            return self._should_preserve_overlay_queue(primary=primary, intent=intent)
        if intent.reason == "rebalance_secondary_bid":
            return self._should_preserve_overlay_queue(primary=primary, intent=intent)
        return False

    def _should_preserve_overlay_queue(self, *, primary: LiveOrder, intent) -> bool:
        if not self.state.instrument or not self.state.book:
            return False
        tick_size = self.state.instrument.tick_size
        if tick_size <= 0:
            return False

        side = primary.side
        if self.state.is_toxic_flow_side_cooling_down(side):
            return False

        tolerance_ticks = max(int(self.config.strategy.rebalance_overlay_preserve_tolerance_ticks), 0)
        min_edge_ticks = max(int(self.config.strategy.secondary_min_positive_edge_ticks), 0)
        min_edge_ticks += self._secondary_markout_edge_penalty_ticks(side=side)
        best_bid = self.state.book.best_bid.price if self.state.book.best_bid else None
        best_ask = self.state.book.best_ask.price if self.state.book.best_ask else None

        if intent.reason == "rebalance_secondary_ask":
            if best_bid is None:
                return False
            if side != "sell" or primary.price > intent.price:
                return False
            current_edge_ticks = passive_edge_ticks(
                side="sell",
                price=primary.price,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )
            target_edge_ticks = passive_edge_ticks(
                side="sell",
                price=intent.price,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )
        elif intent.reason == "rebalance_secondary_bid":
            if best_ask is None:
                return False
            if side != "buy" or primary.price < intent.price:
                return False
            current_edge_ticks = passive_edge_ticks(
                side="buy",
                price=primary.price,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )
            target_edge_ticks = passive_edge_ticks(
                side="buy",
                price=intent.price,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )
        else:
            return False
        if current_edge_ticks is None or target_edge_ticks is None:
            return False

        required_edge_ticks = max(min_edge_ticks, target_edge_ticks - tolerance_ticks)
        return current_edge_ticks >= required_edge_ticks

    def _secondary_markout_edge_penalty_ticks(self, *, side: str) -> int:
        window_ms = max(int(self.config.strategy.secondary_markout_window_ms), 0)
        trigger_samples = max(int(self.config.strategy.secondary_markout_trigger_samples), 0)
        threshold_ticks = max(self.config.strategy.secondary_markout_adverse_threshold_ticks, Decimal("0"))
        severe_extra_ticks = max(self.config.strategy.toxicity_severe_extra_ticks, Decimal("0"))
        level = self.state.adverse_fill_markout_level(
            side=side,
            window_ms=window_ms,
            trigger_samples=trigger_samples,
            threshold_ticks=threshold_ticks,
            severe_extra_ticks=severe_extra_ticks,
        )
        if level <= 0:
            return 0
        edge_penalty_ticks = max(int(self.config.strategy.secondary_markout_penalty_edge_ticks), 0)
        if level >= 2:
            edge_penalty_ticks += max(int(self.config.strategy.toxicity_severe_extra_edge_ticks), 0)
        return edge_penalty_ticks

    def _rebalance_order_requires_refresh(self, *, primary: LiveOrder, intent) -> bool:
        rebalance_reasons = {
            "rebalance_open_long",
            "rebalance_open_short",
            "rebalance_secondary_ask",
            "rebalance_secondary_bid",
        }
        if intent.reason not in rebalance_reasons:
            return False
        return self._rebalance_order_age_exceeded(primary=primary) or self._rebalance_order_drift_exceeded(primary=primary)

    def _rebalance_order_age_exceeded(self, *, primary: LiveOrder) -> bool:
        max_age_ms = int(max(self.config.strategy.rebalance_max_order_age_seconds, 0) * 1000)
        if max_age_ms <= 0:
            return False
        return max(now_ms() - primary.created_at_ms, 0) >= max_age_ms

    def _rebalance_order_drift_exceeded(self, *, primary: LiveOrder) -> bool:
        if not self.state.instrument or not self.state.book:
            return False
        threshold_ticks = max(int(self.config.strategy.rebalance_drift_ticks), 0)
        if threshold_ticks <= 0:
            return False
        tick_size = self.state.instrument.tick_size
        if tick_size <= 0:
            return False
        if primary.side == "sell":
            best_price = self.state.book.best_ask.price if self.state.book.best_ask else None
            if best_price is None or primary.price <= best_price:
                return False
            drift_ticks = int((primary.price - best_price) / tick_size)
            return drift_ticks >= threshold_ticks
        if primary.side == "buy":
            best_price = self.state.book.best_bid.price if self.state.book.best_bid else None
            if best_price is None or primary.price >= best_price:
                return False
            drift_ticks = int((best_price - primary.price) / tick_size)
            return drift_ticks >= threshold_ticks
        return False

    @staticmethod
    def _is_benign_terminal_cancel_error(exc: Exception) -> bool:
        if not isinstance(exc, ExchangeAPIError):
            return False
        if exc.code in {"-2011"}:
            return True
        if not exc.data:
            return False
        terminal_codes = {"51400", "-2011"}
        found_terminal = False
        for item in exc.data:
            s_code = str(item.get("sCode") or item.get("code") or "")
            if s_code in terminal_codes:
                found_terminal = True
                continue
            return False
        return found_terminal

    def _resolved_base_size_for_intent(self, *, side: str, intent, instrument: InstrumentMeta, existing_order: LiveOrder | None = None) -> Decimal:
        if intent.base_size is not None:
            desired = quantize_down(intent.base_size, instrument.lot_size)
        else:
            desired = self._base_size_for_intent(intent.price, intent.quote_notional, instrument)
        max_placeable = self._max_placeable_base_size(
            side=side,
            price=intent.price,
            instrument=instrument,
            existing_order=existing_order,
        )
        return min(desired, max_placeable)

    def _max_placeable_base_size(
        self,
        *,
        side: str,
        price: Decimal,
        instrument: InstrumentMeta,
        existing_order: LiveOrder | None = None,
    ) -> Decimal:
        if not self.state.instrument:
            return Decimal("0")
        if side == "buy":
            reusable_quote = Decimal("0")
            if existing_order and existing_order.side == "buy":
                reusable_quote = existing_order.remaining_size * existing_order.price
            free_quote = (
                self.state.free_balance(self.state.instrument.quote_ccy)
                + reusable_quote
                - self.config.risk.min_free_quote_buffer
            )
            if free_quote <= 0:
                return Decimal("0")
            return quantize_down(free_quote / price, instrument.lot_size)
        if side == "sell":
            reusable_base = Decimal("0")
            if existing_order and existing_order.side == "sell":
                reusable_base = existing_order.remaining_size
            free_base = (
                self.state.free_balance(self.state.instrument.base_ccy)
                + reusable_base
                - self.config.risk.min_free_base_buffer
            )
            if free_base <= 0:
                return Decimal("0")
            return quantize_down(free_base, instrument.lot_size)
        return Decimal("0")

    @staticmethod
    def _base_size_for_intent(price: Decimal, quote_notional: Decimal, instrument: InstrumentMeta) -> Decimal:
        raw = quote_notional / price
        return quantize_down(raw, instrument.lot_size)
