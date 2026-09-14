"""agent_config_tools.py 直接读写用户真实的 Agent 配置文件（~/.claude/settings.json 等），
是这个项目里"改错了代价最大"的一块代码。所有测试都必须通过 tmp_home 把 $HOME
重定向到临时目录，绝不能碰到跑测试这台机器上真实的 Agent 配置。
"""

from __future__ import annotations

import json

import pytest

import agent_config_tools as act


def _write(agent_id, base_url, model, api_key, **kw):
    return json.loads(act.write_agent_channel_config(agent_id, base_url, model, api_key, **kw))


def _execute(agent_id, base_url, model, api_key, **kw):
    return act.execute_write_agent_channel_config(agent_id, base_url, model, api_key, **kw)


class TestValidation:
    def test_unknown_agent_id(self, tmp_home):
        result = _write("not-a-real-agent", "https://x.com/v1", "m", "sk-1")
        assert result == {"ok": False, "error": "未知 agent_id: not-a-real-agent"}

    def test_empty_base_url_rejected(self, tmp_home):
        result = _write("claude_code", "", "m", "sk-1")
        assert result["ok"] is False
        assert "base_url" in result["error"]

    def test_empty_model_rejected(self, tmp_home):
        # 回归测试：曾经这里没有校验，会把字面量 "custom"/"custom-model" 写进用户的真实配置。
        result = _write("claude_code", "https://x.com/v1", "", "sk-1")
        assert result["ok"] is False
        assert "model" in result["error"]

    def test_empty_api_key_rejected(self, tmp_home):
        result = _write("claude_code", "https://x.com/v1", "m", "")
        assert result["ok"] is False
        assert "api_key" in result["error"]


class TestPreviewDoesNotTouchDisk:
    def test_preview_is_side_effect_free(self, tmp_home):
        target = tmp_home / ".claude" / "settings.json"
        result = _write("claude_code", "https://x.com/v1", "claude-3-5", "sk-real-secret")
        assert result["ok"] is True
        assert result["confirm_required"] is True
        assert result["target_path"] == str(target)
        assert result["will_create_new_file"] is True
        assert not target.exists()

    def test_preview_never_contains_raw_key(self, tmp_home):
        result = _write("claude_code", "https://x.com/v1", "claude-3-5", "sk-real-secret-value")
        assert "sk-real-secret-value" not in json.dumps(result)
        assert result["api_key_masked"] != "sk-real-secret-value"


class TestExecuteWrite:
    def test_execute_writes_to_previewed_path(self, tmp_home, isolated_memory_store):
        preview = _write("claude_code", "https://x.com/v1", "claude-3-5", "sk-real")
        result = _execute("claude_code", "https://x.com/v1", "claude-3-5", "sk-real")
        assert result["ok"] is True
        assert result["written"]["path"] == preview["target_path"]
        data = json.loads((tmp_home / ".claude" / "settings.json").read_text())
        assert data["env"]["ANTHROPIC_API_KEY"] == "sk-real"
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://x.com/v1"

    def test_execute_backs_up_existing_file(self, tmp_home, isolated_memory_store):
        claude_dir = tmp_home / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"env": {"OTHER": "1"}, "otherSetting": True}), encoding="utf-8")

        result = _execute("claude_code", "https://x.com/v1", "claude-3-5", "sk-real")
        assert result["ok"] is True
        assert result["written"]["backup"] is not None
        # 原有的、和渠道无关的字段应该被保留，而不是整体清空
        data = json.loads(settings.read_text())
        assert data["otherSetting"] is True
        assert data["env"]["OTHER"] == "1"
        assert data["env"]["ANTHROPIC_API_KEY"] == "sk-real"

    def test_corrupted_existing_file_gets_warning_not_silent_wipe(self, tmp_home, isolated_memory_store):
        claude_dir = tmp_home / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text("{this is not valid json", encoding="utf-8")

        result = _execute("claude_code", "https://x.com/v1", "claude-3-5", "sk-real")
        assert result["ok"] is True
        assert "warning" in result["written"]
        backups = list(claude_dir.glob("settings.json.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "{this is not valid json"

    def test_workbuddy_upserts_by_model_id(self, tmp_home, isolated_memory_store):
        wb_dir = tmp_home / ".workbuddy"
        wb_dir.mkdir()
        (wb_dir / "models.json").write_text(
            json.dumps({"models": [{"id": "other-model", "url": "https://old.com"}]}), encoding="utf-8"
        )
        _execute("workbuddy", "https://new.com/v1", "gpt-x", "sk-real")
        data = json.loads((wb_dir / "models.json").read_text())
        ids = [m["id"] for m in data["models"]]
        assert "other-model" in ids  # 旧条目应该保留
        assert "gpt-x" in ids
        new_entry = next(m for m in data["models"] if m["id"] == "gpt-x")
        assert new_entry["url"] == "https://new.com/v1/chat/completions"

    def test_kingswitch_target_updates_own_config(self, tmp_home, isolated_config_store, isolated_memory_store):
        isolated_config_store.ensure_config_file()
        result = _execute("kingswitch", "https://new-upstream.com/v1", "glm-5", "sk-real")
        assert result["ok"] is True
        cfg = isolated_config_store.load_config()
        assert cfg.api_base_url == "https://new-upstream.com/v1"
        assert cfg.model == "glm-5"
        assert cfg.api_key == "sk-real"


class TestProbeAgentConfigRedaction:
    def test_json_config_is_redacted(self, tmp_home):
        wb_dir = tmp_home / ".workbuddy"
        wb_dir.mkdir()
        (wb_dir / "models.json").write_text(
            json.dumps({"models": [{"id": "m1", "apiKey": "sk-secret-value-1234"}]}), encoding="utf-8"
        )
        result = json.loads(act.probe_agent_config("workbuddy"))
        preview = result["findings"][0]["preview"]
        assert preview["models"][0]["apiKey"] != "sk-secret-value-1234"
        assert "sk-secret-value-1234" not in json.dumps(result)

    def test_non_json_config_is_redacted(self, tmp_home):
        aider_conf = tmp_home / ".aider.conf.yml"
        aider_conf.write_text("openai-api-key: sk-secret-value-1234\nmodel: gpt-4o\n", encoding="utf-8")
        result = json.loads(act.probe_agent_config("aider"))
        preview_text = result["findings"][0]["preview_text"]
        assert "sk-secret-value-1234" not in preview_text
        assert "model: gpt-4o" in preview_text  # 非敏感字段应该原样保留，只脱敏密钥行

    def test_unknown_agent_returns_error(self, tmp_home):
        result = json.loads(act.probe_agent_config("nope"))
        assert result["ok"] is False


class TestTestAgentChannelValidation:
    def test_missing_params_rejected_without_network_call(self, tmp_home):
        result = json.loads(act.test_agent_channel("claude_code", "", "", ""))
        assert result["ok"] is False

    def test_unknown_agent_rejected(self, tmp_home):
        result = json.loads(act.test_agent_channel("nope", "https://x.com", "m", "sk-1"))
        assert result["ok"] is False


class TestChannelHistory:
    """execute_write_agent_channel_config 成功后应该留下一条结构化历史记录，
    这样"上次给这个 Agent 配的是什么"可以精确查询，而不用指望聊天记忆模糊召回。"""

    def test_successful_write_is_recorded(self, tmp_home, isolated_memory_store):
        _execute("claude_code", "https://x.com/v1", "claude-3-5", "sk-real-secret")
        history = json.loads(act.list_channel_history("claude_code"))
        assert history["ok"] is True
        assert len(history["history"]) == 1
        record = history["history"][0]
        assert record["agent_id"] == "claude_code"
        assert record["base_url"] == "https://x.com/v1"
        assert record["model"] == "claude-3-5"
        assert "sk-real-secret" not in json.dumps(record)

    def test_history_filters_by_agent_id(self, tmp_home, isolated_memory_store):
        _execute("claude_code", "https://x.com/v1", "claude-3-5", "sk-1")
        _execute("aider", "https://y.com/v1", "gpt-x", "sk-2")
        only_claude = json.loads(act.list_channel_history("claude_code"))["history"]
        assert len(only_claude) == 1
        assert only_claude[0]["agent_id"] == "claude_code"

    def test_no_history_returns_empty_list(self, tmp_home, isolated_memory_store):
        result = json.loads(act.list_channel_history("claude_code"))
        assert result["ok"] is True
        assert result["history"] == []

    def test_failed_write_is_not_recorded(self, tmp_home, isolated_memory_store):
        _execute("not-a-real-agent", "https://x.com/v1", "m", "sk-1")
        result = json.loads(act.list_channel_history())
        assert result["history"] == []
