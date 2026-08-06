# Sui Dev-Leads Ops Bot -- Project Context

Turns a Slack channel plus a Google Sheet (the "Open Questions" tab) into an always-organized developer-escalation tracker for Sui dev-relations leads. Two entrypoints share one sheet as the single source of truth.

## Entrypoints

- **Auto-tracker** (long-running Slack Socket Mode app): classifies new messages with an LLM, logs escalations to the sheet, tracks resolution with human-in-the-loop confirmation, and serves status, open, and aging reports.
- **MCP server** (FastMCP): 5 natural-language tools for dev-leads inside Claude, `post_message`, `check_status`, `weekly_report`, `mark_solved`, `ping`. Runs over stdio or token-gated streamable HTTP.

## Classification Taxonomy

This is the core domain model. Document it clearly and keep it in sync with `config.py`.

Both **product** and **type** are LLM-classified. The auto-tracker classifies automatically. The MCP `post_message` tool auto-classifies but the human can override. They are stored in two sheet columns (`Product`, `Type`) that the bot auto-adds. The legacy `Escalated To` column is left untouched.

- **product** (one of 15): DeepBook, Walrus, Harbor, Seal, Nautilus, MemWal, Enoki, Slush, zkLogin, SDK, Bridge, Sui Core, Hashi, Program, Other
- **type** (one of 5): Question, Open PR, Bug, Feature Request, Communication
- **priority**: High, Medium, Low

## Repo Structure

Planned final layout:

```
src/sui_ops_bot/
  config.py         env + taxonomy constants
  ids.py            id/source parsing, pure
  logutil.py        log/audit, stderr-switchable for MCP
  classify.py       Anthropic tool-use classifier + resolution judge
  sheet.py          Row + SheetStore, Google Sheets
  reports.py        pure report builders: status/open/aging/weekly, grouping + product/type filters
  slack_client.py   shared Slack WebClient + helpers
  slackbot.py       auto-tracker runtime + event handlers + main
  mcpserver.py      FastMCP tools + http server + main
tests/              pytest on pure logic (ids, taxonomy, reports, field mapping) with mocks; no network
deploy/             Dockerfile, Dockerfile.mcp, docker-compose.yml, .dockerignore
docs/               MCP usage + operations docs
.claude/            this config
pyproject.toml      console_scripts: sui-ops-bot and sui-ops-mcp
```

## Run Commands

- Auto-tracker: `python -m sui_ops_bot.slackbot`   (`--check` preflight, `--diag` live diagnostics)
- MCP stdio: `python -m sui_ops_bot.mcpserver`
- MCP HTTP: `python -m sui_ops_bot.mcpserver --http`
- Tests: `pytest -q`
- Lint: `ruff check .`

## Config

All config is via env (a `.env` next to the code, nothing committed; see `.env.example`) plus a mounted Google service-account json.

Key vars:

- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-haiku-4-5`)
- `SHEET_ID`, `SHEET_GID`, `GOOGLE_APPLICATION_CREDENTIALS`
- `MCP_CHANNEL_ID`, `PING_USER_ID`, `MCP_HTTP_TOKEN`

## Health Stack

- lint: `ruff check .`
- tests: `pytest -q`
- bot preflight: `python -m sui_ops_bot.slackbot --check` (validates config, tokens, and sheet access; no other network side effects)
- mcp preflight: `python -m sui_ops_bot.mcpserver --check`

All four must pass before `/ship`. See `.claude/rules/SOP.md` for the full Build & Test Gate.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:

- Product ideas/brainstorming: invoke /office-hours
- Strategy/scope: invoke /plan-ceo-review
- Architecture: invoke /plan-eng-review
- Full review pipeline: invoke /autoplan
- Bugs/errors: invoke /investigate
- Refactor with no behavior change: invoke /simplify
- Code review/diff check: invoke /review
- QA/testing behavior: invoke /qa or /qa-only
- Ship/deploy/PR: invoke /ship
- Save progress: invoke /context-save
- Resume context: invoke /context-restore

## Writing rules

- NEVER use em dashes (---, &mdash;, or the character) anywhere. Not in UI copy, comments, docs, prompts, log lines, or any generated text. Use a period, comma, or parentheses instead.
- ALWAYS write the product name as "Sui Dev-Leads Ops Bot". This applies everywhere: docs, comments, prompts, and any generated text.
- Use plain language. Write for dev-relations leads and the engineers integrating with the bot, not for internal implementation trivia.

## Commit message rules

- NEVER add `Co-Authored-By` trailers to commit messages.
- Follow the Conventional Commits format defined in `.claude/rules/git-standards.md`.
- Scopes MUST be one of: `bot`, `mcp`, `sheet`, `classify`, `reports`, `config`, `deploy`, `docs`, `tests`, `ci`.
- **Commit early, commit often.** Make small, atomic commits after each logical unit of work on a branch. Do not batch multiple changes into one large commit. The full test gate is required only before `/ship`, not before every commit.

## Task tracking

- NEVER create, update, or interact with Linear issues. We do NOT use Linear for task tracking. All issues and task tracking happen on GitHub Issues exclusively. See `.claude/rules/SOP.md`.

## Secrets

- NEVER commit `.env` or anything under `secrets/`. Both are gitignored.
- The Google service-account json and all Slack, Anthropic, and MCP tokens are secrets. They live only in the environment or in a mounted file, never in the repo.
- Use `.env.example` to document the shape of the config without any real values.
