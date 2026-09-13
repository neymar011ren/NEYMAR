"""scan_service.py 的测试。

任何涉及 scan_all_agents() 的测试都必须同时用 tmp_home（隔离第三方 Agent 配置路径）
和 isolated_config_store（隔离 kingswitch 自身的 data/config.json）——kingswitch 这个
档案的凭证提取直接读 config_store.load_config()，不受 $HOME 影响，漏了隔离就会在
测试时真的把请求打到本机正在用的真实上游去，这个坑之前踩过一次。
"""

from __future__ import annotations

import json

import pytest

import scan_service as ss
from agent_profiles import AGENT_PROFILES


class TestExtractClaudeCode:
    def test_extracts_from_env_block(self, tmp_home):
        path = tmp_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x.com/v1", "ANTHROPIC_API_KEY": "sk-1", "ANTHROPIC_MODEL": "m"}}),
            encoding="utf-8",
        )
        creds = ss.extract_channel_credentials(AGENT_PROFILES["claude_code"])
        assert creds == {
            "base_url": "https://x.com/v1",
            "model": "m",
            "api_key": "sk-1",
            "source_path": str(path),
        }

    def test_no_file_returns_none(self, tmp_home):
        assert ss.extract_channel_credentials(AGENT_PROFILES["claude_code"]) is None

    def test_missing_key_returns_none(self, tmp_home):
        path = tmp_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x.com/v1"}}), encoding="utf-8")
        assert ss.extract_channel_credentials(AGENT_PROFILES["claude_code"]) is None

    def test_malformed_json_returns_none(self, tmp_home):
        path = tmp_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json", encoding="utf-8")
        assert ss.extract_channel_credentials(AGENT_PROFILES["claude_code"]) is None


class TestExtractWorkbuddy:
    def test_extracts_and_strips_chat_completions_suffix(self, tmp_home):
        path = tmp_home / ".workbuddy" / "models.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"models": [{"id": "m1", "url": "https://x.com/v1/chat/completions", "apiKey": "sk-1"}]}),
            encoding="utf-8",
        )
        creds = ss.extract_channel_credentials(AGENT_PROFILES["workbuddy"])
        assert creds["base_url"] == "https://x.com/v1"
        assert creds["model"] == "m1"
        assert creds["api_key"] == "sk-1"


class TestExtractYamlAndTextFormats:
    def test_continue_yaml(self, tmp_home):
        path = tmp_home / ".continue" / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "models:\n  - title: custom\n    provider: openai\n    model: gpt-x\n"
            "    apiBase: https://x.com/v1\n    apiKey: sk-1\n",
            encoding="utf-8",
        )
        creds = ss.extract_channel_credentials(AGENT_PROFILES["continue"])
        assert creds["base_url"] == "https://x.com/v1"
        assert creds["api_key"] == "sk-1"
        assert creds["model"] == "gpt-x"

    def test_aider_conf(self, tmp_home):
        path = tmp_home / ".aider.conf.yml"
        path.write_text("openai-api-base: https://x.com/v1\nopenai-api-key: sk-1\nmodel: openai/custom\n", encoding="utf-8")
        creds = ss.extract_channel_credentials(AGENT_PROFILES["aider"])
        assert creds["base_url"] == "https://x.com/v1"
        assert creds["api_key"] == "sk-1"

    def test_codex_toml_only_reads_forge_custom_block(self, tmp_home):
        path = tmp_home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            'model = "unrelated-top-level-model"\n\n'
            '[model_providers.other_provider]\n'
            'base_url = "https://not-this-one.com"\n'
            'api_key = "sk-wrong"\n\n'
            'model = "gpt-forge"\n'
            'model_provider = "forge_custom"\n\n'
            '[model_providers.forge_custom]\n'
            'name = "KingSwitch Custom"\n'
            'base_url = "https://x.com/v1"\n'
            'api_key = "sk-1"\n',
            encoding="utf-8",
        )
        creds = ss.extract_channel_credentials(AGENT_PROFILES["codex"])
        assert creds["base_url"] == "https://x.com/v1"
        assert creds["api_key"] == "sk-1"
        assert creds["model"] == "gpt-forge"


class TestExtractOpencodeAndCline:
    def test_opencode(self, tmp_home):
        path = tmp_home / ".config" / "opencode" / "opencode.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "provider": {"forge-custom": {"options": {"baseURL": "https://x.com/v1", "apiKey": "sk-1"}}},
                    "model": "forge-custom/gpt-x",
                }
            ),
            encoding="utf-8",
        )
        creds = ss.extract_channel_credentials(AGENT_PROFILES["opencode"])
        assert creds["base_url"] == "https://x.com/v1"
        assert creds["model"] == "gpt-x"

    def test_cline(self, tmp_home):
        path = tmp_home / ".cline" / "data" / "settings" / "global-settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"openAiBaseUrl": "https://x.com/v1", "openAiApiKey": "sk-1", "openAiModelId": "gpt-x"}),
            encoding="utf-8",
        )
        creds = ss.extract_channel_credentials(AGENT_PROFILES["cline"])
        assert creds["base_url"] == "https://x.com/v1"
        assert creds["model"] == "gpt-x"


class TestExtractKingswitch:
    def test_uses_real_config_store_module(self, isolated_config_store):
        from config_store import AgentConfig, save_config

        isolated_config_store.ensure_config_file()
        save_config(AgentConfig.model_validate({**isolated_config_store.load_config().model_dump(), "api_key": "sk-1"}))
        creds = ss.extract_channel_credentials(AGENT_PROFILES["kingswitch"])
        assert creds["api_key"] == "sk-1"

    def test_no_api_key_returns_none(self, isolated_config_store):
        isolated_config_store.ensure_config_file()
        creds = ss.extract_channel_credentials(AGENT_PROFILES["kingswitch"])
        assert creds is None


class TestScanAllAgentsAsync:
    def test_returns_same_shape_as_sync_version(self, tmp_home, isolated_config_store, monkeypatch):
        import asyncio

        isolated_config_store.ensure_config_file()
        monkeypatch.setattr(ss, "probe_channel", lambda *a, **k: {"ok": True})
        result = asyncio.run(ss.scan_all_agents_async(include_tool_call=False, include_fidelity=False))
        assert "generated_at" in result
        assert len(result["agents"]) == len(AGENT_PROFILES)
        assert {row["agent_id"] for row in result["agents"]} == set(AGENT_PROFILES)


class TestScanAllAgents:
    def test_no_network_calls_when_nothing_configured(self, tmp_home, isolated_config_store, monkeypatch):
        # 断言：一个 agent 都没配置时，scan 不应该尝试调用 probe_channel（没有 base_url/key 可用）。
        isolated_config_store.ensure_config_file()
        called = []
        monkeypatch.setattr(ss, "probe_channel", lambda *a, **k: called.append(1) or {"ok": True})
        result = ss.scan_all_agents(include_tool_call=True, include_fidelity=True)
        assert not called
        assert all(row["status"] in {"not_installed", "not_configured"} for row in result["agents"])

    def test_configured_agent_triggers_probe_and_uses_result(self, tmp_home, isolated_config_store, monkeypatch):
        isolated_config_store.ensure_config_file()
        path = tmp_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x.com/v1", "ANTHROPIC_API_KEY": "sk-1", "ANTHROPIC_MODEL": "m"}}),
            encoding="utf-8",
        )

        captured = {}

        def fake_probe(agent_id, base_url, model, api_key, source_protocol=None, include_tool_call=False, timeout_seconds=None):
            captured["args"] = (agent_id, base_url, model, api_key, source_protocol, include_tool_call, timeout_seconds)
            return {"ok": True, "ping": {"ok": True}, "tool_calling": {"ok": True}}

        monkeypatch.setattr(ss, "probe_channel", fake_probe)
        result = ss.scan_all_agents(include_tool_call=True, include_fidelity=True)
        claude_row = next(r for r in result["agents"] if r["agent_id"] == "claude_code")
        assert claude_row["status"] == "ok"
        assert claude_row["config_found"] is True
        assert claude_row["probe"]["ok"] is True
        assert captured["args"][1] == "https://x.com/v1"
        assert captured["args"][5] is True  # include_tool_call 透传
        assert captured["args"][6] == ss.SCAN_TIMEOUT_SECONDS  # 用固定的短超时，不是用户的全局超时

    def test_config_found_but_probe_fails(self, tmp_home, isolated_config_store, monkeypatch):
        isolated_config_store.ensure_config_file()
        path = tmp_home / ".aider.conf.yml"
        path.write_text("openai-api-base: https://x.com/v1\nopenai-api-key: sk-1\nmodel: m\n", encoding="utf-8")
        monkeypatch.setattr(ss, "probe_channel", lambda *a, **k: {"ok": False, "error": "connection refused"})
        result = ss.scan_all_agents(include_tool_call=True, include_fidelity=True)
        aider_row = next(r for r in result["agents"] if r["agent_id"] == "aider")
        assert aider_row["status"] == "probe_failed"


class TestReportRendering:
    def _fake_scan(self):
        return {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "agents": [
                {
                    "agent_id": "claude_code",
                    "name": "Claude Code",
                    "native_protocol": "anthropic_messages",
                    "installed": True,
                    "config_found": True,
                    "base_url": "https://x.com/v1",
                    "model": "m",
                    "api_key_masked": "sk-1...abcd",
                    "source_path": "/home/.claude/settings.json",
                    "status": "ok",
                    "probe": {
                        "ok": True,
                        "ping": {"ok": True},
                        "tool_calling": {"ok": True, "note": "上游正确发起了工具调用"},
                    },
                    "protocol_fidelity": {"ok": True},
                },
                {
                    "agent_id": "aider",
                    "name": "Aider",
                    "native_protocol": "chat_completions",
                    "installed": False,
                    "config_found": False,
                    "status": "not_installed",
                },
            ],
        }

    def test_markdown_contains_table_and_details(self):
        md = ss.render_report_markdown(self._fake_scan())
        assert "| Claude Code |" in md
        assert "✅" in md
        assert "上游正确发起了工具调用" in md
        assert "### Aider" in md

    def test_save_report_writes_file(self, isolated_reports_dir):
        md = ss.render_report_markdown(self._fake_scan())
        path = ss.save_report(md)
        assert path.exists()
        assert path.parent == isolated_reports_dir
        assert path.read_text(encoding="utf-8") == md
