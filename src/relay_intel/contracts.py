"""输入、模型分析和批次结果的数据结构。业务判定统一在 assessment.py。"""

import math
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict,
    Field, SecretStr, StrictBool, StrictInt, StringConstraints, model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Fact = Literal["third_party", "model_access", "upstream_proxy", "relay_clue", "exclusion"]
# 五项事实分别表示：第三方身份、面向用户的模型访问、上游代理关系、具体中转线索、排除依据。
Value = Literal["supported", "refuted", "unknown"]
Label = Literal["确认", "疑似", "排除", "证据不足"]
FACTS = ("third_party", "model_access", "upstream_proxy", "relay_clue", "exclusion")


def now() -> datetime:
    """返回带 UTC 时区的当前时间，用于分析和复核记录。"""
    return datetime.now(timezone.utc)


def public_url(value: str) -> str:
    """校验 HTTP(S) 来源地址格式；不访问网址，也不证明来源内容真实。"""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("expected an HTTP(S) URL without credentials")
    if any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("invalid URL characters")
    _ = parsed.port
    return value


URL = Annotated[Text, AfterValidator(public_url)]


def finite_score(value):
    """置信度只接受 [0,1] 的有限数值，拒绝布尔值、字符串及 NaN。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("confidence must be finite and within [0,1]")
    return float(value)


Score = Annotated[float, BeforeValidator(finite_score)]


class Record(BaseModel):
    """所有输入和持久化对象的基础约束：拒绝未知字段，避免拼写错误被静默忽略。"""
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Policy(Record):
    """可随批次保存的 AI 设置：接口地址、模型和调用限额，不包含 Key。"""
    base_url: URL
    model: Text
    max_requests: Annotated[StrictInt, Field(ge=1, le=5)] = 5
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=60)] = 60
    max_tokens: Annotated[StrictInt, Field(ge=256, le=8192)] = 4096

    @model_validator(mode="after")
    def endpoint(self):
        """接口基地址只保留路径，不接受查询参数或片段。"""
        parsed = urlsplit(self.base_url)
        if parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain query parameters or fragments")
        self.base_url = self.base_url.rstrip("/")
        return self


class AIConfig(Policy):
    """config/ai.json 的完整配置；Key 只用于连接，不进入日志或批次序列化。"""
    api_key: SecretStr = Field(exclude=True)

    def to_policy(self) -> Policy:
        """提取可保存的设置，供分析及中断后继续执行使用。"""
        return Policy.model_validate(self.model_dump())


class Lead(Record):
    """域名发现线索。source_url 是发现出处，扩展线索还保留种子和关联依据。"""
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
        """种子、材料 ID、关联说明必须同时提供，避免留下无法解释的扩展来源。"""
        fields = (self.seed_domain, self.material_id, self.relation)
        if any(fields) and not all(fields):
            raise ValueError("seed_domain, material_id and relation must be supplied together")
        return self


class Annotation(Record):
    """人工材料标注：事实名称、支持/反驳/未知状态和原文。"""
    fact: Fact
    value: Value
    quote: Text


class Material(Record):
    """一条公开观察及其时间、主体和来源属性；成功片段与失败记录互斥。"""
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
    source_kind: Literal["direct", "secondary"] = "direct"
    subject_relation: Literal["exact", "uncertain"] = "exact"

    @model_validator(mode="after")
    def evidence(self):
        """检查材料内部一致性和人工引用；访问失败不能携带业务事实。"""
        if self.access_status == "ok":
            if not self.excerpt or self.failure_reason:
                raise ValueError("successful material requires excerpt and no failure_reason")
        elif not self.failure_reason or self.excerpt or self.annotations:
            raise ValueError("failed/blocked material requires a failure reason and no facts")
        for annotation in self.annotations:
            if annotation.quote not in (self.excerpt or ""):
                raise ValueError("annotation quote cannot be located in excerpt")
        if self.published_at and self.published_at > self.collected_at:
            raise ValueError("published_at is later than collected_at")
        if self.collected_at > now():
            raise ValueError("collected_at cannot be in the future")
        return self


class Citation(Record):
    """可定位引用，由材料 ID 和逐字原文组成。"""
    material_id: Identifier
    quote: Text


class FactSuggestion(Annotation):
    """AI 或复核者提出的事实解释；引用存在不代表语义已经被证明。"""
    material_id: Identifier


class Concern(Record):
    """模型认为影响判定的疑点，人工可按列表序号记录处理结果。"""
    kind: Literal["missing", "conflict", "timeliness", "business", "subject"]
    reason: Text
    citations: list[Citation] = Field(min_length=1, max_length=10)


class Link(Record):
    """材料中实际出现的关联 URL，同时保留原文上下文和业务关系。"""
    material_id: Identifier
    url: URL
    context: Text
    relation: Text


class Analysis(Record):
    """模型的结构化输出；不含最终标签和分数，以免出现两套判定。"""
    facts: list[FactSuggestion] = Field(max_length=40)
    citations: list[Citation] = Field(min_length=1, max_length=40)
    concerns: list[Concern] = Field(max_length=10)
    related_links: list[Link] = Field(max_length=50)
    limitations: list[Text] = Field(max_length=20)


class ToolArgs(Record):
    """两个只读工具共用的参数；material_ids 为空表示读取本域全部材料。"""
    domain: Text
    material_ids: list[Identifier] = Field(default_factory=list, max_length=40)


class Candidate(Record):
    """按完整主机名去重后的候选；同注册域的不同子域仍分别调查。"""
    domain: Text
    registered_domain: Text
    sources: list[Lead] = Field(min_length=1)


class FactEvidence(FactSuggestion):
    """候选事实的一条依据，并区分人工原始标注、模型解释和人工复核。"""
    origin: Literal["human", "model", "review"]


class MergedFact(Record):
    """某项事实的汇总；同时存在 supported/refuted 时保留冲突，不投票消除。"""
    fact: Fact
    value: Value
    conflict: StrictBool
    evidence: list[FactEvidence]


class ToolCall(Record):
    """一次工具调用的名称、材料范围和成功/失败状态，供面试演示追溯。"""
    name: str
    material_ids: list[str]
    result: Literal["ok", "error"]


class Investigation(Record):
    """一次成功的 AI 分析，包含原始输出、工具调用及 token 用量。"""
    analysis: Analysis
    tool_calls: list[ToolCall]
    requests: StrictInt
    usage: dict[str, StrictInt]
    created_at: AwareDatetime = Field(default_factory=now)


class Review(Record):
    """针对固定批次某域名的一次复核；reviewer 写明实际身份，AI 复核不能写成人工。"""
    run_id: Identifier
    domain: Text
    action: Literal["accept", "revise"]
    reviewer: Text
    reviewed_at: AwareDatetime
    reason: Text
    citations: list[Citation] = Field(min_length=1, max_length=40)
    new_label: Label | None = None
    fact_revisions: list[FactSuggestion] = Field(default_factory=list, max_length=20)
    # 原始 analysis.concerns 列表的序号，从 1 开始；原列表不改写，便于对照复核。
    resolved_concerns: list[Annotated[StrictInt, Field(ge=1)]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def revision_fields(self):
        """接受原结论不能夹带修改；修订必须声明目标标签，且复核时间不能在未来。"""
        if (self.action == "revise") != (self.new_label is not None):
            raise ValueError("only revise requires new_label")
        if self.action == "accept" and (self.fact_revisions or self.resolved_concerns):
            raise ValueError("accept cannot change facts or resolve concerns; use revise")
        if self.reviewed_at > now():
            raise ValueError("reviewed_at cannot be in the future")
        return self


class Assessment(Record):
    """一次判定及实际采用的事实；复核会重新生成整份判定，导出直接使用它。"""
    label: Label
    confidence: Score
    confidence_grade: Literal["provisional", "limited", "sufficient"]
    reason: Text
    facts: list[MergedFact]
    # 只有这些材料支撑当前标签；其余材料仍保留，但不能刷新验证时间。
    basis_material_ids: list[Identifier] = Field(min_length=1)
    review_reasons: list[Text]
    review_status: Literal["not_required", "pending", "completed"]


class DomainResult(Record):
    """单域记录：有 assessment 表示分析成功，有 error 表示失败，否则尚待分析。"""
    candidate: Candidate
    materials: list[Material]
    investigation: Investigation | None = None
    assessment: Assessment | None = None
    review: Review | None = None
    error: Text | None = None

    @property
    def needs_analysis(self) -> bool:
        """中断后只继续尚待分析的域名；已完成和已记录失败的域名均不重复调用。"""
        return self.assessment is None and self.error is None


class Issue(Record):
    """输入或复核中被拒绝的条目及原因；location 指向文件行、ID 或域名。"""
    location: Text
    reason: Text


class Expansion(Record):
    """一个确认种子的实际查找结果；leads 为空也表示已经尝试但无新增。"""
    seed_domain: Text
    material_ids: list[Identifier]
    leads: list[Lead]
    reason: Text
    created_at: AwareDatetime = Field(default_factory=now)


class Batch(Record):
    """固定输入和模型配置的一次批次；逐域更新 batch.json，供中断后继续。"""
    run_id: Identifier
    created_at: AwareDatetime = Field(default_factory=now)
    policy: Policy
    synthetic: StrictBool
    results: list[DomainResult]
    issues: list[Issue] = Field(default_factory=list)
    expansions: list[Expansion] = Field(default_factory=list)


class IntelligenceEvidence(Record):
    """可独立复核的证据：当前事实、原始材料、模型引用与疑点、人工处理记录。"""
    materials: list[Material] = Field(min_length=1)
    facts: list[MergedFact]
    basis_material_ids: list[Identifier] = Field(min_length=1)
    citations: list[Citation]
    # 保留模型疑点的原始顺序，与 review.resolved_concerns 的序号一致。
    concerns: list[Concern]
    review: Review | None


class Intelligence(Record):
    """面试交付的一条情报，包含七个必需字段及分档方法、复核状态。"""
    domain: Text
    label: Label
    confidence: Score
    evidence: IntelligenceEvidence
    reason: Text
    discovery_source: list[Lead] = Field(min_length=1)
    last_verified: AwareDatetime
    confidence_method: Text
    review_status: Literal["not_required", "completed"]
