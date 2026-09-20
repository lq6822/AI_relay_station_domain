"""Shared, deliberately small input and persisted record contracts."""

import math
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict,
    Field, StrictBool, StrictInt, StringConstraints, model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Fact = Literal["third_party", "model_access", "upstream_proxy", "relay_clue", "exclusion"]
Value = Literal["supported", "refuted", "unknown"]
Label = Literal["确认", "疑似", "排除", "证据不足"]
FACTS = ("third_party", "model_access", "upstream_proxy", "relay_clue", "exclusion")


def now() -> datetime:
    return datetime.now(timezone.utc)


def public_url(value: str) -> str:
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("source must be an HTTP(S) URL without credentials")
    if any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("invalid URL characters")
    _ = p.port
    return value


URL = Annotated[Text, AfterValidator(public_url)]


def finite_score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number, never a boolean or string")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("confidence must be finite and within [0,1]")
    return float(value)


Score = Annotated[float, BeforeValidator(finite_score)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Policy(Record):
    version: Identifier
    model: Text
    max_requests: Annotated[StrictInt, Field(ge=1, le=5)] = 5
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=60)] = 60
    max_corrections: Annotated[StrictInt, Field(ge=0, le=1)] = 1
    max_tokens: Annotated[StrictInt, Field(ge=256, le=8192)] = 4096
    max_input_bytes: Annotated[StrictInt, Field(ge=1, le=10000000)] = 10000000
    max_input_lines: Annotated[StrictInt, Field(ge=1, le=10000)] = 10000
    max_materials_per_domain: Annotated[StrictInt, Field(ge=1, le=40)] = 40
    max_domain_chars: Annotated[StrictInt, Field(ge=1, le=60000)] = 60000
    max_expansion: Annotated[StrictInt, Field(ge=1, le=50)] = 50
    confidence_method: Literal["label_evidence_grade_v1"]
    confidence_basis: Literal["engineering_default"]
    grades: dict[str, Score]
    criteria: dict[Label, Text]

    @model_validator(mode="after")
    def fixed_grades(self):
        if self.grades != {"provisional": .55, "limited": .75, "sufficient": .90}:
            raise ValueError("policy v1 requires the planned evidence grades")
        if set(self.criteria) != {"确认", "疑似", "排除", "证据不足"}:
            raise ValueError("all four label criteria are required")
        return self


class Lead(Record):
    lead_id: Identifier
    raw_value: Text
    source_url: URL
    discovery_method: Text
    discovered_at: AwareDatetime
    seed_domain: Text | None = None
    material_id: Identifier | None = None
    relation: Text | None = None

    @model_validator(mode="after")
    def seed_fields(self):
        if any((self.seed_domain, self.material_id, self.relation)) and not all(
            (self.seed_domain, self.material_id, self.relation)
        ):
            raise ValueError("seed_domain, material_id and relation must be supplied together")
        return self


class Annotation(Record):
    fact: Fact
    value: Value
    quote: Text


class Material(Record):
    material_id: Identifier
    domain: Text
    source_url: URL
    collected_at: AwareDatetime
    access_status: Literal["ok", "failed", "blocked"]
    evidence_state: Literal["current", "historical", "unknown"]
    excerpt: Text | None = None
    failure_reason: Text | None = None
    published_at: AwareDatetime | None = None
    annotations: list[Annotation] = Field(default_factory=list, max_length=20)
    # Explicit human provenance prevents the model from upgrading indirect evidence.
    source_kind: Literal["direct", "secondary"] = "direct"
    subject_relation: Literal["exact", "uncertain"] = "exact"

    @model_validator(mode="after")
    def evidence(self):
        if self.access_status == "ok":
            if not self.excerpt or self.failure_reason:
                raise ValueError("successful material requires excerpt and no failure_reason")
        elif not self.failure_reason or self.excerpt or self.annotations:
            raise ValueError("failed/blocked material requires a failure reason and no facts")
        for a in self.annotations:
            if a.quote not in (self.excerpt or ""):
                raise ValueError("annotation quote cannot be located in excerpt")
        if self.published_at and self.published_at > self.collected_at:
            raise ValueError("published_at is later than collected_at")
        if self.collected_at > now():
            raise ValueError("collected_at cannot be in the future")
        return self


class Citation(Record):
    material_id: Identifier
    quote: Text


class FactSuggestion(Annotation):
    material_id: Identifier


class Concern(Record):
    kind: Literal["missing", "conflict", "timeliness", "business", "subject"]
    reason: Text
    affects_decision: StrictBool
    citations: list[Citation] = Field(min_length=1, max_length=10)


class Link(Record):
    material_id: Identifier
    url: URL
    context: Text
    relation: Text


class Analysis(Record):
    facts: list[FactSuggestion] = Field(max_length=40)
    suggested_label: Label
    reason: Text
    quality_reason: Text
    quality_citations: list[Citation] = Field(min_length=1, max_length=40)
    concerns: list[Concern] = Field(max_length=10)
    related_links: list[Link] = Field(max_length=50)
    limitations: list[Text] = Field(max_length=20)


class ToolArgs(Record):
    domain: Text
    material_ids: list[Identifier] = Field(default_factory=list, max_length=40)


class Candidate(Record):
    run_id: Identifier
    domain: Text
    registered_domain: Text
    sources: list[Lead] = Field(min_length=1)


class FactEvidence(FactSuggestion):
    origin: Literal["human", "model"]


class MergedFact(Record):
    fact: Fact
    value: Value
    conflict: StrictBool
    evidence: list[FactEvidence]


class ToolCall(Record):
    name: str
    material_ids: list[str]
    result: Literal["ok", "error"]


class Investigation(Record):
    run_id: Identifier
    domain: Text
    version: Annotated[StrictInt, Field(ge=1)]
    fingerprint: Text
    materials: list[Material]
    facts: list[MergedFact]
    analysis: Analysis | None
    failure_reason: Text | None = None
    tool_calls: list[ToolCall]
    requests: StrictInt
    usage: dict[str, StrictInt]
    execution: Literal["live", "offline_test"]
    created_at: AwareDatetime


class Review(Record):
    run_id: Identifier
    domain: Text
    assessment_version: Annotated[StrictInt, Field(ge=1)]
    action: Literal["accept", "revise"]
    reviewer: Text
    reviewed_at: AwareDatetime
    reason: Text
    citations: list[Citation] = Field(min_length=1, max_length=40)
    new_label: Label | None = None
    fact_revisions: list[FactSuggestion] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def revision_label(self):
        if (self.action == "revise") != (self.new_label is not None):
            raise ValueError("only revise requires new_label")
        if self.action == "accept" and self.fact_revisions:
            raise ValueError("accept cannot revise facts")
        if self.reviewed_at > now():
            raise ValueError("reviewed_at cannot be in the future")
        return self


class Assessment(Record):
    run_id: Identifier
    domain: Text
    version: Annotated[StrictInt, Field(ge=1)]
    investigation_version: Annotated[StrictInt, Field(ge=1)]
    fingerprint: Text
    label: Label
    confidence: Score | None
    confidence_grade: Literal["undetermined", "provisional", "limited", "sufficient"]
    confidence_method: Literal["label_evidence_grade_v1"]
    confidence_basis: Literal["engineering_default"]
    reason: Text
    review_required: StrictBool
    review_reasons: list[Text]
    review_status: Literal["not_required", "pending", "completed"]
    review: Review | None = None
    created_at: AwareDatetime


class Issue(Record):
    run_id: Identifier
    domain: str | None = None
    location: str | None = None
    stage: str
    reason: str
    created_at: AwareDatetime = Field(default_factory=now)


class Expansion(Record):
    seed_domain: Text
    investigation_version: StrictInt
    attempted_at: AwareDatetime
    method: Literal["agent_related_links"] = "agent_related_links"
    material_ids: list[str]
    links: list[Link]
    added_domains: list[str]
    reason: Text
    tool_calls: list[ToolCall]
    requests: StrictInt
    usage: dict[str, StrictInt]
    execution: Literal["live", "offline_test"]


class Manifest(Record):
    run_id: Identifier
    created_at: AwareDatetime
    policy: Policy
    program_version: Text
    implementation_digest: Text
    prompt_version: Text
    configuration_digest: Text
    input_digest: Text
    synthetic: StrictBool
    api_base_url: URL | None = None
    status: Literal["running", "analyzed", "exporting", "exported", "failed"]
    domain_state: dict[str, Literal["pending", "completed", "failed"]] = Field(default_factory=dict)
    expansions: list[Expansion] = Field(default_factory=list)
    export_completed: StrictBool = False


class Intelligence(Record):
    domain: Text
    label: Label
    confidence: Score
    evidence: list[Material] = Field(min_length=1)
    reason: Text
    discovery_source: list[Lead] = Field(min_length=1)
    last_verified: AwareDatetime
    confidence_method: Literal["label_evidence_grade_v1"]
    assessment_version: StrictInt
    review_status: Literal["not_required", "completed"]
