#!/usr/bin/env python3
"""MCP control server: natural-language tools for dev-leads inside Claude.

Five tools that map 1:1 to the manual workflow, all backed by the same Google
Sheet the auto-tracker maintains, so IDs stay consistent across every dev lead:

  * post_message   post a new question to the dev-leads channel + log a row.
                   product + type are auto-classified from the text; the caller
                   can override either.
  * check_status   for IDs (or a product/type filter), read the thread and decide
                   Solved / Forwarded / Open, plus the last reply.
  * weekly_report  the open backlog grouped by product, with product/type filters;
                   optionally posts it to the channel.
  * mark_solved    add a check reaction and close the row(s).
  * ping           reply in the thread(s) tagging the owner for a follow-up.

MCP speaks JSON-RPC over stdout, so logging is routed to stderr on import; nothing
but protocol frames may touch stdout.

Run: ``python -m sui_ops_bot.mcpserver`` (stdio), ``--http`` (remote HTTP),
``--check`` (validate config), ``--smoke`` (live end-to-end test).
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import classify as classify_mod
from . import config, logutil, reports

# Reserve stdout for the JSON-RPC transport before anything logs.
logutil.use_stderr()

from .ids import match_enum, norm_id, parse_ids, platform_from_source  # noqa: E402
from .logutil import audit, log, today_str  # noqa: E402
from .sheet import Row, SheetStore  # noqa: E402
from .slack_client import client, has_check_reaction, permalink, post, thread_messages  # noqa: E402

# ---------------------------------------------------------------------------
# Lazy shared store (opening the sheet does network I/O; do it on first use).
# ---------------------------------------------------------------------------
_store: SheetStore | None = None


def get_store() -> SheetStore:
    global _store
    if _store is None:
        _store = SheetStore(config.SHEET_ID, config.SHEET_TAB, config.GOOGLE_CREDENTIALS_FILE)
        log(f"sheet ready: {len(_store.rows)} rows, gid={_store.gid}")
    return _store


def _find_row(store: SheetStore, id_str: str) -> Row | None:
    want = norm_id(id_str)
    for row in store.rows.values():
        if norm_id(row.values.get("ID", "")) == want:
            return row
    return None


def _ping_tag() -> str:
    return f"<@{config.PING_USER_ID}>" if config.PING_USER_ID else config.PING_USER_NAME


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sui-ops")


@mcp.tool()
def post_message(text: str, source: str, product: str = "", type: str = "",
                 priority: str = "", raised_by: str = "") -> str:
    """Post a new developer question to the dev-leads Slack channel and log it to the
    shared tracker sheet, returning its assigned ID and a permalink.

    product and type are auto-classified from the text when left blank; pass them
    to override. Always confirm a `source` before posting (where the question came
    from: a Telegram/Discord/Sui-Forum link, a GitHub issue URL, etc.). If the user
    has not given one, ask for it first.

    Args:
        text: The question / issue to post (one clear sentence or short paragraph).
        source: Where it came from, a URL or a short venue name. Required.
        product: Ecosystem product. One of: DeepBook, Walrus, Harbor, Seal,
            Nautilus, MemWal, Enoki, Slush, zkLogin, SDK, Bridge, Sui Core, Hashi,
            Program, Other. Blank = auto-classify.
        type: Kind of ask. One of: Question, Open PR, Bug, Feature Request,
            Communication. Blank = auto-classify.
        priority: High, Medium, or Low. Blank = auto-classify.
        raised_by: Who originally raised it, if known.
    """
    if not source or not source.strip():
        return ("⚠️ A `source` is required (where the question came from, a link or a "
                "venue name). Ask the user for it, then call post_message again.")
    channel = config.MCP_CHANNEL_ID
    if not channel:
        return "⚠️ No channel configured. Set MCP_CHANNEL_ID or SLACK_CHANNEL_ID."

    store = get_store()

    # Auto-classify only what the caller left blank (one LLM call, best-effort).
    auto: dict = {}
    if not (product.strip() and type.strip() and priority.strip()):
        try:
            auto = classify_mod.classify_message(text).get("input", {})
        except Exception as exc:
            log(f"WARN auto-classify failed, using defaults: {exc}")

    prod = match_enum(product or auto.get("product", ""), config.PRODUCTS, config.PRODUCT_DEFAULT)
    qtype = match_enum(type or auto.get("type", ""), config.TYPES, config.TYPE_DEFAULT)
    prio = match_enum(priority or auto.get("priority", ""), config.PRIORITIES, "Medium")

    is_url = source.strip().startswith("http")
    badge = f"{prod} · {qtype}"

    # Post a first version to get a ts, log the row (ID assigned by the sheet), then
    # edit the message to include the ID + row link.
    preface = (f":sparkles: *New question*  ·  {badge}  ·  {prio}\n"
               f"{text}\n*Source:* {source}")
    if raised_by.strip():
        preface += f"\n*Raised by:* {raised_by.strip()}"
    try:
        posted = post(channel=channel, text=preface)
    except Exception as exc:
        return f"❌ Failed to post to Slack: {exc}"
    ts = posted["ts"]

    fields = {
        "Date Asked": today_str(),
        "Window": config.WINDOW_DEFAULT,
        "Platform": platform_from_source(source),
        "Channel": source,
        "Question Summary": text,
        "Link": source if is_url else "",
        "Raised By": raised_by.strip(),
        "Owner": config.OWNER_DEFAULT,
        "Priority": prio,
        "Status": config.STATUS_ESCALATED,
        "Product": prod,
        "Type": qtype,
        "Date Resolved": "",
        store.notes_col: "",
        "Slack Channel": channel,
        "Slack TS": ts,
        "Bot Refs": json.dumps({"anchor_ts": ts, "source": "mcp"}),
    }
    row = store.append(fields)
    if not row:
        return (f"⚠️ Posted to Slack (permalink {permalink(channel, ts)}) but could not "
                f"confirm the tracker row, check the sheet.")
    rid = row.values.get("ID") or f"row{row.row_number}"
    row_link = store.row_link(row.row_number)

    final = (f":sparkles: *#{rid}*  ·  {badge}  ·  {prio}\n{text}\n*Source:* {source}")
    if raised_by.strip():
        final += f"\n*Raised by:* {raised_by.strip()}"
    final += f"\n<{row_link}|Open tracker row ↗>"
    try:
        client.chat_update(channel=channel, ts=ts, text=final)
    except Exception as exc:
        log(f"WARN could not edit in the ID: {exc}")

    audit("mcp_post", id=rid, row=row.row_number, product=prod, type=qtype, priority=prio, ts=ts)
    return (f"✅ Posted *#{rid}* ({badge}, {prio}) to the dev-leads channel.\n"
            f"• Slack: {permalink(channel, ts) or ts}\n"
            f"• Tracker row: {row_link}")


@mcp.tool()
def check_status(ids: str = "", product: str = "", type: str = "") -> str:
    """Check the status of tracked questions and report, for each, whether it looks
    Solved / Forwarded / Open, plus the last reply in its thread.

    Pass explicit `ids`, or a `product`/`type` filter to check every open item that
    matches (e.g. all open Walrus bugs). Solved = the row is closed, OR a check
    reaction is on the message, OR Claude judges the thread answered. Forwarded =
    someone replied "forwarded".

    Args:
        ids: One or more IDs, e.g. "12" or "12, 13, 15". Optional if product/type given.
        product: Restrict to this product (used when ids is empty).
        type: Restrict to this type (used when ids is empty).
    """
    store = get_store()
    store.reload()
    id_list = parse_ids(ids)
    if id_list:
        rows = [(_find_row(store, raw), raw) for raw in id_list]
    elif product or type:
        matched = reports.filter_rows(store.open_rows(), product=product, type=type)
        rows = [(r, r.values.get("ID", "?")) for r in matched]
        if not rows:
            scope = ", ".join(x for x in (product, type) if x)
            return f"No open items match {scope}."
    else:
        return "⚠️ Give me at least one ID (e.g. `12`) or a product/type filter."

    out = []
    for row, raw in rows:
        if not row:
            out.append(f"*#{raw}* — not found in the tracker.")
            continue
        rid = row.values.get("ID", raw)
        channel = row.slack_channel or config.MCP_CHANNEL_ID
        ts = row.original_ts
        summary = row.values.get("Question Summary", "")
        badge = " · ".join(p for p in (row.product, row.type) if p) or "Unclassified"
        link = row.values.get("Link", "") or store.row_link(row.row_number)

        if not ts or not channel:
            out.append(f"*#{rid}* [{badge}] — {row.status} (no Slack thread). {summary} <{link}|↗>")
            continue

        msgs = thread_messages(channel, ts)
        replies = msgs[1:] if msgs else []
        last = next((m for m in reversed(msgs) if not m["is_bot"]), None)
        forwarded = next((m for m in replies if "forward" in (m["text"] or "").lower()), None)

        if row.status == config.STATUS_CLOSED:
            verdict = "✅ Solved (closed in tracker)"
        elif has_check_reaction(channel, ts):
            verdict = "✅ Solved (✅ on message)"
        elif forwarded:
            verdict = f"➡️ Forwarded (by {forwarded['who']})"
        else:
            thread_text = "\n".join(f"{m['who']}: {m['text']}" for m in msgs if not m["is_bot"])
            resolved = False
            if thread_text.strip():
                try:
                    res = classify_mod.judge_resolution(summary, thread_text)
                    resolved = bool(res["input"].get("resolved"))
                except Exception as exc:
                    log(f"WARN judge failed for #{rid}: {exc}")
            verdict = "✅ Looks solved" if resolved else "🕓 Open — no answer yet"

        last_str = f"{last['who']}: {last['text']}" if last else "(no replies yet)"
        out.append(f"*#{rid}* [{badge}] — {verdict}\n    {summary}\n"
                   f"    _last:_ {last_str}\n    <{link}|open ↗>")
    return "\n".join(out)


@mcp.tool()
def weekly_report(days: int = 7, product: str = "", type: str = "", post: bool = False) -> str:
    """Build a report of the open (unanswered) questions grouped by product, with
    optional product/type filters. Returns a preview; set post=True to publish it to
    the dev-leads channel (ask the user to confirm before posting).

    Args:
        days: Flag items open longer than this many days (default 7).
        product: Restrict to this product (e.g. "Walrus").
        type: Restrict to this type (e.g. "Bug").
        post: If true, post the report to the dev-leads channel.
    """
    store = get_store()
    report = reports.weekly_report(store, days=days, product=product, type=type, linker=permalink)

    if post:
        channel = config.MCP_CHANNEL_ID
        if not channel:
            return "⚠️ No channel configured to post to. Set MCP_CHANNEL_ID or SLACK_CHANNEL_ID."
        header = report
        if config.PING_USER_ID:
            header = f"<@{config.PING_USER_ID}> weekly open-questions report:\n{report}"
        try:
            posted = post(channel=channel, text=header)
        except Exception as exc:
            return f"❌ Failed to post the report: {exc}"
        audit("mcp_report_posted", ts=posted.get("ts"))
        return f"✅ Report posted to the dev-leads channel.\n{permalink(channel, posted['ts'])}"

    return report + "\n\n_(preview — call weekly_report with post=true to publish it.)_"


@mcp.tool()
def mark_solved(ids: str) -> str:
    """Mark one or more questions solved: add a check reaction to the message and set
    the tracker row(s) to Closed with today's date.

    Args:
        ids: One or more IDs, e.g. "12" or "12, 13".
    """
    store = get_store()
    store.reload()
    id_list = parse_ids(ids)
    if not id_list:
        return "⚠️ Give me at least one ID."

    done, notes = [], []
    for raw in id_list:
        row = _find_row(store, raw)
        if not row:
            notes.append(f"#{raw}: not found")
            continue
        rid = row.values.get("ID", raw)
        channel = row.slack_channel or config.MCP_CHANNEL_ID
        ts = row.original_ts
        if channel and ts:
            try:
                client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
            except Exception as exc:
                if "already_reacted" not in str(exc):
                    notes.append(f"#{rid}: reaction failed ({exc})")
        store.set(row.row_number, {"Status": config.STATUS_CLOSED, "Date Resolved": today_str()})
        audit("mcp_mark_solved", id=rid, row=row.row_number)
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
    id_list = parse_ids(ids)
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
        channel = row.slack_channel or config.MCP_CHANNEL_ID
        ts = row.original_ts
        if not (channel and ts):
            notes.append(f"#{rid}: no Slack thread linked")
            continue
        text = f"{tag} :wave: gentle follow-up on *#{rid}* — any update here?"
        if note.strip():
            text += f"\n{note.strip()}"
        try:
            post(channel=channel, thread_ts=ts, text=text)
            audit("mcp_ping", id=rid, row=row.row_number)
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
# Entry points
# ---------------------------------------------------------------------------
def _check() -> int:
    problems = []
    if not config.SLACK_BOT_TOKEN.startswith("xoxb-"):
        problems.append("SLACK_BOT_TOKEN missing or not an xoxb- token")
    if not config.MCP_CHANNEL_ID:
        problems.append("MCP_CHANNEL_ID / SLACK_CHANNEL_ID not set")
    if not config.ANTHROPIC_API_KEY:
        problems.append("ANTHROPIC_API_KEY not set (needed for auto-classify + judging)")
    if not config.SHEET_ID:
        problems.append("SHEET_ID not set")
    if not os.path.exists(config.GOOGLE_CREDENTIALS_FILE):
        problems.append(f"service account file not found: {config.GOOGLE_CREDENTIALS_FILE}")
    if not config.PING_USER_ID:
        log("note: PING_USER_ID not set — ping will tag the plain text "
            f"'{config.PING_USER_NAME}' instead of a real @mention.")
    if problems:
        for p in problems:
            log(f"CONFIG ERROR: {p}")
        return 1
    log(f"preflight OK — channel={config.MCP_CHANNEL_ID}, sheet={config.SHEET_ID}")
    return 0


def _smoke() -> int:
    """End-to-end test against MCP_CHANNEL_ID (use an internal/test channel): post a
    throwaway question, check its status, ping it, mark it solved, re-check, then
    clean up the row + Slack messages. Posts REAL messages, only run where flooding
    is OK."""
    if _check() != 0:
        return 1
    ch = config.MCP_CHANNEL_ID
    print(f"\n=== SMOKE TEST against channel {ch} (posts real messages) ===\n", flush=True)

    print("→ post_message", flush=True)
    r = post_message(
        text="[smoke test] please ignore — MCP end-to-end check.",
        source="https://github.com/MystenLabs/walrus/issues/3443",
        product="Walrus", type="Bug", priority="Low", raised_by="smoke",
    )
    print(r + "\n", flush=True)
    m = re.search(r"#([\w-]+)", r)
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

    print("→ cleanup (delete smoke rows + Slack messages)", flush=True)
    store = get_store()
    store.reload()
    smoke_rows = [rw for rw in list(store.rows.values())
                  if str(rw.values.get("Question Summary", "")).startswith("[smoke test]")]
    for rw in sorted(smoke_rows, key=lambda x: -x.row_number):
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
    `Authorization: Bearer <MCP_HTTP_TOKEN>`. Pure ASGI (not BaseHTTPMiddleware) so
    it does not buffer MCP's streaming responses. `lifespan` and non-http scopes pass
    through so the session manager still starts. `GET /healthz` is open."""

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
    """Serve the MCP over streamable HTTP (for remote dev-leads) with bearer-token
    auth. Endpoint: http://<host>:<port>/mcp ; health: GET /healthz."""
    token = os.environ.get("MCP_HTTP_TOKEN", "").strip()
    if not token:
        log("REFUSING to start: MCP_HTTP_TOKEN is not set — this endpoint can post to "
            "Slack and write the sheet, so it must not be exposed without a token.")
        return 1
    if _check() != 0:
        return 1
    host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_HTTP_PORT", "8787"))
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    # We gate every request with a bearer token, so turn off the SDK's localhost-only
    # Host guard to let remote clients hit http://<public-ip>:<port>.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False)
    app = _BearerASGI(mcp.streamable_http_app(), token)
    log(f"MCP HTTP server on {host}:{port} (endpoint /mcp, bearer-auth on).")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(_check())
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    if "--http" in sys.argv:
        sys.exit(_run_http())
    mcp.run()


if __name__ == "__main__":
    main()
