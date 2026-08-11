"""Pure tests for duplicate / re-forward detection.

Fake rows mirror the shape SheetStore hands out (a `.values` dict), so the module
is exercised without Slack, Sheets, or Anthropic.
"""
from sui_ops_bot.dedup import dedup_key, find_duplicate, text_similarity


class FakeRow:
    def __init__(self, id, product, summary, link="", status="Escalated"):
        self.values = {
            "ID": id, "Product": product, "Question Summary": summary,
            "Link": link, "Status": status,
        }


class TestDedupKey:
    def test_extracts_canonical_github_issue_url_from_link(self):
        assert dedup_key("", "https://github.com/MystenLabs/sui/issues/123") == \
            "github.com/mystenlabs/sui/issues/123"

    def test_extracts_from_url_embedded_in_text(self):
        text = "builder hit this, see https://github.com/MystenLabs/walrus/issues/7 for repro"
        assert dedup_key(text) == "github.com/mystenlabs/walrus/issues/7"

    def test_normalizes_scheme_www_case_and_trailing(self):
        a = dedup_key("", "http://www.github.com/MystenLabs/Sui/issues/9#issuecomment-1")
        b = dedup_key("", "https://github.com/mystenlabs/sui/issues/9")
        assert a == b == "github.com/mystenlabs/sui/issues/9"

    def test_pull_request_url_is_keyed(self):
        assert dedup_key("", "https://github.com/MystenLabs/sui/pull/42") == \
            "github.com/mystenlabs/sui/pull/42"

    def test_returns_none_without_github_url(self):
        assert dedup_key("just a plain question about walrus blobs") is None
        assert dedup_key("see https://forums.sui.io/t/abc", "") is None


class TestTextSimilarity:
    def test_identical_is_one(self):
        assert text_similarity("wallet rpc unreachable", "wallet rpc unreachable") == 1.0

    def test_disjoint_is_zero(self):
        assert text_similarity("walrus blob upload fails", "enoki sponsor gas") == 0.0

    def test_empty_is_zero(self):
        assert text_similarity("", "anything") == 0.0

    def test_typed_then_forwarded_is_high(self):
        typed = "DevX version mismatch: sui-sdk 1.2 vs 1.3 breaks the build on testnet"
        forwarded = ("Forwarded from Jane Dev: DevX version mismatch: sui-sdk 1.2 vs 1.3 "
                     "breaks the build on testnet")
        assert text_similarity(typed, forwarded) >= 0.6


class TestFindDuplicate:
    def _open_rows(self):
        return [
            FakeRow("10", "Walrus", "blob upload returns 500 on testnet",
                    link="https://github.com/MystenLabs/walrus/issues/7"),
            FakeRow("11", "Sui Core", "mainnet rpc unreachable calling sui_getObject"),
        ]

    def test_exact_key_match_even_across_product(self):
        rows = self._open_rows()
        m = find_duplicate("github.com/mystenlabs/walrus/issues/7", "Sui Core",
                           "totally different words here", rows)
        assert m is not None and m.kind == "exact" and m.row.values["ID"] == "10"

    def test_similarity_match_on_same_product(self):
        rows = self._open_rows()
        m = find_duplicate(None, "Sui Core",
                           "mainnet rpc unreachable when calling sui_getObject", rows)
        assert m is not None and m.kind == "similar" and m.row.values["ID"] == "11"

    def test_similar_text_but_different_product_is_none(self):
        rows = self._open_rows()
        m = find_duplicate(None, "Walrus",
                           "mainnet rpc unreachable when calling sui_getObject", rows)
        assert m is None

    def test_unrelated_is_none(self):
        rows = self._open_rows()
        assert find_duplicate(None, "Enoki", "how do I sponsor gas with enoki", rows) is None

    def test_only_open_rows_considered(self):
        closed = [FakeRow("10", "Walrus", "blob upload returns 500 on testnet",
                          link="https://github.com/MystenLabs/walrus/issues/7",
                          status="Closed")]
        assert find_duplicate("github.com/mystenlabs/walrus/issues/7", "Walrus",
                              "blob upload returns 500 on testnet", closed) is None

    def test_typed_then_forwarded_collapses(self):
        rows = [FakeRow("20", "SDK", "DevX version mismatch sui-sdk 1.2 vs 1.3 breaks build")]
        m = find_duplicate(None, "SDK",
                           "Forwarded from Jane: DevX version mismatch sui-sdk 1.2 vs 1.3 "
                           "breaks build", rows)
        assert m is not None and m.kind == "similar" and m.row.values["ID"] == "20"
