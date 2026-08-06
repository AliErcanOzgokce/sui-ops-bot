# Sui Dev-Leads Ops Bot

Turns a Slack channel plus one Google Sheet (the "Open Questions" tab) into an
always-organized developer-escalation tracker for Sui dev-relations leads.

Two entrypoints share the **same sheet as the single source of truth**, so IDs
are consistent across every dev lead:

| Entrypoint | What it is | What it does |
|------------|-----------|--------------|
| **Auto-tracker** (`sui_ops_bot.slackbot`) | Always-on Slack Socket Mode app | Classifies new messages with an LLM, logs escalations to the sheet, tracks resolution with human-in-the-loop confirmation, serves `status` / `open` / `aging` reports. |
| **MCP server** (`sui_ops_bot.mcpserver`) | FastMCP tools inside Claude | Five natural-language tools for dev-leads: `post_message`, `check_status`, `weekly_report`, `mark_solved`, `ping`. Runs over stdio or token-gated HTTP. |

Run either, or both. See [`docs/mcp.md`](docs/mcp.md) for the MCP tool guide and
[`docs/operations.md`](docs/operations.md) for deployment and day-to-day ops.

## Classification taxonomy

Every escalation is classified on two independent axes (plus a priority). Both are
LLM-classified: the auto-tracker does it automatically; the MCP `post_message` tool
auto-classifies but lets the human override. They are written to two sheet columns
(`Product`, `Type`) the bot auto-adds; the legacy `Escalated To` column is left
untouched. The lists live in `src/sui_ops_bot/config.py`.

- **product** (15): DeepBook, Walrus, Harbor, Seal, Nautilus, MemWal, Enoki, Slush,
  zkLogin, SDK, Bridge, Sui Core, Hashi, Program, Other
- **type** (5): Question, Open PR, Bug, Feature Request, Communication
- **priority**: High, Medium, Low

## Quickstart

```bash
# 1. Install (editable, with the dev toolchain)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env          # fill in the values
#   plus a Google service-account json at secrets/service_account.json

# 3. Validate config (no side effects beyond token/sheet checks)
python -m sui_ops_bot.slackbot --check
python -m sui_ops_bot.mcpserver --check

# 4. Run
python -m sui_ops_bot.slackbot          # auto-tracker (long-running)
python -m sui_ops_bot.mcpserver         # MCP over stdio (launched by the client)
```

`--diag` runs live read-only diagnostics for the auto-tracker (Slack identity,
channel membership, one classify call, sheet access).

## How it works

- **Auto-log.** A cheap local pre-filter drops chatter; substantive top-level
  messages go to Claude, which decides if a message is a *new* developer escalation
  and extracts the structured fields (summary, platform, product, type, priority).
  If yes, a row is appended and the bot posts an in-thread note with a deep link.
  React `:x:` to discard a false positive.
- **Resolve, human-in-the-loop.** A thread reply on a tracked message, or a
  `:white_check_mark:` on it, triggers Claude to judge resolution. If it looks
  resolved, the bot proposes closure and asks the owner to confirm with a
  `:white_check_mark:`; the confirming reaction closes the row. Nothing is ever
  auto-closed.
- **Report.** `weekly_report` (MCP) groups the open backlog by product and accepts
  `product=` / `type=` filters. The auto-tracker answers `!status`, `!open`,
  `!aging` in-channel.

The sheet holds the state: two classification columns (`Product`, `Type`) plus
three infra columns (`Slack Channel`, `Slack TS`, `Bot Refs`) let the in-memory
index be rebuilt on every boot, so redeploys on ephemeral hosts never lose tracking.

## Layout

```
src/sui_ops_bot/   config, ids, logutil, classify, sheet, reports,
                   slack_client, slackbot (auto-tracker), mcpserver (MCP)
tests/             pytest on the pure logic (no network)
deploy/            Dockerfiles + docker-compose
docs/              MCP tool guide + operations
.claude/           SOP + git-standards + project context (CLAUDE.md at root)
```

## Development

```bash
pytest            # unit tests on the pure logic (ids, taxonomy, reports)
ruff check .      # lint
```

The workflow (branch, TDD, review, ship) is documented in
[`.claude/rules/SOP.md`](.claude/rules/SOP.md). Task tracking is GitHub Issues only.

## Security

`.env` and everything under `secrets/` are gitignored and must never be committed.
All Slack, Anthropic, and MCP tokens plus the Google service-account json live only
in the environment or a mounted file. The remote MCP HTTP endpoint refuses to start
without `MCP_HTTP_TOKEN` and gates every request behind it.
