"""KingSwitch：探测 / 写入 / 测试第三方 Agent 的模型渠道配置。

写入分两段：
- write_agent_channel_config（LLM 工具）只做校验 + 生成"预览"，不落盘。
- execute_write_agent_channel_config 才是真正写文件的实现，只能通过
  /api/agents/write-config 这个由前端"确认写入"按钮触发的接口调用，
  模型自己没有办法绕过用户确认直接把 Key 写进真实配置文件。
  这个两段式设计和 agent_install.py 里 prepare_agent_install / run_agent_install
  的关系完全一致。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from agent_profiles import (
    AGENT_PROFILES,
    AgentProfile,
    detect_from_text,
    forge_config_path,
    list_profiles_public,
    profile_paths,
    resolve_profile,
)
from config_store import AgentConfig, load_config, mask_secret, save_config
from protocol_adapter import ProtocolAdaptError, convert_to_chat_completions
from memory_service import get_channel_history, record_channel_write

MAX_OUT = 12_000


def _json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + "\n...[截断]"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(path.suffix + f".bak.{stamp}")
    shutil.copy2(path, backup)
    return str(backup)


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k).lower()
            if any(s in key for s in ("key", "token", "secret", "password", "auth")):
                if isinstance(v, str):
                    out[k] = mask_secret(v)
                else:
                    out[k] = "***"
            else:
                out[k] = _redact_obj(v)
        return out
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    return obj


# yaml / toml / conf 等非 JSON 格式没有结构化解析，只能按行做正则脱敏。
# 故意宁可"误伤"一些包含 key/token 等子串的无关字段名，也不要漏掉真正的密钥——
# 过度脱敏最多是显示效果差一点，漏脱敏就是明文密钥外泄。
_SENSITIVE_LINE_RE = re.compile(
    r"""(?im)
    ^(?P<prefix>\s*[\"']?[\w.\-]*(?:key|token|secret|password|auth)[\w.\-]*[\"']?\s*[:=]\s*)
    (?P<quote>["']?)
    (?P<value>[^"'\n#]+?)
    \s*(?P=quote)\s*(?P<trail>[#].*)?$
    """,
    re.VERBOSE,
)


def _redact_text(raw: str) -> str:
    """对 yaml/toml 等非 JSON 格式的配置原文做行级脱敏。"""

    def _mask_line(match: "re.Match[str]") -> str:
        value = match.group("value").strip()
        if not value:
            return match.group(0)
        masked = mask_secret(value)
        quote = match.group("quote") or ""
        trail = match.group("trail") or ""
        return f"{match.group('prefix')}{quote}{masked}{quote}{(' ' + trail) if trail else ''}"

    return _SENSITIVE_LINE_RE.sub(_mask_line, raw)


def list_known_agents() -> str:
    return _json({"ok": True, "agents": list_profiles_public()})


def detect_target_agent(user_text: str) -> str:
    matches = detect_from_text(user_text)
    return _json(
        {
            "ok": True,
            "matches": matches,
            "hint": (
                "若 matches 为空，请向用户澄清目标 Agent 名称；"
                "可用 list_known_agents 展示可配置列表。"
            ),
            "next_steps": [
                "若命中多个，向用户确认唯一目标",
                "联网搜索该 Agent 支持的协议（可结合档案中的 native_protocol）",
                "调用 probe_agent_config 探测本地配置文件",
            ],
        }
    )


def probe_agent_config(agent_id: str) -> str:
    profile = resolve_profile(agent_id)
    if not profile:
        return _json({"ok": False, "error": f"未知 agent_id: {agent_id}", "known": list(AGENT_PROFILES)})
    paths = profile_paths(profile)
    findings = []
    for path in paths:
        item: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "is_file": path.is_file() if path.exists() else False,
        }
        if path.exists() and path.is_file():
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                item["size"] = len(raw)
                if path.suffix.lower() in {".json"} or profile.format in {"json", "env_json"}:
                    try:
                        item["preview"] = _redact_obj(_read_json(path))
                    except Exception:
                        item["preview_text"] = _redact_text(raw[:1500])
                else:
                    item["preview_text"] = _redact_text(raw[:1500])
            except Exception as exc:  # noqa: BLE001
                item["read_error"] = str(exc)
        findings.append(item)
    return _json(
        {
            "ok": True,
            "agent_id": profile.id,
            "name": profile.name,
            "native_protocol": profile.native_protocol,
            "protocol_notes": profile.protocol_notes,
            "docs_hint": profile.docs_hint,
            "format": profile.format,
            "findings": findings,
            "any_config_found": any(f.get("exists") for f in findings),
        }
    )


def _load_existing_dict(path: Path) -> tuple[dict[str, Any], str | None]:
    """安全读取已存在的 JSON 配置，返回 (数据, 警告信息)。

    文件存在但解析失败 / 不是对象时，不再静默丢弃原内容后直接覆盖——
    调用方会把 warning 带回给用户，写入前仍会先 _backup 原文件。
    """
    if not path.exists():
        return {}, None
    try:
        loaded = _read_json(path)
    except Exception as exc:  # noqa: BLE001
        return {}, f"现有文件解析失败（{exc}），本次将基于空配置重建；原文件已在写入前自动备份，可从 .bak 文件恢复。"
    if isinstance(loaded, dict):
        return loaded, None
    return {}, "现有文件内容不是 JSON 对象，本次将基于空配置重建；原文件已在写入前自动备份，可从 .bak 文件恢复。"


def _load_workbuddy_dict(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {"models": []}, None
    try:
        loaded = _read_json(path)
    except Exception as exc:  # noqa: BLE001
        return {"models": []}, f"现有文件解析失败（{exc}），本次将基于空配置重建；原文件已在写入前自动备份，可从 .bak 文件恢复。"
    if isinstance(loaded, dict):
        return loaded, None
    if isinstance(loaded, list):
        return {"models": loaded}, None
    return {"models": []}, "现有文件内容既不是对象也不是数组，本次将基于空配置重建；原文件已在写入前自动备份，可从 .bak 文件恢复。"


def _finalize_json_write(path: Path, data: Any, warning: str | None, **extra: Any) -> dict[str, Any]:
    """7 个写入器里，除了 3 个非 JSON 格式的（yaml/ini 风格/toml），
    其余都是"读 JSON → 改几个字段 → backup → 写回"，收尾部分统一在这里，
    避免每个写入器各自重复一遍、也各自重复一遍忘记 warning 字段的风险。"""
    backup = _backup(path)
    _write_json(path, data)
    result: dict[str, Any] = {"path": str(path), "backup": backup, **extra}
    if warning:
        result["warning"] = warning
    return result


def _finalize_text_write(path: Path, content: str, **extra: Any) -> dict[str, Any]:
    """yaml / ini 风格 / toml 这几个非 JSON 写入器共用的收尾：backup → mkdir → 写文本。"""
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "backup": backup, **extra}


def _write_claude_code(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    data, warning = _load_existing_dict(path)
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    env = dict(env)
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env["ANTHROPIC_API_KEY"] = api_key
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    if model:
        env["ANTHROPIC_MODEL"] = model
    data["env"] = env
    return _finalize_json_write(path, data, warning, keys_written=list(env.keys()))


def _write_workbuddy(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    data, warning = _load_workbuddy_dict(path)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        models = []
    entry = {
        "id": model or "custom-model",
        "name": model or "custom-model",
        "url": url,
        "apiKey": api_key,
        "type": "openai",
    }
    replaced = False
    for i, item in enumerate(models):
        if isinstance(item, dict) and item.get("id") == entry["id"]:
            models[i] = {**item, **entry}
            replaced = True
            break
    if not replaced:
        models.append(entry)
    data = {**data, "models": models}
    return _finalize_json_write(path, data, warning, model_id=entry["id"], url=url)


def _write_continue_yaml(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    # 最小可用 YAML，避免强依赖 PyYAML
    content = (
        f"models:\n"
        f"  - title: {model or 'custom'}\n"
        f"    provider: openai\n"
        f"    model: {model or 'custom'}\n"
        f"    apiBase: {base_url.rstrip('/')}\n"
        f"    apiKey: {api_key}\n"
    )
    return _finalize_text_write(path, content, format="yaml")


def _write_aider(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    lines = []
    if path.exists():
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        skip_keys = {"openai-api-base", "openai-api-key", "model"}
        for line in raw:
            key = line.split(":", 1)[0].strip()
            if key in skip_keys:
                continue
            lines.append(line)
    lines.extend(
        [
            f"openai-api-base: {base_url.rstrip('/')}",
            f"openai-api-key: {api_key}",
            f"model: {model or 'openai/custom'}",
        ]
    )
    content = "\n".join(lines).rstrip() + "\n"
    return _finalize_text_write(path, content)


def _write_codex_toml(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    provider = "forge_custom"
    block = (
        f"\nmodel = \"{model or 'custom'}\"\n"
        f"model_provider = \"{provider}\"\n\n"
        f"[model_providers.{provider}]\n"
        f"name = \"KingSwitch Custom\"\n"
        f"base_url = \"{base_url.rstrip('/')}\"\n"
        f"wire_api = \"chat\"\n"
        f"# api_key 建议写入环境变量 OPENAI_API_KEY 或 auth.json；此处写入便于本地联调\n"
        f"api_key = \"{api_key}\"\n"
    )
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    # 去掉旧 forge_custom 段
    cleaned = re.sub(
        r"\n\[model_providers\.forge_custom\][\s\S]*?(?=\n\[|\Z)",
        "\n",
        existing,
    )
    content = cleaned.rstrip() + "\n" + block
    return _finalize_text_write(path, content, provider=provider)


def _write_opencode(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    data, warning = _load_existing_dict(path)
    provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
    provider = dict(provider)
    provider["forge-custom"] = {
        "npm": "@ai-sdk/openai-compatible",
        "options": {
            "baseURL": base_url.rstrip("/"),
            "apiKey": api_key,
        },
        "models": {
            model or "custom": {"name": model or "custom"},
        },
    }
    data["provider"] = provider
    if model:
        data["model"] = f"forge-custom/{model}"
    return _finalize_json_write(path, data, warning)


def _write_cline(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    data, warning = _load_existing_dict(path)
    data["apiProvider"] = "openai"
    data["openAiBaseUrl"] = base_url.rstrip("/")
    data["openAiApiKey"] = api_key
    data["openAiModelId"] = model or "custom"
    return _finalize_json_write(path, data, warning)


def _write_forge(base_url: str, model: str, api_key: str, protocol: str | None) -> dict[str, Any]:
    current = load_config()
    updates: dict[str, Any] = {
        "api_base_url": base_url.rstrip("/"),
        "model": model or current.model,
    }
    if api_key:
        updates["api_key"] = api_key
    if protocol in {"chat_completions", "responses", "anthropic_messages"}:
        updates["protocol"] = protocol
    saved = save_config(AgentConfig.model_validate({**current.model_dump(), **updates}))
    return {
        "path": str(forge_config_path()),
        "protocol": saved.protocol,
        "model": saved.model,
        "api_base_url": saved.api_base_url,
        "api_key_masked": mask_secret(saved.api_key),
    }


def _select_target_path(profile: AgentProfile, create_if_missing: bool) -> tuple[Path | None, bool, str | None]:
    """纯函数：只决定应该操作哪个候选路径，不做任何磁盘写入。

    返回 (目标路径, 是否需要新建文件, 错误信息)。写入预览 (write_agent_channel_config)
    和真正执行写入 (execute_write_agent_channel_config) 都调用这一个函数，
    保证"预览时说的路径"和"确认后实际写的路径"一定一致。
    """
    paths = profile_paths(profile)
    if not paths:
        return None, False, "该 Agent 没有可写入的本地配置路径"
    target = next((p for p in paths if p.exists()), None)
    if target is None:
        if not create_if_missing:
            return None, False, f"本地未找到配置文件，且 create_if_missing=false；候选路径：{[str(p) for p in paths]}"
        target = paths[0]
    # 与原逻辑保持一致：continue 优先 yaml、cline 优先 global-settings.json
    if profile.id == "continue":
        yaml_paths = [p for p in paths if p.suffix in {".yaml", ".yml"}]
        if yaml_paths:
            target = yaml_paths[0]
    elif profile.id == "cline":
        preferred = [p for p in paths if p.name == "global-settings.json"]
        if preferred:
            target = preferred[0]
    return target, not target.exists(), None


def _resolve_write_target(agent_id: str, base_url: str, model: str, api_key: str, create_if_missing: bool):
    """公共校验 + 目标路径解析，preview 和 execute 共用，避免两边校验逻辑跑偏。

    返回 (profile, target_path, will_create, error_json_or_None)。
    """
    profile = resolve_profile(agent_id)
    if not profile:
        return None, None, False, _json({"ok": False, "error": f"未知 agent_id: {agent_id}"})
    if not (base_url or "").strip():
        return None, None, False, _json({"ok": False, "error": "base_url 不能为空"})
    if not (model or "").strip():
        return None, None, False, _json({"ok": False, "error": "model 不能为空；请让用户提供实际的模型名称/ID"})
    if not (api_key or "").strip():
        return None, None, False, _json({"ok": False, "error": "api_key 不能为空；请让用户在对话中自行提供密钥"})
    if not profile.write_supported:
        return None, None, False, _json({"ok": False, "error": f"{profile.name} 暂不支持自动写入"})

    if profile.id == "kingswitch":
        target = forge_config_path()
        return profile, target, not target.exists(), None

    target, will_create, error = _select_target_path(profile, create_if_missing)
    if error:
        return profile, None, False, _json({"ok": False, "error": error})
    return profile, target, will_create, None


def write_agent_channel_config(
    agent_id: str,
    base_url: str,
    model: str,
    api_key: str,
    protocol: str | None = None,
    create_if_missing: bool = True,
) -> str:
    """【LLM 工具，只读预览，不落盘】

    校验参数并算出"如果写入会发生什么"（目标路径、是否新建、脱敏后的 Key），
    返回 confirm_required=true 的预览。真正的写入只会在用户于前端点击
    「确认写入」按钮、触发 /api/agents/write-config 之后才会发生——
    模型自己无法跳过这一步直接改动户真实的 Agent 配置文件。
    """
    profile, target, will_create, error = _resolve_write_target(
        agent_id, base_url, model, api_key, create_if_missing
    )
    if error:
        return error

    return _json(
        {
            "ok": True,
            "confirm_required": True,
            "agent_id": profile.id,
            "name": profile.name,
            "native_protocol": profile.native_protocol,
            "target_path": str(target),
            "file_exists": not will_create,
            "will_create_new_file": will_create,
            "base_url": base_url.rstrip("/"),
            "model": model,
            "protocol": protocol,
            "api_key_masked": mask_secret(api_key),
            "message": (
                f"即将把 Base URL / 模型 / API Key 写入 {target}"
                + ("（新建文件）" if will_create else "（会先自动备份原文件）")
                + "。已在界面弹出确认卡片，请提醒用户点击「确认写入」，不要替用户点击或臆造已完成。"
            ),
        }
    )


def execute_write_agent_channel_config(
    agent_id: str,
    base_url: str,
    model: str,
    api_key: str,
    protocol: str | None = None,
    create_if_missing: bool = True,
) -> dict[str, Any]:
    """真正执行写入。只能被 /api/agents/write-config 接口调用（对应前端确认按钮），
    不注册为 LLM 工具。"""
    profile, target, _will_create, error = _resolve_write_target(
        agent_id, base_url, model, api_key, create_if_missing
    )
    if error:
        return json.loads(error)

    try:
        if profile.id == "kingswitch":
            written = _write_forge(base_url, model, api_key, protocol)
            _record_history_best_effort(profile.id, base_url, model, protocol, written["path"], api_key)
            return {"ok": True, "agent_id": profile.id, "written": written}

        target.parent.mkdir(parents=True, exist_ok=True)
        if profile.id == "claude_code":
            written = _write_claude_code(target, base_url, model, api_key)
        elif profile.id == "workbuddy":
            written = _write_workbuddy(target, base_url, model, api_key)
        elif profile.id == "continue":
            written = _write_continue_yaml(target, base_url, model, api_key)
        elif profile.id == "aider":
            written = _write_aider(target, base_url, model, api_key)
        elif profile.id == "codex":
            written = _write_codex_toml(target, base_url, model, api_key)
        elif profile.id == "opencode":
            written = _write_opencode(target, base_url, model, api_key)
        elif profile.id == "cline":
            written = _write_cline(target, base_url, model, api_key)
        else:
            return {"ok": False, "error": f"未实现写入器: {profile.id}"}

        _record_history_best_effort(profile.id, base_url, model, protocol, written["path"], api_key)
        return {
            "ok": True,
            "agent_id": profile.id,
            "name": profile.name,
            "native_protocol": profile.native_protocol,
            "written": written,
            "reminder": "密钥已写入本地配置；请勿把完整 Key 贴回聊天记录。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败: {exc}"}


def _record_history_best_effort(
    agent_id: str, base_url: str, model: str, protocol: str | None, target_path: str, api_key: str
) -> None:
    """记历史失败不该影响这次写入本身已经成功的结果，所以单独兜底吞掉异常。"""
    try:
        record_channel_write(agent_id, base_url, model, protocol, target_path, api_key)
    except Exception:  # noqa: BLE001
        pass


def list_channel_history(agent_id: str = "") -> str:
    """【LLM 工具】查此前 KingSwitch 成功写入过的渠道配置历史（结构化记录，不是聊天记忆）。

    用于回答"上次给这个 Agent 配的是什么" 之类的问题，比翻聊天记录里的自由文本更可靠。
    """
    history = get_channel_history(agent_id.strip() or None, limit=10)
    return _json({"ok": True, "agent_id": agent_id.strip() or None, "history": history})


def _chat_url(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


_PROBE_TOOL_NAME = "get_current_time"


def _build_ping_payload(protocol: str, model: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """构造一次最小的 ping 请求（源协议格式 → chat_completions）。"""
    steps: list[dict[str, Any]] = []
    if protocol == "chat_completions":
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Reply with exactly: ok"},
                {"role": "user", "content": "ping"},
            ],
            "max_tokens": 32,
            "temperature": 0,
        }
        steps.append({"step": "build_chat_completions", "ok": True})
        return payload, steps, None
    if protocol == "anthropic_messages":
        source = {
            "model": model,
            "system": "Reply with exactly: ok",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 32,
            "temperature": 0,
        }
    elif protocol == "responses":
        source = {
            "model": model,
            "instructions": "Reply with exactly: ok",
            "input": "ping",
            "max_output_tokens": 32,
            "temperature": 0,
        }
    else:
        return None, steps, f"不支持的协议: {protocol}"
    try:
        payload = convert_to_chat_completions(protocol, source)
    except ProtocolAdaptError as exc:
        return None, steps, str(exc)
    steps.append(
        {
            "step": "protocol_adapt",
            "direction": f"{protocol} → chat_completions",
            "ok": True,
            "converted_message_count": len(payload.get("messages") or []),
        }
    )
    return payload, steps, None


def _build_tool_call_payload(protocol: str, model: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """构造一次带工具定义的探测请求：不只测"能不能对话"，还测"渠道是否真的支持
    function calling"——很多号称兼容 Chat Completions 的国产网关，文本对话没问题，
    工具调用字段却被吞掉或格式不对，只测 ping 发现不了这个。"""
    steps: list[dict[str, Any]] = []
    tool_prompt = "现在几点了？请调用工具查询，不要凭空回答。"
    tool_system = f"You must call the {_PROBE_TOOL_NAME} tool to answer time-related questions."
    if protocol == "chat_completions":
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": tool_system},
                {"role": "user", "content": tool_prompt},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": _PROBE_TOOL_NAME,
                        "description": "返回当前时间",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
            "max_tokens": 64,
            "temperature": 0,
        }
        steps.append({"step": "build_chat_completions_with_tool", "ok": True})
        return payload, steps, None
    if protocol == "anthropic_messages":
        source = {
            "model": model,
            "system": tool_system,
            "messages": [{"role": "user", "content": tool_prompt}],
            "tools": [
                {
                    "name": _PROBE_TOOL_NAME,
                    "description": "返回当前时间",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "max_tokens": 64,
            "temperature": 0,
        }
    elif protocol == "responses":
        source = {
            "model": model,
            "instructions": tool_system,
            "input": tool_prompt,
            "tools": [
                {
                    "type": "function",
                    "name": _PROBE_TOOL_NAME,
                    "description": "返回当前时间",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "max_output_tokens": 64,
            "temperature": 0,
        }
    else:
        return None, steps, f"不支持的协议: {protocol}"
    try:
        payload = convert_to_chat_completions(protocol, source)
    except ProtocolAdaptError as exc:
        return None, steps, str(exc)
    steps.append(
        {
            "step": "protocol_adapt_with_tool",
            "direction": f"{protocol} → chat_completions",
            "ok": True,
        }
    )
    return payload, steps, None


def _post_chat_completions(base_url: str, api_key: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """真正发一次 HTTP 请求。不抛异常，出错也包装进返回值，方便调用方统一处理。"""
    url = _chat_url(base_url)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        timeout = httpx.Timeout(timeout_seconds)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "status_code": None, "message": {}, "body_preview": f"请求异常: {exc}"}
    body_preview = response.text[:800]
    ok = response.status_code < 400
    message: dict[str, Any] = {}
    if ok:
        try:
            data = response.json()
            message = ((data.get("choices") or [{}])[0].get("message")) or {}
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": ok,
        "url": url,
        "status_code": response.status_code,
        "message": message,
        "body_preview": None if ok else body_preview,
    }


def probe_channel(
    agent_id: str,
    base_url: str,
    model: str,
    api_key: str,
    source_protocol: str | None = None,
    include_tool_call: bool = False,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """探测核心逻辑：ping 一次，按需再追加一次带工具定义的探测。

    `test_agent_channel`（LLM 工具，只 ping）和 `scan_service.scan_all_agents`
    （自动巡检，ping + 工具调用）都基于这一个函数，避免两处各写一份、行为跑偏。
    """
    profile = resolve_profile(agent_id)
    if not profile:
        return {"ok": False, "error": f"未知 agent_id: {agent_id}"}
    if not base_url or not api_key or not model:
        return {"ok": False, "error": "测试需要 base_url、model、api_key"}

    protocol = (source_protocol or profile.native_protocol or "chat_completions").strip()
    timeout = timeout_seconds if timeout_seconds is not None else load_config().request_timeout_seconds
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "agent_id": profile.id,
        "native_protocol": profile.native_protocol,
        "tested_protocol": protocol,
        "model": model,
        "base_url": base_url.rstrip("/"),
    }

    try:
        payload, build_steps, error = _build_ping_payload(protocol, model)
        steps.extend(build_steps)
        if error:
            result.update({"ok": False, "steps": steps, "error": error})
            return result

        http_step = _post_chat_completions(base_url, api_key, payload, timeout)
        sample = (http_step["message"].get("content") or "")[:200] if http_step["ok"] else ""
        steps.append(
            {
                "step": "http_chat_completions",
                "url": http_step["url"],
                "status_code": http_step.get("status_code"),
                "ok": http_step["ok"],
                "sample": sample,
                "error_preview": http_step.get("body_preview") if not http_step["ok"] else None,
            }
        )
        ping_ok = http_step["ok"]
        result["ping"] = {"ok": ping_ok}

        tool_call_result: dict[str, Any] | None = None
        if include_tool_call:
            if not ping_ok:
                tool_call_result = {"ok": False, "skipped": True, "reason": "ping 未成功，跳过工具调用探测"}
            else:
                tc_payload, tc_build_steps, tc_error = _build_tool_call_payload(protocol, model)
                steps.extend(tc_build_steps)
                if tc_error:
                    tool_call_result = {"ok": False, "error": tc_error}
                else:
                    tc_http = _post_chat_completions(base_url, api_key, tc_payload, timeout)
                    tool_calls = (tc_http.get("message") or {}).get("tool_calls") or []
                    got_tool_call = bool(tool_calls) and any(
                        (tc.get("function") or {}).get("name") == _PROBE_TOOL_NAME for tc in tool_calls
                    )
                    tool_call_result = {
                        "ok": tc_http["ok"] and got_tool_call,
                        "http_ok": tc_http["ok"],
                        "status_code": tc_http.get("status_code"),
                        "tool_call_detected": got_tool_call,
                        "note": (
                            "上游正确发起了工具调用"
                            if got_tool_call
                            else ("HTTP 请求失败" if not tc_http["ok"] else "上游未返回 tool_calls：可能不支持 function calling，或模型这轮选择不调用")
                        ),
                    }
                    steps.append(
                        {
                            "step": "http_tool_call_probe",
                            "url": tc_http["url"],
                            "status_code": tc_http.get("status_code"),
                            "ok": tool_call_result["ok"],
                            "tool_call_detected": got_tool_call,
                        }
                    )
            result["tool_calling"] = tool_call_result

        result["ok"] = ping_ok and (tool_call_result is None or bool(tool_call_result.get("ok")))
        result["steps"] = steps
        result["summary"] = "探测成功" if result["ok"] else "探测未完全通过，请检查 steps"
        return result
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "exception", "ok": False, "error": str(exc)})
        result.update({"ok": False, "steps": steps, "error": str(exc)})
        return result


def test_agent_channel(
    agent_id: str,
    base_url: str,
    model: str,
    api_key: str,
    source_protocol: str | None = None,
) -> str:
    """验证：按 Agent 原生协议构造探测请求 →（如需）转 Chat Completions → 调用用户渠道。

    注意：这个工具会把 api_key 通过一次真实 HTTP 请求发给 base_url——两者都来自
    当轮对话参数。它不落盘、也不像写入那样有前端确认闸门，因为很多用户的合法场景
    就是验证一个本地/内网自建网关（比如 127.0.0.1 上的 Ollama），
    对 base_url 做内网地址黑名单反而会挡掉这类正常用法。
    这里的风险缓解主要依赖：写入配置已经有确认闸门，test 通常只在
    用户刚确认过的同一套参数上跑一次性 ping，而不是模型可以随意重复调用的持久化动作。
    """
    result = probe_channel(agent_id, base_url, model, api_key, source_protocol=source_protocol, include_tool_call=False)
    if "summary" not in result and "error" not in result:
        result["summary"] = "协议转换与渠道探测成功" if result.get("ok") else "渠道探测失败，请检查 Base URL / Key / 模型"
    return _json(result)
