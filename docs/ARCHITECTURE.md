# CustComm Architecture

A short reference for contributors and for the downstream personas that
subclass these base classes (e.g. Agentsia's ARIA).

---

## Data flow

```
┌──────────────┐   poll    ┌──────────────┐ resolve ┌──────────────┐
│  Gmail API   │──────────▶│ RawInbound   │────────▶│ Thread       │
│  (or IMAP)   │           │ Message      │ thread_id│ (NEW)        │
└──────────────┘           └──────────────┘  (RFC    │ + Message    │
                                             5322)  └──────┬───────┘
                                                           │ triage
                                                           ▼
                                                    ┌──────────────┐
                                                    │ Thread       │
                                                    │ (TRIAGED)    │
                                                    │ intent=?     │
                                                    └──────┬───────┘
                                                           │ draft
                                                           ▼
                                                    ┌──────────────┐
                                                    │ Draft        │
                                                    │ (PENDING)    │
                                                    │ +approval_tok│
                                                    └──────┬───────┘
                                                           │ approve (CLI or MCP)
                                                           ▼
                                                    ┌──────────────┐
                                                    │ Draft        │
                                                    │ (APPROVED)   │
                                                    └──────┬───────┘
                                                           │ send
                                                           ▼
                                                    ┌──────────────┐  ◀─── customer reply
                                                    │ Thread       │      re-enters at NEW
                                                    │ (AWAITING    │      (same thread_id
                                                    │  CUSTOMER)   │       via In-Reply-To)
                                                    └──────┬───────┘
                                                           │ followup_days elapsed
                                                           │ (scheduler)
                                                           ▼
                                                    ┌──────────────┐
                                                    │ Thread       │
                                                    │ (TRIAGED     │
                                                    │  intent=     │
                                                    │  followup)   │
                                                    └──────────────┘
```

Every transition is explicit and one-step. The engine has no "triage and
draft in one call" path, no "draft and send in one call" path, and no
background autosend.

## Which base class touches what

| Stage                  | Class                       | Inputs                                 | Outputs                     |
|------------------------|-----------------------------|----------------------------------------|-----------------------------|
| Poll inbox             | `InboxConnector` subclass   | None                                   | `RawInboundMessage` stream  |
| Resolve thread_id      | `conversation.threading`    | `RawInboundMessage` + DB               | `thread_id`                 |
| Build history view     | `conversation.history`      | Thread + messages                      | `ThreadHistoryView`         |
| Classify intent        | `IntentClassifier` **(pluggable)** | Thread + latest Message        | `IntentResult`              |
| Draft reply            | `ReplyDrafter` **(pluggable)**     | Thread + Intent + history view | `Draft (PENDING)`           |
| Summarize old messages | `ReplyDrafter.summarize_history` (pluggable) | older Messages | `str` → `Thread.summary`    |
| Propose appointment    | `AppointmentHandler.propose` **(pluggable)** | Thread + available slots | `AppointmentProposal`    |
| Interpret confirmation | `AppointmentHandler.interpret_customer_reply` (pluggable) | Thread + Message + proposal | `AppointmentDecision` |
| Send reply             | `ReplySender` subclass      | Approved Draft                         | Gmail message-id            |
| Schedule follow-up     | `scheduler.followups`       | DB threads                             | re-enqueued Threads         |

The three pluggable base classes are the only extension points a productized
persona needs. Every non-AI component (inbox, sender, scheduler, DB, threading,
history, CLI, MCP server) is engine-owned and downstream deployments use it
unchanged.

## Database layout

Single SQLite file (default `./data/custcomm.db`). Four tables:

- `customers` — one row per unique email address
- `threads` — one row per conversation
- `messages` — one row per inbound or outbound message
- `drafts` — one row per generated reply draft (includes `sent_at` when sent)
- `appointments` — one row per proposed/confirmed appointment

JSON blobs hold list-valued fields (`references_headers`, `attachment_log`,
`tags`). Denormalized columns enable fast filtering:

- `threads.status`, `threads.intent`, `threads.next_followup_at`, `threads.last_inbound_at`
- `messages.message_id_header` (UNIQUE — the dedup key for ingest)
- `customers.email` (UNIQUE)
- `drafts.status`, `drafts.thread_id`

## Double-send interlock

The send path is the most dangerous code in the engine, so it's belt-and-braces:

1. Every `Draft` row has an `approval_token` (uuid) generated at draft time.
2. `approve_reply` sets `status = 'approved'` and `approved_at`. Token unchanged.
3. `send_approved` issues:
   ```sql
   UPDATE drafts
      SET status='sent', sent_at=?, sent_message_id=?
    WHERE id=? AND status='approved' AND approval_token=?
   ```
   If another CLI/MCP call already sent this draft, `status='sent'` makes
   the update affect 0 rows — we log and skip.
4. `regenerate_draft` marks the prior PENDING draft `DISCARDED`, which
   ensures any stale `approve_reply` on the old draft affects 0 rows.
5. A UNIQUE constraint on sent drafts + a pre-send check of
   `sent_message_id` ensures we never re-use the same outbound ID.

## MCP server lifecycle

```
                   ┌─────────────────────────────┐
                   │  agentsia aria mcp   OR     │
                   │  custcomm mcp               │
                   │  OR python -m custcomm.mcp  │
                   └──────────────┬──────────────┘
                                  │ imports
                                  ▼
                ┌────────────────────────────────────┐
                │  custcomm.mcp_server.server.main() │
                │                                    │
                │  1. apply *_cls kwargs →           │
                │     INTENT_CLASSIFIER_CLASS, etc.  │
                │  2. load_config() ← cwd now set    │
                │  3. load_api_keys()                │
                │  4. ThreadDatabase(...)            │
                │  5. stdio_server() run loop        │
                └──────────────┬─────────────────────┘
                               │ tool calls
                               ▼
                   ┌───────────────────────────┐
                   │ call_tool(name, args)     │
                   │   reads module globals:   │
                   │   config, keys, db,       │
                   │   *_CLASS constants       │
                   └───────────────────────────┘
```

Key property: steps 2–4 happen *inside* `main()`, not at module import.
This lets an outer caller (`agentsia-core`'s `AgentContext.activate()`)
`chdir` to a client-specific directory *before* the engine reads
`config.yaml` — so `./data/custcomm.db` resolves to the client's DB
and not whatever happened to be cwd when Python first loaded the module.

Stdout is reserved for JSON-RPC frames. Everything else (banners,
progress, debug) goes to stderr via `logging` or a `Console(stderr=True)`.
Violating this shows up as "Unexpected token" errors in the Claude
Desktop log.

## Adding a persona subclass

A productized persona overrides only the three AI base classes, keeping
everything else default:

```python
# somewhere in agents/aria/drafter.py
from pathlib import Path
from custcomm.ai.drafter import ReplyDrafter

_PROMPT_PATH = Path(__file__).parent / "prompts" / "reply.txt"

class AriaReplyDrafter(ReplyDrafter):
    SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
```

The MCP server accepts `intent_classifier_cls`, `reply_drafter_cls`, and
`appointment_handler_cls` kwargs on `main()`:

```python
from custcomm.mcp_server.server import main as mcp_main
await mcp_main(
    intent_classifier_cls=AriaIntentClassifier,
    reply_drafter_cls=AriaReplyDrafter,
    appointment_handler_cls=AriaAppointmentHandler,
)
```

The CLI path does not currently expose this injection because the engine's
own CLI is the generic path; productized CLIs like `agentsia aria ...` do
the injection themselves at the MCP layer.
