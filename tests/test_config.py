from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal

from src.config import load_config


def test_simulated_mode_switches_default_ws_urls(tmp_path):
    secret_path = tmp_path / "secret.yaml"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  api_key: demo_key
  secret_key: demo_secret
  passphrase: demo_pass
""".strip(),
        encoding="utf-8",
    )
    secret_path.write_text(
        """
exchange:
  simulated: true
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.exchange.public_ws_url == "wss://wspap.okx.com:8443/ws/v5/public"
    assert config.exchange.private_ws_url == "wss://wspap.okx.com:8443/ws/v5/private"


def test_telemetry_paths_resolve_from_config_scope(tmp_path, monkeypatch):
    project_root = tmp_path / "trend_bot_6"
    config_dir = project_root / "config"
    data_dir = project_root / "data"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        """
mode: shadow
telemetry:
  journal_path: data/journal.jsonl
  sqlite_path: trend_bot_6/data/audit.db
  state_path: data/state_snapshot.json
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(config_path, validate_live_credentials=False)

    assert Path(config.telemetry.journal_path) == data_dir / "journal.shadow.jsonl"
    assert Path(config.telemetry.sqlite_path) == project_root.parent / "trend_bot_6" / "data" / "audit.shadow.db"
    assert Path(config.telemetry.state_path) == data_dir / "state_snapshot.shadow.json"


def test_live_mode_telemetry_paths_get_live_suffix(tmp_path, monkeypatch):
    project_root = tmp_path / "trend_bot_6"
    config_dir = project_root / "config"
    data_dir = project_root / "data"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  simulated: false
  api_key: live_key
  secret_key: live_secret
  passphrase: live_pass
telemetry:
  journal_path: data/journal.jsonl
  sqlite_path: trend_bot_6/data/audit.db
  state_path: data/state_snapshot.json
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(config_path)

    assert Path(config.telemetry.journal_path) == data_dir / "journal.live.jsonl"
    assert Path(config.telemetry.sqlite_path) == project_root.parent / "trend_bot_6" / "data" / "audit.live.db"
    assert Path(config.telemetry.state_path) == data_dir / "state_snapshot.live.json"


def test_live_paths_prefer_workspace_root_over_nested_cwd_and_apply_live_suffix_to_shared_state_paths(tmp_path, monkeypatch):
    project_root = tmp_path / "trend_bot_6"
    config_dir = project_root / "config"
    data_dir = project_root / "data" / "binance_usd1usdt"
    nested_dir = project_root / "trend_bot_6" / "data" / "binance_usd1usdt"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    nested_dir.mkdir(parents=True)
    (nested_dir / "state_snapshot.json").write_text("{}", encoding="utf-8")
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  name: binance
  simulated: false
  api_key: live_key
  secret_key: live_secret
telemetry:
  journal_path: trend_bot_6/data/journal.jsonl
strategy:
  release_only_shared_state_paths:
    - trend_bot_6/data/binance_usd1usdt/state_snapshot.json
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.chdir(project_root)
    config = load_config(config_path)

    assert Path(config.telemetry.journal_path) == project_root / "data" / "journal.live.jsonl"
    assert config.strategy.release_only_shared_state_paths == [
        str(project_root / "data" / "binance_usd1usdt" / "state_snapshot.live.json")
    ]


def test_explicit_environment_suffix_is_preserved(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  simulated: false
  api_key: live_key
  secret_key: live_secret
  passphrase: live_pass
telemetry:
  journal_path: journal.live.jsonl
  sqlite_path: audit.live.db
  state_path: state_snapshot.live.json
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert Path(config.telemetry.journal_path).name == "journal.live.jsonl"
    assert Path(config.telemetry.sqlite_path).name == "audit.live.db"
    assert Path(config.telemetry.state_path).name == "state_snapshot.live.json"


def test_load_config_reads_exchange_secrets_from_secret_yaml(tmp_path):
    project_root = tmp_path / "trend_bot_6"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    secret_path = config_dir / "secret.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  api_key: ""
  secret_key: ""
  passphrase: ""
""".strip(),
        encoding="utf-8",
    )
    secret_path.write_text(
        """
exchange:
  api_key: secret_key_from_file
  secret_key: secret_secret_from_file
  passphrase: secret_pass_from_file
  simulated: true
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.exchange.api_key == "secret_key_from_file"
    assert config.exchange.secret_key == "secret_secret_from_file"
    assert config.exchange.passphrase == "secret_pass_from_file"
    assert config.exchange.simulated is True


def test_load_config_reads_binance_secrets_from_secret_binance_yaml(tmp_path):
    project_root = tmp_path / "trend_bot_6"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    secret_path = config_dir / "secret.binance.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  name: binance
  binance_env: testnet
  api_key: ""
  secret_key: ""
""".strip(),
        encoding="utf-8",
    )
    secret_path.write_text(
        """
exchange:
  api_key: binance_key_from_file
  secret_key: binance_secret_from_file
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.exchange.name == "binance"
    assert config.exchange.api_key == "binance_key_from_file"
    assert config.exchange.secret_key == "binance_secret_from_file"


def test_load_config_falls_back_to_environment_when_secret_file_missing(tmp_path, monkeypatch):
    project_root = tmp_path / "trend_bot_6"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  simulated: true
  api_key: ""
  secret_key: ""
  passphrase: ""
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("OKX_API_KEY", "env_key")
    monkeypatch.setenv("OKX_SECRET_KEY", "env_secret")
    monkeypatch.setenv("OKX_PASSPHRASE", "env_pass")

    config = load_config(config_path)

    assert config.exchange.api_key == "env_key"
    assert config.exchange.secret_key == "env_secret"
    assert config.exchange.passphrase == "env_pass"


def test_load_config_reads_instance_budget_fields(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  simulated: true
  api_key: demo_key
  secret_key: demo_secret
  passphrase: demo_pass
trading:
  budget_base_total: 12000
  budget_quote_total: 8000
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.trading.budget_base_total == Decimal("12000")
    assert config.trading.budget_quote_total == Decimal("8000")


def test_load_config_reads_sell_drought_guard_fields(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  simulated: true
  api_key: demo_key
  secret_key: demo_secret
  passphrase: demo_pass
strategy:
  sell_drought_guard_enabled: true
  sell_drought_inventory_ratio_pct: 0.58
  sell_drought_rebalance_window_seconds: 900
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.strategy.sell_drought_guard_enabled is True
    assert config.strategy.sell_drought_inventory_ratio_pct == Decimal("0.58")
    assert config.strategy.sell_drought_rebalance_window_seconds == 900


def test_load_config_reads_top_level_managed_prefix(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
managed_prefix: binusdc
exchange:
  name: binance
  api_key: demo_key
  secret_key: demo_secret
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.managed_prefix == "binusdc"


def test_binance_testnet_runtime_defaults_override_urls(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  name: binance
  binance_env: testnet
  api_key: demo_key
  secret_key: demo_secret
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.exchange.name == "binance"
    assert config.exchange.rest_url == "https://testnet.binance.vision"
    assert config.exchange.public_ws_url == "wss://stream.testnet.binance.vision/ws"
    assert config.exchange.private_ws_url == "wss://ws-api.testnet.binance.vision/ws-api/v3"


def test_binance_mainnet_runtime_defaults_override_urls(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
exchange:
  name: binance
  binance_env: mainnet
  api_key: demo_key
  secret_key: demo_secret
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.exchange.name == "binance"
    assert config.exchange.rest_url == "https://api.binance.com"
    assert config.exchange.public_ws_url == "wss://stream.binance.com:9443/ws"
    assert config.exchange.private_ws_url == "wss://ws-api.binance.com:443/ws-api/v3"
