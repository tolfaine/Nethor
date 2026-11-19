from assistant.keys import key_id, key_kind, make_key, migrate_key, mode_for_key


def test_kinds_and_modes() -> None:
    assert (key_kind("dm:1"), mode_for_key("dm:1")) == ("dm", "private")
    assert (key_kind("thread:2"), mode_for_key("thread:2")) == ("thread", "private")
    assert (key_kind("pub:3"), mode_for_key("pub:3")) == ("pub", "public")
    assert (key_kind("priv:4"), mode_for_key("priv:4")) == ("priv", "private")
    assert (key_kind("console"), mode_for_key("console")) == ("dm", "private")


def test_key_id() -> None:
    assert key_id(make_key("thread", 42)) == 42 and key_id(make_key("priv", 43)) == 43
    assert key_id("console") is None and key_id("pub:x") is None and key_id("other:5") is None


def test_migrate_bare_user_id_only() -> None:
    assert migrate_key("123456789012345678") == "dm:123456789012345678"
    assert migrate_key("dm:5") == "dm:5" and migrate_key("console") == "console"


def test_public_thread_key_is_public() -> None:
    assert (key_kind("pthread:5"), mode_for_key("pthread:5"), key_id("pthread:5")) == ("pthread", "public", 5)
    assert make_key("pthread", 6) == "pthread:6"
