"""Regression: the MCP post_message tool is the one place that asks for a source.
The auto-tracker never interrogates, but a manually posted item must have
provenance, so post_message must keep refusing (and asking) when source is
omitted. This returns before any network, so the test stays offline.
"""
from sui_ops_bot import mcpserver


class TestPostMessageRequiresSource:
    def test_blank_source_is_refused_with_a_prompt(self):
        out = mcpserver.post_message("builder cannot upload a Walrus blob", "")
        assert "source" in out.lower()
        assert "required" in out.lower()

    def test_whitespace_source_is_refused(self):
        out = mcpserver.post_message("some question", "   ")
        assert "source" in out.lower() and "required" in out.lower()
