"""CustComm — AI-powered customer communications engine.

Public entry points:
    - custcomm.cli.main        : Click CLI (the `custcomm` command)
    - custcomm.mcp_server.server.main : MCP stdio server

Pluggable base classes (subclass these in a downstream persona repo):
    - custcomm.ai.classifier.IntentClassifier
    - custcomm.ai.drafter.ReplyDrafter
    - custcomm.ai.appointments.AppointmentHandler
"""

__version__ = "0.1.0"
