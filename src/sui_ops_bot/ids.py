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


def is_substantive(text: str, min_chars: int) -> bool:
    """Cheap local pre-filter: keep only messages worth a Claude call."""
    if not text:
        return False
    stripped = re.sub(r"<[^>]+>", "", text).strip()             # drop mentions/links markup
    stripped = re.sub(r":[a-z0-9_+\-]+:", "", stripped).strip()  # drop emoji shortcodes
    if len(stripped) < min_chars:
        return False
    if stripped.lower() in _TRIVIAL:
        return False
    return True
