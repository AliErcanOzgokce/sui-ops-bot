# Sui Dev Leads Ops — MCP

A **Model Context Protocol** server that gives every dev lead manual, natural-language
control over the shared escalation tracker, straight from Claude (Desktop / Code /
any MCP client). It's the companion to the always-on auto-tracker (`bot.py`): the bot
watches the channel and logs escalations automatically; this MCP lets a human *drive* —
"post this", "ping 12 and 13", "what's the status of 7", "give me the weekly report".

Both share **one Google Sheet as the source of truth**, so IDs are consistent across
every dev lead and across the bot. You can run either, or both.

## The five tools

| Say to Claude…                                   | Tool           | What it does |
|--------------------------------------------------|----------------|--------------|
| "post this question, source is <link>"           | `post_message` | Posts a formatted question to the dev-leads channel and logs a tracker row. Shows **source**, an **ID**, and the **department** (SolEng / DevRel / DevX / None). Asks for a source if you didn't give one. |
| "what's the status of 12, 13?"                   | `check_status` | Reads each thread and reports **Solved / Forwarded / Open** + the last reply. Solved = row closed **or** a ✅ on the message **or** Claude judges the thread answered. Forwarded = someone replied "forwarded". |
| "weekly report" / "show the open questions"      | `weekly_report`| The open (unanswered) backlog with IDs, ages, owners and links. Preview by default; posts to the channel when you confirm (`post=true`). |
| "mark 12 solved"                                 | `mark_solved`  | Adds ✅ to the message and closes the tracker row(s). |
| "ping 12, 13 for a reply"                        | `ping`         | Replies in each thread tagging the owner (`PING_USER_ID`). |

IDs are the tracker's own numbers. You can pass them as `12`, `#12`, `Q-12`, or a
list `12, 13, 15` — all are accepted.

## Setup

### 1. Slack app scopes

Use the **same** Slack app / bot token as the auto-bot (same workspace as the target
channel). The MCP needs these **Bot Token Scopes** — the first four the bot already
uses, plus **`reactions:write`** (to add ✅) which you likely need to add + **reinstall**:

- `chat:write` — post, and edit its own messages (to stamp in the ID)
- `channels:history` (`groups:history` for private channels) — read threads
- `reactions:read` — detect an existing ✅
- `users:read` — resolve display names
- **`reactions:write`** — add ✅ on `mark_solved`  ← add this, then reinstall the app

No Socket Mode / app token is needed for the MCP itself (it only uses the Web API).

> Find `PING_USER_ID`: in Slack, click the person → **⋮** → **Copy member ID**
> (looks like `U0123ABCD`). Without it, `/ping` tags the plain text `PING_USER_NAME`.

### 2. Config

Reuse the bot's `.env` (the MCP loads `<this dir>/.env` automatically). Add:

```bash
MCP_CHANNEL_ID=          # channel to post to; defaults to the first SLACK_CHANNEL_ID
PING_USER_ID=U0123ABCD   # Domenico's member id, tagged by /ping and the report
PING_USER_NAME=the owner # fallback text if PING_USER_ID is unset
```

Everything else — `SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `SHEET_ID`, `SHEET_GID`,
`GOOGLE_APPLICATION_CREDENTIALS` — is shared with `bot.py`. See `.env.example`.

### 3. Install deps and preflight

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-mcp.txt
python mcp_server.py --check      # validates config, does not connect MCP
```

`--check` verifies the bot token, channel, Anthropic key, sheet id and the
service-account file, and warns if `PING_USER_ID` is unset.

### 3b. Smoke test against an internal channel (recommended before rollout)

Point `MCP_CHANNEL_ID` at an internal/test channel (e.g. `#ttest`), then:

```bash
python mcp_server.py --smoke
```

This runs the whole flow end-to-end — `post_message` → `check_status` → `ping` →
`mark_solved` → re-check → `weekly_report` — against that channel. It posts **real**
messages and a ✅ and closes the row it creates, so only run it where flooding is OK.
Review the channel and the sheet afterwards. This is exactly the "create an internal
channel to test it first" step Domenico suggested.

### 4. Register with Claude

**Claude Desktop** — add to `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "sui-ops": {
      "command": "/absolute/path/to/sui-ops-bot/.venv/bin/python",
      "args": ["/absolute/path/to/sui-ops-bot/mcp_server.py"]
    }
  }
}
```

`.env` next to `mcp_server.py` is picked up automatically, so no secrets in the
config file. (You *can* instead pass them in an `"env": { ... }` block if you prefer.)

**Claude Code** — from the repo:

```bash
claude mcp add sui-ops -- /absolute/path/to/.venv/bin/python /absolute/path/to/mcp_server.py
```

**Docker (no local Python — easiest to hand to other devleads).** Build once:

```bash
docker build -f Dockerfile.mcp -t sui-ops-mcp .
```

Then point Claude at `docker run` (note **`-i`**, and **no `-t`** — a TTY corrupts
the JSON-RPC stream). Secrets come from `--env-file` and the mounted service account:

```json
{
  "mcpServers": {
    "sui-ops": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--env-file", "/absolute/path/to/sui-ops-bot/.env",
        "-v", "/absolute/path/to/sui-ops-bot/secrets:/app/secrets:ro",
        "sui-ops-mcp"
      ]
    }
  }
}
```

(`GOOGLE_APPLICATION_CREDENTIALS=secrets/service_account.json` in `.env` resolves to
the mounted path inside the container.)

Restart the client; you should see the `sui-ops` tools available. Then just talk:

> **You:** post this to the devleads channel — "Team evaluating Quilt for
> production wants per-patch ownership/ACL/purge; patches are QuiltPatchId not Blob
> objects." source is the walrus issue #3443, category DevX, priority high.
>
> **Claude:** *(calls `post_message`)* ✅ Posted **#48** (DevX, High) …

## How it stays consistent with the bot

- **Same sheet, same schema.** The MCP reuses `bot.py`'s `SheetStore`, so new rows
  get the sheet's own formula-assigned `ID`, land below the auto-detected header row,
  and write the Slack `ts` as text (never rounded). A row posted by the MCP is
  indistinguishable from one the bot logged — `Bot Refs` just records `"source":"mcp"`.
- **Human-in-the-loop stays human.** `mark_solved` is an explicit human action;
  `check_status` only *reports* a verdict, it never closes anything on its own.
- **stdout is protocol-only.** MCP speaks JSON-RPC over stdout, so the server reroutes
  `bot.py`'s diagnostics to **stderr** — you'll see logs in the client's MCP log pane,
  never in the tool output.

## Troubleshooting

- **`missing_scope` on mark_solved** → add `reactions:write` and reinstall the app.
- **`channel_not_found` / `not_in_channel`** → invite the bot: `/invite @YourBot`,
  and confirm `MCP_CHANNEL_ID`.
- **Tools don't appear in Claude** → check the client's MCP logs; run
  `python mcp_server.py --check` in the same venv to rule out config.
- **"not found in the tracker"** on a valid ID → the row may pre-date the bot columns,
  or the sheet's `ID` rendered differently; IDs are matched on digits, so `#12`≡`12`.

---

## Remote mode — let other devleads add it with just a URL

Instead of every dev lead running the MCP locally with secrets, host it **once** and
hand out a URL + token. This is the `sui-ops-mcp-http` service in `docker-compose.yml`.

Run it (already wired):

```bash
docker compose up -d --build sui-ops-mcp-http   # serves :8787/mcp, token-auth
```

Config (in `.env`):

```bash
MCP_HTTP_TOKEN=<a long random secret>   # required; the server refuses to start without it
MCP_HTTP_PORT=8787
```

`python mcp_server.py --http` serves streamable-HTTP at `/mcp` behind a bearer-token
gate (pure-ASGI, so it doesn't buffer MCP's streaming). `GET /healthz` is open for
liveness checks; everything else needs `Authorization: Bearer <MCP_HTTP_TOKEN>`.

A dev lead adds it in **Claude Code** with one command — no repo, no secrets, no
service-account file:

```bash
claude mcp add --transport http sui-ops http://<server>:8787/mcp \
  --header "Authorization: Bearer <MCP_HTTP_TOKEN>"
```

Notes:
- **Transport is plain HTTP** — the token travels in cleartext. Fine for an internal
  POC on a trusted network; for real rollout put it behind TLS (reverse-proxy the
  container, e.g. via the existing nginx) and hand out an `https://…` URL.
- **Open the port.** Make sure `8787` is reachable from the dev lead's machine (host
  firewall / cloud firewall / security group).
- **Claude Desktop** doesn't take a custom bearer-header remote MCP from its UI as
  cleanly as Claude Code; easiest is Claude Code, or bridge with `mcp-remote`.
