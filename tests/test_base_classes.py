"""Base class customization tests.

Verify the two customization paths work:
  (1) Subclassing + overriding SYSTEM_PROMPT
  (2) Config-based prompt path override

No network calls — we just verify the prompt-resolution mechanics.
"""

from __future__ import annotations

from pathlib import Path

from custcomm.ai.appointments import AppointmentHandler
from custcomm.ai.classifier import IntentClassifier
from custcomm.ai.drafter import ReplyDrafter


def test_classifier_subclass_overrides_prompt(test_config, test_keys) -> None:
    class MyClassifier(IntentClassifier):
        SYSTEM_PROMPT = "My custom triage prompt"

    c = MyClassifier(test_config, test_keys)
    assert c._system_prompt == "My custom triage prompt"
    assert "triage" in c._system_prompt


def test_drafter_subclass_overrides_prompt(test_config, test_keys) -> None:
    class MyDrafter(ReplyDrafter):
        SYSTEM_PROMPT = "My custom reply voice"

    d = MyDrafter(test_config, test_keys)
    assert d._system_prompt == "My custom reply voice"


def test_appointment_handler_subclass_overrides_prompt(test_config, test_keys) -> None:
    class MyApp(AppointmentHandler):
        SYSTEM_PROMPT = "Custom booking"

    a = MyApp(test_config, test_keys)
    assert a._system_prompt == "Custom booking"


def test_classifier_config_path_override(
    test_config, test_keys, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "custom_intent.txt"
    prompt_file.write_text("Config-path-override prompt", encoding="utf-8")
    test_config.ai.intent_prompt_path = str(prompt_file)

    c = IntentClassifier(test_config, test_keys)
    assert c._system_prompt == "Config-path-override prompt"


def test_drafter_config_path_override_falls_back_when_missing(
    test_config, test_keys, tmp_path: Path
) -> None:
    test_config.ai.reply_prompt_path = str(tmp_path / "does_not_exist.txt")
    d = ReplyDrafter(test_config, test_keys)
    # Falls back to the class default (which mentions "professional")
    assert "professional" in d._system_prompt.lower()


def test_all_three_base_classes_expose_system_prompt_constant() -> None:
    """Regression: the pluggable seam relies on SYSTEM_PROMPT being a class
    attribute, not an instance-only attribute."""
    assert isinstance(IntentClassifier.SYSTEM_PROMPT, str)
    assert isinstance(ReplyDrafter.SYSTEM_PROMPT, str)
    assert isinstance(AppointmentHandler.SYSTEM_PROMPT, str)
