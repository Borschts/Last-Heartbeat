import asyncio
import logging
import time

from telethon import events, functions, types

from .control import Control, button
from .engine import Engine, Rejected, Retry
from .telegram import BotAPI, Telegram


log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.telegram = Telegram(config)
        self.bot = BotAPI(config.bot_token)
        self.engine = Engine(store, self.telegram, config)
        self.control = Control(self.engine)
        self.bot_ready = False
        self.last_bot = 0
        self.last_health = 0
        self.last_bot_at = 0
        self.last_account_at = 0
        self.bot_id = 0
        self.stop = asyncio.Event()

    async def run(self):
        client = self.telegram.client
        client.add_event_handler(self.user_message, events.NewMessage(incoming=True))
        client.add_event_handler(self.authorization, events.Raw(types.UpdateNewAuthorization))
        tasks = []
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Run last-heartbeat login first")
            owner = await client.get_me()
            if owner.id != self.config.owner_id or owner.bot:
                raise RuntimeError("OWNER_ID does not match the logged-in Telegram account")
            me = await self.bot.call("getMe")
            self.bot_id = me["id"]
            if (await self.bot.call("getWebhookInfo")).get("url"):
                raise RuntimeError(
                    "This bot has a webhook. Use a dedicated bot or remove its webhook first"
                )
            await client.get_dialogs(limit=100)
            log.info("Connected. Open @%s to configure Heartbeat.", me["username"])
            tasks = [
                asyncio.create_task(loop())
                for loop in (self.poll, self.health, self.work, self.notices, self.sessions)
            ]
            stopping = asyncio.create_task(self.stop.wait())
            tasks.append(stopping)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if not self.stop.is_set():
                for task in done:
                    task.result()
                raise RuntimeError("A service task stopped unexpectedly")
        finally:
            self.engine.ready = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.disconnect()
            await self.bot.close()

    async def poll(self):
        while not self.stop.is_set():
            try:
                updates = await self.bot.call(
                    "getUpdates",
                    offset=self.store.get("bot_offset", 0),
                    timeout=10 if self.bot_ready else 0,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    if update["update_id"] < self.store.get("bot_offset", 0):
                        continue
                    reply = None
                    try:
                        reply = await self.handle_update(update)
                    except Exception as error:
                        log.warning("Control request failed: %s", type(error).__name__)
                    finally:
                        with self.store.db:
                            self.store.put("bot_offset", update["update_id"] + 1)
                    if reply:
                        actor, text, buttons, query = reply
                        try:
                            if query:
                                try:
                                    await self.bot.call(
                                        "answerCallbackQuery", callback_query_id=query
                                    )
                                except Rejected:
                                    pass
                            await self.send_reply(actor, text, buttons)
                        except Exception as error:
                            log.warning("Control reply failed: %s", type(error).__name__)
                self.last_bot = time.monotonic()
                if len(updates) < 100:
                    self.bot_ready = True
                    self.last_bot_at = self.engine.clock()
            except Exception as error:
                self.bot_ready = self.engine.ready = False
                log.warning("Control connection failed: %s", type(error).__name__)
                await asyncio.sleep(error.seconds if isinstance(error, Retry) else 5)

    async def handle_update(self, update):
        query = update.get("callback_query")
        message = query.get("message", {}) if query else update.get("message", {})
        sender = query.get("from", {}) if query else message.get("from", {})
        actor = sender.get("id")
        if (
            not actor
            or sender.get("is_bot")
            or message.get("chat", {}).get("type") != "private"
            or message.get("chat", {}).get("id") != actor
            or message.get("forward_origin")
            or message.get("via_bot")
            or message.get("edit_date")
        ):
            return None
        if query and message.get("from", {}).get("id") != self.bot_id:
            return None
        with self.store.db:
            self.store.put(f"bot_started:{actor}", True)
        text = message.get("text", "") if not query else ""
        if not query and not text:
            return actor, "請使用文字或選單按鈕。", None, None
        body, buttons = await self.control.handle(
            actor,
            text,
            query.get("data") if query else None,
            self.engine.clock() if query else message.get("date"),
        )
        return actor, body, buttons, query["id"] if query else None

    async def send_reply(self, actor, text, buttons=None):
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)]
        for i, chunk in enumerate(chunks):
            await self.bot.send(actor, chunk, buttons if i == len(chunks) - 1 else None)

    async def health(self):
        while not self.stop.is_set():
            try:
                current = time.monotonic()
                if current - self.last_health > 45 or current - self.last_bot > 45:
                    self.engine.ready = False
                await self.telegram.call(functions.updates.GetStateRequest())
                self.last_account_at = self.engine.clock()
                if not self.bot_ready or time.monotonic() - self.last_bot > 45:
                    self.engine.ready = False
                elif not self.engine.ready:
                    await self.sync_inbox()
                    if self.bot_ready and time.monotonic() - self.last_bot <= 45:
                        self.engine.connected()
                self.last_health = time.monotonic()
                if self.engine.ready:
                    with self.store.db:
                        self.store.put("health", self.engine.clock())
            except Exception as error:
                self.engine.ready = False
                log.warning("Account connection failed: %s", type(error).__name__)
                if isinstance(error, Rejected) and any(
                    word in str(error) for word in ("AuthKey", "SessionRevoked", "UserDeactivated")
                ):
                    with self.store.db:
                        self.store.notice(
                            "account-unavailable",
                            "userbot 的登入已失效，請在主機重新登入。",
                            self.engine.clock(),
                        )
            await asyncio.sleep(10)

    async def work(self):
        while not self.stop.is_set():
            try:
                if time.monotonic() - min(self.last_health, self.last_bot) > 45:
                    self.engine.ready = False
                state = self.store.state
                deadline_pending = (
                    state["mode"] == "armed"
                    and self.engine.clock() >= state["deadline"]
                    and min(self.last_bot_at, self.last_account_at) < state["deadline"]
                )
                if not deadline_pending:
                    await self.engine.tick()
            except Exception as error:
                log.warning("Worker failed: %s", type(error).__name__)
            await asyncio.sleep(1)

    async def notices(self):
        while not self.stop.is_set():
            if self.bot_ready:
                for notice in self.store.rows(
                    "SELECT * FROM notices WHERE state='pending' AND due<=? ORDER BY due LIMIT 5",
                    (self.engine.clock(),),
                ):
                    state = self.store.state
                    stale = notice["key"].startswith("reminder:") and not notice["key"].startswith(
                        f"reminder:{state['epoch']}:{state['heartbeat']}:"
                    )
                    stale |= (
                        notice["key"].startswith(("reminder:", "grace:"))
                        and state["mode"] != "armed"
                    )
                    if stale:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE notices SET state='cancelled' WHERE key=?", (notice["key"],)
                            )
                        continue
                    try:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE notices SET state='sending' WHERE key=?", (notice["key"],)
                            )
                        buttons = (
                            [[button("Heartbeat", "heartbeat")]]
                            if notice["key"].startswith(("reminder:", "grace:"))
                            else None
                        )
                        await self.bot.send(self.config.owner_id, notice["body"], buttons)
                        result = "sent"
                    except Retry as error:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE notices SET state='pending',due=? WHERE key=?",
                                (self.engine.clock() + error.seconds, notice["key"]),
                            )
                        break
                    except Rejected:
                        result = "failed"
                    except Exception:
                        result = "uncertain"
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE notices SET state=? WHERE key=?", (result, notice["key"])
                        )
            await asyncio.sleep(2)

    async def authorization(self, _):
        self.engine.session_check_at = 0
        await self.engine.check_sessions()

    async def sessions(self):
        while not self.stop.is_set():
            await self.engine.check_sessions()
            await asyncio.sleep(1)

    def recovery_text(self, actor, text):
        words = text.strip().split()
        code = words[1] if len(words) == 2 and words[0] == "/recover" else text.strip()
        if 20 <= len(code) <= 256 and not any(c.isspace() for c in code):
            return self.store.recover(actor, code, self.engine.clock())
        return False

    async def sync_inbox(self):
        state = self.store.state
        if state["mode"] == "paused":
            return
        since = max(state["heartbeat"], self.store.get("health", state["heartbeat"]) - 60)
        async for dialog in self.telegram.client.iter_dialogs():
            if (
                not dialog.is_user
                or getattr(dialog.entity, "bot", False)
                or dialog.id in (self.config.owner_id, 777000)
            ):
                continue
            if not dialog.date or dialog.date.timestamp() < since:
                continue
            async for message in self.telegram.client.iter_messages(dialog.id):
                if message.date.timestamp() < since:
                    break
                if message.out or message.fwd_from or message.via_bot_id or message.action:
                    continue
                if self.recovery_text(dialog.id, message.raw_text or ""):
                    continue
                self.engine.incoming(dialog.id, message.date.timestamp())

    async def user_message(self, event):
        try:
            message = event.message
            if (
                not event.is_private
                or event.out
                or message.fwd_from
                or message.via_bot_id
                or message.action
            ):
                return
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False) or event.sender_id == 777000:
                return
            text = event.raw_text.strip()
            if not text.startswith("/") and self.recovery_text(event.sender_id, text):
                await event.reply("已解除接管。你現在可以重新登入帳號。", parse_mode=None)
                return
            if text.startswith("/"):
                if text.split()[0] in ("/recover", "/handover", "/handover_status", "/groups"):
                    body, buttons = await self.control.handle(
                        event.sender_id, text, when=message.date.timestamp()
                    )
                    if buttons and buttons[0][0]["callback_data"].startswith("handover:"):
                        token = buttons[0][0]["callback_data"].split(":", 1)[1]
                        body += f"\n發送 /confirm {token} 確認。"
                    await event.reply(body, parse_mode=None)
                elif text.startswith("/confirm "):
                    try:
                        self.engine.confirm_handover(event.sender_id, text.split(maxsplit=1)[1])
                        body = "已收到交接確認。可用 /handover_status 查看結果。"
                    except ValueError as error:
                        body = str(error)
                    await event.reply(body, parse_mode=None)
                return
            self.engine.incoming(event.sender_id, message.date.timestamp())
        except Exception as error:
            log.warning("Private message handler failed: %s", type(error).__name__)
