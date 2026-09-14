"""已知对话 Agent 档案：本地配置路径、原生协议、写入策略。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    aliases: tuple[str, ...]
    native_protocol: str  # chat_completions | responses | anthropic_messages
    protocol_notes: str
    config_paths: tuple[str, ...]  # may include ~ 
    format: str  # json | yaml | toml | env_json
    docs_hint: str
    write_supported: bool = True


def _expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


AGENT_PROFILES: dict[str, AgentProfile] = {
    "claude_code": AgentProfile(
        id="claude_code",
        name="Claude Code",
        aliases=("claude code", "claude-code", "cc", "anthropic claude code", "claude"),
        native_protocol="anthropic_messages",
        protocol_notes="默认 Anthropic Messages（/v1/messages）。第三方网关若只提供 Chat Completions，需经协议转换后再探测。",
        config_paths=("~/.claude/settings.json", "~/.claude.json"),
        format="env_json",
        docs_hint="https://docs.anthropic.com/en/docs/claude-code",
    ),
    "codex": AgentProfile(
        id="codex",
        name="OpenAI Codex CLI",
        aliases=("codex", "openai codex", "codex cli"),
        native_protocol="responses",
        protocol_notes="偏 OpenAI Responses / Chat Completions 兼容；常见 wire_api=chat 或 responses。",
        config_paths=("~/.codex/config.toml",),
        format="toml",
        docs_hint="https://github.com/openai/codex",
    ),
    "continue": AgentProfile(
        id="continue",
        name="Continue",
        aliases=("continue", "continue.dev", "continuedev"),
        native_protocol="chat_completions",
        protocol_notes="常见 OpenAI 兼容 Chat Completions（apiBase）。",
        config_paths=("~/.continue/config.yaml", "~/.continue/config.json"),
        format="yaml",
        docs_hint="https://docs.continue.dev",
    ),
    "cline": AgentProfile(
        id="cline",
        name="Cline",
        aliases=("cline", "claude-dev", "claude dev"),
        native_protocol="chat_completions",
        protocol_notes="OpenAI 兼容提供商配置为主。",
        config_paths=(
            "~/.cline/data/settings/global-settings.json",
            "~/.cline/data/settings/providers.json",
        ),
        format="json",
        docs_hint="https://docs.cline.bot",
    ),
    "workbuddy": AgentProfile(
        id="workbuddy",
        name="WorkBuddy",
        aliases=("workbuddy", "work buddy", "wb"),
        native_protocol="chat_completions",
        protocol_notes="通常只支持 OpenAI Chat Completions（url 常写到 /v1/chat/completions）。",
        config_paths=("~/.workbuddy/models.json",),
        format="json",
        docs_hint="WorkBuddy 本地 models.json",
    ),
    "aider": AgentProfile(
        id="aider",
        name="Aider",
        aliases=("aider",),
        native_protocol="chat_completions",
        protocol_notes="OpenAI 兼容：openai-api-base / openai-api-key。",
        config_paths=("~/.aider.conf.yml",),
        format="yaml",
        docs_hint="https://aider.chat",
    ),
    "opencode": AgentProfile(
        id="opencode",
        name="OpenCode",
        aliases=("opencode", "open code"),
        native_protocol="chat_completions",
        protocol_notes="常见 openai-compatible provider + baseURL。",
        config_paths=("~/.config/opencode/opencode.json",),
        format="json",
        docs_hint="https://opencode.ai",
    ),
    "kingswitch": AgentProfile(
        id="kingswitch",
        name="KingSwitch（本助手上游）",
        aliases=(
            "kingswitch",
            "king switch",
            "forge",
            "forge agent",
            "judger",
            "本机 agent",
            "判断器",
            "当前 agent",
        ),
        native_protocol="chat_completions",
        protocol_notes="本仓库 KingSwitch，支持三种协议，配置在 agent/data/config.json。",
        config_paths=(),  # resolved dynamically
        format="json",
        docs_hint="本地 /workspace/agent/data/config.json",
    ),
}


def forge_config_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "config.json"


def list_profiles_public() -> list[dict[str, Any]]:
    rows = []
    for profile in AGENT_PROFILES.values():
        paths = list(profile.config_paths)
        if profile.id == "kingswitch":
            paths = [str(forge_config_path())]
        rows.append(
            {
                "id": profile.id,
                "name": profile.name,
                "aliases": list(profile.aliases),
                "native_protocol": profile.native_protocol,
                "protocol_notes": profile.protocol_notes,
                "config_paths": paths,
                "format": profile.format,
                "docs_hint": profile.docs_hint,
                "write_supported": profile.write_supported,
            }
        )
    return rows


def resolve_profile(agent_id: str) -> AgentProfile | None:
    key = (agent_id or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"forge_agent", "forge", "judger", "judger_agent"}:
        key = "kingswitch"
    if key in AGENT_PROFILES:
        return AGENT_PROFILES[key]
    # soft alias
    for profile in AGENT_PROFILES.values():
        if key == profile.id:
            return profile
        if key in {a.replace("-", "_").replace(" ", "_") for a in profile.aliases}:
            return profile
    return None


def detect_from_text(text: str) -> list[dict[str, Any]]:
    import re

    raw = text or ""
    lowered = raw.lower()
    # 支持 "w o r k b u d d y" / "wo r k bu d d y" 这类间隔拼写
    collapsed_spaces = re.sub(r"(?<=[a-zA-Z])\s+(?=[a-zA-Z])", "", lowered)
    compact = re.sub(r"[\s\-_.]+", "", collapsed_spaces)

    scored: list[tuple[int, AgentProfile]] = []
    for profile in AGENT_PROFILES.values():
        score = 0
        candidates = {
            profile.name.lower(),
            profile.id.replace("_", " "),
            profile.id.replace("_", ""),
            *profile.aliases,
        }
        for alias in candidates:
            alias_l = alias.lower().strip()
            if not alias_l:
                continue
            alias_compact = re.sub(r"[\s\-_.]+", "", alias_l)
            if alias_l in lowered or alias_l in collapsed_spaces:
                score += max(3, len(alias_l) // 2)
            elif alias_compact and alias_compact in compact:
                score += max(4, len(alias_compact) // 2 + 1)
        if score:
            scored.append((score, profile))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    return [
        {
            "id": p.id,
            "name": p.name,
            "score": s,
            "native_protocol": p.native_protocol,
            "protocol_notes": p.protocol_notes,
            "config_paths": (
                [str(forge_config_path())]
                if p.id == "kingswitch"
                else list(p.config_paths)
            ),
        }
        for s, p in scored
    ]


def profile_paths(profile: AgentProfile) -> list[Path]:
    if profile.id == "kingswitch":
        return [forge_config_path()]
    return [_expand(p) for p in profile.config_paths]
