"""Config loader tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from custcomm.config.loader import APIKeys, load_api_keys, load_config


def test_load_config_reads_yaml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "client_name": "Example Co",
                "operator_name": "Alex",
                "operator_email": "alex@example.com",
                "ai": {"model": "claude-sonnet-4-20250514"},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.client_name == "Example Co"
    assert cfg.operator_email == "alex@example.com"
    assert cfg.outreach.require_approval is True  # safety default
    assert cfg.outreach.auto_send is False  # safety default


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_api_keys_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "/tmp/creds.json")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)

    keys = load_api_keys()
    assert keys.anthropic == "sk-ant-test"
    assert keys.gmail_credentials_path == "/tmp/creds.json"
    assert keys.smtp_username == ""  # default


def test_api_keys_defaults_when_env_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [
        "ANTHROPIC_API_KEY",
        "GMAIL_CREDENTIALS_PATH",
        "GMAIL_TOKEN_PATH",
        "SMTP_USERNAME",
    ]:
        monkeypatch.delenv(k, raising=False)
    keys = APIKeys.from_env()
    assert keys.anthropic == ""
    assert keys.gmail_token_path == "./.gmail_token.json"
