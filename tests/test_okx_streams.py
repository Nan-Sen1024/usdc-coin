import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.market_data import PublicBookStream
from src.okx_auth import OKXSigner
from src.private_stream import PrivateUserStream


class FakeWebSocket:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        item = self._responses.pop(0)
        if item == "__block__":
            await asyncio.Future()
        return item

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _noop_book(snapshot):
    del snapshot


async def _noop_trade(trade):
    del trade


async def _noop_order(payload):
    del payload


def test_okx_public_stream_reconnects_on_recv_timeout(monkeypatch):
    ws = FakeWebSocket(["__block__"])
    statuses: list[tuple[str, bool]] = []
    errors: list[tuple[str, str]] = []

    async def on_status(stream_name: str, connected: bool) -> None:
        statuses.append((stream_name, connected))

    async def on_error(stream_name: str, exc: Exception) -> None:
        errors.append((stream_name, str(exc)))

    stream = PublicBookStream(
        url="wss://example.invalid/ws/public",
        inst_id="USDC-USDT",
        on_book=_noop_book,
        on_trade=_noop_trade,
        on_status=on_status,
        on_error=on_error,
    )
    stream.HEARTBEAT_INTERVAL_SECONDS = 3600.0
    stream.RECV_TIMEOUT_SECONDS = 0.01

    monkeypatch.setattr("src.market_data.websockets.connect", lambda *args, **kwargs: FakeConnect(ws))

    async def run() -> None:
        await stream.start()
        await asyncio.sleep(0.05)
        await stream.stop()

    asyncio.run(run())

    assert statuses[0] == ("public_books5", True)
    assert ("public_books5", False) in statuses
    assert errors
    assert "No data received from public_books5" in errors[0][1]


def test_okx_private_stream_emits_activity_on_pong_and_account(monkeypatch):
    ws = FakeWebSocket(
        [
            json.dumps({"event": "login", "code": "0"}),
            json.dumps({"event": "subscribe", "arg": {"channel": "account"}}),
            "pong",
            json.dumps(
                {
                    "arg": {"channel": "account"},
                    "data": [
                        {
                            "details": [
                                {
                                    "ccy": "USDC",
                                    "cashBal": "10",
                                    "availBal": "10",
                                }
                            ]
                        }
                    ],
                }
            ),
        ]
    )
    activities: list[tuple[str, str]] = []
    accounts: list[dict] = []

    async def on_activity(stream_name: str, activity: str) -> None:
        activities.append((stream_name, activity))

    stream: PrivateUserStream | None = None

    async def on_account(payload: dict) -> None:
        accounts.append(payload)
        assert stream is not None
        stream.running = False

    signer = OKXSigner(api_key="k", secret_key="s", passphrase="p")
    stream = PrivateUserStream(
        url="wss://example.invalid/ws/private",
        signer=signer,
        time_offset_ms=0,
        inst_type="SPOT",
        on_order=_noop_order,
        on_account=on_account,
        on_activity=on_activity,
    )
    stream.HEARTBEAT_INTERVAL_SECONDS = 3600.0
    stream.RECV_TIMEOUT_SECONDS = 0.1

    monkeypatch.setattr("src.private_stream.websockets.connect", lambda *args, **kwargs: FakeConnect(ws))

    async def run() -> None:
        await stream.start()
        await asyncio.sleep(0.05)
        await stream.stop()

    asyncio.run(run())

    assert accounts
    assert ("private_user", "pong") in activities
    assert ("private_user", "account") in activities
