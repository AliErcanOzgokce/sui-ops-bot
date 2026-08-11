"""Duplicate / re-forward detection, pure and testable.

The same underlying issue often arrives twice: someone types it, then forwards
the original message, or two people report the same GitHub issue. This module
gives the auto-log path a way to notice that before it opens a second row.

* :func:`dedup_key` derives a strong key from a message: a canonical GitHub
  issue or pull URL when one is present, else ``None``.
* :func:`text_similarity` is a cheap token overlap in ``[0, 1]``.
* :func:`find_duplicate` looks through the currently open rows for an exact key
  match first, then a same-product near match above a similarity threshold.

No I/O: the caller passes in the open rows (each a ``.values`` dict), so the whole
module is unit-tested without Slack or Sheets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config

# Match the identifying core of a GitHub issue/PR URL, ignoring scheme, www, a
# trailing slug, query, or fragment. Owner/repo are case-insensitive on GitHub,
# so the key lowercases them and two spellings of the same URL collapse to one.
_GITHUB_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)", re.IGNORECASE)

_WORD = re.compile(r"[a-z0-9]+")

# Default similarity threshold for a near-duplicate on the same product. Tuned so
# a typed message and its later forward (which adds a "Forwarded from X" prefix)
# still collapse, while genuinely different questions do not.
SIMILARITY_THRESHOLD = 0.6


def dedup_key(text: str, link: str = "") -> str | None:
    """A canonical GitHub issue/PR key from a message, or ``None`` if it has none.

    Looks at the explicit link first, then any URL embedded in the text."""
    for source in (link, text):
        m = _GITHUB_URL.search(source or "")
        if m:
            owner, repo, kind, num = m.groups()
            return f"github.com/{owner.lower()}/{repo.lower()}/{kind.lower()}/{num}"
    return None


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall((s or "").lower()))


def text_similarity(a: str, b: str) -> float:
    """Jaccard overlap of word tokens, in ``[0, 1]``. Order-insensitive, so a
    forwarded copy with an added prefix still scores high against the original."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True)
class DupMatch:
    """A detected duplicate: ``kind`` is ``"exact"`` (key match) or ``"similar"``
    (same product, similar text). ``row`` is the matched open row, ``score`` is
    1.0 for an exact match and the similarity for a near match."""

    kind: str
    row: object
    score: float


def _is_open(row) -> bool:
    return (row.values.get("Status", "") or "") in config.OPEN_STATUSES


def find_duplicate(key: str | None, product: str, text: str, rows,
                   *, threshold: float = SIMILARITY_THRESHOLD) -> DupMatch | None:
    """Find an already-open row that this new item duplicates, or ``None``.

    An exact key match wins regardless of product (a GitHub URL is a strong
    signal). Otherwise the best same-product near match above ``threshold`` is
    returned. Only open rows are considered."""
    open_rows = [r for r in rows if _is_open(r)]

    if key:
        for r in open_rows:
            r_key = dedup_key(r.values.get("Question Summary", ""), r.values.get("Link", ""))
            if r_key and r_key == key:
                return DupMatch("exact", r, 1.0)

    p = (product or "").strip().lower()
    best: DupMatch | None = None
    for r in open_rows:
        if (r.values.get("Product", "") or "").strip().lower() != p:
            continue
        score = text_similarity(text, r.values.get("Question Summary", ""))
        if score >= threshold and (best is None or score > best.score):
            best = DupMatch("similar", r, score)
    return best
