"""让 pytest 无论从哪个目录调用，都能直接 `import config_store` 之类的模块。

本项目内部用的是扁平的模块导入方式（`from config_store import ...`），不是一个
安装好的包，所以需要把 agent/ 目录显式塞进 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import pytest


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """把 $HOME 指向一个临时目录，这样 profile_paths()/Path.expanduser() 解析出来的
    '~/.claude/settings.json' 之类的路径都落在临时目录里，测试不会碰到本机真实的
    Agent 配置文件。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def isolated_config_store(tmp_path, monkeypatch):
    """把 config_store 模块里的路径常量重定向到临时目录，测试 load_config/save_config
    不会读写仓库里真实的 agent/data/config.json。"""
    import config_store

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(config_store, "CONFIG_PATH", data_dir / "config.json")
    monkeypatch.setattr(config_store, "EXAMPLE_PATH", data_dir / "config.example.json")
    return config_store


@pytest.fixture
def isolated_memory_store(tmp_path, monkeypatch):
    """把 memory_service 模块里的路径常量重定向到临时目录。"""
    import memory_service

    memory_dir = tmp_path / "data" / "memory"
    monkeypatch.setattr(memory_service, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(memory_service, "LOCAL_STORE", memory_dir / "local_memories.jsonl")
    monkeypatch.setattr(memory_service, "HISTORY_DB", memory_dir / "history.db")
    monkeypatch.setattr(memory_service, "QDRANT_PATH", memory_dir / "qdrant")
    monkeypatch.setattr(memory_service, "CHANNEL_HISTORY_STORE", memory_dir / "channel_history.jsonl")
    monkeypatch.setattr(memory_service, "SESSION_STORE", memory_dir / "sessions.jsonl")
    return memory_service


@pytest.fixture
def isolated_reports_dir(tmp_path, monkeypatch):
    """把 scan_service 生成报告的目录重定向到临时目录，测试不会在仓库里留下真实报告文件。"""
    import scan_service

    reports_dir = tmp_path / "data" / "reports"
    monkeypatch.setattr(scan_service, "REPORTS_DIR", reports_dir)
    return reports_dir
