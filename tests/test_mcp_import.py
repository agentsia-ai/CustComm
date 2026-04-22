"""Smoke test: importing the MCP server must not load config / credentials.

This guards the contract that config / keys / db are lazily initialized
inside `main()`, so a productized agent runtime can chdir and set env
vars BEFORE the engine reads anything from disk.
"""

from __future__ import annotations


def test_mcp_server_import_does_not_read_config(monkeypatch) -> None:
    # Force load_config / load_api_keys to raise if they somehow run at import.
    called = {"load_config": 0, "load_api_keys": 0}

    def boom_config(*_a, **_kw):  # pragma: no cover — should never be hit
        called["load_config"] += 1
        raise AssertionError("load_config must NOT run at module import")

    def boom_keys(*_a, **_kw):  # pragma: no cover — should never be hit
        called["load_api_keys"] += 1
        raise AssertionError("load_api_keys must NOT run at module import")

    import custcomm.config.loader as loader_mod

    monkeypatch.setattr(loader_mod, "load_config", boom_config)
    monkeypatch.setattr(loader_mod, "load_api_keys", boom_keys)

    # Fresh import of the MCP server module must not touch config/keys.
    import importlib
    import custcomm.mcp_server.server as server_mod

    importlib.reload(server_mod)

    assert called["load_config"] == 0
    assert called["load_api_keys"] == 0
    assert server_mod.config is None
    assert server_mod.keys is None
    assert server_mod.db is None

    # Pluggable class defaults should be the engine base classes at import time.
    from custcomm.ai.classifier import IntentClassifier
    from custcomm.ai.drafter import ReplyDrafter
    from custcomm.ai.appointments import AppointmentHandler

    assert server_mod.INTENT_CLASSIFIER_CLASS is IntentClassifier
    assert server_mod.REPLY_DRAFTER_CLASS is ReplyDrafter
    assert server_mod.APPOINTMENT_HANDLER_CLASS is AppointmentHandler
