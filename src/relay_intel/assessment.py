"""Deterministic four-label rules and evidence grades; no probability claims."""

from .contracts import Assessment, Citation, Investigation, Review, now
from .investigation import merge_facts, validate_citation

CORE = ("third_party", "model_access", "upstream_proxy")


def assess(investigation: Investigation, policy, version: int) -> Assessment:
    facts = {f.fact: f for f in investigation.facts}
    materials = {m.material_id: m for m in investigation.materials}

    def support(name, *, current=False, direct=False):
        return [materials[e.material_id] for e in facts[name].evidence
                if e.value == "supported" and materials[e.material_id].access_status == "ok"
                and (not current or (materials[e.material_id].evidence_state == "current"
                                     and materials[e.material_id].subject_relation == "exact"))
                and (not direct or materials[e.material_id].source_kind == "direct")]

    clue = bool(support("relay_clue") or support("upstream_proxy"))
    excluded = bool(support("exclusion"))
    relevant = set(CORE) | {"relay_clue", "exclusion"} if clue else {"exclusion"}
    conflicts = [f"事实冲突：{name}" for name in relevant if facts[name].conflict]
    if clue and excluded:
        conflicts.append("中转线索与排除依据并存")
    concerns = [c for c in investigation.analysis.concerns if c.affects_decision]
    unresolved = conflicts + [c.reason for c in concerns]
    uncertain = any(m.subject_relation == "uncertain" for name in relevant for m in support(name))
    if uncertain:
        unresolved.append("影响结论的材料与当前主机名关联不明确")

    # Unresolved material conflicts and ambiguous business/subject cannot produce decisive labels.
    blocking = bool(conflicts or uncertain or any(c.kind in {"business", "subject", "conflict"}
                                                 for c in concerns))
    if blocking:
        label = "疑似" if clue else "证据不足"
    elif all(facts[name].value == "supported" and support(name, current=True) for name in CORE):
        label = "确认"
    elif facts["exclusion"].value == "supported" and support("exclusion", current=True):
        label = "排除"
    elif clue:
        label = "疑似"
    else:
        label = "证据不足"

    if label == "确认":
        basis_names = list(CORE)
        explanation = "当前主机名材料支持第三方身份、用户模型访问及上游代理/聚合/转发关系。"
    elif label == "排除":
        basis_names = ["exclusion"]
        explanation = "当前主机名材料明确支持非目标判断，未发现影响结论的反证。"
    elif label == "疑似":
        basis_names = ["relay_clue"] if support("relay_clue") else ["upstream_proxy"]
        explanation = "存在具体中转线索，但确认条件尚不完整或存在未解决问题。"
        missing = [name for name in CORE if facts[name].value != "supported"]
        if missing:
            unresolved.append("确认的关键事实缺失：" + ", ".join(missing))
    else:
        basis_names = ["exclusion"] if excluded else []
        explanation = "已实际调查，现有材料不足以支持当前明确方向；访问失败不表示排除。"

    if basis_names:
        limited = any(not support(name, current=True, direct=True) for name in basis_names)
        if any(not support(name, current=True) for name in basis_names):
            unresolved.append("方向依据仅有历史或时效未知材料，当前状态仍待核验")
    else:
        cited = [materials[c.material_id] for c in investigation.analysis.quality_citations]
        limited = not any(m.access_status != "ok" or
                          (m.source_kind == "direct" and m.evidence_state == "current") for m in cited)

    grade = "provisional" if blocking else "limited" if limited else "sufficient"
    if not materials:
        grade = "undetermined"
    reasons = list(dict.fromkeys(unresolved))
    limitations = list(investigation.analysis.limitations)
    if reasons:
        explanation += " 未解决：" + "；".join(reasons) + "。"
    if limitations:
        explanation += " 限制：" + "；".join(limitations) + "。"
    explanation += " 依据公开材料解释，未实测后台转发。"
    return Assessment(run_id=investigation.run_id, domain=investigation.domain, version=version,
                      investigation_version=investigation.version, fingerprint=investigation.fingerprint,
                      label=label, confidence=policy.grades.get(grade), confidence_grade=grade,
                      confidence_method=policy.confidence_method, confidence_basis=policy.confidence_basis,
                      reason=explanation, review_required=bool(reasons), review_reasons=reasons,
                      review_status="pending" if reasons else "not_required", created_at=now())


def revised_investigation(investigation: Investigation, review: Review):
    materials = {m.material_id: m for m in investigation.materials}
    for citation in review.citations:
        validate_citation(citation, materials)
    for revision in review.fact_revisions:
        m = validate_citation(Citation(material_id=revision.material_id, quote=revision.quote), materials)
        if m.access_status != "ok":
            raise ValueError("review cannot infer business facts from an access failure")
        if not any(c.material_id == revision.material_id and c.quote == revision.quote
                   for c in review.citations):
            raise ValueError("fact revisions must be covered by review citations")
    replaced = {f.fact for f in review.fact_revisions}
    suggestions = [f for f in investigation.analysis.facts if f.fact not in replaced]
    # Explicitly supersede model interpretation only; original human annotations remain.
    analysis = investigation.analysis.model_copy(update={"facts": suggestions + review.fact_revisions})
    revised = investigation.model_copy(update={"analysis": analysis,
                                               "facts": merge_facts(investigation.materials, analysis)})
    for fact in revised.facts:
        for evidence in fact.evidence:
            if evidence.fact in replaced and evidence.origin == "model":
                evidence.origin = "human"
    return revised


def apply_review(current: Assessment, investigation: Investigation, review: Review, policy):
    if (review.run_id, review.domain) != (current.run_id, current.domain):
        raise ValueError("review targets a different batch or domain")
    if current.investigation_version != investigation.version or current.fingerprint != investigation.fingerprint:
        raise ValueError("review targets a stale investigation")
    if current.review == review:
        return current
    if review.assessment_version != current.version:
        raise ValueError("review must target the current assessment version")
    if current.review_status != "pending":
        raise ValueError("only pending assessments require review")
    if review.reviewed_at < current.created_at:
        raise ValueError("review predates the current assessment")
    revised = revised_investigation(investigation, review)
    result = assess(revised, policy, current.version + 1)
    target = current.label if review.action == "accept" else review.new_label
    if target != result.label:
        raise ValueError("requested label is not supported by the validated facts")
    if result.confidence is None:
        raise ValueError("review cannot publish an uninvestigated draft")
    result.review_required = True
    result.review_status = "completed"
    result.review = review
    result.reason += " 人工复核：" + review.reason
    return result
