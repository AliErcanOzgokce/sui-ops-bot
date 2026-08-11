"""Pure tests for image-reference parsing (no network).

These mirror the real Slack shapes: a directly pasted image arrives in the
message's own `files` list; a forwarded image arrives on the `is_share`
attachment, either as its own `files` list or as an `image_url` preview. The
download half is exercised with a monkeypatched opener so no bytes cross the wire.
"""
from sui_ops_bot.attachments import ImageRef, download_image, image_refs


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


class TestDownloadImage:
    def test_uses_bearer_token_and_returns_bytes(self, monkeypatch):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"\x89PNG-bytes"

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return FakeResp()

        monkeypatch.setattr("sui_ops_bot.attachments.urllib.request.urlopen", fake_urlopen)
        ref = ImageRef(url="https://files.slack.com/files-pri/T1-F1/shot.png", mime="image/png")
        data = download_image(ref, "xoxb-secret")
        assert data == b"\x89PNG-bytes"
        assert captured["url"] == ref.url
        assert captured["auth"] == "Bearer xoxb-secret"
