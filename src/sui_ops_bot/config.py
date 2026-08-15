"""Configuration and the classification taxonomy.

Everything the bot needs to know that is NOT a secret lives here as a plain
constant; everything that IS a secret (or deployment-specific) is read from the
environment. Import this module anywhere; it does no I/O beyond reading env vars.

The two classification axes (``PRODUCTS`` and ``TYPES``) are the core domain
model. They are deliberately simple hard-coded lists: editing the taxonomy is a
code change with a test and a review, not a silent config drift.
"""
from __future__ import annotations

import os

from ._dotenv import load_dotenv

# Populate os.environ from a local .env (if present) before reading anything.
load_dotenv()

# ---------------------------------------------------------------------------
# Secrets / deployment config (env only)
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
# One channel id, or a comma-separated list.
SLACK_CHANNEL_IDS = [c.strip() for c in os.environ.get("SLACK_CHANNEL_ID", "").split(",") if c.strip()]

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
# Pass screenshots to the classifier. Requires a vision-capable model (the
# default claude-haiku-4-5 is). Set CLASSIFY_VISION=0 to force text-only if a
# non-vision model is configured; the classifier also fails soft to text-only on
# any vision call error.
CLASSIFY_VISION = os.environ.get("CLASSIFY_VISION", "1").strip().lower() not in ("0", "false", "no", "")

SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_TAB = os.environ.get("SHEET_TAB", "Open Questions")
# If set, open the worksheet by numeric gid (from the sheet URL) instead of by
# name. Robust when the tab is not literally named "Open Questions".
SHEET_GID = os.environ.get("SHEET_GID", "")
GOOGLE_CREDENTIALS_FILE = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/service_account.json"),
)

TZ = os.environ.get("TZ", "UTC")
BACKFILL_HOURS = int(os.environ.get("BACKFILL_HOURS", "24"))
AGING_DAYS = int(os.environ.get("AGING_DAYS", "3"))
AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "audit.jsonl")
MIN_MESSAGE_CHARS = int(os.environ.get("MIN_MESSAGE_CHARS", "25"))
# Hard cap on the Question Summary the bot writes, so the sheet stays a scannable
# one-liner even if the model rambles. The prompt asks for shorter; this is the net.
SUMMARY_MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "110"))
# When a new question has no clear source (or the classifier says the reporter
# still owes info), ask in-thread before opening the row instead of logging a thin
# one. Set ASK_WHEN_UNSOURCED=0 to go back to always logging silently.
ASK_WHEN_UNSOURCED = os.environ.get("ASK_WHEN_UNSOURCED", "1").strip().lower() not in ("0", "false", "no", "")
# How long a held (unanswered) question waits before it is logged anyway, so a
# question is never lost just because nobody replied with the source.
PENDING_TIMEOUT_HOURS = int(os.environ.get("PENDING_TIMEOUT_HOURS", "24"))

# Optional identity override for the bot's own messages (needs the
# chat:write.customize scope). Useful when a renamed app still shows a stale name.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
BOT_ICON_EMOJI = os.environ.get("BOT_ICON_EMOJI", "")

# On a new escalation the bot fills Window with this (blank = leave for a human).
WINDOW_DEFAULT = os.environ.get("WINDOW_DEFAULT", "")
# Owner on a freshly auto-logged row until someone picks it up.
OWNER_DEFAULT = os.environ.get("OWNER_DEFAULT", "Unassigned")

# ---- MCP-specific ----
# Slack member id to @-tag on ping / in the weekly report (e.g. "U0123ABCD").
# If empty, PING_USER_NAME is used as plain text.
PING_USER_ID = os.environ.get("PING_USER_ID", "").strip()
PING_USER_NAME = os.environ.get("PING_USER_NAME", "the owner").strip()
# Channel the MCP posts new questions / reports to. Defaults to the first
# configured SLACK_CHANNEL_ID.
MCP_CHANNEL_ID = (os.environ.get("MCP_CHANNEL_ID", "").strip()
                  or (SLACK_CHANNEL_IDS[0] if SLACK_CHANNEL_IDS else ""))

# ---------------------------------------------------------------------------
# Classification taxonomy (the core domain model)
# ---------------------------------------------------------------------------
# The ecosystem product / area a developer question is about. The LLM is
# constrained to exactly these strings. "Program" = non-product program /
# logistics questions (Overflow deadlines, track changes). "Other" = anything
# that fits no specific product. Edit here to change the taxonomy.
PRODUCTS = [
    "DeepBook", "Walrus", "Harbor", "Seal", "Nautilus", "MemWal", "Enoki",
    "Slush", "zkLogin", "SDK", "Bridge", "Sui Core", "Hashi", "Program", "Other",
]

# The kind of ask. Question = technical/how-to; Open PR = a PR needs review;
# Bug = bug/incident/outage/funds-stuck; Feature Request = enhancement ask;
# Communication = feedback, docs feedback, positioning.
TYPES = ["Question", "Open PR", "Bug", "Feature Request", "Communication"]

PRIORITIES = ["High", "Medium", "Low"]

# Common venues offered by the optional one-tap set-source menu on the log note.
# Agreed with the team; selecting one writes it to the Channel column. Editing
# this list changes the menu (a code change with a test, like the taxonomy).
SOURCE_VENUES = [
    "Telegram", "Discord", "Sui Developer Forum", "GitHub Issues",
    "X / Twitter", "Email", "Other",
]

# Who an open item is waiting on. A light who-are-we-waiting-on tag, not team
# routing: 'organizer' for program/logistics, 'internal team' for a technical
# escalation, 'reporter' when we need more info back. Inferred at classification
# time and left blank when unclear.
WAITING_ON = ["internal team", "organizer", "reporter"]

# Fallback values used when the LLM output cannot be matched to an enum.
PRODUCT_DEFAULT = "Other"
TYPE_DEFAULT = "Question"
# Waiting On is optional: an unknown party is a blank cell, never a guess.
WAITING_ON_DEFAULT = ""

# ---------------------------------------------------------------------------
# Sheet schema
# ---------------------------------------------------------------------------
# The human columns that already exist on the live sheet. "Escalated To" is kept
# as a legacy column: the bot no longer writes it (Product/Type replaced it), but
# historical values stay readable.
HUMAN_COLUMNS = [
    "ID", "Date Asked", "Window", "Platform", "Channel", "Question Summary",
    "Link", "Raised By", "Owner", "Priority", "Status", "Escalated To",
    "Date Resolved", "Notes",
]
# Columns the bot ensures exist (auto-added to the header row on boot if missing):
# the two classification columns, then the three infra columns that let the
# in-memory index be rebuilt from the sheet after a redeploy.
CLASSIFICATION_COLUMNS = ["Product", "Type", "Waiting On"]
INFRA_COLUMNS = ["Slack Channel", "Slack TS", "Bot Refs"]
MANAGED_COLUMNS = CLASSIFICATION_COLUMNS + INFRA_COLUMNS

# Status lifecycle (the sheet's Dashboard "Active" formula must count the open
# ones below). The forwarding workflow moves a question through these states,
# mostly driven by admin emoji reactions. A new question starts as "Sent".
STATUSES = ["Sent", "Acknowledged", "Forwarded", "In Progress", "Solved", "Answered", "Closed"]
STATUS_SENT = "Sent"
STATUS_IN_PROGRESS = "In Progress"
STATUS_CLOSED = "Closed"
# Open = still needs attention, so it shows in reports and the sheet Active count.
# Solved / Answered / Closed are resolved. The legacy "Open" / "Escalated" values
# on historical rows are kept open so those rows keep showing after the switch.
OPEN_STATUSES = {"Sent", "Acknowledged", "Forwarded", "In Progress", "Open", "Escalated"}
# Legacy constants kept so older references and historical rows still resolve.
STATUS_OPEN = "Open"
STATUS_ESCALATED = "Escalated"

CHECK_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
DISCARD_REACTIONS = {"x", "negative_squared_cross_mark", "heavy_multiplication_x", "no_entry"}

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
