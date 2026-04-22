# CLAUDE.md — CustComm

This file provides context and instructions for Claude (or any AI assistant)
working in this codebase. Read this before making any changes.

---

## What This Project Is

CustComm is a generic, AGPL-licensed, AI-powered customer-communications engine.
It's designed to be the open-source core that any operator can configure and
deploy. Its job:

1. Poll inbound customer email (Gmail API in v1; IMAP planned)
2. Classify intent per message against a small, extensible taxonomy
3. Draft replies using Claude, with thread history as context
4. Propose appointment slots and interpret customer confirmations
5. Manage conversation state across long threads (summary + last-N-verbatim)
6. Track follow-ups on threads waiting for a customer response
7. Expose all functionality as an MCP server for conversational control via
   Claude Desktop — *never* autonomously sending.
8. Support productized deployments via subclassing or config-based prompt overrides
   (see *Customization Patterns* below)

This repository is intentionally **identity-free**. Anything specific to a
particular operator, brand, named agent, or voice belongs in a downstream
private repository or a local `config.yaml` — never in this codebase.

---

## Architecture Overview

```
src/custcomm/
├── models.py                # Customer, Thread, Message, Draft, Appointment
├── config/
│   └── loader.py            # Pydantic CustCommConfig + AIConfig + APIKeys
├── inbox/
│   ├── base.py              # InboxConnector ABC
│   ├── gmail.py             # GmailInbox — primary
│   └── imap.py              # IMAPInbox — stub, raises NotImplementedError
├── ai/
│   ├── classifier.py        # IntentClassifier (pluggable)
│   ├── drafter.py           # ReplyDrafter (pluggable)
│   └── appointments.py      # AppointmentHandler (pluggable)
├── outreach/
│   ├── base.py              # ReplySender ABC
│   ├── gmail.py             # GmailSender — primary
│   └── smtp.py              # SMTPSender — fallback
├── conversation/
│   ├── threading.py         # RFC 5322 thread resolution
│   └── history.py           # Prompt-sized thread history view builder
├── scheduler/
│   ├── followups.py         # Overdue-thread follow-up enqueuer
│   └── appointments.py      # Appointment slot storage
├── crm/
│   └── database.py          # ThreadDatabase — async SQLite
├── mcp_server/
│   └── server.py            # MCP server exposing all tools to Claude Desktop
└── cli.py                   # Click CLI entry point
```

### Data Flow

```
Inbox → Message (new) → Thread (NEW)
     → IntentClassifier → Thread (TRIAGED, intent attached)
     → ReplyDrafter → Draft (PENDING)
     → Operator approve (CLI or MCP) → Draft (APPROVED)
     → ReplySender → Draft (SENT), Thread (AWAITING_CUSTOMER)
     → Customer reply → back to Thread (NEW) on a matched thread
     → Or: no reply past followup_days → scheduler enqueues back to TRIAGED
```

Every transition is explicit. The engine will never collapse multiple steps
(e.g. "draft and send in one call") — that's the core guardrail.

---

## Key Design Principles

### 1. Never auto-send
`outreach.require_approval = true` and `outreach.auto_send = false` are the
defaults and the only values the engine ships with verified. The `ReplySender`
refuses to process any draft whose status isn't `APPROVED`. The MCP tool
surface has no "draft and send" combo. The only path from `Message` to
customer is: triage → draft (PENDING) → explicit approve → explicit send.
This is hard-coded, not merely configured.

### 2. Config-driven, not code-driven
Everything client-specific lives in `config.yaml` and `.env`. The engine code
should never contain hardcoded identity, brand, or voice. When adding
features, ask: "should this be configurable?" If yes, add it to the config
schema in `src/custcomm/config/loader.py` first.

### 3. The data models are the contract
`src/custcomm/models.py` defines `Customer`, `Thread`, `Message`, `Draft`,
`Appointment`. Every layer (inbox, AI, outreach, scheduler, CRM) speaks in
these objects. Never pass raw dicts between layers.

### 4. Async everywhere
All I/O is async (`httpx`, `aiosqlite`, `anthropic` async client). Use
`async/await` consistently. Don't introduce synchronous blocking calls in
the hot path.

### 5. MCP tools must be self-describing
The MCP server is the primary operator interface. Tool names and descriptions
must be clear enough that Claude can reason about when to use them without
additional context. Keep tool schemas tight — prefer fewer, well-named
parameters over many optional ones.

### 6. White-label ready / identity-free
The engine code must not contain any operator- or client-specific identity,
brand name, named agent persona, or voice. All identity flows in through
`config.yaml` at runtime, or through a downstream subclass that overrides
the base classes (see *Customization Patterns* below). The only exception
is the LICENSE copyright line, which is required by AGPL-3.0.

If you find yourself wanting to bake "Agent X says..." or "Company Y
handles it this way..." into a prompt or default value here, **stop** —
that belongs in a downstream private repo, not in this engine.

### 7. Engines stay public, personas live in downstream private repos
CustComm is published and forked as a standalone engine. Named personas
(e.g. Agentsia's ARIA) live in a separate private repository and consume
CustComm as an installed dependency, subclassing the base classes for
voice. Do not accept PRs that add persona-specific content to this repo.

---

## Customization Patterns

There are two supported ways to customize prompt behavior without modifying
this engine:

### Pattern A — Config-based prompt override (no code)

Point at external prompt files in your `config.yaml`:

```yaml
ai:
  model: "claude-sonnet-4-20250514"
  intent_prompt_path: "./prompts/intent.txt"
  reply_prompt_path: "./prompts/reply.txt"
  appointment_prompt_path: "./prompts/appointment.txt"
```

The base `IntentClassifier` / `ReplyDrafter` / `AppointmentHandler` will read
these files at construction time and use them as the system prompt. Missing
files log a warning and fall back to the class-constant default.

### Pattern B — Subclassing (for productized agents)

For named agents with personas (e.g. a downstream private repo defining
"ARIA"), subclass and override the class constants:

```python
from custcomm.ai.classifier import IntentClassifier
from custcomm.ai.drafter import ReplyDrafter
from custcomm.ai.appointments import AppointmentHandler

class AriaIntentClassifier(IntentClassifier):
    SYSTEM_PROMPT = "You are ARIA's triage brain..."

class AriaReplyDrafter(ReplyDrafter):
    SYSTEM_PROMPT = "You are ARIA's voice — warm, professional..."

class AriaAppointmentHandler(AppointmentHandler):
    SYSTEM_PROMPT = "You are ARIA's booking specialist..."
```

The MCP server accepts `intent_classifier_cls`, `reply_drafter_cls`, and
`appointment_handler_cls` kwargs on `main()` so an agent runtime can inject
these subclasses at startup. See `mcp_server/server.py`.

Both patterns can be combined (subclass for code-level customization, then
override per-deployment via config). This three-tier model (engine → named
agent → per-client config) is the canonical productization shape.

---

## Working With This Codebase

### Running locally
```bash
# Install dependencies (uv-managed)
uv sync --extra dev

# Set up config
cp .env.example .env                       # fill in API keys
cp config.example.yaml config.yaml         # customize identity + backend

# Initialize database (safe to run anytime)
uv run custcomm pipeline

# Poll inbound mail
uv run custcomm poll

# Triage + draft
uv run custcomm triage
uv run custcomm draft

# Review + approve + send
uv run custcomm list --status drafted
uv run custcomm approve <thread-id>
uv run custcomm send

# Start MCP server
uv run custcomm mcp
```

### Adding a new inbox backend
1. Create `src/custcomm/inbox/<backend>.py`
2. Subclass `InboxConnector` and implement `async def fetch_new() -> AsyncIterator[RawInboundMessage]`
3. Add config fields to `InboxConfig` in `src/custcomm/config/loader.py`
4. Wire it into `build_inbox()` in `src/custcomm/inbox/__init__.py`
5. Add any required env vars to `.env.example`

### Adding a new MCP tool
1. Add the `Tool` definition in `list_tools()` in `src/custcomm/mcp_server/server.py`
2. Add the handler branch in `call_tool()`
3. Update `docs/MCP_SETUP.md` with the new tool in the tools table
4. Keep tool names snake_case and descriptions action-oriented

### Modifying the data models
- Add new fields with sensible defaults so existing DB rows don't break
- If a field needs fast filtering, denormalize it into a column in `crm/database.py`
- Update the row-to-model helper in `crm/database.py` after adding persisted fields

---

## Claude API Usage in This Project

CustComm uses Claude for three things:

### 1. Intent Classification (`src/custcomm/ai/classifier.py`)
- Default model: `claude-sonnet-4-20250514` (override via `ai.model` in config)
- Returns structured JSON `{"intent": "...", "confidence": 0.0, "reasoning": "..."}`
- Default prompt lives on `IntentClassifier.SYSTEM_PROMPT`
- Runs in batches of 5 to manage rate limits
- Confidence below `ai.min_intent_confidence` is collapsed to `Intent.UNCERTAIN`

### 2. Reply Drafting (`src/custcomm/ai/drafter.py`)
- Default model: `claude-sonnet-4-20250514`
- Returns JSON `{"subject": "...", "body": "..."}`
- Default prompt lives on `ReplyDrafter.SYSTEM_PROMPT`
- Drafter also owns `summarize_history()` for long-thread compression
- Hard-capped at `ai.max_reply_chars` (default 2000) — overflow is truncated

### 3. Appointment Handling (`src/custcomm/ai/appointments.py`)
- Default model: `claude-sonnet-4-20250514`
- Two methods: `propose()` (pick slots + write pitch) and
  `interpret_customer_reply()` (structured decision: confirm / reschedule /
  cancel / ambiguous)
- Default prompt lives on `AppointmentHandler.SYSTEM_PROMPT`

### Prompt tuning tips
- Classifier quality improves when you add a few-shot example block of
  edge cases between the system prompt and the user message.
- Drafter quality improves when the operator's voice is described concretely
  ("warm, concise, never over-promises" beats "professional").
- Keep `max_tokens` tight — 400 for classify, 800 for draft, 500 for appointment.

---

## MCP Server Ground Rules

The MCP server uses stdio transport. The stdio stream is the transport for
JSON-RPC frames — anything we write to it that isn't a JSON-RPC frame shows
up as "Unexpected token" errors in the Claude Desktop client.

Therefore, in any code path the MCP server can reach:

- **No `print()`** — ever. Use `logging` (stderr by default).
- **No `Console()` without `stderr=True`** — Rich's default console writes
  to stdout. Always construct `Console(stderr=True)` for MCP-facing output.
- **No config / credentials / DB loading at module import time** —
  initialize them inside `main()` after the caller has had a chance to
  `chdir` and set env vars. Use module-level `None` placeholders declared
  `global` inside `main()`.
- **Module-level pluggable class globals** — `INTENT_CLASSIFIER_CLASS`,
  `REPLY_DRAFTER_CLASS`, `APPOINTMENT_HANDLER_CLASS` default to the engine
  base classes. `main()` accepts `*_cls` kwargs and overwrites the globals
  before the server starts. This is the seam that productized agents plug
  their subclasses into.

See `src/custcomm/mcp_server/server.py` for the reference implementation.

---

## Environment Variables

See `.env.example` for the full list. Required to run anything:
- `ANTHROPIC_API_KEY` — classification and drafting

Required for the default (Gmail) backend:
- `GMAIL_CREDENTIALS_PATH` — path to the OAuth2 credentials JSON

Optional:
- `GMAIL_TOKEN_PATH` — defaults to `./.gmail_token.json` (gitignored)
- `SMTP_*` — for the SMTP outreach fallback
- `IMAP_*` — for the planned IMAP inbox (stub in v1)

Never commit `.env` or `config.yaml` to git. Both are in `.gitignore`.

---

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src

# Run a specific test file
uv run pytest tests/test_base_classes.py
```

Tests use `pytest-asyncio` for async test support. Mock external API calls
with `unittest.mock.AsyncMock` — never make real API calls (Anthropic or
Gmail) in tests.

---

## Common Gotchas

- **`custcomm` command not found:** activate `.venv` or prefix with `uv run`.
- **Gmail OAuth first-run** requires a browser for the consent flow; subsequent
  runs use the cached token at `GMAIL_TOKEN_PATH`.
- **MCP server must use stdio transport.** Don't switch to HTTP without updating
  Claude Desktop config.
- **Config reload:** config is loaded once at MCP server startup; restart the
  server after changing `config.yaml`.
- **Thread merging surprises:** `In-Reply-To` / `References` always win. If two
  unrelated threads look like they're merging, check whether the customer
  replied to a months-old thread with a new subject — the 90-day subject
  fallback window is the usual culprit.

---

## Project Status

See `README.md` for the full feature overview. Current phase: **v0.1.0 / initial scaffold**.

Stubs that still need implementation:
- `src/custcomm/inbox/imap.py` — IMAP inbox connector
- Interactive `custcomm review` CLI command (MCP flow is the primary UI)
- Attachment *parsing* (v1 only logs attachment metadata)

---

## License

AGPL-3.0. Same rules as every AGPL codebase: modifications served over the
network must be open-sourced under AGPL. See `LICENSE`.
