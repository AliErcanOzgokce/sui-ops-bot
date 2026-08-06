# Operations

Deployment and day-to-day running of the two entrypoints. Both read config from the
environment (a repo-root `.env` is loaded automatically) plus a mounted Google
service-account json.

## Configuration

See `.env.example` for the full annotated list. The essentials:

| Var | Used by | Notes |
|-----|---------|-------|
| `SLACK_BOT_TOKEN` | both | `xoxb-...` bot token |
| `SLACK_APP_TOKEN` | auto-tracker | `xapp-...` Socket Mode token |
| `SLACK_CHANNEL_ID` | both | one id, or a comma-separated list |
| `ANTHROPIC_API_KEY` | both | classification + resolution judging |
| `ANTHROPIC_MODEL` | both | default `claude-haiku-4-5` |
| `SHEET_ID`, `SHEET_GID` | both | sheet key + numeric tab gid (robust than name) |
| `GOOGLE_APPLICATION_CREDENTIALS` | both | path to the service-account json |
| `MCP_CHANNEL_ID` | MCP | post target; defaults to first `SLACK_CHANNEL_ID` |
| `PING_USER_ID` | MCP | member id to @-tag on ping / report |
| `MCP_HTTP_TOKEN` | MCP HTTP | required to start the remote endpoint |

The Google Sheet must be shared with the service-account email (Editor).

## Docker Compose

From the repo root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build sui-ops-bot
```

This runs the auto-tracker (Socket Mode, outbound WebSocket only, no ports). The
`secrets/` directory is mounted read-only and `audit.jsonl` is persisted host-side.

## Remote MCP endpoint

Instead of every dev lead running the MCP locally with secrets, host it once and hand
out a URL plus a token. This is the `sui-ops-mcp-http` service in the compose file.

```bash
docker compose -f deploy/docker-compose.yml up -d --build sui-ops-mcp-http
```

It serves streamable HTTP at `:8787/mcp` behind a bearer-token gate. `GET /healthz` is
open for liveness; every other request needs `Authorization: Bearer <MCP_HTTP_TOKEN>`.
The server **refuses to start without `MCP_HTTP_TOKEN`**.

Config (in `.env`):

```bash
MCP_HTTP_TOKEN=<a long random secret>   # required
MCP_HTTP_PORT=8787                       # optional, default 8787
```

A dev lead adds it in Claude Code with one command, no repo or secrets needed:

```bash
claude mcp add --transport http sui-ops http://<server>:8787/mcp \
  --header "Authorization: Bearer <MCP_HTTP_TOKEN>"
```

Notes:

- **Plain HTTP sends the token in cleartext.** Fine for an internal POC on a trusted
  network. For real rollout put it behind TLS (reverse-proxy the container) and hand
  out an `https://...` URL.
- **Open the port.** `8787` must be reachable from the dev lead's machine (host / cloud
  firewall).

## Health checks

```bash
python -m sui_ops_bot.slackbot --check    # config preflight (tokens, sheet, creds)
python -m sui_ops_bot.slackbot --diag     # live read-only diagnostics
python -m sui_ops_bot.mcpserver --check   # MCP config preflight
ruff check . && pytest                    # lint + unit tests
```

## The audit trail

Every LLM decision (classify, resolution) and every MCP mutation (`post`,
`mark_solved`, `ping`, report posted) is written to `AUDIT_LOG_PATH` (default
`audit.jsonl`) as one JSON object per line, and echoed to the process log. The MCP
routes both to stderr so stdout stays pure JSON-RPC.

## Sheet schema notes

- The bot auto-adds any missing managed columns to the header row on boot: `Product`,
  `Type`, `Slack Channel`, `Slack TS`, `Bot Refs`. Existing human columns are never
  touched, and `Escalated To` is kept as a legacy column (no longer written).
- New rows leave `ID` to the sheet's own formula and write `Slack TS` as raw text so
  the numeric-looking timestamp is not parsed and rounded (which would break ts to row
  matching).
- The in-memory index is rebuilt from the sheet on boot and after every mutation, so
  redeploys on ephemeral hosts do not lose tracking.
