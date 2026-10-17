import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from last_heartbeat.runtime import Runtime
from last_heartbeat.engine import DAY, Rejected
from conftest import activate


def runtime(app):
    result = Runtime.__new__(Runtime)
    result.store, result.config, result.engine = app.store, app.config, app.engine
    result.control, result.telegram, result.bot_id = app.control, app.telegram, 42
    return result


def update(actor=10, **extra):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": actor},
            "chat": {"id": actor, "type": "private"},
            "text": "/heartbeat",
            "date": 1_800_000_100,
            **extra,
        },
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"forward_origin": {"type": "user"}},
        {"via_bot": {"id": 5}},
        {"edit_date": 1},
        {"chat": {"id": -5, "type": "group"}},
    ],
)
async def test_forwarded_edited_or_group_commands_do_not_count_as_heartbeat(app, extra):
    await app.engine.arm()
    original = app.store.state["heartbeat"]
    app.now.value += 100
    assert await runtime(app).handle_update(update(**extra)) is None
    assert app.store.state["heartbeat"] == original


async def test_owner_callback_counts_but_forged_message_does_not(app):
    await app.engine.arm()
    original = app.store.state["heartbeat"]
    app.now.value += 100
    event = {
        "update_id": 1,
        "callback_query": {
            "id": "abc",
            "from": {"id": 10},
            "data": "heartbeat",
            "message": {"from": {"id": 99}, "chat": {"id": 10, "type": "private"}},
        },
    }
    handler = runtime(app)
    assert await handler.handle_update(event) is None
    assert app.store.state["heartbeat"] == original
    event["callback_query"]["message"]["from"]["id"] = 42
    await handler.handle_update(event)
    assert app.store.state["heartbeat"] == app.now.value


async def test_sync_recovers_before_resuming_delivery(app):
    await activate(app)
    handler = runtime(app)

    async def dialogs():
        yield SimpleNamespace(
            is_user=True,
            entity=SimpleNamespace(bot=False),
            id=20,
            date=datetime.fromtimestamp(app.now.value, timezone.utc),
        )

    async def messages(_):
        yield SimpleNamespace(
            date=datetime.fromtimestamp(app.now.value, timezone.utc),
            out=False,
            fwd_from=None,
            via_bot_id=None,
            action=None,
            raw_text=app.code,
        )

    handler.telegram.client = SimpleNamespace(iter_dialogs=dialogs, iter_messages=messages)
    await handler.sync_inbox()
    assert app.store.state["mode"] == "paused"
    assert app.store.get("recovery") is None


async def test_ordinary_long_word_still_triggers_first_dm(app):
    app.store.add_message(20, "Hello", "first_dm")
    await activate(app)
    handler = runtime(app)
    event = SimpleNamespace(
        is_private=True,
        out=False,
        sender_id=20,
        raw_text="an-ordinary-long-word-without-spaces",
        message=SimpleNamespace(
            fwd_from=None,
            via_bot_id=None,
            action=None,
            date=datetime.fromtimestamp(app.now.value, timezone.utc),
        ),
        get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)),
        reply=AsyncMock(),
    )
    await handler.user_message(event)
    assert len(app.store.rows("SELECT * FROM deliveries")) == 1
    event.reply.assert_not_called()


async def test_deadline_waits_for_both_telegram_connections_to_catch_up(app, monkeypatch):
    await app.engine.arm()
    handler = runtime(app)
    handler.stop = asyncio.Event()
    handler.last_bot = handler.last_health = time.monotonic()
    handler.last_bot_at = handler.last_account_at = app.now.value
    app.now.value += 3 * DAY

    async def one_tick(_):
        handler.stop.set()

    monkeypatch.setattr("last_heartbeat.runtime.asyncio.sleep", one_tick)
    await handler.work()
    assert not app.engine.active()
    handler.stop.clear()
    handler.last_account_at = app.now.value
    await handler.work()
    assert not app.engine.active()
    handler.stop.clear()
    handler.last_bot_at = app.now.value
    await handler.work()
    assert app.engine.active()


async def test_expired_callback_ack_does_not_drop_heartbeat_or_reply(app):
    await app.engine.arm()
    app.now.value += 100
    handler = runtime(app)
    handler.stop = asyncio.Event()
    handler.bot_ready = False
    event = {
        "update_id": 4,
        "callback_query": {
            "id": "old",
            "from": {"id": 10},
            "data": "heartbeat",
            "message": {"from": {"id": 42}, "chat": {"id": 10, "type": "private"}},
        },
    }

    async def bot_call(method, **_):
        if method == "getUpdates":
            return [event]
        raise Rejected("BotAPI400")

    async def send_reply(*_):
        handler.stop.set()

    handler.bot = SimpleNamespace(call=AsyncMock(side_effect=bot_call))
    handler.send_reply = AsyncMock(side_effect=send_reply)
    await handler.poll()
    assert app.store.state["heartbeat"] == app.now.value
    assert app.store.get("bot_offset") == 5
    handler.send_reply.assert_awaited_once()
