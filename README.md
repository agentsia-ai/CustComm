# CustComm

> An AI-powered customer communications engine for small-business operators. Ingest email, classify intent, draft replies, handle appointments — all under human approval.

---

## Vision

Most customer-communications tools are either too expensive (enterprise helpdesks), too manual (a cluttered inbox), or too autonomous (scary autoresponders that hallucinate). CustComm sits in the middle:

- **AI-first** — Claude classifies inbound intent, drafts replies in context, proposes appointment slots.
- **MCP-native** — runs as an MCP server so you can triage and approve replies conversationally via Claude Desktop or any MCP client.
- **Human-in-the-loop by default** — the engine *never* auto-sends. Every outbound message requires an explicit approve step, whether via CLI or MCP.
- **Fully configurable** — swap out tone, ICP, reply guardrails, and prompts per deployment.
- **Yours to white-label** — the engine is identity-free. Named personas (like Agentsia's ARIA) live in downstream repos as subclasses.

The goal: you spend 15 minutes a day approving AI-drafted replies. CustComm does the rest.

---

## Core Concepts

### The flow

```
┌──────────────┐     poll       ┌──────────────┐    triage      ┌──────────────┐
│  Inbox       │ ─────────────▶ │  Thread (NEW)│ ─────────────▶ │ Thread       │
│  (Gmail)     │                │  + Message   │  IntentClassifier│ (TRIAGED)   │
└──────────────┘                └──────────────┘                └──────┬───────┘
                                                                       │ draft
                                                                       ▼
                                                                ┌──────────────┐
                                                                │ Draft        │
                                                                │ (PENDING)    │
                                                                └──────┬───────┘
                                                                       │ approve (operator)
                                                                       ▼
                                                                ┌──────────────┐
                                                                │ Draft        │
                                                                │ (APPROVED)   │
                                                                └──────┬───────┘
                                                                       │ send
                                                                       ▼
                                                                ┌──────────────┐
                                                                │ Thread       │
                                                                │ (AWAITING    │
                                                                │  CUSTOMER)   │
                                                                └──────────────┘
```

No step can be skipped. A draft moves to `sent` only via the explicit approve → send path, with a DB-level token interlock that prevents double-sends across CLI and MCP paths.

### Three pluggable AI seams

Every class that talks to Claude is subclassable and prompt-overridable:

| Base class            | Where it lives                  | What it does                               |
|-----------------------|---------------------------------|--------------------------------------------|
| `IntentClassifier`    | `src/custcomm/ai/classifier.py` | Classifies an inbound message's intent     |
| `ReplyDrafter`        | `src/custcomm/ai/drafter.py`    | Drafts a reply given thread + intent       |
| `AppointmentHandler`  | `src/custcomm/ai/appointments.py`| Proposes slots, interprets confirmations   |

See [`CLAUDE.md`](./CLAUDE.md) → *Customization Patterns* for both customization paths (config-based prompt swap, or subclassing).

---

## Architecture

```
CustComm/
├── src/
│   └── custcomm/                    # The package (standard Python src-layout)
│       ├── __init__.py
│       ├── cli.py                   # Click CLI entry point (`custcomm ...`)
│       ├── mcp.py                   # `python -m custcomm.mcp` MCP entry shim
│       ├── models.py                # Customer, Thread, Message, Draft, Appointment
│       │
│       ├── ai/                      # Claude integration
│       │   ├── classifier.py        # IntentClassifier — pluggable
│       │   ├── drafter.py           # ReplyDrafter — pluggable
│       │   └── appointments.py      # AppointmentHandler — pluggable
│       │
│       ├── config/
│       │   └── loader.py            # Pydantic CustCommConfig + AIConfig + APIKeys
│       │
│       ├── inbox/                   # Inbound channel connectors
│       │   ├── base.py              # InboxConnector ABC
│       │   ├── gmail.py             # GmailInbox — primary in v1
│       │   └── imap.py              # IMAPInbox — planned stub
│       │
│       ├── outreach/                # Outbound channel connectors
│       │   ├── base.py              # ReplySender ABC
│       │   ├── gmail.py             # GmailSender — primary in v1
│       │   └── smtp.py              # SMTPSender — fallback
│       │
│       ├── conversation/
│       │   ├── threading.py         # RFC 5322 thread resolution
│       │   └── history.py           # Prompt-sized thread history view
│       │
│       ├── scheduler/
│       │   ├── followups.py         # Overdue-thread follow-up enqueuer
│       │   └── appointments.py      # Appointment slot storage
│       │
│       ├── crm/
│       │   └── database.py          # Async SQLite store
│       │
│       └── mcp_server/
│           └── server.py            # MCP server exposing tools to Claude Desktop
│
├── docs/
│   ├── GETTING_STARTED.md
│   ├── MCP_SETUP.md
│   └── ARCHITECTURE.md
│
├── config.example.yaml              # Template config (copy → config.yaml)
├── pyproject.toml
├── .env.example
├── .gitignore
├── CLAUDE.md                        # AI-assistant context for this repo
└── LICENSE                          # AGPL-3.0
```

> **Customizing for a productized agent (named persona, tuned prompts)?**
> See [`CLAUDE.md`](./CLAUDE.md) → *Customization Patterns*. The base classes are
> designed to be subclassed or have their prompts swapped from a downstream repo.

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/agentsia-ai/CustComm.git
cd CustComm

# 2. Install (uv-managed; uv.lock pins exact deps)
uv sync --extra dev

# 3. Configure
cp .env.example .env                     # add your API keys
cp config.example.yaml config.yaml       # customize identity + inbox backend

# 4. Initialize (creates the SQLite DB; safe to run anytime)
uv run custcomm pipeline

# 5. Poll inbound mail
uv run custcomm poll

# 6. Triage + draft
uv run custcomm triage
uv run custcomm draft

# 7. Review + approve (from CLI, or via MCP in Claude Desktop)
uv run custcomm list --status drafted
uv run custcomm approve <thread-id>
uv run custcomm send

# 8. Start the MCP server (connect to Claude Desktop)
uv run custcomm mcp
```

See [`docs/GETTING_STARTED.md`](./docs/GETTING_STARTED.md) for the full walkthrough and [`docs/MCP_SETUP.md`](./docs/MCP_SETUP.md) for Claude Desktop setup.

---

## MCP Integration

CustComm exposes itself as an MCP server so you can triage and approve replies directly from Claude Desktop:

> "Any new inquiries this morning?"
>
> "Draft replies for the three appointment requests, and propose 2pm, 3pm, or 4pm tomorrow for the HVAC one."
>
> "Approve the reply for thread abc-123 and send it."

### Available MCP tools

| Tool                       | What it does                                              |
|----------------------------|-----------------------------------------------------------|
| `pipeline_summary`         | Counts by status + intent + pending-approvals             |
| `list_threads`             | Filter threads by status / intent / customer              |
| `get_thread`               | Full thread detail (messages + current draft)             |
| `poll_inbox`               | Pull new messages from the inbox backend                  |
| `triage_thread`            | Classify intent for one or many threads                   |
| `draft_reply`              | Generate a reply draft (status = PENDING)                 |
| `regenerate_draft`         | Discard pending draft, draft again with optional guidance |
| `approve_reply`            | Mark the pending draft APPROVED                           |
| `send_approved`            | Send all APPROVED drafts (or one specific thread)         |
| `update_thread_status`     | Snooze, escalate, close, etc.                             |
| `propose_appointment`      | Propose time slot(s) inside a reply                       |
| `confirm_appointment`      | Mark a proposed appointment CONFIRMED                     |
| `reschedule_appointment`   | Move an existing appointment                              |
| `escalate_to_operator`     | Force a thread into ESCALATED (won't be auto-drafted)     |
| `get_customer`             | Look up a customer + all their threads                    |

---

## Inbox / Outreach Source Maturity

Not all backends are equally battle-tested. As of v0.1.0:

| Backend               | Status              | Notes                                                 |
|-----------------------|---------------------|-------------------------------------------------------|
| Gmail API (inbox)     | Primary             | OAuth2 with cached refresh token; the default path    |
| Gmail API (send)      | Primary             | Uses the same credentials as the inbox                |
| SMTP (send)           | Supported           | Fallback if Gmail API isn't available                 |
| IMAP (inbox)          | Planned / stub      | Raises `NotImplementedError` — post-v1 roadmap item   |

If you're starting fresh: **use the Gmail backend for both inbox and outreach**.

---

## Safety / Guardrails

The engine ships locked down. A downstream deployment may relax some of these, but the defaults err toward humans:

- `outreach.require_approval = true` and `outreach.auto_send = false` — the engine physically refuses to send without an explicit approve step.
- Approve → send interlock: each draft has an `approval_token` checked at send time, so CLI and MCP can't race each other into a double-send.
- Hard `max_reply_chars` on generated drafts (default 2000).
- Confidence floor on the classifier — anything below `min_intent_confidence` becomes `uncertain` and refuses to auto-draft.
- Non-replyable intents (`complaint`, `cancel`, `unrelated`, `uncertain`) default to auto-escalate rather than draft.
- Default `ReplyDrafter` prompt forbids Claude from inventing prices, availability, or commitments not present in the thread.
- Outbound `From:` is pinned to `operator_email` — drafts can't override it.
- PII-style patterns (emails, long digit runs) are redacted from log records above DEBUG.

---

## Productization / White-Label

CustComm is the open-source engine. Named personas (voice, tone, tuned prompts) live in downstream private repos as subclasses:

```python
from custcomm.ai.drafter import ReplyDrafter

class MyBrandReplyDrafter(ReplyDrafter):
    SYSTEM_PROMPT = "You are MyBrand's customer service voice..."
```

That subclass, plus a `config.yaml` pointing at it via the agent runtime, is the whole productization surface. See [`CLAUDE.md`](./CLAUDE.md).

---

## License

AGPL-3.0 — free to use, modify, and distribute. If you run a modified version as a network service, you must open-source your modifications under the same license. See [LICENSE](LICENSE) for full terms.

---

*A sibling engine to [LeadGen](https://github.com/agentsia-ai/LeadGen). Same architecture, different channel: LeadGen runs outbound, CustComm runs conversational.*
