"""CustComm Configuration Loader.

Loads and validates deployment config from YAML + environment variables.
Mirrors LeadGen's pydantic pattern so operators and downstream personas can
learn one shape and apply it to both engines.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from custcomm.config.business_hours import BusinessHours

load_dotenv()


# ── Pydantic models ───────────────────────────────────────────────────────────


class AIConfig(BaseModel):
    """Optional AI customization. Lets a deployment swap the default Claude
    model and/or override the engine's built-in system prompts by pointing at
    external text files — without subclassing.

    Subclassing the `IntentClassifier` / `ReplyDrafter` / `AppointmentHandler`
    classes is the other supported customization path; see CLAUDE.md →
    Customization Patterns.
    """

    model: str = "claude-sonnet-4-20250514"
    intent_prompt_path: str | None = None
    reply_prompt_path: str | None = None
    appointment_prompt_path: str | None = None

    # Classifier confidence floor. Anything below this is collapsed to
    # Intent.UNCERTAIN, which refuses to auto-draft.
    min_intent_confidence: float = 0.55

    # Hard cap on generated reply length. Overflow is truncated.
    max_reply_chars: int = 2000


class InboxConfig(BaseModel):
    backend: str = "gmail"                              # "gmail" | "imap"
    gmail: dict[str, Any] = {"query": "in:inbox is:unread newer_than:7d"}
    poll_interval_seconds: int = 300                    # reserved for a future daemon
    ignore_senders: list[str] = []                      # case-insensitive substring match


class OutreachConfig(BaseModel):
    backend: str = "gmail"                              # "gmail" | "smtp"

    # SAFETY: both default to the strict setting; require_approval=false
    # is allowed but DISCOURAGED and not covered by tests.
    require_approval: bool = True
    auto_send: bool = False

    daily_send_limit: int = 100
    reply_quoting: str = "bottom"                       # "none" | "bottom" | "top"
    signature: str = ""

    # Intents that are eligible for auto-drafting. Any intent not in this list
    # lands the thread in status=escalated. Intent.UNCERTAIN is NEVER eligible.
    auto_draft_intents: list[str] = [
        "new_inquiry",
        "followup_question",
        "appointment_request",
        "reschedule",
    ]


class HistoryConfig(BaseModel):
    full_messages_kept: int = 10
    summarize_at_messages: int = 12
    max_summary_chars: int = 1200


class SchedulerConfig(BaseModel):
    followup_days: list[int] = [2, 5, 10]
    business_hours: BusinessHours = Field(default_factory=BusinessHours.default)
    appointment_slot_minutes: int = 30
    appointment_buffer_minutes: int = 15

    @field_validator("business_hours", mode="before")
    @classmethod
    def _coerce_business_hours(cls, v: Any) -> BusinessHours:
        if isinstance(v, BusinessHours):
            return v
        if isinstance(v, dict):
            return BusinessHours.from_config(v)
        raise ValueError("business_hours must be a mapping")


class DatabaseConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_path: str = "./data/custcomm.db"


class CustCommConfig(BaseModel):
    client_name: str
    operator_name: str
    operator_email: str
    operator_title: str = ""
    agent_name: str = ""
    agent_email: str = ""

    ai: AIConfig = AIConfig()
    inbox: InboxConfig = InboxConfig()
    outreach: OutreachConfig = OutreachConfig()
    history: HistoryConfig = HistoryConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    database: DatabaseConfig = DatabaseConfig()


# ── API Keys (from environment only — never in config files) ─────────────────


class APIKeys(BaseModel):
    anthropic: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Gmail API
    gmail_credentials_path: str = Field(default="", alias="GMAIL_CREDENTIALS_PATH")
    gmail_token_path: str = Field(default="./.gmail_token.json", alias="GMAIL_TOKEN_PATH")

    # SMTP (send fallback)
    smtp_host: str = Field(default="smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="", alias="SMTP_FROM_NAME")

    # IMAP (planned; stub in v1)
    imap_host: str = Field(default="", alias="IMAP_HOST")
    imap_username: str = Field(default="", alias="IMAP_USERNAME")
    imap_password: str = Field(default="", alias="IMAP_PASSWORD")

    @classmethod
    def from_env(cls) -> "APIKeys":
        values: dict[str, Any] = {}
        for field in cls.model_fields.values():
            alias = field.alias
            if not alias:
                continue
            raw = os.getenv(alias)
            if raw is None or raw == "":
                continue
            values[alias] = raw
        return cls(**values)


# ── Loader ────────────────────────────────────────────────────────────────────


def load_config(config_path: str | Path | None = None) -> CustCommConfig:
    """Load and validate deployment config from a YAML file.

    Resolution order:
      1. explicit `config_path` argument
      2. CONFIG_PATH env var
      3. `./config.yaml`
    """
    path = Path(config_path or os.getenv("CONFIG_PATH", "config.yaml"))

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.yaml to {path} and fill in your details."
        )

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = CustCommConfig(**raw)

    # Anchor a relative SQLite path to the config file's directory rather than
    # the process working directory, so the DB lives in a stable location no
    # matter where the engine is launched from (Claude Desktop with no cwd, a
    # manual run from a parent repo, ...). An already-absolute path — e.g. one
    # the agentsia-core launcher pre-resolved — is left untouched.
    sqlite_path = Path(config.database.sqlite_path)
    if not sqlite_path.is_absolute():
        config.database.sqlite_path = str((path.resolve().parent / sqlite_path).resolve())

    return config


def load_api_keys() -> APIKeys:
    """Load API keys from environment variables (and any .env file)."""
    return APIKeys.from_env()
