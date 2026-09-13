"""渠道配置流水线状态机。

LLM 负责决策转移和填槽；本模块负责：
1. 强制步骤顺序 / 前置条件（工具门禁）
2. 根据工具结果推进状态
3. 每轮生成进度文案注入上下文

步骤：1 意图 → 2 协议 → 3 安装 → 4 协议转换 → 5 探测 → 6 索取凭证 → 7 写入 → 8 测试
"""

from __future__ import annotations

import json
import threading
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_STORE_PATH = DATA_DIR / "pipeline_states.json"

_lock = threading.Lock()
_session_id_var: ContextVar[str | None] = ContextVar("pipeline_session_id", default=None)

ALWAYS_ALLOWED = frozenset({
    "list_files",
    "read_file",
    "write_file",
    "list_known_agents",
    "detect_target_agent",
    "list_channel_history",
    "protocol_adapt",
})

STEP_LABELS = {
    1: "意图识别（确定目标 Agent）",
    2: "协议调研",
    3: "安装检测",
    4: "协议转换准备",
    5: "本地配置探测",
    6: "索取 Base URL / 模型 / API Key",
    7: "写入渠道配置（预览 → 用户确认）",
    8: "测试验证",
}


@dataclass
class ChannelSetupState:
    target_agent: str | None = None
    target_agent_name: str | None = None
    native_protocol: str | None = None
    detect_attempted: bool = False
    candidates: list[str] = field(default_factory=list)

    # installed | missing | pending_user_install
    install_status: str | None = None

    protocol_briefed: bool = False
    protocol_adapt_done: bool = False

    probe_result: dict[str, Any] | None = None

    has_base_url: bool = False
    has_model: bool = False
    has_api_key: bool = False

    write_previewed: bool = False
    written: bool = False
    tested: bool = False
    last_test_ok: bool | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "probe_result": _probe_summary(self.probe_result),
            "completed_steps": completed_step_numbers(self),
            "next_step": next_step_number(self),
            "progress_text": format_progress(self),
        }


def bind_session(session_id: str | None):
    return _session_id_var.set(session_id)


def reset_session(token) -> None:
    _session_id_var.reset(token)


def current_session_id() -> str | None:
    return _session_id_var.get()


def _session_key(session_id: str | None) -> str:
    return (session_id or "").strip() or "_default"


def _load_store() -> dict[str, Any]:
    if not STATE_STORE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_store(store: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _from_dict(raw: dict[str, Any] | None) -> ChannelSetupState:
    if not isinstance(raw, dict):
        return ChannelSetupState()
    known = set(ChannelSetupState.__dataclass_fields__)
    payload = {k: v for k, v in raw.items() if k in known}
    try:
        return ChannelSetupState(**payload)
    except TypeError:
        return ChannelSetupState()


def get_state(session_id: str | None = None) -> ChannelSetupState:
    key = _session_key(session_id if session_id is not None else current_session_id())
    with _lock:
        return _from_dict(_load_store().get(key))


def save_state(state: ChannelSetupState, session_id: str | None = None) -> None:
    key = _session_key(session_id if session_id is not None else current_session_id())
    with _lock:
        store = _load_store()
        store[key] = asdict(state)
        _save_store(store)


def clear_state(session_id: str | None = None) -> None:
    key = _session_key(session_id if session_id is not None else current_session_id())
    with _lock:
        store = _load_store()
        if key in store:
            del store[key]
            _save_store(store)


def _probe_summary(probe: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(probe, dict):
        return None
    return {
        "ok": probe.get("ok"),
        "agent_id": probe.get("agent_id"),
        "any_config_found": probe.get("any_config_found"),
        "native_protocol": probe.get("native_protocol"),
    }


def _needs_protocol_adapt(native: str | None) -> bool:
    return (native or "") in {"anthropic_messages", "responses"}


def completed_step_numbers(state: ChannelSetupState) -> list[int]:
    done: list[int] = []
    if state.target_agent:
        done.append(1)
    if state.target_agent and (state.protocol_briefed or state.native_protocol):
        done.append(2)
    if state.install_status is not None:
        done.append(3)
    if state.target_agent and state.install_status is not None and (
        state.protocol_adapt_done or not _needs_protocol_adapt(state.native_protocol)
    ):
        done.append(4)
    if state.probe_result is not None:
        done.append(5)
    if state.has_base_url and state.has_model and state.has_api_key:
        done.append(6)
    if state.write_previewed or state.written:
        done.append(7)
    if state.tested:
        done.append(8)
    return done


def next_step_number(state: ChannelSetupState) -> int | None:
    done = set(completed_step_numbers(state))
    for step in range(1, 9):
        if step not in done:
            return step
    if state.write_previewed and not state.written:
        return 7
    return None


def format_progress(state: ChannelSetupState) -> str:
    done = completed_step_numbers(state)
    nxt = next_step_number(state)
    lines = [
        "【渠道配置流水线 · 由代码强制，勿跳步臆造】",
        f"已完成步骤：{done if done else '无'}（共 8 步）"
        + (f"；下一步是 {nxt} — {STEP_LABELS[nxt]}" if nxt else "；流水线已完成"),
    ]
    if state.target_agent:
        lines.append(
            f"目标 Agent：{state.target_agent_name or state.target_agent}"
            f"（id={state.target_agent}，协议={state.native_protocol or '未知'}）"
        )
    else:
        lines.append("目标 Agent：未确定（禁止写入 / 测试 / 探测 / 安装准备）")

    if state.install_status:
        lines.append(f"安装状态：{state.install_status}")
    if state.probe_result is not None:
        found = state.probe_result.get("any_config_found")
        lines.append("本地探测：已完成" + ("（发现配置）" if found else "（未发现配置文件）"))

    creds = [n for n, ok in (("base_url", state.has_base_url), ("model", state.has_model), ("api_key", state.has_api_key)) if ok]
    lines.append(f"已收集凭证字段：{', '.join(creds) if creds else '无'}")

    if state.written:
        lines.append("写入：已确认落盘")
    elif state.write_previewed:
        lines.append("写入：已出预览，待用户点击确认")
    else:
        lines.append("写入：未开始")

    if state.tested:
        if state.last_test_ok is True:
            lines.append("测试：已完成（通过）")
        elif state.last_test_ok is False:
            lines.append("测试：已完成（未通过）")
        else:
            lines.append("测试：已完成")
    else:
        lines.append("测试：未开始")

    if nxt is None:
        lines.append("下一步：流水线已完成。若用户有新目标，请重新 detect_target_agent。")
    else:
        lines.append(_next_hint(state, nxt))

    blocked = list_blocked_tools(state)
    if blocked:
        lines.append(f"当前禁止调用：{', '.join(blocked)}")
    return "\n".join(lines)


def _next_hint(state: ChannelSetupState, nxt: int) -> str:
    hints = {
        1: "请调用 detect_target_agent；若无唯一命中则 list_known_agents 并请用户确认。",
        2: "结合 native_protocol 用一两句话说明是否需要协议转换；需要最新资料可联网搜索。",
        3: "请调用 check_agent_installed；未安装则 prepare_agent_install 并让用户点安装按钮。",
        4: "原生协议非 Chat Completions 时，调用 protocol_adapt（可先 execute=false）。",
        5: "请调用 probe_agent_config 探测本地配置文件。",
        6: "向用户索取 Base URL、模型名称、API Key（不要编造密钥）。",
        7: (
            "提醒用户点击界面「确认写入」；written=true 前不要声称已写入，也不要重复刷预览。"
            if state.write_previewed and not state.written
            else "凭证齐套后调用 write_agent_channel_config 生成预览（不会落盘）。"
        ),
        8: "用户确认写入后，调用 test_agent_channel 验证连通性与协议转换。",
    }
    return f"建议：{hints.get(nxt, '')}"


def _agent_id_from_args(arguments: dict[str, Any]) -> str | None:
    raw = arguments.get("agent_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _reason_blocked(name: str, args: dict[str, Any], state: ChannelSetupState) -> str | None:
    """纯规则判断，不拼 progress，避免与 format_progress 互相递归。"""
    agent_id = _agent_id_from_args(args)
    gated = {
        "check_agent_installed",
        "prepare_agent_install",
        "probe_agent_config",
        "write_agent_channel_config",
        "test_agent_channel",
    }
    if name not in gated and name not in ALWAYS_ALLOWED:
        # 未知工具不在此拦截
        return None
    if name in ALWAYS_ALLOWED:
        return None

    if name in gated:
        if state.target_agent is None and not agent_id:
            if name in {"check_agent_installed", "prepare_agent_install"} and state.detect_attempted:
                return "请在参数中提供 agent_id（来自 detect_target_agent 的候选），以确认唯一目标。"
            return "尚未确定目标 Agent。请先调用 detect_target_agent，必要时 list_known_agents 并请用户确认。"
        if state.target_agent and agent_id and agent_id != state.target_agent:
            return (
                f"目标 Agent 已锁定为 {state.target_agent}，与本次参数 agent_id={agent_id} 不一致。"
                "如需换目标，请重新 detect_target_agent。"
            )

    if name == "prepare_agent_install":
        if state.install_status == "installed":
            return "目标已安装，无需 prepare_agent_install。请继续 probe_agent_config。"
        if state.install_status is None:
            return "请先调用 check_agent_installed，再决定是否安装。"

    if name == "probe_agent_config":
        if state.install_status is None:
            return "请先调用 check_agent_installed（安装检测必须在本地探测之前）。"
        if state.install_status == "missing":
            return "目标尚未安装。请先 prepare_agent_install，并等待用户完成安装后再 probe。"
        if state.install_status == "pending_user_install":
            return (
                "安装方案已给出，仍在等待用户确认安装完成。请先让用户点击安装按钮或确认已安装，"
                "然后再次 check_agent_installed。"
            )

    if name == "write_agent_channel_config":
        if state.target_agent is None:
            return "target_agent 未确定，拒绝写入预览。请先完成步骤 1（detect_target_agent / 确认唯一目标）。"
        if state.install_status != "installed":
            return "写入前必须确认已安装（install_status=installed）。请先 check_agent_installed / 完成安装。"
        if state.probe_result is None:
            return "写入前必须先 probe_agent_config 完成本地探测。"
        if not (args.get("base_url") and args.get("model") and args.get("api_key")):
            return "缺少 base_url / model / api_key。请先向用户索取齐套再调用。"

    if name == "test_agent_channel":
        if not state.written:
            return (
                "测试前必须先完成真实写入（用户点击「确认写入」后 written=true）。"
                "write_agent_channel_config 只是预览，不能当作已写入。"
            )
        if state.target_agent is None:
            return "目标 Agent 丢失，请重新 detect_target_agent。"

    return None


def list_blocked_tools(state: ChannelSetupState) -> list[str]:
    blocked: list[str] = []
    for name in (
        "check_agent_installed",
        "prepare_agent_install",
        "probe_agent_config",
        "write_agent_channel_config",
        "test_agent_channel",
    ):
        args: dict[str, Any] = {"agent_id": state.target_agent} if state.target_agent else {}
        if name in {"write_agent_channel_config", "test_agent_channel"}:
            args.update({"base_url": "x", "model": "y", "api_key": "z"})
        if _reason_blocked(name, args, state):
            blocked.append(name)
    return blocked


def guard_tool(
    name: str,
    arguments: dict[str, Any] | None,
    state: ChannelSetupState | None = None,
) -> dict[str, Any] | None:
    if name in ALWAYS_ALLOWED:
        return None
    state = state or get_state()
    args = arguments or {}
    reason = _reason_blocked(name, args, state)
    if not reason:
        return None
    need_step = None
    if "步骤 1" in reason or "尚未确定目标" in reason or "detect_target_agent" in reason and "重新" not in reason:
        need_step = 1
    elif "check_agent_installed" in reason or "尚未安装" in reason or "安装方案" in reason or "install_status" in reason:
        need_step = 3
    elif "probe_agent_config" in reason and "写入前" in reason:
        need_step = 5
    elif "base_url" in reason:
        need_step = 6
    elif "written" in reason or "确认写入" in reason:
        need_step = 7
    payload: dict[str, Any] = {
        "ok": False,
        "rejected_by_pipeline": True,
        "tool": name,
        "error": reason,
        "progress": format_progress(state),
    }
    if need_step is not None:
        payload["need_step"] = need_step
        payload["need_step_label"] = STEP_LABELS.get(need_step)
    return payload


def _parse_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_matches(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("matches")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def apply_tool_result(
    name: str,
    arguments: dict[str, Any] | None,
    result_text: str,
    state: ChannelSetupState | None = None,
    session_id: str | None = None,
) -> ChannelSetupState:
    state = state or get_state(session_id)
    args = arguments or {}
    data = _parse_json(result_text) or {}
    if data.get("rejected_by_pipeline"):
        return state

    if name == "detect_target_agent":
        state.detect_attempted = True
        matches = _extract_matches(data)
        state.candidates = []
        for item in matches:
            cid = str(item.get("id") or item.get("agent_id") or "").strip()
            if cid:
                state.candidates.append(cid)
        if len(matches) == 1:
            m0 = matches[0]
            state.target_agent = str(m0.get("id") or m0.get("agent_id") or "").strip() or None
            state.target_agent_name = str(m0.get("name") or "") or None
            state.native_protocol = str(m0.get("native_protocol") or "").strip() or None
            state.protocol_briefed = bool(state.native_protocol)
            state.protocol_adapt_done = not _needs_protocol_adapt(state.native_protocol)
            state.install_status = None
            state.probe_result = None
            state.write_previewed = False
            state.written = False
            state.tested = False
            state.last_test_ok = None
            state.has_base_url = False
            state.has_model = False
            state.has_api_key = False
        else:
            state.target_agent = None
            state.target_agent_name = None
            state.native_protocol = None
            state.protocol_briefed = False
            state.protocol_adapt_done = False

    elif name in {"check_agent_installed", "prepare_agent_install", "probe_agent_config"}:
        agent_id = _agent_id_from_args(args)
        if not agent_id and data.get("agent_id"):
            agent_id = str(data.get("agent_id")).strip() or None
        if agent_id and state.target_agent is None and state.detect_attempted:
            state.target_agent = agent_id
            state.target_agent_name = str(data.get("name") or agent_id)
            proto = data.get("native_protocol")
            if proto:
                state.native_protocol = str(proto)
                state.protocol_briefed = True
                state.protocol_adapt_done = not _needs_protocol_adapt(state.native_protocol)

        if name == "check_agent_installed" and data.get("ok"):
            state.install_status = "installed" if data.get("installed") else "missing"
            if data.get("name"):
                state.target_agent_name = str(data.get("name"))

        if name == "prepare_agent_install" and data.get("ok"):
            if data.get("already_installed") or data.get("installed"):
                state.install_status = "installed"
            elif data.get("offer_install"):
                state.install_status = "pending_user_install"

        if name == "probe_agent_config" and data.get("ok"):
            any_found = data.get("any_config_found")
            if any_found is None:
                findings = data.get("findings")
                if isinstance(findings, list):
                    any_found = any(
                        isinstance(f, dict) and (f.get("exists") or f.get("found"))
                        for f in findings
                    )
                else:
                    any_found = False
            state.probe_result = {
                "ok": True,
                "agent_id": data.get("agent_id") or state.target_agent,
                "any_config_found": bool(any_found),
                "native_protocol": data.get("native_protocol") or state.native_protocol,
            }
            if data.get("native_protocol"):
                state.native_protocol = str(data.get("native_protocol"))
                state.protocol_briefed = True

    elif name == "protocol_adapt":
        if data.get("ok") is not False:
            state.protocol_adapt_done = True
            state.protocol_briefed = True

    elif name == "write_agent_channel_config":
        if data.get("ok") and data.get("confirm_required"):
            state.write_previewed = True
            state.has_base_url = bool(args.get("base_url"))
            state.has_model = bool(args.get("model"))
            state.has_api_key = bool(args.get("api_key"))

    elif name == "test_agent_channel":
        if data.get("ok") is not None:
            state.tested = True
            steps = data.get("steps")
            if isinstance(steps, list) and steps:
                state.last_test_ok = all(bool(s.get("ok", True)) for s in steps if isinstance(s, dict))
            else:
                state.last_test_ok = bool(data.get("ok"))

    save_state(state, session_id)
    return state


def mark_written(
    agent_id: str,
    *,
    session_id: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> ChannelSetupState:
    state = get_state(session_id)
    agent_id = (agent_id or "").strip()
    if agent_id:
        state.target_agent = agent_id
    state.written = True
    state.write_previewed = True
    state.install_status = state.install_status or "installed"
    if base_url:
        state.has_base_url = True
    if model:
        state.has_model = True
    state.has_api_key = True
    state.tested = False
    state.last_test_ok = None
    save_state(state, session_id)
    return state


def mark_installed(agent_id: str, *, session_id: str | None = None) -> ChannelSetupState:
    state = get_state(session_id)
    agent_id = (agent_id or "").strip()
    if agent_id and not state.target_agent:
        state.target_agent = agent_id
    if agent_id and state.target_agent and agent_id != state.target_agent:
        save_state(state, session_id)
        return state
    state.install_status = "installed"
    save_state(state, session_id)
    return state


def progress_system_message(state: ChannelSetupState | None = None) -> dict[str, str]:
    return {"role": "system", "content": format_progress(state or get_state())}
