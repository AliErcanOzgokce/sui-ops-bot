#!/usr/bin/env python3
"""Auto-tracker runtime: the always-on Slack Socket Mode app.

Turns a Slack channel + the Google Sheet into a low-effort, always-organized
escalation tracker:

  1. Auto-log new escalations. A cheap local pre-filter drops chatter; substantive
     top-level messages go to Claude, which decides if it is a NEW developer
     escalation and extracts the structured fields, including the two taxonomy
     axes (product + type). If yes, a row is appended and the bot posts an
     in-thread note with a deep link. React :x: to discard a false positive.
  2. Auto-update on resolution (human-in-the-loop). A thread reply on a tracked
     message, or a :white_check_mark: on it, triggers Claude to judge resolution.
     If resolved, the bot proposes closure and asks the owner to confirm with a
     :white_check_mark:; a confirming reaction closes it.
  3. Status commands. /status, /open, /aging (and "@bot status|open|aging").

Run: ``python -m sui_ops_bot.slackbot`` (``--check`` validates config and exits,
``--diag`` runs live read-only diagnostics).
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback

from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import config, reports
from .attachments import download_image, image_refs
from .classify import classify_message, judge_resolution
from .ids import clean_channel, effective_text, is_substantive, shared_attachment
from .logutil import audit, current_window, log, today_str
from .sheet import Row, SheetStore
from .slack_client import (
    app,
    permalink,
    post,
    thread_text,
    user_display_name,
)

store: SheetStore | None = None
_bot_user_id: str | None = None


# -- Feature 1: auto-log new escalations ------------------------------------
def download_images(refs: list, limit: int = 4) -> list[dict]:
    """Fetch the bytes for each image reference with the bot token, as
    ``{data, mime}`` for the classifier. Failures are logged without any bytes and
    skipped, so a broken download never blocks classification. At most ``limit``
    images are fetched to bound work on a message with many attachments."""
    out = []
    for ref in refs[:limit]:
        try:
            out.append({"data": download_image(ref, config.SLACK_BOT_TOKEN), "mime": ref.mime})
        except Exception as exc:
            log(f"WARN could not download image ({ref.mime}): {exc}")
    return out


def classify_and_log(channel: str, ts: str, user: str, text: str,
                     forwarded: dict | None = None, images: list[dict] | None = None) -> None:
    try:
        result = classify_message(text, images=images)
    except Exception as exc:
        log(f"ERROR classify failed: {exc}")
        return
    data = result["input"]
    audit("classify", channel=channel, slack_ts=ts, user=user, text=text[:500],
          verdict=data, tokens_in=result["tokens_in"], tokens_out=result["tokens_out"])
    if not data.get("is_escalation"):
        return

    poster_name = user_display_name(user)
    forwarded = forwarded or {}
    link = ""
    try:
        link = app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        pass
    # For a forwarded message, prefer the original author and a link to the source
    # message over the person who forwarded it and the in-channel permalink.
    fwd_author = (forwarded.get("author_name") or "").strip()
    fwd_url = (forwarded.get("from_url") or "").strip()
    product = data.get("product", config.PRODUCT_DEFAULT) or config.PRODUCT_DEFAULT
    qtype = data.get("type", config.TYPE_DEFAULT) or config.TYPE_DEFAULT
    fields = {
        # ID is left to the sheet's own formula (set inside store.append).
        "Date Asked": today_str(),
        "Window": config.WINDOW_DEFAULT or current_window(),
        "Platform": data.get("platform", ""),
        "Channel": clean_channel(data.get("source_channel", "")),
        "Question Summary": data.get("question_summary", ""),
        "Link": data.get("link", "") or fwd_url or link,
        "Raised By": data.get("raised_by", "") or fwd_author or poster_name,
        # The lead who logged it owns it (matches the sheet's old convention), and
        # their uid is kept in Bot Refs for @-mentions.
        "Owner": poster_name or config.OWNER_DEFAULT,
        "Priority": data.get("priority", ""),
        "Status": config.STATUS_ESCALATED,
        "Product": product,
        "Type": qtype,
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
    row_link = store.row_link(row.row_number)
    try:
        posted = post(
            channel=channel, thread_ts=ts,
            text=f":pushpin: Logged as #{rid} ({product} · {qtype}). Discard or mark solved below.",
            blocks=reports.escalation_note_blocks(rid, product, qtype, row_link, value=ts),
        )
        store.set_refs(row.row_number, anchor_ts=posted["ts"])
    except Exception as exc:
        log(f"WARN could not post log note: {exc}")
    log(f"logged escalation #{rid} row {row.row_number} product={product} type={qtype}")


# -- Feature 2: resolution (human-in-the-loop) ------------------------------
def run_resolution_check(row: Row, channel: str, thread_ts: str) -> None:
    # Only for still-active items, and only once (skip if we have already proposed
    # a resolution and are awaiting the confirmation).
    if row.status not in config.OPEN_STATUSES or row.confirm_ts:
        return
    question = row.values.get("Question Summary", "")
    thread = thread_text(channel, thread_ts)
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
    # Keep the sheet's Status UNCHANGED (item stays active until a human confirms).
    # Record the proposal in Notes / Handoff without clobbering existing text.
    existing = row.values.get(store.notes_col, "")
    proposed = f"⏳ Proposed (react ✅ to close): {summary}"
    if answered_by:
        proposed += f" — answered by {answered_by}"
    new_note = proposed + (f"\n{existing}" if existing else "")
    store.set(row.row_number, {store.notes_col: new_note})
    row_link = store.row_link(row.row_number)
    owner_tag = f"<@{row.owner_uid}>" if row.owner_uid else row.values.get("Owner", "there")
    try:
        posted = post(
            channel=channel, thread_ts=thread_ts,
            text=(f"{owner_tag} Looks resolved — {summary or 'see thread'}. "
                  f"React :white_check_mark: to confirm and I'll close it "
                  f"(<{row_link}|row>)."),
        )
        store.set_refs(row.row_number, confirm_ts=posted["ts"])
    except Exception as exc:
        log(f"WARN could not post confirm note: {exc}")
    log(f"row {row.row_number} proposed resolution (awaiting ✅)")


def close_row(row: Row, channel: str) -> None:
    store.set(row.row_number, {"Status": config.STATUS_CLOSED, "Date Resolved": today_str()})
    audit("close", row=row.row_number, id=row.values.get("ID"))
    try:
        post(channel=channel, thread_ts=row.original_ts,
             text=f":lock: Confirmed — *#{row.values.get('ID')}* closed.")
    except Exception:
        pass
    log(f"row {row.row_number} -> Closed")


def discard_row(row: Row, channel: str) -> None:
    rid = row.values.get("ID")
    audit("discard", row=row.row_number, id=rid)
    orig = row.original_ts
    store.delete_row(row.row_number)  # false positive -> remove the row entirely
    try:
        post(channel=channel, thread_ts=orig,
             text=":wastebasket: Discarded — removed from the tracker.")
    except Exception:
        pass
    log(f"row {row.row_number} -> Discarded")


# -- Slack event handlers ----------------------------------------------------
def handle_text_command(channel: str, text: str, ts: str, thread_ts: str | None) -> bool:
    """Text-based command fallback. Works with only channels:history + chat:write,
    so status commands work even before the app is reinstalled with the native
    slash-command scopes. Triggers on a leading `!`/`/` or an @mention of the bot,
    followed by `status` | `open` | `aging`. Returns True if it handled the message."""
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
        post(channel=channel, thread_ts=reply_thread, text=reports.status_report(store))
    elif key == "open":
        post(channel=channel, thread_ts=reply_thread, text=reports.open_report(store, permalink))
    elif key in ("aging", "aged", "old"):
        post(channel=channel, thread_ts=reply_thread, text=reports.aging_report(store, permalink))
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
        if config.SLACK_CHANNEL_IDS and channel not in config.SLACK_CHANNEL_IDS:
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

        # A forwarded message has empty top-level text; its content lives in a
        # shared attachment. effective_text merges both so a plain forward tracks.
        eff = effective_text(event)
        # An image (direct or forwarded) counts as substance on its own, so an
        # image-only report is not dropped for lack of text.
        refs = image_refs(event)
        if not is_substantive(eff, config.MIN_MESSAGE_CHARS, has_image=bool(refs)):
            return
        if store.ts_tracked(ts):
            return
        classify_and_log(channel, ts, user, eff, forwarded=shared_attachment(event),
                         images=download_images(refs))
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
        #   x on the bot's log note OR the original -> discard (while still open)
        #   check on the bot's confirm  -> close
        #   check on the original       -> run the resolution check
        if (reaction in config.DISCARD_REACTIONS
                and ts in (row.anchor_ts, row.original_ts)
                and row.status in config.OPEN_STATUSES):
            discard_row(row, channel)
        elif reaction in config.CHECK_REACTIONS:
            if ts == row.confirm_ts and row.status in config.OPEN_STATUSES:
                close_row(row, channel)
            elif ts == row.original_ts:
                run_resolution_check(row, channel, row.original_ts)
    except Exception:
        log("ERROR in on_reaction:\n" + traceback.format_exc())


def _handle_row_action(body: dict, kind: str) -> None:
    """Shared handler for the Discard / Mark-solved buttons on a log note. `value`
    carries the escalation message ts, so the row is found the same way a reaction
    on the original message would find it."""
    try:
        value = (body.get("actions") or [{}])[0].get("value", "")
        channel = body.get("channel", {}).get("id", "")
        note_ts = body.get("container", {}).get("message_ts", "")
        row = store.find_by_ts(value)
        if not row or row.status not in config.OPEN_STATUSES:
            return
        rid = row.values.get("ID")
        if kind == "discard":
            discard_row(row, channel)
            note = f":wastebasket: *#{rid}* discarded."
        else:
            close_row(row, channel)
            note = f":lock: *#{rid}* marked solved."
        # Replace the note's buttons so they cannot be tapped again.
        if channel and note_ts:
            try:
                app.client.chat_update(channel=channel, ts=note_ts, text=note, blocks=[])
            except Exception as exc:
                log(f"WARN could not update note: {exc}")
    except Exception:
        log("ERROR in row action:\n" + traceback.format_exc())


@app.action("row_discard")
def act_discard(ack, body):
    ack()
    _handle_row_action(body, "discard")


@app.action("row_solved")
def act_solved(ack, body):
    ack()
    _handle_row_action(body, "solved")


@app.command("/status")
def cmd_status(ack, respond):
    ack()
    respond(reports.status_report(store))


@app.command("/open")
def cmd_open(ack, respond):
    ack()
    respond(reports.open_report(store, permalink))


@app.command("/aging")
def cmd_aging(ack, respond):
    ack()
    respond(reports.aging_report(store, permalink))


@app.event("app_mention")
def on_mention(event, say):
    text = (event.get("text", "") or "").lower()
    if "aging" in text:
        say(reports.aging_report(store, permalink))
    elif "open" in text:
        say(reports.open_report(store, permalink))
    else:
        say(reports.status_report(store))


# ---------------------------------------------------------------------------
# Startup backfill — Socket Mode does not replay missed events.
# ---------------------------------------------------------------------------
def backfill() -> None:
    if config.BACKFILL_HOURS <= 0 or not config.SLACK_CHANNEL_IDS:
        return
    oldest = time.time() - config.BACKFILL_HOURS * 3600
    for channel in config.SLACK_CHANNEL_IDS:
        try:
            cursor = None
            scanned = 0
            while True:
                resp = app.client.conversations_history(
                    channel=channel, oldest=str(oldest), limit=200, cursor=cursor)
                for m in resp.get("messages", []):
                    if m.get("subtype") or m.get("bot_id"):
                        continue
                    ts = m.get("ts", "")
                    if store.ts_tracked(ts):
                        continue
                    eff = effective_text(m)
                    refs = image_refs(m)
                    if not is_substantive(eff, config.MIN_MESSAGE_CHARS, has_image=bool(refs)):
                        continue
                    scanned += 1
                    classify_and_log(channel, ts, m.get("user", ""), eff,
                                     forwarded=shared_attachment(m),
                                     images=download_images(refs))
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            log(f"backfill channel {channel}: classified {scanned} untracked messages")
        except Exception as exc:
            log(f"WARN backfill failed for {channel}: {exc}")


# ---------------------------------------------------------------------------
# Preflight + diagnostics + main
# ---------------------------------------------------------------------------
def preflight() -> list[str]:
    problems = []
    if not config.SLACK_BOT_TOKEN.startswith("xoxb-"):
        problems.append("SLACK_BOT_TOKEN missing or not an xoxb- token")
    if not config.SLACK_APP_TOKEN.startswith("xapp-"):
        problems.append("SLACK_APP_TOKEN missing or not an xapp- token")
    if not config.SLACK_CHANNEL_IDS:
        problems.append("SLACK_CHANNEL_ID not set")
    if not config.ANTHROPIC_API_KEY:
        problems.append("ANTHROPIC_API_KEY not set")
    if not config.SHEET_ID:
        problems.append("SHEET_ID not set")
    if not __import__("os").path.exists(config.GOOGLE_CREDENTIALS_FILE):
        problems.append(f"service account file not found: {config.GOOGLE_CREDENTIALS_FILE}")
    return problems


def run_diag() -> None:
    """Live end-to-end diagnostics: Slack identity, Anthropic, Sheet, channel
    membership. Read-only except one tiny Anthropic call. Does NOT connect Socket
    Mode."""
    log("=== DIAG START ===")
    try:
        who = app.client.auth_test()
        log(f"[slack] auth OK — team={who.get('team')} bot user_id={who.get('user_id')} "
            f"name='{who.get('user')}'")
    except Exception as exc:
        log(f"[slack] FAIL auth_test: {exc}")

    for ch in config.SLACK_CHANNEL_IDS:
        try:
            info = app.client.conversations_info(channel=ch)["channel"]
            log(f"[slack] channel {ch} name='{info.get('name')}' is_member={info.get('is_member')}")
            if not info.get("is_member"):
                log(f"[slack] WARN not a member of {ch} — run /invite in that channel")
            app.client.conversations_history(channel=ch, limit=1)
            log(f"[slack] channel {ch} history read OK")
        except Exception as exc:
            log(f"[slack] FAIL channel {ch}: {exc}")

    try:
        pos = classify_message("Dev on Discord is getting a 500 from the mainnet RPC endpoint "
                               "when calling sui_getObject, blocking their launch.")
        pi = pos["input"]
        log(f"[anthropic] classify(sample).is_escalation={pi.get('is_escalation')} "
            f"product={pi.get('product')} type={pi.get('type')} priority={pi.get('priority')}")
        neg = classify_message("gm everyone, great work on the demo yesterday 🎉")
        log(f"[anthropic] classify(chatter).is_escalation={neg['input'].get('is_escalation')}")
    except Exception as exc:
        log(f"[anthropic] FAIL: {exc}")

    try:
        s = SheetStore(config.SHEET_ID, config.SHEET_TAB, config.GOOGLE_CREDENTIALS_FILE)
        log(f"[sheet] OK — header row {s.header_row}, {len(s.rows)} data rows, "
            f"notes col='{s.notes_col}'")
        missing = [c for c in config.MANAGED_COLUMNS if c not in s.header]
        log(f"[sheet] managed columns present: {'yes' if not missing else 'MISSING ' + str(missing)}")
        log(f"[sheet] open/active rows now: {len(s.open_rows())}")
    except FileNotFoundError as exc:
        log(f"[sheet] FAIL service-account file: {exc}")
    except Exception as exc:
        log(f"[sheet] FAIL — often means the sheet is not shared with the service "
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
        sys.exit(0 if check_only else 1)
    if check_only:
        log("preflight OK")
        return

    store = SheetStore(config.SHEET_ID, config.SHEET_TAB, config.GOOGLE_CREDENTIALS_FILE)
    log(f"sheet ready: {len(store.rows)} rows, gid={store.gid}")
    try:
        _bot_user_id = app.client.auth_test().get("user_id")
    except Exception as exc:
        log(f"WARN could not resolve bot user id: {exc}")
    backfill()
    log("starting Socket Mode…")
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
