from sui_ops_bot import config
from sui_ops_bot.ids import (
    is_substantive,
    match_enum,
    norm_id,
    parse_ids,
    platform_from_source,
)


class TestNormId:
    def test_forms_of_twelve_all_equal(self):
        forms = ["12", "#12", " 12 ", "Q-12", "q-12", "Q12", "012"]
        assert {norm_id(f) for f in forms} == {"12"}

    def test_non_numeric_lowercased(self):
        assert norm_id("ABC") == "abc"
        assert norm_id("#Foo-1a") == "foo-1a"

    def test_zero_stays(self):
        assert norm_id("0") == "0"


class TestParseIds:
    def test_comma_and_space_and_newline(self):
        assert parse_ids("12, 13 15\n16") == ["12", "13", "15", "16"]

    def test_list_passthrough(self):
        assert parse_ids(["12", " 13 ", ""]) == ["12", "13"]

    def test_empty(self):
        assert parse_ids("") == []
        assert parse_ids("   ") == []


class TestMatchEnum:
    def test_case_insensitive_match(self):
        assert match_enum("walrus", config.PRODUCTS, "Other") == "Walrus"
        assert match_enum("OPEN PR", config.TYPES, "Question") == "Open PR"

    def test_unknown_falls_back_to_default(self):
        assert match_enum("nope", config.PRODUCTS, "Other") == "Other"

    def test_blank_falls_back_to_default(self):
        assert match_enum("", config.TYPES, "Question") == "Question"

    def test_sui_core_two_words(self):
        assert match_enum("sui core", config.PRODUCTS, "Other") == "Sui Core"


class TestPlatformFromSource:
    def test_known_sources(self):
        cases = {
            "https://github.com/MystenLabs/sui/issues/1": "GitHub",
            "https://t.me/xyz": "Telegram",
            "some telegram group": "Telegram",
            "https://discord.com/channels/1/2": "Discord",
            "https://forums.sui.io/t/abc": "Sui Forum",
            "https://x.com/user/status/1": "X",
            "https://team.slack.com/archives/C1/p1": "Slack",
        }
        for src, want in cases.items():
            assert platform_from_source(src) == want

    def test_unknown_is_blank(self):
        assert platform_from_source("just a note") == ""
        assert platform_from_source("") == ""


class TestIsSubstantive:
    def test_short_dropped(self):
        assert not is_substantive("too short", 25)

    def test_long_enough_kept(self):
        assert is_substantive("Dev cannot pass assert_app_is_authorized on testnet", 25)

    def test_trivial_ack_dropped(self):
        assert not is_substantive("thanks" + " " * 30, 5)

    def test_markup_and_emoji_stripped_before_length(self):
        assert not is_substantive("<@U123> :wave: :tada:", 25)

    def test_empty(self):
        assert not is_substantive("", 25)
