from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from .models import BookLevel, BookSnapshot, TradeTick
from .utils import now_ms, parse_decimal

logger = logging.getLogger(__name__)

BookCallback = Callable[[BookSnapshot], Awaitable[None]]
TradeCallback = Callable[[TradeTick], Awaitable[None]]
ReconnectCallback = Callable[[str], Awaitable[None]]
StatusCallback = Callable[[str, bool], Awaitable[None]]
ErrorCallback = Callable[[str, Exception], Awaitable[None]]
ActivityCallback = Callable[[str, str], Awaitable[None]]


class PublicBookStream:
    HEARTBEAT_INTERVAL_SECONDS = 5.0
    RECV_TIMEOUT_SECONDS = 20.0

    def __init__(
        self,
        *,
        url: str,
        inst_id: str,
        on_book: BookCallback,
        on_trade: TradeCallback | None = None,
        on_reconnect: ReconnectCallback | None = None,
        on_status: StatusCallback | None = None,
        on_error: ErrorCallback | None = None,
        on_activity: ActivityCallback | None = None,
        subscribe_trades: bool = False,
    ):
        self.url = url
        self.inst_id = inst_id
        self.on_book = on_book
        self.on_trade = on_trade
        self.on_reconnect = on_reconnect
        self.on_status = on_status
        self.on_error = on_error
        self.on_activity = on_activity
        self.subscribe_trades = subscribe_trades
        self.ws = None
        self.running = False
        self.task: asyncio.Task | None = None
        self._connected_once = False

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

    async def _run(self) -> None:
        while self.running:
            heartbeat_task: asyncio.Task | None = None
            try:
                async with websockets.connect(self.url, ping_interval=None, close_timeout=5) as ws:
                    self.ws = ws
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    if self._connected_once and self.on_reconnect:
                        await self.on_reconnect("public_books5")
                    self._connected_once = True
                    subscribe_args = [{"channel": "books5", "instId": self.inst_id}]
                    if self.subscribe_trades:
                        subscribe_args.append({"channel": "trades", "instId": self.inst_id})
                    await ws.send(json.dumps({"op": "subscribe", "args": subscribe_args}))
                    await self._emit_status(True)
                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.RECV_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError:
                            raise ConnectionError(
                                f"No data received from public_books5 in {self.RECV_TIMEOUT_SECONDS}s"
                            ) from None
                        if raw == "pong":
                            await self._emit_activity("public_books5", "pong")
                            continue
                        data = json.loads(raw)
                        if data.get("event") in {"subscribe", "unsubscribe"}:
                            continue
                        channel = data.get("arg", {}).get("channel")
                        if channel == "books5":
                            await self._emit_activity("public_books5", "books5")
                            for item in data.get("data", []):
                                snapshot = BookSnapshot(
                                    ts_ms=int(item["ts"]),
                                    received_ms=now_ms(),
                                    bids=[
                                        BookLevel(price=parse_decimal(level[0]), size=parse_decimal(level[1]), order_count=int(level[3]))
                                        for level in item.get("bids", [])
                                    ],
                                    asks=[
                                        BookLevel(price=parse_decimal(level[0]), size=parse_decimal(level[1]), order_count=int(level[3]))
                                        for level in item.get("asks", [])
                                    ],
                                )
                                await self.on_book(snapshot)
                            continue
                        if channel == "trades" and self.on_trade:
                            await self._emit_activity("public_books5", "trades")
                            received_ms = now_ms()
                            for item in data.get("data", []):
                                trade = TradeTick(
                                    ts_ms=int(item["ts"]),
                                    price=parse_decimal(item["px"]),
                                    size=parse_decimal(item["sz"]),
                                    side=str(item.get("side") or ""),
                                    received_ms=received_ms,
                                    trade_id=str(item.get("tradeId") or "") or None,
                                )
                                await self.on_trade(trade)
            except Exception as exc:
                if self.on_error:
                    await self.on_error("public_books5", exc)
                await self._emit_status(False)
                logger.warning("Public book stream reconnecting: %s", exc)
                await asyncio.sleep(1)
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
                self.ws = None

    async def _heartbeat_loop(self, ws) -> None:
        while self.running:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
            await ws.send("ping")

    async def _emit_status(self, connected: bool) -> None:
        if self.on_status:
            await self.on_status("public_books5", connected)

    async def _emit_activity(self, stream_name: str, activity: str) -> None:
        if self.on_activity:
            await self.on_activity(stream_name, activity)
