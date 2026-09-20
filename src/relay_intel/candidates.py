"""Hostname normalization, immutable source IDs, and one-hop expansion."""

import ipaddress
import re
from urllib.parse import urlsplit

import idna
import tldextract

from .contracts import Candidate, Expansion, Issue, Lead, now
from .workspace import digest

# Never fetch a suffix list or write a shared runtime cache.
_suffixes = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                                fallback_to_snapshot=True, include_psl_private_domains=True)


def normalize(raw: str) -> str:
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


def registered_domain(host: str) -> str:
    return _suffixes(normalize(host)).top_domain_under_public_suffix


def merge_ids(existing, incoming, key: str, run_id: str):
    index = {getattr(row, key): row for row in existing}
    issues = []
    for row in incoming:
        identity = getattr(row, key)
        if identity in index and index[identity] != row:
            issues.append(Issue(run_id=run_id, location=identity, stage="input",
                                reason=f"conflicting content for {key}; original retained"))
        else:
            index[identity] = row
    return list(index.values()), issues


def prepare(run_id: str, leads: list[Lead], existing=()):
    previous = [source for candidate in existing for source in candidate.sources]
    rows, issues = merge_ids(previous, leads, "lead_id", run_id)
    by_domain = {}
    for lead in rows:
        try:
            domain = normalize(lead.raw_value)
            if lead.seed_domain:
                lead = lead.model_copy(update={"seed_domain": normalize(lead.seed_domain)})
            by_domain.setdefault(domain, []).append(lead)
        except ValueError as exc:
            issues.append(Issue(run_id=run_id, location=lead.lead_id, stage="candidate", reason=str(exc)))
    return [Candidate(run_id=run_id, domain=d, registered_domain=registered_domain(d), sources=s)
            for d, s in sorted(by_domain.items())], issues


def expand_seed(seed: Candidate, investigation, assessment, candidates, limit: int):
    if assessment.label != "确认" or assessment.confidence is None:
        raise ValueError("expansion seed must be a valid confirmed assessment")
    if assessment.review_status == "pending" or assessment.investigation_version != investigation.version:
        raise ValueError("seed review is pending or assessment is stale")
    calls = [c for c in investigation.tool_calls if c.name == "related_links" and c.result == "ok"]
    if not calls or not investigation.materials:
        raise ValueError("no actual related_links attempt for this seed")
    material_ids = sorted({m for call in calls for m in call.material_ids})
    sources = {m.material_id: m for m in investigation.materials}
    known = {c.domain for c in candidates}
    leads, added = [], []
    for link in investigation.analysis.related_links:
        if link.material_id not in material_ids:
            raise ValueError("expanded link was not read using related_links")
        try:
            domain = normalize(link.url)
        except ValueError:
            continue
        if domain == seed.domain:
            continue
        if domain not in known and len(added) >= limit:
            continue
        material = sources[link.material_id]
        lead = Lead(lead_id="expanded-" + digest([seed.domain, link.model_dump()])[:32],
                    raw_value=domain, source_url=material.source_url,
                    discovery_method="agent_related_links", discovered_at=investigation.created_at,
                    seed_domain=seed.domain, material_id=link.material_id, relation=link.relation)
        leads.append(lead)
        if domain not in known:
            known.add(domain)
            added.append(domain)
    record = Expansion(seed_domain=seed.domain, investigation_version=investigation.version,
                       attempted_at=now(), material_ids=material_ids,
                       links=investigation.analysis.related_links, added_domains=added,
                       tool_calls=investigation.tool_calls, requests=investigation.requests,
                       usage=investigation.usage, execution=investigation.execution,
                       reason="新增域名必须独立取证" if added else "已实际查找；未发现可新增的有效域名")
    return leads, record
