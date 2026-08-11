"""Pure tests for image-reference parsing (no network).

These mirror the real Slack shapes: a directly pasted image arrives in the
message's own `files` list; a forwarded image arrives on the `is_share`
attachment, either as its own `files` list or as an `image_url` preview. The
download half is exercised with a monkeypatched opener so no bytes cross the wire.
"""
import pytest

from sui_ops_bot.attachments import (
    MAX_IMAGE_BYTES,
    ImageRef,
    download_image,
    image_refs,
)


class TestImageRefs:
    def test_direct_pasted_image_is_found(self):
        msg = {
            "text": "check this error",
            "files": [
                {"mimetype": "image/png",
                 "url_private": "https://files.slack.com/files-pri/T1-F1/shot.png"},
            ],
        }
        refs = image_refs(msg)
        assert refs == [ImageRef(
            url="https://files.slack.com/files-pri/T1-F1/shot.png", mime="image/png")]

    def test_non_image_files_ignored(self):
        msg = {"files": [
            {"mimetype": "application/pdf", "url_private": "https://x/doc.pdf"},
            {"mimetype": "text/plain", "url_private": "https://x/log.txt"},
        ]}
        assert image_refs(msg) == []

    def test_forwarded_image_via_attachment_files(self):
        msg = {
            "text": "",
            "attachments": [{
                "is_share": True,
                "author_name": "Jane Dev",
                "text": "getting this on testnet",
                "files": [
                    {"mimetype": "image/jpeg",
                     "url_private": "https://files.slack.com/files-pri/T1-F2/err.jpg"},
                ],
            }],
        }
        refs = image_refs(msg)
        assert refs == [ImageRef(
            url="https://files.slack.com/files-pri/T1-F2/err.jpg", mime="image/jpeg")]

    def test_forwarded_image_via_image_url_infers_mime(self):
        msg = {"attachments": [{
            "is_share": True,
            "image_url": "https://files.slack.com/files-pri/T1-F3/preview.png",
        }]}
        assert image_refs(msg) == [ImageRef(
            url="https://files.slack.com/files-pri/T1-F3/preview.png", mime="image/png")]

    def test_non_share_attachment_ignored(self):
        msg = {"attachments": [{
            "is_share": False,
            "image_url": "https://x/unfurl.png",
        }]}
        assert image_refs(msg) == []

    def test_no_attachments_or_files_is_empty(self):
        assert image_refs({"text": "just typed a question"}) == []
        assert image_refs({}) == []

    def test_direct_and_forwarded_combined(self):
        msg = {
            "files": [{"mimetype": "image/png", "url_private": "https://x/a.png"}],
            "attachments": [{
                "is_share": True,
                "files": [{"mimetype": "image/gif", "url_private": "https://x/b.gif"}],
            }],
        }
        refs = image_refs(msg)
        assert [r.mime for r in refs] == ["image/png", "image/gif"]

    def test_file_without_url_is_skipped(self):
        msg = {"files": [{"mimetype": "image/png"}]}
        assert image_refs(msg) == []

    def test_unsupported_image_format_is_ignored(self):
        # svg, bmp, heic cannot be classified by the vision model, so they are
        # dropped at parse time rather than wasting a rejected vision call.
        msg = {"files": [
            {"mimetype": "image/svg+xml", "url_private": "https://x/a.svg"},
            {"mimetype": "image/bmp", "url_private": "https://x/b.bmp"},
            {"mimetype": "image/heic", "url_private": "https://x/c.heic"},
        ]}
        assert image_refs(msg) == []


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._payload if n < 0 else self._payload[:n]


class TestDownloadImage:
    def test_uses_bearer_token_and_returns_bytes(self, monkeypatch):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return _FakeResp(b"\x89PNG-bytes")

        monkeypatch.setattr("sui_ops_bot.attachments._opener.open", fake_open)
        ref = ImageRef(url="https://files.slack.com/files-pri/T1-F1/shot.png", mime="image/png")
        data = download_image(ref, "xoxb-secret")
        assert data == b"\x89PNG-bytes"
        assert captured["url"] == ref.url
        assert captured["auth"] == "Bearer xoxb-secret"

    def test_oversized_image_is_rejected(self, monkeypatch):
        big = b"x" * (MAX_IMAGE_BYTES + 1)
        monkeypatch.setattr("sui_ops_bot.attachments._opener.open",
                            lambda req, timeout=None: _FakeResp(big))
        ref = ImageRef(url="https://files.slack.com/files-pri/T1-F1/huge.png", mime="image/png")
        with pytest.raises(ValueError):
            download_image(ref, "xoxb-secret")

    def test_image_at_limit_is_kept(self, monkeypatch):
        exact = b"x" * MAX_IMAGE_BYTES
        monkeypatch.setattr("sui_ops_bot.attachments._opener.open",
                            lambda req, timeout=None: _FakeResp(exact))
        ref = ImageRef(url="https://files.slack.com/files-pri/T1-F1/big.png", mime="image/png")
        assert download_image(ref, "xoxb-secret") == exact


class TestRedirectDoesNotLeakToken:
    def _redirect(self, from_url, to_url):
        import email.message
        import urllib.request

        from sui_ops_bot.attachments import _StripAuthOnCrossHostRedirect
        req = urllib.request.Request(from_url, headers={"Authorization": "Bearer xoxb-secret"})
        handler = _StripAuthOnCrossHostRedirect()
        return handler.redirect_request(req, None, 302, "Found", email.message.Message(), to_url)

    def test_cross_host_redirect_strips_authorization(self):
        new = self._redirect(
            "https://files.slack.com/files-pri/T1-F1/shot.png",
            "https://evil.example.com/steal")
        assert new is not None
        assert new.get_header("Authorization") is None

    def test_same_host_redirect_keeps_authorization(self):
        new = self._redirect(
            "https://files.slack.com/files-pri/T1-F1/shot.png",
            "https://files.slack.com/files-pri/T1-F1/shot-2.png")
        assert new is not None
        assert new.get_header("Authorization") == "Bearer xoxb-secret"
