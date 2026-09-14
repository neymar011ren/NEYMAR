from __future__ import annotations

from config_store import AgentConfig
from llm import (
    _build_chat_payload,
    _split_answer_and_refs,
    _to_anthropic_messages,
    _to_responses_input,
    accumulate_usage,
    normalize_usage,
)


class TestNormalizeUsage:
    """三种协议的 usage 字段命名完全不同，归一化错了前端 token 统计就是错的。"""

    def test_chat_completions_shape(self):
        usage = normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
            "chat_completions",
        )
        assert usage == {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 80,
            "total_tokens": 120,
        }

    def test_responses_shape(self):
        usage = normalize_usage(
            {
                "input_tokens": 200,
                "output_tokens": 30,
                "total_tokens": 230,
                "input_tokens_details": {"cached_tokens": 150},
            },
            "responses",
        )
        assert usage["prompt_tokens"] == 200
        assert usage["completion_tokens"] == 30
        assert usage["cached_tokens"] == 150

    def test_anthropic_shape(self):
        usage = normalize_usage(
            {"input_tokens": 300, "output_tokens": 40, "cache_read_input_tokens": 250},
            "anthropic_messages",
        )
        assert usage["prompt_tokens"] == 300
        assert usage["completion_tokens"] == 40
        assert usage["cached_tokens"] == 250
        # 上游没给 total 时用 prompt + completion 兜底
        assert usage["total_tokens"] == 340

    def test_missing_or_malformed_usage_is_zeroed(self):
        assert normalize_usage(None, "chat_completions")["total_tokens"] == 0
        assert normalize_usage("not a dict", "chat_completions")["total_tokens"] == 0
        assert normalize_usage({"prompt_tokens": "abc"}, "chat_completions")["prompt_tokens"] == 0

    def test_accumulate_across_rounds(self):
        total = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
        accumulate_usage(total, {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 5, "total_tokens": 12})
        accumulate_usage(total, {"prompt_tokens": 20, "completion_tokens": 3, "cached_tokens": 0, "total_tokens": 23})
        assert total == {
            "prompt_tokens": 30,
            "completion_tokens": 5,
            "cached_tokens": 5,
            "total_tokens": 35,
        }


class TestBuildChatPayload:
    def test_stream_requests_usage_include(self):
        cfg = AgentConfig(api_key="k", model="m")
        payload = _build_chat_payload(cfg, [{"role": "user", "content": "hi"}], stream=True)
        # 回归测试：没有这个字段，多数 OpenAI 兼容网关不会在流式响应里带 usage，
        # web_search_queries 统计就永远读不到。
        assert payload["stream_options"] == {"include_usage": True}

    def test_non_stream_does_not_set_stream_options(self):
        cfg = AgentConfig(api_key="k", model="m")
        payload = _build_chat_payload(cfg, [{"role": "user", "content": "hi"}], stream=False)
        assert "stream_options" not in payload

    def test_tools_only_included_when_enabled(self):
        cfg = AgentConfig(api_key="k", model="m", enable_tools=False, enable_web_search=False)
        payload = _build_chat_payload(cfg, [], stream=False)
        assert "tools" not in payload


class TestToAnthropicMessages:
    def test_system_extracted_separately(self):
        system, converted = _to_anthropic_messages(
            [{"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"}]
        )
        assert system == "be nice"
        assert converted == [{"role": "user", "content": "hi"}]

    def test_tool_message_becomes_user_tool_result(self):
        _, converted = _to_anthropic_messages(
            [{"role": "tool", "tool_call_id": "call_1", "content": "42"}]
        )
        assert converted[0]["role"] == "user"
        assert converted[0]["content"][0]["type"] == "tool_result"
        assert converted[0]["content"][0]["tool_use_id"] == "call_1"

    def test_assistant_with_tool_calls_becomes_tool_use_blocks(self):
        _, converted = _to_anthropic_messages(
            [
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "list_files", "arguments": '{"path": "."}'},
                        }
                    ],
                }
            ]
        )
        blocks = converted[0]["content"]
        assert blocks[0] == {"type": "text", "text": "checking"}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "list_files"
        assert blocks[1]["input"] == {"path": "."}


class TestToResponsesInput:
    def test_basic_roles(self):
        items = _to_responses_input(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        )
        assert items[0]["role"] == "system"
        assert items[1]["role"] == "user"
        assert items[2]["role"] == "assistant"

    def test_tool_message_becomes_function_call_output(self):
        items = _to_responses_input([{"role": "tool", "tool_call_id": "call_1", "content": "42"}])
        assert items[0]["type"] == "function_call_output"
        assert items[0]["call_id"] == "call_1"
        assert items[0]["output"] == "42"


class TestSplitAnswerAndRefs:
    def test_no_refs_section_returns_whole_text(self):
        body, refs = _split_answer_and_refs("这是一段普通回答，没有引用。")
        assert body == "这是一段普通回答，没有引用。"
        assert refs == []

    def test_splits_reference_section_with_markdown_links(self):
        text = "正文内容\n\n---\n\n参考资料：\n1. [官方文档](https://example.com/docs)\n"
        body, refs = _split_answer_and_refs(text)
        assert body == "正文内容"
        assert refs == [{"title": "官方文档", "url": "https://example.com/docs"}]

    def test_empty_text(self):
        assert _split_answer_and_refs("") == ("", [])
