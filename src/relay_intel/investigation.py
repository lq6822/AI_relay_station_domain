"""材料归属、两个只读工具、引用校验和事实合并。"""

import re

from .candidates import normalize, unique_records
from .contracts import (
    FACTS, Analysis, Candidate, Citation, FactEvidence, FactSuggestion, Issue,
    Material, MergedFact, ToolArgs,
)


def validate_subject(domain: str, material: Material):
    """检查材料属于当前主机；跨站 exact 材料必须逐字出现目标域名。"""
    if normalize(material.domain) != domain:
        raise ValueError("material subject does not match candidate hostname")
    if normalize(material.source_url) != domain and material.subject_relation == "exact":
        text = material.excerpt or material.failure_reason or ""
        if not re.search(r"(?<![\w.-])" + re.escape(domain) + r"(?![\w-]|\.[\w.-])", text, re.IGNORECASE):
            raise ValueError("cross-host material has no locatable exact subject; mark relation uncertain")


def prepare_materials(materials: list[Material], candidates: list[Candidate]):
    """按候选域名分组材料，保留明确的拒绝原因；冲突 ID 要求先修正输入。"""
    grouped = {candidate.domain: [] for candidate in candidates}
    issues = []
    for material in unique_records(materials, "material_id"):
        try:
            domain = normalize(material.domain)
            if domain not in grouped:
                raise ValueError("material does not belong to a candidate")
            material = material.model_copy(update={"domain": domain})
            validate_subject(domain, material)
            grouped[domain].append(material)
        except ValueError as exc:
            issues.append(Issue(location=material.material_id, reason=str(exc)))
    return grouped, issues


def validate_citation(citation: Citation, materials: dict[str, Material]) -> Material:
    """返回被引用材料；材料不存在或原文找不到时拒绝，不验证语义真假。"""
    material = materials.get(citation.material_id)
    if not material:
        raise ValueError(f"unknown material reference: {citation.material_id}")
    if citation.quote not in (material.excerpt or material.failure_reason or ""):
        raise ValueError(f"quote cannot be located: {citation.material_id}")
    return material


def links_in(material: Material) -> list[dict]:
    """从成功采集的原文抽取真实 URL 和附近上下文，不猜测域名或发起网络访问。"""
    if material.access_status != "ok":
        return []
    links = []
    for match in re.finditer(r"https?://[^\s<>\"'\[\]（）]+", material.excerpt):
        url = match.group().rstrip(".,;:!?，。；：！？)")
        links.append({"material_id": material.material_id, "url": url,
                      "context": material.excerpt[max(0, match.start()-100):match.end()+100]})
    return links


class MaterialTools:
    """本域材料的只读视图，并记录模型实际读取过哪些材料与关联链接。"""
    def __init__(self, domain: str, materials: list[Material]):
        """检查材料主体，建立本域索引；其他域名的数据不会进入工具实例。"""
        self.domain = normalize(domain)
        self.materials = {m.material_id: m for m in materials}
        for material in materials:
            validate_subject(self.domain, material)
        self.read_ids = set()
        self.link_ids = set()

    def execute(self, name: str, raw_args: dict):
        """执行白名单工具并返回内容与材料 ID；拒绝越权域名、工具和材料 ID。"""
        if name not in {"read_materials", "related_links"}:
            raise ValueError("tool is not allowed")
        args = ToolArgs.model_validate(raw_args)
        if normalize(args.domain) != self.domain:
            raise ValueError("tool cannot read a different domain")
        ids = args.material_ids or list(self.materials)
        if any(mid not in self.materials for mid in ids):
            raise ValueError("tool references material outside this domain")
        if name == "read_materials":
            self.read_ids.update(ids)
            return [self.materials[mid].model_dump(mode="json") for mid in ids], ids
        self.link_ids.update(ids)
        return [link for mid in ids for link in links_in(self.materials[mid])], ids

    def validate_analysis(self, analysis: Analysis):
        """确认工具确实读取了全部材料，再校验最终输出引用及关联链接。"""
        if self.read_ids != set(self.materials):
            raise ValueError("read all materials before concluding, including counterevidence")
        if self.link_ids != set(self.materials):
            raise ValueError("inspect related_links for all materials before concluding")
        validate_evidence(self.domain, list(self.materials.values()), analysis)


def validate_evidence(domain: str, materials: list[Material], analysis: Analysis):
    """分析、复核和导出共用的证据检查；不宣称能自动验证语义真实性。"""
    by_id = {m.material_id: m for m in materials}
    for material in materials:
        validate_subject(domain, material)
    for fact in analysis.facts:
        material = validate_citation(Citation(material_id=fact.material_id, quote=fact.quote), by_id)
        if material.access_status != "ok":
            raise ValueError("access failure cannot support a business fact")
    for citation in analysis.citations:
        validate_citation(citation, by_id)
    for concern in analysis.concerns:
        for citation in concern.citations:
            validate_citation(citation, by_id)
    for link in analysis.related_links:
        material = by_id.get(link.material_id)
        if not material or link.context not in (material.excerpt or "") or link.url not in link.context:
            raise ValueError("link context cannot be located")
        if link.url not in {item["url"] for item in links_in(material)}:
            raise ValueError("link URL is not an exact observed URL")


def merge_facts(materials: list[Material], analysis: Analysis,
                revisions: list[FactSuggestion] = ()) -> dict[str, MergedFact]:
    """汇总人工标注、模型解释和复核解释，返回按事实名称索引的结果。

    复核仅替换指定事实的模型解释，人工原始标注保持不变；unknown 不算反证，
    supported 与 refuted 同时存在时标记冲突，不通过重复材料数量投票。"""
    evidence = [
        FactEvidence(**annotation.model_dump(), material_id=material.material_id, origin="human")
        for material in materials for annotation in material.annotations
    ]
    replaced = {revision.fact for revision in revisions}
    evidence.extend(FactEvidence(**fact.model_dump(), origin="model")
                    for fact in analysis.facts if fact.fact not in replaced)
    evidence.extend(FactEvidence(**fact.model_dump(), origin="review") for fact in revisions)
    facts = {}
    for name in FACTS:
        rows = [item for item in evidence if item.fact == name]
        values = {item.value for item in rows if item.value != "unknown"}
        facts[name] = MergedFact(fact=name, value=next(iter(values)) if len(values) == 1 else "unknown",
                                 conflict=len(values) > 1, evidence=rows)
    return facts
