"""Pure parsing / normalization helpers.

No I/O, no imports beyond the stdlib and config: everything here is deterministic
and unit-tested. Keeping it separate is what lets the test suite avoid touching
Slack, Sheets, or Anthropic.
"""
from __future__ import annotations

import re

# Words a top-level message can be and still not be worth an LLM call.
_TRIVIAL = {"thanks", "thank you", "ty", "done", "ok", "okay", "+1", "lgtm"}


def norm_id(s: str) -> str:
    """Normalize an ID token so '#12', 'Q-12', ' 12 ' and '12' all compare equal.
    Falls back to the lowercased stripped string for non-numeric IDs."""
    t = str(s).strip().lstrip("#").strip()
    for pre in ("Q-", "q-", "Q", "q"):
        if t.startswith(pre) and t[len(pre):].strip().isdigit():
            t = t[len(pre):].strip()
            break
    return (t.lstrip("0") or t) if t.isdigit() else t.lower()


def parse_ids(ids) -> list[str]:
    """Accept a list, or a comma/space/newline-separated string, of IDs."""
    if isinstance(ids, (list, tuple)):
        raw = ids
    else:
        raw = re.split(r"[,\s]+", str(ids))
    return [x.strip() for x in raw if str(x).strip()]


def match_enum(value: str, choices: list[str], default: str) -> str:
    """Case-insensitively match ``value`` to one of ``choices``; return ``default``
    if there is no match. Used to constrain LLM / human input to a taxonomy."""
    v = (value or "").strip().lower()
    if not v:
        return default
    for c in choices:
        if c.lower() == v:
            return c
    return default


# Placeholder values a classifier may emit for an unknown channel/venue. These
# should land as an empty cell, not literal text, to match the sheet's old format.
_CHANNEL_PLACEHOLDERS = {
    "", "<unknown>", "unknown", "n/a", "na", "none", "<none>", "-", "?",
    "<unclear>", "unclear", "unspecified", "null",
}


def clean_channel(value: str) -> str:
    """Blank out placeholder channel/venue strings so the Channel column stays clean."""
    v = (value or "").strip()
    return "" if v.lower() in _CHANNEL_PLACEHOLDERS else v


def infer_channel(source_channel: str, forwarded: dict | None = None) -> str:
    """The venue to write to the Channel column, without ever interrogating.

    Content-inferred venue first (cleaned of placeholders). When content yields
    nothing, fall back to the forward's own original Slack channel name (from the
    shared attachment's ``channel_name``), normalized to ``#name``. When both are
    empty the venue is genuinely unknown, so this returns a blank; the auto-tracker
    leaves the cell empty rather than asking."""
    venue = clean_channel(source_channel)
    if venue:
        return venue
    name = ((forwarded or {}).get("channel_name") or "").strip().lstrip("#").strip()
    return f"#{name}" if name else ""


def platform_from_source(source: str) -> str:
    """Infer the source medium (Telegram/GitHub/...) from a link or venue string."""
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


def is_admin(user_id: str, owner_uid: str, admin_ids) -> bool:
    """Who may drive a question's status with a reaction: a configured admin (e.g.
    Domenico) or the person who escalated that question (its owner)."""
    if not user_id:
        return False
    return user_id == owner_uid or user_id in (admin_ids or [])


def needs_more_info(waiting_on: str, channel: str) -> bool:
    """True when a new escalation is too thin to track cleanly: the source venue is
    unknown, or the classifier judged the reporter still owes more information. The
    auto-tracker uses this to ask before logging instead of opening a blank row."""
    if not (channel or "").strip():
        return True
    return (waiting_on or "").strip().lower() == "reporter"


def resolve_platform(llm_platform: str, link: str = "", source_channel: str = "") -> str:
    """The true source medium, not the Slack transport.

    Every tracked item reaches the channel through Slack, so the classifier often
    labels a forwarded message 'Slack' even though it started on Telegram, Discord,
    or GitHub. Prefer a concrete origin inferred from a real (non-Slack) link or
    venue string; otherwise trust the model's origin, but drop a bare 'Slack'
    verdict (the transport) and leave the platform blank instead of mislabeling."""
    for src in (link, source_channel):
        p = platform_from_source(src)
        if p and p != "Slack":
            return p
    p = (llm_platform or "").strip()
    return "" if p.lower() == "slack" else p


def clip_summary(text: str, limit: int) -> str:
    """A tidy one-line summary: collapse whitespace, and if longer than ``limit``
    trim at a word boundary with an ellipsis so the sheet stays readable."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(",;:.")
    return (cut or t[:limit]).rstrip() + "…"


def shared_attachment(event: dict) -> dict:
    """Return the first forwarded/shared message attachment on a Slack event, or {}.

    When a Slack message is forwarded, its content is not in the top-level `text`;
    it arrives as an attachment flagged `is_share` with the original `text`,
    `author_name`, and a `from_url` back to the source message."""
    for a in event.get("attachments") or []:
        if a.get("is_share") and (a.get("text") or a.get("fallback")):
            return a
    return {}


def effective_text(event: dict) -> str:
    """The text a classifier should judge: the message's own text plus any forwarded
    content (with the original author noted so `raised_by` can be inferred). This is
    what lets a plain forward, whose top-level text is empty, still be tracked."""
    parts = []
    own = (event.get("text") or "").strip()
    if own:
        parts.append(own)
    att = shared_attachment(event)
    if att:
        atext = (att.get("text") or "").strip()
        author = (att.get("author_name") or "").strip()
        if atext:
            parts.append(f"Forwarded from {author}: {atext}" if author else atext)
    return "\n".join(parts)


def is_substantive(text: str, min_chars: int, has_image: bool = False) -> bool:
    """Cheap local pre-filter: keep only messages worth a Claude call.

    An image carries its own substance (a screenshot of an error or stack trace),
    so a message with one is kept even when its text is short or empty. Without an
    image, the text-length and trivial-ack checks apply as before."""
    if has_image:
        return True
    if not text:
        return False
    stripped = re.sub(r"<[^>]+>", "", text).strip()             # drop mentions/links markup
    stripped = re.sub(r":[a-z0-9_+\-]+:", "", stripped).strip()  # drop emoji shortcodes
    if len(stripped) < min_chars:
        return False
    if stripped.lower() in _TRIVIAL:
        return False
    return True
