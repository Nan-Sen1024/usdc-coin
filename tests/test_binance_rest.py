from decimal import Decimal
import asyncio
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.binance_auth import BinanceSigner
from src.binance_rest import BinanceRestClient, BinanceAPIError
from src.config import ExchangeConfig


def test_binance_signer_adds_signature():
    signer = BinanceSigner("key", "secret")
    signed = signer.sign_query({"symbol": "USDCUSDT", "timestamp": "1"})
    assert "timestamp=1" in signed
    assert signed.index("symbol=USDCUSDT") < signed.index("timestamp=1")
    assert "signature=" in signed


def test_binance_rest_fetch_instrument_parses_exchange_info():
    payload = {
        "symbols": [
            {
                "symbol": "USDCUSDT",
                "baseAsset": "USDC",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "1"},
                    {"filterType": "NOTIONAL", "maxNotional": "1000000"},
                ],
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.fetch_instrument("USDC-USDT", "SPOT")
        finally:
            await client.close()

    instrument = asyncio.run(run())
    assert instrument.inst_id == "USDC-USDT"
    assert instrument.base_ccy == "USDC"
    assert instrument.quote_ccy == "USDT"
    assert instrument.tick_size == Decimal("0.0001")
    assert instrument.lot_size == Decimal("0.01")
    assert instrument.min_size == Decimal("1")
    assert instrument.state == "live"


def test_binance_rest_fetch_balances_parses_account_payload():
    payload = {
        "balances": [
            {"asset": "USDC", "free": "12.5", "locked": "0.5"},
            {"asset": "USDT", "free": "30", "locked": "2"},
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.fetch_balances(["USDC", "USDT"])
        finally:
            await client.close()

    balances = asyncio.run(run())
    assert balances["USDC"].total == Decimal("13.0")
    assert balances["USDC"].available == Decimal("12.5")
    assert balances["USDT"].frozen == Decimal("2")


def test_binance_rest_fetch_balances_retries_after_timestamp_drift():
    requests: list[str] = []
    account_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal account_attempts
        requests.append(str(request.url.path))
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000) + 1200})
        if request.url.path == "/api/v3/account":
            account_attempts += 1
            if account_attempts == 1:
                return httpx.Response(
                    400,
                    json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."},
                )
            return httpx.Response(
                200,
                json={"balances": [{"asset": "USDC", "free": "12.5", "locked": "0.5"}]},
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.fetch_balances(["USDC"])
        finally:
            await client.close()

    balances = asyncio.run(run())
    assert balances["USDC"].total == Decimal("13.0")
    assert requests == ["/api/v3/account", "/api/v3/time", "/api/v3/account"]


def test_binance_rest_refreshes_stale_time_offset_before_signed_request():
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url.path), str(request.url.query)))
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000) + 1200})
        if request.url.path == "/api/v3/account":
            return httpx.Response(
                200,
                json={"balances": [{"asset": "USDC", "free": "12.5", "locked": "0.5"}]},
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        client.time_offset_ms = 250
        client.last_time_sync_ms -= client.TIME_SYNC_REFRESH_MS + 1
        try:
            return await client.fetch_balances(["USDC"])
        finally:
            await client.close()

    balances = asyncio.run(run())
    assert balances["USDC"].total == Decimal("13.0")
    assert requests[0][0] == "/api/v3/time"
    assert requests[1][0] == "/api/v3/account"
    assert "recvWindow=15000" in requests[1][1]


def test_binance_rest_fetch_best_bid_ask_uses_book_ticker_endpoint():
    payload = {
        "symbol": "USDCUSDT",
        "bidPrice": "1.0002",
        "bidQty": "1000",
        "askPrice": "1.0003",
        "askQty": "2000",
    }
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = str(request.url.path)
        seen["query"] = str(request.url.query)
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.fetch_best_bid_ask("USDC-USDT")
        finally:
            await client.close()

    book = asyncio.run(run())
    assert seen["path"] == "/api/v3/ticker/bookTicker"
    assert "symbol=USDCUSDT" in seen["query"]
    assert book.best_bid is not None
    assert book.best_bid.price == Decimal("1.0002")
    assert book.best_bid.size == Decimal("1000")
    assert book.best_ask is not None
    assert book.best_ask.price == Decimal("1.0003")
    assert book.best_ask.size == Decimal("2000")


def test_binance_rest_fetch_best_bid_ask_many_uses_symbols_parameter():
    payload = [
        {
            "symbol": "USDCUSDT",
            "bidPrice": "1.0002",
            "bidQty": "1000",
            "askPrice": "1.0003",
            "askQty": "2000",
        },
        {
            "symbol": "USD1USDC",
            "bidPrice": "0.9999",
            "bidQty": "3000",
            "askPrice": "1.0000",
            "askQty": "4000",
        },
    ]
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = str(request.url.path)
        seen["query"] = str(request.url.query)
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.fetch_best_bid_ask_many(["USDC-USDT", "USD1-USDC"])
        finally:
            await client.close()

    books = asyncio.run(run())
    assert seen["path"] == "/api/v3/ticker/bookTicker"
    assert "symbols=" in seen["query"]
    assert books["USDC-USDT"].best_bid is not None
    assert books["USDC-USDT"].best_bid.price == Decimal("1.0002")
    assert books["USD1-USDC"].best_ask is not None
    assert books["USD1-USDC"].best_ask.price == Decimal("1.0000")


def test_binance_rest_fetch_trade_fee_parses_commission_payload():
    payload = {
        "symbol": "USDCUSDT",
        "standardCommission": {"maker": "0.00000000", "taker": "0.00100000"},
        "discount": {"enabledForAccount": True},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.fetch_trade_fee("SPOT", "USDC-USDT")
        finally:
            await client.close()

    fee = asyncio.run(run())
    assert fee["maker"] == Decimal("0")
    assert fee["taker"] == Decimal("0.001")
    assert fee["feeType"] == "standardCommission"


def test_binance_rest_list_pending_orders_normalizes_binance_payload():
    payload = [
        {
            "symbol": "USDCUSDT",
            "side": "BUY",
            "orderId": 123456,
            "clientOrderId": "binusdcm-buy-1",
            "price": "1.00020000",
            "origQty": "1000.00000000",
            "executedQty": "12.50000000",
            "status": "PARTIALLY_FILLED",
            "time": 1700000000000,
            "updateTime": 1700000001000,
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.list_pending_orders("USDC-USDT", "SPOT")
        finally:
            await client.close()

    orders = asyncio.run(run())
    assert orders == [
        {
            "instId": "USDC-USDT",
            "side": "buy",
            "ordId": "123456",
            "clOrdId": "binusdcm-buy-1",
            "px": "1.00020000",
            "sz": "1000.00000000",
            "accFillSz": "12.50000000",
            "fillSz": "0",
            "fillPx": "0",
            "state": "partially_filled",
            "cTime": "1700000000000",
            "uTime": "1700000001000",
        }
    ]


def test_binance_rest_place_limit_order_raises_binance_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            await client.place_limit_order(
                inst_id="USDC-USDT",
                side="buy",
                price=Decimal("1"),
                size=Decimal("1"),
                cl_ord_id="testbinance001",
                post_only=True,
            )
        finally:
            await client.close()

    try:
        asyncio.run(run())
    except BinanceAPIError as exc:
        assert exc.code == "-1121"
    else:
        raise AssertionError("Expected BinanceAPIError")


def test_binance_rest_amend_order_uses_cancel_replace_and_returns_new_order():
    seen = {}
    payload = {
        "cancelResult": "SUCCESS",
        "newOrderResult": "SUCCESS",
        "cancelResponse": {
            "symbol": "USDCUSDT",
            "origClientOrderId": "old-client",
            "orderId": 111,
            "status": "CANCELED",
        },
        "newOrderResponse": {
            "symbol": "USDCUSDT",
            "clientOrderId": "new-client",
            "orderId": 222,
            "status": "NEW",
            "price": "1.0001",
            "origQty": "5000",
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            return await client.amend_order(
                inst_id="USDC-USDT",
                side="buy",
                post_only=True,
                ord_id="111",
                cl_ord_id="old-client",
                new_price=Decimal("1.0001"),
                new_size=Decimal("5000"),
                req_id="req-amend-1",
            )
        finally:
            await client.close()

    result = asyncio.run(run())
    assert seen["path"] == "/api/v3/order/cancelReplace"
    assert seen["params"]["symbol"] == "USDCUSDT"
    assert seen["params"]["cancelReplaceMode"] == "STOP_ON_FAILURE"
    assert seen["params"]["cancelOrderId"] == "111"
    assert seen["params"]["cancelOrigClientOrderId"] == "old-client"
    assert seen["params"]["newClientOrderId"] == "req-amend-1"
    assert seen["params"]["type"] == "LIMIT_MAKER"
    assert result["ordId"] == "222"
    assert result["clOrdId"] == "new-client"


def test_binance_rest_amend_order_raises_with_cancel_replace_failure_details():
    payload = {
        "code": -2021,
        "msg": "Order cancel-replace partially failed.",
        "data": {
            "cancelResult": "SUCCESS",
            "newOrderResult": "FAILURE",
            "cancelResponse": {
                "symbol": "USDCUSDT",
                "origClientOrderId": "old-client",
                "orderId": 111,
                "status": "CANCELED",
            },
            "newOrderResponse": {
                "code": -1013,
                "msg": "Filter failure: PRICE_FILTER",
            },
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=payload)

    async def run():
        client = BinanceRestClient(ExchangeConfig(name="binance", api_key="k", secret_key="s"))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")
        try:
            await client.amend_order(
                inst_id="USDC-USDT",
                side="sell",
                post_only=True,
                ord_id="111",
                cl_ord_id="old-client",
                new_price=Decimal("1.0002"),
                new_size=Decimal("4000"),
                req_id="req-amend-2",
            )
        finally:
            await client.close()

    try:
        asyncio.run(run())
    except BinanceAPIError as exc:
        assert exc.code == "-2021"
        assert exc.status_code == 409
        assert any(item.get("msg") == "Filter failure: PRICE_FILTER" for item in exc.data)
    else:
        raise AssertionError("Expected BinanceAPIError")
