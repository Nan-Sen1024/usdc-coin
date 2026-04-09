from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.audit_summary import build_profitability_report
from src.audit_summary import _latest_run_id, _latest_run_with_fills, _since_ts_ms_for_window
from src.config import load_config
from src.utils import decimal_to_str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report realized profit density from audit telemetry")
    parser.add_argument("--config", required=True, help="bot config path")
    parser.add_argument(
        "--view",
        choices=["window", "latest-run", "latest-filled-run"],
        default="window",
        help="report scope",
    )
    parser.add_argument("--window-hours", type=int, default=24, help="window size for --view window")
    return parser.parse_args()


def _fmt_decimal(value) -> str:
    if value is None:
        return "na"
    return decimal_to_str(value)


def main() -> None:
    args = parse_args()
    config = load_config(args.config, validate_live_credentials=False)

    title = ""
    run_id = None
    since_ts_ms = None
    if args.view == "latest-run":
        title = "latest_run"
        run_id = _latest_run_id(config.telemetry.sqlite_path)
    elif args.view == "latest-filled-run":
        title = "latest_filled_run"
        latest_run = _latest_run_id(config.telemetry.sqlite_path)
        run_id = _latest_run_with_fills(config.telemetry.sqlite_path, exclude_run_id=latest_run)
    else:
        title = f"window_{args.window_hours}h"
        since_ts_ms = _since_ts_ms_for_window(config.telemetry.sqlite_path, window_hours=args.window_hours)

    report = build_profitability_report(
        config,
        title=title,
        run_id=run_id,
        since_ts_ms=since_ts_ms,
    )

    print("Profit Density Report")
    print(f"view={args.view}")
    print(f"config={args.config}")
    print(f"sqlite_path={config.telemetry.sqlite_path}")
    print(f"journal_path={config.telemetry.journal_path}")
    print(f"state_path={config.telemetry.state_path}")
    print(f"run_id={report.run_id or '-'}")
    print(f"fill_count={report.fill_count}")
    print(f"turnover_quote={_fmt_decimal(report.turnover_quote)}")
    print(f"realized_pnl_quote={_fmt_decimal(report.realized_pnl_quote)}")
    print(f"realized_per_10k_turnover={_fmt_decimal(report.realized_per_10k_turnover)}")
    print(f"cancel_after_terminal_rate={_fmt_decimal(report.cancel_after_terminal_rate)}")
    print(f"same_price_amend_rate={_fmt_decimal(report.same_price_amend_rate)}")
    print(f"largest_turnover_action_class={report.largest_turnover_action_class or '-'}")
    print(f"worst_realized_action_class={report.worst_realized_action_class or '-'}")
    print("action_classes:")
    if not report.action_summaries:
        print("- none")
        return
    for item in report.action_summaries:
        print(
            "- "
            f"{item.action_class}: "
            f"fills={item.fill_count} "
            f"turnover={_fmt_decimal(item.turnover_quote)} "
            f"realized={_fmt_decimal(item.realized_pnl_quote)} "
            f"per10k={_fmt_decimal(item.realized_per_10k_turnover)}"
        )


if __name__ == "__main__":
    main()
