"""唯一的判定规则：事实 → 四标签 → 置信度 → 必要复核。"""

from .contracts import Analysis, Assessment, Citation, DomainResult, Material, Review
from .investigation import merge_facts, validate_citation, validate_evidence

CORE = ("third_party", "model_access", "upstream_proxy")
# 提示词引用这里的标准；真正的分支判定在 assess，改标准时在本文件同步修改。
CRITERIA = {
    "确认": "当前主机名的第三方身份、用户模型访问及上游代理关系均有证据，无影响结论的未解决问题",
    "疑似": "存在具体中转线索，但确认依据不完整或存在未解决问题",
    "排除": "有当前主机名的明确非目标证据，无影响结论的未解决问题",
    "证据不足": "已经实际调查，仍无足够方向依据；访问失败不等于排除",
}
FACT_CRITERIA = {
    "third_party": "服务经营者与所接入模型的原厂不同；自身品牌及接入其他厂商模型的说明可支持。不能把所有网站的官方页面都理解成原厂模型服务。",
    "model_access": "当前主机面向用户提供模型调用入口、API Key、调用文档或模型聊天入口；纯介绍、安装文档不算已提供服务。",
    "upstream_proxy": "明确说明统一接入、转发、路由或代理其他厂商的模型服务。公开的服务说明和调用文档可以支持；仅列模型名、兼容协议或出售自托管开源模型算力不够。",
    "relay_clue": "有具体中转线索，但尚不足以支持全部确认条件。第三方目录的收录属于线索，不证明当前仍在经营。",
    "exclusion": "材料明确体现当前主机为模型原厂服务、纯文档、开源软件、桌面客户端、资讯或导航等非目标用途，且未体现其自身提供中转服务。",
}
GRADES = {"provisional": 0.55, "limited": 0.75, "sufficient": 0.90}
# 工程分档表示所选标签的依据强弱，不是经统计校准的目标概率。
CONFIDENCE_METHOD = "label_evidence_grade_v1"


def assess(materials: list[Material], analysis: Analysis, review: Review | None = None) -> Assessment:
    """将已核验引用的材料和候选事实转成标签、分档、依据及复核需求。

    review 仅提供事实修订和已解决疑点；这个函数不读写文件、不调用模型，
    也不因传入 review 就批准导出。apply_review 另行验证复核处理是否合法。
    """
    if not materials:
        raise ValueError("no investigation materials")
    facts = merge_facts(materials, analysis, review.fact_revisions if review else [])
    by_id = {m.material_id: m for m in materials}

    def support(name, *, current=False, direct=False):
        """找出支持指定事实的材料；current 同时要求当前有效且主体明确。"""
        return [by_id[e.material_id] for e in facts[name].evidence
                if e.value == "supported" and by_id[e.material_id].access_status == "ok"
                and (not current or (by_id[e.material_id].evidence_state == "current"
                                     and by_id[e.material_id].subject_relation == "exact"))
                and (not direct or by_id[e.material_id].source_kind == "direct")]

    # 先判断是否存在足以阻止确认/排除的冲突和疑点。
    clue = bool(support("relay_clue") or support("upstream_proxy"))
    excluded = bool(support("exclusion"))
    relevant = (*CORE, "relay_clue", "exclusion") if clue else ("exclusion",)
    # 明确非目标且无中转线索时，上游信息未知不影响排除，不强制补齐三项事实。
    conflicts = [f"事实冲突：{name}" for name in relevant if facts[name].conflict]
    if clue and excluded:
        conflicts.append("中转线索与排除依据并存")
    resolved = set(review.resolved_concerns) if review else set()
    concerns = [c for index, c in enumerate(analysis.concerns, 1) if index not in resolved]
    uncertain = any(m.subject_relation == "uncertain" for name in relevant for m in support(name))
    review_reasons = conflicts + [c.reason for c in concerns]
    if uncertain:
        review_reasons.append("材料与当前主机名关联不明确")
    blocked = bool(conflicts or uncertain or concerns)

    # 四标签只在这里产生，Agent 和人工复核均不能绕过这些条件。
    if blocked:
        label = "疑似" if clue else "证据不足"
    elif all(facts[name].value == "supported" and support(name, current=True) for name in CORE):
        label = "确认"
    elif facts["exclusion"].value == "supported" and support("exclusion", current=True):
        label = "排除"
    else:
        label = "疑似" if clue else "证据不足"

    if label == "确认":
        basis_names = list(CORE)
        explanation = "当前材料支持第三方身份、用户模型访问及上游代理/聚合/转发关系。"
    elif label == "排除":
        basis_names = ["exclusion"]
        explanation = "当前材料明确支持非目标判断。"
    elif label == "疑似":
        basis_names = ["relay_clue"] if support("relay_clue") else ["upstream_proxy"]
        explanation = "存在具体中转线索，确认依据尚不完整或有待核实。"
        missing = [name for name in CORE if facts[name].value != "supported"]
        if missing:
            # 缺少确认条件本身可由“疑似”表达，不因此让所有疑似都人工审核。
            explanation += " 确认条件缺失：" + ", ".join(missing) + "。"
    else:
        basis_names = ["exclusion"] if excluded else []
        explanation = "已实际调查，材料不足以支持明确方向；访问失败不表示排除。"

    # 同时记录真正支撑所选标签的材料，供导出计算 last_verified。
    basis_ids = set()
    if basis_names:
        limited = any(not support(name, current=True, direct=True) for name in basis_names)
        for name in basis_names:
            current = support(name, current=True)
            basis_ids.update(m.material_id for m in (current or support(name)))
            if not current:
                review_reasons.append("方向依据只有历史或时效未知材料，当前状态待核验")
    else:
        basis_ids.update(c.material_id for c in analysis.citations)
        cited = [by_id[mid] for mid in basis_ids]
        limited = not any(m.access_status != "ok" or
                          (m.source_kind == "direct" and m.evidence_state == "current") for m in cited)
    if blocked:
        # 保守标签也要保留反证和未解决疑点的来源，不能只留下支持中转的证据。
        basis_ids.update(e.material_id for name in relevant for e in facts[name].evidence if e.value != "unknown")
        basis_ids.update(c.material_id for concern in concerns for c in concern.citations)

    provisional = bool(conflicts or uncertain or any(c.kind in {"business", "subject", "conflict"} for c in concerns))
    grade = "provisional" if provisional else "limited" if limited else "sufficient"
    reasons = list(dict.fromkeys(review_reasons))
    if reasons:
        explanation += " 待核实：" + "；".join(reasons) + "。"
    if analysis.limitations:
        explanation += " 限制：" + "；".join(analysis.limitations) + "。"
    explanation += " 基于公开材料，未实测后台转发。"
    # 保存本次判定实际采用的事实。人工修订后也从这里生成，导出无需再推断一遍。
    return Assessment(label=label, confidence=GRADES[grade], confidence_grade=grade,
                      reason=explanation, facts=list(facts.values()), basis_material_ids=sorted(basis_ids),
                      review_reasons=reasons, review_status="pending" if reasons else "not_required")


def apply_review(result: DomainResult, review: Review) -> Assessment:
    """核验复核引用、事实修订及疑点处理，返回重判后可导出的 Assessment。

    原始分析保持不变。accept 允许接受已有依据的保守结论；revise 必须经规则
    重算得到请求的标签。引用错误不能批准，未解决事实冲突不能升级为确认。
    """
    if not result.investigation or not result.assessment or result.error:
        raise ValueError("review target has no completed analysis")
    if review.domain != result.candidate.domain:
        raise ValueError("review targets a different domain")
    # 模型未提出疑点也可能误判；复核者可以主动纠正 not_required 的结果。
    # 每条结果只记录一次复核；已有复核需补证或再修订时建立新批次。
    if result.review or result.assessment.review_status == "completed":
        raise ValueError("result already reviewed; use a new batch for further changes")
    if review.reviewed_at < result.investigation.created_at:
        raise ValueError("review predates the analysis")
    validate_evidence(result.candidate.domain, result.materials, result.investigation.analysis)
    materials = {m.material_id: m for m in result.materials}
    for citation in review.citations:
        validate_citation(citation, materials)
    for revision in review.fact_revisions:
        citation = Citation(material_id=revision.material_id, quote=revision.quote)
        if validate_citation(citation, materials).access_status != "ok":
            raise ValueError("review cannot infer business facts from access failure")
        if citation not in review.citations:
            raise ValueError("fact revision must be covered by review citations")
    concerns = result.investigation.analysis.concerns
    if any(index > len(concerns) for index in review.resolved_concerns):
        raise ValueError("resolved concern number does not exist")
    for index in review.resolved_concerns:
        if not {c.material_id for c in concerns[index - 1].citations} & {c.material_id for c in review.citations}:
            raise ValueError("resolving a concern requires citing its source material")

    # 复核仍回到同一个判定函数：已处理的是人工疑问，规则条件并没有被跳过。
    updated = assess(result.materials, result.investigation.analysis, review)
    target = result.assessment.label if review.action == "accept" else review.new_label
    if updated.label != target:
        raise ValueError("requested label is not supported by the reviewed facts")
    # 此时仍可有已明确记录的不确定性；允许以疑似/证据不足结束，避免无限复核。
    updated.review_status = "completed"
    updated.reason += " 复核记录（" + review.reviewer + "）：" + review.reason
    return updated
