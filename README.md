# Sui Dev Leads Ops Bot

A self-hosted Python bot that turns a Slack channel + a Google Sheet ("Open
Questions" tab) into a low-effort, always-organized escalation tracker. It runs as
a long-lived process over **Slack Socket Mode** — no public URL, no inbound ports.

- **Feature 1 — Auto-log escalations.** A cheap local pre-filter drops chatter;
  substantive top-level messages go to **Claude Haiku**, which decides if it's a
  new developer escalation and extracts structured fields. If yes, a row is
  appended (`Status=Escalated`) and the bot posts an in-thread note with a deep
  link to the row. React **:x:** to discard a false positive.
- **Feature 2 — Auto-update on resolution (human-in-the-loop).** A thread reply on
  a tracked message, or a **:white_check_mark:** on it, triggers Claude to judge
  resolution. If resolved, the bot sets `Status=Needs review` (it **never**
  auto-closes on the model's judgment alone), fills a proposed resolution +
  `Date Resolved`, and asks the owner to react **:white_check_mark:** to confirm.
  A confirming reaction on the bot's reply sets `Status=Closed`.
- **Feature 3 — Status commands.** Native slash commands `/status`, `/open`,
  `/aging` (need the `commands` scope), **plus** a text fallback that needs no
  extra scopes: `!status` · `!open` · `!aging`, or `@bot status | open | aging`.
  Total open, breakdown by Escalated To, aging count, and the oldest open item.

## How it stays reliable

- **The Google Sheet is the source of truth.** The bot stores three of its own
  columns — `Slack Channel`, `Slack TS`, `Bot Refs` (JSON) — and rebuilds its
  in-memory index from the sheet on every boot. A redeploy on an ephemeral host
  (Railway/Render/Fly) does **not** lose tracking of in-flight threads.
- **Backfill on boot.** Socket Mode does not replay events missed while offline,
  so on startup the bot scans the last `BACKFILL_HOURS` (default 24) of channel
  history and classifies any substantive, untracked messages.
- **Every Claude decision is audited** to stdout and a local `audit.jsonl`
  (message, verdict, token counts).
- **Human-in-the-loop for closing.** Items are never hard-closed on LLM judgment
  alone — a human ✅ is always required.

> ⚠️ **The bot must be installed in the SAME Slack workspace as the target
> channel.** A bot in your personal workspace cannot read a channel in the Sui
> Foundation workspace, and it cannot read a **Slack Connect** shared channel
> unless it is installed in the workspace that owns that channel. Install it where
> the channel lives, and invite it to the channel (`/invite @YourBot`).

---

## 1. Create the Slack app (Socket Mode)

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
   Pick the workspace that owns the target channel.
2. **Socket Mode** → toggle **Enable Socket Mode** on. When prompted, create an
   **App-Level Token** with scope `connections:write`. Copy it — this is your
   `SLACK_APP_TOKEN` (`xapp-…`).
3. **OAuth & Permissions** → **Bot Token Scopes**, add:
   - `chat:write`
   - `channels:history` (use `groups:history` too if the channel is private)
   - `reactions:read`
   - `users:read`
   - `commands`
   - `app_mentions:read`
4. **Event Subscriptions** → enable → **Subscribe to bot events**:
   - `message.channels` (and/or `message.groups` for private channels)
   - `reaction_added`
   - `app_mention`
5. **Slash Commands** → create three. With Socket Mode the Request URL can be any
   placeholder (e.g. `https://example.com/slack`) — it is not called.
   - `/status`, `/open`, `/aging`
6. **Install App** to the workspace. Copy the **Bot User OAuth Token** — this is
   your `SLACK_BOT_TOKEN` (`xoxb-…`).
7. In Slack, invite the bot to the channel: `/invite @YourBot`, and grab the
   channel ID (channel → **View channel details** → bottom, `C…`). That's
   `SLACK_CHANNEL_ID`.

> If you change scopes or events later, **reinstall** the app.

## 2. Anthropic API key

Create a key at <https://console.anthropic.com/> → **API Keys**. That's
`ANTHROPIC_API_KEY`. The bot uses `claude-haiku-4-5` for cheap classification;
override with `ANTHROPIC_MODEL` if needed.

## 3. Google service account + share the sheet

1. <https://console.cloud.google.com/> → create/select a project.
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **APIs & Services → Credentials → Create credentials → Service account.**
   Create it, then under the service account → **Keys → Add key → JSON**.
   Download the JSON and save it as `secrets/service_account.json`.
4. Open the JSON and copy the `client_email` (looks like
   `something@project.iam.gserviceaccount.com`).
5. **Share your Google Sheet** with that email as **Editor**.
6. `SHEET_ID` is the long id in the sheet URL:
   `https://docs.google.com/spreadsheets/d/`**`<SHEET_ID>`**`/edit`.
   `SHEET_TAB` defaults to `Open Questions`.

On first boot the bot appends its three bot-owned columns (`Slack Channel`,
`Slack TS`, `Bot Refs`) to the header row if they're missing. Existing human
columns are matched **by name**, so their order doesn't matter.

### Fits an existing OnCall tracker

This bot was built against a live "Sui OnCall — Schedule & Open Questions" sheet and
adapts to its conventions automatically:

- **Header row is auto-detected** (the sheet has intro rows above it — the header
  isn't row 1). Data is read/written below that row.
- **`ID` is left to the sheet's own formula** — the bot replicates the existing
  `=IF(COUNTA(...);"";MAX(...)+1)` formula on each new row, so IDs stay automatic.
- **Status vocabulary is respected:** `Open · In Progress · Answered · Escalated ·
  Closed`. New rows are `Escalated` (+ `Escalated To`) when a target is suggested,
  else `Open`. On a proposed resolution the Status is **left unchanged** (item stays
  active on the Dashboard) and the proposal is written to `Notes / Handoff` as
  `⏳ Proposed (react ✅ to close): …`; a confirming ✅ sets `Closed` + `Date Resolved`.
  A ❌ on a false positive **deletes** the row.
- **`Window`** = the on-call shift (APAC/EMEA/Americas); set `WINDOW_DEFAULT` or leave
  blank. **`Owner`** defaults to `Unassigned` (`OWNER_DEFAULT`). **`Platform`** = source
  medium (Telegram/GitHub/Forum/…), **`Channel`** = the specific venue.
- The `Notes` column is matched by prefix, so `Notes / Handoff` is found.

## 4. Configure

```bash
cp .env.example .env      # then fill in the values
# place the Google JSON at secrets/service_account.json
```

## 5. Run

### Local container (recommended)

```bash
# audit.jsonl must exist as a file for the bind-mount:
touch audit.jsonl

docker compose build
docker compose up -d
docker compose logs -f          # watch it connect + backfill
```

Validate configuration without connecting to Slack:

```bash
docker compose run --rm sui-ops-bot python bot.py --check
```

### Bare metal

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py --check     # preflight
python bot.py             # run
```

### Always-on hosts

Socket Mode needs only outbound network, so it runs anywhere that keeps a process
alive:

- **VPS (systemd):** `docker compose up -d` (has `restart: unless-stopped`), or a
  systemd unit running `python bot.py`.
- **Railway / Render / Fly.io:** deploy as a **worker / background** service (no
  HTTP port). Set all env vars in the dashboard; paste the service-account JSON as
  a secret file mounted at `secrets/service_account.json` (or point
  `GOOGLE_APPLICATION_CREDENTIALS` at wherever the platform mounts it). Because
  the sheet is the source of truth, redeploys don't lose tracked threads — but
  the local `audit.jsonl` is ephemeral there, so rely on stdout logs (captured by
  the platform) for the audit trail.

## Environment variables

See the two tables the operator was handed, or `.env.example`. Required:
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`, `ANTHROPIC_API_KEY`,
`SHEET_ID`, and a service-account JSON at `GOOGLE_APPLICATION_CREDENTIALS`.

## Sheet columns

Human columns (unchanged): `ID, Date Asked, Window, Platform, Channel, Question
Summary, Link, Raised By, Owner, Priority, Status, Escalated To, Date Resolved,
Notes`. Bot-owned columns (auto-added): `Slack Channel, Slack TS, Bot Refs`.

- `ID` — auto `Q-0001`, `Q-0002`, … (max existing + 1).
- `Owner` — the Slack poster (resolved real name).
- `Priority` ∈ `High | Medium | Low`; `Escalated To` ∈ `SolEng | DevRel | DevX | None`.
- `Status` lifecycle: `Escalated → Needs review → Closed`, or `Escalated →
  Discarded`.
- `Window` — **left blank on append.** Give the maintainer a vocabulary and it's a
  one-line change in `classify_and_log`.

## Diagnostics

```bash
docker compose run --rm sui-ops-bot python bot.py --diag
```

Live end-to-end check (read-only, plus one tiny Anthropic call; does **not** open
Socket Mode): Slack identity + which name channels render, channel membership +
history read, Anthropic classification both ways, and Sheet access. Run this before
inviting the bot anywhere.

## Fixing a stale bot name

Slack caches the bot's rendered name (`bots_info.name`) separately from the app's
display info, so a rename can leave the **old** name showing in channels. To fix:

1. api.slack.com/apps → your app → **App Home** → *Your App's Presence in Slack* →
   edit **App Display Name** (and default username). Save.
2. **Basic Information → Display Information** → set the App name + icon. Save.
3. **OAuth & Permissions → Reinstall to Workspace.** This is the step that actually
   pushes the new identity so `bots_info` stops returning the old name.
4. If a channel still shows the old name: remove the bot from the channel and
   re-invite it.

**Guaranteed override:** add the **`chat:write.customize`** bot scope, reinstall,
then set `BOT_USERNAME` (and optionally `BOT_ICON_EMOJI`) in `.env`. Every message
the bot posts then uses that name/icon regardless of the cached identity.

## Notes & guardrails

- **Never auto-close.** Resolution only ever sets `Needs review`; a human ✅ closes.
- **Every Claude decision is logged** (`audit.jsonl` + stdout) with the message,
  the verdict, and token usage — auditable after the fact.
- Costs are bounded: a local pre-filter (bot/thread/join/short-ack/already-tracked)
  runs before any Claude call, and resolution checks only fire on rows still in
  `Escalated`.
