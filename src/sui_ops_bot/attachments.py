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

import urllib.parse
import urllib.request
from dataclasses import dataclass

# The image formats a vision-capable Claude model accepts. An image in any other
# format (svg, bmp, heic) cannot be classified, so it is ignored at parse time
# rather than wasting a vision call that the API would reject.
SUPPORTED_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Mime inferred from a URL extension when Slack gives a bare image_url with no
# explicit mimetype. Anything unrecognized falls back to PNG, the common case.
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Cap a single download. Matches the Anthropic per-image limit, so a larger file
# could not be classified anyway, and bounds memory on the live event handler.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header if a redirect points at a different host, so
    the Slack bot token is never sent anywhere but the host we asked for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if old_host != new_host:
                new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_StripAuthOnCrossHostRedirect)


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
        mime = (f.get("mimetype") or "").strip().lower()
        url = (f.get("url_private") or f.get("url_private_download") or "").strip()
        if mime in SUPPORTED_MIMES and url:
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


def download_image(ref: ImageRef, token: str, *, timeout: float = 10.0) -> bytes:
    """Fetch the bytes behind a Slack ``url_private`` link with the bot token.

    Thin on purpose: the caller decides how to handle failure. The read is capped
    at :data:`MAX_IMAGE_BYTES` (a larger file could not be classified anyway) and
    a cross-host redirect drops the token. The returned bytes are the raw image
    and must never be logged or written to the audit trail.
    """
    req = urllib.request.Request(ref.url, headers={"Authorization": f"Bearer {token}"})
    with _opener.open(req, timeout=timeout) as resp:
        data = resp.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    return data
