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
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta

from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import config, reports
from .attachments import download_image, image_refs
from .classify import classify_message, judge_resolution
from .dedup import dedup_key, find_duplicate
from .ids import (
    clip_summary,
    effective_text,
    infer_channel,
    is_admin,
    is_substantive,
    match_enum,
    needs_more_info,
    platform_from_source,
    resolve_platform,
    shared_attachment,
)
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


def annotate_duplicate(row: Row, new_ts: str, channel: str, summary: str = "") -> None:
    """An exact re-report of an already-open row: annotate that row instead of
    opening a second one, and point the new message at it. The new report's own
    summary is folded into the note so its wording is never lost, only its
    separate row. Idempotent: the `dupes` list in Bot Refs is the load-bearing
    guard (the re-report's ts is never added to the ts index), so a backfill
    re-scan finds the ts already recorded and does not stack notes."""
    dupes = list(row.refs.get("dupes", []))
    if new_ts in dupes:
        return
    dupes.append(new_ts)
    rid = row.values.get("ID", "?")
    existing = row.values.get(store.notes_col, "")
    detail = f": {summary}" if summary else ", not logged as a new row."
    stamp = f"🔁 Also reported ({today_str()}){detail}"
    store.set(row.row_number, {store.notes_col: stamp + (f"\n{existing}" if existing else "")})
    store.set_refs(row.row_number, dupes=dupes)
    try:
        post(channel=channel, thread_ts=new_ts,
             text=f":twisted_rightwards_arrows: This looks like *#{rid}*, already tracked. "
                  f"Not adding a duplicate. (<{store.row_link(row.row_number)}|open row>)")
    except Exception as exc:
        log(f"WARN could not post duplicate note: {exc}")
    log(f"exact duplicate of #{rid}: annotated row {row.row_number}, not appended")


# Questions held pending a source/answer before they are logged. In-memory: the
# live path asks and holds; the backfill path logs directly (which doubles as the
# fallback after a restart). Keyed by the escalation message ts.
_pending: dict[str, dict] = {}


def _resolve_fields(channel: str, ts: str, user: str, text: str,
                    data: dict, forwarded: dict) -> dict:
    """Turn a raw classification into the resolved row fields (product, type,
    waiting-on, a tidy summary, the true platform, the inferred venue, etc.)."""
    poster_name = user_display_name(user)
    link = ""
    try:
        link = app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        pass
    # For a forwarded message, prefer the original author and a link to the source
    # message over the person who forwarded it and the in-channel permalink.
    fwd_author = (forwarded.get("author_name") or "").strip()
    fwd_url = (forwarded.get("from_url") or "").strip()
    new_link = data.get("link", "") or fwd_url or link
    return {
        "product": data.get("product", config.PRODUCT_DEFAULT) or config.PRODUCT_DEFAULT,
        "qtype": data.get("type", config.TYPE_DEFAULT) or config.TYPE_DEFAULT,
        "waiting_on": match_enum(data.get("waiting_on", ""), config.WAITING_ON,
                                 config.WAITING_ON_DEFAULT),
        "summary": clip_summary(data.get("question_summary", ""), config.SUMMARY_MAX_CHARS),
        "link": new_link,
        "channel_venue": infer_channel(data.get("source_channel", ""), forwarded),
        "platform": resolve_platform(data.get("platform", ""), new_link,
                                     data.get("source_channel", "")),
        "raised_by": data.get("raised_by", "") or fwd_author or poster_name,
        "owner": poster_name or config.OWNER_DEFAULT,
        "owner_uid": user,
        "priority": data.get("priority", ""),
        "text": text,
    }


def _log_resolved(channel: str, ts: str, user: str, r: dict, dup_of=None) -> None:
    """Append the row for a resolved question and post the forwarding note."""
    fields = {
        # ID is left to the sheet's own formula (set inside store.append).
        "Date Asked": today_str(),
        "Window": config.WINDOW_DEFAULT or current_window(),
        "Platform": r["platform"],
        "Channel": r["channel_venue"],
        "Question Summary": r["summary"],
        "Link": r["link"],
        "Raised By": r["raised_by"],
        "Owner": r["owner"],
        "Priority": r["priority"],
        "Status": config.STATUS_SENT,
        "Product": r["product"],
        "Type": r["qtype"],
        "Waiting On": r["waiting_on"],
        "Date Resolved": "",
        store.notes_col: "",
        "Slack Channel": channel,
        "Slack TS": ts,
        "Bot Refs": json.dumps({"owner_uid": r["owner_uid"]}),
    }
    row = store.append(fields)
    if not row:
        log("ERROR appended row not found after reload")
        return
    rid = row.values.get("ID", "?")
    row_link = store.row_link(row.row_number)
    try:
        dup_note = f" Possible duplicate of #{dup_of}." if dup_of else ""
        posted = post(
            channel=channel, thread_ts=ts,
            text=(f":inbox_tray: New question forwarded from devleads: #{rid} "
                  f"({r['product']} · {r['qtype']}).{dup_note}"),
            blocks=reports.escalation_note_blocks(rid, r["product"], r["qtype"], row_link,
                                                  value=ts, dup_of=dup_of,
                                                  summary=r["summary"], links=r["link"]),
        )
        store.set_refs(row.row_number, anchor_ts=posted["ts"])
    except Exception as exc:
        log(f"WARN could not post log note: {exc}")
    log(f"logged escalation #{rid} row {row.row_number} product={r['product']} type={r['qtype']}")


def _hold_for_info(channel: str, ts: str, user: str, r: dict, dup_of=None) -> None:
    """Do not open the row yet: ask in-thread for the missing source and hold the
    resolved fields until a source or reply arrives (or the timeout logs it)."""
    _pending[ts] = {"channel": channel, "user": user, "resolved": r, "dup_of": dup_of,
                    "created_at": datetime.now(UTC).isoformat()}
    try:
        post(channel=channel, thread_ts=ts,
             text=(":grey_question: Before I track this I need its source. Where did it come "
                   "from? Pick a venue below, or just reply here. (I will log it anyway if "
                   "nobody answers.)"),
             blocks=reports.set_source_prompt_blocks(ts))
    except Exception as exc:
        log(f"WARN could not post info request: {exc}")
    log(f"held question pending source/info for ts {ts}")


def finalize_pending(ts: str, source: str | None = None, reason: str = "answered") -> None:
    """Log a previously held question, optionally with a source the human supplied."""
    p = _pending.pop(ts, None)
    if not p:
        return
    r = dict(p["resolved"])
    if source:
        r["channel_venue"] = source
        r["platform"] = platform_from_source(source) or r["platform"]
    _log_resolved(p["channel"], ts, p["user"], r, dup_of=p.get("dup_of"))
    log(f"finalized pending ts {ts} ({reason})")


def _sweep_pending() -> None:
    """Log any held question older than the timeout, so none is lost to silence."""
    if not _pending:
        return
    cutoff = datetime.now(UTC) - timedelta(hours=config.PENDING_TIMEOUT_HOURS)
    stale = []
    # Snapshot: another Bolt thread may finalize (pop) a held item concurrently.
    for t, p in list(_pending.items()):
        try:
            if datetime.fromisoformat(p["created_at"]) < cutoff:
                stale.append(t)
        except Exception:
            stale.append(t)
    for t in stale:
        finalize_pending(t, reason="timeout")


def _run_pending_sweeper() -> None:
    """Background heartbeat so a held question is logged once its wait is up even
    if the channel stays silent (the lazy sweep only fires on inbound messages)."""
    interval = max(300, config.PENDING_TIMEOUT_HOURS * 3600 // 12)
    while True:
        time.sleep(interval)
        try:
            _sweep_pending()
        except Exception:
            log("ERROR in pending sweeper:\n" + traceback.format_exc())


def classify_and_log(channel: str, ts: str, user: str, text: str,
                     forwarded: dict | None = None, images: list[dict] | None = None,
                     allow_ask: bool = True) -> None:
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
    r = _resolve_fields(channel, ts, user, text, data, forwarded or {})

    # Duplicate / re-forward check against the open board. An exact key match (a
    # shared GitHub issue URL) annotates the existing row instead of opening a
    # second one; a similarity-only match still logs but flags the possible dup.
    match = find_duplicate(dedup_key(text, r["link"]), r["product"], r["summary"],
                           store.open_rows())
    if match and match.kind == "exact":
        annotate_duplicate(match.row, ts, channel, summary=r["summary"])
        return
    dup_of = match.row.values.get("ID") if match else None

    # If the source is unknown or the question is too thin, ask before logging
    # (live path only). Backfill logs directly, which is also the restart fallback.
    if allow_ask and config.ASK_WHEN_UNSOURCED and needs_more_info(r["waiting_on"], r["channel_venue"]):
        _hold_for_info(channel, ts, user, r, dup_of=dup_of)
        return
    _log_resolved(channel, ts, user, r, dup_of=dup_of)


# -- Feature 2: status transitions + resolution -----------------------------
def set_status(row: Row, channel: str, new_status: str, note: str = "") -> None:
    """Move a row to a new lifecycle status and announce it in its thread."""
    updates = {"Status": new_status}
    if new_status in (config.STATUS_ANSWERED, config.STATUS_CLOSED, "Solved"):
        updates["Date Resolved"] = today_str()
    store.set(row.row_number, updates)
    audit("status", row=row.row_number, id=row.values.get("ID"), status=new_status)
    try:
        post(channel=channel, thread_ts=row.original_ts,
             text=note or f":arrows_counterclockwise: *#{row.values.get('ID')}* now *{new_status}*.")
    except Exception:
        pass
    log(f"row {row.row_number} -> {new_status}")


def run_resolution_check(row: Row, channel: str, thread_ts: str) -> bool:
    """Judge whether the thread answers the question. If so, move it to Answered
    automatically (no confirmation step) and return True. Returns False otherwise."""
    if row.status not in config.OPEN_STATUSES:
        return False
    question = row.values.get("Question Summary", "")
    thread = thread_text(channel, thread_ts)
    if not thread:
        return False
    try:
        result = judge_resolution(question, thread)
    except Exception as exc:
        log(f"ERROR resolution judge failed: {exc}")
        return False
    data = result["input"]
    audit("resolution", row=row.row_number, id=row.values.get("ID"), slack_ts=thread_ts,
          verdict=data, tokens_in=result["tokens_in"], tokens_out=result["tokens_out"])
    if not data.get("resolved"):
        return False
    summary = data.get("resolution_summary", "")
    answered_by = data.get("answered_by", "")
    existing = row.values.get(store.notes_col, "")
    stamp = f"✅ Answered: {summary}" + (f" (by {answered_by})" if answered_by else "")
    store.set(row.row_number, {store.notes_col: stamp + (f"\n{existing}" if existing else "")})
    set_status(row, channel, config.STATUS_ANSWERED,
               note=f":white_check_mark: *#{row.values.get('ID')}* answered. {summary}".strip())
    return True


def close_row(row: Row, channel: str) -> None:
    store.set(row.row_number, {"Status": config.STATUS_CLOSED, "Date Resolved": today_str()})
    audit("close", row=row.row_number, id=row.values.get("ID"))
    try:
        post(channel=channel, thread_ts=row.original_ts,
             text=f":lock: Confirmed. *#{row.values.get('ID')}* closed.")
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
             text=":wastebasket: Discarded, removed from the tracker.")
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
    elif key in ("followups", "followup", "nudge", "nudges"):
        blocks = reports.followups_blocks(store, linker=permalink)
        kwargs = {"blocks": blocks} if blocks else {}
        post(channel=channel, thread_ts=reply_thread,
             text=reports.followups_report(store, linker=permalink), **kwargs)
    elif mentioned:
        post(channel=channel, thread_ts=reply_thread,
             text="Commands: `!status` · `!open` · `!aging` · `!followups` (or `@me status`). "
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

        # Log any held question whose wait has timed out (cheap, lazy sweep).
        _sweep_pending()

        if thread_ts and thread_ts != ts:
            # A reply on a held question counts as the answer: log it now.
            if thread_ts in _pending:
                finalize_pending(thread_ts, reason="reply")
                return
            row = store.find_by_ts(thread_ts)
            if row and not run_resolution_check(row, channel, thread_ts):
                # Not resolved, but a reply means work is happening: bump to In Progress.
                if row.status in config.PRE_PROGRESS_STATUSES:
                    set_status(row, channel, config.STATUS_IN_PROGRESS)
            return

        # A forwarded message has empty top-level text; its content lives in a
        # shared attachment. effective_text merges both so a plain forward tracks.
        eff = effective_text(event)
        # An image (direct or forwarded) counts as substance on its own, so an
        # image-only report is not dropped for lack of text.
        refs = image_refs(event)
        if not is_substantive(eff, config.MIN_MESSAGE_CHARS, has_image=bool(refs)):
            return
        if store.ts_tracked(ts) or ts in _pending:
            return
        classify_and_log(channel, ts, user, eff, forwarded=shared_attachment(event),
                         images=download_images(refs))
    except Exception:
        log("ERROR in on_message:\n" + traceback.format_exc())


@app.event("reaction_added")
def on_reaction(event, logger):
    """Reactions drive the forwarding workflow:
      :x: on the note/original -> discard (safety net, anyone).
      An admin (Domenico or the escalator) reacting with a workflow emoji moves the
      status: :arrow_right: Forwarded, :white_check_mark: Acknowledged,
      :heart: In Progress, :tada: Solved. Closing is the Mark-solved button."""
    try:
        item = event.get("item", {})
        ts = item.get("ts", "")
        channel = item.get("channel", "")
        reaction = event.get("reaction", "")
        reactor = event.get("user", "")
        row = store.find_by_ts(ts)
        if not row or row.status not in config.OPEN_STATUSES:
            return
        if reaction in config.DISCARD_REACTIONS:
            discard_row(row, channel)
            return
        new_status = config.EMOJI_STATUS.get(reaction)
        if new_status and is_admin(reactor, row.owner_uid, config.ADMIN_USER_IDS):
            if new_status != row.status:
                set_status(row, channel, new_status)
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


def nudge_row(row: Row) -> None:
    """One-tap follow-up: ping the item's owner in its own thread, reusing the
    owner-mention mechanism the resolution flow already uses."""
    owner_tag = f"<@{row.owner_uid}>" if row.owner_uid else row.values.get("Owner", "there")
    party = row.values.get("Waiting On", "") or "the team"
    rid = row.values.get("ID", "?")
    row_link = store.row_link(row.row_number)
    try:
        post(channel=row.slack_channel, thread_ts=row.original_ts,
             text=(f":bell: {owner_tag} follow-up nudge on *#{rid}* (still waiting on "
                   f"{party}). (<{row_link}|row>)"))
    except Exception as exc:
        log(f"WARN could not post nudge for row {row.row_number}: {exc}")
        return
    log(f"nudged owner on row {row.row_number} (waiting on {party})")


@app.action("row_nudge")
def act_nudge(ack, body):
    ack()
    try:
        value = (body.get("actions") or [{}])[0].get("value", "")
        row = store.find_by_ts(value)
        if row and row.status in config.OPEN_STATUSES:
            nudge_row(row)
    except Exception:
        log("ERROR in row nudge:\n" + traceback.format_exc())


@app.action("row_set_source")
def act_set_source(ack, body):
    """One-tap set-source: write the picked venue to the row's Channel column and
    confirm to the clicker only (ephemeral), so the flow is never blocked."""
    ack()
    try:
        selected = ((body.get("actions") or [{}])[0].get("selected_option") or {}).get("value", "")
        ts, _, venue = selected.partition("::")
        if not venue:
            return
        # A held (not-yet-logged) question: the picked venue is the source we were
        # waiting for, so log it now instead of updating a row that does not exist.
        if ts in _pending:
            finalize_pending(ts, source=venue, reason="source set")
            channel = body.get("channel", {}).get("id", "")
            uid = body.get("user", {}).get("id", "")
            if channel and uid:
                try:
                    app.client.chat_postEphemeral(
                        channel=channel, user=uid,
                        text=f":round_pushpin: Thanks, logged with source *{venue}*.")
                except Exception as exc:
                    log(f"WARN could not confirm set-source: {exc}")
            return
        row = store.find_by_ts(ts)
        if not row:
            return
        store.set(row.row_number, {"Channel": venue})
        log(f"set source for row {row.row_number} to {venue!r}")
        channel = body.get("channel", {}).get("id", "")
        uid = body.get("user", {}).get("id", "")
        if channel and uid:
            try:
                app.client.chat_postEphemeral(
                    channel=channel, user=uid,
                    text=f":round_pushpin: Source set to *{venue}* for *#{row.values.get('ID','?')}*.")
            except Exception as exc:
                log(f"WARN could not confirm set-source: {exc}")
    except Exception:
        log("ERROR in set source:\n" + traceback.format_exc())


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
    elif "followup" in text or "nudge" in text:
        blocks = reports.followups_blocks(store, linker=permalink)
        kwargs = {"blocks": blocks} if blocks else {}
        say(text=reports.followups_report(store, linker=permalink), **kwargs)
    else:
        say(reports.status_report(store))


# ---------------------------------------------------------------------------
# Startup backfill. Socket Mode does not replay missed events.
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
                    if store.ts_tracked(ts) or ts in _pending:
                        continue
                    eff = effective_text(m)
                    refs = image_refs(m)
                    if not is_substantive(eff, config.MIN_MESSAGE_CHARS, has_image=bool(refs)):
                        continue
                    scanned += 1
                    # Backfill logs directly (allow_ask=False): historical messages
                    # are logged rather than interrogated, which is also the fallback
                    # for questions held before a restart.
                    classify_and_log(channel, ts, m.get("user", ""), eff,
                                     forwarded=shared_attachment(m),
                                     images=download_images(refs), allow_ask=False)
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
        log(f"[slack] auth OK: team={who.get('team')} bot user_id={who.get('user_id')} "
            f"name='{who.get('user')}'")
    except Exception as exc:
        log(f"[slack] FAIL auth_test: {exc}")

    for ch in config.SLACK_CHANNEL_IDS:
        try:
            info = app.client.conversations_info(channel=ch)["channel"]
            log(f"[slack] channel {ch} name='{info.get('name')}' is_member={info.get('is_member')}")
            if not info.get("is_member"):
                log(f"[slack] WARN not a member of {ch}, run /invite in that channel")
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
        log(f"[sheet] OK: header row {s.header_row}, {len(s.rows)} data rows, "
            f"notes col='{s.notes_col}'")
        missing = [c for c in config.MANAGED_COLUMNS if c not in s.header]
        log(f"[sheet] managed columns present: {'yes' if not missing else 'MISSING ' + str(missing)}")
        log(f"[sheet] open/active rows now: {len(s.open_rows())}")
    except FileNotFoundError as exc:
        log(f"[sheet] FAIL service-account file: {exc}")
    except Exception as exc:
        log(f"[sheet] FAIL: often means the sheet is not shared with the service "
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
    threading.Thread(target=_run_pending_sweeper, daemon=True).start()
    log("starting Socket Mode…")
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
