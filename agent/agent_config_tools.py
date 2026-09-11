"""判断器：探测 / 写入 / 测试第三方 Agent 的模型渠道配置。"""

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
    detect_from_text,
    list_profiles_public,
    profile_paths,
    resolve_profile,
)
from config_store import AgentConfig, load_config, mask_secret, save_config
from protocol_adapter import convert_to_chat_completions

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
                        item["preview_text"] = raw[:1500]
                else:
                    item["preview_text"] = raw[:1500]
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


def _write_claude_code(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    env = dict(env)
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env["ANTHROPIC_API_KEY"] = api_key
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    if model:
        env["ANTHROPIC_MODEL"] = model
    data["env"] = env
    backup = _backup(path)
    _write_json(path, data)
    return {"path": str(path), "backup": backup, "keys_written": list(env.keys())}


def _write_workbuddy(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    data: Any = {"models": []}
    if path.exists():
        try:
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
            elif isinstance(loaded, list):
                data = {"models": loaded}
        except Exception:
            pass
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
    data = {"models": models} if not isinstance(data, dict) else {**data, "models": models}
    backup = _backup(path)
    _write_json(path, data)
    return {"path": str(path), "backup": backup, "model_id": entry["id"], "url": url}


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
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "backup": backup, "format": "yaml"}


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
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"path": str(path), "backup": backup}


def _write_codex_toml(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    provider = "forge_custom"
    block = (
        f"\nmodel = \"{model or 'custom'}\"\n"
        f"model_provider = \"{provider}\"\n\n"
        f"[model_providers.{provider}]\n"
        f"name = \"Forge Custom\"\n"
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
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned.rstrip() + "\n" + block, encoding="utf-8")
    return {"path": str(path), "backup": backup, "provider": provider}


def _write_opencode(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
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
    backup = _backup(path)
    _write_json(path, data)
    return {"path": str(path), "backup": backup}


def _write_cline(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    data["apiProvider"] = "openai"
    data["openAiBaseUrl"] = base_url.rstrip("/")
    data["openAiApiKey"] = api_key
    data["openAiModelId"] = model or "custom"
    backup = _backup(path)
    _write_json(path, data)
    return {"path": str(path), "backup": backup}


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
        "path": str(Path(__file__).resolve().parent / "data" / "config.json"),
        "protocol": saved.protocol,
        "model": saved.model,
        "api_base_url": saved.api_base_url,
        "api_key_masked": mask_secret(saved.api_key),
    }


def write_agent_channel_config(
    agent_id: str,
    base_url: str,
    model: str,
    api_key: str,
    protocol: str | None = None,
    create_if_missing: bool = True,
) -> str:
    profile = resolve_profile(agent_id)
    if not profile:
        return _json({"ok": False, "error": f"未知 agent_id: {agent_id}"})
    if not (base_url or "").strip():
        return _json({"ok": False, "error": "base_url 不能为空"})
    if not (api_key or "").strip():
        return _json({"ok": False, "error": "api_key 不能为空；请让用户在对话中自行提供密钥"})
    if not profile.write_supported:
        return _json({"ok": False, "error": f"{profile.name} 暂不支持自动写入"})

    try:
        if profile.id == "forge_agent":
            result = _write_forge(base_url, model, api_key, protocol)
            return _json({"ok": True, "agent_id": profile.id, "written": result})

        paths = profile_paths(profile)
        target = None
        for path in paths:
            if path.exists():
                target = path
                break
        if target is None:
            if not create_if_missing:
                return _json({"ok": False, "error": "本地未找到配置文件，且 create_if_missing=false", "paths": [str(p) for p in paths]})
            target = paths[0]
            target.parent.mkdir(parents=True, exist_ok=True)

        if profile.id == "claude_code":
            written = _write_claude_code(target, base_url, model, api_key)
        elif profile.id == "workbuddy":
            written = _write_workbuddy(target, base_url, model, api_key)
        elif profile.id == "continue":
            # prefer yaml path
            yaml_paths = [p for p in paths if p.suffix in {".yaml", ".yml"}]
            target = yaml_paths[0] if yaml_paths else target
            written = _write_continue_yaml(target, base_url, model, api_key)
        elif profile.id == "aider":
            written = _write_aider(target, base_url, model, api_key)
        elif profile.id == "codex":
            written = _write_codex_toml(target, base_url, model, api_key)
        elif profile.id == "opencode":
            written = _write_opencode(target, base_url, model, api_key)
        elif profile.id == "cline":
            # prefer global-settings
            preferred = [p for p in paths if p.name == "global-settings.json"]
            target = preferred[0] if preferred else target
            written = _write_cline(target, base_url, model, api_key)
        else:
            return _json({"ok": False, "error": f"未实现写入器: {profile.id}"})

        return _json(
            {
                "ok": True,
                "agent_id": profile.id,
                "name": profile.name,
                "native_protocol": profile.native_protocol,
                "written": written,
                "reminder": "密钥已写入本地配置；请勿把完整 Key 贴回聊天记录。",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"写入失败: {exc}"})


def _chat_url(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def test_agent_channel(
    agent_id: str,
    base_url: str,
    model: str,
    api_key: str,
    source_protocol: str | None = None,
) -> str:
    """验证：按 Agent 原生协议构造探测请求 →（如需）转 Chat Completions → 调用用户渠道。"""
    profile = resolve_profile(agent_id)
    if not profile:
        return _json({"ok": False, "error": f"未知 agent_id: {agent_id}"})
    if not base_url or not api_key or not model:
        return _json({"ok": False, "error": "测试需要 base_url、model、api_key"})

    protocol = (source_protocol or profile.native_protocol or "chat_completions").strip()
    steps: list[dict[str, Any]] = []

    try:
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
        elif protocol == "anthropic_messages":
            source = {
                "model": model,
                "system": "Reply with exactly: ok",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 32,
                "temperature": 0,
            }
            payload = convert_to_chat_completions("anthropic_messages", source)
            steps.append(
                {
                    "step": "protocol_adapt",
                    "direction": "anthropic_messages → chat_completions",
                    "ok": True,
                    "converted_message_count": len(payload.get("messages") or []),
                }
            )
        elif protocol == "responses":
            source = {
                "model": model,
                "instructions": "Reply with exactly: ok",
                "input": "ping",
                "max_output_tokens": 32,
                "temperature": 0,
            }
            payload = convert_to_chat_completions("responses", source)
            steps.append(
                {
                    "step": "protocol_adapt",
                    "direction": "responses → chat_completions",
                    "ok": True,
                    "converted_message_count": len(payload.get("messages") or []),
                }
            )
        else:
            return _json({"ok": False, "error": f"不支持的协议: {protocol}"})

        url = _chat_url(base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        timeout = httpx.Timeout(load_config().request_timeout_seconds)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
        body_preview = response.text[:800]
        ok = response.status_code < 400
        sample = ""
        if ok:
            try:
                data = response.json()
                sample = (
                    ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                )[:200]
            except Exception:
                sample = body_preview[:200]
        steps.append(
            {
                "step": "http_chat_completions",
                "url": url,
                "status_code": response.status_code,
                "ok": ok,
                "sample": sample,
                "error_preview": None if ok else body_preview,
            }
        )
        return _json(
            {
                "ok": ok,
                "agent_id": profile.id,
                "native_protocol": profile.native_protocol,
                "tested_protocol": protocol,
                "model": model,
                "base_url": base_url.rstrip("/"),
                "steps": steps,
                "summary": "协议转换与渠道探测成功" if ok else "渠道探测失败，请检查 Base URL / Key / 模型",
            }
        )
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "exception", "ok": False, "error": str(exc)})
        return _json({"ok": False, "agent_id": getattr(profile, "id", agent_id), "steps": steps, "error": str(exc)})
