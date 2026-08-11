"""Image references on a Slack message, plus an authenticated fetch of one.

Two halves, split so the tricky part stays pure and testable:

* :func:`image_refs` is pure parsing. Given a Slack message or event dict, it
  returns the image references (private URL plus mime type) found on the
  message's own ``files`` and on a forwarded ``is_share`` attachment (either its
  own ``files`` or an ``image_url`` preview). Non-image files are ignored.
* :func:`download_image` is a thin authenticated GET: Slack's ``url_private``
  links need the bot token as a bearer header. The bytes it returns are never
  logged.
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass

# Mime inferred from a URL extension when Slack gives a bare image_url with no
# explicit mimetype. Anything unrecognized falls back to PNG, the common case.
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


@dataclass(frozen=True)
class ImageRef:
    """A downloadable image on a message: its private URL and its mime type."""

    url: str
    mime: str


def _mime_from_url(url: str) -> str:
    tail = (url or "").rsplit(".", 1)
    ext = tail[1].split("?", 1)[0].lower() if len(tail) == 2 else ""
    return _EXT_MIME.get(ext, "image/png")


def _refs_from_files(files) -> list[ImageRef]:
    out = []
    for f in files or []:
        mime = (f.get("mimetype") or "").strip()
        url = (f.get("url_private") or f.get("url_private_download") or "").strip()
        if mime.startswith("image/") and url:
            out.append(ImageRef(url=url, mime=mime))
    return out


def image_refs(msg: dict) -> list[ImageRef]:
    """All image references on ``msg``, from its own files and a forwarded share.

    Order is: directly attached images first, then any images carried by the
    forwarded ``is_share`` attachment (its own files, then an ``image_url``
    preview). Non-image files, non-share attachments, and files with no URL are
    ignored, so a message with nothing to read returns an empty list.
    """
    refs = _refs_from_files(msg.get("files"))
    for a in msg.get("attachments") or []:
        if not a.get("is_share"):
            continue
        refs.extend(_refs_from_files(a.get("files")))
        image_url = (a.get("image_url") or "").strip()
        if image_url:
            refs.append(ImageRef(url=image_url, mime=_mime_from_url(image_url)))
    return refs


def download_image(ref: ImageRef, token: str, *, timeout: float = 15.0) -> bytes:
    """Fetch the bytes behind a Slack ``url_private`` link with the bot token.

    Thin on purpose: the caller decides how to handle failure. The returned bytes
    are the raw image and must never be logged or written to the audit trail.
    """
    req = urllib.request.Request(ref.url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
