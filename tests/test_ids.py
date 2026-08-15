from sui_ops_bot import config
from sui_ops_bot.ids import (
    clean_channel,
    clip_summary,
    effective_text,
    infer_channel,
    is_admin,
    is_substantive,
    match_enum,
    needs_more_info,
    norm_id,
    parse_ids,
    platform_from_source,
    resolve_platform,
    shared_attachment,
)
from sui_ops_bot.logutil import current_window


class TestCurrentWindow:
    def test_emea_americas_apac_by_hour(self):
        assert current_window(6) == "EMEA"
        assert current_window(13) == "EMEA"
        assert current_window(14) == "Americas"
        assert current_window(21) == "Americas"
        assert current_window(22) == "APAC"
        assert current_window(3) == "APAC"

    def test_always_returns_a_window(self):
        assert all(current_window(h) in {"EMEA", "Americas", "APAC"} for h in range(24))


class TestCleanChannel:
    def test_placeholders_become_blank(self):
        for p in ("<UNKNOWN>", "unknown", "N/A", "none", "-", "", "  ", "?"):
            assert clean_channel(p) == ""

    def test_real_venue_kept(self):
        assert clean_channel("TG - Overflow DeepBook") == "TG - Overflow DeepBook"
        assert clean_channel("  GitHub Issues  ") == "GitHub Issues"


class TestInferChannel:
    def test_content_venue_wins(self):
        # A venue inferred from content is used as-is; the forward is not consulted.
        fwd = {"channel_name": "overflow-deepbook"}
        assert infer_channel("TG - Overflow DeepBook", fwd) == "TG - Overflow DeepBook"

    def test_falls_back_to_forward_channel_name(self):
        fwd = {"channel_name": "overflow-deepbook"}
        assert infer_channel("", fwd) == "#overflow-deepbook"

    def test_placeholder_content_falls_back_to_forward(self):
        fwd = {"channel_name": "tg-walrus-mirror"}
        assert infer_channel("unknown", fwd) == "#tg-walrus-mirror"

    def test_forward_name_leading_hash_normalized(self):
        assert infer_channel("", {"channel_name": "#already-hashed"}) == "#already-hashed"

    def test_unknown_stays_blank(self):
        assert infer_channel("", {}) == ""
        assert infer_channel("n/a", None) == ""
        assert infer_channel("", {"author_name": "Jane"}) == ""


class TestResolvePlatform:
    def test_non_slack_link_wins_over_slack_verdict(self):
        # The bug: forwarded via Slack, but the real origin is GitHub.
        assert resolve_platform("Slack", "https://github.com/MystenLabs/sui/pull/9") == "GitHub"

    def test_bare_slack_verdict_dropped_when_link_is_slack(self):
        # Slack is the transport, not the origin; a slack archive link is not an origin.
        assert resolve_platform("Slack", "https://team.slack.com/archives/C1/p1") == ""

    def test_llm_origin_kept_when_only_transport_link(self):
        assert resolve_platform("Telegram", "https://team.slack.com/archives/C1/p1") == "Telegram"

    def test_venue_text_infers_origin(self):
        assert resolve_platform("", "", "Sui Developer Forum thread") == "Sui Forum"

    def test_plain_slack_verdict_no_link_is_blank(self):
        assert resolve_platform("Slack", "", "Sui Developer Relations") == ""

    def test_real_origin_passthrough(self):
        assert resolve_platform("GitHub", "") == "GitHub"
        assert resolve_platform("", "https://t.me/xyz") == "Telegram"

    def test_all_empty_is_blank(self):
        assert resolve_platform("", "", "") == ""


class TestIsAdmin:
    def test_configured_admin(self):
        assert is_admin("UDOM", "UOWNER", ["UDOM", "UBOSS"]) is True

    def test_escalator_owner_is_admin(self):
        assert is_admin("UOWNER", "UOWNER", []) is True

    def test_random_user_is_not(self):
        assert is_admin("URANDO", "UOWNER", ["UDOM"]) is False

    def test_empty_user_is_not(self):
        assert is_admin("", "UOWNER", ["UDOM"]) is False


class TestEmojiStatusMap:
    def test_emoji_map_covers_the_four_admin_actions(self):
        assert config.EMOJI_STATUS == {
            "arrow_right": "Forwarded",
            "white_check_mark": "Acknowledged",
            "heart": "In Progress",
            "tada": "Solved",
        }
        # Every mapped status is a real lifecycle state.
        for status in config.EMOJI_STATUS.values():
            assert status in config.STATUSES


class TestNeedsMoreInfo:
    def test_unknown_source_needs_info(self):
        assert needs_more_info("internal team", "") is True
        assert needs_more_info("", "   ") is True

    def test_reporter_waiting_needs_info(self):
        assert needs_more_info("reporter", "#tg-overflow") is True

    def test_sourced_and_not_reporter_is_fine(self):
        assert needs_more_info("internal team", "#tg-overflow") is False
        assert needs_more_info("organizer", "GitHub Issues") is False


class TestClipSummary:
    def test_short_summary_unchanged(self):
        assert clip_summary("RPC endpoint unreachable", 90) == "RPC endpoint unreachable"

    def test_collapses_whitespace_and_newlines(self):
        assert clip_summary("line one\n  line   two", 90) == "line one line two"

    def test_long_summary_trimmed_at_word_boundary(self):
        s = ("Boar Network inquiring about Guardian roadmap and Guardian role and Bitcoin node "
             "infrastructure and requesting direct contact with the Foundation owner")
        out = clip_summary(s, 90)
        assert len(out) <= 91  # limit plus the ellipsis
        assert out.endswith("…")
        assert " " in out and not out[:-1].endswith(" ")  # no dangling space before ellipsis

    def test_empty(self):
        assert clip_summary("", 90) == ""


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


class TestForwardedMessages:
    def _fwd_event(self, own_text=""):
        return {
            "text": own_text,
            "attachments": [{
                "is_share": True,
                "text": "Builder cannot pass assert_app_is_authorized on DeepBook testnet.",
                "author_name": "Jane Dev",
                "from_url": "https://team.slack.com/archives/C0B5FS5HZ37/p1786174903232989",
            }],
        }

    def test_plain_forward_recovers_content(self):
        # Top-level text is empty (the real-world case that used to be dropped).
        ev = self._fwd_event(own_text="")
        eff = effective_text(ev)
        assert "assert_app_is_authorized" in eff
        assert "Forwarded from Jane Dev" in eff
        assert is_substantive(eff, config.MIN_MESSAGE_CHARS)

    def test_forward_with_own_comment_keeps_both(self):
        ev = self._fwd_event(own_text="please look at this one")
        eff = effective_text(ev)
        assert "please look at this one" in eff
        assert "assert_app_is_authorized" in eff

    def test_shared_attachment_exposes_author_and_url(self):
        att = shared_attachment(self._fwd_event())
        assert att.get("author_name") == "Jane Dev"
        assert att.get("from_url", "").startswith("https://")

    def test_no_attachment_is_empty(self):
        assert shared_attachment({"text": "hi"}) == {}
        assert effective_text({"text": "just typed"}) == "just typed"

    def test_non_share_attachment_ignored(self):
        ev = {"text": "", "attachments": [{"is_share": False, "text": "unfurl preview"}]}
        assert shared_attachment(ev) == {}
        assert effective_text(ev) == ""


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

    def test_image_only_message_is_kept(self):
        # An image with little or no text carries the substance itself.
        assert is_substantive("", 25, has_image=True)
        assert is_substantive("see screenshot", 25, has_image=True)

    def test_image_only_dropped_when_no_image(self):
        # Same little-text message with no image stays dropped.
        assert not is_substantive("", 25, has_image=False)
        assert not is_substantive("see screenshot", 25, has_image=False)

    def test_has_image_defaults_false(self):
        assert not is_substantive("too short", 25)
