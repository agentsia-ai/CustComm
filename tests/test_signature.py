"""Outbound signature rendering — operator identity only."""

from __future__ import annotations

from custcomm.ai.drafter import _append_signature
from custcomm.config.loader import CustCommConfig, OutreachConfig


def test_signature_uses_operator_fields_not_agent() -> None:
    config = CustCommConfig(
        client_name="Example Co",
        operator_name="Jane Operator",
        operator_title="Support Lead",
        operator_email="jane@example.com",
        agent_name="CustComm Bot",
        agent_email="bot@example.com",
        outreach=OutreachConfig(
            signature=(
                "Best,\n{operator_name}\n{operator_title}\n{operator_email}"
            ),
        ),
    )

    body = _append_signature("Thanks for your note.", config.outreach.signature, config)

    assert "Jane Operator" in body
    assert "Support Lead" in body
    assert "jane@example.com" in body
    assert "CustComm Bot" not in body
    assert "bot@example.com" not in body
