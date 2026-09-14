from __future__ import annotations

from config_store import AgentConfig
import memory_service as ms


def _cfg(**kw):
    return AgentConfig(api_key="k", model="m", memory_embed_api_key="", **kw)


class TestLocalMemoryRoundTrip:
    def test_add_then_list(self, isolated_memory_store):
        cfg = _cfg()
        result = ms.local_add(cfg, [{"role": "user", "content": "我喜欢用 claude code"}, {"role": "assistant", "content": "记住了"}])
        assert result["ok"] is True
        items = ms.local_list(cfg, limit=10)
        assert len(items) == 1
        assert "claude code" in items[0]["memory"]

    def test_search_ranks_overlapping_terms_first(self, isolated_memory_store):
        cfg = _cfg()
        ms.local_add(cfg, [{"role": "user", "content": "workbuddy 渠道配置"}, {"role": "assistant", "content": "ok"}])
        ms.local_add(cfg, [{"role": "user", "content": "今天天气怎么样"}, {"role": "assistant", "content": "晴"}])
        hits = ms.local_search(cfg, "workbuddy 渠道", limit=5)
        assert hits
        assert "workbuddy" in hits[0]["memory"]

    def test_different_users_are_isolated(self, isolated_memory_store):
        cfg_a = _cfg(memory_user_id="alice")
        cfg_b = _cfg(memory_user_id="bob")
        ms.local_add(cfg_a, [{"role": "user", "content": "alice 的秘密"}, {"role": "assistant", "content": "ok"}])
        assert ms.local_list(cfg_a, limit=10)
        assert ms.local_list(cfg_b, limit=10) == []

    def test_clear_only_removes_target_user(self, isolated_memory_store):
        cfg_a = _cfg(memory_user_id="alice")
        cfg_b = _cfg(memory_user_id="bob")
        ms.local_add(cfg_a, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
        ms.local_add(cfg_b, [{"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}])
        ms.local_clear(cfg_a)
        assert ms.local_list(cfg_a, limit=10) == []
        assert len(ms.local_list(cfg_b, limit=10)) == 1

    def test_add_with_no_usable_content_is_not_ok(self, isolated_memory_store):
        cfg = _cfg()
        result = ms.local_add(cfg, [{"role": "user", "content": "   "}])
        assert result["ok"] is False


class TestRedactSecretsInChatContent:
    """回归测试：用户/模型在聊天里贴出的真实密钥不该被原样存进本地记忆，
    也不该在"查看记忆"或召回时原样吐出来。"""

    def test_bearer_token_masked(self):
        out = ms.redact_secrets('curl -H "Authorization: Bearer sk-abcdefghijklmnop1234" https://x.com')
        assert "sk-abcdefghijklmnop1234" not in out
        assert "Bearer" in out

    def test_bare_sk_token_masked(self):
        out = ms.redact_secrets("裸露的 key: sk-abcdefghijklmnop1234")
        assert "sk-abcdefghijklmnop1234" not in out

    def test_key_value_pair_masked(self):
        out = ms.redact_secrets('api_key="sk-abcdefghijklmnop1234"')
        assert "sk-abcdefghijklmnop1234" not in out

    def test_normal_text_untouched(self):
        text = "正常聊天内容，不含任何密钥，配置到 WorkBuddy 就行"
        assert ms.redact_secrets(text) == text

    def test_local_add_never_persists_raw_key(self, isolated_memory_store):
        cfg = _cfg()
        ms.local_add(
            cfg,
            [
                {
                    "role": "user",
                    "content": 'curl -H "Authorization: Bearer sk-abcdefghijklmnop1234" https://x.com',
                },
                {"role": "assistant", "content": "ok"},
            ],
        )
        raw_file = isolated_memory_store.LOCAL_STORE.read_text(encoding="utf-8")
        assert "sk-abcdefghijklmnop1234" not in raw_file

    def test_local_list_redacts_pre_existing_raw_entries(self, isolated_memory_store):
        # 模拟"修复上线前就已经存在的明文记录"：直接绕过 local_add 写一条原始记录，
        # 确认展示路径（local_list/local_search）自己也会兜底脱敏，而不是只在写入时管一次。
        isolated_memory_store.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        import json as _json
        raw_item = {
            "id": "legacy-1",
            "user_id": "default",
            "memory": "user: curl -H \"Authorization: Bearer sk-legacyleakedkey1234\" https://x.com",
            "created_at": "2020-01-01T00:00:00+00:00",
        }
        isolated_memory_store.LOCAL_STORE.write_text(_json.dumps(raw_item, ensure_ascii=False) + "\n", encoding="utf-8")
        cfg = _cfg()
        items = ms.local_list(cfg, limit=10)
        assert len(items) == 1
        assert "sk-legacyleakedkey1234" not in items[0]["memory"]


class TestFormatMemoryBlock:
    def test_empty_list_returns_empty_string(self):
        assert ms.format_memory_block([]) == ""

    def test_non_empty_includes_each_item(self):
        block = ms.format_memory_block([{"memory": "记忆一"}, {"memory": "记忆二"}])
        assert "记忆一" in block
        assert "记忆二" in block


class TestSessionStore:
    """短期记忆：逐轮原始记录 + token 统计。"""

    def _turn(self, store, session_id, user, assistant, usage=None, tools=None):
        return ms.record_session_turn(
            _cfg(), session_id, user, assistant, usage or {}, tools or [], 1
        )

    def test_record_and_get_turns(self, isolated_memory_store):
        self._turn(isolated_memory_store, "s1", "问题一", "回答一")
        self._turn(isolated_memory_store, "s1", "问题二", "回答二")
        turns = ms.get_session_turns(_cfg(), "s1")
        assert len(turns) == 2
        assert turns[0]["user_message"] == "问题一"
        assert turns[1]["user_message"] == "问题二"

    def test_missing_session_id_is_rejected(self, isolated_memory_store):
        result = ms.record_session_turn(_cfg(), "", "u", "a")
        assert result["ok"] is False

    def test_session_content_is_redacted(self, isolated_memory_store):
        self._turn(
            isolated_memory_store,
            "s1",
            'curl -H "Authorization: Bearer sk-sessionleak1234567" x',
            "ok",
        )
        raw = isolated_memory_store.SESSION_STORE.read_text(encoding="utf-8")
        assert "sk-sessionleak1234567" not in raw

    def test_list_sessions_aggregates_usage_and_tools(self, isolated_memory_store):
        self._turn(
            isolated_memory_store,
            "s1",
            "问题一",
            "回答一",
            usage={"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 80, "total_tokens": 120},
            tools=["detect_target_agent"],
        )
        self._turn(
            isolated_memory_store,
            "s1",
            "问题二",
            "回答二",
            usage={"prompt_tokens": 50, "completion_tokens": 10, "cached_tokens": 0, "total_tokens": 60},
            tools=["probe_agent_config", "detect_target_agent"],
        )
        sessions = ms.list_sessions(_cfg(), limit=10)
        assert len(sessions) == 1
        s = sessions[0]
        assert s["turns"] == 2
        assert s["usage"]["prompt_tokens"] == 150
        assert s["usage"]["cached_tokens"] == 80
        assert s["usage"]["total_tokens"] == 180
        # 工具名去重后保留
        assert sorted(s["tool_calls"]) == ["detect_target_agent", "probe_agent_config"]
        assert s["title"] == "问题一"  # 用首条用户消息当标题

    def test_sessions_sorted_by_recency(self, isolated_memory_store):
        self._turn(isolated_memory_store, "old", "旧会话", "a")
        self._turn(isolated_memory_store, "new", "新会话", "b")
        sessions = ms.list_sessions(_cfg(), limit=10)
        assert sessions[0]["session_id"] == "new"

    def test_sessions_isolated_per_memory_user(self, isolated_memory_store):
        ms.record_session_turn(_cfg(memory_user_id="alice"), "s1", "u", "a")
        assert len(ms.list_sessions(_cfg(memory_user_id="alice"), limit=10)) == 1
        assert ms.list_sessions(_cfg(memory_user_id="bob"), limit=10) == []

    def test_clear_only_removes_target_user(self, isolated_memory_store):
        ms.record_session_turn(_cfg(memory_user_id="alice"), "s1", "u", "a")
        ms.record_session_turn(_cfg(memory_user_id="bob"), "s2", "u", "a")
        ms.clear_sessions(_cfg(memory_user_id="alice"))
        assert ms.list_sessions(_cfg(memory_user_id="alice"), limit=10) == []
        assert len(ms.list_sessions(_cfg(memory_user_id="bob"), limit=10)) == 1


class TestLongTermMemoryLinksToSession:
    def test_local_add_stores_session_id(self, isolated_memory_store):
        cfg = _cfg()
        ms.local_add(cfg, [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答"}], session_id="s42")
        items = ms.local_list(cfg, limit=10)
        assert items[0]["session_id"] == "s42"

    def test_local_add_without_session_id_is_empty_string(self, isolated_memory_store):
        cfg = _cfg()
        ms.local_add(cfg, [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答"}])
        items = ms.local_list(cfg, limit=10)
        assert items[0]["session_id"] == ""
