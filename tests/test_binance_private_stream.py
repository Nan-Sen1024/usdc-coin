from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ExchangeConfig
from src.binance_private_stream import BinancePrivateUserStream


class DummyRest:
    def __init__(self):
        self.config = ExchangeConfig(name="binance", api_key="k", secret_key="s")


def test_binance_private_stream_normalizes_execution_report():
    stream = BinancePrivateUserStream(
        url="wss://ws-api.binance.com:443/ws-api/v3",
        rest=DummyRest(),  # type: ignore[arg-type]
        inst_id="USDC-USDT",
        on_order=None,  # type: ignore[arg-type]
        on_account=None,  # type: ignore[arg-type]
    )

    payload = stream._normalize_execution_report(
        {
            "s": "USDCUSDT",
            "S": "BUY",
            "i": 123,
            "c": "client1",
            "p": "1.0000",
            "q": "1000",
            "z": "200",
            "l": "200",
            "L": "1.0000",
            "X": "PARTIALLY_FILLED",
            "O": 1000,
            "T": 2000,
        }
    )

    assert payload["instId"] == "USDC-USDT"
    assert payload["side"] == "buy"
    assert payload["ordId"] == "123"
    assert payload["clOrdId"] == "client1"
    assert payload["state"] == "partially_filled"
    assert payload["accFillSz"] == "200"


def test_binance_private_stream_normalizes_account_event():
    payload = BinancePrivateUserStream._normalize_account_event(
        {
            "e": "outboundAccountPosition",
            "B": [
                {"a": "USDC", "f": "100", "l": "10"},
                {"a": "USDT", "f": "200", "l": "20"},
            ],
        }
    )

    assert payload["details"][0]["ccy"] == "USDC"
    assert payload["details"][0]["cashBal"] == "100"
    assert payload["details"][1]["ccy"] == "USDT"


def test_binance_private_stream_prefers_original_client_order_id_for_canceled_update():
    stream = BinancePrivateUserStream(
        url="wss://ws-api.binance.com:443/ws-api/v3",
        rest=DummyRest(),  # type: ignore[arg-type]
        inst_id="USDC-USDT",
        on_order=None,  # type: ignore[arg-type]
        on_account=None,  # type: ignore[arg-type]
    )

    payload = stream._normalize_execution_report(
        {
            "s": "USDCUSDT",
            "S": "SELL",
            "i": 456,
            "c": "cancel-request-id",
            "C": "managed-order-id",
            "p": "1.0003",
            "q": "1000",
            "z": "0",
            "l": "0",
            "L": "0",
            "X": "CANCELED",
            "O": 1000,
            "T": 2000,
        }
    )

    assert payload["clOrdId"] == "managed-order-id"
    assert payload["ordId"] == "456"
    assert payload["state"] == "canceled"
