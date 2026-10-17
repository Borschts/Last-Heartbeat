import asyncio
import inspect

import httpx
from telethon import TelegramClient, errors, functions, password, types, utils

from .engine import Rejected, Retry


class Telegram:
    def __init__(self, config):
        self.config = config
        self.client = TelegramClient(
            str(config.data_dir / "account"),
            config.api_id,
            config.api_hash,
            device_model="Last Heartbeat",
            request_retries=0,
            raise_last_call_error=True,
            flood_sleep_threshold=0,
            connection_retries=3,
            catch_up=True,
        )

    async def call(self, request):
        try:
            return await asyncio.wait_for(self.client(request), 25)
        except errors.FloodWaitError as error:
            raise Retry(error.seconds) from None
        except errors.RPCError as error:
            if error.code >= 500:
                raise TimeoutError(type(error).__name__) from None
            raise Rejected(type(error).__name__) from None

    async def resolve(self, value):
        value = value.strip()
        reference = int(value) if value.lstrip("-").isdigit() else value
        try:
            entity = await self.client.get_entity(reference)
        except errors.FloodWaitError as error:
            raise Retry(error.seconds) from None
        except (errors.RPCError, ValueError):
            raise ValueError(
                "找不到這個帳號或群組。請先用本人帳號開啟對話，再提供 @username 或 ID。"
            ) from None
        return entity

    async def person(self, value):
        user = await self.resolve(value)
        if not isinstance(user, types.User) or user.bot or user.deleted:
            raise ValueError("請指定有效的個人帳號。")
        return user.id, utils.get_display_name(user)

    async def group(self, value):
        entity = await self.resolve(value)
        if not isinstance(entity, (types.Channel, types.Chat)) or getattr(
            entity, "deactivated", False
        ):
            raise ValueError("請指定有效的群組或頻道。")
        rights = getattr(entity, "admin_rights", None)
        if not getattr(entity, "creator", False) and not getattr(rights, "add_admins", False):
            raise ValueError("本人帳號沒有指派管理員的權限。")
        return utils.get_peer_id(entity), entity.title

    async def session_age(self, now):
        sessions = await self.call(functions.account.GetAuthorizationsRequest())
        current = next(item for item in sessions.authorizations if item.current)
        return now - current.date_created.timestamp()

    async def revoke_sessions(self, allowed):
        sessions = await self.call(functions.account.GetAuthorizationsRequest())
        revoked = 0
        for session in sessions.authorizations:
            if session.current or not allowed():
                continue
            await self.call(functions.account.ResetAuthorizationRequest(session.hash))
            revoked += 1
        return f"已登出 {revoked} 個其他裝置"

    async def send(self, delivery, allowed):
        peer = await self.client.get_input_entity(delivery["peer"])
        if not allowed():
            return None
        result = await self.call(
            functions.messages.SendMessageRequest(
                peer=peer,
                message=delivery["body"],
                random_id=delivery["random_id"],
                no_webpage=True,
            )
        )
        if isinstance(result, types.UpdateShortSentMessage):
            return result.id
        for update in getattr(result, "updates", []):
            if (
                isinstance(update, types.UpdateMessageID)
                and update.random_id == delivery["random_id"]
            ):
                return update.id
        raise TimeoutError("MissingMessageReceipt")

    async def check_handover(self, group, target):
        entity = await self.client.get_entity(group)
        user = await self.client.get_entity(target)
        if (
            not isinstance(user, types.User)
            or user.bot
            or user.deleted
            or user.id == self.config.owner_id
        ):
            raise ValueError("接任者須為其他有效個人帳號。")
        if not isinstance(entity, (types.Channel, types.Chat)):
            raise ValueError("群組無效。")
        try:
            member = await self.client.get_permissions(entity, user)
            own = await self.client.get_permissions(entity, "me")
        except errors.UserNotParticipantError:
            raise ValueError("本人及接任者必須仍在群組內。") from None
        if getattr(member, "has_left", False) or getattr(member, "is_banned", False):
            raise ValueError("接任者不在群組內。")
        if not own.is_creator and not getattr(own, "add_admins", False):
            raise ValueError("本人帳號沒有交接權限。")
        action = "授予管理員權限（非匿名）"
        if own.is_creator and self.config.password_file:
            action = "轉移群主，Telegram 不允許時改為非匿名管理員"
        return f"{entity.title}（{group}）\n接任者：{utils.get_display_name(user)}（{target}）\n操作：{action}"

    def admin_rights(self, entity):
        group = {"manage_topics", "manage_ranks"}
        channel = {"post_messages", "edit_messages", "manage_direct_messages"}
        fields = set(inspect.signature(types.ChatAdminRights).parameters)
        applicable = fields - {"anonymous", "other"} - (channel if entity.megagroup else group)
        own = getattr(entity, "admin_rights", None)
        rights = {
            name: bool(getattr(entity, "creator", False) or getattr(own, name, False))
            for name in applicable
        }
        return types.ChatAdminRights(**rights, anonymous=False)

    async def handover(self, group, target, allowed):
        await self.check_handover(group, target)
        entity = await self.client.get_entity(group)
        user = await self.client.get_input_entity(target)
        if not allowed():
            return "cancelled"
        if entity.creator and self.config.password_file:
            try:
                secret = self.config.password_file.read_text(encoding="utf-8").strip()
                params = await self.call(functions.account.GetPasswordRequest())
                if not params.has_password:
                    raise Rejected("PasswordMissingError")
                proof = password.compute_check(params, secret)
                del secret
                if not allowed():
                    return "cancelled"
                await self.call(functions.messages.EditChatCreatorRequest(entity, user, proof))
                return "owner"
            except Rejected as error:
                fallback = (
                    "PasswordMissing",
                    "PasswordHashInvalid",
                    "PasswordTooFresh",
                    "SessionTooFresh",
                    "SrpIdInvalid",
                )
                if not str(error).startswith(fallback):
                    raise
            except (TimeoutError, OSError):
                # A timed-out transfer may already have succeeded. Never promote blindly.
                try:
                    member = await self.client.get_permissions(entity, target)
                    if member.is_creator:
                        return "owner"
                except Exception:
                    pass
                raise
        if not allowed():
            return "cancelled"
        if isinstance(entity, types.Chat):
            await self.call(functions.messages.EditChatAdminRequest(entity.id, user, True))
        else:
            await self.call(
                functions.channels.EditAdminRequest(entity, user, self.admin_rights(entity), "")
            )
        return "admin"

    async def reconcile_handover(self, op):
        entity = await self.client.get_entity(op["group_id"])
        member = await self.client.get_permissions(entity, op["target"])
        if member.is_creator:
            return "owner"
        if isinstance(entity, types.Chat):
            return "admin" if member.is_admin else None
        actual = getattr(member.participant, "admin_rights", None)
        expected = self.admin_rights(entity)
        if (
            actual
            and not actual.anonymous
            and all(
                getattr(actual, key, False)
                for key, value in expected.to_dict().items()
                if key != "_" and value is True
            )
        ):
            return "admin"
        return None


class BotAPI:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.http = httpx.AsyncClient(timeout=25)

    async def call(self, method, **data):
        response = await self.http.post(self.url + method, json=data)
        payload = response.json()
        if payload.get("ok"):
            return payload["result"]
        if payload.get("error_code") == 429:
            raise Retry(payload.get("parameters", {}).get("retry_after", 30))
        raise Rejected(f"BotAPI{payload.get('error_code', response.status_code)}")

    async def send(self, peer, text, buttons=None):
        data = {"chat_id": peer, "text": text, "link_preview_options": {"is_disabled": True}}
        if buttons:
            data["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **data)

    async def close(self):
        await self.http.aclose()
