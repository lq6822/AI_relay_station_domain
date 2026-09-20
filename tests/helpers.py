"""离线测试夹具：SDK 响应是假数据，业务函数仍执行真实逻辑。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai.types.chat import ChatCompletion

from relay_intel.candidates import prepare_candidates
from relay_intel.contracts import AIConfig, Analysis, Annotation, Citation, Concern, Lead, Material

ROOT = Path(__file__).parents[1]
TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
QUOTES = {
    "third_party": "We are an independent third party.",
    "model_access": "Users can access model APIs.",
    "upstream_proxy": "Requests are forwarded to upstream model providers.",
    "relay_clue": "An API relay service is advertised.",
    "exclusion": "This is only a directory of links.",
}


def ai_settings(api_key="test-api-key", **updates):
    """使用可提交的配置模板和假 Key；测试不读取用户的本地配置。"""
    settings = json.loads((ROOT / "config/ai.example.json").read_text(encoding="utf-8"))
    settings.update(api_key=api_key, **updates)
    return settings


def policy():
    """取得测试用的模型设置，内容不包含 Key。"""
    return AIConfig.model_validate(ai_settings()).to_policy()


def sample(domain="relay.example.com", identity="m", facts=("exclusion",), needs_review=False, **overrides):
    """构造带逐字引用的合成材料；needs_review 明确增加一个业务疑点。"""
    lead = Lead(lead_id="l-" + identity, raw_value=domain, source_url="https://source.example.com/list",
                discovery_method="manual", discovered_at=TIME)
    values = dict(material_id=identity, domain=domain, source_url="https://" + domain + "/",
                  collected_at=TIME, access_status="ok", evidence_state="current",
                  excerpt=" ".join(QUOTES[f] for f in facts) or "Only a logo is visible.",
                  annotations=[Annotation(fact=f, value="supported", quote=QUOTES[f]) for f in facts])
    values.update(overrides)
    material = Material(**values)
    analysis = Analysis(facts=[], citations=[Citation(material_id=identity,
                        quote=material.excerpt or material.failure_reason)],
                        concerns=[], related_links=[], limitations=[])
    if needs_review:
        analysis.concerns = [Concern(kind="business", reason="Public statement has ambiguous business scope",
                                     citations=analysis.citations)]
    return prepare_candidates([lead])[0][0], material, analysis


def response(content=None, finish="stop", *, tool_calls=None, refusal=None):
    """构造 DeepSeek 格式的实际 SDK 响应，检查消息字段和结束状态。"""
    return ChatCompletion.model_validate({
        "id": "chat-test", "object": "chat.completion", "created": 1, "model": policy().model,
        "choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": content, "tool_calls": tool_calls, "refusal": refusal}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def tool_response(domain="relay.example.com", names=("read_materials", "related_links"), ids=None):
    """模拟模型请求工具，工具执行和证据校验仍由真实业务代码完成。"""
    return response(finish="tool_calls", tool_calls=[
        {"id": "tool-" + str(i), "type": "function", "function": {
            "name": name, "arguments": json.dumps({"domain": domain, "material_ids": ids or []})}}
        for i, name in enumerate(names)
    ])


def final_response(analysis):
    """模拟模型的最终 JSON 输出；字符串可用于测试非法输出路径。"""
    return response(analysis if isinstance(analysis, str) else analysis.model_dump_json())


def client_for(*responses):
    """按给定顺序返回响应或抛异常；多余调用会导致测试失败。"""
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=responses))))
