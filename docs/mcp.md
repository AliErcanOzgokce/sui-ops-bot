# MCP tool guide

The MCP server gives every dev lead manual, natural-language control over the shared
escalation tracker, straight from Claude (Desktop / Code / any MCP client). It is the
companion to the always-on auto-tracker: the auto-tracker watches the channel and
logs escalations automatically; this MCP lets a human drive.

Both share **one Google Sheet as the source of truth**, so IDs are consistent across
every dev lead and across the auto-tracker.

## The five tools

| Say to Claude | Tool | What it does |
|---------------|------|--------------|
| "post this question, source is `<link>`" | `post_message` | Posts a formatted question to the dev-leads channel and logs a tracker row. Shows the source, an ID, and the `product · type` badge. **product and type are auto-classified from the text**; say them explicitly (e.g. "Walrus, Bug") to override. Asks for a source if you did not give one. |
| "what's the status of 12, 13?" | `check_status` | Reads each thread and reports **Solved / Forwarded / Open** plus the last reply. Pass IDs, or a `product` / `type` filter to check every matching open item. |
| "weekly report" / "show open Walrus bugs" | `weekly_report` | The open backlog **grouped by product**, with optional `product=` / `type=` filters. Preview by default; posts to the channel when you confirm (`post=true`). |
| "mark 12 solved" | `mark_solved` | Adds a check reaction to the message and closes the tracker row(s). |
| "ping 12, 13 for a reply" | `ping` | Replies in each thread tagging the owner (`PING_USER_ID`). |

IDs are the tracker's own numbers. Pass them as `12`, `#12`, `Q-12`, or a list
`12, 13, 15`; all are accepted.

## Taxonomy

`post_message` accepts (and `check_status` / `weekly_report` filter on):

- **product**: DeepBook, Walrus, Harbor, Seal, Nautilus, MemWal, Enoki, Slush,
  zkLogin, SDK, Bridge, Sui Core, Hashi, Program, Other
- **type**: Question, Open PR, Bug, Feature Request, Communication

Leave `product` / `type` / `priority` blank on `post_message` to let the LLM classify;
provide any of them to override that one field.

## Setup

### 1. Slack app scopes

Use the **same** Slack app / bot token as the auto-tracker (same workspace as the
target channel). Bot Token Scopes:

- `chat:write` post and edit its own messages (to stamp in the ID)
- `channels:history` (`groups:history` for private channels) read threads
- `reactions:read` detect an existing check reaction
- `users:read` resolve display names
- `reactions:write` add a check reaction on `mark_solved` (add this, then reinstall)

No Socket Mode / app token is needed for the MCP itself (it only uses the Web API).

> Find `PING_USER_ID`: in Slack, click the person, then the overflow menu, then
> **Copy member ID** (looks like `U0123ABCD`). Without it, `ping` tags the plain text
> `PING_USER_NAME`.

### 2. Config

Reuse the auto-tracker's `.env` (the loader picks up the repo-root `.env`
automatically). Relevant keys:

```bash
MCP_CHANNEL_ID=          # channel to post to; defaults to the first SLACK_CHANNEL_ID
PING_USER_ID=U0123ABCD   # member id to @-tag on ping and in the report
PING_USER_NAME=the owner # fallback text if PING_USER_ID is unset
```

Everything else (`SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `SHEET_ID`, `SHEET_GID`,
`GOOGLE_APPLICATION_CREDENTIALS`) is shared with the auto-tracker. See `.env.example`.

### 3. Install and preflight

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m sui_ops_bot.mcpserver --check      # validates config, does not connect MCP
```

### 4. Smoke test against an internal channel (recommended before rollout)

Point `MCP_CHANNEL_ID` at an internal/test channel, then:

```bash
python -m sui_ops_bot.mcpserver --smoke
```

This runs the whole flow end to end (`post_message` -> `check_status` -> `ping` ->
`mark_solved` -> re-check -> `weekly_report`) against that channel. It posts **real**
messages and a check reaction, then deletes the row and Slack messages it created, so
only run it where flooding is OK.

### 5. Register with Claude

**Claude Code** (from the repo):

```bash
claude mcp add sui-ops -- /absolute/path/to/.venv/bin/python -m sui_ops_bot.mcpserver
```

**Claude Desktop**, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sui-ops": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "sui_ops_bot.mcpserver"]
    }
  }
}
```

The repo-root `.env` is picked up automatically, so no secrets in the config file.

**Docker** (no local Python; easiest to hand to other dev-leads). Build once from the
repo root:

```bash
docker build -f deploy/Dockerfile.mcp -t sui-ops-mcp .
```

Then point Claude at `docker run` (note `-i`, and **no `-t`**; a TTY corrupts the
JSON-RPC stream):

```json
{
  "mcpServers": {
    "sui-ops": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--env-file", "/absolute/path/to/.env",
        "-v", "/absolute/path/to/secrets:/app/secrets:ro",
        "sui-ops-mcp"
      ]
    }
  }
}
```

Restart the client; you should see the `sui-ops` tools. Then just talk:

> **You:** post this to devleads: "Team evaluating Quilt for production wants
> per-patch ownership/ACL/purge; patches are QuiltPatchId not Blob objects." source is
> the walrus issue #3443.
>
> **Claude:** *(calls `post_message`, auto-classifies)* Posted **#48** (Walrus ·
> Feature Request, High) ...

## How it stays consistent with the auto-tracker

- **Same sheet, same schema.** The MCP reuses the auto-tracker's `SheetStore`, so new
  rows get the sheet's own formula-assigned `ID`, land below the header row, and write
  the Slack `ts` as text (never rounded). `Bot Refs` records `"source":"mcp"`.
- **Human-in-the-loop stays human.** `mark_solved` is an explicit human action;
  `check_status` only reports a verdict, it never closes anything on its own.
- **stdout is protocol-only.** MCP speaks JSON-RPC over stdout, so logging is rerouted
  to stderr; diagnostics show up in the client's MCP log pane, never in tool output.

## Troubleshooting

- **`missing_scope` on mark_solved** -> add `reactions:write` and reinstall the app.
- **`channel_not_found` / `not_in_channel`** -> invite the bot (`/invite @YourBot`) and
  confirm `MCP_CHANNEL_ID`.
- **Tools do not appear in Claude** -> check the client's MCP logs; run
  `python -m sui_ops_bot.mcpserver --check` in the same venv to rule out config.
- **"not found in the tracker" on a valid ID** -> IDs are matched on digits, so
  `#12` and `12` are equal; the row may pre-date the bot columns.
