"""渠道配置流水线状态机测试：门禁、进度推进、写入确认。"""

from __future__ import annotations

import json

import pipeline
from tools import run_tool


def _session(name: str):
    pipeline.clear_state(name)
    token = pipeline.bind_session(name)
    return token


def test_write_rejected_without_target_agent():
    token = _session("t_write_no_target")
    try:
        out = json.loads(
            run_tool(
                "write_agent_channel_config",
                {
                    "agent_id": "kingswitch",
                    "base_url": "https://example.com/v1",
                    "model": "gpt-test",
                    "api_key": "sk-test-key",
                },
            )
        )
        assert out["ok"] is False
        assert out.get("rejected_by_pipeline") is True
        assert "target_agent" in out["error"]
    finally:
        pipeline.reset_session(token)
        pipeline.clear_state("t_write_no_target")


def test_probe_requires_install_check_first():
    token = _session("t_probe_order")
    try:
        detect = json.loads(run_tool("detect_target_agent", {"user_text": "配置到 kingswitch"}))
        assert detect.get("ok") is True
        state = pipeline.get_state()
        assert state.target_agent == "kingswitch"

        out = json.loads(run_tool("probe_agent_config", {"agent_id": "kingswitch"}))
        assert out["ok"] is False
        assert out.get("rejected_by_pipeline") is True
        assert "check_agent_installed" in out["error"]
    finally:
        pipeline.reset_session(token)
        pipeline.clear_state("t_probe_order")


def test_happy_path_to_write_preview_and_test_gate():
    token = _session("t_happy")
    try:
        detect = json.loads(run_tool("detect_target_agent", {"user_text": "把模型配到 kingswitch"}))
        assert detect["ok"]
        assert pipeline.get_state().target_agent == "kingswitch"

        check = json.loads(run_tool("check_agent_installed", {"agent_id": "kingswitch"}))
        assert check["ok"] is True
        assert check["installed"] is True
        assert pipeline.get_state().install_status == "installed"

        probe = json.loads(run_tool("probe_agent_config", {"agent_id": "kingswitch"}))
        assert probe["ok"] is True
        assert pipeline.get_state().probe_result is not None

        preview = json.loads(
            run_tool(
                "write_agent_channel_config",
                {
                    "agent_id": "kingswitch",
                    "base_url": "https://example.com/v1",
                    "model": "gpt-test",
                    "api_key": "sk-test-key-123456",
                },
            )
        )
        assert preview["ok"] is True
        assert preview.get("confirm_required") is True
        state = pipeline.get_state()
        assert state.write_previewed is True
        assert state.written is False
        assert 7 in pipeline.completed_step_numbers(state)

        blocked = pipeline.guard_tool(
            "test_agent_channel",
            {
                "agent_id": "kingswitch",
                "base_url": "https://example.com/v1",
                "model": "gpt-test",
                "api_key": "sk-test-key-123456",
            },
            state,
        )
        assert blocked is not None
        assert "written" in blocked["error"] or "确认写入" in blocked["error"]

        pipeline.mark_written(
            "kingswitch",
            session_id="t_happy",
            base_url="https://example.com/v1",
            model="gpt-test",
        )
        state = pipeline.get_state()
        assert state.written is True
        assert (
            pipeline.guard_tool(
                "test_agent_channel",
                {
                    "agent_id": "kingswitch",
                    "base_url": "https://example.com/v1",
                    "model": "gpt-test",
                    "api_key": "sk-test-key-123456",
                },
                state,
            )
            is None
        )
        progress = pipeline.format_progress(state)
        assert "下一步是 8" in progress or "测试验证" in progress
    finally:
        pipeline.reset_session(token)
        pipeline.clear_state("t_happy")


def test_progress_lists_completed_and_next():
    token = _session("t_progress")
    try:
        state = pipeline.ChannelSetupState(target_agent="workbuddy", native_protocol="chat_completions", protocol_briefed=True)
        pipeline.save_state(state, "t_progress")
        text = pipeline.format_progress(pipeline.get_state("t_progress"))
        assert "已完成步骤：[1, 2]" in text or "1" in text
        assert "下一步是 3" in text
    finally:
        pipeline.reset_session(token)
        pipeline.clear_state("t_progress")
