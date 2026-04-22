# Getting Started with CustComm

This guide takes you from a fresh clone to your first AI-drafted reply.
Should take about 30 minutes.

---

## Prerequisites

- Python 3.12+
- `uv` package manager — install at https://docs.astral.sh/uv/
- An Anthropic API key (free credits available at https://console.anthropic.com)
- A Gmail account for the inbox + send path (Google Workspace works too)

---

## Step 1 — Clone the Repo

```bash
git clone https://github.com/agentsia-ai/CustComm.git
cd CustComm
```

---

## Step 2 — Install Dependencies

CustComm uses `uv` for fast, reliable dependency management. **Do not mix in
raw `pip install` — it will diverge from `uv.lock`.**

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create the venv and install all dependencies (incl. dev tools)
uv sync --extra dev
```

After this, the `custcomm` command is available inside `.venv`.

To activate the environment for the session:
```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Or prefix all commands with `uv run`:
```bash
uv run custcomm pipeline
```

---

## Step 3 — Get Your API Keys

### Anthropic (required)
1. Go to https://console.anthropic.com → sign up
2. API Keys → Create Key
3. You get $5 in free credits — plenty for early testing

### Gmail API credentials (required for Gmail backend)
1. Go to https://console.cloud.google.com/ → create a project
2. APIs & Services → Library → enable the **Gmail API**
3. APIs & Services → Credentials → Create Credentials → OAuth client ID
4. Application type: **Desktop app**
5. Download the JSON → save it somewhere inside your CustComm checkout
   (we default to `./credentials.json`)
6. APIs & Services → OAuth consent screen → add your Gmail address as a
   Test User (so you can grant consent while the app is in testing)

---

## Step 4 — Configure Your Environment

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:
```
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_CREDENTIALS_PATH=./credentials.json
```

---

## Step 5 — Configure CustComm

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and set the identity fields at the top:

```yaml
client_name: "Your Business Name"
operator_name: "Your Name"
operator_email: "you@example.com"
```

Leave the rest of the defaults for now — you can tune `outreach.signature`,
the `inbox.gmail.query`, and the `scheduler.business_hours` later.

Take some time on the signature once you're sending live replies — it's
appended verbatim to every outbound message.

---

## Step 6 — Initialize the Database

```bash
uv run custcomm pipeline
```

This creates `./data/custcomm.db` and prints an (empty) pipeline summary:

```
┌────────────────────┬───────┐
│ Status             │ Count │
├────────────────────┼───────┤
│ TOTAL              │ 0     │
└────────────────────┴───────┘
```

---

## Step 7 — First Gmail Poll

```bash
uv run custcomm poll
```

The first time you run this, a browser window opens for the Gmail OAuth
consent flow. Sign in with the Gmail account you want CustComm to triage,
click through the scopes, and you'll be redirected back. A refresh token
is cached at `./.gmail_token.json` (gitignored) so subsequent runs are
non-interactive.

You should see output like:

```
OK Polled Gmail — 7 new messages, 4 new threads created, 3 attached to existing threads.
```

If your inbox is quiet, send yourself a test email first.

---

## Step 8 — Triage Intent

```bash
uv run custcomm triage
```

This classifies every thread in status `NEW` against the built-in intent
taxonomy (new inquiry, follow-up question, appointment request, reschedule,
cancel, complaint, unrelated, uncertain):

```
OK Triaged 4 threads:
  - new_inquiry: 2
  - appointment_request: 1
  - complaint: 1 (auto-escalated)
```

Threads whose classifier confidence falls below `ai.min_intent_confidence`
are marked `uncertain` and won't be auto-drafted. Complaints and a few
other intents default to `ESCALATED` status for human handling. See
`CLAUDE.md` → *Safety / Guardrails*.

---

## Step 9 — Draft Replies

```bash
uv run custcomm draft
```

This drafts a reply for every thread in status `TRIAGED` whose intent is
in `outreach.auto_draft_intents`:

```
OK Drafted 3 replies:
  - abc-123  Jane Doe              Re: Can you help with...
  - def-456  Acme Customer Success Re: Appointment request
  - ghi-789  Sam Smith             Re: Quick question about...
```

Each draft has status `PENDING` and is waiting for your approval.

---

## Step 10 — Review Drafts

```bash
uv run custcomm list --status drafted
```

See a table of threads with pending drafts. For a specific thread:

```bash
uv run custcomm show abc-123
```

You'll see the full thread history plus the proposed reply.

---

## Step 11 — Approve + Send

```bash
uv run custcomm approve abc-123
uv run custcomm send
```

Or do it conversationally through Claude Desktop — see
`docs/MCP_SETUP.md` for that flow.

CustComm will never send without an explicit approve. If you want to see
what would go out without sending, use `--dry-run`:

```bash
uv run custcomm send --dry-run
```

---

## Step 12 — Follow-Up Scheduling

For threads where you've replied and are waiting on the customer:

```bash
uv run custcomm schedule-followups
```

This looks at every thread in status `AWAITING_CUSTOMER` whose last outbound
is older than your `scheduler.followup_days` thresholds and re-enqueues it
into the normal triage → draft flow so you get a nudge-drafted follow-up
to approve.

---

## Step 13 — Connect to Claude Desktop (Recommended)

CustComm's MCP server is the most pleasant way to operate day-to-day. Start it:

```bash
uv run custcomm mcp
```

Then see `docs/MCP_SETUP.md` for the Claude Desktop configuration block.

---

## Common Issues

**`custcomm: command not found`**
→ Your virtual environment isn't activated. Run `source .venv/bin/activate`
  (or `.venv\Scripts\Activate.ps1` on Windows), or prefix commands with `uv run`.

**`Config file not found: config.yaml`**
→ Run `cp config.example.yaml config.yaml` and edit the identity fields.

**`ANTHROPIC_API_KEY is not set`**
→ Your `.env` file is missing or the key isn't set correctly.

**Gmail OAuth "Access blocked: has not completed Google verification"**
→ Add your Gmail address as a Test User on the OAuth consent screen in
  Google Cloud Console while your app is in testing mode.

**MCP server prints "Unexpected token" warnings in Claude Desktop**
→ Something in the engine is writing to stdout. Every new code path reachable
  from `mcp_server/server.py` must use `logging` or `Console(stderr=True)`,
  not `print` or a default `Console()`. See `CLAUDE.md` → *MCP Server Ground Rules*.

---

## Next Steps

- Read `docs/ARCHITECTURE.md` for the deep dive on how everything fits together
- Read `docs/MCP_SETUP.md` to wire CustComm into Claude Desktop
- Read `CLAUDE.md` → *Customization Patterns* when you're ready to plug in a
  custom voice or persona
