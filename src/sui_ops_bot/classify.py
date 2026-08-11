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
            "question_summary": {"type": "string", "description": "A short one-line summary of the core ask, at most about 100 characters (roughly 12 to 15 words). Capture the single main question; do NOT enumerate every sub-point or paste the message."},
            "platform": {"type": "string", "description": "The ORIGINAL source medium the question started on: one of Telegram, Discord, GitHub, Sui Forum, X, Email, Other. This channel is on Slack, so Slack is only the transport for a forward, NEVER the origin: never answer 'Slack' for a forwarded message. Infer the true origin from the content and any links; leave empty if genuinely unclear."},
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
            "waiting_on": {
                "type": "string",
                "enum": config.WAITING_ON,
                "description": "Optional. Who this item is waiting on: 'organizer' for a program/logistics question, 'internal team' for a technical escalation that our team must resolve, 'reporter' when we need more information back from the person who raised it. Omit if unclear.",
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


def classify_system() -> str:
    """The classifier's system prompt: the domain model written as guidance.

    The channel mixes the leads' own internal coordination (skip) with forwarded
    external community questions (log). The include/exclude lists draw that line,
    the product guide keeps product tagging consistent, and a few short examples
    anchor the edge cases seen in real forwarded traffic."""
    return (
        "You triage a Slack channel used by Sui developer-relations leads. The channel mixes "
        "two things: the leads' own internal team coordination, and forwarded external "
        "community questions. Decide if a message is a NEW external developer question or "
        "issue that should be tracked across on-call shifts. Answers to existing threads, "
        "acknowledgements, status updates, and social chatter are NOT escalations. "
        "A message may include screenshots (an error, a stack trace, a console); read them "
        "as part of the report.\n"
        "\n"
        "SKIP (is_escalation false) the leads' own internal ops. Examples: invoices and "
        "payments, the on-call rota and shift handoffs, drive folders and where to put files "
        "(\"where do we put the .md\"), \"upload your reports\", scheduling and logistics "
        "internal to the team, and general chatter.\n"
        "\n"
        "LOG (is_escalation true) an external community question or issue. This includes "
        "community PROGRAM and logistics questions, which are Product 'Program' and Type "
        "'Communication': Overflow participant certifications, submission deadlines, track "
        "changes, forms, and eligibility. A logistics question from a community member is a "
        "real escalation even though it is not technical; do not confuse it with the leads' "
        "own internal scheduling.\n"
        "\n"
        "PRODUCT DISAMBIGUATION (pick the most specific match):\n"
        "- RPC, fullnode, node, or network reachability -> Sui Core\n"
        "- wallet or transaction signing -> Slush\n"
        "- TypeScript SDK or dapp-kit -> SDK\n"
        "- blob or quilt storage -> Walrus\n"
        "- TEE or confidential compute -> Nautilus\n"
        "Use 'Program' for non-product program/logistics questions, and 'Other' only when no "
        "specific product fits.\n"
        "\n"
        "WAITING ON (who we are waiting on, optional): 'organizer' for a program or logistics "
        "question that a program organizer answers; 'internal team' for a technical escalation "
        "our own team must resolve; 'reporter' when we need more information back from the "
        "person who raised it. Omit it if unclear.\n"
        "\n"
        "EXAMPLES:\n"
        "- \"Please upload your weekly reports to the drive before Friday.\" -> is_escalation "
        "false (internal ops).\n"
        "- \"Who is covering the Americas on-call shift this week?\" -> is_escalation false "
        "(internal rota).\n"
        "- \"A participant is asking when the Overflow certifications will be sent out.\" -> "
        "is_escalation true, Product Program, Type Communication.\n"
        "- \"Builder on Discord gets a 500 from the mainnet RPC calling sui_getObject.\" -> "
        "is_escalation true, Product Sui Core, Type Bug.\n"
        "- \"How do I sign a transaction with the Slush wallet from dapp-kit?\" -> "
        "is_escalation true, Product Slush, Type Question.\n"
        "\n"
        "Keep 'question_summary' to one short line (about 100 characters), the single core "
        "ask, not a list of every sub-point. "
        "'platform' is the ORIGINAL source medium (Telegram/Discord/GitHub/Sui Forum/X/Email/"
        "Other). This channel runs on Slack, so Slack is only the transport of a forward, never "
        "the origin: never answer 'Slack' for a forwarded message, infer the real origin from "
        "the content and links, and leave it empty if unclear. "
        "'source_channel' is the specific venue name. Classify 'product' (the ecosystem "
        f"product/area, one of {config.PRODUCTS}) and 'type' (the kind of ask, one of "
        f"{config.TYPES}). Constrain priority to {config.PRIORITIES}."
    )


def classify_message(text: str, images: list[dict] | None = None) -> dict:
    """Classify a Slack message, optionally with attached screenshots.

    ``images`` is a list of ``{"data": bytes, "mime": str}``. When present and
    vision is enabled, each is passed to the model as an image block alongside the
    text, so a screenshot of an error or stack trace gets classified. If the vision
    call fails (for example a non-vision model is configured), it falls back to a
    text-only classification. Image bytes are never logged."""
    system = classify_system()
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
