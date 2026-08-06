from sui_ops_bot import config


def test_products_are_the_locked_fifteen():
    assert config.PRODUCTS == [
        "DeepBook", "Walrus", "Harbor", "Seal", "Nautilus", "MemWal", "Enoki",
        "Slush", "zkLogin", "SDK", "Bridge", "Sui Core", "Hashi", "Program", "Other",
    ]


def test_types_are_the_locked_five():
    assert config.TYPES == ["Question", "Open PR", "Bug", "Feature Request", "Communication"]


def test_no_duplicate_enum_values():
    assert len(config.PRODUCTS) == len(set(config.PRODUCTS))
    assert len(config.TYPES) == len(set(config.TYPES))


def test_defaults_are_members_of_their_enums():
    assert config.PRODUCT_DEFAULT in config.PRODUCTS
    assert config.TYPE_DEFAULT in config.TYPES


def test_managed_columns_include_product_and_type():
    assert "Product" in config.MANAGED_COLUMNS
    assert "Type" in config.MANAGED_COLUMNS
    # Legacy team column is preserved, not repurposed.
    assert "Escalated To" in config.HUMAN_COLUMNS


def test_team_taxonomy_is_gone():
    # The old escalation-target concept must not linger as a public constant.
    assert not hasattr(config, "ESCALATION_TARGETS")
