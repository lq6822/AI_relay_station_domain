"""命令入口：参数解析 → 业务函数 → 打印结果。"""

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from . import agent, delivery, pipeline
from .workspace import Workspace, encode, validation_message


def parse_args(argv=None):
    """定义分析、继续、复核、扩展和导出命令；参数错误由 argparse 处理。"""
    parser = argparse.ArgumentParser(description="公开材料驱动的 AI 中转站域名情报原型")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "review", "expand", "export"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--run-id", required=True)
        if name == "run":
            command.add_argument("--leads", default="data/inputs/leads.jsonl")
            command.add_argument("--materials", default="data/inputs/materials.jsonl")
            command.add_argument("--synthetic", action="store_true")
        elif name == "review":
            command.add_argument("--actions", required=True)
    return parser.parse_args(argv)


async def dispatch(args) -> int:
    """将一个命令映射到一个业务流程，统一打印结果和返回命令状态。"""
    workspace = Workspace(args.root)
    if args.command in {"run", "resume"}:
        config = None
        if args.command == "run":
            config = workspace.read_ai_config()
            batch = pipeline.prepare_batch(
                workspace, args.run_id, config.to_policy(),
                leads_path=args.leads, materials_path=args.materials, synthetic=args.synthetic,
            )
        else:
            # 材料和模型设置沿用已保存批次，连接时再读取本地文件中的当前 Key。
            batch = workspace.load_batch(args.run_id)
        # 全部处理过的批次直接报告状态，无需模型凭证或额外调用。
        if any(result.needs_analysis for result in batch.results):
            config = config or workspace.read_ai_config()
            if config.to_policy() != batch.policy:
                raise ValueError("AI settings differ from the saved batch; restore settings or use a new run_id")
            async with agent.create_client(batch.policy, config.api_key) as client:
                batch = await pipeline.run_batch(workspace, batch, client)
        pending = [r.candidate.domain for r in batch.results
                   if r.assessment and r.assessment.review_status == "pending"]
        failed = [r.candidate.domain for r in batch.results if r.error]
        print(encode({"candidates": len(batch.results), "pending_review": pending,
                      "failed": failed, "input_issues": batch.issues}))
        return 2 if pending or failed or batch.issues else 0

    batch = workspace.load_batch(args.run_id)
    if args.command == "review":
        issues = pipeline.review_batch(workspace, batch, args.actions)
        print(encode({"review_errors": issues}))
        return 2 if issues else 0
    if args.command == "expand":
        leads = pipeline.expand_batch(workspace, batch)
        print(encode({"seeds_searched": len(batch.expansions), "new_leads": len(leads)}))
        return 0 if batch.expansions else 2
    summary = delivery.export(workspace, batch)
    print(encode({key: summary[key] for key in (
        "exported_count", "formal_result_count", "at_least_50_real_domains", "unfinished",
    )}))
    # export 的成功只表示文件写入完成；数量、未完成项由 summary 如实说明。
    return 0


def main(argv=None) -> int:
    """CLI 入口：解析参数、执行命令，向终端报告可处理的配置/输入/文件错误。"""
    args = parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except (ValueError, OSError) as exc:
        reason = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
        print(f"error: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
