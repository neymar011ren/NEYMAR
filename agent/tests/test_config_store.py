from __future__ import annotations

import pytest

from config_store import AgentConfig, ensure_config_file, load_config, mask_secret, public_config, save_config


class TestAgentConfigValidators:
    def test_fixes_missing_leading_h_in_scheme(self):
        cfg = AgentConfig(api_base_url="ttps://example.com/v1")
        assert cfg.api_base_url == "https://example.com/v1"

    def test_adds_scheme_if_missing(self):
        cfg = AgentConfig(api_base_url="example.com/v1")
        assert cfg.api_base_url == "https://example.com/v1"

    def test_strips_trailing_slash(self):
        cfg = AgentConfig(api_base_url="https://example.com/v1/")
        assert cfg.api_base_url == "https://example.com/v1"

    def test_rejects_unknown_protocol(self):
        with pytest.raises(ValueError):
            AgentConfig(protocol="not-a-real-protocol")

    def test_memory_user_id_defaults_and_truncates(self):
        assert AgentConfig(memory_user_id="  ").memory_user_id == "default"
        long_id = "x" * 200
        assert len(AgentConfig(memory_user_id=long_id).memory_user_id) == 64


class TestMaskSecret:
    def test_empty(self):
        assert mask_secret("") == ""

    def test_short_value_fully_masked(self):
        assert mask_secret("abcd") == "****"

    def test_long_value_keeps_head_and_tail(self):
        assert mask_secret("sk-abcdefghijklmnop") == "sk-a...mnop"
        assert "abcdefghijklmnop"[2:-4] not in mask_secret("sk-abcdefghijklmnop")


class TestConfigPersistence:
    def test_ensure_config_file_seeds_from_example(self, isolated_config_store):
        isolated_config_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        isolated_config_store.EXAMPLE_PATH.write_text(
            AgentConfig(model="example-model").model_dump_json(), encoding="utf-8"
        )
        ensure_config_file()
        assert isolated_config_store.CONFIG_PATH.exists()
        cfg = load_config()
        assert cfg.model == "example-model"

    def test_ensure_config_file_falls_back_to_defaults(self, isolated_config_store):
        # 没有 example 文件时，也不应该崩，而是用代码里的默认值
        ensure_config_file()
        cfg = load_config()
        assert cfg.model == AgentConfig().model

    def test_save_then_load_round_trip(self, isolated_config_store):
        ensure_config_file()
        cfg = load_config()
        saved = save_config(cfg.model_copy(update={"model": "gpt-test", "api_key": "sk-real"}))
        assert saved.model == "gpt-test"
        reloaded = load_config()
        assert reloaded.model == "gpt-test"
        assert reloaded.api_key == "sk-real"

    def test_public_config_never_leaks_raw_key(self, isolated_config_store):
        ensure_config_file()
        save_config(load_config().model_copy(update={"api_key": "sk-real-secret-value"}))
        data = public_config()
        assert data["api_key"] == ""
        assert data["api_key_set"] is True
        assert "sk-real-secret-value" not in str(data)
