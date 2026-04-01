import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.audit_summary import render_audit_summary
from src.bot import TrendBot6
from src.config import load_config
from src.evolution_cycle import (
    default_evolution_spec_path,
    default_evolution_state_dir,
    render_cycle_result,
    render_evolution_status,
    run_evolution_cycle,
)
from src.evolution_dispatch import (
    build_dispatch_plan,
    execute_dispatch_plan,
    render_dispatch_plan,
    render_dispatch_result,
)
from src.evolution_evaluator import (
    generate_evaluation_report,
    render_evaluation_generation_result,
)
from src.evolution_runner import (
    apply_evaluation_report,
    render_candidate_bundle,
    render_evaluation_apply_result,
    seed_active_candidate_bundle,
)
from src.evolution_proposer import (
    propose_next_candidate,
    render_candidate_proposal,
)
from src.market_observer import render_market_observer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trend Bot 6 - OKX USDC-USDT 做市机器人")
    default_config = Path(__file__).resolve().parent / "config" / "config.yaml"
    parser.add_argument("--config", type=str, default=str(default_config), help="配置文件路径")
    parser.add_argument("--mode", choices=["shadow", "live"], default=None, help="运行模式覆盖")
    parser.add_argument("--summary", action="store_true", help="输出中文运行摘要并退出")
    parser.add_argument("--run-id", type=str, default=None, help="配合 --summary 查看指定 run_id")
    parser.add_argument("--observe-markets", action="store_true", help="输出多交易对 fee/depth/spread 观测")
    parser.add_argument("--observe-inst-id", action="append", default=None, help="指定观测的交易对，可多次传入")
    parser.add_argument("--observe-quote-size", type=str, default=None, help="观测时的参考挂单金额(quote)")
    parser.add_argument("--evolution-status", action="store_true", help="输出自治优化控制面状态并退出")
    parser.add_argument("--evolution-cycle", action="store_true", help="执行一轮自治优化控制面判断并退出")
    parser.add_argument("--evolution-dispatch-plan", action="store_true", help="输出下一轮自治优化 worker 的派发计划")
    parser.add_argument("--evolution-dispatch", action="store_true", help="按当前控制面状态启动一轮自治优化 worker")
    parser.add_argument("--evolution-propose-candidate", action="store_true", help="根据当前控制状态在 spec 中激活或生成下一个 bounded candidate")
    parser.add_argument("--evolution-seed-candidate", action="store_true", help="为当前 active candidate 创建落盘 bundle")
    parser.add_argument("--evolution-generate-evaluation-report", type=str, default=None, help="根据 evaluation request 生成标准评估报告")
    parser.add_argument("--evolution-apply-evaluation-report", type=str, default=None, help="导入一份评估报告并推进 promote/reject/rollback")
    parser.add_argument("--evolution-spec", type=str, default=None, help="自治优化规范文件路径")
    parser.add_argument("--evolution-state-dir", type=str, default=None, help="自治优化状态目录")
    parser.add_argument("--evolution-codex-bin", type=str, default="codex", help="自治派发时使用的 codex 可执行文件")
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main() -> None:
    args = parse_args()
    setup_logging()
    evolution_mode = (
        args.evolution_status
        or args.evolution_cycle
        or args.evolution_dispatch_plan
        or args.evolution_dispatch
        or args.evolution_propose_candidate
        or args.evolution_seed_candidate
        or bool(args.evolution_generate_evaluation_report)
        or bool(args.evolution_apply_evaluation_report)
    )
    config = load_config(
        args.config,
        mode_override=args.mode,
        validate_live_credentials=not (args.summary or evolution_mode),
    )
    spec_path = args.evolution_spec or str(default_evolution_spec_path(args.config))
    state_dir = args.evolution_state_dir or str(default_evolution_state_dir(args.config))
    if args.summary:
        print(render_audit_summary(config, run_id=args.run_id))
        return
    if args.evolution_status:
        print(
            render_evolution_status(
                config=config,
                spec_path=spec_path,
                state_dir=state_dir,
            )
        )
        return
    if args.evolution_cycle:
        print(
            render_cycle_result(
                run_evolution_cycle(
                    config=config,
                    spec_path=spec_path,
                    state_dir=state_dir,
                )
            )
        )
        return
    if args.evolution_dispatch_plan:
        print(
            render_dispatch_plan(
                build_dispatch_plan(
                    config_path=args.config,
                    spec_path=spec_path,
                    state_dir=state_dir,
                    codex_bin=args.evolution_codex_bin,
                )
            )
        )
        return
    if args.evolution_dispatch:
        plan = build_dispatch_plan(
            config_path=args.config,
            spec_path=spec_path,
            state_dir=state_dir,
            codex_bin=args.evolution_codex_bin,
        )
        print(
            render_dispatch_result(
                execute_dispatch_plan(
                    plan,
                    state_dir=state_dir,
                )
            )
        )
        return
    if args.evolution_propose_candidate:
        print(
            render_candidate_proposal(
                propose_next_candidate(
                    spec_path=spec_path,
                    state_dir=state_dir,
                )
            )
        )
        return
    if args.evolution_seed_candidate:
        print(
            render_candidate_bundle(
                seed_active_candidate_bundle(
                    spec_path=spec_path,
                    state_dir=state_dir,
                )
            )
        )
        return
    if args.evolution_generate_evaluation_report:
        print(
            render_evaluation_generation_result(
                generate_evaluation_report(
                    config=config,
                    spec_path=spec_path,
                    state_dir=state_dir,
                    request_path=args.evolution_generate_evaluation_report,
                )
            )
        )
        return
    if args.evolution_apply_evaluation_report:
        print(
            render_evaluation_apply_result(
                apply_evaluation_report(
                    spec_path=spec_path,
                    state_dir=state_dir,
                    report_path=args.evolution_apply_evaluation_report,
                )
            )
        )
        return
    if args.observe_markets:
        reference_quote_size = Decimal(args.observe_quote_size) if args.observe_quote_size else None
        print(
            await render_market_observer(
                config=config,
                inst_ids=args.observe_inst_id,
                reference_quote_size=reference_quote_size,
            )
        )
        return
    bot = TrendBot6(config)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
