# MCP Setup — Connecting CustComm to Claude Desktop

CustComm ships an MCP (Model Context Protocol) server so you can triage
and approve customer replies conversationally from Claude Desktop.

There are two ways to run it depending on whether you're using CustComm
standalone or as the engine for a productized agent like ARIA.

---

## Option A: Standalone (CustComm as a generic engine)

For operators running the public CustComm engine on its own.

### Step 1: Install CustComm

```bash
git clone https://github.com/agentsia-ai/CustComm.git
cd CustComm
uv sync --extra dev                      # creates .venv and installs CustComm
cp .env.example .env                     # fill in Anthropic + Gmail keys
cp config.example.yaml config.yaml       # customize identity + backend
```

### Step 2: Configure Claude Desktop

Find your Claude Desktop config file:

| OS      | Path                                                                |
|---------|---------------------------------------------------------------------|
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json`   |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`                       |

Add CustComm to the `mcpServers` section:

```json
{
  "mcpServers": {
    "custcomm": {
      "command": "python",
      "args": ["-m", "custcomm.mcp"],
      "cwd": "/absolute/path/to/your/CustComm"
    }
  }
}
```

On Windows, because Claude Desktop doesn't inherit your shell's PATH, it's
safest to point at the venv's absolute executable path directly:

```json
{
  "mcpServers": {
    "custcomm": {
      "command": "D:\\agentsia\\CustComm\\.venv\\Scripts\\custcomm.exe",
      "args": ["mcp"],
      "cwd": "D:\\agentsia\\CustComm"
    }
  }
}
```

The `cwd` matters: the MCP server resolves relative paths in `config.yaml`
(database, prompt overrides, Gmail token cache) from this working directory.

---

## Option B: Productized agent (e.g. ARIA via agentsia-core)

If you're running a named-persona deployment of CustComm, use the agent
runtime's CLI entry point instead. It injects the agent's tuned
`IntentClassifier` / `ReplyDrafter` / `AppointmentHandler` subclasses into
the MCP server automatically.

For ARIA (in the `agentsia-core` private repo):

```json
{
  "mcpServers": {
    "aria": {
      "command": "agentsia",
      "args": ["aria", "mcp"]
    }
  }
}
```

On Windows, again, point at the absolute exe path:

```json
{
  "mcpServers": {
    "aria": {
      "command": "D:\\agentsia\\agentsia-core\\.venv\\Scripts\\agentsia.exe",
      "args": ["aria", "mcp"]
    }
  }
}
```

For per-client deployments of ARIA:

```json
{
  "mcpServers": {
    "aria-acme": {
      "command": "agentsia",
      "args": ["aria", "--client", "acme_hvac", "mcp"]
    }
  }
}
```

The `agentsia` CLI handles config layering, env loading, and working-directory
resolution itself — no `cwd` needed in the JSON.

> Heads up: Claude Desktop runs MCP commands without your shell's PATH on some
> systems. If `agentsia` isn't found, replace `"command": "agentsia"` with the
> absolute path output by `which agentsia` (or `where agentsia` on Windows).

---

## Step 3: Restart Claude Desktop

After saving the config, fully quit and relaunch Claude Desktop. You should
see a tools icon indicating MCP tools are available.

## Step 4: Talk to your inbox

You can now say things like:

> "Any new customer emails this morning? Show me the pipeline."

> "Draft replies for all the new inquiries, but skip anything classified
> as a complaint — I want to handle those myself."

> "Approve the reply for thread abc-123 and send it."

> "For the HVAC appointment request, propose Thursday at 2pm, 3pm, or 4pm."

> "Escalate thread xyz-456 to me with the note 'customer asked about refund'."

---

## Available MCP tools

| Tool                       | What it does                                              |
|----------------------------|-----------------------------------------------------------|
| `pipeline_summary`         | Counts by status + intent + pending approvals             |
| `list_threads`             | Filter threads by status / intent / customer              |
| `get_thread`               | Full thread detail (messages + current draft)             |
| `poll_inbox`               | Pull new messages from the inbox backend                  |
| `triage_thread`            | Classify intent for one thread or many                    |
| `draft_reply`              | Generate a reply draft (status = PENDING)                 |
| `regenerate_draft`         | Discard pending draft, draft again with optional guidance |
| `approve_reply`            | Mark the pending draft APPROVED                           |
| `send_approved`            | Send APPROVED drafts (all, or one thread)                 |
| `update_thread_status`     | Snooze, escalate, close, etc.                             |
| `propose_appointment`      | Propose time slot(s) as part of a reply                   |
| `confirm_appointment`      | Mark a proposed appointment CONFIRMED                     |
| `reschedule_appointment`   | Move an existing appointment                              |
| `escalate_to_operator`     | Force a thread into ESCALATED (won't be auto-drafted)     |
| `get_customer`             | Look up a customer + all their threads                    |

`draft_reply` and `send_approved` are intentionally separate — the MCP
protocol must never allow a single conversational turn to both write a
reply and push it to a customer. That's the core "no auto-send" guardrail
expressed at the tool surface.

---

## Troubleshooting

**Tools not showing up in Claude Desktop:**
- Confirm the `cwd` path is correct and absolute (Option A only).
- Confirm `custcomm` / `agentsia` resolves on your PATH, or use the absolute
  executable path (see the Windows example above).
- Check Claude Desktop logs:
  - macOS: `~/Library/Logs/Claude/`
  - Windows: `%APPDATA%\Claude\Logs\`

**"Unexpected token" or JSON parse warnings in the Claude Desktop log:**
- Something in the engine wrote non-JSON to stdout. CustComm routes all
  logs to stderr by design; if you see this after a code change, find
  the stray `print()` or default `Console()` and switch it to `logging`
  or `Console(stderr=True)`. See `CLAUDE.md` → *MCP Server Ground Rules*.

**Gmail poll returns 0 messages:**
- Check your `inbox.gmail.query` — `in:inbox is:unread newer_than:7d` by
  default. Broaden the query, or mark a recent email unread.
- Confirm the OAuth consent flow completed (first run opens a browser).
  The cached token lives at `GMAIL_TOKEN_PATH` (default `./.gmail_token.json`).

**Classifier always returns `uncertain`:**
- Confirm `ANTHROPIC_API_KEY` is set.
- Lower `ai.min_intent_confidence` if your messages are legitimately
  ambiguous (e.g. very short "ok" replies).
- Run `custcomm --debug triage` to see raw Claude responses.
