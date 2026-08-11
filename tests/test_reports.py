from datetime import date

from sui_ops_bot import reports


class FakeRow:
    """Minimal stand-in for sheet.Row: reports only touches these attributes."""

    def __init__(self, rid, product="", type="", status="Escalated",
                 date_asked="2026-08-01", summary="", raised_by="",
                 link="", ts="", channel="", row_number=1, waiting_on=""):
        self.row_number = row_number
        self.status = status
        self.original_ts = ts
        self.slack_channel = channel
        self.product = product
        self.type = type
        self.values = {
            "ID": rid, "Product": product, "Type": type, "Status": status,
            "Date Asked": date_asked, "Question Summary": summary,
            "Raised By": raised_by, "Link": link, "Waiting On": waiting_on,
        }


class FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def reload(self):
        pass

    def open_rows(self):
        return list(self._rows)

    def row_link(self, n):
        return f"https://sheet/row/{n}"


def _link(_ch, _ts):
    return "https://slack/permalink"


class TestAgeDays:
    def test_counts_whole_days(self):
        assert reports.age_days("2026-08-01", today=date(2026, 8, 6)) == 5

    def test_same_day_is_zero(self):
        assert reports.age_days("2026-08-06", today=date(2026, 8, 6)) == 0

    def test_unparseable_is_none(self):
        assert reports.age_days("", today=date(2026, 8, 6)) is None
        assert reports.age_days("not-a-date", today=date(2026, 8, 6)) is None


class TestFilterRows:
    def setup_method(self):
        self.rows = [
            FakeRow("1", product="Walrus", type="Bug"),
            FakeRow("2", product="Walrus", type="Question"),
            FakeRow("3", product="DeepBook", type="Bug"),
        ]

    def test_filter_by_product_case_insensitive(self):
        got = reports.filter_rows(self.rows, product="walrus")
        assert {r.values["ID"] for r in got} == {"1", "2"}

    def test_filter_by_type(self):
        got = reports.filter_rows(self.rows, type="Bug")
        assert {r.values["ID"] for r in got} == {"1", "3"}

    def test_filter_by_both(self):
        got = reports.filter_rows(self.rows, product="Walrus", type="Bug")
        assert [r.values["ID"] for r in got] == ["1"]

    def test_no_filter_returns_all(self):
        assert len(reports.filter_rows(self.rows)) == 3


class TestGroupByProduct:
    def test_taxonomy_order_then_unclassified_last(self):
        rows = [
            FakeRow("1", product="Walrus"),
            FakeRow("2", product="DeepBook"),
            FakeRow("3", product=""),           # -> Unclassified
            FakeRow("4", product="DeepBook"),
        ]
        grouped = reports.group_by_product(rows)
        # DeepBook precedes Walrus in the canonical PRODUCTS order.
        assert list(grouped.keys()) == ["DeepBook", "Walrus", "Unclassified"]
        assert [r.values["ID"] for r in grouped["DeepBook"]] == ["2", "4"]
        assert [r.values["ID"] for r in grouped["Unclassified"]] == ["3"]


class TestWeeklyReport:
    def test_groups_by_product_and_flags_and_filters(self):
        rows = [
            FakeRow("1", product="Walrus", type="Bug", summary="blob lifecycle",
                    date_asked="2026-01-01", ts="1", channel="C1"),
            FakeRow("2", product="DeepBook", type="Question", summary="spot pool",
                    date_asked="2026-08-05", ts="2", channel="C1"),
        ]
        store = FakeStore(rows)
        out = reports.weekly_report(store, days=7, linker=_link)
        assert "*DeepBook* (1)" in out
        assert "*Walrus* (1)" in out
        assert "2 unanswered" in out

    def test_product_filter_narrows_scope(self):
        rows = [
            FakeRow("1", product="Walrus", type="Bug"),
            FakeRow("2", product="DeepBook", type="Question"),
        ]
        out = reports.weekly_report(FakeStore(rows), product="Walrus", linker=_link)
        assert "Walrus" in out
        assert "DeepBook" not in out
        assert "(Walrus)" in out

    def test_empty_is_clear_message(self):
        out = reports.weekly_report(FakeStore([]), linker=_link)
        assert "tracker is clear" in out


class TestEscalationNoteBlocks:
    def test_has_discard_and_solved_buttons_with_value(self):
        blocks = reports.escalation_note_blocks("44", "Walrus", "Bug",
                                                "https://sheet/row/48", value="123.456")
        # find the actions block
        actions = next(b for b in blocks if b["type"] == "actions")
        ids = {e["action_id"] for e in actions["elements"]}
        assert ids == {"row_discard", "row_solved"}
        assert all(e["value"] == "123.456" for e in actions["elements"])
        # discard is guarded by a confirm dialog
        discard = next(e for e in actions["elements"] if e["action_id"] == "row_discard")
        assert "confirm" in discard

    def test_summary_line_shows_id_and_badge(self):
        blocks = reports.escalation_note_blocks("7", "Seal", "Question", "https://x", "1")
        section = next(b for b in blocks if b["type"] == "section")
        assert "*#7*" in section["text"]["text"]
        assert "Seal · Question" in section["text"]["text"]

    def test_no_dup_callout_by_default(self):
        blocks = reports.escalation_note_blocks("7", "Seal", "Question", "https://x", "1")
        assert not any("possible duplicate" in str(b).lower() for b in blocks)

    def test_dup_of_renders_possible_duplicate_line(self):
        blocks = reports.escalation_note_blocks("9", "SDK", "Bug", "https://x", "1", dup_of="7")
        text = " ".join(str(b) for b in blocks).lower()
        assert "possible duplicate of" in text and "#7" in text

    def _set_source_select(self, blocks):
        for b in blocks:
            if b.get("type") != "actions":
                continue
            for el in b.get("elements", []):
                if el.get("action_id") == "row_set_source":
                    return el
        return None

    def test_set_source_select_offers_the_agreed_venues(self):
        from sui_ops_bot import config
        blocks = reports.escalation_note_blocks("7", "Seal", "Question", "https://x", "123.456")
        sel = self._set_source_select(blocks)
        assert sel is not None and sel["type"] == "static_select"
        labels = [o["text"]["text"] for o in sel["options"]]
        assert labels == config.SOURCE_VENUES

    def test_set_source_option_values_embed_the_row_ts(self):
        blocks = reports.escalation_note_blocks("7", "Seal", "Question", "https://x", "123.456")
        sel = self._set_source_select(blocks)
        # Each option carries the row ts so the action handler can find the row.
        for o in sel["options"]:
            ts, sep, venue = o["value"].partition("::")
            assert ts == "123.456" and sep == "::" and venue

    def test_buttons_block_still_intact(self):
        # The set-source control is a separate actions block; the button block is
        # unchanged so the discard/solved flow is untouched.
        blocks = reports.escalation_note_blocks("7", "Seal", "Question", "https://x", "123.456")
        buttons = next(b for b in blocks if b["type"] == "actions")
        ids = {e["action_id"] for e in buttons["elements"]}
        assert ids == {"row_discard", "row_solved"}


class TestFollowups:
    TODAY = date(2026, 8, 10)

    def _rows(self):
        return [
            FakeRow("1", waiting_on="internal team", date_asked="2026-08-01", summary="rpc down"),
            FakeRow("2", waiting_on="organizer", date_asked="2026-08-09", summary="cert deadline"),
            FakeRow("3", waiting_on="", date_asked="2026-08-01", summary="no party"),
            FakeRow("4", waiting_on="reporter", date_asked="2026-08-02", summary="needs logs"),
            FakeRow("5", waiting_on="internal team", date_asked="2026-08-03", summary="sdk build"),
        ]

    def test_group_followups_filters_and_orders(self):
        grouped = reports.group_followups(self._rows(), days=3, today=self.TODAY)
        # organizer item is only 1 day old -> excluded; blank party -> excluded.
        assert list(grouped.keys()) == ["internal team", "reporter"]
        # within a party, oldest first (id 1 is 9d, id 5 is 7d)
        assert [r.values["ID"] for r in grouped["internal team"]] == ["1", "5"]
        assert [r.values["ID"] for r in grouped["reporter"]] == ["4"]

    def test_report_groups_by_party_and_links(self):
        out = reports.followups_report(FakeStore(self._rows()), days=3,
                                       linker=_link, today=self.TODAY)
        assert "Waiting on internal team" in out
        assert "Waiting on reporter" in out
        assert "over 3 days" in out
        assert "*#1*" in out and "*#5*" in out and "*#4*" in out
        # excluded ones do not appear
        assert "*#2*" not in out and "*#3*" not in out
        # each item carries a link
        assert out.count("https://") >= 3

    def test_report_empty_when_nothing_stale(self):
        fresh = [FakeRow("9", waiting_on="organizer", date_asked="2026-08-10")]
        out = reports.followups_report(FakeStore(fresh), days=3, linker=_link, today=self.TODAY)
        assert "No follow-ups" in out

    def test_report_ignores_rows_without_a_party(self):
        rows = [FakeRow("7", waiting_on="", date_asked="2026-07-01", summary="old but no party")]
        out = reports.followups_report(FakeStore(rows), days=3, linker=_link, today=self.TODAY)
        assert "No follow-ups" in out


class TestStatusReport:
    def test_by_product_breakdown(self):
        rows = [
            FakeRow("1", product="Walrus"),
            FakeRow("2", product="Walrus"),
            FakeRow("3", product="Seal"),
        ]
        out = reports.status_report(FakeStore(rows))
        assert "*Open items:* 3" in out
        assert "Walrus: 2" in out
        assert "Seal: 1" in out
