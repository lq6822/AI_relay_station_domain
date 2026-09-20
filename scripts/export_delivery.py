"""汇总实际完成的批次，生成可独立查看和重新运行的面试交付数据。"""

import argparse
from collections import Counter

from relay_intel.contracts import now
from relay_intel.delivery import is_synthetic, to_intelligence
from relay_intel.workspace import Workspace


def select_results(batches):
    """按显式给出的批次顺序取每域最后一次结果；后一次失败不能退回旧的成功。"""
    selected, replaced = {}, []
    for batch in batches:
        if batch.issues:
            raise ValueError(f"resolve input/review issues before delivery: {batch.run_id}")
        for result in batch.results:
            domain = result.candidate.domain
            if is_synthetic(batch, domain):
                raise ValueError(f"synthetic data cannot enter delivery: {domain}")
            if domain in selected:
                previous, old = selected[domain]
                replaced.append(dict(domain=domain, previous_run=previous.run_id,
                                     selected_run=batch.run_id, previous_error=old.error))
            selected[domain] = (batch, result)
    # 在写任何文件前检查全部结果。未完成分析或必要复核会直接报错。
    records = [to_intelligence(selected[domain][1]) for domain in sorted(selected)]
    return selected, records, replaced


def main():
    """先完整检查，再导出情报、统计、固定输入和原批次；不重新调用 AI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", nargs="+", help="按先后顺序填写实际批次名；后批覆盖同域前批")
    parser.add_argument("--output", default="delivery")
    args = parser.parse_args()
    workspace = Workspace(".")
    batches = [workspace.load_batch(run_id) for run_id in args.run_ids]
    selected, records, replaced = select_results(batches)
    if len(records) < 50:
        raise ValueError("delivery requires at least 50 completed real domains")
    results = [selected[record.domain][1] for record in records]
    investigations = [r.investigation for b in batches for r in b.results if r.investigation]
    expansions = [e for b in batches for e in b.expansions]
    summary = dict(
        exported_at=now().isoformat(), domain_count=len(records),
        registered_domain_count=len({r.candidate.registered_domain for r in results}),
        label_distribution=dict(Counter(r.label for r in records)),
        review_distribution=dict(Counter(r.review_status for r in records)),
        source_runs=args.run_ids,
        domain_runs={domain: batch.run_id for domain, (batch, _) in sorted(selected.items())},
        superseded_records=replaced,
        material_count=sum(len(r.materials) for r in results),
        successful_materials=sum(m.access_status == "ok" for r in results for m in r.materials),
        seeds_searched=len(expansions),
        expansion_leads=sum(len(e.leads) for e in expansions),
        # 失败调用未返回完整用量；明确统计范围，不能冒充账号的全部消费。
        completed_analysis_usage=dict(
            analyses=len(investigations), requests=sum(i.requests for i in investigations),
            input_tokens=sum(i.usage.get("input_tokens", 0) for i in investigations),
            output_tokens=sum(i.usage.get("output_tokens", 0) for i in investigations),
        ),
        usage_scope="所列批次中成功完成的分析；不含失败调用和前期小样本验证",
        review_scope="reviewer 字段记录实际复核者；AI 复核不表示独立人工验收",
        limitations=["公开网页未实测后台转发", "置信度为证据分档，未经统计校准",
                     "本批为定向选样，不代表全网覆盖率"],
    )
    output = workspace.path(args.output)
    workspace.write(output / "intelligence.json", records)
    workspace.write(output / "summary.json", summary)
    workspace.write(output / "expansions.json", expansions)
    # 各批次可能复用局部 ID。供独立重跑的输入统一编号，原编号仍保留在情报和原批次中。
    leads = [s for r in results for s in r.candidate.sources]
    materials = [m for r in results for m in r.materials]
    workspace.write(output / "inputs/leads.jsonl",
                    [s.model_copy(update={"lead_id": f"delivery-lead-{i:04d}"})
                     for i, s in enumerate(leads, 1)], jsonl=True)
    workspace.write(output / "inputs/materials.jsonl",
                    [m.model_copy(update={"material_id": f"delivery-material-{i:04d}"})
                     for i, m in enumerate(materials, 1)], jsonl=True)
    for batch in batches:
        workspace.write(output / "batches" / (batch.run_id + ".json"), batch)
    print(f"Exported {len(records)} domains to {output}")


if __name__ == "__main__":
    main()
