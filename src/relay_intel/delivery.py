"""导出已完成的判定，并说明数量、未完成项和扩展情况。"""

from collections import Counter

from .assessment import CONFIDENCE_METHOD
from .contracts import Batch, DomainResult, Intelligence, IntelligenceEvidence
from .investigation import validate_evidence
from .workspace import Workspace


def is_synthetic(batch: Batch, domain: str) -> bool:
    """识别显式演示批次及保留示例域名；真实来源仍需人工核验，程序无法鉴真。"""
    reserved = ("example.com", "example.net", "example.org")
    return batch.synthetic or any(domain == name or domain.endswith("." + name) for name in reserved)


def to_intelligence(result: DomainResult) -> Intelligence:
    """检查单域完成状态、复核和引用，转换为七字段情报；未满足条件则拒绝。"""
    assessment = result.assessment
    if result.error or not result.investigation or not assessment:
        raise ValueError(result.error or "analysis has not completed")
    if assessment.review_status == "pending":
        raise ValueError("necessary review pending")
    if assessment.review_status == "completed" and not result.review:
        raise ValueError("completed review has no review record")
    validate_evidence(result.candidate.domain, result.materials, result.investigation.analysis)
    basis = [m for m in result.materials if m.material_id in assessment.basis_material_ids]
    if len({m.material_id for m in basis}) != len(set(assessment.basis_material_ids)):
        raise ValueError("assessment has no supporting material")
    analysis = result.investigation.analysis
    # 当前事实来自实际判定；人工修订已体现在其中，原始疑点与复核过程同时保留。
    evidence = IntelligenceEvidence(
        materials=result.materials, facts=assessment.facts,
        basis_material_ids=assessment.basis_material_ids,
        citations=analysis.citations, concerns=analysis.concerns, review=result.review,
    )
    # 验证时间只取支撑结论的材料，不能用导出时间或无关的新访问失败刷新旧结论。
    return Intelligence(
        domain=result.candidate.domain, label=assessment.label, confidence=assessment.confidence,
        evidence=evidence, reason=assessment.reason, discovery_source=result.candidate.sources,
        last_verified=max(m.collected_at for m in basis),
        confidence_method=CONFIDENCE_METHOD, review_status=assessment.review_status,
    )


def collect(batch: Batch) -> tuple[list[Intelligence], dict]:
    """收集可导出四标签记录，另列未完成原因；数量达标与整份作业完成分开。"""
    rows, unfinished = [], []
    for result in batch.results:
        try:
            rows.append(to_intelligence(result))
        except ValueError as exc:
            unfinished.append({"domain": result.candidate.domain, "reason": str(exc)})
    # 合成记录可以演示导出，但不能凑足题目要求的 50 个真实域名。
    formal = [row for row in rows if not is_synthetic(batch, row.domain)]
    formal_domains = {row.domain for row in formal}
    summary = {
        "run_id": batch.run_id,
        "candidate_count": len(batch.results),
        "exported_count": len(rows),
        "formal_result_count": len(formal),
        "formal_registered_domain_count": len({r.candidate.registered_domain for r in batch.results
                                               if r.candidate.domain in formal_domains}),
        "label_distribution": dict(Counter(row.label for row in formal)),
        "synthetic_domains": [row.domain for row in rows if is_synthetic(batch, row.domain)],
        "at_least_50_real_domains": len(formal) >= 50,
        "real_seed_expansion_attempt": any(e.seed_domain in formal_domains for e in batch.expansions),
        "unfinished": unfinished,
        "issues": batch.issues,
        "expansions": batch.expansions,
        "confidence_method": CONFIDENCE_METHOD,
        "confidence_basis": "engineering_default; not statistically calibrated",
    }
    return rows, summary


def export(workspace: Workspace, batch: Batch) -> dict:
    """写情报与统计并返回统计；写失败上报，重新执行时重写这组导出文件。"""
    rows, summary = collect(batch)
    folder = workspace.batch_dir(batch.run_id) / "exports"
    workspace.write(folder / "intelligence.json", rows)
    workspace.write(folder / "summary.json", summary)
    return summary
