#!/usr/bin/env python3
"""
Sui Dev Leads Ops Bot.

Turns a Slack channel + a Google Sheet ("Open Questions" tab) into a low-effort,
always-organized escalation tracker. Runs as a long-lived process over Slack
Socket Mode (no public URL required).

Features
  1. Auto-log new escalations. A cheap local pre-filter drops chatter; substantive
     top-level messages go to Claude Haiku, which decides if it's a NEW developer
     escalation and extracts structured fields. If yes, a row is appended
     (Status="Escalated") and the bot posts an in-thread note with a deep link to
     the row. React :x: to discard a false positive.
  2. Auto-update on resolution (human-in-the-loop). A thread reply on a tracked
     message, or a :white_check_mark: on it, triggers Claude to judge resolution.
     If resolved, Status -> "Needs review" (never auto-closed) and the bot asks the
     owner to react :white_check_mark: to confirm. A confirming reaction -> "Closed".
  3. Status commands. /status, /open, /aging (and "@bot status|open|aging").

State of truth is the Sheet itself: three bot-owned columns (Slack Channel, Slack
TS, Bot Refs) let the in-memory index be rebuilt on every boot, so redeploys on
ephemeral hosts don't lose tracking. Every Claude decision is logged to stdout and
a local JSONL file for audit.

See README.md for setup. Run: `python bot.py` (or `python bot.py --check` to
validate configuration and exit).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration (all via env / mounted secrets)
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
# One channel id, or a comma-separated list.
SLACK_CHANNEL_IDS = [c.strip() for c in os.environ.get("SLACK_CHANNEL_ID", "").split(",") if c.strip()]

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_TAB = os.environ.get("SHEET_TAB", "Open Questions")
# If set, open the worksheet by numeric gid (from the sheet URL) instead of by
# name — robust when the tab isn't literally named "Open Questions".
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

# Optional identity override for the bot's own messages. Requires the
# chat:write.customize scope. Useful when a renamed app still shows a stale name
# in a channel — this forces every post to use the given name/icon regardless of
# cached app identity.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
BOT_ICON_EMOJI = os.environ.get("BOT_ICON_EMOJI", "")

# Column vocabularies (Claude is constrained to these exact strings).
PRIORITIES = ["High", "Medium", "Low"]
ESCALATION_TARGETS = ["SolEng", "DevRel", "DevX", "None"]

# Sheet schema. The 14 human columns come first (existing sheet), then 3 the bot
# owns. Columns are matched by NAME from the header row, so exact order/position
# in the sheet can vary; missing bot columns are appended automatically on boot.
HUMAN_COLUMNS = [
    "ID", "Date Asked", "Window", "Platform", "Channel", "Question Summary",
    "Link", "Raised By", "Owner", "Priority", "Status", "Escalated To",
    "Date Resolved", "Notes",
]
BOT_COLUMNS = ["Slack Channel", "Slack TS", "Bot Refs"]
ALL_COLUMNS = HUMAN_COLUMNS + BOT_COLUMNS

# Matches the sheet's own vocabulary + its Dashboard "Active" formula.
OPEN_STATUSES = {"Open", "In Progress", "Escalated"}
STATUS_OPEN = "Open"
STATUS_ESCALATED = "Escalated"
STATUS_CLOSED = "Closed"

# On a new escalation the bot fills Window with this (blank = leave for a human).
WINDOW_DEFAULT = os.environ.get("WINDOW_DEFAULT", "")
# Owner on a freshly auto-logged row (the Read Me convention until someone picks it up).
OWNER_DEFAULT = os.environ.get("OWNER_DEFAULT", "Unassigned")

CHECK_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
DISCARD_REACTIONS = {"x", "negative_squared_cross_mark", "heavy_multiplication_x", "no_entry"}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ)
    except Exception:
        return ZoneInfo("UTC")


def today_str() -> str:
    return datetime.now(_tz()).date().isoformat()


# ---------------------------------------------------------------------------
# Audit log — every Claude decision, to stdout + local JSONL.
# ---------------------------------------------------------------------------
_audit_lock = threading.Lock()


def audit(kind: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    line = json.dumps(rec, ensure_ascii=False)
    with _audit_lock:
        print("AUDIT " + line, flush=True)
        try:
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # never let audit failure crash a handler
            print(f"WARN could not write audit file: {exc}", flush=True)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Google Sheet store — source of truth, with an in-memory ts index.
# ---------------------------------------------------------------------------
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class Row:
    """A tracked sheet row plus the Slack references the bot needs."""

    def __init__(self, row_number: int, values: dict):
        self.row_number = row_number
        self.values = values
        refs = {}
        raw = values.get("Bot Refs", "")
        if raw:
            try:
                refs = json.loads(raw)
            except Exception:
                refs = {}
        self.refs = refs

    @property
    def status(self) -> str:
        return self.values.get("Status", "")

    @property
    def original_ts(self) -> str:
        return self.values.get("Slack TS", "")

    @property
    def anchor_ts(self) -> str:
        return self.refs.get("anchor_ts", "")

    @property
    def confirm_ts(self) -> str:
        return self.refs.get("confirm_ts", "")

    @property
    def owner_uid(self) -> str:
        return self.refs.get("owner_uid", "")

    @property
    def slack_channel(self) -> str:
        return self.values.get("Slack Channel", "")


class SheetStore:
    def __init__(self, sheet_id: str, tab: str, creds_file: str):
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(sheet_id)
        if SHEET_GID:
            self.ws = self.sh.get_worksheet_by_id(int(SHEET_GID))
        else:
            self.ws = self.sh.worksheet(tab)
        self.gid = self.ws.id
        self.sheet_id = sheet_id
        self.lock = threading.RLock()
        self.header: list[str] = []
        self.col_index: dict[str, int] = {}   # column name -> 0-based index
        self.rows: dict[int, Row] = {}          # row_number -> Row
        self.ts_index: dict[str, int] = {}      # any slack ts -> row_number
        self._ensure_header()
        self.reload()

    # -- header / schema -----------------------------------------------------
    def _ensure_header(self) -> None:
        """Locate the real header row (this sheet has intro rows above it, so it's
        not row 1) and add only the 3 bot-owned columns if missing — never the
        human ones, which already exist."""
        with self.lock:
            values = self.ws.get_all_values()
            self.header_row = 1
            for i, raw in enumerate(values, start=1):
                if "ID" in raw and "Date Asked" in raw:
                    self.header_row = i
                    break
            header = values[self.header_row - 1] if values else []
            missing = [c for c in BOT_COLUMNS if c not in header]
            if missing:
                start = len(header)
                rng = (f"{gspread.utils.rowcol_to_a1(self.header_row, start + 1)}:"
                       f"{gspread.utils.rowcol_to_a1(self.header_row, start + len(missing))}")
                self.ws.update(range_name=rng, values=[missing])
                header = header + missing
                log(f"added bot columns to header row {self.header_row}: {missing}")
            self.header = header
            self.col_index = {name: i for i, name in enumerate(header) if name}
            # This sheet calls the notes column "Notes / Handoff".
            self.notes_col = next(
                (h for h in header if h.strip().lower().startswith("notes")), "Notes")
            self.data_start = self.header_row + 1

    # -- read ----------------------------------------------------------------
    def reload(self) -> None:
        """Re-read the sheet and rebuild the ts index (called on boot and after
        every mutation; escalation volume is low so this stays well under quota).
        Includes ALL data rows — human-entered and bot-entered — so status
        reports reflect the whole board."""
        with self.lock:
            values = self.ws.get_all_values()
            self.rows.clear()
            self.ts_index.clear()
            for i, raw in enumerate(values, start=1):
                if i < self.data_start:
                    continue
                rowvals = {name: (raw[idx] if idx < len(raw) else "")
                           for name, idx in self.col_index.items()}
                if not rowvals.get("ID") and not rowvals.get("Slack TS"):
                    continue
                row = Row(i, rowvals)
                self.rows[i] = row
                for ts in (row.original_ts, row.anchor_ts, row.confirm_ts):
                    if ts:
                        self.ts_index[ts] = i

    def find_by_ts(self, ts: str) -> Row | None:
        with self.lock:
            rn = self.ts_index.get(ts)
            return self.rows.get(rn) if rn else None

    def ts_tracked(self, ts: str) -> bool:
        with self.lock:
            return ts in self.ts_index

    def _next_row(self) -> int:
        return (max(self.rows) if self.rows else self.header_row) + 1

    # -- write ---------------------------------------------------------------
    def append(self, fields: dict) -> Row:
        """Write a new row at an explicitly computed position (append_row's
        table-detection is unreliable here because of the intro rows). ID is left
        to the sheet's own formula, replicated for this row so it stays automatic."""
        with self.lock:
            r = self._next_row()
            width = len(self.header)
            rowvals = ["" for _ in range(width)]
            for name, val in fields.items():
                if name in self.col_index:
                    rowvals[self.col_index[name]] = val
            if "ID" in self.col_index:
                rowvals[self.col_index["ID"]] = (
                    f'=IF(COUNTA($B{r}:$L{r})=0;"";MAX($A${self.header_row}:A{r - 1})+1)')
            rng = (f"{gspread.utils.rowcol_to_a1(r, 1)}:"
                   f"{gspread.utils.rowcol_to_a1(r, width)}")
            # USER_ENTERED so dates become real dates and the ID formula evaluates…
            self.ws.update(range_name=rng, values=[rowvals],
                           value_input_option="USER_ENTERED")
            # …then force Slack TS to text (RAW) so Sheets doesn't parse the numeric-
            # looking ts as a float and round it, which would break ts→row matching.
            ts = fields.get("Slack TS", "")
            if ts and "Slack TS" in self.col_index:
                cell = gspread.utils.rowcol_to_a1(r, self.col_index["Slack TS"] + 1)
                self.ws.update(range_name=cell, values=[[ts]], value_input_option="RAW")
            self.reload()
            return self.find_by_ts(ts)

    def delete_row(self, row_number: int) -> None:
        with self.lock:
            self.ws.delete_rows(row_number)
            self.reload()

    def set(self, row_number: int, updates: dict) -> None:
        with self.lock:
            cells = []
            for name, val in updates.items():
                if name not in self.col_index:
                    continue
                a1 = gspread.utils.rowcol_to_a1(row_number, self.col_index[name] + 1)
                cells.append({"range": a1, "values": [[val]]})
            if cells:
                self.ws.batch_update(cells, value_input_option="USER_ENTERED")
            self.reload()

    def set_refs(self, row_number: int, **kv) -> None:
        with self.lock:
            row = self.rows.get(row_number)
            refs = dict(row.refs) if row else {}
            refs.update({k: v for k, v in kv.items() if v is not None})
            self.set(row_number, {"Bot Refs": json.dumps(refs)})

    def row_link(self, row_number: int) -> str:
        return (f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"
                f"/edit#gid={self.gid}&range=A{row_number}")

    def open_rows(self) -> list[Row]:
        with self.lock:
            return [r for r in self.rows.values() if r.status in OPEN_STATUSES]


# ---------------------------------------------------------------------------
# Claude — classification + resolution, via forced tool use (validated JSON).
# ---------------------------------------------------------------------------
from anthropic import Anthropic

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

CLASSIFY_TOOL = {
    "name": "log_escalation",
    "description": "Record the classification of a Slack message as a developer escalation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_escalation": {
                "type": "boolean",
                "description": "True only if this is a NEW developer question/issue being escalated for help (not chatter, an answer, an ack, or an already-in-progress thread).",
            },
            "question_summary": {"type": "string", "description": "One-line summary of the developer question/issue."},
            "platform": {"type": "string", "description": "The SOURCE MEDIUM the question came in on: one of Telegram, Discord, GitHub, Sui Forum, X, Slack, Email, Other. Empty if unclear."},
            "source_channel": {"type": "string", "description": "The specific channel/venue name if identifiable (e.g. 'TG - Overflow DeepBook', 'GitHub Issues (repo #123)', 'Sui Developer Forum'). Empty if unknown."},
            "link": {"type": "string", "description": "Any URL in the message, else empty."},
            "raised_by": {"type": "string", "description": "Who originally raised it if named in the message, else empty."},
            "priority": {"type": "string", "enum": PRIORITIES},
            "escalation_target": {"type": "string", "enum": ESCALATION_TARGETS},
        },
        "required": ["is_escalation", "question_summary", "platform", "source_channel",
                     "link", "raised_by", "priority", "escalation_target"],
    },
}

RESOLUTION_TOOL = {
    "name": "resolution_verdict",
    "description": "Judge whether the tracked question is now resolved based on the thread.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolved": {"type": "boolean", "description": "True only if the thread contains an actual answer/fix that resolves the original question."},
            "resolution_summary": {"type": "string", "description": "One-line summary of the resolution, empty if not resolved."},
            "answered_by": {"type": "string", "description": "Who provided the resolving answer, empty if not resolved."},
        },
        "required": ["resolved", "resolution_summary", "answered_by"],
    },
}


def _tool_call(tool: dict, system: str, user: str) -> dict:
    resp = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            usage = getattr(resp, "usage", None)
            return {
                "input": block.input,
                "tokens_in": getattr(usage, "input_tokens", None),
                "tokens_out": getattr(usage, "output_tokens", None),
            }
    raise RuntimeError("model did not return a tool call")


def classify_message(text: str) -> dict:
    system = (
        "You triage a Slack channel used by Sui developer-relations leads to escalate "
        "developer questions that stay open across on-call shifts. Decide if a message is a "
        "NEW developer-question escalation that should be tracked. Chatter, acknowledgements, "
        "answers to existing threads, status updates, and social messages are NOT escalations. "
        "'platform' is the source medium (Telegram/Discord/GitHub/Sui Forum/X/Slack/Email/Other); "
        "'source_channel' is the specific venue name. Constrain priority to "
        f"{PRIORITIES} and escalation_target to {ESCALATION_TARGETS}."
    )
    return _tool_call(CLASSIFY_TOOL, system, f"Slack message:\n\n{text}")


def judge_resolution(question: str, thread_text: str) -> dict:
    system = (
        "You decide whether a tracked developer question has been resolved by a Slack "
        "thread. Only say resolved=true if the thread contains a concrete answer or fix "
        "for the ORIGINAL question. A follow-up question, a 'looking into it', or an "
        "unrelated reply is NOT a resolution."
    )
    user = f"Original question:\n{question}\n\nThread so far:\n{thread_text}"
    return _tool_call(RESOLUTION_TOOL, system, user)


# ---------------------------------------------------------------------------
# Slack app
# ---------------------------------------------------------------------------
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# token_verification_enabled=False: don't hit auth.test at import time, so
# `--check` can report a clean, aggregated config error instead of crashing here.
app = App(token=SLACK_BOT_TOKEN, token_verification_enabled=False)
store: SheetStore | None = None
_bot_user_id: str | None = None
_user_name_cache: dict[str, str] = {}


def post(**kwargs):
    """chat.postMessage with an optional identity override (BOT_USERNAME/icon),
    so a renamed app always posts under the intended name."""
    if BOT_USERNAME:
        kwargs.setdefault("username", BOT_USERNAME)
    if BOT_ICON_EMOJI:
        kwargs.setdefault("icon_emoji", BOT_ICON_EMOJI)
    return app.client.chat_postMessage(**kwargs)


def user_display_name(uid: str) -> str:
    if not uid:
        return ""
    if uid in _user_name_cache:
        return _user_name_cache[uid]
    try:
        info = app.client.users_info(user=uid)["user"]
        name = info.get("real_name") or info.get("profile", {}).get("display_name") or uid
    except Exception:
        name = uid
    _user_name_cache[uid] = name
    return name


def is_substantive(text: str) -> bool:
    """Cheap local pre-filter: keep only messages worth a Claude call."""
    if not text:
        return False
    stripped = re.sub(r"<[^>]+>", "", text).strip()          # drop mentions/links markup
    stripped = re.sub(r":[a-z0-9_+\-]+:", "", stripped).strip()  # drop emoji shortcodes
    if len(stripped) < MIN_MESSAGE_CHARS:
        return False
    if stripped.lower() in {"thanks", "thank you", "ty", "done", "ok", "okay", "+1", "lgtm"}:
        return False
    return True


def _thread_text(channel: str, thread_ts: str, limit: int = 50) -> str:
    try:
        resp = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        parts = []
        for m in resp.get("messages", []):
            if m.get("bot_id"):
                continue
            who = user_display_name(m.get("user", ""))
            parts.append(f"{who}: {m.get('text','')}")
        return "\n".join(parts)
    except Exception as exc:
        log(f"WARN could not fetch thread {thread_ts}: {exc}")
        return ""


# -- Feature 1: auto-log new escalations ------------------------------------
def classify_and_log(channel: str, ts: str, user: str, text: str) -> None:
    try:
        result = classify_message(text)
    except Exception as exc:
        log(f"ERROR classify failed: {exc}")
        return
    data = result["input"]
    audit("classify", channel=channel, slack_ts=ts, user=user, text=text[:500],
          verdict=data, tokens_in=result["tokens_in"], tokens_out=result["tokens_out"])
    if not data.get("is_escalation"):
        return

    poster_name = user_display_name(user)
    permalink = ""
    try:
        permalink = app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        pass
    # Their convention: Status="Escalated" (+ Escalated To) when a target is
    # suggested, otherwise a plain "Open" item nobody has picked up yet.
    target = data.get("escalation_target", "None")
    escalated = target in ("SolEng", "DevRel", "DevX")
    fields = {
        # ID is left to the sheet's own formula (set inside store.append).
        "Date Asked": today_str(),
        "Window": WINDOW_DEFAULT,
        "Platform": data.get("platform", ""),
        "Channel": data.get("source_channel", ""),
        "Question Summary": data.get("question_summary", ""),
        "Link": data.get("link", "") or permalink,
        "Raised By": data.get("raised_by", "") or poster_name,
        "Owner": OWNER_DEFAULT,
        "Priority": data.get("priority", ""),
        "Status": STATUS_ESCALATED if escalated else STATUS_OPEN,
        "Escalated To": target if escalated else "",
        "Date Resolved": "",
        store.notes_col: "",
        "Slack Channel": channel,
        "Slack TS": ts,
        "Bot Refs": json.dumps({"owner_uid": user}),
    }
    row = store.append(fields)
    if not row:
        log("ERROR appended row not found after reload")
        return

    rid = row.values.get("ID", "?")
    link = store.row_link(row.row_number)
    try:
        posted = post(
            channel=channel, thread_ts=ts,
            text=(f":pushpin: Logged as *#{rid}* — {fields['Status']} "
                  f"(<{link}|open row>). Not an escalation? React :x: to discard."),
        )
        store.set_refs(row.row_number, anchor_ts=posted["ts"])
    except Exception as exc:
        log(f"WARN could not post log note: {exc}")
    log(f"logged escalation #{rid} row {row.row_number} status={fields['Status']}")


# -- Feature 2: resolution (human-in-the-loop) ------------------------------
def run_resolution_check(row: Row, channel: str, thread_ts: str) -> None:
    # Only for still-active items, and only once (skip if we've already proposed a
    # resolution and are awaiting the ✅ confirmation).
    if row.status not in OPEN_STATUSES or row.confirm_ts:
        return
    question = row.values.get("Question Summary", "")
    thread = _thread_text(channel, thread_ts)
    if not thread:
        return
    try:
        result = judge_resolution(question, thread)
    except Exception as exc:
        log(f"ERROR resolution judge failed: {exc}")
        return
    data = result["input"]
    audit("resolution", row=row.row_number, id=row.values.get("ID"), slack_ts=thread_ts,
          verdict=data, tokens_in=result["tokens_in"], tokens_out=result["tokens_out"])
    if not data.get("resolved"):
        return

    summary = data.get("resolution_summary", "")
    answered_by = data.get("answered_by", "")
    # Keep the sheet's Status UNCHANGED (item stays active on the Dashboard until a
    # human confirms). Record the proposal in Notes / Handoff without clobbering any
    # existing handoff text.
    existing = row.values.get(store.notes_col, "")
    proposed = f"⏳ Proposed (react ✅ to close): {summary}"
    if answered_by:
        proposed += f" — answered by {answered_by}"
    new_note = proposed + (f"\n{existing}" if existing else "")
    store.set(row.row_number, {store.notes_col: new_note})
    link = store.row_link(row.row_number)
    owner_tag = f"<@{row.owner_uid}>" if row.owner_uid else row.values.get("Owner", "there")
    try:
        posted = post(
            channel=channel, thread_ts=thread_ts,
            text=(f"{owner_tag} Looks resolved — {summary or 'see thread'}. "
                  f"React :white_check_mark: to confirm and I'll close it "
                  f"(<{link}|row>)."),
        )
        store.set_refs(row.row_number, confirm_ts=posted["ts"])
    except Exception as exc:
        log(f"WARN could not post confirm note: {exc}")
    log(f"row {row.row_number} proposed resolution (awaiting ✅)")


def close_row(row: Row, channel: str) -> None:
    store.set(row.row_number, {"Status": STATUS_CLOSED, "Date Resolved": today_str()})
    audit("close", row=row.row_number, id=row.values.get("ID"))
    try:
        post(
            channel=channel, thread_ts=row.original_ts,
            text=f":lock: Confirmed — *#{row.values.get('ID')}* closed.",
        )
    except Exception:
        pass
    log(f"row {row.row_number} -> Closed")


def discard_row(row: Row, channel: str) -> None:
    rid = row.values.get("ID")
    audit("discard", row=row.row_number, id=rid)
    orig = row.original_ts
    store.delete_row(row.row_number)  # false positive → remove the row entirely
    try:
        post(
            channel=channel, thread_ts=orig,
            text=f":wastebasket: Discarded — removed from the tracker.",
        )
    except Exception:
        pass
    log(f"row {row.row_number} -> Discarded")


# -- Slack event handlers ----------------------------------------------------
def handle_text_command(channel: str, text: str, ts: str, thread_ts: str | None) -> bool:
    """Text-based command fallback. Works with only channels:history + chat:write
    (no `commands` / `app_mentions:read` scope needed), so status commands work even
    before the app is reinstalled with the native slash-command scopes.

    Triggers on a leading `!` or `/`, or on an @mention of the bot, followed by
    `status` | `open` | `aging`. Returns True if it handled the message."""
    if not text:
        return False
    mentioned = bool(_bot_user_id) and (f"<@{_bot_user_id}>" in text)
    body = re.sub(r"<@[^>]+>", "", text).strip()
    prefixed = body.startswith("!") or body.startswith("/")
    if not (mentioned or prefixed):
        return False
    tokens = body.lstrip("!/").strip().lower().split()
    key = tokens[0] if tokens else ""
    reply_thread = thread_ts if (thread_ts and thread_ts != ts) else None
    if key == "status":
        post(channel=channel, thread_ts=reply_thread, text=status_report())
    elif key == "open":
        post(channel=channel, thread_ts=reply_thread, text=open_report())
    elif key in ("aging", "aged", "old"):
        post(channel=channel, thread_ts=reply_thread, text=aging_report())
    elif mentioned:
        post(channel=channel, thread_ts=reply_thread,
             text="Commands: `!status` · `!open` · `!aging` (or `@me status`). "
                  "Native `/status /open /aging` work after the app is reinstalled with the "
                  "`commands` scope.")
    else:
        return False
    log(f"handled text command '{key or 'help'}' in {channel}")
    return True


@app.event("message")
def on_message(event, logger):
    try:
        if event.get("subtype") or event.get("bot_id"):
            return
        channel = event.get("channel", "")
        if SLACK_CHANNEL_IDS and channel not in SLACK_CHANNEL_IDS:
            return
        ts = event.get("ts", "")
        user = event.get("user", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts")

        if handle_text_command(channel, text, ts, thread_ts):
            return

        if thread_ts and thread_ts != ts:
            row = store.find_by_ts(thread_ts)
            if row:
                run_resolution_check(row, channel, thread_ts)
            return

        if not is_substantive(text):
            return
        if store.ts_tracked(ts):
            return
        classify_and_log(channel, ts, user, text)
    except Exception:
        log("ERROR in on_message:\n" + traceback.format_exc())


@app.event("reaction_added")
def on_reaction(event, logger):
    try:
        item = event.get("item", {})
        ts = item.get("ts", "")
        channel = item.get("channel", "")
        reaction = event.get("reaction", "")
        row = store.find_by_ts(ts)
        if not row:
            return
        # Meaning is derived from WHICH message was reacted on:
        #   ❌ on the bot's log note  -> discard (only while still open)
        #   ✅ on the bot's confirm note -> close
        #   ✅ on the original question -> run the resolution check
        if reaction in DISCARD_REACTIONS and ts == row.anchor_ts and row.status in OPEN_STATUSES:
            discard_row(row, channel)
        elif reaction in CHECK_REACTIONS:
            if ts == row.confirm_ts and row.status in OPEN_STATUSES:
                close_row(row, channel)
            elif ts == row.original_ts:
                run_resolution_check(row, channel, row.original_ts)
    except Exception:
        log("ERROR in on_reaction:\n" + traceback.format_exc())


# -- Feature 3: status commands ---------------------------------------------
def _age_days(date_asked: str) -> int | None:
    try:
        d = datetime.fromisoformat(date_asked).date()
        return (datetime.now(_tz()).date() - d).days
    except Exception:
        return None


def status_report() -> str:
    store.reload()
    rows = store.open_rows()
    if not rows:
        return ":white_check_mark: No open items."
    by_target: dict[str, int] = {}
    aging = 0
    oldest = None
    oldest_age = -1
    for r in rows:
        tgt = r.values.get("Escalated To", "") or "None"
        by_target[tgt] = by_target.get(tgt, 0) + 1
        age = _age_days(r.values.get("Date Asked", ""))
        if age is not None:
            if age > AGING_DAYS:
                aging += 1
            if age > oldest_age:
                oldest_age, oldest = age, r
    lines = [f"*Open items:* {len(rows)}"]
    lines.append("*By Escalated To:* " + ", ".join(f"{k}: {v}" for k, v in sorted(by_target.items())))
    lines.append(f"*Aging (>{AGING_DAYS}d, still open):* {aging}")
    if oldest is not None:
        link = oldest.values.get("Link", "") or store.row_link(oldest.row_number)
        lines.append(f"*Oldest:* {oldest.values.get('ID')} ({oldest_age}d) — "
                     f"{oldest.values.get('Question Summary','')} <{link}|↗>")
    return "\n".join(lines)


def open_report() -> str:
    store.reload()
    rows = sorted(store.open_rows(), key=lambda r: r.values.get("Date Asked", ""))
    if not rows:
        return ":white_check_mark: No open items."
    lines = [f"*{len(rows)} open items:*"]
    for r in rows[:30]:
        link = r.values.get("Link", "") or store.row_link(r.row_number)
        age = _age_days(r.values.get("Date Asked", ""))
        agestr = f"{age}d" if age is not None else "?"
        lines.append(f"• *{r.values.get('ID')}* [{r.status}, {agestr}] "
                     f"{r.values.get('Question Summary','')} → {r.values.get('Escalated To','')} <{link}|↗>")
    if len(rows) > 30:
        lines.append(f"…and {len(rows) - 30} more.")
    return "\n".join(lines)


def aging_report() -> str:
    store.reload()
    rows = []
    for r in store.open_rows():
        age = _age_days(r.values.get("Date Asked", ""))
        if age is not None and age > AGING_DAYS:
            rows.append((age, r))
    rows.sort(key=lambda t: -t[0])
    if not rows:
        return f":white_check_mark: Nothing aging over {AGING_DAYS} days."
    lines = [f"*{len(rows)} items aging over {AGING_DAYS} days:*"]
    for age, r in rows[:30]:
        link = r.values.get("Link", "") or store.row_link(r.row_number)
        lines.append(f"• *{r.values.get('ID')}* ({age}d) {r.values.get('Question Summary','')} "
                     f"→ {r.values.get('Escalated To','')} <{link}|↗>")
    return "\n".join(lines)


@app.command("/status")
def cmd_status(ack, respond):
    ack()
    respond(status_report())


@app.command("/open")
def cmd_open(ack, respond):
    ack()
    respond(open_report())


@app.command("/aging")
def cmd_aging(ack, respond):
    ack()
    respond(aging_report())


@app.event("app_mention")
def on_mention(event, say):
    text = (event.get("text", "") or "").lower()
    if "aging" in text:
        say(aging_report())
    elif "open" in text:
        say(open_report())
    else:
        say(status_report())


# ---------------------------------------------------------------------------
# Startup backfill — Socket Mode does not replay missed events.
# ---------------------------------------------------------------------------
def backfill() -> None:
    if BACKFILL_HOURS <= 0 or not SLACK_CHANNEL_IDS:
        return
    oldest = time.time() - BACKFILL_HOURS * 3600
    for channel in SLACK_CHANNEL_IDS:
        try:
            cursor = None
            scanned = 0
            while True:
                resp = app.client.conversations_history(
                    channel=channel, oldest=str(oldest), limit=200,
                    cursor=cursor,
                )
                for m in resp.get("messages", []):
                    if m.get("subtype") or m.get("bot_id"):
                        continue
                    ts = m.get("ts", "")
                    if store.ts_tracked(ts):
                        continue
                    if not is_substantive(m.get("text", "")):
                        continue
                    scanned += 1
                    classify_and_log(channel, ts, m.get("user", ""), m.get("text", ""))
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            log(f"backfill channel {channel}: classified {scanned} untracked messages")
        except Exception as exc:
            log(f"WARN backfill failed for {channel}: {exc}")


# ---------------------------------------------------------------------------
# Preflight + main
# ---------------------------------------------------------------------------
def preflight() -> list[str]:
    problems = []
    if not SLACK_BOT_TOKEN.startswith("xoxb-"):
        problems.append("SLACK_BOT_TOKEN missing or not an xoxb- token")
    if not SLACK_APP_TOKEN.startswith("xapp-"):
        problems.append("SLACK_APP_TOKEN missing or not an xapp- token")
    if not SLACK_CHANNEL_IDS:
        problems.append("SLACK_CHANNEL_ID not set")
    if not ANTHROPIC_API_KEY:
        problems.append("ANTHROPIC_API_KEY not set")
    if not SHEET_ID:
        problems.append("SHEET_ID not set")
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        problems.append(f"service account file not found: {GOOGLE_CREDENTIALS_FILE}")
    return problems


def run_diag() -> None:
    """Live end-to-end diagnostics: Slack identity, Anthropic, Sheet, channel
    membership. Each check is independent and reports its own pass/fail so it's
    useful even when some config is still missing. Read-only except it makes one
    tiny Anthropic call. Does NOT connect Socket Mode."""
    log("=== DIAG START ===")

    # --- Slack identity (also diagnoses a stale bot name) ---
    try:
        who = app.client.auth_test()
        log(f"[slack] auth OK — team={who.get('team')} team_id={who.get('team_id')}")
        log(f"[slack] bot user_id={who.get('user_id')} auth_test name='{who.get('user')}'")
        try:
            prof = app.client.users_info(user=who["user_id"])["user"]
            log(f"[slack] display name shown in Slack: real_name='{prof.get('real_name')}' "
                f"display_name='{prof.get('profile',{}).get('display_name')}'")
            bot_id = prof.get("profile", {}).get("bot_id")
            if bot_id:
                binfo = app.client.bots_info(bot=bot_id).get("bot", {})
                log(f"[slack] bots_info name='{binfo.get('name')}' "
                    f"(this is what channels render; if stale, reinstall the app)")
        except Exception as exc:
            log(f"[slack] WARN could not read bot profile: {exc}")
    except Exception as exc:
        log(f"[slack] FAIL auth_test: {exc}")

    # --- Channel membership + read access ---
    for ch in SLACK_CHANNEL_IDS:
        try:
            info = app.client.conversations_info(channel=ch)["channel"]
            member = info.get("is_member")
            log(f"[slack] channel {ch} name='{info.get('name')}' is_member={member} "
                f"is_private={info.get('is_private')} shared={info.get('is_shared') or info.get('is_ext_shared')}")
            if not member:
                log(f"[slack] WARN not a member of {ch} — run /invite in that channel")
            app.client.conversations_history(channel=ch, limit=1)
            log(f"[slack] channel {ch} history read OK")
        except Exception as exc:
            log(f"[slack] FAIL channel {ch}: {exc}")

    # --- Anthropic (one tiny classify each way) ---
    try:
        pos = classify_message("Dev on Discord is getting a 500 from the mainnet RPC endpoint when calling sui_getObject, blocking their launch.")
        log(f"[anthropic] classify(sample escalation).is_escalation="
            f"{pos['input'].get('is_escalation')} priority={pos['input'].get('priority')} "
            f"target={pos['input'].get('escalation_target')} tokens={pos['tokens_in']}/{pos['tokens_out']}")
        neg = classify_message("gm everyone, great work on the demo yesterday 🎉")
        log(f"[anthropic] classify(sample chatter).is_escalation={neg['input'].get('is_escalation')}")
    except Exception as exc:
        log(f"[anthropic] FAIL: {exc}")

    # --- Google Sheet ---
    try:
        s = SheetStore(SHEET_ID, SHEET_TAB, GOOGLE_CREDENTIALS_FILE)
        log(f"[sheet] OK — header row {s.header_row}, {len(s.rows)} data rows, "
            f"notes col='{s.notes_col}'")
        log(f"[sheet] columns: {[h for h in s.header if h]}")
        missing = [c for c in BOT_COLUMNS if c not in s.header]
        log(f"[sheet] bot columns present: {'yes' if not missing else 'MISSING ' + str(missing)}")
        opens = s.open_rows()
        log(f"[sheet] open/active rows now: {len(opens)}")
    except FileNotFoundError as exc:
        log(f"[sheet] FAIL service-account file: {exc}")
    except Exception as exc:
        log(f"[sheet] FAIL — often means the sheet isn't shared with the service "
            f"account email, or wrong SHEET_ID/gid: {exc}")

    log("=== DIAG END ===")


def main() -> None:
    global store, _bot_user_id
    if "--diag" in sys.argv:
        run_diag()
        return
    problems = preflight()
    check_only = "--check" in sys.argv
    if problems:
        for p in problems:
            log(f"CONFIG ERROR: {p}")
        if check_only or True:
            log("preflight failed — fix the above and restart.")
            sys.exit(1)
    if check_only:
        log("preflight: config looks OK (Slack/Anthropic/Sheets not yet contacted).")

    log("connecting to Google Sheet…")
    store = SheetStore(SHEET_ID, SHEET_TAB, GOOGLE_CREDENTIALS_FILE)
    log(f"sheet ready: {len(store.rows)} rows, gid={store.gid}")

    try:
        _bot_user_id = app.client.auth_test()["user_id"]
        log(f"slack auth ok as {_bot_user_id}")
    except Exception as exc:
        log(f"CONFIG ERROR: slack auth_test failed: {exc}")
        sys.exit(1)

    if check_only:
        log("preflight --check complete; exiting before Socket Mode connect.")
        return

    backfill()
    log("starting Socket Mode…")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
