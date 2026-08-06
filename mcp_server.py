#!/usr/bin/env python3
"""
Sui Dev Leads Ops — MCP server.

A Model Context Protocol server that gives every dev lead manual, natural-language
control over the same escalation tracker the auto-bot (`bot.py`) maintains. Devleads
add it to Claude (Desktop / Code) and just say "post this", "ping on 12,13",
"what's the status of 7", "give me the weekly report".

It exposes five tools that map 1:1 to the workflow Domenico specified:

  • post_message   — post a new question to the dev-leads Slack channel + log a
                     tracker row. Shows source, an ID, and the department/category.
  • check_status   — for one or more IDs, read the thread and decide Solved /
                     Forwarded / Open (Claude + a ✅-reaction / "forwarded" check),
                     and return the last reply.
  • weekly_report  — the open (unanswered) backlog with IDs, ages, owners and
                     links; optionally posts it to the channel.
  • mark_solved    — add ✅ to the message(s) and close the tracker row(s).
  • ping           — reply in the thread(s) tagging the owner for a follow-up.

Design: the Google Sheet is the shared source of truth (same sheet as bot.py), so
IDs are consistent across every dev lead running this MCP. All heavy lifting —
the sheet schema, the Slack Web client, and the Claude resolution judge — is reused
from `bot.py` unchanged; this file only adds the manual-control tool surface.

IMPORTANT (MCP stdio): stdout is the JSON-RPC channel, so this module reroutes
`bot.py`'s stdout diagnostics to stderr on import — nothing but protocol frames may
touch stdout.

Run standalone check:  python mcp_server.py --check
Run as MCP server:     python mcp_server.py     (stdio; launched by the MCP client)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv() -> None:
    """Load `<script dir>/.env` into os.environ (without overriding anything already
    set) so `python mcp_server.py` works the same whether launched by an MCP client
    with an env block or from a shell. Must run BEFORE importing bot (bot reads its
    config at import time). Minimal parser — no python-dotenv dependency."""
    path = os.path.join(_HERE, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.split(" #", 1)[0].strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as exc:
        print(f"WARN could not read .env: {exc}", file=sys.stderr, flush=True)


_load_dotenv()

# Make this directory importable regardless of the client's CWD, then import the
# existing bot module and reuse its store / Slack client / Claude helpers.
sys.path.insert(0, _HERE)
import bot  # noqa: E402


# ---------------------------------------------------------------------------
# stdout hygiene — MCP speaks JSON-RPC over stdout. bot.py prints diagnostics to
# stdout via log()/audit(); reroute both to stderr so they never corrupt frames.
# (Functions inside bot.py resolve `log`/`audit` from bot's module globals at call
# time, so reassigning the attributes here redirects those calls too.)
# ---------------------------------------------------------------------------
def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _log(msg: str) -> None:
    _err(f"{datetime.now().isoformat()} {msg}")


def _audit(kind: str, **fields) -> None:
    try:
        _err("AUDIT " + json.dumps({"kind": kind, **fields}, ensure_ascii=False, default=str))
    except Exception:
        pass


bot.log = _log
bot.audit = _audit


# ---------------------------------------------------------------------------
# MCP-specific configuration (everything else comes from bot.py's env).
# ---------------------------------------------------------------------------
# Slack member id to @-tag on /ping and in the weekly report (e.g. Domenico's id,
# "U0123ABCD"). If empty, PING_USER_NAME is used as plain text.
PING_USER_ID = os.environ.get("PING_USER_ID", "").strip()
PING_USER_NAME = os.environ.get("PING_USER_NAME", "the owner").strip()
# Channel new questions/reports are posted to. Defaults to the first configured
# channel from SLACK_CHANNEL_ID.
MCP_CHANNEL_ID = (os.environ.get("MCP_CHANNEL_ID", "").strip()
                  or (bot.SLACK_CHANNEL_IDS[0] if bot.SLACK_CHANNEL_IDS else ""))

CATEGORIES = bot.ESCALATION_TARGETS          # ["SolEng", "DevRel", "DevX", "None"]
REAL_CATEGORIES = {"SolEng", "DevRel", "DevX"}
PRIORITIES = bot.PRIORITIES                   # ["High", "Medium", "Low"]

# The Slack Web client is the one bot.py already built from SLACK_BOT_TOKEN.
client = bot.app.client


# ---------------------------------------------------------------------------
# Lazy shared store (opening the sheet does network I/O; do it on first use).
# ---------------------------------------------------------------------------
_store: "bot.SheetStore | None" = None


def get_store() -> "bot.SheetStore":
    global _store
    if _store is None:
        _store = bot.SheetStore(bot.SHEET_ID, bot.SHEET_TAB, bot.GOOGLE_CREDENTIALS_FILE)
        _log(f"sheet ready: {len(_store.rows)} rows, gid={_store.gid}")
    return _store


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _norm_id(s: str) -> str:
    """Normalize an ID token so '#12', 'Q-12', ' 12 ' and '12' all compare equal.
    Falls back to the lowercased stripped string for non-numeric IDs."""
    t = str(s).strip().lstrip("#").strip()
    for pre in ("Q-", "q-", "Q", "q"):
        if t.startswith(pre) and t[len(pre):].strip().isdigit():
            t = t[len(pre):].strip()
            break
    return t.lstrip("0") or t if t.isdigit() else t.lower()


def _parse_ids(ids) -> list[str]:
    """Accept a list, or a comma/space/newline-separated string, of IDs."""
    if isinstance(ids, (list, tuple)):
        raw = ids
    else:
        raw = re.split(r"[,\s]+", str(ids))
    return [x.strip() for x in raw if str(x).strip()]


def _find_row(store: "bot.SheetStore", id_str: str) -> "bot.Row | None":
    want = _norm_id(id_str)
    for row in store.rows.values():
        if _norm_id(row.values.get("ID", "")) == want:
            return row
    return None


def _permalink(channel: str, ts: str) -> str:
    try:
        return client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        return ""


def _platform_from_source(source: str) -> str:
    s = (source or "").lower()
    if "github.com" in s:
        return "GitHub"
    if "t.me" in s or "telegram" in s:
        return "Telegram"
    if "discord" in s:
        return "Discord"
    if "forums.sui" in s or "forum" in s:
        return "Sui Forum"
    if "x.com" in s or "twitter" in s:
        return "X"
    if "slack.com" in s:
        return "Slack"
    return ""


def _thread_messages(channel: str, ts: str, limit: int = 60) -> list[dict]:
    """Return the thread as [{who, text, is_bot, ts}], oldest→newest."""
    try:
        resp = client.conversations_replies(channel=channel, ts=ts, limit=limit)
    except Exception as exc:
        _log(f"WARN could not fetch thread {ts}: {exc}")
        return []
    out = []
    for m in resp.get("messages", []):
        out.append({
            "who": "bot" if m.get("bot_id") else bot.user_display_name(m.get("user", "")),
            "text": m.get("text", ""),
            "is_bot": bool(m.get("bot_id")),
            "ts": m.get("ts", ""),
        })
    return out


def _has_check_reaction(channel: str, ts: str) -> bool:
    try:
        resp = client.reactions_get(channel=channel, timestamp=ts)
        reactions = (resp.get("message", {}) or {}).get("reactions", []) or []
        return any(r.get("name", "") in bot.CHECK_REACTIONS for r in reactions)
    except Exception:
        return False


def _ping_tag() -> str:
    return f"<@{PING_USER_ID}>" if PING_USER_ID else PING_USER_NAME


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sui-ops")


@mcp.tool()
def post_message(text: str, source: str, category: str = "None",
                 priority: str = "Medium", raised_by: str = "") -> str:
    """Post a new developer question to the dev-leads Slack channel and log it to the
    shared tracker sheet, returning its assigned ID and a permalink.

    Always confirm a `source` before posting — it is where the question originally
    came from (a Telegram/Discord/Sui-Forum link, a GitHub issue URL, etc.). If the
    user hasn't given one, ask for it first.

    Args:
        text: The question / issue to post (one clear sentence or short paragraph).
        source: Where it came from — a URL or a short venue name. Required.
        category: Department to route to. One of SolEng, DevRel, DevX, None.
        priority: High, Medium, or Low.
        raised_by: Who originally raised it, if known.
    """
    store = get_store()

    cat = next((c for c in CATEGORIES if c.lower() == (category or "").strip().lower()), "None")
    prio = next((p for p in PRIORITIES if p.lower() == (priority or "").strip().lower()), "Medium")
    if not source or not source.strip():
        return ("⚠️ A `source` is required (where the question came from — a link or a "
                "venue name). Ask the user for it, then call post_message again.")

    channel = MCP_CHANNEL_ID
    if not channel:
        return "⚠️ No channel configured. Set MCP_CHANNEL_ID or SLACK_CHANNEL_ID."

    is_url = source.strip().startswith("http")
    escalated = cat in REAL_CATEGORIES

    # 1) Post a first version so we get a ts, then log the row (ID is assigned by the
    #    sheet), then edit the message to include the ID + row link.
    dept_label = cat if escalated else "General"
    preface = (f":sparkles: *New question*  ·  {dept_label}  ·  {prio}\n"
               f"{text}\n"
               f"*Source:* {source}")
    if raised_by.strip():
        preface += f"\n*Raised by:* {raised_by.strip()}"
    try:
        posted = bot.post(channel=channel, text=preface)
    except Exception as exc:
        return f"❌ Failed to post to Slack: {exc}"
    ts = posted["ts"]

    fields = {
        "Date Asked": bot.today_str(),
        "Window": bot.WINDOW_DEFAULT,
        "Platform": _platform_from_source(source),
        "Channel": source,
        "Question Summary": text,
        "Link": source if is_url else "",
        "Raised By": raised_by.strip(),
        "Owner": bot.OWNER_DEFAULT,
        "Priority": prio,
        "Status": bot.STATUS_ESCALATED if escalated else bot.STATUS_OPEN,
        "Escalated To": cat if escalated else "",
        "Date Resolved": "",
        store.notes_col: "",
        "Slack Channel": channel,
        "Slack TS": ts,
        "Bot Refs": json.dumps({"anchor_ts": ts, "source": "mcp"}),
    }
    row = store.append(fields)
    if not row:
        return (f"⚠️ Posted to Slack (permalink {_permalink(channel, ts)}) but could not "
                f"confirm the tracker row — check the sheet.")
    rid = row.values.get("ID") or f"row{row.row_number}"
    row_link = store.row_link(row.row_number)

    final = (f":sparkles: *#{rid}*  ·  {dept_label}  ·  {prio}\n"
             f"{text}\n"
             f"*Source:* {source}")
    if raised_by.strip():
        final += f"\n*Raised by:* {raised_by.strip()}"
    final += f"\n<{row_link}|Open tracker row ↗>"
    try:
        client.chat_update(channel=channel, ts=ts, text=final)
    except Exception as exc:
        _log(f"WARN could not edit in the ID: {exc}")

    _audit("mcp_post", id=rid, row=row.row_number, category=cat, priority=prio, ts=ts)
    permalink = _permalink(channel, ts)
    return (f"✅ Posted *#{rid}* ({dept_label}, {prio}) to the dev-leads channel.\n"
            f"• Slack: {permalink or ts}\n"
            f"• Tracker row: {row_link}")


@mcp.tool()
def check_status(ids: str) -> str:
    """Check the status of one or more tracked questions by ID and report, for each,
    whether it looks Solved / Forwarded / Open, plus the last reply in its thread.

    Solved = the tracker row is closed, OR a ✅ reaction is on the message, OR Claude
    judges the thread to contain a real answer. Forwarded = someone replied
    "forwarded" (the message was handed off elsewhere). Otherwise Open.

    Args:
        ids: One or more IDs, e.g. "12" or "12, 13, 15".
    """
    store = get_store()
    store.reload()
    id_list = _parse_ids(ids)
    if not id_list:
        return "⚠️ Give me at least one ID, e.g. `12` or `12,13`."

    out = []
    for raw in id_list:
        row = _find_row(store, raw)
        if not row:
            out.append(f"*#{raw}* — not found in the tracker.")
            continue
        rid = row.values.get("ID", raw)
        channel = row.slack_channel or MCP_CHANNEL_ID
        ts = row.original_ts
        summary = row.values.get("Question Summary", "")
        link = row.values.get("Link", "") or store.row_link(row.row_number)

        if not ts or not channel:
            out.append(f"*#{rid}* — {row.status} (no Slack thread linked). {summary} <{link}|↗>")
            continue

        msgs = _thread_messages(channel, ts)
        replies = [m for m in msgs[1:]] if msgs else []
        last = next((m for m in reversed(msgs) if not m["is_bot"]), None)
        forwarded = next((m for m in replies
                          if "forward" in (m["text"] or "").lower()), None)

        if row.status == bot.STATUS_CLOSED:
            verdict = "✅ Solved (closed in tracker)"
        elif _has_check_reaction(channel, ts):
            verdict = "✅ Solved (✅ on message)"
        elif forwarded:
            verdict = f"➡️ Forwarded (by {forwarded['who']})"
        else:
            thread_text = "\n".join(f"{m['who']}: {m['text']}" for m in msgs if not m["is_bot"])
            resolved = False
            if thread_text.strip():
                try:
                    res = bot.judge_resolution(summary, thread_text)
                    resolved = bool(res["input"].get("resolved"))
                except Exception as exc:
                    _log(f"WARN judge failed for #{rid}: {exc}")
            verdict = "✅ Looks solved" if resolved else "🕓 Open — no answer yet"

        last_str = f"{last['who']}: {last['text']}" if last else "(no replies yet)"
        out.append(
            f"*#{rid}* — {verdict}\n"
            f"    {summary}\n"
            f"    _last:_ {last_str}\n"
            f"    <{link}|open ↗>"
        )
    return "\n".join(out)


@mcp.tool()
def weekly_report(days: int = 7, post: bool = False) -> str:
    """Build a report of the open (unanswered) questions and their IDs, ages, owners
    and links. Returns the report text as a preview; set post=True to publish it to
    the dev-leads channel (ask the user to confirm before posting).

    Args:
        days: Highlight items open longer than this many days (default 7).
        post: If true, post the report to the dev-leads channel.
    """
    store = get_store()
    store.reload()
    rows = store.open_rows()
    if not rows:
        report = ":white_check_mark: No open questions — the tracker is clear."
    else:
        rows.sort(key=lambda r: r.values.get("Date Asked", ""))
        lines = [f"*:memo: Open questions report* — {len(rows)} unanswered"]
        for r in rows:
            rid = r.values.get("ID", "?")
            age = bot._age_days(r.values.get("Date Asked", ""))
            agestr = f"{age}d" if age is not None else "?"
            flag = " :hourglass_flowing_sand:" if (age is not None and age > days) else ""
            tgt = r.values.get("Escalated To", "") or "General"
            raised = r.values.get("Raised By", "")
            channel = r.slack_channel or MCP_CHANNEL_ID
            link = r.values.get("Link", "")
            if not link and r.original_ts and channel:
                link = _permalink(channel, r.original_ts)
            link = link or store.row_link(r.row_number)
            raised_str = f" · raised by {raised}" if raised else ""
            lines.append(f"• *#{rid}* [{r.status}, {agestr}{flag}] "
                         f"{r.values.get('Question Summary','')} → {tgt}{raised_str} <{link}|↗>")
        report = "\n".join(lines)

    if post:
        channel = MCP_CHANNEL_ID
        if not channel:
            return "⚠️ No channel configured to post to. Set MCP_CHANNEL_ID or SLACK_CHANNEL_ID."
        header = report
        if PING_USER_ID:
            header = f"<@{PING_USER_ID}> weekly open-questions report:\n{report}"
        try:
            posted = bot.post(channel=channel, text=header)
        except Exception as exc:
            return f"❌ Failed to post the report: {exc}"
        _audit("mcp_report_posted", count=len(rows), ts=posted.get("ts"))
        return f"✅ Report posted to the dev-leads channel.\n{_permalink(channel, posted['ts'])}"

    return report + "\n\n_(preview — call weekly_report with post=true to publish it.)_"


@mcp.tool()
def mark_solved(ids: str) -> str:
    """Mark one or more questions solved: add a ✅ reaction to the message and set the
    tracker row(s) to Closed with today's date.

    Args:
        ids: One or more IDs, e.g. "12" or "12, 13".
    """
    store = get_store()
    store.reload()
    id_list = _parse_ids(ids)
    if not id_list:
        return "⚠️ Give me at least one ID."

    done, notes = [], []
    for raw in id_list:
        row = _find_row(store, raw)
        if not row:
            notes.append(f"#{raw}: not found")
            continue
        rid = row.values.get("ID", raw)
        channel = row.slack_channel or MCP_CHANNEL_ID
        ts = row.original_ts
        if channel and ts:
            try:
                client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
            except Exception as exc:
                if "already_reacted" not in str(exc):
                    notes.append(f"#{rid}: reaction failed ({exc})")
        store.set(row.row_number, {"Status": bot.STATUS_CLOSED,
                                   "Date Resolved": bot.today_str()})
        _audit("mcp_mark_solved", id=rid, row=row.row_number)
        done.append(str(rid))

    msg = ""
    if done:
        msg += f"✅ Marked solved & closed: {', '.join('#' + d for d in done)}."
    if notes:
        msg += ("\n" if msg else "") + "⚠️ " + "; ".join(notes)
    return msg or "Nothing to do."


@mcp.tool()
def ping(ids: str, note: str = "") -> str:
    """Post a follow-up reply in each question's thread, tagging the owner for a reply.

    Args:
        ids: One or more IDs, e.g. "12" or "12, 13".
        note: Optional extra context to include in the ping.
    """
    store = get_store()
    store.reload()
    id_list = _parse_ids(ids)
    if not id_list:
        return "⚠️ Give me at least one ID."

    pinged, notes = [], []
    tag = _ping_tag()
    for raw in id_list:
        row = _find_row(store, raw)
        if not row:
            notes.append(f"#{raw}: not found")
            continue
        rid = row.values.get("ID", raw)
        channel = row.slack_channel or MCP_CHANNEL_ID
        ts = row.original_ts
        if not (channel and ts):
            notes.append(f"#{rid}: no Slack thread linked")
            continue
        text = f"{tag} :wave: gentle follow-up on *#{rid}* — any update here?"
        if note.strip():
            text += f"\n{note.strip()}"
        try:
            bot.post(channel=channel, thread_ts=ts, text=text)
            _audit("mcp_ping", id=rid, row=row.row_number)
            pinged.append(str(rid))
        except Exception as exc:
            notes.append(f"#{rid}: post failed ({exc})")

    msg = ""
    if pinged:
        msg += f"📣 Pinged: {', '.join('#' + p for p in pinged)}."
    if notes:
        msg += ("\n" if msg else "") + "⚠️ " + "; ".join(notes)
    return msg or "Nothing to ping."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _check() -> int:
    problems = []
    if not bot.SLACK_BOT_TOKEN.startswith("xoxb-"):
        problems.append("SLACK_BOT_TOKEN missing or not an xoxb- token")
    if not MCP_CHANNEL_ID:
        problems.append("MCP_CHANNEL_ID / SLACK_CHANNEL_ID not set")
    if not bot.ANTHROPIC_API_KEY:
        problems.append("ANTHROPIC_API_KEY not set (needed for check_status judging)")
    if not bot.SHEET_ID:
        problems.append("SHEET_ID not set")
    if not os.path.exists(bot.GOOGLE_CREDENTIALS_FILE):
        problems.append(f"service account file not found: {bot.GOOGLE_CREDENTIALS_FILE}")
    if not PING_USER_ID:
        _err("note: PING_USER_ID not set — /ping will tag the plain text "
             f"'{PING_USER_NAME}' instead of a real @mention.")
    if problems:
        for p in problems:
            _err(f"CONFIG ERROR: {p}")
        return 1
    _err(f"preflight OK — channel={MCP_CHANNEL_ID}, sheet={bot.SHEET_ID}, "
         f"ping_target={'<@'+PING_USER_ID+'>' if PING_USER_ID else PING_USER_NAME}")
    return 0


def _smoke() -> int:
    """End-to-end test against the configured channel (#ttest for internal testing):
    post a throwaway question, check its status, ping it, mark it solved, re-check.
    This posts REAL messages + a ✅ to MCP_CHANNEL_ID and closes the row it creates —
    only run it in an internal/test channel. Not part of the MCP surface."""
    if _check() != 0:
        return 1
    ch = MCP_CHANNEL_ID
    print(f"\n=== SMOKE TEST against channel {ch} (posts real messages) ===\n", flush=True)

    print("→ post_message", flush=True)
    r = post_message(
        text="[smoke test] please ignore — MCP end-to-end check.",
        source="https://github.com/MystenLabs/walrus/issues/3443",
        category="DevX", priority="Low", raised_by="smoke",
    )
    print(r + "\n", flush=True)
    m = re.search(r"#([\w-]+)", r)          # [\w-]+ so trailing markdown `*` isn't captured
    if not m:
        print("could not parse an ID from post_message — stopping.", flush=True)
        return 1
    rid = m.group(1)

    for label, fn in (
        ("check_status", lambda: check_status(rid)),
        ("ping", lambda: ping(rid, note="(smoke test — ignore)")),
        ("mark_solved", lambda: mark_solved(rid)),
        ("check_status (after solve)", lambda: check_status(rid)),
        ("weekly_report (preview)", lambda: weekly_report(post=False)),
    ):
        print(f"→ {label}", flush=True)
        try:
            print(fn() + "\n", flush=True)
        except Exception as exc:
            print(f"  ERROR: {exc}\n", flush=True)

    # Self-cleanup: remove every smoke artifact (this run's + any leftovers) from the
    # shared sheet AND delete the throwaway Slack messages, so the real tracker and
    # channel stay pristine.
    print("→ cleanup (delete smoke rows + Slack messages)", flush=True)
    store = get_store()
    store.reload()
    smoke_rows = [rw for rw in list(store.rows.values())
                  if str(rw.values.get("Question Summary", "")).startswith("[smoke test]")]
    for rw in sorted(smoke_rows, key=lambda x: -x.row_number):  # bottom-up: row numbers stay valid
        rid2 = rw.values.get("ID", "?")
        chan = rw.slack_channel or ch
        tsv = rw.original_ts
        try:
            store.delete_row(rw.row_number)
            if chan and tsv:
                try:
                    client.chat_delete(channel=chan, ts=tsv)
                except Exception as exc:
                    print(f"  note: could not delete Slack msg for #{rid2}: {exc}", flush=True)
            print(f"  removed smoke #{rid2}", flush=True)
        except Exception as exc:
            print(f"  ERROR cleaning #{rid2}: {exc}", flush=True)

    print(f"=== SMOKE DONE — full cycle on #{rid}, artifacts cleaned. ===", flush=True)
    return 0


class _BearerASGI:
    """Tiny pure-ASGI gate in front of the MCP app: every /mcp request must carry
    `Authorization: Bearer <MCP_HTTP_TOKEN>`. Pure ASGI (not BaseHTTPMiddleware) so it
    doesn't buffer the streaming responses MCP uses. `lifespan` and non-http scopes
    pass straight through so the session manager still starts. `GET /healthz` is open."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/healthz":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"ok":true}'})
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {self.token}":
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        return await self.app(scope, receive, send)


def _run_http() -> int:
    """Serve the MCP over streamable HTTP (for remote devleads) with bearer-token auth.
    Endpoint: http://<host>:<port>/mcp  ·  health: GET /healthz."""
    token = os.environ.get("MCP_HTTP_TOKEN", "").strip()
    if not token:
        _err("REFUSING to start: MCP_HTTP_TOKEN is not set — this endpoint can post to "
             "Slack and write the sheet, so it must not be exposed without a token.")
        return 1
    if _check() != 0:
        return 1
    host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_HTTP_PORT", "8787"))
    import uvicorn
    # The SDK's DNS-rebinding guard only allows a localhost Host header by default, so
    # remote clients hitting http://<public-ip>:<port> get a 421 "Invalid Host header".
    # We already gate every request with a bearer token, so turn that guard off (any
    # Host/Origin allowed) to let remote devleads connect.
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False)
    app = _BearerASGI(mcp.streamable_http_app(), token)
    _err(f"MCP HTTP server on {host}:{port} (endpoint /mcp, bearer-auth on).")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_check())
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    if "--http" in sys.argv:
        sys.exit(_run_http())
    mcp.run()
