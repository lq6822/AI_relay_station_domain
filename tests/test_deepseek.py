"""DeepSeek 接入测试：本地配置、Key 替换、HTTP 请求格式和工具调用回传。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from helpers import ai_settings, client_for, final_response, policy, sample, tool_response
from relay_intel import agent, cli, pipeline
from relay_intel.contracts import AIConfig
from relay_intel.workspace import Workspace


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_file_controls_client_and_key_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(folder)
            for key in ("first-test-key", "replacement-test-key"):
                workspace.write(workspace.path("config/ai.json"), ai_settings(api_key=key))
                config = workspace.read_ai_config()
                async with agent.create_client(config.to_policy(), config.api_key) as client:
                    self.assertEqual(client.api_key, key)
                    self.assertEqual(str(client.base_url).rstrip("/"), "https://api.deepseek.com")
                    self.assertEqual(client.max_retries, 0)

    async def test_key_is_excluded_from_config_serialization_and_saved_batch(self):
        secret = "only-in-local-config-test-key"
        config = AIConfig.model_validate(ai_settings(api_key=secret))
        self.assertNotIn(secret, repr(config))
        self.assertNotIn(secret, config.model_dump_json())
        self.assertNotIn("api_key", config.model_dump())
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(folder)
            candidate, material, analysis = sample()
            workspace.write(workspace.path("leads.jsonl"), candidate.sources, jsonl=True)
            workspace.write(workspace.path("materials.jsonl"), [material], jsonl=True)
            batch = pipeline.prepare_batch(workspace, "r", config.to_policy(), leads_path="leads.jsonl",
                                           materials_path="materials.jsonl", synthetic=True)
            await pipeline.run_batch(workspace, batch, client_for(tool_response(), final_response(analysis)))
            saved = (workspace.batch_dir("r") / "batch.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, saved)
            self.assertNotIn("api_key", saved)

    def test_missing_or_invalid_local_config_has_an_actionable_error_without_key(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(folder)
            with self.assertRaisesRegex(ValueError, "config/ai.example.json"):
                workspace.read_ai_config()
            workspace.write(workspace.path("config/ai.json"), ai_settings(api_key="private-test-key", max_requests=99))
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(cli.main(["run", "--root", folder, "--run-id", "r"]), 1)
            self.assertIn("max_requests", output.getvalue())
            self.assertNotIn("private-test-key", output.getvalue())

    def test_resume_rejects_changed_endpoint_or_model_before_using_key(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(folder)
            candidate, material, _ = sample()
            workspace.write(workspace.path("leads.jsonl"), candidate.sources, jsonl=True)
            workspace.write(workspace.path("materials.jsonl"), [material], jsonl=True)
            batch = pipeline.prepare_batch(workspace, "r", policy(), leads_path="leads.jsonl",
                                           materials_path="materials.jsonl", synthetic=True)
            workspace.save_batch(batch)
            for update in ({"base_url": "https://other.example.com"}, {"model": "another-model"}):
                workspace.write(workspace.path("config/ai.json"), ai_settings(**update))
                output = io.StringIO()
                with patch("relay_intel.agent.create_client", side_effect=AssertionError("settings must match")):
                    with self.subTest(update=update), redirect_stderr(output):
                        self.assertEqual(cli.main(["resume", "--root", folder, "--run-id", "r"]), 1)
                self.assertIn("AI settings differ", output.getvalue())


class DeepSeekProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_sends_chat_completions_and_returns_each_tool_result(self):
        candidate, material, analysis = sample()
        replies = [tool_response(), final_response(analysis)]
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=replies[len(requests) - 1].model_dump(mode="json"))

        # 使用实际 SDK 完成 HTTP 序列化和响应解析；传输层在本机返回假响应，不访问网络。
        async with AsyncOpenAI(api_key="wire-test-key", base_url=policy().base_url, max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
            result = await agent.analyze(candidate.domain, [material], policy(), client)

        self.assertEqual(result.analysis, analysis)
        self.assertEqual(result.usage, {"input_tokens": 20, "output_tokens": 10})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.path, "/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer wire-test-key")
        first = json.loads(requests[0].content)
        self.assertEqual(first["model"], "deepseek-flash")
        self.assertEqual(first["response_format"], {"type": "json_object"})
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertEqual(first["tools"][0]["type"], "function")
        second = json.loads(requests[1].content)
        self.assertEqual([m["role"] for m in second["messages"]], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in second["messages"][-2:]], ["tool-0", "tool-1"])
        self.assertEqual(json.loads(second["messages"][-2]["content"])[0]["material_id"], "m")

    async def test_http_authentication_error_is_not_retried_or_logged_with_key(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(401, json={"error": {"message": "rejected private-test-key", "type": "authentication_error"}})

        candidate, material, _ = sample()
        async with AsyncOpenAI(api_key="private-test-key", base_url=policy().base_url, max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
            with self.assertRaises(agent.AgentFailure) as caught:
                await agent.analyze(candidate.domain, [material], policy(), client)
        self.assertEqual(len(requests), 1)
        self.assertIn("status=401", str(caught.exception))
        self.assertNotIn("private-test-key", str(caught.exception))

    async def test_malformed_tool_arguments_can_be_corrected_once(self):
        candidate, material, analysis = sample()
        malformed = tool_response(names=("read_materials",))
        malformed.choices[0].message.tool_calls[0].function.arguments = "not-json"
        client = client_for(malformed, tool_response(), final_response(analysis))
        result = await agent.analyze(candidate.domain, [material], policy(), client)
        self.assertEqual((result.requests, result.tool_calls[0].result), (3, "error"))
        with self.assertRaisesRegex(agent.AgentFailure, "correction limit"):
            await agent.analyze(candidate.domain, [material], policy(), client_for(malformed, malformed))
