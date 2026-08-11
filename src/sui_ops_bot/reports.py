"""Report builders for the open backlog.

The pure core (:func:`age_days`, :func:`filter_rows`, :func:`group_by_product`)
is deterministic and unit-tested against fake rows. The formatters take a store
and a ``linker`` (so tests can inject one instead of calling Slack) and turn the
core output into the Slack-markdown strings the auto-tracker commands and the MCP
``weekly_report`` both emit.
"""
from __future__ import annotations

from datetime import date, datetime

from . import config
from .logutil import tz


def age_days(date_asked: str, today: date | None = None) -> int | None:
    """Whole days since an ISO 'Date Asked' string, or None if unparseable."""
    today = today or datetime.now(tz()).date()
    try:
        d = datetime.fromisoformat(str(date_asked)).date()
        return (today - d).days
    except Exception:
        return None


def filter_rows(rows, product: str = "", type: str = ""):
    """Case-insensitively filter rows by product and/or type. Empty = no filter."""
    p = (product or "").strip().lower()
    t = (type or "").strip().lower()
    out = []
    for r in rows:
        if p and (r.values.get("Product", "") or "").strip().lower() != p:
            continue
        if t and (r.values.get("Type", "") or "").strip().lower() != t:
            continue
        out.append(r)
    return out


def group_by_product(rows) -> dict[str, list]:
    """Group rows by their Product value (blank -> 'Unclassified'), returned in the
    canonical taxonomy order, then any extras, then Unclassified last."""
    buckets: dict[str, list] = {}
    for r in rows:
        key = (r.values.get("Product", "") or "").strip() or "Unclassified"
        buckets.setdefault(key, []).append(r)
    ordered: dict[str, list] = {}
    for p in config.PRODUCTS:
        if p in buckets:
            ordered[p] = buckets.pop(p)
    for extra in sorted(k for k in buckets if k != "Unclassified"):
        ordered[extra] = buckets.pop(extra)
    if "Unclassified" in buckets:
        ordered["Unclassified"] = buckets.pop("Unclassified")
    return ordered


def group_followups(rows, days: int, today: date | None = None) -> dict[str, list]:
    """Open rows that name a Waiting On party and have gone unanswered for more
    than ``days``, grouped by party. Parties come back in the canonical order
    (internal team, organizer, reporter), each party's items oldest first. Rows
    with no party or not yet stale are dropped."""
    buckets: dict[str, list] = {}
    for r in rows:
        party = (r.values.get("Waiting On", "") or "").strip()
        if not party:
            continue
        age = age_days(r.values.get("Date Asked", ""), today)
        if age is None or age <= days:
            continue
        buckets.setdefault(party, []).append(r)
    for items in buckets.values():
        items.sort(key=lambda r: -(age_days(r.values.get("Date Asked", ""), today) or 0))
    ordered: dict[str, list] = {}
    for p in config.WAITING_ON:
        key = next((k for k in buckets if k.lower() == p.lower()), None)
        if key:
            ordered[key] = buckets.pop(key)
    for extra in sorted(buckets):
        ordered[extra] = buckets.pop(extra)
    return ordered


def followups_report(store, days: int | None = None, linker=None,
                     today: date | None = None) -> str:
    """Text digest of stale follow-ups grouped by the party we are waiting on."""
    days = config.AGING_DAYS if days is None else days
    store.reload()
    grouped = group_followups(store.open_rows(), days, today=today)
    if not grouped:
        return f":white_check_mark: No follow-ups waiting over {days} days."
    lines = [f":bell: *Follow-ups waiting over {days} days*"]
    for party, rows in grouped.items():
        lines.append(f"\n*Waiting on {party}* ({len(rows)})")
        for r in rows:
            age = age_days(r.values.get("Date Asked", ""), today)
            agestr = f"{age}d" if age is not None else "?"
            lines.append(f"• *#{r.values.get('ID','?')}* [{_badge(r)}, {agestr}] "
                         f"{r.values.get('Question Summary','')} <{_link(store, r, linker)}|↗>")
    return "\n".join(lines)


# How many items the interactive digest renders before truncating, to stay under
# Slack's 50-block message limit (each item is one section block with a button).
_FOLLOWUPS_BLOCK_CAP = 40


def followups_blocks(store, days: int | None = None, linker=None,
                     today: date | None = None) -> list | None:
    """Block Kit digest: one section per stale item with a one-tap Nudge button
    (its value is the item's message ts, so the action handler can ping the owner
    in the original thread). Returns None when nothing is stale."""
    days = config.AGING_DAYS if days is None else days
    store.reload()
    grouped = group_followups(store.open_rows(), days, today=today)
    if not grouped:
        return None
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
        "text": f":bell: *Follow-ups waiting over {days} days*"}}]
    shown, total = 0, sum(len(v) for v in grouped.values())
    for party, rows in grouped.items():
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"*Waiting on {party}* ({len(rows)})"}]})
        for r in rows:
            if shown >= _FOLLOWUPS_BLOCK_CAP:
                break
            age = age_days(r.values.get("Date Asked", ""), today)
            agestr = f"{age}d" if age is not None else "?"
            blocks.append({"type": "section",
                "text": {"type": "mrkdwn",
                    "text": f"*#{r.values.get('ID','?')}* [{_badge(r)}, {agestr}] "
                            f"{r.values.get('Question Summary','')} "
                            f"(<{_link(store, r, linker)}|open>)"},
                "accessory": {"type": "button", "action_id": "row_nudge",
                    "text": {"type": "plain_text", "text": ":bell: Nudge"},
                    "value": r.original_ts or str(r.row_number)}})
            shown += 1
    if shown < total:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"…and {total - shown} more not shown."}]})
    return blocks


def _badge(row) -> str:
    """'Product · Type' badge for a row, tolerating blanks."""
    parts = [p for p in (row.values.get("Product", ""), row.values.get("Type", "")) if p]
    return " · ".join(parts) if parts else "Unclassified"


def _link(store, row, linker) -> str:
    link = row.values.get("Link", "")
    if not link and row.original_ts and row.slack_channel and linker:
        link = linker(row.slack_channel, row.original_ts)
    return link or store.row_link(row.row_number)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def escalation_note_blocks(rid, product: str, qtype: str, row_link: str, value: str,
                           dup_of=None) -> list:
    """Block Kit for the bot's in-thread log note: the summary line plus one-tap
    Discard and Mark-solved buttons, so a lead never has to hunt for a reaction.
    `value` is the escalation message ts, used by the action handler to find the row.
    When `dup_of` is set, a context line flags this as a possible duplicate of that
    row so a lead can merge it by hand."""
    badge = " · ".join(p for p in (product, qtype) if p) or "Unclassified"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f":pushpin: Logged as *#{rid}*  ·  {badge}  (<{row_link}|open row>)"}},
    ]
    if dup_of:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f":twisted_rightwards_arrows: Possible duplicate of *#{dup_of}* "
                    f"(merge by hand if so)."}]})
    blocks.append(
        {"type": "actions", "block_id": f"esc-{rid}", "elements": [
            {"type": "button", "action_id": "row_discard", "style": "danger",
             "text": {"type": "plain_text", "text": ":wastebasket: Discard"}, "value": value,
             "confirm": {
                 "title": {"type": "plain_text", "text": "Discard this?"},
                 "text": {"type": "mrkdwn", "text": f"Remove *#{rid}* from the tracker."},
                 "confirm": {"type": "plain_text", "text": "Discard"},
                 "deny": {"type": "plain_text", "text": "Keep"}}},
            {"type": "button", "action_id": "row_solved", "style": "primary",
             "text": {"type": "plain_text", "text": ":white_check_mark: Mark solved"},
             "value": value},
        ]})
    # Optional one-tap set-source menu, in its own actions block so it never
    # touches the discard/solved flow. Each option carries the row ts so the
    # handler can find the row. Purely optional: picking nothing changes nothing.
    blocks.append({"type": "actions", "block_id": f"src-{rid}", "elements": [
        {"type": "static_select", "action_id": "row_set_source",
         "placeholder": {"type": "plain_text", "text": "Set source…"},
         "options": [
             {"text": {"type": "plain_text", "text": v}, "value": f"{value}::{v}"}
             for v in config.SOURCE_VENUES
         ]},
    ]})
    return blocks


def status_report(store) -> str:
    store.reload()
    rows = store.open_rows()
    if not rows:
        return ":white_check_mark: No open items."
    by_product = group_by_product(rows)
    aging, oldest, oldest_age = 0, None, -1
    for r in rows:
        age = age_days(r.values.get("Date Asked", ""))
        if age is not None:
            if age > config.AGING_DAYS:
                aging += 1
            if age > oldest_age:
                oldest_age, oldest = age, r
    lines = [f"*Open items:* {len(rows)}"]
    lines.append("*By product:* " + ", ".join(f"{p}: {len(rs)}" for p, rs in by_product.items()))
    lines.append(f"*Aging (>{config.AGING_DAYS}d, still open):* {aging}")
    if oldest is not None:
        link = oldest.values.get("Link", "") or store.row_link(oldest.row_number)
        lines.append(f"*Oldest:* {oldest.values.get('ID')} ({oldest_age}d): "
                     f"{oldest.values.get('Question Summary','')} <{link}|↗>")
    return "\n".join(lines)


def open_report(store, linker=None) -> str:
    store.reload()
    rows = sorted(store.open_rows(), key=lambda r: r.values.get("Date Asked", ""))
    if not rows:
        return ":white_check_mark: No open items."
    lines = [f"*{len(rows)} open items:*"]
    for r in rows[:30]:
        age = age_days(r.values.get("Date Asked", ""))
        agestr = f"{age}d" if age is not None else "?"
        lines.append(f"• *{r.values.get('ID')}* [{_badge(r)} · {r.status}, {agestr}] "
                     f"{r.values.get('Question Summary','')} <{_link(store, r, linker)}|↗>")
    if len(rows) > 30:
        lines.append(f"…and {len(rows) - 30} more.")
    return "\n".join(lines)


def aging_report(store, linker=None) -> str:
    store.reload()
    rows = []
    for r in store.open_rows():
        age = age_days(r.values.get("Date Asked", ""))
        if age is not None and age > config.AGING_DAYS:
            rows.append((age, r))
    rows.sort(key=lambda t: -t[0])
    if not rows:
        return f":white_check_mark: Nothing aging over {config.AGING_DAYS} days."
    lines = [f"*{len(rows)} items aging over {config.AGING_DAYS} days:*"]
    for age, r in rows[:30]:
        lines.append(f"• *{r.values.get('ID')}* ({age}d) [{_badge(r)}] "
                     f"{r.values.get('Question Summary','')} <{_link(store, r, linker)}|↗>")
    return "\n".join(lines)


def weekly_report(store, days: int = 7, product: str = "", type: str = "", linker=None) -> str:
    """The open backlog grouped by product, with optional product/type filters.
    Items older than ``days`` are flagged."""
    store.reload()
    rows = filter_rows(store.open_rows(), product=product, type=type)
    scope = ""
    if product or type:
        scope = " (" + ", ".join(x for x in (product, type) if x) + ")"
    if not rows:
        return f":white_check_mark: No open questions{scope}. The tracker is clear."
    grouped = group_by_product(rows)
    lines = [f"*:memo: Open questions report*{scope}: {len(rows)} unanswered"]
    for prod, prod_rows in grouped.items():
        prod_rows = sorted(prod_rows, key=lambda r: r.values.get("Date Asked", ""))
        lines.append(f"\n*{prod}* ({len(prod_rows)})")
        for r in prod_rows:
            rid = r.values.get("ID", "?")
            age = age_days(r.values.get("Date Asked", ""))
            agestr = f"{age}d" if age is not None else "?"
            flag = " :hourglass_flowing_sand:" if (age is not None and age > days) else ""
            typ = r.values.get("Type", "") or "-"
            raised = r.values.get("Raised By", "")
            raised_str = f" · raised by {raised}" if raised else ""
            lines.append(f"• *#{rid}* [{typ} · {r.status}, {agestr}{flag}] "
                         f"{r.values.get('Question Summary','')}{raised_str} "
                         f"<{_link(store, r, linker)}|↗>")
    return "\n".join(lines)
