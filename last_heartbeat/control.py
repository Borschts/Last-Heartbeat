from datetime import datetime
from zoneinfo import ZoneInfo

from .engine import Rejected, Retry


def button(label, data):
    return {"text": label, "callback_data": data}


HOME = [
    [button("Heartbeat", "heartbeat"), button("狀態", "status")],
    [button("預留訊息", "messages:0"), button("聯絡人", "people:0")],
    [button("交接群組", "groups:0"), button("解除碼", "newcode")],
    [button("啟用", "arm"), button("暫停", "pause"), button("紀錄", "activity")],
]
BACK = [[button("返回選單", "home")]]
CANCEL = [[button("取消", "cancel")]]


class Control:
    def __init__(self, engine):
        self.engine = engine
        self.store, self.telegram, self.config = engine.store, engine.telegram, engine.config

    def editable(self):
        if self.store.state["mode"] != "paused":
            raise ValueError("請先暫停 Heartbeat，再修改設定。接管期間須先用解除碼解除。")

    def draft(self, value):
        with self.store.db:
            self.store.put("draft", value)

    def status(self):
        state = self.store.state
        modes = {"paused": "已暫停", "armed": "等待 Heartbeat", "active": "接管中"}
        text = f"Last Heartbeat\n{modes[state['mode']]}"
        if state["mode"] == "armed":
            deadline = datetime.fromtimestamp(state["deadline"], ZoneInfo(self.config.timezone))
            text += f"\n下次確認期限：{deadline:%Y-%m-%d %H:%M}（{self.config.timezone}）"
        text += "\nTelegram：" + ("已連線" if self.engine.ready else "正在重新連線")
        sessions = self.store.get("sessions")
        if sessions:
            text += "\n裝置檢查：" + sessions["result"]
        if not self.store.get("recovery"):
            text += "\n請設定解除碼後再啟用。"
        return text

    async def handle(self, actor, text="", callback=None, when=None):
        try:
            return await self.route(actor, text, callback, when)
        except Retry as error:
            return (
                f"Telegram 要求等待 {error.seconds} 秒，請稍後再試。",
                BACK if actor == self.config.owner_id else None,
            )
        except (ValueError, Rejected) as error:
            return str(error), BACK if actor == self.config.owner_id else None

    async def route(self, actor, text, callback, when):
        words = text.strip().split()
        command = words[0].split("@")[0] if words else ""
        now = self.engine.clock()
        if command == "/recover" or (
            not callback
            and not command.startswith("/")
            and len(words) == 1
            and 20 <= len(words[0]) <= 256
            and (actor != self.config.owner_id or not self.store.get("draft"))
        ):
            code = (
                words[1]
                if command == "/recover" and len(words) == 2
                else (words[0] if command != "/recover" else "")
            )
            if not code:
                return "用法：/recover 解除碼", None
            if self.store.recover(actor, code, now):
                self.draft(None)
                return "已解除接管。你現在可以重新登入帳號。", None
            return "解除碼不正確，或嘗試太頻繁。請稍後再試。", None
        if command == "/handover":
            if len(words) != 3:
                return "用法：/handover 群組ID 接任者ID", None
            token, details = await self.engine.prepare_handover(actor, int(words[1]), int(words[2]))
            return details + "\n請在五分鐘內確認。", [[button("確認交接", "handover:" + token)]]
        if callback and callback.startswith("handover:"):
            self.engine.confirm_handover(actor, callback.split(":", 1)[1])
            return "已收到交接確認。可用 /handover_status 查看結果。", None
        if command == "/handover_status" and self.store.trusted(actor):
            rows = self.store.rows(
                "SELECT group_id,target,state,result FROM handovers WHERE actor=? ORDER BY rowid DESC LIMIT 10",
                (actor,),
            )
            return "\n".join(
                f"{r['group_id']} → {r['target']}：{r['state']} {r['result'] or ''}" for r in rows
            ) or "沒有交接紀錄。", None
        if command == "/groups" and self.store.trusted(actor) and self.engine.active():
            groups = self.store.rows("SELECT id,name FROM groups ORDER BY id")
            return "\n".join(
                f"{g['name']}：{g['id']}" for g in groups
            ) or "沒有獲授權的群組。", None
        if actor != self.config.owner_id:
            return "持有解除碼可使用 /recover。已授權的親友可使用 /handover。", None
        action = callback or (command.removeprefix("/") if command.startswith("/") else "")
        if action in ("start", "home", "cancel", "help"):
            self.draft(None)
            return self.status(), HOME
        if action == "status":
            return self.status(), HOME
        if action == "heartbeat":
            self.engine.heartbeat(when)
            return "已確認。" + "\n" + self.status(), HOME
        if action == "arm":
            if not self.store.get("recovery_saved"):
                raise ValueError("請先儲存解除碼，並按「我已保管好」。")
            await self.engine.arm()
            self.draft(None)
            return "已啟用。" + "\n" + self.status(), HOME
        if action == "pause":
            if self.engine.active():
                raise ValueError("接管期間請使用 /recover 解除碼。")
            with self.store.db:
                self.store.pause()
            return "已暫停。重新啟用後會重新計算 72 小時。", HOME
        if action == "activity":
            return self.activity(), BACK
        self.editable()
        if action == "newcode":
            return "設定新解除碼後，舊碼會立即失效。", [
                [button("產生隨機碼", "generatecode"), button("自行設定", "customcode")],
                *BACK,
            ]
        if action == "generatecode":
            code = self.store.set_recovery()
            return self.code_reply(code)
        if action == "customcode":
            self.draft({"kind": "code"})
            return "輸入 20–256 字元的解除碼，不要包含空白。", CANCEL
        if action.startswith("savedcode:"):
            recovery = self.store.get("recovery")
            if not recovery or action.split(":", 1)[1] != recovery["salt"]:
                raise ValueError("這個解除碼已失效，請保管最新的解除碼。")
            with self.store.db:
                self.store.put("recovery_saved", True)
            return "已確認保管。你可以啟用 Heartbeat。", HOME
        if action.startswith(("messages:", "people:", "groups:")):
            kind, page = action.split(":")
            return self.list_items(kind, max(0, int(page)))
        if action == "addmessage":
            self.draft({"kind": "message", "step": "recipient", "message": {}})
            return (
                "訊息要留給誰？輸入 @username 或 Telegram ID。\n如要設定其他人的首次私訊回覆，輸入 *。",
                CANCEL,
            )
        if action.startswith("message:"):
            message = self.message(int(action.split(":")[1]))
            return self.describe(message) + "\n\n" + message["body"], [
                [
                    button("編輯", f"edit:{message['id']}"),
                    button("刪除", f"delete_message:{message['id']}"),
                ],
                *BACK,
            ]
        if action.startswith("edit:"):
            message = self.message(int(action.split(":")[1]))
            self.draft({"kind": "message", "step": "recipient", "message": {"id": message["id"]}})
            return "重新輸入收件人的 @username 或 ID。\n輸入 * 設定通用首次私訊回覆。", CANCEL
        if action.startswith("delete_message:"):
            ident = int(action.split(":")[1])
            self.message(ident)
            return f"刪除訊息 #{ident}？", [[button("刪除", f"remove_message:{ident}")], *BACK]
        if action.startswith("remove_message:"):
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM messages WHERE id=?", (int(action.split(":")[1]),)
                )
            return "已刪除。", BACK
        if action == "addperson":
            self.draft({"kind": "person", "step": "person"})
            return "輸入聯絡人的 @username 或 Telegram ID。", CANCEL
        if action.startswith("person:"):
            ident = int(action.split(":")[1])
            self.draft({"kind": "person", "step": "role", "id": ident})
            return "設定聯絡人身份：", self.roles()
        if action.startswith("remove_person:"):
            ident = int(action.split(":")[1])
            with self.store.db:
                self.store.db.execute("UPDATE people SET role='recipient' WHERE id=?", (ident,))
            self.draft(None)
            return "已移出密友／親友名單，預留訊息仍會保留。", BACK
        if action == "addgroup":
            self.draft({"kind": "group"})
            return "輸入要交接的群組 ID 或公開連結。\n也可以從帳號的群組列表選擇。", [
                [button("查看群組", "dialogs")],
                *CANCEL,
            ]
        if action == "dialogs":
            dialogs = await self.telegram.client.get_dialogs(limit=100)
            rows = []
            for dialog in dialogs:
                entity = dialog.entity
                if getattr(entity, "creator", False) or getattr(
                    getattr(entity, "admin_rights", None), "add_admins", False
                ):
                    rows.append([button(dialog.name[:35], f"addgroup:{dialog.id}")])
            return "選擇群組，或直接輸入群組 ID。", rows[:50] + CANCEL
        if action.startswith("addgroup:"):
            return await self.add_group(action.split(":")[1])
        if action.startswith("group:"):
            ident = int(action.split(":")[1])
            return f"群組 {ident}\n親友可指定群內成員接任。", [
                [button("取消此群組的交接授權", f"remove_group:{ident}")],
                *BACK,
            ]
        if action.startswith("remove_group:"):
            with self.store.db:
                self.store.db.execute("DELETE FROM groups WHERE id=?", (int(action.split(":")[1]),))
            return "已取消交接授權。", BACK
        draft = self.store.get("draft")
        if draft:
            return await self.continue_draft(draft, text, callback)
        return "請從選單選擇操作。", HOME

    def code_reply(self, code):
        with self.store.db:
            self.store.put("recovery_saved", False)
        self.draft(None)
        return f"解除碼：\n{code}\n\n請另行保存，並交給能確認你情況的親友。", [
            [button("我已保管好", "savedcode:" + self.store.get("recovery")["salt"])]
        ]

    def roles(self):
        return [
            [
                button("收件人", "role:recipient"),
                button("密友", "role:friend"),
                button("親友", "role:family"),
            ],
            *CANCEL,
        ]

    def list_items(self, kind, page):
        table = {"messages": "messages", "people": "people", "groups": "groups"}[kind]
        rows = self.store.rows(f"SELECT * FROM {table} ORDER BY id LIMIT 11 OFFSET ?", (page * 10,))
        buttons = []
        for row in rows[:10]:
            if kind == "messages":
                label, key = f"#{row['id']} → {row['recipient']} {row['body'][:20]}", "message"
            else:
                label, key = (
                    row["name"][:25] + f" ({row['id']})",
                    "person" if kind == "people" else "group",
                )
            buttons.append([button(label, f"{key}:{row['id']}")])
        if page:
            buttons.append([button("上一頁", f"{kind}:{page - 1}")])
        if len(rows) > 10:
            buttons.append([button("下一頁", f"{kind}:{page + 1}")])
        add = {"messages": "addmessage", "people": "addperson", "groups": "addgroup"}[kind]
        buttons += [[button("新增", add)], *BACK]
        return ("選擇項目。" if rows else "尚未加入任何項目。"), buttons

    def message(self, ident):
        rows = self.store.rows("SELECT * FROM messages WHERE id=?", (ident,))
        if not rows:
            raise ValueError("找不到這則訊息。")
        return rows[0]

    def describe(self, message):
        triggers = {"activation": "接管時", "first_dm": "接管後首次私訊", "schedule": "定時發送"}
        text = (
            f"收件人：{message['recipient'] or '其他私訊者'}\n方式：{triggers[message['trigger']]}"
        )
        if message["trigger"] == "schedule":
            text += f"\n首次延遲：{message['delay']} {'天' if message['unit'] == 'days' else '月'}"
            text += (
                f"\n之後每 {message['interval']} 個月，共 {message['count']} 次。"
                if message["interval"]
                else "\n只發送一次。"
            )
        return text

    async def add_group(self, reference):
        ident, name = await self.telegram.group(reference)
        self.editable()
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO groups VALUES (?,?)", (ident, name))
        self.draft(None)
        return f"已允許親友交接 {name}（{ident}）。", BACK

    async def continue_draft(self, draft, text, action):
        if draft["kind"] == "code" and not action:
            return self.code_reply(self.store.set_recovery(text.strip()))
        if draft["kind"] == "group" and not action:
            return await self.add_group(text.strip())
        if draft["kind"] == "person":
            if draft["step"] == "person" and not action:
                ident, name = await self.telegram.person(text.strip())
                self.editable()
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO people(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                        (ident, name),
                    )
                draft.update(id=ident, step="role")
                self.draft(draft)
                return f"{name}（{ident}）\n密友和親友可以執行群組交接。", self.roles()
            if draft["step"] == "role" and action in (
                "role:recipient",
                "role:friend",
                "role:family",
            ):
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE people SET role=? WHERE id=?", (action.split(":")[1], draft["id"])
                    )
                self.draft(None)
                return "已儲存。", BACK
        if draft["kind"] != "message":
            return "請按提示輸入，或取消後重來。", CANCEL
        message, step = draft["message"], draft["step"]
        if step == "recipient" and not action:
            if text.strip() == "*":
                message["recipient"] = 0
            else:
                ident, name = await self.telegram.person(text.strip())
                self.editable()
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO people(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                        (ident, name),
                    )
                message["recipient"] = ident
            draft["step"] = "body"
            reply = "請輸入要留下的訊息，可使用換行。"
        elif step == "body" and not action:
            if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
                raise ValueError("訊息須為 1–4096 字元。")
            message["body"], draft["step"] = text, "trigger"
            self.draft(draft)
            options = (
                [[button("接管時", "trigger:activation"), button("定時發送", "trigger:schedule")]]
                if message["recipient"]
                else []
            )
            return "甚麼時候發送？", options + [[button("首次私訊時", "trigger:first_dm")], *CANCEL]
        elif step == "trigger" and action in (
            "trigger:activation",
            "trigger:schedule",
            "trigger:first_dm",
        ):
            message.update(trigger=action.split(":")[1], delay=0, unit="days", interval=0, count=1)
            if not message["recipient"] and message["trigger"] != "first_dm":
                raise ValueError("通用回覆只適用於首次私訊。")
            if message["trigger"] != "schedule":
                return self.review(draft)
            draft["step"] = "delay"
            reply = "接管後多久發送第一則？例如：7 天、1 月。立即發送可輸入 0 天。"
        elif step == "delay" and not action:
            parts = text.strip().split()
            if len(parts) != 2 or parts[1] not in ("天", "月", "days", "months"):
                raise ValueError("請輸入數字和單位，例如 7 天或 1 月。")
            delay = int(parts[0])
            if not 0 <= delay <= 120:
                raise ValueError("請輸入 0–120。")
            message.update(delay=delay, unit="days" if parts[1] in ("天", "days") else "months")
            draft["step"] = "interval"
            reply = "之後每隔幾個月重複發送？輸入 0 表示只發一次。"
        elif step == "interval" and not action:
            interval = int(text.strip())
            if not 0 <= interval <= 120:
                raise ValueError("請輸入 0–120。")
            message["interval"] = interval
            if not interval:
                return self.review(draft)
            draft["step"] = "count"
            reply = "連同第一次，共發送幾次？輸入 1–120。"
        elif step == "count" and not action:
            count = int(text.strip())
            if not 1 <= count <= 120:
                raise ValueError("請輸入 1–120。")
            message["count"] = count
            return self.review(draft)
        elif step == "review" and action == "save_message":
            values = {
                k: message[k]
                for k in ("recipient", "body", "trigger", "delay", "unit", "interval", "count")
            }
            ident = self.store.add_message(**values, ident=message.get("id"))
            return f"已儲存訊息 #{ident}。", BACK
        else:
            return "請按提示輸入，或取消後重來。", CANCEL
        self.draft(draft)
        return reply, CANCEL

    def review(self, draft):
        draft["step"] = "review"
        self.draft(draft)
        return self.describe(draft["message"]) + "\n\n" + draft["message"]["body"], [
            [button("儲存", "save_message")],
            *CANCEL,
        ]

    def activity(self):
        deliveries = self.store.rows(
            "SELECT message_id,peer,state,error FROM deliveries ORDER BY rowid DESC LIMIT 15"
        )
        handovers = self.store.rows(
            "SELECT group_id,target,state,result FROM handovers ORDER BY rowid DESC LIMIT 10"
        )
        lines = [
            f"訊息 #{r['message_id']} → {r['peer']}：{r['state']} {r['error'] or ''}"
            for r in deliveries
        ]
        lines += [
            f"交接 {r['group_id']} → {r['target']}：{r['state']} {r['result'] or ''}"
            for r in handovers
        ]
        return "\n".join(lines) or "沒有發送或交接紀錄。"
