"""The classifier's system prompt is the domain model in prose. These tests pin
the guidance that separates internal team ops (skip) from external community
questions (log) and the product-disambiguation guide, so a future edit that drops
a rule fails loudly. No network: the prompt is built by a pure function.
"""
from sui_ops_bot import classify


class TestClassifySystemPrompt:
    def setup_method(self):
        self.system = classify.classify_system()

    def test_excludes_internal_team_ops(self):
        s = self.system.lower()
        # The leads' own coordination must be skipped, not tracked.
        for phrase in ("invoice", "on-call", "drive folder", "upload your report"):
            assert phrase in s, f"missing exclude cue: {phrase}"

    def test_includes_community_program_logistics(self):
        s = self.system.lower()
        # External program/logistics questions still get logged.
        for phrase in ("overflow", "certification", "deadline", "program", "communication"):
            assert phrase in s, f"missing include cue: {phrase}"

    def test_product_disambiguation_guide_present(self):
        s = self.system
        # RPC / fullnode / network -> Sui Core
        assert "RPC" in s and "Sui Core" in s
        # wallet / signing -> Slush
        assert "Slush" in s
        # TS SDK / dapp-kit -> SDK
        assert "dapp-kit" in s
        # blob / quilt -> Walrus
        assert "quilt" in s.lower() and "Walrus" in s
        # TEE -> Nautilus
        assert "TEE" in s and "Nautilus" in s

    def test_taxonomy_still_embedded(self):
        # Regression: the enums must remain in the prompt.
        assert "Question" in self.system
        for product in ("Walrus", "Program", "Hashi"):
            assert product in self.system
