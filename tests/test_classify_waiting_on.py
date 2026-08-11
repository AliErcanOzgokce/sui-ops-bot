"""The classifier gains an optional waiting_on field: who an open item is
waiting on. These pin the tool schema (optional, constrained to the three
parties) and the inference guidance in the prompt. No network.
"""
from sui_ops_bot import classify, config
from sui_ops_bot.ids import match_enum


class TestClassifyToolSchema:
    def test_waiting_on_present_with_the_enum(self):
        props = classify.CLASSIFY_TOOL["input_schema"]["properties"]
        assert "waiting_on" in props
        assert props["waiting_on"]["enum"] == config.WAITING_ON

    def test_waiting_on_is_optional(self):
        # Not required: the model may omit it when it cannot tell.
        assert "waiting_on" not in classify.CLASSIFY_TOOL["input_schema"]["required"]


class TestWaitingOnGuidanceInPrompt:
    def test_prompt_explains_who_to_wait_on(self):
        s = classify.classify_system().lower()
        assert "waiting" in s
        for party in ("internal team", "organizer", "reporter"):
            assert party in s


class TestWaitingOnConstraint:
    def test_match_enum_constrains_and_defaults_blank(self):
        assert match_enum("Organizer", config.WAITING_ON, config.WAITING_ON_DEFAULT) == "organizer"
        assert match_enum("INTERNAL TEAM", config.WAITING_ON, "") == "internal team"
        assert match_enum("nonsense", config.WAITING_ON, config.WAITING_ON_DEFAULT) == ""
        assert match_enum("", config.WAITING_ON, config.WAITING_ON_DEFAULT) == ""
