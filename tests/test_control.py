import pytest

from conftest import activate


async def test_message_menu_saves_preview_once(app):
    control = app.control
    for text, action in [
        ("", "addmessage"),
        ("@friend", None),
        ("Hello\nTake care.", None),
        ("", "trigger:schedule"),
        ("1 月", None),
        ("1", None),
        ("12", None),
    ]:
        reply, _ = await control.handle(10, text, action)
    assert "Hello\nTake care." in reply
    assert "12" in reply
    assert not app.store.rows("SELECT * FROM messages")
    await control.handle(10, callback="save_message")
    await control.handle(10, callback="save_message")
    messages = app.store.rows("SELECT * FROM messages")
    assert len(messages) == 1
    assert messages[0] == {
        "id": 1,
        "recipient": 20,
        "body": "Hello\nTake care.",
        "trigger": "schedule",
        "delay": 1,
        "unit": "months",
        "interval": 1,
        "count": 12,
    }
    assert app.store.get("draft") is None


async def test_edit_message_keeps_id(app):
    ident = app.store.add_message(20, "Old", "activation")
    for text, action in [
        ("", f"edit:{ident}"),
        ("@friend", None),
        ("New", None),
        ("", "trigger:first_dm"),
        ("", "save_message"),
    ]:
        await app.control.handle(10, text, action)
    assert app.store.rows("SELECT id,body,trigger FROM messages") == [
        {"id": ident, "body": "New", "trigger": "first_dm"}
    ]


@pytest.mark.parametrize(
    "action", ["addmessage", "generatecode", "pause", "arm", "remove_group:-100123", "person:20"]
)
async def test_non_owner_cannot_change_settings(app, action):
    before = app.store.get("recovery")
    await app.control.handle(20, callback=action)
    assert app.store.get("recovery") == before
    assert app.store.state["mode"] == "paused"
    assert app.store.get("draft") is None
    assert app.store.rows("SELECT * FROM groups")


async def test_active_owner_must_use_recovery_to_pause(app):
    await activate(app)
    await app.control.handle(10, "/pause")
    await app.control.handle(10, callback="generatecode")
    assert app.engine.active()
    assert app.store.get("recovery")
    await app.control.handle(10, "/recover " + app.code)
    assert app.store.state["mode"] == "paused"
    await app.control.handle(10, "/arm")
    assert app.store.state["mode"] == "paused"


async def test_raw_code_from_relative_works_while_owner_edits(app):
    await app.control.handle(10, callback="addmessage")
    await app.control.handle(20, app.code)
    assert app.store.get("recovery") is None
    assert app.store.get("draft") is None


async def test_recovery_attempts_throttled_and_hash_only(app):
    assert app.code not in str(app.store.rows("SELECT * FROM settings"))
    assert not app.store.recover(20, "wrong-code-that-is-long", app.now.value)
    assert not app.store.recover(20, app.code, app.now.value)
    app.now.value += 2
    assert app.store.recover(20, app.code, app.now.value)


async def test_new_code_must_be_saved_before_arming(app):
    await app.control.handle(10, callback="generatecode")
    await app.control.handle(10, "/arm")
    assert app.store.state["mode"] == "paused"
    await app.control.handle(10, callback="savedcode:" + app.store.get("recovery")["salt"])
    await app.control.handle(10, "/arm")
    assert app.store.state["mode"] == "armed"


async def test_trusted_contacts_can_discover_only_allowlisted_groups(app):
    await activate(app)
    text, _ = await app.control.handle(20, "/groups")
    assert "-100123" in text
    text, _ = await app.control.handle(30, "/groups")
    assert "-100123" not in text


def test_message_limits_count_utf16_units(app):
    with pytest.raises(ValueError):
        app.store.add_message(20, "🙂" * 2049, "activation")
    with pytest.raises(ValueError):
        app.store.add_message(0, "Hi", "activation")
    with pytest.raises(ValueError):
        app.store.add_message(20, "Hi", "schedule", interval=0, count=2)


async def test_message_body_is_not_interpreted_as_a_menu_action(app):
    for text, action in [("", "addmessage"), ("@friend", None), ("arm", None)]:
        await app.control.handle(10, text, action)
    assert app.store.state["mode"] == "paused"
    assert app.store.get("draft")["message"]["body"] == "arm"


async def test_old_confirmation_cannot_confirm_new_recovery_code(app):
    old = app.store.get("recovery")["salt"]
    await app.control.handle(10, callback="generatecode")
    await app.control.handle(10, callback="savedcode:" + old)
    assert not app.store.get("recovery_saved")


def test_deleted_message_ids_are_not_reused(app):
    old = app.store.add_message(20, "Old", "activation")
    with app.store.db:
        app.store.db.execute("DELETE FROM messages WHERE id=?", (old,))
    assert app.store.add_message(20, "New", "activation") != old
