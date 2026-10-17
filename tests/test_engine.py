import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from last_heartbeat.engine import DAY, Retry, scheduled_at
from last_heartbeat.store import Store
from conftest import activate


async def test_heartbeat_extends_deadline_and_only_owner_can_confirm(app):
    await app.engine.arm()
    start = app.now.value
    app.now.value += DAY
    await app.control.handle(20, "/heartbeat")
    assert app.store.state["heartbeat"] == start
    await app.control.handle(10, "/heartbeat", when=app.now.value)
    assert app.store.state["deadline"] == start + 4 * DAY
    app.engine.heartbeat(start + 1)
    assert app.store.state["deadline"] == start + 4 * DAY


async def test_takeover_at_72_hours_delivers_and_revokes_once(app):
    app.store.add_message(20, "Goodbye", "activation")
    await app.engine.arm()
    app.now.value += 3 * DAY - 1
    await app.engine.tick()
    assert not app.engine.active()
    app.telegram.send.assert_not_called()
    app.now.value += 1
    await app.engine.tick()
    assert app.engine.active()
    app.telegram.revoke_sessions.assert_awaited_once()
    app.telegram.send.assert_awaited_once()
    await app.engine.tick()
    assert app.telegram.send.await_count == 1
    app.now.value += 30
    await app.engine.tick()
    assert app.telegram.revoke_sessions.await_count == 2


async def test_reminder_dedup_and_only_latest_overdue_reminder(app):
    await app.engine.arm()
    app.now.value += 60 * 3600
    await app.engine.tick()
    await app.engine.tick()
    rows = app.store.rows("SELECT * FROM notices")
    assert len(rows) == 1
    assert rows[0]["key"].endswith(":60")


async def test_offline_deadline_gets_grace_after_backlog_sync(app):
    await app.engine.arm()
    app.engine.ready = False
    app.now.value += 4 * DAY
    await app.engine.tick()
    assert app.store.state["mode"] == "armed"
    app.engine.connected()
    assert app.store.state["deadline"] == app.now.value + DAY
    await app.engine.tick()
    assert not app.engine.active()
    app.now.value += DAY
    await app.engine.tick()
    assert app.engine.active()


async def test_recent_queued_heartbeat_prevents_grace_and_takeover(app):
    await app.engine.arm()
    app.engine.ready = False
    app.now.value += 4 * DAY
    app.engine.heartbeat(app.now.value - 3600)
    app.engine.connected()
    assert app.store.state["deadline"] == app.now.value - 3600 + 3 * DAY
    assert not app.store.rows("SELECT * FROM notices WHERE key LIKE 'grace:%'")


async def test_arm_requires_old_session_and_recovery(app):
    app.telegram.session_age.return_value = DAY - 1
    with pytest.raises(ValueError, match="24"):
        await app.engine.arm()
    app.telegram.session_age.return_value = DAY
    app.store.recover(20, app.code, app.now.value)
    with pytest.raises(ValueError, match="解除碼"):
        await app.engine.arm()


async def test_recovery_while_arm_awaits_cannot_rearm(app):
    async def age(_):
        assert app.store.recover(20, app.code, app.now.value)
        return DAY

    app.telegram.session_age.side_effect = age
    with pytest.raises(ValueError, match="變更"):
        await app.engine.arm()
    assert app.store.state["mode"] == "paused"


async def test_first_dm_specific_wins_over_fallback_and_runs_once(app):
    app.store.add_message(20, "For you", "first_dm")
    app.store.add_message(0, "For others", "first_dm")
    await activate(app)
    app.engine.incoming(20, app.now.value - 1)
    assert not app.store.rows("SELECT * FROM deliveries")
    for peer in (20, 20, 30):
        app.engine.incoming(peer, app.now.value)
    await app.engine.tick()
    assert [
        (r["peer"], r["body"]) for r in app.store.rows("SELECT * FROM deliveries ORDER BY peer")
    ] == [(20, "For you"), (30, "For others")]
    assert app.telegram.send.await_count == 2


async def test_recovery_stops_every_pending_action_and_is_single_use(app):
    app.store.add_message(20, "Later", "schedule", delay=1)
    await activate(app)
    token, _ = await app.engine.prepare_handover(20, -100123, 30)
    app.engine.confirm_handover(20, token)
    assert app.store.recover(999, app.code, app.now.value)
    assert not app.store.recover(999, app.code, app.now.value)
    app.now.value += 2 * DAY
    await app.engine.tick()
    assert app.store.state["mode"] == "paused"
    assert app.store.rows("SELECT state FROM deliveries") == [{"state": "cancelled"}]
    assert app.store.rows("SELECT state FROM handovers") == [{"state": "cancelled"}]
    app.telegram.send.assert_not_called()
    app.telegram.handover.assert_not_called()
    assert app.telegram.revoke_sessions.await_count == 1


async def test_recovery_during_recipient_lookup_prevents_send(app):
    app.store.add_message(20, "Goodbye", "activation")

    async def send(_, allowed):
        app.store.recover(20, app.code, app.now.value)
        assert not allowed()
        return None

    app.telegram.send.side_effect = send
    await activate(app)
    assert app.store.rows("SELECT state FROM deliveries") == [{"state": "cancelled"}]


async def test_disconnect_before_send_keeps_delivery_pending(app):
    app.store.add_message(20, "Goodbye", "activation")

    async def send(_, allowed):
        app.engine.ready = False
        assert not allowed()
        return None

    app.telegram.send.side_effect = send
    await activate(app)
    assert app.store.rows("SELECT state FROM deliveries") == [{"state": "pending"}]
    app.telegram.send.side_effect = None
    app.engine.connected()
    await app.engine.tick()
    assert app.store.rows("SELECT state FROM deliveries") == [{"state": "sent"}]


async def test_flood_wait_reuses_random_id_without_changing_schedule(app):
    app.store.add_message(20, "Goodbye", "activation")
    app.telegram.send.side_effect = [Retry(60), 101]
    await activate(app)
    original = app.store.rows("SELECT * FROM deliveries")[0]
    app.now.value += 59
    await app.engine.tick()
    assert app.telegram.send.await_count == 1
    app.now.value += 1
    await app.engine.tick()
    sent = app.store.rows("SELECT * FROM deliveries")[0]
    assert sent["state"] == "sent"
    assert sent["due"] == original["due"]
    assert sent["random_id"] == original["random_id"]


async def test_timeout_is_not_retried_even_after_reopening_store(app):
    app.store.add_message(20, "Goodbye", "activation")
    app.telegram.send.side_effect = TimeoutError()
    await activate(app)
    assert app.store.rows("SELECT state FROM deliveries") == [{"state": "uncertain"}]
    app.store.close()
    app.store = Store(app.config.data_dir / "heartbeat.db")
    app.engine.store = app.store
    app.now.value += DAY
    await app.engine.tick()
    assert app.telegram.send.await_count == 1


async def test_cancelled_send_becomes_uncertain_on_restart(app):
    app.store.add_message(20, "Goodbye", "activation")
    app.telegram.send.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await activate(app)
    app.store.close()
    app.store = Store(app.config.data_dir / "heartbeat.db")
    assert app.store.rows("SELECT state FROM deliveries") == [{"state": "uncertain"}]


def test_month_end_uses_original_day_each_month():
    date = datetime(2028, 1, 31, 12, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    message = {"delay": 1, "unit": "months", "interval": 1}
    dates = [
        datetime.fromtimestamp(
            scheduled_at(date.timestamp(), message, n, "Asia/Hong_Kong"), date.tzinfo
        )
        for n in (0, 1)
    ]
    assert dates == [date.replace(month=2, day=29), date.replace(month=3)]


async def test_only_latest_missed_recurring_delivery_is_sent(app):
    app.store.add_message(
        20, "Thinking of you", "schedule", delay=1, unit="months", interval=1, count=3
    )
    await activate(app)
    app.now.value += 100 * DAY
    await app.engine.tick()
    assert [r["state"] for r in app.store.rows("SELECT state FROM deliveries ORDER BY due")] == [
        "skipped",
        "skipped",
        "sent",
    ]
    app.telegram.send.assert_awaited_once()


async def test_handover_confirmation_bound_to_actor_epoch_and_expiry(app):
    await activate(app)
    token, _ = await app.engine.prepare_handover(20, -100123, 30)
    with pytest.raises(ValueError):
        app.engine.confirm_handover(30, token)
    app.now.value += 301
    with pytest.raises(ValueError):
        app.engine.confirm_handover(20, token)
    token, _ = await app.engine.prepare_handover(20, -100123, 30)
    app.engine.confirm_handover(20, token)
    with pytest.raises(ValueError):
        app.engine.confirm_handover(20, token)
    await app.engine.tick()
    app.telegram.handover.assert_awaited_once()


async def test_unlisted_actor_or_group_cannot_handover(app):
    await activate(app)
    for actor, group in ((30, -100123), (20, -100456)):
        with pytest.raises(ValueError):
            await app.engine.prepare_handover(actor, group, 30)
    app.telegram.check_handover.assert_not_called()


async def test_handover_rechecks_authorization_after_lookup(app):
    await activate(app)

    async def check(*_):
        app.store.recover(20, app.code, app.now.value)
        return "Group"

    app.telegram.check_handover.side_effect = check
    with pytest.raises(ValueError):
        await app.engine.prepare_handover(20, -100123, 30)
    assert not app.store.rows("SELECT * FROM handovers")


async def test_uncertain_handover_checks_rights_without_repeating_write(app):
    await activate(app)
    token, _ = await app.engine.prepare_handover(20, -100123, 30)
    app.engine.confirm_handover(20, token)
    app.telegram.handover.side_effect = TimeoutError()
    await app.engine.tick()
    app.telegram.reconcile_handover.return_value = "owner"
    await app.engine.tick()
    assert app.store.rows("SELECT state,result FROM handovers") == [
        {"state": "done", "result": "owner"}
    ]
    app.telegram.handover.assert_awaited_once()


async def test_session_check_is_not_blocked_by_message_delivery(app):
    app.store.add_message(20, "Hello", "activation")
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(*_):
        started.set()
        await finish.wait()
        return 55

    app.telegram.send.side_effect = send
    task = asyncio.create_task(activate(app))
    await asyncio.wait_for(started.wait(), 2)
    app.engine.session_check_at = 0
    await app.engine.check_sessions()
    assert app.telegram.revoke_sessions.await_count == 2
    finish.set()
    await task


async def test_new_login_does_not_bypass_telegram_rate_limit(app):
    app.telegram.revoke_sessions.side_effect = [Retry(60), "done"]
    await activate(app)
    app.engine.session_check_at = 0
    await app.engine.check_sessions()
    assert app.telegram.revoke_sessions.await_count == 1
    app.now.value += 60
    await app.engine.check_sessions()
    assert app.telegram.revoke_sessions.await_count == 2
