from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from judger_prompt import KINGSWITCH_SYSTEM_PROMPT
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
    system_prompt: str = Field(default=KINGSWITCH_SYSTEM_PROMPT)
    enable_tools: bool = True
    enable_web_search: bool = False
    request_timeout_seconds: int = Field(default=120, ge=10, le=600)
    # Mem0 长期记忆（本地 Qdrant）
    enable_memory: bool = True
    memory_user_id: str = Field(default="default", description="记忆命名空间 / 用户 ID")
    memory_embed_base_url: str = Field(
        default="",
        description="向量模型 API Base URL（独立配置，勿与文本模型混用）",
    )
    memory_embed_api_key: str = Field(default="", description="向量模型 API Key")
    memory_embed_model: str = Field(default="text-embedding-3-small", description="向量模型 ID")
    memory_llm_model: str = Field(default="", description="记忆抽取用模型，空则复用主模型")
    memory_top_k: int = Field(default=5, ge=1, le=20)
    memory_embed_dims: int = Field(default=1536, ge=64, le=4096)
    # 打开页面时自动巡检本机已知 Agent（安装状态 + 已配置渠道的连通性/工具调用探测）。
    # 默认开启，但会对已发现凭证的渠道发起真实网络请求，谨慎的用户可以关掉。
    enable_auto_scan: bool = True

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        allowed = {"chat_completions", "responses", "anthropic_messages"}
        if value not in allowed:
            raise ValueError(f"protocol 必须是 {sorted(allowed)} 之一")
        return value

    @field_validator("api_base_url", "memory_embed_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        cleaned = (value or "").strip().rstrip("/")
        if cleaned.startswith("ttps://"):
            cleaned = "h" + cleaned
        elif cleaned.startswith("ttp://"):
            cleaned = "h" + cleaned
        if cleaned and "://" not in cleaned:
            cleaned = "https://" + cleaned
        return cleaned

    @field_validator("memory_user_id")
    @classmethod
    def normalize_user_id(cls, value: str) -> str:
        cleaned = (value or "").strip() or "default"
        return cleaned[:64]


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
    data["memory_embed_api_key_masked"] = mask_secret(cfg.memory_embed_api_key)
    data["memory_embed_api_key_set"] = bool(cfg.memory_embed_api_key)
    # 前端编辑时默认不回传明文 key；若用户未改 key，提交空字符串表示保持原值
    data["api_key"] = ""
    data["memory_embed_api_key"] = ""
    return data
