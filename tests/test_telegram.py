from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from last_heartbeat.engine import Rejected, Retry
from last_heartbeat.telegram import Telegram


def adapter(app):
    telegram = Telegram.__new__(Telegram)
    telegram.config = app.config
    telegram.client = AsyncMock()
    return telegram


def channel(creator=True, megagroup=True):
    return types.Channel(
        id=123,
        title="Group",
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(timezone.utc),
        creator=creator,
        megagroup=megagroup,
        broadcast=not megagroup,
        access_hash=456,
    )


async def test_device_revocation_never_logs_out_own_session(app):
    telegram = adapter(app)
    telegram.call = AsyncMock(
        side_effect=[
            SimpleNamespace(
                authorizations=[
                    SimpleNamespace(current=True, hash=1),
                    SimpleNamespace(current=False, hash=2),
                    SimpleNamespace(current=False, hash=3),
                ]
            ),
            True,
            True,
        ]
    )
    await telegram.revoke_sessions(lambda: True)
    calls = [a.args[0] for a in telegram.call.await_args_list]
    assert [r.hash for r in calls[1:]] == [2, 3]


async def test_recovery_during_revocation_stops_following_devices(app):
    telegram = adapter(app)
    allowed = True

    async def call(request):
        nonlocal allowed
        if isinstance(request, functions.account.GetAuthorizationsRequest):
            return SimpleNamespace(
                authorizations=[
                    SimpleNamespace(current=False, hash=2),
                    SimpleNamespace(current=False, hash=3),
                ]
            )
        allowed = False
        return True

    telegram.call = AsyncMock(side_effect=call)
    await telegram.revoke_sessions(lambda: allowed)
    assert telegram.call.await_count == 2


async def test_send_uses_durable_random_id_and_plain_text(app):
    telegram = adapter(app)
    telegram.client.get_input_entity.return_value = types.InputPeerUser(20, 456)
    telegram.call = AsyncMock(
        return_value=types.Updates(
            updates=[types.UpdateMessageID(id=55, random_id=99)],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=1,
        )
    )
    result = await telegram.send({"peer": 20, "body": "**literal**", "random_id": 99}, lambda: True)
    assert result == 55
    request = telegram.call.await_args.args[0]
    assert request.message == "**literal**"
    assert request.random_id == 99
    assert not request.entities


async def test_send_cancelled_after_lookup_makes_no_write(app):
    telegram = adapter(app)
    telegram.call = AsyncMock()
    assert await telegram.send({"peer": 20}, lambda: False) is None
    telegram.call.assert_not_called()


def test_admin_rights_exclude_anonymity_and_match_group_type(app):
    telegram = adapter(app)
    rights = telegram.admin_rights(channel())
    assert rights.add_admins and rights.ban_users and rights.manage_topics and rights.manage_ranks
    assert not rights.anonymous and not rights.post_messages
    assert rights.post_stories and rights.edit_stories and rights.delete_stories
    assert rights.manage_linked_peers and rights.manage_welcome_messages
    rights = telegram.admin_rights(channel(megagroup=False))
    assert rights.post_messages and rights.edit_stories and rights.manage_direct_messages
    assert not rights.manage_topics and not rights.anonymous


def test_admin_cannot_grant_rights_they_do_not_have(app):
    telegram = adapter(app)
    entity = channel(creator=False)
    entity.admin_rights = types.ChatAdminRights(add_admins=True, invite_users=True, anonymous=True)
    rights = telegram.admin_rights(entity)
    assert rights.add_admins and rights.invite_users
    assert not rights.ban_users and not rights.anonymous


@pytest.mark.parametrize("basic", [False, True])
async def test_ownership_transfer_uses_current_api(app, monkeypatch, basic):
    telegram = adapter(app)
    path = app.config.data_dir / "2fa"
    path.write_text("secret", encoding="utf-8")
    telegram.config = replace(app.config, password_file=path)
    entity = (
        types.Chat(
            123, "Group", types.ChatPhotoEmpty(), 3, datetime.now(timezone.utc), 1, creator=True
        )
        if basic
        else channel()
    )
    telegram.check_handover = AsyncMock()
    telegram.client.get_entity.return_value = entity
    telegram.client.get_input_entity.return_value = types.InputUser(30, 1)
    telegram.call = AsyncMock(side_effect=[SimpleNamespace(has_password=True), True])
    monkeypatch.setattr(
        "last_heartbeat.telegram.password.compute_check", lambda *_: types.InputCheckPasswordEmpty()
    )
    assert await telegram.handover(-123, 30, lambda: True) == "owner"
    request = telegram.call.await_args.args[0]
    assert isinstance(request, functions.messages.EditChatCreatorRequest)
    assert request.peer is entity


async def test_2fa_rejection_falls_back_to_nonanonymous_admin(app, monkeypatch):
    telegram = adapter(app)
    path = app.config.data_dir / "2fa"
    path.write_text("secret", encoding="utf-8")
    telegram.config = replace(app.config, password_file=path)
    telegram.check_handover = AsyncMock()
    telegram.client.get_entity.return_value = channel()
    telegram.client.get_input_entity.return_value = types.InputUser(30, 1)
    telegram.call = AsyncMock(
        side_effect=[SimpleNamespace(has_password=True), Rejected("PasswordTooFreshError"), True]
    )
    monkeypatch.setattr(
        "last_heartbeat.telegram.password.compute_check", lambda *_: types.InputCheckPasswordEmpty()
    )
    assert await telegram.handover(-123, 30, lambda: True) == "admin"
    request = telegram.call.await_args.args[0]
    assert isinstance(request, functions.channels.EditAdminRequest)
    assert request.admin_rights.add_admins and not request.admin_rights.anonymous


async def test_timed_out_transfer_checks_owner_and_never_blindly_promotes(app, monkeypatch):
    telegram = adapter(app)
    path = app.config.data_dir / "2fa"
    path.write_text("secret", encoding="utf-8")
    telegram.config = replace(app.config, password_file=path)
    telegram.check_handover = AsyncMock()
    telegram.client.get_entity.return_value = channel()
    telegram.client.get_permissions.return_value = SimpleNamespace(is_creator=False)
    telegram.call = AsyncMock(side_effect=[SimpleNamespace(has_password=True), TimeoutError()])
    monkeypatch.setattr(
        "last_heartbeat.telegram.password.compute_check", lambda *_: types.InputCheckPasswordEmpty()
    )
    with pytest.raises(TimeoutError):
        await telegram.handover(-123, 30, lambda: True)
    assert telegram.call.await_count == 2


async def test_flood_wait_is_retryable_but_server_error_is_uncertain(app):
    telegram = adapter(app)
    telegram.client.side_effect = errors.FloodWaitError(None, capture=42)
    with pytest.raises(Retry) as result:
        await telegram.call(functions.updates.GetStateRequest())
    assert result.value.seconds == 42
    telegram.client.side_effect = errors.ServerError(None, "INTERNAL", 500)
    with pytest.raises(TimeoutError):
        await telegram.call(functions.updates.GetStateRequest())
