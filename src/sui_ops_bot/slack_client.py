"""The shared Slack surface: one Bolt app + the Web API helpers both entrypoints
use. The auto-tracker registers event handlers on ``app``; the MCP server only
uses ``client`` (the Web API). Building the app with ``token_verification_enabled
=False`` means importing this never hits ``auth.test``, so ``--check`` can report a
clean aggregated config error instead of crashing at import.
"""
from __future__ import annotations

from slack_bolt import App

from . import config
from .logutil import log

app = App(token=config.SLACK_BOT_TOKEN, token_verification_enabled=False)
client = app.client

_user_name_cache: dict[str, str] = {}


def post(**kwargs):
    """chat.postMessage with an optional identity override (BOT_USERNAME/icon), so a
    renamed app always posts under the intended name."""
    if config.BOT_USERNAME:
        kwargs.setdefault("username", config.BOT_USERNAME)
    if config.BOT_ICON_EMOJI:
        kwargs.setdefault("icon_emoji", config.BOT_ICON_EMOJI)
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


def permalink(channel: str, ts: str) -> str:
    try:
        return client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        return ""


def thread_text(channel: str, thread_ts: str, limit: int = 50) -> str:
    """Flatten a thread to 'name: text' lines, skipping bot messages."""
    try:
        resp = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
    except Exception as exc:
        log(f"WARN could not fetch thread {thread_ts}: {exc}")
        return ""
    parts = []
    for m in resp.get("messages", []):
        if m.get("bot_id"):
            continue
        who = user_display_name(m.get("user", ""))
        parts.append(f"{who}: {m.get('text','')}")
    return "\n".join(parts)


def thread_messages(channel: str, ts: str, limit: int = 60) -> list[dict]:
    """Return the thread as [{who, text, is_bot, ts}], oldest -> newest."""
    try:
        resp = client.conversations_replies(channel=channel, ts=ts, limit=limit)
    except Exception as exc:
        log(f"WARN could not fetch thread {ts}: {exc}")
        return []
    out = []
    for m in resp.get("messages", []):
        out.append({
            "who": "bot" if m.get("bot_id") else user_display_name(m.get("user", "")),
            "text": m.get("text", ""),
            "is_bot": bool(m.get("bot_id")),
            "ts": m.get("ts", ""),
        })
    return out


def has_check_reaction(channel: str, ts: str) -> bool:
    try:
        resp = client.reactions_get(channel=channel, timestamp=ts)
        reactions = (resp.get("message", {}) or {}).get("reactions", []) or []
        return any(r.get("name", "") in config.CHECK_REACTIONS for r in reactions)
    except Exception:
        return False
