"""Claude-backed classification and resolution judgement, via forced tool use.

Two calls, both returning validated JSON (the model is forced to call a single
tool, so the output always matches the schema):

* :func:`classify_message` decides whether a Slack message is a new developer
  escalation and, if so, extracts the structured fields including the two
  taxonomy axes ``product`` and ``type``.
* :func:`judge_resolution` decides whether a tracked question has been answered
  by its thread.
"""
from __future__ import annotations

import base64

from anthropic import Anthropic

from . import config
from .logutil import log

_client: Anthropic | None = None


def client() -> Anthropic:
    """Lazily build the Anthropic client so importing this module never requires a
    key (tests and ``--check`` import it without calling the API)."""
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


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
            "priority": {"type": "string", "enum": config.PRIORITIES},
            "product": {
                "type": "string",
                "enum": config.PRODUCTS,
                "description": "The Sui ecosystem product/area the question is about. Use 'Program' for non-product program/logistics questions (deadlines, track changes), 'Other' when no specific product fits.",
            },
            "type": {
                "type": "string",
                "enum": config.TYPES,
                "description": "The kind of ask: Question (technical/how-to), Open PR (a pull request needs review), Bug (bug/incident/outage/funds-stuck), Feature Request (enhancement ask), Communication (feedback, docs feedback, positioning).",
            },
        },
        "required": ["is_escalation", "question_summary", "platform", "source_channel",
                     "link", "raised_by", "priority", "product", "type"],
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


def image_block(data: bytes, mime: str) -> dict:
    """A single Anthropic image content block from raw bytes and a mime type."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def assemble_user_content(text: str, images: list[dict] | None):
    """Build the user-message content for the classifier.

    With no images this returns the plain ``text`` string, exactly as before. With
    images it returns a list of one image block per image (``{data, mime}``)
    followed by a single text block when ``text`` is non-empty."""
    if not images:
        return text
    blocks = [image_block(img["data"], img["mime"]) for img in images]
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def _tool_call(tool: dict, system: str, content) -> dict:
    resp = client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=512,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}],
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


def classify_message(text: str, images: list[dict] | None = None) -> dict:
    """Classify a Slack message, optionally with attached screenshots.

    ``images`` is a list of ``{"data": bytes, "mime": str}``. When present and
    vision is enabled, each is passed to the model as an image block alongside the
    text, so a screenshot of an error or stack trace gets classified. If the vision
    call fails (for example a non-vision model is configured), it falls back to a
    text-only classification. Image bytes are never logged."""
    system = (
        "You triage a Slack channel used by Sui developer-relations leads to escalate "
        "developer questions that stay open across on-call shifts. Decide if a message is a "
        "NEW developer-question escalation that should be tracked. Chatter, acknowledgements, "
        "answers to existing threads, status updates, and social messages are NOT escalations. "
        "A message may include screenshots (an error, a stack trace, a console); read them as "
        "part of the report. "
        "'platform' is the source medium (Telegram/Discord/GitHub/Sui Forum/X/Slack/Email/Other); "
        "'source_channel' is the specific venue name. Classify 'product' (the ecosystem "
        f"product/area, one of {config.PRODUCTS}) and 'type' (the kind of ask, one of "
        f"{config.TYPES}). Constrain priority to {config.PRIORITIES}."
    )
    user_text = f"Slack message:\n\n{text}"
    imgs = images if (images and config.CLASSIFY_VISION) else None
    if imgs:
        try:
            return _tool_call(CLASSIFY_TOOL, system, assemble_user_content(user_text, imgs))
        except Exception as exc:
            # Log the error type only, never the exception body: it could echo the
            # request, which contains the base64 image.
            log(f"WARN vision classify failed ({type(exc).__name__}), retrying text-only")
    return _tool_call(CLASSIFY_TOOL, system, assemble_user_content(user_text, None))


def judge_resolution(question: str, thread_text: str) -> dict:
    system = (
        "You decide whether a tracked developer question has been resolved by a Slack "
        "thread. Only say resolved=true if the thread contains a concrete answer or fix "
        "for the ORIGINAL question. A follow-up question, a 'looking into it', or an "
        "unrelated reply is NOT a resolution."
    )
    user = f"Original question:\n{question}\n\nThread so far:\n{thread_text}"
    return _tool_call(RESOLUTION_TOOL, system, user)
