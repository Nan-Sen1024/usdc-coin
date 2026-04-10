from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from .okx_rest import OKXAPIError
from .okx_auth import OKXSigner
from .utils import build_req_id, ws_login_timestamp

logger = logging.getLogger(__name__)

RawHandler = Callable[[dict], Awaitable[None]]
ReconnectHandler = Callable[[str], Awaitable[None]]
StatusHandler = Callable[[str, bool], Awaitable[None]]
ErrorHandler = Callable[[str, Exception], Awaitable[None]]
ActivityHandler = Callable[[str, str], Awaitable[None]]


class PrivateUserStream:
    HEARTBEAT_INTERVAL_SECONDS = 5.0
    RECV_TIMEOUT_SECONDS = 20.0
    REQUEST_TIMEOUT_SECONDS = 8.0

    def __init__(
        self,
        *,
        url: str,
        signer: OKXSigner,
        time_offset_ms: int,
        inst_type: str,
        on_order: RawHandler,
        on_account: RawHandler,
        on_reconnect: ReconnectHandler | None = None,
        on_status: StatusHandler | None = None,
        on_error: ErrorHandler | None = None,
        on_activity: ActivityHandler | None = None,
    ):
        self.url = url
        self.signer = signer
        self.time_offset_ms = time_offset_ms
        self.inst_type = inst_type
        self.on_order = on_order
        self.on_account = on_account
        self.on_reconnect = on_reconnect
        self.on_status = on_status
        self.on_error = on_error
        self.on_activity = on_activity
        self.running = False
        self.ws = None
        self.task: asyncio.Task | None = None
        self._connected_once = False
        self._send_lock = asyncio.Lock()
        self._request_futures: dict[str, asyncio.Future] = {}

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
        for future in self._request_futures.values():
            if not future.done():
                future.cancel()
        self._request_futures.clear()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None

    def trade_ready(self) -> bool:
        return self.ws is not None

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

                    await self._login(ws)
                    await self._emit_activity("private_user", "login")
                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "args": [
                                    {"channel": "orders", "instType": self.inst_type},
                                    {"channel": "account"},
                                ],
                            }
                        )
                    )
                    await self._emit_activity("private_user", "subscribe")
                    await self._emit_status(True)

                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.RECV_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError:
                            raise ConnectionError(
                                f"No data received from private_user in {self.RECV_TIMEOUT_SECONDS}s"
                            ) from None
                        if raw == "pong":
                            await self._emit_activity("private_user", "pong")
                            continue
                        data = json.loads(raw)
                        if data.get("event") in {"login", "subscribe", "unsubscribe"}:
                            await self._emit_activity("private_user", str(data.get("event") or "event"))
                            continue
                        request_id = str(data.get("id") or "")
                        if request_id:
                            future = self._request_futures.pop(request_id, None)
                            if future and not future.done():
                                future.set_result(data)
                                await self._emit_activity("private_user", "request")
                                continue
                        channel = data.get("arg", {}).get("channel")
                        if channel == "orders":
                            await self._emit_activity("private_user", "orders")
                            for item in data.get("data", []):
                                await self.on_order(item)
                        elif channel == "account":
                            await self._emit_activity("private_user", "account")
                            for item in data.get("data", []):
                                await self.on_account(item)
            except Exception as exc:
                if self.on_error:
                    await self.on_error("private_user", exc)
                await self._emit_status(False)
                logger.warning("Private user stream reconnecting: %s", exc)
                await asyncio.sleep(1)
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
                self.ws = None
                for future in self._request_futures.values():
                    if not future.done():
                        future.cancel()
                self._request_futures.clear()

    async def _heartbeat_loop(self, ws) -> None:
        while self.running:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
            await ws.send("ping")

    async def _login(self, ws) -> None:
        timestamp = ws_login_timestamp(self.time_offset_ms)
        await ws.send(json.dumps({"op": "login", "args": [self.signer.ws_login_args(timestamp)]}))
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if response.get("event") != "login" or response.get("code") != "0":
            raise RuntimeError(f"Private WS login failed: {response}")

    async def _emit_status(self, connected: bool) -> None:
        if self.on_status:
            await self.on_status("private_user", connected)

    async def _emit_activity(self, stream_name: str, activity: str) -> None:
        if self.on_activity:
            await self.on_activity(stream_name, activity)

    @staticmethod
    def _trade_identifier_payload(*, inst_id: str, inst_id_code: str | None = None) -> dict[str, str]:
        if inst_id_code:
            return {"instIdCode": str(inst_id_code)}
        return {"instId": inst_id}

    @staticmethod
    def _require_trade_response(*, op: str, response: dict[str, Any]) -> list[dict[str, Any]]:
        path = f"ws:{op}"
        if str(response.get("code") or "0") != "0":
            raise OKXAPIError.from_payload(path=path, payload=response)
        data = list(response.get("data") or [])
        if not data:
            raise OKXAPIError(path=path, msg="empty data")
        return data

    @staticmethod
    def _require_single_trade_success(*, op: str, response: dict[str, Any]) -> dict[str, Any]:
        data = PrivateUserStream._require_trade_response(op=op, response=response)
        item = data[0]
        if str(item.get("sCode") or "0") != "0":
            raise OKXAPIError(path=f"ws:{op}", code=str(item.get("sCode") or ""), msg=str(item.get("sMsg") or ""), data=[item])
        return item

    async def send_request(self, *, op: str, args: list[dict[str, Any]], request_id: str | None = None) -> dict[str, Any]:
        ws = self.ws
        if ws is None:
            raise RuntimeError("private websocket is not connected")
        resolved_request_id = request_id or build_req_id("okxws", op)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._request_futures[resolved_request_id] = future
        payload = {"id": resolved_request_id, "op": op, "args": args}
        try:
            async with self._send_lock:
                if self.ws is None:
                    raise RuntimeError("private websocket disconnected before request send")
                await self.ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=self.REQUEST_TIMEOUT_SECONDS)
        except Exception:
            self._request_futures.pop(resolved_request_id, None)
            raise

    async def place_limit_order(
        self,
        *,
        inst_id: str,
        side: str,
        price: Any,
        size: Any,
        cl_ord_id: str,
        post_only: bool = False,
        req_id: str | None = None,
        inst_id_code: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            **self._trade_identifier_payload(inst_id=inst_id, inst_id_code=inst_id_code),
            "tdMode": "cash",
            "side": side,
            "ordType": "post_only" if post_only else "limit",
            "px": str(price),
            "sz": str(size),
            "clOrdId": cl_ord_id,
        }
        response = await self.send_request(op="order", args=[payload], request_id=req_id)
        return self._require_single_trade_success(op="order", response=response)

    async def amend_order(
        self,
        *,
        inst_id: str,
        side: Any | None = None,
        new_price: Any,
        new_size: Any,
        filled_size: Any | None = None,
        post_only: Any | None = None,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
        cxl_on_fail: bool = False,
        req_id: str | None = None,
        inst_id_code: str | None = None,
    ) -> dict[str, Any]:
        del side, filled_size, post_only
        amend_req_id = req_id or build_req_id("okxws", "amnd")
        payload = {
            **self._trade_identifier_payload(inst_id=inst_id, inst_id_code=inst_id_code),
            "newPx": str(new_price),
            "newSz": str(new_size),
            "cxlOnFail": str(cxl_on_fail).lower(),
            "reqId": amend_req_id,
        }
        if ord_id:
            payload["ordId"] = ord_id
        if cl_ord_id:
            payload["clOrdId"] = cl_ord_id
        response = await self.send_request(op="amend-order", args=[payload], request_id=amend_req_id)
        return self._require_single_trade_success(op="amend-order", response=response)

    async def cancel_order(
        self,
        *,
        inst_id: str,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
        req_id: str | None = None,
        inst_id_code: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            **self._trade_identifier_payload(inst_id=inst_id, inst_id_code=inst_id_code),
        }
        if ord_id:
            payload["ordId"] = ord_id
        if cl_ord_id:
            payload["clOrdId"] = cl_ord_id
        response = await self.send_request(op="cancel-order", args=[payload], request_id=req_id)
        return self._require_single_trade_success(op="cancel-order", response=response)

    async def batch_cancel_orders(self, *, orders: list[dict[str, Any]], request_id: str | None = None) -> list[dict[str, Any]]:
        response = await self.send_request(op="batch-cancel-orders", args=orders, request_id=request_id)
        return self._require_trade_response(op="batch-cancel-orders", response=response)

    async def batch_amend_orders(self, *, orders: list[dict[str, Any]], request_id: str | None = None) -> list[dict[str, Any]]:
        response = await self.send_request(op="batch-amend-orders", args=orders, request_id=request_id)
        return self._require_trade_response(op="batch-amend-orders", response=response)
