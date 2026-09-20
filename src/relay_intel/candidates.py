"""域名规范化、来源合并，以及一轮关联候选扩展。"""

import ipaddress
import re
from urllib.parse import urlsplit
from uuid import uuid4

import idna
import tldextract

from .contracts import Candidate, DomainResult, Expansion, Issue, Lead, now

_suffixes = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                                fallback_to_snapshot=True, include_psl_private_domains=True)


def normalize(raw: str) -> str:
    """从主机名或 HTTP(S) URL 得到规范域名；处理 IDNA，拒绝 IP 和非法主机。"""
    value = raw.strip()
    if not value or any(c.isspace() for c in value) or any(c in value for c in "*\\@"):
        raise ValueError("invalid hostname characters or credentials")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only HTTP(S) URLs with a hostname are accepted")
        _ = parsed.port
        value = parsed.hostname
    elif any(c in value for c in "/?#:"):
        raise ValueError("use a hostname or a complete HTTP(S) URL")
    if value.endswith("."):
        value = value[:-1]
    try:
        host = idna.encode(value, uts46=True, std3_rules=True).decode("ascii").lower()
    except idna.IDNAError as exc:
        raise ValueError("invalid IDNA hostname") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not candidates")
    if len(host) > 253 or "." not in host or not re.search(r"[a-z]", host.rsplit(".", 1)[1]):
        raise ValueError("invalid public hostname")
    suffix = _suffixes(host)
    if not suffix.suffix or not suffix.domain:
        raise ValueError("hostname has no registered domain in the bundled suffix snapshot")
    return host


def unique_records(records: list, key: str) -> list:
    """同 ID 同内容合并；同 ID 不同内容直接报错，要求修正输入。"""
    index = {}
    for record in records:
        identity = getattr(record, key)
        if identity in index and index[identity] != record:
            raise ValueError(f"conflicting content for {key}: {identity}")
        index[identity] = record
    return list(index.values())


def prepare_candidates(leads: list[Lead]) -> tuple[list[Candidate], list[Issue]]:
    """规范化并按完整主机名分组，合并发现来源，返回候选和被拒绝的线索。"""
    by_domain, issues = {}, []
    for lead in unique_records(leads, "lead_id"):
        try:
            domain = normalize(lead.raw_value)
            if lead.seed_domain:
                lead = lead.model_copy(update={"seed_domain": normalize(lead.seed_domain)})
            by_domain.setdefault(domain, []).append(lead)
        except ValueError as exc:
            issues.append(Issue(location=lead.lead_id, reason=str(exc)))
    candidates = [
        Candidate(domain=domain, registered_domain=_suffixes(domain).top_domain_under_public_suffix, sources=sources)
        for domain, sources in sorted(by_domain.items())
    ]
    return candidates, issues


def expand_seed(result: DomainResult, known_domains: set[str]) -> Expansion:
    """只使用 Agent 已实际读取、校验过的链接；不复制种子标签。"""
    if not result.assessment or result.assessment.label != "确认" or result.assessment.review_status == "pending":
        raise ValueError("expansion requires a confirmed seed with necessary review completed")
    investigation = result.investigation
    if not investigation or result.error:
        raise ValueError("seed has no successful investigation")
    material_ids = {mid for call in investigation.tool_calls
                    if call.name == "related_links" and call.result == "ok" for mid in call.material_ids}
    if material_ids != {m.material_id for m in result.materials}:
        raise ValueError("seed links have not actually been inspected")
    materials = {m.material_id: m for m in result.materials}
    leads = []
    seen = set(known_domains)
    for link in investigation.analysis.related_links:
        try:
            domain = normalize(link.url)
        except ValueError:
            continue
        if domain in seen:
            continue
        # 同一域名即使在多个链接中出现，也只作为一个新增线索进入下一批。
        seen.add(domain)
        leads.append(Lead(
            lead_id="exp-" + uuid4().hex, raw_value=domain,
            source_url=materials[link.material_id].source_url,
            discovery_method="agent_related_links", discovered_at=now(),
            seed_domain=result.candidate.domain, material_id=link.material_id, relation=link.relation,
        ))
    return Expansion(
        seed_domain=result.candidate.domain, material_ids=sorted(material_ids), leads=leads,
        reason="发现关联候选，须独立取证" if leads else "已查找材料中的关联链接，未发现新的有效候选",
    )
