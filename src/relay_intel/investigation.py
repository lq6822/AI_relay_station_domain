"""Read-only material tools and quote/subject validation before fact merging."""

import re

from .candidates import normalize
from .contracts import (
    FACTS, Analysis, Citation, FactEvidence, Material, MergedFact, ToolArgs,
)


def validate_citation(citation: Citation, materials: dict[str, Material]):
    material = materials.get(citation.material_id)
    if not material:
        raise ValueError(f"unknown material reference: {citation.material_id}")
    if citation.quote not in (material.excerpt or material.failure_reason or ""):
        raise ValueError(f"quote cannot be located: {citation.material_id}")
    return material


def links_in(material: Material):
    if material.access_status != "ok":
        return []
    links = []
    for match in re.finditer(r"https?://[^\s<>\"'\[\]（）]+", material.excerpt):
        url = match.group().rstrip(".,;:!?，。；：！？)")
        links.append({"material_id": material.material_id, "url": url,
                      "context": material.excerpt[max(0, match.start()-100):match.end()+100]})
    return links


class MaterialTools:
    def __init__(self, domain: str, materials: list[Material]):
        self.domain = normalize(domain)
        self.materials = {m.material_id: m for m in materials}
        if any(normalize(m.domain) != self.domain for m in materials):
            raise ValueError("material subject does not match candidate hostname")
        for material in materials:
            if normalize(material.source_url) != self.domain and material.subject_relation == "exact":
                text = material.excerpt or material.failure_reason or ""
                if not re.search(r"(?<![\w.-])" + re.escape(self.domain) + r"(?![\w.-])", text, re.IGNORECASE):
                    raise ValueError("cross-host material has no locatable exact subject; mark relation uncertain")
        self.read_ids = set()
        self.link_ids = set()

    def execute(self, name: str, raw_args: dict):
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
        if self.read_ids != set(self.materials):
            raise ValueError("read all supplied materials before concluding, including counterevidence")
        for fact in analysis.facts:
            m = validate_citation(Citation(material_id=fact.material_id, quote=fact.quote), self.materials)
            if m.access_status != "ok":
                raise ValueError("access failure cannot support a business fact")
        for citation in analysis.quality_citations:
            validate_citation(citation, self.materials)
        for concern in analysis.concerns:
            for citation in concern.citations:
                validate_citation(citation, self.materials)
        for link in analysis.related_links:
            m = self.materials.get(link.material_id)
            if not m or link.material_id not in self.link_ids:
                raise ValueError("link must come from related_links tool")
            if link.context not in (m.excerpt or "") or link.url not in link.context:
                raise ValueError("link context cannot be located")
            if link.url not in {item["url"] for item in links_in(m)}:
                raise ValueError("link URL is not an exact observed URL")


def merge_facts(materials: list[Material], analysis: Analysis) -> list[MergedFact]:
    evidence = []
    for material in materials:
        evidence.extend(FactEvidence(**a.model_dump(), material_id=material.material_id, origin="human")
                        for a in material.annotations)
    evidence.extend(FactEvidence(**a.model_dump(), origin="model") for a in analysis.facts)
    result = []
    for name in FACTS:
        rows = [e for e in evidence if e.fact == name]
        values = {e.value for e in rows if e.value != "unknown"}
        conflict = len(values) > 1
        result.append(MergedFact(fact=name, value=next(iter(values)) if len(values) == 1 else "unknown",
                                 conflict=conflict, evidence=rows))
    return result
