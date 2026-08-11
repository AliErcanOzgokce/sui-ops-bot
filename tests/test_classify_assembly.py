"""Tests for vision request assembly in the classifier.

The pure assembly helper is tested directly. The classify_message wiring is
tested with a fake Anthropic client that captures the request, so no key and no
network are needed. Image bytes only ever appear base64-encoded inside an image
block, never in logs.
"""
import base64

from sui_ops_bot import classify, config


class TestAssembleUserContent:
    def test_text_only_returns_plain_string(self):
        # Unchanged from today: no images means a bare string, no blocks.
        assert classify.assemble_user_content("hello", None) == "hello"
        assert classify.assemble_user_content("hello", []) == "hello"

    def test_images_produce_one_block_each_plus_text(self):
        images = [
            {"data": b"AAA", "mime": "image/png"},
            {"data": b"BBB", "mime": "image/jpeg"},
        ]
        content = classify.assemble_user_content("look at these", images)
        assert isinstance(content, list)
        image_blocks = [b for b in content if b["type"] == "image"]
        text_blocks = [b for b in content if b["type"] == "text"]
        assert len(image_blocks) == 2
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "look at these"
        assert image_blocks[0]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(b"AAA").decode("ascii"),
        }
        assert image_blocks[1]["source"]["media_type"] == "image/jpeg"

    def test_images_with_empty_text_has_no_text_block(self):
        content = classify.assemble_user_content("", [{"data": b"AAA", "mime": "image/png"}])
        assert [b["type"] for b in content] == ["image"]


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeBlock:
    type = "tool_use"

    def __init__(self, data):
        self.input = data


class _FakeResp:
    def __init__(self, data):
        self.content = [_FakeBlock(data)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, sink, fail_on_list=False):
        self._sink = sink
        self._fail_on_list = fail_on_list

    def create(self, **kwargs):
        self._sink.append(kwargs)
        content = kwargs["messages"][0]["content"]
        if self._fail_on_list and isinstance(content, list):
            raise RuntimeError("model does not support image input")
        return _FakeResp({"is_escalation": True, "product": "Walrus", "type": "Bug"})


class _FakeClient:
    def __init__(self, sink, fail_on_list=False):
        self.messages = _FakeMessages(sink, fail_on_list)


class TestClassifyMessageVision:
    def test_text_only_request_has_no_image_blocks(self, monkeypatch):
        sink = []
        monkeypatch.setattr(classify, "client", lambda: _FakeClient(sink))
        classify.classify_message("wallet rpc is unreachable")
        content = sink[0]["messages"][0]["content"]
        assert isinstance(content, str)
        assert "wallet rpc is unreachable" in content

    def test_system_prompt_includes_taxonomy(self, monkeypatch):
        sink = []
        monkeypatch.setattr(classify, "client", lambda: _FakeClient(sink))
        classify.classify_message("something")
        system = sink[0]["system"]
        assert "Walrus" in system and "Question" in system

    def test_images_add_image_blocks_to_request(self, monkeypatch):
        sink = []
        monkeypatch.setattr(classify, "client", lambda: _FakeClient(sink))
        monkeypatch.setattr(config, "CLASSIFY_VISION", True)
        classify.classify_message("screenshot", images=[{"data": b"AAA", "mime": "image/png"}])
        content = sink[0]["messages"][0]["content"]
        assert isinstance(content, list)
        assert sum(b["type"] == "image" for b in content) == 1

    def test_vision_disabled_falls_back_to_text_only(self, monkeypatch):
        sink = []
        monkeypatch.setattr(classify, "client", lambda: _FakeClient(sink))
        monkeypatch.setattr(config, "CLASSIFY_VISION", False)
        classify.classify_message("screenshot", images=[{"data": b"AAA", "mime": "image/png"}])
        assert isinstance(sink[0]["messages"][0]["content"], str)

    def test_fail_soft_retries_text_only_on_vision_error(self, monkeypatch):
        sink = []
        monkeypatch.setattr(classify, "client", lambda: _FakeClient(sink, fail_on_list=True))
        monkeypatch.setattr(config, "CLASSIFY_VISION", True)
        result = classify.classify_message(
            "screenshot", images=[{"data": b"AAA", "mime": "image/png"}])
        # First attempt (list content) failed, second (string content) succeeded.
        assert result["input"]["is_escalation"] is True
        assert isinstance(sink[0]["messages"][0]["content"], list)
        assert isinstance(sink[-1]["messages"][0]["content"], str)


def test_config_has_vision_flag():
    assert isinstance(config.CLASSIFY_VISION, bool)
