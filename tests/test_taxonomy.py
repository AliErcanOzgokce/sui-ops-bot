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


def test_status_lifecycle_is_the_seven_states():
    assert config.STATUSES == [
        "Sent", "Acknowledged", "Forwarded", "In Progress",
        "Solved", "Answered", "Closed",
    ]
    assert config.STATUS_SENT == "Sent"
    assert config.STATUS_CLOSED == "Closed"
    assert config.STATUS_SENT in config.STATUSES


def test_open_statuses_cover_active_states_and_legacy():
    # Active states a lead still acts on are "open".
    for s in ("Sent", "Acknowledged", "Forwarded", "In Progress"):
        assert s in config.OPEN_STATUSES
    # Resolved states are not open.
    for s in ("Solved", "Answered", "Closed"):
        assert s not in config.OPEN_STATUSES
    # Legacy statuses on historical rows still count as open so they keep showing.
    for s in ("Open", "Escalated"):
        assert s in config.OPEN_STATUSES


def test_waiting_on_enum_is_the_three_parties():
    assert config.WAITING_ON == ["internal team", "organizer", "reporter"]
    assert len(config.WAITING_ON) == len(set(config.WAITING_ON))


def test_waiting_on_default_is_blank():
    # The field is optional: unknown lands as an empty cell, not a guessed party.
    assert config.WAITING_ON_DEFAULT == ""
    assert config.WAITING_ON_DEFAULT not in config.WAITING_ON


def test_waiting_on_is_a_managed_column_not_a_human_one():
    assert "Waiting On" in config.MANAGED_COLUMNS
    assert "Waiting On" not in config.HUMAN_COLUMNS
    # Legacy human/escalation columns stay untouched.
    assert "Escalated To" in config.HUMAN_COLUMNS


def test_source_venues_are_the_agreed_list():
    # Agreed with the team for the one-tap set-source menu (Office Hours excluded).
    assert config.SOURCE_VENUES == [
        "Telegram", "Discord", "Sui Developer Forum", "GitHub Issues",
        "X / Twitter", "Email", "Other",
    ]
    assert len(config.SOURCE_VENUES) == len(set(config.SOURCE_VENUES))
