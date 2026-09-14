from __future__ import annotations

import os

import agent_install as ai


class TestCheckAgentInstalled:
    def test_unknown_agent_is_explicit_failure(self):
        # 之前这里对未知 agent_id 也返回 ok=True，main.py 里对应的 404 分支永远不会触发。
        result = ai.check_agent_installed("definitely-not-a-real-agent")
        assert result == {"ok": False, "error": "未知 agent_id: definitely-not-a-real-agent"}

    def test_known_agent_returns_ok(self, tmp_home):
        result = ai.check_agent_installed("aider")
        assert result["ok"] is True
        assert result["agent_id"] == "aider"
        assert "installed" in result


class TestAugmentedSearchPath:
    def test_finds_binary_in_npm_global_bin(self, tmp_home, monkeypatch):
        npm_bin = tmp_home / ".npm-global" / "bin"
        npm_bin.mkdir(parents=True)
        fake = npm_bin / "fake-cli"
        fake.write_text("#!/bin/sh\necho hi\n")
        fake.chmod(0o755)

        # 故意清空真实 PATH，确保能找到完全依赖新增的搜索路径
        monkeypatch.setenv("PATH", "/usr/bin")
        search_path = ai._augmented_search_path()
        assert str(npm_bin) in search_path

    def test_check_agent_installed_sees_npm_global_binary(self, tmp_home, monkeypatch):
        # 复现原来的 bug 场景：run_agent_install 用 npm_config_prefix 把 claude
        # 装到 ~/.npm-global/bin，但检测环节如果只看没更新过的 os.environ["PATH"]，
        # 会一直报"未安装"。
        npm_bin = tmp_home / ".npm-global" / "bin"
        npm_bin.mkdir(parents=True)
        fake_claude = npm_bin / "claude"
        fake_claude.write_text("#!/bin/sh\necho hi\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("PATH", "/usr/bin")

        result = ai.check_agent_installed("claude_code")
        assert result["installed"] is True
        assert any("npm-global" in b for b in result["bins_found"])
