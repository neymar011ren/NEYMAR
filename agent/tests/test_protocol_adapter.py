"""protocol_adapter.py 是纯函数，最容易、也最值得覆盖：
一旦这里的转换逻辑跑偏，用户配的第三方 Agent 打通协议转换后可能直接调用失败，
而且很难从「渠道测试报错」反推是转换器的问题还是上游本身的问题。
"""

from __future__ import annotations

import json

import pytest

from protocol_adapter import (
    ProtocolAdaptError,
    check_protocol_fidelity,
    convert_anthropic_to_chat,
    convert_responses_to_chat,
    convert_to_chat_completions,
)


class TestConvertAnthropicToChat:
    def test_system_string_and_plain_turns(self):
        chat = convert_anthropic_to_chat(
            {
                "model": "claude-3-5",
                "system": "You are helpful.",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            }
        )
        assert chat["model"] == "claude-3-5"
        assert chat["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert chat["messages"][1] == {"role": "user", "content": "hi"}
        assert chat["messages"][2]["role"] == "assistant"
        assert chat["messages"][2]["content"] == "hello"

    def test_system_as_content_block_list(self):
        chat = convert_anthropic_to_chat(
            {
                "system": [{"type": "text", "text": "sys-a"}, {"type": "text", "text": "sys-b"}],
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        assert chat["messages"][0] == {"role": "system", "content": "sys-asys-b"}

    def test_user_tool_result_becomes_tool_message(self):
        chat = convert_anthropic_to_chat(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": [{"type": "text", "text": "42"}],
                            }
                        ],
                    }
                ]
            }
        )
        assert chat["messages"] == [
            {"role": "tool", "tool_call_id": "call_1", "content": "42"}
        ]

    def test_user_tool_result_plus_trailing_text_kept(self):
        chat = convert_anthropic_to_chat(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
                            {"type": "text", "text": "接下来呢？"},
                        ],
                    }
                ]
            }
        )
        assert chat["messages"][0] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}
        assert chat["messages"][1] == {"role": "user", "content": "接下来呢？"}

    def test_assistant_tool_use_becomes_tool_calls(self):
        chat = convert_anthropic_to_chat(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "let me check"},
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "list_files",
                                "input": {"path": "."},
                            },
                        ],
                    }
                ]
            }
        )
        msg = chat["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "let me check"
        assert msg["tool_calls"][0]["id"] == "call_1"
        assert msg["tool_calls"][0]["function"]["name"] == "list_files"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "."}

    def test_tools_mapping_prefers_input_schema(self):
        chat = convert_anthropic_to_chat(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "read_file",
                        "description": "read a file",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    }
                ],
            }
        )
        assert chat["tools"][0]["function"]["name"] == "read_file"
        assert chat["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] == "string"
        assert chat["tool_choice"] == "auto"


class TestConvertResponsesToChat:
    def test_instructions_and_string_input(self):
        chat = convert_responses_to_chat(
            {"model": "gpt-4o-mini", "instructions": "be brief", "input": "ping"}
        )
        assert chat["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "ping"},
        ]

    def test_input_list_with_content_parts(self):
        chat = convert_responses_to_chat(
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ]
            }
        )
        assert chat["messages"] == [{"role": "user", "content": "hello"}]

    def test_function_call_then_output_round_trip(self):
        chat = convert_responses_to_chat(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "list_files",
                        "arguments": '{"path": "."}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "a.txt\nb.txt",
                    },
                ]
            }
        )
        assert chat["messages"][0]["role"] == "assistant"
        assert chat["messages"][0]["tool_calls"][0]["id"] == "call_1"
        assert chat["messages"][0]["tool_calls"][0]["function"]["name"] == "list_files"
        assert chat["messages"][1] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "a.txt\nb.txt",
        }

    def test_max_output_tokens_maps_to_max_tokens(self):
        chat = convert_responses_to_chat({"input": "hi", "max_output_tokens": 64})
        assert chat["max_tokens"] == 64

    def test_tools_mapping(self):
        chat = convert_responses_to_chat(
            {
                "input": "hi",
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        assert chat["tools"][0]["function"]["name"] == "read_file"
        assert chat["tool_choice"] == "auto"


class TestConvertToChatCompletions:
    def test_unsupported_protocol_raises(self):
        with pytest.raises(ProtocolAdaptError):
            convert_to_chat_completions("chat_completions", {"messages": []})

    def test_accepts_raw_json_string(self):
        chat = convert_to_chat_completions(
            "anthropic_messages",
            json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
        )
        assert chat["messages"] == [{"role": "user", "content": "hi"}]

    def test_invalid_json_string_raises(self):
        with pytest.raises(ProtocolAdaptError):
            convert_to_chat_completions("anthropic_messages", "{not json")

    def test_empty_messages_after_conversion_raises(self):
        with pytest.raises(ProtocolAdaptError):
            convert_to_chat_completions("responses", {"input": []})


class TestProtocolFidelity:
    """离线校验：复杂的多轮 + 工具调用请求转换后，system/多轮文本/工具参数/
    工具结果这几类内容都不应该被悄悄丢掉。"""

    def test_chat_completions_native_is_skipped(self):
        result = check_protocol_fidelity("chat_completions")
        assert result["ok"] is True
        assert result["skipped"] is True

    def test_anthropic_messages_preserves_all_markers(self):
        result = check_protocol_fidelity("anthropic_messages")
        assert result["ok"] is True
        assert result["missing"] == []
        assert result["has_tools"] is True

    def test_responses_preserves_all_markers(self):
        result = check_protocol_fidelity("responses")
        assert result["ok"] is True
        assert result["missing"] == []
        assert result["has_tools"] is True

    def test_unknown_protocol_reports_error(self):
        result = check_protocol_fidelity("not-a-real-protocol")
        assert result["ok"] is False
        assert "error" in result
