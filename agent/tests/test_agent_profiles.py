from __future__ import annotations

from agent_profiles import detect_from_text, forge_config_path, profile_paths, resolve_profile


class TestResolveProfile:
    def test_canonical_id(self):
        assert resolve_profile("claude_code").id == "claude_code"

    def test_hyphen_and_space_normalized(self):
        assert resolve_profile("claude-code").id == "claude_code"
        assert resolve_profile("Claude Code").id == "claude_code"

    def test_alias_matches(self):
        assert resolve_profile("cc").id == "claude_code"
        assert resolve_profile("work buddy").id == "workbuddy"

    def test_forge_aliases_map_to_kingswitch(self):
        for alias in ("forge", "forge_agent", "judger", "judger_agent", "kingswitch"):
            assert resolve_profile(alias).id == "kingswitch"

    def test_unknown_returns_none(self):
        assert resolve_profile("totally-not-a-real-agent") is None

    def test_empty_returns_none(self):
        assert resolve_profile("") is None


class TestDetectFromText:
    def test_direct_mention(self):
        matches = detect_from_text("我想把模型配到 Claude Code")
        assert matches
        assert matches[0]["id"] == "claude_code"

    def test_spaced_out_alias(self):
        # 系统里专门处理了 "w o r k b u d d y" 这种被空格拆开的英文拼写
        matches = detect_from_text("帮我配到 w o r k b u d d y")
        assert matches
        assert matches[0]["id"] == "workbuddy"

    def test_no_match_returns_empty(self):
        assert detect_from_text("今天天气怎么样") == []

    def test_kingswitch_config_path_uses_forge_config_path(self):
        matches = detect_from_text("配置 kingswitch 自己")
        hit = next(m for m in matches if m["id"] == "kingswitch")
        assert hit["config_paths"] == [str(forge_config_path())]


class TestProfilePaths:
    def test_kingswitch_uses_forge_config_path(self):
        profile = resolve_profile("kingswitch")
        assert profile_paths(profile) == [forge_config_path()]

    def test_third_party_expands_home(self, tmp_home):
        profile = resolve_profile("claude_code")
        paths = profile_paths(profile)
        assert all(str(p).startswith(str(tmp_home)) for p in paths)
        assert paths[0].name == "settings.json"
