from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

import websockets

from .binance_auth import BinanceSigner
from .binance_rest import BinanceRestClient
from .exchange_errors import ExchangeAPIError
from .utils import build_req_id, now_ms

logger = logging.getLogger(__name__)

RawHandler = Callable[[dict], Awaitable[None]]
ReconnectHandler = Callable[[str], Awaitable[None]]
StatusHandler = Callable[[str, bool], Awaitable[None]]
ErrorHandler = Callable[[str, Exception], Awaitable[None]]


class BinancePrivateUserStream:
    HEARTBEAT_INTERVAL_SECONDS = 20.0
    RECV_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        *,
        url: str,
        rest: BinanceRestClient,
        inst_id: str,
        on_order: RawHandler,
        on_account: RawHandler,
        on_reconnect: ReconnectHandler | None = None,
        on_status: StatusHandler | None = None,
        on_error: ErrorHandler | None = None,
    ):
        self.url = url
        self.rest = rest
        self.inst_id = inst_id
        self.symbol = inst_id.replace("-", "").upper()
        self.on_order = on_order
        self.on_account = on_account
        self.on_reconnect = on_reconnect
        self.on_status = on_status
        self.on_error = on_error
        self.running = False
        self.ws = None
        self.task: asyncio.Task | None = None
        self._connected_once = False
        self._subscription_id: int | None = None
        self.signer = BinanceSigner(rest.config.api_key, rest.config.secret_key)

    async def start(self) -> None:
        if self.task:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        await self._emit_status(False)
        if self.ws:
            await self.ws.close()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None
        self._subscription_id = None

    def trade_ready(self) -> bool:
        return self.ws is not None and self._subscription_id is not None

    async def _run(self) -> None:
        while self.running:
            heartbeat_task: asyncio.Task | None = None
            try:
                async with websockets.connect(self.url, ping_interval=None, close_timeout=5) as ws:
                    self.ws = ws
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    if self._connected_once and self.on_reconnect:
                        await self.on_reconnect("private_user")
                    self._connected_once = True
                    await self._subscribe_user_stream()
                    await self._emit_status(True)
                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.RECV_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError:
                            await ws.ping()
                            continue
                        data = json.loads(raw)
                        event = str(data.get("e") or "")
                        if not event and isinstance(data.get("event"), dict):
                            event_payload = data.get("event") or {}
                            event = str(event_payload.get("e") or "")
                            payload = event_payload
                        else:
                            payload = data
                        if event == "executionReport":
                            await self.on_order(self._normalize_execution_report(payload))
                        elif event in {"outboundAccountPosition", "balanceUpdate"}:
                            await self.on_account(self._normalize_account_event(payload))
            except Exception as exc:
                if self.on_error:
                    await self.on_error("private_user", exc)
                await self._emit_status(False)
                logger.warning("Binance private user stream reconnecting: %s", exc)
                await asyncio.sleep(1)
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
                with contextlib.suppress(Exception):
                    await self._unsubscribe_user_stream()
                self._subscription_id = None
                self.ws = None

    async def _heartbeat_loop(self, ws) -> None:
        while self.running:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
            await ws.ping()

    async def _emit_status(self, connected: bool) -> None:
        if self.on_status:
            await self.on_status("private_user", connected)

    async def _subscribe_user_stream(self) -> None:
        response = await self._send_request_sync(
            method="userDataStream.subscribe.signature",
            params={
                "apiKey": self.rest.config.api_key,
                "timestamp": now_ms() + self.rest.time_offset_ms,
                "recvWindow": Decimal("5000"),
            },
            signed=True,
        )
        status = int(response.get("status") or 0)
        if status != 200:
            raise ExchangeAPIError(
                path="ws:userDataStream.subscribe.signature",
                code=str(status),
                msg=str(response.get("error") or response),
            )
        result = response.get("result") or {}
        self._subscription_id = int(result.get("subscriptionId"))

    async def _unsubscribe_user_stream(self) -> None:
        if self.ws is None:
            return
        params = {"subscriptionId": self._subscription_id} if self._subscription_id is not None else None
        with contextlib.suppress(Exception):
            await self._send_request_sync(
                method="userDataStream.unsubscribe",
                params=params or {},
                signed=False,
            )

    async def _send_request_sync(self, *, method: str, params: dict[str, object], signed: bool) -> dict:
        ws = self.ws
        if ws is None:
            raise RuntimeError("binance private websocket is not connected")
        request_id = build_req_id("binws", method.replace(".", "")[:8])
        payload_params = dict(params)
        if signed:
            sign_payload = {
                key: self._stringify_param(value)
                for key, value in payload_params.items()
                if value is not None
            }
            query = self.signer.sign_query(sign_payload)
            for item in query.split("&"):
                key, _, value = item.partition("=")
                payload_params[key] = value
        payload = {
            "id": request_id,
            "method": method,
            "params": payload_params,
        }
        await ws.send(json.dumps(payload))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        return json.loads(raw)

    @staticmethod
    def _stringify_param(value: object) -> str:
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        return str(value)

    def _normalize_execution_report(self, payload: dict) -> dict:
        inst_id = self.inst_id if str(payload.get("s") or "").upper() == self.symbol else str(payload.get("s") or "")
        state = self._normalize_order_status(str(payload.get("X") or ""))
        client_order_id = str(payload.get("c") or "")
        original_client_order_id = str(payload.get("C") or "")
        # Binance terminal cancel/expire updates can surface a transient cancel request id in `c`
        # while the original managed order id is carried in `C`.
        if state == "canceled" and original_client_order_id:
            client_order_id = original_client_order_id
        elif not client_order_id and original_client_order_id:
            client_order_id = original_client_order_id
        return {
            "instId": inst_id,
            "side": str(payload.get("S") or "").lower(),
            "ordId": str(payload.get("i") or ""),
            "clOrdId": client_order_id,
            "px": str(payload.get("p") or "0"),
            "sz": str(payload.get("q") or "0"),
            "accFillSz": str(payload.get("z") or "0"),
            "fillSz": str(payload.get("l") or "0"),
            "fillPx": str(payload.get("L") or "0"),
            "state": state,
            "cTime": str(payload.get("O") or payload.get("E") or "0"),
            "uTime": str(payload.get("T") or payload.get("E") or "0"),
        }

    @staticmethod
    def _normalize_order_status(status: str) -> str:
        value = status.upper()
        mapping = {
            "NEW": "live",
            "PARTIALLY_FILLED": "partially_filled",
            "FILLED": "filled",
            "CANCELED": "canceled",
            "PENDING_CANCEL": "live",
            "REJECTED": "canceled",
            "EXPIRED": "canceled",
            "EXPIRED_IN_MATCH": "canceled",
        }
        return mapping.get(value, value.lower())

    @staticmethod
    def _normalize_account_event(payload: dict) -> dict:
        if str(payload.get("e") or "") == "outboundAccountPosition":
            details = [
                {
                    "ccy": item.get("a"),
                    "cashBal": item.get("f"),
                    "availBal": item.get("f"),
                    "locked": item.get("l"),
                }
                for item in payload.get("B", [])
            ]
            return {"details": details}
        asset = str(payload.get("a") or "")
        delta = payload.get("d") or "0"
        return {"details": [{"ccy": asset, "cashBal": delta, "availBal": delta}]}
