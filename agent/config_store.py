from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"
EXAMPLE_PATH = DATA_DIR / "config.example.json"

_lock = threading.Lock()


class AgentConfig(BaseModel):
    api_base_url: str = Field(default="https://api.openai.com/v1", description="API Base URL，通常到 /v1")
    api_key: str = Field(default="", description="API Key")
    model: str = Field(default="gpt-4o-mini", description="模型 ID")
    protocol: str = Field(default="chat_completions", description="接口协议")
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    system_prompt: str = Field(
        default="你是一个本地可配置的 AI Agent。你可以调用工具完成任务，回答要简洁、可执行。"
    )
    enable_tools: bool = True
    request_timeout_seconds: int = Field(default=120, ge=10, le=600)

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        allowed = {"chat_completions", "responses", "anthropic_messages"}
        if value not in allowed:
            raise ValueError(f"protocol 必须是 {sorted(allowed)} 之一")
        return value

    @field_validator("api_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


DEFAULT_CONFIG = AgentConfig()


def ensure_config_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        source = EXAMPLE_PATH if EXAMPLE_PATH.exists() else None
        if source:
            CONFIG_PATH.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG.model_dump(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


def load_config() -> AgentConfig:
    ensure_config_file()
    with _lock:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return AgentConfig.model_validate(raw)


def save_config(config: AgentConfig) -> AgentConfig:
    ensure_config_file()
    payload = config.model_dump()
    with _lock:
        CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def public_config(config: AgentConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    data = deepcopy(cfg.model_dump())
    data["api_key_masked"] = mask_secret(cfg.api_key)
    data["api_key_set"] = bool(cfg.api_key)
    # 前端编辑时默认不回传明文 key；若用户未改 key，提交空字符串表示保持原值
    data["api_key"] = ""
    return data
